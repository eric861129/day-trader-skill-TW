import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
UMC = os.path.join(DIR, "2303-60d5m.json.gz")   # Yahoo v8 chart 2303.TW, range=60d interval=5m, fetched 2026-11-02 16:48
with gzip.open(UMC, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-11-02 09:25"
NOW_UTC = tm.parse_now(NOW)

# the reel's plan, as trader-exit writes it (PHASE1 contract): 聯電 opening-range breakout, 2026-11-02
PLAN = {"created_by": "trader-exit", "symbol": "2303", "market": "TWSE", "name": "聯電", "side": "buy",
        "date": "2026-11-02", "setup": "orb", "entry": 110.0, "entry_time": "09:20", "entry_bar": 3,
        "interval": "5m", "opening_range": {"high": 109.5, "low": 107.5, "minutes": 15},
        "stop": 108.5, "stop_rule": "or_mid", "stop_label": "開盤區間中點",
        "target": 111.5, "target_r": 1.0, "target_label": "1R", "risk_per_share": 1.5, "reward_risk": 1.0,
        "flat_by": "13:20", "tick": 0.5, "risk_ticks": 3, "prev_close": 111.5, "limit_up": 122.5,
        "limit_down": 100.5, "limits_source": "mis", "cost_per_share": 0.4785, "cost_r": 0.32,
        "reward_risk_net": 0.52}
TW_KEYS = ("market", "name", "tick", "risk_ticks", "prev_close", "limit_up", "limit_down", "limits_source",
           "cost_per_share", "cost_r", "reward_risk_net")
BARE = {k: v for k, v in PLAN.items() if k not in TW_KEYS}

# fingerprints of the seed-19 run: any change to the draws, the pool or the clamp moves them
BANDS = "b8428555889ab9d9"
PATHS = "19a13701e2b4724f"
TRADES = "a8b9b2d1f2304476"
HIST = {"lo": 102.79, "hi": 117.14,
        "bins": [113, 132, 153, 249, 290, 393, 492, 582, 662, 704, 764, 808, 725, 688, 628, 589, 499, 387, 303,
                 242, 156, 126, 97, 64]}
VERDICT = ("明顯偏向停損：1R 加上成本 0.32R 時，碰到停利或停損的重播裡，停利要先到 66% 才打平；"
           "實際是 43%（先到停利 43.0% 對先到停損 56.5%）。")


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def tpe(ts):
    return tm.tw.ts_to_taipei(ts)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Reel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_reel_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "13:25"))
        self.assertEqual(m["pool_sessions"], 58)
        self.assertEqual(m["bars_left"], 49)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-05", "2026-10-30"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.4305, 0.565, 0.0045, 0.432))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (109.68, 104.7, 114.92))
        self.assertEqual((m["avg_R"], m["avg_R_net"]), (-0.135, -0.454))
        self.assertEqual(m["r_at_target"], 1.0)

    def test_costs_move_the_break_even(self):
        m = self.doc["mc"]
        # 110.0 x (0.1425 % x 2 + 0.15 %) = 0.4785 a share, over the 1.5 risk = 0.319R
        self.assertEqual((m["cost_per_share"], m["cost_r"]), (0.4785, 0.319))
        self.assertEqual(m["break_even"], 0.6595)          # (1 + 0.319) / (1 + 1)
        gap, share, even = tm.lean(m)
        self.assertEqual((round(share, 3), round(even, 4)), (0.432, 0.6595))
        self.assertAlmostEqual(m["avg_R"] - m["cost_r"], m["avg_R_net"], places=2)

    def test_the_limits_come_from_the_plan(self):
        m = self.doc["mc"]
        self.assertEqual((m["limit_up"], m["limit_down"], m["limits_source"]), (122.5, 100.5, "mis"))
        self.assertEqual((m["p_limit_up"], m["p_limit_down"]), (0.0002, 0.0015))

    def test_fingerprints(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [104.59, 107.51, 109.6, 111.83, 114.82])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (58, 25))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (7, 9, 9, 9))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (-0.142, -0.431))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{k: t[k] for k in ("date", "entry", "outcome", "R", "R_net")} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        self.assertEqual(h["trades"][0], {"date": "2026-08-10", "time": "09:20", "entry": 120.0, "stop": 118.0,
                                          "target": 122.0, "outcome": "target", "R": 1.0, "R_net": 0.74})
        for t in h["trades"]:   # every history price sits on the tick grid
            for k in ("entry", "stop", "target"):
                self.assertTrue(tm.tw.on_tick(t[k], "2303"), t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], VERDICT)
        self.assertEqual(self.doc["verdict_notes"], [])
        self.assertEqual(self.doc["warnings"], [])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [110.0] * 5)
        self.assertEqual(len(m["bands"]), 50)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:20", "13:25", 50))
        self.assertTrue(all(p[0] == 110.0 and len(p) == 50 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["market"], p["name"], p["entry"], p["stop"], p["target"]),
                         ("2303", "TWSE", "聯電", 110.0, 108.5, 111.5))
        self.assertEqual((p["entry_bar"], p["entry_time"], p["risk_per_share"], p["target_r"], p["flat_by"]),
                         (3, "09:20", 1.5, 1.0, "13:20"))

    def test_the_empty_day_is_left_out(self):
        # 2026-10-23: every 5-minute bar is empty market-wide in Yahoo's data: the typhoon day
        self.assertEqual(self.doc["data"]["short_sessions_left_out"],
                         [{"date": "2026-10-23", "bars": 0, "closed": "薔琵颱風，台北市停班，全日休市"}])
        self.assertNotIn("2026-10-23", [t["date"] for t in self.doc["history"]["trades"]])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("聯電 2303 做多，2026-11-02（一）：進場 110.0（09:20，第 3 根），停損 108.5，停利 111.5"
                     "（1R，每股風險 1.5，3 檔）。",
                     "重播當天剩下的盤 10,000 次：09:20 到 13:25（49 根 5 分 K），亂數種子 19。",
                     "抽自聯電 2303 過去 58 個完整交易日（2026-08-05 到 2026-10-30）",
                     "每一根重播都夾在漲停 122.5 和跌停 100.5 之間。",
                     "成本：每股約 0.48（手續費買賣各一次，加當沖證交稅），是 0.32R。",
                     "  先到停利   43.0%", "  先到停損   56.5%", "  都沒碰到   0.4%    13:25 還沒出場",
                     "  站上進場價 43.2%   先到停利，或 13:25 時高於進場價",
                     "13:25 的價格：中位數 109.68，90% 的重播在 104.70 到 114.92 之間。",
                     "平均 R：-0.135，扣成本 -0.454",
                     "0.02% 的重播碰到漲停，0.15% 碰到跌停。",
                     "觸發 25 次：停利 7、停損 9、都沒碰到 9。平均 R -0.142，扣成本 -0.431。",
                     VERDICT, "略過休市日：2026-10-23（薔琵颱風，台北市停班，全日休市）。",
                     "不是預測", "收盤集合競價不在 5 分 K 資料裡", "--seed 19"):
            self.assertIn(line, text)
        for word in ("獲利", "賺", "保證", "即時", "逐筆", "profit", "P&L"):
            self.assertNotIn(word, text)


