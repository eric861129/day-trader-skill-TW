import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

alerts = load("trader-alerts", "alerts")
tw = load("trader-alerts", "twmarket")

ADIR = os.path.join(FIX, "trader-alerts")
MIS = os.path.join(FIX, "twmarket", "mis-2026-11-02.json")              # 2330, 6488, 0050, 00631L at 13:30
MIS_BOTH = os.path.join(ADIR, "mis-2303-6488-2026-11-02.json")        # tse_ and otc_ channels for both
MIS_PRE = os.path.join(ADIR, "premarket", "mis-2026-11-03-0841.json")  # 2303, 2308, 1815, 2317 before the open
MIS_SESSION = os.path.join(ADIR, "session", "mis-2026-11-03-0941.json")  # 3665, 4533, 5202, 2614 at 09:41
CHART_1M = os.path.join(FIX, "trader-entry", "2303-2026-11-02-1m.json")
CHART_5M = os.path.join(FIX, "trader-entry", "2303-2026-11-02-5m.json")
CHART_6488 = os.path.join(ADIR, "yahoo-6488.TWO-1m.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-2303.json")
CAL = os.path.join(FIX, "twmarket", "rwd-holidaySchedule-%d.json")
D = datetime.date


def now(text):
    return tw.parse_now(text)


def status(text, cal=None):
    return alerts.market_status(now(text), cal)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def quote(price, **kw):
    q = {"symbol": "2303", "market": "TWSE", "name": "聯電", "price": price, "prev_close": 111.5,
         "limit_up": 122.5, "limit_down": 100.5, "time": "2026-11-02T01:42:00Z", "when": "", "source": "mis"}
    q.update(kw)
    return q


class Clock(unittest.TestCase):
    def test_the_session_phases_in_taipei_time(self):
        for t, phase, is_open in (("07:00", "before", False), ("08:30", "pre_open", False), ("08:59", "pre_open", False),
                                  ("09:00", "continuous", True), ("13:24", "continuous", True),
                                  ("13:25", "closing_auction", True), ("13:29", "closing_auction", True),
                                  ("13:30", "after", False), ("20:00", "after", False)):
            st = status("2026-11-02 " + t)
            self.assertEqual((st["phase"], st["open"]), (phase, is_open), t)
            self.assertTrue(st["trading_day"])
        st = status("2026-11-02 09:00")
        self.assertEqual((st["opens"], st["odd_lot_first_match"], st["closing_auction"], st["closes"]),
                         ("09:00", "09:10", "13:25", "13:30"))
        self.assertEqual(st["weekday"], "一")
        self.assertEqual(st["now_taipei"], "2026-11-02 09:00")

    def test_taipei_is_utc_plus_8_whatever_the_machine_says(self):
        self.assertEqual(alerts.market_status(datetime.datetime(2026, 11, 2, 1, 42))["now_taipei"], "2026-11-02 09:42")
        self.assertTrue(status("2026-11-02T01:42:00Z")["open"])

    def test_next_open(self):
        self.assertEqual(status("2026-11-02 08:45")["next_open"], "2026-11-02（一） 09:00")
        self.assertEqual(status("2026-11-02 13:31")["next_open"], "2026-11-03（二） 09:00")
        self.assertEqual(status("2026-10-30 14:00")["next_open"], "2026-11-02（一） 09:00")   # Friday -> Monday

    def test_weekends_holidays_and_typhoon_days_are_closed(self):
        st = status("2026-11-01 10:00")
        self.assertEqual((st["open"], st["phase"], st["reason"]), (False, "closed", "週末"))
        st = status("2026-10-23 10:00")
        self.assertFalse(st["trading_day"])
        self.assertIn("薔琵颱風", st["reason"])
        # typhoon Friday, weekend, 光復節 observed on Monday: the next open is Tuesday
        self.assertEqual(st["next_open"], "2026-10-27（二） 09:00")
        self.assertIn("巴威颱風", status("2026-07-10 09:30")["reason"])
        self.assertEqual(status("2026-10-09 10:00")["reason"], "國慶日")

    def test_an_unpublished_year_says_so(self):
        st = status("2027-01-04 09:30")
        self.assertTrue(st["open"])
        self.assertFalse(st["calendar_known"])
        self.assertIn("calendar --update 2027", st["note"])
        self.assertIn("2027", status("2026-12-31 14:00")["note"])     # the next open is in 2027
        self.assertNotIn("note", status("2026-11-02 09:30"))

    def test_extra_closed_and_cached_years_from_the_trader_folder(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"extra_closed": {"2026-11-03": "颱風停止交易"}}, fh, ensure_ascii=False)
        tw.save_calendar(2027, {"2027-01-01": "中華民國開國紀念日"}, d)
        _, cal = alerts.load_context(cfg)
        self.assertEqual(alerts.market_status(now("2026-11-03 10:00"), cal)["reason"], "颱風停止交易")
        self.assertEqual(alerts.market_status(now("2026-11-02 13:31"), cal)["next_open"], "2026-11-04（三） 09:00")
        self.assertTrue(cal.known(2027))
        self.assertEqual(alerts.trader_dir("trader/config.json"), "trader")


