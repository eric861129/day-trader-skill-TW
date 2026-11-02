import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

inst = load("trader-institutional", "inst")
tw = inst.tw
DIR = os.path.join(FIX, "trader-institutional")
DAYS = ["2026-11-02", "2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-22", "2026-10-21",
        "2026-10-20", "2026-10-19", "2026-10-16"]
D = datetime.date.fromisoformat
AFTER = "2026-11-02 18:00"      # the reel day's numbers are out
BEFORE = "2026-11-02 09:00"     # a replay before 17:00 uses 10-30


def raw(name):
    path = os.path.join(DIR, name)
    if name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def t86(day):
    return inst.parse_t86(raw("T86-%s.json.gz" % day))


def tpex(day):
    return inst.parse_tpex(raw("tpex-dailyTrade-%s.json.gz" % day))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = inst.main(args)
    return code, out.getvalue(), err.getvalue()


def offline(*args, now=AFTER):
    return run(list(args) + ["--now", now, "--offline", DIR])


def reel_days(n=10, start="2026-11-02"):
    src = inst.Source(DIR)
    return inst.load_days(src, tw.Calendar(), D(start), n), src


class Parsing(unittest.TestCase):
    def test_the_reel_row_on_twse(self):
        doc = t86("2026-11-02")
        self.assertEqual(doc["date"], D("2026-11-02"))
        self.assertEqual(len(doc["rows"]), 1343)
        self.assertEqual(doc["rows"]["2303"], {
            "code": "2303", "name": "聯電", "market": "TWSE", "foreign": 5060280, "foreign_dealer": 0,
            "trust": -17884623, "dealer": 3261405, "dealer_self": 2031004, "dealer_hedge": 1230401,
            "total": -9562938})
        self.assertTrue(all(r["name"] == r["name"].strip() for r in doc["rows"].values()))   # names come space-padded

    def test_tpex_group_order(self):
        doc = tpex("2026-11-02")
        self.assertEqual(doc["date"], D("2026-11-02"))
        self.assertEqual(len(doc["rows"]), 884)
        self.assertEqual(doc["rows"]["6488"], {
            "code": "6488", "name": "環球晶", "market": "TPEx", "foreign": -90304, "foreign_dealer": 0,
            "trust": -1202788, "dealer_self": 104400, "dealer_hedge": 93205, "dealer": 197605, "total": -1095487})
        # the repeated titles: group 3 is 外資 + 外資自營商, and every row adds up to the exchange's total
        data = json.loads(raw("tpex-dailyTrade-2026-11-02.json.gz"))["tables"][0]["data"]
        for r in data:
            net = [inst._int(r[2 + 3 * k + 2]) for k in range(7)]
            self.assertEqual(net[0] + net[1], net[2], r[0])
            self.assertEqual(net[4] + net[5], net[6], r[0])
            self.assertEqual(net[2] + net[3] + net[6], inst._int(r[-1]), r[0])

    def test_every_day_adds_up_in_both_markets(self):
        for day in DAYS:
            for doc in (t86(day), tpex(day)):
                self.assertEqual(doc["date"], D(day))
                for r in doc["rows"].values():
                    self.assertEqual(r["foreign"] + r["trust"] + r["dealer"], r["total"], (day, r["code"]))
                    self.assertEqual(r["dealer_self"] + r["dealer_hedge"], r["dealer"], (day, r["code"]))

    def test_a_closed_day_has_no_rows(self):
        self.assertEqual(inst.parse_t86(raw("T86-2026-10-23.json")), {"date": None, "rows": {}})   # the typhoon day
        self.assertEqual(inst.parse_tpex('{"stat": "ok", "tables": []}'), {"date": None, "rows": {}})
        self.assertIsNone(inst.parse_bfi82u(raw("T86-2026-10-23.json")))
        self.assertIsNone(inst.parse_tpex_summary("{}"))

    def test_a_changed_page_is_an_error_not_a_guess(self):
        doc = json.loads(raw("T86-2026-11-02.json.gz"))
        doc["fields"][10] = "投信淨買"
        with self.assertRaises(inst.InstError):
            inst.parse_t86(json.dumps(doc))
        doc = json.loads(raw("tpex-dailyTrade-2026-11-02.json.gz"))
        doc["tables"][0]["data"][0] = doc["tables"][0]["data"][0][:-1]
        with self.assertRaises(inst.InstError):
            inst.parse_tpex(json.dumps(doc))
        with self.assertRaises(inst.InstError):
            inst.parse_t86("<html>FOR SECURITY REASONS</html>")

    def test_numbers(self):
        self.assertEqual([inst._int(x) for x in ("1,234", "-17,884,623", "", "--", None, "0")],
                         [1234, -17884623, 0, 0, 0, 0])
        with self.assertRaises(inst.InstError):
            inst._int("abc")

    def test_market_totals(self):
        twse = inst.parse_bfi82u(raw("BFI82U-2026-11-02.json"))
        self.assertEqual(twse, {"dealer_self": 466422738, "dealer_hedge": -15754565255, "trust": 33815831633,
                                "foreign": -73520805132, "foreign_dealer": 0, "total": -54993116016,
                                "dealer": -15288142517})
        # 外資自營商 is inside 自營商: the total is 外資 + 投信 + 自營商
        self.assertEqual(twse["foreign"] + twse["trust"] + twse["dealer"], twse["total"])
        otc = inst.parse_tpex_summary(raw("tpex-summary-2026-11-02.json"))
        self.assertEqual(otc, {"foreign": -1045136668, "foreign_dealer": 0, "trust": -2045796806,
                               "dealer": 698018267, "dealer_self": 229573708, "dealer_hedge": 468444559,
                               "total": -2392915207})
        self.assertEqual(otc["foreign"] + otc["trust"] + otc["dealer"], otc["total"])


