#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker (Taiwan). Standard library only.

With "broker": "manual" (the default) this tool never talks to a broker: the
user places every order by hand in their own broker app. With "broker":
"shioaji" it can also send an approved ticket to 永豐金 through the Shioaji API
(sinopac.py; the simulation environment while "paper" is true). The tool does
its work around the moments that matter:

  show      renders each proposed ticket in plain Chinese: exactly what to fill
            in a Taiwan broker app (上市/上櫃, code and name, 買進/賣出, 整股 or
            盤中零股, 現股當沖 or 現股, 市價 or 限價, ROD/IOC/FOK, 張 or 股), the
            stop and the target as 條件單 to set in the app, and the max loss at
            the stop with the fees and the tax. A ticket is READY only if
            trader-risk passed THIS exact ticket today (matched by fingerprint)
            and nobody has decided on it yet.
  approve   records the user's explicit yes for ONE ticket, word for word. It
            refuses 好, ok, 可以, "yes to all", a yes with a doubt or a condition
            in it, a yes that names another stock or a number that is not on
            the ticket, and any ticket that is not READY.
  decline   records a no.
  result    records what happened after the user's own app: placed (with the
            委託書號), skipped (not sent after all) or failed.
  fill      records a fill in trader/portfolio.json (cash, shares, fee and tax).
  flatten   builds the sell ticket that closes today's day trade by flat_by. It
            still goes through trader-risk and the user's yes.
  log       prints today's decisions.

With "broker": "shioaji" (pip install shioaji; keys in environment variables):
  send      sends ONE approved ticket through Shioaji: only when trader-risk
            passed it today, the user's yes is recorded and fresh (10 minutes),
            and it was never sent before. Paper = Shioaji's simulation; a live
            account also needs --live on this command. Claude Code asks the user
            before it runs (keep ticket.py send off the allow list).
  sync      reads today's orders and fills back from Shioaji into broker.json
            and portfolio.json (each fill once).
  cancel    asks Shioaji to cancel a sent order that is still open (needs a yes).
  positions prints the holdings Shioaji reports, next to portfolio.json.
  probe     simulation only: logs in, lists the accounts and looks up one code.
            Places nothing.

    python3 ticket.py show                    # every ticket, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "是"
    python3 ticket.py result --index 1 --status placed --order-no A1234
    python3 ticket.py fill --symbol 2303 --side buy --qty 1000 --price 110.0
    python3 ticket.py flatten --symbol 2303
    python3 ticket.py log
    python3 ticket.py send --index 1          # "broker": "shioaji" only
    python3 ticket.py sync

Everything is written to trader/state/broker.json (append only). Dates are
Taipei dates; --now "2026-11-02 09:20" pins the clock (Taipei time).
"""

import argparse
import datetime
import json
import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

MARKET_WORDS = {"TWSE": "上市", "TPEx": "上櫃"}
SIDE_WORDS = {"buy": "買進", "sell": "賣出"}
LOT_WORDS = {"board": "整股", "odd": "盤中零股"}
TRADE_WORDS = {"day_trade": "現股當沖", "cash": "現股（留倉）"}
TIF_WORDS = {"ROD": "當日有效，收盤前沒成交的部分一直掛著",
             "IOC": "立即成交，沒成交的部分取消",
             "FOK": "全部成交，否則全部取消"}
STATUS_WORDS = {"approved": "已同意", "declined": "不要", "placed": "已送出", "skipped": "沒送出",
                "failed": "失敗", "filled": "成交", "cancelled": "已刪單"}
SEND_WITHIN_MINUTES = 10        # a yes older than this is stale: ask again before sending
BROKERS = ("manual", "shioaji")

YES_EN = re.compile(r"^(yes|y)(?![a-z'])")
YES_ZH = ("是", "確認", "確定")
BATCH_EN = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
BATCH_ZH = ("全部", "都", "所有", "每一張", "每張", "整批", "一起")
DOUBT_EN = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later", "hold",
            "unless", "if"}
DOUBT_ZH = ("不", "等", "但", "如果", "可能", "再說", "取消", "晚點", "暫停", "考慮", "嗎", "吧", "假如", "要是")
CODE_TOKEN = re.compile(r"(?<![\d.])\d{4,6}[A-Z]?(?![\d.])")
TIME_TOKEN = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- clock and files

def taipei_iso(now_utc):
    """naive UTC -> "2026-11-02T09:20:00+08:00" (Taipei wall clock with its offset)."""
    return tw.to_taipei(now_utc).replace(microsecond=0).isoformat() + "+08:00"


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("找不到%s：%s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise TicketError("%s 不是有效的 JSON：%s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date", else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    v = _f(x)
    if v is None or v != int(v):
        return None
    return int(v)


def _amount(x):
    """NT$ for a JSON file: a whole number as int, else two decimals."""
    d = tw.dec(x)
    return int(d) if d == d.to_integral_value() else round(float(d), 2)


def _date(text):
    try:
        return datetime.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- the ticket

def fingerprint(order):
    """Same function as trader-risk: tw.fingerprint over the ticket fields."""
    return tw.fingerprint(order)


def _code(order):
    return str(order.get("symbol", "")).strip().upper()


def _who(order):
    """"聯電 2303（上市）"."""
    code, name = _code(order), str(order.get("name") or "").strip()
    who = "%s %s" % (name, code) if name else code
    market = MARKET_WORDS.get(order.get("market"))
    return "%s（%s）" % (who, market) if market else who


def _qty_words(order):
    q = _int(order.get("qty"))
    if q is None:
        return "%s 股" % order.get("qty")
    return tw.fmt_shares(q)


def _price_words(order):
    if str(order.get("price_type", "market")) == "limit":
        return "限價 %s" % _p(order.get("price"), _code(order))
    return "市價"


def _p(v, code=""):
    return tw.fmt_price(v, code) if _f(v) else str(v)


def plan_of(order):
    return order.get("plan") if isinstance(order.get("plan"), dict) else {}


def entry_ref(order):
    """The price the ticket buys at: the limit price, else the plan's entry."""
    if str(order.get("price_type", "market")) == "limit" and _f(order.get("price")):
        return _f(order["price"])
    return _f(plan_of(order).get("entry"))


def max_loss(order, fees=None, on=None):
    """{"total", "costs", "stop"}: what a buy loses if it is sold exactly at the stop, fees and tax
    included, or None. A market ticket uses the plan's own number; a limit price is recomputed."""
    if str(order.get("side", "")).lower() != "buy":
        return None
    plan = plan_of(order)
    stop = _f(order.get("stop")) or _f(plan.get("stop"))
    q = _int(order.get("qty"))
    entry = entry_ref(order)
    if not stop or not q or not entry:
        return None
    limit = str(order.get("price_type", "market")) == "limit"
    if not limit and _f(plan.get("max_loss")) is not None and _f(plan.get("entry")) == entry:
        costs = _f(plan.get("cost_at_stop"))
        return {"total": _f(plan["max_loss"]), "costs": int(costs) if costs is not None else None, "stop": stop}
    day = _date(plan.get("date")) or on
    r = tw.loss_at_stop(entry, stop, q, _code(order), order.get("trade") == "day_trade", fees, day)
    return {"total": r["total"], "costs": r["costs"], "stop": stop}