class Quotes(unittest.TestCase):
    def test_mis_quotes(self):
        q = alerts.mis_quotes(read(MIS))
        self.assertEqual(sorted(q), ["0050", "00631L", "2330", "6488"])
        self.assertEqual(q["6488"]["market"], "TPEx")
        self.assertEqual((q["2330"]["price"], q["2330"]["prev_close"], q["2330"]["limit_up"], q["2330"]["limit_down"]),
                         (2330.0, 2370.0, 2605.0, 2135.0))
        self.assertEqual(q["2330"]["time"], "2026-11-02T05:30:00Z")        # 13:30:00 Taipei
        self.assertEqual(q["2330"]["when"], "13:30:00 成交")

    def test_mis_answers_only_the_channel_that_exists(self):
        # the real answer to tse_2303|otc_2303|tse_6488|otc_6488|tse_9999: blanks for the wrong ones
        q = alerts.mis_quotes(read(MIS_BOTH))
        self.assertEqual(sorted(q), ["2303", "6488"])
        self.assertEqual((q["2303"]["market"], q["2303"]["price"], q["2303"]["name"]), ("TWSE", 110.0, "聯電"))
        self.assertEqual(q["6488"]["market"], "TPEx")

    def test_before_the_first_trade_mis_has_the_reference_and_todays_limits(self):
        # the real 08:41 answer on 2026-11-03: no trade yet, yesterday's close, today's limits, the trial match
        q = alerts.mis_quotes(read(MIS_PRE))
        self.assertEqual(sorted(q), ["1815", "2303", "2308", "2317"])
        u = q["2303"]
        self.assertEqual((u["price"], u["prev_close"], u["limit_up"], u["limit_down"], u["trial"], u["no_trade"]),
                         (110.0, 110.0, 121.0, 99.0, 107.0, True))
        self.assertEqual(u["time"], "2026-11-03T00:41:49Z")
        self.assertEqual(alerts.quote_line("2303", u),
                         "聯電 2303（上市）  110.0  昨收，今天還沒有成交  試撮 107.0（-2.73%）  "
                         "漲停 121.0／跌停 99.0  08:41:49 的報價  [mis]")
        self.assertIn("試撮 1,265（-6.64%）", alerts.quote_line("2308", q["2308"]))
        self.assertEqual(q["1815"]["market"], "TPEx")
        self.assertNotIn("no_trade", alerts.mis_quotes(read(MIS_BOTH))["2303"])   # it traded: the last trade

    def test_in_the_session_a_snapshot_without_a_trade_reads_the_best_bid(self):
        # 2026-11-03 09:41: MIS left out 3665's last trade; Yahoo said 1,605 (09:21), MIS bid 1,660
        q = alerts.mis_quotes(read(MIS_SESSION))
        b = q["3665"]
        self.assertEqual((b["price"], b["book"], b["bid"], b["ask"]), (1660.0, "bid", 1660.0, 1665.0))
        self.assertEqual(b["when"], "09:39:57 委買價（這一刻沒有成交價）")
        self.assertEqual((q["5202"]["price"], q["5202"]["ask"]), (16.3, None))   # locked limit up: market bids first
        self.assertEqual((q["2614"]["price"], q["2614"].get("book")), (13.6, None))   # a trade in the snapshot
        self.assertEqual(tw._best("0.0000_16.3000_16.2500_"), 16.3)
        self.assertIsNone(tw._best("-"))

    def test_a_yahoo_trade_from_an_earlier_day_says_which_day(self):
        doc = read(CHART_1M)
        self.assertEqual(alerts.chart_quote(doc, now("2026-11-02 14:00"))[1]["when"], "13:30:01 成交")
        self.assertEqual(alerts.chart_quote(doc, now("2026-11-03 08:39"))[1]["when"], "11-02 13:30:01 成交")

    def test_a_replay_reads_the_last_bar_that_ended(self):
        doc = read(CHART_1M)
        self.assertEqual(alerts.chart_quote(doc, now("2026-11-02 08:59")), ("2303", None))
        code, q = alerts.chart_quote(doc, now("2026-11-02 09:41"))
        self.assertEqual((code, q["price"], q["when"]), ("2303", 109.0, "09:40 那根 1 分 K 收盤"))
        _, q = alerts.chart_quote(doc, now("2026-11-02 09:42"))
        self.assertEqual((q["price"], q["when"], q["time"]), (108.5, "09:41 那根 1 分 K 收盤", "2026-11-02T01:42:00Z"))
        self.assertEqual((q["prev_close"], q["limit_up"], q["limit_down"], q["market"]), (111.5, 122.5, 100.5, "TWSE"))
        _, q = alerts.chart_quote(doc, now("2026-11-02 13:31"))   # past its own time: the live price
        self.assertEqual((q["price"], q["time"]), (110.0, "2026-11-02T05:30:01Z"))
        _, q = alerts.chart_quote(doc)                           # no clock: the live price
        self.assertEqual(q["price"], 110.0)
        _, q = alerts.chart_quote(read(CHART_5M), now("2026-11-02 09:42"))
        self.assertEqual((q["price"], q["when"]), (109.5, "09:35 那根 5 分 K 收盤"))   # the 09:40 bar is not done

    def test_offline_files_and_folders(self):
        q, err = alerts.get_quotes(["2303", "6488", "2330", "0050"], "mis", [ADIR, MIS])
        self.assertEqual(err, [])
        self.assertEqual({c: (v["price"], v["source"]) for c, v in q.items()},
                         {"2303": (110.0, "mis"), "6488": (760.0, "mis"), "2330": (2330.0, "mis"), "0050": (104.1, "mis")})
        q, err = alerts.get_quotes(["6488"], "yahoo", [ADIR], now("2026-11-02 10:00"))
        self.assertEqual((q["6488"]["price"], q["6488"]["source"], q["6488"]["market"]), (738.0, "yahoo", "TPEx"))

    def test_a_replay_never_sees_the_future(self):
        q, err = alerts.get_quotes(["2303"], "mis", [MIS_BOTH], now("2026-11-02 10:00"))
        self.assertEqual(q, {})
        self.assertIn("比 --now 晚", err[0])
        # the MIS quote is from 13:30, so at 09:42 the 1m chart answers
        q, err = alerts.get_quotes(["2303"], "mis", [MIS_BOTH, CHART_1M], now("2026-11-02 09:42"))
        self.assertEqual((q["2303"]["price"], q["2303"]["source"]), (108.5, "yahoo"))
        q, err = alerts.get_quotes(["2303"], "mis", [MIS_BOTH, CHART_1M], now("2026-11-02 08:50"))
        self.assertEqual(q, {})

    def test_yahoos_no_data_answer(self):
        q, err = alerts.get_quotes(["9999"], "yahoo", [ADIR])
        self.assertEqual(q, {})
        self.assertIn("No data found", err[0])
        with self.assertRaises(alerts.AlertError):
            alerts.chart_quote(read(os.path.join(ADIR, "yahoo-9999.TW.json")))
        with self.assertRaises(alerts.AlertError):
            alerts.get_quotes(["2303"], "stooq")
        with self.assertRaises(alerts.AlertError):
            alerts.get_quotes(["2303"], "mis", [os.path.join(ADIR, "missing.json")])

    def test_prices_json_is_the_contract_shape_and_merges(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "prices.json")
        q, _ = alerts.get_quotes(["2303"], "mis", [CHART_1M], now("2026-11-02 09:42"))
        alerts.write_prices(path, q, now("2026-11-02 09:42"))
        q, _ = alerts.get_quotes(["2330"], "mis", [MIS])
        doc = alerts.write_prices(path, q, now("2026-11-02 13:31"))
        self.assertEqual(read(path), doc)
        self.assertEqual(sorted(doc), ["as_of", "detail", "prices", "source"])
        self.assertEqual((doc["as_of"], doc["source"], doc["prices"]),
                         ("2026-11-02T05:31:00Z", "mis", {"2303": 108.5, "2330": 2330.0}))
        self.assertEqual(doc["detail"]["2303"], {"time": "2026-11-02T01:42:00Z", "fetched": "2026-11-02T01:42:00Z",
                                                 "name": None, "market": "TWSE", "prev_close": 111.5,
                                                 "limit_up": 122.5, "limit_down": 100.5, "source": "yahoo"})
        self.assertEqual(doc["detail"]["2330"]["time"], "2026-11-02T05:30:00Z")   # the quote's own time
        self.assertEqual(doc["detail"]["2330"]["name"], "台積電")