class Units(unittest.TestCase):
    def test_lots_round_half_away_from_zero(self):
        self.assertEqual([inst.lots(x) for x in (5060280, -17884623, 3261405, -9562938, 499, 500, -500, 0)],
                         [5060, -17885, 3261, -9563, 0, 1, -1, 0])
        self.assertEqual(inst.fmt_lots(5060280), "+5,060")
        self.assertEqual(inst.fmt_lots(-129272000), "-129,272")
        self.assertEqual(inst.fmt_lots(400), "0")

    def test_yi(self):
        self.assertEqual(inst.yi(33815831633), "+338.16 億")
        self.assertEqual(inst.yi(-73520805132), "-735.21 億")
        self.assertEqual(inst.yi(0), "0.00 億")


class Days(unittest.TestCase):
    def setUp(self):
        self.cal = tw.Calendar()

    def at(self, text):
        return tw.to_taipei(tw.parse_now(text))

    def test_a_replay_waits_until_17(self):
        day, note = inst.first_day(self.at(BEFORE), self.cal, replay=True)
        self.assertEqual(day, D("2026-10-30"))
        self.assertIn("17:00", note)
        self.assertIn("用 2026-10-30（五）的資料", note)
        self.assertEqual(inst.first_day(self.at("2026-11-02 16:59"), self.cal, True)[0], D("2026-10-30"))
        self.assertEqual(inst.first_day(self.at("2026-11-02 17:00"), self.cal, True), (D("2026-11-02"), ""))

    def test_live_asks_for_today_after_the_close(self):
        self.assertEqual(inst.first_day(self.at("2026-11-02 14:00"), self.cal, False), (D("2026-11-02"), ""))
        self.assertEqual(inst.first_day(self.at("2026-11-02 12:00"), self.cal, False)[0], D("2026-10-30"))

    def test_closed_days(self):
        day, note = inst.first_day(self.at("2026-10-23 18:00"), self.cal, True)
        self.assertEqual(day, D("2026-10-22"))
        self.assertIn("薔琵颱風", note)
        day, note = inst.first_day(self.at("2026-10-24 10:00"), self.cal, True)
        self.assertEqual((day, "週末" in note), (D("2026-10-22"), True))

    def test_the_typhoon_gap(self):
        days, _ = reel_days(3, "2026-10-27")
        # 10-26 光復節 observed and 10-23 薔琵颱風: both skipped
        self.assertEqual([d.isoformat() for d, _ in days], ["2026-10-27", "2026-10-22", "2026-10-21"])

    def test_offline_runs_out(self):
        days, src = reel_days(12)
        self.assertEqual([d.isoformat() for d, _ in days], DAYS)
        self.assertEqual(src.notes, ["離線資料只到 2026-10-16（五），所以只有 10 天。"])


