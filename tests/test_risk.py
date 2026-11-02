import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

check = load("trader-risk", "check")
tw = check.tw

ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-2303.json")
LIST_FILES = [os.path.join(FIX, "twmarket", "twse-daytrade-2026-11-02.json"),
              os.path.join(FIX, "twmarket", "tpex-daytrade-2026-11-02.json"),
              os.path.join(FIX, "trader-scanner", "twse-punish-2026-11-02.json"),
              os.path.join(FIX, "trader-scanner", "tpex-disposal-2026-11-02.json")]
NOW = "2026-11-02 09:20"
TODAY = "2026-11-02"
PRICE_TIME = datetime.datetime(2026, 11, 2, 1, 30)    # the 09:25 bar closed at 110.5 (09:30 Taipei)
# the reel config: trader-budget/config.example.json with "day_trade_enabled": true
CFG = {"budget": 300000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_twd": 250000,
       "min_order_twd": 1000, "max_loss_per_trade_twd": 3000, "max_daily_loss_twd": 9000, "max_trades_per_day": 3,
       "max_orders_per_day": 10, "min_reward_risk": 1.0, "min_reward_risk_net": None, "require_stop": True,
       "no_entries_before": "09:15", "no_entries_after": "13:00", "flat_by": "13:20", "day_trade_enabled": True,
       "day_trade_quota_twd": None, "allow_overnight": False, "allow_disposition": False, "fee_discount": 1,
       "fee_min_twd": 20, "fee_min_odd_twd": 1, "extra_closed": {}, "broker_name": "", "paper": True, "blocked": [],
       "allow_options": False, "allow_margin": False, "allow_short": False}
LISTS = check.read_lists(LIST_FILES)


def at(text):
    return tw.parse_now(text)


def cfg(**over):
    c = dict(CFG)
    c.update(over)
    return c


def reel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return {k: v for k, v in o.items() if v is not None}


def ticket(sym, entry, stop, target, qty=1000, **over):
    o = {"symbol": sym, "market": "TWSE", "side": "buy", "lot": "board", "qty": qty, "price_type": "market",
         "tif": "IOC", "trade": "day_trade", "stop": tw.price_text(stop, sym), "target": tw.price_text(target, sym),
         "plan": {"entry": entry, "stop": stop, "target": target}}
    o.update(over)
    return {k: v for k, v in o.items() if v is not None}


def run(orders, now=NOW, lists=LISTS, **kw):
    c = kw.pop("config", None) or cfg()
    return check.check(orders, c, now=at(now), lists=lists, **kw)


