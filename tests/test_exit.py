"""trader-exit tests on the reel: 聯電 2303, Mon 2026-11-02, real Yahoo 5-minute bars
(tests/fixtures/trader-entry/2303-2026-11-02-5m.json) and the real TWSE MIS quote taken after
the close (tests/fixtures/trader-exit/mis-2303-2026-11-02.json: prev close 111.5, limits 122.5 / 100.5)."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")

FIVE = os.path.join(FIX, "trader-entry", "2303-2026-11-02-5m.json")
MIS = os.path.join(FIX, "trader-exit", "mis-2303-2026-11-02.json")
DAY = datetime.date(2026, 11, 2)

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "2303", "market": "TWSE", "name": "聯電", "side": "buy",
             "date": "2026-11-02", "setup": "orb", "entry": 110.0, "entry_time": "09:20", "entry_bar": 3,
             "interval": "5m", "opening_range": {"high": 109.5, "low": 107.5, "minutes": 15},
             "stop": 108.5, "stop_rule": "or_mid", "stop_label": "開盤區間中點",
             "target": 111.5, "target_r": 1.0, "target_label": "1R（一倍風險）",
             "risk_per_share": 1.5, "reward_risk": 1.0, "flat_by": "13:20",
             "tick": 0.5, "risk_ticks": 3, "prev_close": 111.5, "limit_up": 122.5, "limit_down": 100.5,
             "limits_source": "mis", "cost_per_share": 0.4785, "cost_r": 0.32, "reward_risk_net": 0.52}
# trader/entry.json as trader-entry writes it when the 09:15 bar closes above the range
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "2303", "market": "TWSE", "name": "聯電", "setup": "orb",
              "side": "buy", "date": "2026-11-02", "status": "triggered", "time": "09:20", "bar_start": "09:15",
              "bar_index": 3, "price": 110.0, "level": 109.5, "interval": "5m",
              "opening_range": {"high": 109.5, "low": 107.5, "minutes": 15}, "source": "yahoo",
              "checked_at": "2026-11-02T01:20:00Z"}

SAFETY = """## 安全

