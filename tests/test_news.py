import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
UNIVERSE = os.path.join(FIX, "trader-scanner", "universe-2026-11-02.txt")
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def at(text):
    """A Taipei time -> aware UTC, the way --now reads it."""
    return news.aware(news.tw.parse_now(text))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-2303.TW.xml"), "2303")
        self.assertEqual(len(items), 19)
        first = items[0]
        self.assertEqual(first["title"], "不到一個月慘跌26% 聯華電子公佈2026年Q3財務報告")   # four spaces become one
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 29, 6, 54, 24, tzinfo=UTC))
        self.assertEqual(news.stamp(first["time"]), "2026-10-29 14:54")                 # Taipei
        self.assertTrue(first["link"].startswith("https://tw.stock.yahoo.com/news/"))
        self.assertTrue(first["id"].startswith("yahoo:"))
        self.assertEqual(first["source"], "Yahoo股市")

    def test_google_news(self):
        items = news.parse_gnews(raw("gnews-2303.xml"), "2303")
        self.assertEqual(len(items), 61)
        tvbs = [i for i in items if "三星" in i["title"]][0]
        self.assertEqual(tvbs["title"], "市場傳三星急回頭追萬片訂單 聯電產能滿載「不接了」")    # " - TVBS新聞網" cut off
        self.assertEqual((tvbs["source"], tvbs["via"]), ("TVBS新聞網", "Google 新聞"))
        self.assertEqual(news.stamp(tvbs["time"]), "2026-11-02 08:09")
        self.assertTrue(tvbs["link"].startswith("https://news.google.com/rss/articles/"))

    def test_a_company_year_on_mops(self):
        items = news.parse_mops(raw("t05st01-2303-115.html"), {"2303"})
        self.assertEqual(len(items), 82)
        q3 = items[-1]
        self.assertEqual(q3["title"], "聯電 本公司2026年第三季財務報告")
        self.assertEqual(q3["id"], "mops:2303-1151028-170604-5")
        self.assertEqual(news.stamp(q3["time"]), "2026-10-28 17:06")
        self.assertEqual(q3["summary"], "重大訊息：在公開資訊觀測站查 2303，115/10/28 17:06:04（第 5 則）")
        # a subject on two lines in the page is one line here
        self.assertIn("聯電 代重要子公司聯芯集成電路製造（廈門）有限公司公告總經理異動", [i["title"] for i in items])
        self.assertEqual(news.parse_mops(raw("t05st01-2303-115.html"), {"2330"}), [])

    def test_today_on_mops(self):
        items = news.parse_mops(raw("t05sr01_1-2026-11-02.html"))
        self.assertEqual(len(items), 186)
        self.assertTrue(all(news.stamp(i["time"]).startswith("2026-11-02") for i in items))
        # the page writes 子 and 山 as Kangxi radicals; they read as the usual characters
        self.assertIn("鼎炫-KY 代子公司隆揚電子(昆山)股份有限公司公告取得理財產品", [i["title"] for i in items])
        auo = news.parse_mops(raw("t05sr01_1-2026-11-02.html"), {"2409"})
        self.assertEqual([i["title"] for i in auo], ["友達 公告本公司將參加玉山證券舉辦之「法人座談會」"])

    def test_mops_blocking_a_burst_is_an_error(self):
        with self.assertRaises(news.NewsError):
            news.parse_mops(b"<html><body>FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!</body></html>")
        with self.assertRaises(news.NewsError):
            news.parse_rss(b"<rss><channel><item>", "2303")

    def test_times(self):
        self.assertEqual(news.parse_time("Mon, 02 Nov 2026 00:09:00 GMT").hour, 0)
        self.assertEqual(news.parse_time("2026-11-02T09:20:00+08:00").hour, 1)
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.roc_time("115/10/28", "17:06:04"), datetime.datetime(2026, 10, 28, 9, 6, 4, tzinfo=UTC))
        self.assertIsNone(news.roc_time("115/13/40", "17:06:04"))
        self.assertEqual(at("2026-11-02 09:20"), datetime.datetime(2026, 11, 2, 1, 20, tzinfo=UTC))


