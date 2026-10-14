# HROne Employee Attendance & Analytics API

A REST API for employee attendance tracking, manual corrections, and analytics built with FastAPI, MongoDB, and Python.

## Tech Stack

- Python 3.11+ / FastAPI 0.110+ / Pydantic v2
- MongoDB 6.0+ with PyMongo 4.7+
- pytest for automated tests

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (local, Docker, or Atlas)

## Installation

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## MongoDB Setup

### Option 1: Local MongoDB
Install from https://www.mongodb.com/try/download/community and start with `mongod`.

### Option 2: Docker (Development Only)
```bash
docker run -d --name hrone-mongo -p 27017:27017 mongo:7
```
**Note:** No Dockerfile is included in the repository.

### Option 3: MongoDB Atlas
Create a free cluster at https://www.mongodb.com/atlas and use the connection string.

## Configuration

Set environment variables (or use defaults for local development):

```bash
# PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

# Bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

Do not commit a `.env` file with secrets.

## Running

```bash
uvicorn app.main:app --port 8000 --reload
```

Verify:
- Health check: http://localhost:8000/health
- API docs: http://localhost:8000/docs

## Seeding Sample Data

```bash
python sample_seed.py
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Readiness probe (verifies MongoDB) |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees (paginated, filterable by department) |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records (filtered, paginated) |
| PATCH | `/attendance/{emp_code}/{date}` | Manual correction with audit trail |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| GET | `/analytics/departments/summary` | Department attendance summary |
| GET | `/analytics/leaderboard/late` | Late arrivals leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain plan for an endpoint |

## Running Tests

```bash
pytest tests/ -v
```

Tests use a temporary database that is automatically dropped after the session.

## Project Structure

```
├── app/
│   ├── __init__.py
│   └── main.py              # All application code
├── tests/
│   └── test_api.py           # 60 automated tests
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── requirements.txt
├── sample_seed.py
├── openapi.yaml              # API contract (authoritative)
├── DATA_MODEL.md             # MongoDB schema
├── REVIEW.md                 # Starter code defects
├── DECISIONS.md              # Architectural decisions
└── README.md
```

## Business Rules Summary

| Rule | Description |
|------|-------------|
| R1 | IST (UTC+5:30) determines the attendance date; overnight shift exception |
| R2 | Late = punch-in strictly > 10 min after shift_start; late_minutes = floor from shift_start |
| R3 | Overtime = floor minutes after shift_end, only if >= 30 |
| R4 | Work hours = (punch_out - punch_in) / 3600, 2 decimals half-up |
| R5 | Half day = work_hours < 4.50 |
| R6 | PRESENT, WFH, ON_DUTY count as present |
| R7 | Working days = Mon-Fri from joined_on; half day = 0.5 present |
| R8 | 2 decimals half-up (rates: 4 decimals half-up) |
| R9 | Headcount = employees with joined_on <= end of period |
| R10 | page starts at 1, page_size max 100 (default 20) |
