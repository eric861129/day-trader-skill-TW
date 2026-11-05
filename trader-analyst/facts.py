#!/usr/bin/env python3
"""Earnings reader for trader-analyst (台股). Standard library only.

Reads what a company filed on MOPS (公開資訊觀測站, the official filing site of
TWSE and TPEx) and lays out what changed: monthly revenue, the consolidated
income statement quarter by quarter, and the latest investor conference (法說會).

    python3 facts.py 2303
    python3 facts.py 2303 2330 --months 24 --quarters 8
    python3 facts.py 2303 --json
    python3 facts.py 2303 2330 --metrics trader/metrics.json

Sources (MOPS answers a plain POST form with UTF-8 HTML; one request per page,
at least 2 seconds apart, because MOPS blocks bursts):
  https://mopsov.twse.com.tw/mops/web/ajax_t05st10_ifrs   one month's revenue
  https://mopsov.twse.com.tw/mops/web/ajax_t164sb04       one quarter's income statement
  https://mopsov.twse.com.tw/mops/web/ajax_t100sb07_1     the latest investor conference

How the numbers are built:
  - Monthly revenue: the last N months (default 12) plus one more for the
    month-on-month change. Companies publish a month by the 10th of the next
    month; a month MOPS does not have yet is a note, never a guess.
  - Quarters: the statement for Q1..Q3 carries the quarter itself and the
    same quarter a year earlier (Q1 lists the year to date first; for Q1 the
    two are the same). The Q4 statement is the full year only, so Q4 alone is
    the year minus Q1..Q3 to date, marked 推算; derived EPS is approximate.
  - Net margin and the net income growth use the profit attributable to the
    parent's owners (母公司業主淨利), the line EPS is built on. EPS is basic.
  - Published months and quarters never change, so they are cached under
    trader/state/analyst/<code>/ (--cache DIR; --no-cache to skip). The
    newest unpublished period and the conference are asked for every time.
  - Banks, insurers and financial holding companies file a different
    statement; when MOPS has no general income statement for one, the
    quarters are left out with a note.

--metrics FILE merges one row per code into a JSON file (trailing four
quarters, the latest margins and growth, the latest month). --offline DIR
reads the saved pages instead (t05st10-<code>-<year>-<MM>.html,
t164sb04-<code>-<year>-<0Q>.html, t100sb07_1-<code>-<year>.html, ROC years).

Amounts are in 新台幣仟元 as published. Numbers only: this tool never says
whether a stock is a buy or a sell.
"""

import argparse
import datetime
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

MOPS = "https://mopsov.twse.com.tw"
PAGES = {"revenue": "ajax_t05st10_ifrs", "income": "ajax_t164sb04", "conference": "ajax_t100sb07_1"}
FORM = ("encodeURIComponent=1&step=1&firstin=1&off=1&queryName=co_id&inpuType=co_id"
        "&TYPEK=all&isnew=false&co_id=%s&year=%d")
MOPS_PAUSE = 2.0        # seconds between MOPS requests: it blocks bursts
NOT_FOUND = ("查無所需資料", "查無需求資料", "查無資料", "無應編製合併財報")
BLOCKED = ("FOR SECURITY REASONS", "因為安全性考量", "頁面無法執行", "查詢過於頻繁")
UNIT = "新台幣仟元"
# filing deadlines for general industries (一般行業); banks, insurers and holding companies differ
QUARTER_DUE = {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)}
REVENUE_DUE_DAY = 10

# statement lines, first match wins; labels are normalised by norm()
LINES = {
    "revenue": ("營業收入合計", "收益合計", "淨收益", "營業收入"),
    "gross_profit": ("營業毛利（毛損）淨額", "營業毛利（毛損）"),
    "operating_income": ("營業利益（損失）",),
    "non_operating": ("營業外收入及支出合計",),
    "pretax": ("稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）", "繼續營業單位稅前損益"),
    "net_income": ("本期淨利（淨損）", "本期稅後淨利（淨損）"),
    "net_income_parent": ("母公司業主（淨利／損）", "母公司業主（淨利／淨損）"),
    "eps": ("基本每股盈餘",),
    "eps_diluted": ("稀釋每股盈餘",),
}
AMOUNTS = ("revenue", "gross_profit", "operating_income", "non_operating", "pretax", "net_income",
           "net_income_parent")


class FactsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

