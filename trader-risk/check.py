#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk, Taiwan market. Standard library only.

Reads the proposed tickets (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json), prices
(trader/prices.json), what was placed today (trader/state/broker.json) and the
day's exchange lists (處置股 and the stocks that may be day-traded). Each
ticket gets PASS or BLOCK with the reasons. Tickets are checked in file order
as if each PASS had filled, so buys that are fine one by one but break a limit
together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-02 09:20"        # pin the clock (Taipei time)
    python3 check.py --lists trader/state/lists      # saved lists (a folder or JSON files), no fetching
    python3 check.py --lists none                    # skip the lists: a warning says what was not checked
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

The lists. Without --lists it uses today's copies in trader/state/lists/, or
fetches them (TWSE and TPEx OpenAPI) and keeps a copy there. A list it cannot
get is a warning, never a crash: the report says what it did not check.

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell that
would go short). An EXIT only reduces what you hold. Getting out must always be
possible: an exit is checked for format, the symbol, short sales and what the
exchange allows (今天買的零股今天不能賣); every other rule turns into a warning.

What blocks a ticket (every ticket):

  格式      side, lot and qty (整股 a multiple of 1,000 shares, 零股 1 to 999), price
            type and price (a limit price on the tick grid, a market order has none),
            time in force (ROD / IOC / FOK), trade (day_trade / cash); 零股 only limit
            ROD and never day_trade; before 09:00, in the 13:25 closing auction and
            after the close, 整股 only limit ROD; stop and target on the tick grid
  漲跌停    a limit price outside the day's limit down to limit up
  標的      not a Taiwan stock or ETF code
  放空      selling more than you hold, unless "allow_short": true
  交割      selling shares bought today that cannot be sold today: odd lots (next
            trading day at the earliest), board lots without 現股當沖

What blocks an entry (on top):

  封鎖      a code on your "blocked" list
  處置股    an active 處置股 (call auction every few minutes, prepaid), unless allow_disposition
  當沖      a day trade while "day_trade_enabled" is false; a code missing from the
            day's 現股當沖標的 list; 先賣後買 where it is suspended
  當沖額度  today's day-trade buys plus this one over day_trade_quota_twd
  留倉      a cash buy (odd lots, or 整股 not day-traded) unless "allow_overnight": true
  金額      under min_order_twd, over max_order_twd
  部位      the stock would be over max_position_pct of the account
  現金      the buy plus its fee would dip into your cash buffer or need more cash
            than you have (a day trade that cannot be sold is due in full at T+2)
  重複      the same code and side twice in this batch, or already placed today
  每日筆數  max_orders_per_day tickets placed today
  停損      no stop while "require_stop": true; a stop on the wrong side; a stop
            under the day's limit down (it cannot trigger today)
  漲跌停    a buy at or above limit up
  虧損      the loss at the stop, fees and tax included (twmarket.loss_at_stop),
            over max_loss_per_trade_twd. The entry is the limit price for a limit
            order, else the HIGHER of the plan's entry and a fresh price (under 15
            minutes old), so a stale plan cannot understate the risk
  報酬風險比 (target - entry) / (entry - stop) under min_reward_risk; after costs
            under min_reward_risk_net when that is set
  單日虧損  the losses at the stops of today's entries plus this one over max_daily_loss_twd
  交易次數  max_trades_per_day entries already placed today
  時段      a closed day (weekend, TWSE holiday, typhoon day, extra_closed), outside
            09:00 to 13:30, before no_entries_before or after no_entries_after (Taipei)

Exit code 0 when every ticket passes, 1 when any is blocked. The verdicts go to
trader/state/risk.json with a fingerprint of each ticket (twmarket.fingerprint),
so trader-broker only shows a ticket as ready if THIS exact ticket passed today.
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_twd": 250000,
    "min_order_twd": 1000, "max_loss_per_trade_twd": 3000, "max_daily_loss_twd": 9000,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "min_reward_risk_net": None,
    "require_stop": True, "no_entries_before": tw.NO_ENTRIES_BEFORE, "no_entries_after": tw.NO_ENTRIES_AFTER,
    "flat_by": tw.FLAT_BY, "day_trade_enabled": False, "day_trade_quota_twd": None, "allow_overnight": False,
    "allow_disposition": False, "paper": True, "blocked": [], "allow_options": False, "allow_margin": False,
    "allow_short": False,
}
FRESH_MINUTES = 15
EPS = 1e-9
REF_WORDS = {"plan": "計畫進場價", "price": "最新價", "limit": "限價", "stale price": "舊價格"}
LOT_WORDS = {"board": "整股", "odd": "零股"}
TRADE_WORDS = {"day_trade": "現股當沖", "cash": "現股"}
SIDE_WORDS = {"buy": "買進", "sell": "賣出"}
PHASE_WORDS = {"pre_open": "開盤前（09:00 之前）", "closing": "收盤集合競價（13:25 到 13:30）",
               "closed": "收盤後或休市"}
