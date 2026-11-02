#!/usr/bin/env python3
"""Price alerts and the Taiwan market clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the Taiwan market open? next open / close
    python3 alerts.py calendar                       # this year's closed weekdays, typhoon days included
    python3 alerts.py calendar --update 2027         # fetch a year's TWSE schedule into trader/state
    python3 alerts.py quote 2303 6488 --write        # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # the plan, portfolio and watchlist codes
    python3 alerts.py add --from-plan                # stop, target, flat by 13:20 and limit down, from trader/plan.json
    python3 alerts.py add --symbol 2303 --stop 108.5
    python3 alerts.py add --symbol 2303 --target 111.5
    python3 alerts.py add --symbol 2303 --pct 3
    python3 alerts.py add --symbol 2303 --limit-down
    python3 alerts.py add --at 13:20 --label "13:20 前出清" --symbol 2303
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-02 09:42" --offline tests/fixtures/trader-entry/2303-2026-11-02-1m.json
    python3 alerts.py list
    python3 alerts.py reset 2303-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  limit_up        the price is at the day's limit up (漲停): a buy may not fill
  limit_down      the price is at the day's limit down (跌停): a sell, a stop
                  included, may not fill
  opening_bell    the market opened today, 09:00 (rings once per trading day)
  time            the Taipei clock passed "at" (HH:MM) on a trading day (rings
                  once per trading day); "flat by 13:20" is one

A rule with "date" is only watched on that Taipei date (rules from a plan are
for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). pct_move and the limit alerts ring at most once a day.

Every rule compares the LAST price (the last trade, or in a replay the close
of the last bar that ended by --now), never a bar's low or high: a price that
passes a level and comes back between two checks is not seen. Check often.

Quotes (free; for alerts and sizing checks, never for timing an order):
  mis     the TWSE MIS page, https://mis.twse.com.tw/stock/api/getStockInfo.jsp
          (the exchange's own quote page, not an API contract; one request for
          every code, 上市 and 上櫃 together; the default)
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/2303.TW (unofficial;
          query1 after a 429 or a 5xx; one request per code, half a second
          apart). A code MIS has no last trade for falls back to Yahoo.
  --offline PATH (a file or a folder, repeatable) reads saved MIS JSON and
  Yahoo chart JSON (.json or .json.gz) instead. A replay never sees the future:
  a MIS quote stamped after --now is not used, and a chart gives the close of
  the last bar that ended by --now.

Calendar: twmarket.py's TWSE schedule with the typhoon days, the years cached in
trader/state/calendar-YYYY.json (calendar --update) and "extra_closed" in
trader/config.json. Session: orders from 08:30, open 09:00, intraday odd lots
from 09:10, closing call auction 13:25 to 13:30.
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

PAUSE = 0.5    # seconds between Yahoo requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
SOURCES = ("mis", "yahoo")
MARKET_LABEL = {"TWSE": "上市", "TPEx": "上櫃"}
STEP_SECONDS = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600}


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return tw.parse_now(text)
    except ValueError as e:
        raise AlertError(str(e))


def utc_z(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(text):
    """"2026-11-02T01:42:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trader_dir(config_path):
    """trader/config.json -> trader: where state/calendar-YYYY.json and universe.txt live."""
    return os.path.dirname(config_path or "") or "."


def load_context(config_path):
    cfg = _read(config_path, {}) if config_path else {}
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg, tw.load_calendar(trader_dir(config_path), cfg)


def unknown_year_note(cal, d):
    if cal.known(d.year):
        return ""
    return ("%d 年的休市日還不在清單裡：證交所公布後執行 alerts.py calendar --update %d，"
            "或在 trader/config.json 的 extra_closed 自己加上" % (d.year, d.year))


