"""
Employee Attendance & Analytics API

FastAPI application for employee attendance logging, regularizations,
and analytics backed by MongoDB aggregation pipelines.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Configuration from environment variables
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def init_db():
    """Create all required MongoDB indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_ms(epoch_ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (R1)."""
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime with whole seconds."""
    return datetime.fromtimestamp(truncate_ms(epoch_ms) / 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (naive or tz-aware) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a float half-up to the given number of decimal places (R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = Decimal("10") ** -decimals
    return float(d.quantize(pattern, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute attendance date per R1:
    - Record date is the IST calendar date of its punch_in.
    - Exception: overnight shift (shift_end <= shift_start), punch-in earlier than
      shift_end belongs to the shift that started the previous day.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)
    dt_ist = punch_in_dt.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    if (eh_h, eh_m) <= (sh_h, sh_m):  # Overnight shift
        if dt_ist.time() < time(eh_h, eh_m, 0):
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Compute exact shift start and end instants in IST for a given attendance date."""
    d = date.fromisoformat(date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    start_dt = datetime(d.year, d.month, d.day, sh_h, sh_m, 0, tzinfo=IST)
    if (eh_h, eh_m) <= (sh_h, sh_m):  # Overnight shift
        next_d = d + timedelta(days=1)
        end_dt = datetime(next_d.year, next_d.month, next_d.day, eh_h, eh_m, 0, tzinfo=IST)
    else:
        end_dt = datetime(d.year, d.month, d.day, eh_h, eh_m, 0, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    """
    Compute late minutes per R2:
    Grace period 10 minutes (600s). Late only if strictly more than 10:00 after shift_start.
    Then whole minutes (floor) since shift_start, otherwise 0.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """
    Compute overtime minutes per R3:
    Whole minutes (floor) from shift_end to punch_out, counted only if >= 30, otherwise 0.
    """
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=UTC)
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """Compute work hours per R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=UTC)
    sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Compute half day per R5: true when work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee(doc: dict) -> dict:
    """Format employee document for API response (epoch ms for created_at, no _id)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def format_history_entry(entry: dict) -> dict:
    """Format history entry for API response (epoch ms for punch times)."""
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_ms(to_val)
        changes_api[k] = {"from": from_val, "to": to_val}
    at_val = entry["at"]
    if isinstance(at_val, datetime):
        at_val = dt_to_ms(at_val)
    return {
        "at": at_val,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_api,
    }


def format_attendance(doc: dict) -> dict:
    """Format attendance document for API response (no _id, no id, epoch ms for instants)."""
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT")

    @model_validator(mode="after")
    def validate_punch_in(self):
        if self.status not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_regularize(self):
        if self.status is not None:
            if self.status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
                raise ValueError("status must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee. Enforced by unique index on emp_code."""
    created_at = datetime.now(tz=UTC).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """Record a punch-in. Race-safe via unique index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = ms_to_utc_dt(body.punched_at)
    else:
        now_dt = datetime.now(tz=UTC)
        punch_in_dt = now_dt.replace(microsecond=0)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    shift_start_dt, _ = get_shift_instants(att_date, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes employee's most recent open record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_utc_dt(body.punched_at)
    else:
        now_dt = datetime.now(tz=UTC)
        punch_out_dt = now_dt.replace(microsecond=0)

    # Check if there is an active open record with punch_in after punch_out_dt
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if open_record:
        open_in_dt = open_record["punch_in"]
        if open_in_dt.tzinfo is None:
            open_in_dt = open_in_dt.replace(tzinfo=UTC)
        if punch_out_dt <= open_in_dt:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    # Find the employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    att_date = record["date"]
    _, shift_end_dt = get_shift_instants(att_date, emp["shift_start"], emp["shift_end"])

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime_minutes(punch_out_dt, shift_end_dt)
    half_day = compute_half_day(work_hours)

    # Atomic update ensuring only one concurrent request punches out
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return format_attendance(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    """Regularize (manually correct) one day's record and append to history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # If no field supplied at all
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    # Setting ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT or LEAVE cannot be supplied with punch times")

    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        # Determine punch_out
        if body.punch_out is not None:
            new_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=UTC)
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)

            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

            shift_start_dt, shift_end_dt = get_shift_instants(date, emp["shift_start"], emp["shift_end"])
            new_late_minutes = compute_late_minutes(new_punch_in, shift_start_dt)
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, shift_end_dt)
            new_half_day = compute_half_day(new_work_hours)
        else:
            shift_start_dt, _ = get_shift_instants(date, emp["shift_start"], emp["shift_end"])
            new_late_minutes = compute_late_minutes(new_punch_in, shift_start_dt)
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compare changes
    changes_db: Dict[str, dict] = {}

    def check_change(field_name: str, old_val: Any, new_val: Any, is_instant: bool = False):
        if is_instant:
            old_cmp = dt_to_ms(old_val)
            new_cmp = dt_to_ms(new_val)
            if old_cmp != new_cmp:
                changes_db[field_name] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes_db[field_name] = {"from": old_val, "to": new_val}

    check_change("status", record.get("status"), target_status)
    check_change("punch_in", record.get("punch_in"), new_punch_in, is_instant=True)
    check_change("punch_out", record.get("punch_out"), new_punch_out, is_instant=True)
    check_change("work_hours", record.get("work_hours"), new_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_change("half_day", record.get("half_day", False), new_half_day)

    if not changes_db:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    current_history = record.get("history") or []
    # Optimistic concurrency: ensure history has not changed concurrently
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": current_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent modification detected")

    return format_attendance(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee via aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # Working days per R7
    eff_start = max(month_start, emp["joined_on"])
    working_days = 0
    if eff_start <= month_end:
        cur = date.fromisoformat(eff_start)
        end_d = date.fromisoformat(month_end)
        while cur <= end_d:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}},
                        "timezone": "UTC",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        doc = res[0]
        present_days = float(doc.get("present_days", 0.0))
        leave_days = int(doc.get("leave_days", 0))
        late_count = int(doc.get("late_count", 0))
        total_late = int(doc.get("total_late_minutes", 0))
        total_ot = int(doc.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month across collections via aggregation."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "date": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}},
                                    "timezone": "UTC",
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_valid_work_hours": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]},
                                            ]
                                        },
                                        "$$l.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "sum_work_hours": {"$sum": {"$sum": "$emp_valid_work_hours"}},
                "count_work_hours": {"$sum": {"$size": "$emp_valid_work_hours"}},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "sum_work_hours": 1,
                "count_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        count_wh = d["count_work_hours"]
        if count_wh > 0:
            avg_wh = round_half_up(d["sum_work_hours"] / count_wh, 2)
        else:
            avg_wh = None

        items.append(
            {
                "department": d["department"],
                "headcount": d["headcount"],
                "present_days": float(d["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": d["late_count"],
                "total_late_minutes": d["total_late_minutes"],
                "leave_count": d["leave_count"],
                "on_duty_count": d["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with competition ranking ties via $setWindowFields."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily attendance trend with gap filling ($densify) and 7-day moving average."""
    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must be >= from")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Range cannot exceed 92 days")

    start_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    end_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": {"date": "$date_dt", "timezone": "UTC"}},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"joined_on": 1, "_id": 0}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    docs = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = round_half_up(d["attendance_rate"], 4) if d["attendance_rate"] is not None else None
        m_avg = round_half_up(d["moving_avg_7d"], 4) if d["moving_avg_7d"] is not None else None
        items.append(
            {
                "date": d["date"],
                "is_working_day": d["is_working_day"],
                "headcount": d["headcount"],
                "present_count": float(d["present_count"]),
                "late_count": d["late_count"],
                "attendance_rate": att_rate,
                "moving_avg_7d": m_avg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Raw MongoDB explain with executionStats for the named endpoint's main query."""
    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
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
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        collection_name = "attendance_logs"
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            }
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "department_summary":
        collection_name = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_end = f"{month}-{last_day:02d}"
        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_stage["department"] = department
        pipeline = [{"$match": match_stage}]
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        collection_name = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            }
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "department_trend":
        collection_name = "attendance_logs"
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}}
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", command, verbosity="executionStats")

    else:
        raise HTTPException(status_code=422, detail="Unknown endpoint name")

    # Serialize BSON objects into standard JSON
    explain_json = json_util.loads(json_util.dumps(exp))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_json,
    }
