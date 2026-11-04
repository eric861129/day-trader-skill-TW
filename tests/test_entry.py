"""trader-entry tests (Taiwan). Fixtures in tests/fixtures/trader-entry/:

2303-2026-11-02-5m.json  聯電's 2026-11-02 five-minute bars, Yahoo chart JSON v8 range=1d,
                         fetched after the close (16:47 Taipei). The 13:25 auction bar is
                         empty and a 13:30 print of the close carries no volume, as Yahoo
                         serves them.
2303-2026-11-02-1m.json  the same session in one-minute bars (13:25 to 13:29 empty).

Anything else is built in this file from the reel and says SYNTHETIC: the in-progress
bar and live tick of a mid-session answer, a day with no breakout, a late breakout.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")
tw = entry.tw

EDIR = os.path.join(FIX, "trader-entry")
FIVE = os.path.join(EDIR, "2303-2026-11-02-5m.json")
ONE = os.path.join(EDIR, "2303-2026-11-02-1m.json")
UNIVERSE = os.path.join(FIX, "trader-scanner", "universe-2026-11-02.txt")
OPEN_TS = 1793581200       # 2026-11-02 09:00 Taipei
REEL = datetime.date(2026, 11, 2)
DT = datetime.datetime

SAFETY = """## 安全

- 這是教學與工具，不構成投資建議。PASS 只代表通過規則檢查，不是推薦。
- 預設模擬交易（`"paper": true`）。要不要動用真錢，由使用者自己在自己的設定檔決定。
- 這套 skill 不會自己下單。只有 trader-broker 會產生委託單，而且三件事都成立才會：trader-risk 今天通過了這一張單、使用者在對話中針對這一張單明確回覆 yes、使用者自己在券商 App 或下單軟體送出。
- 不做融資、融券、先賣後買、選擇權或期貨，除非設定檔開啟而且使用者確認。
- 券商帳號、密碼和憑證不在對話裡出現，也不寫進任何檔案。
"""


def tpe(text):
    return entry.parse_now(text)


def doc(path=FIVE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def chart(d=None, ysym="2303.TW"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), ysym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), tpe(text), **kw)


def quote(d):
    return d["chart"]["result"][0]["indicators"]["quote"][0]


def shift_to(d, day):
    """The same session moved to another date. Taiwan has no daylight saving: whole days."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    delta = (day - REEL).days * 86400
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def capped(d, first, last, high=109.5, close=109.0):
    """SYNTHETIC: bars first..last (indexes) held under the 109.5 range high."""
    d = copy.deepcopy(d)
    q = quote(d)
    for i in range(first, last + 1):
        if q["close"][i] is None:
            continue
        q["close"][i] = min(q["close"][i], close)
        q["open"][i] = min(q["open"][i], close)
        q["high"][i] = min(q["high"][i], high)
    return d


def no_breakout(d=None):
    """SYNTHETIC: every bar after the range stays under the 109.5 high."""
    d = d if d is not None else doc()
    return capped(d, 3, len(quote(d)["close"]) - 1)


def late_breakout():
    """SYNTHETIC: 09:15 to 10:30 held under the high, so the first close above is the 10:35 bar's."""
    return capped(doc(), 3, 18)


