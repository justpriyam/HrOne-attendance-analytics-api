"""Comprehensive tests for the HROne Attendance API."""
import time
import concurrent.futures
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# We need to set env vars BEFORE importing the app
import os
import uuid

TEST_DB = f"hrone_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_DB"] = TEST_DB

from app.main import app, db, client as mongo_client

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def clean_db():
    """Clean the test database before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    # Don't drop between tests — autouse clean handles it


@pytest.fixture(scope="session", autouse=True)
def drop_test_db():
    """Drop the test database at the end of the session."""
    yield
    mongo_client.drop_database(TEST_DB)


@pytest.fixture
def api():
    """FastAPI test client."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed_employee(api):
    """Helper to create an employee."""
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


def _to_epoch_ms(year, month, day, hour, minute, second=0, tz=timezone.utc):
    """Helper to create epoch milliseconds from components."""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return int(dt.timestamp() * 1000)


# ── Health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_ok(self, api):
        r = api.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ── Employees ────────────────────────────────────────────────────────────────

class TestEmployees:
    def test_create_employee(self, api, seed_employee):
        r = seed_employee()
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["name"] == "Asha Rao"
        assert data["email"] == "asha@example.com"
        assert data["department"] == "Engineering"
        assert data["shift_start"] == "09:30"
        assert data["shift_end"] == "18:30"
        assert data["joined_on"] == "2026-01-05"
        assert isinstance(data["created_at"], int)

    def test_duplicate_employee(self, api, seed_employee):
        seed_employee()
        r = seed_employee()
        assert r.status_code == 409

    def test_invalid_emp_code(self, api):
        r = api.post("/employees", json={
            "emp_code": "INVALID", "name": "X", "email": "x@x.com",
            "department": "D", "joined_on": "2026-01-01",
        })
        assert r.status_code == 422

    def test_list_employees(self, api, seed_employee):
        seed_employee(emp_code="EMP0001", name="A")
        seed_employee(emp_code="EMP0002", name="B", email="b@b.com")
        r = api.get("/employees")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        # Sorted by emp_code ascending
        assert data["items"][0]["emp_code"] == "EMP0001"
        assert data["items"][1]["emp_code"] == "EMP0002"

    def test_list_filter_department(self, api, seed_employee):
        seed_employee(emp_code="EMP0001", department="Engineering")
        seed_employee(emp_code="EMP0002", department="Sales", email="b@b.com")
        r = api.get("/employees", params={"department": "Sales"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["department"] == "Sales"

    def test_pagination(self, api, seed_employee):
        for i in range(1, 6):
            seed_employee(emp_code=f"EMP{i:04d}", email=f"e{i}@x.com")
        r = api.get("/employees", params={"page": 1, "page_size": 2})
        data = r.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["items"][0]["emp_code"] == "EMP0001"

        r2 = api.get("/employees", params={"page": 2, "page_size": 2})
        data2 = r2.json()
        assert data2["items"][0]["emp_code"] == "EMP0003"

    def test_shift_start_equals_shift_end(self, api):
        r = api.post("/employees", json={
            "emp_code": "EMP0001", "name": "X", "email": "x@x.com",
            "department": "D", "shift_start": "09:30", "shift_end": "09:30",
            "joined_on": "2026-01-01",
        })
        assert r.status_code == 422


# ── Punch-In ─────────────────────────────────────────────────────────────────

class TestPunchIn:
    def test_basic_punch_in(self, api, seed_employee):
        seed_employee()
        # 09:30 IST = 04:00 UTC on 2026-07-06
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["date"] == "2026-07-06"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] == ts
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False
        assert data["history"] == []

    def test_punch_in_unknown_employee(self, api):
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ts})
        assert r.status_code == 404

    def test_duplicate_punch_in(self, api, seed_employee):
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        r1 = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r1.status_code == 201
        ts2 = _to_epoch_ms(2026, 7, 6, 5, 0)
        r2 = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts2})
        assert r2.status_code == 409

    def test_punch_in_with_wfh_status(self, api, seed_employee):
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        r = api.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": ts, "status": "WFH"
        })
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"

    def test_punch_in_invalid_status(self, api, seed_employee):
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        r = api.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": ts, "status": "ABSENT"
        })
        assert r.status_code == 422


