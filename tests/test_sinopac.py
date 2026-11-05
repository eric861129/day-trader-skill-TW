"""trader-broker on 永豐金 Shioaji, offline: a fake shioaji module stands in for the real one.

The fake follows the documented calls (sj.Shioaji(simulation=), login, activate_ca,
contracts.get, StockOrder, place_order, update_status, list_trades, cancel_order,
list_positions, list_accounts, logout). The reel: 聯電 2303 on 2026-11-02, 1 張 bought at
110.0 at 09:22, sold at 111.5 at 13:21."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from _load import FIX, ROOT, load

ticket = load("trader-broker", "ticket")
sinopac = load("trader-broker", "sinopac")
tw = load("trader-broker", "twmarket")

TODAY = "2026-11-02"
KEY, SECRET = "KEY-1234567890", "SECRET-abcdefghij"
ENV = {"SHIOAJI_API_KEY": KEY, "SHIOAJI_SECRET_KEY": SECRET}


def enum(*names):
    return SimpleNamespace(**{n: SimpleNamespace(value=n, name=n) for n in names})


class World(object):
    """What the fake broker remembers across logins."""

    def __init__(self):
        self.logins, self.logouts, self.simulation, self.ca = [], 0, [], None
        self.trades, self.positions, self.orders = [], [], []
        self.fail_login = self.fail_place = False
        self.reply_status = "Submitted"
        self.next_no = 1

    def fill(self, idx, price, units):
        t = self.trades[idx]
        t.status.deals.append(SimpleNamespace(seq=str(len(t.status.deals) + 1), price=price, quantity=units, ts=0))
        t.status.deal_quantity += units
        t.status.status = SJ.status("Filled" if t.status.deal_quantity >= t.order.quantity else "PartFilled")


class FakeApi(object):
    def __init__(self, world, simulation):
        self.w = world
        world.simulation.append(simulation)
        self.stock_account = "STOCK-ACCOUNT"
        self.contracts = SimpleNamespace(get=self._contract)

    def login(self, api_key, secret_key):
        if self.w.fail_login:
            raise RuntimeError("login refused for key %s" % api_key)
        self.w.logins.append((api_key, secret_key))
        return [SimpleNamespace()]

    def activate_ca(self, ca_path, ca_passwd, person_id):
        self.w.ca = (ca_path, ca_passwd, person_id)
        return True

    def logout(self):
        self.w.logouts += 1
        return True

    def _contract(self, code):
        return SimpleNamespace(code=code, name="聯電", exchange=SimpleNamespace(value="TSE"), reference=111.5,
                               limit_up=122.5, limit_down=100.5, day_trade=SimpleNamespace(value="Yes"))

    def place_order(self, contract, order):
        if self.w.fail_place:
            raise TimeoutError("timeout after 30000 ms")
        self.w.orders.append(order)
        no = "S%04d" % self.w.next_no
        self.w.next_no += 1
        t = SimpleNamespace(
            contract=contract,
            order=SimpleNamespace(id="id" + no, ordno=no, seqno="1", action=order.action, price=order.price,
                                  quantity=order.quantity, order_lot=order.order_lot),
            status=SimpleNamespace(status=SJ.status(self.w.reply_status), status_code="0",
                                   msg="委託成功" if self.w.reply_status != "Failed" else "價格超過漲跌停",
                                   deal_quantity=0, cancel_quantity=0, deals=[]))
        self.w.trades.append(t)
        return t

    def update_status(self, account=None):
        return None

    def list_trades(self):
        return list(self.w.trades)

    def cancel_order(self, trade):
        trade.status.cancel_quantity = trade.order.quantity - trade.status.deal_quantity
        trade.status.status = SJ.status("Cancelled")

    def list_positions(self, account=None, unit=None):
        return list(self.w.positions)

    def list_accounts(self):
        StockAccount = type("StockAccount", (), {"signed": True})
        return [StockAccount()]


class FakeOrder(object):
    def __init__(self, **kw):
        self.kw = kw
        self.__dict__.update(kw)


class SJ(object):
    """The fake shioaji module."""
    Action = enum("Buy", "Sell")
    StockPriceType = enum("LMT", "MKT")
    OrderType = enum("ROD", "IOC", "FOK")
    StockOrderLot = enum("Common", "Fixing", "Odd", "IntradayOdd")
    StockOrderCond = enum("Cash", "MarginTrading", "ShortSelling")
    Unit = enum("Common", "Share")
    Status = enum("PendingSubmit", "PreSubmitted", "Submitted", "Failed", "Cancelled", "Filled", "PartFilled")
    StockOrder = FakeOrder
    world = None

    @classmethod
    def status(cls, name):
        return getattr(cls.Status, name)

    @classmethod
    def Shioaji(cls, simulation=True):
        return FakeApi(cls.world, simulation)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue() + err.getvalue()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)


def reel(**over):
    o = read(os.path.join(FIX, "trader-risk", "orders-2303.json"))["orders"][0]
    o.update(over)
    return o


def risk_doc(orders, verdict="PASS", kind=None, date=TODAY):
    return {"date": date, "now_taipei": date + " 09:20", "paper": True,
            "results": [{"index": i, "symbol": o["symbol"], "side": o["side"],
                         "kind": kind or ("entry" if o["side"] == "buy" else "exit"),
                         "fingerprint": tw.fingerprint(o), "verdict": verdict,
                         "reasons": [] if verdict == "PASS" else ["虧損：超過上限"], "warnings": []}
                        for i, o in enumerate(orders, 1)]}


class Mapping(unittest.TestCase):
    def test_a_board_lot_counts_in_lots(self):
        kw = sinopac.order_kwargs(SJ, reel(), "ACC")
        self.assertEqual(kw["quantity"], 1)                     # 1,000 shares = 1 張
        self.assertEqual(kw["price"], 0)                        # market: no price
        self.assertEqual([kw[k].value for k in ("action", "price_type", "order_type", "order_lot", "order_cond")],
                         ["Buy", "MKT", "IOC", "Common", "Cash"])
        self.assertEqual(kw["account"], "ACC")

    def test_an_odd_lot_counts_in_shares(self):
        o = reel(lot="odd", qty=350, price_type="limit", price="110.0", tif="ROD", trade="cash")
        kw = sinopac.order_kwargs(SJ, o, "ACC")
        self.assertEqual((kw["quantity"], kw["price"], kw["order_lot"].value, kw["price_type"].value),
                         (350, 110.0, "IntradayOdd", "LMT"))

    def test_bad_tickets_are_refused_before_the_broker(self):
        for bad in (reel(qty=1500), reel(lot="odd", qty=350), reel(lot="odd", qty=1000, price_type="limit",
                                                                   price="110.0", tif="ROD"),
                    reel(tif="GTC"), reel(price_type="limit"), reel(side="short"), reel(lot="fixing")):
            with self.assertRaises(sinopac.BrokerError):
                sinopac.order_kwargs(SJ, bad, "ACC")

    def test_older_releases_keep_the_enums_under_constant(self):
        old = SimpleNamespace(constant=SimpleNamespace(Action=SJ.Action))
        self.assertEqual(sinopac._const(old, "Action", "Sell").value, "Sell")
        with self.assertRaises(sinopac.BrokerError):
            sinopac._const(SimpleNamespace(), "Action", "Buy")

    def test_a_trade_reads_back_in_shares(self):
        t = SimpleNamespace(contract=SimpleNamespace(code="2303"),
                            order=SimpleNamespace(id="a1", ordno="Y27FI", seqno="9", action=SJ.Action.Buy,
                                                  price=0, quantity=2, order_lot=SJ.StockOrderLot.Common),
                            status=SimpleNamespace(status=SJ.Status.PartFilled, status_code="0", msg="",
                                                   deal_quantity=1, cancel_quantity=0,
                                                   deals=[SimpleNamespace(seq="1", price=110.0, quantity=1, ts=0)]))
        info = sinopac.trade_info(t)
        self.assertEqual((info["shares"], info["deal_shares"], info["deals"][0]["shares"], info["status"]),
                         (2000, 1000, 1000, "PartFilled"))


class Credentials(unittest.TestCase):
    def test_simulation_needs_the_two_keys_only(self):
        self.assertEqual(sinopac.credentials(ENV, False), {"api_key": KEY, "secret_key": SECRET})
        self.assertEqual(sinopac.credentials({"SJ_API_KEY": KEY, "SJ_SEC_KEY": SECRET}, False)["api_key"], KEY)

    def test_missing_ones_are_named_never_shown(self):
        with self.assertRaises(sinopac.BrokerError) as e:
            sinopac.credentials({"SHIOAJI_API_KEY": KEY}, False)
        self.assertIn("SHIOAJI_SECRET_KEY", str(e.exception))
        self.assertNotIn(KEY, str(e.exception))
        with self.assertRaises(sinopac.BrokerError) as e:
            sinopac.credentials(ENV, True)
        for name in ("SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD", "SHIOAJI_PERSON_ID"):
            self.assertIn(name, str(e.exception))

    def test_the_certificate_file_must_exist(self):
        env = dict(ENV, SHIOAJI_CA_PATH=os.path.join(ROOT, "no-such.pfx"), SHIOAJI_CA_PASSWD="pw",
                   SHIOAJI_PERSON_ID="A123456789")
        with self.assertRaises(sinopac.BrokerError):
            sinopac.credentials(env, True)

    def test_the_package_is_optional(self):
        with mock.patch.dict("sys.modules", {"shioaji": None}):
            with self.assertRaises(sinopac.BrokerError) as e:
                sinopac.load_sdk()
        self.assertIn("pip install shioaji", str(e.exception))


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        p = lambda *x: os.path.join(self.d, *x)
        self.cfg, self.orders, self.risk = p("config.json"), p("orders.json"), p("state", "risk.json")
        self.state, self.pf = p("state", "broker.json"), p("portfolio.json")
        cfg = read(os.path.join(ROOT, "trader-budget", "config.example.json"))
        cfg.update({"day_trade_enabled": True, "broker": "shioaji"})
        write(self.cfg, cfg)
        write(self.orders, {"orders": [reel()]})
        write(self.risk, risk_doc([reel()]))
        SJ.world = self.world = World()
        old = ticket.SDK
        ticket.SDK = SJ
        self.addCleanup(setattr, ticket, "SDK", old)
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        for k in ("SJ_API_KEY", "SJ_SEC_KEY", "SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD", "SHIOAJI_PERSON_ID"):
            os.environ.pop(k, None)

    def config(self, **kw):
        cfg = read(self.cfg)
        cfg.update(kw)
        write(self.cfg, cfg)

    def cmd(self, name, now, *extra):
        argv = [name, "--orders", self.orders, "--config", self.cfg, "--risk", self.risk, "--state", self.state,
                "--now", now] + list(extra)
        return run(argv)

    def approve(self, now="2026-11-02 09:21", reply="是，買 1 張聯電"):
        code, text = self.cmd("approve", now, "--index", "1", "--reply", reply)
        self.assertEqual(code, 0, text)
        return text

    def send(self, now="2026-11-02 09:22", *extra):
        return self.cmd("send", now, "--index", "1", *extra)

    def sync(self, now="2026-11-02 09:23"):
        return run(["sync", "--config", self.cfg, "--state", self.state, "--portfolio", self.pf, "--now", now])

    def records(self):
        return read(self.state)["orders"]

    # ------------------------------------------------------------ the happy path

    def test_approve_points_at_send(self):
        text = self.approve()
        self.assertIn("ticket.py send --index 1", text)
        self.assertIn("模擬環境", text)
        self.assertIn("Claude Code 會先問你允不允許", text)
        code, shown = self.cmd("show", "2026-11-02 09:21")
        self.assertIn("APPROVED", shown)
        self.assertIn("送到永豐金的委託", shown)

    def test_send_places_one_order_in_the_simulation(self):
        self.approve()
        code, text = self.send()
        self.assertEqual(code, 0, text)
        self.assertIn("已送到永豐金模擬環境", text)
        self.assertIn("委託書號 S0001", text)
        self.assertEqual(self.world.simulation, [True])
        self.assertEqual(self.world.logins, [(KEY, SECRET)])
        self.assertIsNone(self.world.ca)                         # simulation needs no certificate
        self.assertEqual(self.world.logouts, 1)
        kw = self.world.orders[0].kw
        self.assertEqual((kw["quantity"], kw["price_type"].value, kw["order_type"].value), (1, "MKT", "IOC"))
        rec = self.records()[-1]
        self.assertEqual((rec["status"], rec["order_no"], rec["broker"], rec["simulation"], rec["value_twd"],
                          rec["max_loss"], rec["kind"]), ("placed", "S0001", "shioaji", True, 110000.0, 1972.0, "entry"))

    def test_sync_records_each_fill_once(self):
        self.approve()
        self.send()
        self.world.fill(0, 110.0, 1)
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("完全成交", text)
        self.assertEqual(read(self.pf)["cash"], 189844)          # 300,000 - 110,000 - fee 156
        self.sync("2026-11-02 09:24")
        self.assertEqual(read(self.pf)["cash"], 189844)          # the same fill is not counted twice
        fills = [r for r in self.records() if r["status"] == "filled"]
        self.assertEqual([(r["qty"], r["price"], r["deal_key"]) for r in fills], [(1000, 110.0, "S0001:1")])

    def test_a_day_trade_from_buy_to_flat(self):
        self.approve()
        self.send()
        self.world.fill(0, 110.0, 1)
        self.sync()
        code, text = run(["flatten", "--symbol", "2303", "--orders", self.orders, "--portfolio", self.pf,
                          "--config", self.cfg, "--plan", os.path.join(FIX, "trader-budget", "plan-2303.json"),
                          "--now", "2026-11-02 13:20"])
        self.assertEqual(code, 0, text)
        sell = read(self.orders)["orders"][0]
        write(self.risk, risk_doc([sell], kind="exit"))
        self.approve("2026-11-02 13:20", "yes")
        code, text = self.send("2026-11-02 13:20")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.world.orders[-1].kw["action"].value, "Sell")
        self.world.fill(1, 111.5, 1)
        self.sync("2026-11-02 13:21")
        pf = read(self.pf)
        self.assertEqual(pf["cash"], 301019)                     # + 111,500 - fee 158 - day-trade tax 167
        self.assertEqual(pf["positions"], [])

    # ------------------------------------------------------------ the gates

    def test_nothing_goes_without_the_yes(self):
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("還沒有你的 yes", text)
        self.assertEqual(self.world.orders, [])
        self.assertEqual(self.world.logins, [])

    def test_a_blocked_or_unchecked_ticket_never_goes(self):
        self.approve()
        write(self.risk, risk_doc([reel()], verdict="BLOCK"))
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("trader-risk 擋下", text)
        write(self.orders, {"orders": [reel(qty=2000)]})          # edited after the check
        write(self.risk, risk_doc([reel()]))
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertEqual(self.world.orders, [])

    def test_one_ticket_is_sent_once(self):
        self.approve()
        self.assertEqual(self.send()[0], 0)
        code, text = self.send("2026-11-02 09:23")
        self.assertEqual(code, 1)
        self.assertIn("已經送出過了", text)
        self.assertEqual(len(self.world.orders), 1)
        code, text = self.cmd("approve", "2026-11-02 09:24", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 1)                                # and it cannot be approved again

    def test_a_yes_goes_stale(self):
        self.approve("2026-11-02 09:21")
        code, text = self.send("2026-11-02 09:32")              # 11 minutes later
        self.assertEqual(code, 1)
        self.assertIn("超過 10 分鐘", text)
        self.assertIn("過期", self.cmd("show", "2026-11-02 09:32")[1])
        self.approve("2026-11-02 09:32", "yes")                  # a fresh yes for the same ticket
        self.assertEqual(self.send("2026-11-02 09:33")[0], 0)

    def test_real_money_needs_live_twice(self):
        self.approve()
        self.config(paper=False)
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("--live", text)
        self.config(paper=True)
        code, text = self.send("2026-11-02 09:22", "--live")
        self.assertEqual(code, 1)
        self.assertIn("不能加 --live", text)
        self.assertEqual(self.world.orders, [])

    def test_a_live_send_uses_the_certificate(self):
        pfx = os.path.join(self.d, "Sinopac.pfx")
        with open(pfx, "wb") as fh:
            fh.write(b"x")
        os.environ.update({"SHIOAJI_CA_PATH": pfx, "SHIOAJI_CA_PASSWD": "ca-secret-pw",
                           "SHIOAJI_PERSON_ID": "A123456789"})
        self.config(paper=False)
        self.approve()
        code, text = self.send("2026-11-02 09:22", "--live")
        self.assertEqual(code, 0, text)
        self.assertIn("正式環境（真的錢）", text)
        self.assertEqual(self.world.simulation, [False])
        self.assertEqual(self.world.ca, (pfx, "ca-secret-pw", "A123456789"))
        self.assertFalse(self.records()[-1]["simulation"])

    def test_the_simulation_takes_no_odd_lots(self):
        o = reel(lot="odd", qty=350, price_type="limit", price="110.0", tif="ROD", trade="cash")
        write(self.orders, {"orders": [o]})
        write(self.risk, risk_doc([o]))
        self.approve(reply="是，350 股")
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("不收零股", text)

    def test_only_in_the_session(self):
        self.approve("2026-11-02 13:29")
        code, text = self.send("2026-11-02 13:31")
        self.assertEqual(code, 1)
        self.assertIn("不在交易時段", text)
        # 2026-10-23: the typhoon day, approved and checked that day, still not sent
        write(self.risk, risk_doc([reel()], date="2026-10-23"))
        code, text = self.cmd("approve", "2026-10-23 09:21", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 0, text)
        code, text = self.cmd("send", "2026-10-23 09:22", "--index", "1")
        self.assertEqual(code, 1)
        self.assertIn("休市", text)
        self.assertEqual(self.world.orders, [])

    def test_manual_broker_never_sends(self):
        self.config(broker="manual")
        self.approve()
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("自己在券商 App 送出", text)
        self.config(broker="ibkr")
        self.assertIn("只能是 manual", self.send()[1])

    # ------------------------------------------------------------ errors

    def test_a_failed_login_sends_nothing_and_keeps_the_yes(self):
        self.approve()
        self.world.fail_login = True
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("沒有送出", text)
        self.assertNotIn(KEY, text)                              # the key is scrubbed from the SDK's message
        self.assertEqual(self.records()[-1]["status"], "approved")
        self.world.fail_login = False
        self.assertEqual(self.send("2026-11-02 09:23")[0], 0)

    def test_an_error_while_sending_counts_as_sent_until_sync_knows(self):
        self.approve()
        self.world.fail_place = True
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("不要重送", text)
        rec = self.records()[-1]
        self.assertEqual((rec["status"], rec["uncertain"]), ("placed", True))
        self.assertEqual(self.send("2026-11-02 09:23")[0], 1)    # never resent
        # it did reach the broker after all: sync finds it by code, side and size
        self.world.fail_place = False
        api = FakeApi(self.world, True)
        api.place_order(api._contract("2303"), FakeOrder(**sinopac.order_kwargs(SJ, reel(), "ACC")))
        code, text = self.sync()
        self.assertIn("永豐有收到，委託書號 S0001", text)
        placed = [r for r in self.records() if r["status"] == "placed"]
        self.assertEqual([(r["order_no"], r["uncertain"]) for r in placed], [("S0001", False)])

    def test_a_refusal_from_the_broker_is_recorded(self):
        self.approve()
        self.world.reply_status = "Failed"
        code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("價格超過漲跌停", text)
        self.assertEqual(self.records()[-1]["status"], "failed")

    def test_without_the_package(self):
        ticket.SDK = None
        self.approve()
        with mock.patch.dict("sys.modules", {"shioaji": None}):
            code, text = self.send()
        self.assertEqual(code, 1)
        self.assertIn("pip install shioaji", text)

    def test_keys_never_reach_a_file_or_the_output(self):
        self.approve()
        out = self.send()[1]
        self.world.fill(0, 110.0, 1)
        out += self.sync()[1]
        for path in (self.state, self.pf, self.cfg, self.orders):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(KEY, text, path)
            self.assertNotIn(SECRET, text, path)
        self.assertNotIn(KEY, out)
        self.assertNotIn(SECRET, out)

    # ------------------------------------------------------------ cancel, positions, probe

    def test_cancel_needs_a_yes(self):
        self.approve()
        self.send()
        code, text = self.cmd("cancel", "2026-11-02 09:25", "--index", "1", "--reply", "好")
        self.assertEqual(code, 1)
        self.assertEqual(self.world.trades[0].status.status.value, "Submitted")
        code, text = self.cmd("cancel", "2026-11-02 09:25", "--index", "1", "--reply", "是，刪單")
        self.assertEqual(code, 0, text)
        self.assertIn("已刪單", text)
        self.assertEqual(self.records()[-1]["status"], "cancelled")
        self.assertEqual(self.send("2026-11-02 09:26")[0], 1)

    def test_positions_against_the_portfolio(self):
        self.world.positions = [SimpleNamespace(code="2303", quantity=1000, yd_quantity=0, price=110.0,
                                                direction=SJ.Action.Buy)]
        write(self.pf, {"cash": 189844, "positions": []})
        code, text = run(["positions", "--config", self.cfg, "--portfolio", self.pf])
        self.assertEqual(code, 0, text)
        self.assertIn("不一樣", text)
        self.assertIn("以永豐為準", text)

    def test_probe_is_simulation_only_and_places_nothing(self):
        self.config(paper=False)
        code, text = run(["probe", "--symbol", "2303"])
        self.assertEqual(code, 0, text)
        self.assertEqual(self.world.simulation, [True])
        self.assertEqual(self.world.orders, [])
        self.assertIn("已簽署 API 下單：是", text)
        self.assertIn("漲停：122.5", text)
        self.assertIn("可否當沖：Yes", text)


if __name__ == "__main__":
    unittest.main()