def market_status(now_utc, cal=None):
    """Where the Taiwan session is at now_utc (Taipei time, the given calendar)."""
    cal = cal or tw.Calendar()
    local = tw.to_taipei(now_utc)
    d, t = local.date(), local.time()
    why = cal.closed(d)
    st = {"now_taipei": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": tw.WEEKDAYS[d.weekday()], "calendar_known": cal.known(d.year),
          "trading_day": why is None, "open": False, "closed_reason": why}
    if why is None:
        st.update({"pre_open": tw.hm(tw.PRE_OPEN), "opens": tw.hm(tw.OPEN),
                   "odd_lot_first_match": tw.hm(tw.ODD_LOT_FIRST_MATCH),
                   "closing_auction": tw.hm(tw.CLOSING_AUCTION), "closes": tw.hm(tw.CLOSE)})
        if t < tw.PRE_OPEN:
            st["phase"], st["reason"] = "before", "還沒開始接受委託（08:30 起）"
        elif t < tw.OPEN:
            st["phase"], st["reason"] = "pre_open", "盤前：08:30 起接受委託，09:00 開盤集合競價"
        elif t < tw.CLOSING_AUCTION:
            st["phase"], st["reason"], st["open"] = "continuous", "盤中逐筆交易", True
        elif t < tw.CLOSE:
            st["phase"], st["reason"], st["open"] = ("closing_auction",
                                                     "收盤集合競價（13:25 到 13:30，只收限價 ROD）", True)
        else:
            st["phase"], st["reason"] = "after", "已收盤"
    else:
        st["phase"], st["reason"] = "closed", why
    nd = d if (why is None and t < tw.OPEN) else cal.next_trading_day(d)
    st["next_open"] = "%s 09:00" % tw.day_label(nd)
    note = unknown_year_note(cal, d) or (unknown_year_note(cal, nd) if nd.year != d.year else "")
    if note:
        st["note"] = note
    return st


def status_line(st):
    return "台股%s（台北 %s（%s），%s）。" % ("開盤中" if st["open"] else "沒有開盤", st["now_taipei"],
                                            st["weekday"], st["reason"])


# ---------------------------------------------------------------- files

def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s 不是有效的 JSON：%s" % (path, e))


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- symbols

def code_of(text):
    """2303, 2303.TW, 6488.TWO -> (code, market or None)."""
    try:
        return tw.parse_symbol(text)
    except ValueError:
        raise AlertError("%s 不是台股代號（例如 2303、2303.TW、6488.TWO、0050）" % text)


def known_symbols(config_path, plan_path=None, portfolio_path=None, watchlist_path=None):
    """{code: {"market", "name"}} from the plan, the portfolio, the watchlist and trader/universe.txt."""
    out = {}
    uni = tw.load_universe(os.path.join(trader_dir(config_path), "universe.txt"))
    for code, row in uni.items():
        out[code] = {"market": row.get("market"), "name": row.get("name")}

    def put(sym, market=None, name=None):
        try:
            code, suffix = tw.parse_symbol(str(sym or ""))
        except ValueError:
            return
        row = out.setdefault(code, {"market": None, "name": None})
        row["market"] = market or suffix or row.get("market")
        row["name"] = name or row.get("name")
    plan = _read(plan_path, {}) if plan_path else {}
    if isinstance(plan, dict) and plan.get("symbol"):
        put(plan["symbol"], plan.get("market"), plan.get("name"))
    pf = _read(portfolio_path, {}) if portfolio_path else {}
    for p in (pf.get("positions") or []) if isinstance(pf, dict) else []:
        put(p.get("symbol") or p.get("ticker"), p.get("market"), p.get("name"))
    wl = _read(watchlist_path, {}) if watchlist_path else {}
    for h in (wl.get("hits") or []) if isinstance(wl, dict) else []:
        put(h.get("symbol") or h.get("ticker"), h.get("market"), h.get("name"))
    return out


