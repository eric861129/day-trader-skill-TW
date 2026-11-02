#!/usr/bin/env python3
"""Entry Timer for trader-entry (Taiwan): watches a setup bar by bar and says
the moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check 2303                          # opening-range breakout, 5-minute bars
    python3 entry.py check 2303 --now "2026-11-02 09:20" --offline 2303.json
    python3 entry.py check 2303 --setup level --level 111.5     # first close above a price
    python3 entry.py check 6488.TWO --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch 2303 --every 30 --until 13:00  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Symbols: 2303, 2303.TW (TWSE) or 6488.TWO (TPEx). A bare code takes its market
from trader/universe.txt (trader-scanner writes it), else Yahoo is asked for
.TW first and .TWO when .TW has no data.

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:00, 09:05 and 09:10 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 09:00 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 09:15 bar closes at
09:20) and its price is that close, on the tick grid. Only bars on the interval
grid count (Yahoo's live tick, e.g. 10:49:45, is dropped), a bar counts only
once it has closed (now >= its start + the interval), and only bars of
continuous trading count: 09:00 to 13:25. Yahoo leaves the 13:25 closing
auction empty and adds a 13:30 print of the close with no volume; neither is a bar.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 13:00);
no-data (the market is closed today, or Yahoo sent no bars). Closed days come
from the TWSE calendar in twmarket.py, the cached years in trader/state and
"extra_closed" in trader/config.json (a typhoon day).

trader/config.json (optional): "no_entries_before" (09:15) and
"no_entries_after" (13:00), Taipei time.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/2303.TW?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<CODE>.json (or <CODE>.TW.json, <CODE>.TWO.json) when PATH is a folder.
  Without --now, an offline file is read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": tw.NO_ENTRIES_BEFORE, "no_entries_after": tw.NO_ENTRIES_AFTER}
EPOCH = datetime.datetime(1970, 1, 1)
NOTHING = "什麼單都沒有下。"


class EntryError(Exception):
    pass


class NoData(EntryError):
    """Yahoo knows no such symbol (or has no bars for it): try the other market."""


# ---------------------------------------------------------------- time

def utc_now():
    return tw.utc_now()


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-11-02 09:20" (or just 09:20 for today) is
    Taipei time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (tw.to_taipei(utc_now()).date().isoformat(), t.zfill(5))
    try:
        return tw.parse_now(t)
    except ValueError as e:
        raise EntryError(str(e))


def parse_hhmm(text, what):
    try:
        return tw.parse_hhmm(text, what)
    except ValueError:
        raise EntryError("%s 要像 13:00 這樣的台北時間，不是 %r" % (what, text))


def hm(t):
    """09:15, 13:00: the contract's wall-clock form, also used in sentences."""
    return tw.hm(t)


def next_open(cal, d):
    try:
        return "%s %s" % (cal.next_trading_day(d).isoformat(), hm(tw.OPEN))
    except ValueError:
        return None


# ---------------------------------------------------------------- symbols

def resolve(text, universe=None, market=None, name=None):
    """'2303', '2303.TW', '6488.TWO' -> (code, market or None, name or None)."""
    try:
        code, m = tw.parse_symbol(text)
    except ValueError as e:
        raise EntryError(str(e))
    u = (universe or {}).get(code) or {}
    return code, m or market or u.get("market") or None, name or u.get("name") or None


# ---------------------------------------------------------------- Yahoo