# ── Late Detection (R2) ─────────────────────────────────────────────────────

class TestLateDetection:
    def test_within_grace_exact_10min(self, api, seed_employee):
        """09:40:00 with shift_start 09:30 -> exactly 10 minutes -> NOT late."""
        seed_employee()
        # 09:40:00 IST = 04:10:00 UTC
        ts = _to_epoch_ms(2026, 7, 6, 4, 10)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_late_by_one_second(self, api, seed_employee):
        """09:40:01 with shift_start 09:30 -> strictly > 10 min -> late, late_minutes=10."""
        seed_employee()
        # 09:40:01 IST = 04:10:01 UTC
        ts = _to_epoch_ms(2026, 7, 6, 4, 10, 1)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10  # floor(601/60) = 10

    def test_late_35_minutes(self, api, seed_employee):
        """10:05 with shift_start 09:30 -> 35 minutes late."""
        seed_employee()
        # 10:05:00 IST = 04:35:00 UTC
        ts = _to_epoch_ms(2026, 7, 6, 4, 35)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 35

    def test_late_45_minutes_59_seconds(self, api, seed_employee):
        """10:15:59 with shift_start 09:30 -> floor = 45 minutes late."""
        seed_employee()
        # 10:15:59 IST = 04:45:59 UTC
        ts = _to_epoch_ms(2026, 7, 6, 4, 45, 59)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 45


# ── Punch-Out ────────────────────────────────────────────────────────────────

class TestPunchOut:
    def test_basic_punch_out(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

        # 18:35 IST = 13:05 UTC
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 200
        data = r.json()
        assert data["punch_out"] == ts_out
        assert data["work_hours"] == 9.08  # (13:05 - 04:00) = 9h5m = 9.08
        # 18:35 - 18:30 = 5 min, < 30 threshold -> 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False

    def test_punch_out_no_open_record(self, api, seed_employee):
        seed_employee()
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 404

    def test_punch_out_unknown_employee(self, api):
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": ts_out})
        assert r.status_code == 404

    def test_punch_out_before_punch_in(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 3, 0)  # before punch_in
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code in (404, 422)  # 404 because no record with punch_in <= ts_out

    def test_punch_out_more_than_24h(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 8, 4, 0)  # 48 hours later
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.status_code == 422

    def test_double_punch_out_returns_409(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 0)
        r1 = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r1.status_code == 200
        r2 = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r2.status_code in (404, 409)  # no open record found


# ── Work Hours & Overtime ────────────────────────────────────────────────────

class TestWorkHoursOvertime:
    def test_half_up_rounding(self, api, seed_employee):
        """Verify half-up rounding, not banker's rounding."""
        seed_employee()
        # Create a shift that results in x.xx5 hours to test rounding
        # 9h 5m = 9.0833... hours
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # punch_out at 13:05 UTC = 9h5m = 32700 seconds = 9.083333... hours -> 9.08
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.json()["work_hours"] == 9.08

    def test_overtime_above_30min(self, api, seed_employee):
        """Overtime counted when >= 30 minutes after shift_end."""
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 19:10 IST = 13:40 UTC, shift_end = 18:30 IST = 13:00 UTC
        # diff = 40 min >= 30 -> overtime_minutes = 40
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 40)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.json()["overtime_minutes"] == 40

    def test_overtime_below_30min(self, api, seed_employee):
        """Overtime not counted when < 30 minutes."""
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 18:50 IST = 13:20 UTC, shift_end = 18:30 IST = 13:00 UTC
        # diff = 20 min < 30 -> 0
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 20)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        assert r.json()["overtime_minutes"] == 0

    def test_half_day_under_450(self, api, seed_employee):
        """Work hours < 4.50 -> half_day = true."""
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 4 hours = punch_out at 08:00 UTC
        ts_out = _to_epoch_ms(2026, 7, 6, 8, 0)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        data = r.json()
        assert data["work_hours"] == 4.0
        assert data["half_day"] is True

    def test_not_half_day_at_450(self, api, seed_employee):
        """Work hours == 4.50 -> half_day = false."""
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 4h30m = 4.5 hours -> not half day
        ts_out = _to_epoch_ms(2026, 7, 6, 8, 30)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        data = r.json()
        assert data["work_hours"] == 4.5
        assert data["half_day"] is False