class FlatBy(unittest.TestCase):
    def test_until_the_flat_by_time(self):
        doc = machine(until="13:20")
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("13:20", 48, "13:20"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.4389, 0.5571, 0.004, 0.4406))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (109.74, 104.88, 114.93))
        self.assertEqual((m["avg_R"], m["avg_R_net"]), (-0.119, -0.438))
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"]), (25, 7, 9, 9))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (-0.158, -0.447))
        self.assertTrue(doc["verdict"].startswith("明顯偏向停損：1R 加上成本 0.32R 時"))
        self.assertIn("實際是 44%（先到停利 43.9% 對先到停損 55.7%）", doc["verdict"])
        self.assertIn("13:20 還沒出場", "\n".join(tm.report(doc)))


class Costs(unittest.TestCase):
    def test_a_plan_without_the_tw_keys(self):
        doc = machine(tm.normalize_plan(BARE))
        m = doc["mc"]
        self.assertEqual((m["cost_per_share"], m["cost_r"], m["avg_R_net"]), (0.4785, 0.319, -0.454))
        # the plan is for the data's last day, so Yahoo's previousClose (111.5) gives the limits
        self.assertEqual((m["limit_up"], m["limit_down"], m["limits_source"]), (122.5, 100.5, "computed"))
        self.assertEqual(doc["warnings"], ["plan 沒有漲跌停：用Yahoo 的昨收 111.5 算出 122.5 / 100.5",
                                           "plan 沒有成本欄位：用設定檔的手續費算，每股約 0.48（0.32R）"])
        self.assertEqual(doc["plan"]["market"], "TWSE")    # from the data's own symbol, 2303.TW

    def test_the_fee_discount_in_the_config(self):
        doc = machine(tm.normalize_plan(BARE), config={"fee_discount": 0.6})
        m, h = doc["mc"], doc["history"]
        # 0.1425 % x 0.6 x 2 + 0.15 % = 0.321 % of 110.0 = 0.3531 a share
        self.assertEqual((m["cost_per_share"], m["cost_r"], m["break_even"], m["avg_R_net"]),
                         (0.3531, 0.2354, 0.6177, -0.371))
        self.assertEqual(h["avg_R_net"], -0.355)
        self.assertIn("1R 加上成本 0.24R 時", doc["verdict"])
        self.assertIn("停利要先到 62% 才打平", doc["verdict"])

    def test_no_cost_is_the_original_coin_flip_scale(self):
        mc = {"p_target": 0.30, "p_stop": 0.26, "r_at_target": 1.0, "cost_r": 0.0}
        self.assertEqual(tm.verdict(mc), "接近擲硬幣：先到停利 30.0% 對先到停損 26.0%。")
        doc = machine(plan(cost_per_share=0.0001))
        self.assertEqual(doc["mc"]["break_even"], 0.5)
        self.assertEqual(doc["verdict"], "偏向停損：先到停利 43.0% 對先到停損 56.5%。")

    def test_the_tax_in_words(self):
        doc = machine(replays=200)
        line = lambda code: [x for x in tm.report(dict(doc, plan=dict(doc["plan"], symbol=code))) if x.startswith("成本")][0]
        self.assertIn("（手續費買賣各一次，加當沖證交稅）", line("2303"))
        self.assertIn("（手續費買賣各一次，加 ETF 證交稅）", line("00632R"))
        self.assertIn("（手續費買賣各一次，債券 ETF 免證交稅）", line("00931B"))

    def test_the_same_odds_need_more_with_costs(self):
        mc = {"p_target": 0.30, "p_stop": 0.26, "r_at_target": 1.0, "cost_r": 0.32}
        self.assertEqual(tm.verdict(mc), "偏向停損：1R 加上成本 0.32R 時，碰到停利或停損的重播裡，停利要先到 66% "
                                         "才打平；實際是 54%（先到停利 30.0% 對先到停損 26.0%）。")