def live_cut(d, hhmmss, tick_close):
    """SYNTHETIC: what Yahoo would show at hhmmss: the closed bars, the bar in progress
    (with its final values) and the live tick at that second."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    h, m, s = (int(x) for x in hhmmss.split(":"))
    t_now = OPEN_TS + (h - 9) * 3600 + m * 60 + s
    keep = [i for i, t in enumerate(r["timestamp"]) if t <= t_now]
    q = r["indicators"]["quote"][0]
    r["timestamp"] = [r["timestamp"][i] for i in keep] + [t_now]
    for k in q:
        q[k] = [q[k][i] for i in keep] + [0 if k == "volume" else tick_close]
    r["meta"]["regularMarketTime"] = t_now
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_0920_on_the_0915_bar(self):
        r = at("2026-11-02 09:20")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["symbol"], r["market"]), ("2303", "TWSE"))
        self.assertEqual(r["opening_range"]["high"], 109.5)
        self.assertEqual(r["opening_range"]["low"], 107.5)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "09:20")
        self.assertEqual(t["bar_start"], "09:15")
        self.assertEqual(t["bar_index"], 3)
        self.assertEqual(t["price"], 110.0)
        self.assertEqual(t["pct_over"], 0.46)
        self.assertFalse(t["at_limit_up"])
        self.assertEqual(r["level"], 109.5)
        self.assertEqual(r["prev_close"], 111.5)
        self.assertEqual((r["limit_up"], r["limit_down"]), (122.5, 100.5))   # what MIS said that day

    def test_at_0917_the_0915_bar_is_still_forming(self):
        r = at("2026-11-02 09:17")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:10")
        self.assertEqual(r["last"]["close"], 109.0)
        self.assertEqual(r["forming"]["bar_start"], "09:15")
        self.assertIn("距開盤區間高點 109.5 還差 0.5 元（1 檔，-0.46%）", r["reason"])
        self.assertEqual(at("2026-11-02 09:19:59")["status"], "waiting")
        text = "\n".join(entry.describe(r))
        self.assertIn("09:15 K 棒還在走（目前 110.0，09:20 收盤）；收盤前不算數。", text)

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-11-02 08:50")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("09:00 開盤", r["reason"])
        r = at("2026-11-02 09:12")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-11-02 09:16")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_auction_bar_and_the_close_print_are_not_bars(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], OPEN_TS + 270 * 60)   # the 13:30 print is in the file
        self.assertEqual(c["bars"][-1]["v"], 0)
        r = entry.evaluate(c, tpe("2026-11-02 13:40"))
        self.assertEqual(r["_bars"][-1]["slot"], 52)                # the 13:20 bar
        self.assertEqual(r["bars_closed"], tw.BARS_5M)              # 53: 09:00 to 13:20
        self.assertEqual(r["phase"], "after-close")

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        d = live_cut(doc(), "09:49:45", 109.0)
        c = chart(d)
        self.assertEqual(c["bars"][-1]["t"], OPEN_TS + 49 * 60 + 45)
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))        # read as of the file's own time
        self.assertEqual(r["now"], "2026-11-02 09:49:45")
        self.assertEqual(r["_bars"][-1]["slot"], 8)                 # the 09:40 bar
        self.assertEqual(r["forming"]["bar_start"], "09:45")
        self.assertEqual(r["bars_closed"], 9)
        # 09:50: closed by the clock, but Yahoo's last trade (09:49:45) is before its close
        r = at("2026-11-02 09:50", d)
        self.assertEqual(r["_bars"][-1]["slot"], 8)
        self.assertIn("Yahoo 最後一筆成交在 09:49:45", r["lag_note"])
        # a minute later with no newer trade it counts; the 09:49:45 tick never becomes a bar
        r = at("2026-11-02 09:51", d)
        self.assertEqual(r["_bars"][-1]["slot"], 9)
        self.assertEqual(r["bars_closed"], 10)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-11-02 13:40")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "09:20")
        text = "\n".join(entry.describe(r))
        self.assertIn("260 分鐘前", text)
        self.assertIn("晚進場是另一筆交易", text)
        self.assertNotIn("現在就是進場時機", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation(self):
        r = at("2026-11-02 09:20", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 7384168)   # the 09:00 bar holds the opening auction
        self.assertEqual(r["status"], "waiting")
        self.assertIn("量只有區間均量的 0.94 倍，低於你設的 1.00 倍", r["reason"])
        r = at("2026-11-02 13:01", min_volume_x=1)
        self.assertEqual(r["status"], "expired")                       # no bar after the range is that heavy
        self.assertIn("到 13:00 都沒有夠量又收在開盤區間高點 109.5 之上的 K 棒", r["reason"])
        r = at("2026-11-02 09:20", min_volume_x=0.9)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["volume_x"]), ("09:20", 0.94))

    def test_level_setup(self):
        r = at("2026-11-02 10:00", setup="level", level=110.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("09:30", 5, 110.5))
        # a level under the open: the first eligible close is the 09:10 bar's, at 09:15 (no_entries_before)
        r = at("2026-11-02 10:00", setup="level", level=100)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:15", 2))
        r = at("2026-11-02 13:40", setup="level", level=110.5)
        self.assertEqual(r["status"], "expired")                       # 110.5 was touched, never closed above
        with self.assertRaises(entry.EntryError):
            at("2026-11-02 10:00", setup="level")

    def test_a_close_at_the_high_is_not_above_it(self):
        d = copy.deepcopy(doc())
        quote(d)["close"][3] = 109.5
        r = at("2026-11-02 09:21", d)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("剛好等於開盤區間高點 109.5，不算站上", r["reason"])

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        self.assertEqual(at("2026-11-02 10:00", d)["status"], "waiting")
        self.assertEqual(at("2026-11-02 12:59", d)["status"], "waiting")
        r = at("2026-11-02 13:00", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("到 13:00 都沒有收在開盤區間高點 109.5 之上", r["reason"])
        self.assertEqual(at("2026-11-02 13:40", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = live_cut(no_breakout(), "10:44:40", 109.0)
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t % 300 == 0]   # no tick: Yahoo is just behind
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        cfg = {"no_entries_before": "09:15", "no_entries_after": "10:45"}
        r = at("2026-11-02 10:45:10", d, cfg=cfg)                    # the 10:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo 最後一筆成交在 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-11-02 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_window(self):
        r = at("2026-11-02 09:20", cfg={"no_entries_before": "09:15", "no_entries_after": "09:15"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-11-02 10:00", cfg={"no_entries_before": "09:25", "no_entries_after": "13:00"})
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:30", 5))   # first close above at or after 09:25

    def test_or_minutes_30(self):
        r = at("2026-11-02 13:40", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (110.5, 107.5))
        self.assertEqual(r["status"], "expired")

    def test_limit_up_close_says_a_buy_may_not_fill(self):
        d = copy.deepcopy(doc())
        q = quote(d)
        q["close"][3] = q["high"][3] = 122.5
        r = at("2026-11-02 09:20", d)
        self.assertTrue(r["trigger"]["at_limit_up"])
        self.assertIn("這根 K 棒收在漲停價 122.5：漲停鎖住時買單可能排隊也買不到。", entry.describe(r))

    def test_prices_snap_to_the_tick(self):
        d = copy.deepcopy(doc())
        quote(d)["close"][3] = 109.99999237060547                     # a float the way Yahoo sends it
        r = at("2026-11-02 09:20", d)
        self.assertEqual(r["trigger"]["price"], 110.0)

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-02 09:20", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-02 09:20", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-11-02 09:20"), DT(2026, 11, 2, 1, 20))
        self.assertEqual(entry.parse_now("2026-11-02T01:20:00Z"), DT(2026, 11, 2, 1, 20))
        self.assertEqual(entry.parse_now("2026-07-01 09:20"), DT(2026, 7, 1, 1, 20))   # no daylight saving


class OneMinute(unittest.TestCase):
    def test_fifteen_bars_make_the_range(self):
        c = chart(doc(ONE))
        r = entry.evaluate(c, tpe("2026-11-02 10:00"), interval="1m")
        orr = r["opening_range"]
        self.assertEqual((orr["high"], orr["low"], orr["bars"], orr["of"]), (109.5, 107.5, 15, 15))
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:19", "09:18", 18, 110.0))
        r = entry.evaluate(c, tpe("2026-11-02 09:18:30"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:18")

    def test_the_auction_minutes_are_dropped(self):
        c = chart(doc(ONE))
        r = entry.evaluate(c, tpe("2026-11-02 13:40"), interval="1m")
        self.assertEqual(r["_bars"][-1]["slot"], 264)               # 13:24, the last minute of continuous trading
        self.assertEqual(r["bars_closed"], 265)

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "2303", "--offline", ONE, "--config", "none/config.json"])
        self.assertEqual(code, 1)
        self.assertIn("1m K 棒，不是 5m", err)


class Calendar(unittest.TestCase):
    def test_another_day_same_rule(self):
        d = shift_to(doc(), datetime.date(2026, 11, 3))
        r = entry.evaluate(chart(d), tpe("2026-11-03 09:20"))
        self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["bar_index"]), ("triggered", "09:20", 3))

    def test_weekend_holiday_typhoon_and_unknown_year(self):
        r = at("2026-10-31 10:00")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("週末", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-02 09:00")
        d = shift_to(doc(), datetime.date(2026, 10, 9))
        r = entry.evaluate(chart(d), tpe("2026-10-09 10:00"))
        self.assertIn("國慶日", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-12 09:00")
        cal = tw.Calendar().add({"2026-11-02": "颱風停止交易"})
        r = entry.evaluate(chart(), tpe("2026-11-02 10:00"), cal=cal)
        self.assertEqual(r["phase"], "closed")
        self.assertIn("颱風停止交易", r["reason"])
        d = shift_to(doc(), datetime.date(2027, 3, 2))
        r = entry.evaluate(chart(d), tpe("2027-03-02 09:20"))
        self.assertFalse(r["calendar_known"])
        self.assertEqual(r["trigger"]["time"], "09:20")
        self.assertIn("2027 年的休市日不在清單裡", "\n".join(entry.describe(r)))

    def test_empty_data_and_yesterdays_bars(self):
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-11-02 09:20", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-03 09:20")                                   # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_yahoo_errors_and_us_charts(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.NoData) as cm:
            entry.parse_chart(bad, "9999.TW")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "2303.TW")
        d = copy.deepcopy(doc())
        d["chart"]["result"][0]["meta"]["symbol"] = "TSLA"
        with self.assertRaises(entry.EntryError) as cm:
            chart(d)
        self.assertIn("不是台股的線圖", str(cm.exception))


class Symbols(unittest.TestCase):
    def test_resolve_with_the_universe(self):
        u = tw.load_universe(UNIVERSE)
        self.assertEqual(entry.resolve("2303", u), ("2303", "TWSE", "聯電"))
        self.assertEqual(entry.resolve("6488", u), ("6488", "TPEx", "環球晶"))
        self.assertEqual(entry.resolve("6488.two", {}), ("6488", "TPEx", None))
        self.assertEqual(entry.resolve("0050", u)[1:], ("TWSE", "元大台灣50"))
        with self.assertRaises(entry.EntryError) as cm:
            entry.resolve("TSLA", u)
        self.assertIn("不像台股代號", str(cm.exception))


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(FIVE, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("2303", "5m", REEL, today=REEL, market="TWSE")
        self.assertEqual(len(c["bars"]), 54)                         # 53 bars and the 13:30 print
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/2303.TW?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("2303", "5m", REEL, today=REEL, market="TWSE")
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("2303", "5m", REEL, today=REEL, market="TWSE")
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_a_bare_code_tries_tw_then_two(self):
        d = doc()
        d["chart"]["result"][0]["meta"]["symbol"] = "6488.TWO"
        self.body = json.dumps(d)
        self.answers(404, 200)
        c = entry.get_chart("6488", "5m", REEL, today=REEL)
        self.assertEqual([u.split("/chart/")[1] for u in self.calls],
                         ["6488.TW?range=1d&interval=5m", "6488.TWO?range=1d&interval=5m"])
        self.assertEqual((c["symbol"], c["market"]), ("6488", "TPEx"))

    def test_unknown_on_both_markets(self):
        self.answers(404, 404)
        with self.assertRaises(entry.NoData) as cm:
            entry.get_chart("9999", "5m", REEL, today=REEL)
        self.assertIn("No data found", str(cm.exception))

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "2303", "--config", "none/config.json"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 11, 3)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(REEL, today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 15), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 15), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 15), today, "1m")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 8, 1), today, "5m")
        self.assertEqual(entry.chart_path("6488.TWO", "1d", "5m"), "/v8/finance/chart/6488.TWO?range=1d&interval=5m")


def live_provider(d, lag_until=None, calls=None):
    """SYNTHETIC: bars the way Yahoo serves them at `now`: closed bars final, the bar in
    progress showing a spike to 111.0 (above the 109.5 high, then it closes where it closed),
    plus a live tick. lag_until: before this UTC time Yahoo's last trade lags 30 seconds."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 111.0, max(b["h"], 111.0)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 111.0, "h": 111.0, "l": 111.0, "c": 111.0, "v": 0})
        return {"symbol": "2303", "market": "TWSE", "name": "聯電", "meta": {}, "bars": bars,
                "prev_close": 111.5, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(tpe(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-11-02 09:12", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        r = fired[0][0]
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("09:20", 110.0))
        self.assertEqual(r["opening_range"]["high"], 109.5)          # the 09:10 spike to 111 never counted
        self.assertEqual([tw.to_taipei(c).strftime("%H:%M:%S") for c in calls], ["09:12:00", "09:15:02", "09:20:02"])
        text = "\n".join(lines)
        self.assertIn("09:10 K 棒  收 109.0  開盤區間定案：高點 109.5，低點 107.5", text)
        self.assertIn("09:15 K 棒  收 110.0  觸發", text)
        self.assertEqual(text.count("觸發："), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = tpe("2026-11-02 09:20:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-11-02 09:17", every=30)
        self.assertEqual(code, 0)
        times = [tw.to_taipei(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:17:00", "09:20:02", "09:20:32"])   # 09:20:02: Yahoo still at 09:19:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "09:20")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-11-02 08:30", every=30)
        self.assertEqual(code, 0)
        self.assertIn("還沒開盤", lines[0])
        self.assertEqual(sum(1 for x in lines if " K 棒  收 " in x), 4)    # 09:00 ... 09:15, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(tpe("2026-11-02 09:46"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(late_breakout(), calls=calls), clock.now, clock.sleep,
                                     lines.append, dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [tw.to_taipei(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:46:00", "10:04:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in ("09:45", "09:50", "09:55", "10:00"):
            self.assertIn(start + " K 棒  收", text)             # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:40")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-02 12:30", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(tw.to_taipei(clock.now()).strftime("%H:%M"), "13:00")
        until = DT(2026, 11, 2, 10, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-02 09:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 10:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("週末", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo 沒有回應")
        code, _, lines, _, _ = self.loop(broken, "2026-11-02 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("連續 5 次讀取失敗", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        shutil.copy(UNIVERSE, os.path.join("trader", "universe.txt"))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "2303", "--offline", FIVE, "--now", "2026-11-02 09:20", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("2303 聯電  2026-11-02（一） 09:20 台北", text)
        self.assertIn("觸發  09:20，價格 110.0：09:15 K 棒（bar 3）收在開盤區間高點 109.5 之上（+0.46%）。", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "2303", "market": "TWSE", "name": "聯電", "setup": "orb",
                "side": "buy", "date": "2026-11-02", "status": "triggered", "time": "09:20", "bar_start": "09:15",
                "bar_index": 3, "price": 110.0, "level": 109.5, "interval": "5m",
                "opening_range": {"high": 109.5, "low": 107.5, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-11-02T01:20:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "2303", "--offline", FIVE, "--now", "2026-11-02 09:17", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["name"], "聯電")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "2303", "--offline", FIVE, "--now", "2026-11-02 09:17", "--write", "e.json"])
        with open("e.json", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("收盤前不算數", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(FIVE, "rb") as src, gzip.open(os.path.join("charts", "2303.TW.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "2303.tw", "--offline", "charts", "--now", "2026-11-02 09:20"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "2330", "--offline", FIVE])
        self.assertEqual(code, 1)
        self.assertIn("這個線圖檔是 2303，不是 2330", err)
        code, _, err = run(["check", "2303.TWO", "--offline", FIVE])
        self.assertIn("不是 2303.TWO", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "2303", "--offline", FIVE, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("目前只規劃做多", err)
        code, _, err = run(["check", "2303", "--offline", FIVE, "--level", "100"])
        self.assertIn("--level 只用在 --setup level", err)
        code, _, err = run(["check", "2303", "--offline", FIVE, "--setup", "level"])
        self.assertIn("需要 --level", err)
        code, _, err = run(["check", "TSLA", "--offline", FIVE])
        self.assertIn("不像台股代號", err)

    def test_config_is_read(self):
        with open(os.path.join("trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"no_entries_after": "09:15"}, fh)
        code, text, _ = run(["check", "2303", "--offline", FIVE, "--now", "2026-11-02 09:20"])
        self.assertEqual(code, 1)
        self.assertIn("過期", text)
        with open(os.path.join("trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"extra_closed": {"2026-11-02": "颱風停止交易"}}, fh)
        code, text, _ = run(["check", "2303", "--offline", FIVE, "--now", "2026-11-02 09:20"])
        self.assertEqual(code, 1)
        self.assertIn("今天休市（颱風停止交易）", text)
        with open(os.path.join("trader", "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "2303", "--offline", FIVE])
        self.assertIn("不是有效的 JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "2303", "--offline", FIVE, "--now", "2026-11-02 09:10", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:15 K 棒  收 110.0  觸發：收在 109.5 之上（+0.46%）。要做就現在進場：09:20，110.0", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bar_index"], 3)
        code, text, _ = run(["watch", "2303", "--offline", FIVE, "--now", "2026-11-02 09:10", "--until", "09:15",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(FIVE, os.path.join("charts", "2303.json"))
        shutil.copy(FIVE, os.path.join("charts", "2317.json"))
        with open(os.path.join("trader", "watchlist.json"), "w", encoding="utf-8") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "2303", "market": "TWSE", "name": "聯電", "kind": "股票", "setups": ["orb"]},
                {"symbol": "6488", "market": "TPEx", "name": "環球晶", "setups": ["orb"]},
                {"symbol": "2317", "setups": ["orb"]}]}, fh, ensure_ascii=False)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-02 09:20"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("2303    觸發      09:20  110.0     109.5     110.0     +0.46%    聯電"), rows[1])
        self.assertIn("6488    錯誤：charts 裡沒有 6488 的離線檔", rows[2])
        self.assertIn("2317    錯誤：這個線圖檔是 2303，不是 2317", rows[3])
        self.assertIn("3 檔中有 1 檔觸發", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-02 09:17", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("先跑 /trader-scanner", err)


MIS_SESSION = os.path.join(FIX, "trader-alerts", "session", "mis-2026-11-03-0941.json")
MIS_0920 = os.path.join(FIX, "trader-entry", "mis-2026-11-04-0920.json")
MIS_DAY = datetime.date(2026, 11, 3)


def mis_snap(at, high, low, trade, trade_at, lots):
    return {"at": at, "high": high, "low": low, "open": 29.0, "trade": trade, "trade_at": trade_at,
            "bid": None, "ask": None, "volume_lots": lots}


# SYNTHETIC, shaped like 協易機 4533 on 2026-11-03: Yahoo's range was 29.65 to 28.65 and the
# 09:15 bar closed at 29.75 (09:20 trigger); the 09:41:30 row is the real MIS answer. A row
# whose trade is at its own time is the snapshot that showed that price; a later row with the
# same volume proves nothing traded since.
MIS_ROWS = {"0905": mis_snap("09:05:01", 29.4, 28.65, 29.0, "09:05:01", 400),
            "0914": mis_snap("09:14:58", 29.65, 28.65, 29.55, "09:14:58", 1100),
            "0915": mis_snap("09:15:02", 29.65, 28.65, 29.55, "09:14:58", 1100),
            "0919": mis_snap("09:19:35", 29.7, 28.65, 29.7, "09:19:35", 1300),
            "091952": mis_snap("09:19:52", 29.75, 28.65, 29.75, "09:19:52", 1350),
            "0920": mis_snap("09:20:04", 29.75, 28.65, 29.75, "09:19:52", 1350),
            "0941": mis_snap("09:41:30", 30.25, 28.65, 29.7, "09:40:15", 2096)}


def mis_stock(*keys, **extra):
    snaps = [MIS_ROWS[k] for k in keys] + list(extra.pop("snaps", []))
    return dict({"name": "協易機", "market": "TPEx", "prev_close": 29.5, "snaps": snaps}, **extra)


def mis_eval(stock, now, **kw):
    return entry.evaluate_mis(stock, "4533", tpe("2026-11-03 " + now), **kw)


class MisMode(unittest.TestCase):
    """--source mis: the opening range at 09:15 and the 09:20 close in real time, not 20 minutes late."""

    def test_parse_mis_reads_the_last_trade_when_the_snapshot_has_none(self):
        with open(MIS_SESSION, encoding="utf-8") as fh:
            q = tw.parse_mis(fh.read())
        self.assertEqual((q["3665"]["last"], q["3665"]["trade_price"], q["3665"]["trade_time"]), (None, 1665.0, "09:38:32"))
        self.assertEqual((q["4533"]["high"], q["4533"]["low"], q["4533"]["trade_price"], q["4533"]["time"]),
                         (30.25, 28.65, 29.7, "09:41:30"))

    def test_the_0920_trigger_at_0920(self):
        r = mis_eval(mis_stock("0905", "0914", "0915", "0919", "091952", "0920"), "09:20:10")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"], r["opening_range"]["exact"]),
                         (29.65, 28.65, True))
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"], t["exact"]), ("09:20", "09:15", 3, 29.75, True))
        text = "\n".join(entry.describe(r))
        self.assertIn("（證交所 MIS 盤中快照，可能落後幾秒到一兩分鐘），資料到 09:20:04", text)
        self.assertIn("開盤區間 09:00-09:15：高點 29.65，低點 28.65（MIS 09:15:02 快照裡的當日最高最低）。", text)
        self.assertIn("觸發  09:20，價格 29.75：09:15 K 棒（bar 3）收在開盤區間高點 29.65 之上", text)
        self.assertIn("現在就是進場時機", text)
        self.assertNotIn("data_note", r)

    def test_a_trade_just_before_the_close_stands_in_for_it(self):
        r = mis_eval(mis_stock("0915", "0919"), "09:21")
        self.assertEqual((r["status"], r["trigger"]["price"], r["trigger"]["exact"]), ("triggered", 29.7, False))
        self.assertIn("收盤價取自 09:19:35 的成交", r["data_note"])

    def test_a_bar_no_snapshot_saw_is_said(self):
        busy = mis_snap("09:22:30", 29.8, 28.65, 29.8, "09:22:10", 1500)
        r = mis_eval(mis_stock("0915", snaps=[busy]), "09:22:40")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["unknown_bars"], ["09:15"])
        self.assertIn("09:15 這 1 根的 K 棒收盤沒有快照看到", r["reason"])
        self.assertIn("entry.py watch --source mis", r["reason"])
        self.assertEqual(r["forming"]["price"], 29.8)

    def test_a_quiet_stock_shows_its_close_later(self):
        shown = mis_snap("09:18:00", 29.8, 28.65, 29.8, "09:18:00", 1200)
        thin = mis_snap("09:30:05", 29.8, 28.65, 29.8, "09:18:00", 1200)
        r = mis_eval(mis_stock("0915", snaps=[shown, thin]), "09:30:10")
        self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["exact"]), ("triggered", "09:20", True))
        self.assertEqual(r["unknown_bars"], [])            # 09:20 and 09:25 had no trade
        self.assertEqual(r["minutes_since"], 10)
        r = mis_eval(mis_stock("0915", snaps=[thin]), "09:30:10")      # the volume then is not known
        self.assertEqual((r["status"], r["unknown_bars"]), ("waiting", ["09:15", "09:20", "09:25"]))

    def test_the_same_volume_twice_is_a_quiet_stretch(self):
        still = mis_snap("09:26:00", 29.65, 28.65, 29.55, "09:14:58", 1100)
        r = mis_eval(mis_stock("0915", snaps=[still]), "09:26:10")
        self.assertEqual(r["unknown_bars"], ["09:15"])                # 09:15:02 to 09:26:00 quiet: the 09:20 bar
        r = mis_eval(mis_stock("0914", "0915", snaps=[still]), "09:26:10")
        self.assertEqual((r["status"], r["unknown_bars"]), ("waiting", []))
        self.assertIn("還沒有看得到收盤的 K 棒", r["reason"])

    def test_a_trade_seen_late_still_stands_in_for_its_close(self):
        # from the MIS log of 2026-11-04, 大研生醫 7780: 15.05 at 09:24:57, first shown in the
        # 09:25:53 snapshot; 18 more lots traded by 09:27:33, so it is not the exact 09:20 close
        rows = [("09:14:07", 15.0, 14.85, 15.0, "09:10:53", 56), ("09:17:07", 15.0, 14.85, 15.0, "09:10:53", 57),
                ("09:20:14", 15.0, 14.85, 15.0, "09:10:53", 87), ("09:25:53", 15.05, 14.85, 15.05, "09:24:57", 141),
                ("09:27:33", 15.05, 14.85, 15.05, "09:24:57", 159)]
        stock = {"name": "大研生醫", "market": "TPEx", "prev_close": 14.9, "snaps": [mis_snap(*row) for row in rows]}
        r = entry.evaluate_mis(stock, "7780", tpe("2026-11-04 09:27:40"))
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["exact"]), (15.0, True))
        self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["price"], r["trigger"]["exact"]),
                         ("triggered", "09:25", 15.05, False))
        self.assertEqual(r["unknown_bars"], ["09:15"])
        self.assertIn("收盤價取自 09:24:57 的成交（09:25 收盤前，MIS 09:25:53 的快照看到）", r["data_note"])
        self.assertIn("09:15 這 1 根的 K 棒收盤沒有快照看到", r["data_note"])

    def test_a_repeated_trade_proves_nothing_once_the_volume_grows(self):
        # REAL, 2026-11-04: 台積電's last shown trade stayed 2300 at 09:17:25 while 113 lots
        # traded; 環球晶 showed 636 at 09:19:57 with 6,636 lots and had 6,651 at 09:20:02
        with open(MIS_0920, encoding="utf-8") as fh:
            answers = json.load(fh)["answers"]
        day, log = datetime.date(2026, 11, 4), None
        for a in answers:
            log = entry.log_mis(log, tw.parse_mis(json.dumps(a)), day)
        snaps = lambda code: sorted((dict(s, _at=entry._at(day, s["at"])) for s in log["stocks"][code]["snaps"]),
                                    key=lambda s: s["_at"])
        self.assertEqual(entry.mis_closes(snaps("2330"), day, 300), ({}, set()))
        closes, quiet = entry.mis_closes(snaps("6488"), day, 300)
        self.assertEqual((sorted(closes), closes[3]["c"], closes[3]["exact"], closes[3]["trade_at"]),
                         ([3], 636.0, False, "09:19:57"))
        self.assertEqual(quiet, set())

    def test_the_range_from_a_snapshot_a_little_late(self):
        self.assertTrue(mis_eval(mis_stock("0905", "0914", "0915"), "09:16")["opening_range"]["exact"])
        # a trade shown at 09:14:58 does not prove nothing traded until 09:15:02
        self.assertFalse(mis_eval(mis_stock("0905", "0915"), "09:16")["opening_range"]["exact"])
        late = mis_snap("09:15:20", 29.7, 28.65, 29.7, "09:15:10", 1150)
        r = mis_eval(mis_stock("0905", snaps=[late]), "09:16")
        self.assertFalse(r["opening_range"]["exact"])
        self.assertIn("多含了 09:15 到 09:15:20 之間的成交", "\n".join(entry.describe(r)))
        same = mis_snap("09:15:40", 29.4, 28.65, 29.3, "09:15:30", 1150)        # same high and low as 09:05
        self.assertTrue(mis_eval(mis_stock("0905", snaps=[same]), "09:16")["opening_range"]["exact"])

    def test_too_late_for_the_range(self):
        r = mis_eval(mis_stock("0941"), "09:42")
        self.assertEqual((r["status"], r["final"]), ("no-data", True))
        self.assertIn("不能當開盤區間", r["reason"])
        shown = mis_snap("09:14:30", 29.4, 28.65, 29.3, "09:14:30", 900)
        thin = mis_snap("09:41:30", 29.4, 28.65, 29.3, "09:14:30", 900)        # nothing traded since 09:14:30
        self.assertEqual(mis_eval(mis_stock(snaps=[shown, thin]), "09:42")["opening_range"]["high"], 29.4)
        self.assertEqual(mis_eval(mis_stock(snaps=[thin]), "09:42")["status"], "no-data")

    def test_before_the_range_ends(self):
        r = mis_eval(mis_stock("0905"), "09:10")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertIn("目前高點 29.40，低點 28.65（MIS 到 09:05:01，還在形成）", "\n".join(entry.describe(r)))
        self.assertEqual(mis_eval(mis_stock(), "08:50")["phase"], "pre-open")
        with self.assertRaises(entry.EntryError):
            mis_eval(mis_stock("0915"), "09:20", min_volume_x=1.5)

    def test_a_snapshot_without_a_new_trade_moves_the_last_one_forward(self):
        q = {"time": "09:20:04", "date": "20261103", "name": "協易機", "market": "TPEx", "high": 29.75, "low": 28.65,
             "open": 29.0, "trade_price": 29.75, "trade_time": "09:19:52", "volume_lots": 1350, "bid": 29.7, "ask": 29.75}
        log = entry.log_mis(None, {"4533": q}, MIS_DAY)
        log = entry.log_mis(log, {"4533": dict(q, time="09:20:14", bid=29.65)}, MIS_DAY)
        self.assertEqual([(s["at"], s["bid"]) for s in log["stocks"]["4533"]["snaps"]], [("09:20:14", 29.65)])
        log = entry.log_mis(log, {"4533": dict(q, time="09:20:24", trade_price=29.8, trade_time="09:20:20",
                                                volume_lots=1352)}, MIS_DAY)
        self.assertEqual([s["at"] for s in log["stocks"]["4533"]["snaps"]], ["09:20:14", "09:20:24"])
        log = entry.log_mis(log, {"4533": dict(q, time="09:20:34", date="20261102")}, MIS_DAY)   # another day
        self.assertEqual(len(log["stocks"]["4533"]["snaps"]), 2)
        shown = dict(q, time="09:20:40", trade_price=29.8, trade_time="09:20:40", volume_lots=1353)
        for t in ("09:20:40", "09:20:50", "09:21:00"):
            log = entry.log_mis(log, {"4533": dict(shown, time=t)}, MIS_DAY)
        # the snapshot that showed the price stays: its volume is the proof
        self.assertEqual([s["at"] for s in log["stocks"]["4533"]["snaps"]][2:], ["09:20:40", "09:21:00"])

    def test_a_long_list_is_asked_fifty_channels_at_a_time(self):
        asked = []
        real_open, real_sleep = entry._open_url, entry._sleep
        entry._open_url = lambda url, timeout=20: asked.append(url) or '{"msgArray": []}'
        entry._sleep = lambda s: None
        try:
            entry.mis_get([("%04d" % (1000 + i), "TWSE") for i in range(60)] + [("4533", None)])
        finally:
            entry._open_url, entry._sleep = real_open, real_sleep
        self.assertEqual([len(u.split("ex_ch=")[1].split("&")[0].split("|")) for u in asked], [50, 12])
        self.assertIn("tse_4533.tw|otc_4533.tw", asked[1])

    def test_yesterdays_trade_is_not_today(self):
        pre = dict(mis_snap("08:59:50", None, None, 30.0, "13:30:00", 0))
        r = mis_eval(mis_stock(snaps=[pre, MIS_ROWS["0915"]]), "09:16")
        self.assertEqual(r["opening_range"]["high"], 29.65)
        self.assertEqual(entry.mis_closes([dict(pre, _at=DT(2026, 11, 3, 8, 59, 50))], MIS_DAY, 300), ({}, set()))


class MisCli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))
        shutil.copy(UNIVERSE, os.path.join("trader", "universe.txt"))
        self.log = os.path.join("trader", "state", "mis-2026-11-03.json")
        with open(self.log, "w", encoding="utf-8") as fh:
            json.dump({"date": "2026-11-03", "source": "mis",
                       "stocks": {"4533": mis_stock("0905", "0914", "0915", "0919", "091952", "0920", "0941")}},
                      fh, ensure_ascii=False)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_replays_todays_log_and_writes_the_entry(self):
        code, text, _ = run(["check", "4533", "--source", "mis", "--now", "2026-11-03 09:20:10", "--write"])
        self.assertEqual(code, 0, text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        self.assertEqual((e["source"], e["time"], e["price"], e["opening_range"]),
                         ("mis", "09:20", 29.75, {"high": 29.65, "low": 28.65, "minutes": 15}))
        code, text, _ = run(["check", "4533", "--source", "mis", "--offline", self.log])   # as of its newest snapshot
        self.assertEqual(code, 0)
        self.assertIn("09:41 台北", text)
        self.assertIn("那是 21 分鐘前的事", text)

    def test_watch_replays_the_log_from_the_open(self):
        code, text, _ = run(["watch", "4533", "--source", "mis", "--offline", self.log, "--every", "30"])
        self.assertEqual(code, 0, text)
        self.assertIn("開盤區間 09:00-09:15：高點 29.65，低點 28.65", text)
        self.assertIn("09:15 K 棒  收 29.75  觸發", text)           # at 09:20:00, from the 09:19:52 snapshot
        self.assertIn("收盤價取自 09:19:52 的成交", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["source"], "mis")
        code, _, err = run(["watch", "4533", "--source", "mis", "--every", "3"])
        self.assertEqual(code, 1)
        self.assertIn("至少 5 秒", err)

    def test_watchlist_is_one_request(self):
        with open(os.path.join("trader", "watchlist.json"), "w", encoding="utf-8") as fh:
            json.dump({"hits": [{"symbol": "4533", "setups": ["orb"]}, {"symbol": "2303", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--source", "mis", "--now", "2026-11-03 09:20:10"])
        self.assertEqual(code, 0, text)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("4533    觸發      09:20  29.75"), rows[1])
        self.assertTrue(rows[2].startswith("2303    沒資料"), rows[2])
        self.assertIn("證交所 MIS 盤中快照", text)

    def test_live_adds_a_snapshot_to_todays_log(self):
        os.remove(self.log)
        with open(MIS_SESSION, encoding="utf-8") as fh:
            body = fh.read()
        asked = []
        real_open, real_now = entry._open_url, entry.utc_now

        def fake_open(url, timeout=20):
            asked.append(url)
            return body
        entry._open_url = fake_open
        entry.utc_now = lambda: DT(2026, 11, 3, 1, 41, 20)            # 09:41:20, ten seconds behind MIS
        try:
            code, text, _ = run(["check", "4533", "--source", "mis"])
        finally:
            entry._open_url, entry.utc_now = real_open, real_now
        self.assertEqual(code, 1)
        self.assertEqual(len(asked), 1)
        self.assertIn("ex_ch=otc_4533.tw", asked[0])
        self.assertIn("09:41 台北", text)
        self.assertIn("不能當開盤區間", text)
        with open(self.log, encoding="utf-8") as fh:
            snaps = json.load(fh)["stocks"]["4533"]["snaps"]
        self.assertEqual(snaps, [{"at": "09:41:30", "high": 30.25, "low": 28.65, "open": 29.0, "trade": 29.7,
                                  "trade_at": "09:40:15", "bid": 29.75, "ask": 29.8, "volume_lots": 2096}])


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertTrue(text.endswith(SAFETY), "the Safety block must close the file, word for word")
        for must in ("K 棒", "可能延遲", "大多數當沖的人", "/trader-exit", "只做多", "背景", "/loop",
                     "什麼單都沒有下", "09:20", "110.0", "bar 3", "0.94 倍", "零股", "漲停", "13:25", "13:00"):
            self.assertIn(must, text)
        for never in ("\u2014", "即時", "獲利", "穩賺", "保證"):
            self.assertNotIn(never, text)


if __name__ == "__main__":
    unittest.main()