def holdings(plan_path, portfolio_path, watchlist_path):
    """The codes in the plan, the portfolio and the watchlist, in that order."""
    out = []
    plan = _read(plan_path, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        out.append(plan["symbol"])
    pf = _read(portfolio_path, {})
    for p in (pf.get("positions") or []) if isinstance(pf, dict) else []:
        out.append(p.get("symbol") or p.get("ticker"))
    wl = _read(watchlist_path, {})
    for h in (wl.get("hits") or []) if isinstance(wl, dict) else []:
        out.append(h.get("symbol") or h.get("ticker"))
    codes = []
    for s in out:
        try:
            c = tw.parse_symbol(str(s or ""))[0]
        except ValueError:
            continue
        if c not in codes:
            codes.append(c)
    return codes


def label(code, q=None, known=None, rule=None):
    """聯電 2303 (or just 2303 when no name is known)."""
    name = ((q or {}).get("name") or ((known or {}).get(code) or {}).get("name")
            or (rule or {}).get("name"))
    return ("%s %s" % (name, code)) if name else code


# ---------------------------------------------------------------- quotes

_urlopen = tw.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": tw.UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("連不上 %s：%s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s 回應 HTTP %d" % (url.split("?")[0], code))
    return text


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1m" % (host, sym)


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s 回應 HTTP %d" % (last[0].split("?")[0], last[1]))


def _limits(code, prev_close):
    if not prev_close:
        return None, None
    up, down = tw.price_limits(prev_close, code)
    return float(up), float(down)


def mis_quotes(payload):
    """A MIS getStockInfo answer -> {code: quote}. A code without a last trade is left out."""
    out = {}
    for code, m in tw.parse_mis(payload).items():
        if m.get("last") is None:
            continue
        when = None
        try:
            local = datetime.datetime.strptime("%s %s" % (m.get("date"), m.get("time")), "%Y%m%d %H:%M:%S")
            when = tw.taipei_to_utc(local)
        except (TypeError, ValueError):
            pass
        out[code] = {"symbol": code, "market": m["market"], "name": m.get("name") or None,
                     "price": m["last"], "prev_close": m.get("prev_close"), "limit_up": m.get("limit_up"),
                     "limit_down": m.get("limit_down"), "open": m.get("open"), "high": m.get("high"),
                     "low": m.get("low"), "time": utc_z(when) if when else None,
                     "when": "%s 成交" % m.get("time") if m.get("time") else "", "source": "mis"}
    return out


def chart_quote(doc, now_utc=None):
    """A Yahoo v8 chart -> (code, quote at now_utc or None).

    The live price (meta.regularMarketPrice) once its own time has passed; before
    that, in a replay, the close of the last bar that ended by now_utc. None when
    no bar had ended yet: a replay never sees the future."""
    try:
        res = doc["chart"]["result"]
    except (KeyError, TypeError):
        raise AlertError("Yahoo 的回應看不懂")
    if not res:
        err = ((doc.get("chart") or {}).get("error") or {}).get("description", "代號不存在")
        raise AlertError("Yahoo 沒有資料：%s" % err)
    r = res[0]
    meta = r.get("meta") or {}
    code, market = code_of(meta.get("symbol") or "")
    now_ts = tw.taipei_to_ts(tw.to_taipei(now_utc)) if now_utc else None
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    quote = {"symbol": code, "market": market, "name": None, "prev_close": prev, "source": "yahoo"}
    quote["limit_up"], quote["limit_down"] = _limits(code, prev)
    live_ts = meta.get("regularMarketTime")
    if meta.get("regularMarketPrice") is not None and live_ts and (now_ts is None or live_ts <= now_ts):
        quote.update({"price": float(tw.to_tick(meta["regularMarketPrice"], code)),
                      "time": utc_z(tw.taipei_to_utc(tw.ts_to_taipei(live_ts))),
                      "when": "%s 成交" % tw.ts_to_taipei(live_ts).strftime("%H:%M:%S"),
                      "high": meta.get("regularMarketDayHigh"), "low": meta.get("regularMarketDayLow")})
        return code, quote
    step = STEP_SECONDS.get(str(meta.get("dataGranularity") or "1m"), 60)
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    best = None
    for i, ts in enumerate(r.get("timestamp") or []):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        if now_ts is not None and ts + step > now_ts:
            break
        best = (ts, c)
    if best is None:
        return code, None
    ts, c = best
    bar = tw.ts_to_taipei(ts)
    known = tw.taipei_to_utc(tw.ts_to_taipei(ts + step))
    if now_utc and known > now_utc:
        known = now_utc
    quote.update({"price": float(tw.to_tick(c, code)), "time": utc_z(known),
                  "when": "%s 那根 %s K 收盤" % (bar.strftime("%H:%M"), str(meta.get("dataGranularity") or "1m")
                                                 .replace("m", " 分"))})
    return code, quote


def load_offline(paths):
    """Saved MIS JSON and Yahoo chart JSON from files or folders -> {"mis": [quotes...], "charts": {code: [doc]}}."""
    files = []
    for p in paths or []:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith((".json", ".json.gz"))]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise AlertError("找不到離線報價 %s" % p)
    out = {"mis": [], "charts": {}}
    for f in files:
        doc = _read(f, None)
        if isinstance(doc, dict) and "msgArray" in doc:
            out["mis"].append(mis_quotes(doc))
        elif isinstance(doc, dict) and "chart" in doc:
            res = (doc.get("chart") or {}).get("result")
            sym = ((res or [{}])[0].get("meta") or {}).get("symbol") if res else None
            if not sym:   # Yahoo's own "No data found" answer, saved as yahoo-<SYMBOL>.json
                m = re.match(r"^yahoo-(\d{4,6}[A-Z]?(?:\.TWO?)?)", os.path.basename(f))
                sym = m.group(1) if m else None
            if not sym:
                continue
            try:
                code = tw.parse_symbol(sym)[0]
            except ValueError:
                continue
            out["charts"].setdefault(code, []).append(doc)
    for code, docs in out["charts"].items():   # the finest bars first
        docs.sort(key=lambda d: STEP_SECONDS.get(
            str((((d.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}).get("dataGranularity")), 9999))
    return out


def _offline_quote(code, saved, source, now_utc):
    """(quote or None, why) from the saved files, never one stamped after now_utc."""
    mis, why = None, ""
    for batch in saved["mis"]:
        q = batch.get(code)
        if not q:
            continue
        t = parse_z(q.get("time"))
        if now_utc and t and t > now_utc:
            why = "存下來的 MIS 報價是 %s 的，比 --now 晚（重播不看未來的價格）" % tw.to_taipei(t).strftime("%Y-%m-%d %H:%M")
            continue
        if mis is None or (parse_z(mis.get("time")) or datetime.datetime.min) < (t or datetime.datetime.min):
            mis = q
    chart = None
    for doc in saved["charts"].get(code, []):
        try:
            _, chart = chart_quote(doc, now_utc)
        except AlertError as e:
            why = why or str(e)
            continue
        if chart:
            break
        why = why or "存下來的 K 線在 --now 之前還沒有一根收完"
    order = (mis, chart) if source == "mis" else (chart, mis)
    for q in order:
        if q:
            return q, ""
    return None, why or "離線檔案裡沒有 %s" % code


def get_quotes(codes, source="mis", offline=None, now_utc=None, known=None, pause=PAUSE):
    """{code: quote} and [errors]. MIS: one request for every code; a code it has no
    last trade for, or every code when it fails, goes to Yahoo, one at a time."""
    if source not in SOURCES:
        raise AlertError("報價來源只有 mis 或 yahoo")
    known = known or {}
    quotes, errors = {}, []
    if offline:
        saved = load_offline(offline)
        for code in codes:
            q, why = _offline_quote(code, saved, source, now_utc)
            if q:
                quotes[code] = q
            else:
                errors.append("%s：%s" % (code, why))
    else:
        left = list(codes)
        if source == "mis":
            pairs = []
            for code in codes:
                market = (known.get(code) or {}).get("market")
                pairs += [(code, market)] if market else [(code, "TWSE"), (code, "TPEx")]
            try:
                got = mis_quotes(fetch(tw.MIS_URL % "|".join(tw.mis_channel(c, m) for c, m in pairs)))
            except (AlertError, ValueError) as e:
                got = {}
                errors.append("MIS 報價抓不到（%s），改用 Yahoo" % e)
            for code in codes:
                if code in got:
                    quotes[code] = got[code]
            left = [c for c in codes if c not in quotes]
            if left and got:
                errors.append("MIS 沒有 %s 的成交價，改用 Yahoo" % "、".join(left))
        for i, code in enumerate(left):
            market = (known.get(code) or {}).get("market")
            tries = [tw.yahoo_symbol(code, market)] if market else [tw.yahoo_symbol(code, "TWSE"),
                                                                     tw.yahoo_symbol(code, "TPEx")]
            for j, sym in enumerate(tries):
                try:
                    if (i or j) and pause:
                        _sleep(pause)
                    _, q = chart_quote(json.loads(fetch_yahoo(sym)), now_utc)
                    if q:
                        quotes[code] = q
                    else:
                        errors.append("%s：Yahoo 還沒有今天的成交" % code)
                    break
                except (AlertError, ValueError) as e:
                    if j == len(tries) - 1:
                        errors.append("%s：%s" % (code, e))
    for code, q in quotes.items():
        row = known.get(code) or {}
        q["name"] = q.get("name") or row.get("name")
        q["market"] = q.get("market") or row.get("market")
    return quotes, errors


def write_prices(path, quotes, now_utc):
    """Merge quotes into trader/prices.json: the shape trader-budget and trader-risk read."""
    old = _read(path, {}) if os.path.exists(path) else {}
    if not isinstance(old, dict):
        old = {}
    prices = dict(old.get("prices") or {})
    detail = dict(old.get("detail") or {})
    stamp = utc_z(now_utc)
    for code, q in quotes.items():
        prices[code] = q["price"]
        detail[code] = {"time": q.get("time"), "fetched": stamp, "name": q.get("name"), "market": q.get("market"),
                        "prev_close": q.get("prev_close"), "limit_up": q.get("limit_up"),
                        "limit_down": q.get("limit_down"), "source": q.get("source")}
    sources = sorted({q.get("source") for q in quotes.values() if q.get("source")})
    doc = {"as_of": stamp, "source": "+".join(sources) or old.get("source", "mis"), "prices": prices,
           "detail": detail}
    _write(path, doc)
    return doc


def quote_line(code, q):
    chg = ""
    if q.get("prev_close"):
        chg = "  %+.2f%%（昨收 %s）" % (100.0 * (q["price"] / float(q["prev_close"]) - 1),
                                       tw.fmt_price(q["prev_close"], code))
    lim = ""
    if q.get("limit_up") and q.get("limit_down"):
        lim = "  漲停 %s／跌停 %s" % (tw.fmt_price(q["limit_up"], code), tw.fmt_price(q["limit_down"], code))
    mk = MARKET_LABEL.get(q.get("market"), "")
    return "%s%s  %s%s%s  %s  [%s]" % (label(code, q), ("（%s）" % mk) if mk else "", tw.fmt_price(q["price"], code),
                                       chg, lim, q.get("when") or "", q.get("source"))


# ---------------------------------------------------------------- rules

RULE_TYPES = ("above", "below", "stop", "target", "pct_move", "limit_up", "limit_down", "opening_bell", "time")
NO_QUOTE = ("opening_bell", "time")
ONCE_A_DAY = ("pct_move", "limit_up", "limit_down")


def _hhmm(text):
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(text or ""))
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(rule):
    rid = rule.get("id")
    t = rule.get("type")
    if t not in RULE_TYPES:
        raise AlertError("規則 %s：不認得的類型 %r" % (rid, t))
    if t not in NO_QUOTE:
        if not rule.get("symbol"):
            raise AlertError("規則 %s 要有代號（symbol）" % rid)
        code_of(str(rule["symbol"]))
    if t in ("above", "below", "stop", "target") and not (_num(rule.get("price")) and rule["price"] > 0):
        raise AlertError("規則 %s 要有價格（price，數字）" % rid)
    if t == "pct_move" and not (_num(rule.get("pct")) and rule["pct"] > 0):
        raise AlertError("規則 %s 要有百分比（pct，數字）" % rid)
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("規則 %s 的 at 要是台北時間，例如 \"13:20\"" % rid)
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("規則 %s 的 date 要是 YYYY-MM-DD" % rid)


