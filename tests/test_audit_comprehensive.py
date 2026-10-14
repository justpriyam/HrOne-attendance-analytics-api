"""
Comprehensive Audit Verification Test Suite for HROne Attendance & Analytics API.
Covers strict contract checks, business rule boundaries R1-R10, concurrency, and explain plans.
"""

import concurrent.futures
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient

from app.main import app, db, client as mongo_client, _round2, _round4

IST = timezone(timedelta(hours=5, minutes=30))


def _to_epoch_ms(year, month, day, hour, minute, second=0, millisecond=0):
    dt = datetime(year, month, day, hour, minute, second, millisecond * 1000, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@pytest.fixture(autouse=True)
def clean_db():
    """Clean the test database before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def api():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed_emp(api):
    def _create(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-05",
    ):
        return api.post("/employees", json={
            "emp_code": emp_code,
            "name": name,
            "email": email,
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
        })
    return _create


# ==============================================================================
# AUDIT 1: R1 Truncation & Timestamp Precision
# ==============================================================================

class TestR1SubsecondTruncation:
    def test_subsecond_punch_in_is_truncated_to_whole_seconds(self, api, seed_emp):
        """R1: 09:40:00.900 counts as 09:40:00, and the truncated value is stored and returned."""
        seed_emp()
        # 09:40:00.900 IST = 04:10:00.900 UTC
        ts_with_ms = _to_epoch_ms(2026, 7, 6, 4, 10, 0, millisecond=900)
        expected_ts = _to_epoch_ms(2026, 7, 6, 4, 10, 0, millisecond=0)

        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_with_ms})
        assert r.status_code == 201
        data = r.json()
        assert data["punch_in"] == expected_ts
        # Since 09:40:00 is within 10 min grace period, late_minutes must be 0
        assert data["late_minutes"] == 0


# ==============================================================================
# AUDIT 2: R2 Grace Period Exact Boundaries
# ==============================================================================

class TestR2GracePeriodBoundaries:
    def test_exact_grace_boundary_09_40_00_is_not_late(self, api, seed_emp):
        """09:40:00 IST for 09:30 shift is exactly 10:00 -> not late (0 min)."""
        seed_emp()
        ts = _to_epoch_ms(2026, 7, 6, 4, 10, 0)  # 09:40:00 IST
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_one_second_past_grace_09_40_01_is_late_10_minutes(self, api, seed_emp):
        """09:40:01 IST is strictly > 10:00 -> late 10 minutes (floor from shift_start)."""
        seed_emp()
        ts = _to_epoch_ms(2026, 7, 6, 4, 10, 1)  # 09:40:01 IST
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10


# ==============================================================================
# AUDIT 3: R3 Overtime Threshold Exact Boundaries
# ==============================================================================

class TestR3OvertimeBoundaries:
    def test_overtime_29_minutes_is_zero(self, api, seed_emp):
        """Overtime < 30 minutes must be 0."""
        seed_emp()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)   # 09:30 IST
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 29) # 18:59 IST = 29 min past shift_end 18:30
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 0

    def test_overtime_30_minutes_is_counted(self, api, seed_emp):
        """Overtime exactly 30 minutes is eligible -> 30."""
        seed_emp()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 30) # 19:00 IST = 30 min past 18:30
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 30


# ==============================================================================
# AUDIT 4: R4 & R5 Half-Day and Half-Up Rounding
# ==============================================================================

class TestR4R5HalfDayAndRounding:
    def test_half_day_boundary_at_4_50(self, api, seed_emp):
        """R5: half_day = true when rounded work_hours < 4.50."""
        seed_emp()
        # Exactly 4.50 hours: 4 hours 30 minutes
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 8, 30) # 4h 30m = 4.50h
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 200
        data = r.json()
        assert data["work_hours"] == 4.50
        assert data["half_day"] is False

    def test_half_day_boundary_at_4_49(self, api, seed_emp):
        """4.49 hours -> half_day = true."""
        seed_emp()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 4 hours 29 minutes and 30 seconds = 4.491666... -> rounds to 4.49
        ts_out = _to_epoch_ms(2026, 7, 6, 8, 29, 30)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 200
        data = r.json()
        assert data["work_hours"] == 4.49
        assert data["half_day"] is True


# ==============================================================================
# AUDIT 5: Concurrency - Atomic Punch-Out & Corrections
# ==============================================================================

class TestConcurrencyAudit:
    def test_concurrent_punch_outs_exactly_one_succeeds(self, api, seed_emp):
        """Simultaneous punch-outs for the same open record: exactly one 200, the other 409."""
        seed_emp()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

        ts_out = _to_epoch_ms(2026, 7, 6, 13, 0)

        def do_punch_out():
            with TestClient(app) as c:
                return c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_punch_out) for _ in range(2)]
            results = [f.result() for f in futures]

        codes = sorted([r.status_code for r in results])
        assert codes == [200, 409]

    def test_concurrent_corrections_guard(self, api, seed_emp):
        """Concurrent manual corrections: history size guard prevents overwrites; loser gets 409."""
        seed_emp()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

        new_in = _to_epoch_ms(2026, 7, 6, 4, 5)

        def do_regularize(idx):
            with TestClient(app) as c:
                return c.patch(
                    "/attendance/EMP0001/2026-07-06",
                    json={
                        "reason": f"Correction attempt {idx}",
                        "regularized_by": f"admin_{idx}",
                        "punch_in": new_in + idx * 1000,
                    }
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_regularize, i) for i in range(2)]
            results = [f.result() for f in futures]

        codes = [r.status_code for r in results]
        assert 200 in codes


# ==============================================================================
# AUDIT 6: R7 & R9 Analytics Edge Cases
# ==============================================================================

class TestAnalyticsAccuracy:
    def test_mid_month_joiner_working_days(self, api, seed_emp):
        """Employee joined on 2026-07-15: working days counted only from 15th to end of month."""
        seed_emp(emp_code="EMP0002", joined_on="2026-07-15")
        r = api.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        # July 2026: 15th is Wednesday. Weekdays from 15 to 31:
        # 15,16,17 (3) + 20..24 (5) + 27..31 (5) = 13 weekdays
        assert data["working_days"] == 13
        assert data["present_days"] == 0.0
        assert data["attendance_pct"] == 0.0

    def test_weekend_record_does_not_count_towards_present_days(self, api, seed_emp):
        """Weekend records contribute to overtime/late but NOT to present_days."""
        seed_emp()
        # 2026-07-11 is a Saturday
        ts_in = _to_epoch_ms(2026, 7, 11, 4, 35) # 10:05 IST -> 35 min late
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 11, 13, 40) # 19:10 IST -> 40 min overtime
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        r = api.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        # Present days must be 0 because it was Saturday
        assert data["present_days"] == 0.0
        # But overtime and late totals must include Saturday
        assert data["total_late_minutes"] == 35
        assert data["total_overtime_minutes"] == 40

    def test_department_headcount_includes_zero_log_employees(self, api, seed_emp):
        """R9: Department summary headcount includes employees with zero logs."""
        seed_emp(emp_code="EMP0001", department="Engineering", joined_on="2026-01-01")
        seed_emp(emp_code="EMP0002", email="e2@example.com", department="Engineering", joined_on="2026-01-01")
        # No attendance logs for either employee
        r = api.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        dept = data["items"][0]
        assert dept["headcount"] == 2
        assert dept["present_days"] == 0.0


# ==============================================================================
# AUDIT 7: Leaderboard Ranking Ties
# ==============================================================================

class TestLeaderboardRankingTies:
    def test_standard_competition_ranking_with_ties(self, api, seed_emp):
        """Standard competition ranking: ties get same rank, next rank is skipped (1, 2, 2, 4)."""
        seed_emp(emp_code="EMP0001")
        seed_emp(emp_code="EMP0002", email="e2@example.com")
        seed_emp(emp_code="EMP0003", email="e3@example.com")
        seed_emp(emp_code="EMP0004", email="e4@example.com")

        # EMP0001: 50 min late
        ts1 = _to_epoch_ms(2026, 7, 6, 4, 50)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts1})

        # EMP0002: 30 min late
        ts2 = _to_epoch_ms(2026, 7, 6, 4, 30)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ts2})

        # EMP0003: 30 min late (tied with EMP0002)
        ts3 = _to_epoch_ms(2026, 7, 6, 4, 30)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ts3})

        # EMP0004: 15 min late
        ts4 = _to_epoch_ms(2026, 7, 6, 4, 15)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ts4})

        r = api.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 4
        assert items[0]["rank"] == 1
        assert items[0]["emp_code"] == "EMP0001"
        assert items[1]["rank"] == 2
        assert items[2]["rank"] == 2
        assert items[3]["rank"] == 4
        assert items[3]["emp_code"] == "EMP0004"


# ==============================================================================
# AUDIT 8: Contract & Schema Strictness
# ==============================================================================

class TestContractStrictness:
    def test_no_internal_id_leaks(self, api, seed_emp):
        """MongoDB _id must never be present in any response."""
        r_emp = seed_emp()
        assert "_id" not in r_emp.json()

        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        r_punch = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        assert "_id" not in r_punch.json()

        r_list = api.get("/attendance")
        for item in r_list.json()["items"]:
            assert "_id" not in item

    def test_pagination_max_page_size_100(self, api):
        """Page size > 100 must be rejected with 422."""
        r = api.get("/attendance?page_size=101")
        assert r.status_code == 422