# ── Attendance List ──────────────────────────────────────────────────────────

class TestAttendanceList:
    def test_list_empty(self, api):
        r = api.get("/attendance")
        assert r.status_code == 200
        assert r.json()["items"] == []
        assert r.json()["total"] == 0

    def test_list_with_filters(self, api, seed_employee):
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        ts2 = _to_epoch_ms(2026, 7, 7, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts2})

        r = api.get("/attendance", params={
            "emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"
        })
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_date_from_after_date_to(self, api):
        r = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"})
        assert r.status_code == 422


# ── Regularize (Manual Corrections) ─────────────────────────────────────────

class TestRegularize:
    def test_basic_correction(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 35)  # 10:05 IST, late
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 40)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        # Correct punch_in to 09:28 IST = 03:58 UTC
        new_pi = _to_epoch_ms(2026, 7, 6, 3, 58)
        r = api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pi,
            "reason": "biometric glitch on door scanner",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["punch_in"] == new_pi
        assert data["late_minutes"] == 0  # now within grace
        assert len(data["history"]) == 1
        h = data["history"][0]
        assert h["by"] == "hr.admin"
        assert h["reason"] == "biometric glitch on door scanner"
        assert "punch_in" in h["changes"]
        assert h["changes"]["punch_in"]["from"] == ts_in
        assert h["changes"]["punch_in"]["to"] == new_pi

    def test_correction_no_change(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        r = api.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "no change test that should fail",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_correction_not_found(self, api, seed_employee):
        seed_employee()
        r = api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": _to_epoch_ms(2026, 7, 6, 4, 0),
            "reason": "should not find record",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404

    def test_absent_with_punch_times_rejected(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        r = api.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": ts_in,
            "reason": "should be rejected with 422",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_correction_to_leave(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        r = api.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "LEAVE",
            "reason": "employee was actually on leave",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "LEAVE"
        assert data["punch_in"] is None
        assert data["punch_out"] is None
        assert data["late_minutes"] == 0
        assert data["half_day"] is False


# ── Overnight Shift ──────────────────────────────────────────────────────────

class TestOvernightShift:
    def test_overnight_punch_in_date(self, api, seed_employee):
        """Employee with 22:00-06:00 shift. Punch at 21:55 IST = 16:25 UTC."""
        seed_employee(
            emp_code="EMP0005", name="Meera", email="meera@x.com",
            department="Support", shift_start="22:00", shift_end="06:00",
            joined_on="2026-03-10",
        )
        # 21:55 IST on July 6 = 16:25 UTC
        ts = _to_epoch_ms(2026, 7, 6, 16, 25)
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    def test_overnight_punch_out(self, api, seed_employee):
        """Overnight shift: punch_out next morning."""
        seed_employee(
            emp_code="EMP0005", name="Meera", email="meera@x.com",
            department="Support", shift_start="22:00", shift_end="06:00",
            joined_on="2026-03-10",
        )
        ts_in = _to_epoch_ms(2026, 7, 6, 16, 25)  # 21:55 IST Jul 6
        api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ts_in})

        ts_out = _to_epoch_ms(2026, 7, 7, 1, 10)  # 06:40 IST Jul 7
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ts_out})
        assert r.status_code == 200
        data = r.json()
        assert data["work_hours"] == 8.75
        # overtime: 06:40 - 06:00 = 40 min >= 30 -> 40
        assert data["overtime_minutes"] == 40


