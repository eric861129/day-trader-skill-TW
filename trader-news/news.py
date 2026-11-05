#!/usr/bin/env python3
"""News monitor for trader-news (Taiwan market). Standard library only.

Watches headlines and the companies' own material information (重大訊息)
for the stocks you watch or hold and prints only what is new since the last
check, newest first, with how far each stock has moved today and a flag on
anything that names another stock you watch.

    python3 news.py                                  # watchlist + plan + portfolio
    python3 news.py --symbols 2303 2330 6488.TWO
    python3 news.py --symbols 2303 --all --hours 168 # a week, seen or not
    python3 news.py --no-google --no-mops            # Yahoo headlines only
    python3 news.py --no-moves                       # skip the price line per stock

Your stocks, when --symbols is not given, in this order: the symbol in
trader/plan.json (trader-exit), the positions in trader/portfolio.json, every
hit in trader/watchlist.json (trader-scanner). The market (上市 .TW or 上櫃
.TWO) and the name come from those files, trader/universe.txt, a .TW / .TWO
suffix, or the MIS quote.

Sources:
  - Yahoo headline RSS per stock (Chinese, mostly Yahoo股市 and the wires):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=2303.TW&region=TW&lang=zh-Hant-TW
  - Google News RSS search for "<name> <code>": many outlets, links are
    news.google.com redirects to the story:
    https://news.google.com/rss/search?q=聯電+2303&hl=zh-TW&gl=TW&ceid=TW:zh-Hant
  - MOPS (公開資訊觀測站) 重大訊息, the companies' official announcements:
    today's list for every company (one request, t05sr01_1), and, when the
    window reaches before today, each company's list for the year
    (ajax_t05st01, one request per company, at most --mops-max companies,
    2 seconds apart: MOPS blocks bursts).
  - The move: TWSE MIS quotes (one request for every stock: last price,
    yesterday's close, limit up and down), Yahoo's chart as the fallback.

Times print in Taipei time. State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. With --now, nothing published
after that time is shown or remembered: a replay never sees the future.
Offline: --offline DIR reads yahoo-<2303.TW>.xml, gnews-<code>.xml,
t05sr01_1-<YYYY-MM-DD>.html, t05st01-<code>-<ROC year>.html, mis*.json and
chart-<2303.TW>.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
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
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twmarket as tw  # noqa: E402

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=TW&lang=zh-Hant-TW"
GNEWS = "https://news.google.com/rss/search?q=%s&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
MOPS_TODAY = "https://mopsov.twse.com.tw/mops/web/t05sr01_1"
MOPS_HISTORY = "https://mopsov.twse.com.tw/mops/web/ajax_t05st01"
MOPS_PAGE = "https://mopsov.twse.com.tw/mops/web/t05st01"
MOPS_FORM = ("encodeURIComponent=1&step=1&firstin=1&off=1&queryName=co_id&inpuType=co_id"
             "&TYPEK=all&co_id=%s&year=%d&month=all")
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) day-trader-skill-tw/1.0"
UTC = _dt.timezone.utc
KEEP_DAYS = 30
MOVE_PCT = 3.0      # a stock this far from yesterday's close is flagged
MOPS_MAX = 10       # companies whose yearly MOPS list is read per check
PAUSE = 0.3         # seconds between requests to one host
MOPS_PAUSE = 2.0    # MOPS answers "FOR SECURITY REASONS" to bursts
MIS_BATCH = 40      # channels per MIS request
DEDUPE_HOURS = 36   # the same title this close in time is one story
RANK = {"mops": 0, "yahoo": 1, "gnews": 2}   # who keeps a story told by several sources

# Tags from the title (the summary of a Chinese story names too many other things).
KEYWORDS = [
    ("財報", r"財報|財務報告|季報|年報|每股盈餘|EPS|獲利|毛利率|財測"),
    ("營收", r"營收|營業收入"),
    ("法說會", r"法說|說明會|座談會"),
    ("評等目標價", r"目標價|評等|升評|降評|上修|下修"),
    ("法律", r"訴訟|起訴|判決|法院|檢調|搜索|約談|裁罰|罰款|侵權|專利"),
    ("併購", r"併購|收購|合併(?!財務|營收|報表|損益)|公開收購|入股|策略聯盟"),
    ("股利／庫藏股／增減資", r"股利|配息|除息|除權|殖利率|庫藏股|買回本公司股份|增資|減資|公司債|可轉債|私募|籌資"),
    ("人事", r"董事長|總經理|執行長|財務長|發言人|人事|異動|辭任|請辭|接任|解任"),
    ("董事會", r"董事會"),
    # 資安 alone is the 資安股 sector as often as an attack: "資安爆發…強攻漲停" (2026-11-03)
    ("風險", r"停工|火災|爆炸|地震|斷電|停電|違約|跳票|重整|破產|下市|跌停|罷工|資安事件|網路攻擊|駭客|勒索|召回"),
    ("處置／注意股", r"處置|注意股|警示股|全額交割|分盤"),
    ("暫停交易", r"暫停交易|停止買賣|暫停買賣|終止上市"),
    ("澄清報導", r"澄清|說明.{0,20}報導"),
    ("法人買賣超", r"外資|投信|自營商|三大法人|買超|賣超"),
    ("ADR", r"ADR"),
    ("總經", r"Fed|聯準會|FOMC|CPI|通膨|央行|升息|降息|關稅|匯率|非農|GDP|PMI"),
]
# A MOPS announcement with one of these tags is marked "!".
IMPORTANT_TAGS = {"財報", "澄清報導", "暫停交易", "風險", "處置／注意股"}
# Short names that are also everyday words: they count as a mention only with the code.
COMMON_NAMES = {"世界", "中華", "台灣", "大同", "統一", "國泰", "富邦", "元大", "永豐", "華南", "第一",
                "兆豐", "中信", "新光", "台新", "凱基", "群益", "全國", "大眾", "聯合", "亞洲", "光明",
                "中國", "美國", "日本", "南亞", "台塑", "東元"}


class NewsError(Exception):
    pass


class Missing(NewsError):
    """An offline file that is not there: that source is quiet, not broken."""


# ---------------------------------------------------------------- time

def aware(naive_utc):
    return naive_utc.replace(tzinfo=UTC)


def taipei(t):
    """Aware UTC -> naive Taipei wall-clock time."""
    return tw.to_taipei(t.astimezone(UTC).replace(tzinfo=None))


def stamp(t):
    return taipei(t).strftime("%Y-%m-%d %H:%M")


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def roc_time(day, clock):
    """'115/10/28', '17:06:04' (Taipei) -> aware UTC, or None."""
    try:
        d = tw.roc_date(day)
        h, m, s = (int(x) for x in clock.split(":"))
    except (ValueError, AttributeError):
        return None
    return aware(tw.taipei_to_utc(_dt.datetime.combine(d, _dt.time(h, m, s))))


# ---------------------------------------------------------------- your stocks

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _add(watch, sym, name=None, market=None, skipped=None):
    try:
        code, suffix_market = tw.parse_symbol(str(sym))
    except ValueError:
        if skipped is not None:
            skipped.append(str(sym))
        return None
    w = watch.setdefault(code, {"name": None, "market": None})
    w["name"] = w["name"] or (name or None)
    w["market"] = w["market"] or (market if market in tw.MARKETS else None) or suffix_market
    return code


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json", skipped=None):
    """{code: {"name", "market"}}: the planned trade, your holdings, the scanner's hits."""
    out = {}
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        _add(out, pl["symbol"], pl.get("name"), pl.get("market"), skipped)
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("symbol") or p.get("ticker")
        if sym:
            _add(out, sym, p.get("name"), p.get("market"), skipped)
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            _add(out, sym, h.get("name"), h.get("market"), skipped)
    return out


