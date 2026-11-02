#!/usr/bin/env python3
"""Taiwan stock market rules for the trader skills. Standard library only, Python 3.8+.

The Taipei clock, the TWSE trading calendar, tick sizes, the daily price
limits, board lots and odd lots, the broker fee and the securities
transaction tax, symbols (.TW for TWSE, .TWO for TPEx) and the TWSE MIS quote.

The same file sits in every skill folder that uses it, so each folder still
works when it is copied on its own. tests/test_twmarket.py checks that every
copy is identical: edit one, then run
    python tests/sync_twmarket.py trader-scanner/twmarket.py

Sources, read 2026-11-02:
  calendar      https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json
                (the same list is at https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule)
  session       09:00 to 13:30; 13:25 to 13:30 is the closing call auction. Intraday odd lots
                match from 09:10, every 5 seconds since 2024-12-02, to 13:30
  tick sizes    stocks: under 10, 0.01; 10 to 50, 0.05; 50 to 100, 0.1; 100 to 500, 0.5;
                500 to 1000, 1; 1000 and up, 5. ETFs, ETNs and REITs: under 50, 0.01; 50 and up, 0.05
  price limits  10 % either side of the reference price: limit up rounded down to a tick, limit
                down rounded up. Checked against mis.twse.com.tw on 2026-11-02 for 2330, 6488,
                0050 and 00631L (a 2x leveraged ETF moves 20 %)
  tax           0.3 % of a sale; 0.15 % on a same-day (現股當沖) sale through 2027-12-31;
                0.1 % for ETFs; bond ETFs 0 through 2026-12-31
  fee           0.1425 % a side at most, NT$20 minimum on a board-lot order; brokers discount it
  orders        board lots in the continuous session: market or limit, each ROD, IOC or FOK; before
                09:00 and in the closing auction only limit ROD. Intraday odd lots: limit ROD only.
                Odd lots cannot be day-traded: one bought today can be sold the next trading day
  lists         處置股 https://openapi.twse.com.tw/v1/announcement/punish and
                https://www.tpex.org.tw/openapi/v1/tpex_disposal_information; stocks that may be
                day-traded (現股當沖標的) https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U and
                https://www.tpex.org.tw/openapi/v1/tpex_securities. On 2026-11-02 the 14 active
                處置股 were all missing from the day-trade lists
"""

import datetime
import hashlib
import json
import os
import re
import sys
import urllib.request
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

UA = "Mozilla/5.0 (compatible; day-trader-skill-tw/1.0; +https://github.com/eric861129/day-trader-skill-TW)"

# ---------------------------------------------------------------- the session

UTC_OFFSET = datetime.timedelta(hours=8)    # Taipei, no daylight saving time
PRE_OPEN = datetime.time(8, 30)             # orders accepted, nothing matches yet
OPEN = datetime.time(9, 0)
CLOSING_AUCTION = datetime.time(13, 25)     # 13:25 to 13:30: one call auction, no continuous trading
CLOSE = datetime.time(13, 30)
ODD_LOT_FIRST_MATCH = datetime.time(9, 10)
SESSION_MINUTES = 270
# 5-minute bars of continuous trading: 09:00 to 13:20 bar starts, the last closes at 13:25. Yahoo
# leaves the 13:25 auction bar empty, and only on the current day adds a 13:30 print of the close
# with no volume, so the official close is not in past days' 5-minute bars.
BARS_5M = 53
LOT = 1000                                  # one board lot (1 張) is 1,000 shares

# trader/config.json defaults the skills share (Taipei time)
NO_ENTRIES_BEFORE = "09:15"                 # after a 15-minute opening range
NO_ENTRIES_AFTER = "13:00"
FLAT_BY = "13:20"                           # five minutes before the closing auction

WEEKDAYS = "一二三四五六日"

# ---------------------------------------------------------------- the calendar