# ── Analytics ────────────────────────────────────────────────────────────────

class TestEmployeeMonthly:
    def test_unknown_employee(self, api):
        r = api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
        assert r.status_code == 404

    def test_empty_month(self, api, seed_employee):
        seed_employee()
        r = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-01"})
        assert r.status_code == 200
        data = r.json()
        assert data["present_days"] == 0.0
        assert data["late_count"] == 0

    def test_monthly_with_data(self, api, seed_employee):
        seed_employee()
        # Create a full day attendance on Mon Jul 6, 2026
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        r = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["present_days"] == 1.0
        assert data["working_days"] > 0


class TestDepartmentSummary:
    def test_empty_month(self, api, seed_employee):
        seed_employee()
        r = api.get("/analytics/departments/summary", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) >= 1  # at least one dept
        # Engineering has headcount of 1
        eng = next((i for i in data["items"] if i["department"] == "Engineering"), None)
        assert eng is not None
        assert eng["headcount"] == 1


class TestLeaderboard:
    def test_empty_month(self, api, seed_employee):
        seed_employee()
        r = api.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_leaderboard_ranking(self, api, seed_employee):
        seed_employee(emp_code="EMP0001", name="A", email="a@a.com")
        seed_employee(emp_code="EMP0002", name="B", email="b@b.com")
        seed_employee(emp_code="EMP0003", name="C", email="c@c.com", department="Sales")

        # EMP0001: late 35 min (10:05 IST, shift 09:30)
        ts = _to_epoch_ms(2026, 7, 6, 4, 35)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})

        # EMP0002: late 35 min (tied with EMP0001)
        ts2 = _to_epoch_ms(2026, 7, 6, 4, 35)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ts2})

        # EMP0003: late 50 min (10:20 IST, shift 09:30) -> highest
        ts3 = _to_epoch_ms(2026, 7, 6, 4, 50)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ts3})

        r = api.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3
        # Standard competition ranking: EMP0003 rank 1 (50 min), EMP0001/EMP0002 tied at rank 2
        assert items[0]["rank"] == 1
        assert items[0]["total_late_minutes"] == 50
        assert items[1]["rank"] == 2
        assert items[2]["rank"] == 2


class TestDepartmentTrend:
    def test_unknown_department(self, api):
        r = api.get("/analytics/departments/FakeDept/trend", params={
            "from": "2026-07-06", "to": "2026-07-07"
        })
        assert r.status_code == 404

    def test_to_before_from(self, api, seed_employee):
        seed_employee()
        r = api.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-07", "to": "2026-07-06"
        })
        assert r.status_code == 422

    def test_range_exceeds_92_days(self, api, seed_employee):
        seed_employee()
        r = api.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-01-01", "to": "2026-07-01"
        })
        assert r.status_code == 422

    def test_basic_trend(self, api, seed_employee):
        seed_employee()
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        r = api.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-06", "to": "2026-07-08"
        })
        assert r.status_code == 200
        items = r.json()["items"]
        # Should have 3 days: Jul 6 (Mon), Jul 7 (Tue), Jul 8 (Wed)
        assert len(items) == 3
        assert items[0]["date"] == "2026-07-06"
        assert items[0]["present_count"] == 1.0
        assert items[0]["headcount"] == 1

    def test_includes_weekends(self, api, seed_employee):
        seed_employee()
        # Jul 11 (Sat), Jul 12 (Sun) should be included
        r = api.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-10", "to": "2026-07-13"
        })
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 4
        # Jul 11 is Saturday
        sat = next(i for i in items if i["date"] == "2026-07-11")
        assert sat["is_working_day"] is False
        assert sat["attendance_rate"] is None  # not a working day


# ── Admin Explain ────────────────────────────────────────────────────────────

