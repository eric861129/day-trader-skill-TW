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

scan = load("trader-scanner", "scan")
tw = scan.tw

SDIR = os.path.join(FIX, "trader-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-11-02.json.gz")        # 240 real v8 5-min charts, Mon 2026-11-02
SPARK5 = os.path.join(SDIR, "spark-5m-2026-11-02.json.gz")    # the same 240 as Yahoo's spark answer (closes)
DAILY = os.path.join(SDIR, "daily-spark-2026-11-02.json.gz")  # 3 months of daily closes for the 240, spark
ISIN = os.path.join(SDIR, "isin")                             # excerpts of the real ISIN pages of 2026-11-02
UNIVERSE = os.path.join(SDIR, "universe-2026-11-02.txt")      # the full pages through the same rule
TWFIX = os.path.join(FIX, "twmarket")
DT = datetime.datetime
D = datetime.date
DAY = D(2026, 11, 2)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(text):
    """Taipei wall-clock text -> unix seconds."""
    return tw.taipei_to_ts(DT.strptime(text, "%Y-%m-%d %H:%M:%S" if text.count(":") == 2 else "%Y-%m-%d %H:%M"))


def bars_series(day, closes, highs=None, lows=None, start="09:00", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.5 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.5 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


class Universe(unittest.TestCase):
    def setUp(self):
        self.pages = {}
        for market, mode in scan.ISIN_PAGES:
            with open(os.path.join(ISIN, scan.ISIN_FILE % mode), "rb") as fh:
                self.pages[market] = fh.read().decode("cp950")
        self.kept, self.kept_by, self.dropped, self.rows, self.updated = scan.build_universe(self.pages)

    def test_the_kept_list(self):
        self.assertEqual(self.rows, 311)
        self.assertEqual(self.updated, "2026/11/02")
        self.assertEqual(len(self.kept), 264)
        self.assertEqual(self.kept_by, {"TWSE": {"股票": 156, "創新板": 7, "ETF": 34},
                                        "TPEx": {"股票": 60, "ETF": 7}})
        rows = {r[0]: r for r in self.kept}
        self.assertEqual(rows["2303"], ("2303", "聯電", "TWSE", "股票"))
        self.assertEqual(rows["6488"], ("6488", "環球晶", "TPEx", "股票"))
        self.assertEqual(rows["0050"], ("0050", "元大台灣50", "TWSE", "ETF"))
        self.assertEqual(rows["00631L"][3], "ETF")
        self.assertEqual(rows["7610"], ("7610", "聯友金屬-創", "TWSE", "創新板"))

    def test_what_is_left_out(self):
        self.assertEqual(self.dropped, {
            ("TWSE", "上市認購(售)權證"): 6, ("TWSE", "特別股"): 6, ("TWSE", "ETN"): 6,
            ("TWSE", "臺灣存託憑證(TDR)"): 6, ("TWSE", "受益證券-不動產投資信託"): 6,
            ("TPEx", "上櫃認購(售)權證"): 6, ("TPEx", "ETN"): 6, ("TPEx", "特別股"): 1,
            ("TPEx", "受益證券-資產基礎證券"): 4})
        self.assertEqual(sum(self.dropped.values()) + len(self.kept), self.rows)
        self.assertEqual(scan.drop_label("TPEx", "上櫃認購(售)權證"), "上櫃認購(售)權證")
        self.assertEqual(scan.drop_label("TWSE", "ETN"), "上市 ETN")
        codes = [r[0] for r in self.kept]
        self.assertNotIn("1101B", codes)                    # 台泥乙特, a preferred share
        self.assertNotIn("9103", codes)                     # a TDR

    def test_the_full_pages_through_the_same_rule(self):
        u = tw.load_universe(UNIVERSE)
        self.assertEqual(len(u), 2350)
        by = {}
        for v in u.values():
            by[(v["market"], v["kind"])] = by.get((v["market"], v["kind"]), 0) + 1
        self.assertEqual(by, {("TWSE", "股票"): 1058, ("TWSE", "創新板"): 31, ("TWSE", "ETF"): 245,
                              ("TPEx", "股票"): 897, ("TPEx", "ETF"): 119})
        self.assertTrue(all(tw.CODE.match(c) for c in u))

    def test_not_an_isin_page(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_isin("<html>maintenance</html>")
        with self.assertRaises(scan.ScanError):
            scan.build_universe({"TWSE": self.pages["TWSE"]})


class Time(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(scan.ts_to_taipei(ts("2026-11-02 09:00")), DT(2026, 11, 2, 9, 0))
        self.assertEqual(scan.parse_now("2026-11-02 09:45"), DT(2026, 11, 2, 1, 45))
        self.assertEqual(scan.parse_now("2026-07-01 09:45"), DT(2026, 7, 1, 1, 45))   # no daylight saving
        self.assertEqual(scan.parse_now("2026-11-02T01:45:00Z"), DT(2026, 11, 2, 1, 45))
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_watch_day(self):
        self.assertEqual(scan.watch_day(scan.parse_now("2026-11-02 08:30")), D(2026, 11, 2))
        self.assertEqual(scan.watch_day(scan.parse_now("2026-11-02 13:30")), D(2026, 11, 3))
        self.assertEqual(scan.watch_day(scan.parse_now("2026-10-31 20:00")), D(2026, 11, 2))    # Saturday
        self.assertEqual(scan.watch_day(scan.parse_now("2026-10-23 15:00")), D(2026, 10, 27))   # 10-26 光復節補假
        self.assertEqual(scan.watch_day(scan.parse_now("2026-02-11 14:00")), D(2026, 2, 23))    # 春節


class Spark(unittest.TestCase):
    def setUp(self):
        self.spark, _ = scan.load_offline(SPARK5)
        self.v8, _ = scan.load_offline(WALL)

    def test_a_real_spark_answer_is_put_back_together(self):
        self.assertEqual(len(self.spark), 240)
        s = self.spark["2303.TW"]
        self.assertIsNone(s["h"])                           # spark is closes only
        self.assertEqual(s["meta"]["regularMarketVolume"], 131534402)
        self.assertEqual(s["filled"], 16)                   # Yahoo sent 39 of 55 points
        self.assertEqual(len(s["t"]), 55)
        closes = dict((scan.ts_to_taipei(t).strftime("%H:%M"), c) for t, c in zip(s["t"], s["c"]))
        self.assertEqual(closes["09:05"], 108.5)            # 108.0, (108.5), 109.0: on the line, left out
        self.assertEqual(closes["10:40"], 109.5)
        self.assertEqual(closes["11:10"], 110.0)            # a run of equal closes

    def test_the_filled_points_match_the_v8_charts(self):
        checked = 0
        for sym, s in self.spark.items():
            got = dict(zip(s["t"], s["c"]))
            for t, c in zip(self.v8[sym]["t"], self.v8[sym]["c"]):
                if c is None or scan.ts_to_taipei(t).time() >= tw.CLOSING_AUCTION:
                    continue
                self.assertIn(t, got, sym)
                self.assertAlmostEqual(got[t], c, delta=0.001, msg=sym)
                checked += 1
        self.assertEqual(checked, 12591)
        self.assertEqual(sum(s["filled"] for s in self.spark.values()), 2120)

    def test_daily_points_are_put_back_by_trading_day(self):
        data, _ = scan.load_offline(DAILY)
        s = data["2303.TW"]
        self.assertEqual(s["filled"], 2)
        closes = dict(scan.daily_closes(s, scan.parse_now("2026-11-02 08:30")))
        self.assertEqual(len(closes), 60)
        # v8 daily closes, read 2026-11-02: 123.0 and 133.0 (by bar count 10-16 would wrongly be 132.5)
        self.assertEqual(closes[D(2026, 8, 11)], 123.0)
        self.assertEqual(closes[D(2026, 10, 16)], 133.0)
        self.assertNotIn(D(2026, 10, 26), closes)          # a market holiday is never filled
        self.assertEqual(sum(x["filled"] for x in data.values()), 193)

    def test_a_closed_day_bar_from_yahoo_is_dropped(self):
        # 2026-10-23 was a typhoon day with no trading; Yahoo still sends a bar, yesterday's close again
        data, _ = scan.load_offline(DAILY)
        s = data["2303.TW"]
        self.assertEqual(s["offdays"], ["2026-10-23"])
        self.assertNotIn(ts("2026-10-23 09:00"), s["t"])
        closes = dict(scan.daily_closes(s, scan.parse_now("2026-11-02 08:30")))
        self.assertEqual((closes[D(2026, 10, 22)], closes[D(2026, 10, 27)]), (125.5, 122.5))
        self.assertNotIn(D(2026, 10, 23), closes)
        self.assertEqual(sum(1 for x in data.values() if x["offdays"]), 235)
        # a v8-style daily series (no densify) loses the same bar in daily_closes
        v8 = {"t": [ts("2026-10-22 09:00"), ts("2026-10-23 09:00"), ts("2026-10-27 09:00")],
              "c": [125.5, 125.5, 122.5], "meta": {}}
        self.assertEqual(scan.daily_closes(v8, scan.parse_now("2026-11-02 08:30")),
                         [(D(2026, 10, 22), 125.5), (D(2026, 10, 27), 122.5)])
        self.assertEqual(scan.offdays(v8), {"2026-10-23"})

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls(self):
        self.assertEqual(scan.spark_path(["2303.TW", "6488.two"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=2303.TW,6488.TWO&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("2303.TW", "1d", "5m"), "/v8/finance/chart/2303.TW?range=1d&interval=5m")


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = scan._read_maybe_gz(SPARK5)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=2303.TW&range=1d&interval=5m"), self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/2303.TW?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        real = sorted(scan.parse_spark(self.body))[:19]
        syms = real + ["9999.TW"] + ["X%02d.TW" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "1d", "5m", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["9999.TW"], "Yahoo 沒有資料")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00.TW"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/9999.TW?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Closes through Fri 2026-10-30, the pre-market scan for Mon 2026-11-02 (the 10-23 typhoon-day bar dropped).
    EXPECT = {
        "2308": ["oversold"], "2303": ["oversold"], "3665": ["oversold"], "1519": ["oversold"],
        "8210": ["oversold"], "3211": ["oversold"], "3376": ["oversold"], "4966": ["oversold"],
        "2379": ["breakout_20d"], "2891": ["breakout_20d"], "2603": ["breakout_20d"],
        "2352": ["breakout_20d"], "2890": ["breakout_20d"], "2412": ["breakout_20d"], "00632R": ["breakout_20d"],
        "1101": ["breakout_20d"], "6199": ["breakout_20d"], "7704": ["breakout_20d"], "2892": ["breakout_20d"],
        "2801": ["breakout_20d"], "4526": ["breakout_20d"], "5880": ["breakout_20d"], "00687B": ["breakout_20d"],
        "2880": ["breakout_20d"], "00679B": ["breakout_20d"], "00937B": ["breakout_20d"],
        "1301": ["bull_flag"], "5876": ["bull_flag"],
        "2615": ["reclaim_ma20"],
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)
        self.uni = tw.load_universe(UNIVERSE)

    def test_setups_before_the_open(self):
        self.assertEqual(len(self.syms), 240)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-02 08:30"), 0, 0,
                                             uni=self.uni)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 10, 30))
        self.assertEqual(counts, {"requested": 240, "fetched": 240, "illiquid": 0, "short": 1, "scanned": 239,
                                  "offday": 235, "offday_dates": ["2026-10-23"], "hits": 29})
        umc = [h for h in hits if h["symbol"] == "2303"][0]
        self.assertEqual(umc, {"symbol": "2303", "market": "TWSE", "name": "聯電", "kind": "股票",
                               "setups": ["oversold"], "price": 111.5, "close_date": "2026-10-30", "rsi": 27.8,
                               "volume": 131534402})
        self.assertEqual(scan.offday_line(counts), "Yahoo 的日線裡有 235 檔帶著休市日的 K 棒（2026-10-23，"
                                                   "收盤價照抄前一天），已略過，不算進型態。")

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-02 08:30"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-02 13:29"), 0, 0)
        self.assertEqual(before, during)
        bars = scan.daily_closes(self.data["2303.TW"], scan.parse_now("2026-11-02 13:29"))
        self.assertEqual(bars[-1], (D(2026, 10, 30), 111.5))
        bars = scan.daily_closes(self.data["2303.TW"], scan.parse_now("2026-11-02 13:30"))
        self.assertEqual(bars[-1], (D(2026, 11, 2), 110.0))

    def test_liquidity_filter(self):
        # the fixture was saved after Monday's close, so its volume is Monday's whole day
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-02 08:30"), 10, 1000000)
        self.assertEqual((counts["illiquid"], counts["scanned"], counts["hits"]), (24, 216, 29))

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:00" % d) for d in range(1, 11)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X.TW": s, "E.TW": {"t": [], "c": [], "meta": {}}},
                                             ["X.TW", "E.TW", "GONE.TW"], scan.parse_now("2026-10-20 08:30"),
                                             10, 1000000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))
        cls.uni = tw.load_universe(UNIVERSE)

    def scan_at(self, now, min_price=10, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(13, 0))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0, uni=self.uni)

    def test_the_reel_scan_at_0945(self):
        hits, counts, failed = self.scan_at("2026-11-02 09:45")
        self.assertEqual(len(self.syms), 240)
        self.assertEqual(self.errors, {})
        self.assertEqual(counts, {"requested": 240, "fetched": 240, "unusable": 3, "illiquid": 5, "scanned": 232,
                                  "candidates": 161, "confirmed": 161, "unconfirmed": 0, "hits": 70})
        umc = [h for h in hits if h["symbol"] == "2303"][0]
        self.assertEqual(umc, {"symbol": "2303", "market": "TWSE", "name": "聯電", "kind": "股票", "setups": ["orb"],
                               "price": 110.0, "at": "09:20", "bar_start": "09:15", "bar_index": 3,
                               "or_high": 109.5, "or_low": 107.5, "pct_over": 0.46})
        self.assertEqual(hits[0]["symbol"], "00662")       # sorted by time, then code
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))
        self.assertEqual(sum(1 for h in hits if h["at"] == "09:20"), 17)
        tpex = [h for h in hits if h["symbol"] == "6488"][0]
        self.assertEqual((tpex["market"], tpex["at"], tpex["or_high"]), ("TPEx", "09:30", 763.0))

    def test_limit_up_is_flagged(self):
        hits, _, _ = self.scan_at("2026-11-02 09:45")
        self.assertEqual([h["symbol"] for h in hits if h.get("at_limit_up")], ["6271"])   # 同欣電 at 216.5
        self.assertTrue(scan.at_limit_up("6271", 216.5, self.data["6271.TW"]["meta"]))
        self.assertFalse(scan.at_limit_up("2303", 110.0, self.data["2303.TW"]["meta"]))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-11-02 09:45", min_price=0)
        self.assertEqual(len(hits), 71)                    # 009828 at 8.96 is back in
        self.assertEqual(counts["illiquid"], 0)
        self.assertIn("009828", [h["symbol"] for h in hits])

    def test_unusable_charts(self):
        day, cut = DAY, DT(2026, 11, 2, 9, 45)
        bad = [s for s in self.syms if not scan.usable(scan.session_bars(self.data[s], day, cut), day, cut, 3)]
        self.assertEqual(bad, ["2059.TW", "5269.TW", "5274.TWO"])

    def test_in_progress_bar(self):
        day = DAY
        bars = scan.session_bars(self.data["2303.TW"], day, DT(2026, 11, 2, 9, 24))
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars], ["09:00", "09:05", "09:10", "09:15"])
        hits, _, _ = self.scan_at("2026-11-02 09:20", syms=["2303.TW"])
        self.assertEqual(hits[0]["at"], "09:20")

    def test_the_auction_bar_and_the_close_print_are_left_out(self):
        s = self.data["2303.TW"]
        tail = [(scan.ts_to_taipei(t).strftime("%H:%M"), c, v) for t, c, v in zip(s["t"], s["c"], s["v"])][-3:]
        self.assertEqual(tail, [("13:20", 110.0, 2032466), ("13:25", None, None), ("13:30", 110.0, 0)])
        bars = scan.session_bars(s, DAY, DT(2026, 11, 2, 13, 30))
        self.assertEqual(len(bars), tw.BARS_5M)
        self.assertEqual(bars[-1]["start"], DT(2026, 11, 2, 13, 20))
        self.assertTrue(scan.usable(bars, DAY, DT(2026, 11, 2, 13, 30), 3))

    def test_a_live_tick_is_dropped(self):
        s = bars_series("2026-11-02", [100, 100.5, 101, 101.5, 102], extra=[("2026-11-02 09:26:47", 102.5)])
        bars = scan.session_bars(s, DAY, DT(2026, 11, 2, 9, 27))
        self.assertEqual(len(bars), 5)                     # 09:00 to 09:20; the 09:26:47 tick is not a bar

    def test_live_yahoo_is_about_20_minutes_behind(self):
        # 2026-11-03 09:22: the newest point was 09:02:35, so every slot to 09:22 was never there
        s = bars_series("2026-11-02", [100], extra=[("2026-11-02 09:02:35", 100.5)])
        self.assertEqual(scan.data_cutoff({"2303.TW": s}, DAY, DT(2026, 11, 2, 9, 22)),
                         (DT(2026, 11, 2, 9, 2), 20))
        s = bars_series("2026-11-02", [100, 100.5, 101, 101.5], extra=[("2026-11-02 09:21:10", 102)])
        cut = DT(2026, 11, 2, 9, 22)
        self.assertEqual(scan.data_cutoff({"2303.TW": s}, DAY, cut), (cut, None))   # a minute behind is live

    def test_stage_one_is_a_necessary_condition(self):
        for cut in (DT(2026, 11, 2, 9, 45), DT(2026, 11, 2, 13, 0)):
            for sym, s in self.data.items():
                bars = scan.session_bars(s, DAY, cut)
                if scan.usable(bars, DAY, cut, 3) and scan.orb_hit(bars, 3):
                    self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_spark_then_v8(self):
        spark, _ = scan.load_offline(SPARK5)
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        day, cut = DAY, DT(2026, 11, 2, 9, 45)
        hits, counts, failed = scan.scan_orb(spark, sorted(spark), day, cut, 15, 10, 0, confirm, uni=self.uni)
        ref, _, _ = self.scan_at("2026-11-02 09:45")
        self.assertEqual(hits, ref)
        self.assertEqual((counts["candidates"], counts["confirmed"]), (162, 161))
        self.assertEqual(len(asked), 162)
        self.assertEqual(list(failed.values()), ["v8 圖少了 K 棒"])   # usable in spark, a bar short in v8

        def broken(sym):
            raise scan.ScanError("Yahoo 回應 HTTP 429（/v8/finance/chart/%s）" % sym)
        hits, counts, failed = scan.scan_orb(spark, ["2303.TW", "2330.TW"], day, cut, 15, 10, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["2303.TW"])

    def test_stage_two_cap_takes_the_most_traded(self):
        spark, _ = scan.load_offline(SPARK5)
        day, cut = DAY, DT(2026, 11, 2, 9, 45)
        hits, counts, _ = scan.scan_orb(spark, sorted(spark), day, cut, 15, 10, 0, lambda s: self.data[s],
                                        max_confirm=10)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (10, 152))
        self.assertEqual([h["symbol"] for h in hits], ["3711", "1303", "2308", "3037"])
        everything, _, _ = scan.scan_orb(spark, sorted(spark), day, cut, 15, 10, 0, lambda s: self.data[s])
        for h in hits:
            self.assertIn(h, everything)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-11-02", [100, 100.5, 101, None, 102, 102.5, 103, 103.5, 104])
        bars = scan.session_bars(s, DAY, DT(2026, 11, 2, 9, 45))
        self.assertFalse(scan.usable(bars, DAY, DT(2026, 11, 2, 9, 45), 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.5, 101, 100.5, 102] + [101.0] * 53      # 60 bars, to 13:55
        s = bars_series("2026-11-03", closes, highs=[c + 0.5 for c in closes])
        hits, counts, _ = scan.scan_orb({"X.TW": s}, ["X.TW"], D(2026, 11, 3), DT(2026, 11, 3, 12, 0), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        # range 09:00 to 09:30 (six bars, high 101.5); the 09:30 bar closes 102 at 09:35
        self.assertEqual((hits[0]["at"], hits[0]["or_high"], hits[0]["price"]), ("09:35", 101.5, 102.0))
        bars = scan.session_bars(s, D(2026, 11, 3), DT(2026, 11, 3, 13, 30))
        self.assertEqual(len(bars), tw.BARS_5M)            # the grid stops at the 13:20 bar
        self.assertFalse(scan.usable(bars[:-1], D(2026, 11, 3), DT(2026, 11, 3, 13, 30), 6))

    def test_window_messages(self):
        until = datetime.time(13, 0)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("週末", msg("2026-10-31 09:45"))
        self.assertIn("臺灣光復暨金門古寧頭大捷紀念日", msg("2026-10-26 09:45"))
        self.assertIn("請在 09:20 之後再跑 orb", msg("2026-11-02 09:10"))
        self.assertIn("請在 09:20 之後再跑 orb", msg("2026-11-02 09:19"))
        self.assertIn("過期", msg("2026-11-02 13:05"))
        self.assertIn("過期", msg("2026-11-02 11:30", until=datetime.time(11, 0)))
        self.assertIn("已在 13:30 收盤", msg("2026-11-02 13:40"))
        self.assertIn("比現在（09:30）還晚", msg("2026-11-02 09:30", at=datetime.time(9, 45)))
        self.assertIn("13:30 收盤之後", msg("2026-11-02 15:00", at=datetime.time(13, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-02 16:30"), datetime.time(9, 45), 15, until),
                         (DAY, DT(2026, 11, 2, 9, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-02 09:20"), None, 15, until)[1],
                         DT(2026, 11, 2, 9, 20))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-02 09:45"), None, 7, until)

    def test_an_unknown_year_still_runs_on_weekdays(self):
        day, cut = scan.orb_window(scan.parse_now("2027-03-01 09:45"), None, 15, datetime.time(13, 0))
        self.assertEqual(day, D(2027, 3, 1))
        self.assertIn("2027", tw.Calendar().unknown_note(day))


class Disposition(unittest.TestCase):
    def setUp(self):
        self.disp = scan.load_disposition(SDIR)

    def test_periods_and_numbers(self):
        self.assertEqual(scan.parse_period("115/10/28～115/11/03"), (D(2026, 10, 28), D(2026, 11, 3)))
        self.assertEqual(scan.parse_period("1151102~1151110"), (D(2026, 11, 2), D(2026, 11, 10)))
        self.assertIsNone(scan.parse_period("soon"))
        self.assertEqual([scan._cn_int(x) for x in ("二", "五", "十", "二十", "2", "２０")], [2, 5, 10, 20, 2, 20])

    def test_both_lists(self):
        self.assertEqual(len(self.disp), 14)               # 3 TWSE rows, 11 TPEx rows
        self.assertEqual(self.disp["4526"], {"name": "東台", "market": "TWSE", "start": D(2026, 10, 30),
                                             "end": D(2026, 11, 9), "every_minutes": 2})
        self.assertEqual(self.disp["3374"], {"name": "精材", "market": "TPEx", "start": D(2026, 11, 2),
                                             "end": D(2026, 11, 10), "every_minutes": 2})
        self.assertEqual(len(scan.active_disposition(self.disp, DAY)), 14)
        self.assertEqual(sorted(scan.active_disposition(self.disp, D(2026, 11, 6))),   # 2466 and 4577 end 11/5
                         ["3374", "3441", "3691", "36913", "4526", "8099"])
        self.assertEqual(scan.active_disposition(self.disp, D(2026, 11, 11)), {})

    def test_the_reel_holds_out_4526(self):
        hits = [{"symbol": "2303"}, {"symbol": "4526"}]
        kept, held = scan.apply_disposition(hits, self.disp, DAY, False)
        self.assertEqual(kept, [{"symbol": "2303"}])
        self.assertEqual([c for c, _ in held], ["4526"])
        self.assertEqual(scan.disposition_line(held, False, {}),
                         "處置股不列入（1 檔）：4526 東台（處置到 11/9，約每 2 分鐘撮合一次）。")
        kept, held = scan.apply_disposition(hits, self.disp, DAY, True)
        self.assertEqual(kept[1]["disposition"], {"until": "2026-11-09", "every_minutes": 2})
        self.assertEqual(scan.apply_disposition(hits, None, DAY, False), (hits, []))


class Symbols(unittest.TestCase):
    def test_resolve_and_settle(self):
        uni = {"6488": {"name": "環球晶", "market": "TPEx", "kind": "股票"}}
        syms, pairs = scan.resolve([("2303", "TWSE"), ("6488", None), ("3105", None)], uni)
        self.assertEqual(syms, ["2303.TW", "6488.TWO", "3105.TW", "3105.TWO"])
        self.assertEqual(pairs, {"3105.TW": "3105.TWO"})
        failed = {"3105.TW": "Yahoo 沒有資料"}
        self.assertEqual(scan.settle(syms, pairs, {"2303.TW": 1, "6488.TWO": 1, "3105.TWO": 1}, failed),
                         ["2303.TW", "6488.TWO", "3105.TWO"])
        self.assertEqual(failed, {})
        failed = {"3105.TW": "x", "3105.TWO": "x"}
        self.assertEqual(scan.settle(syms, pairs, {}, failed), ["2303.TW", "6488.TWO", "3105.TW"])
        self.assertEqual(failed, {"3105.TW": "x"})

    def test_lists(self):
        d = tempfile.mkdtemp()
        try:
            txt = os.path.join(d, "list.txt")
            with open(txt, "w", encoding="utf-8") as fh:
                fh.write("# my list\n2303|聯電|TWSE|股票\n6488.two\n\n2330\n")
            self.assertEqual(scan.items_from_file(txt), [("2303", "TWSE"), ("6488", "TPEx"), ("2330", None)])
            js = os.path.join(d, "wl.json")
            with open(js, "w", encoding="utf-8") as fh:
                json.dump({"hits": [{"symbol": "2303", "market": "TWSE"}, {"symbol": "6488", "market": "TPEx"}]}, fh)
            self.assertEqual(scan.items_from_file(js), [("2303", "TWSE"), ("6488", "TPEx")])
        finally:
            shutil.rmtree(d)
        with self.assertRaises(scan.ScanError):
            scan.parse_items("TSLA")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self._default = scan.DEFAULT_UNIVERSE
        scan.DEFAULT_UNIVERSE = os.path.join(self.t, "universe.txt")   # names come from the project's list

    def tearDown(self):
        scan.DEFAULT_UNIVERSE = self._default
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", ISIN, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("保留 264 檔：上市 156 檔股票、7 檔創新板、34 檔 ETF；上櫃 60 檔股票、7 檔 ETF。", text)
        self.assertIn("離線模式：沒有更新休市日。", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 264)
        self.assertIn("2303|聯電|TWSE|股票", lines)
        self.assertIn("6488|環球晶|TPEx|股票", lines)

        # daily over that universe with the fixture charts: 24 of its names have no chart, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-11-02 08:30",
                             "--out", self.wl, "--disposition", SDIR])
        self.assertEqual(code, 0)
        self.assertIn("抓到 264 檔中的 240 檔圖（24 檔失敗或沒有資料）", text)
        self.assertIn("處置股不列入（1 檔）：4526 東台", text)
        self.assertIn("Yahoo 的日線裡有 235 檔帶著休市日的 K 棒（2026-10-23", text)
        self.assertIn("28 檔符合型態：17 檔收盤創 20 日新高、2 檔多頭旗形、1 檔站回 20 日均線、8 檔超賣", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["for_session"]),
                         ("daily", 264, 216, 24, "2026-11-02"))
        self.assertEqual(doc["as_of"], "2026-10-30 收盤")
        self.assertEqual(doc["disposition"], {"checked": True, "held": ["4526"]})
        self.assertEqual(len(doc["hits"]), 28)
        self.assertEqual(doc["hits"][1]["symbol"], "2303")

        # the reel's 09:45 scan: the charts come from the file, the names from trader/universe.txt
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-02 09:45", "--out", self.wl,
                             "--disposition", SDIR])
        self.assertEqual(code, 0)
        self.assertIn("抓到 240 檔中的 240 檔圖；232 檔可用（另有 5 檔低於股價或成交量門檻）；161 檔候選", text)
        self.assertIn("232 檔可用的圖裡，有 69 檔在 09:45 前收盤站上開盤區間高點：", text)
        self.assertIn("  09:20  2303       110.0  區間 107.5 到 109.5  高出 0.46%  聯電", text)
        self.assertIn("同欣電  漲停：可能買不到", text)
        self.assertIn("處置股不列入（1 檔）：4526 東台（處置到 11/9，約每 2 分鐘撮合一次）。", text)
        self.assertIn("不是買進清單", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source", "disposition")},
                         {"created_by": "trader-scanner", "created_at": "2026-11-02T01:45:00Z", "mode": "orb",
                          "as_of": "2026-11-02 09:45 台北", "universe": 240, "scanned": 232, "source": "yahoo",
                          "disposition": {"checked": True, "held": ["4526"]}})
        self.assertEqual(len(doc["hits"]), 69)
        self.assertIn({"symbol": "2303", "market": "TWSE", "name": "聯電", "kind": "股票", "setups": ["orb"],
                       "price": 110.0, "at": "09:20", "bar_start": "09:15", "bar_index": 3, "or_high": 109.5,
                       "or_low": 107.5, "pct_over": 0.46}, doc["hits"])

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "09:45", "--watchlist", self.wl, "--json",
                             "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (69, 69, 69))
        self.assertFalse(out["disposition"]["checked"])

    def test_universe_refreshes_the_calendar(self):
        def get_bytes(url, timeout=120):
            mode = url.rsplit("=", 1)[1]
            with open(os.path.join(ISIN, scan.ISIN_FILE % mode), "rb") as fh:
                return fh.read()

        def get(url, timeout=20):
            name = "rwd-holidaySchedule-%s.json" % url.split("date=")[1][:4]
            with open(os.path.join(TWFIX, name), encoding="utf-8") as fh:
                return fh.read()
        old = scan.http_get_bytes, scan.http_get, scan.SLEEP
        scan.http_get_bytes, scan.http_get, scan.SLEEP = get_bytes, get, (lambda s: None)
        try:
            code, text, _ = run(["universe", "--out", os.path.join(self.t, "universe.txt"),
                                 "--now", "2026-11-02 08:00"])
        finally:
            scan.http_get_bytes, scan.http_get, scan.SLEEP = old
        self.assertEqual(code, 0)
        self.assertIn("2026 年的休市日已更新：18 個平日休市。", text)
        self.assertIn("證交所還沒公布 2027 年的開休市日期。", text)
        self.assertTrue(os.path.isfile(os.path.join(self.t, "state", "calendar-2026.json")))
        self.assertFalse(os.path.exists(os.path.join(self.t, "state", "calendar-2027.json")))
        self.assertTrue(tw.load_calendar(self.t).known(2026))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the 2026-11-02 files."""
        bundle = json.loads(scan._read_maybe_gz(WALL))
        spark = json.loads(scan._read_maybe_gz(SPARK5))
        by_sym = {r["symbol"]: r for r in spark["spark"]["result"]}
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                want = path.split("symbols=")[1].split("&")[0].split(",")
                return json.dumps({"spark": {"result": [by_sym[s] for s in want if s in by_sym], "error": None}})
            return json.dumps(bundle[path.split("/chart/")[1].split("?")[0]])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-02 09:50")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-11-02 09:45", "--max-confirm", "10",
                                 "--no-write", "--disposition", "none"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("235 檔可用（另有 5 檔低於股價或成交量門檻）；162 檔候選，10 檔用真實高低點確認", text)
        self.assertIn("只看了一部分：還有 152 檔候選沒確認", text)
        self.assertIn("確認的 10 檔裡，有 4 檔在 09:45 前收盤站上開盤區間高點", text)
        self.assertIn("沒有檢查處置股（--disposition none）。", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 10)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 12)   # 240 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))

    def delayed_orb(self, now, behind=20):
        """The networked path with Yahoo answering only the points `behind` minutes before now."""
        spark = json.loads(scan._read_maybe_gz(SPARK5))
        edge = ts(now) - behind * 60
        by_sym = {}
        for r in spark["spark"]["result"]:
            resp = r["response"][0]
            keep = [i for i, t in enumerate(resp["timestamp"]) if t <= edge]
            resp["timestamp"] = [resp["timestamp"][i] for i in keep]
            q = resp["indicators"]["quote"][0]
            q["close"] = [q["close"][i] for i in keep]
            by_sym[r["symbol"]] = r

        charts = json.loads(scan._read_maybe_gz(WALL))
        for doc in charts.values():
            res = doc["chart"]["result"][0]
            keep = [i for i, t in enumerate(res["timestamp"]) if t <= edge]
            res["timestamp"] = [res["timestamp"][i] for i in keep]
            q = res["indicators"]["quote"][0]
            for k in q:
                q[k] = [q[k][i] for i in keep]

        def get(path):
            if path.startswith("/v8/"):
                return json.dumps(charts[path.split("/chart/")[1].split("?")[0]])
            want = path.split("symbols=")[1].split("&")[0].split(",")
            return json.dumps({"spark": {"result": [by_sym[s] for s in want if s in by_sym], "error": None}})
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or now)
        try:
            return run(["orb", "--symbols", "2303,2330", "--now", now, "--no-write", "--disposition", "none",
                        "--max-confirm", "0", "--json"]), run(["orb", "--symbols", "2303,2330", "--now", now,
                                                                "--no-write", "--disposition", "none"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now

    def test_a_delayed_feed_is_said_not_read_as_no_breakout(self):
        (code, out, _), (_, text, _) = self.delayed_orb("2026-11-02 09:30")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["data_delay_minutes"], 20)
        self.assertIn("Yahoo 的台股盤中資料延遲約 20 分鐘：最新只到 09:10", text)
        self.assertIn("區間後的第一根 K 棒 09:20 才收完，加上延遲約 09:40 才看得到", text)
        self.assertNotIn("沒有圖收盤站上", text)
        # 20 minutes later the range and the 09:15 bar are there: the scan runs as of 09:25
        (code, out, _), (_, text, _) = self.delayed_orb("2026-11-02 09:45")
        doc = json.loads(out)
        self.assertEqual((doc["as_of"], doc["data_delay_minutes"]), ("2026-11-02 09:25 台北", 20))
        self.assertEqual(doc["counts"]["scanned"], 2)

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-31 09:45", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("2026-10-31（六）休市（週末）", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-02 09:14", "--out", self.wl])
        self.assertIn("09:20 之後", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"no_entries_after": "09:30"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-02 09:45", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("過期", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-02 09:45", "--symbols", "2330",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout, but the empty list is written
        self.assertIn("到 09:45 為止，沒有圖收盤站上開盤區間高點。", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("沒有離線檔案", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w", encoding="utf-8") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("沒有可以掃描的代號", err)
        code, _, err = run(["daily", "--offline", DAILY, "--symbols", "TSLA"])
        self.assertIn("不像台股代號", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for name, sym in (("2303.TW", "2303.TW"), ("6488.TWO", "6488.TWO"), ("2330", "2330.TW")):
            with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump(bundle[sym], fh)
        with open(os.path.join(folder, "9999.TW.json"), "w", encoding="utf-8") as fh:
            json.dump({"chart": {"result": None, "error": {"description": "No data found"}}}, fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-02 09:45", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([(h["symbol"], h["market"]) for h in out["hits"]], [("2303", "TWSE"), ("6488", "TPEx")])
        self.assertEqual(code, 0)

    def test_kinds_limits_and_lists(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--universe", UNIVERSE, "--kind", "ETF", "--now",
                             "2026-11-02 08:30", "--json", "--no-write", "--min-volume", "0"])
        out = json.loads(text)
        self.assertTrue(out["hits"])
        self.assertEqual({h["kind"] for h in out["hits"]}, {"ETF"})
        self.assertEqual(out["universe"], 364)              # every ETF on the list, 240 charts or not
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("2303\n6488.TWO\n")
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-02 08:30",
                             "--json", "--no-write", "--min-volume", "0"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-02 08:30", "--json",
                             "--no-write", "--min-volume", "0"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-02 08:30", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 29)               # 4526 stays in: offline, no disposition list given
        self.assertEqual(out["disposition"], {"checked": False, "held": []})
        self.assertEqual(out["note"], "掃描結果是符合規則的圖，不是買進清單。")


if __name__ == "__main__":
    unittest.main()