CALENDAR_URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=%d0101&response=json"
CALENDAR_YEARS = (2026,)
# Weekdays the market does not trade, from the TWSE list (weekend rows left out).
TWSE_CLOSED = {
    "2026-01-01": "中華民國開國紀念日",
    "2026-02-12": "市場無交易，僅辦理結算交割作業",
    "2026-02-13": "市場無交易，僅辦理結算交割作業",
    "2026-02-16": "農曆除夕及春節",
    "2026-02-17": "農曆除夕及春節",
    "2026-02-18": "農曆除夕及春節",
    "2026-02-19": "農曆除夕及春節",
    "2026-02-20": "農曆除夕及春節",
    "2026-02-27": "和平紀念日",
    "2026-04-03": "兒童節及民族掃墓節",
    "2026-04-06": "兒童節及民族掃墓節",
    "2026-05-01": "勞動節",
    "2026-06-19": "端午節",
    "2026-09-25": "中秋節",
    "2026-09-28": "孔子誕辰紀念日/ 教師節",
    "2026-10-09": "國慶日",
    "2026-10-26": "臺灣光復暨金門古寧頭大捷紀念日",
    "2026-12-25": "行憲紀念日",
}
# Days the TWSE list never carries: the market shuts when Taipei City calls off work for the
# whole day or the morning (a typhoon). Past ones, from the news; Yahoo's bars are empty on both.
UNPLANNED_CLOSED = {
    "2026-07-10": "巴威颱風，台北市停班，全日休市",
    "2026-10-23": "薔琵颱風，台北市停班，全日休市",
}
# Rows in the TWSE list that mark a trading day ("國曆新年開始交易日", "農曆春節前最後交易日").
_TRADING_MARKER = re.compile(r"開始交易|最後交易")


class Calendar(object):
    """Closed weekdays by ISO date. Built in: 2026, with its typhoon days. Other
    years come from the TWSE (fetch_calendar, cached in trader/state/calendar-YYYY.json).
    A new unplanned closure, like tomorrow's typhoon day, goes in trader/config.json
    as "extra_closed": ["2026-07-28"] or {"2026-07-28": "颱風停止交易"}."""

    def __init__(self):
        self.closed_days = dict(TWSE_CLOSED)
        self.closed_days.update(UNPLANNED_CLOSED)
        self.years = set(CALENDAR_YEARS)

    def add(self, closed, year=None):
        if isinstance(closed, dict):
            items = closed.items()
        else:
            items = ((d, "休市") for d in (closed or ()))
        for d, name in items:
            self.closed_days[str(d)[:10]] = name or "休市"
        if year:
            self.years.add(int(year))
        return self

    def known(self, year):
        return int(year) in self.years

    def closed(self, d):
        """Why the market is closed on date d, or None on a trading day."""
        if d.weekday() >= 5:
            return "週末"
        return self.closed_days.get(d.isoformat())

    def is_trading_day(self, d):
        return self.closed(d) is None

    def next_trading_day(self, d):
        for _ in range(40):
            d += datetime.timedelta(days=1)
            if self.is_trading_day(d):
                return d
        raise ValueError("no trading day in the 40 days after %s" % d)

    def previous_trading_day(self, d):
        for _ in range(40):
            d -= datetime.timedelta(days=1)
            if self.is_trading_day(d):
                return d
        raise ValueError("no trading day in the 40 days before %s" % d)

    def unknown_note(self, d):
        """One plain line when the holiday list for d's year is not loaded, else ''."""
        if self.known(d.year):
            return ""
        return ("%d 年的休市日不在清單裡：執行 scan.py universe 更新（證交所公布後才抓得到），"
                "或在 trader/config.json 的 extra_closed 自己加上" % d.year)


def parse_calendar(payload):
    """The TWSE calendar, either shape, -> {ISO date: name} of closed days.
    Website: {"data": [["2026-01-01", name, note], ...]}. OpenAPI: [{"Date": "1150101", "Name": ...}]."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    rows = []
    if isinstance(payload, dict):
        for r in payload.get("data") or []:
            if len(r) >= 2:
                rows.append((r[0], r[1]))
    else:
        for r in payload or []:
            rows.append((r.get("Date"), r.get("Name")))
    out = {}
    for raw, name in rows:
        name = (name or "").strip()
        if _TRADING_MARKER.search(name):
            continue
        d = roc_date(raw) if re.match(r"^\d{7}$", str(raw or "")) else datetime.date.fromisoformat(str(raw)[:10])
        out[d.isoformat()] = name or "休市"
    return out


def fetch_calendar(year, get=None):
    """Closed days for one year from the TWSE website. ValueError if it has none yet."""
    url = CALENDAR_URL % int(year)
    closed = parse_calendar((get or http_get)(url))
    if not closed:
        raise ValueError("證交所還沒公布 %d 年的開休市日期" % int(year))
    return closed


def save_calendar(year, closed, trader_dir="trader"):
    path = os.path.join(trader_dir, "state", "calendar-%d.json" % int(year))
    write_json(path, {"year": int(year), "source": CALENDAR_URL % int(year),
                      "fetched": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "closed": closed})
    return path


def load_calendar(trader_dir="trader", config=None):
    """The built-in calendar plus the cached years in trader/state and the config's extra_closed."""
    cal = Calendar()
    state = os.path.join(trader_dir, "state")
    if os.path.isdir(state):
        for f in sorted(os.listdir(state)):
            m = re.match(r"^calendar-(\d{4})\.json$", f)
            if not m:
                continue
            doc = read_json(os.path.join(state, f), {})
            if isinstance(doc, dict) and isinstance(doc.get("closed"), dict):
                cal.add(doc["closed"], int(m.group(1)))
    cal.add((config or {}).get("extra_closed"))
    return cal


