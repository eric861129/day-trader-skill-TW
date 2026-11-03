import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
tw = load("trader-broker", "twmarket")

ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-2303.json")
NOW = "2026-11-02 09:20"
TODAY = "2026-11-02"
CFG = {"budget": 300000, "paper": True, "day_trade_enabled": True, "flat_by": "13:20",
       "fee_discount": 1, "fee_min_twd": 20, "fee_min_odd_twd": 1}
PLAN = {"created_by": "trader-exit", "symbol": "2303", "market": "TWSE", "name": "聯電", "side": "buy",
        "date": TODAY, "entry": 110.0, "stop": 108.5, "target": 111.5, "flat_by": "13:20",
        "prev_close": 111.5, "limit_up": 122.5, "limit_down": 100.5}
REEL_PLAIN = ("買進 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖 / 停損 108.5 / 停利 111.5 / 停損出場約虧 "
              "NT$1,972（含手續費和證交稅 NT$472；急跌、跳空或跌停鎖死時可能虧更多）")
TSMC = {"symbol": "2330", "market": "TWSE", "name": "台積電", "side": "buy", "lot": "board", "qty": 1000,
        "price_type": "limit", "price": "1460", "tif": "ROD", "trade": "day_trade", "stop": "1450",
        "target": "1475"}


def reel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def odd(**over):
    o = {"symbol": "2303", "market": "TWSE", "name": "聯電", "side": "buy", "lot": "odd", "qty": 350,
         "price_type": "limit", "price": "110.0", "tif": "ROD", "trade": "cash", "stop": "108.5",
         "target": "111.5"}
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def write(path, doc):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def risk_doc(orders, verdicts=None, date=TODAY, kinds=None):
    """trader/state/risk.json the way trader-risk writes it, built here (no check.py)."""
    results = []
    for i, o in enumerate(orders, 1):
        v = (verdicts or {}).get(i, ("PASS", []))
        results.append({"index": i, "symbol": o["symbol"], "side": o["side"],
                        "kind": (kinds or {}).get(i, "entry" if o["side"] == "buy" else "exit"),
                        "fingerprint": tw.fingerprint(o), "verdict": v[0], "reasons": v[1], "warnings": []})
    return {"date": date, "now_taipei": date + " 09:20", "paper": True, "results": results,
            "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "block": sum(1 for r in results if r["verdict"] == "BLOCK"), "warnings": []}


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=None):
        order = order or reel()
        return ticket.parse_yes(reply, order, batch if batch is not None else [order, TSMC])[0]

    def test_a_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "是", "是的", "是，買 1 張聯電", "確認", "確定下單", "確定 1000 股",
                  "yes 2303", "yes 聯電", "yes 108.5", "yes 111.5", "是，進場 110.0", "是，停損出場約虧 1,972",
                  "yes NT$1,972", "「是」", "是，13:20 前出清"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "好", "好的", "ok", "OK", "可以", "sure", "yeah", "yep", "go", "do it", "下單", "沒問題",
                  "是嗎", "是？", "yes?", "確定嗎？", "是吧", "是不是", "全部都買", "是，全部", "是，兩張都買",
                  "yes to all", "yes both", "yes, all of them", "是，但停損改 108", "是，等一下", "是，先不要",
                  "如果到 110 再買", "是，可能", "是，晚點再說", "yes, wait", "yes if under 110", "yes but 2000",
                  "no", "不要", "not yes", "是 2 張", "yes 2000", "是，停損 108", "是，14:00 前賣"):
            self.assertFalse(self.yes(r), r)

    def test_naming_another_ticket_is_not_a_yes_for_this_one(self):
        ok, why = ticket.parse_yes("是，2330", reel(), [reel(), TSMC])
        self.assertFalse(ok)
        self.assertIn("2330", why)
        self.assertFalse(self.yes("是，台積電"))
        self.assertTrue(self.yes("是，1 張台積電", TSMC, [reel(), TSMC]))
        self.assertTrue(self.yes("yes 1460", TSMC, [reel(), TSMC]))
        self.assertFalse(self.yes("是，聯電", TSMC, [reel(), TSMC]))

    def test_odd_lot_amounts(self):
        o = odd()
        self.assertTrue(self.yes("是，350 股", o, [o]))
        self.assertFalse(self.yes("是，1 張", o, [o]))       # an odd-lot ticket has no 張