def first_part(order):
    """"買進 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖"."""
    return "%s %s，%s %s，%s %s，%s" % (
        SIDE_WORDS.get(str(order.get("side", "")).lower(), order.get("side")), _who(order),
        LOT_WORDS.get(order.get("lot"), order.get("lot")), _qty_words(order),
        _price_words(order), order.get("tif", ""), TRADE_WORDS.get(order.get("trade"), order.get("trade")))


def plain_parts(order, fees=None, on=None):
    parts = [first_part(order)]
    code = _code(order)
    if order.get("stop") is not None:
        parts.append("停損 %s" % _p(order["stop"], code))
    if order.get("target") is not None:
        parts.append("停利 %s" % _p(order["target"], code))
    loss = max_loss(order, fees, on)
    if loss is not None:
        extra = ("含手續費和證交稅 %s；" % tw.twd(loss["costs"])) if loss["costs"] is not None else ""
        parts.append("停損出場約虧 %s（%s急跌、跳空或跌停鎖死時可能虧更多）" % (tw.twd(loss["total"]), extra))
    return parts


def plain(order, fees=None, on=None):
    """買進 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖 / 停損 108.5 / 停利 111.5 / 停損出場約虧 NT$1,972（...）"""
    return " / ".join(plain_parts(order, fees, on))