def placed(sym, side="buy", day=TODAY, max_loss=1972.0, kind=None, status="placed", trade="day_trade",
           value=110000.0):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:21:00+08:00", "trade": trade,
         "value_twd": value}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheReel(unittest.TestCase):
    def test_the_2303_ticket_passes_every_check(self):
        res = run([reel()])
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(res["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"], r["stop"], r["target"]),
                         ("entry", 110.0, "plan", 108.5, 111.5))
        self.assertEqual((r["risk_per_share"], r["risk_ticks"], r["max_loss"], r["costs_at_stop"]),
                         (1.5, 3, 1972.0, 472))
        self.assertEqual((r["reward_risk"], r["reward_risk_net"]), (1.0, 0.52))
        self.assertEqual((r["value_twd"], r["amount"], r["qty"], r["name"]), (110000.0, "1 張", 1000, "聯電"))
        self.assertEqual(r["fingerprint"], "63bf9f525d5603a5")
        self.assertEqual(res["cash_after"], 189844.0)
        self.assertEqual((res["date"], res["now_taipei"]), (TODAY, NOW))
        self.assertIs(res["paper"], True)
        self.assertEqual(res["lists"], {"disposition_checked": True, "daytrade_checked": True,
                                        "daytrade_date": "2026-11-02"})

    def test_two_lots_are_blocked_by_the_max_loss_only(self):
        res = run([reel(qty=2000)])
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["虧損：2 張 × 每股風險 1.5 = NT$3,000，加手續費和證交稅 NT$947，停損出場約虧 NT$3,947，"
                          "超過你的單筆最大虧損 NT$3,000"])

    def test_the_fingerprint_covers_the_ticket_not_the_notes(self):
        o = reel()
        self.assertEqual(tw.fingerprint(o), "63bf9f525d5603a5")
        self.assertEqual(tw.fingerprint(dict(o, why="anything", plan={}, name="")), tw.fingerprint(o))
        self.assertNotEqual(tw.fingerprint(dict(o, qty=2000)), tw.fingerprint(o))
        self.assertNotEqual(tw.fingerprint(dict(o, stop="108.0")), tw.fingerprint(o))
        self.assertNotEqual(tw.fingerprint(dict(o, tif="ROD")), tw.fingerprint(o))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w", encoding="utf-8") as fh:
                json.dump(CFG, fh)
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            args = ["--orders", ORDERS_PATH, "--config", c, "--portfolio", none, "--prices", none,
                    "--state", os.path.join(d, "state", "broker.json"), "--now", NOW, "--out", out]
            for f in LIST_FILES:
                args += ["--lists", f]
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(args)
            text = buf.getvalue()
            self.assertEqual(code, 0)
            for line in ("PASS  #1 買進 聯電 2303 1 張（整股，市價 IOC，現股當沖），進場",
                         "進場 110.0（計畫進場價）、停損 108.5、停利 111.5：每股風險 1.5（3 檔）",
                         "停損出場約虧 NT$1,972（價差 NT$1,500 + 手續費和證交稅 NT$472；上限 NT$3,000）",
                         "報酬風險比 1.00（扣成本 0.52；最低 1）",
                         "今天在這之前：0/3 筆，單日虧損 NT$0/NT$9,000",
                         "通過 1 張、擋下 0 張（台北時間 2026-11-02 09:20）。通過的單都成交後現金剩 NT$189,844（模擬帳戶）。",
                         "不構成投資建議"):
                self.assertIn(line, text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], tw.fingerprint(reel()))
            self.assertEqual(rec["orders_file"], ORDERS_PATH)
            self.assertIn("checked_at", rec)
            buf = io.StringIO()
            with redirect_stdout(buf):
                check.main(args + ["--json", "--no-write"])
            self.assertEqual(json.loads(buf.getvalue())["results"][0]["max_loss"], 1972.0)


class DayTradeGates(unittest.TestCase):
    def test_day_trading_must_be_switched_on(self):
        res = run([reel()], config=cfg(day_trade_enabled=False))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("當沖：設定檔沒有開啟現股當沖（\"day_trade_enabled\": false）", reasons(res))
        self.assertIn("違約交割", reasons(res))

    def test_a_disposition_stock_is_blocked(self):
        o = ticket("4526", 55.5, 55.0, 56.5, name="東台")
        res = run([o])
        why = reasons(res)
        self.assertIn("處置股：4526 東台 是處置股（到 2026-11-09，每 2 分鐘撮合一次，要預收款券）；不進場", why)
        self.assertIn("當沖：4526 不在現股當沖標的清單上（2026-11-02 的清單）", why)
        res = run([o], config=cfg(allow_disposition=True))
        self.assertIn("處置股：", warnings(res))
        self.assertNotIn("處置股：", reasons(res))
        self.assertIn("當沖：4526 不在", reasons(res))       # still not on the day-trade list

    def test_a_stock_off_the_day_trade_list(self):
        res = run([ticket("1213", 20.0, 19.8, 20.4)])
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("當沖：1213 不在現股當沖標的清單上", reasons(res))
        self.assertEqual(LISTS["daytrade"]["codes"]["2303"]["name"], "聯電")
        self.assertEqual(len(LISTS["daytrade"]["codes"]), 2080)

    def test_sell_first_where_it_is_suspended(self):
        short = ticket("3701", 20.0, 20.4, 19.6, side="sell")
        res = run([short], config=cfg(allow_short=True))
        self.assertIn("當沖：3701 暫停先賣後買", reasons(res))
        self.assertFalse(LISTS["daytrade"]["codes"]["3701"]["sell_first"])

    def test_the_day_trade_quota(self):
        st = {"orders": [placed("2330", value=110000.0)]}
        res = run([reel()], config=cfg(day_trade_quota_twd=200000), state=st)
        self.assertIn("當沖額度：今天已用 NT$110,000，加這張 NT$110,000 = NT$220,000，超過你的當沖額度 NT$200,000",
                      reasons(res))
        self.assertEqual(verdicts(run([reel()], config=cfg(day_trade_quota_twd=220000), state=st)), ["PASS"])
        res = run([reel(), dict(reel(), symbol="2303.TW")], config=cfg(day_trade_quota_twd=200000))
        self.assertIn("當沖額度：今天已用 NT$110,000", reasons(res, 1))

    def test_without_lists_it_warns(self):
        res = run([reel()], lists=None)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("清單：沒有處置股和現股當沖標的清單，沒有確認能不能當沖／是不是處置股", warnings(res))
        res = run([reel()], lists={"disposition": LISTS["disposition"], "daytrade": None})
        self.assertEqual(warnings(res), "清單：沒有現股當沖標的清單，沒有確認能不能當沖")
        res = run([reel()], lists={"disposition": None, "daytrade": LISTS["daytrade"]})
        self.assertEqual(warnings(res), "清單：沒有處置股清單，沒有確認是不是處置股")

    def test_an_old_day_trade_list_warns(self):
        res = run([reel(plan=dict(reel()["plan"]))], now="2026-11-05 09:20")
        self.assertIn("現股當沖標的清單是 2026-11-02 的，比前一個交易日（2026-11-04）還舊", " ".join(res["warnings"]))
        res = run([reel()], now="2026-11-03 09:20")
        self.assertEqual(res["warnings"], [])

    def test_overnight_buys_need_allow_overnight(self):
        odd = reel(lot="odd", qty=300, price_type="limit", price="110.0", tif="ROD", trade="cash")
        res = run([odd])
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("留倉：這張是現股買進（不是當沖）。今天買的零股最快下一個交易日才能賣", reasons(res))
        self.assertIn("賣出證交稅 0.3%", reasons(res))
        res = run([odd], config=cfg(allow_overnight=True))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["max_loss"], r["costs_at_stop"], r["amount"]), (640.0, 190, "300 股"))  # tax 0.3 %, NT$1 fee minimum
        board = reel(trade="cash")
        self.assertIn("今天買的股票最快下一個交易日才能賣", reasons(run([board])))
        etf = ticket("0050", 104.1, 103.0, 106.0, trade="cash", price_type="limit", price="104.10", tif="ROD")
        self.assertIn("賣出證交稅 0.1%", reasons(run([etf])))


