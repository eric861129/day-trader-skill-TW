import datetime
import filecmp
import os
import tempfile
import unittest
from decimal import Decimal

from _load import FIX, ROOT, load
from sync_twmarket import users

tw = load("trader-scanner", "twmarket")
D = os.path.join(FIX, "twmarket")


def fixture(name):
    with open(os.path.join(D, name), encoding="utf-8") as fh:
        return fh.read()


class Copies(unittest.TestCase):
    def test_every_copy_is_identical(self):
        ref = os.path.join(ROOT, "trader-scanner", "twmarket.py")
        copies = [os.path.join(ROOT, d, "twmarket.py") for d in os.listdir(ROOT)
                  if os.path.isfile(os.path.join(ROOT, d, "twmarket.py"))]
        self.assertGreaterEqual(len(copies), 1)
        for c in copies:
            self.assertTrue(filecmp.cmp(ref, c, shallow=False),
                            "%s differs: python tests/sync_twmarket.py trader-scanner/twmarket.py" % c)

    def test_every_tool_that_imports_it_has_a_copy(self):
        for d in users():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, d, "twmarket.py")), d)


class Ticks(unittest.TestCase):
    def test_stock_bands(self):
        for price, step in ((9.99, "0.01"), (10, "0.05"), (49.95, "0.05"), (50, "0.1"), (99.9, "0.1"),
                            (100, "0.5"), (499.5, "0.5"), (500, "1"), (999, "1"), (1000, "5"), (2330, "5")):
            self.assertEqual(tw.tick_size(price), Decimal(step), price)

    def test_etf_bands(self):
        self.assertEqual(tw.tick_size(49.99, "00878"), Decimal("0.01"))
        self.assertEqual(tw.tick_size(104.1, "0050"), Decimal("0.05"))
        self.assertEqual(tw.tick_size(250, "006208"), Decimal("0.05"))

    def test_snapping(self):
        self.assertEqual(tw.to_tick(358.325, "2330", "down"), Decimal("358.0"))
        self.assertEqual(tw.to_tick(358.325, "2330", "up"), Decimal("358.5"))
        self.assertEqual(tw.to_tick(49.97, "", "up"), Decimal("50.00"))   # crosses into the 0.1 band, still valid
        self.assertEqual(tw.to_tick(100.3, "", "down"), Decimal("100.0"))
        self.assertEqual(tw.to_tick(2343, "", "nearest"), Decimal("2345"))
        self.assertTrue(tw.on_tick(2330))
        self.assertFalse(tw.on_tick(2332))
        self.assertTrue(tw.on_tick(104.15, "0050"))
        self.assertFalse(tw.on_tick(104.12, "0050"))

    def test_tick_steps_cross_bands(self):
        self.assertEqual(tw.tick_steps(99.8, 101), 4)      # 99.8, 99.9, 100.0, 100.5, 101.0
        self.assertEqual(tw.tick_steps(2330, 2345), 3)
        self.assertEqual(tw.tick_steps(2345, 2330), -3)

    def test_price_limits_match_mis(self):
        quotes = tw.parse_mis(fixture("mis-2026-11-02.json"))
        self.assertEqual(sorted(quotes), ["0050", "00631L", "2330", "6488"])
        for code, q in quotes.items():
            up, down = tw.price_limits(q["prev_close"], code)
            self.assertEqual(float(up), q["limit_up"], code)
            self.assertEqual(float(down), q["limit_down"], code)
        self.assertEqual(tw.limit_pct("00631L"), 20)
        self.assertEqual(tw.limit_pct("2330"), 10)

    def test_format(self):
        self.assertEqual(tw.fmt_price(2330), "2,330")
        self.assertEqual(tw.fmt_price(355.5), "355.5")
        self.assertEqual(tw.fmt_price(62.3), "62.3")
        self.assertEqual(tw.fmt_price(45.25), "45.25")
        self.assertEqual(tw.fmt_price(104.1, "0050"), "104.10")
        self.assertEqual(tw.twd(2913.44), "NT$2,913")
        self.assertEqual(tw.twd(-46.88), "NT$-47")


