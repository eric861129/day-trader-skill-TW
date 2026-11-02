#!/usr/bin/env python3
"""Position sizing for trader-budget (Taiwan). Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, in whole board lots
(1 張 = 1,000 shares, 現股當沖), and writes one ticket to trader/orders.json.
It never places anything: trader-risk checks the ticket, trader-broker asks
you yes or no, and you place it yourself in your broker's app.

    python3 size.py                                   # trader/plan.json + trader/config.json
    python3 size.py --write                           # write trader/orders.json
    python3 size.py --limit                           # limit ROD at the plan's entry
    python3 size.py --limit 109.5 --write             # limit ROD at 109.5
    python3 size.py --odd-lot                         # an odd-lot buy (held overnight, not a day trade)
    python3 size.py --now "2026-11-02 09:20" --json   # pin the clock (Taipei time)

How many 張: the smallest of

    max loss       max_loss_per_trade_twd / the loss at the stop, fees and tax included
                   (twmarket.loss_at_stop: the same number trader-risk checks)
    position cap   budget x max_position_pct / the value of one 張
    max order      max_order_twd / the value of one 張
    cash           (budget minus the cash buffer, and no more than the cash in
                   trader/portfolio.json) / (one 張 plus its buy fee). A day trade
                   that cannot be sold the same day is due in full at T+2.
    day-trade quota  day_trade_quota_twd / the value of one 張, when it is set

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero 張 is a plain refusal: no order is written.

The default ticket is a market IOC order: what does not fill at once is
canceled (a market ROD buy that does not fill rests at the limit-up price).
Odd lots (--odd-lot) are limit ROD only and cannot be day-traded: bought
today, they can be sold the next trading day at the earliest.

Weights mode: split a budget across a list of codes by weight, in whole
shares, each position as a board-lot ticket plus an odd-lot ticket.

    python3 size.py --weights 0050=40,2330=30,2303=15,6488=15 --prices trader/prices.json
    python3 size.py --targets targets.example.json --prices trader/prices.json --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the codes you keep (blocked codes
                   are dropped first; --top N keeps the N biggest)
  3. cap           no code gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by weight
  4. min order     a position under min_order_twd is dropped and its weight shared out
  5. shares        whole shares that fit each slice, buy fees included; then one
                   share at a time to the code furthest below its slice

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

# The contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_twd": 250000,
    "min_order_twd": 1000,
    "max_loss_per_trade_twd": 3000,
    "max_daily_loss_twd": 9000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "min_reward_risk_net": None,
    "require_stop": True,
    "no_entries_before": tw.NO_ENTRIES_BEFORE,
    "no_entries_after": tw.NO_ENTRIES_AFTER,
    "flat_by": tw.FLAT_BY,
    "day_trade_enabled": False,
    "day_trade_quota_twd": None,
    "allow_overnight": False,
    "allow_disposition": False,
    "fee_discount": 1,
    "fee_min_twd": 20,
    "fee_min_odd_twd": 1,
    "extra_closed": {},
    "broker_name": "",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
ODD_MAX = tw.LOT - 1
SETUP_WORDS = {"orb": "開盤區間突破", "level": "突破關鍵價", "flag": "旗形整理", "bounce": "超賣反彈"}
MARKET_WORDS = {"TWSE": "上市", "TPEx": "上櫃"}
LIMIT_WORDS = {"max_loss": "單筆最大虧損", "position_cap": "部位上限", "max_order": "單筆金額上限",
               "cash": "可用現金", "quota": "當沖額度", "odd_max": "零股上限"}
NOTE = ("只是提案。trader-risk 通過這一張、你在對話中針對這一張明確回覆 yes、你自己在券商 App 送出，"
        "三件事都成立才會下單。")
DISCLAIMER = "這是教學與工具，不構成投資建議。"


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(x):
    """1.0 -> "1", 0.5 -> "0.5", 75 -> "75"."""
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


def _p(price, code=""):
    return tw.fmt_price(price, code)


def _diff(x, ref, code=""):
    """A price difference with the decimals of the tick at ref: 1.5 at 110.0 -> "1.5"."""
    step = tw.tick_size(ref, code)
    places = max(0, -step.normalize().as_tuple().exponent)
    return "{:,.{n}f}".format(tw.dec(x), n=places)


def label(code, name):
    return "%s %s" % (name, code) if name else code


def code_of(text):
    """'2303', '2303.TW' -> '2303'; anything else -> SizeError."""
    try:
        return tw.parse_symbol(str(text))
    except ValueError as e:
        raise SizeError(str(e))


def utc_iso(now_utc=None):
    now_utc = now_utc or tw.utc_now()
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    """"2026-11-02T01:30:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def parse_now(text):
    try:
        return tw.parse_now(text)
    except ValueError as e:
        raise SizeError(str(e))


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("%s 找不到%s" % (path, what))
    except json.JSONDecodeError as e:
        raise SizeError("%s 不是有效的 JSON：%s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "設定檔"))
    return cfg


def load_prices(path):
    """prices.json -> ({code: price}, {code: {"name", "market"}}). Keys like "2303.TW" are accepted."""
    if not path:
        return {}, {}
    data = read_json(path, "價格檔")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    detail = (data.get("detail") or {}) if isinstance(data, dict) else {}
    out, info = {}, {}
    for k, p in prices.items():
        v = _f(p)
        if v is None or v <= 0:
            continue
        try:
            code, market = tw.parse_symbol(str(k))
        except ValueError:
            continue
        out[code] = v
        d = detail.get(k) or detail.get(code) or {}
        info[code] = {"name": d.get("name", ""), "market": d.get("market") or market}
    return out, info


def price_time(doc, code):
    """When this code's price is from: the quote's own time (detail.time), else when it was
    fetched (detail.fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(code) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, code, now_utc, minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for code from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(code))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, code)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- plan mode (the default)

def _largest(n_max, fits):
    """The largest n in 0..n_max with fits(n), for a fits() that holds up to some n and fails after
    (the loss at the stop and the cash a buy needs both grow with n). Binary search."""
    lo, hi = 0, max(int(n_max), 0)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, odd_lot=False):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit    a limit price (None = market; an odd-lot buy defaults to the plan's entry)
    price    a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash     the cash in trader/portfolio.json, if there is one
    today    Taipei date (YYYY-MM-DD); a plan for another day is refused
    odd_lot  size an odd-lot buy (1 to 999 shares, held overnight) instead of whole 張"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("計畫不是 JSON 物件")
    if not plan.get("symbol"):
        raise SizeError("計畫沒有代號（先跑 trader-exit）")
    code, market = code_of(plan["symbol"])
    market = plan.get("market") or market
    name = plan.get("name") or ""
    side = str(plan.get("side") or "buy").strip().lower()
    if side not in ("buy", "long"):
        raise SizeError("這套 skill 只算做多（買進）的當沖；計畫寫的是 %r。先賣後買要另外開啟，而且要使用者確認" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("這份計畫是 %s 的，今天（台北）是 %s：用 trader-exit 重新規劃" % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for key, word, v in (("entry", "進場價", entry_plan), ("stop", "停損", stop), ("target", "停利", target)):
        if v is None or v <= 0:
            raise SizeError("計畫需要大於零的%s（%s，先跑 trader-exit）" % (word, key))
    blocked = {str(b).strip().upper().split(".")[0] for b in (c.get("blocked") or [])}
    if code in blocked:
        raise SizeError("%s 在你的封鎖清單上" % label(code, name))

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("沒有預算：加 --budget，或在 trader/config.json 設定 \"budget\"")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct 要在 0 到 100 之間")

    up, down = _f(plan.get("limit_up")), _f(plan.get("limit_down"))
    if odd_lot and limit is None:
        limit = entry_plan
    if limit is not None:
        lim = tw.dec(limit)
        if not tw.on_tick(lim, code):
            raise SizeError("限價 %s 不在升降單位上（%s 這個價位一檔 %s）：最接近的是 %s 或 %s"
                            % (limit, code, tw.tick_size(lim, code), _p(tw.to_tick(lim, code, "down"), code),
                               _p(tw.to_tick(lim, code, "up"), code)))
        if (up and lim > tw.dec(up)) or (down and lim < tw.dec(down)):
            raise SizeError("限價 %s 超出今天的漲跌停 %s / %s：交易所會退單"
                            % (_p(lim, code), _p(up, code), _p(down, code)))
        ref, ref_from = lim, "limit"
    elif price is not None and tw.to_tick(price, code, "up") > tw.dec(entry_plan):
        ref, ref_from = tw.to_tick(price, code, "up"), "price"
    else:
        ref, ref_from = tw.dec(entry_plan), "plan"
    stop_d, target_d = tw.dec(stop), tw.dec(target)
    if stop_d >= ref:
        raise SizeError("停損 %s 不在進場價 %s 下方：做多的停損要在下面" % (_p(stop_d, code), _p(ref, code)))
    if target_d <= ref:
        raise SizeError("停利 %s 不在進場價 %s 上方：行情已經走完了。跳過這筆，或用 trader-exit 重新規劃"
                        % (_p(target_d, code), _p(ref, code)))
    if up and ref >= tw.dec(up):
        raise SizeError("進場價 %s 已經到漲停 %s：漲停鎖住時買進，隔天可能開低，當天也可能賣不掉。不下單"
                        % (_p(ref, code), _p(up, code)))
    if down and stop_d < tw.dec(down):
        raise SizeError("停損 %s 在今天的跌停價 %s 下方：價格今天到不了，這個停損等於沒設"
                        % (_p(stop_d, code), _p(down, code)))

    day_trade = not odd_lot
    unit = 1 if odd_lot else tw.LOT
    on = datetime.date.fromisoformat(str(plan.get("date") or today)) if (plan.get("date") or today) else None
    fees = tw.fee_settings(c)
    risk = ref - stop_d
    reward = target_d - ref
    risk_ticks = tw.tick_steps(stop_d, ref, code)

    def loss(n):
        return tw.loss_at_stop(ref, stop_d, n * unit, code, day_trade, fees, on)

    def buy_fee(n):
        return tw.round_trip(ref, ref, n * unit, code, day_trade, fees, on)["buy_fee"] if n else 0

    per_unit = ref * unit
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "預算 %s 扣保留現金 %s" % (tw.twd(budget), tw.twd(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "trader/portfolio.json 的現金 %s 扣保留現金 %s" % (tw.twd(cash), tw.twd(cash_buffer))
    available = max(round(available, 2), 0.0)
    unit_word = "股" if odd_lot else "張"
    each = ("每股 %s" % _p(ref, code)) if odd_lot else ("每張 %s" % tw.twd(per_unit))

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_twd"))
    no_cost = None
    if max_loss:
        no_cost = int(tw.dec(max_loss) // (risk * unit))
        n = _largest(no_cost, lambda k: loss(k)["total"] <= max_loss + EPS)
        shown = loss(n or 1)
        limits.append({"id": "max_loss", "twd": max_loss, "units": n,
                       "how": "%s：%s停損出場約虧 %s（價差 %s + 成本 %s）"
                              % (tw.twd(max_loss), "%d %s" % (n or 1, "股" if odd_lot else "張"),
                                 tw.twd(shown["total"]), tw.twd(shown["move"]), tw.twd(shown["costs"]))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"id": "position_cap", "twd": cap, "units": int(tw.dec(cap) // per_unit),
                       "how": "預算 %s 的 %s%% = %s / %s" % (tw.twd(budget), _num(cap_pct), tw.twd(cap), each)})
    max_order = _f(c.get("max_order_twd"))
    if max_order:
        limits.append({"id": "max_order", "twd": max_order, "units": int(tw.dec(max_order) // per_unit),
                       "how": "%s / %s" % (tw.twd(max_order), each)})
    n_cash = _largest(int(tw.dec(available) // per_unit),
                      lambda k: float(per_unit * k) + buy_fee(k) <= available + EPS)
    limits.append({"id": "cash", "twd": available, "units": n_cash,
                   "how": "%s = %s / %s" % (cash_note, tw.twd(available),
                                            ("每股 %s 加手續費" % _p(ref, code)) if odd_lot
                                            else ("每張 %s（含手續費）" % tw.twd(float(per_unit) + buy_fee(1))))})
    quota = _f(c.get("day_trade_quota_twd"))
    if quota and day_trade:
        limits.append({"id": "quota", "twd": quota, "units": int(tw.dec(quota) // per_unit),
                       "how": "%s / 每張 %s" % (tw.twd(quota), tw.twd(per_unit))})
    if odd_lot:
        limits.append({"id": "odd_max", "twd": None, "units": ODD_MAX, "how": "盤中零股一筆最多 999 股"})

    units = max(0, min(l["units"] for l in limits))
    bound = next(l for l in limits if l["units"] == units)
    for l in limits:
        l["name"] = LIMIT_WORDS[l["id"]]
        l["shares"] = l["units"] * unit
        l["binds"] = l is bound
    qty = units * unit
    value = float(per_unit * units)
    fee_in = buy_fee(units)
    at_stop = loss(units) if units else {"move": 0.0, "costs": 0, "total": 0.0}
    probe = units or 1
    net_reward = tw.net_result(ref, target_d, probe * unit, code, day_trade, fees, on)["net"]
    rr = round(float(reward / risk), 2)
    rr_net = round(net_reward / loss(probe)["total"], 2)
    refusal = None
    if units == 0:
        if bound["id"] == "max_loss":
            one = loss(1)
            refusal = ("%s停損出場約虧 %s（價差 %s + 手續費和證交稅 %s），超過你的單筆最大虧損 %s。不下單。"
                       "跳過這筆，或用 trader-exit 規劃更近的停損。"
                       % ("一股" if odd_lot else "一張", tw.twd(one["total"]), tw.twd(one["move"]),
                          tw.twd(one["costs"]), tw.twd(max_loss)))
        else:
            refusal = ("%s %s 要 %s（含手續費），超過你的%s（%s）。不下單。"
                       % ("一股" if odd_lot else "一張", label(code, name), tw.twd(float(per_unit) + buy_fee(1)),
                          bound["name"], bound["how"]))
        if not odd_lot:
            refusal += ("零股不能當沖；要用零股就是留倉，可以加 --odd-lot 看看（trader-risk 要 "
                        "\"allow_overnight\": true 才會放行）。")
    elif _f(c.get("min_reward_risk")) and rr < _f(c["min_reward_risk"]) - EPS:
        refusal = ("報酬風險比 %.2f（停利 %s、進場 %s、停損 %s）低於你的最低要求 %s，trader-risk 會擋下。不下單。"
                   "跳過這筆，或用 trader-exit 重新規劃。"
                   % (rr, _p(target_d, code), _p(ref, code), _p(stop_d, code), _num(c["min_reward_risk"])))
    elif _f(c.get("min_reward_risk_net")) and rr_net < _f(c["min_reward_risk_net"]) - EPS:
        refusal = ("扣成本後的報酬風險比 %.2f 低於你的最低要求 %s（手續費和證交稅讓停利少賺、停損多虧），"
                   "trader-risk 會擋下。不下單。" % (rr_net, _num(c["min_reward_risk_net"])))
    elif value < float(c.get("min_order_twd") or 0) - 0.005:
        refusal = ("%s %s 只有 %s，低於你的最低單筆金額 %s。不下單。"
                   % (tw.fmt_shares(qty), label(code, name), tw.twd(value), tw.twd(c["min_order_twd"])))
    return {
        "symbol": code, "market": market, "name": name, "side": "buy", "setup": plan.get("setup", ""),
        "date": plan.get("date"), "entry_time": plan.get("entry_time", ""),
        "entry_plan": float(tw.dec(entry_plan)), "entry": float(ref),
        "entry_from": "plan_limit" if ref_from == "limit" and ref == tw.dec(entry_plan) else ref_from,
        "stop": float(stop_d), "target": float(target_d), "risk_per_share": float(risk), "risk_ticks": risk_ticks,
        "reward_risk": rr, "reward_risk_net": rr_net,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "prev_close": _f(plan.get("prev_close")), "limit_up": up, "limit_down": down,
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_twd": max_loss, "limits": limits, "lot": "odd" if odd_lot else "board",
        "unit": unit_word, "units": units, "qty": qty, "bound_by": bound["id"],
        "no_cost_units": no_cost,
        "value": value, "buy_fee": fee_in, "cash_needed": value + fee_in,
        "max_loss": at_stop["total"], "move_at_stop": at_stop["move"], "costs_at_stop": at_stop["costs"],
        "flat_by": None if odd_lot else (plan.get("flat_by") or c.get("flat_by")),
        "price_type": "limit" if ref_from == "limit" else "market",
        "tif": "ROD" if ref_from == "limit" else "IOC",
        "trade": "cash" if odd_lot else "day_trade",
        "day_trade_enabled": bool(c.get("day_trade_enabled")), "refusal": refusal,
    }


def _bound_phrase(s, cfg):
    c = dict(DEFAULTS)
    c.update(cfg or {})
    b = s["bound_by"]
    if b == "max_loss":
        return "單筆最大虧損 %s " % tw.twd(s["max_loss_per_trade_twd"] or 0)
    if b == "position_cap":
        return "部位上限（預算的 %s%%）" % _num(c.get("max_position_pct") or 0)
    if b == "max_order":
        return "單筆金額上限 %s " % tw.twd(c.get("max_order_twd") or 0)
    if b == "quota":
        return "當沖額度 %s " % tw.twd(c.get("day_trade_quota_twd") or 0)
    if b == "odd_max":
        return "盤中零股一筆最多 999 股"
    return "扣掉保留現金後的可用現金"


def ticket(s, cfg=None):
    """The one proposed ticket (the contract's keys), or None when sizing refused."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    code = s["symbol"]
    o = {"symbol": code}
    if s["market"]:
        o["market"] = s["market"]
    if s["name"]:
        o["name"] = s["name"]
    o.update({"side": "buy", "lot": s["lot"], "qty": int(s["qty"]), "price_type": s["price_type"]})
    if s["price_type"] == "limit":
        o["price"] = tw.price_text(s["entry"], code)
    o.update({"tif": s["tif"], "trade": s["trade"], "stop": tw.price_text(s["stop"], code),
              "target": tw.price_text(s["target"], code)})
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "當沖")
    what = tw.fmt_shares(s["qty"]) if s["lot"] == "board" else "盤中零股 %d 股" % s["qty"]
    why = ("%s，%s：%s，由%s決定（停損出場約虧 %s，其中手續費和證交稅 %s）"
           % (setup, label(code, s["name"]), what, _bound_phrase(s, cfg), tw.twd(s["max_loss"]),
              tw.twd(s["costs_at_stop"])))
    if s["lot"] == "odd":
        why = why[:-1] + "；零股不能當沖，最快下一個交易日才能賣）"
    o["why"] = why
    plan = {"date": s["date"], "setup": s["setup"], "entry": s["entry"], "entry_time": s["entry_time"],
            "stop": s["stop"], "target": s["target"], "risk_per_share": s["risk_per_share"],
            "risk_ticks": s["risk_ticks"], "max_loss": s["max_loss"], "cost_at_stop": s["costs_at_stop"]}
    if s["flat_by"]:
        plan["flat_by"] = s["flat_by"]
    else:
        plan["hold"] = "overnight"
    plan.update({"prev_close": s["prev_close"], "limit_up": s["limit_up"], "limit_down": s["limit_down"]})
    o["plan"] = plan
    return o


def ticket_words(o):
    """買進 聯電 2303（上市） 整股 1 張，市價，IOC，現股當沖"""
    code = o["symbol"]
    where = ("（%s）" % MARKET_WORDS.get(o.get("market"), o.get("market"))) if o.get("market") else ""
    lot = ("整股 %s" % tw.fmt_shares(o["qty"])) if o["lot"] == "board" else ("盤中零股 %d 股" % o["qty"])
    how = ("限價 %s" % o["price"]) if o["price_type"] == "limit" else "市價"
    trade = "現股當沖" if o["trade"] == "day_trade" else "現股（留倉）"
    return "%s %s%s %s，%s，%s，%s" % ("買進" if o["side"] == "buy" else "賣出", label(code, o.get("name", "")),
                                    where, lot, how, o["tif"], trade)


def plan_report(s, order, notes=(), cfg=None):
    code = s["symbol"]
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "計畫")
    lines.append("%s 做多，%s，進場 %s%s" % (label(code, s["name"]), setup, _p(s["entry_plan"], code),
                                         ("（%s）" % s["entry_time"]) if s["entry_time"] else ""))
    lines.append("停損 %s%s。停利 %s%s。" % (_p(s["stop"], code), ("：" + s["stop_label"]) if s["stop_label"] else "",
                                         _p(s["target"], code),
                                         ("：" + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "計畫的進場價", "price": "最新價，高於計畫的進場價", "limit": "你的限價",
           "plan_limit": "限價，用計畫的進場價"}[s["entry_from"]]
    lines.append("以 %s 計算（%s）。每股風險 %s（%d 檔）。報酬風險比 %.2f，扣成本後 %.2f。"
                 % (_p(s["entry"], code), src, _diff(s["risk_per_share"], s["entry"], code), s["risk_ticks"],
                    s["reward_risk"],
                    s["reward_risk_net"]))
    lines.append("")
    lines.append("上限（%s，取最小的那個）：" % ("以股計算" if s["lot"] == "odd" else "整張計算"))
    for l in s["limits"]:
        pad = "　" * (6 - len(l["name"]))      # full-width spaces line up the Chinese names
        mark = ("  <- 決定%s數" % s["unit"]) if l["binds"] else ""
        lines.append("  %s%s %s  %d %s%s" % (l["name"], pad, l["how"], l["units"], s["unit"], mark))
    if s["no_cost_units"] is not None and s["no_cost_units"] != next(
            l["units"] for l in s["limits"] if l["id"] == "max_loss"):
        lines.append("不算成本的話，單筆最大虧損可以買 %d %s；算進手續費和證交稅只剩 %d %s。"
                     % (s["no_cost_units"], s["unit"],
                        next(l["units"] for l in s["limits"] if l["id"] == "max_loss"), s["unit"]))
    lines.append("")
    if order is None:
        lines.append("不下單：" + s["refusal"])
        lines.append(DISCLAIMER)
        return "\n".join(lines)
    lines.append("%s：%s %s = %s，價格 %s，買進手續費 %s。停損出場最多虧 %s：價差 %s，加手續費和證交稅 %s。"
                 % ("張數" if s["lot"] == "board" else "股數", tw.fmt_shares(s["qty"]), label(code, s["name"]),
                    tw.twd(s["value"]), _p(s["entry"], code), tw.twd(s["buy_fee"]), tw.twd(s["max_loss"]),
                    tw.twd(s["move_at_stop"]), tw.twd(s["costs_at_stop"])))
    lines.append("停損是委託，不是保證：急殺、跳空或跌停鎖死時，成交價可能比 %s 差，甚至賣不掉。" % _p(s["stop"], code))
    if s["lot"] == "odd":
        lines.append("零股不是當沖：今天買的零股最快下一個交易日才能賣。停損今天用不上，隔天開盤可能直接跳空跌破 %s，"
                     "虧損可能比上面的數字大。" % _p(s["stop"], code))
        lines.append("成本以一般證交稅（股票 0.3%%、ETF 0.1%%）和零股最低手續費 %s 計算。盤中零股只能限價 ROD，"
                     "09:10 第一次撮合，之後每 5 秒一次。" % tw.twd(tw.fee_settings(cfg)["odd_minimum"]))
        lines.append("trader-risk 會擋下這張單，除非設定檔的 \"allow_overnight\" 是 true。")
    elif not s["day_trade_enabled"]:
        lines.append("設定檔的 \"day_trade_enabled\" 是 false：trader-risk 會擋下這張當沖單，直到你確認已經和券商"
                     "簽好現股當沖契約，再把它改成 true。")
    lines.append("")
    lines.append("委託單（只是提案，什麼都沒有下單）：")
    lines.append("  " + ticket_words(order))
    lines.append("  停損 %s、停利 %s：在券商 App 設條件單（觸價單），或用 trader-alerts 提醒。"
                 % (order["stop"], order["target"]))
    if order["price_type"] == "market" and order["tif"] == "IOC":
        lines.append("  市價 IOC：沒成交的部分直接取消（市價 ROD 沒成交的部分會掛在漲停價等到收盤）。")
    if s["flat_by"]:
        lines.append("%s 前出清：還沒出場的部位這時一律平倉（trader-alerts 會提醒，trader-broker flatten 產生賣單）。"
                     % s["flat_by"])
    lines.append("")
    lines.append("下一步：trader-risk 檢查這張單，再由 trader-broker 問你 yes 或 no，最後由你自己在券商 App 送出。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode

def parse_weights(data):
    """A weights file, a {code: weight} dict or a list of rows -> [{"symbol", "name", "weight"}]."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"symbol": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] 不是物件" % i)
            t = r.get("symbol") or r.get("code") or r.get("ticker")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] 需要代號和權重" % i)
            rows.append({"symbol": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("權重要是清單或物件")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("%s 的權重不是數字：%r" % (r["symbol"], r["weight"]))
        code, _ = code_of(r["symbol"])
        out.append({"symbol": code, "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """0050=40,2330=30 -> {code: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights 要用 代號=權重，例如 0050=40,2330=30；收到 %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).strip().upper().split(".")[0] for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["symbol"] in seen:
            raise SizeError("%s 在權重裡出現兩次" % r["symbol"])
        seen.add(r["symbol"])
        if r["symbol"] in blocked:
            dropped.append({"symbol": r["symbol"], "reason": "在你的封鎖清單上"})
        elif r["weight"] <= 0:
            dropped.append({"symbol": r["symbol"], "reason": "權重是零或負數"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"symbol": r["symbol"], "reason": "不在權重最大的 %d 檔裡" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact NT$ per code with the cap enforced: proportional to weight; a code over the cap is
    pinned at the cap and the rest shared over the others, until nobody is over.
    Returns ({code: NT$}, set of capped codes)."""
    alloc, capped = {}, set()
    active = list(rows)
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["symbol"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["symbol"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["symbol"]] = cap
            capped.add(r["symbol"])
            money_left -= cap
        active = [r for r in active if r["symbol"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["symbol"]] = 0.0
            break
    return alloc, capped


def split_fees(shares, price, fees):
    """The buy fees for a position bought as a board-lot order plus an odd-lot order."""
    lots, odd = tw.split_lots(shares)
    p = tw.dec(price)
    return (tw.broker_fee(p * lots * tw.LOT, fees["discount"], fees["minimum"]) +
            tw.broker_fee(p * odd, fees["discount"], fees["odd_minimum"]))


def whole_shares(slices, prices, order, cap, cash, fees):
    """Whole shares: the most that fit each slice with its buy fees, then one more share at a time
    for the code furthest below its slice while the cash lasts and the cap holds."""
    def cost(t, n):
        return n * prices[t] + split_fees(n, prices[t], fees) if n else 0.0

    shares = {}
    for t in order:
        p = prices[t]
        n = int(math.floor(slices[t] / p + EPS)) if slices[t] > 0 else 0
        while n > 0 and cost(t, n) > slices[t] + EPS:
            n -= 1
        shares[t] = n
    left = cash - sum(cost(t, shares[t]) for t in order)
    while True:
        best, gap = None, None
        for t in order:
            n = shares[t]
            step = cost(t, n + 1) - cost(t, n)
            if step > left + EPS or (n + 1) * prices[t] > cap + EPS:
                continue
            g = slices[t] - cost(t, n)
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        left -= cost(best, shares[best] + 1) - cost(best, shares[best])
        shares[best] += 1
    return shares


def size(rows, cfg, budget=None, prices=None, top=None, info=None):
    """Weights mode. Pure function: weights + rules + prices -> the full sizing result (a dict)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("沒有預算：加 --budget，或在 trader/config.json 設定 \"budget\"")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    cap_pct = float(c.get("max_position_pct") or 100)
    min_order = float(c.get("min_order_twd") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct 要在 0 到 100 之間")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct 要大於 0、最多 100")
    prices = prices or {}
    info = info or {}
    fees = tw.fee_settings(c)

    keep, dropped = normalise(rows, c.get("blocked"), top)
    if not keep:
        raise SizeError("沒有東西可以分配：封鎖清單之後沒有剩下的權重")
    missing = [r["symbol"] for r in keep if not prices.get(r["symbol"])]
    if missing:
        raise SizeError("整股和零股都要有價格：%s 沒有價格（放進 trader/prices.json，或用 --prices）"
                        % "、".join(missing))
    # a limit price on the tick grid, never above the quote
    prices = {t: float(tw.to_tick(prices[t], t, "down")) for t in prices}

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["symbol"] for r in sorted(live, key=lambda r: -exact[r["symbol"]])]
        shares = whole_shares(exact, prices, order, cap, investable, fees)
        value = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if value[r["symbol"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["symbol"]))
        t = worst["symbol"]
        if shares[t] == 0:
            reason = "一股 %s，比分到的 %s 還貴，連一股都買不起" % (tw.twd(prices[t]), tw.twd(exact[t]))
        else:
            reason = "分到 %s，低於你的最低單筆金額 %s" % (tw.twd(value[t]), tw.twd(min_order))
        dropped.append({"symbol": t, "reason": reason})
        keep = [r for r in keep if r["symbol"] != t]
        if not keep:
            raise SizeError("每一檔都低於最低單筆金額：提高預算或少放幾檔")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["symbol"]
        lots, odd = tw.split_lots(shares[t])
        meta = info.get(t) or {}
        positions.append({"symbol": t, "name": r["name"] or meta.get("name", ""), "market": meta.get("market"),
                          "weight": round(r["weight"], 2), "source_weight": r["source_weight"],
                          "slice": round(exact[t], 2), "price": prices[t], "shares": shares[t], "lots": lots,
                          "odd": odd, "value": value[t], "fees": split_fees(shares[t], prices[t], fees),
                          "capped": t in capped})
    invested = round(sum(p["value"] for p in positions), 2)
    fee_total = sum(p["fees"] for p in positions)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap, "min_order_twd": min_order,
        "positions": positions, "invested": invested, "fees": fee_total,
        "idle": round(investable - invested - fee_total, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: per position a board-lot ticket (whole 張) and an odd-lot ticket for the rest,
    both limit ROD at the price, trade "cash" (a holding, not a day trade), no stop."""
    out = []
    for p in result["positions"]:
        base = {"symbol": p["symbol"]}
        if p.get("market"):
            base["market"] = p["market"]
        if p.get("name"):
            base["name"] = p["name"]
        why = "配置 %.2f%%" % p["weight"]
        if source:
            why = "%s，權重來自 %s" % (why, source)
        if p["capped"]:
            why += "；以預算 %s 的 %s%% 為上限" % (tw.twd(result["budget"]), _num(result["max_position_pct"]))
        for lot, qty in (("board", p["lots"] * tw.LOT), ("odd", p["odd"])):
            if qty <= 0:
                continue
            o = dict(base)
            o.update({"side": "buy", "lot": lot, "qty": qty, "price_type": "limit",
                      "price": tw.price_text(p["price"], p["symbol"]), "tif": "ROD", "trade": "cash",
                      "why": "%s，%s" % (why, "整股" if lot == "board" else "盤中零股")})
            out.append(o)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("權重來源：%s" % source)
    lines.append("預算 %s，保留現金 %s%% = %s，投入 %s，每檔上限 %s%% = %s"
                 % (tw.twd(r["budget"]), _num(r["cash_buffer_pct"]), tw.twd(r["cash_buffer"]),
                    tw.twd(r["investable"]), _num(r["max_position_pct"]), tw.twd(r["cap"])))
    lines.append("")
    lines.append("%-7s %-10s %7s %10s %12s %12s %8s  備註" % ("代號", "名稱", "權重", "價格", "股數", "金額", "手續費"))
    for p in r["positions"]:
        lines.append("%-7s %-10s %6.2f%% %10s %12s %12s %8s%s"
                     % (p["symbol"], (p["name"] or "")[:10], p["weight"], _p(p["price"], p["symbol"]),
                        tw.fmt_shares(p["shares"]) if p["shares"] else "0 股", tw.twd(p["value"]), tw.twd(p["fees"]),
                        ("  上限 %s%%" % _num(r["max_position_pct"])) if p["capped"] else ""))
    lines.append("%-18s %s" % ("投入", tw.twd(r["invested"])))
    lines.append("%-18s %s" % ("手續費", tw.twd(r["fees"])))
    lines.append("%-18s %s" % ("保留現金", tw.twd(r["cash_buffer"])))
    lines.append("%-18s %s" % ("閒置", tw.twd(r["idle"])))
    if r["idle"] >= 1:
        capped_all = r["positions"] and all(p["capped"] for p in r["positions"])
        lines.append("")
        lines.append("閒置 %s：%s" % (tw.twd(r["idle"]), "每一檔都到上限了：多放幾檔，或提高 max_position_pct"
                                     if capped_all else "整股和零股都要買整數股，分不完的零頭留在現金"))
    for d in r["dropped"]:
        lines.append("拿掉 %s：%s" % (d["symbol"], d["reason"]))
    lines.append("")
    lines.append("委託單（只是提案，什麼都沒有下單；整股和零股要分開下單）：")
    for o in orders:
        lines.append("  " + ticket_words(o))
    lines.append("")
    lines.append("這些是長期持有的買單，沒有停損，也不是當沖：\"require_stop\" 是 true 時 trader-risk 會擋下，"
                 "\"allow_overnight\" 是 false 時也會擋下。今天買的零股最快下一個交易日才能賣。")
    lines.append("下一步：trader-risk 逐張檢查，再由 trader-broker 一張一張問你 yes 或 no。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": NOTE,
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = tw.to_taipei(now).date().isoformat()
    plan = read_json(a.plan, "計畫（先跑 trader-exit，或用 --weights 分配預算）")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit 要一個價格，例如 --limit 109.5")
    code = ""
    if isinstance(plan, dict) and plan.get("symbol"):
        code = code_of(plan["symbol"])[0]
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and not a.odd_lot and code:
        pdoc = read_json(ppath, "價格檔")
        p, t, fresh = fresh_price(pdoc, code, now)
        if p is not None and fresh:
            price = p
            if tw.to_tick(p, code, "up") > tw.dec(_f(plan.get("entry")) or 0):
                notes.append("%s 最新價 %s（台北 %s）高於計畫的進場價 %s，風險變大：以 %s 計算。"
                             % (code, _p(tw.to_tick(p, code, "up"), code), tw.hm(tw.to_taipei(t)),
                                _p(plan.get("entry"), code), _p(tw.to_tick(p, code, "up"), code)))
        elif p is not None:
            notes.append("%s 裡的 %s 價格%s：超過 %d 分鐘，沒有採用。以計畫的進場價計算。"
                         % (ppath, code, ("是台北 " + tw.to_taipei(t).strftime("%Y-%m-%d %H:%M") + " 的")
                            if t else "沒有時間", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "帳戶檔")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, a.odd_lot)
    order = ticket(s, cfg)
    if order and a.write:
        src = "trader/plan.json（%s %s %s）" % (label(s["symbol"], s["name"]), s["setup"] or "plan",
                                              s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes},
                         ensure_ascii=False, indent=2))
    else:
        print(plan_report(s, order, notes, cfg))
        if order and a.write:
            print("已把 1 張委託單提案寫入 %s。" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "權重檔")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    prices, info = load_prices(ppath)
    universe = tw.load_universe(a.universe) if a.universe else {}
    for code, u in universe.items():
        meta = info.setdefault(code, {})
        meta["name"] = meta.get("name") or u.get("name", "")
        meta["market"] = meta.get("market") or u.get("market")
    result = size(rows, cfg, a.budget, prices, a.top, info)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, ensure_ascii=False, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("已把 %d 張委託單提案寫入 %s。" % (len(orders), a.write))
    return 0


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="依預算和單筆最大虧損算張數（trader-budget）。")
    ap.add_argument("--plan", default="trader/plan.json", help="交易計畫（預設 trader/plan.json）")
    ap.add_argument("--config", default="trader/config.json", help="規則（預設 trader/config.json）")
    ap.add_argument("--budget", type=float, help="蓋過設定檔的預算（新台幣）")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="限價 ROD，價格用計畫的進場價或 PRICE（預設市價 IOC）")
    ap.add_argument("--odd-lot", action="store_true",
                    help="改算盤中零股（1 到 999 股，限價 ROD，不是當沖，最快隔一個交易日才能賣）")
    ap.add_argument("--prices", help="價格 JSON（預設 trader/prices.json，如果有）")
    ap.add_argument("--portfolio", default="trader/portfolio.json", help="你的帳戶；有的話，現金會限制張數")
    ap.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 09:20\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="寫出委託單提案（預設 trader/orders.json）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    g = ap.add_argument_group("權重模式（把預算分給一串代號）")
    g.add_argument("--weights", help="直接給權重：0050=40,2330=30")
    g.add_argument("--targets", help="權重檔：{\"weights\": [{\"symbol\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="只留權重最大的 N 檔")
    g.add_argument("--universe", default="trader/universe.txt", help="名稱和市場（trader-scanner 的清單）")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            if a.odd_lot or a.limit is not None:
                raise SizeError("--odd-lot 和 --limit 用在單筆計畫；權重模式本來就是限價 ROD、整股加零股")
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top 要搭配 --weights 或 --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