# ---------------------------------------------------------------- the clock

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def to_taipei(utc):
    return utc.replace(tzinfo=None) + UTC_OFFSET


def taipei_to_utc(local):
    return local.replace(tzinfo=None) - UTC_OFFSET


def ts_to_taipei(ts):
    """Unix seconds -> naive Taipei time. Never uses the machine's own time zone."""
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)) + UTC_OFFSET


def taipei_to_ts(local):
    return int((taipei_to_utc(local) - datetime.datetime(1970, 1, 1)).total_seconds())


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is Taipei time. ValueError if unreadable."""
    if not text:
        return utc_now()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ValueError('--now 要像 "2026-11-02 09:30"（台北時間）或 "2026-11-02T01:30:00Z"（UTC）')
    if dt.tzinfo is None:
        return taipei_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("%s 要像 09:30 這樣的台北時間" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """datetime or time -> '09:30'."""
    return "%02d:%02d" % (t.hour, t.minute)


def day_label(d):
    """date -> '2026-11-02（一）'."""
    return "%s（%s）" % (d.isoformat(), WEEKDAYS[d.weekday()])


def roc_date(text):
    """A Republic of China date ('1151102', '115/11/02', '115-11-02') -> date."""
    s = str(text or "").strip()
    m = re.match(r"^(\d{2,3})[/-]?(\d{2})[/-]?(\d{2})$", s)
    if not m:
        raise ValueError("not a ROC date: %r" % text)
    return datetime.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))


# ---------------------------------------------------------------- symbols

MARKETS = {"TWSE": ".TW", "TPEx": ".TWO"}
CODE = re.compile(r"^(?:[1-9]\d{3}|00\d{2,4}[A-Z]?)$")   # 2330, 6488; ETFs 0050, 00878, 006208, 00631L


def is_etf_like(code):
    """ETFs, ETNs and REITs: their codes start with 0 and they use the two-band tick table."""
    return str(code).startswith("0")


def parse_symbol(text):
    """'2330', '2330.TW', '6488.two' -> (code, market or None). ValueError if it is not a TW code."""
    s = str(text or "").strip().upper()
    market = None
    for name, suffix in MARKETS.items():
        if s.endswith(suffix) and CODE.match(s[:-len(suffix)]):
            s, market = s[:-len(suffix)], name
            break
    if not CODE.match(s):
        raise ValueError("%s 不像台股代號（例如 2330、6488、0050、00878）" % (text,))
    return s, market


def yahoo_symbol(code, market):
    return code + MARKETS.get(market or "TWSE", ".TW")


def load_universe(path="trader/universe.txt"):
    """trader/universe.txt ('2330|台積電|TWSE|股票' a line) -> {code: {name, market, kind}}."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if not parts or not parts[0].strip():
                continue
            code = parts[0].strip()
            out[code] = {"name": parts[1].strip() if len(parts) > 1 else "",
                         "market": parts[2].strip() if len(parts) > 2 else None,
                         "kind": parts[3].strip() if len(parts) > 3 else ""}
    return out


# ---------------------------------------------------------------- ticks and limits

STOCK_TICKS = ((Decimal(10), Decimal("0.01")), (Decimal(50), Decimal("0.05")), (Decimal(100), Decimal("0.1")),
               (Decimal(500), Decimal("0.5")), (Decimal(1000), Decimal("1")), (None, Decimal("5")))
FUND_TICKS = ((Decimal(50), Decimal("0.01")), (None, Decimal("0.05")))