def http_post(url, body, timeout=40):
    req = urllib.request.Request(url, data=body.encode("ascii"), headers={
        "User-Agent": tw.UA, "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"})
    with tw.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def page_name(kind, code, year, part=None):
    """The saved file name of one MOPS page: t05st10-2303-115-09.html and so on."""
    base = PAGES[kind].replace("ajax_", "").replace("_ifrs", "")
    if part is None:
        return "%s-%s-%d.html" % (base, code, year)
    return "%s-%s-%d-%02d.html" % (base, code, year, part)


class Fetcher(object):
    """One MOPS page at a time, politely; or the saved pages with --offline."""

    def __init__(self, offline=None, pause=MOPS_PAUSE):
        self.offline, self.pause, self._last, self.requests = offline, pause, 0.0, 0

    def page(self, kind, code, year, part=None):
        """The page's HTML, or None when MOPS (or the offline folder) has nothing for it."""
        if self.offline:
            path = os.path.join(self.offline, page_name(kind, code, year, part))
            if not os.path.isfile(path):
                return None
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        else:
            body = FORM % (code, year)
            if kind == "revenue":
                body += "&month=%02d" % part
            elif kind == "income":
                body += "&season=%02d" % part
            else:
                body += "&month=all"
            gap = time.time() - self._last
            if self._last and gap < self.pause:
                time.sleep(self.pause - gap)
            self.requests += 1
            try:
                text = http_post("%s/mops/web/%s" % (MOPS, PAGES[kind]), body)
            except urllib.error.HTTPError as e:
                raise FactsError("公開資訊觀測站回應 HTTP %s" % e.code)
            except (urllib.error.URLError, OSError) as e:
                raise FactsError("連不上公開資訊觀測站：%s" % getattr(e, "reason", e))
            finally:
                self._last = time.time()
        if any(b in text for b in BLOCKED):
            raise FactsError("公開資訊觀測站暫時擋下查詢（太頻繁），過幾分鐘再試")
        if any(n in text for n in NOT_FOUND):
            return None
        return text


# ---------------------------------------------------------------- parsing

def norm(label):
    return re.sub(r"\s+", "", str(label or "")).replace("∕", "／").replace("/", "／")


def cell_text(raw):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def page_text(text):
    """A whole page as plain text, scripts left out."""
    return cell_text(re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I))