class Format(unittest.TestCase):
    def test_lots_and_quantities(self):
        self.assertIn("格式：整股的 qty 要是 1000 的倍數，1500 股不是", reasons(run([reel(qty=1500)])))
        odd = reel(lot="odd", qty=1000, price_type="limit", price="110.0", tif="ROD", trade="cash")
        self.assertIn("格式：零股一次 1 到 999 股", reasons(run([odd])))
        self.assertIn("格式：qty 要是正整數股數", reasons(run([reel(qty="1.5")])))
        self.assertIn("格式：lot 要是 board（整股）或 odd（零股）", reasons(run([reel(lot="x")])))
        self.assertEqual(verdicts(run([reel(qty="1000")])), ["PASS"])

    def test_odd_lots_are_limit_rod_and_never_day_trades(self):
        odd = reel(lot="odd", qty=300, price_type="market", tif="IOC", trade="day_trade")
        why = reasons(run([odd]))
        self.assertIn("格式：盤中零股只接受限價 ROD", why)
        self.assertIn("格式：零股不能當沖：今天買的零股最快下一個交易日才能賣", why)

    def test_prices_types_and_fields(self):
        self.assertIn("格式：限價單要有 price", reasons(run([reel(price_type="limit", tif="ROD")])))
        self.assertIn("格式：限價 110.3 不在升降單位上（這個價位每檔 0.5）",
                      reasons(run([reel(price_type="limit", price="110.3", tif="ROD")])))
        self.assertIn("格式：市價單不帶 price", reasons(run([reel(price="110.0")])))
        self.assertIn("格式：tif 要是 ROD、IOC 或 FOK", reasons(run([reel(tif="GTC")])))
        self.assertIn("格式：trade 要是 day_trade（現股當沖）或 cash（現股）", reasons(run([reel(trade="margin")])))
        self.assertIn("格式：side 要是 buy 或 sell", reasons(run([reel(side="hold")])))
        self.assertIn("格式：price_type 要是 market（市價）或 limit（限價）", reasons(run([reel(price_type="stop")])))
        self.assertIn("格式：停損 108.3 不在升降單位上", reasons(run([reel(stop="108.3")])))
        self.assertIn("格式：代號 2303.TWO 是 TPEx，委託單寫 TWSE", reasons(run([reel(symbol="2303.TWO")])))
        self.assertIn("格式：market 要是 TWSE（上市）或 TPEx（上櫃）", reasons(run([reel(market="NYSE")])))
        self.assertIn("沒有標明上市（TWSE）或上櫃（TPEx）", warnings(run([reel(market=None)])))

    def test_only_taiwan_codes(self):
        for sym in ("TSLA", "BTC/USD", "2330A", ""):
            self.assertIn("標的：", reasons(run([reel(symbol=sym)])), sym)

    def test_call_auctions_take_limit_rod_only(self):
        why = reasons(run([reel()], now="2026-11-02 08:50"))
        self.assertIn("格式：現在是開盤前（09:00 之前），整股只接受限價 ROD（市價、IOC、FOK 會被退單）", why)
        why = reasons(run([reel(price_type="limit", price="110.0", tif="FOK")], now="2026-11-02 13:26"))
        self.assertIn("格式：現在是收盤集合競價（13:25 到 13:30），整股只接受限價 ROD", why)
        why = reasons(run([reel(price_type="limit", price="110.0", tif="ROD")], now="2026-11-02 13:26"))
        self.assertNotIn("格式：", why)
        self.assertIn("時段：13:00 之後不再進場（現在 13:26）", why)

    def test_a_limit_price_inside_the_day_limits(self):
        self.assertIn("漲跌停：限價 123.0 高於今天的漲停價 122.5，會被退單",
                      reasons(run([reel(price_type="limit", price="123.0", tif="ROD")])))
        self.assertIn("漲跌停：限價 100.0 低於今天的跌停價 100.5，會被退單",
                      reasons(run([reel(price_type="limit", price="100.0", tif="ROD")])))


