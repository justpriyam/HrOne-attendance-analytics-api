"""
Shared pytest configuration and fixtures for HROne Attendance API test suite.
"""

import os
import uuid
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Configure test database
TEST_DB_NAME = f"hrone_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_DB"] = TEST_DB_NAME

from app.main import app, db, client as mongo_client


@pytest.fixture(autouse=True)
def clean_db():
    """Clean the test collections before each test run."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture(scope="session", autouse=True)
def drop_test_db():
    """Drop the test database at the end of the test session."""
    yield
    try:
        mongo_client.drop_database(TEST_DB_NAME)
    except Exception:
        pass


@pytest.fixture
def api():
    """FastAPI TestClient fixture."""
    with TestClient(app) as c:
        yield c