class Moves(unittest.TestCase):
    def quotes(self):
        q = news.tw.parse_mis(raw("mis-2026-11-02.json").decode("utf-8"))
        q.update(news.tw.parse_mis(raw("mis-2303-2026-11-02.json").decode("utf-8")))
        return q

    def test_the_mis_quote(self):
        q = self.quotes()
        m = news.move_from_quote("2303", q["2303"], at("2026-11-02 22:00"))
        self.assertEqual((m["price"], m["prev_close"], m["pct"], m["source"]), (110.0, 111.5, -1.35, "證交所 MIS"))
        self.assertEqual(news.stamp(m["time"]), "2026-11-02 13:30")
        self.assertEqual((m["limit_up"], m["limit_down"], m["limit"]), (122.5, 100.5, None))
        # a replay at 09:20 cannot use the 13:30 quote
        self.assertIsNone(news.move_from_quote("2303", q["2303"], at("2026-11-02 09:20")))

    def test_the_chart_replays_to_the_last_closed_bar(self):
        m = news.parse_chart(raw("chart-2303.TW.json"), "2303", at("2026-11-02 09:20"))
        self.assertEqual((m["price"], m["pct"], m["source"]), (110.0, -1.35, "Yahoo 5 分 K"))   # the 09:15 bar
        self.assertEqual(news.stamp(m["time"]), "2026-11-02 09:20")
        self.assertEqual((m["limit_up"], m["limit_down"]), (122.5, 100.5))
        self.assertEqual(news.parse_chart(raw("chart-2303.TW.json"), "2303", at("2026-11-02 09:42"))["price"], 109.5)
        early = news.parse_chart(raw("chart-2303.TW.json"), "2303", at("2026-11-02 09:00"))
        self.assertIsNone(early["price"])
        self.assertEqual(news.render_moves([dict(early, name="聯電")], at("2026-11-02 09:00")),
                         ["2303 聯電  今天還沒有成交，昨收 111.5（Yahoo 5 分 K）"])
        late = news.parse_chart(raw("chart-2303.TW.json"), "2303", at("2026-11-02 22:00"))
        self.assertEqual(news.stamp(late["time"]), "2026-11-02 13:30")
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "2303")

    def test_limit_and_big_move_flags(self):
        now = at("2026-11-02 13:30")
        mk = lambda p: dict(news.make_move("2303", p, 111.5, now, 122.5, 100.5, "證交所 MIS"), name="聯電")
        self.assertEqual(mk(122.5)["limit"], "up")
        self.assertEqual(mk(100.5)["limit"], "down")
        lines = news.render_moves([mk(122.5), mk(100.5), mk(115.0), mk(107.5), mk(110.0)], now)
        self.assertEqual(lines[0], "2303 聯電  122.5  +9.87%  昨收 111.5  13:30（證交所 MIS）  漲停")
        self.assertTrue(lines[1].endswith("跌停"))
        self.assertTrue(lines[2].endswith("大漲"))          # +3.14 %
        self.assertTrue(lines[3].endswith("大跌"))          # -3.59 %
        self.assertTrue(lines[4].endswith("（證交所 MIS）"))
        self.assertFalse(news.render_moves([mk(115.0)], now, move_pct=5)[0].endswith("大漲"))
        yesterday = news.render_moves([mk(110.0)], at("2026-11-03 08:30"))[0]
        self.assertIn("11-02 13:30", yesterday)