class StopsAndPrices(unittest.TestCase):
    def test_a_stop_is_required(self):
        o = reel(stop=None, target=None, plan={"entry": 110.0})
        self.assertIn("停損：沒有停損。每筆進場都要有（\"require_stop\": true）", reasons(run([o])))
        res = run([o], config=cfg(require_stop=False, min_reward_risk=0))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("沒有停損：虧損沒有上限", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        res = run([reel(stop=None, target=None)])
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("停損 108.5 只在計畫裡、不在委託單上", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 1972.0)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("停損：停損 110.5 要低於進場價 110.0", reasons(run([reel(stop="110.5")])))
        self.assertIn("停利：停利 109.5 要高於進場價 110.0", reasons(run([reel(target="109.5")])))

    def test_a_stop_under_limit_down_cannot_trigger(self):
        o = reel(stop="100.0", plan=dict(reel()["plan"], stop=100.0))
        why = reasons(run([o], config=cfg(max_loss_per_trade_twd=20000, max_daily_loss_twd=20000)))
        self.assertIn("停損：停損 100.0 低於今天的跌停價 100.5，今天碰不到", why)

    def test_no_buy_at_limit_up(self):
        res = run([reel()], prices={"2303": 122.5}, price_times={"2303": at("2026-11-02 09:19")})
        self.assertIn("漲跌停：進場價 122.5 已經是漲停價 122.5", reasons(res))

    def test_reward_risk_gross_and_net(self):
        res = run([reel(target="111.0")])
        self.assertIn("報酬風險比：0.67（停利 111.0、進場 110.0、停損 108.5）低於你的最低 1", reasons(res))
        self.assertEqual(verdicts(run([reel(target="111.0")], config=cfg(min_reward_risk=0.5))), ["PASS"])
        res = run([reel()], config=cfg(min_reward_risk_net=0.6))
        self.assertEqual(res["results"][0]["reasons"], ["報酬風險比：扣掉手續費和證交稅後 0.52，低於你的最低 0.6"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = run([reel()], now="2026-11-02 09:32", prices={"2303": 110.5}, price_times={"2303": PRICE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"], r["risk_ticks"]),
                         (110.5, "price", 2.0, 4))
        self.assertEqual((r["max_loss"], r["reward_risk"], r["reward_risk_net"]), (2473.0, 0.5, 0.21))
        self.assertEqual(r["reasons"], ["報酬風險比：0.50（停利 111.5、進場 110.5、停損 108.5）低於你的最低 1"])
        self.assertIn("2303 最新價 110.5 高於計畫進場價 110.0：風險從 110.5 算", r["info"][0])
        self.assertEqual(r["info"][1], "進場 110.5（最新價）、停損 108.5、停利 111.5：每股風險 2.0（4 檔）")

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-11-02 09:50", "2026-11-02 09:20"):
            res = run([reel()], now=now, prices={"2303": 110.5}, price_times={"2303": PRICE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 110.0)
            self.assertIn("價格超過 15 分鐘：沒有用", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = run([reel()], prices={"2303": 109.5})
        self.assertEqual(res["results"][0]["entry_ref"], 110.0)

    def test_a_limit_order_uses_its_limit_price(self):
        res = run([reel(price_type="limit", price="109.5", tif="ROD")], prices={"2303": 110.5})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["reward_risk"], r["verdict"]), (109.5, 1472.0, 2.0, "PASS"))


class DayLimits(unittest.TestCase):
    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("2330"), placed("2317")]}
        res = run([reel()], state=two)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("今天在這之前：2/3 筆，單日虧損 NT$3,944/NT$9,000", res["results"][0]["info"][-1])
        three = {"orders": [placed(s, max_loss=2500.0) for s in ("2330", "2317", "2454")]}
        why = reasons(run([reel()], state=three))
        self.assertIn("交易次數：今天已進場 3 筆，這批前面又通過 0 筆，上限一天 3 筆", why)
        self.assertIn("單日虧損：今天已經押上 NT$7,500，加這筆 NT$1,972 = NT$9,472，超過你的單日虧損上限 NT$9,000", why)
        yesterday = {"orders": [placed(s, day="2026-10-30") for s in ("2330", "2317", "2454")]}
        self.assertEqual(verdicts(run([reel()], state=yesterday)), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("2330"), placed("2330", side="sell", kind="exit", max_loss=None),
                         placed("2317", status="declined"), placed("2454", status="failed")]}
        res = run([reel()], state=st)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)
        self.assertEqual(res["today"]["orders_placed"], 2)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [ticket(s, p, round(p - 0.5, 2), round(p + 1.0, 2)) for s, p in
                  (("2884", 48.7), ("2892", 43.65), ("2887", 41.7), ("2880", 49.6))]
        res = run(orders)
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertEqual(reasons(res, 3), "交易次數：今天已進場 0 筆，這批前面又通過 3 筆，上限一天 3 筆")

    def test_duplicates_and_the_daily_order_limit(self):
        res = run([reel(), reel()])
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("重複：這批裡第二張買進 2303", reasons(res, 1))
        self.assertIn("重複：今天已經送出過買進 2303", reasons(run([reel()], state={"orders": [placed("2303")]})))
        ten = {"orders": [placed("T%d" % i, side="sell", kind="exit", max_loss=None) for i in range(10)]}
        self.assertIn("每日筆數：今天已送出 10 張，這批前面又通過 0 張，上限一天 10 張", reasons(run([reel()], state=ten)))
        declined = {"orders": [placed("2303", status="declined")]}
        self.assertEqual(verdicts(run([reel()], state=declined)), ["PASS"])