class TicketShape(unittest.TestCase):
    def test_the_fingerprint_is_the_core_one(self):
        self.assertEqual(ticket.fingerprint(reel()), "63bf9f525d5603a5")
        self.assertEqual(ticket.fingerprint(reel(why="改寫理由", name="", plan={})), "63bf9f525d5603a5")
        self.assertNotEqual(ticket.fingerprint(reel(qty=2000)), "63bf9f525d5603a5")

    def test_plain_chinese(self):
        self.assertEqual(ticket.plain(reel()), REEL_PLAIN)
        self.assertEqual(ticket.max_loss(reel()), {"total": 1972.0, "costs": 472, "stop": 108.5})
        lim = reel(price_type="limit", price="109.5", tif="ROD")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "買進 聯電 2303（上市），整股 1 張，限價 109.5 ROD，現股當沖")
        self.assertEqual(ticket.max_loss(lim), {"total": 1472.0, "costs": 472, "stop": 108.5})
        no_plan = reel()
        del no_plan["plan"]
        self.assertIsNone(ticket.max_loss(no_plan))
        self.assertEqual(ticket.plain(no_plan), "買進 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖 / 停損 108.5 / 停利 111.5")

    def test_an_odd_lot_is_held_overnight(self):
        o = odd()
        self.assertEqual(ticket.first_part(o), "買進 聯電 2303（上市），盤中零股 350 股，限價 110.0 ROD，現股（留倉）")
        want = tw.loss_at_stop(110.0, 108.5, 350, "2303", False, None, None)["total"]
        self.assertEqual(ticket.max_loss(o, None, None)["total"], want)
        self.assertEqual(ticket.max_loss(o)["costs"], 54 + 54 + 113)   # both fees, then the full 0.3 % tax
        tiny = odd(qty=5)
        self.assertEqual(ticket.max_loss(tiny)["costs"], 1 + 1 + 1)    # NT$1 odd-lot minimum fees
        lines = ticket.condition_lines(o)
        self.assertEqual(len(lines), 1)
        self.assertIn("最快下一個交易日才能賣", lines[0])
        self.assertIn(("數量", "350 股"), ticket.app_fields(o))
        self.assertIn(("交易別", "現股"), ticket.app_fields(o))

    def test_what_to_type_in_the_app(self):
        self.assertEqual(ticket.app_fields(reel()), [
            ("市場", "上市"), ("代號", "2303 聯電"), ("買賣", "買進"), ("整股／零股", "整股"),
            ("交易別", "現股（今天先買後賣就是現股當沖，要先跟券商簽好當沖契約）"), ("價格", "市價"),
            ("委託條件", "IOC（立即成交，沒成交的部分取消）"), ("數量", "1 張（1,000 股）")])
        sell = {"symbol": "6488", "market": "TPEx", "side": "sell", "lot": "board", "qty": 2000,
                "price_type": "limit", "price": "355.5", "tif": "ROD", "trade": "day_trade"}
        f = dict(ticket.app_fields(sell))
        self.assertEqual((f["市場"], f["代號"], f["價格"], f["數量"]), ("上櫃", "6488", "限價 355.5", "2 張（2,000 股）"))
        self.assertIn("現沖", f["交易別"])
        self.assertEqual(ticket.condition_lines(sell), [])

    def test_the_stop_and_target_become_conditional_orders(self):
        lines = ticket.condition_lines(reel())
        self.assertEqual(lines[0], "買進成交後，在券商 App 設條件單（觸價單）：")
        self.assertIn("成交價小於或等於 108.5 時，市價賣出 1 張", lines[1])
        self.assertIn("成交價大於或等於 111.5 時，賣出 1 張", lines[2])
        self.assertIn("二擇一", lines[3])