class Tagging(unittest.TestCase):
    def item(self, title, kind="headline", code="2303"):
        return {"id": "x:" + title, "source": "test", "kind": kind, "code": code, "title": title,
                "summary": "", "link": "", "time": None}

    def tags(self, title, kind="headline"):
        it = news.tag([self.item(title, kind)], {"2303": {"name": "聯電", "market": "TWSE"}})[0]
        return it["tags"], it["important"]

    def test_tags_in_taiwan_words(self):
        self.assertEqual(self.tags("【公告】聯電董事會決議通過民國115年第三季合併財務報告")[0], ["財報", "董事會"])
        self.assertEqual(self.tags("外資轉賣超688億元 聯電跌停遭砍12.92萬張居榜首")[0], ["風險", "法人買賣超"])
        self.assertEqual(self.tags("聯電ADR27日上漲0.27美元漲幅1.41%折台股126.24元")[0], ["ADR"])
        self.assertEqual(self.tags("三大題材助攻！「這大廠」Q4產能利用率衝破90% 目標價奔160元")[0], ["評等目標價"])
        self.assertEqual(self.tags("友達 公告本公司將參加玉山證券舉辦之「法人座談會」")[0], ["法說會"])
        self.assertIn("併購", self.tags("聯電宣布公開收購子公司股權")[0])
        self.assertIn("總經", self.tags("Fed 降息、CPI 降溫，台股期貨走高")[0])
        self.assertIn("處置／注意股", self.tags("聯電列處置股關禁閉，股價下殺近6%")[0])
        # 資安 alone is a sector (2026-11-03 08:38); an attack is still a risk
        self.assertEqual(self.tags("今日漲停股｜資安爆發與電子週邊狂飆，富喬、華經、高僑強攻漲停1102")[0], [])
        self.assertIn("風險", self.tags("聯電遭駭客入侵 公告資安事件")[0])

    def test_important_mops_announcements(self):
        self.assertEqual(self.tags("聯電 說明115年10月6日中時電子報報導", "mops"), (["澄清報導"], True))
        self.assertEqual(self.tags("聯電 本公司2026年第三季財務報告", "mops"), (["財報"], True))
        self.assertEqual(self.tags("聯電 本公司董事會通過資本預算執行案", "mops"), (["董事會"], False))
        self.assertTrue(self.tags("某公司 公告本公司股票暫停交易", "mops")[1])
        self.assertFalse(self.tags("聯電 本公司2026年第三季財務報告")[1])        # headlines never carry "!"

    def test_mentions_of_other_stocks_you_watch(self):
        watch = {"2303": {"name": "聯電"}, "2409": {"name": "友達"}, "5347": {"name": "世界"}}
        items = news.tag([self.item("聯電(2303)、友達(2409)雙破底可以買？"),
                          self.item("世界經濟論壇：半導體需求"),
                          self.item("5347 世界 法說會", code="9999")], watch)
        self.assertEqual(items[0]["touches"], ["2303", "2409"])
        self.assertEqual(items[1]["touches"], ["2303"])       # 世界 is an everyday word: no mention
        self.assertEqual(items[2]["touches"], ["5347"])       # the code counts
        self.assertEqual(news.tag([self.item("23030 不是聯電的代號")], {"2303": {}})[0]["touches"], ["2303"])

    def test_dedupe(self):
        g = news.tag(news.parse_gnews(raw("gnews-2303.xml"), "2303"), {"2303": {"name": "聯電"}})
        tvbs = news.dedupe([i for i in g if "法說後震盪" in i["title"]])
        self.assertEqual(len(tvbs), 1)
        self.assertEqual((tvbs[0]["source"], tvbs[0]["also"]), ("TVBS新聞網", ["LINE TODAY"]))   # the earlier one
        # the company's own word beats a copy of it
        mops = news.parse_mops(raw("t05st01-2303-115.html"), {"2303"})
        yahoo = news.parse_rss(raw("yahoo-2303.TW.xml"), "2303")
        both = news.dedupe(news.tag(yahoo + mops, {"2303": {"name": "聯電"}}))
        q3 = [i for i in both if i["title"].endswith("本公司2026年第三季財務報告")]
        self.assertEqual([(i["source"], i["also"]) for i in q3], [("公開資訊觀測站", ["Yahoo股市"])])
        self.assertEqual(len(both), 19 + 82 - 2)          # 2 Yahoo copies of an announcement
        twice = [i for i in both if i["title"] == "聯電 本公司受邀參與法人說明會相關資訊"]
        self.assertEqual(len(twice), 6)                    # same subject, different filings: all kept
        # the same title three days apart is two stories
        a = self.item("聯電ADR上漲")
        b = dict(a, id="x:b", time=datetime.datetime(2026, 10, 30, tzinfo=UTC))
        a["time"] = datetime.datetime(2026, 10, 27, tzinfo=UTC)
        self.assertEqual(len(news.dedupe(news.tag([a, b], {"2303": {}}))), 2)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--portfolio", none, "--watchlist", none,
                     "--plan", none, "--universe", UNIVERSE]

    def test_the_reel_morning(self):
        # 聯電 2303 at 09:20 on 2026-11-02, the minute the opening-range breakout triggered
        code, out, err = run(["--symbols", "2303", "--now", "2026-11-02 09:20"] + self.base)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "2303 聯電  110.0  -1.35%  昨收 111.5  09:20（Yahoo 5 分 K）")
        heads = [l for l in lines if l.startswith("  2026-") or l.startswith("! 2026-")]
        self.assertEqual(len(heads), 12)                  # 48 hours back: Google's stories, nothing newer than 09:20
        self.assertEqual(heads[0], "  2026-11-02 08:43  2303  Yahoo股市  近10個交易日慘跌21.5％！法人上週提款聯電20萬張 "
                                   "投信連7賣不手軟、外資逆勢回補")
        self.assertIn("  2026-11-02 08:09  2303  TVBS新聞網  市場傳三星急回頭追萬片訂單 聯電產能滿載「不接了」", lines)
        self.assertNotIn("法說後震盪", out)               # 10:50, after the pinned clock
        self.assertIn("檢查時間 2026-11-02 09:20（台北）", out)
        self.assertIn("不構成投資建議", out)
        self.assertNotIn("附註", out)
        self.assertNotIn(chr(0x2014), out)

    def test_a_week_back_reaches_the_q3_report(self):
        code, out, err = run(["--symbols", "2303", "--now", "2026-11-02 09:20", "--hours", "168",
                              "--no-google", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("! 2026-10-28 17:06  2303  公開資訊觀測站（也見 Yahoo股市）  聯電 本公司2026年第三季財務報告", lines)
        self.assertIn("! 2026-10-28 17:00  2303  公開資訊觀測站  聯電 公告本公司董事會決議通過民國115年第三季合併財務報告",
                      lines)
        self.assertIn("      標籤：財報  重大訊息：在公開資訊觀測站查 2303，115/10/28 17:06:04（第 5 則）  "
                      "https://mopsov.twse.com.tw/mops/web/t05st01", lines)
        heads = [l for l in lines if l[2:7] == "2026-"]
        self.assertEqual(len(heads), 16)                  # 4 announcements + 12 Yahoo headlines, one told twice
        self.assertEqual(sum(1 for l in heads if l.startswith("!")), 2)

    def test_new_items_once_and_a_replay_never_remembers_the_future(self):
        args = ["--symbols", "2303", "--now", "2026-11-02 09:20"] + self.base
        self.assertEqual(run(args)[0], 0)
        code, out, _ = run(args)
        self.assertEqual(code, 1)
        self.assertIn("沒有新消息。", out)
        code, out, _ = run(args + ["--all"])
        self.assertEqual(code, 1)                          # seen before: shown, not new
        self.assertIn("市場傳三星急回頭追萬片訂單", out)
        # at 22:00 the stories published after 09:20 are new: the 09:20 check never saw them
        code, out, err = run(["--symbols", "2303", "--now", "2026-11-02 22:00"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("  2026-11-02 10:50  2303  TVBS新聞網（也見 LINE TODAY）  聯電法說後震盪！", out)
        self.assertNotIn("市場傳三星急回頭追萬片訂單", out)
        self.assertIn("2303 聯電  110.0  -1.35%  昨收 111.5  13:30（證交所 MIS）", out)

    def test_the_evening_check_three_stocks(self):
        code, out, err = run(["--symbols", "2303", "2330", "6488", "2409", "--now", "2026-11-02 22:00",
                              "--hours", "24", "--dry-run", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["checked_taipei"], "2026-11-02 22:00")
        self.assertEqual(data["watch"]["6488"], {"name": "環球晶", "market": "TPEx"})    # from the MIS quote
        self.assertEqual(data["watch"]["2409"], {"name": "友達", "market": "TWSE"})      # from the universe file
        self.assertEqual([(m["code"], m["price"], m["pct"], m["time_taipei"]) for m in data["moves"]],
                         [("2303", 110.0, -1.35, "2026-11-02 13:30"), ("2330", 2330.0, -1.69, "2026-11-02 13:30"),
                          ("6488", 760.0, 0.53, "2026-11-02 13:30")])
        self.assertEqual(data["notes"], [])
        self.assertEqual(len(data["items"]), 68)
        both = [i["title"] for i in data["items"] if i["touches"] == ["2303", "2409"]]
        self.assertEqual(len(both), 3)                    # 友達 named in a 聯電 story, by code or by name
        self.assertTrue(both[0].startswith("聯電(2303)、友達(2409)雙破底可以買？"))
        auo = [i for i in data["items"] if i["kind"] == "mops"]
        self.assertEqual([(i["code"], i["time_taipei"]) for i in auo], [("2409", "2026-11-02 15:30")])
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_a_lookalike_name_is_left_out(self):
        # Yahoo's feed for 台達電 2308, 2026-11-02 23:30: 6 of its 17 items are about 台達化 1309
        code, out, err = run(["--symbols", "2308", "--now", "2026-11-02 23:30", "--hours", "48", "--no-google",
                              "--no-mops", "--no-moves", "--dry-run", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        titles = [i["title"] for i in data["items"]]
        self.assertEqual(len(titles), 11)
        self.assertFalse([t for t in titles if "台達化" in t])
        self.assertIn("20萬股東哭哭！光寶科法說後翻黑跌3% 分析師：受「老大哥」台達電拖累", titles)
        self.assertEqual(data["notes"], ["略過名稱相近的股票的新聞：台達電 2308 的新聞裡有 6 則其實在講台達化 1309。"])

    def test_the_lookalike_note_counts_this_window_once(self):
        # 2026-11-03 08:38 still counted the 6 skipped the night before: only new ones in the window count
        args = ["--symbols", "2308", "--no-google", "--no-mops", "--no-moves", "--json"] + self.base
        code, out, err = run(["--now", "2026-11-02 23:30", "--hours", "2"] + args)
        self.assertEqual(json.loads(out)["notes"], [], err)          # the 台達化 items are from 18:05 to 18:30
        run(["--now", "2026-11-02 23:30", "--hours", "48"] + args)    # seen once, without --dry-run
        code, out, err = run(["--now", "2026-11-03 08:38", "--hours", "16"] + args)
        self.assertEqual(json.loads(out)["notes"], [], err)

    def test_lookalikes_share_the_first_two_characters(self):
        universe = news.tw.load_universe(UNIVERSE)
        sims = news.lookalikes({"2308": {"name": "台達電", "market": "TWSE"}}, universe)
        self.assertEqual(sims, {"2308": [("1309", "台達化")]})
        item = {"kind": "headline", "code": "2308", "title": "台達化 台達電 同日公告"}   # names both: kept
        self.assertEqual(news.drop_lookalikes([item], {"2308": {"name": "台達電"}}, universe), ([item], []))

    def test_an_otc_stock_reads_its_two_suffix(self):
        code, out, err = run(["--symbols", "6488.TWO", "--now", "2026-11-02 22:00", "--hours", "720",
                              "--no-google", "--no-mops", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("6488 環球晶  760  +0.53%  昨收 756  13:30（證交所 MIS）", out)
        self.assertIn("  2026-10-20 15:25  6488  Yahoo股市  【公告】環球晶召開2026Q3英文線上法說會", out)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith("  2026-")), 3)

    def test_stocks_from_the_plan_portfolio_and_watchlist(self):
        d = self.tmp.name
        paths = {}
        for name, doc in (("plan", {"created_by": "trader-exit", "symbol": "2303", "name": "聯電", "market": "TWSE"}),
                          ("portfolio", {"cash": 1, "positions": [{"symbol": "6488", "shares": 10}]}),
                          ("watchlist", {"hits": [{"symbol": "2330", "market": "TWSE", "name": "台積電"},
                                                  {"symbol": "TSLA"}, {"symbol": "2303"}]})):
            paths[name] = os.path.join(d, name + ".json")
            with open(paths[name], "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
        skipped = []
        watch = news.watch_from_files(paths["portfolio"], paths["watchlist"], paths["plan"], skipped)
        self.assertEqual(list(watch), ["2303", "6488", "2330"])          # plan, holdings, scanner hits
        self.assertEqual(watch["2330"], {"name": "台積電", "market": "TWSE"})
        self.assertEqual(skipped, ["TSLA"])
        args = ["--offline", DIR, "--state", self.state, "--universe", UNIVERSE, "--now", "2026-11-02 22:00",
                "--hours", "24", "--dry-run", "--json", "--no-google"]
        for name, p in paths.items():
            args += ["--" + name, p]
        code, out, err = run(args)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(list(data["watch"]), ["2303", "6488", "2330"])
        self.assertIn("略過不是台股代號的：TSLA", data["notes"])

    def test_no_stocks_or_a_bad_code(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("沒有要看的股票", err)
        code, _, err = run(["--symbols", "TSLA"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("TSLA 不是台股代號", err)
        code, _, err = run(["--symbols", "2303", "--now", "tomorrow"] + self.base)
        self.assertEqual(code, 2)

    def test_quiet_when_a_source_has_nothing_offline(self):
        code, out, err = run(["--symbols", "1101", "--now", "2026-11-02 09:20"] + self.base)
        self.assertEqual(code, 1, err)
        self.assertIn("沒有新消息。", out)
        self.assertNotIn("附註", out)
        code, out, _ = run(["--symbols", "9999", "--now", "2026-11-02 09:20", "--dry-run"] + self.base)
        self.assertIn("附註：不確定 9999 是上市還是上櫃，先當上市（.TW）查新聞", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00", "fresh": "2026-10-30T00:00:00+00:00"}}
        now = at("2026-11-02 09:20")
        news.remember([], state, now)
        self.assertEqual(sorted(state["seen"]), ["fresh"])
        self.assertEqual(state["last_check"], now.isoformat())


class Fetching(unittest.TestCase):
    class Recorder(news.Fetcher):
        def __init__(self, fail=False):
            news.Fetcher.__init__(self)
            self.calls, self.fail = [], fail

        def get(self, url, name, form=None):
            self.calls.append((url, name, form))
            if self.fail:
                raise news.NewsError("HTTP 503")
            raise news.Missing(name)

    def test_the_requests(self):
        f = self.Recorder()
        watch = {"2303": {"name": "聯電", "market": "TWSE"}, "6488": {"name": "環球晶", "market": "TPEx"}}
        news.collect(f, watch, at("2026-11-02 09:20"), 48, mops_max=1, errors=[])
        urls = [c[0] for c in f.calls]
        self.assertEqual(urls[0], news.MOPS_TODAY)
        self.assertEqual(f.calls[1][2], "encodeURIComponent=1&step=1&firstin=1&off=1&queryName=co_id&inpuType=co_id"
                                        "&TYPEK=all&co_id=2303&year=115&month=all")
        self.assertIn("s=2303.TW&region=TW&lang=zh-Hant-TW", urls[2])
        self.assertIn("q=%E8%81%AF%E9%9B%BB+2303", urls[3])
        self.assertIn("s=6488.TWO", urls[4])
        self.assertEqual(len(urls), 6)                    # the yearly MOPS list for the first stock only

    def test_a_broken_source_is_a_note(self):
        notes = []
        items = news.collect(self.Recorder(fail=True), {"2303": {"name": "聯電", "market": "TWSE"}},
                             at("2026-11-02 09:20"), 48, errors=notes)
        self.assertEqual(items, [])
        self.assertEqual(notes, ["公開資訊觀測站即時重大訊息：HTTP 503", "公開資訊觀測站 2303 的重大訊息：HTTP 503",
                                 "Yahoo 新聞 2303：HTTP 503", "Google 新聞 2303：HTTP 503"])

    def test_more_stocks_than_mops_max(self):
        notes = []
        watch = {c: {"name": None, "market": "TWSE"} for c in ("2303", "2330", "2409")}
        news.collect(self.Recorder(), watch, at("2026-11-02 09:20"), 48, google=False, yahoo=False,
                     mops_max=1, errors=notes)
        self.assertEqual(notes, ["公開資訊觀測站的歷史重大訊息只查了前 1 檔（--mops-max）；其餘 2 檔只看今天的即時重大訊息"])
        notes = []
        news.collect(self.Recorder(), watch, at("2026-11-02 09:20"), 6, google=False, yahoo=False, errors=notes)
        self.assertEqual(notes, [])                        # 6 hours back stays inside today: no yearly lists


if __name__ == "__main__":
    unittest.main()
