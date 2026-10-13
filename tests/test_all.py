"""
Comprehensive test suite verifying all requirements of the
Employee Attendance & Analytics API assignment.
"""
import calendar
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from pymongo import MongoClient
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app, client, db, init_db

test_client = TestClient(app)

def setup_module():
    # Reload sample seed
    os.system("python sample_seed.py")
    init_db()

def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employee_crud():
    # 1. Create valid employee
    new_emp = {
        "emp_code": "EMP9999",
        "name": "Test Tester",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    res = test_client.post("/employees", json=new_emp)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000
    assert "_id" not in data

    # 2. Duplicate emp_code -> 409
    res_dup = test_client.post("/employees", json=new_emp)
    assert res_dup.status_code == 409

    # 3. Validation: shift_start == shift_end -> 422
    bad_emp = dict(new_emp, emp_code="EMP9998", shift_end="09:30")
    assert test_client.post("/employees", json=bad_emp).status_code == 422

    # 4. Validation: bad emp_code format -> 422
    bad_code = dict(new_emp, emp_code="BAD123")
    assert test_client.post("/employees", json=bad_code).status_code == 422

    # 5. List employees
    res_list = test_client.get("/employees?department=Engineering&page=1&page_size=2")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert list_data["page_size"] == 2
    assert len(list_data["items"]) == 2
    # Check sorted by emp_code asc
    assert list_data["items"][0]["emp_code"] <= list_data["items"][1]["emp_code"]
    # Check total reflects department filter
    assert list_data["total"] >= 2

def test_punch_in_and_out():
    # Employee 0001
    # Check unknown employee punch-in -> 404
    res_unknown = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0999"})
    assert res_unknown.status_code == 404

    # Valid punch-in for 2026-07-20
    # 2026-07-20 09:40:01 IST (601 seconds after 09:30) -> late_minutes = 10
    dt_in = datetime(2026, 7, 20, 9, 40, 1, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    in_ms = int(dt_in.timestamp() * 1000)

    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": in_ms,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["emp_code"] == "EMP0001"
    assert data_in["date"] == "2026-07-20"
    assert data_in["status"] == "PRESENT"
    assert data_in["late_minutes"] == 10
    assert data_in["work_hours"] is None
    assert data_in["punch_out"] is None
    assert "_id" not in data_in
    assert "id" not in data_in

    # Duplicate punch-in for same date -> 409
    res_in_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": in_ms,
        "status": "PRESENT"
    })
    assert res_in_dup.status_code == 409

    # Punch-out before punch-in -> 422
    dt_early = dt_in - timedelta(minutes=5)
    early_ms = int(dt_early.timestamp() * 1000)
    assert test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": early_ms
    }).status_code == 422

    # Valid punch-out at 19:15:00 IST (45 min after 18:30 shift end -> overtime 45)
    # Work hours: 09:40:01 to 19:15:00 = 9h 34m 59s = 34499s -> 34499/3600 = 9.58305 -> 9.58
    dt_out = datetime(2026, 7, 20, 19, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out_ms = int(dt_out.timestamp() * 1000)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_ms
    })
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["late_minutes"] == 10
    assert data_out["overtime_minutes"] == 45
    assert data_out["work_hours"] == 9.58
    assert data_out["half_day"] is False
    assert data_out["punch_out"] == (out_ms // 1000) * 1000

    # Second punch out -> 409
    res_out_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_ms
    })
    assert res_out_dup.status_code == 409

def test_punch_out_targets_correct_record():
    """Punch-out with punched_at between two punch-ins must target the earlier record."""
    # Create a fresh employee for isolation
    test_client.post("/employees", json={
        "emp_code": "EMP8888",
        "name": "Punch Out Test",
        "email": "punchout@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in on day 1 (July 15)
    dt_in1 = datetime(2026, 7, 15, 9, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    in1_ms = int(dt_in1.timestamp() * 1000)
    res1 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP8888", "punched_at": in1_ms, "status": "PRESENT"
    })
    assert res1.status_code == 201

    # Punch out on day 1
    dt_out1 = datetime(2026, 7, 15, 18, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out1_ms = int(dt_out1.timestamp() * 1000)
    res_out1 = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888", "punched_at": out1_ms
    })
    assert res_out1.status_code == 200

    # Punch in on day 2 (July 16) — this is now the most recent record
    dt_in2 = datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    in2_ms = int(dt_in2.timestamp() * 1000)
    res2 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP8888", "punched_at": in2_ms, "status": "PRESENT"
    })
    assert res2.status_code == 201

    # Punch out on day 2 — should target the July 16 record, not July 15
    dt_out2 = datetime(2026, 7, 16, 18, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out2_ms = int(dt_out2.timestamp() * 1000)
    res_out2 = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP8888", "punched_at": out2_ms
    })
    assert res_out2.status_code == 200
    assert res_out2.json()["date"] == "2026-07-16"

