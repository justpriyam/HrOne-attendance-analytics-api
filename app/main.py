"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Rebuilt from the starter code. Every defect listed in REVIEW.md has been fixed.
"""

import calendar
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Lifespan: create indexes at startup
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Indexes - idempotent
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index("status")
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000  # minimum valid epoch ms
EPOCH_MAX = 4_102_444_800_000  # maximum valid epoch ms
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PUNCH_IN_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _round2(val: float) -> float:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round4(val: float) -> float:
    """Round to 4 decimal places using ROUND_HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _validate_epoch_ms(v: int) -> int:
    """Validate that a value is a proper epoch milliseconds integer."""
    if not isinstance(v, int):
        raise HTTPException(422, detail="timestamp must be an integer")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise HTTPException(422, detail=f"timestamp out of range")
    return v


def _epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime, truncated to whole seconds (R1)."""
    # Truncate to whole seconds
    ms_truncated = (ms // 1000) * 1000
    return datetime.fromtimestamp(ms_truncated / 1000, tz=timezone.utc)


def _dt_to_epoch_ms(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime has UTC tzinfo (pymongo may return naive datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ist_date_str(dt: datetime) -> str:
    """Get the IST date as YYYY-MM-DD string from a UTC datetime."""
    dt = _ensure_utc(dt)
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%Y-%m-%d")


def _parse_shift_time(time_str: str) -> tuple[int, int]:
    """Parse HH:MM string into (hour, minute) tuple."""
    h, m = map(int, time_str.split(":"))
    return h, m


def _is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift is overnight (shift_end <= shift_start)."""
    sh, sm = _parse_shift_time(shift_start)
    eh, em = _parse_shift_time(shift_end)
    return (eh * 60 + em) <= (sh * 60 + sm)


def _get_attendance_date(punch_in_utc: datetime, emp: dict) -> str:
    """
    Determine the attendance date (R1).
    For normal shifts: IST date of punch_in.
    For overnight shifts: if punch_in IST time is earlier than shift_end, it belongs
    to the previous day's shift.
    """
    punch_in_utc = _ensure_utc(punch_in_utc)
    ist_dt = punch_in_utc.astimezone(IST)
    date_str = ist_dt.strftime("%Y-%m-%d")

    if _is_overnight_shift(emp["shift_start"], emp["shift_end"]):
        eh, em = _parse_shift_time(emp["shift_end"])
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute
        end_minutes = eh * 60 + em
        if punch_time_minutes < end_minutes:
            # This punch-in is before the shift_end time, so it belongs to the previous day
            prev_day = ist_dt - timedelta(days=1)
            date_str = prev_day.strftime("%Y-%m-%d")

    return date_str