def rule_id(rule):
    return rule.get("id") or "%s-%s" % (rule.get("symbol", "market"), rule["type"])


def active(rule, today):
    """A rule with a "date" is watched on that Taipei date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this Taipei date?"""
    t = parse_z(stamp)
    if t is None:
        return bool(stamp) and str(stamp)[:10] == today
    return tw.to_taipei(t).date().isoformat() == today


def _p(x, code):
    return tw.fmt_price(x, code)


def evaluate(rules, quotes, status, state, now_utc, known=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, type, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = tw.to_taipei(now_utc)
    stamp = utc_z(now_utc)
    now_word = "現在" if status["open"] else "最後成交"
    rings = []
    for r in rules:
        validate(r)
        rid = rule_id(r)
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "type": r["type"],
                              "message": "開盤：台股開盤了（台北 %s）。09:00 開盤集合競價之後逐筆交易，盤中零股 "
                                         "09:10 第一次撮合，13:25 起收盤集合競價，13:30 收盤。" % status["now_taipei"]})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status["trading_day"] and local.time() >= at and not _rang_today(rung.get(rid), today):
                what = r.get("label") or "時間提醒"
                msg = "%s：現在台北 %s（設定 %s），" % (what, local.strftime("%H:%M"), tw.hm(at))
                if local.time() < tw.CLOSING_AUCTION:
                    msg += "13:25 起是收盤集合競價（只收限價 ROD），13:30 收盤。"
                elif local.time() < tw.CLOSE:
                    msg += "現在是收盤集合競價，只收限價 ROD，13:30 收盤。"
                else:
                    msg += "已經收盤。"
                sym = str(r.get("symbol") or "")
                if sym:
                    code = code_of(sym)[0]
                    msg += ("%s 還有部位的話就平倉：trader-broker 的 ticket.py flatten --symbol %s 產生賣單，"
                            "再經過 trader-risk 和你的 yes。" % (label(code, quotes.get(code), known, r), code))
                rings.append({"id": rid, "symbol": sym, "type": r["type"], "message": msg})
                rung[rid] = stamp
            continue
        code = code_of(str(r["symbol"]))[0]
        q = quotes.get(code)
        if not q:
            continue
        p = q["price"]
        name = label(code, q, known, r)
        when = ("（%s）" % q["when"]) if q.get("when") else ""
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "大於或等於" if typ == "above" else "小於或等於"
                what = {"stop": "停損", "target": "停利"}.get(r["type"], "警示價")
                note = ("（%s）" % r["note"]) if r.get("note") else ""
                rings.append({"id": rid, "symbol": code, "type": r["type"],
                              "message": "%s %s %s%s，%s你的%s %s%s。" % (name, now_word, _p(p, code), when, word, what,
                                                                         _p(r["price"], code), note)})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": code, "type": r["type"],
                              "message": "%s %s %.2f%%，%s %s%s（%s %s）。" % (
                                  name, "漲" if move > 0 else "跌", abs(move), now_word, _p(p, code), when,
                                  "參考價" if r.get("ref") else "昨收", _p(ref, code))})
                rung[rid] = stamp
        elif typ in ("limit_up", "limit_down"):
            lim = q.get(typ)
            if lim is None and q.get("prev_close"):
                lim = _limits(code, q["prev_close"])[0 if typ == "limit_up" else 1]
            if lim is None:
                continue
            hit = p >= lim - 1e-9 if typ == "limit_up" else p <= lim + 1e-9
            if hit and not _rang_today(rung.get(rid), today):
                if typ == "limit_up":
                    msg = ("%s 漲停 %s%s：漲停鎖住時買單排隊也可能買不到；手上有部位的話，賣出通常不難。"
                           % (name, _p(lim, code), when))
                else:
                    msg = ("%s 跌停 %s%s：跌停鎖住時賣單可能賣不掉，停損也可能出不去；當沖沒沖掉的部分 T+2 要付全額。"
                           % (name, _p(lim, code), when))
                rings.append({"id": rid, "symbol": code, "type": r["type"], "message": msg})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> the stop, the target, flat by (13:20) and limit down, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("計畫裡沒有代號（先跑 trader-exit）")
    code = code_of(str(plan["symbol"]))[0]
    out = []
    for key, typ, word in (("stop", "stop", "計畫停損"), ("target", "target", "計畫停利")):
        v = plan.get(key)
        if not _num(v) or v <= 0:
            raise AlertError("計畫裡沒有%s（先跑 trader-exit）" % ("停損" if key == "stop" else "停利"))
        note = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (code, key), "symbol": code, "type": typ, "price": float(v),
                    "note": word + (("：" + note) if note else "")})
    flat = plan.get("flat_by") or cfg_flat_by or tw.FLAT_BY
    if _hhmm(flat) is None:
        raise AlertError("計畫的 flat_by %r 不是 13:20 這樣的時間" % flat)
    out.append({"id": "%s-flat-by" % code, "symbol": code, "type": "time", "at": flat,
                "label": "%s 前出清" % flat})
    out.append({"id": "%s-limit-down" % code, "symbol": code, "type": "limit_down"})
    for r in out:
        if plan.get("name"):
            r["name"] = str(plan["name"])
        if plan.get("date"):
            r["date"] = str(plan["date"])
    return out