def app_fields(order):
    """[(label, value)]: what to fill in the broker app, in the order most apps ask."""
    code = _code(order)
    side = str(order.get("side", "")).lower()
    q = _int(order.get("qty"))
    rows = [("市場", MARKET_WORDS.get(order.get("market"), "（看代號，上市或上櫃）")),
            ("代號", ("%s %s" % (code, order.get("name"))).strip() if order.get("name") else code),
            ("買賣", SIDE_WORDS.get(side, side)),
            ("整股／零股", LOT_WORDS.get(order.get("lot"), str(order.get("lot"))))]
    if order.get("trade") == "day_trade":
        if side == "buy":
            rows.append(("交易別", "現股（今天先買後賣就是現股當沖，要先跟券商簽好當沖契約）"))
        else:
            rows.append(("交易別", "現股（賣掉今天買的股票就是現股當沖；有的 App 要選「現沖」，照你券商的畫面）"))
    else:
        rows.append(("交易別", "現股"))
    rows.append(("價格", "限價 %s" % order.get("price") if order.get("price_type") == "limit" else "市價"))
    tif = str(order.get("tif", ""))
    rows.append(("委託條件", "%s（%s）" % (tif, TIF_WORDS[tif]) if tif in TIF_WORDS else tif))
    if q is not None and order.get("lot") == "board":
        rows.append(("數量", "%d 張（%s 股）" % (q // tw.LOT, "{:,}".format(q)) if q % tw.LOT == 0 else "%d 股" % q))
    else:
        rows.append(("數量", "%s 股" % (q if q is not None else order.get("qty"))))
    return rows


def condition_lines(order):
    """The stop and the target as 條件單 the user sets in the app after the buy fills."""
    if str(order.get("side", "")).lower() != "buy" or (order.get("stop") is None and order.get("target") is None):
        return []
    code, qty = _code(order), _qty_words(order)
    if order.get("trade") != "day_trade":
        return ["這一張是留倉（現股，不是當沖）：今天買的最快下一個交易日才能賣，今天的停損和停利都用不上；"
                "隔天開盤可能直接跳空跌破停損。"]
    lines = ["買進成交後，在券商 App 設條件單（觸價單）："]
    if order.get("stop") is not None:
        lines.append("  停損：成交價小於或等於 %s 時，市價賣出 %s。沒有條件單就用 trader-alerts 響鈴，自己賣。"
                     % (_p(order["stop"], code), qty))
    if order.get("target") is not None:
        lines.append("  停利：成交價大於或等於 %s 時，賣出 %s；或直接掛限價 %s 賣出。"
                     % (_p(order["target"], code), qty, _p(order["target"], code)))
    if order.get("stop") is not None and order.get("target") is not None:
        lines.append("  兩張都設的話，一張成交後要刪掉另一張，免得重複賣出（有的券商有「二擇一」條件單會自動處理）。")
    return lines


def amount_numbers(order, fees=None, on=None):
    """Every number a yes may name: the qty in shares and in 張, the prices, the max loss."""
    nums = []
    q = _int(order.get("qty"))
    if q is not None:
        nums.append(float(q))
        if order.get("lot") == "board" and q % tw.LOT == 0:
            nums.append(float(q // tw.LOT))
    for v in (order.get("price"), order.get("stop"), order.get("target"), entry_ref(order)):
        if _f(v) is not None:
            nums.append(_f(v))
    loss = max_loss(order, fees, on)
    if loss is not None:
        nums.append(float(loss["total"]))
    return nums


def _others(batch, order):
    """Other tickets' codes and names in the batch (items are tickets or bare codes)."""
    code, name = _code(order), str(order.get("name") or "")
    codes, names = set(), set()
    for b in batch or ():
        if isinstance(b, dict):
            c, n = _code(b), str(b.get("name") or "").strip()
        else:
            c, n = str(b).strip().upper(), ""
        if c and c != code:
            codes.add(c)
            if n and n != name:
                names.add(n)
    return codes, names


def parse_yes(reply, order, batch=(), fees=None, on=None):
    """Is this reply an explicit yes for THIS one ticket? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "回覆是空的"
    if "?" in raw or "？" in raw:
        return False, "回覆是一個問題，不是 yes"
    s = re.sub(r"^[\s\"'「『(（]+", "", raw.lower())
    rest = None
    for w in YES_ZH:
        if s.startswith(w):
            rest = s[len(w):]
            break
    if rest is None:
        m = YES_EN.match(s)
        if m:
            rest = s[m.end():]
    if rest is None:
        return False, "只有開頭是「yes」「是」「確認」或「確定」的回覆才算（好、ok、可以、sure 都不算）"
    words = set(re.findall(r"[a-z']+", rest))
    if BATCH_EN & words or any(w in rest for w in BATCH_ZH):
        return False, "一個 yes 只算一張委託，不能整批同意"
    if DOUBT_EN & words or any(w in rest for w in DOUBT_ZH):
        return False, "回覆裡有條件或猶豫，請再問一次"
    code = _code(order)
    codes, names = _others(batch, order)
    text = raw.upper()
    named = sorted(n for n in names if n in raw)
    amounts = amount_numbers(order, fees, on)
    for tok in CODE_TOKEN.findall(text):
        if tok in codes and not any(abs(float(re.sub(r"[A-Z]", "", tok)) - a) < 0.005 for a in amounts):
            named.append(tok)
    if named:
        return False, "回覆提到 %s，但這一張是 %s" % ("、".join(sorted(set(named))), _who(order))
    flat_by = str(plan_of(order).get("flat_by") or "")
    for h, m in TIME_TOKEN.findall(text):
        if "%02d:%s" % (int(h), m) != flat_by:
            return False, "回覆裡的 %s:%s 跟這一張不符" % (h, m)
    text = TIME_TOKEN.sub(" ", text)
    text = re.sub(r"(?<![\d.])%s(?![\d.])" % re.escape(code), " ", text) if code else text
    bad = [n for n in NUMBER.findall(text)
           if not any(abs(float(n.replace(",", "")) - a) < 0.005 for a in amounts)]
    if bad:
        return False, "回覆裡的 %s 跟這一張不符（%s）" % ("、".join(bad), plain(order, fees, on))
    return True, "明確同意這一張：%s" % plain(order, fees, on)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "委託紀錄", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk 還沒檢查這些委託（先執行 check.py）"
    if risk.get("date") != today:
        return None, "風險檢查是 %s 的，不是今天：再執行一次 check.py" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "這一張不在最近一次風險檢查裡（內容改過或是新的）：再執行一次 check.py"


def parse_at(text):
    """"2026-11-02T09:21:00+08:00" -> naive UTC, or None."""
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return tw.taipei_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def yes_expired(rec, now):
    """An API yes is good for SEND_WITHIN_MINUTES: after that the price may have moved."""
    at = parse_at(rec.get("at"))
    return at is None or (now - at).total_seconds() > SEND_WITHIN_MINUTES * 60


def status_of(order, risk, state, today, now=None, api=False):
    """(ready, label, risk_result). api: the Shioaji flow, where a yes expires before it is sent."""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    rec = latest(state, fp, today)
    st = (rec or {}).get("status")
    at = str((rec or {}).get("at", ""))[11:16]
    if rec and st != "approved":
        # already decided today; a later check blocks it as a duplicate and, once the position
        # is flat, as a short without a stop (2026-11-03 13:21): that is not news about this ticket
        return False, "DONE：%s（%s）" % (STATUS_WORDS.get(st, st), at), rv
    if rv is None:
        return False, "NOT READY：" + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED：trader-risk 擋下：" + "；".join(rv.get("reasons", [])), rv
    if rec and api:
        if now is not None and yes_expired(rec, now):
            return True, ("READY：你 %s 的 yes 已經超過 %d 分鐘，過期了；要送出請再回覆一次 yes"
                          % (at, SEND_WITHIN_MINUTES)), rv
        return False, "APPROVED：你在 %s 回覆了 yes，等 ticket.py send 送到永豐金" % at, rv
    if rec:
        return False, "APPROVED：你在 %s 回覆了 yes，等你自己在券商 App 送出，再用 result 記錄" % at, rv
    return True, "READY：trader-risk 今天通過了這一張，等你回覆 yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (without a verdict: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


def broker_of(cfg):
    b = str(cfg.get("broker") or "manual").strip().lower()
    if b not in BROKERS:
        raise TicketError("設定檔的 broker 是 %r：只能是 manual（自己在券商 App 送出）或 shioaji（永豐金 API）" % b)
    return b


def account_line(cfg):
    paper = cfg.get("paper", True)
    if broker_of(cfg) == "shioaji":
        if paper:
            return "永豐金 Shioaji 模擬環境：回覆 yes 之後由 ticket.py send 送到永豐的模擬環境，不是真的錢。"
        return "永豐金 Shioaji 正式環境（\"paper\": false）：回覆 yes 之後由 ticket.py send --live 送出，這是真的錢。"
    if paper:
        return "模擬交易，不要在真的券商送出，回覆 yes 後用 fill 記錄假設成交。"
    return "真錢帳戶（\"paper\": false）：這一張送出去就是真的錢。"


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today, now=None):
    fees = tw.fee_settings(cfg)
    on = _date(today)
    api = broker_of(cfg) == "shioaji"
    ready, label, rv = status_of(order, risk, state, today, now, api)
    paper = cfg.get("paper", True)
    head = "委託 %d／%d，%s" % (idx, total, "模擬交易" if paper else "真錢帳戶")
    if cfg.get("broker_name"):
        head += "，券商：%s" % cfg["broker_name"]
    lines = [head]
    for part in plain_parts(order, fees, on):
        lines.append("  " + part)
    plan = plan_of(order)
    if str(order.get("side", "")).lower() == "buy" and _f(plan.get("entry")):
        lines.append("  計畫進場 %s%s" % (_p(plan["entry"], _code(order)),
                                       ("（%s）" % plan["entry_time"]) if plan.get("entry_time") else ""))
    for c in condition_lines(order):
        lines.append("  " + c)
    if order.get("trade") == "day_trade" and str(order.get("side", "")).lower() == "buy":
        lines.append("  %s 前出清：不論輸贏都賣掉，不留倉（到時用 ticket.py flatten 產生賣單）。沒沖掉的部分 T+2 要付全額，"
                     "付不出來就是違約交割。" % (plan.get("flat_by") or cfg.get("flat_by") or tw.FLAT_BY))
    if order.get("why"):
        lines.append("  理由：" + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  注意：" + w)
    if api:
        lines.append("  送到永豐金的委託（%s）：" % ("ticket.py send --live" if not cfg.get("paper", True)
                                              else "ticket.py send，模擬環境"))
    else:
        lines.append("  在券商 App 依序填：")
    for k, v in app_fields(order):
        lines.append("    %s：%s" % (k, v))
    lines.append("  " + account_line(cfg))
    if ready:
        lines.append("  要下這一張就回覆 yes（或「是」）；不要就回覆 no。一個 yes 只算這一張。")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, now=None):
    fees = tw.fee_settings(cfg)
    on = _date(today)
    ready, label, rv = status_of(order, risk, state, today, now, broker_of(cfg) == "shioaji")
    loss = max_loss(order, fees, on)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, fees, on),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)), "broker": broker_of(cfg),
            "ticket": {k: order[k] for k in tw.TICKET_FIELDS if order.get(k) is not None},
            "app_fields": [list(r) for r in app_fields(order)], "conditions": condition_lines(order),
            "why": order.get("why", ""), "max_loss": loss["total"] if loss else None,
            "costs_at_stop": loss["costs"] if loss else None, "stop": _f(order.get("stop")),
            "target": _f(order.get("target"))}


# ---------------------------------------------------------------- fills -> portfolio

def _position(pf, code):
    for p in pf.setdefault("positions", []):
        if str(p.get("symbol") or p.get("ticker") or "").upper() == code:
            return p
    return None


def _lot_of(qty):
    if qty % tw.LOT == 0:
        return "board"
    if qty < tw.LOT:
        return "odd"
    raise TicketError("一筆成交不是整股（1,000 股的倍數）就是零股（1 到 999 股），不會是 %s 股" % "{:,}".format(qty))


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, trade=None, market=None, name=None):
    """Record a filled order in trader/portfolio.json. A buy costs its value plus the fee. A sell
    adds its value minus the fee and the tax (the day-trade rate on shares bought the same day);
    it relieves today's board lots first, then the oldest. Odd lots bought today cannot be sold."""
    try:
        code, sfx_market = tw.parse_symbol(symbol)
    except ValueError as e:
        raise TicketError(str(e))
    q, p = _int(qty), _f(price)
    if q is None or q <= 0 or p is None or p <= 0:
        raise TicketError("成交要有正的整數股數和正的價格")
    if not tw.on_tick(p, code):
        raise TicketError("成交價 %s 不在升降單位上（%s 這個價位一檔是 %s）"
                          % (price, code, tw.tick_size(p, code)))
    lot = _lot_of(q)
    day = _date(date)
    if day is None:
        raise TicketError("--date 要像 2026-11-02")
    pf = read_json(portfolio_path, "持股", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("還沒有 %s，設定檔也沒有 budget 可以當起始現金" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    fees = tw.fee_settings(cfg)
    minimum = fees["minimum"] if lot == "board" else fees["odd_minimum"]
    value = tw.dec(p) * q
    fee = tw.broker_fee(value, fees["discount"], minimum)
    pos = _position(pf, code)
    if side == "buy":
        if pos is None:
            pos = {"symbol": code, "market": market or sfx_market, "name": name or "", "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        trade = trade or ("day_trade" if lot == "board" else "cash")
        if lot == "odd":
            trade = "cash"
        lots.append({"id": "%s-%d" % (code, len(lots) + 1), "date": day.isoformat(), "shares": q,
                     "cost": _amount(value + fee), "lot": lot, "trade": trade})
        pos["shares"] = int(pos.get("shares", 0)) + q
        pf["cash"] = _amount(tw.dec(pf.get("cash", 0)) - value - fee)
        detail = {"value": value, "fee": fee, "tax": 0}
    else:
        held = int(pos.get("shares", 0)) if pos else 0
        lots = pos.get("lots", []) if pos else []
        odd_today = sum(int(l["shares"]) for l in lots if l.get("date") == day.isoformat() and l.get("lot") == "odd")
        if held - odd_today < q:
            why = "持股只有 %s 股" % "{:,}".format(held)
            if odd_today:
                why += "，其中 %d 股是今天買的零股，零股不能當沖，最快下一個交易日才能賣" % odd_today
            raise TicketError("不能記錄賣出 %s 股 %s：%s" % ("{:,}".format(q), code, why))
        today_lots = [l for l in lots if l.get("date") == day.isoformat() and l.get("lot") != "odd"]
        older = sorted((l for l in lots if l.get("date") != day.isoformat()), key=lambda l: l.get("date", ""))
        left, same_day = q, 0
        for l in today_lots + older:
            if left <= 0:
                break
            have = int(l["shares"])
            take = min(left, have)
            per = tw.dec(l["cost"]) / have if have else Decimal(0)
            l["cost"] = _amount(tw.dec(l["cost"]) - per * take)
            l["shares"] = have - take
            if l.get("date") == day.isoformat():
                same_day += take
            left -= take
        pos["lots"] = [l for l in lots if int(l["shares"]) > 0]
        pos["shares"] = held - q
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        tax = (tw.sell_tax(tw.dec(p) * same_day, code, True, day)
               + tw.sell_tax(tw.dec(p) * (q - same_day), code, False, day))
        pf["cash"] = _amount(tw.dec(pf.get("cash", 0)) + value - fee - tax)
        detail = {"value": value, "fee": fee, "tax": tax}
    write_json(portfolio_path, pf)
    return pf, detail


def log_fill(state_path, now, today, symbol, side, qty, price, lot=None, trade=None, extra=None):
    """A fill in the log, so trader-risk can see what happened today."""
    state = load_state(state_path)
    q = int(qty)
    rec = {"at": taipei_iso(now), "date": today, "status": "filled", "symbol": str(symbol).upper(), "side": side,
           "qty": q, "price": float(price), "lot": lot or ("board" if q % tw.LOT == 0 else "odd"), "trade": trade,
           "plain": "%s %s %s，成交價 %s" % (SIDE_WORDS.get(side, side), str(symbol).upper(), tw.fmt_shares(q),
                                         tw.fmt_price(price, str(symbol).upper()))}
    rec.update(extra or {})
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    try:
        now = tw.parse_now(getattr(a, "now", None))
    except ValueError as e:
        raise TicketError(str(e))
    today = getattr(a, "today", None) or tw.to_taipei(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today 要像 2026-11-02")
    return now, today


def _load(a):
    doc = read_json(a.orders, "委託單（先執行 trader-budget，或 ticket.py flatten）")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("%s 裡沒有委託" % a.orders)
    cfg = read_json(a.config, "設定檔", required=False) or {}
    risk = read_json(a.risk, "風險檢查", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("要說是哪一張：--index N")
    if not 1 <= index <= len(orders):
        raise TicketError("沒有第 %d 張（總共 %d 張）" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, now) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, ensure_ascii=False, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, now)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\n一次一張：一個 yes 只算一張委託，不會整批同意。這是教學與工具，不構成投資建議。")
    return 0 if any_ready else 1


def _value(order):
    q, ref = _int(order.get("qty")), entry_ref(order)
    if q is None or not ref:
        return None
    return float(tw.dec(ref) * q)


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, now, broker_of(cfg) == "shioaji")
    if not ready:
        raise TicketError("第 %d 張還不能同意：%s" % (a.index, label))
    fees, on = tw.fee_settings(cfg), _date(today)
    ok, why = parse_yes(a.reply, order, orders, fees, on)
    if not ok:
        print("沒有記錄：%s。請帶著第 %d 張的內容再問使用者一次。" % (why, a.index))
        return 1
    loss = max_loss(order, fees, on)
    rec = {"at": taipei_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": _code(order), "name": order.get("name", ""), "side": order.get("side"),
           "lot": order.get("lot"), "qty": _int(order.get("qty")), "trade": order.get("trade"),
           "kind": kind_of(order, rv), "value_twd": _value(order),
           "max_loss": loss["total"] if loss else None, "stop": _f(order.get("stop")),
           "plain": plain(order, fees, on), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True))}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("已記錄你對第 %d 張的 yes：%s。" % (a.index, plain(order, fees, on)))
    print("還沒有任何東西送出。")
    if broker_of(cfg) == "shioaji":
        live = not cfg.get("paper", True)
        print("下一步：ticket.py send --index %d%s，%d 分鐘內有效，送到永豐金%s。"
              % (a.index, " --live" if live else "", SEND_WITHIN_MINUTES, "正式環境（真的錢）" if live else "模擬環境"))
        print("Claude 執行 send 時，Claude Code 會先問你允不允許：你按允許才會送出。")
        print("送出後用 ticket.py sync 把成交記回來。")
        return 0
    if cfg.get("paper", True):
        print("模擬交易：不要在真的券商送出。假設它成交，記錄成交價：")
        print("  ticket.py result --index %d --status placed --filled-qty %s --filled-price 成交價"
              % (a.index, _int(order.get("qty")) or "股數"))
    else:
        print("現在請你自己在券商 App 照這些欄位送出：")
        for k, v in app_fields(order):
            print("  %s：%s" % (k, v))
        print("送出後告訴我委託書號：ticket.py result --index %d --status placed --order-no 委託書號" % a.index)
        print("成交後：ticket.py result 加上 --filled-qty 和 --filled-price，或用 ticket.py fill。")
    print("最後沒有送出：--status skipped。送出失敗：--status failed --note 原因。失敗的單不重送，要重新檢查、重新回覆 yes。")
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fees, on = tw.fee_settings(cfg), _date(today)
    rec = {"at": taipei_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": _code(order), "name": order.get("name", ""), "side": order.get("side"),
           "plain": plain(order, fees, on), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("已記錄：你不要第 %d 張（%s）。什麼都沒有送出。" % (a.index, plain(order, fees, on)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("第 %d 張今天沒有記錄到你的 yes；要先同意，才能記錄結果" % a.index)
    new = {"at": taipei_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "name": rec.get("name", ""), "side": rec["side"], "plain": rec["plain"], "status": a.status,
           "paper": bool(cfg.get("paper", True))}
    for k in ("lot", "qty", "trade", "kind", "value_twd", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.order_no:
        new["order_no"] = a.order_no
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("已記錄第 %d 張：%s。" % (a.index, STATUS_WORDS.get(a.status, a.status)))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        pf, d = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today,
                           rec.get("trade"), order.get("market"), order.get("name"))
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price,
                 rec.get("lot"), rec.get("trade"))
        print("成交已記到 %s：手續費 %s%s，現金現在 %s。"
              % (a.portfolio, tw.twd(d["fee"]), ("，證交稅 %s" % tw.twd(d["tax"])) if d["tax"] else "",
                 tw.twd(pf["cash"])))
    elif a.status == "placed":
        print("成交後記錄：ticket.py fill --symbol %s --side %s --qty 股數 --price 成交價" % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and order.get("stop") is not None:
            print("買進成交後，記得在券商 App 設好停損和停利的條件單；條件單成交時也用 fill 記錄那筆賣出。")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "設定檔", required=False) or {}
    now, _ = _clock(a)
    date = a.date or tw.to_taipei(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date 要像 2026-11-02")
    pf, d = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, a.trade)
    q = int(a.qty)
    log_fill(a.state, now, date, a.symbol, a.side, q, a.price, "board" if q % tw.LOT == 0 else "odd", a.trade)
    print("已記錄 %s %s %s，成交價 %s，%s，在 %s。手續費 %s%s，現金現在 %s。"
          % (date, SIDE_WORDS.get(a.side, a.side), str(a.symbol).upper(), a.price, tw.fmt_shares(q), a.portfolio,
             tw.twd(d["fee"]), ("，證交稅 %s" % tw.twd(d["tax"])) if d["tax"] else "", tw.twd(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "設定檔", required=False) or {}
    try:
        code, sfx_market = tw.parse_symbol(a.symbol)
    except ValueError as e:
        raise TicketError(str(e))
    now, today = _clock(a)
    local = tw.to_taipei(now)
    cal = tw.load_calendar(os.path.dirname(a.config) or "trader", cfg)
    closed = cal.closed(local.date())
    if closed:
        raise TicketError("今天（%s）休市：%s。沒有可以出清的當沖" % (tw.day_label(local.date()), closed))
    t = local.time()
    if t < tw.OPEN:
        raise TicketError("還沒開盤（%s）：今天買的部位 09:00 之後才會有" % tw.hm(t))
    if t >= tw.CLOSE:
        raise TicketError("已經收盤（%s，收盤 13:30）：今天賣不掉了。當沖沒沖掉的部分 T+2 要付全額，付不出來就是違約交割："
                          "確認帳戶有足夠交割款，下一個交易日 09:00 起再賣" % tw.hm(t))
    plan = read_json(a.plan, "計畫", required=False) or {}
    if str(plan.get("symbol", "")).upper() != code or plan.get("date") not in (None, today):
        plan = {}
    pf = read_json(a.portfolio, "持股", required=False) or {}
    pos = _position(pf, code) if isinstance(pf, dict) and pf.get("positions") else None
    source = "--qty"
    if a.qty is not None:
        qty = _int(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty 要填你持有的股數，例如 --qty 1000（1 張）")
        if qty % tw.LOT:
            raise TicketError("當沖只能是整股（1,000 股的倍數）：零股不能當沖，今天買的零股最快下一個交易日才能賣")
    else:
        lots = (pos or {}).get("lots", [])
        today_lots = [l for l in lots if l.get("date") == today]
        qty = sum(int(l["shares"]) for l in today_lots if l.get("lot") != "odd")
        odd = sum(int(l["shares"]) for l in today_lots if l.get("lot") == "odd")
        source = a.portfolio
        if qty <= 0:
            if odd:
                raise TicketError("%s 今天只有零股 %d 股：零股不能當沖，最快下一個交易日才能賣" % (code, odd))
            raise TicketError("沒有要出清的當沖：%s 裡沒有今天買的 %s 整股。若你確實有，用 --qty 帶入券商 App 顯示的股數"
                              % (a.portfolio, code))
    flat_by = plan.get("flat_by") or cfg.get("flat_by") or tw.FLAT_BY
    market = plan.get("market") or (pos or {}).get("market") or sfx_market
    name = plan.get("name") or (pos or {}).get("name") or ""
    order = {"symbol": code}
    if market:
        order["market"] = market
    if name:
        order["name"] = name
    order.update({"side": "sell", "lot": "board", "qty": qty})
    auction = t >= tw.CLOSING_AUCTION
    if a.price is not None or auction:
        if a.price is not None:
            price = _f(a.price)
            if price is None or price <= 0 or not tw.on_tick(price, code):
                raise TicketError("--price %s 不是升降單位上的價格" % a.price)
        else:
            price = _f(plan.get("limit_down"))
            if not price and _f(plan.get("prev_close")):
                price = float(tw.price_limits(plan["prev_close"], code)[1])
            if not price:
                raise TicketError("13:25 起是收盤集合競價，只收限價 ROD：需要今天的跌停價，trader/plan.json 沒有。"
                                  "用 --price 帶入跌停價（券商 App 看得到）")
        order.update({"price_type": "limit", "price": tw.price_text(price, code), "tif": "ROD"})
    else:
        order.update({"price_type": "market", "tif": "IOC"})
    order["trade"] = "day_trade"
    try:
        early = t < tw.parse_hhmm(flat_by, "flat_by")
    except ValueError:
        early = False
    if early:       # a stop or a target before flat_by (2026-11-03 10:48 said "13:20 前出清" for a stop)
        order["why"] = "提前出場：賣掉今天 %s 的當沖部位（股數來自 %s；最晚 %s 前出清）" % (_who(order), source, flat_by)
    else:
        order["why"] = "%s 前出清：賣掉今天 %s 的當沖部位（股數來自 %s）" % (flat_by, _who(order), source)
    order["plan"] = {"date": today, "flat_by": flat_by, "limit_down": plan.get("limit_down")}
    old = read_json(a.orders, "委託單", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "出清賣單。trader-risk 通過、你回覆 yes、你自己在券商 App 送出，三件事都成立才會下單。",
           "source": "ticket.py flatten %s %s" % (code, today), "orders": [order]}
    write_json(a.orders, doc)
    print("出清 %s：%s（計畫 %s 前出清）。" % (code, plain(order), flat_by))
    if auction:
        print("現在 %s 是收盤集合競價（13:25 到 13:30）：只收限價 ROD，所以用跌停價 %s 掛賣，等於不限價，"
              "13:30 一次撮合；跌停鎖死時可能賣不掉。" % (tw.hm(t), order["price"]))
    elif order["price_type"] == "market":
        print("市價 IOC：馬上成交，沒成交的部分取消；沒賣完就再執行一次 flatten。")
    print("已寫入 %s%s。" % (a.orders, ("（取代了先前 %d 筆提案）" % replaced) if replaced else ""))
    if a.qty is None:
        print("股數來自 %s。說 yes 之前，先對一下券商 App 裡的庫存。" % source)
    print("下一步：")
    print("  1. 如果停損或停利的條件單還掛著，先在券商 App 刪掉，免得同一批股票賣兩次。")
    print("  2. trader-risk：check.py。只是賣掉手上持股的賣單，不會被每日上限或進場時段擋下。")
    print("  3. ticket.py show，再請使用者針對這一張回覆 yes，然後自己在券商 App 送出。")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("%s沒有任何紀錄。" % ("" if a.all else "今天"))
        return 1
    for r in rows:
        extra = (" 委託書號 " + r["order_no"]) if r.get("order_no") else ""
        reply = ("，回覆「%s」" % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = ("，停損出場約虧 %s" % tw.twd(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %s  %s%s%s%s" % (r.get("at"), idx, STATUS_WORDS.get(r.get("status"), r.get("status")),
                                       r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%s已送出 %d 筆。" % ("" if a.all else "今天", placed))
    return 0


# ---------------------------------------------------------------- 永豐金 Shioaji (optional)

SDK = None      # the shioaji module; None = import it. Tests put a fake here.
SJ_STATUS = {"PendingSubmit": "傳送中", "PreSubmitted": "預約單", "Submitted": "已委託", "Failed": "失敗",
             "Cancelled": "已刪單", "Filled": "完全成交", "PartFilled": "部分成交"}


def _sinopac():
    """The Shioaji connection (sinopac.py), imported only when a Shioaji command runs."""
    import sinopac
    return sinopac


def _session(live):
    sp = _sinopac()
    try:
        return sp.Session(live, os.environ, sdk=SDK)
    except sp.BrokerError as e:
        raise TicketError(str(e))


def _with_broker(live, fn):
    sp = _sinopac()
    try:
        with _session(live) as s:
            return fn(s)
    except sp.BrokerError as e:
        raise TicketError(str(e))


def _need_shioaji(cfg):
    if broker_of(cfg) != "shioaji":
        raise TicketError("設定檔的 broker 不是 shioaji：委託請你自己在券商 App 送出。要用永豐金 API，"
                          "在 trader/config.json 設 \"broker\": \"shioaji\"（設定方法見 SKILL.md）")


def _plain_yes(reply):
    """A yes for a cancel: starts with yes / 是 / 確認 / 確定, no doubt in it."""
    s = re.sub(r"^[\s\"'「『(（]+", "", (reply or "").strip().lower())
    rest = next((s[len(w):] for w in YES_ZH if s.startswith(w)), None)
    if rest is None:
        m = YES_EN.match(s)
        rest = s[m.end():] if m else None
    if rest is None or "?" in s or "？" in s:
        return False
    return not ((DOUBT_EN - {"cancel"}) & set(re.findall(r"[a-z']+", rest))
                or any(w in rest for w in DOUBT_ZH if w != "取消"))


def _carry(rec, new):
    for k in ("lot", "qty", "trade", "kind", "value_twd", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    return new


def cmd_send(a):
    orders, cfg, risk, state, today, now = _load(a)
    _need_shioaji(cfg)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        raise TicketError("不能送出第 %d 張：%s" % (a.index, why))
    if rv.get("verdict") != "PASS":
        raise TicketError("不能送出第 %d 張：trader-risk 擋下了（%s）" % (a.index, "；".join(rv.get("reasons", []))))
    rec = latest(state, fp, today)
    st = (rec or {}).get("status")
    if st in ("placed", "cancelled"):
        raise TicketError("第 %d 張今天已經送出過了（委託書號 %s）：一張只送一次，不重送"
                          % (a.index, rec.get("order_no") or "不明"))
    if st == "failed":
        raise TicketError("第 %d 張上次送出失敗：不要重送；要重來就重新規劃、重新檢查、重新回覆 yes" % a.index)
    if st != "approved":
        raise TicketError("第 %d 張還沒有你的 yes：先把委託單給使用者看，記錄他針對這一張的明確同意（approve）" % a.index)
    if yes_expired(rec, now):
        raise TicketError("你對第 %d 張的 yes 是 %s 記錄的，超過 %d 分鐘：價格可能變了。先重新檢查（check.py），"
                          "再請使用者回覆一次 yes" % (a.index, str(rec.get("at", ""))[11:16], SEND_WITHIN_MINUTES))
    live = not cfg.get("paper", True)
    if live and not a.live:
        raise TicketError("設定檔是真錢帳戶（\"paper\": false）：要送出真的委託，指令要加 --live，"
                          "而且使用者要在對話中確認這是真的錢")
    if a.live and not live:
        raise TicketError("設定檔是模擬交易（\"paper\": true），不能加 --live")
    if not live and order.get("lot") == "odd":
        raise TicketError("永豐的模擬環境不收零股：這一張不要用 send。假設成交的話，用 "
                          "ticket.py result --index %d --status placed --filled-qty 股數 --filled-price 價格 記錄" % a.index)
    local = tw.to_taipei(now)
    closed = tw.load_calendar(os.path.dirname(a.config) or "trader", cfg).closed(local.date())
    if closed:
        raise TicketError("今天（%s）休市：%s。這個 skill 不送預約單" % (tw.day_label(local.date()), closed))
    if not tw.PRE_OPEN <= local.time() < tw.CLOSE:
        raise TicketError("現在 %s 不在交易時段（08:30 到 13:30）：這個 skill 不送預約單" % tw.hm(local.time()))

    fees, on = tw.fee_settings(cfg), _date(today)
    base = {"at": taipei_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": _code(order),
            "name": order.get("name", ""), "side": order.get("side"), "plain": plain(order, fees, on),
            "paper": not live, "broker": "shioaji", "simulation": not live}
    _carry(rec, base)
    sp = _sinopac()
    session = _session(live)            # the package and the keys: nothing has been sent yet
    try:
        with session as s:
            info = s.place(order)
    except sp.BrokerError as e:
        if not e.maybe_sent:            # login, certificate, contract: nothing reached the exchange
            raise TicketError("沒有送出：%s" % e)
        # counted as placed until sync proves otherwise: trader-risk must not hand out the same room twice
        base.update({"status": "placed", "uncertain": True, "note": str(e)})
        state.setdefault("orders", []).append(base)
        write_json(a.state, state)
        print("送出時出錯：%s" % e)
        print("永豐可能已經收到這張委託，所以先記成「已送出（結果不明）」：執行 ticket.py sync 確認，不要重送。")
        return 1
    failed = info["status"] == "Failed"
    base.update({"status": "failed" if failed else "placed", "order_no": info["ordno"],
                 "broker_trade_id": info["id"], "broker_status": info["status"], "broker_msg": info["msg"]})
    state.setdefault("orders", []).append(base)
    write_json(a.state, state)
    where = "永豐金模擬環境" if not live else "永豐金正式環境（真的錢）"
    if failed:
        print("永豐回覆失敗：%s（%s）。第 %d 張沒有送出。" % (info["msg"] or "沒有說明", where, a.index))
        return 1
    print("已送到%s：第 %d 張，%s。" % (where, a.index, plain(order, fees, on)))
    print("委託書號 %s，狀態 %s%s。" % (info["ordno"] or "（還沒給）", SJ_STATUS.get(info["status"], info["status"]),
                                  ("，%s" % info["msg"]) if info["msg"] else ""))
    print("下一步：ticket.py sync 把成交記回來。")
    if order.get("side") == "buy" and order.get("stop") is not None:
        print("停損和停利沒有一起送出：成交後在永豐的 App 設條件單，或用 trader-alerts 響鈴。")
    return 0


def cmd_sync(a):
    cfg = read_json(a.config, "設定檔", required=False) or {}
    _need_shioaji(cfg)
    now, today = _clock(a)
    live = not cfg.get("paper", True)
    state = load_state(a.state)
    sent = [r for r in state.get("orders", []) if rec_day(r) == today and r.get("broker") == "shioaji"
            and r.get("status") == "placed"]
    if not sent:
        print("今天沒有用 ticket.py send 送出的委託。")
        return 0
    trades = _with_broker(live, lambda s: s.trades())
    linked = {r.get("order_no") for r in sent if r.get("order_no")}
    for r in sent:
        if r.get("order_no") or r.get("broker_trade_id"):
            continue
        # a send that errored: find its order by code, side and size among the unlinked ones
        t = next((x for x in trades if x["ordno"] not in linked and x["code"] == r["symbol"]
                  and x["side"] == r["side"] and x["shares"] == _int(r.get("qty"))), None)
        if t is not None:
            r.update({"order_no": t["ordno"], "broker_trade_id": t["id"], "uncertain": False})
            linked.add(t["ordno"])
            print("#%s：永豐有收到，委託書號 %s。" % (r.get("index"), t["ordno"]))
        else:
            print("#%s：永豐今天沒有這張委託，應該沒有送出成功。不要重送；要重來就重新規劃、重新檢查、重新回覆 yes。"
                  % r.get("index"))
    write_json(a.state, state)
    logged = {r.get("deal_key") for r in state.get("orders", []) if r.get("deal_key")}
    done = {(r.get("fingerprint"), r.get("status")) for r in state.get("orders", []) if rec_day(r) == today}
    terminal = []
    for r in sent:
        t = next((x for x in trades if (r.get("order_no") and x["ordno"] == r["order_no"])
                  or (r.get("broker_trade_id") and x["id"] == r["broker_trade_id"])), None)
        label = "#%s 委託書號 %s" % (r.get("index"), r.get("order_no") or "不明")
        if t is None:
            print("%s：永豐今天的委託裡找不到。" % label)
            continue
        for d in t["deals"]:
            key = "%s:%s" % (t["ordno"] or t["id"], d["seq"])
            if key in logged:
                continue
            apply_fill(a.portfolio, cfg, r["symbol"], r["side"], d["shares"], d["price"], today, r.get("trade"),
                       None, r.get("name"))
            log_fill(a.state, now, today, r["symbol"], r["side"], d["shares"], d["price"], r.get("lot"),
                     r.get("trade"), {"deal_key": key, "order_no": t["ordno"], "broker": "shioaji"})
            logged.add(key)
            print("%s：記錄成交 %s，成交價 %s。" % (label, tw.fmt_shares(d["shares"]), tw.fmt_price(d["price"], r["symbol"])))
        end = {"Cancelled": "cancelled", "Failed": "failed"}.get(t["status"])
        if end and (r.get("fingerprint"), end) not in done:
            terminal.append(_carry(r, {"at": taipei_iso(now), "date": today, "index": r.get("index"),
                                       "fingerprint": r.get("fingerprint"), "symbol": r["symbol"],
                                       "name": r.get("name", ""), "side": r["side"], "plain": r.get("plain"),
                                       "status": end, "order_no": t["ordno"], "broker": "shioaji",
                                       "broker_status": t["status"], "broker_msg": t["msg"]}))
        print("%s：%s，成交 %s／%s 股%s。" % (label, SJ_STATUS.get(t["status"], t["status"]),
                                        "{:,}".format(t["deal_shares"]), "{:,}".format(t["shares"]),
                                        ("，刪單 %s 股" % "{:,}".format(t["cancel_shares"])) if t["cancel_shares"] else ""))
    if terminal:
        state = load_state(a.state)
        state.setdefault("orders", []).extend(terminal)
        write_json(a.state, state)
    pf = read_json(a.portfolio, "持股", required=False)
    if pf:
        print("現金現在 %s（%s）。" % (tw.twd(pf.get("cash", 0)), a.portfolio))
    return 0


def cmd_cancel(a):
    orders, cfg, risk, state, today, now = _load(a)
    _need_shioaji(cfg)
    order = _pick(orders, a.index)
    rec = latest(state, fingerprint(order), today)
    if not rec or rec.get("status") != "placed" or rec.get("broker") != "shioaji":
        raise TicketError("第 %d 張今天沒有用 ticket.py send 送出，沒有可以刪的委託" % a.index)
    if not _plain_yes(a.reply):
        print("沒有刪單：要使用者針對這一張明確回覆 yes（或「是」），才會送出刪單。")
        return 1
    live = not cfg.get("paper", True)
    info = _with_broker(live, lambda s: s.cancel(rec.get("order_no", ""), rec.get("broker_trade_id", "")))
    if info and info["status"] == "Cancelled":
        state = load_state(a.state)
        state["orders"].append(_carry(rec, {"at": taipei_iso(now), "date": today, "index": a.index,
                                            "fingerprint": rec.get("fingerprint"), "symbol": rec["symbol"],
                                            "name": rec.get("name", ""), "side": rec["side"], "plain": rec.get("plain"),
                                            "status": "cancelled", "order_no": rec.get("order_no"),
                                            "broker": "shioaji", "reply": a.reply}))
        write_json(a.state, state)
        print("已刪單：第 %d 張（委託書號 %s）。已經成交的部分刪不掉，用 sync 記錄。" % (a.index, rec.get("order_no")))
    else:
        print("已送出刪單要求，永豐目前的狀態是 %s：等一下用 ticket.py sync 確認。"
              % SJ_STATUS.get((info or {}).get("status"), (info or {}).get("status")))
    return 0


def cmd_positions(a):
    cfg = read_json(a.config, "設定檔", required=False) or {}
    _need_shioaji(cfg)
    pos = _with_broker(not cfg.get("paper", True), lambda s: s.positions())
    pf = read_json(a.portfolio, "持股", required=False) or {}
    mine = {str(p.get("symbol") or p.get("ticker") or "").upper(): int(p.get("shares", 0))
            for p in pf.get("positions", [])}
    codes = sorted(set(pos) | set(mine))
    if not codes:
        print("永豐和 %s 都沒有庫存。" % a.portfolio)
        return 0
    off = 0
    for c in codes:
        b, m = pos.get(c, {}).get("shares", 0), mine.get(c, 0)
        flag = "" if b == m else "  <- 不一樣"
        off += 1 if flag else 0
        print("%-7s 永豐 %s 股（昨日庫存 %s 股），%s %s 股%s"
              % (c, "{:,}".format(b), "{:,}".format(pos.get(c, {}).get("yd_shares", 0)), a.portfolio, "{:,}".format(m), flag))
    if off:
        print("有 %d 檔不一樣：以永豐為準。先執行 ticket.py sync；還是不一樣，就是有成交沒記到（例如在 App 手動下的單），用 fill 補記。" % off)
    return 0


def cmd_probe(a):
    """Simulation only: log in, list the accounts, look up one code. Places nothing."""
    code = str(a.symbol).strip().upper()

    def look(s):
        accts = s.accounts()
        c = s.contract(code)
        return accts, c

    accts, c = _with_broker(False, look)
    print("已登入永豐金 Shioaji 模擬環境（沒有下任何單）。")
    for x in accts:
        print("  帳戶：%s%s" % (x["kind"], "" if x["signed"] is None else "，已簽署 API 下單：%s" % ("是" if x["signed"] else "否")))
    fields = [("代號", "code"), ("名稱", "name"), ("市場", "exchange"), ("參考價", "reference"),
              ("漲停", "limit_up"), ("跌停", "limit_down"), ("可否當沖", "day_trade")]
    print("  合約 %s：" % code)
    for label, attr in fields:
        v = getattr(c, attr, None)
        if v is not None:
            print("    %s：%s" % (label, _sinopac()._name(v) if attr in ("exchange", "day_trade") else v))
    print("正式環境要先完成：在簽署中心簽 API 下單條款，並在模擬環境完成登入和下單測試（約 5 分鐘審核）。")
    return 0


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="委託單和你的 yes（trader-broker）。不會自己下單。")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 09:20\"")
        p.add_argument("--today", help="YYYY-MM-DD（預設：台北的今天）")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="顯示委託單")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="記錄使用者對一張委託的明確 yes")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="使用者的回覆，一字不改")
    p = sub.add_parser("decline", help="記錄使用者說不要")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="記錄使用者在券商 App 送出之後的結果")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "skipped", "failed"])
    p.add_argument("--order-no", help="券商的委託書號")
    p.add_argument("--note")
    p.add_argument("--filled-qty", help="成交股數")
    p.add_argument("--filled-price", help="成交價")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="把成交記到 trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True, help="股數（1 張是 1000）")
    p.add_argument("--price", required=True)
    p.add_argument("--trade", choices=["day_trade", "cash"],
                   help="買進的交易別（預設：整股 day_trade，零股 cash）")
    p.add_argument("--date", help="YYYY-MM-DD（預設：台北的今天）")
    p.add_argument("--now")
    p.add_argument("--today")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="產生今天當沖部位的出清賣單")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="持有股數，看券商 App（預設：trader/portfolio.json 裡今天買的整股）")
    p.add_argument("--price", help="限價（預設：連續交易時段用市價 IOC，13:25 起用跌停價限價 ROD）")
    p.add_argument("--orders", default="trader/orders.json", help="賣單寫到哪裡")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="今天的紀錄")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("send", help="（永豐金 Shioaji）送出一張已同意的委託")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--live", action="store_true", help="真錢帳戶（\"paper\": false）一定要加，模擬交易不能加")
    p = sub.add_parser("sync", help="（永豐金 Shioaji）把今天的委託狀態和成交記回來")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    clock(p)
    p = sub.add_parser("cancel", help="（永豐金 Shioaji）刪掉一張已送出、還沒成交的委託")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="使用者的回覆，一字不改")
    p = sub.add_parser("positions", help="（永豐金 Shioaji）永豐的庫存，對照 trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("probe", help="（永豐金 Shioaji）只用模擬環境：登入、列帳戶、查一檔合約，不下單")
    p.add_argument("--symbol", default="2303")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log, "send": cmd_send, "sync": cmd_sync,
                "cancel": cmd_cancel, "positions": cmd_positions, "probe": cmd_probe}[a.cmd](a)
    except TicketError as e:
        print("ticket.py：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
