"""The order path end to end on the reel: trader-budget -> trader-risk -> trader-broker.

聯電 2303 on 2026-11-02, the plan trader-exit wrote at 09:20, a fresh NT$300,000 paper account.
Each tool runs through its own main(), as Claude runs them, on files in a temporary trader/ folder."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

PLAN = os.path.join(FIX, "trader-budget", "plan-2303.json")
REEL_ORDERS = os.path.join(FIX, "trader-risk", "orders-2303.json")
LISTS = [os.path.join(FIX, "twmarket", "twse-daytrade-2026-11-02.json"),
         os.path.join(FIX, "twmarket", "tpex-daytrade-2026-11-02.json"),
         os.path.join(FIX, "trader-scanner", "twse-punish-2026-11-02.json"),
         os.path.join(FIX, "trader-scanner", "tpex-disposal-2026-11-02.json")]


def run(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue() + err.getvalue()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class OrderPath(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        p = lambda *x: os.path.join(self.d, *x)
        self.cfg, self.orders, self.risk = p("config.json"), p("orders.json"), p("state", "risk.json")
        self.state, self.pf, self.prices = p("state", "broker.json"), p("portfolio.json"), p("prices.json")
        cfg = read(os.path.join(ROOT, "trader-budget", "config.example.json"))
        cfg["day_trade_enabled"] = True        # the user has signed 現股當沖 with the broker
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def budget(self, *extra):
        return run(size.main, ["--plan", PLAN, "--config", self.cfg, "--portfolio", self.pf,
                               "--now", "2026-11-02 09:20", "--write", self.orders] + list(extra))

    def risk_check(self, now):
        argv = ["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf, "--prices", self.prices,
                "--state", self.state, "--out", self.risk, "--now", now]
        for f in LISTS:
            argv += ["--lists", f]
        return run(check.main, argv)

    def broker(self, cmd, now, *extra):
        argv = [cmd, "--orders", self.orders, "--config", self.cfg, "--risk", self.risk, "--state", self.state,
                "--now", now] + list(extra)
        return run(ticket.main, argv)

    def test_one_day_trade_from_plan_to_flat(self):
        # trader-budget: 1 張, bound by the NT$3,000 max loss once the costs are counted
        code, text = self.budget()
        self.assertEqual(code, 0, text)
        self.assertEqual(read(self.orders), read(REEL_ORDERS))

        # trader-risk passes this exact ticket at 09:20
        code, text = self.risk_check("2026-11-02 09:20")
        self.assertEqual(code, 0, text)
        res = read(self.risk)["results"][0]
        self.assertEqual((res["verdict"], res["max_loss"], res["fingerprint"]), ("PASS", 1972.0, "63bf9f525d5603a5"))

        # trader-broker shows it as ready, refuses a vague reply, records an explicit yes
        code, text = self.broker("show", "2026-11-02 09:21")
        self.assertEqual(code, 0, text)
        self.assertIn("READY", text)
        self.assertEqual(self.broker("approve", "2026-11-02 09:21", "--index", "1", "--reply", "好")[0], 1)
        code, text = self.broker("approve", "2026-11-02 09:21", "--index", "1", "--reply", "是，買 1 張聯電")
        self.assertEqual(code, 0, text)

        # the user sends it in their app; it fills at 110.0
        code, text = self.broker("result", "2026-11-02 09:22", "--index", "1", "--status", "placed",
                                 "--filled-qty", "1000", "--filled-price", "110.0", "--portfolio", self.pf)
        self.assertEqual(code, 0, text)
        self.assertEqual(read(self.pf)["cash"], 189844)

        # the same ticket again is a duplicate, and today's NT$1,972 is counted
        code, text = self.risk_check("2026-11-02 09:25")
        self.assertEqual(code, 1, text)
        again = read(self.risk)
        self.assertTrue(any(r.startswith("重複") for r in again["results"][0]["reasons"]), again)
        self.assertEqual(again["today"]["loss_at_risk"], 1972.0)
        self.assertIn("BLOCK", self.broker("show", "2026-11-02 09:25")[1])

        # flat by 13:20: the sell ticket passes as an exit after the 13:00 entry cutoff
        code, text = run(ticket.main, ["flatten", "--symbol", "2303", "--orders", self.orders,
                                       "--portfolio", self.pf, "--plan", PLAN, "--config", self.cfg,
                                       "--now", "2026-11-02 13:20"])
        self.assertEqual(code, 0, text)
        sell = read(self.orders)["orders"][0]
        self.assertEqual((sell["side"], sell["qty"], sell["price_type"], sell["tif"], sell["trade"]),
                         ("sell", 1000, "market", "IOC", "day_trade"))
        code, text = self.risk_check("2026-11-02 13:20")
        self.assertEqual(code, 0, text)
        self.assertEqual(read(self.risk)["results"][0]["kind"], "exit")
        code, text = self.broker("approve", "2026-11-02 13:20", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 0, text)
        code, text = self.broker("result", "2026-11-02 13:21", "--index", "1", "--status", "placed",
                                 "--filled-qty", "1000", "--filled-price", "111.5", "--portfolio", self.pf)
        self.assertEqual(code, 0, text)
        pf = read(self.pf)
        self.assertEqual(pf["cash"], 301019)          # + 111,500 - fee 158 - day-trade tax 167
        self.assertEqual(pf["positions"], [])

    def test_an_edited_ticket_needs_a_new_check(self):
        self.assertEqual(self.budget()[0], 0)
        self.assertEqual(self.risk_check("2026-11-02 09:20")[0], 0)
        doc = read(self.orders)
        doc["orders"][0]["qty"] = 2000
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        code, text = self.broker("show", "2026-11-02 09:21")
        self.assertEqual(code, 1, text)
        self.assertIn("NOT READY", text)
        self.assertEqual(self.broker("approve", "2026-11-02 09:21", "--index", "1", "--reply", "yes")[0], 1)
        code, text = self.risk_check("2026-11-02 09:21")      # 2 張 lose NT$3,947 at the stop
        self.assertEqual(code, 1, text)
        self.assertTrue(any(r.startswith("虧損") for r in read(self.risk)["results"][0]["reasons"]))

    def test_without_the_day_trade_agreement_nothing_is_ready(self):
        cfg = read(self.cfg)
        cfg["day_trade_enabled"] = False
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        self.assertEqual(self.budget()[0], 0)                 # it still sizes, with a note
        self.assertEqual(self.risk_check("2026-11-02 09:20")[0], 1)
        self.assertIn("BLOCK", self.broker("show", "2026-11-02 09:21")[1])


if __name__ == "__main__":
    unittest.main()