def describe(rule):
    """One line in Chinese for a rule."""
    t = rule["type"]
    sym = str(rule.get("symbol") or "")
    if sym:
        try:
            sym = label(code_of(sym)[0], rule=rule)
        except AlertError:
            pass
    if t in ("above", "below", "stop", "target"):
        word = {"above": "到或高於", "target": "停利：到或高於", "below": "到或低於", "stop": "停損：到或低於"}[t]
        s = "%s %s %s" % (sym, word, _n(rule["price"]))
    elif t == "pct_move":
        s = "%s 比%s%s %s%%" % (sym, "參考價 %s " % _n(rule["ref"]) if rule.get("ref") else "昨收",
                               {"up": "漲", "down": "跌"}.get(rule.get("direction"), "漲跌"), _n(rule["pct"]))
    elif t == "limit_up":
        s = "%s 漲停" % sym
    elif t == "limit_down":
        s = "%s 跌停" % sym
    elif t == "opening_bell":
        s = "開盤（09:00）"
    else:
        s = "%s%s 響：%s%s" % ("" if rule.get("date") else "每個交易日 ", rule["at"], rule.get("label") or "時間提醒",
                             ("（%s）" % sym) if sym else "")
    if rule.get("date"):
        s += "，只看 %s" % rule["date"]
    if rule.get("rearm"):
        s += "，回頭後重新啟用"
    return s


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- commands