class Window(unittest.TestCase):
    def test_the_entry_window_in_taipei_time(self):
        cases = [("2026-11-02 09:10", "時段：09:15 之前不進場（現在 09:10）"),
                 ("2026-11-02 13:01", "時段：13:00 之後不再進場（現在 13:01）"),
                 ("2026-11-02 08:00", "時段：現在不是交易時間（台北時間 2026-11-02（一） 08:00；盤中是 09:00 到 13:30）"),
                 ("2026-11-02 14:00", "時段：現在不是交易時間"),
                 ("2026-11-01 10:00", "時段：今天休市（週末"),
                 ("2026-10-23 10:00", "時段：今天休市（薔琵颱風"),
                 ("2026-10-26 10:00", "時段：今天休市（臺灣光復暨金門古寧頭大捷紀念日"),
                 ("2026-11-02T01:10:00Z", "時段：09:15 之前不進場（現在 09:10）")]
        for now, want in cases:
            self.assertIn(want, reasons(run([reel()], now=now)), now)
        for now in ("2026-11-02 09:15", "2026-11-02 13:00", "2026-11-02T01:20:00Z", "2026-11-03 10:00"):
            self.assertEqual(verdicts(run([reel()], now=now)), ["PASS"], now)
        self.assertEqual(tw.parse_now(NOW), datetime.datetime(2026, 11, 2, 1, 20))

    def test_extra_closed_from_the_config(self):
        c = cfg(extra_closed={"2026-11-03": "颱風停止交易"})
        self.assertIn("時段：今天休市（颱風停止交易", reasons(run([reel()], now="2026-11-03 10:00", config=c)))

    def test_switching_the_window_off(self):
        lim = reel(price_type="limit", price="110.0", tif="ROD")
        res = run([lim], now="2026-11-01 10:00", config=cfg(no_entries_before=None, no_entries_after=None))
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            run([reel()], config=cfg(no_entries_before="9.15am"))

    def test_an_unknown_year_warns(self):
        res = run([reel()], now="2027-01-04 09:20")
        self.assertIn("2027 年的休市日不在清單裡", " ".join(res["warnings"]))