def test_overnight_shift():
    # EMP0005 has shift 22:00 - 06:00
    # Punch in at 2026-07-21 01:15 IST -> Earlier than 06:00 -> attendance date is 2026-07-20!
    dt_in = datetime(2026, 7, 21, 1, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    in_ms = int(dt_in.timestamp() * 1000)
    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": in_ms,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-20"
    # Punch out at 2026-07-21 06:45 IST -> 45 min after 06:00 -> overtime 45
    dt_out = datetime(2026, 7, 21, 6, 45, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out_ms = int(dt_out.timestamp() * 1000)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0005",
        "punched_at": out_ms
    })
    assert res_out.status_code == 200
    assert res_out.json()["overtime_minutes"] == 45

def test_attendance_list():
    res = test_client.get("/attendance?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) <= 5
    for item in data["items"]:
        assert "_id" not in item
        assert "id" not in item

    # date_from > date_to -> 422
    assert test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422

def test_regularize():
    # Regularize EMP0001 on 2026-07-06
    # Original: punched_in 09:28 IST (late 0), out 18:35 (ot 0)
    # Correct punch_in to 10:15:00 IST -> late 45
    dt_new_in = datetime(2026, 7, 6, 10, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    new_in_ms = int(dt_new_in.timestamp() * 1000)

    res = test_client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": new_in_ms,
        "reason": "Traffic jam correction",
        "regularized_by": "manager.bob"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 45
    assert len(data["history"]) >= 1
    h = data["history"][-1]
    assert h["by"] == "manager.bob"
    assert h["reason"] == "Traffic jam correction"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["to"] == 45

    # Change nothing -> 422
    assert test_client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": new_in_ms,
        "reason": "Traffic jam correction",
        "regularized_by": "manager.bob"
    }).status_code == 422

    # Setting ABSENT with punch_in -> 422
    assert test_client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": new_in_ms,
        "reason": "Attempt bad update",
        "regularized_by": "manager.bob"
    }).status_code == 422

def test_analytics_monthly():
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0
    assert isinstance(data["attendance_pct"], float)

def test_analytics_departments_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = [item["department"] for item in data["items"]]
    assert "Engineering" in depts
    for item in data["items"]:
        assert item["headcount"] >= 1
        assert "present_days" in item
        assert "avg_work_hours" in item

def test_analytics_late_leaderboard():
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert "items" in data
    # Each item must have rank, emp_code, name, department, total_late_minutes, late_count
    for item in data["items"]:
        assert item["rank"] >= 1
        assert item["total_late_minutes"] > 0

def test_analytics_trend():
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7 # Exactly 7 calendar days
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"]

    # 404 on unknown department
    assert test_client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-06&to=2026-07-12").status_code == 404

    # 422 on to < from
    assert test_client.get("/analytics/departments/Engineering/trend?from=2026-07-12&to=2026-07-06").status_code == 422

def test_explain_endpoints():
    endpoints = [
        ("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07", "attendance_logs"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12", "attendance_logs")
    ]
    for url, col in endpoints:
        res = test_client.get(url)
        assert res.status_code == 200, f"Explain failed for {url}: {res.text}"
        data = res.json()
        assert data["collection"] == col
        assert "explain" in data
        assert "executionStats" in data["explain"] or "queryPlanner" in data["explain"]

if __name__ == "__main__":
    setup_module()
    test_health()
    test_employee_crud()
    test_punch_in_and_out()
    test_punch_out_targets_correct_record()
    test_overnight_shift()
    test_attendance_list()
    test_regularize()
    test_analytics_monthly()
    test_analytics_departments_summary()
    test_analytics_late_leaderboard()
    test_analytics_trend()
    test_explain_endpoints()
    print("ALL TESTS PASSED!")