def chart_path(ysym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo 的 1 分 K 只保留大約一週：%s 請用 --interval 5m" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo 的 5 分 K 只保留大約 60 天：%s 太久以前了" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": tw.UA, "Accept": "application/json"})
    with tw.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
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
                    raise NoData("Yahoo 在 %s 找不到這檔的線圖（HTTP 404）" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo 對 %s 回答 HTTP %d" % (url.split("?")[0], code))
            except urllib.error.URLError as e:
                last = "連不上：%s" % (e.reason,)
                break
            except OSError as e:
                last = "網路錯誤：%s" % (e,)
                break
    raise EntryError("Yahoo 沒有回應（query2 和 query1 都是 %s）：等一分鐘再試" % last)


def parse_chart(text, ysym):
    """Yahoo chart JSON v8 -> {symbol, market, yahoo, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here (the 13:25 auction bar); off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo 回傳了看不懂的內容（%s）" % ysym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise NoData("Yahoo 沒有 %s 的資料：%s" % (ysym, err.get("description") or "查無此代號"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    ysym = str(meta.get("symbol") or ysym).upper()
    try:
        code, market = tw.parse_symbol(ysym)
    except ValueError:
        raise EntryError("%s 不是台股的線圖（.TW 或 .TWO）" % ysym)
    return {"symbol": code, "market": market, "yahoo": ysym, "name": None, "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, code):
    if os.path.isdir(path):
        stems = [code, code + ".TW", code + ".TWO"]
        for name in [s + ext for s in stems for ext in (".json", ".json.gz")]:
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("%s 裡沒有 %s 的離線檔（要 %s.json）" % (path, code, code))
    if not os.path.exists(path):
        raise EntryError("沒有離線檔 %s" % path)
    return path


def get_chart(code, interval, day=None, offline=None, today=None, market=None, name=None):
    """Bars for one stock, from Yahoo (range=1d for today) or a saved file.
    Without a market, Yahoo is asked for .TW first and .TWO when .TW has no data."""
    if offline:
        f = offline_file(offline, code)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("讀不到 %s：%s" % (f, e))
        ch = parse_chart(text, tw.yahoo_symbol(code, market))
    else:
        today = today or tw.to_taipei(utc_now()).date()
        markets = [market] if market else ["TWSE", "TPEx"]
        ch = None
        for i, m in enumerate(markets):
            text = yahoo_get(chart_path(tw.yahoo_symbol(code, m), chart_range(day or today, today, interval),
                                        interval))
            try:
                ch = parse_chart(text, tw.yahoo_symbol(code, m))
                break
            except NoData:
                if i == len(markets) - 1:
                    raise
    if ch["symbol"] != code:
        raise EntryError("這個線圖檔是 %s，不是 %s" % (ch["symbol"], code))
    if market and ch["market"] != market:
        raise EntryError("這個線圖檔是 %s（%s），不是 %s" % (ch["yahoo"], ch["market"], tw.yahoo_symbol(code, market)))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s 的檔案是 %s K 棒，不是 %s：改用 --interval %s" % (code, gran, interval, gran))
    ch["name"] = name
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    """(the entry window, the whole config doc for the calendar)."""
    cfg, doc = dict(DEFAULTS), {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s 不是有效的 JSON：%s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg, doc


def trader_dir(config_path):
    return os.path.dirname(config_path or "") or "trader"


def snap(x, code):
    """A chart price onto the tick grid (Yahoo sends floats like 109.4999)."""
    return None if x is None else float(tw.to_tick(x, code, "nearest"))


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, cal=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    cal = cal or tw.Calendar()
    if interval not in INTERVALS:
        raise EntryError("--interval 只能是 5m 或 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes 必須是整數根 %s K 棒" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup 只能是 orb 或 level")
    if setup == "level" and level is None:
        raise EntryError("--setup level 需要 --level 價格（例如 20 日新高）")
    code = chart["symbol"]
    n_or = or_minutes * 60 // step
    local = tw.to_taipei(now_utc)
    day = local.date()
    res = {"symbol": code, "market": chart.get("market"), "name": chart.get("name"), "setup": setup,
           "side": "buy", "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "limit_up": None, "limit_down": None,
           "calendar_known": cal.known(day.year), "calendar_note": cal.unknown_note(day), "_bars": []}
    if res["prev_close"]:
        up, down = tw.price_limits(res["prev_close"], code)
        res["limit_up"], res["limit_down"] = float(up), float(down)
    dt = chart.get("data_time")
    res["data_time"] = tw.ts_to_taipei(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    why = cal.closed(day)
    if why:
        res.update(status="no-data", phase="closed", reason="今天休市（%s）" % why, next_open=next_open(cal, day))
        return res
    open_dt = datetime.datetime.combine(day, tw.OPEN)
    auction_dt = datetime.datetime.combine(day, tw.CLOSING_AUCTION)
    close_dt = datetime.datetime.combine(day, tw.CLOSE)
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(tw.OPEN), closes=hm(tw.CLOSE), no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = tw.ts_to_taipei(b["t"])
        if et.date() != day or not (open_dt <= et < auction_dt):
            continue                       # the 13:30 close print is not a bar of continuous trading
        slot = int((et - open_dt).total_seconds()) // step
        b = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
        for k in ("o", "h", "l", "c"):
            b[k] = snap(b[k], code)
        by_slot[slot] = b
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": b["c"]}
                res["lag_note"] = ("Yahoo 最後一筆成交在 %s，早於 %s K 棒收盤：等 Yahoo 跟上才算（最多一分鐘）"
                                   % (tw.ts_to_taipei(dt).strftime("%H:%M:%S"), hm(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": b["c"]}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="%s 開盤%s" % (
            hm(tw.OPEN), "，%d 分鐘開盤區間在 %s 定案" % (or_minutes, hm(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": max(b["h"] for b in or_bars), "low": min(b["l"] for b in or_bars),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d 根中已收 %d 根；區間在 %s 定案" % (n_or, len(or_bars), hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo 沒有開盤區間的 K 棒")
            return res
        lvl = res["opening_range"]["high"]
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": last["c"],
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="%s K 棒收在%s之上" % (hm(trig["et"]), "開盤區間高點" if setup == "orb" else "水準"))
        res["trigger"] = {"time": hm(trig["end"]), "bar_start": hm(trig["et"]), "bar_index": trig["slot"],
                          "price": trig["c"], "volume": trig["v"], "volume_x": r2(vx),
                          "pct_over": r2(100.0 * (trig["c"] / lvl - 1)),
                          "at_limit_up": bool(res["limit_up"] and trig["c"] >= res["limit_up"])}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < tw.taipei_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo 最後一筆成交在 %s，早於這根 K 棒收盤：收盤價可能還會差一檔"
                                % tw.ts_to_taipei(dt).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo 還沒有 %s 的 K 棒" % day.isoformat())
        return res
    px = lambda p: tw.fmt_price(p, code)
    what = ("開盤區間高點 %s" % px(lvl)) if setup == "orb" else ("水準 %s" % px(lvl))
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="到 %s 都沒有%s收在%s 之上的 K 棒，%s 之後不再新進場" % (
                       hm(cutoff), "夠量又" if thin else "", what, hm(cutoff)))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "%s K 棒收在 %s，站上了，但量只有區間均量的 %.2f 倍，低於你設的 %.2f 倍" % (
            hm(last["et"]), px(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "%s K 棒收在 %s，站上了，但 %s 之前不新進場（設定檔）" % (
            hm(last["et"]), px(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        head = "最後收盤 %s（%s K 棒）" % (px(last["c"]), hm(last["et"]))
        if gap == 0:
            res["reason"] = "%s，剛好等於%s，不算站上" % (head, what)
        else:
            ticks = ("%d 檔，" % abs(tw.tick_steps(last["c"], lvl, code))) if tw.on_tick(lvl, code) else ""
            res["reason"] = "%s，%s %s 元（%s%+.2f%%）" % (
                head, ("距%s 還差" if gap > 0 else "比%s 高") % what, fmt_gap(gap, lvl, code), ticks,
                100.0 * (last["c"] / lvl - 1))
    return res


def fmt_gap(gap, ref, code):
    """A price difference with the decimals of the tick at ref: 0.5 at 109.5, 0.05 at 45.25."""
    places = max(0, -tw.tick_size(ref, code).normalize().as_tuple().exponent)
    return "{:,.{n}f}".format(abs(gap), n=places)


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "market": res.get("market"),
            "name": res.get("name"), "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    tw.write_json(path, doc)


# ---------------------------------------------------------------- words

LABEL = {"triggered": "觸發", "waiting": "等待", "expired": "過期", "no-data": "沒有資料"}
PHASE_LABEL = {"pre-open": "還沒開盤", "forming": "開盤區間形成中", "closed": "休市"}
SHORT = {"pre-open": "未開盤", "forming": "區間形成", "closed": "休市", "triggered": "觸發",
         "waiting": "等待", "expired": "過期", "no-data": "沒資料"}
BARS = {"5m": "5 分 K", "1m": "1 分 K"}


def setup_name(res):
    if res["setup"] == "orb":
        return "開盤區間突破（%d 分鐘區間）" % res["or_minutes"]
    if res["level"] is not None:
        return "收在 %s 之上" % tw.fmt_price(res["level"], res["symbol"])
    return "收在水準之上"


def title(res):
    return ("%s %s" % (res["symbol"], res["name"])) if res.get("name") else res["symbol"]


def describe(res):
    """Plain lines for a person."""
    code = res["symbol"]
    px = lambda p: tw.fmt_price(p, code)
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    head = "%s  %s %s 台北  %s，%s（Yahoo 免費資料，可能延遲）" % (
        title(res), tw.day_label(when.date()), when.strftime("%H:%M"), setup_name(res), BARS[res["interval"]])
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += "，資料到 %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("開盤區間 %s-%s：高點 %s，低點 %s（%d 根 K 棒收了 %d 根%s）。" % (
            hm(start), hm(end), px(orr["high"]), px(orr["low"]), orr["of"], orr["bars"],
            "" if orr["complete"] else "，還在形成"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"])
    t = res.get("trigger")
    if t:
        out.append("觸發  %s，價格 %s：%s K 棒（bar %d）收在%s %s 之上（%+.2f%%%s）。" % (
            t["time"], px(t["price"]), t["bar_start"], t["bar_index"],
            "開盤區間高點" if res["setup"] == "orb" else "水準", px(res["level"]), t["pct_over"],
            ("，量是區間均量的 %.2f 倍" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        if t.get("at_limit_up"):
            out.append("這根 K 棒收在漲停價 %s：漲停鎖住時買單可能排隊也買不到。" % px(res["limit_up"]))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("那是 %d 分鐘前的事；最後收盤 %s（%s K 棒）。晚進場是另一筆交易：價格、停損距離和勝率都變了。" % (
                res["minutes_since"], px(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + "。")
        if res.get("minutes_since", 0) > step:
            out.append(NOTHING)
        else:
            out.append("如果你要做這筆交易，現在就是進場時機。" + NOTHING)
        out.append("下一步：買進之前先設好停損和停利：/trader-exit（exit.py plan --from trader/entry.json）。")
    else:
        out.append("%s  %s。" % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + "。")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("%s K 棒還在走（目前 %s，%s 收盤）；收盤前不算數。" % (f["bar_start"], px(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("今天這個型態沒有進場點。" + NOTHING)
        if res.get("next_open"):
            out.append("下一個交易日開盤：%s 台北。" % res["next_open"])
    if res.get("calendar_note"):
        out.append(res["calendar_note"] + "。")
    return out


def pad(text, width):
    """Left-justify by screen columns: a CJK character takes two."""
    cols = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - cols)


def row_text(cells, widths):
    return "  ".join(pad(c, w) for c, w in zip(cells, widths)) + "  " + cells[-1]


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    code = res["symbol"]
    px = lambda p: tw.fmt_price(p, code)
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "區間目前 %s 到 %s" % (px(min(x["l"] for x in seen)), px(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "開盤區間定案：高點 %s，低點 %s" % (px(orr["high"]), px(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "觸發：收在 %s 之上（%+.2f%%）。要做就現在進場：%s，%s" % (px(lvl), t["pct_over"], t["time"], px(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "站上 %s" % px(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += "，但量 %.2f 倍低於 %.2f 倍" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += "，但 %s 之前不新進場" % res["no_entries_before"]
    else:
        note = "差 %s 元到 %s" % (fmt_gap(lvl - b["c"], lvl, code), px(lvl))
    return "%s K 棒  收 %s  %s" % (hm(b["et"]), px(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    cal = opts.get("cal") or tw.Calendar()
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = tw.to_taipei(now)
        day = local.date()
        why = cal.closed(day)
        if why:
            emit("%s：今天休市（%s），沒有可以盯的。" % (day.isoformat(), why))
            return 1, None
        open_dt = datetime.datetime.combine(day, tw.OPEN)
        close_dt = datetime.datetime.combine(day, tw.CLOSE)
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s 台北  %s：從第一根 K 棒收盤 %s 開始盯。" % (
                    local.strftime("%H:%M"), "還沒開盤" if local < open_dt else "已開盤，還沒有收盤的 K 棒",
                    hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = tw.taipei_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s 台北  讀不到 K 棒：%s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("連續 %d 次讀取失敗，停止。" % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("開盤區間 "):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("開盤區間 ") or (line.startswith("觸發") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("過期  %s。" % (res["reason"] or "收盤了，沒有觸發"))
            return 1, res
        if until and local >= until:
            emit("到 --until %s 都沒有觸發，停止。%s。" % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or tw.NO_ENTRIES_AFTER,
                                                            "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg, cal):
    if a.side in ("sell", "short"):
        raise EntryError("目前只規劃做多：不做空，也不做先賣後買。改盯買進（--side buy）。")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level 只用在 --setup level（orb 的水準就是開盤區間高點）")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x 必須大於 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level 必須是大於 0 的價格")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg, "cal": cal}


def _setup(a):
    """(entry window, calendar, universe) from the config folder."""
    cfg, doc = read_config(a.config)
    folder = trader_dir(a.config)
    cal = tw.load_calendar(folder, doc)
    return cfg, cal, tw.load_universe(os.path.join(folder, "universe.txt"))


def _load(a, sym, now, universe, market=None, name=None):
    """(chart, now). An offline file without --now is read as of its own time."""
    code, market, name = resolve(sym, universe, market, name)
    real_today = tw.to_taipei(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(code, a.interval, offline=a.offline, market=market, name=name)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(code, a.interval, tw.to_taipei(now).date(), a.offline, real_today, market, name), now


def cmd_check(a):
    cfg, cal, universe = _setup(a)
    opts = _opts(a, cfg, cal)
    chart, now = _load(a, a.symbol, parse_now(a.now) if a.now else None, universe)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, ensure_ascii=False))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("已寫入 %s（狀態 %s）。" % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg, cal, universe = _setup(a)
    opts = _opts(a, cfg, cal)
    code, market, name = resolve(a.symbol, universe)
    if a.every <= 0:
        raise EntryError("--every 必須大於 0 秒")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, code, None, universe, market, name)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = tw.to_taipei(utc_now()).date()
    found = {"market": market}

    def provider(now):
        ch = get_chart(code, a.interval, tw.to_taipei(now).date(), a.offline, real_today, found["market"], name)
        found["market"] = ch["market"]          # ask Yahoo for the right market from now on
        return ch

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(tw.to_taipei(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("已寫入 %s。" % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code_, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                          on_trigger=on_trigger)
    return code_


def cmd_watchlist(a):
    cfg, cal, universe = _setup(a)
    opts = _opts(a, cfg, cal)
    if not os.path.exists(a.watchlist):
        raise EntryError("%s 沒有觀察清單：先跑 /trader-scanner（它會把命中的股票寫在那裡）" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s 不是有效的 JSON：%s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("%s 裡沒有命中的股票：掃描器沒找到可以盯的" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist 的 --offline 要一個放 <代號>.json 的資料夾")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("需要水準：--level 價格，或觀察清單裡的 \"level\"")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now, universe, h.get("market"), h.get("name"))
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, ensure_ascii=False))
    else:
        widths = (6, 8, 5, 8, 8, 8, 8)
        print(row_text(("代號", "狀態", "時間", "價格", "水準", "最新", "距水準", "名稱"), widths))
        for s, r, e in rows:
            if e:
                print("%s  錯誤：%s" % (pad(s, widths[0]), e))
                continue
            code = r["symbol"]
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            key = "triggered" if r["status"] == "triggered" else (r["phase"] if r["phase"] in SHORT else r["status"])
            print(row_text((
                code, SHORT.get(key, key), t.get("time") or "-",
                tw.fmt_price(t["price"], code) if t else "-",
                tw.fmt_price(r["level"], code) if r.get("level") is not None else "-",
                tw.fmt_price(last["close"], code) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-",
                r.get("name") or ""), widths))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d 檔中有 %d 檔觸發。挑一檔，再執行 entry.py check <代號> --write，然後 /trader-exit。" % (len(rows), n))
        print("Yahoo 免費 K 棒，非官方，可能延遲。" + NOTHING)
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar (Taiwan).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this Taipei time, e.g. "2026-11-02 09:20"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol", help="2303, 2303.TW or 6488.TWO")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this Taipei time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: 已停止。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
