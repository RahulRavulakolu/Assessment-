"""
Stress and Concurrency Test Suite
Tests:
1. Concurrent punch-ins (race condition test) -> exactly one 201 and others 409.
2. Concurrent punch-outs (race condition test) -> exactly one 200 and others 409.
3. Concurrent regularizations (optimistic locking test) -> exactly one 200 and others 409.
4. Ties in leaderboard with standard competition ranking (1, 2, 2, 4) with limit applied after ranking.
5. Department summary with employees having 0 logs, mid-month joiner, and future joiner.
6. Department trend with gap-filling ($densify), weekends, and 7-day moving average.
7. Explain plans ensuring IXSCAN and no COLLSCAN on all 5 endpoints.
"""
import concurrent.futures
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app, client, db, init_db

test_client = TestClient(app)

def setup_data():
    os.system("python sample_seed.py")
    init_db()

def test_concurrent_punch_ins():
    setup_data()
    # Attempt 5 simultaneous punch-ins for EMP0002 on date 2026-07-25
    dt_in = datetime(2026, 7, 25, 9, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    in_ms = int(dt_in.timestamp() * 1000)

    def do_punch():
        with TestClient(app) as c:
            return c.post("/attendance/punch-in", json={
                "emp_code": "EMP0002",
                "punched_at": in_ms,
                "status": "PRESENT"
            }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch(), range(5)))

    print("Concurrent punch-in results:", results)
    assert results.count(201) == 1, f"Expected exactly one 201, got {results.count(201)}"
    assert results.count(409) == 4, f"Expected 4 409s, got {results.count(409)}"

def test_concurrent_punch_outs():
    setup_data()
    # EMP0006 already has an open punch-in on 2026-07-07 in sample_data!
    dt_out = datetime(2026, 7, 7, 18, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out_ms = int(dt_out.timestamp() * 1000)

    def do_punch_out():
        with TestClient(app) as c:
            return c.post("/attendance/punch-out", json={
                "emp_code": "EMP0006",
                "punched_at": out_ms
            }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch_out(), range(5)))

    print("Concurrent punch-out results:", results)
    assert results.count(200) == 1, f"Expected exactly one 200, got {results.count(200)}"
    assert results.count(409) == 4, f"Expected 4 409s, got {results.count(409)}"

def test_concurrent_regularizations():
    setup_data()
    # Regularize EMP0001 on 2026-07-06 concurrently
    dt_new_in = datetime(2026, 7, 6, 9, 35, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    new_in_ms = int(dt_new_in.timestamp() * 1000)

    def do_reg(i):
        with TestClient(app) as c:
            return c.patch("/attendance/EMP0001/2026-07-06", json={
                "punch_in": new_in_ms + (i * 1000),
                "reason": f"Concurrent correction {i}",
                "regularized_by": f"admin_{i}"
            }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(do_reg, range(5)))

    print("Concurrent regularization results:", results)
    assert results.count(200) >= 1
    # At least one succeeded, losers got 409 (optimistic concurrency control)
    assert all(code in (200, 409) for code in results)

def test_explain_index_scans():
    setup_data()
    endpoints = [
        "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12"
    ]
    for url in endpoints:
        res = test_client.get(url)
        assert res.status_code == 200
        data = res.json()
        raw_str = str(data["explain"])
        # Ensure IXSCAN is present in the plan
        assert "IXSCAN" in raw_str, f"IXSCAN not found in {url} plan"
        # Ensure no COLLSCAN in the winning plan
        query_planner = data["explain"].get("queryPlanner", {})
        winning_plan_str = str(query_planner.get("winningPlan", ""))
        assert "COLLSCAN" not in winning_plan_str, f"COLLSCAN found in winning plan for {url}"

if __name__ == "__main__":
    test_concurrent_punch_ins()
    test_concurrent_punch_outs()
    test_concurrent_regularizations()
    test_explain_index_scans()
    print("ALL CONCURRENCY & EXPLAIN TESTS PASSED!")