def table_rows(text):
    """Every <tr> as a list of cell texts (th and td alike)."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)(?=<tr[^>]*>|</table>)", text, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)(?=<t[dh][\s>]|</tr>|</table>|$)", tr, re.S | re.I)
        out.append([cell_text(c) for c in cells])
    return out


def number(text):
    t = str(text or "").replace(",", "").replace("&nbsp;", "").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return None


def company_from(text):
    """(name, market) from '本資料由 (上市公司)聯電 公司提供' or '本資料由聯電公司提供'."""
    plain = page_text(text)
    m = re.search(r"本資料由\s*[（(](上市|上櫃|興櫃|公開發行)公司[）)]\s*(\S+?)\s*公司提供", plain)
    if m:
        return m.group(2), {"上市": "TWSE", "上櫃": "TPEx"}.get(m.group(1), m.group(1))
    m = re.search(r"本資料由\s*(\S+?)\s*公司提供", plain)
    if m:
        return m.group(1), None
    m = re.search(r"公司名稱：\s*(\S+)", plain)
    return (m.group(1) if m else None), None


def parse_revenue(text):
    """One ajax_t05st10_ifrs page -> {year, month, revenue, last_year, yoy, ytd, ytd_last_year,
    ytd_yoy, note} (amounts in thousands, yoy as published)."""
    plain = page_text(text)
    m = re.search(r"民國\s*(\d+)\s*年\s*(\d+)\s*月", plain)
    if not m:
        raise FactsError("月營收頁面裡找不到年月")
    vals = {}
    seen = {}
    for row in table_rows(text):
        if len(row) < 2:
            continue
        label = norm(row[0])
        seen[label] = seen.get(label, 0) + 1
        key = {"本月": "revenue", "去年同期": "last_year", "本年累計": "ytd", "去年累計": "ytd_last_year"}.get(label)
        if label == "增減百分比":
            key = "yoy" if seen[label] == 1 else "ytd_yoy"
        if key:
            vals[key] = number(row[1])
        if label.startswith("備註"):
            vals["note"] = row[1].strip() if len(row) > 1 else ""
    if vals.get("revenue") is None:
        raise FactsError("月營收頁面裡找不到本月營收")
    out = {"year": int(m.group(1)) + 1911, "month": int(m.group(2))}
    for k in ("revenue", "last_year", "yoy", "ytd", "ytd_last_year", "ytd_yoy"):
        out[k] = vals.get(k)
    out["note"] = vals.get("note") or ""
    return out


def column_period(label):
    """A column heading -> (kind, western year, quarter): ("q", 2026, 3) for '115年第3季',
    ("ytd", 2026, 3) for '115年01月01日至115年09月30日', ("ytd", 2025, 4) for '114年度'."""
    t = norm(label)
    m = re.match(r"^(\d+)年第(\d)季$", t)
    if m:
        return "q", int(m.group(1)) + 1911, int(m.group(2))
    m = re.match(r"^(\d+)年01月01日至(\d+)年(\d\d)月\d\d日$", t)
    if m:
        return "ytd", int(m.group(2)) + 1911, int(m.group(3)) // 3
    m = re.match(r"^(\d+)年度$", t)
    if m:
        return "ytd", int(m.group(1)) + 1911, 4
    return None


def parse_income(text):
    """One ajax_t164sb04 page -> {"period": (year, quarter), "columns": {(kind, year, q): {line: value}}}.
    None when the page has no general income statement (banks, insurers, holding companies)."""
    rows = table_rows(text)
    head = next((r for r in rows if r and norm(r[0]) == "會計項目"), None)
    if not head:
        return None
    periods = [column_period(c) for c in head[1:]]
    plain = page_text(text)
    m = re.search(r"民國\s*(\d+)\s*年第\s*(\d)\s*季", plain)
    period = (int(m.group(1)) + 1911, int(m.group(2))) if m else None
    columns = {p: {} for p in periods if p}
    wanted = {}
    for key, labels in LINES.items():
        for lab in labels:
            wanted.setdefault(norm(lab), key)
    for row in rows:
        if not row:
            continue
        key = wanted.get(norm(row[0]))
        if not key:
            continue
        values = [number(row[1 + 2 * k]) if 1 + 2 * k < len(row) else None for k in range(len(periods))]
        if all(v is None for v in values):
            continue                                     # a section heading, not the line itself
        rank = [norm(x) for x in LINES[key]].index(norm(row[0]))
        for p, v in zip(periods, values):
            if not p or v is None:
                continue
            have = columns[p].get("_rank_" + key)
            if have is None or rank < have:
                columns[p][key] = v
                columns[p]["_rank_" + key] = rank
    for col in columns.values():
        for k in [k for k in col if k.startswith("_rank_")]:
            del col[k]
    return {"period": period, "columns": columns}


def parse_conference(text):
    """The ajax_t100sb07_1 page -> the latest investor conference, or None."""
    plain = page_text(text)
    m = re.search(r"召開法人說明會日期：\s*(\d+)/(\d+)/(\d+)\s*時間：\s*(\d+)\s*點\s*(\d+)\s*分", plain)
    if not m:
        return None
    day = datetime.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    out = {"date": day.isoformat(), "time": "%02d:%02d" % (int(m.group(4)), int(m.group(5)))}

    def after(label, stop):
        g = re.search(re.escape(label) + r"\s*(.*?)\s*(?:" + stop + r")", plain)
        return g.group(1).strip() if g else ""

    out["place"] = after("召開法人說明會地點：", "法人說明會擇要訊息：")
    out["summary"] = after("法人說明會擇要訊息：", "法人說明會簡報內容|2 公司網站|$")
    files = {}
    for lang, label in (("zh", "中文檔案"), ("en", "英文檔案")):
        g = re.search(label + r"：.*?href='([^']+\.pdf)'", text, re.S)
        if g:
            href = g.group(1)
            files[lang] = {"name": href.rsplit("/", 1)[-1], "url": href if href.startswith("http") else MOPS + href}
    out["files"] = files
    site = re.search(r"公司網站是否有提供法人說明會相關資訊.*?href='(https?://[^']+)'", text, re.S)
    out["website"] = site.group(1) if site else ""
    video = re.search(r"法說會影音連結資訊：\s*<a[^>]*href='(https?://[^']+)'", text, re.S)
    out["video"] = video.group(1) if video else ""
    other = after("其他應敘明事項：", "$")
    out["other"] = "" if other.strip("。. ") in ("無", "") else other
    return out


# ---------------------------------------------------------------- periods

def month_back(year, month, n=1):
    k = year * 12 + (month - 1) - n
    return k // 12, k % 12 + 1


def quarter_back(year, q, n=1):
    k = year * 4 + (q - 1) - n
    return k // 4, k % 4 + 1


def revenue_due(year, month):
    """The day a month's revenue is due: the 10th of the next month."""
    y, m = month_back(year, month, -1)
    return datetime.date(y, m, REVENUE_DUE_DAY)