def dec(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _band(price, code=""):
    """(upper bound or None, tick) of the band the price sits in."""
    p = dec(price)
    if p <= 0:
        raise ValueError("a price must be above zero, not %s" % price)
    for upper, step in (FUND_TICKS if is_etf_like(code) else STOCK_TICKS):
        if upper is None or p < upper:
            return upper, step


def tick_size(price, code=""):
    return _band(price, code)[1]


def to_tick(price, code="", mode="nearest"):
    """Snap a price onto the tick grid: mode 'down', 'up' or 'nearest' (half up)."""
    p = dec(price)
    step = tick_size(p, code)
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
    return ((p / step).to_integral_value(rounding=rounding) * step).quantize(step)


def on_tick(price, code=""):
    return to_tick(price, code) == dec(price)


def tick_steps(low, high, code=""):
    """How many ticks from low up to high (both on the grid), across bands."""
    lo, hi = dec(low), dec(high)
    if hi < lo:
        return -tick_steps(hi, lo, code)
    steps = Decimal(0)
    while lo < hi:
        upper, step = _band(lo, code)
        nxt = hi if upper is None else min(hi, upper)
        steps += (nxt - lo) / step
        lo = nxt
    return int(steps.to_integral_value(rounding=ROUND_HALF_UP))


def limit_pct(code=""):
    """10 % for stocks and most ETFs; 20 % for a 2x leveraged ETF (code ending in L).
    ETFs that hold foreign stocks have no limit at all: the MIS quote says so (no u / w)."""
    return 20 if is_etf_like(code) and str(code).endswith("L") else 10


def price_limits(ref, code=""):
    """(limit up, limit down) from the reference price (usually yesterday's close)."""
    r = dec(ref)
    pct = Decimal(limit_pct(code)) / 100
    return to_tick(r * (1 + pct), code, "down"), to_tick(r * (1 - pct), code, "up")


def fmt_price(price, code=""):
    """A price with as many decimals as its tick: 2,330 / 355.5 / 104.10 / 9.87."""
    p = dec(price)
    step = tick_size(p, code) if p > 0 else Decimal("0.01")
    places = max(0, -step.normalize().as_tuple().exponent)
    return "{:,.{n}f}".format(p, n=places)


def twd(x):
    """Whole New Taiwan dollars: NT$2,913 (NT$-46 for a loss)."""
    v = int(dec(x).to_integral_value(rounding=ROUND_HALF_UP))
    return ("NT$-{:,}" if v < 0 else "NT${:,}").format(abs(v))


# ---------------------------------------------------------------- lots

def split_lots(shares):
    """Shares -> (board lots, odd shares): 2,350 -> (2, 350)."""
    n = int(shares)
    return n // LOT, n % LOT


def fmt_shares(shares):
    """2000 -> '2 張', 350 -> '350 股', 2350 -> '2 張又 350 股'."""
    lots, odd = split_lots(shares)
    if lots and odd:
        return "%d 張又 %d 股" % (lots, odd)
    if lots:
        return "%d 張" % lots
    return "%d 股" % odd


# ---------------------------------------------------------------- fees and tax

FEE_RATE = Decimal("0.001425")
FEE_MIN = 20                    # NT$ per board-lot order at most brokers
FEE_MIN_ODD = 1                 # NT$ per odd-lot order at many brokers online; check yours
TAX_RATE = Decimal("0.003")
DAY_TRADE_TAX_RATE = Decimal("0.0015")
DAY_TRADE_TAX_UNTIL = datetime.date(2027, 12, 31)
ETF_TAX_RATE = Decimal("0.001")
BOND_ETF_TAX_FREE_UNTIL = datetime.date(2026, 12, 31)


def fee_settings(config=None):
    """The broker fee terms from trader/config.json: fee_discount (0.6 is 6 折), fee_min_twd, fee_min_odd_twd."""
    c = config or {}
    return {"discount": dec(c.get("fee_discount", 1)),
            "minimum": int(c.get("fee_min_twd", FEE_MIN)),
            "odd_minimum": int(c.get("fee_min_odd_twd", FEE_MIN_ODD))}


def broker_fee(value, discount=1, minimum=FEE_MIN):
    """0.1425 % of the trade value times the discount, cut to a whole dollar, at least the minimum."""
    v = dec(value)
    if v <= 0:
        return 0
    fee = int((v * FEE_RATE * dec(discount)).to_integral_value(rounding=ROUND_FLOOR))
    return max(int(minimum), fee)


def sell_tax_rate(code="", day_trade=False, on=None):
    on = on or datetime.date.today()
    if is_etf_like(code):
        if str(code).endswith("B") and on <= BOND_ETF_TAX_FREE_UNTIL:
            return Decimal(0)
        return ETF_TAX_RATE
    if day_trade and on <= DAY_TRADE_TAX_UNTIL:
        return DAY_TRADE_TAX_RATE
    return TAX_RATE


def sell_tax(value, code="", day_trade=False, on=None):
    v = dec(value)
    if v <= 0:
        return 0
    return int((v * sell_tax_rate(code, day_trade, on)).to_integral_value(rounding=ROUND_FLOOR))


def round_trip(buy_price, sell_price, shares, code="", day_trade=True, fees=None, on=None):
    """What a buy and its sale cost in whole NT$: {buy_fee, sell_fee, tax, total}.
    fees is fee_settings(config); odd-lot orders (under 1,000 shares) use its odd_minimum."""
    f = fees or fee_settings()
    n = int(shares)
    minimum = f["minimum"] if n >= LOT else f["odd_minimum"]
    buy_fee = broker_fee(dec(buy_price) * n, f["discount"], minimum)
    sell_fee = broker_fee(dec(sell_price) * n, f["discount"], minimum)
    tax = sell_tax(dec(sell_price) * n, code, day_trade, on)
    return {"buy_fee": buy_fee, "sell_fee": sell_fee, "tax": tax, "total": buy_fee + sell_fee + tax}


def cost_rate(code="", day_trade=True, fees=None, on=None):
    """The round trip as a fraction of the price, ignoring the fee minimum: fee twice plus the tax."""
    f = fees or fee_settings()
    return FEE_RATE * f["discount"] * 2 + sell_tax_rate(code, day_trade, on)


def net_result(buy_price, sell_price, shares, code="", day_trade=True, fees=None, on=None):
    """A buy and its sale in NT$: {"move": (sell - buy) x shares, "costs": fees and tax, "net": move - costs}."""
    n = int(shares)
    move = (dec(sell_price) - dec(buy_price)) * n
    costs = round_trip(buy_price, sell_price, n, code, day_trade, fees, on)["total"]
    return {"move": float(move), "costs": costs, "net": float(move - costs)}


def loss_at_stop(entry, stop, shares, code="", day_trade=True, fees=None, on=None):
    """What a buy at entry loses if it is sold exactly at the stop, fees and tax included:
    {"move", "costs", "total"} in NT$. trader-budget sizes with it and trader-risk checks with it,
    so both get the same number. A stop can fill worse than its price; this is the loss AT the stop."""
    r = net_result(entry, stop, shares, code, day_trade, fees, on)
    return {"move": -r["move"], "costs": r["costs"], "total": -r["net"]}


# ---------------------------------------------------------------- tickets (budget -> risk -> broker)

# The fields a ticket is placed with. trader-risk records a verdict per fingerprint and
# trader-broker shows a ticket as ready only when its fingerprint passed today, so any
# change to these fields needs a new check. Notes (name, why, plan) are left out.
TICKET_FIELDS = ("symbol", "market", "side", "lot", "qty", "price_type", "price", "tif", "trade",
                 "stop", "target")
LOTS = ("board", "odd")                     # 整股, 盤中零股
PRICE_TYPES = ("market", "limit")           # 市價, 限價
TIFS = ("ROD", "IOC", "FOK")                # 當日有效, 立即成交否則取消, 全部成交否則取消
TRADES = ("day_trade", "cash")              # 現股當沖, 現股 (held overnight at least)


def fingerprint(order):
    core = {k: order[k] for k in TICKET_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def price_text(price, code=""):
    """A ticket price as plain text: the decimals of its tick, no thousands separator (110.0, 2330, 104.10)."""
    return fmt_price(price, code).replace(",", "")


# ---------------------------------------------------------------- 處置股 and the day-trade lists

PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
DAYTRADE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TPEX_DAYTRADE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_securities"

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_MATCH = re.compile(r"每\s*([0-9０-９一二三四五六七八九十]+)\s*分鐘撮合")


def _cn_int(text):
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_CN_NUM.get(tens, 1) if tens else 1) * 10 + (_CN_NUM.get(ones, 0) if ones else 0)
    return _CN_NUM.get(text)


def parse_period(text):
    """'115/10/28～115/11/03' or '1151102~1151110' -> (start, end) dates, or None."""
    parts = [p for p in re.split(r"\s*[～~]\s*", str(text or "").strip()) if p]
    if len(parts) != 2:
        return None
    try:
        return roc_date(parts[0]), roc_date(parts[1])
    except ValueError:
        return None


def parse_disposition(twse_rows=None, tpex_rows=None):
    """The two disposition lists -> {code: {name, market, start, end, every_minutes}}."""
    out = {}
    rows = [(r, "TWSE", "Code", "Name", "Detail") for r in (twse_rows or [])]
    rows += [(r, "TPEx", "SecuritiesCompanyCode", "CompanyName", "DisposalCondition") for r in (tpex_rows or [])]
    for r, market, k_code, k_name, k_text in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get(k_code) or "").strip()
        period = parse_period(r.get("DispositionPeriod"))
        if not code or not period:
            continue
        m = _MATCH.search(str(r.get(k_text) or ""))
        every = _cn_int(m.group(1)) if m else None
        old = out.get(code)
        if old:  # two dispositions of one stock: cover both
            period = (min(old["start"], period[0]), max(old["end"], period[1]))
            every = every or old["every_minutes"]
        out[code] = {"name": str(r.get(k_name) or "").strip(), "market": market, "start": period[0],
                     "end": period[1], "every_minutes": every}
    return out