def fill_from_universe(watch, universe):
    for code, w in watch.items():
        u = universe.get(code)
        if u:
            w["name"] = w["name"] or u.get("name") or None
            w["market"] = w["market"] or u.get("market") or None
    return watch


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline
        self._last = {}

    def read(self, name):
        path = os.path.join(self.offline, name)
        for p in (path, path + ".gz"):
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    data = fh.read()
                return gzip.decompress(data) if p.endswith(".gz") else data
        raise Missing("離線資料夾裡沒有 %s" % name)

    def get(self, url, name, form=None):
        if self.offline:
            return self.read(name)
        host = urllib.parse.urlsplit(url).netloc
        pause = MOPS_PAUSE if "mops" in host else PAUSE
        gap = time.time() - self._last.get(host, 0.0)
        if gap < pause:
            time.sleep(pause - gap)
        headers = {"User-Agent": BROWSER_UA if "mops" in host else tw.UA, "Accept-Encoding": "gzip"}
        body = form.encode("utf-8") if form else None
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with tw.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise NewsError("連不上（%s）" % getattr(exc, "reason", exc))
        finally:
            self._last[host] = time.time()


def offline_files(fetcher, prefix, suffix):
    if not fetcher.offline or not os.path.isdir(fetcher.offline):
        return []
    return sorted(f for f in os.listdir(fetcher.offline)
                  if f.startswith(prefix) and (f.endswith(suffix) or f.endswith(suffix + ".gz")))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def clean(text):
    """HTML fragment -> one line of plain text (Kangxi radicals as the usual characters)."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    s = "".join(unicodedata.normalize("NFKC", c) if 0x2F00 <= ord(c) <= 0x2FDF else c for c in s)
    s = re.sub(r"\s*\n\s*", "", s.replace("\r", ""))
    return re.sub(r"[ \t　\xa0]+", " ", s).strip()


def _xml(data, what):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s不是有效的 XML（%s）" % (what, exc))


def parse_rss(data, code):
    """Yahoo headline RSS -> items."""
    out = []
    for it in _xml(data, "Yahoo 新聞").iter("item"):
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "yahoo:" + guid, "source": "Yahoo股市", "kind": "headline", "code": code,
                    "title": clean(_text(it, "title")), "summary": clean(_text(it, "description"))[:200],
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_gnews(data, code):
    """Google News RSS search -> items. The outlet is <source>; the title loses its " - outlet" tail."""
    out = []
    for it in _xml(data, "Google 新聞").iter("item"):
        outlet = _text(it, "source") or "Google 新聞"
        title = clean(_text(it, "title"))
        tail = " - " + outlet
        if title.endswith(tail):
            title = title[:-len(tail)].strip()
        link = _text(it, "link")
        out.append({"id": "gnews:" + (_text(it, "guid") or link), "source": outlet, "via": "Google 新聞",
                    "kind": "headline", "code": code, "title": title, "summary": "", "link": link,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_SEQ = re.compile(r"seq_no\.value='(\d+)'", re.I)


def parse_mops(data, codes=None):
    """A MOPS 重大訊息 list (a company's year, ajax_t05st01, or today's, t05sr01_1) -> items.
    codes: keep only these companies (None keeps every row)."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    if "FOR SECURITY REASONS" in text.upper():
        raise NewsError("公開資訊觀測站暫時擋下查詢（太頻繁），稍後再試")
    out = []
    for row in _ROW.findall(text):
        cells = [clean(c) for c in _TD.findall(row)]
        if len(cells) < 5 or not re.match(r"^\d{2,3}/\d{1,2}/\d{1,2}$", cells[2]):
            continue
        code, name, day, clock, subject = cells[:5]
        if codes is not None and code not in codes:
            continue
        when = roc_time(day, clock)
        seq = _SEQ.search(row)
        key = "%s-%s-%s-%s" % (code, day.replace("/", ""), clock.replace(":", ""), seq.group(1) if seq else "1")
        out.append({"id": "mops:" + key, "source": "公開資訊觀測站", "kind": "mops", "code": code,
                    "title": "%s %s" % (name, subject),
                    "summary": "重大訊息：在公開資訊觀測站查 %s，%s %s%s" % (
                        code, day, clock, ("（第 %s 則）" % seq.group(1)) if seq else ""),
                    "link": MOPS_PAGE, "time": when})
    return out


