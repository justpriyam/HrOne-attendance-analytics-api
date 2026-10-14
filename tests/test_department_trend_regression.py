"""
Focused regression tests for the refactored department_trend endpoint.
Verifies pure MongoDB aggregation pipeline behavior:
- Weekdays and weekends handling
- Missing records gap-filled by $densify
- Employees joining mid-period (dynamic headcount)
- Empty attendance dataset
- Moving-average boundaries and window constraints
"""

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app, db


def _to_epoch_ms(year, month, day, hour, minute, second=0):
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@pytest.fixture(autouse=True)
def clean_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def api():
    with TestClient(app) as c:
        yield c


class TestDepartmentTrendRegression:
    def test_weekdays_and_weekends_semantics(self, api):
        """
        Verify that:
        - Mon-Fri have is_working_day=True
        - Sat-Sun have is_working_day=False
        - Weekend attendance_rate is null even if presence occurs
        - Weekend presence still counts towards present_count
        """
        # Create 1 employee in Engineering
        api.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        })

        # Saturday punch (2026-07-04)
        ts_in_sat = _to_epoch_ms(2026, 7, 4, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_sat})
        ts_out_sat = _to_epoch_ms(2026, 7, 4, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_sat})

        # Friday punch (2026-07-03)
        ts_in_fri = _to_epoch_ms(2026, 7, 3, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_fri})
        ts_out_fri = _to_epoch_ms(2026, 7, 3, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_fri})

        # Query Friday to Sunday: 2026-07-03 to 2026-07-05
        r = api.get("/analytics/departments/Engineering/trend?from=2026-07-03&to=2026-07-05")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3

        # Friday
        assert items[0]["date"] == "2026-07-03"
        assert items[0]["is_working_day"] is True
        assert items[0]["present_count"] == 1.0
        assert items[0]["attendance_rate"] == 1.0

        # Saturday
        assert items[1]["date"] == "2026-07-04"
        assert items[1]["is_working_day"] is False
        assert items[1]["present_count"] == 1.0  # Present count recorded
        assert items[1]["attendance_rate"] is None  # But rate is null

        # Sunday
        assert items[2]["date"] == "2026-07-05"
        assert items[2]["is_working_day"] is False
        assert items[2]["present_count"] == 0.0
        assert items[2]["attendance_rate"] is None

    def test_missing_records_gap_filling(self, api):
        """
        Verify that missing days with no records are gap-filled by $densify:
        - Exactly one row per calendar day
        - Working days with 0 records have present_count=0, late_count=0, attendance_rate=0.0
        """
        api.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        })

        # Only punch on Monday 2026-07-06
        ts_in = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
        ts_out = _to_epoch_ms(2026, 7, 6, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

        # Query Wednesday 2026-07-01 to Friday 2026-07-10 (10 calendar days)
        r = api.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 10

        # Check gap-filled day (e.g. 2026-07-01, 2026-07-02, 2026-07-07)
        day_07_01 = next(item for item in items if item["date"] == "2026-07-01")
        assert day_07_01["is_working_day"] is True
        assert day_07_01["headcount"] == 1
        assert day_07_01["present_count"] == 0.0
        assert day_07_01["late_count"] == 0
        assert day_07_01["attendance_rate"] == 0.0

        day_07_06 = next(item for item in items if item["date"] == "2026-07-06")
        assert day_07_06["present_count"] == 1.0
        assert day_07_06["attendance_rate"] == 1.0

    def test_employees_joining_mid_period(self, api):
        """
        Verify Rule R9:
        - Emp1 joins 2026-07-01
        - Emp2 joins 2026-07-06 (mid-period)
        - Headcount before 07-06 is 1, on and after 07-06 is 2
        - Attendance rate denominators adjust accordingly
        """
        api.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        })
        api.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-06",
        })

        # On 2026-07-03 (Friday): EMP0001 punches in
        ts_in_03 = _to_epoch_ms(2026, 7, 3, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_03})
        ts_out_03 = _to_epoch_ms(2026, 7, 3, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_03})

        # On 2026-07-06 (Monday): EMP0001 punches in, EMP0002 does not
        ts_in_06 = _to_epoch_ms(2026, 7, 6, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_06})
        ts_out_06 = _to_epoch_ms(2026, 7, 6, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_06})

        r = api.get("/analytics/departments/Sales/trend?from=2026-07-03&to=2026-07-07")
        assert r.status_code == 200
        items = r.json()["items"]

        # 2026-07-03: headcount=1, present=1.0 -> rate=1.0
        row_03 = next(i for i in items if i["date"] == "2026-07-03")
        assert row_03["headcount"] == 1
        assert row_03["present_count"] == 1.0
        assert row_03["attendance_rate"] == 1.0

        # 2026-07-06: headcount=2, present=1.0 -> rate=0.5
        row_06 = next(i for i in items if i["date"] == "2026-07-06")
        assert row_06["headcount"] == 2
        assert row_06["present_count"] == 1.0
        assert row_06["attendance_rate"] == 0.5

    def test_empty_attendance_dataset(self, api):
        """
        Verify department with employees but zero attendance logs:
        - Trend returns all days
        - Working days have headcount=N, present_count=0.0, late_count=0, attendance_rate=0.0
        - moving_avg_7d=0.0
        """
        api.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Karan Bedi",
            "email": "karan@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        r = api.get("/analytics/departments/Support/trend?from=2026-07-01&to=2026-07-05")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 5

        for item in items:
            assert item["headcount"] == 1
            assert item["present_count"] == 0.0
            assert item["late_count"] == 0
            if item["is_working_day"]:
                assert item["attendance_rate"] == 0.0
                assert item["moving_avg_7d"] == 0.0
            else:
                assert item["attendance_rate"] is None

    def test_moving_average_boundaries_and_window(self, api):
        """
        Verify 7-day moving average window:
        - Shorter window at beginning of range (e.g. day 1 uses 1 day, day 2 uses 2 days)
        - Window slides up to 6 preceding rows + current row
        - Ignores weekends (null rates) without distorting the denominator
        - Exactly matches spec's 4 decimal half-up precision
        """
        api.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        })

        # Day 1 (Wednesday 2026-07-01): rate = 1.0
        ts_in_1 = _to_epoch_ms(2026, 7, 1, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_1})
        ts_out_1 = _to_epoch_ms(2026, 7, 1, 13, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_1})

        # Day 2 (Thursday 2026-07-02): half day WFH -> rate = 0.5
        ts_in_2 = _to_epoch_ms(2026, 7, 2, 4, 0)
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in_2, "status": "WFH"})
        ts_out_2 = _to_epoch_ms(2026, 7, 2, 8, 0)
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_2})

        # Day 3 (Friday 2026-07-03): no punch -> rate = 0.0

        r = api.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-05")
        assert r.status_code == 200
        items = r.json()["items"]

        # Day 1: [1.0] -> 1.0
        assert items[0]["date"] == "2026-07-01"
        assert items[0]["attendance_rate"] == 1.0
        assert items[0]["moving_avg_7d"] == 1.0

        # Day 2: [1.0, 0.5] -> 0.75
        assert items[1]["date"] == "2026-07-02"
        assert items[1]["attendance_rate"] == 0.5
        assert items[1]["moving_avg_7d"] == 0.75

        # Day 3: [1.0, 0.5, 0.0] -> 0.5
        assert items[2]["date"] == "2026-07-03"
        assert items[2]["attendance_rate"] == 0.0
        assert items[2]["moving_avg_7d"] == 0.5

        # Day 4 (Saturday): rate=None, window non-nulls=[1.0, 0.5, 0.0] -> 0.5
        assert items[3]["date"] == "2026-07-04"
        assert items[3]["attendance_rate"] is None
        assert items[3]["moving_avg_7d"] == 0.5

        # Day 5 (Sunday): rate=None, window non-nulls=[1.0, 0.5, 0.0] -> 0.5
        assert items[4]["date"] == "2026-07-05"
        assert items[4]["attendance_rate"] is None
        assert items[4]["moving_avg_7d"] == 0.5