def active_disposition(disp, day):
    return {c: v for c, v in (disp or {}).items() if v["start"] <= day <= v["end"]}


def parse_daytrade(rows):
    """The TWSE and TPEx lists of stocks that may be day-traded (rows of either, mixed) ->
    {"date": the list's date or None, "codes": {code: {"name", "market", "sell_first"}}}.
    sell_first is False when 先賣後買 is suspended for it (暫停現股賣出後現款買進當沖)."""
    codes, dates = {}, set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if "證券代號" in r:
            code, name, flag, day, market = (r.get("證券代號"), r.get("證券名稱"),
                                             r.get("暫停現股賣出後現款買進當沖註記"), r.get("資料日期"), "TPEx")
        else:
            code, name, flag, day, market = r.get("Code"), r.get("Name"), r.get("Suspension"), r.get("Date"), "TWSE"
        code = str(code or "").strip()
        if not code:
            continue
        codes[code] = {"name": str(name or "").strip(), "market": market,
                       "sell_first": str(flag or "").strip().upper() != "Y"}
        if day:
            try:
                dates.add(roc_date(str(day)))
            except ValueError:
                pass
    return {"date": max(dates) if dates else None, "codes": codes}


def list_kind(rows):
    """Which list a saved JSON array is: "disposition", "daytrade" or None."""
    first = next((r for r in (rows or []) if isinstance(r, dict)), None) if isinstance(rows, list) else None
    if not first:
        return None
    if "DispositionPeriod" in first:
        return "disposition"
    if "Suspension" in first or "暫停現股賣出後現款買進當沖註記" in first:
        return "daytrade"
    return None