class Streaks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days, _ = reel_days()

    def test_the_reel(self):
        s = dict((g, inst.streak(self.days, "2303", g)) for g in inst.GROUPS)
        self.assertEqual(s, {"foreign": 2, "trust": -8, "dealer": 2})   # 投信 sold 10-20 through 11-02
        self.assertEqual(inst.streak(self.days, "6488", "foreign"), -10)
        self.assertEqual(inst.streak(self.days, "2330", "trust"), 10)
        self.assertEqual(inst.streak(self.days, "4526", "trust"), 0)    # did nothing on the newest day

    def test_a_missing_day_breaks_it(self):
        days = [(D("2026-11-02"), {"1": {"trust": 5}}), (D("2026-10-30"), {}), (D("2026-10-29"), {"1": {"trust": 9}})]
        self.assertEqual(inst.streak(days, "1", "trust"), 1)
        self.assertEqual(inst.streak(days, "2", "trust"), 0)

    def test_sums(self):
        self.assertEqual(inst.lots(inst.window_sum(self.days, "2303", "foreign")), -139522)
        self.assertEqual(inst.lots(inst.window_sum(self.days, "2303", "trust")), -144882)
        self.assertEqual(inst.lots(inst.window_sum(self.days, "2303", "total")), -295408)
        self.assertEqual(inst.lots(inst.window_sum(self.days, "2303", "foreign", 1)), 5060)

    def test_the_text(self):
        self.assertEqual(inst.streak_text(-8, "trust", 10), "投信連賣 8 天")
        self.assertEqual(inst.streak_text(10, "trust", 10), "投信連買 10 天以上（只看了 10 天）")
        self.assertEqual(inst.streak_text(0, "dealer", 10), "自營商最新一天沒有買賣超")


