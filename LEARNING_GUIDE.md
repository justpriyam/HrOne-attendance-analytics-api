# LEARNING_GUIDE.md — HROne Architecture Walkthrough

## 1. Architecture Overview

```
Client (curl / browser / grader)
        │
        ▼
   FastAPI App  (app/main.py)
   ├── Pydantic models: validate requests
   ├── Helper functions: business rules R1-R10
   ├── Endpoints: sync functions in threadpool
   └── PyMongo: sync MongoDB driver
        │
        ▼
   MongoDB 6.0+
   ├── employees collection
   └── attendance_logs collection
```

**Key design choice:** Synchronous PyMongo, not async Motor. FastAPI runs sync endpoint functions in a threadpool automatically. This avoids async complexity while keeping the API responsive.

## 2. Endpoint Reference

### 2.1 Health (`GET /health`)
- Calls `client.admin.command("ping")` to verify MongoDB
- Returns 200 `{"status": "ok"}` or 503 on failure
- **Why this matters:** The grader polls this before running tests

### 2.2 Employees (`POST /employees`, `GET /employees`)
- **Create:** Inserts directly; catches `DuplicateKeyError` for 409
- **List:** Server-side sort by `emp_code`, skip/limit pagination
- **Important:** `created_at` is returned as epoch milliseconds, not a datetime string
- **Concurrency:** Unique index on `emp_code` prevents races

### 2.3 Punch-In (`POST /attendance/punch-in`)
1. Validate `emp_code` exists (404 if not)
2. Validate `punched_at` is valid epoch ms (422 if not)
3. Truncate to whole seconds (R1)
4. Calculate attendance date using IST conversion + overnight shift logic
5. Calculate `late_minutes` using R2
6. Insert with compound unique index catch for duplicate prevention

### 2.4 Punch-Out (`POST /attendance/punch-out`)
1. Find the most recent record with `punch_in <= punched_at` and `punch_out: null`
2. Validate: `punched_at > punch_in` and `<= 24 hours` after
3. Compute `work_hours` (R4), `overtime_minutes` (R3), `half_day` (R5)
4. **Atomic update:** `update_one` with `{"punch_out": None}` in the filter — only succeeds if no one else punched out first

### 2.5 Attendance List (`GET /attendance`)
- Server-side sort: `date` DESC, `emp_code` ASC
- Server-side pagination with `skip()` and `limit()`
- Never loads entire collection into memory

### 2.6 Regularize (`PATCH /attendance/{emp_code}/{date}`)
- Validates: ABSENT/LEAVE can't have punch times; presence status needs punch_in
- Recomputes all derived fields from final punch times
- Builds `changes` dict with only the fields that actually changed
- Uses `$set` + `$push` in one atomic update
- **Concurrency guard:** Checks `history: {$size: N}` to detect concurrent edits

### 2.7 Employee Monthly (`GET /analytics/employees/{emp_code}/monthly`)
- Aggregation pipeline: `$match` → `$group`
- Working days calculated in Python (Mon-Fri from `joined_on`)
- `present_days`: half_day counts as 0.5, weekday records only
- `attendance_pct = present_days / working_days × 100`, 2 decimals half-up

### 2.8 Department Summary (`GET /analytics/departments/summary`)
- Headcount from `employees` collection (`joined_on <= month_end`)
- Attendance logs queried by `emp_code` list per department
- `avg_work_hours`: average over presence-status records that have `work_hours`

### 2.9 Late Leaderboard (`GET /analytics/leaderboard/late`)
- Pipeline: `$match` → `$group` by emp_code → `$lookup` employees → `$setWindowFields` with `$rank`
- **Standard competition ranking:** ties share rank, next is skipped (1, 2, 2, 4)
- `$rank` requires exactly one sort field in MongoDB
- `limit` applies after ranking: `rank <= limit`

### 2.10 Department Trend (`GET /analytics/departments/{department}/trend`)
- Generates all calendar days (including weekends) from Python
- Attendance data grouped by date from MongoDB
- `attendance_rate` only on working days with headcount > 0
- `moving_avg_7d`: mean of non-null rates in the last 7 rows within the range

### 2.11 Admin Explain (`GET /admin/explain/{endpoint}`)
- Runs the same query/pipeline as the real endpoint with `explain=True`
- Grader checks for `IXSCAN` (index scan) and no `COLLSCAN`

## 3. Business Rules Deep Dive

### R1: Attendance Date (IST)
```python
# Convert UTC punch-in to IST
ist_dt = punch_in_utc.astimezone(IST)
date_str = ist_dt.strftime("%Y-%m-%d")

# Overnight shift exception: if punch_in IST time < shift_end,
# it belongs to the previous day's shift
if is_overnight and punch_ist_time < shift_end_time:
    date_str = (ist_dt - timedelta(days=1)).strftime("%Y-%m-%d")
```