def cmd_clock(a):
    cfg, cal = load_context(a.config)
    st = market_status(parse_now(a.now), cal)
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print(status_line(st))
        if st["trading_day"]:
            print("今天：08:30 起接受委託，09:00 開盤，盤中零股 09:10 第一次撮合（之後每 5 秒），"
                  "13:25 起收盤集合競價（只收限價 ROD），13:30 收盤。")
        if not st["open"]:
            print("下一次開盤：%s。" % st["next_open"])
        if st.get("note"):
            print(st["note"] + "。")
    return 0 if st["open"] else 1


def cmd_calendar(a):
    cfg, cal = load_context(a.config)
    now = parse_now(a.now)
    if a.update is not None:
        year = a.update or (tw.to_taipei(now).year + 1)
        if a.offline:
            if not os.path.isfile(a.offline):
                raise AlertError("找不到 %s" % a.offline)
            with open(a.offline, encoding="utf-8") as fh:
                text = fh.read()
            get = lambda url: text  # noqa: E731
        else:
            get = fetch
        try:
            closed = tw.fetch_calendar(year, get=get)
        except ValueError as e:
            print("%s，還不能更新。颱風假本來就不在證交所的清單裡，臨時休市請寫進 trader/config.json 的 "
                  "extra_closed。" % e)
            return 1
        path = tw.save_calendar(year, closed, trader_dir(a.config))
        weekdays = sorted(d for d in closed if datetime.date.fromisoformat(d).weekday() < 5)
        print("已更新 %d 年的休市日：%d 個平日休市，存到 %s。" % (year, len(weekdays), path))
        for d in weekdays:
            print("  %s  %s" % (tw.day_label(datetime.date.fromisoformat(d)), closed[d]))
        print("颱風假不在證交所的清單裡：臨時休市請寫進 trader/config.json 的 extra_closed。")
        return 0
    year = a.year or tw.to_taipei(now).year
    days = sorted((d, n) for d, n in cal.closed_days.items()
                  if d.startswith("%d-" % year) and datetime.date.fromisoformat(d).weekday() < 5)
    if a.json:
        print(json.dumps({"year": year, "known": cal.known(year), "closed": dict(days)}, ensure_ascii=False, indent=2))
        return 0 if cal.known(year) else 1
    if not cal.known(year):
        print(unknown_year_note(cal, datetime.date(year, 1, 1)) + "。")
        for d, n in days:
            print("  %s  %s" % (tw.day_label(datetime.date.fromisoformat(d)), n))
        return 1
    print("%d 年的平日休市日（週末另計）：" % year)
    for d, n in days:
        print("  %s  %s" % (tw.day_label(datetime.date.fromisoformat(d)), n))
    print("颱風假不在證交所的清單裡：臨時休市請寫進 trader/config.json 的 extra_closed。")
    return 0