class Commands(unittest.TestCase):
    def test_stock(self):
        code, out, err = offline("stock", "2303")
        self.assertEqual(code, 0, err)
        self.assertIn("聯電 2303（上市），三大法人買賣超（張），最近 10 個交易日", out)
        row = [l for l in out.splitlines() if l.startswith("  2026-11-02")][0].split()
        self.assertEqual(row[1:], ["+5,060", "-17,885", "+3,261", "-9,563"])
        row = [l for l in out.splitlines() if l.startswith("  2026-10-29")][0].split()
        self.assertEqual(row[1], "-129,272")                # 外資 sold 12.9 萬張 the day it hit limit down
        total = [l for l in out.splitlines() if "10 日合計" in l][0].split()
        self.assertEqual(total[2:], ["-139,522", "-144,882", "-11,003", "-295,408"])
        self.assertIn("最新一天的自營商：自行買賣 +2,031 張、避險 +1,230 張。", out)
        self.assertIn("目前連續：外資連買 2 天、投信連賣 8 天、自營商連買 2 天。", out)
        self.assertIn("不是內線消息，也不是買賣訊號", out)

    def test_stock_on_both_markets_and_unknown(self):
        code, out, _ = offline("stock", "6488.TWO", "9999", "--days", "3")
        self.assertEqual(code, 0)
        self.assertIn("環球晶 6488（上櫃）", out)
        self.assertIn("外資連賣 3 天以上（只看了 3 天）", out)
        self.assertIn("9999 這 3 個交易日都沒有出現在三大法人買賣超日報裡", out)
        code, out, _ = offline("stock", "9999", "--days", "2")
        self.assertEqual(code, 1)
        code, _, err = offline("stock", "TSLA")
        self.assertEqual(code, 1)
        self.assertIn("inst.py：", err)

    def test_a_replay_before_17_uses_the_previous_day(self):
        code, out, _ = offline("stock", "2303", "--days", "3", now=BEFORE)
        self.assertEqual(code, 0)
        self.assertIn("2026-11-02（一）的三大法人買賣超要收盤後才公布（重播以 17:00 為準），現在 09:00，"
                      "用 2026-10-30（五）的資料。", out)
        rows = [l.split()[0] for l in out.splitlines() if l.startswith("  2026-")]
        self.assertEqual(rows, ["2026-10-30（五）", "2026-10-29（四）", "2026-10-28（三）"])
        self.assertIn("投信連賣 3 天以上（只看了 3 天）", out)

    def test_date(self):
        code, out, _ = offline("stock", "2303", "--days", "1", "--date", "2026-10-23")
        self.assertIn("2026-10-23（五） 休市（薔琵颱風，台北市停班，全日休市），用 2026-10-22（四）。", out)
        self.assertIn("  2026-10-22（四）", out)
        code, out, _ = offline("stock", "2303", "--days", "1", "--date", "2026-11-02", now=BEFORE)
        self.assertIn("還看不到", out)
        self.assertEqual(offline("stock", "2303", "--date", "11/2")[0], 1)

    def test_market(self):
        code, out, err = offline("market")
        self.assertEqual(code, 0, err)
        for text in ("-735.21 億", "+338.16 億", "-152.88 億", "+4.66 億", "-157.55 億", "-549.93 億",
                     "-10.45 億", "-20.46 億", "+6.98 億", "-23.93 億"):
            self.assertIn(text, out)
        self.assertIn("外資自營商已經算在自營商裡", out)
        code, out, _ = offline("market", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["TWSE"]["trust"], doc["TPEx"]["total"]), (33815831633, -2392915207))
        code, _, err = offline("market", now=BEFORE)       # no 10-30 totals in the folder
        self.assertEqual(code, 1)
        self.assertIn("沒有三大法人買賣金額統計", err)

    def test_top(self):
        code, out, _ = offline("top", "--group", "trust", "-n", "3")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        i = lines.index("投信買超前 3 名")
        self.assertIn("台新新光金 2887（上市）", lines[i + 1])
        self.assertTrue(lines[i + 1].endswith("+8,641 張"))
        i = lines.index("投信賣超前 3 名")
        self.assertIn("聯電 2303（上市）", lines[i + 1])
        self.assertTrue(lines[i + 1].endswith("-17,885 張"))
        code, out, _ = offline("top", "--group", "foreign", "--side", "sell", "-n", "2", "--no-etf", "--json")
        doc = json.loads(out)
        self.assertEqual([(s["symbol"], s["net"]) for s in doc["lists"][0]["stocks"]],
                         [("6770", -16392225), ("2330", -15005014)])
        code, out, _ = offline("top", "--group", "trust", "--market", "TPEx", "-n", "1", "--json")
        doc = json.loads(out)
        self.assertEqual([(l["side"], l["stocks"][0]["symbol"]) for l in doc["lists"]], [("buy", "1815"), ("sell", "4979")])
        code, out, _ = offline("top", "--group", "total", "--side", "buy", "-n", "1", "--no-etf")
        self.assertIn("群創 3481（上市）", out)
        self.assertNotIn("元大台灣50 0050", offline("top", "--no-etf", "-n", "20")[1])

    def test_streaks(self):
        code, out, _ = offline("streaks", "-n", "3")
        self.assertEqual(code, 0)
        self.assertIn("投信連買 3 天以上，最近 10 個交易日到 2026-11-02（一）為止，上市＋上櫃，共 99 檔，列出前 3 檔", out)
        first = [l for l in out.splitlines() if l.startswith("    1.")][0]
        self.assertIn("元大台灣50 0050（上市）", first)
        self.assertIn("連買 10 天以上  這段期間 +33,821 張  最新一天 +5,800 張", first)
        code, out, _ = offline("streaks", "--no-etf", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["count"], doc["stocks"][0]["symbol"], doc["stocks"][0]["days"]), (94, "2330", 10))
        self.assertTrue(all(s["days"] >= 3 for s in doc["stocks"]))
        self.assertFalse(any(s["symbol"].startswith("0") for s in doc["stocks"]))

    def test_streaks_in_a_replay(self):
        code, out, _ = offline("streaks", "--group", "foreign", "--side", "sell", "--min-days", "5", "--no-etf",
                               "-n", "1", now=BEFORE)
        self.assertEqual(code, 0)
        self.assertIn("離線資料只到 2026-10-16（五），所以只有 9 天。", out)
        self.assertIn("外資連賣 5 天以上，最近 9 個交易日到 2026-10-30（五）為止，上市＋上櫃，共 105 檔", out)
        self.assertIn("台積電 2330（上市）", out)
        self.assertIn("連賣 9 天以上  這段期間 -73,530 張", out)

    def test_no_match(self):
        code, out, _ = offline("streaks", "--min-days", "11", "--days", "10")
        self.assertEqual(code, 0)
        self.assertIn("沒有符合的股票。", out)