class Limits(unittest.TestCase):
    def test_every_replay_is_clamped(self):
        doc = machine(plan(limit_up=112.0, limit_down=108.0), replays=2000)
        m = doc["mc"]
        self.assertEqual((m["p5_close"], m["p95_close"]), (108.0, 112.0))
        self.assertEqual((m["p_limit_up"], m["p_limit_down"]), (0.514, 0.6145))
        for path in m["sample_paths"]:
            self.assertTrue(all(108.0 <= x <= 112.0 for x in path))
        for band in m["bands"]:
            self.assertTrue(108.0 <= band[0] and band[4] <= 112.0)

    def test_a_target_above_limit_up_cannot_be_reached(self):
        doc = machine(plan(target=113.0, limit_up=112.5, limit_down=100.5), replays=1000)
        self.assertEqual(doc["mc"]["p_target"], 0.0)
        self.assertIn("停利 113.0 高於漲停 112.5：重播碰不到", doc["warnings"])

    def test_a_stop_below_limit_down(self):
        doc = machine(plan(limit_up=122.5, limit_down=109.0), replays=500)
        self.assertEqual(doc["mc"]["p_stop"], 0.0)
        self.assertIn("停損 108.5 低於跌停 109.0：重播碰不到，跌停鎖住時也賣不掉", doc["warnings"])

    def test_mis_had_no_limits(self):
        # 2026-11-04: MIS gave 00931B (US Treasuries) no limits and the replay was clamped to 10 % anyway
        doc = machine(plan(limit_up=None, limit_down=None, limits_source="mis"), replays=200)
        self.assertEqual((doc["mc"]["limit_up"], doc["mc"]["limit_down"], doc["mc"]["limits_source"]),
                         (None, None, "mis"))
        self.assertIn("MIS 沒有給漲跌停價（國外成分 ETF 沒有漲跌幅限制）：重播沒有夾在漲跌停之間", doc["warnings"])
        self.assertNotIn("每一根重播都夾在", "\n".join(tm.report(doc)))

    def test_limits_from_the_plans_prev_close(self):
        doc = machine(plan(limit_up=None, limit_down=None, limits_source=None), replays=200)
        self.assertEqual((doc["mc"]["limit_up"], doc["mc"]["limit_down"], doc["mc"]["limits_source"]),
                         (122.5, 100.5, "computed"))
        self.assertIn("plan 沒有漲跌停：用plan 的昨收 111.5 算出 122.5 / 100.5", doc["warnings"])

    def test_limits_from_the_previous_session_in_the_data(self):
        p = tm.normalize_plan(dict(BARE, date="2026-10-30"))
        meta, bars, _ = tm.parse_bars(tm.chart_result(TEXT, "2303"), NOW_UTC)
        up, down, src, note = tm.resolve_limits(p, meta, tm.split_sessions(bars))
        self.assertEqual((up, down, src), (127.0, 104.0, "computed"))
        self.assertIn("2026-10-29 最後一根 5 分 K 的收盤", note)
        self.assertIn("115.5 算出 127.0 / 104.0", note)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        self.assertEqual(machine(replays=2000, seed=5)["mc"], machine(replays=2000, seed=5)["mc"])

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.4328, 0.5637, 109.75))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(58)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "2303")

    def test_the_auction_bar_and_the_close_print_are_left_out(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # 60 empty 13:25 auction bars plus today's 13:30 close print; 2026-10-23's 53 empty bars
        self.assertEqual(dropped, {"no_price": 53, "off_grid": 0, "not_complete": 48, "outside_hours": 61})
        self.assertEqual(max(b["slot"] for b in bars), 52)
        self.assertEqual(sorted({b["start"] for b in bars})[-1], "13:20")
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-11-02", "09:20", 4))
        entry = [b for b in bars if b["date"] == "2026-11-02" and b["slot"] == 3][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:15", 110.0))
        # with the real clock (any time later) the data's own time, 13:30, completes the day
        today = [b for b in tm.parse_bars(self.result, tm.parse_now(None))[1] if b["date"] == "2026-11-02"]
        self.assertEqual(len(today), 53)

    def test_now_pins_the_clock(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-02 09:17"))
        self.assertIn("沒有進場那根 K 棒", str(cm.exception))
        self.assertIn("資料只到 09:10 那根", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-02 09:20"), replays=200)["mc"]["pool_sessions"], 58)

    def test_taipei_has_no_daylight_saving(self):
        stamps = [1785891600,   # 2026-08-05 01:00Z = 09:00 Taipei
                  1793581200,   # 2026-11-02 01:00Z = 09:00 Taipei
                  1793579400,   # 2026-11-02 00:30Z = 08:30, before the open
                  1793597100]   # 2026-11-02 05:25Z = 13:25, the auction
        res = {"meta": {"symbol": "X"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 4, "high": [1] * 4, "low": [1] * 4, "close": [1] * 4,
                                         "volume": [0] * 4}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-11-03T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-08-05", "09:00", 0), ("2026-11-02", "09:00", 0)])
        self.assertEqual(dropped["outside_hours"], 2)

    def test_a_gap_is_left_out(self):
        def cut(res):
            keep_bars(res, lambda ts: not (tpe(ts).date().isoformat() == "2026-09-15" and tpe(ts).hour == 10
                                          and tpe(ts).minute == 0))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertEqual(doc["data"]["short_sessions_left_out"],
                         [{"date": "2026-09-15", "bars": 52},
                          {"date": "2026-10-23", "bars": 0, "closed": "薔琵颱風，台北市停班，全日休市"}])
        self.assertNotIn("2026-09-15", [t["date"] for t in doc["history"]["trades"]])
        text = "\n".join(tm.report(doc))
        self.assertIn("略過休市日：2026-10-23（薔琵颱風，台北市停班，全日休市）。", text)
        self.assertIn("略過 1 個 K 棒不完整的交易日：2026-09-15（52 根）。", text)

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-30")
        self.assertNotIn("2026-11-02", [t["date"] for t in doc["history"]["trades"]])

    def test_a_bar_without_a_price_is_five_quiet_minutes(self):
        # Yahoo lists the stamp with no price when nobody traded: filled flat at the last close
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 54)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)   # 2026-08-05 stays in
        self.assertEqual(doc["data"]["quiet_bars_filled"], {"days": 1, "bars": 1})
        self.assertIn("1 個交易日有沒人成交的 5 分鐘（共 1 根）：用前一根的收盤補成平的 K 棒（量 0）。",
                      "\n".join(tm.report(doc)))

    def test_what_is_not_filled(self):
        bar = lambda k, c: {"date": "2026-10-01", "slot": k, "start": "", "o": c, "h": c, "l": c, "c": c, "v": 1}
        day = {k: bar(k, 29.0 + k / 100) for k in range(tm.SESSION_BARS) if k not in (7, 8)}
        filled = tm.fill_quiet(day, {7, 8})
        self.assertEqual((filled[8]["c"], filled[8]["h"], filled[8]["v"], filled[8]["start"]), (29.06, 29.06, 0, "09:40"))
        self.assertIsNone(tm.fill_quiet(day, {7}))                                     # 8 is missing, not quiet
        self.assertIsNone(tm.fill_quiet({k: b for k, b in day.items() if k}, {0, 7, 8}))   # no 09:00 bar
        thin = {k: b for k, b in day.items() if k < 20}
        self.assertIsNone(tm.fill_quiet(thin, set(range(20, tm.SESSION_BARS)) | {7, 8}))  # a halt, not a quiet stock

    def test_a_thinly_traded_stock_has_enough_days(self):
        # 協易機 4533 on 2026-11-03: quiet five-minute bars left only 9 of its 60 days complete
        with gzip.open(os.path.join(DIR, "4533-60d5m.json.gz"), "rt", encoding="utf-8") as fh:
            text = fh.read()
        p = plan(symbol="4533", market="TPEx", name="協易機", date="2026-11-03", entry=29.75, stop=29.15,
                 target=30.35, opening_range={"high": 29.7, "low": 28.65, "minutes": 15}, risk_per_share=0.6,
                 tick=0.05, risk_ticks=12, prev_close=29.5, limit_up=32.45, limit_down=26.55)
        doc = tm.time_machine(text, p, tm.parse_now("2026-11-03 09:47"), replays=200)
        self.assertGreaterEqual(doc["mc"]["pool_sessions"], 40)
        self.assertGreater(doc["data"]["quiet_bars_filled"]["bars"], 300)
        self.assertEqual([x["date"] for x in doc["data"]["short_sessions_left_out"]], ["2026-10-23"])   # the typhoon day

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "2303.TW"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("沒有 2303 收完的 5 分 K", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: tpe(ts).date().isoformat() >= "2026-10-01")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("只有 19 個完整交易日", str(cm.exception))
        self.assertIn("至少要 20 個", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: tpe(ts).date().isoformat() >= "2026-09-30")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_stock(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="2330"))
        self.assertIn("資料是 2303.TW 的，計畫卻是 2330.TW", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(market="TPEx"))
        self.assertIn("計畫卻是 2303.TWO", str(cm.exception))


class Calendar(unittest.TestCase):
    def test_a_holiday_says_so(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-26"))
        self.assertIn("那天休市：臺灣光復暨金門古寧頭大捷紀念日", str(cm.exception))

    def test_a_day_without_bars(self):
        with self.assertRaises(tm.TMError) as cm:      # 2026-10-23, the typhoon day: Yahoo's bars are empty
            machine(plan(date="2026-10-23"))
        self.assertIn("那天休市：薔琵颱風", str(cm.exception))
        cal = tm.tw.Calendar()
        cal.closed_days.pop("2026-10-23")
        with self.assertRaises(tm.TMError) as cm:      # a calendar that missed it still finds no bars
            machine(plan(date="2026-10-23"), calendar=cal)
        self.assertIn("那天沒有 K 棒", str(cm.exception))
        self.assertNotIn("休市", str(cm.exception))

    def test_an_unknown_year_is_a_note(self):
        doc = machine(calendar=tm.tw.Calendar(), replays=100)
        self.assertEqual(doc["warnings"], [])
        cal = tm.tw.Calendar()
        cal.years = set()
        doc = machine(calendar=cal, replays=100)
        self.assertIn("2026 年的休市日不在清單裡", doc["warnings"][0])


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-03"))
        self.assertIn("那天沒有 K 棒", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 09:25 has not closed in this data yet
            machine(plan(entry_time="09:30", entry_bar=None))
        self.assertIn("沒有進場那根 K 棒", str(cm.exception))

    def test_an_entry_from_mis_before_yahoo_shows_it(self):
        # trader-entry --source mis triggers 20 minutes before Yahoo has the bar (7780 on 2026-11-04);
        # the replay only draws from past sessions, so it runs once the bar has closed by the clock
        text = edited(lambda r: keep_bars(r, lambda ts: tpe(ts).strftime("%Y-%m-%d %H:%M") < "2026-11-02 09:15"))
        doc = machine(text=text, replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertEqual(doc["mc"]["p_target"], machine(replays=200)["mc"]["p_target"])
        self.assertIn("Yahoo 還沒有進場那根 K 棒（09:20 收盤；盤中延遲約 20 分鐘，資料只到 09:10 那根）", doc["warnings"][0])
        with self.assertRaises(tm.TMError) as cm:        # 09:17: the bar has not closed yet
            machine(text=text, now=tm.parse_now("2026-11-02 09:17"))
        self.assertIn("資料只到 09:10 那根", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="09:22", entry_bar=None), "5 分鐘格線"),
            (dict(entry_time="13:25", entry_bar=None), "09:05 到 13:20 之間收盤"),
            (dict(entry_bar=4), "自相矛盾"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "1 分 K 只有大約 7 天"),
            (dict(stop=111.0), "停損 < 進場 < 停利"),
            (dict(side="short"), "buy 或 sell"),
            (dict(symbol="TSLA"), "台股代號"),
            (dict(symbol=""), "台股代號"),
            (dict(market="NYSE"), "TWSE 或 TPEx"),
            (dict(entry="abc"), "要是數字"),
            (dict(date="Nov 2"), "2026-11-02"),
            (dict(opening_range={"minutes": 7}), "5 的倍數"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_symbols_with_a_suffix(self):
        p = tm.normalize_plan(dict(BARE, symbol="2303.TW"))
        self.assertEqual((p["symbol"], p["market"]), ("2303", "TWSE"))
        p = tm.normalize_plan(dict(BARE, symbol="6488.two"))
        self.assertEqual((p["symbol"], p["market"]), ("6488", "TPEx"))

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "09:20")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="9:20", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="9:20 AM", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="1:15", entry_bar=None)["entry_bar"], 50)   # a bare 1 o'clock is 13:15

    def test_until_cuts_the_replay(self):
        m = machine(until="13:20", replays=500)["mc"]
        self.assertEqual(machine(until="1:20 PM", replays=500)["mc"], m)
        for bad, words in (("09:20", "沒有 K 棒可以重播"), ("13:30", "不能晚於 13:25"),
                           ("13:22", "5 分鐘格線"), ("noon", "台北時間")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_prices_off_the_bar_or_the_tick_are_flagged(self):
        doc = machine(plan(entry=112.0, stop=110.5, target=113.5), replays=200)
        self.assertIn("計畫的進場價 112.0 不在那根 K 棒的 108.0 到 110.0 之間", doc["warnings"])
        doc = machine(plan(stop=108.3), replays=200)
        self.assertIn("停損 108.3 不在升降單位上（這個價位一檔是 0.5）", doc["warnings"])

    def test_stops_snap_away_from_the_entry(self):
        self.assertEqual(tm.tick_away((109.5 + 107.5) / 2, "2303", True), 108.5)
        self.assertEqual(tm.tick_away(108.75, "2303", True), 108.5)
        self.assertEqual(tm.tick_away(108.75, "2303", False), 109.0)
        self.assertEqual(tm.tick_away(108.49999999, "2303", True), 108.5)
        self.assertEqual(tm.tick_away(99.95, "2303", True), 99.9)       # under 100 the tick is 0.1
        self.assertEqual(tm.tick_away(45.27, "0050", True), 45.27)      # ETFs under 50 tick 0.01

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=107.5, target=112.5), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=108.0, target=114.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 1.8182, 2.0))
        self.assertEqual(plan(stop_rule="or-mid")["stop_rule"], "or_mid")

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="level"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertIn("歷史：略過", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=111.5, target=108.5, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"]), (0.5665, 0.431))
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("放空", "\n".join(tm.report(doc)))
        self.assertIn("低於進場價", "\n".join(tm.report(doc)))

    def test_a_2r_target(self):
        doc = machine(plan(target=113.0))
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"]), (2.0, 0.2404, 0.6807, 0.0789))
        self.assertEqual((m["break_even"], m["avg_R_net"]), (0.4397, -0.485))
        self.assertEqual((h["target"], h["stop"], h["close"], h["avg_R_net"]), (1, 12, 12, -0.61))
        self.assertEqual(doc["verdict"], "明顯偏向停損：2R 加上成本 0.32R 時，碰到停利或停損的重播裡，停利要先到 44% "
                                         "才打平；實際是 26%（先到停利 24.0% 對先到停損 68.1%）。")


class Verdicts(unittest.TestCase):
    def test_the_thresholds_at_1r_without_costs(self):
        v = lambda pt, ps: tm.verdict({"p_target": pt, "p_stop": ps})  # noqa: E731
        self.assertTrue(v(0.30, 0.26).startswith("接近擲硬幣"))
        self.assertTrue(v(0.40, 0.30).startswith("偏向停利"))
        self.assertTrue(v(0.20, 0.30).startswith("偏向停損"))
        self.assertTrue(v(0.60, 0.30).startswith("明顯偏向停利"))
        self.assertEqual(v(0.10, 0.60), "明顯偏向停損：先到停利 10.0% 對先到停損 60.0%。")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "13:20"}),
                         "到 13:20 沒有任何一次重播碰到停利或停損：每一次都由 13:20 的價格決定。")

    def test_a_2r_target_without_costs(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "接近損益兩平：2R 時，碰到停利或停損的重播裡，停利要先到 33% 才打平；"
                                         "實際是 32%（先到停利 24.1% 對先到停損 50.5%）。")
        self.assertEqual(tm.verdict_notes(mc), [])

    def test_break_even_with_costs(self):
        # 1R with a 0.32R cost needs 66 %: a 55 / 45 split leans toward the stop
        mc = {"p_target": 0.55, "p_stop": 0.45, "r_at_target": 1.0, "cost_r": 0.32}
        gap, share, even = tm.lean(mc)
        self.assertEqual((round(share, 2), round(even, 2)), (0.55, 0.66))
        self.assertTrue(tm.verdict(mc).startswith("偏向停損：1R 加上成本 0.32R 時"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.64, p_stop=0.36)).startswith("接近損益兩平"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.85, p_stop=0.15)).startswith("明顯偏向停利"))

    def test_the_notes_under_the_verdict(self):
        mc = {"p_target": 0.15, "p_stop": 0.40, "p_neither": 0.45, "r_at_target": 1.0, "cost_r": 0.1,
              "avg_R": 0.2, "avg_R_net": 0.1, "until": "13:20"}
        self.assertEqual(tm.verdict_notes(mc), [
            "45.0% 的重播到 13:20 都沒碰到停利或停損，這些由 13:20 的價格決定。",
            "不過扣成本的平均 R 是 +0.100：那些都沒碰到的重播把它拉往另一邊。"])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            host = url.split(".finance")[0].split("//")[1]
            self.calls.append("%s %s" % (host, url.split("/chart/")[1].split("?")[0]))
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("2303.TW"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/2303.TW?range=60d&interval=5m")
        self.assertIn("/chart/6488.TWO?", tm.chart_url("6488.two", "query1"))
        self.assertIn("day-trader-skill-tw", tm.UA)

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("2303.TW"), TEXT)
        self.assertEqual(self.calls, ["query2 2303.TW", "query2 2303.TW", "query1 2303.TW"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("2303.TW"), TEXT)
        self.assertEqual(self.calls, ["query2 2303.TW", "query1 2303.TW"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("2303.TW")
        self.assertIn("限制請求次數", str(cm.exception))

    def test_an_unknown_market_tries_twse_then_tpex(self):
        self.answer([404, 200])
        self.assertEqual(tm.fetch_for("6488", None), (TEXT, "TPEx"))
        self.assertEqual(self.calls, ["query2 6488.TW", "query2 6488.TWO"])
        self.calls[:] = []
        self.answer([200])
        self.assertEqual(tm.fetch_for("2303", None), (TEXT, "TWSE"))
        self.assertEqual(self.calls, ["query2 2303.TW"])

    def test_404_on_both(self):
        self.answer([404, 404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_for("9999", None)
        self.assertIn("請確認代號", str(cm.exception))

    def test_the_universe_names_the_market(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "trader"))
            with open(os.path.join(d, "trader", "universe.txt"), "w", encoding="utf-8") as fh:
                fh.write("2303|聯電|TWSE|股票\n")
            with open(os.path.join(d, "trader", "plan.json"), "w", encoding="utf-8") as fh:
                json.dump(BARE, fh, ensure_ascii=False)
            self.answer([200])
            code, text, err = run(["run", "--plan", os.path.join(d, "trader", "plan.json"),
                                   "--config", os.path.join(d, "trader", "config.json"),
                                   "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["query2 2303.TW"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        self.cfg = os.path.join(self.d, "trader", "config.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(PLAN, fh, indent=2, ensure_ascii=False)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def base(self, *more):
        return ["run", "--plan", self.plan, "--config", self.cfg, "--offline", UMC, "--now", NOW] + list(more)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(self.base("--write", self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("先到停利   43.0%", text)
        self.assertIn(VERDICT, text)
        self.assertIn("已寫入 %s。" % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["avg_R_net"]), (0.4305, -0.135, -0.454))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R_net"]), (25, -0.431))
        self.assertEqual((doc["plan"]["from"], doc["plan"]["name"]), (self.plan, "聯電"))

    def test_the_config_fee_discount(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"fee_discount": 0.6}, fh)
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(BARE, fh)
        code, text, err = run(self.base("--json"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["mc"]["cost_r"], 0.2354)

    def test_json_output(self):
        code, text, _ = run(self.base("--json"))
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.565)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertTrue(doc["verdict"].startswith("明顯偏向停損"))
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "2303", "--date", "2026-11-02", "--entry", "110",
                               "--stop", "108.5", "--target", "111.5", "--entry-time", "09:20",
                               "--config", self.cfg, "--offline", UMC, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["cost_r"]), (0.4305, 0.432, 0.319))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (7, 9, -0.142))
        code, _, err = run(["run", "--symbol", "2303", "--entry", "110", "--offline", UMC])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(UMC, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* 中位數", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     self.base()[:5] + ["--offline", os.path.join(self.d, "missing.json.gz")],
                     self.base("--until", "9:00"),
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(self.base("--write", self.out))
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertEqual(lines[0], "聯電 2303 2026-11-02，從 09:20 進場的 2,000 條重播路徑（亂數種子 19）")
        self.assertTrue(any(l.startswith("   111.50 |") and l.endswith("  停利") for l in lines))
        self.assertTrue(any(l.startswith("   108.50 |") and l.endswith("  停損") for l in lines))
        self.assertTrue(any(l.startswith("   110.00 |*") and l.endswith("  進場") for l in lines))
        self.assertIn("09:20", text)
        self.assertIn("13:25", text)
        self.assertEqual(lines[-1], VERDICT)
        code, again, _ = run(["chart", "--plan", self.plan, "--config", self.cfg, "--offline", UMC, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