class TestAdminExplain:
    def test_explain_attendance_list(self, api, seed_employee):
        seed_employee()
        r = api.get("/admin/explain/attendance_list", params={"emp_code": "EMP0001"})
        assert r.status_code == 200
        data = r.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data

    def test_explain_invalid_endpoint(self, api):
        r = api.get("/admin/explain/invalid_endpoint")
        assert r.status_code == 422

    def test_explain_employee_monthly(self, api, seed_employee):
        seed_employee()
        r = api.get("/admin/explain/employee_monthly", params={
            "emp_code": "EMP0001", "month": "2026-07"
        })
        assert r.status_code == 200

    def test_explain_late_leaderboard(self, api, seed_employee):
        seed_employee()
        r = api.get("/admin/explain/late_leaderboard", params={"month": "2026-07"})
        assert r.status_code == 200


# ── Concurrency ──────────────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_punch_in(self, api, seed_employee):
        """Two concurrent punch-ins should yield exactly one 201 and one 409."""
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)

        results = []
        def do_punch():
            with TestClient(app) as c:
                return c.post("/attendance/punch-in", json={
                    "emp_code": "EMP0001", "punched_at": ts
                })

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_punch) for _ in range(2)]
            results = [f.result() for f in futures]

        codes = sorted([r.status_code for r in results])
        assert codes == [201, 409]

    def test_concurrent_employee_create(self, api):
        """Two concurrent employee creates should yield exactly one 201 and one 409."""
        results = []
        def do_create():
            with TestClient(app) as c:
                return c.post("/employees", json={
                    "emp_code": "EMP0001", "name": "Test", "email": "t@t.com",
                    "department": "Eng", "joined_on": "2026-01-01",
                })

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_create) for _ in range(2)]
            results = [f.result() for f in futures]

        codes = sorted([r.status_code for r in results])
        assert codes == [201, 409]


# ── Epoch Milliseconds Validation ────────────────────────────────────────────

class TestEpochValidation:
    def test_punch_in_with_seconds_value_rejected(self, api, seed_employee):
        """Values < 100000000000 (seconds-looking) should be rejected."""
        seed_employee()
        r = api.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": 1000000000  # looks like seconds
        })
        assert r.status_code == 422

    def test_punch_in_with_valid_epoch_ms(self, api, seed_employee):
        """A valid epoch ms should work."""
        seed_employee()
        ts = _to_epoch_ms(2026, 7, 6, 4, 0)
        assert ts >= 100_000_000_000
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
        assert r.status_code == 201


# ── Sample Data Verification ─────────────────────────────────────────────────

class TestSampleData:
    def test_sample_normal_full_day(self, api, seed_employee):
        """Verify sample 1: 09:28 IST in, 18:35 IST out -> work_hours 9.12, late 0, overtime 0."""
        seed_employee()
        # 09:28 IST = 03:58 UTC
        ts_in = _to_epoch_ms(2026, 7, 6, 3, 58)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 18:35 IST = 13:05 UTC
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 5)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        data = r.json()
        assert data["work_hours"] == 9.12
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False

    def test_sample_late_with_overtime(self, api, seed_employee):
        """Sample 2: 10:05 IST in, 19:10 IST out -> late 35, overtime 40."""
        seed_employee()
        # 10:05 IST = 04:35 UTC
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 35)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        # 19:10 IST = 13:40 UTC
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 40)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        data = r.json()
        assert data["work_hours"] == 9.08
        assert data["late_minutes"] == 35
        assert data["overtime_minutes"] == 40
        assert data["half_day"] is False

    def test_sample_half_day_wfh(self, api, seed_employee):
        """Sample 4: 3.5 hours worked -> half_day true."""
        seed_employee()
        # 09:30 IST = 04:00 UTC
        ts_in = _to_epoch_ms(2026, 7, 7, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in, "status": "WFH"})
        # 13:00 IST = 07:30 UTC -> 3.5 hours
        ts_out = _to_epoch_ms(2026, 7, 7, 7, 30)
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
        data = r.json()
        assert data["work_hours"] == 3.5
        assert data["half_day"] is True
