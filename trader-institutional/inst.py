#!/usr/bin/env python3
"""三大法人買賣超 for trader-institutional: 外資、投信、自營商. Standard library only, Python 3.8+.

    python3 inst.py market                               # the day's totals, 上市 and 上櫃, in 億元
    python3 inst.py top --group trust -n 10              # the day's biggest 投信 net buys and sells
    python3 inst.py stock 2303 --days 10                 # one stock day by day, the sums, the streaks
    python3 inst.py streaks --group trust --min-days 3   # 投信連買 3 天以上
    python3 inst.py watch --write                        # plan, holdings, watchlist -> trader/institutional.json
    python3 inst.py stock 2303 --now "2026-11-02 09:00" --offline tests/fixtures/trader-institutional

Which day. The exchanges publish a day's numbers after the close and do not say when; on
2026-11-02 a Yahoo ranking built from them appeared at 16:21. So a replay (--now) uses day D only
from 17:00 on D, and the previous trading day before that. Live, from 13:30 it asks for today
first and falls back to the previous trading day while the answer is still empty.

Sources (read 2026-11-02; per-stock numbers are shares, totals are NT$):
  TWSE per stock  https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json
  TPEx per stock  https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=YYYY/MM/DD&response=json
                  The column titles repeat; the 7 groups of (buy, sell, net) are, in order:
                  外資及陸資(不含外資自營商), 外資自營商, 外資及陸資合計, 投信, 自營商(自行買賣),
                  自營商(避險), 自營商合計, then the 三大法人 total.
  TWSE totals     https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json
  TPEx totals     https://www.tpex.org.tw/www/zh-tw/insti/summary?type=Daily&date=YYYY/MM/DD&response=json
外資 here is 外資及陸資 without 外資自營商; 自營商 is 自行買賣 plus 避險. 外資自營商 is already inside
自營商, so the exchanges do not add it to the total again. The numbers include odd lots, after-hours
fixed-price and block trades (the exchanges' notes). A closed day answers "很抱歉，沒有符合條件的資料!".

A published day never changes, so it is cached in trader/state/institutional/. --offline DIR reads
T86-YYYY-MM-DD.json, tpex-dailyTrade-YYYY-MM-DD.json, BFI82U-YYYY-MM-DD.json and
tpex-summary-YYYY-MM-DD.json (each may be .gz) instead; the tests use it.

These are public statistics published after the close: never inside information, never a signal
by themselves. A streak describes the past; it forecasts nothing.
"""

import argparse
import datetime
import gzip
import json
import os
import sys
import time
import unicodedata
import urllib.error
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date=%s&selectType=ALLBUT0999&response=json"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=%s&response=json"
BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=%s&response=json"
TPEX_SUMMARY_URL = "https://www.tpex.org.tw/www/zh-tw/insti/summary?type=Daily&date=%s&response=json"
PUBLISHED = datetime.time(17, 0)   # a replay sees day D's numbers from this time on D
PAUSE = 2.0                        # seconds between requests
GROUPS = ("foreign", "trust", "dealer")
NAMES = {"foreign": "外資", "trust": "投信", "dealer": "自營商", "total": "三大法人",
         "dealer_self": "自營商（自行買賣）", "dealer_hedge": "自營商（避險）", "foreign_dealer": "外資自營商"}
MARKET_NAMES = {"TWSE": "上市", "TPEx": "上櫃"}
ROW_KEYS = ("foreign", "foreign_dealer", "trust", "dealer", "dealer_self", "dealer_hedge", "total")
TOP_RANK = 20       # a watched stock in the market's top 20 of a group's net buys or sells is flagged
STREAK_FLAG = 3     # a streak this long is flagged (投信連買 3 天)
FRAMING = "這是盤後公布的公開統計，不是內線消息，也不是買賣訊號；連買連賣只描述過去，不預測之後。"

T86_KEYS = {"foreign": "外陸資買賣超股數(不含外資自營商)", "foreign_dealer": "外資自營商買賣超股數",
            "trust": "投信買賣超股數", "dealer": "自營商買賣超股數",
            "dealer_self": "自營商買賣超股數(自行買賣)", "dealer_hedge": "自營商買賣超股數(避險)",
            "total": "三大法人買賣超股數"}
