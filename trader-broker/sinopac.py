#!/usr/bin/env python3
"""The 永豐金 Shioaji connection for trader-broker. Optional.

Only `ticket.py send`, `sync`, `cancel`, `positions` and `probe` import this file, and only
they need the shioaji package (pip install shioaji). Everything else in the pack stays on
the standard library. Everything Shioaji-specific lives here: login, the certificate,
turning a ticket into a StockOrder, reading trades and positions back. ticket.py keeps the
gates (trader-risk PASS, the user's yes, one send per ticket) and the files.

Credentials come from environment variables only. They are never read from a file in this
pack, never printed and never written; error messages are scrubbed of them.

    SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY      every connection (SJ_API_KEY / SJ_SEC_KEY, the
                                             names the shioaji CLI uses, work too)
    SHIOAJI_CA_PATH, SHIOAJI_CA_PASSWD,      the production environment only: the order
    SHIOAJI_PERSON_ID                        certificate (simulation needs none)

From the Shioaji docs, read 2026-11-02 (https://sinotrade.github.io/zh/):
    sj.Shioaji(simulation=True); api.login(api_key=, secret_key=); api.activate_ca(ca_path=,
    ca_passwd=, person_id=); api.contracts.get(code) (older releases: api.Contracts.Stocks[code]);
    sj.StockOrder(action, price, quantity, price_type, order_type, order_lot, order_cond, account);
    api.place_order(contract, order) -> Trade; api.update_status(account); api.list_trades();
    api.list_positions(account, unit=Unit.Share); api.cancel_order(trade); api.logout().
    Trade.status.status: PendingSubmit, PreSubmitted, Submitted, Failed, Cancelled, Filled, PartFilled.
    Simulation takes no odd lots. Limits: 5 connections per person, 1,000 logins a day, 250
    order calls per 10 seconds (a breach suspends the service for a minute).
Quantity: a Common (整股) order counts 張 and an IntradayOdd order counts shares; the docs'
list_positions example shows the same position as 1 under Unit.Common and 1000 under
Unit.Share. `ticket.py probe` checks it on your own simulation login.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

ENV_KEY = ("SHIOAJI_API_KEY", "SJ_API_KEY")
ENV_SECRET = ("SHIOAJI_SECRET_KEY", "SJ_SEC_KEY")
ENV_CA = ("SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD", "SHIOAJI_PERSON_ID")
LOT = tw.LOT
INSTALL = "這個功能要用永豐金的 shioaji 套件：pip install shioaji（或 uv add shioaji），再執行一次"


class BrokerError(Exception):
    """maybe_sent: the error came from place_order itself, so Shioaji may have the order anyway."""

    def __init__(self, message, maybe_sent=False):
        Exception.__init__(self, message)
        self.maybe_sent = maybe_sent


def load_sdk():
    """The shioaji module. Tests replace this function with a fake."""
    try:
        import shioaji
    except ImportError:
        raise BrokerError(INSTALL)
    return shioaji


# ---------------------------------------------------------------- credentials

def _first(env, names):
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


def credentials(env, live):
    """{api_key, secret_key[, ca_path, ca_passwd, person_id]} from the environment, or a BrokerError
    that names the missing variables (never their values)."""
    out = {"api_key": _first(env, ENV_KEY), "secret_key": _first(env, ENV_SECRET)}
    missing = [names[0] for names, k in ((ENV_KEY, "api_key"), (ENV_SECRET, "secret_key")) if not out[k]]
    if live:
        for name, k in zip(ENV_CA, ("ca_path", "ca_passwd", "person_id")):
            out[k] = (env.get(name) or "").strip()
            if not out[k]:
                missing.append(name)
        if out.get("ca_path") and not os.path.isfile(out["ca_path"]):
            raise BrokerError("SHIOAJI_CA_PATH 指的憑證檔不存在（下載的 .pfx 放在哪裡，就設成那個路徑）")
    if missing:
        raise BrokerError("缺少環境變數：%s。金鑰在永豐金理財網的 API 管理頁申請；請你自己在終端機設定，"
                          "不要貼在對話裡，也不要寫進任何檔案" % "、".join(missing))
    return out


def _scrub(text, creds):
    s = str(text)
    for v in (creds or {}).values():
        if v and len(v) >= 4:
            s = s.replace(v, "***")
    return s


# ---------------------------------------------------------------- enums and values

def _const(sdk, group, name):
    """sdk.Action.Buy, or sdk.constant.Action.Buy in older releases."""
    holder = getattr(sdk, group, None) or getattr(getattr(sdk, "constant", None), group, None)
    if holder is None:
        raise BrokerError("這個版本的 shioaji 沒有 %s：請更新（pip install -U shioaji）" % group)
    return getattr(holder, name)


def _name(v):
    """An enum member or a string -> its last part: Action.Buy -> "Buy"."""
    for attr in ("value", "name"):
        x = getattr(v, attr, None)
        if isinstance(x, str) and x:
            return x.split(".")[-1]
    return str(v).split(".")[-1]


def order_kwargs(sdk, ticket, account):
    """A ticket (trader-budget / flatten shape) -> the StockOrder arguments."""
    lot = ticket.get("lot")
    qty = int(ticket.get("qty"))
    if lot == "board":
        if qty % LOT:
            raise BrokerError("整股要是 1,000 股的倍數，這一張是 %d 股" % qty)
        quantity, order_lot = qty // LOT, "Common"
    elif lot == "odd":
        if not 1 <= qty < LOT:
            raise BrokerError("盤中零股是 1 到 999 股，這一張是 %d 股" % qty)
        quantity, order_lot = qty, "IntradayOdd"
    else:
        raise BrokerError("不知道的 lot：%r" % lot)
    side = str(ticket.get("side", "")).lower()
    if side not in ("buy", "sell"):
        raise BrokerError("side 要是 buy 或 sell")
    limit = ticket.get("price_type") == "limit"
    if limit and not ticket.get("price"):
        raise BrokerError("限價單要有價格")
    if lot == "odd" and not (limit and ticket.get("tif") == "ROD"):
        raise BrokerError("盤中零股只能限價 ROD")
    tif = str(ticket.get("tif", ""))
    if tif not in ("ROD", "IOC", "FOK"):
        raise BrokerError("委託條件要是 ROD、IOC 或 FOK")
    return {
        "action": _const(sdk, "Action", "Buy" if side == "buy" else "Sell"),
        "price": float(ticket["price"]) if limit else 0,
        "quantity": quantity,
        "price_type": _const(sdk, "StockPriceType", "LMT" if limit else "MKT"),
        "order_type": _const(sdk, "OrderType", tif),
        "order_lot": _const(sdk, "StockOrderLot", order_lot),
        "order_cond": _const(sdk, "StockOrderCond", "Cash"),
        "account": account,
    }


def trade_info(t):
    """A Shioaji Trade -> plain values, quantities in shares."""
    o, s, c = t.order, t.status, getattr(t, "contract", None)
    lot = "odd" if _name(getattr(o, "order_lot", "")) == "IntradayOdd" else "board"
    mult = 1 if lot == "odd" else LOT
    deals = []
    for d in getattr(s, "deals", None) or []:
        deals.append({"seq": str(getattr(d, "seq", "")), "price": float(d.price), "shares": int(d.quantity) * mult,
                      "ts": getattr(d, "ts", None)})
    return {"id": str(getattr(o, "id", "") or ""), "ordno": str(getattr(o, "ordno", "") or ""),
            "seqno": str(getattr(o, "seqno", "") or ""), "code": str(getattr(c, "code", "") or ""),
            "side": "buy" if _name(o.action) == "Buy" else "sell", "lot": lot,
            "shares": int(getattr(o, "quantity", 0) or 0) * mult, "price": float(getattr(o, "price", 0) or 0),
            "status": _name(getattr(s, "status", "")), "status_code": str(getattr(s, "status_code", "") or ""),
            "msg": str(getattr(s, "msg", "") or ""),
            "deal_shares": int(getattr(s, "deal_quantity", 0) or 0) * mult,
            "cancel_shares": int(getattr(s, "cancel_quantity", 0) or 0) * mult, "deals": deals}


# ---------------------------------------------------------------- the session

class Session(object):
    """One login, always logged out again: `with Session(live, os.environ) as s: s.place(ticket)`."""

    def __init__(self, live, env, sdk=None):
        self.live = bool(live)
        self.creds = credentials(env, self.live)
        self.sdk = sdk or load_sdk()
        self.api = None

    def _call(self, what, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except BrokerError:
            raise
        except Exception as e:  # the SDK raises its own types; report them plainly
            raise BrokerError("%s失敗：%s" % (what, _scrub(e, self.creds)), maybe_sent=(what == "送出委託"))

    def __enter__(self):
        self.api = self._call("建立連線", self.sdk.Shioaji, simulation=not self.live)
        self._call("登入", self.api.login, api_key=self.creds["api_key"], secret_key=self.creds["secret_key"])
        if self.live:
            ok = self._call("啟用憑證", self.api.activate_ca, ca_path=self.creds["ca_path"],
                            ca_passwd=self.creds["ca_passwd"], person_id=self.creds["person_id"])
            if ok is False:
                raise BrokerError("憑證沒有啟用成功：確認 SHIOAJI_CA_PATH、SHIOAJI_CA_PASSWD、SHIOAJI_PERSON_ID")
        return self

    def __exit__(self, *exc):
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                pass
        return False

    @property
    def account(self):
        acct = getattr(self.api, "stock_account", None)
        if acct is None:
            raise BrokerError("這組金鑰沒有證券帳戶（或沒有勾選帳戶權限）")
        return acct

    def contract(self, code):
        getter = getattr(getattr(self.api, "contracts", None), "get", None)
        if getter is not None:
            c = self._call("查合約", getter, code)
        else:
            c = self._call("查合約", lambda: self.api.Contracts.Stocks[code])
        if c is None:
            raise BrokerError("永豐查不到 %s 這檔" % code)
        return c

    def place(self, ticket):
        code = str(ticket.get("symbol", "")).strip().upper()
        contract = self.contract(code)
        kw = order_kwargs(self.sdk, ticket, self.account)
        maker = getattr(self.sdk, "StockOrder", None) or self.api.Order
        order = self._call("建立委託", maker, **kw)
        return trade_info(self._call("送出委託", self.api.place_order, contract, order))

    def trades(self):
        self._call("更新委託狀態", self.api.update_status, self.account)
        return [trade_info(t) for t in (self._call("查委託", self.api.list_trades) or [])]

    def find(self, ordno="", trade_id=""):
        self._call("更新委託狀態", self.api.update_status, self.account)
        for t in self._call("查委託", self.api.list_trades) or []:
            info = trade_info(t)
            if (ordno and info["ordno"] == ordno) or (trade_id and info["id"] == trade_id):
                return t, info
        return None, None

    def cancel(self, ordno="", trade_id=""):
        t, info = self.find(ordno, trade_id)
        if t is None:
            raise BrokerError("永豐今天的委託裡找不到委託書號 %s" % (ordno or trade_id))
        self._call("刪單", self.api.cancel_order, t)
        t, info = self.find(ordno, trade_id)
        return info

    def positions(self):
        """{code: {"shares", "yd_shares", "price"}} counted in shares."""
        unit = _const(self.sdk, "Unit", "Share")
        rows = self._call("查庫存", self.api.list_positions, self.account, unit=unit) or []
        out = {}
        for p in rows:
            code = str(getattr(p, "code", "") or "")
            if code:
                out[code] = {"shares": int(getattr(p, "quantity", 0) or 0),
                             "yd_shares": int(getattr(p, "yd_quantity", 0) or 0),
                             "price": float(getattr(p, "price", 0) or 0),
                             "direction": _name(getattr(p, "direction", ""))}
        return out

    def accounts(self):
        """[{"kind", "signed"}] for the probe: no account numbers."""
        out = []
        for a in self._call("查帳戶", self.api.list_accounts) or []:
            out.append({"kind": type(a).__name__, "signed": getattr(a, "signed", None)})
        return out