class Calendar(unittest.TestCase):
    def test_both_shapes_give_the_builtin_list(self):
        weekdays = lambda c: {d: n for d, n in c.items()
                              if datetime.date.fromisoformat(d).weekday() < 5}
        self.assertEqual(weekdays(tw.parse_calendar(fixture("rwd-holidaySchedule-2026.json"))), tw.TWSE_CLOSED)
        self.assertEqual(weekdays(tw.parse_calendar(fixture("openapi-holidaySchedule-2026.json"))), tw.TWSE_CLOSED)

    def test_trading_markers_are_not_holidays(self):
        cal = tw.Calendar()
        for d in ("2026-01-02", "2026-02-11", "2026-02-23", "2026-11-02"):
            self.assertTrue(cal.is_trading_day(datetime.date.fromisoformat(d)), d)
        self.assertEqual(cal.closed(datetime.date(2026, 2, 12)), "市場無交易，僅辦理結算交割作業")
        self.assertEqual(cal.closed(datetime.date(2026, 11, 1)), "週末")

    def test_typhoon_days_are_closed(self):
        cal = tw.Calendar()
        self.assertIn("薔琵颱風", cal.closed(datetime.date(2026, 10, 23)))
        self.assertIn("巴威颱風", cal.closed(datetime.date(2026, 7, 10)))
        # 10-23 typhoon, 10-24/25 weekend, 10-26 光復節 observed: Thursday to Tuesday
        self.assertEqual(cal.next_trading_day(datetime.date(2026, 10, 22)), datetime.date(2026, 10, 27))

    def test_next_and_previous(self):
        cal = tw.Calendar()
        self.assertEqual(cal.next_trading_day(datetime.date(2026, 2, 11)), datetime.date(2026, 2, 23))
        self.assertEqual(cal.previous_trading_day(datetime.date(2026, 11, 2)), datetime.date(2026, 10, 30))

    def test_unpublished_year(self):
        with self.assertRaises(ValueError):
            tw.fetch_calendar(2027, get=lambda url: fixture("rwd-holidaySchedule-2027.json"))
        cal = tw.Calendar()
        self.assertFalse(cal.known(2027))
        self.assertIn("2027", cal.unknown_note(datetime.date(2027, 1, 4)))
        self.assertEqual(cal.unknown_note(datetime.date(2026, 11, 2)), "")

    def test_cache_and_typhoon_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            closed = tw.fetch_calendar(2026, get=lambda url: fixture("rwd-holidaySchedule-2026.json"))
            tw.save_calendar(2027, {"2027-01-01": "中華民國開國紀念日"}, tmp)
            cal = tw.load_calendar(tmp, {"extra_closed": {"2026-07-28": "颱風停止交易"}})
            self.assertTrue(cal.known(2027))
            self.assertFalse(cal.is_trading_day(datetime.date(2027, 1, 1)))
            self.assertEqual(cal.closed(datetime.date(2026, 7, 28)), "颱風停止交易")
            self.assertIn("2026-02-12", closed)


