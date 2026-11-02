#!/usr/bin/env python3
"""Time Machine for trader-timemachine, Taiwan market. Standard library only.

Replays a planned day trade before you risk a dollar: 10,000 simulated runs of
the rest of the session from your entry, plus the same rule on every past
session in the data, both net of the round-trip cost.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 13:20            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol 2303 --date 2026-11-02 --entry 110 \\
        --stop 108.5 --target 111.5 --entry-time 09:20
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (same algorithm and the same random draws as the original):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m;
     one request, .TW for TWSE, .TWO for TPEx). Keep only FULL sessions before
     the plan's date: all 53 bars of continuous trading, 09:00 to 13:20 bar
     starts, Taipei time. Yahoo leaves the 13:25 closing-auction bar empty and
     adds a 13:30 print of the close only on the current day: both are left out,
     so the auction is not modelled. Short sessions and today are left out.
  2. The pool: for every bar slot 1..52 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..52, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The high, low and close are clamped into the day's
     limit-down to limit-up range (10 % around the reference price; the plan's
     limits, else computed from its prev_close). The stop is checked before the
     target in every bar, so a bar that touches both counts as the stop.
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 13:25, the end of
     continuous trading). "green" = target first, or above the entry at --until
     (below for a sell). R = the target's R at the target, -1 at the stop,
     otherwise (price at --until - entry) / risk. Net R = R minus the cost in R
     (fee twice plus the day-trade tax, over the risk a share).
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R, every price on the tick grid) re-run
     on every one of those past full sessions, net of costs too.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R k and cost c (in R): (1 + c) / (1 + k),
so 50 % at 1R with no cost, 66 % at 1R with a 0.32R cost. The gap, in points:
  less than 5 points    接近擲硬幣 (1R, no cost) / 接近損益兩平 (otherwise)
  5 to 15 points        偏向停利 / 偏向停損
  15 points or more     明顯偏向停利 / 明顯偏向停損
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the net average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: news, a disposition (處置) or a limit lock can do what the last
60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<CODE>.TW?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are Taipei wall-clock; --now "2026-11-02 09:25" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

UA = tw.UA
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = tw.BARS_5M                                  # 53: 09:00 to 13:20 bar starts
OPEN_MIN = tw.OPEN.hour * 60 + tw.OPEN.minute              # 09:00
END_MIN = tw.CLOSING_AUCTION.hour * 60 + tw.CLOSING_AUCTION.minute   # 13:25, continuous trading ends
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
STOP_WORDS = {"or_mid": "開盤區間中點", "or_low": "開盤區間低點", "or_high": "開盤區間高點"}


class TMError(Exception):
    pass


class NoChart(TMError):
    """Yahoo has no chart for this symbol (HTTP 404): the market suffix may be the wrong one."""


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return tw.parse_now(text)
    except ValueError as e:
        raise TMError(str(e))


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'09:20', '9:20', '13:20' or '1:20 PM' (a bare 1 to 5 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s 要像 09:20 或 13:20 這樣的台北時間，不是 %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r 不是時間" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 5:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(ysym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(ysym.upper()))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with tw.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(ysym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(ysym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise NoChart("Yahoo 沒有 %s 的線圖（HTTP 404）：請確認代號" % ysym.upper())
                raise TMError("Yahoo 回應 HTTP %d（%s）" % (e.code, ysym.upper()))
            except urllib.error.URLError as e:
                raise TMError("連不上 Yahoo：%s" % e.reason)
            except OSError as e:
                raise TMError("連不上 Yahoo：%s" % e)
    if last == 429:
        raise TMError("Yahoo 正在限制請求次數（HTTP 429），等一分鐘再跑一次")
    raise TMError("Yahoo 兩台主機都回應 HTTP %s（%s），稍後再試" % (last, ysym.upper()))


def fetch_for(code, market):
    """(chart text, market). An unknown market tries .TW first, then .TWO."""
    if market:
        return fetch_chart(tw.yahoo_symbol(code, market)), market
    try:
        return fetch_chart(tw.yahoo_symbol(code, "TWSE")), "TWSE"
    except NoChart:
        _sleep(PAUSE)
        return fetch_chart(tw.yahoo_symbol(code, "TPEx")), "TPEx"


def read_offline(path, code):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <CODE>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % code, "%s-60d5m.json.gz" % code):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("%s 裡沒有 %s-60d5m.json(.gz)" % (path, code))
    if not os.path.exists(path):
        raise TMError("找不到離線線圖檔 %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("讀不到 %s：%s" % (path, e))


def chart_result(text, code):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo 給的 %s 資料不是 JSON" % code)
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo 給的 %s 資料格式不對" % code)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo 沒有 %s 的資料：%s" % (code, err.get("description") or "未知的代號"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). 5-min bars of continuous trading only
    (09:00 to 13:20 starts, Taipei), oldest first. Dropped: bars off the 5-minute grid
    (the live tick), bars outside continuous trading (the empty 13:25 auction bar and
    the 13:30 print of the close), bars with a missing price, and bars not complete yet
    (now < start + 5 min, where now is the earlier of --now and regularMarketTime)."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None:
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = tw.to_taipei(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before, seen=()):
    """Full sessions (all 53 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (gaps, a halt). A day in seen with no
    priced bar at all (Yahoo keeps an empty day when the market stops trading
    without notice, like a typhoon day) is a short session with 0 bars."""
    full, short = [], []
    for d in sorted(set(days) | set(seen)):
        if d >= before:
            continue
        s = days.get(d, {})
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


def session_dates(result):
    """Every Taipei date with an on-grid timestamp inside continuous trading, priced or not."""
    out = set()
    for ts in result.get("timestamp") or []:
        if ts is None or int(ts) % INTERVAL:
            continue
        local = tw.to_taipei(utc_of(ts))
        if 0 <= local.hour * 60 + local.minute - OPEN_MIN < SESSION_BARS * 5:
            out.add(local.date().isoformat())
    return out


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy",
             limit_up=None, limit_down=None, cost_r=0.0):
    """Monte Carlo replays of bars k+1..last, every price clamped into [limit_down, limit_up].
    The draws and their order are the original's: a clamp never takes a random number."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    cap_hi = limit_up if limit_up else float("inf")
    cap_lo = limit_down if limit_down else 0.0

    def clamp(x):
        return cap_hi if x > cap_hi else cap_lo if x < cap_lo else x

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    at_up = at_down = 0
    for n in range(replays):
        p = entry
        outcome = None
        touched_up = touched_down = False
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = clamp(p * h), clamp(p * l), clamp(p * c)
            if hi >= cap_hi:
                touched_up = True
            if lo <= cap_lo:
                touched_down = True
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        at_up += touched_up
        at_down += touched_down
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = clamp(p * c)
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    avg = statistics.mean(Rs)
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(avg, 3),
        "cost_r": round(cost_r, 4), "avg_R_net": round(avg - cost_r, 3),
        "r_at_target": r_target, "limit_up": limit_up, "limit_down": limit_down,
        "p_limit_up": round(at_up / replays, 4), "p_limit_down": round(at_down / replays, 4),
        "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def snap(x, code, mode="nearest"):
    """A price onto the tick grid, as a float."""
    return float(tw.to_tick(round(x, 6), code, mode))


def tick_away(x, code, long):
    """A stop onto the tick grid, away from the entry: down for a buy, up for a sell."""
    return snap(x, code, "down" if long else "up")


def opening_range(D, n, code):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list, on the tick grid."""
    rows = [D[i] for i in range(n)]
    return snap(max(b["h"] for b in rows), code), snap(min(b["l"] for b in rows), code)


def rule_stop(rule, oh, ol, entry, long, code):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, code, long)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, code, long)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), code, long)


def history_replay(sessions, rule, last, side, code, cost_rate):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until. Net R takes off that trade's cost."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n, code)
        kk = next((i for i in range(n, last + 1)
                   if (snap(D[i]["c"], code) > oh if long else snap(D[i]["c"], code) < ol)), None)
        if kk is None:
            continue
        e = snap(D[kk]["c"], code)
        s = rule_stop(rule, oh, ol, e, long, code)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = snap(e + rule["target_r"] * risk, code) if long else snap(e - rule["target_r"] * risk, code)
        if (t <= e) if long else (t >= e):
            continue
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        cost = e * cost_rate / risk
        trades.append({"date": d, "time": clock24(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(R - cost, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("plan 的 %s 要是數字，拿到 %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("plan 的 %s 要是正的價格，拿到 %r" % (what, v))
    return x


def _opt_num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 and not math.isinf(x) else None


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise TMError("%s 不是有效的 JSON：%s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("用 --symbol 時也要給 %s" % "、".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "market": a.market, "side": a.side,
               "date": a.date or tw.to_taipei(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("%s 沒有計畫：先跑 /trader-exit，或用 --symbol --date --entry --stop --target "
                          "--entry-time 直接給" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s 不是有效的 JSON：%s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s 不是交易計畫" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    try:
        code, suffix_market = tw.parse_symbol(raw.get("symbol"))
    except ValueError:
        raise TMError("plan 要有台股代號（例如 2303、6488.TWO），拿到 %r" % raw.get("symbol"))
    market = raw.get("market") or suffix_market
    if market not in (None, "TWSE", "TPEx"):
        raise TMError("market 要是 TWSE 或 TPEx，拿到 %r" % market)
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side 要是 buy 或 sell，拿到 %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("只用 5 分 K（plan 寫的是 %s）：Yahoo 保留 60 天的 5 分 K，1 分 K 只有大約 7 天，"
                      "交易日太少，重播池不公平" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("plan 的 date 要像 2026-11-02，拿到 %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("做多要 停損 < 進場 < 停利（拿到 停損 %s、進場 %s、停利 %s）" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("放空要 停利 < 進場 < 停損（拿到 停利 %s、進場 %s、停損 %s）" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s 不在 5 分鐘格線上（用那根 K 棒的收盤時間，例如 09:20）" % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar 要是 K 棒編號，拿到 %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("plan 自相矛盾：entry_bar 是 %d，但 entry_time %s 是第 %d 根" % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("plan 要有 entry_time（進場那根 K 棒的收盤時間，例如 09:20）或 entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("進場那根 K 棒要在 09:05 到 13:20 之間收盤，後面才有 K 棒可以重播（拿到 %s）"
                      % clock24(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes 要是數字")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes 要是 5 的倍數（拿到 %s）" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    out = {
        "symbol": code, "market": market, "name": raw.get("name") or None, "side": side, "date": date,
        "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock24(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 4),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto").replace("-", "_"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
    }
    for key in ("prev_close", "limit_up", "limit_down", "cost_per_share", "cost_r"):
        out[key] = _opt_num(raw.get(key))
    out["limits_source"] = raw.get("limits_source") if out["limit_up"] and out["limit_down"] else None
    return out


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    code = plan["symbol"]
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n, code)
            if abs(tick_away((oh + ol) / 2, code, long) - plan["stop"]) < 1e-6:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, code, long) - plan["stop"]) < 1e-6:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


def resolve_limits(plan, meta, days):
    """(limit_up, limit_down, source, note). The plan's limits; else 10 % around the plan's
    prev_close; else around Yahoo's previousClose when the plan is for the data's last day;
    else around the previous session's last 5-minute close (the auction is not in the data)."""
    code = plan["symbol"]
    if plan["limit_up"] and plan["limit_down"]:
        return plan["limit_up"], plan["limit_down"], plan.get("limits_source") or "plan", ""
    ref, how = plan["prev_close"], "plan 的昨收"
    if ref is None:
        last_day = None
        rmt = meta.get("regularMarketTime")
        if isinstance(rmt, (int, float)):
            last_day = tw.to_taipei(utc_of(rmt)).date().isoformat()
        if last_day == plan["date"] and _opt_num(meta.get("previousClose")):
            ref, how = float(meta["previousClose"]), "Yahoo 的昨收"
    if ref is None:
        before = [d for d in sorted(days) if d < plan["date"]]
        if not before:
            return None, None, None, "沒有昨收可以算漲跌停：重播沒有夾在漲跌停之間"
        s = days[before[-1]]
        ref = snap(s[max(s)]["c"], code)
        how = "%s 最後一根 5 分 K 的收盤（收盤集合競價不在資料裡，可能差一兩檔）" % before[-1]
    up, down = tw.price_limits(ref, code)
    note = "plan 沒有漲跌停：用%s %s 算出 %s / %s" % (how, tw.fmt_price(ref, code), tw.fmt_price(up, code),
                                                   tw.fmt_price(down, code))
    return float(up), float(down), "computed", note


def resolve_cost(plan, cfg):
    """(cost per share as a fraction of the price, note). The plan's cost_per_share, else its
    cost_r, else the config's fee terms: fee twice plus the day-trade tax, no fee minimum."""
    if plan["cost_per_share"] is not None:
        return plan["cost_per_share"] / plan["entry"], ""
    if plan["cost_r"] is not None:
        return plan["cost_r"] * plan["risk_per_share"] / plan["entry"], ""
    day = datetime.date.fromisoformat(plan["date"])
    rate = float(tw.cost_rate(plan["symbol"], True, tw.fee_settings(cfg), day))
    per = plan["entry"] * rate
    return rate, ("plan 沒有成本欄位：用設定檔的手續費算，每股約 %.2f（%.2fR）" % (per, per / plan["risk_per_share"]))


# ---------------------------------------------------------------- the run

def _until(mc):
    return mc.get("until") or clock24(END_MIN)


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R and the cost. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    c = mc.get("cost_r") or 0.0
    share, even = pt / (pt + ps), (1.0 + c) / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "到 %s 沒有任何一次重播碰到停利或停損：每一次都由 %s 的價格決定。" % (_until(mc), _until(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    c = mc.get("cost_r") or 0.0
    one_r = abs(r - 1.0) < 0.005
    no_cost = c < 0.005
    if abs(d) < COIN_FLIP:
        head = "接近擲硬幣" if one_r and no_cost else "接近損益兩平"
    elif abs(d) < STRONG:
        head = "偏向停利" if d > 0 else "偏向停損"
    else:
        head = "明顯偏向停利" if d > 0 else "明顯偏向停損"
    odds = "先到停利 %.1f%% 對先到停損 %.1f%%" % (100 * pt, 100 * ps)
    if one_r and no_cost:
        return "%s：%s。" % (head, odds)
    terms = "%gR" % round(r, 2) if no_cost else "%gR 加上成本 %.2fR" % (round(r, 2), c)
    return ("%s：%s 時，碰到停利或停損的重播裡，停利要先到 %.0f%% 才打平；實際是 %.0f%%（%s）。"
            % (head, terms, 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f%% 的重播到 %s 都沒碰到停利或停損，這些由 %s 的價格決定。"
                     % (100 * pn, _until(mc), _until(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R_net", mc.get("avg_R"))
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("不過扣成本的平均 R 是 %+.3f：那些都沒碰到的重播把它拉往另一邊。" % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 config=None, calendar=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays 要在 1 到 1,000,000 之間")
    if not 0 <= window <= 10:
        raise TMError("--window 要在 0 到 10 根之間")
    code = plan["symbol"]
    cal = calendar or tw.Calendar()
    result = chart_result(text, code)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym:
        mcode, _, suffix = msym.partition(".")
        data_market = {"TW": "TWSE", "TWO": "TPEx"}.get(suffix)
        if mcode != code or (plan["market"] and data_market and data_market != plan["market"]):
            raise TMError("資料是 %s 的，計畫卻是 %s" % (msym, tw.yahoo_symbol(code, plan["market"])
                                                     if plan["market"] else code))
        plan = dict(plan, market=plan["market"] or data_market)
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("資料是 %s K 棒；時光機要 5 分 K" % gran)
    if not bars:
        raise TMError("資料裡沒有 %s 收完的 5 分 K" % code)
    k = plan["entry_bar"]
    until_min = END_MIN if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > END_MIN:
        raise TMError("--until 要在 5 分鐘格線上，而且不能晚於 %s（13:25 之後是收盤集合競價）" % clock24(END_MIN))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s 在 %s 進場之後沒有 K 棒可以重播" % (clock24(until_min), plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        day = datetime.date.fromisoformat(plan["date"])
        closed = cal.closed(day)
        if closed:
            reason = "那天休市：%s" % closed
        elif not entry_day:
            reason = "那天沒有 K 棒"
        else:
            reason = "資料只到 %s 那根" % entry_day[max(entry_day)]["start"]
        raise TMError("資料裡沒有進場那根 K 棒：%s %s 的 %s 那根（%s 收盤）還沒有收完的資料（%s）"
                      % (code, plan["date"], clock24(OPEN_MIN + 5 * k), plan["entry_time"], reason))
    eb = entry_day[k]
    warn = []
    tick = float(tw.tick_size(plan["entry"], code))
    if not eb["l"] - tick / 2 <= plan["entry"] <= eb["h"] + tick / 2:
        warn.append("計畫的進場價 %s 不在那根 K 棒的 %s 到 %s 之間"
                    % (tw.fmt_price(plan["entry"], code), tw.fmt_price(snap(eb["l"], code), code),
                       tw.fmt_price(snap(eb["h"], code), code)))
    for what, price in (("進場價", plan["entry"]), ("停損", plan["stop"]), ("停利", plan["target"])):
        if not tw.on_tick(round(price, 6), code):
            warn.append("%s %s 不在升降單位上（這個價位一檔是 %s）"
                        % (what, price, "%g" % float(tw.tick_size(price, code))))
    unknown = cal.unknown_note(datetime.date.fromisoformat(plan["date"]))
    if unknown:
        warn.append(unknown)
    sessions, short = full_sessions(days, plan["date"], session_dates(result))
    for s in short:
        why = cal.closed(datetime.date.fromisoformat(s["date"]))
        if why:
            s["closed"] = why
    if len(sessions) < MIN_SESSIONS:
        raise TMError("資料裡 %s 在 %s 之前只有 %d 個完整交易日；時光機至少要 %d 個"
                      % (code, plan["date"], len(sessions), MIN_SESSIONS))
    lu, ld, lsrc, lnote = resolve_limits(plan, meta, days)
    if lnote:
        warn.append(lnote)
    long = plan["side"] == "buy"
    if lu and ((plan["target"] > lu) if long else (plan["stop"] > lu)):
        warn.append("%s %s 高於漲停 %s：重播碰不到" % ("停利" if long else "停損",
                                                tw.fmt_price(plan["target"] if long else plan["stop"], code),
                                                tw.fmt_price(lu, code)))
    if ld and ((plan["stop"] < ld) if long else (plan["target"] < ld)):
        warn.append("%s %s 低於跌停 %s：重播碰不到，跌停鎖住時也賣不掉"
                    % ("停損" if long else "停利", tw.fmt_price(plan["stop"] if long else plan["target"], code),
                       tw.fmt_price(ld, code)))
    rate, cnote = resolve_cost(plan, config or {})
    if cnote:
        warn.append(cnote)
    cost_r = plan["entry"] * rate / plan["risk_per_share"]
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  lu, ld, cost_r)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], limits_source=lsrc, cost_per_share=round(plan["entry"] * rate, 4),
              break_even=round(lean(mc)[2], 4) if lean(mc) else None,
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], code, rate)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "略過：歷史重跑只認得開盤區間突破（orb），這份計畫的型態是 %r" % plan["setup"]}
    last_bar = bars[-1]
    who = "%s %s" % (plan["name"], code) if plan.get("name") else code
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or tw.yahoo_symbol(code, plan["market"]), "interval": "5m", "range": "60d",
                 "last_bar": "%s %s 台北" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("這是用%s 最近 %d 個完整交易日做的模擬，不是預測：新聞、處置、漲跌停鎖死都可能做出這些"
                 "交易日沒發生過的事。13:25 到 13:30 的收盤集合競價不在 5 分 K 資料裡，沒有模擬。"
                 "同一個種子，同樣的數字。" % (who, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f%%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    code = p["symbol"]
    long = p["side"] == "buy"
    until = m["until"]
    r = p["target_r"]
    fp = lambda x: tw.fmt_price(x, code)  # noqa: E731
    who = "%s %s" % (p["name"], code) if p.get("name") else code
    risk_ticks = tw.tick_steps(p["stop"], p["entry"], code) if long else tw.tick_steps(p["entry"], p["stop"], code)
    lines = [
        "%s %s，%s：進場 %s（%s，第 %d 根），停損 %s，停利 %s（%gR，每股風險 %s，%d 檔）。"
        % (who, "做多" if long else "放空", tw.day_label(datetime.date.fromisoformat(p["date"])), fp(p["entry"]),
           p["entry_time"], p["entry_bar"], fp(p["stop"]), fp(p["target"]), r,
           ("%.4f" % p["risk_per_share"]).rstrip("0").rstrip("."), risk_ticks),
        "重播當天剩下的盤 %s 次：%s 到 %s（%d 根 5 分 K），亂數種子 %d。"
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "每一根都整根抽自%s 過去 %d 個完整交易日（%s 到 %s）同一時段（前後 %d 根）的 5 分 K。"
        % (who, m["pool_sessions"], m["pool_from"], m["pool_to"], m["window"]),
    ]
    if m.get("limit_up") and m.get("limit_down"):
        lines.append("每一根重播都夾在漲停 %s 和跌停 %s 之間。" % (fp(m["limit_up"]), fp(m["limit_down"])))
    lines += [
        "成本：每股約 %.2f（手續費買賣各一次，加當沖證交稅），是 %.2fR。" % (m["cost_per_share"], m["cost_r"]),
        "",
        "  先到停利   %s" % pc(m["p_target"]),
        "  先到停損   %s" % pc(m["p_stop"]),
        "  都沒碰到   %-7s %s 還沒出場" % (pc(m["p_neither"]), until),
        "  站上進場價 %-7s 先到停利，或 %s 時%s進場價" % (pc(m["p_green"]), until, "高於" if long else "低於"),
        "",
        "  %s 的價格：中位數 %.2f，90%% 的重播在 %.2f 到 %.2f 之間。"
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  平均 R：%+.3f，扣成本 %+.3f（R 是每股風險：停利 +%gR，停損 -1R）。"
        % (m["avg_R"], m["avg_R_net"], m["r_at_target"]),
    ]
    if m.get("p_limit_up") or m.get("p_limit_down"):
        lines.append("  %.2f%% 的重播碰到漲停，%.2f%% 碰到跌停。"
                     % (100.0 * m.get("p_limit_up", 0), 100.0 * m.get("p_limit_down", 0)))
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = STOP_WORDS.get(ru["stop_rule"], "進場價下方 %.2f%%" % ru.get("stop_pct", 0)
                                    if long else "進場價上方 %.2f%%" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("歷史：同一條規則（%d 分鐘開盤區間、停損在%s、%gR 停利）在這 %d 個真實交易日觸發 %d 次："
                         "停利 %d、停損 %d、都沒碰到 %d。平均 R %+.3f，扣成本 %+.3f。"
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]))
        else:
            lines.append("歷史：同一條規則在這 %d 個交易日一次都沒觸發。" % h["sessions"])
    else:
        lines.append("歷史：" + h.get("note", "略過。"))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("注意：" + w + "。")
    left_out = doc["data"]["short_sessions_left_out"]
    closed = [x for x in left_out if x.get("closed")]
    gaps = [x for x in left_out if not x.get("closed")]
    if closed:
        lines.append("略過休市日：%s。" % "、".join("%s（%s）" % (x["date"], x["closed"]) for x in closed))
    if gaps:
        lines.append("略過 %d 個 K 棒不完整的交易日：%s。"
                     % (len(gaps), "、".join("%s（%d 根）" % (x["date"], x["bars"]) for x in gaps)))
    lines.append(doc["note"] + "用 --seed %d 可以重跑。" % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "停利", row(p["stop"]): "停損"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "進場"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    who = "%s %s" % (p["name"], p["symbol"]) if p.get("name") else p["symbol"]
    out = ["%s %s，從 %s 進場的 %s 條重播路徑（亂數種子 %d）" % (
        who, p["date"], p["entry_time"], "{:,}".format(m.get("band_paths", BAND_PATHS)), m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"停利": p["target"], "停損": p["stop"], "進場": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* 中位數   : 中間一半的路徑   . 90% 的路徑   - 停損和停利")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    cfg = read_config(a.config)
    trader_dir = os.path.dirname(a.config) or "."
    cal = tw.load_calendar(trader_dir, cfg)
    if not plan["market"]:
        plan["market"] = (tw.load_universe(os.path.join(trader_dir, "universe.txt")).get(plan["symbol"]) or {}).get(
            "market")
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, market = fetch_for(plan["symbol"], plan["market"])
        plan["market"], source = market, "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, cfg, cal)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("已寫入 %s。" % a.write)
        print("下一步：/trader-budget 依你的預算和單筆最大虧損算股數。")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("沒有 %s：先跑 timemachine.py run --write" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s 不是時光機的結果" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="時光機：在下單前重播一筆當沖計畫（trader-timemachine）。")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="trader-exit 寫的計畫")
        p.add_argument("--config", default="trader/config.json", help="手續費折扣等設定（可以沒有）")
        p.add_argument("--symbol", help="或用參數直接給計畫：台股代號，例如 2303")
        p.add_argument("--market", choices=["TWSE", "TPEx"], help="上市 TWSE 或上櫃 TPEx（不給就自己找）")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="計畫的日期 YYYY-MM-DD（預設台北的今天）")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help="進場那根 K 棒的收盤時間，例如 09:20")
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="從同一時段前後這麼多根裡抽")
        p.add_argument("--until", help="每次重播在這個時間結束（預設 13:25，連續交易結束）")
        p.add_argument("--offline", metavar="FILE", help="存好的 Yahoo 線圖 JSON（.json 或 .json.gz），不連網")
        p.add_argument("--now", help='假裝現在是這個時間（"2026-11-02 09:25" 台北時間，或帶 Z 的 ISO）')

    p = sub.add_parser("run", help="重播計畫：10,000 次模擬加上歷史")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="存下結果（預設 trader/timemachine.json）")
    p = sub.add_parser("chart", help="重播路徑的 ASCII 扇形圖")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="畫存好的結果，不重跑")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