def granularity(text):
    m = re.match(r"^(\d+)([mhd])$", str(text or ""))
    if not m:
        return 300
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_chart(data, code, now=None):
    """Yahoo chart JSON -> a move. With now, the price is the last bar that had closed by then."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        res = doc["chart"]["result"][0]
        meta = res["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("Yahoo 走勢圖不能用")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not prev:
        raise NewsError("Yahoo 走勢圖沒有昨收")
    price, when = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if now is not None and (not when or _dt.datetime.fromtimestamp(when, UTC) > now):
        price = when = None
        step = granularity(meta.get("dataGranularity"))
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        cut = (now - _dt.datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
        for t, c in zip(res.get("timestamp") or [], closes):
            if c is not None and t + step <= cut:
                price, when = c, t + step
    t = _dt.datetime.fromtimestamp(when, UTC) if when else None
    up, down = tw.price_limits(prev, code)
    return make_move(code, price and float(tw.to_tick(price, code)), float(prev), t,
                     float(up), float(down), "Yahoo %s K" % ({300: "5 分", 60: "1 分"}.get(
                         granularity(meta.get("dataGranularity")), "")))


def make_move(code, price, prev, when, limit_up, limit_down, source):
    m = {"code": code, "price": price, "prev_close": prev, "pct": None, "time": when,
         "limit_up": limit_up, "limit_down": limit_down, "limit": None, "source": source}
    if price:
        m["pct"] = round((price / prev - 1) * 100, 2)
        if limit_up and price >= limit_up - 1e-9:
            m["limit"] = "up"
        elif limit_down and price <= limit_down + 1e-9:
            m["limit"] = "down"
    return m


def quote_time(q):
    """A MIS quote's own time (d YYYYMMDD, t HH:MM:SS, Taipei) -> aware UTC, or None."""
    try:
        d = _dt.datetime.strptime("%s %s" % (q.get("date"), q.get("time")), "%Y%m%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return aware(tw.taipei_to_utc(d))


def move_from_quote(code, q, now):
    """A MIS quote -> a move, or None when it has no price or is from after now."""
    when = quote_time(q)
    if not q.get("prev_close") or not q.get("last") or when is None or when > now:
        return None
    return make_move(code, q["last"], q["prev_close"], when, q.get("limit_up"), q.get("limit_down"),
                     "證交所 MIS")


# ---------------------------------------------------------------- collecting

def collect_quotes(fetcher, watch, errors):
    """MIS quotes for every watched code. They also tell the market and the name of a code
    that no file knew; a code with an unknown market is asked on both markets."""
    pairs = []
    for code, w in watch.items():
        pairs += [(code, w["market"])] if w["market"] else [(code, "TWSE"), (code, "TPEx")]
    quotes = {}
    if fetcher.offline:
        for f in offline_files(fetcher, "mis", ".json"):
            try:
                quotes.update(tw.parse_mis(fetcher.read(f).decode("utf-8")))
            except ValueError:
                errors.append("%s 不是有效的 MIS 報價" % f)
    else:
        for i in range(0, len(pairs), MIS_BATCH):
            chans = "|".join(tw.mis_channel(c, m) for c, m in pairs[i:i + MIS_BATCH])
            try:
                quotes.update(tw.parse_mis(fetcher.get(tw.MIS_URL % chans, "mis.json").decode("utf-8")))
            except (NewsError, ValueError) as exc:
                errors.append("證交所 MIS 報價：%s" % exc)
    quotes = {c: q for c, q in quotes.items() if c in watch}
    for code, q in quotes.items():
        watch[code]["market"] = watch[code]["market"] or q.get("market")
        watch[code]["name"] = watch[code]["name"] or q.get("name") or None
    return quotes


def collect_moves(fetcher, watch, quotes, now, errors):
    out = []
    for code, w in watch.items():
        m = move_from_quote(code, quotes[code], now) if code in quotes else None
        if m is None:
            sym = tw.yahoo_symbol(code, w["market"])
            try:
                try:
                    data = fetcher.get(CHART % ("query2", sym), "chart-%s.json" % sym)
                except NewsError as exc:
                    if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                        raise
                    data = fetcher.get(CHART % ("query1", sym), "chart-%s.json" % sym)
                m = parse_chart(data, code, now)
            except Missing:
                continue
            except NewsError as exc:
                errors.append("報價 %s：%s" % (code, exc))
                continue
        m["name"], m["market"] = w["name"], w["market"]
        out.append(m)
    return out


def collect(fetcher, watch, now, hours, yahoo=True, google=True, mops=True, mops_max=MOPS_MAX, errors=None):
    """Every item from every source, MOPS first (the official word wins a dedupe)."""
    errors = [] if errors is None else errors
    items = []
    if mops:
        today = taipei(now).date()
        try:
            data = fetcher.get(MOPS_TODAY, "t05sr01_1-%s.html" % today.isoformat())
            items += parse_mops(data, set(watch))
        except Missing:
            pass
        except NewsError as exc:
            errors.append("公開資訊觀測站即時重大訊息：%s" % exc)
        start = taipei(now - _dt.timedelta(hours=hours)).date()
        if start < today:
            years = sorted({start.year - 1911, today.year - 1911})
            codes = list(watch)
            for code in codes[:mops_max]:
                for y in years:
                    try:
                        data = fetcher.get(MOPS_HISTORY, "t05st01-%s-%d.html" % (code, y), MOPS_FORM % (code, y))
                        items += parse_mops(data, {code})
                    except Missing:
                        pass
                    except NewsError as exc:
                        errors.append("公開資訊觀測站 %s 的重大訊息：%s" % (code, exc))
            if len(codes) > mops_max:
                errors.append("公開資訊觀測站的歷史重大訊息只查了前 %d 檔（--mops-max）；其餘 %d 檔只看今天的即時重大訊息"
                              % (mops_max, len(codes) - mops_max))
    for code, w in watch.items():
        if yahoo:
            sym = tw.yahoo_symbol(code, w["market"])
            try:
                items += parse_rss(fetcher.get(YAHOO % sym, "yahoo-%s.xml" % sym), code)
            except Missing:
                pass
            except NewsError as exc:
                errors.append("Yahoo 新聞 %s：%s" % (code, exc))
        if google:
            q = urllib.parse.quote_plus(("%s %s" % (w["name"] or "", code)).strip())
            try:
                items += parse_gnews(fetcher.get(GNEWS % q, "gnews-%s.xml" % code), code)
            except Missing:
                pass
            except NewsError as exc:
                errors.append("Google 新聞 %s：%s" % (code, exc))
    return items


# ---------------------------------------------------------------- tags, dedupe, state

def mention_patterns(watch):
    pats = {}
    for code, w in watch.items():
        alts = [r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(code)]
        name = w.get("name") or ""
        if len(name) >= 2 and name not in COMMON_NAMES:
            alts.append(re.escape(name))
        pats[code] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your stocks each item names, and why it matters (from the title)."""
    pats = mention_patterns(watch)
    for it in items:
        touches = {it["code"]} if it["code"] in watch else set()
        for code, rx in pats.items():
            if rx.search(it["title"]):
                touches.add(code)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, it["title"], re.I)]
        it["important"] = it["kind"] == "mops" and bool(IMPORTANT_TAGS & set(it["tags"]))
    return items


def lookalikes(watch, universe):
    """{code: [(code, name)]}: listed stocks whose name starts with the same two characters as a
    watched one (台達電 2308 and 台達化 1309). Yahoo's per-stock feed searches by name and mixes
    them in: on 2026-11-02, 6 of the 17 items in 2308's feed were about 台達化."""
    out = {}
    for code, w in watch.items():
        name = w.get("name") or ""
        if len(name) < 2:
            continue
        sims = sorted((c, u["name"]) for c, u in universe.items()
                      if c != code and len(u.get("name") or "") >= 2 and u["name"][:2] == name[:2]
                      and name not in u["name"] and u["name"] not in name)
        if sims:
            out[code] = sims
    return out


def drop_lookalikes(items, watch, universe):
    """Leave out a headline that names a look-alike stock and never names the stock it was fetched
    for. Returns (kept, dropped); a dropped item carries "lookalike": (code, name)."""
    sims = lookalikes(watch, universe)
    pats = mention_patterns(watch)
    kept, dropped = [], []
    for it in items:
        other = None
        if it["kind"] == "headline" and it["code"] in sims and not pats[it["code"]].search(it["title"]):
            other = next(((c, n) for c, n in sims[it["code"]] if n in it["title"]), None)
        if other:
            it["lookalike"] = other
            dropped.append(it)
        else:
            kept.append(it)
    return kept, dropped


def lookalike_note(dropped, watch):
    pairs = {}
    for it in dropped:
        key = (it["code"], it["lookalike"])
        pairs[key] = pairs.get(key, 0) + 1
    parts = ["%s %s 的新聞裡有 %d 則其實在講%s %s" % (watch[c]["name"] or "", c, n, o[1], o[0])
             for (c, o), n in sorted(pairs.items())]
    return "略過名稱相近的股票的新聞：%s。" % "；".join(parts)


def _norm(title):
    return re.sub(r"[\W_]+", "", (title or "").lower())


def _same_story(a, b):
    if a["kind"] == "mops" and b["kind"] == "mops":
        return False          # two filings with the same subject are two filings
    if a["time"] and b["time"] and abs((a["time"] - b["time"]).total_seconds()) > DEDUPE_HOURS * 3600:
        return False
    x, y = _norm(a["title"]), _norm(b["title"])
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    return short == long_ or (len(short) >= 12 and long_.startswith(short))


def dedupe(items):
    """One item per story: the same id or link, or the same title (one may carry an outlet tail)
    within 36 hours. MOPS wins, then Yahoo, then Google, then the earliest; the others go in "also"."""
    out, by_key = [], {}
    late = _dt.datetime.max.replace(tzinfo=UTC)
    for it in sorted(items, key=lambda i: (RANK.get(i["id"].split(":", 1)[0], 3), i["time"] or late)):
        hit = next((by_key[k] for k in (it["id"], it["link"]) if k and k in by_key and k != MOPS_PAGE), None)
        if hit is None:
            hit = next((o for o in out if _same_story(o, it)), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            if it["source"] != hit["source"] and it["source"] not in hit.setdefault("also", []):
                hit["also"].append(it["source"])
            continue
        it.setdefault("also", [])
        for k in (it["id"], it["link"]):
            if k and k != MOPS_PAGE:
                by_key[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


def select(items, state, now, hours=48, show_all=False, ahead_minutes=10):
    """Items within the window, newest first; only unseen ones unless show_all.
    ahead_minutes is 0 for a pinned --now: nothing from after it."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=ahead_minutes)
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        it["new"] = fingerprint(it) not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now, ahead_minutes=10):
    """Mark what was collected as seen (never what lies after now) and forget after 30 days."""
    seen = state.setdefault("seen", {})
    mark = now.isoformat()
    ahead = now + _dt.timedelta(minutes=ahead_minutes)
    for it in items:
        if it["time"] is None or it["time"] <= ahead:
            seen.setdefault(fingerprint(it), mark)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = mark


# ---------------------------------------------------------------- output

def render_moves(moves, now, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        label = "%s %s" % (m["code"], m.get("name") or "")
        if not m["price"]:
            out.append("%s  今天還沒有成交，昨收 %s（%s）" % (label, tw.fmt_price(m["prev_close"], m["code"]),
                                                       m["source"]))
            continue
        when = taipei(m["time"]) if m["time"] else None
        at = ""
        if when:
            at = when.strftime("%H:%M") if when.date() == taipei(now).date() else when.strftime("%m-%d %H:%M")
        flag = ""
        if m["limit"] == "up":
            flag = "  漲停"
        elif m["limit"] == "down":
            flag = "  跌停"
        elif m["pct"] >= move_pct:
            flag = "  大漲"
        elif m["pct"] <= -move_pct:
            flag = "  大跌"
        out.append("%s  %s  %+.2f%%  昨收 %s  %s（%s）%s" % (
            label, tw.fmt_price(m["price"], m["code"]), m["pct"], tw.fmt_price(m["prev_close"], m["code"]),
            at, m["source"], flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], now, move_pct)
    if out:
        out.append("")
    if not items:
        out.append("沒有新消息。")
    for it in items:
        when = stamp(it["time"]) if it["time"] else "時間不明"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["code"]
        src = it["source"] + (("（也見 %s）" % "、".join(it["also"])) if it.get("also") else "")
        out.append("%s %s  %s  %s  %s" % (mark, when, who, src, it["title"]))
        extra = []
        if it["tags"]:
            extra.append("標籤：" + "、".join(it["tags"]))
        if it["kind"] == "mops":
            extra.append(it["summary"])
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("附註：%s" % e)
    out.append("檢查時間 %s（台北）。標題只是線索，不是事實：行動前先打開原文；公開資訊觀測站的重大訊息才是公司的正式公告。"
               "這是教學與工具，不構成投資建議。" % stamp(now))
    return "\n".join(out) + "\n"


def _json_ready(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["time"] = r["time"].isoformat() if r.get("time") else None
        d["time_taipei"] = stamp(r["time"]) if r.get("time") else None
        out.append(d)
    return out


# ---------------------------------------------------------------- main

def build_watch(args, notes, universe):
    skipped = []
    known = watch_from_files(args.portfolio, args.watchlist, args.plan, skipped)
    if args.symbols:
        watch = {}
        for s in args.symbols:
            if _add(watch, s) is None:
                raise NewsError("%s 不是台股代號（例如 2303、2303.TW、6488.TWO）" % s)
        for code, w in watch.items():
            k = known.get(code) or {}
            w["name"] = w["name"] or k.get("name")
            w["market"] = w["market"] or k.get("market")
    else:
        watch = known
        if skipped:
            notes.append("略過不是台股代號的：%s" % "、".join(skipped))
    return fill_from_universe(watch, universe)


def main(argv=None):
    tw.utf8_stdio()
    ap = argparse.ArgumentParser(description="台股新聞與重大訊息（trader-news）。")
    ap.add_argument("--symbols", "--tickers", nargs="*", help="代號，例如 2303 2330 6488.TWO（預設：計畫、持股、掃盤清單）")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="代號、名稱、上市或上櫃（trader-scanner 的清單）")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="只看這麼多小時內的消息（預設 48）")
    ap.add_argument("--all", action="store_true", help="看過的也列出來")
    ap.add_argument("--no-yahoo", action="store_true", help="不讀 Yahoo 新聞")
    ap.add_argument("--no-google", action="store_true", help="不讀 Google 新聞")
    ap.add_argument("--no-mops", action="store_true", help="不讀公開資訊觀測站的重大訊息")
    ap.add_argument("--mops-max", type=int, default=MOPS_MAX, help="最多查幾檔的歷史重大訊息（預設 10）")
    ap.add_argument("--no-moves", action="store_true", help="不印每檔的漲跌")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="漲跌超過幾 %% 標成大漲／大跌（預設 3）")
    ap.add_argument("--offline", metavar="DIR", help="從資料夾讀存下來的來源，不連網")
    ap.add_argument("--now", help="假裝現在是這個台北時間，例如 \"2026-11-02 09:20\"")
    ap.add_argument("--dry-run", action="store_true", help="不更新看過的紀錄")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    notes = []
    try:
        now = aware(tw.parse_now(args.now))
        universe = tw.load_universe(args.universe)
        watch = build_watch(args, notes, universe)
    except (NewsError, ValueError) as exc:
        print("news.py：%s" % exc, file=sys.stderr)
        return 2
    if not watch:
        print("news.py：沒有要看的股票。用 --symbols 2303 2330，或先跑 /trader-scanner（它會寫 "
              "trader/watchlist.json）。", file=sys.stderr)
        return 2
    ahead = 0 if args.now else 10
    fetcher = Fetcher(args.offline)
    quotes = {}
    if not args.no_moves or any(not w["market"] for w in watch.values()):
        quotes = collect_quotes(fetcher, watch, notes)
    for code, w in watch.items():
        if not w["market"]:
            w["market"] = "TWSE"
            notes.append("不確定 %s 是上市還是上櫃，先當上市（.TW）查新聞" % code)
    state = load_json(args.state) or {}
    items = collect(fetcher, watch, now, args.hours, not args.no_yahoo, not args.no_google,
                    not args.no_mops, args.mops_max, notes)
    items, dropped = drop_lookalikes(items, watch, universe)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, args.hours, args.all, ahead)
    # count only the look-alikes this check would have shown, and remember them like the rest:
    # 2026-11-03 08:38 with --hours 16 still counted the 6 skipped the night before
    skipped = select(dropped, state, now, args.hours, args.all, ahead)
    if skipped:
        notes.append(lookalike_note(skipped, watch))
    moves = [] if args.no_moves else collect_moves(fetcher, watch, quotes, now, notes)
    if args.json:
        mv = []
        for m in _json_ready(moves):
            m["big_move"] = m["pct"] is not None and abs(m["pct"]) >= args.move_pct
            mv.append(m)
        print(json.dumps({"checked": now.isoformat(), "checked_taipei": stamp(now), "watch": watch,
                          "moves": mv, "items": _json_ready(shown), "notes": notes},
                         ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render(shown, notes, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items + dropped, state, now, ahead)
        tw.write_json(args.state, state)
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