TPEX_GROUPS = ("foreign", "foreign_dealer", "foreign_all", "trust", "dealer_self", "dealer_hedge", "dealer")
BFI82U_ROWS = {"自營商(自行買賣)": "dealer_self", "自營商(避險)": "dealer_hedge", "投信": "trust",
               "外資及陸資(不含外資自營商)": "foreign", "外資自營商": "foreign_dealer", "合計": "total"}
TPEX_SUMMARY_ROWS = {"外資及陸資(不含自營商)": "foreign", "外資自營商": "foreign_dealer", "投信": "trust",
                     "自營商(自行買賣)": "dealer_self", "自營商(避險)": "dealer_hedge", "自營商合計": "dealer",
                     "三大法人合計*": "total", "三大法人合計": "total"}

http_get = tw.http_get    # tests swap these two
_sleep = time.sleep


class InstError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def _int(text):
    s = str(text if text is not None else "").replace(",", "").strip()
    if s in ("", "-", "--"):
        return 0
    try:
        return int(Decimal(s))
    except ArithmeticError:
        raise InstError("看不懂的數字：%r" % text)


def _load(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            raise InstError("回來的不是 JSON（網站可能改版或擋下了這次查詢）")
    return payload


def _ok(doc):
    return isinstance(doc, dict) and str(doc.get("stat", "")).lower() == "ok"


def parse_t86(payload):
    """TWSE T86 -> {"date": date or None, "rows": {code: row}}. A closed or unpublished day has no rows."""
    doc = _load(payload)
    if not _ok(doc):
        return {"date": None, "rows": {}}
    fields = [str(f).strip() for f in doc.get("fields") or []]
    try:
        idx = dict((k, fields.index(v)) for k, v in T86_KEYS.items())
        i_code, i_name = fields.index("證券代號"), fields.index("證券名稱")
    except ValueError as e:
        raise InstError("證交所三大法人日報的欄位變了：%s" % e)
    rows = {}
    for r in doc.get("data") or []:
        code = str(r[i_code]).strip()
        if not code:
            continue
        row = {"code": code, "name": str(r[i_name]).strip(), "market": "TWSE"}
        for k, i in idx.items():
            row[k] = _int(r[i])
        rows[code] = row
    day = None
    try:
        day = datetime.datetime.strptime(str(doc.get("date", "")), "%Y%m%d").date()
    except ValueError:
        pass
    return {"date": day, "rows": rows}


def parse_tpex(payload):
    """TPEx dailyTrade -> {"date", "rows"} in the same shape as parse_t86."""
    doc = _load(payload)
    if not _ok(doc) or not doc.get("tables"):
        return {"date": None, "rows": {}}
    table = doc["tables"][0]
    width = 2 + 3 * len(TPEX_GROUPS) + 1
    rows = {}
    for r in table.get("data") or []:
        if len(r) != width:
            raise InstError("櫃買中心三大法人明細的欄位數變了（%d 欄，應該是 %d 欄）" % (len(r), width))
        code = str(r[0]).strip()
        if not code:
            continue
        row = {"code": code, "name": str(r[1]).strip(), "market": "TPEx"}
        for k, group in enumerate(TPEX_GROUPS):
            if group != "foreign_all":
                row[group] = _int(r[2 + 3 * k + 2])     # (buy, sell, net): the net
        row["total"] = _int(r[-1])
        rows[code] = row
    day = None
    try:
        day = tw.roc_date(str(table.get("date", "")))
    except ValueError:
        pass
    return {"date": day, "rows": rows}


def _totals(doc, names, value_col):
    out = {}
    for r in (doc.get("data") or []):
        if len(r) <= value_col:
            continue
        key = names.get(str(r[0]).strip())
        if key and key not in out:
            out[key] = _int(r[value_col])
    if "dealer" not in out:
        out["dealer"] = out.get("dealer_self", 0) + out.get("dealer_hedge", 0)
    return out


def parse_bfi82u(payload):
    """TWSE BFI82U -> {group: net NT$} or None for a day without numbers."""
    doc = _load(payload)
    if not _ok(doc):
        return None
    return _totals(doc, BFI82U_ROWS, 3)


def parse_tpex_summary(payload):
    """TPEx summary -> {group: net NT$} or None."""
    doc = _load(payload)
    if not _ok(doc) or not doc.get("tables"):
        return None
    return _totals(doc["tables"][0], TPEX_SUMMARY_ROWS, 3)


# ---------------------------------------------------------------- where the days come from

class Source(object):
    """Days of numbers from --offline DIR, the cache, or the exchanges (2 s apart)."""

    def __init__(self, offline=None, cache_dir=None):
        self.offline = offline
        self.cache_dir = cache_dir
        self.requests = 0
        self.notes = []
        self._last = 0.0

    def _file(self, name):
        for p in (os.path.join(self.offline, name), os.path.join(self.offline, name + ".gz")):
            if os.path.isfile(p):
                if p.endswith(".gz"):
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        return fh.read()
                with open(p, encoding="utf-8") as fh:
                    return fh.read()
        return None

    def _get(self, url):
        gap = time.time() - self._last
        if self._last and gap < PAUSE:
            _sleep(PAUSE - gap)
        try:
            return http_get(url, timeout=40)
        except urllib.error.HTTPError as e:
            raise InstError("HTTP %s：%s" % (e.code, url))
        except (urllib.error.URLError, OSError) as e:
            raise InstError("連不上交易所網站（%s）" % getattr(e, "reason", e))
        finally:
            self._last = time.time()
            self.requests += 1

    def _cache_path(self, name):
        return os.path.join(self.cache_dir, name) if self.cache_dir else None

    def _cache_read(self, name):
        path = self._cache_path(name)
        if not path or not os.path.isfile(path):
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _cache_write(self, name, doc):
        path = self._cache_path(name)
        if not path:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    def flows(self, day):
        """{code: row} for both markets on day, or None when that day has no numbers (closed, not
        published yet, or missing from the offline folder)."""
        key = day.isoformat()
        if not self.offline:
            cached = self._cache_read("%s.json.gz" % key)
            if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
                return dict((r["code"], r) for r in cached["rows"])
        if self.offline:
            t86 = self._file("T86-%s.json" % key)
            tpex = self._file("tpex-dailyTrade-%s.json" % key)
            if t86 is None and tpex is None:
                return None
        else:
            t86 = self._get(T86_URL % day.strftime("%Y%m%d"))
            tpex = self._get(TPEX_URL % day.strftime("%Y%%2F%m%%2F%d"))
        twse = parse_t86(t86)["rows"] if t86 is not None else {}
        otc = parse_tpex(tpex)["rows"] if tpex is not None else {}
        if not twse and not otc:
            return None
        if not twse or not otc:
            self.notes.append("%s只有%s的資料。" % (tw.day_label(day), "上櫃" if otc else "上市"))
        rows = dict(twse)
        rows.update(otc)
        if twse and otc and not self.offline:
            self._cache_write("%s.json.gz" % key, {"date": key, "rows": list(rows.values())})
        return rows

    def totals(self, day):
        """{"TWSE": {...} or None, "TPEx": {...} or None} in NT$."""
        key = day.isoformat()
        if not self.offline:
            cached = self._cache_read("%s-totals.json.gz" % key)
            if isinstance(cached, dict) and (cached.get("TWSE") or cached.get("TPEx")):
                return cached
        if self.offline:
            twse, tpex = self._file("BFI82U-%s.json" % key), self._file("tpex-summary-%s.json" % key)
        else:
            twse = self._get(BFI82U_URL % day.strftime("%Y%m%d"))
            tpex = self._get(TPEX_SUMMARY_URL % day.strftime("%Y%%2F%m%%2F%d"))
        out = {"TWSE": parse_bfi82u(twse) if twse is not None else None,
               "TPEx": parse_tpex_summary(tpex) if tpex is not None else None}
        if out["TWSE"] and out["TPEx"] and not self.offline:
            self._cache_write("%s-totals.json.gz" % key, out)
        return out


# ---------------------------------------------------------------- which days

def first_day(now_tpe, cal, replay):
    """(the newest trading day whose numbers can be out at now, a note when it is not today)."""
    today = now_tpe.date()
    why = cal.closed(today)
    if why:
        prev = cal.previous_trading_day(today)
        return prev, "今天 %s 休市（%s），用 %s的資料。" % (tw.day_label(today), why, tw.day_label(prev))
    t = now_tpe.time()
    if t >= PUBLISHED or (not replay and t >= tw.CLOSE):
        return today, ""
    prev = cal.previous_trading_day(today)
    if replay:
        return prev, ("%s的三大法人買賣超要收盤後才公布（重播以 17:00 為準），現在 %s，用 %s的資料。"
                      % (tw.day_label(today), tw.hm(now_tpe), tw.day_label(prev)))
    return prev, "今天的三大法人買賣超要收盤後才公布，先用 %s的資料。" % tw.day_label(prev)


def load_days(src, cal, start, n, today=None):
    """[(day, rows)] newest first: up to n trading days with numbers, from start back.
    A missing today (live, not published yet) falls back quietly with a note."""
    out, d, misses = [], start, 0
    while len(out) < n:
        rows = src.flows(d)
        if rows is None:
            if src.offline:
                if out:
                    src.notes.append("離線資料只到 %s，所以只有 %d 天。" % (tw.day_label(out[-1][0]), len(out)))
                    break
                src.notes.append("離線資料夾裡沒有 %s的三大法人資料。" % tw.day_label(d))
            elif d == today and not out:
                src.notes.append("%s的資料還沒公布，用前一個交易日。" % tw.day_label(d))
            else:
                src.notes.append("%s沒有三大法人資料（臨時休市或還沒公布）。" % tw.day_label(d))
            misses += 1
            if misses >= 3:
                break
        else:
            out.append((d, rows))
            misses = 0
        d = cal.previous_trading_day(d)
    return out


# ---------------------------------------------------------------- arithmetic

def streak(days, code, group):
    """Consecutive days, newest first, the group was a net buyer (+n) or a net seller (-n). 0 if it
    did nothing on the newest day. A day the stock is missing from the list counts as no trading."""
    n, sign = 0, 0
    for _, rows in days:
        v = (rows.get(code) or {}).get(group, 0)
        s = (v > 0) - (v < 0)
        if s == 0 or (sign and s != sign):
            break
        sign, n = s, n + 1
    return n * sign


def window_sum(days, code, group, count=None):
    return sum((rows.get(code) or {}).get(group, 0) for _, rows in days[:count])


def ranking(rows, group, side, markets=None, no_etf=False):
    """[(rank, row)] of the day's net buyers (side "buy") or sellers ("sell") of a group."""
    pick = [r for r in rows.values()
            if (not markets or r["market"] in markets) and not (no_etf and tw.is_etf_like(r["code"]))]
    if side == "buy":
        pick = [r for r in pick if r.get(group, 0) > 0]
        pick.sort(key=lambda r: (-r[group], r["code"]))
    else:
        pick = [r for r in pick if r.get(group, 0) < 0]
        pick.sort(key=lambda r: (r[group], r["code"]))
    return list(enumerate(pick, 1))


def lots(shares):
    """Shares -> 張, half away from zero (5,060,280 -> 5,060)."""
    return int((Decimal(int(shares)) / 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_lots(shares):
    return "{:+,}".format(lots(shares)) if lots(shares) else "0"


def yi(ntd):
    """NT$ -> '+338.16 億'."""
    v = (Decimal(int(ntd)) / Decimal(100000000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "{:+,.2f} 億".format(v) if v else "0.00 億"


def streak_text(s, group, seen):
    name = NAMES[group]
    if s == 0:
        return "%s最新一天沒有買賣超" % name
    text = "%s連%s %d 天" % (name, "買" if s > 0 else "賣", abs(s))
    if abs(s) >= seen:
        text += "以上（只看了 %d 天）" % seen
    return text


def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, cols, right=False):
    gap = " " * max(0, cols - width(text))
    return gap + text if right else text + gap


def who(row):
    """聯電 2303（上市）, from a parsed row (code) or a result (symbol)."""
    return "%s %s（%s）" % (row["name"], row.get("code") or row.get("symbol"),
                          MARKET_NAMES.get(row["market"], row["market"]))


# ---------------------------------------------------------------- the watch list (same sources as trader-news)

def _read(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            try:
                return json.load(fh)
            except ValueError:
                raise InstError("%s 不是有效的 JSON" % path)
    return None


def watch_codes(plan, portfolio, watchlist, symbols=None):
    """Codes in order: --symbols, else the plan's symbol, the holdings, the scanner's hits."""
    raw = list(symbols or [])
    if not raw:
        pl = _read(plan) or {}
        raw.append(pl.get("symbol"))
        for p in (_read(portfolio) or {}).get("positions", []):
            raw.append(p.get("symbol") or p.get("ticker"))
        for h in (_read(watchlist) or {}).get("hits", []):
            raw.append(h.get("symbol") or h.get("ticker"))
    out, bad = [], []
    for s in raw:
        if not s:
            continue
        try:
            code = tw.parse_symbol(str(s))[0]
        except ValueError:
            bad.append(str(s))
            continue
        if code not in out:
            out.append(code)
    return out, bad


# ---------------------------------------------------------------- commands

def stock_view(days, code):
    latest = next((rows[code] for _, rows in days if code in rows), None)
    if latest is None:
        return None
    table = []
    for d, rows in days:
        r = rows.get(code) or {}
        table.append(dict([("date", d.isoformat())] + [(k, r.get(k, 0)) for k in ROW_KEYS]))
    return {"symbol": code, "name": latest["name"], "market": latest["market"], "rows": table,
            "sums": dict((g, window_sum(days, code, g)) for g in GROUPS + ("total",)),
            "streaks": dict((g, streak(days, code, g)) for g in GROUPS)}


def render_stock(v, seen):
    lines = ["%s，三大法人買賣超（張），最近 %d 個交易日" % (who(v), seen)]
    head = pad("  日期", 20) + "".join(pad(NAMES[g], 10, True) for g in GROUPS + ("total",))
    lines.append(head.replace("三大法人", "    合計"))
    for r in v["rows"]:
        d = datetime.date.fromisoformat(r["date"])
        lines.append(pad("  " + tw.day_label(d), 20) + "".join(pad(fmt_lots(r[g]), 10, True)
                                                              for g in GROUPS + ("total",)))
    lines.append(pad("  %d 日合計" % seen, 20) + "".join(pad(fmt_lots(v["sums"][g]), 10, True)
                                                        for g in GROUPS + ("total",)))
    newest = v["rows"][0]
    lines.append("  最新一天的自營商：自行買賣 %s 張、避險 %s 張。" % (fmt_lots(newest["dealer_self"]),
                                                                  fmt_lots(newest["dealer_hedge"])))
    lines.append("目前連續：%s。" % "、".join(streak_text(v["streaks"][g], g, seen) for g in GROUPS))
    return lines


def cmd_stock(a, src, days, notes):
    views, missing = [], []
    for s in a.codes:
        try:
            code = tw.parse_symbol(s)[0]
        except ValueError as e:
            raise InstError(str(e))
        v = stock_view(days, code)
        (views if v else missing).append(v if v else code)
    if a.json:
        return {"date": days[0][0].isoformat(), "days": [d.isoformat() for d, _ in days], "stocks": views,
                "missing": missing, "notes": notes}, bool(views)
    lines = []
    for v in views:
        lines.extend(render_stock(v, len(days)))
        lines.append("")
    for code in missing:
        lines.append("%s 這 %d 個交易日都沒有出現在三大法人買賣超日報裡（沒有法人買賣，或代號不對）。"
                     % (code, len(days)))
    return lines, bool(views)


def cmd_streaks(a, src, days, notes):
    group, side = a.group, a.side
    sign = 1 if side == "buy" else -1
    markets = None if a.market == "all" else (a.market,)
    hits = []
    for code, r in days[0][1].items():
        if markets and r["market"] not in markets:
            continue
        if a.no_etf and tw.is_etf_like(code):
            continue
        s = streak(days, code, group)
        if s * sign >= a.min_days:
            n = abs(s)
            hits.append({"symbol": code, "name": r["name"], "market": r["market"], "days": n,
                         "period": window_sum(days, code, group, n), "latest": r.get(group, 0)})
    hits.sort(key=lambda h: (-h["days"], -abs(h["period"]), h["symbol"]))
    top = hits[:a.n]
    title = "%s連%s %d 天以上，最近 %d 個交易日到 %s為止，%s，共 %d 檔" % (
        NAMES[group], "買" if side == "buy" else "賣", a.min_days, len(days), tw.day_label(days[0][0]),
        {"all": "上市＋上櫃", "TWSE": "上市", "TPEx": "上櫃"}[a.market], len(hits))
    if a.json:
        return {"date": days[0][0].isoformat(), "days": [d.isoformat() for d, _ in days], "group": group,
                "side": side, "min_days": a.min_days, "count": len(hits), "stocks": top, "notes": notes}, True
    lines = [title + ("，列出前 %d 檔" % len(top) if len(top) < len(hits) else "")]
    for i, h in enumerate(top, 1):
        more = "以上" if h["days"] >= len(days) else ""
        lines.append("  %3d. %s  連%s %d 天%s  這段期間 %s 張  最新一天 %s 張"
                     % (i, pad(who(h), 28), "買" if side == "buy" else "賣", h["days"], more,
                        fmt_lots(h["period"]), fmt_lots(h["latest"])))
    if not hits:
        lines.append("  沒有符合的股票。")
    return lines, True


def cmd_top(a, src, days, notes):
    d, rows = days[0]
    groups = GROUPS if a.group == "all" else (a.group,)
    sides = ("buy", "sell") if a.side == "both" else (a.side,)
    markets = None if a.market == "all" else (a.market,)
    out = {"date": d.isoformat(), "lists": [], "notes": notes}
    lines = ["三大法人買賣超排行（張），%s，%s%s" % (tw.day_label(d), {"all": "上市＋上櫃", "TWSE": "上市",
                                                                 "TPEx": "上櫃"}[a.market],
                                              "，不含 ETF" if a.no_etf else "")]
    for g in groups:
        for side in sides:
            ranked = ranking(rows, g, side, markets, a.no_etf)[:a.n]
            out["lists"].append({"group": g, "side": side, "stocks": [
                {"rank": i, "symbol": r["code"], "name": r["name"], "market": r["market"], "net": r[g]}
                for i, r in ranked]})
            lines.append("%s%s前 %d 名" % (NAMES[g], "買超" if side == "buy" else "賣超", len(ranked)))
            for i, r in ranked:
                lines.append("  %3d. %s %s 張" % (i, pad(who(r), 28), pad(fmt_lots(r[g]), 9, True)))
    return (out, True) if a.json else (lines, True)


def cmd_market(a, src, days, notes):
    d = days[0][0]
    tot = src.totals(d)
    if not tot["TWSE"] and not tot["TPEx"]:
        raise InstError("%s沒有三大法人買賣金額統計（離線資料沒有，或還沒公布）。" % tw.day_label(d))
    if a.json:
        return {"date": d.isoformat(), "unit": "NT$", "TWSE": tot["TWSE"], "TPEx": tot["TPEx"], "notes": notes}, True
    lines = ["三大法人買賣超金額，%s" % tw.day_label(d),
             pad("", 16) + pad("上市", 16, True) + pad("上櫃", 16, True)]
    for key, label in (("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商"),
                       ("dealer_self", "  自行買賣"), ("dealer_hedge", "  避險"), ("total", "合計")):
        cells = [yi(t[key]) if t and key in t else "沒有資料" for t in (tot["TWSE"], tot["TPEx"])]
        lines.append(pad("  " + label, 16) + "".join(pad(c, 16, True) for c in cells))
    lines.append("外資不含外資自營商；外資自營商已經算在自營商裡，交易所不把它重複加進合計。")
    return lines, True


def watch_view(days, code, ranks):
    v = stock_view(days, code)
    if v is None:
        return None
    newest = days[0][1].get(code) or {}
    flags, rank_out = [], {}
    for g in GROUPS:
        s = v["streaks"][g]
        if abs(s) >= STREAK_FLAG:
            flags.append(streak_text(s, g, len(days)))
        for side in ("buy", "sell"):
            r = ranks[(g, side)].get(code)
            if r and r <= TOP_RANK:
                rank_out[g] = {"side": side, "rank": r}
                flags.append("%s大%s（%s超第 %d 名）" % (NAMES[g], "買" if side == "buy" else "賣",
                                                       "買" if side == "buy" else "賣", r))
    v.update({"latest": dict((k, newest.get(k, 0)) for k in ROW_KEYS), "ranks": rank_out, "flags": flags})
    return v


def cmd_watch(a, src, days, notes):
    codes, bad = watch_codes(a.plan, a.portfolio, a.watchlist, a.symbols)
    if not codes:
        raise InstError("沒有要看的代號：%s、%s、%s 裡都沒有，或用 --symbols 2303 6488"
                        % (a.plan, a.portfolio, a.watchlist))
    rows = days[0][1]
    ranks = {}
    for g in GROUPS:
        for side in ("buy", "sell"):
            ranks[(g, side)] = dict((r["code"], i) for i, r in ranking(rows, g, side))
    stocks, missing = [], []
    for code in codes:
        v = watch_view(days, code, ranks)
        (stocks if v else missing).append(v if v else code)
    doc = {"created_by": "trader-institutional", "created_at": tw.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "date": days[0][0].isoformat(), "days": [d.isoformat() for d, _ in days], "unit": "shares",
           "note": FRAMING, "stocks": stocks, "missing": missing, "skipped": bad, "notes": notes}
    if a.write:
        tw.write_json(a.write, doc)
    if a.json:
        return doc, bool(stocks)
    lines = ["你關注的股票，三大法人買賣超（張），%s，看最近 %d 個交易日" % (tw.day_label(days[0][0]), len(days))]
    for v in stocks:
        lt = v["latest"]
        lines.append("  %s  外資 %s  投信 %s  自營商 %s  合計 %s"
                     % (pad(who(v), 26), fmt_lots(lt["foreign"]), fmt_lots(lt["trust"]),
                        fmt_lots(lt["dealer"]), fmt_lots(lt["total"])))
        lines.append("      連續：%s。" % "、".join(streak_text(v["streaks"][g], g, len(days)) for g in GROUPS))
        if v["flags"]:
            lines.append("      注意：%s。" % "、".join(v["flags"]))
    for code in missing:
        lines.append("  %s：這 %d 個交易日沒有出現在三大法人買賣超日報裡。" % (code, len(days)))
    if bad:
        lines.append("略過看不懂的代號：%s。" % "、".join(bad))
    if a.write:
        lines.append("已寫入 %s。" % a.write)
    return lines, bool(stocks)


# ---------------------------------------------------------------- main

def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="三大法人（外資、投信、自營商）買賣超追蹤（trader-institutional）。")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, days=10):
        p.add_argument("--date", help="看這一天（YYYY-MM-DD，預設：已經公布的最新一天）")
        p.add_argument("--days", type=int, default=days, help="往回看幾個交易日（預設 %d）" % days)
        p.add_argument("--now", help='假裝現在是這個台北時間，例如 "2026-11-02 09:00"')
        p.add_argument("--offline", help="存下來的交易所 JSON 資料夾（測試用）")
        p.add_argument("--config", default="trader/config.json", help="設定檔（休市日的 extra_closed）")
        p.add_argument("--cache", help="快取資料夾（預設 trader/state/institutional）")
        p.add_argument("--no-cache", action="store_true", help="不讀也不寫快取")
        p.add_argument("--json", action="store_true")

    def filters(p, n):
        p.add_argument("--market", choices=["all", "TWSE", "TPEx"], default="all", help="上市、上櫃或全部")
        p.add_argument("--no-etf", action="store_true", help="不列 ETF（代號 0 開頭）")
        p.add_argument("-n", type=int, default=n, help="列幾檔（預設 %d）" % n)

    p = sub.add_parser("market", help="當天三大法人買賣超金額，上市和上櫃，億元")
    common(p, days=1)
    p = sub.add_parser("top", help="當天各法人買超、賣超最多的股票")
    common(p, days=1)
    p.add_argument("--group", choices=["all", "foreign", "trust", "dealer", "total"], default="all")
    p.add_argument("--side", choices=["both", "buy", "sell"], default="both")
    filters(p, 10)
    p = sub.add_parser("stock", help="一檔或幾檔股票逐日的買賣超、合計和連續天數")
    p.add_argument("codes", nargs="+", metavar="CODE", help="代號，例如 2303、6488.TWO")
    common(p)
    p = sub.add_parser("streaks", help="連續買超或賣超的股票，例如投信連買 3 天以上")
    p.add_argument("--group", choices=["foreign", "trust", "dealer", "total"], default="trust")
    p.add_argument("--side", choices=["buy", "sell"], default="buy")
    p.add_argument("--min-days", type=int, default=3)
    common(p)
    filters(p, 20)
    p = sub.add_parser("watch", help="計畫、持股和觀察名單的法人動向")
    p.add_argument("--symbols", nargs="+", help="直接給代號，不讀檔案")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--write", nargs="?", const="trader/institutional.json", metavar="PATH",
                   help="把結果寫進 JSON（預設 trader/institutional.json）")
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        if a.days < 1 or a.days > 60:
            raise InstError("--days 要在 1 到 60 之間")
        now = tw.parse_now(a.now)
        now_tpe = tw.to_taipei(now)
        trader_dir = os.path.dirname(a.config) or "."
        cfg = tw.read_json(a.config, {}) or {}
        cal = tw.load_calendar(trader_dir, cfg)
        cache = None if (a.no_cache or a.offline) else (a.cache or os.path.join(trader_dir, "state", "institutional"))
        src = Source(a.offline, cache)
        notes = []
        start, note = first_day(now_tpe, cal, replay=bool(a.now))
        if note:
            notes.append(note)
        if a.date:
            try:
                want = datetime.date.fromisoformat(a.date)
            except ValueError:
                raise InstError("--date 要像 2026-11-02")
            if want > start:
                notes.append("%s的資料在 %s 還看不到，用 %s。" % (tw.day_label(want), tw.hm(now_tpe),
                                                                tw.day_label(start)))
            else:
                start = want if cal.is_trading_day(want) else cal.previous_trading_day(want)
                if start != want:
                    notes.append("%s 休市（%s），用 %s。" % (tw.day_label(want), cal.closed(want),
                                                          tw.day_label(start)))
        unknown = cal.unknown_note(start)
        if unknown:
            notes.append(unknown + "。")
        days = load_days(src, cal, start, a.days, today=now_tpe.date() if not a.now else None)
        notes.extend(src.notes)
        if not days:
            raise InstError("找不到三大法人資料。%s" % "".join(notes))
        handler = {"market": cmd_market, "top": cmd_top, "stock": cmd_stock, "streaks": cmd_streaks,
                   "watch": cmd_watch}[a.cmd]
        out, found = handler(a, src, days, notes)
    except (InstError, ValueError) as e:
        print("inst.py：%s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if notes:
            for n in notes:
                print(n)
            print("")
        while out and not out[-1]:
            out.pop()
        print("\n".join(out))
        print("")
        print("統計含零股、盤後定價和鉅額交易%s。" % ("" if a.cmd == "market" else "；張數是股數除以 1,000 後四捨五入"))
        print(FRAMING)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
