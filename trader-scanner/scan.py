#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan every Taiwan chart for setups. Standard library only.

    python3 scan.py universe                          # TWSE + TPEx ISIN lists -> trader/universe.txt
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols 2330,2303,6488    # or --watchlist FILE, --limit 200, --kind 股票
    python3 scan.py orb                               # after 09:20: opening-range breakouts so far
    python3 scan.py orb --at 09:45 --or-minutes 15    # cut the clock at 09:45 Taipei time
    python3 scan.py orb --offline wall5m.json.gz --now "2026-11-02 09:45" --universe universe.txt

universe  Downloads the ISIN lists of the TWSE (C_public.jsp?strMode=2) and
          the TPEx (strMode=4): cp950 HTML pages, rows grouped under one-cell
          section headers. Keeps three sections: 股票, 創新板 and ETF.
          Warrants, preferred shares, TDRs, ETNs, REITs and asset-backed
          securities are left out. Writes code|name|market|kind a line, and
          refreshes the TWSE trading calendar for this year and next
          (trader/state/calendar-YYYY.json; a year not yet published is a note).

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity: the last complete
          close >= --min-price (10) and Yahoo's regularMarketVolume >=
          --min-volume (1,000,000 shares, 1,000 張). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 13:30 close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: the 09:00, 09:05 and 09:10 bars.
          Stage 1: spark closes for every symbol (1 day of 5-minute bars, 20 a
          request); a chart goes on only if a completed bar after the range
          closed at or above the highest close of the range (a necessary
          condition). Stage 2: each candidate's v8 chart (real highs and lows):
          range high = the highest high of the range bars, a hit = the first
          completed bar to CLOSE above it, at that bar's close time. Stage 2
          fetches at most --max-confirm (200) charts, the most traded first.
          A chart is usable only when every 5-minute bar from 09:00 to the
          cutoff is there. Floors: --min-price 10 (last close), --min-volume 0.

處置股    The TWSE and TPEx disposition lists (處置有價證券). A symbol inside
          an active disposition period trades by call auction every few
          minutes; it is left out of the hits and named in one note line
          (--keep-disposition keeps it, flagged). Offline runs check it only
          with --disposition PATH (a folder or file of the saved lists).

Bars: null closes are dropped, a bar off the 5-minute grid (a live tick) is
dropped, a bar counts only once it is complete (start + 5 minutes <= the
clock), and only bars of continuous trading count: 09:00 to 13:20 starts.
Yahoo leaves the 13:25 auction bar empty and prints the close at 13:30 with
no volume; both are ignored. Times are Taipei (UTC+8, no daylight saving).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with C_public-2.html and C_public-4.html; daily and orb take a folder of
<CODE>.TW.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"2303.TW": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

UA = tw.UA
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=%s"
ISIN_PAGES = (("TWSE", "2"), ("TPEx", "4"))
ISIN_FILE = "C_public-%s.html"
KEEP_SECTIONS = ("股票", "創新板", "ETF")
MARKET_LABEL = {"TWSE": "上市", "TPEx": "上櫃"}
PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
LOT = tw.LOT
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_CONFIG = "trader/config.json"
NOTE = "掃描結果是符合規則的圖，不是買進清單。"