def _compute_late_minutes(punch_in_utc: datetime, emp: dict, date_str: str) -> int:
    """
    Compute late minutes per R2.
    Grace period is 10 minutes. Late only if strictly > 10:00 after shift_start.
    When late, late_minutes = floor(minutes since shift_start).
    """
    punch_in_utc = _ensure_utc(punch_in_utc)
    sh, sm = _parse_shift_time(emp["shift_start"])

    # Build shift start datetime in IST for the attendance date
    year, month, day = map(int, date_str.split("-"))
    shift_start_ist = datetime(year, month, day, sh, sm, 0, tzinfo=IST)

    # For overnight shifts, if punch_in is on the next calendar day, adjust
    if _is_overnight_shift(emp["shift_start"], emp["shift_end"]):
        punch_ist = punch_in_utc.astimezone(IST)
        eh, em = _parse_shift_time(emp["shift_end"])
        punch_time_minutes = punch_ist.hour * 60 + punch_ist.minute
        end_minutes = eh * 60 + em
        if punch_time_minutes < end_minutes:
            # punch is on the next day, shift_start was the previous day
            pass  # shift_start_ist is correct (it's on the attendance date)

    diff_seconds = (punch_in_utc - shift_start_ist.astimezone(timezone.utc)).total_seconds()
    grace_seconds = 10 * 60  # 10 minutes

    if diff_seconds > grace_seconds:
        return int(diff_seconds // 60)  # floor
    return 0


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Compute work hours rounded half-up to 2 decimals (R4)."""
    punch_in = _ensure_utc(punch_in)
    punch_out = _ensure_utc(punch_out)
    seconds = (punch_out - punch_in).total_seconds()
    hours = seconds / 3600
    return _round2(hours)


def _compute_overtime_minutes(punch_out_utc: datetime, emp: dict, date_str: str) -> int:
    """
    Compute overtime minutes per R3.
    Floor minutes between shift_end and punch_out, only if >= 30.
    """
    punch_out_utc = _ensure_utc(punch_out_utc)
    eh, em = _parse_shift_time(emp["shift_end"])
    year, month, day = map(int, date_str.split("-"))

    shift_end_ist = datetime(year, month, day, eh, em, 0, tzinfo=IST)

    # For overnight shifts, shift_end falls on the next calendar day
    if _is_overnight_shift(emp["shift_start"], emp["shift_end"]):
        shift_end_ist += timedelta(days=1)

    diff_seconds = (punch_out_utc - shift_end_ist.astimezone(timezone.utc)).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = int(diff_seconds // 60)  # floor
    return minutes if minutes >= 30 else 0


def _compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _record_to_response(doc: dict) -> dict:
    """Convert a MongoDB attendance_logs document to API response format."""
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _dt_to_epoch_ms(doc.get("punch_in")) if doc.get("punch_in") else None,
        "punch_out": _dt_to_epoch_ms(doc.get("punch_out")) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for entry in doc.get("history", []):
        h = {
            "at": _dt_to_epoch_ms(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": {},
        }
        for field_name, change in entry.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            # Convert BSON dates in changes to epoch ms
            if field_name in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = _dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = _dt_to_epoch_ms(to_val)
            h["changes"][field_name] = {"from": from_val, "to": to_val}
        result["history"].append(h)
    return result


def _employee_to_response(doc: dict) -> dict:
    """Convert a MongoDB employee document to API response format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": _dt_to_epoch_ms(doc.get("created_at")),
    }


def _count_working_days(year: int, month: int, joined_on: str = None) -> int:
    """Count Mon-Fri working days in a month, optionally starting from joined_on."""
    import calendar as cal
    _, last_day = cal.monthrange(year, month)
    start_day = 1
    if joined_on:
        j_parts = joined_on.split("-")
        j_year, j_month, j_day = int(j_parts[0]), int(j_parts[1]), int(j_parts[2])
        if j_year > year or (j_year == year and j_month > month):
            return 0  # joined after this month
        if j_year == year and j_month == month:
            start_day = j_day

    count = 0
    from datetime import date
    for d in range(start_day, last_day + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
    return count


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("must be HH:MM (24-hour)")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PUNCH_IN_STATUSES:
            raise ValueError(f"status must be one of {PUNCH_IN_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB not available")


# ── Employees ────────────────────────────────────────────────────────────────

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return _employee_to_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_employee_to_response(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Attendance ───────────────────────────────────────────────────────────────

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    if body.punched_at is not None:
        _validate_epoch_ms(body.punched_at)
        ts = _epoch_ms_to_dt(body.punched_at)
    else:
        # Default to now, truncated to whole seconds
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)

    date_str = _get_attendance_date(ts, emp)
    late_minutes = _compute_late_minutes(ts, emp, date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    doc.pop("_id", None)
    return _record_to_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    if body.punched_at is not None:
        _validate_epoch_ms(body.punched_at)
        ts = _epoch_ms_to_dt(body.punched_at)
    else:
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at and punch_out is null
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, detail="no open punch-in found")

    punch_in_dt = _ensure_utc(record["punch_in"])

    # Validate: punched_at must be > punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, detail="punch_out must be after punch_in")

    # Validate: no more than 24 hours
    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punch_out more than 24 hours after punch_in")

    work_hours = _compute_work_hours(punch_in_dt, ts)
    overtime = _compute_overtime_minutes(ts, emp, record["date"])
    half_day = _compute_half_day(work_hours)

    # Atomic update: only succeed if punch_out is still None (concurrency)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _record_to_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate date_from > date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"invalid status")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_record_to_response(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Regularize (Manual Correction) ──────────────────────────────────────────

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="no attendance record for this date")

    new_status = body.status if body.status is not None else record["status"]
    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    # If setting to ABSENT or LEAVE, punch times must be cleared
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="cannot supply punch times with ABSENT/LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires a punch_in
        if body.punch_in is not None:
            _validate_epoch_ms(body.punch_in)
            new_punch_in = _epoch_ms_to_dt(body.punch_in)
        if body.punch_out is not None:
            _validate_epoch_ms(body.punch_out)
            new_punch_out = _epoch_ms_to_dt(body.punch_out)

        # Presence status requires punch_in to exist
        if new_punch_in is None:
            raise HTTPException(422, detail="presence status requires punch_in")

        # Validate punch_in stays on the attendance date (R1)
        if body.punch_in is not None:
            pi_date = _get_attendance_date(new_punch_in, emp)
            if pi_date != date:
                raise HTTPException(422, detail="punch_in must be on the attendance date")

        # Validate punch_out > punch_in and within 24h
        if new_punch_out is not None:
            pi = _ensure_utc(new_punch_in)
            po = _ensure_utc(new_punch_out)
            if po <= pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (po - pi).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail="punch_out more than 24 hours after punch_in")

    # Compute new derived values
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    elif new_punch_in is not None:
        new_punch_in_utc = _ensure_utc(new_punch_in)
        new_late_minutes = _compute_late_minutes(new_punch_in_utc, emp, date)
        if new_punch_out is not None:
            new_punch_out_utc = _ensure_utc(new_punch_out)
            new_work_hours = _compute_work_hours(new_punch_in_utc, new_punch_out_utc)
            new_overtime = _compute_overtime_minutes(new_punch_out_utc, emp, date)
            new_half_day = _compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False
    else:
        new_late_minutes = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False

    # Build changes dict: only fields that actually changed
    changes = {}
    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    # Compare punch_in (both as epoch ms for comparison)
    old_pi_ms = _dt_to_epoch_ms(old_punch_in) if old_punch_in else None
    new_pi_ms = _dt_to_epoch_ms(new_punch_in) if new_punch_in else None
    if old_pi_ms != new_pi_ms:
        # Store as BSON dates in MongoDB, convert to epoch ms in response
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    old_po_ms = _dt_to_epoch_ms(old_punch_out) if old_punch_out else None
    new_po_ms = _dt_to_epoch_ms(new_punch_out) if new_punch_out else None
    if old_po_ms != new_po_ms:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}

    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(422, detail="no fields changed")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    # Use the current history length as a guard against concurrent corrections
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        update_doc,
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="concurrent modification detected")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return _record_to_response(updated)