# ---------------------------------------------------------------- the MIS quote

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=%s&json=1&delay=0"


def mis_channel(code, market):
    return "%s_%s.tw" % ("otc" if market == "TPEx" else "tse", code)


def _num(x):
    try:
        v = float(str(x).split("_")[0])
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_mis(payload):
    """TWSE MIS getStockInfo JSON -> {code: {name, market, last, prev_close, limit_up, limit_down,
    open, high, low, volume_lots, date, time}}. A field the quote does not carry is None."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    out = {}
    for m in (payload or {}).get("msgArray") or []:
        code = m.get("c")
        if not code:
            continue
        vol = _num(m.get("v"))
        out[code] = {"name": m.get("n", ""), "market": "TPEx" if m.get("ex") == "otc" else "TWSE",
                     "last": _num(m.get("z")), "prev_close": _num(m.get("y")),
                     "limit_up": _num(m.get("u")), "limit_down": _num(m.get("w")),
                     "open": _num(m.get("o")), "high": _num(m.get("h")), "low": _num(m.get("l")),
                     "volume_lots": int(vol) if vol else 0, "date": m.get("d"), "time": m.get("t")}
    return out


def fetch_mis(pairs, get=None):
    """[(code, market), ...] -> parse_mis(...). One request for up to about 50 codes."""
    chans = "|".join(mis_channel(c, m) for c, m in pairs)
    return parse_mis((get or http_get)(MIS_URL % chans))


# ---------------------------------------------------------------- files and output

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def read_json(path, default=None):
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def utf8_stdio():
    """Print UTF-8 even when Windows hands Python a cp950 pipe."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