SETUPS = [
    ("breakout_20d", "收盤創 20 日新高"),
    ("bull_flag", "多頭旗形"),
    ("reclaim_ma20", "站回 20 日均線"),
    ("oversold", "超賣（RSI 低於 30）"),
    ("oversold_bounce", "超賣反彈"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is Taipei time."""
    try:
        return tw.parse_now(text)
    except ValueError as e:
        raise ScanError(str(e))


def parse_hhmm(text, flag="--at"):
    try:
        return tw.parse_hhmm(text, flag)
    except ValueError as e:
        raise ScanError(str(e))


def ts_to_taipei(ts):
    """Unix seconds -> naive Taipei time. Never uses the machine's time zone."""
    return tw.ts_to_taipei(ts)


def calendar(cal=None):
    return cal if cal is not None else tw.Calendar()


def watch_day(now_utc, cal=None):
    """The session a pre-market list is for: today before the 13:30 close, else the next trading day."""
    cal = calendar(cal)
    now = tw.to_taipei(now_utc)
    d = now.date()
    if cal.is_trading_day(d) and now.time() < tw.CLOSE:
        return d
    return cal.next_trading_day(d)


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_get_bytes(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo 回應 %s（%s）" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo 回應 %s（%s）" % (last, path.split("?")[0]))


def to_yahoo(sym):
    return sym.strip().upper()


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(to_yahoo(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym), rng, interval)


# ---------------------------------------------------------------- symbols

def split_key(sym):
    """'2303.TW' -> ('2303', 'TWSE'); '6488.TWO' -> ('6488', 'TPEx')."""
    try:
        code, market = tw.parse_symbol(sym)
    except ValueError:
        return sym, None
    return code, market or "TWSE"


def kind_of(code, uni=None):
    info = (uni or {}).get(code) or {}
    return info.get("kind") or ("ETF" if tw.is_etf_like(code) else "股票")


def ident(sym, uni=None, meta=None):
    """The watchlist's view of one chart: symbol (bare code), market, name, kind."""
    code, market = split_key(sym)
    info = (uni or {}).get(code) or {}
    name = info.get("name") or (meta or {}).get("shortName") or ""
    return {"symbol": code, "market": info.get("market") or market, "name": name, "kind": kind_of(code, uni)}


def resolve(items, uni):
    """[(code, market or None)] -> (Yahoo symbols, pairs {code.TW: code.TWO}). A bare code the
    universe does not know is asked as both; settle() keeps the one Yahoo answers."""
    out, pairs = [], {}
    for code, market in items:
        market = market or ((uni.get(code) or {}).get("market"))
        if market:
            syms = [tw.yahoo_symbol(code, market)]
        else:
            syms = [code + ".TW", code + ".TWO"]
            pairs[syms[0]] = syms[1]
        for s in syms:
            if s not in out:
                out.append(s)
    return out, pairs


def settle(symbols, pairs, data, failed):
    """Keep one symbol of each .TW / .TWO pair: the one with data (.TW when neither has any)."""
    twins = set(pairs.values())
    out = []
    for s in symbols:
        if s in twins:
            continue
        alt = pairs.get(s)
        if alt and s not in data and alt in data:
            failed.pop(s, None)
            out.append(alt)
            continue
        if alt:
            failed.pop(alt, None)
        out.append(s)
    return out


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def _snap(c, code):
    try:
        return float(tw.to_tick(c, code))
    except ValueError:
        return float(c)


def densify(series, code="", cal=None):
    """Spark leaves out every close that lies on the straight line (in time) between its
    neighbors, and Taiwan's coarse ticks make that common: 108.0, (108.5), 109.0, or a run of
    equal closes. Put the dropped points back: the missing 5-minute slots of a day, or the
    missing trading days of a daily series, interpolated by time and snapped to the tick grid.
    That is exact for what Yahoo dropped (checked against v8 charts on 2026-11-02); a day with
    no trades at all would be filled too, so the v8 chart stays the word on missing bars.
    Every close is snapped to the tick grid, which also clears Yahoo's float noise.
    A daily point on a day the market was closed (Yahoo keeps a 2026-10-23 bar, a typhoon day,
    with yesterday's close) is left out of the result, but it still anchors the line: Yahoo
    chose what to leave out with that point in its series."""
    cal = calendar(cal)
    daily = (series.get("meta") or {}).get("dataGranularity") == "1d"
    pts = [(t, _snap(c, code)) for t, c in zip(series["t"], series["c"]) if c is not None]
    out, filled, offdays = [], 0, []
    for k, (t, c) in enumerate(pts):
        if k:
            t0, c0 = pts[k - 1]
            d0, d1 = ts_to_taipei(t0), ts_to_taipei(t)
            gaps = []
            if daily:
                d = d0.date() + datetime.timedelta(days=1)
                while d < d1.date():
                    if cal.is_trading_day(d):
                        gaps.append(t0 + (d - d0.date()).days * 86400)
                    d += datetime.timedelta(days=1)
            elif d0.date() == d1.date():
                g = d0.replace(second=0) + datetime.timedelta(minutes=BAR_MIN - d0.minute % BAR_MIN)
                while g < d1:
                    gaps.append(tw.taipei_to_ts(g))
                    g += datetime.timedelta(minutes=BAR_MIN)
            for tg in gaps:
                out.append((tg, _snap(c0 + (c - c0) * (tg - t0) / float(t - t0), code)))
                filled += 1
        d = ts_to_taipei(t).date()
        if daily and not cal.is_trading_day(d):
            offdays.append(d.isoformat())
            continue
        out.append((t, c))
    return dict(series, t=[t for t, _ in out], c=[c for _, c in out], h=None, l=None, o=None, v=None,
                filled=filled, offdays=offdays)


def parse_spark(text_or_doc, wanted=None, cal=None):
    """Spark JSON -> {SYM: series}, keyed by Yahoo symbol (2303.TW), with the points spark
    left out put back (densify). Symbols Yahoo does not know are simply missing from the
    answer. `wanted` maps Yahoo symbols back to ours."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo spark 的回應格式不是這支工具認得的")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = (item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or "").upper()
        out[(wanted or {}).get(ysym, ysym)] = densify(series_from_result(resp[0]), split_key(ysym)[0], cal)
    if err and not out:
        raise ScanError("Yahoo spark：%s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo 回了無法解讀的資料（%s）" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("%s 沒有資料：%s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _chart_key(name_hint, doc):
    """The Yahoo symbol of an offline v8 chart: the bundle key or file name, else the chart's meta."""
    meta_sym = ""
    try:
        meta_sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"] or ""
    except (AttributeError, KeyError, TypeError, IndexError):
        pass
    sym = (name_hint or meta_sym).strip().upper()
    if not sym:
        raise ScanError("離線的 v8 圖沒有代號：把檔名取成 <代號>.TW.json")
    if "." not in sym:  # a bare code: take the market from the chart, else TWSE
        sym = meta_sym.upper() if meta_sym.upper().startswith(sym + ".") else sym + ".TW"
    return sym


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = _chart_key(name_hint, doc)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("離線檔 %s 不是 spark 回應、v8 圖或打包檔" % name_hint)


def load_offline(path):
    """A folder of <SYM>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("%s 裡沒有 .json 檔" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s 不是有效的 JSON：%s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s 不是有效的 JSON：%s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("%s 沒有離線檔案或資料夾" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def parse_isin(text):
    """One ISIN page (decoded) -> (updated 'YYYY/MM/DD' or None, [(section, code, name)])."""
    m = re.search(r"最近更新日期\s*[:：]\s*([\d/]+)", text)
    rows, section = [], None
    for r in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [re.sub(r"<.*?>", "", c).strip() for c in re.findall(r"<td.*?>(.*?)</td>", r, re.S)]
        if not cells:
            continue
        if len(cells) == 1 or "colspan" in r:
            section = cells[0].strip()
            continue
        code, _, name = cells[0].partition("　")
        rows.append((section, code.strip(), name.strip()))
    if not rows:
        raise ScanError("這不是證交所的 ISIN 清單頁")
    return (m.group(1) if m else None), rows


def build_universe(pages):
    """{market: decoded ISIN page} -> (kept [(code, name, market, kind)], kept_by {market: {kind: n}},
    dropped {(market, section): n}, rows, updated)."""
    kept, seen, kept_by, dropped, rows_n, updated = [], set(), {}, {}, 0, None
    for market, _ in ISIN_PAGES:
        if market not in pages:
            raise ScanError("少了%s的 ISIN 清單" % MARKET_LABEL[market])
        up, rows = parse_isin(pages[market])
        updated = updated or up
        rows_n += len(rows)
        for section, code, name in rows:
            if section not in KEEP_SECTIONS:
                key = (market, section or "未分類")
            elif not tw.CODE.match(code):
                key = (market, "代號格式不符")
            elif code in seen:
                key = (market, "重複")
            else:
                seen.add(code)
                kept.append((code, name, market, section))
                kept_by.setdefault(market, {})
                kept_by[market][section] = kept_by[market].get(section, 0) + 1
                continue
            dropped[key] = dropped.get(key, 0) + 1
    return kept, kept_by, dropped, rows_n, updated


def _kind(text):
    """A space before a Latin word: '245 檔 ETF', but '1,058 檔股票'."""
    return (" " + text) if text[:1].isascii() else text


def drop_label(market, section):
    if section.startswith(("上市", "上櫃")):
        return section
    return "%s%s" % (MARKET_LABEL[market], _kind(section))


def kept_line(kept_by):
    parts = []
    for market, _ in ISIN_PAGES:
        k = kept_by.get(market) or {}
        bits = ["%s 檔%s" % ("{:,}".format(k[s]), _kind(s)) for s in KEEP_SECTIONS if k.get(s)]
        if bits:
            parts.append("%s %s" % (MARKET_LABEL[market], "、".join(bits)))
    return "；".join(parts)


def refresh_calendar(years, trader_dir):
    """Fetch and cache the TWSE calendar for each year -> ({year: closed days or None}, note lines)."""
    got, notes = {}, []
    for y in years:
        try:
            closed = tw.fetch_calendar(y, get=http_get)
        except ValueError as e:
            got[y] = None
            notes.append("%s。" % e)
            continue
        except (urllib.error.URLError, OSError) as e:
            got[y] = None
            notes.append("%d 年的休市日抓不到（%s），沿用舊的。" % (y, getattr(e, "reason", e)))
            continue
        tw.save_calendar(y, closed, trader_dir)
        got[y] = closed
        weekdays = sum(1 for d in closed if datetime.date.fromisoformat(d).weekday() < 5)
        notes.append("%d 年的休市日已更新：%d 個平日休市。" % (y, weekdays))
    return got, notes


# ---------------------------------------------------------------- 處置股

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_MATCH = re.compile(r"每\s*([0-9０-９一二三四五六七八九十]+)\s*分鐘撮合")


def _cn_int(text):
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_CN_NUM.get(tens, 1) if tens else 1) * 10 + (_CN_NUM.get(ones, 0) if ones else 0)
    return _CN_NUM.get(text)


def parse_period(text):
    """'115/10/28～115/11/03' or '1151102~1151110' -> (start, end) dates, or None."""
    parts = [p for p in re.split(r"\s*[～~]\s*", str(text or "").strip()) if p]
    if len(parts) != 2:
        return None
    try:
        return tw.roc_date(parts[0]), tw.roc_date(parts[1])
    except ValueError:
        return None


def parse_disposition(twse_rows=None, tpex_rows=None):
    """The two disposition lists -> {code: {name, market, start, end, every_minutes}}."""
    out = {}
    rows = [(r, "TWSE", "Code", "Name", "Detail") for r in (twse_rows or [])]
    rows += [(r, "TPEx", "SecuritiesCompanyCode", "CompanyName", "DisposalCondition") for r in (tpex_rows or [])]
    for r, market, k_code, k_name, k_text in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get(k_code) or "").strip()
        period = parse_period(r.get("DispositionPeriod"))
        if not code or not period:
            continue
        m = _MATCH.search(str(r.get(k_text) or ""))
        every = _cn_int(m.group(1)) if m else None
        old = out.get(code)
        if old:  # two dispositions of one stock: cover both
            period = (min(old["start"], period[0]), max(old["end"], period[1]))
            every = every or old["every_minutes"]
        out[code] = {"name": str(r.get(k_name) or "").strip(), "market": market, "start": period[0],
                     "end": period[1], "every_minutes": every}
    return out


def active_disposition(disp, day):
    return {c: v for c, v in (disp or {}).items() if v["start"] <= day <= v["end"]}


def load_disposition(path):
    """A folder or file of saved lists (TWSE punish and TPEx disposal JSON) -> parse_disposition."""
    if os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path))
                 if f.endswith(".json") and ("punish" in f or "disposal" in f)]
    elif os.path.isfile(path):
        files = [path]
    else:
        raise ScanError("%s 沒有處置股清單" % path)
    twse, tpex = [], []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            try:
                rows = json.load(fh)
            except ValueError as e:
                raise ScanError("%s 不是有效的 JSON：%s" % (f, e))
        for r in rows if isinstance(rows, list) else []:
            (tpex if isinstance(r, dict) and "SecuritiesCompanyCode" in r else twse).append(r)
    return parse_disposition(twse, tpex)


def fetch_disposition():
    """Both lists from the network -> (dispositions, note). A failure is a note, not an error."""
    try:
        twse = json.loads(http_get(PUNISH_URL))
        tpex = json.loads(http_get(TPEX_DISPOSAL_URL))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, "處置股清單抓不到（%s）：這次沒有排除處置股。" % getattr(e, "reason", e)
    return parse_disposition(twse, tpex), ""


def disposition_for(a):
    """-> (dispositions or None when not checked, note)."""
    if a.disposition:
        if a.disposition.lower() == "none":
            return None, "沒有檢查處置股（--disposition none）。"
        return load_disposition(a.disposition), ""
    if a.offline:
        return None, "離線重播沒有檢查處置股（加 --disposition 指向存下來的處置股清單）。"
    return fetch_disposition()


def apply_disposition(hits, disp, day, keep):
    """-> (hits, held [(code, info)]). Held hits are dropped unless keep, then flagged."""
    if disp is None:
        return hits, []
    act = active_disposition(disp, day)
    out, held = [], []
    for h in hits:
        info = act.get(h["symbol"])
        if not info:
            out.append(h)
            continue
        held.append((h["symbol"], info))
        if keep:
            out.append(dict(h, disposition={"until": info["end"].isoformat(),
                                            "every_minutes": info["every_minutes"]}))
    return out, held


def disposition_line(held, keep, names):
    if not held:
        return ""
    bits = []
    for code, info in held:
        every = ("，約每 %d 分鐘撮合一次" % info["every_minutes"]) if info["every_minutes"] else ""
        bits.append("%s %s（處置到 %d/%d%s）" % (code, names.get(code) or info["name"], info["end"].month,
                                              info["end"].day, every))
    head = "處置股照樣列出，但成交是分盤集合競價" if keep else "處置股不列入（%d 檔）" % len(held)
    return "%s：%s。" % (head, "、".join(bits))


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc, cal=None):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the 13:30 close, anything dated after the clock dropped, and any bar on a day the
    market was closed dropped (Yahoo keeps one for the 2026-10-23 typhoon day)."""
    cal = calendar(cal)
    now = tw.to_taipei(now_utc)
    today = now.date()
    today_done = not cal.is_trading_day(today) or now.time() >= tw.CLOSE
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_taipei(ts).date()
        if d > today or (d == today and not today_done) or not cal.is_trading_day(d):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def offdays(series, cal=None):
    """Dates of the daily bars Yahoo sent for days the market was closed."""
    cal = calendar(cal)
    days = set(series.get("offdays") or [])
    for ts, c in zip(series["t"], series["c"]):
        d = ts_to_taipei(ts).date()
        if c is not None and not cal.is_trading_day(d):
            days.add(d.isoformat())
    return days


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume, cal=None, uni=None):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates, off = [], {}, set()
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0, "offday": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        o = offdays(s, cal)
        if o:
            counts["offday"] += 1
            off |= o
        bars = daily_closes(s, now_utc, cal)
        meta = s.get("meta") or {}
        price = bars[-1][1] if bars else meta.get("regularMarketPrice")
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append(dict(ident(sym, uni, meta), setups=tags, price=round(closes[-1], 2),
                             close_date=d_last.isoformat(), rsi=round(nums["rsi"], 1),
                             volume=int(vol) if vol is not None else None))
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most NT$ traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["offday_dates"] = sorted(off)
    counts["hits"] = len(hits)
    return hits, counts, last_date


def offday_line(counts):
    if not counts.get("offday"):
        return ""
    return ("Yahoo 的日線裡有 %s 檔帶著休市日的 K 棒（%s，收盤價照抄前一天），已略過，不算進型態。"
            % ("{:,}".format(counts["offday"]), "、".join(counts["offday_dates"])))


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, tw.OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of continuous trading on `day` that are complete at `cutoff` (naive Taipei
    time), on the grid, with a close. Sorted, one per slot. The empty 13:25 auction bar and the
    13:30 print of the close are left out."""
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        t = ts_to_taipei(ts)
        if t.date() != day or t.second or t.minute % BAR_MIN:
            continue  # another day, or a live tick off the grid
        if t.time() < tw.OPEN or t.time() >= tw.CLOSING_AUCTION:
            continue  # before the open, the closing auction, the 13:30 print
        if t + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[t] = {"start": t, "c": float(c),
                  "h": None if not hs or hs[i] is None else float(hs[i]),
                  "l": None if not ls or ls[i] is None else float(ls[i]),
                  "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 09:00 to the cutoff (or 13:25) is there, and at least the range."""
    end = min(cutoff, datetime.datetime.combine(day, tw.CLOSING_AUCTION))
    want = int((end - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": tw.hm(closes_at),
                    "bar_start": tw.hm(b["start"]), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def at_limit_up(code, price, meta):
    """True when the price is today's limit up, worked out from Yahoo's previous close."""
    prev = (meta or {}).get("chartPreviousClose") or (meta or {}).get("previousClose")
    if not prev:
        return False
    try:
        up, _ = tw.price_limits(prev, code)
    except ValueError:
        return False
    return float(up) == round(float(price), 2)


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, cal=None):
    """-> (day, cutoff naive Taipei) or raise ScanError with the plain message."""
    now = tw.to_taipei(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes 要是 5 到 120 之間 5 的倍數（15 是最常用的）")
    why = calendar(cal).closed(day)
    if why:
        raise ScanError("%s休市（%s）：沒有交易。請在交易日的開盤區間之後再跑 orb，或用 --offline 重播過去的交易日。"
                        % (tw.day_label(day), why))
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, tw.CLOSE)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s 比現在（%s）還晚。" % (tw.hm(at), tw.hm(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s 在 %s 13:30 收盤之後。" % (tw.hm(at), tw.day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("%s 已在 13:30 收盤。要重播今天，加上 --at 09:45。" % tw.day_label(day))
    if cutoff < first_close:
        raise ScanError("現在 %s：%d 分鐘的開盤區間是 09:00 到 %s，區間後的第一根 K 棒 %s 才收完。請在 %s 之後再跑 orb。"
                        % (tw.hm(cutoff), or_minutes, tw.hm(range_end), tw.hm(first_close), tw.hm(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("現在 %s，已經過了 %s（no_entries_after，不再新進場的時間）：這時才找到的開盤區間突破已經過期。"
                        "下個交易日 %s 之後再跑，或用 --at 09:45 重播今天。"
                        % (tw.hm(cutoff), tw.hm(until), tw.hm(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None, uni=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol, s))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _, s in cands:
        full, meta = bars, s.get("meta") or {}
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            meta = s2.get("meta") or meta
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "v8 圖少了 K 棒"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hit = dict(ident(sym, uni, meta), setups=["orb"], **h)
            if at_limit_up(hit["symbol"], h["price"], meta):
                hit["at_limit_up"] = True
            hits.append(hit)
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "Yahoo 沒有資料"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark：%d / %d 次請求" % (k + 1, len(batches)))
    return data, failed


def parse_items(text):
    """'2330, 6488.TWO' -> [(code, market or None)]."""
    out = []
    for s in re.split(r"[,\s]+", text or ""):
        if not s:
            continue
        try:
            out.append(tw.parse_symbol(s))
        except ValueError as e:
            raise ScanError(str(e))
    return out


def items_from_file(path):
    """A watchlist.json (hits), a universe file (code|name|market|kind) or a text list -> [(code, market)]."""
    if not os.path.exists(path):
        raise ScanError("%s 沒有這個檔案" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s 不是有效的 JSON：%s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        out = []
        for x in items:
            if not x:
                continue
            sym = x.get("symbol") if isinstance(x, dict) else x
            code, market = parse_items(str(sym))[0]
            out.append((code, market or (x.get("market") if isinstance(x, dict) else None)))
        return out
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("|")
        code, market = parse_items(parts[0].strip())[0]
        if not market and len(parts) > 2 and parts[2].strip() in tw.MARKETS:
            market = parts[2].strip()
        out.append((code, market))
    return out


def read_symbols(a, uni, offline_syms=None):
    """-> (Yahoo symbols, .TW/.TWO pairs to settle)."""
    if a.symbols:
        items = parse_items(a.symbols)
    elif a.watchlist:
        items = items_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        items = [split_key(s) for s in offline_syms]  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("還沒有 %s：先跑 `scan.py universe`，或用 --symbols 指定代號" % path)
        items = items_from_file(path)
    if a.kind:
        kinds = set(k for k in re.split(r"[,\s]+", a.kind) if k)
        items = [it for it in items if kind_of(it[0], uni) in kinds]
    seen, uniq = set(), []
    for it in items:
        if it[0] not in seen:
            seen.add(it[0])
            uniq.append(it)
    if a.limit:
        uniq = uniq[:a.limit]
    if not uniq:
        raise ScanError("沒有可以掃描的代號")
    return resolve(uniq, uni)


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s 不是有效的 JSON：%s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def trader_dir(config_path):
    return os.path.dirname(config_path) if config_path else "trader"


def write_json(path, doc):
    tw.write_json(path, doc)


def lots(shares):
    return "{:,}".format(int(shares) // LOT)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    pages = {}
    for k, (market, mode) in enumerate(ISIN_PAGES):
        if a.offline:
            p = os.path.join(a.offline, ISIN_FILE % mode)
            if not os.path.exists(p):
                raise ScanError("%s 裡沒有 %s" % (a.offline, ISIN_FILE % mode))
            with open(p, "rb") as fh:
                raw = fh.read()
        else:
            if k and a.pause:
                SLEEP(a.pause)
            try:
                raw = http_get_bytes(ISIN_URL % mode)
            except urllib.error.HTTPError as e:
                raise ScanError("isin.twse.com.tw 回應 HTTP %d（%s清單）" % (e.code, MARKET_LABEL[market]))
            except (urllib.error.URLError, OSError) as e:
                raise ScanError("連不上 isin.twse.com.tw：%s" % getattr(e, "reason", e))
        pages[market] = raw.decode("cp950", "replace")
    kept, kept_by, dropped, rows_n, updated = build_universe(pages)
    cal_notes, cal_years = [], {}
    if not a.no_write:
        with open(tw_mkdirs(a.out), "w", encoding="utf-8") as fh:
            for row in kept:
                fh.write("|".join(row) + "\n")
        if a.offline:
            cal_notes.append("離線模式：沒有更新休市日。")
        else:
            year = tw.to_taipei(parse_now(a.now)).year
            cal_years, cal_notes = refresh_calendar((year, year + 1), os.path.dirname(a.out) or ".")
    drop_items = sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))
    if a.json:
        print(json.dumps({"count": len(kept), "rows": rows_n, "updated": updated, "kept": kept_by,
                          "dropped": {drop_label(m, s): n for (m, s), n in drop_items},
                          "out": None if a.no_write else a.out,
                          "calendar": {str(y): (len(c) if c else None) for y, c in cal_years.items()},
                          "notes": cal_notes, "seconds": round(time.time() - t0, 1)}, ensure_ascii=False, indent=2))
    else:
        print("證交所 ISIN 清單（上市與上櫃%s）：%s 列。" % (("，%s 更新" % updated) if updated else "",
                                                       "{:,}".format(rows_n)))
        print("保留 %s 檔：%s。" % ("{:,}".format(len(kept)), kept_line(kept_by)))
        print("排除：" + "、".join("%s %s" % (drop_label(m, s), "{:,}".format(n)) for (m, s), n in drop_items) + "。")
        if not a.no_write:
            print("已寫入 %s（一行一檔：代號|名稱|市場|類別）。" % a.out)
        for n in cal_notes:
            print(n)
    return 0 if kept else 1


def tw_mkdirs(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return path


def _names(hits):
    return {h["symbol"]: h.get("name") or "" for h in hits}


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    cfg = read_config(a.config)
    cal = tw.load_calendar(trader_dir(a.config), cfg)
    uni = tw.load_universe(a.universe or DEFAULT_UNIVERSE)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols, pairs = read_symbols(a, uni, offline_syms)
    if not a.offline:
        progress(a, "抓 %s 檔的日收盤價（%d 次 spark 請求）..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    symbols = settle(symbols, pairs, data, failed)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, cal, uni)
    day = watch_day(now, cal)
    disp, disp_note = disposition_for(a)
    hits, held = apply_disposition(hits, disp, day, a.keep_disposition)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s 收盤" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "for_session": day.isoformat(), "universe": len(symbols), "scanned": counts["scanned"],
           "failed": nfailed, "source": "yahoo", "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "disposition": {"checked": disp is not None, "held": [c for c, _ in held]},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), ensure_ascii=False, indent=2))
        return 0 if hits else 1
    local = tw.to_taipei(now)
    print("每日掃描，%s%s，收盤價到 %s（Yahoo spark，只有日收盤價），給 %s 的盤用。" % (
        tw.day_label(local.date()), tw.hm(local), last_date.isoformat() if last_date else "（沒有）", day.isoformat()))
    note = cal.unknown_note(day)
    if note:
        print(note + "。")
    print("抓到 %s 檔中的 %s 檔圖%s。%s 檔通過流動性門檻（收盤價 %s 元以上、成交量 %s 張以上）%s；掃描 %s 檔。"
          % ("{:,}".format(len(symbols)), "{:,}".format(counts["fetched"]),
             ("（%s 檔失敗或沒有資料）" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price), lots(a.min_volume),
             ("，%d 檔不到 %d 天" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
             "{:,}".format(counts["scanned"])))
    if offday_line(counts):
        print(offday_line(counts))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d 檔符合型態：%s。" % (len(hits), "、".join("%d 檔%s" % (n, _kind(LABEL[k])) for k, n in per if n)))
        print("成交金額大的排前面（收盤價 x 成交量）：")
    else:
        print("沒有符合型態的圖。")
    for h in hits[:a.show]:
        print("  %-6s %9s  RSI %5.1f  %s  %s" % (h["symbol"], tw.fmt_price(h["price"], h["symbol"]), h["rsi"],
                                                "、".join(LABEL[t] for t in h["setups"]), h["name"]))
    if len(hits) > a.show:
        print("  ...還有 %d 檔在 watchlist 裡（--show N 可以多印）。" % (len(hits) - a.show))
    line = disposition_line(held, a.keep_disposition, _names(hits))
    for n in (line, disp_note):
        if n:
            print(n)
    if not a.no_write:
        print("已寫入 %s（mode daily，%d 檔）。" % (a.out, len(hits)))
    print(NOTE + "下一步：挑一張圖，再跑 /trader-news 代號。")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    cal = tw.load_calendar(trader_dir(a.config), cfg)
    uni = tw.load_universe(a.universe or DEFAULT_UNIVERSE)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or tw.NO_ENTRIES_AFTER, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_taipei(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("離線重播：請給 --at HH:MM 或 --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cal)
    symbols, pairs = read_symbols(a, uni, offline_syms)
    confirm = None
    if not a.offline:
        today = tw.to_taipei(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo 免費的 5 分 K 只留幾天：%s 請用 --offline 重播" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "第一階段：%s 檔的 5 分 K 收盤價（%d 次 spark 請求）..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  第二階段：已抓 %d 張 v8 圖" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    symbols = settle(symbols, pairs, data, failed)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm, uni)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    disp, disp_note = disposition_for(a)
    hits, held = apply_disposition(hits, disp, day, a.keep_disposition)
    as_of = "%s %s 台北" % (day.isoformat(), tw.hm(cutoff))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN, "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "disposition": {"checked": disp is not None, "held": [c for c, _ in held]},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), ensure_ascii=False, indent=2))
        return 0 if hits else 1
    print("開盤區間突破掃描，%s%s：%d 分鐘開盤區間（09:00 到 %s），5 分 K，Yahoo。"
          % (tw.day_label(day), tw.hm(cutoff), a.or_minutes, tw.hm(slot(day, a.or_minutes // BAR_MIN))))
    note = cal.unknown_note(day)
    if note:
        print(note + "。")
    print("抓到 %s 檔中的 %s 檔圖%s；%s 檔可用%s；%s 檔候選，%s 檔用真實高低點確認。"
          % ("{:,}".format(len(symbols)), "{:,}".format(counts["fetched"]),
             ("（%s 檔失敗或沒有資料）" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             ("（另有 %s 檔低於股價或成交量門檻）" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("只看了一部分：還有 %s 檔候選沒確認（只確認成交金額最大的 %s 檔；--max-confirm 0 全部確認）。"
              % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%s裡，有 %d 檔在 %s 前收盤站上開盤區間高點："
              % (("確認的 %s 檔" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else ("%s 檔可用的圖" % "{:,}".format(counts["scanned"])), len(hits), tw.hm(cutoff)))
        for h in hits[:a.show]:
            code = h["symbol"]
            print("  %s  %-6s %9s  區間 %s 到 %s  高出 %.2f%%  %s%s" % (
                h["at"], code, tw.fmt_price(h["price"], code), tw.fmt_price(h["or_low"], code),
                tw.fmt_price(h["or_high"], code), h["pct_over"], h["name"],
                "  漲停：可能買不到" if h.get("at_limit_up") else ""))
        if len(hits) > a.show:
            print("  ...還有 %d 檔在 watchlist 裡（--show N 可以多印）。" % (len(hits) - a.show))
    else:
        print("到 %s 為止，沒有圖收盤站上開盤區間高點。" % tw.hm(cutoff))
    line = disposition_line(held, a.keep_disposition, _names(hits))
    for n in (line, disp_note):
        if n:
            print(n)
    if not a.no_write:
        print("已寫入 %s（mode orb，%d 檔）。" % (a.out, len(hits)))
    print(NOTE + "下一步：挑一張圖，再跑 /trader-news 代號 和 /trader-entry 代號。")
    return 0 if hits else 1


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="Chart Scanner（trader-scanner）：每日型態與開盤區間突破。")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="下載上市、上櫃 ISIN 清單 -> trader/universe.txt，並更新休市日")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="DIR", help="有 C_public-2.html 和 C_public-4.html 的資料夾")
    p.add_argument("--now", help='假裝現在是這個時間（"2026-11-02 08:30" 台北時間）')
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="用逗號分開的代號，取代整個清單（2330,6488.TWO）")
        g.add_argument("--watchlist", metavar="FILE", help="watchlist.json（hits）或一行一個代號的文字檔")
        p.add_argument("--universe", help="代號清單，一行 代號|名稱|市場|類別（預設 %s）" % DEFAULT_UNIVERSE)
        p.add_argument("--kind", help="只掃這些類別，用逗號分開：股票,創新板,ETF")
        p.add_argument("--limit", type=int, help="只掃前 N 檔")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume, help="股數（1 張 = 1,000 股）")
        p.add_argument("--offline", metavar="PATH", help="用離線檔案或資料夾，不連網")
        p.add_argument("--disposition", metavar="PATH",
                       help="存下來的處置股清單（資料夾或檔案）；none 表示不檢查")
        p.add_argument("--keep-disposition", action="store_true", help="處置股照樣列出（標示處置）")
        p.add_argument("--now", help='假裝現在是這個時間（"2026-11-02 09:45" 台北時間，或 ...Z）')
        p.add_argument("--config", default=DEFAULT_CONFIG)
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="每次請求之間停幾秒（%.1f）" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="印出幾檔（全部都會寫進檔案）")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="開盤前用日收盤價找型態")
    common(p, 10.0, 1000000)
    p = sub.add_parser("orb", help="今天到目前為止的開盤區間突破")
    common(p, 10.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="第二階段最多抓幾張 v8 圖，成交金額大的先（%d；0 = 全部）" % MAX_CONFIRM)
    p.add_argument("--at", help="把時間切在這個台北時間，HH:MM")
    p.add_argument("--until", help="HH:MM 之後不再新進場（預設：config 的 no_entries_after，13:00）")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py：%s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py：已停止", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
