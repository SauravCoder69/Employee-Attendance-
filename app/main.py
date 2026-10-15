import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"), tz_aware=True)
db = client[os.getenv("MONGO_DB")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("status", ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    diff_second = (punch_in - shift_start_dt).total_seconds()
    if diff_second > 600:
        return int(diff_second//60)
    return 0
    


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    second = Decimal(str((punch_out - punch_in).total_seconds()))
    hours  = second/Decimal("3600")
    rounded_hours = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded_hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str,shift_start:str) -> int:
    h, m = map(int, shift_end.split(":"))
    end_date = datetime.fromisoformat(date_str).date()
    start_h, start_m = map(int, shift_start.split(":"))

    if (h, m) <= (start_h, start_m):
        end_date += timedelta(days=1)

    end = datetime.combine(end_date, datetime.min.time(), tzinfo=IST)
    end = end.replace(hour=h, minute=m)

    diff_seconds = (punch_out - end).total_seconds()

    if diff_seconds < 1800:
        return 0

    return int(diff_seconds // 60)
    


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database Unavilable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    return doc

    

@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int  = Query(1,ge = 1),
                   page_size: int = Query(20, ge=1, le=100),
    ):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    
    


    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        ).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    local_ts = ts.astimezone(IST)
    local_date = local_ts.date()

    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))

    # Overnight shift: early-morning punch belongs to previous date
    if (end_h, end_m) <= (start_h, start_m):
        if (local_ts.hour, local_ts.minute) < (end_h, end_m):
            local_date -= timedelta(days=1)

    d = local_date.isoformat()

    # Calculate shift start using the attendance date
    shift_start_dt = datetime.combine(
        local_date,
        datetime.strptime(emp["shift_start"], "%H:%M").time(),
        tzinfo=IST
    )

    late_minutes = compute_late_minutes(local_ts, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
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
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date"
        )

    doc.pop("_id", None)
    return doc

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):






    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        if status not in ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status"
            )
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    skip = (page - 1) * page_size

    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort("date", DESCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# ===================== REMAINING IMPLEMENTATION =====================

from pydantic import ConfigDict, field_validator
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError


VALID_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def validate_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format")


def validate_month(value: str) -> tuple[str, str]:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        if parsed.strftime("%Y-%m") != value:
            raise ValueError
        start = value + "-01"
        if parsed.month == 12:
            end = f"{parsed.year + 1}-01-01"
        else:
            end = f"{parsed.year}-{parsed.month + 1:02d}-01"
        return start, end
    except (ValueError, TypeError):
        raise HTTPException(422, "Month must use YYYY-MM format")


def validate_time(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%H:%M")
        if parsed.strftime("%H:%M") != value:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise HTTPException(422, "Time must use HH:MM format")


def utc_to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def api_document(doc):
    """Remove MongoDB _id and return API timestamps in epoch milliseconds."""
    if isinstance(doc, list):
        return [api_document(item) for item in doc]
    if not isinstance(doc, dict):
        return doc

    result = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        if isinstance(value, datetime):
            result[key] = utc_to_ms(value)
        elif isinstance(value, dict):
            result[key] = api_document(value)
        elif isinstance(value, list):
            result[key] = [
                api_document(item) if isinstance(item, dict) else
                utc_to_ms(item) if isinstance(item, datetime) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def shift_end_datetime(date_str: str, shift_start: str, shift_end: str):
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.strptime(shift_start, "%H:%M").time()
    end = datetime.strptime(shift_end, "%H:%M").time()

    if end <= start:
        day += timedelta(days=1)

    return datetime.combine(day, end, tzinfo=IST)


def derive_fields(record, employee, punch_in, punch_out):
    """Recalculate derived values after punch-out or manual correction."""
    if record.get("status") not in PRESENT_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        raise HTTPException(422, "Present status requires punch_in")

    punch_in = punch_in.replace(microsecond=0)

    start_time = validate_time(employee["shift_start"])
    end_time = validate_time(employee["shift_end"])

    start_dt = datetime.combine(
        datetime.strptime(record["date"], "%Y-%m-%d").date(),
        datetime.strptime(start_time, "%H:%M").time(),
        tzinfo=IST,
    )

    late_minutes = compute_late_minutes(punch_in.astimezone(IST), start_dt)

    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }

    punch_out = punch_out.replace(microsecond=0)
    if punch_out < punch_in:
        raise HTTPException(422, "punch_out cannot be before punch_in")

    work_hours = compute_work_hours(punch_in, punch_out)
    end_dt = shift_end_datetime(
        record["date"], start_time, end_time
    ).astimezone(timezone.utc)

    overtime_seconds = (punch_out - end_dt).total_seconds()
    overtime_minutes = (
        int(overtime_seconds // 60)
        if overtime_seconds >= 1800 else 0
    )

    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": work_hours < 4.50,
    }


# ----------------------------- Request models -----------------------------

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    corrected_by: str
    reason: str


# ------------------------------- PUNCH OUT --------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if body.punched_at is not None:
        try:
            if abs(body.punched_at) < 100_000_000_000:
                raise ValueError
            ts = datetime.fromtimestamp(
                body.punched_at / 1000, tz=timezone.utc
            ).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid epoch-millisecond timestamp")
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's currently open attendance record.
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_out": None,
        "status": {"$in": PRESENT_STATUSES},
        "punch_in": {"$ne": None},
    })

    if not record:
        employee = db.employees.find_one({"emp_code": body.emp_code})
        if not employee:
            raise HTTPException(404, "Employee not found")
        raise HTTPException(404, "No open attendance record found")

    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    if ts < record["punch_in"]:
        raise HTTPException(422, "punch_out cannot be before punch_in")

    derived = derive_fields(record, employee, record["punch_in"], ts)

    # Atomic conditional update: only one concurrent request can succeed.
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
            "status": {"$in": PRESENT_STATUSES},
        },
        {
            "$set": {
                "punch_out": ts,
                **derived,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        raise HTTPException(409, "Attendance record was already punched out")

    return api_document(result)


# ------------------------------ REGULARIZATION ----------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    validate_date(date)

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if not record:
        raise HTTPException(404, "Attendance record not found")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    if not body.corrected_by.strip() or not body.reason.strip():
        raise HTTPException(422, "corrected_by and reason are required")

    updates = {}
    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(422, "Invalid attendance status")
        updates["status"] = body.status

    for field, value in (
        ("punch_in", body.punch_in),
        ("punch_out", body.punch_out),
    ):
        if value is not None:
            try:
                if abs(value) < 100_000_000_000:
                    raise ValueError
                updates[field] = datetime.fromtimestamp(
                    value / 1000, tz=timezone.utc
                ).replace(microsecond=0)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, f"Invalid {field} timestamp")

    new_status = updates.get("status", record["status"])
    new_in = updates.get("punch_in", record.get("punch_in"))
    new_out = updates.get("punch_out", record.get("punch_out"))

    if new_status in PRESENT_STATUSES and new_in is None:
        raise HTTPException(422, "Present status requires punch_in")

    if new_status in {"ABSENT", "LEAVE"}:
        new_in, new_out = None, None
        updates["punch_in"] = None
        updates["punch_out"] = None

    candidate = {**record, **updates}
    derived = derive_fields(candidate, employee, new_in, new_out)
    updates.update(derived)

    # Audit only the fields whose values actually changed.
    changes = {}
    for key, new_value in updates.items():
        old_value = record.get(key)
        if old_value != new_value:
            changes[key] = {
                "before": old_value,
                "after": new_value,
            }

    if not changes:
        return api_document(record)

    audit_entry = {
        "when": datetime.now(timezone.utc).replace(microsecond=0),
        "who": body.corrected_by,
        "why": body.reason,
        "changes": changes,
    }

    # $push is atomic, so concurrent corrections don't overwrite history.
    result = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date},
        {
            "$set": updates,
            "$push": {"history": audit_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    return api_document(result)


# -------------------------- EMPLOYEE MONTHLY ANALYTICS --------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = validate_month(month)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    # Working-day count is calculated inside MongoDB.
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start, "$lt": end},
        }},
        {"$group": {
            "_id": None,
            "total_records": {"$sum": 1},
            "present_days": {"$sum": {
                "$cond": [
                    {"$in": ["$status", PRESENT_STATUSES]}, 1, 0
                ]
            }},
            "half_days": {"$sum": {
                "$cond": [{"$eq": ["$half_day", True]}, 1, 0]
            }},
            "absent_days": {"$sum": {
                "$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]
            }},
            "leave_days": {"$sum": {
                "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
            }},
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    stats = rows[0] if rows else {
        "total_records": 0,
        "present_days": 0,
        "half_days": 0,
        "absent_days": 0,
        "leave_days": 0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "total_work_hours": 0,
    }
    stats.pop("_id", None)

    # Count Mon-Fri from the later of joined_on or month start.
    joined_on = max(employee["joined_on"], start)
    first = datetime.strptime(joined_on, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    days = (last - first).days
    working_days = 0

    if days > 0:
        working_days = list(db.employees.aggregate([
            {"$match": {"emp_code": emp_code}},
            {"$project": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {"$range": [0, days]},
                            "as": "day",
                            "cond": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateAdd": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": joined_on
                                                    }
                                                },
                                                "unit": "day",
                                                "amount": "$$day",
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        }
                    }
                }
            }},
        ]))[0]["working_days"]

    stats["working_days"] = working_days
    stats["present_days"] = stats["present_days"]
    stats["attendance_rate"] = (
        round(stats["present_days"] / working_days, 4)
        if working_days else 0.0
    )

    return {
        "emp_code": emp_code,
        "month": month,
        **stats,
    }