# trader/state/lists/<name>-YYYY-MM-DD.json: the raw lists, as published
LIST_SOURCES = (("punish", tw.PUNISH_URL), ("disposal", tw.TPEX_DISPOSAL_URL),
                ("twse-daytrade", tw.DAYTRADE_URL), ("tpex-daytrade", tw.TPEX_DAYTRADE_URL))
http_get = tw.http_get   # tests swap this out; nothing in the tests touches the network


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _p(x, code=""):
    try:
        return tw.fmt_price(x, code)
    except (ValueError, ArithmeticError):
        return str(x)


def _step(x, ref, code=""):
    """A price distance with the decimals of the tick at ref: 1.5, 2.0, 0.50."""
    try:
        places = max(0, -tw.tick_size(ref, code).normalize().as_tuple().exponent)
    except (ValueError, ArithmeticError):
        return _num(x)
    return "%.*f" % (places, float(x))


def _qty(x):
    """An int share count from 1000 or "1000", or None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x == int(x):
        return int(x)
    if isinstance(x, str) and re.match(r"^\s*\d+\s*$", x):
        return int(x)
    return None


def _code(sym):
    """'2303', '2303.TW' -> '2303'; anything else upper-cased as it is."""
    try:
        return tw.parse_symbol(sym)[0]
    except ValueError:
        return str(sym or "").strip().upper()


def hhmm(value, key):
    """"09:15" -> time(9, 15); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    try:
        return tw.parse_hhmm(value, key)
    except ValueError:
        raise RiskError("%s 要像 \"09:15\" 這樣的台北時間，現在是 %r" % (key, value))


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("沒有%s：%s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise RiskError("%s 不是有效的 JSON：%s" % (path, e))


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- the clock and the session

def phase(now_tp, cal):
    """Where the session is: closed, pre_open, continuous or closing."""
    if not cal.is_trading_day(now_tp.date()):
        return "closed"
    t = now_tp.time()
    if t < tw.OPEN:
        return "pre_open"
    if t < tw.CLOSING_AUCTION:
        return "continuous"
    if t < tw.CLOSE:
        return "closing"
    return "closed"


def window_check(now_tp, c, cal):
    """Reasons an entry cannot be placed now (Taipei time), and warnings. Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_tp.date(), now_tp.time().replace(second=0, microsecond=0)
    note = cal.unknown_note(d)
    if note:
        warns.append(note)
    stamp = "%s %s" % (tw.day_label(d), tw.hm(now_tp))
    why = cal.closed(d)
    if why:
        return ["時段：今天休市（%s，台北時間 %s）" % (why, stamp)], warns
    if t < tw.OPEN or now_tp.time() >= tw.CLOSE:
        return ["時段：現在不是交易時間（台北時間 %s；盤中是 %s 到 %s）"
                % (stamp, tw.hm(tw.OPEN), tw.hm(tw.CLOSE))], warns
    if before is not None and t < before:
        return ["時段：%s 之前不進場（現在 %s）" % (tw.hm(before), tw.hm(t))], warns
    if after is not None and t > after:
        return ["時段：%s 之後不再進場（現在 %s）" % (tw.hm(after), tw.hm(t))], warns
    return [], warns


# ---------------------------------------------------------------- the exchange lists

def _list_rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise RiskError("%s 不是有效的 JSON：%s" % (path, e))


def read_lists(paths):
    """Saved raw lists (folders or JSON files) -> {"disposition", "daytrade", "files"}.
    A kind with no file is None (not checked). Files are told apart by their rows
    (twmarket.list_kind), an empty list by its name (punish, disposal, daytrade)."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith(".json")]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise RiskError("%s 沒有清單檔" % p)
    twse, tpex, daytrade = [], [], []
    seen = set()
    for f in files:
        rows = _list_rows(f)
        kind = tw.list_kind(rows)
        base = os.path.basename(f).lower()
        if kind is None and isinstance(rows, list):
            kind = "daytrade" if "daytrade" in base else ("disposition" if ("punish" in base or "disposal" in base)
                                                          else None)
        if kind == "disposition":
            seen.add(kind)
            for r in rows:
                (tpex if isinstance(r, dict) and "SecuritiesCompanyCode" in r else twse).append(r)
        elif kind == "daytrade":
            seen.add(kind)
            daytrade += rows
    return {"disposition": tw.parse_disposition(twse, tpex) if "disposition" in seen else None,
            "daytrade": tw.parse_daytrade(daytrade) if "daytrade" in seen else None, "files": files}


def cached_lists(folder, today):
    """Today's copies of the four lists in folder, fetching the missing ones.
    Returns (paths that exist, notes about the ones it could not get)."""
    paths, notes = [], []
    for name, url in LIST_SOURCES:
        path = os.path.join(folder, "%s-%s.json" % (name, today))
        if not os.path.isfile(path):
            try:
                rows = json.loads(http_get(url))
                if not isinstance(rows, list):
                    raise ValueError("不是清單")
            except (urllib.error.URLError, OSError, ValueError) as e:
                notes.append("%s 抓不到（%s）" % (url, getattr(e, "reason", e)))
                continue
            tw.write_json(path, rows)
        paths.append(path)
    return paths, notes


def resolve_lists(arg, state_path, today):
    """--lists -> (lists dict or None, notes)."""
    if arg and len(arg) == 1 and arg[0].lower() == "none":
        return None, ["沒有檢查處置股和現股當沖標的清單（--lists none）：沒有確認能不能當沖／是不是處置股"]
    if arg:
        return read_lists(arg), []
    folder = os.path.join(os.path.dirname(state_path) or ".", "lists")
    paths, notes = cached_lists(folder, today)
    lists = read_lists(paths) if paths else None
    if notes:
        notes = ["清單抓不到：%s。沒有確認能不能當沖／是不是處置股" % "；".join(notes)]
    return lists, notes


# ---------------------------------------------------------------- the account and the log

def account(cfg, portfolio, prices, today):
    """Starting account: cash, shares and values, plus the shares bought today by lot.
    No portfolio = a fresh account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("沒有 trader/portfolio.json，設定檔也沒有 budget：沒有東西可以拿來檢查")
        return {"cash": budget, "shares": {}, "values": {}, "today": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json 要有 \"cash\" 數字")
    shares, values, bought = {}, {}, {}
    for p in portfolio.get("positions", []):
        t = _code(p.get("symbol") or p.get("ticker"))
        if not t:
            continue
        lots = p.get("lots", [])
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in lots)
        shares[t] = shares.get(t, 0) + n
        for l in lots:
            if str(l.get("date", ""))[:10] == today:
                b = bought.setdefault(t, {"board": 0, "odd": 0})
                kind = l.get("lot") or ("board" if (_f(l.get("shares")) or 0) >= tw.LOT else "odd")
                b["odd" if kind == "odd" else "board"] += int(_f(l.get("shares")) or 0)
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in lots)
            values[t] = basis
            warnings.append("沒有 %s 的價格：用成本 %s 估市值" % (t, tw.twd(basis)))
    return {"cash": cash, "shares": shares, "values": values, "today": bought}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; without it a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Tickets the user placed today, as trader-broker logged them."""
    return [r for r in (state or {}).get("orders", []) if r.get("status") == "placed" and rec_day(r) == today]


def load_prices(pdoc):
    """prices.json -> ({code: price}, {code: price time as naive UTC or None}).
    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        code = _code(s)
        out[code] = float(p)
        d = detail.get(s) or detail.get(code) or {}
        times[code] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          lists=None, cal=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...], ...}.

    now          naive UTC (default: the real clock); its Taipei date is "today"
    price_times  {code: naive UTC time of that price}; None = every price counts as fresh
    lists        {"disposition": ..., "daytrade": ...} from read_lists; None = not checked
    cal          a twmarket.Calendar (default: the built-in one plus the config's extra_closed)"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {_code(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    if price_times is not None:
        price_times = {_code(k): v for k, v in price_times.items()}
    now_utc = now or tw.utc_now()
    now_tp = tw.to_taipei(now_utc)
    today = today or now_tp.date().isoformat()
    day = datetime.date.fromisoformat(today)
    cal = cal or tw.Calendar().add(c.get("extra_closed"))
    fees = tw.fee_settings(c)
    acct, top_warn = account(c, portfolio, prices, today)
    blocked = {_code(b) for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today, dt_today = 0.0, 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("今天送出的一張進場單（%s %s）紀錄裡沒有停損虧損：沒有算進單日虧損"
                            % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    for r in done_today:
        if r.get("trade") == "day_trade" and str(r.get("side", "")).lower() == "buy":
            dt_today += _f(r.get("value_twd")) or 0.0
    placed_keys = {(_code(r.get("symbol")), str(r.get("side", "")).lower()) for r in done_today}
    win_reasons, win_warns = window_check(now_tp, c, cal)
    now_phase = phase(now_tp, cal)
    lists = lists or {}
    disp = lists.get("disposition")
    daytrade = lists.get("daytrade")
    active = tw.active_disposition(disp, day) if disp is not None else None
    if daytrade is not None and daytrade.get("date"):
        try:
            prev = cal.previous_trading_day(day)
        except ValueError:
            prev = None
        if prev and daytrade["date"] < prev:
            top_warn.append("現股當沖標的清單是 %s 的，比前一個交易日（%s）還舊：可能已經過期"
                            % (daytrade["date"].isoformat(), prev.isoformat()))
    if not c.get("paper", True):
        top_warn.append("真錢帳戶（\"paper\": false）：這些委託會動用真錢")
    top_warn.extend(win_warns)
    seen = set()
    results = []
    batch_entries, batch_loss, batch_dt = 0, 0.0, 0.0

    def fresh(code):
        if code not in prices:
            return None
        if price_times is None:
            return prices[code]
        t = price_times.get(code)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[code] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        o = o if isinstance(o, dict) else {}
        raw_sym = str(o.get("symbol", "")).strip()
        try:
            code, sym_market = tw.parse_symbol(raw_sym)
        except ValueError:
            code, sym_market = raw_sym.upper(), None
            reasons.append("標的：%s 不像台股代號；這套只做上市、上櫃的股票和 ETF" % (raw_sym or "（空白）"))
        side = str(o.get("side", "")).strip().lower()
        lot = str(o.get("lot", "")).strip().lower()
        qty = _qty(o.get("qty"))
        ptype = str(o.get("price_type", "")).strip().lower()
        tif = str(o.get("tif", "")).strip().upper()
        trade = str(o.get("trade", "")).strip().lower()
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        market = o.get("market") or sym_market
        name = o.get("name") or ""
        price = _f(o.get("price"))
        limit_up, limit_down = _f(plan.get("limit_up")), _f(plan.get("limit_down"))

        # 格式
        if side not in ("buy", "sell"):
            reasons.append("格式：side 要是 buy 或 sell，現在是 %r" % o.get("side"))
        if o.get("market") and o.get("market") not in tw.MARKETS:
            reasons.append("格式：market 要是 TWSE（上市）或 TPEx（上櫃），現在是 %r" % o.get("market"))
        elif o.get("market") and sym_market and o.get("market") != sym_market:
            reasons.append("格式：代號 %s 是 %s，委託單寫 %s" % (raw_sym, sym_market, o.get("market")))
        elif not market:
            warns.append("委託單沒有標明上市（TWSE）或上櫃（TPEx）：下單前確認市場")
        if lot not in tw.LOTS:
            reasons.append("格式：lot 要是 board（整股）或 odd（零股），現在是 %r" % o.get("lot"))
        if qty is None or qty <= 0:
            reasons.append("格式：qty 要是正整數股數（1 張 = 1000），現在是 %r" % o.get("qty"))
            qty = None
        elif lot == "board" and qty % tw.LOT:
            reasons.append("格式：整股的 qty 要是 1000 的倍數，%d 股不是；零頭用零股（lot odd）另外下" % qty)
        elif lot == "odd" and qty >= tw.LOT:
            reasons.append("格式：零股一次 1 到 999 股，%d 股要用整股（lot board）" % qty)
        if ptype not in tw.PRICE_TYPES:
            reasons.append("格式：price_type 要是 market（市價）或 limit（限價），現在是 %r" % o.get("price_type"))
        elif ptype == "limit":
            if price is None or price <= 0:
                reasons.append("格式：限價單要有 price（正數）")
                price = None
            elif code and not tw.on_tick(price, code):
                reasons.append("格式：限價 %s 不在升降單位上（這個價位每檔 %s）"
                               % (o.get("price"), tw.tick_size(price, code)))
        elif o.get("price") is not None:
            reasons.append("格式：市價單不帶 price（現在是 %r）" % o.get("price"))
        if tif not in tw.TIFS:
            reasons.append("格式：tif 要是 ROD、IOC 或 FOK，現在是 %r" % o.get("tif"))
        if trade not in tw.TRADES:
            reasons.append("格式：trade 要是 day_trade（現股當沖）或 cash（現股），現在是 %r" % o.get("trade"))
        if lot == "odd":
            if ptype != "limit" or tif != "ROD":
                reasons.append("格式：盤中零股只接受限價 ROD")
            if trade == "day_trade":
                reasons.append("格式：零股不能當沖：今天買的零股最快下一個交易日才能賣")
        elif lot == "board" and now_phase != "continuous" and (ptype == "market" or tif in ("IOC", "FOK")):
            reasons.append("格式：現在是%s，整股只接受限價 ROD（市價、IOC、FOK 會被退單）" % PHASE_WORDS[now_phase])
        for key, word in (("stop", "停損"), ("target", "停利")):
            if o.get(key) is None:
                continue
            v = _f(o.get(key))
            if v is None or v <= 0:
                reasons.append("格式：%s要是正的價格，現在是 %r" % (word, o.get(key)))
            elif code and not tw.on_tick(v, code):
                reasons.append("格式：%s %s 不在升降單位上（這個價位每檔 %s）" % (word, o.get(key), tw.tick_size(v, code)))
        if ptype == "limit" and price:
            if limit_up and price > limit_up + EPS:
                reasons.append("漲跌停：限價 %s 高於今天的漲停價 %s，會被退單" % (_p(price, code), _p(limit_up, code)))
            elif limit_down and price < limit_down - EPS:
                reasons.append("漲跌停：限價 %s 低於今天的跌停價 %s，會被退單" % (_p(price, code), _p(limit_down, code)))

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(code)
        ref, ref_from = None, None
        if ptype == "limit" and price:
            ref, ref_from = price, "limit"
        else:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                if ref_from == "price":
                    try:
                        ref = float(tw.to_tick(ref, code, "up"))
                    except (ValueError, ArithmeticError):
                        pass
                    if plan_entry:      # an exit from flatten has no plan entry (2026-11-03 10:48 printed None)
                        info.append("%s 最新價 %s 高於計畫進場價 %s：風險從 %s 算"
                                    % (code, _p(ref, code), _p(plan_entry, code), _p(ref, code)))
            elif code in prices:
                ref, ref_from = prices[code], "stale price"
        if plan_entry and live is None and code in prices and price_times is not None and ptype == "market":
            warns.append("trader/prices.json 裡 %s 的價格超過 %d 分鐘：沒有用" % (code, FRESH_MINUTES))
        value = round(float(tw.dec(ref) * qty), 2) if (ref and qty) else None

        # entry or exit
        held = acct["shares"].get(code, 0.0)
        held_value = acct["values"].get(code, 0.0)
        exiting = False
        if side == "sell" and held > EPS and qty is not None:
            exiting = qty <= held + EPS
        elif side == "buy" and held < -EPS and qty is not None:
            exiting = qty <= -held + EPS
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and qty:
            later.append("價格：沒有 %s 的價格可以算金額；加到 trader/prices.json 再檢查一次" % code)

        # 交割: what can be sold today
        if side == "sell" and kind == "exit" and qty is not None:
            b = acct["today"].get(code, {"board": 0, "odd": 0})
            odd_today = b["odd"]
            board_today = b["board"] if not c.get("day_trade_enabled") else 0
            free = held - odd_today - board_today
            if qty > free + EPS:
                if odd_today and qty > held - odd_today + EPS:
                    reasons.append("交割：今天買的 %d 股零股最快下一個交易日才能賣（零股不能當沖）" % odd_today)
                if board_today and qty > held - board_today + EPS:
                    reasons.append("交割：今天買的 %s，要已開通現股當沖才能今天賣（\"day_trade_enabled\": false）；"
                                   "沒開通就賣是違約交割" % tw.fmt_shares(board_today))

        # 封鎖, 處置股, 當沖, 留倉
        if code in blocked:
            later.append("封鎖：%s 在你的封鎖清單上" % code)
        if active is not None and code in active:
            dd = active[code]
            msg = ("處置股：%s %s 是處置股（到 %s%s，要預收款券）"
                   % (code, dd.get("name") or name, dd["end"].isoformat(),
                      ("，每 %d 分鐘撮合一次" % dd["every_minutes"]) if dd.get("every_minutes") else ""))
            if c.get("allow_disposition"):
                warns.append(msg + "；allow_disposition 開著，沒有擋")
            else:
                later.append(msg + "；不進場")
        if trade == "day_trade" and lot != "odd":
            if not c.get("day_trade_enabled"):
                later.append("當沖：設定檔沒有開啟現股當沖（\"day_trade_enabled\": false）。沒簽當沖契約就當天賣出"
                             "會變成違約交割；已經在券商簽好，再把它改成 true")
            if daytrade is not None:
                row = daytrade["codes"].get(code)
                stamp = daytrade["date"].isoformat() if daytrade.get("date") else "日期不明"
                if row is None:
                    later.append("當沖：%s 不在現股當沖標的清單上（%s 的清單）；處置股、全額交割股等不能當沖" % (code, stamp))
                elif side == "sell" and not row.get("sell_first", True):
                    later.append("當沖：%s 暫停先賣後買（%s 的清單）" % (code, stamp))
            quota = _f(c.get("day_trade_quota_twd"))
            if quota and side == "buy" and value is not None:
                used = dt_today + batch_dt
                if used + value > quota + EPS:
                    later.append("當沖額度：今天已用 %s，加這張 %s = %s，超過你的當沖額度 %s"
                                 % (tw.twd(used), tw.twd(value), tw.twd(used + value), tw.twd(quota)))
        if trade == "cash" and side == "buy" and not c.get("allow_overnight"):
            what = "零股" if lot == "odd" else "股票"
            later.append("留倉：這張是現股買進（不是當沖）。今天買的%s最快下一個交易日才能賣，今天的停損用不上，"
                         "隔天開盤可能跳空；賣出證交稅 %s%%。要做就在設定檔開啟 allow_overnight"
                         % (what, _num(tw.sell_tax_rate(code, False, day) * 100)))
        if kind == "entry":
            no_disp = active is None
            no_dt = trade == "day_trade" and lot != "odd" and daytrade is None
            if no_disp and no_dt:
                warns.append("清單：沒有處置股和現股當沖標的清單，沒有確認能不能當沖／是不是處置股")
            elif no_disp:
                warns.append("清單：沒有處置股清單，沒有確認是不是處置股")
            elif no_dt:
                warns.append("清單：沒有現股當沖標的清單，沒有確認能不能當沖")

        # 金額, 部位, 現金
        if value is not None:
            if value < float(c.get("min_order_twd") or 0) - EPS:
                later.append("金額：%s 低於你的最小委託 %s" % (tw.twd(value), tw.twd(c["min_order_twd"])))
            if c.get("max_order_twd") and value > float(c["max_order_twd"]) + EPS:
                later.append("金額：%s 超過你的單筆上限 %s" % (tw.twd(value), tw.twd(c["max_order_twd"])))
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_twd = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        buy_fee = 0
        if value is not None and side == "buy":
            buy_fee = tw.broker_fee(value, fees["discount"], fees["minimum"] if lot == "board" else fees["odd_minimum"])
        if value is not None and side == "buy" and kind == "entry":
            need = value + buy_fee
            after_cash = cash - need
            if after_cash < -EPS and not c.get("allow_margin"):
                later.append("現金：這張要 %s（含手續費 %s），現金只有 %s。當沖如果賣不掉（例如跌停鎖死），"
                             "T+2 要付全額，付不出來就是違約交割" % (tw.twd(need), tw.twd(buy_fee), tw.twd(cash)))
            elif after_cash < buffer_twd - EPS:
                later.append("現金：買完只剩 %s，低於你保留的 %s%% 現金（%s）"
                             % (tw.twd(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), tw.twd(buffer_twd)))
            pos_after = held_value + value
            cap = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > cap + EPS:
                later.append("部位：%s 會是 %s，占帳戶 %s 的 %.1f%%，超過你的 %s%% 上限"
                             % (code, tw.twd(pos_after), tw.twd(total), 100.0 * pos_after / total,
                                _pct(c["max_position_pct"])))
        if side == "sell" and code and kind == "entry" and qty is not None:
            if not c.get("allow_short"):
                reasons.append("放空：要賣的 %s 比持有的多（持有 %s 股）；不做放空（先賣後買要開啟 allow_short）。"
                               "如果是剛買進的，先記錄成交（ticket.py fill）" % (code, _num(held)))

        # 停損, 虧損, 報酬風險比, 單日虧損, 交易次數, 時段
        max_loss = costs = risk_ps = risk_ticks = rr = rr_net = stop = target = None
        if kind == "entry" and side in ("buy", "sell") and qty:
            long = side == "buy"
            dt = trade == "day_trade"
            stop = _f(o.get("stop")) or _f(plan.get("stop"))
            target = _f(o.get("target")) or _f(plan.get("target"))
            if o.get("stop") is None and stop:
                warns.append("停損 %s 只在計畫裡、不在委託單上：在券商設停損條件單，或用 trader-alerts 提醒"
                             % _p(stop, code))
            if long and ref and limit_up and ref >= limit_up - EPS:
                later.append("漲跌停：進場價 %s 已經是漲停價 %s：鎖住時買不到，打開時可能急跌"
                             % (_p(ref, code), _p(limit_up, code)))
            # the risk above is from the plan's entry; a price already past the stop makes it a different
            # trade (2026-11-04 11:46: 7780 at 14.75 under its 14.90 stop passed with a 15.05 entry)
            if stop and live and ((long and live <= stop + EPS) or (not long and live >= stop - EPS)):
                later.append("停損：%s 最新價 %s 已經%s停損 %s：計畫失效，進場就在停損外面；"
                             "要做就用 trader-exit 依現在的價格重新規劃"
                             % (code, _p(live, code), "跌到" if long else "漲到", _p(stop, code)))
            if not stop:
                if c.get("require_stop", True):
                    later.append("停損：沒有停損。每筆進場都要有（\"require_stop\": true），由 trader-exit 算、"
                                 "trader-budget 寫進委託單")
                else:
                    warns.append("沒有停損：虧損沒有上限，所以沒有算進單日虧損")
            elif ref:
                if (long and stop >= ref - EPS) or (not long and stop <= ref + EPS):
                    later.append("停損：停損 %s 要%s進場價 %s" % (_p(stop, code), "低於" if long else "高於",
                                                              _p(ref, code)))
                else:
                    if long and limit_down and stop < limit_down - EPS:
                        later.append("停損：停損 %s 低於今天的跌停價 %s，今天碰不到" % (_p(stop, code),
                                                                                 _p(limit_down, code)))
                    if not long and limit_up and stop > limit_up + EPS:
                        later.append("停損：停損 %s 高於今天的漲停價 %s，今天碰不到" % (_p(stop, code),
                                                                                 _p(limit_up, code)))
                    risk_ps = float(abs(tw.dec(ref) - tw.dec(stop)))
                    risk_ticks = abs(tw.tick_steps(stop, ref, code))
                    if long:
                        loss = tw.loss_at_stop(ref, stop, qty, code, dt, fees, day)
                        max_loss, costs, move = loss["total"], loss["costs"], loss["move"]
                    else:
                        r = tw.net_result(stop, ref, qty, code, dt, fees, day)
                        max_loss, costs, move = -r["net"], r["costs"], -r["move"]
                    cap_loss = _f(c.get("max_loss_per_trade_twd"))
                    if cap_loss and max_loss > cap_loss + EPS:
                        later.append("虧損：%s × 每股風險 %s = %s，加手續費和證交稅 %s，停損出場約虧 %s，"
                                     "超過你的單筆最大虧損 %s" % (tw.fmt_shares(qty), _step(risk_ps, ref, code), tw.twd(move),
                                                                 tw.twd(costs), tw.twd(max_loss), tw.twd(cap_loss)))
                    min_rr = _f(c.get("min_reward_risk"))
                    min_net = _f(c.get("min_reward_risk_net"))
                    if target:
                        if (long and target <= ref + EPS) or (not long and target >= ref - EPS):
                            later.append("停利：停利 %s 要%s進場價 %s" % (_p(target, code), "高於" if long else "低於",
                                                                      _p(ref, code)))
                        else:
                            rr = round(float(abs(tw.dec(target) - tw.dec(ref)) / tw.dec(risk_ps)), 2)
                            win = (tw.net_result(ref, target, qty, code, dt, fees, day) if long
                                   else tw.net_result(target, ref, qty, code, dt, fees, day))
                            rr_net = round(win["net"] / max_loss, 2) if max_loss > 0 else None
                            if min_rr and rr < min_rr - EPS:
                                later.append("報酬風險比：%.2f（停利 %s、進場 %s、停損 %s）低於你的最低 %s"
                                             % (rr, _p(target, code), _p(ref, code), _p(stop, code), _num(min_rr)))
                            if min_net and rr_net is not None and rr_net < min_net - EPS:
                                later.append("報酬風險比：扣掉手續費和證交稅後 %.2f，低於你的最低 %s"
                                             % (rr_net, _num(min_net)))
                    elif min_rr:
                        later.append("報酬風險比：沒有停利可以算（min_reward_risk %s）" % _num(min_rr))
            daily = _f(c.get("max_daily_loss_twd"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + EPS:
                    later.append("單日虧損：今天已經押上 %s，加這筆 %s = %s，超過你的單日虧損上限 %s"
                                 % (tw.twd(used), tw.twd(max_loss), tw.twd(used + max_loss), tw.twd(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("交易次數：今天已進場 %d 筆，這批前面又通過 %d 筆，上限一天 %d 筆"
                             % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("進場 %s（%s）、停損 %s、停利 %s：每股風險 %s（%d 檔）"
                            % (_p(ref, code), REF_WORDS.get(ref_from, ref_from), _p(stop, code),
                               _p(target, code) if target else "沒有", _step(risk_ps, ref, code), risk_ticks))
                cap_loss = _f(c.get("max_loss_per_trade_twd"))
                info.append("停損出場約虧 %s（價差 %s + 手續費和證交稅 %s%s）"
                            % (tw.twd(max_loss), tw.twd(move), tw.twd(costs),
                               ("；上限 %s" % tw.twd(cap_loss)) if cap_loss else ""))
                min_rr = _f(c.get("min_reward_risk"))
                info.append("報酬風險比 %s（扣成本 %s%s）"
                            % (("%.2f" % rr) if rr is not None else "無法計算",
                               ("%.2f" % rr_net) if rr_net is not None else "無法計算",
                               ("；最低 %s" % _num(min_rr)) if min_rr else ""))
            if daily or cap_t:
                info.append("今天在這之前：%d/%s 筆，單日虧損 %s/%s"
                            % (len(entries_today) + batch_entries, cap_t or "不限",
                               tw.twd(loss_today + batch_loss), tw.twd(daily) if daily else "不限"))

        # 重複, 每日筆數
        key = (code, side)
        if key in seen:
            later.append("重複：這批裡第二張%s %s" % (SIDE_WORDS.get(side, side), code))
        if key in placed_keys:
            later.append("重複：今天已經送出過%s %s" % (SIDE_WORDS.get(side, side), code))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("每日筆數：今天已送出 %d 張，這批前面又通過 %d 張，上限一天 %d 張"
                         % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("出場不擋：" + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
                if trade == "day_trade" and side == "buy" and value is not None:
                    batch_dt += value
            if qty is not None:
                if side == "buy":
                    if value is not None:
                        acct["cash"] -= value + buy_fee
                        acct["values"][code] = acct["values"].get(code, 0.0) + value
                    acct["shares"][code] = held + qty
                    b = acct["today"].setdefault(code, {"board": 0, "odd": 0})
                    b["odd" if lot == "odd" else "board"] += qty
                elif side == "sell":
                    if value is not None:
                        out_fee = tw.broker_fee(value, fees["discount"],
                                                fees["minimum"] if lot == "board" else fees["odd_minimum"])
                        acct["cash"] += value - out_fee - tw.sell_tax(value, code, trade == "day_trade", day)
                        acct["values"][code] = max(acct["values"].get(code, 0.0) - value, 0.0)
                    acct["shares"][code] = held - qty
        results.append({
            "index": i, "symbol": code, "name": name, "side": side, "kind": kind, "lot": lot, "trade": trade,
            "amount": tw.fmt_shares(qty) if qty else str(o.get("qty")), "qty": qty,
            "value_twd": value, "entry_ref": ref, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "risk_ticks": risk_ticks,
            "max_loss": max_loss, "costs_at_stop": costs, "reward_risk": rr, "reward_risk_net": rr_net,
            "fingerprint": tw.fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_taipei": now_tp.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2), "day_trade_value": round(dt_today, 2)},
        "lists": {"disposition_checked": disp is not None, "daytrade_checked": daytrade is not None,
                  "daytrade_date": daytrade["date"].isoformat() if daytrade and daytrade.get("date") else None},
    }


def headline(r, o=None):
    """'買進 聯電 2303 1 張（整股，市價 IOC，現股當沖），進場'"""
    o = o if isinstance(o, dict) else {}
    how = [LOT_WORDS.get(r.get("lot"), "")]
    pt = {"market": "市價", "limit": "限價 %s" % o.get("price")}.get(str(o.get("price_type")), "")
    how.append(("%s %s" % (pt, o.get("tif") or "")).strip())
    how.append(TRADE_WORDS.get(r.get("trade"), ""))
    who = ("%s %s" % (r["name"], r["symbol"])) if r.get("name") else r["symbol"]
    return "%s %s %s（%s），%s" % (SIDE_WORDS.get(r["side"], r["side"]), who, r["amount"],
                                 "，".join(x for x in how if x), "進場" if r["kind"] == "entry" else "出場")


def report(res, orders=()):
    lines = []
    for w in res["warnings"]:
        lines.append("注意：" + w)
    if res["warnings"]:
        lines.append("")
    orders = list(orders)
    for n, r in enumerate(res["results"]):
        lines.append("%-5s #%d %s" % (r["verdict"], r["index"], headline(r, orders[n] if n < len(orders) else None)))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("通過 %d 張、擋下 %d 張（台北時間 %s）。通過的單都成交後現金剩 %s（%s）。"
                 % (res["pass"], res["block"], res["now_taipei"], tw.twd(res["cash_after"]),
                    "模擬帳戶" if res["paper"] else "真錢帳戶"))
    if res["block"]:
        lines.append("被擋下的單不會交給 trader-broker。改單或改規則，再檢查一次。")
    else:
        lines.append("每一張都通過了。trader-broker 還是會一張一張問你 yes，最後由你自己在券商 App 送出。")
    lines.append("這是教學與工具，不構成投資建議。")
    return "\n".join(lines)


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="用你的規則檢查每一張委託單（trader-risk）。")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="你的帳戶；沒有的話，當成用 budget 開的新帳戶")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="trader-broker 記錄的今天送出的單")
    ap.add_argument("--lists", action="append", metavar="PATH",
                    help="存下來的處置股和現股當沖標的清單（資料夾或 JSON 檔，可重複），none 表示不檢查")
    ap.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 09:20\"")
    ap.add_argument("--today", help="YYYY-MM-DD（預設：台北的今天）")
    ap.add_argument("--out", default="trader/state/risk.json", help="檢查結果寫到哪裡")
    ap.add_argument("--no-write", action="store_true", help="不記錄檢查結果")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        try:
            now = tw.parse_now(a.now)
        except ValueError as e:
            raise RiskError(str(e))
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today 要像 2026-11-02 這樣的日期")
        doc = read_json(a.orders, "委託單檔（先跑 trader-budget）")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("%s 裡沒有委託單" % a.orders)
        cfg = read_json(a.config, "設定檔", required=False) or {}
        portfolio = read_json(a.portfolio, "帳戶", required=False)
        pdoc = read_json(a.prices, "價格", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "下單紀錄", required=False)
        today = a.today or tw.to_taipei(now).date().isoformat()
        cal = tw.load_calendar(os.path.dirname(a.config) or "trader", cfg)
        lists, notes = resolve_lists(a.lists, a.state, today)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, lists, cal)
        res["warnings"] = notes + res["warnings"]
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            rec = dict(res)
            rec["checked_at"] = tw.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            tw.write_json(tmp, rec)
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py：%s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res, orders))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