# ── Analytics ────────────────────────────────────────────────────────────────

def _validate_month(month: str) -> tuple[int, int]:
    """Parse and validate YYYY-MM month string. Returns (year, month_int)."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(422, detail="month must be YYYY-MM")
    parts = month.split("-")
    return int(parts[0]), int(parts[1])


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    year, mon = _validate_month(month)
    _, last_day = calendar.monthrange(year, mon)
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

    # Working days (R7): count from joined_on
    working_days = _count_working_days(year, mon, emp.get("joined_on"))

    # Build pure aggregation pipeline
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if not results:
        # No records for this month
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": _round2(0.0) if working_days > 0 else None,
        }

    data = results[0]
    present_days = data["present_days"]
    attendance_pct = None
    if working_days > 0:
        attendance_pct = _round2((present_days / working_days) * 100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mon = _validate_month(month)
    _, last_day = calendar.monthrange(year, mon)
    month_end_str = f"{year:04d}-{mon:02d}-{last_day:02d}"
    month_start_str = f"{year:04d}-{mon:02d}-01"

    match_emp: dict = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                                ]
                            },
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_list"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        if r["headcount"] == 0:
            continue
        avg_wh = None
        if r.get("avg_work_hours") is not None:
            avg_wh = _round2(r["avg_work_hours"])
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = _validate_month(month)
    _, last_day = calendar.monthrange(year, mon)
    month_start_str = f"{year:04d}-{mon:02d}-01"
    month_end_str = f"{year:04d}-{mon:02d}-{last_day:02d}"

    match_stage: dict = {"date": {"$gte": month_start_str, "$lte": month_end_str}}

    # Build aggregation pipeline with $rank window function
    pipeline = [
        {"$match": match_stage},
        # Only records with late_minutes > 0
        {"$match": {"late_minutes": {"$gt": 0}}},
        # Group by emp_code
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Only employees with total > 0 (already filtered, but be safe)
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Lookup employee details
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        # Unwind employee info (ignore if employee doesn't exist)
        {"$unwind": {"path": "$emp_info"}},
    ]

    # Filter by department if specified (from employees collection)
    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    # Use $setWindowFields for standard competition ranking ($rank)
    # $rank requires exactly one sort key
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        # Filter: rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Sort: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Project output
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    # Validate department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, detail="unknown department")

    # Validate dates
    try:
        from datetime import date as dt_date
        from_date = dt_date.fromisoformat(date_from)
        to_date = dt_date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, detail="invalid date format")

    if to_date < from_date:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    if (to_date - from_date).days > 92:
        raise HTTPException(422, detail="date range must not exceed 92 days")

    # Get all employees in this department
    all_employees = list(db.employees.find({"department": department}))

    # Get attendance logs for the date range
    emp_codes = [e["emp_code"] for e in all_employees]
    logs = list(
        db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        )
    )

    # Index logs by date
    logs_by_date: dict[str, list] = {}
    for log in logs:
        d = log["date"]
        if d not in logs_by_date:
            logs_by_date[d] = []
        logs_by_date[d].append(log)

    # Generate all calendar days
    from datetime import date as dt_date
    items = []
    current = from_date
    attendance_rates = []  # for moving average

    while current <= to_date:
        date_str = current.isoformat()
        is_working_day = current.weekday() < 5

        # Headcount: employees with joined_on <= current day (R9)
        headcount = sum(1 for e in all_employees if e["joined_on"] <= date_str)

        day_logs = logs_by_date.get(date_str, [])

        # present_count: presence-status records, half_day = 0.5
        present_count = 0.0
        late_count = 0
        for log in day_logs:
            status = log.get("status", "")
            if status in PRESENCE_STATUSES:
                if log.get("half_day", False):
                    present_count += 0.5
                else:
                    present_count += 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = _round4(present_count / headcount)
        else:
            attendance_rate = None

        attendance_rates.append(attendance_rate)

        # moving_avg_7d: mean of non-null attendance_rate in last 7 rows
        window = attendance_rates[-7:]
        non_null = [r for r in window if r is not None]
        moving_avg = _round4(sum(non_null) / len(non_null)) if non_null else None

        items.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# ── Admin Explain ────────────────────────────────────────────────────────────

def _sanitize_explain(obj: Any) -> Any:
    """Recursively convert BSON types to JSON-safe representations."""
    if isinstance(obj, dict):
        return {k: _sanitize_explain(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_explain(v) for v in obj]
    elif isinstance(obj, bytes):
        return obj.hex()
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, '__str__') and type(obj).__name__ in ('ObjectId', 'Binary', 'Code', 'Regex', 'Decimal128'):
        return str(obj)
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    # All possible query params from the various endpoints
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        explain = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip((page - 1) * page_size).limit(page_size).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _sanitize_explain(explain)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        year, mon = _validate_month(month)
        _, last_day = calendar.monthrange(year, mon)
        month_start = f"{year:04d}-{mon:02d}-01"
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$group": {
                    "_id": None,
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _sanitize_explain(explain)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        year, mon = _validate_month(month)
        _, last_day = calendar.monthrange(year, mon)
        month_start = f"{year:04d}-{mon:02d}-01"
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

        match_q: dict = {"date": {"$gte": month_start, "$lte": month_end}}
        if department:
            emp_codes = [
                e["emp_code"]
                for e in db.employees.find({"department": department}, {"emp_code": 1})
            ]
            match_q["emp_code"] = {"$in": emp_codes}

        pipeline = [
            {"$match": match_q},
            {
                "$group": {
                    "_id": "$emp_code",
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }
            },
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _sanitize_explain(explain)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        year, mon = _validate_month(month)
        _, last_day = calendar.monthrange(year, mon)
        month_start = f"{year:04d}-{mon:02d}-01"
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

        match_stage: dict = {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0},
        }

        pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
        ]
        if department:
            pipeline.append({"$match": {"emp_info.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
        ])

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _sanitize_explain(explain)}

    elif endpoint == "department_trend":
        if not department or not trend_from or not trend_to:
            raise HTTPException(422, detail="department, from, and to are required")

        emp_codes = [
            e["emp_code"]
            for e in db.employees.find({"department": department}, {"emp_code": 1})
        ]
        if not emp_codes:
            raise HTTPException(404, detail="unknown department")

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": trend_from, "$lte": trend_to},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                }
            },
        ]
        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _sanitize_explain(explain)}

