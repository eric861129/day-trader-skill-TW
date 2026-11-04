import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")
tw = load("trader-budget", "twmarket")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-2303.json")
PRICES = os.path.join(FIX, "trader-budget", "prices-2026-11-02.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-2303.json")
NOW = "2026-11-02 09:20"
TODAY = "2026-11-02"
# the contract's config, key for key
CONTRACT_CFG = {
    "budget": 300000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_twd": 250000,
    "min_order_twd": 1000, "max_loss_per_trade_twd": 3000, "max_daily_loss_twd": 9000,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "min_reward_risk_net": None,
    "require_stop": True, "no_entries_before": "09:15", "no_entries_after": "13:00", "flat_by": "13:20",
    "day_trade_enabled": False, "day_trade_quota_twd": None, "allow_overnight": False,
    "allow_disposition": False, "fee_discount": 1, "fee_min_twd": 20, "fee_min_odd_twd": 1,
    "extra_closed": {}, "broker": "manual", "broker_name": "", "paper": True, "blocked": [], "allow_options": False,
    "allow_margin": False, "allow_short": False,
}
# the example split on 2026-11-02's closes: NT$300,000, 10 % kept in cash
SPLIT = {"0050": 1041, "2330": 34, "2303": 372, "6488": 54}


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cfg(**over):
    c = load_json(CFG)
    c["day_trade_enabled"] = True        # the reel: the user has signed 現股當沖 with the broker
    c.update(over)
    return c


def plan(**over):
    p = load_json(PLAN)
    p.update(over)
    return p