def _known(a):
    return known_symbols(a.config, getattr(a, "plan", None), getattr(a, "portfolio", None),
                         getattr(a, "watchlist", None))


def cmd_quote(a):
    codes = []
    known = _known(a)
    for s in a.symbols:
        code, market = code_of(s)
        if market:
            known.setdefault(code, {"market": None, "name": None})["market"] = market
        codes.append(code)
    if a.holdings:
        codes += holdings(a.plan, a.portfolio, a.watchlist)
    codes = [c for i, c in enumerate(codes) if c not in codes[:i]]
    if not codes:
        raise AlertError("要報哪幾檔？alerts.py quote 2303 6488，或加 --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(codes, a.source, a.offline, now if a.now else None, known)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for c in codes:
            if c in quotes:
                print(quote_line(c, quotes[c]))
        for e in errors:
            print("注意：" + e)
        if a.write and quotes:
            print("已把 %d 檔報價寫進 %s。" % (len(quotes), a.write))
        print("免費報價可能延遲，只拿來提醒和檢查張數；真正成交的是券商的價格。")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("%s 裡沒有規則：先用 alerts.py add --from-plan 加" % a.rules)
    rules = doc["rules"]
    for r in rules:
        validate(r)
    now = parse_now(a.now)
    cfg, cal = load_context(a.config)
    status = market_status(now, cal)
    known = _known(a)
    watched = [r for r in rules if active(r, status["date"])]
    codes = []
    for r in watched:
        if r.get("type") in NO_QUOTE:
            continue
        code, market = code_of(str(r["symbol"]))
        if market or r.get("market"):
            known.setdefault(code, {"market": None, "name": None})["market"] = market or r.get("market")
        if code not in codes:
            codes.append(code)
    quotes, errors = get_quotes(codes, a.source, a.offline, now if a.now else None, known) if codes else ({}, [])
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now, known)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {c: q["price"] for c, q in quotes.items()}}, ensure_ascii=False, indent=2))
    else:
        print(status_line(status))
        if status.get("note"):
            print(status["note"] + "。")
        for r in rings:
            print("警示 " + r["message"])
        if not rings:
            seen = "、".join("%s %s" % (label(c, quotes[c], known), _p(quotes[c]["price"], c))
                            for c in codes if c in quotes)
            print("沒有警示。今天看 %d 條規則%s。" % (len(watched), ("，" + seen) if seen else ""))
        for e in errors:
            print("注意：" + e)
        if rings:
            print("警示只是資訊，不是委託。台股交易所沒有停損單：在券商 App 設了條件單（觸價單）的話，先看它有沒有"
                  "成交，成交了用 trader-broker 的 ticket.py fill 記錄；沒設的話，要不要賣由你決定，賣出用 "
                  "ticket.py flatten，再經過 trader-risk 和你的 yes。")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("%s 沒有計畫（先跑 trader-exit）" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("已加 %s：%s" % (r["id"], describe(r)))
        print("部位還在的時候，每一兩分鐘跑一次 alerts.py check（排程或 /loop）。免費報價可能延遲；"
              "警示不會幫你賣，真正出場要靠你在券商 App 設的條件單或自己下的賣單。")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at 要是台北時間，例如 13:20")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "時間提醒"}
        if a.symbol:
            rule["symbol"] = code_of(a.symbol)[0]
    else:
        if not a.symbol:
            raise AlertError("要給 --symbol（或用 --from-plan、--at、--opening-bell）")
        code = code_of(a.symbol)[0]
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.limit_up:
            rule = {"type": "limit_up"}
        elif a.limit_down:
            rule = {"type": "limit_down"}
        else:
            raise AlertError("給 --above、--below、--stop、--target、--pct、--limit-up 或 --limit-down 其中一個")
        val = rule.get("price", rule.get("pct"))
        rid = a.id or ("%s-%s%s" % (code, rule["type"].replace("_", "-"), ("-" + _n(val)) if val is not None else ""))
        rule = dict({"id": rid, "symbol": code}, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("已經有 id 是 %s 的規則" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("已加 %s：%s" % (rule["id"], describe(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("還沒有規則。")
        return 1
    for r in doc["rules"]:
        rid = rule_id(r)
        rung = (state.get("rung") or {}).get(rid)
        when = ""
        if rung and parse_z(rung):
            when = "（%s 響過）" % tw.to_taipei(parse_z(rung)).strftime("%Y-%m-%d %H:%M")
        print("%-22s %s%s" % (rid, describe(r), when))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in (state.get("rung") or {}):
        print("%s 還沒響過，不用重新啟用。" % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("已重新啟用 %s。" % a.id)
    return 0


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="到價提醒和台股開收盤時間（trader-alerts）。不會下單。")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default="trader/config.json", help="flat_by、extra_closed（預設 trader/config.json）")
        p.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 09:42\"")
        p.add_argument("--json", action="store_true")

    def net(p):
        common(p)
        p.add_argument("--source", default="mis", choices=list(SOURCES), help="報價來源（預設 mis，沒有就用 Yahoo）")
        p.add_argument("--offline", action="append", metavar="PATH",
                       help="存下來的 MIS JSON 或 Yahoo K 線 JSON（檔案或資料夾，可重複），不連網")
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--watchlist", default="trader/watchlist.json")

    p = sub.add_parser("clock", help="台股現在有沒有開？")
    common(p)
    p = sub.add_parser("calendar", help="休市日；--update 抓證交所公布的年度休市日")
    common(p)
    p.add_argument("--year", type=int, help="列出哪一年（預設今年）")
    p.add_argument("--update", nargs="?", type=int, const=0, metavar="YEAR",
                   help="抓這一年的休市日存進 trader/state（預設明年）")
    p.add_argument("--offline", metavar="FILE", help="存下來的證交所休市日 JSON，不連網")
    p = sub.add_parser("quote", help="報價")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="加上計畫、部位和觀察清單的代號")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="寫進 prices.json（預設 trader/prices.json）")
    p = sub.add_parser("check", help="檢查每一條規則，到了就響")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="加一條規則")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--limit-up", action="store_true", help="碰到漲停就響")
    g.add_argument("--limit-down", action="store_true", help="碰到跌停就響")
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="時間提醒，台北時間")
    g.add_argument("--from-plan", action="store_true", help="停損、停利、出清時間和跌停，取自計畫")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="計畫沒有 flat_by 時用設定檔的")
    p.add_argument("--label", help="時間提醒要說什麼，例如 \"13:20 前出清\"")
    p.add_argument("--date", help="只在這個台北日期看這條規則（YYYY-MM-DD）")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="列出規則")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="重新啟用一條響過的規則")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "calendar": cmd_calendar, "quote": cmd_quote, "check": cmd_check,
                "add": cmd_add, "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