class NoBrokerApi(unittest.TestCase):
    def test_no_alpaca_left(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")))
        for f in ("ticket.py", "SKILL.md"):
            with open(os.path.join(ROOT, "trader-broker", f), encoding="utf-8") as fh:
                text = fh.read()
            for bad in ("Alpaca", "alpaca", "place_stock_order", "Allow once", "mcpServers", "New York",
                        "eastern_offset"):
                self.assertNotIn(bad, text, "%s: %s" % (f, bad))
            self.assertIn("券商", text)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        write(self.cfg, CFG)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.plan = os.path.join(self.d, "plan.json")
        write(self.plan, PLAN)

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def orders_now(self):
        return read(self.orders)["orders"]

    def pass_risk(self, **kw):
        write(self.risk, risk_doc(self.orders_now(), **kw))

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY：trader-risk 還沒檢查", text)
        self.assertNotIn("要下這一張就回覆 yes", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "是"))
        self.assertEqual(code, 1)
        self.assertIn("還不能同意", err)
        self.assertFalse(os.path.exists(self.state))

    def test_the_reel_from_ticket_to_fill(self):
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("委託 1／1，模擬交易\n", text)
        for part in REEL_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        for line in ("  計畫進場 110.0（09:20）", "    市場：上市", "    代號：2303 聯電", "    數量：1 張（1,000 股）",
                     "  READY：trader-risk 今天通過了這一張，等你回覆 yes",
                     "  模擬交易，不要在真的券商送出，回覆 yes 後用 fill 記錄假設成交。",
                     "  13:20 前出清：不論輸贏都賣掉", "違約交割"):
            self.assertIn(line, text)
        self.assertIn("理由：開盤區間突破，聯電 2303：1 張", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "好"))
        self.assertEqual(code, 1)
        self.assertIn("沒有記錄", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "是，買 1 張聯電"))
        self.assertEqual(code, 0)
        self.assertIn("已記錄你對第 1 張的 yes", text)
        self.assertIn("還沒有任何東西送出", text)
        self.assertIn("ticket.py result --index 1 --status placed --filled-qty 1000", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "是"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--order-no", "A1234",
                                      "--filled-qty", "1000", "--filled-price", "110.0", "--portfolio", self.pf,
                                      now="2026-11-02 09:21"))
        self.assertEqual(code, 0)
        self.assertIn("手續費 NT$156，現金現在 NT$189,844", text)
        st = read(self.state)
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc, fill = st["orders"]
        self.assertEqual(appr["at"], "2026-11-02T09:20:00+08:00")
        self.assertEqual((appr["reply"], appr["kind"], appr["date"], appr["symbol"], appr["name"]),
                         ("是，買 1 張聯電", "entry", TODAY, "2303", "聯電"))
        self.assertEqual((appr["lot"], appr["qty"], appr["trade"], appr["value_twd"], appr["max_loss"], appr["stop"]),
                         ("board", 1000, "day_trade", 110000.0, 1972.0, 108.5))
        self.assertEqual((plc["order_no"], plc["max_loss"], plc["kind"], plc["value_twd"], plc["at"]),
                         ("A1234", 1972.0, "entry", 110000.0, "2026-11-02T09:21:00+08:00"))
        self.assertEqual((fill["qty"], fill["price"], fill["lot"], fill["trade"]), (1000, 110.0, "board", "day_trade"))
        pf = read(self.pf)
        self.assertEqual(pf, {"cash": 189844, "positions": [
            {"symbol": "2303", "market": "TWSE", "name": "聯電", "shares": 1000,
             "lots": [{"id": "2303-1", "date": TODAY, "shares": 1000, "cost": 110156, "lot": "board",
                       "trade": "day_trade"}]}]})

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("DONE：已送出（09:21）", text)
        code, text, _ = run(["log", "--state", self.state, "--now", "2026-11-02 09:30"])
        self.assertIn("今天已送出 1 筆", text)
        self.assertIn("停損出場約虧 NT$1,972", text)
        self.assertIn("委託書號 A1234", text)
        self.assertEqual(run(["log", "--state", self.state, "--now", "2026-11-03 09:30"])[0], 1)

    def test_show_json(self):
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        self.assertEqual(code, 0)
        t = json.loads(text)
        self.assertEqual((t["ready"], t["fingerprint"], t["max_loss"], t["costs_at_stop"], t["stop"], t["target"]),
                         (True, "63bf9f525d5603a5", 1972.0, 472, 108.5, 111.5))
        self.assertEqual(t["ticket"], {k: v for k, v in reel().items() if k in tw.TICKET_FIELDS})
        self.assertEqual(t["app_fields"][0], ["市場", "上市"])

    def test_a_live_account_says_real_money(self):
        write(self.cfg, dict(CFG, paper=False, broker_name="某某證券"))
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("委託 1／1，真錢帳戶，券商：某某證券", text)
        self.assertIn("這一張送出去就是真的錢", text)
        self.assertNotIn("模擬交易", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("請你自己在券商 App 照這些欄位送出", text)
        self.assertIn("  委託條件：IOC（立即成交，沒成交的部分取消）", text)
        self.assertIn("--order-no 委託書號", text)

    def test_one_yes_one_ticket(self):
        write(self.orders, {"orders": [reel(), TSMC]})
        self.pass_risk()
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0)
        self.assertIn("委託 1／2，模擬交易", text)
        self.assertIn("委託 2／2，模擬交易", text)
        self.assertIn("買進 台積電 2330（上市），整股 1 張，限價 1,460 ROD，現股當沖", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "是，兩張都買"))
        self.assertEqual(code, 1)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "是，2330"))
        self.assertEqual(code, 1)
        self.assertIn("回覆提到 2330", text)
        self.assertEqual(run(self.args("approve", "--index", "2", "--reply", "是，台積電 1 張"))[0], 0)
        self.assertEqual(run(self.args("show", "--index", "1"))[0], 0)      # #1 is still waiting for its own yes

    def test_an_edited_ticket_is_not_ready(self):
        self.pass_risk()
        doc = read(self.orders)
        doc["orders"][0]["qty"] = 2000
        write(self.orders, doc)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("這一張不在最近一次風險檢查裡", text)

    def test_yesterdays_check_does_not_count(self):
        self.pass_risk(date="2026-10-30")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("風險檢查是 2026-10-30 的，不是今天", text)

    def test_blocked_tickets_are_never_ready(self):
        self.pass_risk(verdicts={1: ("BLOCK", ["時段：09:15 之前不進場（現在 09:10）", "當沖：設定檔沒有開啟現股當沖"])})
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-02 09:10"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED：trader-risk 擋下：時段：09:15 之前不進場（現在 09:10）；當沖：設定檔沒有開啟現股當沖", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "是", now="2026-11-02 09:10"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", err)

    def test_result_needs_a_yes_and_a_no_is_logged(self):
        self.pass_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("沒有記錄到你的 yes", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "不要了"))
        self.assertEqual(code, 0)
        self.assertIn("什麼都沒有送出", text)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE：不要", text)

    def test_skipped_after_the_yes(self):
        self.pass_risk()
        run(self.args("approve", "--index", "1", "--reply", "確認"))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "skipped", "--note", "價格跑掉了"))
        self.assertEqual(code, 0)
        self.assertEqual(read(self.state)["orders"][-1]["status"], "skipped")
        self.assertIn("DONE：沒送出", run(self.args("show", "--index", "1"))[1])
        self.assertIn("今天已送出 0 筆", run(["log", "--state", self.state, "--now", NOW])[1])

    def fill_reel(self):
        self.pass_risk()
        run(self.args("approve", "--index", "1", "--reply", "是"))
        run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "1000",
                      "--filled-price", "110.0", "--portfolio", self.pf))

    def flatten(self, now, *extra, **kw):
        return run(["flatten", "--symbol", kw.get("symbol", "2303"), "--orders", self.orders,
                    "--portfolio", self.pf, "--plan", self.plan, "--config", self.cfg, "--now", now] + list(extra))

    def test_flatten_by_13_20(self):
        self.fill_reel()
        late = "2026-11-02 13:20"
        code, text, _ = self.flatten(late)
        self.assertEqual(code, 0)
        self.assertIn("出清 2303：賣出 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖（計畫 13:20 前出清）", text)
        self.assertIn("取代了先前 1 筆提案", text)
        self.assertIn("先在券商 App 刪掉", text)
        o = self.orders_now()[0]
        self.assertEqual({k: o[k] for k in tw.TICKET_FIELDS if k in o},
                         {"symbol": "2303", "market": "TWSE", "side": "sell", "lot": "board", "qty": 1000,
                          "price_type": "market", "tif": "IOC", "trade": "day_trade"})
        self.assertEqual(read(self.orders)["created_by"], "trader-broker")
        self.pass_risk(kinds={1: "exit"})
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertIn("交易別：現股（賣掉今天買的股票就是現股當沖", text)
        self.assertNotIn("條件單（觸價單）", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "是，賣 1 張", now=late))[0], 0)
        self.assertEqual(read(self.state)["orders"][-1]["kind"], "exit")
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "1000",
                                      "--filled-price", "111.5", "--portfolio", self.pf, now="2026-11-02 13:21"))
        self.assertEqual(code, 0)
        self.assertIn("手續費 NT$158，證交稅 NT$167", text)       # 0.15 % day-trade tax
        self.assertEqual(read(self.pf), {"cash": 301019, "positions": []})
        code, _, err = self.flatten(late)
        self.assertEqual(code, 1)
        self.assertIn("沒有要出清的當沖", err)

    def test_a_stop_before_13_20_and_the_ticket_after(self):
        # 2026-11-03: 4533 stopped out at 10:48; the sell said "13:20 前出清", and at 13:21 the
        # filled ticket showed as BLOCKED (a short without a stop) instead of already sent
        self.fill_reel()
        stop = "2026-11-02 10:48"
        code, text, _ = self.flatten(stop)
        self.assertEqual(code, 0)
        why = self.orders_now()[0]["why"]
        self.assertTrue(why.startswith("提前出場：賣掉今天 聯電 2303"), why)
        self.assertIn("最晚 13:20 前出清", why)
        self.pass_risk(kinds={1: "exit"})
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now=stop))[0], 0)
        run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "1000",
                      "--filled-price", "108.0", "--portfolio", self.pf, now="2026-11-02 10:54"))
        self.pass_risk(verdicts={1: ("BLOCK", ["放空：要賣的 2303 比持有的多（持有 0 股）", "重複：今天已經送出過賣出 2303"])})
        code, text, _ = run(self.args("show", now="2026-11-02 13:21"))
        self.assertEqual(code, 1)
        self.assertIn("DONE：已送出（10:54）", text)
        self.assertNotIn("BLOCKED", text)
        self.assertNotIn("放空", text)

    def test_the_closing_auction_takes_limit_rod_only(self):
        self.fill_reel()
        code, text, _ = self.flatten("2026-11-02 13:26")
        self.assertEqual(code, 0)
        self.assertIn("整股 1 張，限價 100.5 ROD，現股當沖", text)
        self.assertIn("收盤集合競價", text)
        o = self.orders_now()[0]
        self.assertEqual((o["price_type"], o["price"], o["tif"]), ("limit", "100.5", "ROD"))
        write(self.plan, dict(PLAN, limit_down=None))
        self.assertEqual(self.orders_now()[0]["price"], "100.5")
        code, _, _ = self.flatten("2026-11-02 13:27")
        self.assertEqual((code, self.orders_now()[0]["price"]), (0, "100.5"))   # from the prev close
        write(self.plan, {"symbol": "2303"})
        code, _, err = self.flatten("2026-11-02 13:27")
        self.assertEqual(code, 1)
        self.assertIn("跌停價", err)
        code, _, _ = self.flatten("2026-11-02 13:27", "--price", "101.0")
        self.assertEqual((code, self.orders_now()[0]["price"]), (0, "101.0"))

    def test_after_the_close_it_is_too_late(self):
        self.fill_reel()
        code, _, err = self.flatten("2026-11-02 13:31")
        self.assertEqual(code, 1)
        self.assertIn("已經收盤", err)
        self.assertIn("違約交割", err)
        code, _, err = self.flatten("2026-11-01 10:00")
        self.assertIn("休市", err)
        code, _, err = self.flatten("2026-10-23 10:00")
        self.assertIn("薔琵颱風", err)

    def test_odd_lots_bought_today_cannot_be_flattened(self):
        write(self.pf, {"cash": 0, "positions": [{"symbol": "2303", "shares": 350, "lots": [
            {"id": "2303-1", "date": TODAY, "shares": 350, "cost": 38501, "lot": "odd", "trade": "cash"}]}]})
        code, _, err = self.flatten("2026-11-02 13:20")
        self.assertEqual(code, 1)
        self.assertIn("零股不能當沖", err)
        code, _, err = self.flatten("2026-11-02 13:20", "--qty", "350")
        self.assertEqual(code, 1)
        self.assertIn("整股", err)

    def test_flatten_with_the_qty_from_the_app(self):
        code, text, _ = run(["flatten", "--symbol", "6488.TWO", "--qty", "2000", "--orders", self.orders,
                             "--portfolio", os.path.join(self.d, "none"), "--plan", os.path.join(self.d, "none"),
                             "--config", self.cfg, "--now", "2026-11-02 13:20"])
        self.assertEqual(code, 0)
        self.assertIn("賣出 6488（上櫃），整股 2 張，市價 IOC，現股當沖", text)
        self.assertNotIn("先對一下券商 App 裡的庫存", text)
        self.assertEqual(run(["flatten", "--symbol", "2303", "--qty", "0", "--orders", self.orders,
                              "--now", "2026-11-02 13:20"])[0], 1)
        self.assertEqual(run(["flatten", "--symbol", "TSLA", "--qty", "1000", "--orders", self.orders])[0], 1)


