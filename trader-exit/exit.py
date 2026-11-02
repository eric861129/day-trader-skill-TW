#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Taiwan stocks and ETFs (TWSE and TPEx),
long side only. Standard library only. It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: 聯電 stop 108.5, target 111.5
    python3 exit.py plan --symbol 2303 --entry 110 --or-high 109.5 --or-low 107.5
    python3 exit.py plan --stop atr:1.5 --offline 2303.json # ATR needs the session's bars
    python3 exit.py plan --symbol 0050 --entry 104.1 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Every price sits on the tick grid (升降單位, twmarket.py): a stop snaps DOWN,
away from the entry; a target snaps to the nearest tick (half up); an entry
off the grid snaps up.

Stops:
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets: 2R (default), 1R, 1.5R ... (entry + R x risk), or price:X.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a stop less than 2 ticks under the entry (normal wiggle would take it), a
target above today's limit up, a stop below today's limit down, an
entry.json that has not triggered (unless you give --entry), an entry at or
after flat_by, a short (longs only).

Today's limits (漲跌停): --mis FILE (a saved TWSE MIS quote), else
--prev-close X, else the --offline chart's previous close, else, for today
and online, the MIS quote (or Yahoo's previous close, +-10 %).

Costs: the broker fee on the buy and on the sale plus the day-trade tax
(0.15 %, ETFs 0.1 %), with fee_discount from trader/config.json, in NT$ a
share and in R, and reward:risk after them.

flat_by (default 13:20, or "flat_by" in trader/config.json): out by then, win
or lose, before the 13:25 closing auction. "min_reward_risk" (default 1.0)
only warns here; trader-risk enforces it.

A stop is a plan, not a guarantee: a gap or a stock locked at limit down can
fill past it, or not at all.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": tw.FLAT_BY, "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_TICKS = 2                 # one tick is the spread: a 1-tick stop is inside normal wiggle
COST_WARN_R = Decimal("0.25")
MILLI = Decimal("0.001")
CENT = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "停損是計畫，不是保證：跳空、急殺或跌停鎖死時，成交價可能比停損差，甚至賣不掉。"
ODD_LOT_NOTE = ("這份計畫是整股當沖。零股目前不能現股當沖：今天買的零股最快下一個交易日才能賣，"
                "當天的停損和停利都用不上。")
NUMERALS = {1: "一", 2: "兩", 3: "三", 4: "四", 5: "五"}


class PlanError(Exception):
    pass


class NoData(PlanError):
    pass


# ---------------------------------------------------------------- time (Taipei, from twmarket)

def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is Taipei time; a bare HH:MM is today."""
    t = (text or "").strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (tw.to_taipei(tw.utc_now()).date().isoformat(), t.zfill(5))
    try:
        return tw.parse_now(t)
    except ValueError as e:
        raise PlanError(str(e))


def parse_hhmm(text, what):
    try:
        return tw.parse_hhmm(text, what)
    except ValueError as e:
        raise PlanError(str(e))


def parse_clock(text, what):
    """09:20, 9:20, 1:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    bad = PlanError("%s 要像 09:20 這樣的時間，不是 %r" % (what, text))
    if not m:
        raise bad
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise bad
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise bad
    return datetime.time(h, mi)


hm = tw.hm


# ---------------------------------------------------------------- price math (Decimal, on the tick grid)

def D(x, what="這個值"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().replace(",", "").replace("NT$", "").lstrip("$"))
    except (InvalidOperation, ValueError):
        raise PlanError("%s 不是數字：%r" % (what, x))


def px(x, what="價格"):
    """A price from Yahoo or a flag -> Decimal, with float noise cut at 0.001."""
    return D(x, what).quantize(MILLI, rounding=ROUND_HALF_UP)


def num(x):
    """1, 1.5, 0.4785: a Decimal without trailing zeros."""
    return format(D(x).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.27."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def pct_text(rate):
    """Decimal("0.0015") -> '0.15%'."""
    return num(D(rate) * 100) + "%"


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop 是 or-mid、or-low、price:X、pct:X 或 atr:K（收到 %r）" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s 要一個大於 0 的數字" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X 的 X 要小於 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target 要一個大於 0 的 R 倍數，例如 1R 或 2R")
        return "r", r
    m = re.match(r"^price:\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        v = D(m.group(1), "--target")
        if v <= 0:
            raise PlanError("--target price:X 要一個大於 0 的價格")
        return "price", v
    raise PlanError("--target 是 2R、1R、1.5R ... 或 price:X（收到 %r）" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    if n in NUMERALS:
        return "%sR（%s倍風險）" % (num(r), NUMERALS[n])
    return "%sR（%s 倍風險）" % (num(r), num(r))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, code=""):
    """The stop, the target and the risk on the tick grid. Raises PlanError with the reason."""
    fp = lambda x: tw.fmt_price(x, code)
    e_raw = px(entry, "進場價")
    if e_raw <= 0:
        raise PlanError("進場價要大於 0")
    e = tw.to_tick(e_raw, code, "up")
    entry_note = ""
    if e != e_raw:
        entry_note = "進場價 %s 不在升降單位上，往上取到 %s" % (num(e_raw), fp(e))
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("%s 停損需要開盤區間（--or-high 和 --or-low）" % stop_rule.replace("_", "-"))
        oh, ol = px(or_high), px(or_low)
        if oh <= ol:
            raise PlanError("開盤區間高點 %s 要高於低點 %s" % (num(oh), num(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "開盤區間中點"
            detail = "(%s + %s) / 2 = %s" % (fp(oh), fp(ol), num(raw))
        else:
            raw, label = ol, "開盤區間低點"
    elif stop_rule == "price":
        raw, label = stop_param, "你的價格"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "進場價下方 %s%%" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (fp(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("atr 停損需要這個交易日的 K 棒")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "進場價下方 %s 倍 ATR(%d)" % (num(stop_param), atr_bars)
        detail = "ATR %s，算到進場那根為止的 %d 根 K 棒%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, "（還不到 14 根）" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("不認得的停損規則 %r" % stop_rule)
    if raw <= 0:
        raise PlanError("停損算出來是 %s：請選一個高於 0 的停損" % num(raw))
    stop = tw.to_tick(raw, code, "down")
    if stop != raw:
        if stop_rule == "price":
            detail = "%s 不在升降單位上，往下取到 %s" % (num(raw), fp(stop))
        elif detail:
            detail += "，往下取到升降單位 %s" % num(tw.tick_size(stop, code))
        else:
            detail = "%s 往下取到升降單位 %s" % (num(raw), num(tw.tick_size(stop, code)))
    if stop >= e:
        raise PlanError("停損 %s（%s）在進場價 %s 或以上：做多的停損要放在進場價下方" % (fp(stop), label, fp(e)))
    risk = e - stop
    ticks = tw.tick_steps(stop, e, code)
    if ticks < MIN_RISK_TICKS:
        raise PlanError("停損只差 %d 檔（%s 到 %s），正常跳動就會掃到。把停損放遠一點，至少差 %d 檔。"
                        % (ticks, fp(stop), fp(e), MIN_RISK_TICKS))
    raw_t = e + target_param * risk if target_rule == "r" else target_param
    target = tw.to_tick(raw_t, code, "nearest")
    if target <= e:
        raise PlanError("停利 %s 在進場價 %s 或以下：做多的停利要放在進場價上方" % (fp(target), fp(e)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    t_detail = ""
    if target_rule == "r":
        t_label = r_label(target_param)
        if target != raw_t:
            t_detail = "%s 取到最近的升降單位" % num(raw_t)
            if rr != D(target_param).quantize(CENT):
                t_detail += "，實際 %sR" % ratio(rr)
    else:
        t_label = "你的價格（%sR）" % ratio(rr)
        if target != raw_t:
            t_detail = "%s 不在升降單位上，取到最近的 %s" % (num(raw_t), fp(target))
    return {"entry": e, "entry_note": entry_note, "stop": stop, "stop_label": label, "stop_detail": detail,
            "target": target, "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "target_detail": t_detail, "risk": risk, "risk_ticks": ticks, "reward_risk": rr}


def check_limits(c, limits, code=""):
    """Refuse what today's price limits make impossible; return warnings."""
    fp = lambda x: tw.fmt_price(x, code)
    warns = []
    up, down = limits.get("limit_up"), limits.get("limit_down")
    if down is not None:
        down = px(down)
        if c["stop"] < down:
            raise PlanError("停損 %s 低於今天的跌停價 %s：價格跌不破跌停，停損永遠不會觸發，跌停鎖死時也賣不掉。"
                            "把停損放在跌停價以上" % (fp(c["stop"]), fp(down)))
        if c["stop"] == down:
            warns.append("停損就在跌停價：跌停鎖死時賣單要排隊，不一定賣得掉")
    if up is not None:
        up = px(up)
        if c["entry"] >= up:
            raise PlanError("進場價 %s 已經在今天的漲停價 %s 或以上：上面沒有空間放停利，漲停鎖住時買單也要排隊"
                            % (fp(c["entry"]), fp(up)))
        if c["target"] > up:
            raise PlanError("停利 %s 高於今天的漲停價 %s：碰不到。停利放在漲停價以內，例如 --target price:%s 或小一點的 R"
                            % (fp(c["target"]), fp(up), fp(up)))
    return warns


def costs(c, code, cfg, day):
    """The round trip a share at the entry price: the fee twice plus the day-trade tax, ignoring the fee minimum."""
    fees = tw.fee_settings(cfg)
    rate = tw.cost_rate(code, True, fees, day)
    per = (c["entry"] * rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    cost_r = (per / c["risk"]).quantize(CENT, rounding=ROUND_HALF_UP)
    net = ((c["target"] - c["entry"] - per) / (c["risk"] + per)).quantize(CENT, rounding=ROUND_HALF_UP)
    tax = tw.sell_tax_rate(code, True, day)
    fee = "手續費 %s 買賣各一次" % pct_text(tw.FEE_RATE)
    if fees["discount"] != 1:
        fee += "（打 %s 折）" % num(fees["discount"] * 10)
    if tw.is_etf_like(code):
        tax_text = "ETF 證交稅 %s" % pct_text(tax) if tax else "債券 ETF 免證交稅"
    elif tax == tw.DAY_TRADE_TAX_RATE:
        tax_text = "當沖證交稅 %s" % pct_text(tax)
    else:
        tax_text = "證交稅 %s" % pct_text(tax)
    return {"per": per, "cost_r": cost_r, "net": net, "detail": "%s、%s" % (fee, tax_text)}


# ---------------------------------------------------------------- the network (Yahoo bars, the MIS quote)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": tw.UA, "Accept": "application/json"})
    with tw.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "沒有回應"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise NoData("Yahoo 沒有 %s 的圖表（HTTP 404）" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo 回了 HTTP %d：%s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "連不上：%s" % (e.reason,)
                break
            except OSError as e:
                last = "網路錯誤：%s" % (e,)
                break
    raise PlanError("Yahoo 沒有回應（query2 和 query1 都是 %s）：等一分鐘再試" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_chart(text, code):
    """A Yahoo v8 chart -> {bars, data_time, market, range, prev_close}."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo 給的 %s 資料看不懂" % code)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise NoData("Yahoo 沒有 %s 的資料：%s" % (code, err.get("description") or "查無此代號"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    sym = str(meta.get("symbol") or "")
    market = None
    if sym:
        try:
            mcode, market = tw.parse_symbol(sym)
        except ValueError:
            mcode = sym
        if mcode != code:
            raise PlanError("圖表檔是 %s，不是 %s" % (sym, code))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime"), "market": market,
            "range": meta.get("range"), "prev_close": meta.get("chartPreviousClose")}


def load_chart(code, market, interval, day, offline=None, today=None):
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, code + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("找不到離線檔 %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("讀不了 %s：%s" % (path, e))
        return parse_chart(text, code)
    back = ((today or day) - day).days
    rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
    if interval == "1m" and back > 6:
        raise PlanError("Yahoo 的 1 分 K 只留大約一週")
    last = None
    for m in ([market] if market else ["TWSE", "TPEx"]):
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (tw.yahoo_symbol(code, m), rng, interval))
        try:
            return parse_chart(text, code)
        except NoData as e:
            last = e
    raise last


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, cal=None):
    """Closed, on-grid bars of continuous trading (09:00 to 13:25 Taipei), oldest first.
    The live tick, the empty 13:25 auction bar and the 13:30 close print are dropped."""
    step = INTERVALS[interval]
    if not (cal or tw.Calendar()).is_trading_day(day):
        return []
    open_dt = datetime.datetime.combine(day, tw.OPEN)
    end_dt = datetime.datetime.combine(day, tw.CLOSING_AUCTION)
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = tw.to_taipei(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        lt = tw.ts_to_taipei(b["t"])
        if lt.date() != day or not (open_dt <= lt < end_dt):
            continue
        if local and lt + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((lt - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


def mis_get(url):
    return _open_url(url)


def _from_quote(q, code):
    lim = {"prev_close": q.get("prev_close"), "limit_up": q.get("limit_up"), "limit_down": q.get("limit_down"),
           "limits_source": "mis", "name": q.get("name"), "market": q.get("market"), "note": ""}
    if lim["limit_up"] is None or lim["limit_down"] is None:
        lim["limit_up"] = lim["limit_down"] = None
        lim["note"] = "MIS 沒有給 %s 的漲跌停價（國外成分 ETF 沒有漲跌幅限制）：停損和停利沒有對照漲跌停" % code
    return lim


def _computed(prev, code):
    up, down = tw.price_limits(prev, code)
    lim = {"prev_close": float(px(prev)), "limit_up": float(up), "limit_down": float(down),
           "limits_source": "computed", "note": ""}
    if tw.is_etf_like(code):
        lim["note"] = ("ETF 的漲跌停是用昨收 %d%% 算的；國外成分 ETF 沒有漲跌幅限制，以 MIS 或券商顯示為準"
                       % tw.limit_pct(code))
    return lim


def get_limits(a, code, market, day, today, chart=None):
    """Today's limit up and down: {prev_close, limit_up, limit_down, limits_source, note, [name, market]}."""
    none = {"prev_close": None, "limit_up": None, "limit_down": None, "limits_source": None}
    if a.mis:
        try:
            quotes = tw.parse_mis(read_text(a.mis))
        except (OSError, ValueError) as e:
            raise PlanError("讀不了 %s：%s" % (a.mis, e))
        q = quotes.get(code)
        if not q:
            raise PlanError("%s 裡沒有 %s 的報價" % (a.mis, code))
        if q.get("date") and q["date"] != day.strftime("%Y%m%d"):
            raise PlanError("%s 是 %s 的報價，不是計畫日 %s：漲跌停每天不同" % (a.mis, q["date"], day.isoformat()))
        return _from_quote(q, code)
    if a.prev_close is not None:
        return _computed(a.prev_close, code)
    if chart and chart.get("range") == "1d" and chart.get("prev_close") and chart["bars"] \
            and tw.ts_to_taipei(chart["bars"][0]["t"]).date() == day:
        return _computed(chart["prev_close"], code)
    if a.offline:
        return dict(none, note="離線沒有昨收：加 --mis 檔案或 --prev-close，才能對照漲跌停")
    if day != today:
        return dict(none, note="線上只查得到今天的漲跌停；%s 請加 --prev-close" % day.isoformat())
    try:
        pairs = [(code, market)] if market else [(code, "TWSE"), (code, "TPEx")]
        q = tw.fetch_mis(pairs, get=mis_get).get(code)
        if q:
            return _from_quote(q, code)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        ch = load_chart(code, market, "5m", day, None, today)
        if ch.get("prev_close"):
            lim = _computed(ch["prev_close"], code)
            lim["market"] = ch.get("market")
            return lim
    except PlanError:
        pass
    return dict(none, note="查不到今天的漲跌停價（MIS 和 Yahoo 都沒回應）：停損和停利沒有對照漲跌停")


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("%s 沒有 %s" % (path, what))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s 不是有效的 JSON：%s" % (path, e))


def read_config(path):
    """The whole config (fee keys included), with the defaults this skill needs."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "設定檔")
        for k, v in doc.items():
            if v is not None:
                cfg[k] = v
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, warnings). Out before the close; the 13:25 to 13:30 auction gets a warning."""
    flag = "--flat-by" if explicit else "flat_by"
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], flag)
    warns = []
    if t >= tw.CLOSE:
        raise PlanError("%s %s 在 13:30 收盤或之後：那時已經沒辦法出場" % (flag, hm(t)))
    if t <= tw.OPEN:
        raise PlanError("%s %s 在 09:00 開盤或之前" % (flag, hm(t)))
    if t >= tw.CLOSING_AUCTION:
        warns.append("最晚出場 %s 落在 13:25 到 13:30 的收盤集合競價：這段時間不連續撮合，13:30 一次成交，"
                     "價格要等收盤才知道" % hm(t))
    if entry_time and entry_time >= t:
        raise PlanError("進場時間 %s 已經在最晚出場 %s 或之後：沒有時間抱這筆單" % (hm(entry_time), hm(t)))
    return hm(t), warns


def _symbol(text):
    try:
        return tw.parse_symbol(text)
    except ValueError as e:
        raise PlanError(str(e))


def gather(a, universe):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("--from 和 --symbol 只能擇一")
    if a.side in ("sell", "short"):
        raise PlanError("只做多：不規劃放空的停損。請改成做多的計畫。")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol 要搭配 --entry 價格")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("--or-high 和 --or-low 要一起給，或都不給")
        code, market = _symbol(a.symbol)
        info = {"symbol": code, "market": market, "name": None, "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "進場紀錄（先跑 /trader-entry，或給 --symbol 和 --entry）")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s 是 %s：只做多" % (path, doc.get("side")))
        if not doc.get("symbol"):
            raise PlanError("%s 沒有代號或價格" % path)
        code, market = _symbol(doc.get("symbol"))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s 說 %s 是 %s，還沒觸發：等觸發再規劃，或給 --entry 價格先規劃"
                            % (path, code, doc.get("status") or "狀態不明"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": code, "market": doc.get("market") or market, "name": doc.get("name"),
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if info["entry"] is None:
            raise PlanError("%s 沒有代號或價格" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("K 棒是 5m 或 1m")
    known = universe.get(info["symbol"]) or {}
    info["market"] = info["market"] or known.get("market")
    info["name"] = info["name"] or known.get("name") or None
    return info


def build_plan(a, now_utc):
    cfg = read_config(a.config)
    trader_dir = os.path.dirname(a.config) or "."
    cal = tw.load_calendar(trader_dir, cfg)
    info = gather(a, tw.load_universe(os.path.join(trader_dir, "universe.txt")))
    code = info["symbol"]
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("%s 型態沒有開盤區間可以當預設停損：請給 --stop price:X、pct:X 或 atr:K" % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    today = tw.to_taipei(now_utc).date()
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("日期要像 2026-11-02，不是 %r" % info["date"])
    else:
        day = today
    entry_time = parse_clock(info["entry_time"], "進場時間") if info["entry_time"] else None
    chart = atr = atr_n = None
    if stop_rule == "atr" or a.offline:
        chart = load_chart(code, info["market"], info["interval"], day, a.offline, today)
        info["market"] = info["market"] or chart.get("market")
    if stop_rule == "atr":
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None, cal)
        if len(bars) < 3:
            raise PlanError("atr 停損需要 %s 在 %s 進場前至少 3 根已收盤的 %s K 棒；只找到 %d 根"
                            % (code, day, info["interval"], len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, code)
    lim = get_limits(a, code, info["market"], day, today, chart)
    info["market"] = info["market"] or lim.get("market")
    info["name"] = info["name"] or lim.get("name") or None
    limit_warns = check_limits(c, lim, code)
    cost = costs(c, code, cfg, day)
    flat_by, flat_warns = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(px(info["or_high"])), "low": float(px(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": code, "market": info["market"], "name": info["name"],
            "side": "buy", "date": day.isoformat(), "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick": float(tw.tick_size(c["entry"], code)), "risk_ticks": c["risk_ticks"],
            "prev_close": lim["prev_close"], "limit_up": lim["limit_up"], "limit_down": lim["limit_down"],
            "limits_source": lim["limits_source"],
            "cost_per_share": float(cost["per"]), "cost_r": float(cost["cost_r"]),
            "reward_risk_net": float(cost["net"]), "cost_detail": cost["detail"],
            "stop_detail": c["stop_detail"], "target_detail": c["target_detail"],
            "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if c["entry_note"]:
        warnings.append(c["entry_note"] + "。")
    if cost["cost_r"] >= COST_WARN_R:
        warnings.append("成本約 %sR：停損只有 %d 檔時，手續費加證交稅占了每股風險的 %s%%。扣成本後的報酬風險比是 %s，"
                        "不是 %s。停損越近，成本占的比例越大。"
                        % (ratio(cost["cost_r"]), c["risk_ticks"], num((cost["cost_r"] * 100).normalize()),
                           ratio(cost["net"]), ratio(c["reward_risk"])))
    warnings.extend(w + "。" for w in limit_warns)
    if lim.get("note"):
        warnings.append(lim["note"] + "。")
    warnings.extend(w + "。" for w in flat_warns)
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("報酬風險比 %s 低於設定的 min_reward_risk %s：trader-risk 會檢查這一項。"
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    why = cal.closed(day)
    if why:
        warnings.append("%s 不是交易日（%s）。" % (day.isoformat(), why))
    unknown = cal.unknown_note(day)
    if unknown:
        warnings.append(unknown + "。")
    if day != today:
        warnings.append("這份計畫是 %s 的，不是今天（%s）。" % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    code = plan["symbol"]
    fp = lambda x: tw.fmt_price(x, code)
    who = "%s %s" % (plan["name"], code) if plan.get("name") else code
    setup = {"orb": "開盤區間突破", "level": "收盤站上關卡價"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    step = 5 if plan.get("interval", "5m") == "5m" else 1
    when = ""
    if plan.get("entry_time"):
        when = "，%s" % plan["entry_time"]
        t = parse_clock(plan["entry_time"], "進場時間")
        start = (datetime.datetime.combine(day, t) - datetime.timedelta(minutes=step)).time()
        when += "（%s 那根 K 棒收盤" % hm(start)
        when += "，編號 %d）" % plan["entry_bar"] if plan.get("entry_bar") is not None else "）"
    out = ["出場計畫：%s 做多，%s，%s，%s" % (who, tw.day_label(day), setup, "5 分 K" if step == 5 else "1 分 K")]
    out.append("  進場          %s%s" % (fp(plan["entry"]), when))
    out.append("  停損          %s  %s%s" % (fp(plan["stop"]), plan["stop_label"],
                                             ("：" + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  停利          %s  %s%s" % (fp(plan["target"]), plan["target_label"],
                                             ("：" + plan["target_detail"]) if plan.get("target_detail") else ""))
    ticks = "，%d 檔" % plan["risk_ticks"] if plan.get("risk_ticks") is not None else ""
    out.append("  每股風險      %s（進場價的 %s%%%s）" % (num(risk), pct, ticks))
    out.append("  報酬風險比    %s" % ratio(plan["reward_risk"]))
    if plan.get("cost_per_share") is not None:
        out.append("  成本          每股約 %s（%s），約 %sR；扣成本後報酬風險比 %s" % (
            "%.2f" % plan["cost_per_share"], plan.get("cost_detail") or "手續費加證交稅", ratio(plan["cost_r"]),
            ratio(plan["reward_risk_net"])))
    if plan.get("limit_up") is not None and plan.get("limit_down") is not None:
        src = {"mis": "MIS 報價", "computed": "用昨收 %s 算的" % fp(plan["prev_close"]) if plan.get("prev_close") else "算的"}
        out.append("  漲跌停        %s / %s（%s）" % (fp(plan["limit_up"]), fp(plan["limit_down"]),
                                                   src.get(plan.get("limits_source"), "")))
    else:
        out.append("  漲跌停        沒有對照（見下方說明）")
    out.append("  最晚出場      %s，不論輸贏都出場，不留倉" % plan["flat_by"])
    out.append("進場前就設好。價格碰到停損就出場；停損只能往進場價靠近，絕不往遠處移。")
    out.append(NOTE)
    out.append(ODD_LOT_NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        tw.write_json(a.write, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), ensure_ascii=False, indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("注意：" + w)
    if a.write:
        print("已寫入 %s。" % a.write)
    print("下一步：/trader-timemachine 重播這份計畫，再用 /trader-budget 依你的單筆最大虧損算股數。什麼都沒有下單。")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("%s 還沒有計畫。先做一份：exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "計畫")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s 少了 %r：用 exit.py plan --write 重做一份" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = tw.to_taipei(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("注意：這份計畫是 %s 的，不是今天（%s）。" % (plan["date"], today))
    return 0


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="出場規劃（trader-exit）：進場前先設好停損和停利。")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="設定停損和停利")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="從哪個 entry.json 規劃（預設 trader/entry.json）")
    p.add_argument("--symbol", help="代號，例如 2303、2303.TW、6488.TWO")
    p.add_argument("--entry", type=float, help="進場價（搭配 --from 時是預先規劃的價格）")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="交易日 YYYY-MM-DD（預設：進場紀錄的日期，或今天）")
    p.add_argument("--time", help="進場時間，例如 09:20")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="只做多")
    p.add_argument("--stop", help="or-mid（orb 預設）、or-low、price:X、pct:X、atr:K")
    p.add_argument("--target", help="2R（預設）、1R、1.5R、price:X")
    p.add_argument("--flat-by", help="HH:MM 台北時間（預設：設定檔的 flat_by，13:20）")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="存下來的 Yahoo 圖表 JSON（atr 停損、昨收）；不連網")
    p.add_argument("--mis", metavar="FILE", help="存下來的證交所 MIS 報價 JSON（當天的漲跌停價）")
    p.add_argument("--prev-close", type=float, help="昨收（參考價），用來算 ±10%% 漲跌停")
    p.add_argument("--now", help='假裝現在是這個台北時間，例如 "2026-11-02 09:20"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="存下計畫（預設 trader/plan.json）")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="印出目前的計畫")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