class FakeResponse(object):
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet(object):
    """Answers by URL fragment; records every request and every pause."""

    def __init__(self, answers):
        self.answers = list(answers)   # [(fragment, status, body)] used in order
        self.urls, self.headers, self.sleeps = [], [], []

    def urlopen(self, req, timeout=20):
        url = req.full_url
        self.urls.append(url)
        self.headers.append(dict(req.header_items()))
        for i, (frag, code, body) in enumerate(self.answers):
            if frag in url:
                del self.answers[i]
                if isinstance(code, Exception):
                    raise code
                if code != 200:
                    raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body.encode("utf-8")))
                return FakeResponse(body)
        raise AssertionError("unexpected request %s" % url)

    def sleep(self, s):
        self.sleeps.append(s)


def fixture_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Network(unittest.TestCase):
    def use(self, answers):
        net = FakeNet(answers)
        old = (alerts._urlopen, alerts._sleep)
        alerts._urlopen, alerts._sleep = net.urlopen, net.sleep
        self.addCleanup(setattr, alerts, "_urlopen", old[0])
        self.addCleanup(setattr, alerts, "_sleep", old[1])
        return net

    def test_one_mis_request_for_every_code_both_boards_when_unknown(self):
        net = self.use([("getStockInfo", 200, fixture_text(MIS_BOTH))])
        q, err = alerts.get_quotes(["2303", "6488"], "mis")
        self.assertEqual(err, [])
        self.assertEqual(sorted(q), ["2303", "6488"])
        self.assertEqual(len(net.urls), 1)
        self.assertIn("tse_2303.tw|otc_2303.tw|tse_6488.tw|otc_6488.tw", net.urls[0])
        self.assertEqual(net.headers[0]["User-agent"], tw.UA)

    def test_a_known_board_asks_one_channel(self):
        net = self.use([("getStockInfo", 200, fixture_text(MIS_BOTH))])
        alerts.get_quotes(["2303"], "mis", known={"2303": {"market": "TWSE", "name": "聯電"}})
        self.assertIn("ex_ch=tse_2303.tw&", net.urls[0])

    def test_a_code_mis_does_not_answer_falls_back_to_yahoo(self):
        net = self.use([("getStockInfo", 200, fixture_text(MIS_BOTH)),
                        ("9999.TW?", 404, fixture_text(os.path.join(ADIR, "yahoo-9999.TW.json"))),
                        ("9999.TWO?", 404, fixture_text(os.path.join(ADIR, "yahoo-9999.TW.json")))])
        q, err = alerts.get_quotes(["2303", "9999"], "mis")
        self.assertEqual(sorted(q), ["2303"])
        self.assertIn("MIS 沒有 9999 的成交價，改用 Yahoo", err)
        self.assertIn("No data found", err[-1])
        self.assertEqual(net.sleeps, [alerts.PAUSE])           # a pause before the second Yahoo request

    def test_before_the_open_mis_is_enough(self):
        # 2026-11-03 08:39 asked Yahoo for 86 codes (56 s) and printed yesterday's limits
        net = self.use([("getStockInfo", 200, fixture_text(MIS_PRE))])
        q, err = alerts.get_quotes(["2303", "1815"], "mis")
        self.assertEqual((err, len(net.urls)), ([], 1))
        self.assertEqual((q["2303"]["limit_up"], q["2303"]["source"]), (121.0, "mis"))

    def test_in_the_session_mis_bid_beats_a_20_minute_old_yahoo_trade(self):
        net = self.use([("getStockInfo", 200, fixture_text(MIS_SESSION))])
        q, err = alerts.get_quotes(["3665", "4533"], "mis")
        self.assertEqual((err, len(net.urls)), ([], 1))
        self.assertEqual((q["4533"]["price"], q["4533"]["market"]), (29.75, "TPEx"))

    def test_mis_down_means_yahoo_for_everything(self):
        net = self.use([("getStockInfo", urllib.error.URLError("timed out"), ""),
                        ("6488.TWO?", 200, fixture_text(CHART_6488))])
        q, err = alerts.get_quotes(["6488"], "mis", known={"6488": {"market": "TPEx", "name": "環球晶"}})
        self.assertEqual((q["6488"]["price"], q["6488"]["source"], q["6488"]["name"]), (760.0, "yahoo", "環球晶"))
        self.assertIn("MIS 報價抓不到", err[0])
        self.assertIn("query2.finance.yahoo.com/v8/finance/chart/6488.TWO?range=1d&interval=1m", net.urls[1])

    def test_yahoo_429_waits_once_then_query1(self):
        net = self.use([("query2.finance.yahoo.com/v8/finance/chart/2303.TW", 429, ""),
                        ("query2.finance.yahoo.com/v8/finance/chart/2303.TW", 429, ""),
                        ("query1.finance.yahoo.com/v8/finance/chart/2303.TW", 200, fixture_text(CHART_1M))])
        q, _ = alerts.get_quotes(["2303"], "yahoo", known={"2303": {"market": "TWSE", "name": "聯電"}})
        self.assertEqual(q["2303"]["price"], 110.0)
        self.assertEqual(net.sleeps, [alerts.BACKOFF])
        net = self.use([("query2", 503, ""), ("query1", 200, fixture_text(CHART_1M))])
        self.assertIn("2303", alerts.get_quotes(["2303"], "yahoo", known={"2303": {"market": "TWSE"}})[0])
        net = self.use([("query2", 403, "")])
        q, err = alerts.get_quotes(["2303"], "yahoo", known={"2303": {"market": "TWSE"}})
        self.assertEqual(q, {})
        self.assertIn("HTTP 403", err[0])
        self.assertEqual(len(net.urls), 1)                     # a 403 is not retried

    def test_calendar_update_online(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.use([("holidaySchedule?date=20260101", 200, fixture_text(CAL % 2026))])
        code, out, _ = run(["calendar", "--update", "2026", "--config", os.path.join(d, "config.json")])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(d, "state", "calendar-2026.json")))