class Fills(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "portfolio.json")
        self.cfg = os.path.join(self.d, "config.json")
        write(self.cfg, CFG)
        self.state = os.path.join(self.d, "broker.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def fill(self, *argv):
        return run(["fill"] + list(argv) + ["--portfolio", self.pf, "--config", self.cfg, "--state", self.state])

    def test_a_same_day_sale_relieves_todays_lots_first(self):
        write(self.pf, {"cash": 0, "positions": [{"symbol": "2303", "shares": 2000, "lots": [
            {"id": "2303-1", "date": "2026-10-30", "shares": 1000, "cost": 108154, "lot": "board", "trade": "cash"},
            {"id": "2303-2", "date": TODAY, "shares": 1000, "cost": 110156, "lot": "board", "trade": "day_trade"}]}]})
        code, text, _ = self.fill("--symbol", "2303", "--side", "sell", "--qty", "1000", "--price", "108.5",
                                  "--date", TODAY)
        self.assertEqual(code, 0)
        self.assertIn("手續費 NT$154，證交稅 NT$162", text)
        pf = read(self.pf)
        self.assertEqual(pf["cash"], 108500 - 154 - 162)
        self.assertEqual([l["id"] for l in pf["positions"][0]["lots"]], ["2303-1"])
        self.assertEqual(read(self.state)["orders"][-1]["status"], "filled")
        code, text, _ = self.fill("--symbol", "2303", "--side", "sell", "--qty", "1000", "--price", "108.5",
                                  "--date", TODAY)
        self.assertIn("證交稅 NT$325", text)                          # held from 10-30: 0.3 %, not a day trade
        self.assertEqual(read(self.pf)["positions"], [])

    def test_odd_lots(self):
        code, text, _ = self.fill("--symbol", "2303", "--side", "buy", "--qty", "350", "--price", "110.0",
                                  "--date", TODAY)
        self.assertEqual(code, 0)
        self.assertIn("手續費 NT$54", text)
        pf = read(self.pf)
        self.assertEqual(pf["cash"], 300000 - 38500 - 54)
        self.assertEqual(pf["positions"][0]["lots"][0],
                         {"id": "2303-1", "date": TODAY, "shares": 350, "cost": 38554, "lot": "odd", "trade": "cash"})
        code, _, err = self.fill("--symbol", "2303", "--side", "sell", "--qty", "350", "--price", "111.0",
                                 "--date", TODAY)
        self.assertEqual(code, 1)
        self.assertIn("最快下一個交易日才能賣", err)
        self.assertNotIn("Traceback", err)
        code, text, _ = self.fill("--symbol", "2303", "--side", "sell", "--qty", "350", "--price", "112.0",
                                  "--date", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertIn("手續費 NT$55，證交稅 NT$117", text)
        self.assertEqual(read(self.pf)["cash"], 300000 - 38554 + 39200 - 55 - 117)

    def test_bad_fills(self):
        for argv, msg in ((("--qty", "1500", "--price", "110.0"), "整股"),
                          (("--qty", "1000", "--price", "110.3"), "升降單位"),
                          (("--qty", "0", "--price", "110.0"), "正的"),
                          (("--qty", "1000", "--price", "110.0", "--date", "11/02"), "--date")):
            code, _, err = self.fill("--symbol", "2303", "--side", "buy", *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err, argv)
            self.assertNotIn("Traceback", err)
        code, _, err = self.fill("--symbol", "2303", "--side", "sell", "--qty", "1000", "--price", "110.0")
        self.assertEqual(code, 1)
        self.assertIn("持股只有 0 股", err)

    def test_a_portfolio_needs_a_start(self):
        write(self.cfg, {})
        code, _, err = self.fill("--symbol", "2303", "--side", "buy", "--qty", "1000", "--price", "110.0")
        self.assertEqual(code, 1)
        self.assertIn("budget", err)


if __name__ == "__main__":
    unittest.main()