- 這是教學與工具，不構成投資建議。PASS 只代表通過規則檢查，不是推薦。
- 預設模擬交易（`"paper": true`）。要不要動用真錢，由使用者自己在自己的設定檔決定。
- 這套 skill 不會自己下單。只有 trader-broker 會產生委託單，而且三件事都成立才會：trader-risk 今天通過了這一張單、使用者在對話中針對這一張單明確回覆 yes、使用者自己在券商 App 或下單軟體送出。
- 不做融資、融券、先賣後買、選擇權或期貨，除非設定檔開啟而且使用者確認。
- 券商帳號、密碼和憑證不在對話裡出現，也不寫進任何檔案。
"""
NEVER = (chr(0x2014), "獲利", "賺", "pro" + "fit", "即時")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = exitp.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=109.5, ol=107.5, atr=None, n=None, code="2303"):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, code)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(110.0, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("108.5"))            # (109.5 + 107.5) / 2, already on the 0.5 grid
        self.assertEqual(p["risk"], Decimal("1.5"))
        self.assertEqual(p["risk_ticks"], 3)
        self.assertEqual(p["target"], Decimal("111.5"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R（一倍風險）")
        self.assertEqual(p["stop_detail"], "(109.5 + 107.5) / 2 = 108.5")
        self.assertEqual(p["entry_note"], "")

    def test_the_default_2r(self):
        p = c(110.0, "or-mid")
        self.assertEqual(p["target"], Decimal("113.0"))
        self.assertEqual(p["target_label"], "2R（兩倍風險）")

    def test_raw_yahoo_floats_land_on_the_grid(self):
        p = c(109.99999237060547, "or-mid", "1R", 109.50000762939453, 107.49999237060547)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("110.0"), Decimal("108.5"), Decimal("111.5")))

    def test_other_stops_snap_down(self):
        p = c(110.0, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["risk_ticks"], p["target"]),
                         (Decimal("107.5"), Decimal("2.5"), 5, Decimal("115.0")))
        p = c(110.0, "price:108.7")
        self.assertEqual(p["stop"], Decimal("108.5"))                # away from the entry
        self.assertEqual(p["stop_detail"], "108.7 不在升降單位上，往下取到 108.5")
        p = c(110.0, "pct:1")
        self.assertEqual((p["stop"], p["risk"]), (Decimal("108.5"), Decimal("1.5")))
        self.assertEqual(p["stop_detail"], "110.0 x (1 - 1%) = 108.9，往下取到升降單位 0.5")
        p = c(110.0, "atr:1.5", atr=1.625, n=4)
        self.assertEqual(p["stop"], Decimal("107.5"))                # 110 - 2.4375 = 107.5625
        self.assertEqual(p["stop_label"], "進場價下方 1.5 倍 ATR(4)")
        self.assertIn("還不到 14 根", p["stop_detail"])

    def test_ticks_across_bands_and_prices(self):
        p = c(100.5, "pct:1", code="")                               # 99.495 sits in the 0.1 band
        self.assertEqual((p["stop"], p["risk"], p["risk_ticks"]), (Decimal("99.4"), Decimal("1.1"), 7))
        p = c(104.1, "pct:1", code="0050")                           # an ETF: 0.05 ticks above 50
        self.assertEqual((p["stop"], p["risk"], p["risk_ticks"], p["target"]),
                         (Decimal("103.05"), Decimal("1.05"), 21, Decimal("106.20")))
        p = c(2330, "pct:1", "1R", code="2330")                      # 1000 and up: 5 a tick
        self.assertEqual((p["stop"], p["risk"], p["risk_ticks"], p["target"]),
                         (Decimal("2305"), Decimal("25"), 5, Decimal("2355")))

    def test_targets_snap_to_the_nearest_tick(self):
        p = c(110.0, "or-mid", "1.5R")
        self.assertEqual(p["target"], Decimal("112.5"))              # 112.25 rounds half up
        self.assertEqual(p["target_r"], Decimal("1.5"))
        self.assertEqual(p["reward_risk"], Decimal("1.67"))
        self.assertEqual(p["target_detail"], "112.25 取到最近的升降單位，實際 1.67R")
        p = c(110.0, "or-mid", "price:112.3")
        self.assertEqual(p["target"], Decimal("112.5"))
        self.assertEqual(p["target_label"], "你的價格（1.67R）")
        self.assertEqual(p["target_detail"], "112.3 不在升降單位上，取到最近的 112.5")
        p = c(110.2, "price:108")                                    # an entry off the grid snaps up
        self.assertEqual(p["entry"], Decimal("110.5"))
        self.assertEqual(p["entry_note"], "進場價 110.2 不在升降單位上，往上取到 110.5")

    def test_refusals_say_why(self):
        cases = [("price:110", "2R", "在進場價 110.0 或以上"),
                 ("price:111", "2R", "在進場價 110.0 或以上"),
                 ("or-mid", "price:110", "在進場價 110.0 或以下"),
                 ("price:109.5", "2R", "停損只差 1 檔（109.5 到 110.0），正常跳動就會掃到")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(110.0, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        self.assertEqual(c(110.0, "price:109")["risk_ticks"], 2)     # two ticks is the least it takes
        with self.assertRaises(exitp.PlanError):
            c(9.99, "price:9.98", code="")                           # one 0.01 tick
        self.assertEqual(c(9.99, "price:9.97", code="")["risk_ticks"], 2)
        with self.assertRaises(exitp.PlanError):
            c(108.0, "or-mid")                                       # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x", "price:0"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(110.0, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(110.0, "or-mid", oh=107.5, ol=109.5)


class LimitsAndCosts(unittest.TestCase):
    MIS_LIMITS = {"limit_up": 122.5, "limit_down": 100.5}

    def test_limits_refuse_the_unreachable(self):
        self.assertEqual(exitp.check_limits(c(110.0, "or-mid", "price:122.5"), self.MIS_LIMITS, "2303"), [])
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.check_limits(c(110.0, "or-mid", "price:123"), self.MIS_LIMITS, "2303")
        self.assertIn("停利 123.0 高於今天的漲停價 122.5：碰不到", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.check_limits(c(110.0, "price:100", "1R"), self.MIS_LIMITS, "2303")
        self.assertIn("停損 100.0 低於今天的跌停價 100.5", str(cm.exception))
        self.assertIn("跌停鎖死時也賣不掉", str(cm.exception))
        warns = exitp.check_limits(c(110.0, "price:100.5", "1R"), self.MIS_LIMITS, "2303")
        self.assertEqual(warns, ["停損就在跌停價：跌停鎖死時賣單要排隊，不一定賣得掉"])
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.check_limits(c(122.5, "price:120", "1R"), self.MIS_LIMITS, "2303")
        self.assertIn("已經在今天的漲停價 122.5 或以上", str(cm.exception))
        self.assertEqual(exitp.check_limits(c(110.0, "or-mid"), {}, "2303"), [])

    def test_computed_limits_match_mis(self):
        lim = exitp._computed(111.5, "2303")
        self.assertEqual((lim["limit_up"], lim["limit_down"], lim["limits_source"]), (122.5, 100.5, "computed"))
        self.assertEqual(lim["note"], "")
        self.assertIn("國外成分 ETF 沒有漲跌幅限制", exitp._computed(104.95, "0050")["note"])

    def test_costs_in_r(self):
        cost = exitp.costs(c(110.0, "or-mid", "1R"), "2303", {}, DAY)
        self.assertEqual((cost["per"], cost["cost_r"], cost["net"]), (Decimal("0.4785"), Decimal("0.32"), Decimal("0.52")))
        self.assertEqual(cost["detail"], "手續費 0.1425% 買賣各一次、當沖證交稅 0.15%")
        self.assertEqual(exitp.costs(c(110.0, "or-mid"), "2303", {}, DAY)["net"], Decimal("1.27"))
        six = exitp.costs(c(110.0, "or-mid", "1R"), "2303", {"fee_discount": 0.6}, DAY)
        self.assertEqual((six["per"], six["cost_r"], six["net"]), (Decimal("0.3531"), Decimal("0.24"), Decimal("0.62")))
        self.assertIn("（打 6 折）", six["detail"])
        etf = exitp.costs(c(104.1, "pct:1", code="0050"), "0050", {}, DAY)
        self.assertEqual((etf["per"], etf["cost_r"]), (Decimal("0.4008"), Decimal("0.38")))
        self.assertIn("ETF 證交稅 0.1%", etf["detail"])
        late = exitp.costs(c(110.0, "or-mid", "1R"), "2303", {}, datetime.date(2028, 1, 3))
        self.assertIn("證交稅 0.3%", late["detail"])                 # the day-trade cut ends 2027-12-31


class Bars(unittest.TestCase):
    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("2303", None, "5m", DAY, FIVE)
        self.assertEqual((ch["market"], ch["prev_close"], ch["range"]), ("TWSE", 111.5, "1d"))
        bars = exitp.session_bars(ch, DAY, "5m", upto_slot=3)
        self.assertEqual(len(bars), 4)                               # 09:00 ... the 09:15 entry bar
        self.assertEqual([b["c"] for b in bars], [108.0, 108.5, 109.0, 110.0])
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(atr, (2.0 + 1.5 + 1.0 + 2.0) / 4, places=9)
        # the whole day: 53 bars of continuous trading; the empty 13:25 bar and the 13:30 print never count
        all_bars = exitp.session_bars(ch, DAY, "5m")
        self.assertEqual(len(all_bars), 53)
        self.assertEqual(exitp.tw.hm(exitp.tw.ts_to_taipei(all_bars[-1]["t"])), "13:20")
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        self.assertEqual(exitp.session_bars(ch, datetime.date(2026, 11, 1), "5m"), [])   # a Sunday
        # a clock at 09:12 sees only the bars that have closed: 09:00 and 09:05
        early = exitp.session_bars(ch, DAY, "5m", now_utc=exitp.tw.parse_now("2026-11-02 09:12"))
        self.assertEqual(len(early), 2)

    def test_the_chart_must_be_the_symbol(self):
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("2330", None, "5m", DAY, FIVE)
        self.assertIn("圖表檔是 2303.TW，不是 2330", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self.real_open = exitp._open_url

        def offline(url, timeout=20):
            raise urllib.error.URLError("no network in tests")
        exitp._open_url = offline

    def tearDown(self):
        exitp._open_url = self.real_open
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)

    def plan(self, *args):
        return run(["plan", "--now", "2026-11-02 09:21"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def assertClean(self, text):
        for never in NEVER:
            self.assertNotIn(never, text)

    def test_the_reel_end_to_end(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write", "--mis", MIS)
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        for line in ("出場計畫：聯電 2303 做多，2026-11-02（一），開盤區間突破，5 分 K",
                     "  進場          110.0，09:20（09:15 那根 K 棒收盤，編號 3）",
                     "  停損          108.5  開盤區間中點：(109.5 + 107.5) / 2 = 108.5",
                     "  停利          111.5  1R（一倍風險）",
                     "  每股風險      1.5（進場價的 1.36%，3 檔）",
                     "  報酬風險比    1.0",
                     "  成本          每股約 0.48（手續費 0.1425% 買賣各一次、當沖證交稅 0.15%），約 0.32R；扣成本後報酬風險比 0.52",
                     "  漲跌停        122.5 / 100.5（MIS 報價）",
                     "  最晚出場      13:20，不論輸贏都出場，不留倉",
                     "注意：成本約 0.32R：停損只有 3 檔時，手續費加證交稅占了每股風險的 32%。扣成本後的報酬風險比是 0.52，不是 1.0。"):
            self.assertIn(line, text)
        self.assertIn("不是保證", text)
        self.assertIn("零股目前不能現股當沖", text)
        self.assertIn("/trader-timemachine", text)
        self.assertIn("什麼都沒有下單", text)
        self.assertClean(text)
        code, text, _ = self.plan("--write", "--mis", MIS)          # the defaults: or-mid, 2R
        self.assertEqual((self.read_plan()["target"], self.read_plan()["target_r"]), (113.0, 2.0))
        self.assertEqual(self.read_plan()["reward_risk_net"], 1.27)
        code, text, _ = run(["show", "--now", "2026-11-02 09:30"])
        self.assertEqual(code, 0)
        self.assertIn("113.0  2R（兩倍風險）", text)
        self.assertNotIn("不是今天", text)
        code, text, _ = run(["show", "--now", "2026-11-03 09:30"])
        self.assertIn("這份計畫是 2026-11-02 的，不是今天（2026-11-03）", text)

    def test_manual_flags_and_json(self):
        code, text, err = self.plan("--symbol", "2303.TW", "--entry", "110", "--or-high", "109.5", "--or-low", "107.5",
                                    "--time", "09:20", "--entry-bar", "3", "--date", "2026-11-02", "--target", "1R",
                                    "--mis", MIS, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(len(p["warnings"]), 1)                       # the cost warning
        self.assertIn("成本約 0.32R", p["warnings"][0])

    def test_market_and_name_from_the_universe(self):
        with open(os.path.join("trader", "universe.txt"), "w", encoding="utf-8") as fh:
            fh.write("2303|聯電|TWSE|股票\n6488|環球晶|TPEx|股票\n")
        code, text, err = self.plan("--symbol", "2303", "--entry", "110", "--stop", "pct:1", "--prev-close", "111.5",
                                    "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["market"], p["name"], p["limits_source"], p["limit_up"]), ("TWSE", "聯電", "computed", 122.5))
        self.assertEqual(p["setup"], "level")
        code, _, err = self.plan("--symbol", "TSLA", "--entry", "364.18")
        self.assertIn("不像台股代號", err)

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", FIVE, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["risk_ticks"], p["target"]), (107.5, 2.5, 5, 115.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"], p["atr"]), ("atr", 4, 1.625))
        # the offline chart's previous close gives the limits
        self.assertEqual((p["prev_close"], p["limit_up"], p["limit_down"], p["limits_source"]),
                         (111.5, 122.5, 100.5, "computed"))
        self.assertEqual((p["cost_r"], p["reward_risk_net"]), (0.19, 1.52))
        self.assertEqual(p["warnings"], [])                           # 0.19R is under the 0.25R warning
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", FIVE, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("至少 3 根已收盤", err)

    def test_limits_fall_back_and_say_so(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan()                                 # online, but the network is down
        self.assertEqual(code, 0, err)
        self.assertIn("漲跌停        沒有對照", text)
        self.assertIn("查不到今天的漲跌停價（MIS 和 Yahoo 都沒回應）", text)
        p = json.loads(self.plan("--json")[1])
        self.assertEqual((p["limit_up"], p["limit_down"], p["limits_source"]), (None, None, None))
        # an offline chart that is not the day's 1d chart has no previous close to lean on
        with open(FIVE, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["chart"]["result"][0]["meta"]["range"] = "5d"
        self.write("five-day.json", doc)
        code, text, _ = self.plan("--offline", "five-day.json")
        self.assertIn("離線沒有昨收", text)
        code, text, _ = self.plan("--prev-close", "111.5")
        self.assertIn("122.5 / 100.5（用昨收 111.5 算的）", text)
        # the MIS file must be the plan's day
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2026-11-03"))
        code, _, err = self.plan("--mis", MIS)
        self.assertEqual(code, 1)
        self.assertIn("是 20261102 的報價，不是計畫日 2026-11-03", err)

    def test_limit_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, _, err = self.plan("--target", "price:123", "--mis", MIS)
        self.assertEqual(code, 1)
        self.assertIn("高於今天的漲停價 122.5", err)
        code, _, err = self.plan("--stop", "price:100", "--mis", MIS)
        self.assertEqual(code, 1)
        self.assertIn("低於今天的跌停價 100.5", err)
        code, _, err = self.plan("--stop", "price:109.5", "--mis", MIS)
        self.assertIn("停損只差 1 檔", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("2303 是 waiting，還沒觸發", err)
        code, text, err = self.plan("--entry", "110", "--json", "--mis", MIS)   # plan ahead
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (110.0, 108.5, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=109.5))
        code, _, err = self.plan()
        self.assertIn("型態沒有開盤區間可以當預設停損", err)
        self.assertEqual(self.plan("--stop", "pct:1", "--mis", MIS)[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("只做多", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("只做多", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("進場紀錄", err)
        code, _, err = self.plan("--symbol", "2303")
        self.assertIn("要搭配 --entry", err)
        code, _, err = self.plan("--symbol", "2303", "--from", "x.json", "--entry", "1")
        self.assertIn("只能擇一", err)
        code, _, err = self.plan("--symbol", "2303", "--entry", "110", "--or-high", "109.5")
        self.assertIn("要一起給", err)
        self.assertNotIn("Traceback", err)

    def test_config_flat_by_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "13:10", "min_reward_risk": 1.5,
                                                           "fee_discount": 0.6})
        code, text, _ = self.plan("--target", "1R", "--json", "--mis", MIS)
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "13:10")
        self.assertEqual((p["cost_per_share"], p["cost_r"], p["reward_risk_net"]), (0.3531, 0.24, 0.62))
        self.assertIn("打 6 折", p["cost_detail"])
        self.assertEqual(p["warnings"], ["報酬風險比 1.0 低於設定的 min_reward_risk 1.5：trader-risk 會檢查這一項。"])
        os.remove(os.path.join("trader", "config.json"))

        code, _, err = self.plan("--flat-by", "13:30", "--mis", MIS)
        self.assertEqual(code, 1)
        self.assertIn("在 13:30 收盤或之後", err)
        code, text, _ = self.plan("--flat-by", "13:27", "--mis", MIS)
        self.assertEqual(code, 0)
        self.assertIn("收盤集合競價", text)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="13:20"))
        code, _, err = self.plan("--mis", MIS)
        self.assertEqual(code, 1)
        self.assertIn("沒有時間抱這筆單", err)

    def test_the_calendar(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2026-11-01"))
        code, text, _ = self.plan("--prev-close", "111.5")
        self.assertIn("2026-11-01 不是交易日（週末）", text)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2027-03-03"))
        code, text, _ = self.plan("--prev-close", "111.5")
        self.assertEqual(code, 0)
        self.assertIn("2027 年的休市日不在清單裡", text)
        self.write(os.path.join("trader", "config.json"), {"extra_closed": {"2026-11-02": "颱風停止交易"}})
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, _ = self.plan("--mis", MIS)
        self.assertIn("2026-11-02 不是交易日（颱風停止交易）", text)

    def test_show_without_a_plan(self):
        code, text, _ = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("還沒有計畫", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertTrue(text.endswith(SAFETY), "the Safety block must close the file, word for word")
        low = " ".join(text.lower().split())
        for must in ("進場前", "停損是計畫，不是保證", "/trader-timemachine", "/trader-budget", "只做多",
                     "108.5", "111.5", "113.0", "0.32r", "0.52", "升降單位", "漲停", "跌停", "零股",
                     "正常跳動就會掃到", "13:20"):
            self.assertIn(must, low)
        for never in NEVER:
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