class AccountChecks(unittest.TestCase):
    def test_max_and_min_order(self):
        res = run([reel(qty=3000)], config=cfg(max_loss_per_trade_twd=10000, max_position_pct=100))
        self.assertIn("金額：NT$330,000 超過你的單筆上限 NT$250,000", reasons(res))
        tiny = reel(lot="odd", qty=5, price_type="limit", price="110.0", tif="ROD", trade="cash")
        self.assertIn("金額：NT$550 低於你的最小委託 NT$1,000", reasons(run([tiny], config=cfg(allow_overnight=True))))

    def test_position_cap(self):
        self.assertIn("部位：2303 會是 NT$110,000，占帳戶 NT$300,000 的 36.7%，超過你的 30% 上限",
                      reasons(run([reel()], config=cfg(max_position_pct=30))))

    def test_cash_and_the_settlement_risk(self):
        res = run([reel()], portfolio={"cash": 100000, "positions": []})
        self.assertIn("現金：這張要 NT$110,156（含手續費 NT$156），現金只有 NT$100,000。當沖如果賣不掉", reasons(res))
        self.assertIn("付不出來就是違約交割", reasons(res))
        res = run([reel()], portfolio={"cash": 120000, "positions": []})
        self.assertIn("現金：買完只剩 NT$9,844，低於你保留的 10% 現金（NT$12,000）", reasons(res))

    def test_blocked_list(self):
        self.assertIn("封鎖：2303 在你的封鎖清單上", reasons(run([reel()], config=cfg(blocked=["2303.TW"]))))

    def test_a_ticket_with_no_price(self):
        o = reel(plan={})
        self.assertIn("價格：沒有 2303 的價格可以算金額", reasons(run([o])))

    def test_live_account_warns(self):
        self.assertIn("真錢帳戶", run([reel()], config=cfg(paper=False))["warnings"][0])


