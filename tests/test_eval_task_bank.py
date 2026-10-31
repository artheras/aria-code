"""Every task in the eval bank can be solved, and its trap is caught.

`--check` proves each task starts red. That does not prove the grader can be
satisfied, or that it fails the tempting half-fix the task is built around —
a grader with either flaw measures nothing while looking rigorous. For each
task here this writes a correct solution into a copy of the fixture and
requires a pass, then writes the half-fix and requires the named test to fail.

(test_operations_suite.py does the same for the first operations tasks.)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "fixtures"
SUITES = ROOT / "evals" / "suites"


class _Task(unittest.TestCase):
    fixture = ""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()) / self.fixture
        shutil.copytree(FIXTURES / self.fixture, self.dir)
        self.addCleanup(shutil.rmtree, self.dir.parent, True)

    def write(self, name: str, source: str) -> None:
        (self.dir / name).write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")

    def write_json(self, name: str, data) -> None:
        (self.dir / name).write_text(json.dumps(data), encoding="utf-8")

    def grade(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def assertPasses(self) -> None:
        proc = self.grade()
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:])

    def assertFailsOn(self, test_name: str) -> None:
        proc = self.grade()
        self.assertNotEqual(proc.returncode, 0, "the grader accepted the mistake it exists to catch")
        self.assertIn(test_name, proc.stdout)


# --- software -------------------------------------------------------------


class CsvQuotedFields(_Task):
    fixture = "csv_quoted_fields"

    def test_the_csv_module_passes(self) -> None:
        self.write("orders.py", """
            import csv
            import io


            def parse_line(line):
                return next(csv.reader([line]))


            def load_orders(text):
                rows = [row for row in csv.reader(io.StringIO(text)) if row]
                header, out = rows[0], []
                for row in rows[1:]:
                    if len(row) != len(header):
                        raise ValueError(f"expected {len(header)} fields, got {len(row)}")
                    out.append(dict(zip(header, row)))
                return out
            """)
        self.assertPasses()

    def test_line_by_line_parsing_fails_on_a_multiline_note(self) -> None:
        self.write("orders.py", """
            import csv


            def parse_line(line):
                return next(csv.reader([line]))


            def load_orders(text):
                lines = [line for line in text.splitlines() if line.strip()]
                header = parse_line(lines[0])
                out = []
                for line in lines[1:]:
                    row = parse_line(line)
                    if len(row) != len(header):
                        raise ValueError("bad row")
                    out.append(dict(zip(header, row)))
                return out
            """)
        self.assertFailsOn("test_a_note_can_span_lines")


class SharedDefaultState(_Task):
    fixture = "shared_default_state"

    def _shipment(self, parcels: str, tags: str) -> None:
        self.write("shipment.py", f"""
            class Shipment:
                def __init__(self, shipment_id, parcels=None, tags=None):
                    self.shipment_id = shipment_id
                    self.parcels = {parcels}
                    self.tags = {tags}

                def add(self, parcel_id):
                    self.parcels.append(parcel_id)

                def tag(self, key, value):
                    self.tags[key] = value
            """)

    def test_copying_what_was_passed_passes(self) -> None:
        self._shipment("list(parcels or [])", "dict(tags or {})")
        self.assertPasses()

    def test_none_defaults_alone_still_alias_the_callers_list(self) -> None:
        self._shipment("parcels if parcels is not None else []", "tags if tags is not None else {}")
        self.assertFailsOn("test_the_callers_list_is_left_alone")


class RetryTransientOnly(_Task):
    fixture = "retry_transient_only"

    def _fetch(self, catch: str) -> None:
        self.write("fetch.py", f"""
            import time

            TRANSIENT = (ConnectionError, TimeoutError)


            def with_retries(call, attempts=3, delay=0.5, sleep=time.sleep):
                if attempts < 1:
                    raise ValueError("attempts must be at least 1")
                for i in range(attempts):
                    try:
                        return call()
                    except {catch}:
                        if i == attempts - 1:
                            raise
                        sleep(delay * 2 ** i)
            """)

    def test_retrying_transient_errors_only_passes(self) -> None:
        self._fetch("TRANSIENT")
        self.assertPasses()

    def test_retrying_everything_fails(self) -> None:
        self._fetch("Exception")
        self.assertFailsOn("test_a_bad_request_is_not_retried")


# --- finance ----------------------------------------------------------------


_ALLOCATE = """
    def allocate(total_cents, weights):
        if any(w < 0 for w in weights) or not any(weights):
            raise ValueError("weights must be non-negative and not all zero")
        whole = sum(weights)
        sign = -1 if total_cents < 0 else 1
        amount = abs(total_cents)
        parts = [divmod(amount * w, whole) for w in weights]
        shares = [q for q, _r in parts]
        order = sorted(range(len(weights)), key=lambda i: (-parts[i][1], i))
        for i in order[: amount - sum(shares)]:
            shares[i] += 1
        return [sign * s for s in shares]
    """


class AllocateCents(_Task):
    fixture = "allocate_cents"

    def test_largest_remainder_passes(self) -> None:
        self.write("allocate.py", _ALLOCATE)
        self.assertPasses()

    def test_flooring_a_refund_fails(self) -> None:
        # Largest remainder on the signed total: floor division pushes every
        # negative share down, and the "leftover" is negative.
        self.write("allocate.py", _ALLOCATE.replace(
            "sign = -1 if total_cents < 0 else 1\n        amount = abs(total_cents)",
            "sign = 1\n        amount = total_cents"))
        self.assertFailsOn("test_a_refund_mirrors_a_charge")


class DrawdownRunningPeak(_Task):
    fixture = "drawdown_running_peak"

    def _perf(self, periods: str) -> None:
        self.write("perf.py", f"""
            def max_drawdown(values):
                if not values:
                    raise ValueError("no values")
                peak, worst = values[0], 0.0
                for v in values:
                    peak = max(peak, v)
                    worst = max(worst, (peak - v) / peak)
                return worst


            def annualized_return(closes, periods_per_year=252):
                if len(closes) < 2:
                    raise ValueError("need at least two closes")
                return (closes[-1] / closes[0]) ** (periods_per_year / ({periods})) - 1
            """)

    def test_running_peak_and_return_count_passes(self) -> None:
        self._perf("len(closes) - 1")
        self.assertPasses()

    def test_counting_closes_instead_of_returns_fails(self) -> None:
        self._perf("len(closes)")
        self.assertFailsOn("test_one_year_of_closes")


class FifoCostBasis(_Task):
    fixture = "fifo_cost_basis"

    def _lots(self, order: str) -> None:
        self.write("lots.py", f"""
            import csv
            from collections import defaultdict, deque
            from pathlib import Path


            def load(name="trades.csv"):
                with open(Path(__file__).parent / name, newline="", encoding="utf-8") as fh:
                    return list(csv.DictReader(fh))


            def realized_gains(trades=None):
                trades = trades if trades is not None else load()
                lots, gains = defaultdict(deque), defaultdict(float)
                for t in {order}:
                    sym, q, price = t["symbol"], float(t["qty"]), float(t["price"])
                    if t["side"] == "buy":
                        lots[sym].append([q, price])
                        continue
                    if q > sum(lot[0] for lot in lots[sym]) + 1e-9:
                        raise ValueError(f"selling more {{sym}} than is held")
                    while q > 1e-9:
                        lot = lots[sym][0]
                        used = min(q, lot[0])
                        gains[sym] += used * (price - lot[1])
                        lot[0] -= used
                        q -= used
                        if lot[0] <= 1e-9:
                            lots[sym].popleft()
                return {{sym: round(g, 2) for sym, g in gains.items()}}
            """)

    def test_fifo_in_date_order_passes(self) -> None:
        self._lots('sorted(trades, key=lambda t: t["date"])')
        self.assertPasses()

    def test_fifo_in_file_order_fails(self) -> None:
        self._lots("trades")
        self.assertFailsOn("test_trades_are_matched_in_date_order")


class ArAging(_Task):
    fixture = "ar_aging"

    def _aging(self, *, apply_late_payments: bool) -> None:
        import csv
        from collections import defaultdict
        from datetime import date

        as_of = date(2026, 9, 30)
        paid: dict[str, float] = defaultdict(float)
        for p in csv.DictReader((self.dir / "payments.csv").open()):
            if apply_late_payments or date.fromisoformat(p["received_date"]) <= as_of:
                paid[p["invoice_no"]] += float(p["amount"])
        out: dict[str, dict[str, float]] = {}
        for inv in csv.DictReader((self.dir / "invoices.csv").open()):
            if date.fromisoformat(inv["issue_date"]) > as_of:
                continue
            owed = float(inv["amount"]) - paid[inv["invoice_no"]]
            days = (as_of - date.fromisoformat(inv["due_date"])).days
            bucket = ("current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60
                      else "61-90" if days <= 90 else "90+")
            row = out.setdefault(inv["customer"], dict.fromkeys(("current", "1-30", "31-60", "61-90", "90+"), 0.0))
            row[bucket] += max(owed, 0.0)
        self.write_json("aging.json", out)

    def test_books_as_of_month_end_pass(self) -> None:
        self._aging(apply_late_payments=False)
        self.assertPasses()

    def test_applying_an_october_payment_fails(self) -> None:
        self._aging(apply_late_payments=True)
        self.assertFailsOn("test_a_payment_after_month_end_is_not_applied")


class SplitAdjustedReturns(_Task):
    fixture = "split_adjusted_returns"

    def test_adjusted_returns_pass(self) -> None:
        self.write_json("returns.json", {"NOVA": 118 / (400 / 4) - 1, "ORBT": 33.48 / (3.10 * 10) - 1,
                                         "PIKE": 55.12 / 52 - 1})
        self.assertPasses()

    def test_percent_is_accepted(self) -> None:
        self.write_json("returns.json", {"NOVA": 18.0, "ORBT": 8.0, "PIKE": 6.0})
        self.assertPasses()

    def test_raw_closes_fail(self) -> None:
        self.write_json("returns.json", {"NOVA": 118 / 400 - 1, "ORBT": 33.48 / 3.10 - 1,
                                         "PIKE": 55.12 / 52 - 1})
        self.assertFailsOn("test_a_forward_split_is_not_a_crash")


# --- logistics --------------------------------------------------------------


class DimWeightBilling(_Task):
    fixture = "dim_weight_billing"

    def _rating(self, rounding: str) -> None:
        self.write("rating.py", f"""
            import math

            DIM_DIVISOR = 5000
            RATES = {{"A": (8.00, 2.50), "B": (12.00, 3.50)}}


            def chargeable_weight(actual_kg, length_cm, width_cm, height_cm):
                if actual_kg < 0 or min(length_cm, width_cm, height_cm) <= 0:
                    raise ValueError("weight and dimensions must be positive")
                volumetric = length_cm * width_cm * height_cm / DIM_DIVISOR
                return max(0.5, {rounding}(max(actual_kg, volumetric) * 2) / 2)


            def quote(actual_kg, dims, zone):
                if zone not in RATES:
                    raise ValueError(f"unknown zone {{zone!r}}")
                weight = chargeable_weight(actual_kg, *dims)
                first, extra = RATES[zone]
                return round(first + round((weight - 0.5) / 0.5) * extra, 2)
            """)

    def test_rounding_up_passes(self) -> None:
        self._rating("math.ceil")
        self.assertPasses()

    def test_rounding_to_nearest_fails(self) -> None:
        self._rating("round")
        self.assertFailsOn("test_actual_weight_rounds_up")


class EtaBusinessDays(_Task):
    fixture = "eta_business_days"

    def _eta(self, cutoff: str) -> None:
        self.write("eta.py", f"""
            import csv
            from datetime import timedelta
            from pathlib import Path

            CUTOFF_HOUR = 15

            with open(Path(__file__).with_name("holidays.csv"), newline="", encoding="utf-8") as fh:
                HOLIDAYS = {{row["date"] for row in csv.DictReader(fh)}}


            def _working(day):
                return day.weekday() < 5 and day.isoformat() not in HOLIDAYS


            def _next_working(day):
                day += timedelta(days=1)
                while not _working(day):
                    day += timedelta(days=1)
                return day


            def estimated_delivery(order_time, transit_days):
                day = order_time.date()
                if not (_working(day) and {cutoff}):
                    day = _next_working(day)
                for _ in range(transit_days):
                    day = _next_working(day)
                return day
            """)

    def test_working_days_and_cutoff_pass(self) -> None:
        self._eta("order_time.hour < CUTOFF_HOUR")
        self.assertPasses()

    def test_counting_15_00_as_before_cutoff_fails(self) -> None:
        self._eta("order_time.hour <= CUTOFF_HOUR and order_time.minute == 0")
        self.assertFailsOn("test_the_cutoff_itself_is_too_late")


class SlaTimezones(_Task):
    fixture = "sla_timezones"

    def _sla(self, parse: str) -> None:
        self.write("sla.py", f"""
            from datetime import datetime


            def _parse(stamp):
                {parse}


            def hours_late(promised, delivered):
                late = (_parse(delivered) - _parse(promised)).total_seconds() / 3600
                return max(0.0, round(late, 2))


            def late_shipments(rows, grace_minutes=30):
                return [r["id"] for r in rows
                        if hours_late(r["promised"], r["delivered"]) * 60 > grace_minutes]
            """)

    def test_aware_timestamps_pass(self) -> None:
        self._sla("parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))\n"
                  "                if parsed.tzinfo is None:\n"
                  "                    raise ValueError(f'no UTC offset in {stamp!r}')\n"
                  "                return parsed")
        self.assertPasses()

    def test_assuming_utc_for_a_naive_stamp_fails(self) -> None:
        self._sla("from datetime import timezone\n"
                  "                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))\n"
                  "                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)")
        self.assertFailsOn("test_a_timestamp_without_an_offset_is_refused")


class CarrierOnTime(_Task):
    fixture = "carrier_on_time"

    def _kpi(self, *, dedupe: bool, overdue_in_transit_late: bool) -> None:
        import csv
        from datetime import date

        rows = list(csv.DictReader((self.dir / "deliveries.csv").open()))
        if dedupe:
            rows = list({r["shipment_id"]: r for r in rows}.values())
        counts: dict[str, list[int]] = {}
        for r in rows:
            promised = date.fromisoformat(r["promised_date"])
            if r["status"] == "cancelled":
                continue
            if r["status"] == "in_transit":
                if overdue_in_transit_late and promised < date(2026, 9, 30):
                    counts.setdefault(r["carrier"], [0, 0])[0] += 1
                continue
            c = counts.setdefault(r["carrier"], [0, 0])
            c[0] += 1
            c[1] += date.fromisoformat(r["delivered_date"]) <= promised
        self.write_json("kpi.json", {k: {"shipments": n, "on_time_rate": ok / n}
                                     for k, (n, ok) in counts.items()})

    def test_counting_each_shipment_once_passes(self) -> None:
        self._kpi(dedupe=True, overdue_in_transit_late=True)
        self.assertPasses()

    def test_counting_every_scan_fails(self) -> None:
        self._kpi(dedupe=False, overdue_in_transit_late=True)
        self.assertFailsOn("test_shipments_counted_once_cancellations_excluded")

    def test_leaving_out_an_overdue_parcel_fails(self) -> None:
        self._kpi(dedupe=True, overdue_in_transit_late=False)
        self.assertFailsOn("test_an_overdue_parcel_in_transit_is_late")


# --- batch 2: software ------------------------------------------------------


class PaginationCursor(_Task):
    fixture = "pagination_cursor"

    def _client(self, loop_guard: bool) -> None:
        guard = ("if cursor in cursors:\n"
                 "                        raise RuntimeError(f\"cursor {cursor!r} repeated\")\n"
                 "                    cursors.add(cursor)") if loop_guard else "pass"
        self.write("client.py", f"""
            def fetch_all(get_page, page_size=100):
                items, seen, cursors, cursor = [], set(), set(), None
                while True:
                    page = get_page(cursor, page_size)
                    for item in page["items"]:
                        if item["id"] not in seen:
                            seen.add(item["id"])
                            items.append(item)
                    cursor = page.get("next")
                    if not cursor:
                        return items
                    {guard}
            """)

    def test_dedupe_and_loop_guard_pass(self) -> None:
        self._client(loop_guard=True)
        self.assertPasses()

    def test_dedupe_alone_fails_on_a_loop(self) -> None:
        self._client(loop_guard=False)
        self.assertFailsOn("test_a_looping_cursor_is_an_error")


class ConfigDeepMerge(_Task):
    fixture = "config_deep_merge"

    def _merge(self, copy_untouched: str) -> None:
        self.write("merge.py", f"""
            import copy

            DEFAULTS = {{
                "carrier": {{"name": "SF", "timeout": 30, "retries": 3}},
                "warehouses": ["SHA"],
                "label": {{"size": "4x6", "dpi": 203}},
            }}


            def merge_config(defaults, override):
                result = {copy_untouched}
                for key, value in override.items():
                    if isinstance(value, dict) and isinstance(result.get(key), dict):
                        result[key] = merge_config(result[key], value)
                    else:
                        result[key] = copy.deepcopy(value)
                return result
            """)

    def test_a_deep_copy_passes(self) -> None:
        self._merge("copy.deepcopy(defaults)")
        self.assertPasses()

    def test_a_shallow_copy_shares_nested_defaults(self) -> None:
        self._merge("dict(defaults)")
        self.assertFailsOn("test_changing_the_result_leaves_the_defaults_alone")


class SlidingWindowLimiter(_Task):
    fixture = "sliding_window_limiter"

    def _limiter(self, record_refused: bool) -> None:
        refused = "self._calls.append(now)\n                    return False" if record_refused else "return False"
        self.write("limiter.py", f"""
            import time
            from collections import deque


            class RateLimiter:
                def __init__(self, limit, window, clock=time.monotonic):
                    if limit <= 0 or window <= 0:
                        raise ValueError("limit and window must be positive")
                    self.limit, self.window, self.clock = limit, window, clock
                    self._calls = deque()

                def allow(self):
                    now = self.clock()
                    while self._calls and now - self._calls[0] >= self.window:
                        self._calls.popleft()
                    if len(self._calls) < self.limit:
                        self._calls.append(now)
                        return True
                    {refused}
            """)

    def test_a_sliding_window_passes(self) -> None:
        self._limiter(record_refused=False)
        self.assertPasses()

    def test_counting_refused_calls_fails(self) -> None:
        self._limiter(record_refused=True)
        self.assertFailsOn("test_refused_calls_do_not_count")


class UniqueSlugs(_Task):
    fixture = "unique_slugs"

    def _slug(self, unique_body: str) -> None:
        self.write("slug.py", """
            import re
            import unicodedata


            def slugify(title):
                text = unicodedata.normalize("NFKD", title.lower())
                text = "".join(ch for ch in text if not unicodedata.combining(ch))
                slug = re.sub(r"[^a-z0-9\\u4e00-\\u9fff]+", "-", text).strip("-")
                if not slug:
                    raise ValueError(f"nothing to slug in {title!r}")
                return slug


            def unique_slugs(titles):
            """ + unique_body)

    def test_checking_every_taken_slug_passes(self) -> None:
        self._slug("""
                taken, out = set(), []
                for title in titles:
                    base = candidate = slugify(title)
                    n = 2
                    while candidate in taken:
                        candidate, n = f"{base}-{n}", n + 1
                    taken.add(candidate)
                    out.append(candidate)
                return out
            """)
        self.assertPasses()

    def test_counting_per_base_collides(self) -> None:
        self._slug("""
                counts, out = {}, []
                for title in titles:
                    base = slugify(title)
                    counts[base] = counts.get(base, 0) + 1
                    out.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
                return out
            """)
        self.assertFailsOn("test_a_number_never_collides_with_a_real_title")


class SqlLikeEscape(_Task):
    fixture = "sql_like_escape"

    def _db(self, escape: bool) -> None:
        pattern = ('"%" + text.replace("\\\\", "\\\\\\\\").replace("%", "\\\\%").replace("_", "\\\\_") + "%"'
                   if escape else '"%" + text + "%"')
        clause = "name LIKE ? ESCAPE '\\\\'" if escape else "name LIKE ?"
        self.write("inventory_db.py", f"""
            import sqlite3


            def connect(rows):
                con = sqlite3.connect(":memory:")
                con.execute("CREATE TABLE items (sku TEXT, name TEXT, qty INTEGER)")
                con.executemany("INSERT INTO items VALUES (?, ?, ?)", rows)
                return con


            def find_by_name(con, name):
                return [r[0] for r in con.execute("SELECT sku FROM items WHERE name = ? ORDER BY sku", (name,))]


            def search(con, text):
                sql = "SELECT sku FROM items WHERE {clause} ORDER BY sku"
                return [r[0] for r in con.execute(sql, ({pattern},))]
            """)

    def test_parameters_and_an_escaped_pattern_pass(self) -> None:
        self._db(escape=True)
        self.assertPasses()

    def test_parameters_without_escaping_like_fail(self) -> None:
        self._db(escape=False)
        self.assertFailsOn("test_percent_is_a_percent")


# --- batch 2: finance -------------------------------------------------------


class LoanAmortization(_Task):
    fixture = "loan_amortization"

    def _loan(self, settle_last: bool) -> None:
        last = ("if month == months - 1:\n"
                "                        paid, payment_now = round(balance, 2), round(balance + interest, 2)\n"
                "                    else:\n"
                "                        paid, payment_now = round(payment - interest, 2), payment") if settle_last else \
               "paid, payment_now = round(payment - interest, 2), payment"
        self.write("loan.py", f"""
            def schedule(principal, annual_rate, months):
                r = annual_rate / 12
                payment = round(principal / months, 2) if r == 0 else round(principal * r / (1 - (1 + r) ** -months), 2)
                rows, balance = [], principal
                for month in range(months):
                    interest = round(balance * r, 2)
                    {last}
                    balance = round(balance - paid, 2)
                    rows.append((payment_now, interest, paid, balance))
                return rows
            """)

    def test_settling_the_last_payment_passes(self) -> None:
        self._loan(settle_last=True)
        self.assertPasses()

    def test_a_monthly_rate_alone_leaves_cents_over(self) -> None:
        self._loan(settle_last=False)
        self.assertFailsOn("test_the_balance_ends_at_zero")


class SharpeRatio(_Task):
    fixture = "sharpe_ratio"

    def _risk(self, stdev: str) -> None:
        self.write("risk.py", f"""
            import math
            import statistics


            def sharpe_ratio(daily_returns, annual_risk_free=0.0, periods=252):
                if len(daily_returns) < 2:
                    raise ValueError("need at least two returns")
                excess = [r - annual_risk_free / periods for r in daily_returns]
                sd = statistics.{stdev}(excess)
                if sd == 0:
                    raise ValueError("returns do not vary")
                return statistics.mean(excess) / sd * math.sqrt(periods)
            """)

    def test_sample_stdev_passes(self) -> None:
        self._risk("stdev")
        self.assertPasses()

    def test_population_stdev_fails(self) -> None:
        self._risk("pstdev")
        self.assertFailsOn("test_without_a_risk_free_rate")


class XirrDated(_Task):
    fixture = "xirr_dated"

    def _xirr(self, year: str) -> None:
        self.write("cashflows.py", f"""
            def xirr(flows):
                amounts = [a for _d, a in flows]
                if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
                    raise ValueError("cash flows never change sign")
                start = min(d for d, _a in flows)

                def npv(rate):
                    return sum(a / (1 + rate) ** ((d - start).days / {year}) for d, a in flows)

                lo, hi = -0.9999, 100.0
                for _ in range(300):
                    mid = (lo + hi) / 2
                    if npv(mid) > 0:
                        lo = mid
                    else:
                        hi = mid
                return mid
            """)

    def test_actual_days_over_365_pass(self) -> None:
        self._xirr("365")
        self.assertPasses()

    def test_a_365_25_day_year_fails(self) -> None:
        self._xirr("365.25")
        self.assertFailsOn("test_half_a_year")


class SessionVwap(_Task):
    fixture = "session_vwap"

    def _vwap(self, price: str) -> None:
        self.write("vwap.py", f"""
            def daily_vwap(bars):
                pv, vol = {{}}, {{}}
                for ts, high, low, close, volume in bars:
                    day = ts[:10]
                    pv[day] = pv.get(day, 0.0) + ({price}) * volume
                    vol[day] = vol.get(day, 0.0) + volume
                return {{day: pv[day] / vol[day] for day in pv if vol[day] > 0}}
            """)

    def test_typical_price_per_day_passes(self) -> None:
        self._vwap("(high + low + close) / 3")
        self.assertPasses()

    def test_close_price_fails(self) -> None:
        self._vwap("close")
        self.assertFailsOn("test_typical_price_weighted_by_volume")


class FxInvoiceTotals(_Task):
    fixture = "fx_invoice_totals"

    def _totals(self, *, rate_on_or_before: bool) -> None:
        import csv

        rates: dict[str, dict[str, float]] = {}
        for r in csv.DictReader((self.dir / "rates.csv").open()):
            rates.setdefault(r["date"], {})[r["pair"]] = float(r["rate"])
        out = {}
        for inv in csv.DictReader((self.dir / "invoices.csv").open()):
            amount, cur = float(inv["amount"]), inv["currency"]
            if cur == "EUR":
                out[inv["invoice_no"]] = round(amount, 2)
                continue
            day = (max(d for d in rates if d <= inv["date"]) if rate_on_or_before
                   else min(d for d in rates if d >= inv["date"]))
            out[inv["invoice_no"]] = round(amount / rates[day]["EUR" + cur], 2)
        self.write_json("eur_totals.json", {"invoices": out, "total_eur": round(sum(out.values()), 2)})

    def test_the_last_rate_on_or_before_passes(self) -> None:
        self._totals(rate_on_or_before=True)
        self.assertPasses()

    def test_the_next_published_rate_fails(self) -> None:
        self._totals(rate_on_or_before=False)
        self.assertFailsOn("test_a_weekend_invoice_uses_the_last_rate_before_it")


# --- batch 2: logistics -----------------------------------------------------


class FefoPicking(_Task):
    fixture = "fefo_picking"

    def _picking(self, usable: str) -> None:
        self.write("picking.py", f"""
            import csv
            from datetime import date
            from pathlib import Path


            def load(name="lots.csv"):
                with open(Path(__file__).parent / name, newline="", encoding="utf-8") as fh:
                    return list(csv.DictReader(fh))


            def allocate(sku, qty, as_of, lots=None):
                lots = lots if lots is not None else load()
                rows = sorted((r for r in lots if r["sku"] == sku and {usable}),
                              key=lambda r: (r["expires"], r["received"]))
                picks, need = [], qty
                for row in rows:
                    if need <= 0:
                        break
                    take = min(need, int(row["qty"]))
                    picks.append((row["lot"], take))
                    need -= take
                if need > 0:
                    raise ValueError(f"short {{need}} of {{sku}}")
                return picks
            """)

    def test_fefo_with_expiry_and_hold_passes(self) -> None:
        self._picking('date.fromisoformat(r["expires"]) > as_of and r["status"] != "hold"')
        self.assertPasses()

    def test_allowing_a_lot_that_expires_on_the_ship_date_fails(self) -> None:
        self._picking('date.fromisoformat(r["expires"]) >= as_of and r["status"] != "hold"')
        self.assertFailsOn("test_a_lot_expiring_on_the_ship_date_cannot_go")


class DepotDistance(_Task):
    fixture = "depot_distance"

    def _distance(self, columns: str) -> None:
        self.write("distance.py", f"""
            import csv
            from math import asin, cos, radians, sin, sqrt
            from pathlib import Path

            EARTH_RADIUS_KM = 6371.0


            def load(name="depots.csv"):
                with open(Path(__file__).parent / name, newline="", encoding="utf-8") as fh:
                    return {{r["name"]: ({columns}) for r in csv.DictReader(fh)}}


            def distance_km(a, b):
                lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
                h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
                return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


            def nearest_depot(point):
                depots = load()
                return min(depots, key=lambda name: distance_km(point, depots[name]))
            """)

    def test_radians_and_named_columns_pass(self) -> None:
        self._distance('float(r["lat"]), float(r["lon"])')
        self.assertPasses()

    def test_radians_alone_fails_on_the_column_order(self) -> None:
        self._distance('float(r["lon"]), float(r["lat"])')
        self.assertFailsOn("test_tianjin_goes_to_beijing")


class DockWindows(_Task):
    fixture = "dock_windows"

    def _windows(self, joins: str) -> None:
        self.write("windows.py", f"""
            def _minutes(text):
                hours, _, minutes = text.partition(":")
                h, m = int(hours), int(minutes)
                if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
                    raise ValueError(f"not a time of day: {{text!r}}")
                return h * 60 + m


            def _text(total):
                return f"{{total // 60:02d}}:{{total % 60:02d}}"


            def merge_windows(windows):
                spans = []
                for start, end in windows:
                    s, e = _minutes(start), _minutes(end)
                    spans += [(s, 1440), (0, e)] if e < s else [(s, e)]
                merged = []
                for s, e in sorted(spans):
                    if merged and s {joins} merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                    else:
                        merged.append((s, e))
                return [(_text(s), _text(e)) for s, e in merged]
            """)

    def test_minutes_with_touching_windows_joined_pass(self) -> None:
        self._windows("<=")
        self.assertPasses()

    def test_only_overlaps_joined_fails(self) -> None:
        self._windows("<")
        self.assertFailsOn("test_touching_windows_merge")


class PostcodeZones(_Task):
    fixture = "postcode_zones"

    def _zones(self, normalise: str) -> None:
        self.write("zones.py", f"""
            import csv
            from pathlib import Path

            with open(Path(__file__).with_name("zones.csv"), newline="", encoding="utf-8") as _fh:
                ZONES = list(csv.DictReader(_fh))


            def zone_for(postcode):
                {normalise}
                for row in ZONES:
                    if row["from"] <= code <= row["to"]:
                        return int(row["zone"])
                raise LookupError(f"no zone for {{code}}")
            """)

    def test_validating_and_padding_passes(self) -> None:
        self._zones("code = str(postcode).strip()\n"
                    "                if not (code.isdigit() and 1 <= len(code) <= 5):\n"
                    "                    raise ValueError(f\"not a postcode: {postcode!r}\")\n"
                    "                code = code.zfill(5)")
        self.assertPasses()

    def test_padding_without_validation_fails(self) -> None:
        self._zones("code = str(postcode).strip().zfill(5)")
        self.assertFailsOn("test_not_a_postcode")


class CustomsDeMinimis(_Task):
    fixture = "customs_de_minimis"

    def _duties(self, *, per_shipment: bool) -> None:
        import csv
        from collections import defaultdict

        rates = {r["hs_prefix"]: float(r["rate"]) for r in csv.DictReader((self.dir / "duty_rates.csv").open())}
        fx = {r["currency"]: float(r["eur_per_unit"]) for r in csv.DictReader((self.dir / "fx.csv").open())}
        lines = defaultdict(list)
        for r in csv.DictReader((self.dir / "lines.csv").open()):
            lines[r["shipment_id"]].append(r)
        out = {}
        for shipment, rows in lines.items():
            values = [float(r["value"]) * fx[r["currency"]] for r in rows]
            duty = 0.0
            for value, row in zip(values, rows):
                exempt = sum(values) <= 150 if per_shipment else value <= 150
                if not exempt:
                    prefix = max((p for p in rates if row["hs_code"].startswith(p)), key=len)
                    duty += value * rates[prefix]
            out[shipment] = round(duty, 2)
        self.write_json("duties.json", out)

    def test_threshold_per_shipment_passes(self) -> None:
        self._duties(per_shipment=True)
        self.assertPasses()

    def test_threshold_per_line_fails(self) -> None:
        self._duties(per_shipment=False)
        self.assertFailsOn("test_the_threshold_is_per_shipment_not_per_line")


class CycleCountVariance(_Task):
    fixture = "cycle_count_variance"

    def _variances(self, *, convert: bool) -> None:
        import csv

        system = {(r["location"], r["sku"]): int(r["qty_each"])
                  for r in csv.DictReader((self.dir / "system.csv").open())}
        uom = {(r["sku"], r["uom"]): int(r["eaches"]) for r in csv.DictReader((self.dir / "uom.csv").open())}
        rows = []
        for r in csv.DictReader((self.dir / "counts.csv").open()):
            factor = 1 if r["uom"] == "EACH" or not convert else uom[(r["sku"], r["uom"])]
            counted = int(r["counted"]) * factor
            held = system.get((r["location"], r["sku"]), 0)
            if counted != held:
                rows.append({"location": r["location"], "sku": r["sku"], "system": held,
                             "counted": counted, "variance": counted - held})
        self.write_json("variances.json", rows)

    def test_counts_in_eaches_pass(self) -> None:
        self._variances(convert=True)
        self.assertPasses()

    def test_cases_taken_as_eaches_fail(self) -> None:
        self._variances(convert=False)
        self.assertFailsOn("test_cases_are_counted_as_eaches")


# --- the suite files --------------------------------------------------------


class TheBank(unittest.TestCase):
    def _tasks(self):
        tasks = []
        for suite in sorted(SUITES.glob("*.yaml")):
            tasks += yaml.safe_load(suite.read_text(encoding="utf-8"))["tasks"]
        return tasks

    def test_every_fixture_is_run_by_a_suite(self) -> None:
        on_disk = {p.name for p in FIXTURES.iterdir() if p.is_dir()}
        used = {t["fixture"] for t in self._tasks()}
        self.assertEqual(on_disk - used, set(), "fixture with no task — it is never measured")
        self.assertEqual(used - on_disk, set())

    def test_ids_are_unique(self) -> None:
        ids = [t["id"] for t in self._tasks()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_data_files_are_protected(self) -> None:
        for task in self._tasks():
            fixture = FIXTURES / task["fixture"]
            if any(fixture.glob("*.csv")):
                with self.subTest(task=task["id"]):
                    self.assertIn("*.csv", task.get("protect", []),
                                  "an agent could edit the data until it passes")

    def test_answer_keys_are_hidden(self) -> None:
        """An operations grader holds the expected numbers; in view it is the answer."""
        tasks = yaml.safe_load((SUITES / "operations.yaml").read_text(encoding="utf-8"))["tasks"]
        for task in tasks:
            with self.subTest(task=task["id"]):
                hidden = task.get("hidden") or []
                self.assertTrue(hidden, "grader is visible to the agent")
                for name in hidden:
                    self.assertTrue((FIXTURES / task["fixture"] / name).is_file(), name)
                    # Named, so the agent's own test files are not scored with it.
                    self.assertIn(name, task["verify"])
                self.assertIn("--noconftest", task["verify"],
                              "a conftest.py the agent writes could skip the grader")

    def test_the_bank_covers_all_three_areas(self) -> None:
        tasks = self._tasks()
        self.assertGreaterEqual(len(tasks), 50)
        tags = [set(t["tags"]) for t in tasks]
        self.assertGreaterEqual(sum("software" in t for t in tags), 12)
        self.assertGreaterEqual(sum("finance" in t or "payments" in t for t in tags), 12)
        self.assertGreaterEqual(sum("logistics" in t for t in tags), 12)


if __name__ == "__main__":
    unittest.main()