class Rules(unittest.TestCase):
    def setUp(self):
        self.open = status("2026-11-02 10:00")

    def ev(self, rules, quotes, when="2026-11-02 10:00", state=None):
        return alerts.evaluate(rules, quotes, status(when), state or {}, now(when))

    def test_a_target_rings_once(self):
        rules = [{"id": "t", "symbol": "2303", "type": "target", "price": 111.5}]
        rings, st = self.ev(rules, {"2303": quote(111.0)})
        self.assertEqual(rings, [])
        rings, st = self.ev(rules, {"2303": quote(111.5, when="10:30 那根 1 分 K 收盤")}, state=st)
        self.assertEqual(rings[0]["message"],
                         "聯電 2303 現在 111.5（10:30 那根 1 分 K 收盤），大於或等於你的停利 111.5。")
        self.assertEqual(self.ev(rules, {"2303": quote(112.0)}, state=st)[0], [])

    def test_stop_and_rearm(self):
        rules = [{"id": "s", "symbol": "2303", "type": "stop", "price": 108.5, "rearm": True, "note": "計畫停損"}]
        rings, st = self.ev(rules, {"2303": quote(108.5)})
        self.assertEqual(rings[0]["message"], "聯電 2303 現在 108.5，小於或等於你的停損 108.5（計畫停損）。")
        rings, st = self.ev(rules, {"2303": quote(109.0)}, state=st)
        self.assertEqual((rings, st["rung"]), ([], {}))        # re-armed when it crossed back
        self.assertEqual(len(self.ev(rules, {"2303": quote(108.0)}, state=st)[0]), 1)
        closed = alerts.evaluate([{"id": "b", "symbol": "2303", "type": "below", "price": 120}], {"2303": quote(110.0)},
                                 status("2026-11-02 14:00"), {}, now("2026-11-02 14:00"))[0]
        self.assertIn("最後成交 110.0", closed[0]["message"])
        self.assertIn("警示價 120.0", closed[0]["message"])
        early = alerts.evaluate(rules, {"2303": quote(108.0, no_trade=True, when="08:41:49 的報價")},
                                status("2026-11-03 08:41"), {}, now("2026-11-03 08:41"))[0]
        self.assertEqual(early[0]["message"], "聯電 2303 昨收 108.0（08:41:49 的報價），小於或等於你的停損 108.5（計畫停損）。")

    def test_pct_move_from_the_previous_close_once_a_day(self):
        rules = [{"id": "m", "symbol": "2303", "type": "pct_move", "pct": 2}]
        rings, st = self.ev(rules, {"2303": quote(109.0)})
        self.assertEqual(rings[0]["message"], "聯電 2303 跌 2.24%，現在 109.0（昨收 111.5）。")
        self.assertEqual(self.ev(rules, {"2303": quote(108.0)}, state=st)[0], [])
        self.assertEqual(len(self.ev(rules, {"2303": quote(108.0)}, "2026-11-03 10:00", st)[0]), 1)
        up = [{"id": "u", "symbol": "2303", "type": "pct_move", "pct": 2, "direction": "up"}]
        self.assertEqual(self.ev(up, {"2303": quote(109.0)})[0], [])
        ref = [{"id": "r", "symbol": "2303", "type": "pct_move", "pct": 1, "ref": 110.0}]
        self.assertIn("參考價 110.0", self.ev(ref, {"2303": quote(108.5)})[0][0]["message"])

    def test_limit_up_and_limit_down(self):
        rules = [{"id": "d", "symbol": "2303", "type": "limit_down"}, {"id": "u", "symbol": "2303", "type": "limit_up"}]
        rings, st = self.ev(rules, {"2303": quote(100.5)})
        self.assertEqual([r["id"] for r in rings], ["d"])
        self.assertIn("跌停 100.5", rings[0]["message"])
        self.assertIn("賣單可能賣不掉", rings[0]["message"])
        self.assertEqual(self.ev(rules, {"2303": quote(100.5)}, state=st)[0], [])
        rings, _ = self.ev(rules, {"2303": quote(122.5, limit_up=None, limit_down=None)})   # from the previous close
        self.assertEqual([r["id"] for r in rings], ["u"])
        self.assertIn("買單排隊也可能買不到", rings[0]["message"])
        etf = {"0050": quote(104.1, symbol="0050", prev_close=None, limit_up=None, limit_down=None)}
        self.assertEqual(self.ev([{"id": "e", "symbol": "0050", "type": "limit_down"}], etf)[0], [])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.ev(rules, {}, "2026-11-02 08:59")[0], [])
        rings, st = self.ev(rules, {}, "2026-11-02 09:00")
        self.assertIn("開盤：台股開盤了（台北 2026-11-02 09:00）", rings[0]["message"])
        self.assertEqual(self.ev(rules, {}, "2026-11-02 09:30", st)[0], [])
        self.assertEqual(self.ev(rules, {}, "2026-10-23 09:30")[0], [])     # typhoon day
        self.assertEqual(len(self.ev(rules, {}, "2026-11-03 09:01", st)[0]), 1)

    def test_a_time_alert_once_per_trading_day(self):
        rules = [{"id": "f", "symbol": "2303", "type": "time", "at": "13:20", "label": "13:20 前出清"}]
        self.assertEqual(self.ev(rules, {}, "2026-11-02 13:19")[0], [])
        rings, st = self.ev(rules, {}, "2026-11-02 13:20")
        self.assertEqual(rings[0]["message"],
                         "13:20 前出清：現在台北 13:20（設定 13:20），13:25 起是收盤集合競價（只收限價 ROD），13:30 收盤。"
                         "2303 還有部位的話就平倉：trader-broker 的 ticket.py flatten --symbol 2303 產生賣單，"
                         "再經過 trader-risk 和你的 yes。")
        self.assertEqual(self.ev(rules, {}, "2026-11-02 13:40", st)[0], [])
        self.assertIn("現在是收盤集合競價", self.ev(rules, {}, "2026-11-02 13:26")[0][0]["message"])
        self.assertIn("已經收盤", self.ev(rules, {}, "2026-11-02 14:00")[0][0]["message"])
        self.assertEqual(self.ev(rules, {}, "2026-10-23 13:30")[0], [])     # typhoon day
        self.assertEqual(self.ev(rules, {}, "2026-11-01 13:30")[0], [])     # Sunday

    def test_a_dated_rule_is_watched_that_day_only(self):
        rules = [{"id": "s", "symbol": "2303", "type": "stop", "price": 108.5, "date": "2026-11-02"}]
        self.assertEqual(self.ev(rules, {"2303": quote(100.0)}, "2026-11-03 10:00")[0], [])
        self.assertEqual(len(self.ev(rules, {"2303": quote(100.0)})[0]), 1)

    def test_the_plan_gives_stop_target_flat_by_and_limit_down(self):
        plan = read(PLAN)
        rules = alerts.rules_from_plan(plan)
        example = read(os.path.join(ROOT, "trader-alerts", "alerts.example.json"))["rules"]
        self.assertEqual(rules, example[1:5])
        self.assertEqual(alerts.rules_from_plan(dict(plan, flat_by=None), "13:10")[2]["at"], "13:10")
        self.assertEqual(alerts.rules_from_plan(dict(plan, flat_by=None))[2]["at"], "13:20")
        for bad in ({}, dict(plan, stop=None), dict(plan, target=0), dict(plan, flat_by="soon"),
                    dict(plan, symbol="TSLA")):
            with self.assertRaises(alerts.AlertError):
                alerts.rules_from_plan(bad)

    def test_the_example_rules_are_valid(self):
        for r in read(os.path.join(ROOT, "trader-alerts", "alerts.example.json"))["rules"]:
            alerts.validate(r)

    def test_bad_rules(self):
        for bad in ({"id": "a", "type": "sideways", "symbol": "2303"},
                    {"id": "b", "type": "above", "price": 110},
                    {"id": "c", "type": "above", "symbol": "2303", "price": "110"},
                    {"id": "d", "type": "above", "symbol": "TSLA", "price": 110},
                    {"id": "e", "type": "pct_move", "symbol": "2303"},
                    {"id": "f", "type": "time", "at": "1:20 PM"},
                    {"id": "g", "type": "stop", "symbol": "2303", "price": 108.5, "date": "11/02"}):
            with self.assertRaises(alerts.AlertError, msg=bad["id"]):
                alerts.validate(bad)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        p = lambda *x: os.path.join(self.d, *x)
        self.rules, self.state, self.prices, self.cfg = p("alerts.json"), p("state", "alerts.json"), \
            p("prices.json"), p("config.json")

    def check(self, when, *extra):
        return run(["check", "--now", when, "--offline", CHART_1M, "--rules", self.rules, "--state", self.state,
                    "--prices-out", self.prices, "--plan", PLAN, "--config", self.cfg] + list(extra))

    def test_the_reel_replayed_from_the_plan(self):
        code, out, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                            "--config", self.cfg])
        self.assertEqual(code, 0, out)
        self.assertEqual([r["id"] for r in read(self.rules)["rules"]],
                         ["2303-plan-stop", "2303-plan-target", "2303-flat-by", "2303-limit-down"])
        self.assertIn("已加 2303-plan-stop：聯電 2303 停損：到或低於 108.5，只看 2026-11-02", out)
        code, out, _ = self.check("2026-11-02 09:40")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[:2], ["台股開盤中（台北 2026-11-02 09:40（一），盤中逐筆交易）。",
                                                "沒有警示。今天看 4 條規則，聯電 2303 109.5。"])
        self.assertEqual(self.check("2026-11-02 09:41")[0], 1)          # the 09:40 bar closed 109.0
        code, out, _ = self.check("2026-11-02 09:42")
        self.assertEqual(code, 0, out)
        self.assertIn("警示 聯電 2303 現在 108.5（09:41 那根 1 分 K 收盤），小於或等於你的停損 108.5"
                      "（計畫停損：開盤區間中點）。", out)
        self.assertIn("台股交易所沒有停損單", out)
        self.assertEqual(read(self.prices)["prices"], {"2303": 108.5})
        self.assertEqual(read(self.prices)["detail"]["2303"]["time"], "2026-11-02T01:42:00Z")
        self.assertEqual(self.check("2026-11-02 09:43")[0], 1)          # it rang once
        code, out, _ = self.check("2026-11-02 13:20")
        self.assertEqual(code, 0)
        self.assertIn("警示 13:20 前出清：現在台北 13:20（設定 13:20）", out)
        self.assertIn("聯電 2303 還有部位的話就平倉：trader-broker 的 ticket.py flatten --symbol 2303", out)
        code, out, _ = self.check("2026-11-02 13:31")
        self.assertEqual(code, 1)
        self.assertIn("台股沒有開盤（台北 2026-11-02 13:31（一），已收盤）", out)
        self.assertIn("聯電 2303 110.0", out)
        code, out, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("（2026-11-02 09:42 響過）", out)
        # the next trading day the plan's rules are not watched
        code, out, _ = self.check("2026-11-03 10:00")
        self.assertIn("今天看 0 條規則", out)

    def test_a_typhoon_day(self):
        run(["add", "--opening-bell", "--rules", self.rules])
        code, out, _ = self.check("2026-10-23 10:00")
        self.assertEqual(code, 1)
        self.assertIn("台股沒有開盤（台北 2026-10-23（五），薔琵颱風，台北市停班，全日休市）", out.replace(" 10:00", ""))

    def test_add_list_reset(self):
        for argv in (["--symbol", "2303.TW", "--target", "111.5"], ["--symbol", "2303", "--pct", "3", "--direction", "down"],
                     ["--symbol", "6488.TWO", "--limit-down"], ["--at", "13:20", "--label", "13:20 前出清", "--symbol", "2303"],
                     ["--opening-bell"], ["--symbol", "0050", "--below", "100", "--rearm", "--note", "回測", "--date",
                                          "2026-11-02"]):
            code, out, err = run(["add", "--rules", self.rules] + argv)
            self.assertEqual(code, 0, err)
        ids = [r["id"] for r in read(self.rules)["rules"]]
        self.assertEqual(ids, ["2303-target-111.5", "2303-pct-move-3", "6488-limit-down", "time-1320", "opening-bell",
                               "0050-below-100"])
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "2303", "--target", "111.5"])
        self.assertEqual(code, 1)
        self.assertIn("已經有 id 是 2303-target-111.5 的規則", err)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TSLA", "--target", "400"])
        self.assertEqual(code, 1)
        self.assertIn("不是台股代號", err)
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "2303"])[0], 1)
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "1:20pm"])[0], 1)
        code, out, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("每個交易日 13:20 響：13:20 前出清（2303）", out)
        self.assertIn("0050 到或低於 100，只看 2026-11-02，回頭後重新啟用", out)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)
        run(["check", "--now", "2026-11-02 09:05", "--offline", CHART_1M, "--rules", self.rules, "--state", self.state,
             "--prices-out", self.prices, "--config", self.cfg])
        code, out, _ = run(["reset", "opening-bell", "--state", self.state])
        self.assertEqual((code, out.strip()), (0, "已重新啟用 opening-bell。"))
        self.assertEqual(run(["list", "--rules", os.path.join(self.d, "none.json"), "--state", self.state])[0], 1)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("裡沒有規則", err)

    def test_quote_write_and_holdings(self):
        pf = os.path.join(self.d, "portfolio.json")
        wl = os.path.join(self.d, "watchlist.json")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"cash": 1, "positions": [{"symbol": "6488", "shares": 1000}]}, fh)
        with open(wl, "w", encoding="utf-8") as fh:
            json.dump({"hits": [{"symbol": "2330", "market": "TWSE", "name": "台積電"}]}, fh, ensure_ascii=False)
        code, out, _ = run(["quote", "--holdings", "--plan", PLAN, "--portfolio", pf, "--watchlist", wl,
                            "--offline", ADIR, "--offline", MIS, "--write", self.prices, "--config", self.cfg])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[:3],
                         ["聯電 2303（上市）  110.0  -1.35%（昨收 111.5）  漲停 122.5／跌停 100.5  13:30:00 成交  [mis]",
                          "環球晶 6488（上櫃）  760  +0.53%（昨收 756）  漲停 831／跌停 681  13:30:00 成交  [mis]",
                          "台積電 2330（上市）  2,330  -1.69%（昨收 2,370）  漲停 2,605／跌停 2,135  13:30:00 成交  [mis]"])
        self.assertEqual(sorted(read(self.prices)["prices"]), ["2303", "2330", "6488"])
        self.assertEqual(alerts.holdings(PLAN, pf, wl), ["2303", "6488", "2330"])
        code, _, err = run(["quote", "--config", self.cfg, "--plan", os.path.join(self.d, "no-plan.json")])
        self.assertEqual(code, 1)
        self.assertIn("要報哪幾檔", err)
        code, out, _ = run(["quote", "9999", "--offline", ADIR, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("注意：9999", out)

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-11-02 09:30", "--config", self.cfg])[0], 0)
        code, out, _ = run(["clock", "--now", "2026-10-23 09:30", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("下一次開盤：2026-10-27（二） 09:00。", out)
        code, out, _ = run(["clock", "--now", "2026-11-02 13:27", "--config", self.cfg, "--json"])
        self.assertEqual((code, json.loads(out)["phase"]), (0, "closing_auction"))
        code, _, err = run(["clock", "--now", "tomorrow", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("--now", err)

    def test_calendar_list_and_update_offline(self):
        code, out, _ = run(["calendar", "--year", "2026", "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("2026-10-23（五）  薔琵颱風，台北市停班，全日休市", out)
        self.assertIn("2026-07-10（五）  巴威颱風，台北市停班，全日休市", out)
        code, out, _ = run(["calendar", "--update", "2027", "--offline", CAL % 2027, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("證交所還沒公布 2027 年的開休市日期，還不能更新", out)
        self.assertEqual(run(["calendar", "--year", "2027", "--config", self.cfg])[0], 1)
        code, out, _ = run(["calendar", "--update", "2026", "--offline", CAL % 2026, "--config", self.cfg])
        self.assertEqual(code, 0, out)
        self.assertIn("已更新 2026 年的休市日：18 個平日休市", out)
        saved = read(os.path.join(self.d, "state", "calendar-2026.json"))
        self.assertEqual(saved["closed"]["2026-10-26"], "臺灣光復暨金門古寧頭大捷紀念日")
        code, out, _ = run(["calendar", "--year", "2026", "--config", self.cfg, "--json"])
        self.assertTrue(json.loads(out)["known"])


class Words(unittest.TestCase):
    def test_no_us_market_left_and_no_em_dash(self):
        for f in ("alerts.py", "SKILL.md", "alerts.example.json"):
            with open(os.path.join(ROOT, "trader-alerts", f), encoding="utf-8") as fh:
                text = fh.read()
            for word in ("NYSE", "New York", "stooq", "Stooq", "Alpaca", "TSLA", chr(0x2014)):
                self.assertNotIn(word, text, "%s: %s" % (f, word))


if __name__ == "__main__":
    unittest.main()
