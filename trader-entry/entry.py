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


# ---------------------------------------------------------------- MIS (real time)

MIS_BATCH = 50     # channels per MIS request


def mis_get(pairs):
    """[(code, market or None), ...] -> tw.parse_mis rows, MIS_BATCH channels a request.
    A code without a market asks both."""
    chans = []
    for code, market in pairs:
        chans += [tw.mis_channel(code, m) for m in ([market] if market else ["TWSE", "TPEx"])]
    out = {}
    for i in range(0, len(chans), MIS_BATCH):
        if i:
            _sleep(PAUSE)
        try:
            out.update(tw.parse_mis(_open_url(tw.MIS_URL % "|".join(chans[i:i + MIS_BATCH]))))
        except (urllib.error.URLError, OSError) as e:
            raise EntryError("讀不到證交所 MIS：%s" % (getattr(e, "reason", None) or e))
        except ValueError:
            raise EntryError("證交所 MIS 回傳了看不懂的內容")
    return out


def mis_log_path(config_path, day):
    return os.path.join(trader_dir(config_path), "state", "mis-%s.json" % day.isoformat())


def read_mis_log(path, required=False):
    if not os.path.exists(path):
        if required:
            raise EntryError("沒有 MIS 快照紀錄 %s" % path)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise EntryError("%s 不是有效的 JSON：%s" % (path, e))


def log_mis(log, quotes, day):
    """Add one MIS answer ({code: parse_mis row}) to the day's log: per stock its name, market,
    limits and every snapshot (time, the day's high and low so far, the last trade and its time,
    the best bid and ask, the day's volume in lots). A quote of another day is left out. A
    snapshot with no new trade (same high, low, trade, trade time and volume) moves the last
    one's time forward instead: the later time proves the same quiet stretch, and a full day
    of a thin stock stays small. The snapshot taken at its trade's time is kept, though: its
    volume is what later ones must show to prove nothing traded since (see mis_closes)."""
    if not log or log.get("date") != day.isoformat():
        log = {"date": day.isoformat(), "source": "mis", "stocks": {}}
    for code, q in quotes.items():
        if not q.get("time") or (q.get("date") and q["date"] != day.strftime("%Y%m%d")):
            continue
        st = log["stocks"].setdefault(code, {"snaps": []})
        for k in ("name", "market", "prev_close", "limit_up", "limit_down"):
            if q.get(k) is not None:
                st[k] = q[k]
        snap = {"at": q["time"], "high": q.get("high"), "low": q.get("low"), "open": q.get("open"),
                "trade": q.get("trade_price"), "trade_at": q.get("trade_time"),
                "bid": q.get("bid"), "ask": q.get("ask"), "volume_lots": q.get("volume_lots") or 0}
        same = ("high", "low", "trade", "trade_at", "volume_lots")
        last = st["snaps"][-1] if st["snaps"] else None
        if last and (last["at"] == snap["at"]
                     or (last["at"] != last.get("trade_at") and all(last.get(k) == snap[k] for k in same))):
            st["snaps"][-1] = snap
        else:
            st["snaps"].append(snap)
    return log


def mis_snapshot(pairs, log_path, day):
    """Fetch MIS once, add it to the day's log on disk. -> the log."""
    quotes = mis_get(pairs)
    log = log_mis(read_mis_log(log_path), quotes, day)
    write_json(log_path, log)
    return log


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