def sized(**kw):
    over = kw.pop("cfg", {})
    p = kw.pop("plan", None) or plan()
    return size.size_trade(p, cfg(**over), today=kw.pop("today", TODAY), **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def prices_doc(price, quote_time="2026-11-02T01:30:00Z"):
    """09:25's 5-minute bar closed at 110.5: the quote at 09:30 Taipei (01:30 UTC)."""
    return {"as_of": quote_time, "source": "yahoo", "prices": {"2303": price},
            "detail": {"2303": {"prev_close": 111.5, "time": quote_time, "source": "yahoo"}}}


def limits(s):
    return [(l["id"], l["units"]) for l in s["limits"]]


class TheReel(unittest.TestCase):
    """NT$300,000, 10 % cash, NT$3,000 max loss; 聯電 risk 1.5 -> 1 張, NT$1,972 at the stop."""

    def test_config_example_is_the_contract(self):
        self.assertEqual(load_json(CFG), CONTRACT_CFG)

    def test_one_lot_bound_by_the_max_loss(self):
        s = sized()
        self.assertEqual((s["units"], s["qty"], s["lot"], s["bound_by"]), (1, 1000, "board", "max_loss"))
        self.assertEqual((s["entry"], s["stop"], s["target"]), (110.0, 108.5, 111.5))
        self.assertEqual((s["risk_per_share"], s["risk_ticks"]), (1.5, 3))
        self.assertEqual((s["value"], s["buy_fee"], s["cash_needed"]), (110000.0, 156, 110156.0))
        self.assertEqual((s["max_loss"], s["move_at_stop"], s["costs_at_stop"]), (1972.0, 1500.0, 472))
        self.assertEqual((s["reward_risk"], s["reward_risk_net"]), (1.0, 0.52))
        self.assertEqual((s["cash_buffer"], s["cash_available"]), (30000.0, 270000.0))
        self.assertEqual(limits(s), [("max_loss", 1), ("position_cap", 2), ("max_order", 2), ("cash", 2)])
        self.assertEqual([l["id"] for l in s["limits"] if l["binds"]], ["max_loss"])
        self.assertEqual((s["price_type"], s["tif"], s["trade"], s["flat_by"]), ("market", "IOC", "day_trade", "13:20"))
        self.assertIsNone(s["refusal"])

    def test_the_costs_take_the_second_lot(self):
        s = sized()
        self.assertEqual(s["no_cost_units"], 2)               # 2 x 1,500 = 3,000 fits without costs
        self.assertEqual(tw.loss_at_stop(110.0, 108.5, 2000, "2303", True, None,
                                         tw.roc_date("1151102"))["total"], 3947.0)
        self.assertEqual(sized(cfg={"max_loss_per_trade_twd": 3947})["units"], 2)
        self.assertEqual(sized(cfg={"max_loss_per_trade_twd": 3946})["units"], 1)
        text = size.plan_report(s, size.ticket(s, cfg()), cfg=cfg())
        self.assertIn("不算成本的話，單筆最大虧損可以買 2 張；算進手續費和證交稅只剩 1 張。", text)

    def test_the_ticket_is_the_contract_fixture(self):
        self.assertEqual(size.ticket(sized(), cfg()), load_json(ORDERS)["orders"][0])

    def test_the_fingerprint(self):
        self.assertEqual(tw.fingerprint(size.ticket(sized(), cfg())), "63bf9f525d5603a5")

    def test_cli_writes_the_contract_document(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            write_json(c, cfg())
            out = os.path.join(d, "trader", "orders.json")
            code, text, err = run(["--plan", PLAN, "--config", c, "--portfolio", os.path.join(d, "none"),
                                   "--now", NOW, "--write", out])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(load_json(out), load_json(ORDERS))
            for line in ("1 張 聯電 2303 = NT$110,000，價格 110.0，買進手續費 NT$156",
                         "停損出場最多虧 NT$1,972：價差 NT$1,500，加手續費和證交稅 NT$472",
                         "單筆最大虧損 NT$3,000：1 張停損出場約虧 NT$1,972（價差 NT$1,500 + 成本 NT$472）  1 張  <- 決定張數",
                         "每股風險 1.5（3 檔）。報酬風險比 1.00，扣成本後 0.52。",
                         "買進 聯電 2303（上市） 整股 1 張，市價，IOC，現股當沖",
                         "停損 108.5、停利 111.5：在券商 App 設條件單",
                         "13:20 前出清",
                         "不構成投資建議"):
                self.assertIn(line, text)
            self.assertNotIn("day_trade_enabled", text)

    def test_day_trading_not_enabled_still_writes_with_a_note(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("\"day_trade_enabled\" 是 false：trader-risk 會擋下這張當沖單", text)
            self.assertEqual(load_json(out)["orders"], load_json(ORDERS)["orders"])

    def test_cli_json(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["sizing"]["qty"], out["sizing"]["max_loss"]), (1000, 1972.0))
        self.assertEqual(out["orders"][0]["qty"], 1000)


class Limits(unittest.TestCase):
    def test_each_limit_can_bind(self):
        wide = {"max_loss_per_trade_twd": 10000}
        s = sized(cfg=wide)
        self.assertEqual((s["units"], s["bound_by"]), (2, "position_cap"))
        self.assertEqual(limits(s)[0], ("max_loss", 5))
        self.assertEqual(sized(cfg=dict(wide, max_order_twd=150000))["bound_by"], "max_order")
        s = sized(cfg=dict(wide, max_position_pct=40))
        self.assertEqual((s["units"], s["bound_by"]), (1, "position_cap"))
        s = sized(cfg=dict(wide, day_trade_quota_twd=150000))
        self.assertEqual((s["units"], s["bound_by"]), (1, "quota"))
        s = sized(cfg=wide, cash=200000)
        self.assertEqual((s["units"], s["bound_by"], s["cash_available"]), (1, "cash", 170000.0))

    def test_the_cash_counts_the_buy_fee(self):
        # 110,000 fits 110,000 of cash, but the buy fee (156) does not
        s = sized(cfg={"max_loss_per_trade_twd": 10000}, cash=140000)
        self.assertEqual((s["units"], s["bound_by"], s["cash_available"]), (0, "cash", 110000.0))
        self.assertIn("一張 聯電 2303 要 NT$110,156（含手續費），超過你的可用現金", s["refusal"])
        s = sized(cfg={"max_loss_per_trade_twd": 10000}, cash=140156)
        self.assertEqual(s["units"], 1)

    def test_the_fee_discount(self):
        s = sized(cfg={"fee_discount": 0.6})
        self.assertEqual((s["costs_at_stop"], s["max_loss"]), (94 + 92 + 162, 1848.0))

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf, c = os.path.join(d, "portfolio.json"), os.path.join(d, "config.json")
            write_json(pf, {"cash": 200000, "positions": []})
            write_json(c, cfg(max_loss_per_trade_twd=10000))
            out = json.loads(run(["--plan", PLAN, "--config", c, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["units"], out["sizing"]["bound_by"]), (1, "cash"))


class Orders(unittest.TestCase):
    def test_a_limit_at_the_plans_entry(self):
        s = sized(limit=110.0)
        o = size.ticket(s, cfg())
        self.assertEqual((o["price_type"], o["price"], o["tif"], o["qty"]), ("limit", "110.0", "ROD", 1000))
        self.assertEqual(s["entry_from"], "plan_limit")
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(json.loads(text)["orders"][0]["price"], "110.0")

    def test_a_lower_limit_risks_less(self):
        s = sized(limit=109.5)
        self.assertEqual((s["units"], s["risk_per_share"], s["max_loss"]), (2, 1.0, 2946.0))
        self.assertEqual((s["reward_risk"], s["reward_risk_net"]), (2.0, 1.03))
        self.assertEqual(size.ticket(s, cfg())["price"], "109.5")

    def test_a_limit_off_the_tick_grid_or_outside_the_limits(self):
        with self.assertRaises(size.SizeError) as e:
            sized(limit=109.3)
        self.assertIn("最接近的是 109.0 或 109.5", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            sized(limit=123.0)
        self.assertIn("超出今天的漲跌停 122.5 / 100.5", str(e.exception))
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--limit", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("--limit", err)

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = sized(price=110.5)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"], s["risk_ticks"]), (110.5, "price", 2.0, 4))
        self.assertIn("報酬風險比 0.50（停利 111.5、進場 110.5、停損 108.5）低於你的最低要求 1", s["refusal"])
        self.assertIsNone(size.ticket(s, cfg()))
        s = sized(price=110.5, cfg={"min_reward_risk": 0})
        self.assertEqual((s["units"], s["max_loss"], s["costs_at_stop"], s["reward_risk_net"]), (1, 2473.0, 473, 0.21))
        s = sized(price=110.3, cfg={"min_reward_risk": 0})       # off the grid: snapped up
        self.assertEqual(s["entry"], 110.5)
        lower = sized(price=109.5)
        self.assertEqual((lower["entry"], lower["entry_from"], lower["units"]), (110.0, "plan", 1))

    def test_a_fresh_price_at_the_stop_is_refused(self):
        # 2026-11-04 11:46: 7780 bid 14.75 under its 14.90 stop was sized from the 15.05 entry
        for p in (108.5, 108.0):
            with self.assertRaises(size.SizeError) as e:
                sized(price=p)
            self.assertIn("已經跌到停損 108.5：計畫失效", str(e.exception))
        self.assertEqual(sized(price=109.0)["entry_from"], "plan")              # above the stop: as before

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            write_json(pp, prices_doc(110.5))
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--now", "2026-11-02 09:31"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["entry"], out["orders"]), (1, 110.5, []))
            self.assertIn("高於計畫的進場價 110.0", out["notes"][0])
            code, text, _ = run(base + ["--now", "2026-11-02 10:00"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["entry"], out["sizing"]["qty"]), (0, 110.0, 1000))
            self.assertIn("沒有採用", out["notes"][0])
            # a limit order ignores the price file
            out = json.loads(run(base + ["--now", "2026-11-02 09:31", "--limit"])[1])
            self.assertEqual((out["sizing"]["entry"], out["notes"]), (110.0, []))


class OddLots(unittest.TestCase):
    def test_an_odd_lot_buy_is_held_overnight(self):
        s = sized(odd_lot=True)
        self.assertEqual((s["lot"], s["qty"], s["bound_by"], s["trade"]), ("odd", 999, "odd_max", "cash"))
        self.assertEqual(limits(s)[0], ("max_loss", 1404))
        self.assertEqual(limits(s)[-1], ("odd_max", 999))
        self.assertEqual((s["value"], s["buy_fee"], s["max_loss"], s["costs_at_stop"]), (109890.0, 156, 2133.5, 635))
        self.assertIsNone(s["flat_by"])
        o = size.ticket(s, cfg())
        self.assertEqual({k: o[k] for k in ("lot", "qty", "price_type", "price", "tif", "trade")},
                         {"lot": "odd", "qty": 999, "price_type": "limit", "price": "110.0", "tif": "ROD",
                          "trade": "cash"})
        self.assertEqual(o["plan"]["hold"], "overnight")
        self.assertNotIn("flat_by", o["plan"])
        self.assertTrue(o["why"].endswith("；零股不能當沖，最快下一個交易日才能賣）"))

    def test_odd_lots_pay_the_full_tax_and_the_odd_fee_minimum(self):
        s = sized(odd_lot=True, cfg={"max_loss_per_trade_twd": 1500})
        # 702 shares: move 1,053 + buy fee 110 + sell fee 108 + tax 0.3 % 228
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"], s["costs_at_stop"]), (702, "max_loss", 1499.0, 446))
        # 1 share: 1.5 + 1 + 1 + 0 tax = 3.5; 2 shares: 3 + 1 + 1 + 0 = 5
        self.assertEqual(sized(odd_lot=True, cfg={"max_loss_per_trade_twd": 4})["qty"], 1)
        self.assertEqual(sized(odd_lot=True, cfg={"max_loss_per_trade_twd": 3})["qty"], 0)

    def test_the_report_warns(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--odd-lot"])
        self.assertEqual(code, 0)
        for line in ("零股不是當沖：今天買的零股最快下一個交易日才能賣",
                     "隔天開盤可能直接跳空跌破 108.5",
                     "盤中零股只能限價 ROD",
                     "\"allow_overnight\" 是 true",
                     "買進 聯電 2303（上市） 盤中零股 999 股，限價 110.0，ROD，現股（留倉）",
                     "<- 決定股數"):
            self.assertIn(line, text)
        self.assertNotIn("13:20 前出清", text)


class Refusals(unittest.TestCase):
    def test_zero_lots_is_a_plain_refusal(self):
        s = sized(cfg={"max_loss_per_trade_twd": 1500})
        self.assertEqual((s["units"], s["bound_by"]), (0, "max_loss"))
        self.assertIn("一張停損出場約虧 NT$1,972（價差 NT$1,500 + 手續費和證交稅 NT$472），超過你的單筆最大虧損 "
                      "NT$1,500", s["refusal"])
        self.assertIn("--odd-lot", s["refusal"])
        self.assertIsNone(size.ticket(s, cfg()))
        with tempfile.TemporaryDirectory() as d:
            c, out = os.path.join(d, "config.json"), os.path.join(d, "orders.json")
            write_json(c, cfg(max_loss_per_trade_twd=1500))
            code, text, _ = run(["--plan", PLAN, "--config", c, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("不下單：", text)
            self.assertFalse(os.path.exists(out))

    def test_reward_risk_gross_and_net(self):
        s = sized(plan=plan(target=111.0))
        self.assertIn("報酬風險比 0.67", s["refusal"])
        self.assertIsNone(size.ticket(s, cfg()))
        s = sized(cfg={"min_reward_risk_net": 0.6})
        self.assertIn("扣成本後的報酬風險比 0.52 低於你的最低要求 0.6", s["refusal"])
        self.assertIsNone(sized(cfg={"min_reward_risk_net": 0.5})["refusal"])

    def test_under_the_minimum_order(self):
        s = sized(cfg={"min_order_twd": 200000})
        self.assertEqual(s["units"], 1)
        self.assertIn("1 張 聯電 2303 只有 NT$110,000，低於你的最低單筆金額 NT$200,000", s["refusal"])

    def test_bad_plans(self):
        for p, word in ((plan(stop=110.0), "不在進場價 110.0 下方"),
                        (plan(target=110.0), "行情已經走完了"),
                        (plan(target=None), "大於零的停利"),
                        (plan(side="sell"), "只算做多"),
                        (plan(symbol=""), "沒有代號"),
                        (plan(symbol="TSLA"), "TSLA"),
                        (plan(entry=122.5, stop=121.0, target=125.0, limit_up=122.5), "已經到漲停 122.5"),
                        (plan(stop=100.0), "在今天的跌停價 100.5 下方")):
            with self.assertRaises(size.SizeError, msg=word) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            sized(cfg={"blocked": ["2303.TW"]})
        self.assertIn("封鎖清單", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            sized(today="2026-11-03")
        self.assertIn("用 trader-exit 重新規劃", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            sized(cfg={"budget": 0})
        self.assertIn("沒有預算", str(e.exception))

    def test_cli_errors(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("找不到計畫", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--now", "2026-11-03 09:20"])
        self.assertEqual(code, 1)
        self.assertIn("2026-11-03", err)


class WeightsMode(unittest.TestCase):
    """The example split: 0050 40, 2330 30, 2303 15, 6488 15 on 2026-11-02's closes."""

    def result(self, **over):
        prices, info = size.load_prices(PRICES)
        rows = size.parse_weights(load_json(TARGETS))
        return size.size(rows, cfg(**over), prices=prices, info=info)

    def shares(self, r):
        return {p["symbol"]: p["shares"] for p in r["positions"]}

    def test_the_example_split(self):
        r = self.result()
        self.assertEqual(self.shares(r), SPLIT)
        self.assertEqual((r["investable"], r["invested"], r["fees"], r["idle"]), (270000.0, 269548.1, 382, 69.9))
        self.assertAlmostEqual(r["invested"] + r["fees"] + r["idle"], r["investable"], places=2)
        first = r["positions"][0]
        self.assertEqual((first["symbol"], first["lots"], first["odd"], first["value"], first["fees"]),
                         ("0050", 1, 41, 108368.1, 154))
        self.assertEqual([p["name"] for p in r["positions"]], ["元大台灣50", "台積電", "聯電", "環球晶"])

    def test_inline_weights_match_the_file(self):
        prices, info = size.load_prices(PRICES)
        rows = size.parse_weights(size.weights_from_arg("0050=40,2330=30,2303.TW=15,6488.TWO=15"))
        self.assertEqual(self.shares(size.size(rows, cfg(), prices=prices, info=info)), SPLIT)
        r = size.size(size.parse_weights({"0050": 4, "2330": 3, "2303": 1.5, "6488": 1.5}), cfg(), prices=prices)
        self.assertEqual({p["symbol"]: p["weight"] for p in r["positions"]},
                         {"0050": 40.0, "2330": 30.0, "2303": 15.0, "6488": 15.0})

    def test_orders_are_a_board_ticket_and_an_odd_ticket(self):
        orders = size.orders_from(self.result(), "範例")
        self.assertEqual([(o["symbol"], o["lot"], o["qty"], o["price"]) for o in orders],
                         [("0050", "board", 1000, "104.10"), ("0050", "odd", 41, "104.10"),
                          ("2330", "odd", 34, "2330"), ("2303", "odd", 372, "110.0"), ("6488", "odd", 54, "760")])
        for o in orders:
            self.assertEqual((o["side"], o["price_type"], o["tif"], o["trade"]), ("buy", "limit", "ROD", "cash"))
            self.assertNotIn("stop", o)
            self.assertTrue(set(o) <= set(tw.TICKET_FIELDS) | {"name", "why"})
        self.assertEqual(orders[4]["market"], "TPEx")
        self.assertIn("盤中零股", orders[1]["why"])

    def test_the_cap_redistributes_until_nobody_is_over(self):
        rows = [{"symbol": s, "weight": w} for s, w in (("A", 60), ("B", 30), ("C", 10))]
        alloc, capped = size.allocate(rows, 1000, 400)
        self.assertEqual({k: round(v, 6) for k, v in alloc.items()}, {"A": 400, "B": 400, "C": 200})
        self.assertEqual(capped, {"A", "B"})

    def test_everything_capped_leaves_money_idle(self):
        prices, _ = size.load_prices(PRICES)
        r = size.size(size.parse_weights({"0050": 50, "2303": 50}), cfg(max_position_pct=25), prices=prices)
        self.assertTrue(all(p["capped"] for p in r["positions"]))
        self.assertTrue(all(p["value"] <= 75000 for p in r["positions"]))
        self.assertGreater(r["idle"], 100000)
        self.assertIn("每一檔都到上限了", size.report(r, size.orders_from(r)))

    def test_small_slices_are_dropped_and_shared_out(self):
        r = self.result(min_order_twd=45000)
        self.assertEqual([d["symbol"] for d in r["dropped"]], ["2303"])
        self.assertIn("低於你的最低單筆金額", r["dropped"][0]["reason"])
        self.assertNotIn("2303", self.shares(r))
        prices, _ = size.load_prices(PRICES)
        r = size.size(size.parse_weights({"0050": 90, "2330": 10}), cfg(budget=20000, cash_buffer_pct=0, max_position_pct=100),
                      prices=prices)
        self.assertEqual(r["dropped"][0]["symbol"], "2330")
        self.assertIn("連一股都買不起", r["dropped"][0]["reason"])

    def test_blocked_and_top(self):
        r = self.result(blocked=["2330"])
        self.assertNotIn("2330", self.shares(r))
        self.assertIn("封鎖清單", r["dropped"][0]["reason"])
        prices, _ = size.load_prices(PRICES)
        r = size.size(size.parse_weights(load_json(TARGETS)), cfg(), prices=prices, top=2)
        self.assertEqual(sorted(self.shares(r)), ["0050", "2330"])

    def test_errors(self):
        rows = size.parse_weights(load_json(TARGETS))
        with self.assertRaises(size.SizeError) as e:
            size.size(rows, cfg(), prices={"0050": 104.1})
        self.assertIn("沒有價格", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size(rows, cfg(budget=0), prices=size.load_prices(PRICES)[0])
        with self.assertRaises(size.SizeError) as e:
            size.normalise(size.parse_weights({"2303": 1, "2303.TW": 2}))
        self.assertIn("出現兩次", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.parse_weights({"TSLA": 10})
        with self.assertRaises(size.SizeError):
            size.weights_from_arg("0050")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            base = ["--targets", TARGETS, "--prices", PRICES, "--config", CFG, "--universe", ""]
            code, text, _ = run(base + ["--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            for line in ("閒置 NT$70", "require_stop", "allow_overnight", "買進 環球晶 6488（上櫃） 盤中零股 54 股，限價 760，ROD",
                         "不構成投資建議"):
                self.assertIn(line, text)
            doc = load_json(out)
            self.assertEqual(doc["created_at"], "2026-11-02T01:20:00Z")
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["0050", "0050", "2330", "2303", "6488"])
            self.assertEqual(json.loads(run(base + ["--json"])[1])["result"]["idle"], 69.9)
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", CFG])
            self.assertEqual(code, 1)
            self.assertIn("找不到權重檔", err)
            code, _, err = run(base + ["--odd-lot"])
            self.assertEqual(code, 1)
            self.assertIn("權重模式", err)


class Files(unittest.TestCase):
    def test_no_em_dashes_and_chinese_output(self):
        folder = os.path.join(ROOT, "trader-budget")
        for f in os.listdir(folder) + [os.path.join("..", "tests", "test_budget.py")]:
            path = os.path.join(folder, f)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn(chr(0x2014), fh.read(), f)
        with open(os.path.join(folder, "size.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("import twmarket", src)
        for us in ("eastern_offset", "New York", "Alpaca", "bracket", "notional"):
            self.assertNotIn(us, src, us)


if __name__ == "__main__":
    unittest.main()