class ShortSales(unittest.TestCase):
    SHORT = {"symbol": "2303", "market": "TWSE", "side": "sell", "lot": "board", "qty": 1000, "price_type": "market",
             "tif": "IOC", "trade": "day_trade", "stop": "111.5", "target": "108.5", "plan": {"entry": 110.0}}

    def test_short_is_off(self):
        self.assertIn("放空：要賣的 2303 比持有的多（持有 0 股）；不做放空", reasons(run([self.SHORT])))

    def test_sell_first_when_allowed(self):
        res = run([self.SHORT], config=cfg(allow_short=True))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 1979.0, 1.0),
                         r["reasons"])
        res = run([dict(self.SHORT, stop="109.5")], config=cfg(allow_short=True))
        self.assertIn("停損：停損 109.5 要高於進場價 110.0", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 189844.0, "positions": [{"symbol": "2303", "market": "TWSE", "name": "聯電", "shares": 1000,
                                           "lots": [{"id": "2303-1", "date": TODAY, "shares": 1000, "cost": 110156,
                                                     "lot": "board", "trade": "day_trade"}]}]}
    SELL = {"symbol": "2303", "market": "TWSE", "side": "sell", "lot": "board", "qty": 1000, "price_type": "market",
            "tif": "IOC", "trade": "day_trade"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("2303"), placed("2330"), placed("2317")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("2303", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["2303"], max_order_twd=1000, max_daily_loss_twd=50)
        res = run([self.SELL], now="2026-11-02 13:20", config=c, portfolio=self.PF, state=full)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("封鎖：", "重複：", "每日筆數："):
            self.assertIn("出場不擋：" + word, why)
        self.assertNotIn("時段", why)

    def test_odd_lots_bought_today_cannot_be_sold_today(self):
        pf = {"cash": 267000.0, "positions": [{"symbol": "2303", "shares": 300, "lots": [
            {"date": TODAY, "shares": 300, "cost": 33047, "lot": "odd", "trade": "cash"}]}]}
        sell = dict(self.SELL, lot="odd", qty=300, price_type="limit", price="109.0", tif="ROD", trade="cash")
        res = run([sell], portfolio=pf)
        self.assertEqual(res["results"][0]["kind"], "exit")
        self.assertIn("交割：今天買的 300 股零股最快下一個交易日才能賣（零股不能當沖）", reasons(res))
        pf["positions"][0]["lots"][0]["date"] = "2026-10-30"
        self.assertEqual(verdicts(run([sell], portfolio=pf)), ["PASS"])

    def test_board_lots_bought_today_need_day_trading(self):
        res = run([self.SELL], portfolio=self.PF, config=cfg(day_trade_enabled=False))
        self.assertIn("交割：今天買的 1 張，要已開通現股當沖才能今天賣", reasons(res))
        pf = json.loads(json.dumps(self.PF))
        pf["positions"][0]["lots"][0]["date"] = "2026-10-30"
        self.assertEqual(verdicts(run([self.SELL], portfolio=pf, config=cfg(day_trade_enabled=False))), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty=500, lot="odd", price_type="limit", price="109.0", tif="ROD", trade="cash")
        pf = json.loads(json.dumps(self.PF))
        pf["positions"][0]["lots"][0]["date"] = "2026-10-30"
        res = run([half, half], portfolio=pf, prices={"2303": 109.0})
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("出場不擋：重複", warnings(res, 1))
        res = run([half, half, half], portfolio=pf, prices={"2303": 109.0})
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("放空：", reasons(res, 2))

    def test_format_and_short_still_apply_to_a_sell(self):
        res = run([dict(self.SELL, price_type="limit", tif="ROD")], portfolio=self.PF)
        self.assertIn("格式：限價單要有 price", reasons(res))
        res = run([dict(self.SELL, qty=2000)], portfolio=self.PF)
        self.assertIn("放空：要賣的 2303 比持有的多（持有 1000 股）", reasons(res))
        self.assertIn("先記錄成交（ticket.py fill）", reasons(run([self.SELL])))

    def test_the_closing_auction_takes_a_limit_sell(self):
        res = run([self.SELL], now="2026-11-02 13:26", portfolio=self.PF)
        self.assertIn("格式：現在是收盤集合競價", reasons(res))
        floor = dict(self.SELL, price_type="limit", price="100.5", tif="ROD")
        self.assertEqual(verdicts(run([floor], now="2026-11-02 13:26", portfolio=self.PF)), ["PASS"])

    def test_buy_then_sell_in_one_batch(self):
        res = run([reel(), self.SELL])
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")
        odd = reel(lot="odd", qty=300, price_type="limit", price="110.0", tif="ROD", trade="cash")
        res = run([odd, dict(self.SELL, lot="odd", qty=300, price_type="limit", price="110.0", tif="ROD",
                             trade="cash")], config=cfg(allow_overnight=True))
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("交割：今天買的 300 股零股", reasons(res, 1))

    def test_a_sell_frees_cash_for_a_later_buy(self):
        pf = {"cash": 50000.0, "positions": [{"symbol": "2330", "shares": 1000, "lots": [
            {"date": "2026-10-30", "shares": 1000, "cost": 1000000, "lot": "board", "trade": "cash"}]}]}
        sell = {"symbol": "2330", "market": "TWSE", "side": "sell", "lot": "board", "qty": 1000,
                "price_type": "limit", "price": "1000", "tif": "ROD", "trade": "cash"}
        c = cfg(max_position_pct=100)
        res = run([sell, reel()], portfolio=pf, prices={"2330": 1000.0}, config=c)
        self.assertEqual(verdicts(res), ["PASS", "PASS"], res["results"][1]["reasons"])
        self.assertEqual(res["cash_after"], 50000 + 1000000 - 1425 - 3000 - 110156)
        self.assertIn("現金：", reasons(run([reel()], portfolio=pf, prices={"2330": 1000.0}, config=c)))


class Lists(unittest.TestCase):
    def test_reading_a_folder_and_an_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            for f in LIST_FILES:
                shutil.copy(f, d)
            got = check.read_lists([d])
            self.assertEqual(len(got["daytrade"]["codes"]), 2080)
            self.assertEqual(len(tw.active_disposition(got["disposition"], datetime.date(2026, 11, 2))), 14)
            empty = os.path.join(d, "empty")
            os.makedirs(empty)
            with open(os.path.join(empty, "punish-2026-11-02.json"), "w") as fh:
                fh.write("[]")
            got = check.read_lists([empty])
            self.assertEqual((got["disposition"], got["daytrade"]), ({}, None))
            with self.assertRaises(check.RiskError):
                check.read_lists([os.path.join(d, "missing")])

    def fake_get(self, url):
        name = {tw.DAYTRADE_URL: LIST_FILES[0], tw.TPEX_DAYTRADE_URL: LIST_FILES[1], tw.PUNISH_URL: LIST_FILES[2],
                tw.TPEX_DISPOSAL_URL: LIST_FILES[3]}[url]
        self.calls.append(url)
        with open(name, encoding="utf-8") as fh:
            return fh.read()

    def test_fetching_and_caching_the_lists(self):
        self.calls = []
        old = check.http_get
        check.http_get = self.fake_get
        try:
            with tempfile.TemporaryDirectory() as d:
                state = os.path.join(d, "state", "broker.json")
                lists, notes = check.resolve_lists(None, state, TODAY)
                self.assertEqual(notes, [])
                self.assertEqual(len(self.calls), 4)
                self.assertEqual(sorted(os.listdir(os.path.join(d, "state", "lists"))),
                                 ["disposal-2026-11-02.json", "punish-2026-11-02.json",
                                  "tpex-daytrade-2026-11-02.json", "twse-daytrade-2026-11-02.json"])
                self.assertIn("2303", lists["daytrade"]["codes"])
                check.resolve_lists(None, state, TODAY)
                self.assertEqual(len(self.calls), 4)       # today's copies, no second fetch
        finally:
            check.http_get = old

    def test_a_failed_fetch_is_a_warning(self):
        def down(url):
            raise urllib.error.URLError("offline")
        old = check.http_get
        check.http_get = down
        try:
            with tempfile.TemporaryDirectory() as d:
                c = os.path.join(d, "config.json")
                with open(c, "w", encoding="utf-8") as fh:
                    json.dump(CFG, fh)
                none = os.path.join(d, "none")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = check.main(["--orders", ORDERS_PATH, "--config", c, "--portfolio", none, "--prices", none,
                                       "--state", os.path.join(d, "state", "broker.json"), "--now", NOW,
                                       "--no-write"])
                self.assertEqual(code, 0)
                self.assertIn("注意：清單抓不到", buf.getvalue())
                self.assertIn("沒有確認能不能當沖／是不是處置股", buf.getvalue())
        finally:
            check.http_get = old

    def test_lists_none(self):
        lists, notes = check.resolve_lists(["none"], "trader/state/broker.json", TODAY)
        self.assertIsNone(lists)
        self.assertIn("--lists none", notes[0])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(CFG, fh)
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def base(self, prices=None):
        args = ["--orders", ORDERS_PATH, "--config", self.cfg, "--portfolio", self.none,
                "--prices", prices or self.none, "--state", os.path.join(self.d, "broker.json"), "--no-write"]
        for f in LIST_FILES:
            args += ["--lists", f]
        return args

    def test_prices_file_freshness(self):
        pp = os.path.join(self.d, "prices.json")
        with open(pp, "w", encoding="utf-8") as fh:
            json.dump({"as_of": "2026-11-02T01:31:00Z", "prices": {"2303.TW": 110.5},
                       "detail": {"2303.TW": {"time": "2026-11-02T01:30:00Z", "fetched": "2026-11-02T01:31:00Z"}}}, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(check.main(self.base(pp) + ["--now", "2026-11-02 09:32"]), 1)
        self.assertIn("報酬風險比：0.50（停利 111.5、進場 110.5、停損 108.5）低於你的最低 1", buf.getvalue())
        self.assertIn("BLOCK #1", buf.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(self.base(pp) + ["--now", "2026-11-02 09:50"]), 0)

    def test_errors_are_one_line(self):
        cases = [(["--orders", os.path.join(self.d, "nope.json"), "--no-write"], "沒有委託單檔"),
                 (self.base() + ["--now", "noon"], "--now 要像"),
                 (self.base() + ["--today", "monday"], "--today 要像"),
                 (self.base() + ["--now", NOW, "--lists", os.path.join(self.d, "missing")], "沒有清單檔")]
        for args, want in cases:
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(args), 1)
            self.assertIn(want, err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