def quarter_due(year, q):
    m, d = QUARTER_DUE[q]
    return datetime.date(year + 1 if q == 4 else year, m, d)


def roc(year):
    return year - 1911


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 2)


def ratio(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 2)


# ---------------------------------------------------------------- the cache

class Cache(object):
    """Published months and quarters, one JSON file each under <dir>/<code>/."""

    def __init__(self, folder):
        self.folder = folder

    def _path(self, code, name):
        return os.path.join(self.folder, code, name + ".json")

    def get(self, code, name):
        if not self.folder:
            return None
        p = self._path(code, name)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def put(self, code, name, value):
        if not self.folder or value is None:
            return
        p = self._path(code, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)


def _cols_to_json(parsed):
    return {"period": list(parsed["period"]) if parsed["period"] else None,
            "columns": [[list(k), v] for k, v in sorted(parsed["columns"].items())]}


def _cols_from_json(doc):
    return {"period": tuple(doc["period"]) if doc.get("period") else None,
            "columns": {tuple(k): v for k, v in doc["columns"]}}


# ---------------------------------------------------------------- one company

class Company(object):
    def __init__(self, code, fetcher, cache, today):
        self.code, self.f, self.cache, self.today = code, fetcher, cache, today
        self.name, self.market, self.notes = None, None, []
        self._income = {}

    def _learn(self, text):
        if text and not self.name:
            self.name, market = company_from(text)
            self.market = self.market or market

    # monthly revenue
    def month(self, year, month):
        key = "revenue-%d-%02d" % (roc(year), month)
        hit = self.cache.get(self.code, key)
        if hit:
            self.name = self.name or hit.get("name")
            self.market = self.market or hit.get("market")
            return hit
        text = self.f.page("revenue", self.code, roc(year), month)
        if text is None:
            return None
        self._learn(text)
        rec = parse_revenue(text)
        rec["name"], rec["market"] = self.name, self.market
        self.cache.put(self.code, key, rec)
        return rec

    def months(self, n):
        """The last n published months (newest first), plus one more for the month-on-month change."""
        y, m = month_back(self.today.year, self.today.month)
        found, missed, tries = [], [], 0
        while len(found) < n + 1 and tries < n + 4:
            tries += 1
            rec = self.month(y, m)
            if rec is None:
                missed.append((y, m))
                if not found and len(missed) >= 3:
                    break
            else:
                found.append(rec)
            y, m = month_back(y, m)
        rows = []
        for i, rec in enumerate(found[:n]):
            prev = found[i + 1] if i + 1 < len(found) else None
            if prev and (prev["year"], prev["month"]) != month_back(rec["year"], rec["month"]):
                prev = None                       # a gap: no month-on-month change across it
            rows.append({
                "month": "%d-%02d" % (rec["year"], rec["month"]), "revenue": rec["revenue"],
                "mom": pct(rec["revenue"], prev["revenue"] if prev else None),
                # MOPS's own percentages are shown as published (its pages do not always round alike)
                "yoy": rec["yoy"] if rec["yoy"] is not None else pct(rec["revenue"], rec["last_year"]),
                "last_year": rec["last_year"], "ytd": rec["ytd"],
                "ytd_yoy": rec["ytd_yoy"] if rec["ytd_yoy"] is not None else pct(rec["ytd"], rec["ytd_last_year"]),
                "note": rec.get("note", ""),
            })
        latest = (found[0]["year"], found[0]["month"]) if found else None
        for (yy, mm) in missed:
            if latest and (yy, mm) > latest:
                due = revenue_due(yy, mm)
                if self.today > due:
                    self.notes.append("%d-%02d 的營收過了 %s 的期限還查不到。" % (yy, mm, due.isoformat()))
            elif latest:
                self.notes.append("%d-%02d 的營收查不到。" % (yy, mm))
        if not found:
            self.notes.append("查不到 %s 最近的月營收。" % self.code)
        nxt = None
        if latest:
            ny, nm = month_back(latest[0], latest[1], -1)
            nxt = {"month": "%d-%02d" % (ny, nm), "due": revenue_due(ny, nm).isoformat()}
        return rows, nxt

    # quarterly statements
    def income_page(self, year, q):
        if (year, q) in self._income:
            return self._income[(year, q)]
        key = "income-%d-%02d" % (roc(year), q)
        hit = self.cache.get(self.code, key)
        if hit:
            parsed = _cols_from_json(hit)
        else:
            text = self.f.page("income", self.code, roc(year), q)
            if text is None:
                parsed = None
            else:
                self._learn(text)
                parsed = parse_income(text)
                if parsed is None:
                    parsed = {"period": None, "columns": {}, "no_statement": True}
                elif parsed["columns"]:
                    self.cache.put(self.code, key, _cols_to_json(parsed))
        self._income[(year, q)] = parsed
        return parsed

    def column(self, kind, year, q):
        """One column of values from whichever loaded page carries it: the period's own statement,
        or next year's, where it is the comparison column."""
        for py, pq in ((year, q), (year + 1, q)):
            page = self._income.get((py, pq))
            if page and page.get("columns", {}).get((kind, year, q)):
                return page["columns"][(kind, year, q)]
        return None

    def single(self, year, q):
        """(values, derived) for one quarter on its own."""
        v = self.column("q", year, q)
        if v:
            return v, False
        if q == 1:
            v = self.column("ytd", year, 1)
            return (v, False) if v else (None, False)
        whole, before = self.column("ytd", year, q), self.column("ytd", year, q - 1)
        if not whole or not before:
            return None, False
        out = {}
        for k in AMOUNTS:
            if whole.get(k) is not None and before.get(k) is not None:
                out[k] = whole[k] - before[k]
        for k in ("eps", "eps_diluted"):
            if whole.get(k) is not None and before.get(k) is not None:
                out[k] = round(whole[k] - before[k], 2)
        return out, True

    def quarters(self, n):
        """The last n published quarters, newest first."""
        y, q = quarter_back(self.today.year, (self.today.month - 1) // 3 + 1)
        wanted, tries = [], 0
        while len(wanted) < n and tries < n + 3:
            tries += 1
            page = self.income_page(y, q)
            if page is None:
                due = quarter_due(y, q)
                if wanted:
                    self.notes.append("%d Q%d 的財報查不到。" % (y, q))
                elif self.today > due:
                    self.notes.append("%d Q%d 的財報過了 %s 的期限還查不到（一般產業的期限）。"
                                      % (y, q, due.isoformat()))
            elif page.get("no_statement"):
                self.notes.append("%s 在公開資訊觀測站沒有一般產業的綜合損益表（銀行、保險和金控的格式不同），"
                                  "這裡不列季報。" % self.code)
                return [], None
            else:
                if q == 4:
                    self.income_page(y, 3)       # Q4 alone = the year minus Q1..Q3 to date
                wanted.append((y, q))
            y, q = quarter_back(y, q)
        rows = []
        for (yy, qq) in wanted:
            cur, derived = self.single(yy, qq)
            if not cur:
                self.notes.append("%d Q%d 拆不出單季數字。" % (yy, qq))
                continue
            prior, _ = self.single(yy - 1, qq)
            prev, _ = self.single(*quarter_back(yy, qq))
            rev = cur.get("revenue")
            row = {
                "quarter": "%d Q%d" % (yy, qq), "year": yy, "q": qq, "derived": derived,
                "revenue": rev, "gross_profit": cur.get("gross_profit"),
                "operating_income": cur.get("operating_income"), "non_operating": cur.get("non_operating"),
                "pretax": cur.get("pretax"), "net_income": cur.get("net_income"),
                "net_income_parent": cur.get("net_income_parent"), "eps": cur.get("eps"),
                "eps_diluted": cur.get("eps_diluted"),
                "gross_margin": ratio(cur.get("gross_profit"), rev),
                "operating_margin": ratio(cur.get("operating_income"), rev),
                "net_margin": ratio(cur.get("net_income_parent"), rev),
                "revenue_yoy": pct(rev, (prior or {}).get("revenue")),
                "net_income_yoy": pct(cur.get("net_income_parent"), (prior or {}).get("net_income_parent")),
                "eps_last_year": (prior or {}).get("eps"),
                "eps_yoy": pct(cur.get("eps"), (prior or {}).get("eps")),
                "revenue_qoq": pct(rev, (prev or {}).get("revenue")),
                "prev_margins": None if not prev else {
                    "gross": ratio(prev.get("gross_profit"), prev.get("revenue")),
                    "operating": ratio(prev.get("operating_income"), prev.get("revenue")),
                    "net": ratio(prev.get("net_income_parent"), prev.get("revenue"))},
            }
            rows.append(row)
        nxt = None
        if wanted:
            ny, nq = quarter_back(wanted[0][0], wanted[0][1], -1)
            nxt = {"quarter": "%d Q%d" % (ny, nq), "due": quarter_due(ny, nq).isoformat()}
        return rows, nxt

    def conference(self):
        for year in (self.today.year, self.today.year - 1):
            text = self.f.page("conference", self.code, roc(year))
            if text is None:
                continue
            self._learn(text)
            conf = parse_conference(text)
            if conf:
                conf["status"] = "即將召開" if conf["date"] > self.today.isoformat() else "已召開"
                return conf
        return None


def ttm(rows):
    """The last four quarters added up, when the four are consecutive."""
    if len(rows) < 4:
        return {}
    four = rows[:4]
    keys = [r["year"] * 4 + r["q"] for r in four]
    if keys != list(range(keys[0], keys[0] - 4, -1)):
        return {}
    out = {}
    for k in ("revenue", "net_income_parent"):
        vals = [r.get(k) for r in four]
        out[k] = sum(vals) if all(v is not None for v in vals) else None
    eps = [r.get("eps") for r in four]
    out["eps"] = round(sum(eps), 2) if all(v is not None for v in eps) else None
    out["from"], out["to"] = four[-1]["quarter"], four[0]["quarter"]
    return out


def changes(result):
    """Plain sentences: what changed in the latest quarter and the latest month."""
    out = []
    rows = result["quarters"]
    if rows:
        cur = rows[0]
        line = "%s%s：營收 %s 仟元" % (cur["quarter"], "（推算）" if cur["derived"] else "", _amt(cur["revenue"]))
        if cur["revenue_yoy"] is not None:
            line += "，年增 %s" % _pp(cur["revenue_yoy"])
        if cur["net_income_parent"] is not None:
            line += "；歸屬母公司淨利 %s 仟元" % _amt(cur["net_income_parent"])
            if cur["net_income_yoy"] is not None:
                line += "，年增 %s" % _pp(cur["net_income_yoy"])
        if cur["eps"] is not None:
            line += "；基本 EPS %.2f 元" % cur["eps"]
            if cur["eps_last_year"] is not None:
                line += "（去年同季 %.2f 元，%s）" % (cur["eps_last_year"], _pp(cur["eps_yoy"]))
        out.append(line + "。")
        prev = cur.get("prev_margins") or {}
        parts = []
        for key, pkey, name in (("gross_margin", "gross", "毛利率"), ("operating_margin", "operating", "營業利益率"),
                                ("net_margin", "net", "淨利率")):
            if cur[key] is None:
                continue
            s = "%s %.2f%%" % (name, cur[key])
            if prev.get(pkey) is not None:
                s += "（比上一季 %+.2f 個百分點）" % (cur[key] - prev[pkey])
            parts.append(s)
        if parts:
            out.append("，".join(parts) + "。")
        if cur["gross_margin"] is None and cur["revenue"] is not None:
            out.append("沒有營業毛利科目，毛利率留白（金融業常見）。")
        non_op, pretax = cur.get("non_operating"), cur.get("pretax")
        if non_op is not None and pretax and abs(non_op) >= 0.2 * abs(pretax):
            out.append("業外收支 %s 仟元，占稅前淨利 %.1f%%：投資收益、匯兌這些不屬於本業的項目也在淨利裡，"
                       "看成長之前先拆開。" % (_amt(non_op), 100.0 * non_op / pretax))
        if cur["revenue_qoq"] is not None:
            out.append("營收比上一季 %s（很多產業有季節性，先跟去年同季比）。" % _pp(cur["revenue_qoq"]))
        heavy = ["%s %.1f%%" % (r["quarter"], 100.0 * r["non_operating"] / r["pretax"]) for r in rows[1:]
                 if r.get("non_operating") is not None and r.get("pretax")
                 and abs(r["non_operating"]) >= 0.2 * abs(r["pretax"])]
        if heavy:
            out.append("業外收支占稅前淨利兩成以上的季度：%s。這幾季的淨利率和 EPS 不全是本業賺的。"
                       % "、".join(heavy))
    months = result["months"]
    if months:
        m = months[0]
        line = "最新月營收 %s：%s 仟元" % (m["month"], _amt(m["revenue"]))
        bits = []
        if m["mom"] is not None:
            bits.append("月增 %s" % _pp(m["mom"]))
        if m["yoy"] is not None:
            bits.append("年增 %s" % _pp(m["yoy"]))
        if m["ytd_yoy"] is not None:
            bits.append("今年累計年增 %s" % _pp(m["ytd_yoy"]))
        if bits:
            line += "，" + "，".join(bits)
        out.append(line + "。")
        if m["note"]:
            out.append("公司的營收說明：%s" % m["note"])
    return out


def analyse(code, fetcher, cache, today, months=12, quarters=4):
    co = Company(code, fetcher, cache, today)
    month_rows, next_month = co.months(months) if months else ([], None)
    quarter_rows, next_quarter = co.quarters(quarters) if quarters else ([], None)
    conf = co.conference()
    result = {"symbol": code, "name": co.name, "market": co.market, "unit": UNIT,
              "source": "公開資訊觀測站", "as_of": today.isoformat(), "months": month_rows,
              "quarters": quarter_rows, "ttm": ttm(quarter_rows), "conference": conf,
              "next": {"revenue": next_month, "quarter": next_quarter}, "notes": co.notes}
    result["changes"] = changes(result)
    return result


# ---------------------------------------------------------------- output

def _amt(v):
    return "-" if v is None else "{:,}".format(v)


def _pp(v):
    return "-" if v is None else "%+.2f%%" % v


def _pm(v):
    return "-" if v is None else "%.2f%%" % v


def _eps(v):
    return "-" if v is None else "%.2f" % v


def width(text):
    """Columns a string takes in a terminal: CJK characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def row(cells, widths):
    """A table row: the first cell left-aligned, the rest right-aligned, CJK-aware."""
    out = []
    for i, (c, w) in enumerate(zip(cells, widths)):
        gap = " " * max(0, w - width(c))
        out.append(c + gap if i == 0 else gap + c)
    return "  " + " ".join(out)


def render(r):
    where = {"TWSE": "上市", "TPEx": "上櫃"}.get(r["market"], r["market"] or "")
    out = ["%s %s%s，資料：公開資訊觀測站，單位%s（EPS 為元）。" % (
        r["name"] or "", r["symbol"], ("（%s）" % where) if where else "", UNIT), ""]
    if r["months"]:
        out.append("月營收（最近 %d 個月）" % len(r["months"]))
        widths = (9, 13, 9, 9, 13)
        out.append(row(("月份", "營收", "月增", "年增", "今年累計年增"), widths))
        for m in r["months"]:
            out.append(row((m["month"], _amt(m["revenue"]), _pp(m["mom"]), _pp(m["yoy"]), _pp(m["ytd_yoy"])),
                           widths))
        out.append("")
    if r["quarters"]:
        out.append("季報（合併綜合損益表，最近 %d 季）" % len(r["quarters"]))
        widths = (9, 13, 9, 8, 8, 8, 6, 10)
        out.append(row(("季別", "營收", "年增", "毛利率", "營益率", "淨利率", "EPS", "EPS 年增"), widths))
        for q in r["quarters"]:
            out.append(row((q["quarter"], _amt(q["revenue"]), _pp(q["revenue_yoy"]), _pm(q["gross_margin"]),
                            _pm(q["operating_margin"]), _pm(q["net_margin"]), _eps(q["eps"]), _pp(q["eps_yoy"])),
                           widths) + ("  推算" if q["derived"] else ""))
        t = r["ttm"]
        if t:
            out.append("近四季（%s 到 %s）：營收 %s 仟元，歸屬母公司淨利 %s 仟元，EPS %s 元。" % (
                t["from"], t["to"], _amt(t["revenue"]), _amt(t["net_income_parent"]), _eps(t["eps"])))
        out.append("淨利率和淨利年增用「歸屬於母公司業主」的淨利；EPS 是基本每股盈餘。")
        if any(q["derived"] for q in r["quarters"]):
            out.append("第 4 季是全年減去前三季累計推算的，EPS 是近似值（全年和前三季的股數不同）。")
        out.append("")
    if r["changes"]:
        out.append("有什麼變化：")
        for line in r["changes"]:
            out.append("  - " + line)
        out.append("")
    c = r["conference"]
    if c:
        out.append("法說會（%s）" % c["status"])
        out.append("  %s %s，%s：%s" % (c["date"], c["time"], c["place"] or "地點未註明", c["summary"] or "沒有摘要"))
        for lang, label in (("zh", "中文簡報"), ("en", "英文簡報")):
            if c["files"].get(lang):
                out.append("  %s：%s" % (label, c["files"][lang]["url"]))
        if c["video"]:
            out.append("  影音：%s" % c["video"])
        elif c["website"]:
            out.append("  公司網站：%s" % c["website"])
        if c["other"]:
            out.append("  其他：%s" % c["other"])
        out.append("")
    nxt = r["next"]
    lines = []
    if nxt.get("revenue"):
        lines.append("  %s 的營收：最晚 %s 公布。" % (nxt["revenue"]["month"], nxt["revenue"]["due"]))
    if nxt.get("quarter"):
        lines.append("  %s 的財報：一般產業最晚 %s 公布（銀行、保險、金控和證券業的期限不同）。"
                     % (nxt["quarter"]["quarter"], nxt["quarter"]["due"]))
    if lines:
        out.append("接下來：")
        out.extend(lines)
        out.append("")
    for n in r["notes"]:
        out.append("注意：" + n)
    out.append("這是公開資訊的整理，不構成投資建議。")
    return "\n".join(out) + "\n"


def metrics_row(r):
    q = r["quarters"][0] if r["quarters"] else {}
    m = r["months"][0] if r["months"] else {}
    t = r["ttm"]
    return {"symbol": r["symbol"], "name": r["name"], "market": r["market"], "quarter": q.get("quarter"),
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net_income_parent"), "eps_ttm": t.get("eps"),
            "revenue_growth_yoy": q.get("revenue_yoy"), "net_income_growth_yoy": q.get("net_income_yoy"),
            "eps_growth_yoy": q.get("eps_yoy"), "gross_margin": q.get("gross_margin"),
            "operating_margin": q.get("operating_margin"), "net_margin": q.get("net_margin"),
            "revenue_month": m.get("month"), "revenue_month_amount": m.get("revenue"),
            "revenue_mom": m.get("mom"), "revenue_yoy": m.get("yoy"), "revenue_ytd_yoy": m.get("ytd_yoy"),
            "unit": UNIT, "source": "公開資訊觀測站"}


def write_metrics(path, rows, today):
    existing = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for r in json.load(fh).get("companies", []):
                existing[r.get("symbol") or r.get("ticker")] = r
    for r in rows:
        existing[r["symbol"]] = r
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"as_of": today.isoformat(), "unit": UNIT,
                   "companies": sorted(existing.values(), key=lambda r: str(r.get("symbol")))},
                  fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return len(existing)


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="讀公開資訊觀測站的月營收、季報和法說會（trader-analyst）。")
    ap.add_argument("symbols", nargs="*", help="代號，例如 2303、2330、6488.TWO")
    ap.add_argument("--months", type=int, default=12, help="月營收看幾個月（預設 12）")
    ap.add_argument("--quarters", type=int, default=4, help="季報看幾季（預設 4）")
    ap.add_argument("--metrics", metavar="FILE", help="每檔一列，合併寫進這個 JSON")
    ap.add_argument("--cache", default=os.path.join("trader", "state", "analyst"),
                    help="已公布的月份和季度存在這裡（預設 trader/state/analyst）")
    ap.add_argument("--no-cache", action="store_true", help="不讀也不寫快取")
    ap.add_argument("--offline", metavar="DIR", help="讀存下來的頁面，不連網")
    ap.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 21:00\"")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.symbols:
        ap.print_help()
        return 2
    try:
        today = tw.to_taipei(tw.parse_now(a.now)).date()
    except ValueError as e:
        print("facts.py：%s" % e, file=sys.stderr)
        return 2
    if a.months < 0 or a.quarters < 0:
        print("facts.py：--months 和 --quarters 不能是負數", file=sys.stderr)
        return 2
    fetcher = Fetcher(offline=a.offline)
    cache = Cache(None if a.no_cache else a.cache)
    results, failed = [], 0
    for raw in a.symbols:
        try:
            code, _ = tw.parse_symbol(raw)
        except ValueError as e:
            print("facts.py：%s" % e, file=sys.stderr)
            failed += 1
            continue
        if tw.is_etf_like(code):
            print("facts.py：%s 是 ETF（或 ETN、受益證券），沒有月營收和財報。" % code, file=sys.stderr)
            failed += 1
            continue
        try:
            r = analyse(code, fetcher, cache, today, a.months, a.quarters)
        except FactsError as e:
            print("facts.py：%s：%s" % (code, e), file=sys.stderr)
            failed += 1
            continue
        if not r["months"] and not r["quarters"] and not r["conference"]:
            print("facts.py：%s：公開資訊觀測站查不到月營收、季報或法說會（代號對嗎？）" % code, file=sys.stderr)
            failed += 1
            continue
        results.append(r)
        if not a.json:
            sys.stdout.write(render(r))
            sys.stdout.write("\n")
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if a.metrics and results:
        n = write_metrics(a.metrics, [metrics_row(r) for r in results], today)
        if not a.json:
            print("已寫入 %s（%d 家公司）" % (a.metrics, n))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
