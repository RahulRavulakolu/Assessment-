# Employee Attendance & Analytics API

FastAPI backend providing attendance tracking, regularizations, and reporting analytics backed by MongoDB aggregation pipelines.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0 / 8.0)

## Quickstart

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   The application reads `MONGO_URI` and `MONGO_DB` directly from environment variables (defaults to `mongodb://localhost:27017` and `attendance_db`).

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Or via python module:
   ```bash
   python -m uvicorn app.main:app --port 8000
   ```

## Key Highlights & Implementation Details
- **Part A (`REVIEW.md`):** Complete defect catalog identifying 10+ starter flaws, race conditions, edge-case rounding bugs, and pagination issues.
- **Part B (`app/main.py`):** Full implementation of all endpoints compliant with `openapi.yaml` and rules R1–R10 (overnight shifts, 10-minute grace period, competition ranking ties, mid-month joiners, and gap-filling with `$densify`).
- **Part C (`DECISIONS.md` & `/admin/explain/{endpoint}`):** Production-grade indexes created idempotently at startup. Explain plans verify all critical queries and aggregations use `IXSCAN` with zero `COLLSCAN`.