class Clock(unittest.TestCase):
    def test_taipei_is_utc_plus_8_all_year(self):
        self.assertEqual(tw.parse_now("2026-11-02 09:30"), datetime.datetime(2026, 11, 2, 1, 30))
        self.assertEqual(tw.parse_now("2026-07-01 09:30"), datetime.datetime(2026, 7, 1, 1, 30))
        self.assertEqual(tw.parse_now("2026-11-02T01:30:00Z"), datetime.datetime(2026, 11, 2, 1, 30))
        self.assertEqual(tw.to_taipei(datetime.datetime(2026, 11, 2, 1, 30)), datetime.datetime(2026, 11, 2, 9, 30))
        ts = tw.taipei_to_ts(datetime.datetime(2026, 11, 2, 9, 0))
        self.assertEqual(tw.ts_to_taipei(ts), datetime.datetime(2026, 11, 2, 9, 0))
        with self.assertRaises(ValueError):
            tw.parse_now("tomorrow")

    def test_labels(self):
        self.assertEqual(tw.day_label(datetime.date(2026, 11, 2)), "2026-11-02（一）")
        self.assertEqual(tw.hm(datetime.time(9, 5)), "09:05")
        self.assertEqual(tw.roc_date("1151102"), datetime.date(2026, 11, 2))
        self.assertEqual(tw.roc_date("115/10/28"), datetime.date(2026, 10, 28))

    def test_session_shape(self):
        mins = lambda t: t.hour * 60 + t.minute
        self.assertEqual(mins(tw.CLOSE) - mins(tw.OPEN), tw.SESSION_MINUTES)
        self.assertEqual((mins(tw.CLOSING_AUCTION) - mins(tw.OPEN)) // 5, tw.BARS_5M)


class Symbols(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(tw.parse_symbol("2330"), ("2330", None))
        self.assertEqual(tw.parse_symbol("2330.tw"), ("2330", "TWSE"))
        self.assertEqual(tw.parse_symbol("6488.TWO"), ("6488", "TPEx"))
        self.assertEqual(tw.parse_symbol("00631L"), ("00631L", None))
        self.assertEqual(tw.parse_symbol("006208.TW"), ("006208", "TWSE"))
        for bad in ("TSLA", "2330A", "030001", "123", ""):
            with self.assertRaises(ValueError):
                tw.parse_symbol(bad)
        self.assertEqual(tw.yahoo_symbol("6488", "TPEx"), "6488.TWO")
        self.assertEqual(tw.yahoo_symbol("2330", None), "2330.TW")

    def test_universe_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "universe.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("2330|台積電|TWSE|股票\n6488|環球晶|TPEx|股票\n")
            u = tw.load_universe(p)
            self.assertEqual(u["6488"], {"name": "環球晶", "market": "TPEx", "kind": "股票"})
        self.assertEqual(tw.load_universe(os.path.join(D, "missing.txt")), {})

    def test_lots(self):
        self.assertEqual(tw.split_lots(2350), (2, 350))
        self.assertEqual(tw.fmt_shares(2000), "2 張")
        self.assertEqual(tw.fmt_shares(350), "350 股")
        self.assertEqual(tw.fmt_shares(2350), "2 張又 350 股")


class Costs(unittest.TestCase):
    def test_fee(self):
        self.assertEqual(tw.broker_fee(2330000), 3320)           # 0.1425 %, cut to the dollar
        self.assertEqual(tw.broker_fee(2330000, 0.6), 1992)      # 6 折
        self.assertEqual(tw.broker_fee(5000), 20)                # the minimum
        self.assertEqual(tw.broker_fee(5000, 1, 1), 7)
        self.assertEqual(tw.broker_fee(0), 0)

    def test_tax(self):
        day = datetime.date(2026, 11, 2)
        self.assertEqual(tw.sell_tax(2330000, "2330", True, day), 3495)
        self.assertEqual(tw.sell_tax(2330000, "2330", False, day), 6990)
        self.assertEqual(tw.sell_tax(104100, "0050", True, day), 104)
        self.assertEqual(tw.sell_tax_rate("2330", True, datetime.date(2028, 1, 3)), Decimal("0.003"))
        self.assertEqual(tw.sell_tax_rate("00679B", False, day), Decimal(0))

    def test_round_trip(self):
        day = datetime.date(2026, 11, 2)
        rt = tw.round_trip(2330, 2345, 1000, "2330", True, None, day)
        self.assertEqual(rt, {"buy_fee": 3320, "sell_fee": 3341, "tax": 3517, "total": 10178})
        odd = tw.round_trip(2330, 2345, 10, "2330", False, tw.fee_settings({"fee_discount": 0.6}), day)
        self.assertEqual(odd["buy_fee"], 19)                     # odd lots: NT$1 minimum by default
        self.assertEqual(odd["tax"], 70)                         # odd lots cannot be day-traded: 0.3 %
        self.assertEqual(tw.cost_rate("2330", True, None, day), Decimal("0.004350"))


if __name__ == "__main__":
    unittest.main()