class Watch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def path(self, name):
        return os.path.join(self.d, name)

    def put(self, name, doc):
        with open(self.path(name), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)

    def test_symbols_and_write(self):
        out_file = self.path("institutional.json")
        code, out, err = offline("watch", "--symbols", "2303", "6488", "4526", "ABC", "--write", out_file)
        self.assertEqual(code, 0, err)
        self.assertIn("注意：外資大買（買超第 9 名）、投信連賣 8 天、投信大賣（賣超第 1 名）、自營商大買（買超第 15 名）。", out)
        self.assertIn("東台 4526（上市）", out)
        self.assertIn("略過看不懂的代號：ABC。", out)
        with open(out_file, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-institutional")
        self.assertEqual((doc["date"], len(doc["days"]), doc["unit"]), ("2026-11-02", 10, "shares"))
        first = doc["stocks"][0]
        self.assertEqual((first["symbol"], first["latest"]["trust"], first["streaks"]["trust"]), ("2303", -17884623, -8))
        self.assertEqual(first["ranks"]["trust"], {"side": "sell", "rank": 1})
        self.assertEqual(doc["skipped"], ["ABC"])
        self.assertIn("不是內線消息", doc["note"])

    def test_from_the_trader_files(self):
        self.put("plan.json", {"symbol": "2303", "name": "聯電"})
        self.put("portfolio.json", {"cash": 1, "positions": [{"ticker": "6488.TWO"}, {"symbol": "2303"}]})
        self.put("watchlist.json", {"hits": [{"symbol": "2330"}, {"symbol": "9999"}]})
        code, out, _ = offline("watch", "--plan", self.path("plan.json"), "--portfolio", self.path("portfolio.json"),
                               "--watchlist", self.path("watchlist.json"), "--json")
        doc = json.loads(out)
        self.assertEqual([s["symbol"] for s in doc["stocks"]], ["2303", "6488", "2330"])
        self.assertEqual(doc["missing"], ["9999"])
        self.assertIn("外資連賣 10 天以上（只看了 10 天）", doc["stocks"][1]["flags"])

    def test_nothing_to_watch(self):
        code, _, err = offline("watch", "--plan", self.path("x"), "--portfolio", self.path("y"),
                               "--watchlist", self.path("z"))
        self.assertEqual(code, 1)
        self.assertIn("沒有要看的代號", err)


class Live(unittest.TestCase):
    """The network path, with the exchanges replaced by the recorded answers."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.urls, self.sleeps = [], []
        self.empty = set()
        old_get, old_sleep = inst.http_get, inst._sleep
        self.addCleanup(setattr, inst, "http_get", old_get)
        self.addCleanup(setattr, inst, "_sleep", old_sleep)
        inst.http_get = self.fake
        inst._sleep = self.sleeps.append

    def fake(self, url, timeout=20):
        self.urls.append(url)
        if "T86" in url:
            day = url.split("date=")[1][:8]
            name = "T86-%s-%s-%s.json.gz" % (day[:4], day[4:6], day[6:8])
        elif "dailyTrade" in url:
            day = url.split("date=")[1].split("&")[0].replace("%2F", "")
            name = "tpex-dailyTrade-%s-%s-%s.json.gz" % (day[:4], day[4:6], day[6:8])
        elif "BFI82U" in url:
            name = "BFI82U-2026-11-02.json"
        else:
            name = "tpex-summary-2026-11-02.json"
        if name in self.empty or not os.path.exists(os.path.join(DIR, name)):
            return raw("T86-2026-10-23.json")            # what a day without numbers answers
        return raw(name)

    def test_urls_pauses_and_cache(self):
        cache = os.path.join(self.d, "institutional")
        src = inst.Source(None, cache)
        rows = src.flows(D("2026-11-02"))
        self.assertEqual(rows["2303"]["trust"], -17884623)
        self.assertEqual(self.urls, [
            "https://www.twse.com.tw/rwd/zh/fund/T86?date=20261102&selectType=ALLBUT0999&response=json",
            "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=2026%2F11%2F02&response=json"])
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(0 < self.sleeps[0] <= inst.PAUSE)
        self.assertTrue(os.path.isfile(os.path.join(cache, "2026-11-02.json.gz")))
        again = inst.Source(None, cache)
        self.assertEqual(again.flows(D("2026-11-02")), rows)
        self.assertEqual(again.requests, 0)
        tot = again.totals(D("2026-11-02"))
        self.assertEqual(tot["TWSE"]["total"], -54993116016)
        self.assertTrue(os.path.isfile(os.path.join(cache, "2026-11-02-totals.json.gz")))

    def test_today_not_published_yet(self):
        self.empty = {"T86-2026-11-02.json.gz", "tpex-dailyTrade-2026-11-02.json.gz"}
        src = inst.Source(None, os.path.join(self.d, "c"))
        days = inst.load_days(src, tw.Calendar(), D("2026-11-02"), 2, today=D("2026-11-02"))
        self.assertEqual([d.isoformat() for d, _ in days], ["2026-10-30", "2026-10-29"])
        self.assertEqual(src.notes, ["2026-11-02（一）的資料還沒公布，用前一個交易日。"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "c", "2026-11-02.json.gz")))   # nothing cached

    def test_an_unplanned_closure(self):
        self.empty = {"T86-2026-10-30.json.gz", "tpex-dailyTrade-2026-10-30.json.gz"}
        src = inst.Source(None, None)
        days = inst.load_days(src, tw.Calendar(), D("2026-11-02"), 2)
        self.assertEqual([d.isoformat() for d, _ in days], ["2026-11-02", "2026-10-29"])
        self.assertEqual(src.notes, ["2026-10-30（五）沒有三大法人資料（臨時休市或還沒公布）。"])

    def test_network_errors(self):
        def down(url, timeout=20):
            raise urllib.error.URLError("timed out")
        inst.http_get = down
        with self.assertRaises(inst.InstError) as e:
            inst.Source(None, None).flows(D("2026-11-02"))
        self.assertIn("連不上", str(e.exception))
        code, _, err = run(["stock", "2303", "--now", AFTER, "--no-cache", "--config", os.path.join(self.d, "c.json")])
        self.assertEqual(code, 1)
        self.assertIn("連不上交易所網站", err)


class Cli(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(run([])[0], 2)
        self.assertEqual(offline("stock", "2303", "--days", "0")[0], 1)
        self.assertEqual(offline("stock", "2303", "--days", "61")[0], 1)
        self.assertEqual(run(["stock", "2303", "--now", "tomorrow", "--offline", DIR])[0], 1)

    def test_json_everywhere(self):
        for args in (["market"], ["top", "-n", "1"], ["stock", "2303", "--days", "2"], ["streaks", "-n", "1"],
                     ["watch", "--symbols", "2303", "--days", "2"]):
            code, out, err = offline(*(args + ["--json"]))
            self.assertEqual(code, 0, (args, err))
            self.assertIsInstance(json.loads(out), dict)


class SkillFile(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-institutional", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-institutional\n"))
        for word in ("三大法人", "投信連買", "inst.py", "不是內線消息", "## 安全"):
            self.assertIn(word, text)
        self.assertTrue(text.rstrip().endswith("券商帳號、密碼和憑證不在對話裡出現，也不寫進任何檔案。"))

    def test_no_em_dash(self):
        for f in ("inst.py", "SKILL.md"):
            with open(os.path.join(ROOT, "trader-institutional", f), encoding="utf-8") as fh:
                self.assertNotIn(chr(0x2014), fh.read(), f)


if __name__ == "__main__":
    unittest.main()