# --------------------------- DEPARTMENT SUMMARY --------------------------

@app.get("/analytics/departments/summary")
def department_summary(month: str):
    start, end = validate_month(month)

    # Start from employees, so employees with zero logs remain in headcount.
    pipeline = [
        {"$match": {"joined_on": {"$lt": end}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start]},
                        {"$lt": ["$date", end]},
                    ]
                }}},
                {"$project": {
                    "status": 1,
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                    "work_hours": 1,
                    "half_day": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_records": {"$sum": {
                "$cond": [
                    {"$in": ["$logs.status", PRESENT_STATUSES]}, 1, 0
                ]
            }},
            "late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            "work_hours_total": {"$sum": {"$ifNull": ["$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {
                "$cond": [{"$isNumber": "$logs.work_hours"}, 1, 0]
            }},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_records": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "average_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$round": [
                        {"$divide": ["$work_hours_total", "$work_hours_count"]},
                        2
                    ]},
                    0
                ]
            },
        }},
        {"$sort": {"department": 1}},
    ]

    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


# ------------------------------ LATE LEADERBOARD -------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=100),
):
    start, end = validate_month(month)

    pipeline = [
        {"$match": {
            "date": {"$gte": start, "$lt": end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "late_days": {"$sum": 1},
            "total_late_minutes": {"$sum": "$late_minutes"},
        }},
        {"$setWindowFields": {
            "sortBy": {"late_days": -1, "total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$sort": {
            "rank": 1,
            "_id": 1,
        }},
        {"$limit": limit},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": {
            "path": "$employee",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "late_days": 1,
            "total_late_minutes": 1,
            "rank": 1,
        }},
    ]

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


# ----------------------------- DEPARTMENT TREND --------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    validate_date(date_from)
    validate_date(date_to)

    if date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")

    start_dt = datetime.strptime(date_from, "%Y-%m-%d")
    end_dt = datetime.strptime(date_to, "%Y-%m-%d")

    pipeline = [
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {
            "employee.department": department,
            "date": {"$gte": date_from, "$lte": date_to},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", PRESENT_STATUSES]}, 1, 0
                ]
            }},
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$set": {
            "day": {"$dateFromString": {"dateString": "$_id"}}
        }},
        {"$densify": {
            "field": "day",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [start_dt, end_dt + timedelta(days=1)],
            },
        }},
        {"$set": {
            "_id": {"$dateToString": {
                "format": "%Y-%m-%d",
                "date": "$day",
            }},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_average_present": {
                    "$avg": "$present_count",
                    "window": {"range": [-6, 0], "unit": "day"},
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "present_count": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "moving_average_present": {"$round": ["$moving_average_present", 2]},
        }},
        {"$sort": {"date": 1}},
    ]

    return {
        "department": department,
        "date_from": date_from,
        "date_to": date_to,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


# ------------------------------- EXPLAIN ----------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, f"endpoint must be one of {sorted(allowed)}")

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            if status not in VALID_STATUSES:
                raise HTTPException(422, "Invalid attendance status")
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                validate_date(date_from)
                query["date"]["$gte"] = date_from
            if date_to:
                validate_date(date_to)
                query["date"]["$lte"] = date_to

        cursor = db.attendance_logs.find(query, {"_id": 0}).sort(
            "date", DESCENDING
        ).skip((page - 1) * page_size).limit(page_size)
        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = validate_month(month)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": start, "$lt": end},
            }},
            {"$sort": {"date": 1}},
        ]
        return db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = validate_month(month)
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
        ]
        return db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = validate_month(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": start, "$lt": end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "late_days": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
            }},
            {"$sort": {"late_days": -1, "total_late_minutes": -1}},
            {"$limit": limit},
        ]
        return db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(422, "department, date_from and date_to are required")
        validate_date(date_from)
        validate_date(date_to)
        pipeline = [
            {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
            {"$sort": {"date": 1}},
        ]
        return db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })