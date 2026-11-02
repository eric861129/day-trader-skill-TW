import datetime
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")
DAY = datetime.date(2026, 11, 2)
NOW = "2026-11-02 21:00"


def page(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


def reel(today=DAY, fetcher=None, months=12, quarters=4):
    return facts.analyse("2303", fetcher or facts.Fetcher(offline=DIR), facts.Cache(None), today, months, quarters)


class Skip(facts.Fetcher):
    """The offline pages, minus the ones listed: as if MOPS did not have them yet."""

    def __init__(self, missing=(), swap=None):
        facts.Fetcher.__init__(self, offline=DIR)
        self.missing, self.swap = set(missing), swap or {}

    def page(self, kind, code, year, part=None):
        if (kind, year, part) in self.missing:
            return None
        if (kind, year, part) in self.swap:
            return page(self.swap[(kind, year, part)])
        return facts.Fetcher.page(self, kind, code, year, part)


class Revenue(unittest.TestCase):
    def test_one_month(self):
        r = facts.parse_revenue(page("t05st10-2303-115-09.html"))
        self.assertEqual(r, {"year": 2026, "month": 9, "revenue": 25350431, "last_year": 19927175,
                             "yoy": 27.22, "ytd": 204009681, "ytd_last_year": 175743654, "ytd_yoy": 16.08,
                             "note": ""})

    def test_an_unpublished_month_is_none(self):
        self.assertIn("查無需求資料", page("t05st10-2303-115-10.html"))
        self.assertIsNone(facts.Fetcher(offline=DIR).page("revenue", "2303", 115, 10))
        self.assertIsNone(facts.Fetcher(offline=DIR).page("revenue", "2303", 100, 1))    # no file at all

    def test_company(self):
        self.assertEqual(facts.company_from(page("t05st10-2303-115-09.html")), ("聯電", "TWSE"))
        self.assertEqual(facts.company_from(page("t164sb04-2303-115-03.html")), ("聯電", None))

    def test_twelve_months(self):
        r = reel()
        self.assertEqual(len(r["months"]), 12)
        first, last = r["months"][0], r["months"][-1]
        self.assertEqual((first["month"], first["revenue"], first["mom"], first["yoy"], first["ytd_yoy"]),
                         ("2026-09", 25350431, 1.22, 27.22, 16.08))
        self.assertEqual((last["month"], last["revenue"], last["mom"], last["yoy"]), ("2025-10", 21294974, 6.86, -0.36))
        self.assertEqual([m["mom"] for m in r["months"][:4]], [1.22, 5.04, 3.11, 0.79])
        self.assertEqual(r["next"]["revenue"], {"month": "2026-10", "due": "2026-11-10"})
        self.assertFalse([n for n in r["notes"] if "營收" in n])          # October is simply not due yet

    def test_overdue_and_missing_months(self):
        r = reel(datetime.date(2026, 11, 12))
        self.assertIn("2026-10 的營收過了 2026-11-10 的期限還查不到。", r["notes"])
        r = reel(fetcher=Skip(missing={("revenue", 115, 5)}), months=6)
        self.assertIn("2026-05 的營收查不到。", r["notes"])
        self.assertEqual([m["month"] for m in r["months"]],
                         ["2026-09", "2026-08", "2026-07", "2026-06", "2026-04", "2026-03"])
        self.assertIsNone(r["months"][3]["mom"])             # no May: no month-on-month change for June
        self.assertEqual(r["months"][4]["mom"], 8.80)

    def test_due_dates(self):
        self.assertEqual(facts.revenue_due(2026, 10), datetime.date(2026, 11, 10))
        self.assertEqual(facts.revenue_due(2026, 12), datetime.date(2027, 1, 10))
        self.assertEqual(facts.quarter_due(2026, 3), datetime.date(2026, 11, 14))
        self.assertEqual(facts.quarter_due(2026, 4), datetime.date(2027, 3, 31))
        self.assertEqual(facts.month_back(2026, 1), (2025, 12))
        self.assertEqual(facts.quarter_back(2026, 1), (2025, 4))


class Income(unittest.TestCase):
    def test_columns(self):
        self.assertEqual(facts.column_period("115年第3季"), ("q", 2026, 3))
        self.assertEqual(facts.column_period("115年01月01日至115年09月30日"), ("ytd", 2026, 3))
        self.assertEqual(facts.column_period("114年度"), ("ytd", 2025, 4))
        self.assertIsNone(facts.column_period("會計項目"))

    def test_the_third_quarter(self):
        p = facts.parse_income(page("t164sb04-2303-115-03.html"))
        self.assertEqual(p["period"], (2026, 3))
        self.assertEqual(sorted(p["columns"]), [("q", 2025, 3), ("q", 2026, 3), ("ytd", 2025, 3), ("ytd", 2026, 3)])
        q = p["columns"][("q", 2026, 3)]
        self.assertEqual((q["revenue"], q["gross_profit"], q["operating_income"], q["net_income_parent"], q["eps"]),
                         (74239117, 25204523, 18642533, 16433947, 1.32))
        self.assertEqual(q["net_income"], 16389129)           # the whole group, before minorities
        self.assertEqual(q["eps_diluted"], 1.31)
        self.assertEqual(p["columns"][("q", 2025, 3)]["eps"], 1.20)
        self.assertEqual(p["columns"][("ytd", 2026, 3)]["eps"], 6.00)

    def test_q1_lists_the_year_to_date_first(self):
        p = facts.parse_income(page("t164sb04-2303-115-01.html"))
        self.assertEqual(p["columns"][("ytd", 2026, 1)], p["columns"][("q", 2026, 1)])
        self.assertEqual(p["columns"][("q", 2026, 1)]["revenue"], 61037902)
        self.assertEqual(p["columns"][("q", 2025, 1)]["eps"], 0.62)

    def test_q4_is_the_full_year_only(self):
        p = facts.parse_income(page("t164sb04-2303-114-04.html"))
        self.assertEqual(sorted(p["columns"]), [("ytd", 2024, 4), ("ytd", 2025, 4)])
        year = p["columns"][("ytd", 2025, 4)]
        self.assertEqual((year["revenue"], year["eps"]), (237553199, 3.34))
        self.assertEqual(year["net_income_parent"], 41716249)     # its label is written with U+2215, not ／

    def test_no_statement(self):
        self.assertIsNone(facts.parse_income(page("t164sb04-2801-115-03.html")))    # 彰銀: a company picker
        self.assertIsNone(facts.Fetcher(offline=DIR).page("income", "2303", 115, 4))  # 查無所需資料

    def test_four_quarters(self):
        r = reel()
        self.assertEqual([q["quarter"] for q in r["quarters"]], ["2026 Q3", "2026 Q2", "2026 Q1", "2025 Q4"])
        q3 = r["quarters"][0]
        self.assertEqual((q3["revenue"], q3["revenue_yoy"], q3["gross_margin"], q3["operating_margin"],
                          q3["net_margin"]), (74239117, 25.56, 33.95, 25.11, 22.14))
        self.assertEqual((q3["eps"], q3["eps_last_year"], q3["eps_yoy"], q3["net_income_yoy"]), (1.32, 1.20, 10.0, 9.69))
        self.assertEqual(q3["revenue_qoq"], 8.01)
        self.assertEqual(q3["prev_margins"], {"gross": 32.48, "operating": 21.75, "net": 61.48})
        q2 = r["quarters"][1]
        self.assertEqual((q2["revenue"], q2["eps"], q2["eps_yoy"]), (68732662, 3.39, 377.46))
        self.assertFalse(any(q["derived"] for q in r["quarters"][:3]))

    def test_the_fourth_quarter_is_derived(self):
        q4 = reel()["quarters"][3]
        self.assertTrue(q4["derived"])
        self.assertEqual(q4["revenue"], 237553199 - 175743654)      # 61,809,545
        self.assertEqual(q4["eps"], 0.80)                           # 3.34 - 2.54, approximate
        self.assertEqual((q4["revenue_yoy"], q4["eps_yoy"]), (2.36, 17.65))   # 2024 Q4: 3.80 - 3.12 = 0.68
        self.assertEqual(q4["gross_margin"], 30.67)

    def test_trailing_four_quarters(self):
        self.assertEqual(reel()["ttm"], {"revenue": 265819226, "net_income_parent": 84920301, "eps": 6.80,
                                         "from": "2025 Q4", "to": "2026 Q3"})
        self.assertEqual(facts.ttm(reel(quarters=3)["quarters"]), {})

    def test_an_unfiled_quarter_steps_back(self):
        skip = Skip(missing={("income", 115, 3)})
        r = reel(fetcher=skip)
        self.assertEqual(r["quarters"][0]["quarter"], "2026 Q2")
        self.assertEqual(r["next"]["quarter"], {"quarter": "2026 Q3", "due": "2026-11-14"})
        self.assertFalse([n for n in r["notes"] if "財報" in n])          # before the deadline: no alarm
        late = reel(datetime.date(2026, 11, 20), Skip(missing={("income", 115, 3)}))
        self.assertIn("2026 Q3 的財報過了 2026-11-14 的期限還查不到（一般產業的期限）。", late["notes"])

    def test_a_bank_is_left_out_with_a_note(self):
        r = reel(fetcher=Skip(swap={("income", 115, 3): "t164sb04-2801-115-03.html"}))
        self.assertEqual(r["quarters"], [])
        self.assertTrue(any("銀行、保險和金控的格式不同" in n for n in r["notes"]))
        self.assertEqual(len(r["months"]), 12)                     # the months still come through


class Conference(unittest.TestCase):
    def test_the_latest(self):
        c = facts.parse_conference(page("t100sb07_1-2303-115.html"))
        self.assertEqual((c["date"], c["time"], c["place"]), ("2026-10-28", "17:00", "線上法說會"))
        self.assertEqual(c["summary"], "本公司2026年第三季財務暨營運報告。")
        self.assertEqual(c["files"]["zh"], {"name": "230320261028M001.pdf",
                                            "url": "https://mopsov.twse.com.tw/nas/STR/230320261028M001.pdf"})
        self.assertEqual(c["files"]["en"]["name"], "230320261028E001.pdf")
        self.assertEqual(c["website"], "http://www.umc.com")
        self.assertTrue(c["video"].endswith("/2026/2026Q3"))
        self.assertEqual(c["other"], "")

    def test_status(self):
        self.assertEqual(reel()["conference"]["status"], "已召開")
        self.assertEqual(reel(datetime.date(2026, 10, 20))["conference"]["status"], "即將召開")
        self.assertIsNone(facts.Fetcher(offline=DIR).page("conference", "9999", 115))


class Words(unittest.TestCase):
    def test_what_changed(self):
        lines = reel()["changes"]
        self.assertEqual(lines[0], "2026 Q3：營收 74,239,117 仟元，年增 +25.56%；歸屬母公司淨利 16,433,947 仟元，"
                                   "年增 +9.69%；基本 EPS 1.32 元（去年同季 1.20 元，+10.00%）。")
        self.assertEqual(lines[1], "毛利率 33.95%（比上一季 +1.47 個百分點），營業利益率 25.11%（比上一季 +3.36 個百分點），"
                                   "淨利率 22.14%（比上一季 -39.34 個百分點）。")
        self.assertIn("業外收支占稅前淨利兩成以上的季度：2026 Q2 66.9%、2026 Q1 32.2%、2025 Q4 21.1%。"
                      "這幾季的淨利率和 EPS 不全是本業賺的。", lines)
        self.assertEqual(lines[-1], "最新月營收 2026-09：25,350,431 仟元，月增 +1.22%，年增 +27.22%，今年累計年增 +16.08%。")

    def test_the_report(self):
        code, out, err = run(["2303", "--offline", DIR, "--no-cache", "--now", NOW])
        self.assertEqual(code, 0, err)
        for s in ("聯電 2303（上市），資料：公開資訊觀測站，單位新台幣仟元（EPS 為元）。",
                  "  2026-09      25,350,431    +1.22%   +27.22%       +16.08%",
                  "  2025 Q4      61,809,545    +2.36%   30.67%   19.78%   16.27%   0.80    +17.65%  推算",
                  "近四季（2025 Q4 到 2026 Q3）：營收 265,819,226 仟元，歸屬母公司淨利 84,920,301 仟元，EPS 6.80 元。",
                  "法說會（已召開）", "  2026-10-28 17:00，線上法說會：本公司2026年第三季財務暨營運報告。",
                  "  2026-10 的營收：最晚 2026-11-10 公布。",
                  "  2026 Q4 的財報：一般產業最晚 2027-03-31 公布（銀行、保險、金控和證券業的期限不同）。",
                  "這是公開資訊的整理，不構成投資建議。"):
            self.assertIn(s, out)
        self.assertNotIn("try {", out)                    # the page's scripts stay out
        self.assertNotIn(chr(0x2014), out)              # no em dash in what the user reads


class Online(unittest.TestCase):
    """MOPS itself, played back from the fixtures."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._post, self._sleep = facts.http_post, facts.time.sleep

        def post(url, body):
            self.calls.append((url, body))
            f = dict(x.split("=", 1) for x in body.split("&"))
            kind = {v: k for k, v in facts.PAGES.items()}[url.rsplit("/", 1)[1]]
            part = f.get("month") if kind == "revenue" else f.get("season")
            name = facts.page_name(kind, f["co_id"], int(f["year"]), int(part) if part else None)
            path = os.path.join(DIR, name)
            if not os.path.isfile(path):
                return "<html><body>查無資料</body></html>"
            with open(path, encoding="utf-8") as fh:
                return fh.read()

        facts.http_post = post
        facts.time.sleep = self.sleeps.append
        self.addCleanup(setattr, facts, "http_post", self._post)
        self.addCleanup(setattr, facts.time, "sleep", self._sleep)

    def test_the_form_and_the_pause(self):
        f = facts.Fetcher()
        f.page("revenue", "2303", 115, 9)
        f.page("income", "2303", 115, 3)
        f.page("conference", "2303", 115)
        self.assertEqual(self.calls[0], ("https://mopsov.twse.com.tw/mops/web/ajax_t05st10_ifrs",
                                         "encodeURIComponent=1&step=1&firstin=1&off=1&queryName=co_id&inpuType=co_id"
                                         "&TYPEK=all&isnew=false&co_id=2303&year=115&month=09"))
        self.assertTrue(self.calls[1][1].endswith("&season=03"))
        self.assertTrue(self.calls[2][1].endswith("&month=all"))
        self.assertGreaterEqual(facts.MOPS_PAUSE, 2)
        self.assertEqual(len(self.sleeps), 2)                       # none before the first request
        self.assertTrue(all(s > 1.9 for s in self.sleeps), self.sleeps)

    def test_the_cache_keeps_what_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ["2303", "--cache", tmp, "--now", NOW]
            code, first, err = run(args)
            self.assertEqual(code, 0, err)
            n = len(self.calls)
            self.assertEqual(n, 14 + 5 + 1)        # 13 months + October, 5 statements, the conference
            self.assertTrue(os.path.isfile(os.path.join(tmp, "2303", "revenue-115-09.json")))
            self.assertFalse(os.path.isfile(os.path.join(tmp, "2303", "revenue-115-10.json")))
            code, second, err = run(args)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(self.calls) - n, 2)       # only October's revenue and the conference again
            self.assertEqual(first, second)

    def test_a_block_is_an_error_not_a_crash(self):
        facts.http_post = lambda url, body: "<html>FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!</html>"
        code, out, err = run(["2303", "--no-cache", "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("暫時擋下查詢", err)

        def down(url, body):
            raise urllib.error.URLError("timed out")
        facts.http_post = down
        code, out, err = run(["2303", "--no-cache", "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("連不上公開資訊觀測站", err)

    def test_tls_still_verifies(self):
        ctx = facts.tw.tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            self.assertFalse(ctx.verify_flags & ssl.VERIFY_X509_STRICT)


class Cli(unittest.TestCase):
    def test_json(self):
        code, out, err = run(["2303", "--offline", DIR, "--no-cache", "--now", NOW, "--json",
                              "--months", "3", "--quarters", "2"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc[0]["symbol"], doc[0]["name"], doc[0]["market"]), ("2303", "聯電", "TWSE"))
        self.assertEqual((len(doc[0]["months"]), len(doc[0]["quarters"])), (3, 2))
        self.assertEqual(doc[0]["unit"], "新台幣仟元")

    def test_metrics_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "metrics.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"as_of": "2026-10-01", "companies": [{"symbol": "2330", "name": "台積電"}]}, fh)
            code, out, err = run(["2303.TW", "--offline", DIR, "--no-cache", "--now", NOW, "--metrics", path])
            self.assertEqual(code, 0, err)
            self.assertIn("（2 家公司）", out)
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        self.assertEqual(doc["as_of"], "2026-11-02")
        self.assertEqual([c["symbol"] for c in doc["companies"]], ["2303", "2330"])
        row = doc["companies"][0]
        self.assertEqual((row["quarter"], row["revenue_ttm"], row["eps_ttm"], row["revenue_growth_yoy"],
                          row["gross_margin"], row["revenue_month"], row["revenue_yoy"]),
                         ("2026 Q3", 265819226, 6.8, 25.56, 33.95, "2026-09", 27.22))

    def test_the_example_metrics_are_a_real_run(self):
        with open(os.path.join(ROOT, "trader-analyst", "metrics.example.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = {c["symbol"]: c for c in doc["companies"]}
        self.assertEqual(sorted(rows), ["2303", "2330"])
        self.assertEqual(rows["2303"], facts.metrics_row(reel()))

    def test_refusals(self):
        self.assertEqual(run([])[0], 2)
        code, out, err = run(["0050", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("ETF", err)
        self.assertEqual(run(["TSLA", "--offline", DIR, "--now", NOW])[0], 2)
        code, out, err = run(["9999", "--offline", DIR, "--no-cache", "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("查不到月營收、季報或法說會", err)
        code, out, err = run(["2303", "0050", "--offline", DIR, "--no-cache", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("聯電 2303", out)
        self.assertEqual(run(["2303", "--now", "tomorrow"])[0], 2)
        self.assertEqual(run(["2303", "--months", "-1"])[0], 2)

    def test_no_us_sources_left(self):
        with open(os.path.join(ROOT, "trader-analyst", "facts.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("sec.gov", "sec_user_agent", "SEC_USER_AGENT", "company_tickers", "New York"):
            self.assertNotIn(bad, src)
        self.assertIn("import twmarket", src)


if __name__ == "__main__":
    unittest.main()