### R2: Late Detection
```
shift_start = 09:30 IST
grace_end = shift_start + 10 minutes = 09:40 IST

punch_in at 09:40:00 → NOT late (exactly 10 min = within grace)
punch_in at 09:40:01 → LATE, late_minutes = floor(601/60) = 10
punch_in at 10:15:59 → LATE, late_minutes = floor(2759/60) = 45
```

### R3: Overtime
```
shift_end = 18:30 IST
punch_out at 19:00 → 30 min ≥ 30 → overtime_minutes = 30
punch_out at 18:50 → 20 min < 30 → overtime_minutes = 0
```

### R4: Work Hours (Half-Up Rounding)
```python
from decimal import Decimal, ROUND_HALF_UP
hours = seconds / 3600
result = float(Decimal(str(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
# round(8.845, 2) = 8.84 (Python default, WRONG)
# Decimal("8.845").quantize(...) = 8.85 (half-up, CORRECT)
```

### R5: Half Day
```
half_day = True when rounded work_hours < 4.50
half_day counts as 0.5 of a present day
```

## 4. Indexes

| Collection | Index | Purpose |
|---|---|---|
| `employees` | `emp_code` (unique) | Prevent duplicate employees; fast lookup by emp_code |
| `employees` | `department` | Department filtering in list and analytics |
| `attendance_logs` | `(emp_code, date)` compound unique | One record per employee per day; atomic duplicate prevention |
| `attendance_logs` | `date` | Date range queries in attendance list and analytics |
| `attendance_logs` | `emp_code` | Employee lookup in analytics |
| `attendance_logs` | `status` | Status filtering |

## 5. Concurrency Patterns

### Pattern 1: Insert with Unique Index
```python
try:
    db.collection.insert_one(doc)
except DuplicateKeyError:
    raise HTTPException(409, "duplicate")
```
Used for: employee creation, punch-in.

### Pattern 2: Conditional Update
```python
result = db.attendance_logs.update_one(
    {"_id": record["_id"], "punch_out": None},  # only if still open
    {"$set": {"punch_out": ts, ...}}
)
if result.modified_count == 0:
    raise HTTPException(409, "already modified")
```
Used for: punch-out.

### Pattern 3: History Length Guard
```python
result = db.attendance_logs.update_one(
    {"_id": record["_id"], "history": {"$size": current_len}},
    {"$set": {...}, "$push": {"history": entry}}
)
```
Used for: corrections — prevents concurrent edits from silently dropping each other's history.

## 6. Likely Interview Questions

1. **Why synchronous PyMongo instead of Motor?**
   — The spec says all code in one file. Sync is simpler, and FastAPI runs sync handlers in a threadpool. For this assignment's scale, the difference is negligible.

2. **Why not use `round()` for work hours?**
   — Python's `round()` uses banker's rounding (ROUND_HALF_EVEN). `round(8.845, 2)` = `8.84`. The spec requires half-up: `8.845` → `8.85`.

3. **How do you prevent duplicate punch-ins?**
   — Unique compound index on `(emp_code, date)`. No check-then-insert pattern. `DuplicateKeyError` is caught for 409.

4. **What happens with overnight shifts?**
   — If `shift_end <= shift_start` (e.g., 22:00-06:00), the shift is overnight. A punch-in whose IST time is before `shift_end` belongs to the previous day.

5. **How does the leaderboard handle ties?**
   — MongoDB's `$rank` window function gives standard competition ranking: ties share a rank, the next rank is skipped. `limit` filters by `rank <= limit`, so tied employees at the boundary are all included.

6. **What if an employee has zero attendance logs?**
   — They still count in headcount (department summary uses `employees` collection, not logs). Their present_days is 0, contributing to a lower department attendance rate.

7. **How do corrections preserve the audit trail?**
   — `$push` appends to `history` atomically in the same `update_one` as the `$set` of new values. A concurrency guard (`history: {$size: N}`) prevents lost updates.

## 7. Commands Reference

```bash
# Run the app
uvicorn app.main:app --port 8000 --reload

# Seed sample data
python sample_seed.py

# Run all tests
pytest tests/ -v

# Test a specific endpoint
curl http://localhost:8000/health
curl http://localhost:8000/employees
curl http://localhost:8000/analytics/departments/summary?month=2026-07

# Check explain plan
curl "http://localhost:8000/admin/explain/attendance_list?emp_code=EMP0001"
```