def check_args(setup, or_minutes, interval, level):
    """-> the bar length in seconds, or EntryError."""
    if interval not in INTERVALS:
        raise EntryError("--interval 只能是 5m 或 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes 必須是整數根 %s K 棒" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup 只能是 orb 或 level")
    if setup == "level" and level is None:
        raise EntryError("--setup level 需要 --level 價格（例如 20 日新高）")
    return step


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, cal=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    cal = cal or tw.Calendar()
    step = check_args(setup, or_minutes, interval, level)
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


MIS_NEAR = 60       # seconds: a trade this close before a bar ends stands in for its close
MIS_MIN_EVERY = 5   # seconds: the shortest poll of the public MIS site


def _at(day, text):
    """'09:15:03' on day -> naive Taipei datetime, or None."""
    try:
        h, m, s = (int(x) for x in str(text).split(":"))
        return datetime.datetime.combine(day, datetime.time(h, m, s))
    except (TypeError, ValueError):
        return None


def mis_closes(snaps, day, step):
    """The bar closes the snapshots prove. MIS shows a trade's price only now and then (z: 32
    of 402 snapshots at 09:20 on 2026-11-04), and its "trade" repeats the last one it showed
    while trades without a shown price go on (台積電's stayed at 09:17:25 while 113 lots
    traded). So a trade P at T says nothing about the time after T, unless a snapshot shows the
    day's volume of the snapshot taken at T: then nothing traded from T to it. P closed T's bar
    when such a snapshot came after that bar ended (exact), and the bars that ended in between
    had no trade (quiet). A trade, or a proven quiet stretch, that ends within MIS_NEAR seconds
    of the end of its bar stands in for that close (not exact). Two snapshots with the same
    volume prove the bars between them quiet as well.
    -> ({slot: {"c", "exact", "seen", "trade_at"}}, {quiet slots})"""
    open_dt = datetime.datetime.combine(day, tw.OPEN)
    auction = datetime.datetime.combine(day, tw.CLOSING_AUCTION)
    end_of = lambda k: open_dt + datetime.timedelta(seconds=(k + 1) * step)
    closes, quiet = {}, set()
    volume_then = {}                       # trade time -> the day's volume in its own snapshot
    for s in snaps:
        if s.get("trade") and s.get("volume_lots") and s.get("trade_at") == s.get("at"):
            volume_then.setdefault(s["at"], s["volume_lots"])
    for s in snaps:
        tt = _at(day, s.get("trade_at"))
        if not s.get("trade") or not s.get("volume_lots") or tt is None:
            continue                       # no trade today yet: the trade is yesterday's
        if tt < open_dt or tt >= auction or tt > s["_at"]:
            continue
        # quiet up to the snapshot only when its volume is still that of the trade's own snapshot;
        # otherwise it may have traded since, at prices MIS did not show
        until = s["_at"] if volume_then.get(s["trade_at"]) == s["volume_lots"] else tt
        k = int((tt - open_dt).total_seconds()) // step
        if until >= end_of(k):
            closes[k] = {"c": s["trade"], "exact": True, "seen": s["at"], "trade_at": s["trade_at"]}
            j = k + 1
            while end_of(j) <= min(until, auction):
                quiet.add(j)
                j += 1
        elif ((end_of(k) - until).total_seconds() <= MIS_NEAR and not closes.get(k, {}).get("exact")
              and s["trade_at"] > closes.get(k, {}).get("trade_at", "")):     # a later trade, or the first sight
            closes[k] = {"c": s["trade"], "exact": False, "seen": s["at"], "trade_at": s["trade_at"]}
    for a, b in zip(snaps, snaps[1:]):
        if a.get("volume_lots") and a["volume_lots"] == b.get("volume_lots"):
            k = max(0, -(-int((a["_at"] - open_dt).total_seconds()) // step))      # first bar starting after a
            while end_of(k) <= min(b["_at"], auction):
                if k not in closes:
                    quiet.add(k)
                k += 1
    return closes, quiet


def slot_spans(slots, open_dt, step):
    """[3, 4, 5, 9] -> '09:15 到 09:25 這 3 根、09:45 這 1 根' (bar start times)."""
    runs = []
    for k in sorted(slots):
        if runs and k == runs[-1][1] + 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    at = lambda k: hm(open_dt + datetime.timedelta(seconds=k * step))
    return "、".join(("%s 這 1 根" % at(a)) if a == b else ("%s 到 %s 這 %d 根" % (at(a), at(b), b - a + 1))
                    for a, b in runs)


def mis_range(snaps, day, or_minutes, n_or):
    """The day's high and low when the opening range ended, from the first snapshot after it.
    Exact when the last snapshot before the end already had the same high and low (they only
    grow apart); otherwise it may hold a few seconds more, up to MIS_NEAR. The snapshot's last
    shown trade being before the end proves nothing: later trades may have no shown price.
    -> (opening_range, None) or (None, why not)."""
    open_dt = datetime.datetime.combine(day, tw.OPEN)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    seen = [s for s in snaps if s.get("high") and s.get("low")]
    after = [s for s in seen if s["_at"] >= or_end]
    if not after:
        return None, "還沒有 %s 之後的 MIS 快照" % hm(or_end)
    s = after[0]
    before = [p for p in seen if p["_at"] < or_end]
    exact = bool(before and (before[-1]["high"], before[-1]["low"]) == (s["high"], s["low"]))
    late = (s["_at"] - or_end).total_seconds()
    if not exact and late > MIS_NEAR:
        return None, ("MIS 在 %s 之後的第一張快照是 %s，那時的最高最低可能已經含區間之後的成交，不能當開盤區間。"
                      "下個交易日在 %s 前開始 entry.py watch --source mis，或改看 Yahoo（不加 --source，約 %s 之後）"
                      % (hm(or_end), s["at"], hm(or_end), hm(or_end + datetime.timedelta(minutes=25))))
    return {"high": s["high"], "low": s["low"], "minutes": or_minutes, "bars": n_or, "of": n_or,
            "complete": True, "avg_volume": None, "as_of": s["at"], "exact": exact}, None


def evaluate_mis(stock, code, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
                 min_volume_x=None, cfg=None, cal=None, live=False):
    """evaluate() from one stock's MIS snapshots (its entry in the day's log), with the same
    result shape and "source": "mis". The opening range is MIS's high and low of the day just
    after it ended; a bar's close is the last trade before it ended (see mis_closes). A bar
    whose close no snapshot shows is listed in "unknown_bars": a breakout there is not seen.
    live: the newest snapshot is now, even if this computer's clock is a little behind."""
    cfg = cfg or dict(DEFAULTS)
    cal = cal or tw.Calendar()
    step = check_args(setup, or_minutes, interval, level)
    if min_volume_x:
        raise EntryError("--source mis 沒有每根 K 棒的成交量，不能用 --min-volume-x")
    n_or = or_minutes * 60 // step
    local = tw.to_taipei(now_utc)
    day = local.date()
    snaps = sorted((dict(s, _at=_at(day, s.get("at"))) for s in (stock or {}).get("snaps") or []
                    if _at(day, s.get("at"))), key=lambda s: s["_at"])
    if live and snaps and snaps[-1]["_at"] > local:
        local = snaps[-1]["_at"]
    snaps = [s for s in snaps if s["_at"] <= local]
    res = {"symbol": code, "market": (stock or {}).get("market"), "name": (stock or {}).get("name"),
           "setup": setup, "side": "buy", "date": day.isoformat(), "interval": interval,
           "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": None, "source": "mis",
           "prev_close": (stock or {}).get("prev_close"), "limit_up": None, "limit_down": None,
           "calendar_known": cal.known(day.year), "calendar_note": cal.unknown_note(day), "_bars": [],
           "data_time": "%s %s" % (day.isoformat(), snaps[-1]["at"]) if snaps else None, "unknown_bars": []}
    if res["prev_close"]:
        up, down = tw.price_limits(res["prev_close"], code)
        res["limit_up"], res["limit_down"] = float(up), float(down)
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
    if local < open_dt:
        res.update(phase="pre-open", reason="%s 開盤%s" % (
            hm(tw.OPEN), "，%d 分鐘開盤區間在 %s 定案" % (or_minutes, hm(or_end)) if setup == "orb" else ""))
        return res
    if not snaps:
        res.update(status="no-data", phase="open", reason="還沒有 %s 的 MIS 快照" % day.isoformat())
        return res
    code_px = lambda p: tw.fmt_price(p, code)
    newest = snaps[-1]
    if newest.get("trade") and newest.get("volume_lots") and _at(day, newest.get("trade_at")):
        res["mis_now"] = {"price": snap(newest["trade"], code), "trade_at": newest["trade_at"], "at": newest["at"],
                          "bid": newest.get("bid"), "ask": newest.get("ask")}
        k = int((min(local, auction_dt) - open_dt).total_seconds()) // step
        bar_start = open_dt + datetime.timedelta(seconds=k * step)
        if local < auction_dt:
            res["forming"] = {"bar_start": hm(bar_start), "closes": hm(bar_start + datetime.timedelta(seconds=step)),
                              "price": res["mis_now"]["price"]}
    if setup == "orb":
        if local < or_end:
            hi, lo = newest.get("high"), newest.get("low")
            if hi and lo:
                res["opening_range"] = {"high": hi, "low": lo, "minutes": or_minutes, "bars": 0, "of": n_or,
                                        "complete": False, "avg_volume": None, "as_of": newest["at"], "exact": True}
            res.update(phase="forming", reason="開盤區間在 %s 定案" % hm(or_end))
            return res
        orr, why_not = mis_range(snaps, day, or_minutes, n_or)
        if not orr:
            res.update(status="no-data", phase="open", reason=why_not)
            res["final"] = any(s["_at"] >= or_end for s in snaps)     # too late today: watching will not fix it
            return res
        res["opening_range"] = orr
        lvl = orr["high"]
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    closes, quiet = mis_closes(snaps, day, step)
    bars = []
    for k in sorted(closes):
        et = open_dt + datetime.timedelta(seconds=k * step)
        end = et + datetime.timedelta(seconds=step)
        if k < first_slot or end > local:
            continue
        bars.append(dict(closes[k], c=snap(closes[k]["c"], code), slot=k, et=et, end=end, v=0))
    seen_until = min(newest["_at"], auction_dt, cutoff)
    unknown, k = [], first_slot
    while open_dt + datetime.timedelta(seconds=(k + 1) * step) <= seen_until:
        if k not in closes and k not in quiet:
            unknown.append(k)
        k += 1
    res["unknown_bars"] = [hm(open_dt + datetime.timedelta(seconds=j * step)) for j in unknown]
    res["_bars"] = bars
    res["bars_closed"] = len(bars)
    trig = None
    for b in bars:
        if before <= b["end"] <= cutoff and b["c"] > lvl:
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": last["c"], "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}
    what = ("開盤區間高點 %s" % code_px(lvl)) if setup == "orb" else ("水準 %s" % code_px(lvl))
    missed = [j for j in unknown if trig is None or j < trig["slot"]]
    miss_note = ("%s的 K 棒收盤沒有快照看到，那裡的突破看不到" % slot_spans(missed, open_dt, step)) if missed else ""
    if trig:
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="%s K 棒收在%s之上" % (hm(trig["et"]), "開盤區間高點" if setup == "orb" else "水準"))
        res["trigger"] = {"time": hm(trig["end"]), "bar_start": hm(trig["et"]), "bar_index": trig["slot"],
                          "price": trig["c"], "volume": None, "volume_x": None,
                          "pct_over": r2(100.0 * (trig["c"] / lvl - 1)),
                          "at_limit_up": bool(res["limit_up"] and trig["c"] >= res["limit_up"]),
                          "exact": trig["exact"], "seen": trig["seen"], "trade_at": trig["trade_at"]}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        notes = []
        if not trig["exact"]:
            notes.append("收盤價取自 %s 的成交（%s 收盤前，MIS %s 的快照看到）：之後到收盤可能還有成交，收盤價可能不一樣"
                         % (trig["trade_at"], hm(trig["end"]), trig["seen"]))
        if miss_note:
            notes.append(miss_note + "，突破可能更早")
        if notes:
            res["data_note"] = "；".join(notes)
        return res
    if local >= cutoff:
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="到 %s 都沒有收在%s 之上的 K 棒，%s 之後不再新進場%s" % (
                       hm(cutoff), what, hm(cutoff), ("（%s）" % miss_note) if miss_note else ""))
        return res
    res["phase"] = "open"
    if last:
        gap = lvl - last["c"]
        head = "最後確定的收盤 %s（%s K 棒）" % (code_px(last["c"]), hm(last["et"]))
        if gap == 0:
            res["reason"] = "%s，剛好等於%s，不算站上" % (head, what)
        elif gap < 0:
            res["reason"] = "%s，站上了%s，但 %s 之前不新進場（設定檔）" % (head, what, hm(before))
        else:
            ticks = ("%d 檔，" % abs(tw.tick_steps(last["c"], lvl, code))) if tw.on_tick(lvl, code) else ""
            res["reason"] = "%s，距%s 還差 %s 元（%s%+.2f%%）" % (
                head, what, fmt_gap(gap, lvl, code), ticks, 100.0 * (last["c"] / lvl - 1))
    else:
        res["reason"] = "%s之後還沒有看得到收盤的 K 棒（%s）" % (
            "開盤區間" if setup == "orb" else "開盤", what)
    if miss_note:
        res["reason"] += ("；" + miss_note + "：MIS 只在部分快照給成交價，用 entry.py watch --source mis --every 10"
                          "能多看到幾根，成交熱絡的股票還是會漏")
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
            "source": res.get("source", "yahoo"), "checked_at": iso(now_utc), "reason": res.get("reason")}


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
    mis = res.get("source") == "mis"
    head = "%s  %s %s 台北  %s，%s（%s）" % (
        title(res), tw.day_label(when.date()), when.strftime("%H:%M"), setup_name(res), BARS[res["interval"]],
        "證交所 MIS 盤中快照，可能落後幾秒到一兩分鐘" if mis else "Yahoo 免費資料，可能延遲")
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += "，資料到 %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr and mis:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        if not orr["complete"]:
            out.append("開盤區間 %s-%s：目前高點 %s，低點 %s（MIS 到 %s，還在形成）。" % (
                hm(start), hm(end), px(orr["high"]), px(orr["low"]), orr["as_of"]))
        else:
            out.append("開盤區間 %s-%s：高點 %s，低點 %s（MIS %s 快照裡的當日最高最低%s）。" % (
                hm(start), hm(end), px(orr["high"]), px(orr["low"]), orr["as_of"],
                "" if orr["exact"] else "，多含了 %s 到 %s 之間的成交" % (hm(end), orr["as_of"])))
    elif orr:
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


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None,
               evaluator=None, poll=False):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes. evaluator(chart, now, **opts)
    is evaluate() unless given; poll: fetch every `every` seconds all the time (MIS snapshots
    show a close only when one is taken near the end of the bar or before its next trade)."""
    evaluator = evaluator or evaluate
    step = INTERVALS[opts.get("interval", "5m")]
    cal = opts.get("cal") or tw.Calendar()
    printed, errors, said_preopen, first, said_range = -1, 0, False, True, False
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
        res = evaluator(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        set_range = res.get("source") == "mis" and (res.get("opening_range") or {}).get("complete")
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("開盤區間 "):
                emit(lines[1])
                said_range = set_range
        elif set_range and not said_range:      # MIS has no range bars to print: say it once it is set
            emit(lines[1])
            said_range = True
        if first:
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
        if res.get("final"):
            emit("停止  %s。" % res["reason"])
            return 1, res
        if until and local >= until:
            emit("到 --until %s 都沒有觸發，停止。%s。" % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind or poll:
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


def _mis_results(a, stocks, now, opts, cal):
    """stocks: [(code, market, name)]. Live (no --now, no --offline): one MIS snapshot of them all
    is added to today's log first. --now replays today's log (or --offline PATH) up to that time.
    -> ({code: result}, now)"""
    live = not a.offline and now is None
    if live:
        now = utc_now()
        day = tw.to_taipei(now).date()
        path = mis_log_path(a.config, day)
        log = read_mis_log(path) if cal.closed(day) else mis_snapshot([(c, m) for c, m, _ in stocks], path, day)
    else:
        path = a.offline or mis_log_path(a.config, tw.to_taipei(now).date())
        log = read_mis_log(path, required=True)
        if now is None:
            day = datetime.date.fromisoformat(log.get("date"))
            times = [_at(day, s.get("at")) for st in (log.get("stocks") or {}).values() for s in st.get("snaps", [])]
            times = [t for t in times if t]
            if not times:
                raise EntryError("%s 裡沒有快照" % path)
            now = tw.taipei_to_utc(max(times))
    out = {}
    for code, market, name in stocks:
        st = ((log or {}).get("stocks") or {}).get(code) or {"market": market, "snaps": []}
        if name and not st.get("name"):
            st = dict(st, name=name)
        out[code] = evaluate_mis(st, code, now, live=live, **opts)
    return out, now


def cmd_check(a):
    cfg, cal, universe = _setup(a)
    opts = _opts(a, cfg, cal)
    if a.source == "mis":
        code, market, name = resolve(a.symbol, universe)
        got, now = _mis_results(a, [(code, market, name)], parse_now(a.now) if a.now else None, opts, cal)
        res = got[code]
    else:
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
    mis = a.source == "mis"
    if mis and a.every < MIS_MIN_EVERY:
        raise EntryError("--source mis 的 --every 至少 %d 秒：MIS 是證交所的公開網站，別問太快" % MIS_MIN_EVERY)
    if a.offline or start is not None:
        if start is None and mis:               # a saved MIS log replays from just before its day opens
            day = datetime.date.fromisoformat(read_mis_log(a.offline, required=True).get("date"))
            start = tw.taipei_to_utc(datetime.datetime.combine(day, datetime.time(8, 59)))
        elif start is None:
            start = _load(a, code, None, universe, market, name)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = tw.to_taipei(utc_now()).date()
    found = {"market": market}
    live = not (a.offline or a.now)
    saved = {}

    def provider(now):
        if not mis:
            ch = get_chart(code, a.interval, tw.to_taipei(now).date(), a.offline, real_today, found["market"], name)
            found["market"] = ch["market"]          # ask Yahoo for the right market from now on
            return ch
        day = tw.to_taipei(now).date()
        if live:
            log = mis_snapshot([(code, found["market"])], mis_log_path(a.config, day), day)
        else:
            path = a.offline or mis_log_path(a.config, day)
            if path not in saved:
                saved[path] = read_mis_log(path, required=True)
            log = saved[path]
        st = (log.get("stocks") or {}).get(code) or {"market": found["market"], "snaps": []}
        found["market"] = st.get("market") or found["market"]
        return dict(st, name=st.get("name") or name)

    evaluator = (lambda st, now, **o: evaluate_mis(st, code, now, live=live, **o)) if mis else None

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
                          on_trigger=on_trigger, evaluator=evaluator, poll=mis)
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
    mis = a.source == "mis"
    if a.offline and not mis and not os.path.isdir(a.offline):
        raise EntryError("watchlist 的 --offline 要一個放 <代號>.json 的資料夾")
    now = parse_now(a.now) if a.now else None
    rows = []
    if mis:                                     # one MIS request for the whole list
        stocks = [resolve(str(h["symbol"]).upper(), universe, h.get("market"), h.get("name")) for h in hits]
        got, now = _mis_results(a, stocks, now, opts, cal)
        hits = []
        rows = [(c, got[c], None) for c, _, _ in stocks]
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
        print(("證交所 MIS 盤中快照，收盤價是快照看到的最後一筆成交。" if mis else "Yahoo 免費 K 棒，非官方，可能延遲。")
              + NOTHING)
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
        p.add_argument("--offline", metavar="PATH",
                       help="saved Yahoo chart JSON (.json/.json.gz) or a folder; with --source mis a MIS log")
        p.add_argument("--source", choices=["yahoo", "mis"], default="yahoo",
                       help="yahoo: 5-minute bars about 20 minutes late; mis: TWSE MIS snapshots in real time")
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
