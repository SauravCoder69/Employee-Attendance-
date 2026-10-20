import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from bson.decimal128 import Decimal128

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"), tz_aware=True)
db = client[os.getenv("MONGO_DB")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def _index_signature(index_doc):
    if not index_doc:
        return ()
    key = index_doc.get("key")
    if isinstance(key, list):
        return tuple((field, direction) for field, direction in key)
    if isinstance(key, dict):
        return tuple(key.items())
    return ()


def _index_covers_collection(index_doc):
    collation = index_doc.get("collation")
    uses_simple_collation = not collation or collation.get("locale") == "simple"
    return (
        not index_doc.get("sparse")
        and "partialFilterExpression" not in index_doc
        and uses_simple_collation
    )


def _index_options(index_doc):
    return {
        "unique": bool(index_doc.get("unique", False)),
        "sparse": bool(index_doc.get("sparse", False)),
        "partialFilterExpression": index_doc.get("partialFilterExpression"),
        "collation": index_doc.get("collation"),
    }


def _raise_index_conflict(collection, fields, matching_indexes, detail):
    existing = [
        {"name": name, **_index_options(index_doc)}
        for name, index_doc in matching_indexes
    ]
    raise RuntimeError(
        f"Cannot ensure index {fields!r} on {collection.full_name}: {detail}. "
        f"Existing indexes with this key pattern: {existing!r}. "
        "No existing indexes or documents were changed. Resolve the index-option "
        "conflict manually, then restart the application."
    )


def _ensure_index(collection, fields, unique=False):
    key_spec = tuple(fields)
    expected_name = "_".join(f"{field}_{direction}" for field, direction in fields)
    index_info = collection.index_information()
    matching_indexes = [
        (index_name, index_doc)
        for index_name, index_doc in index_info.items()
        if _index_signature(index_doc) == key_spec
    ]
    for _, index_doc in matching_indexes:
        if _index_covers_collection(index_doc) and (
            not unique or index_doc.get("unique") is True
        ):
            return

    current_indexes = collection.index_information()
    index_name = expected_name
    if index_name in current_indexes:
        suffix = 1
        while f"{expected_name}_required_{suffix}" in current_indexes:
            suffix += 1
        index_name = f"{expected_name}_required_{suffix}"

    try:
        collection.create_index(list(fields), unique=unique, name=index_name)
    except DuplicateKeyError as exc:
        raise RuntimeError(
            f"Cannot create required {'unique ' if unique else ''}index {fields!r} "
            f"on {collection.full_name}: existing documents contain duplicate keys. "
            "Correct the duplicate records without deleting unrelated data, then "
            "restart the application."
        ) from exc
    except OperationFailure as exc:
        if exc.code in {11000, 11001}:
            raise RuntimeError(
                f"Cannot create required {'unique ' if unique else ''}index "
                f"{fields!r} on {collection.full_name}: existing documents contain "
                "duplicate keys. Correct the duplicate records without deleting "
                "unrelated data, then restart the application."
            ) from exc
        if exc.code in {85, 86}:
            current_indexes = collection.index_information()
            conflicts = [
                (name, doc)
                for name, doc in current_indexes.items()
                if _index_signature(doc) == key_spec
            ]
            _raise_index_conflict(
                collection,
                fields,
                conflicts,
                f"MongoDB rejected the requested index options (code {exc.code}: {exc})",
            )
        if exc.code == 14031:
            raise RuntimeError(
                f"Cannot create required {'unique ' if unique else ''}index "
                f"{fields!r} on {collection.full_name}: MongoDB reports insufficient "
                f"free disk space ({exc}). Free space on the MongoDB server and "
                "restart the application. No existing index was dropped."
            ) from exc
        raise


def _ensure_unique_index(collection, fields):
    _ensure_index(collection, fields, unique=True)


@app.on_event("startup")
def create_indexes():
    _ensure_unique_index(db.employees, [("emp_code", ASCENDING)])
    _ensure_unique_index(db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)])

    for fields in [
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
    ]:
        _ensure_index(db.attendance_logs, fields)

    for fields in [
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        [("joined_on", ASCENDING)],
        [("department", ASCENDING), ("emp_code", ASCENDING)],
    ]:
        _ensure_index(db.employees, fields)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    diff = punch_in.replace(microsecond=0) - shift_start_dt.replace(microsecond=0)
    diff_seconds = diff.days * 86400 + diff.seconds
    if diff_seconds > 600:
        return diff_seconds // 60
    return 0
    


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    duration = punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)
    seconds = Decimal(duration.days * 86400 + duration.seconds)
    hours = seconds / Decimal("3600")
    rounded_hours = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded_hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    punch_out = punch_out.replace(microsecond=0)
    h, m = map(int, shift_end.split(":"))
    end_date = datetime.fromisoformat(date_str).date()
    start_h, start_m = map(int, shift_start.split(":"))

    if (h, m) <= (start_h, start_m):
        end_date += timedelta(days=1)

    end = datetime.combine(end_date, datetime.min.time(), tzinfo=IST)
    end = end.replace(hour=h, minute=m)

    diff = punch_out - end
    diff_seconds = diff.days * 86400 + diff.seconds

    if diff_seconds < 1800:
        return 0

    return int(diff_seconds // 60)


def validate_epoch_ms(value, field_name: str = "timestamp") -> datetime:
    if value is None:
        raise HTTPException(422, f"{field_name} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, f"{field_name} must be an integer epoch-millisecond timestamp")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, f"{field_name} is out of range")
    try:
        return datetime.fromtimestamp(value // 1000, tz=timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, f"Invalid {field_name} timestamp")


def ms_to_utc_datetime(value):
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        return value
    if isinstance(value, bool):
        return value
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "Invalid epoch-millisecond timestamp")
    return dt.replace(microsecond=0)


def determine_attendance_date(local_ts: datetime, shift_start: str, shift_end: str):
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    if (end_h, end_m) <= (start_h, start_m):
        if (local_ts.hour, local_ts.minute) < (end_h, end_m):
            return (local_ts.date() - timedelta(days=1))
    return local_ts.date()


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
            detail="Database Unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not re.fullmatch(r"^EMP\d{4,6}$", body.emp_code):
        raise HTTPException(422, "emp_code must match ^EMP\\d{4,6}$")
    if len(body.name) < 1 or len(body.name) > 100:
        raise HTTPException(422, "name must be 1-100 characters")
    if len(body.email) > 120 or not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email):
        raise HTTPException(422, "email is invalid")
    if len(body.department) < 1 or len(body.department) > 50:
        raise HTTPException(422, "department must be 1-50 characters")
    validate_time(body.shift_start)
    validate_time(body.shift_end)
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    validate_date(body.joined_on)

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    return api_document(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return {
        "items": api_document(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an epoch-millisecond timestamp")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid attendance status")

    ts = (
        validate_epoch_ms(body.punched_at, "punched_at")
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    local_ts = ts.astimezone(IST)
    d = determine_attendance_date(local_ts, emp["shift_start"], emp["shift_end"]).isoformat()
    shift_start_dt = datetime.combine(
        datetime.strptime(d, "%Y-%m-%d").date(),
        datetime.strptime(emp["shift_start"], "%H:%M").time(),
        tzinfo=IST,
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
        raise HTTPException(409, "Already punched in for this date")

    doc.pop("_id", None)
    return api_document(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        validate_date(date_from)
    if date_to is not None:
        validate_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status is not None:
        if status not in VALID_STATUSES:
            raise HTTPException(422, "Invalid attendance status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": api_document(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ===================== REMAINING IMPLEMENTATION =====================

from pymongo import ReturnDocument


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
    else:
        value = value.astimezone(timezone.utc)
    value = value.replace(microsecond=0)
    elapsed = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000


def api_document(doc):
    """Remove MongoDB _id and convert values to JSON-compatible formats."""

    if isinstance(doc, list):
        return [api_document(item) for item in doc]

    if isinstance(doc, dict):
        result = {}

        for key, value in doc.items():
            if key == "_id":
                continue

            result[key] = api_document(value)

        return result

    if isinstance(doc, datetime):
        return utc_to_ms(doc)

    if isinstance(doc, Decimal128):
        return float(doc.to_decimal())

    return doc


def explain_result(endpoint: str, collection: str, command: dict):
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": db.command({"explain": command, "verbosity": "executionStats"}),
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str):
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "_joined_on": {"$dateFromString": {"dateString": "$joined_on", "timezone": "UTC"}},
            "_month_start": {"$dateFromString": {"dateString": start, "timezone": "UTC"}},
            "_month_end": {"$dateFromString": {"dateString": end, "timezone": "UTC"}},
        }},
        {"$set": {
            "_working_start": {"$max": ["$_joined_on", "$_month_start"]},
            "_working_end_exclusive": "$_month_end",
        }},
        {"$set": {
            "working_days": {"$sum": {"$map": {
                "input": {"$range": [0, {"$max": [
                    0,
                    {"$dateDiff": {
                        "startDate": "$_working_start",
                        "endDate": "$_working_end_exclusive",
                        "unit": "day",
                    }},
                ]}]},
                "as": "offset",
                "in": {"$cond": [
                    {"$lte": [{"$isoDayOfWeek": {"$dateAdd": {
                        "startDate": "$_working_start",
                        "unit": "day",
                        "amount": "$$offset",
                    }}}, 5]},
                    1,
                    0,
                ]},
            }}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lt": end}}},
                {"$project": {
                    "status": 1,
                    "half_day": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "weekday": {"$dayOfWeek": {"$dateFromString": {
                        "dateString": "$date",
                        "timezone": "UTC",
                    }}},
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", PRESENT_STATUSES]},
                            {"$gte": ["$weekday", 2]},
                            {"$lte": ["$weekday", 6]},
                        ]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }},
            ],
            "as": "_metrics",
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": 1,
            "metrics": {"$arrayElemAt": ["$_metrics", 0]},
        }},
    ]


def department_summary_pipeline(
    start: str,
    end: str,
    month_end: str,
    department: Optional[str] = None,
):
    present_condition = {"$in": ["$status", PRESENT_STATUSES]}
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$addToSet": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lt": end}}},
                {"$project": {
                    "status": 1,
                    "half_day": 1,
                    "work_hours": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "weekday": {"$dayOfWeek": {"$dateFromString": {
                        "dateString": "$date",
                        "timezone": "UTC",
                    }}},
                }},
            ],
            "as": "_attendance",
        }},
        {"$unwind": {"path": "$_attendance", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "status": "$_attendance.status",
            "half_day": "$_attendance.half_day",
            "work_hours": "$_attendance.work_hours",
            "late_minutes": {"$ifNull": ["$_attendance.late_minutes", 0]},
            "weekday": "$_attendance.weekday",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$first": "$headcount"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    present_condition,
                    {"$gte": ["$weekday", 2]},
                    {"$lte": ["$weekday", 6]},
                ]},
                {"$cond": ["$half_day", 0.5, 1.0]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [present_condition, {"$ne": ["$work_hours", None]}]},
                "$work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    start: str,
    end: str,
    limit: int,
    department: Optional[str] = None,
):
    employee_pipeline = [{"$project": {"name": 1, "department": 1}}]
    if department is not None:
        employee_pipeline.insert(0, {"$match": {"department": department}})
    return [
        {"$match": {
            "date": {"$gte": start, "$lt": end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": employee_pipeline,
            "as": "_employee",
        }},
        {"$unwind": "$_employee"},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$_employee.name",
            "department": "$_employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]


def department_trend_pipeline(
    department: str, from_date: str, to_date: str
):
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_count = (datetime.strptime(to_date, "%Y-%m-%d").date()
                 - datetime.strptime(from_date, "%Y-%m-%d").date()).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": None,
            "employees": {"$push": {
                "emp_code": "$emp_code",
                "joined_on": "$joined_on",
            }},
        }},
        {"$project": {
            "_id": 0,
            "employees": 1,
            "dates": {"$map": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "in": {"$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": {"$dateAdd": {
                        "startDate": start,
                        "unit": "day",
                        "amount": "$$offset",
                    }},
                    "timezone": "UTC",
                }},
            }},
        }},
        {"$unwind": "$dates"},
        {"$set": {
            "date": "$dates",
            "_eligible_employees": {"$filter": {
                "input": "$employees",
                "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", "$dates"]},
            }},
        }},
        {"$set": {
            "headcount": {"$size": "$_eligible_employees"},
            "_eligible_codes": {"$map": {
                "input": "$_eligible_employees",
                "as": "employee",
                "in": "$$employee.emp_code",
            }},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": {
                "$dateFromString": {"dateString": "$dates", "timezone": "UTC"},
            }}, 5]},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "_eligible_codes",
            "foreignField": "emp_code",
            "let": {"trend_date": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$trend_date"]}}},
                {"$project": {
                    "status": 1,
                    "half_day": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "_attendance",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {
                "input": "$_attendance",
                "as": "record",
                "in": {"$cond": [
                    {"$in": ["$$record.status", PRESENT_STATUSES]},
                    {"$cond": [{"$ifNull": ["$$record.half_day", False]}, 0.5, 1.0]},
                    0,
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$_attendance",
                "as": "record",
                "cond": {"$gt": ["$$record.late_minutes", 0]},
            }}},
        }},
        {"$set": {
            "_attendance_rate_units": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$toInt": {"$floor": {"$add": [
                    {"$multiply": [
                        {"$divide": [
                            {"$toDecimal": "$present_count"},
                            {"$toDecimal": "$headcount"},
                        ]},
                        10000,
                    ]},
                    0.5,
                ]}}},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "_moving_rate_units_sum": {
                    "$sum": "$_attendance_rate_units",
                    "window": {"documents": [-6, "current"]},
                },
                "_moving_valid_days": {
                    "$sum": {"$cond": [
                        {"$ne": ["$_attendance_rate_units", None]},
                        1,
                        0,
                    ]},
                    "window": {"documents": [-6, "current"]},
                },
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$eq": ["$_attendance_rate_units", None]},
                None,
                {"$divide": ["$_attendance_rate_units", 10000]},
            ]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$_moving_valid_days", 0]},
                None,
                {"$divide": [
                    {"$floor": {"$add": [
                        {"$divide": [
                            {"$toDecimal": "$_moving_rate_units_sum"},
                            {"$toDecimal": "$_moving_valid_days"},
                        ]},
                        0.5,
                    ]}},
                    10000,
                ]},
            ]},
        }},
    ]


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

    overtime_delta = punch_out - end_dt
    overtime_seconds = overtime_delta.days * 86400 + overtime_delta.seconds
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
    reason: str
    regularized_by: str


# ------------------------------- PUNCH OUT --------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an epoch-millisecond timestamp")
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    ts = (
        validate_epoch_ms(body.punched_at, "punched_at")
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": PRESENT_STATUSES},
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        future_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": PRESENT_STATUSES},
                "punch_in": {"$ne": None},
            },
            sort=[("punch_in", -1)],
        )
        if future_record is not None:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        raise HTTPException(404, "No open attendance record found")

    stored_punch_in = record["punch_in"].replace(microsecond=0)
    if ts <= stored_punch_in:
        raise HTTPException(422, "punch_out cannot be before punch_in")

    elapsed_seconds = (ts - stored_punch_in).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record was already punched out")

    derived = derive_fields(record, employee, stored_punch_in, ts)

    update_filter = {
        "_id": record["_id"],
        "punch_out": record.get("punch_out"),
        "punch_in": record["punch_in"],
        "status": record["status"],
    }
    for field in derived:
        update_filter[field] = record[field] if field in record else {"$exists": False}
    result = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": {"punch_in": stored_punch_in, "punch_out": ts, **derived}},
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

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    if len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, "reason must be 5-200 characters")
    if len(body.regularized_by) < 1 or len(body.regularized_by) > 50:
        raise HTTPException(422, "regularized_by must be 1-50 characters")

    updates = {}
    if "status" in body.model_fields_set and body.status is None:
        raise HTTPException(422, "status must be a valid attendance status")
    if "punch_in" in body.model_fields_set and body.punch_in is None:
        raise HTTPException(422, "punch_in must be an epoch-millisecond timestamp")
    if "punch_out" in body.model_fields_set and body.punch_out is None:
        raise HTTPException(422, "punch_out must be an epoch-millisecond timestamp")
    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(422, "Invalid attendance status")
        updates["status"] = body.status

    if body.punch_in is not None:
        updates["punch_in"] = validate_epoch_ms(body.punch_in, "punch_in")
    if body.punch_out is not None:
        updates["punch_out"] = validate_epoch_ms(body.punch_out, "punch_out")

    new_status = updates.get("status", record.get("status"))
    new_in = updates.get("punch_in", record.get("punch_in"))
    new_out = updates.get("punch_out", record.get("punch_out"))
    if new_in is not None:
        new_in = new_in.replace(microsecond=0)
        updates["punch_in"] = new_in
    if new_out is not None:
        new_out = new_out.replace(microsecond=0)
        updates["punch_out"] = new_out

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records require both punch times to be null")
        updates["punch_in"] = None
        updates["punch_out"] = None
        new_in = None
        new_out = None
    elif new_status in PRESENT_STATUSES and new_in is None:
        raise HTTPException(422, "Present status requires punch_in")

    if new_in is not None:
        local_dt = new_in.astimezone(IST)
        mapped_date = determine_attendance_date(local_dt, employee["shift_start"], employee["shift_end"]).isoformat()
        if mapped_date != date:
            raise HTTPException(422, f"punch_in must map to record date {date}")

    if new_in is not None and new_out is not None:
        if new_out <= new_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_out - new_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    candidate = {**record, **updates}
    derived = derive_fields(candidate, employee, new_in, new_out)
    updates.update(derived)

    changes = {}
    final_values = {**record, **updates}
    defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(key, defaults.get(key))
        new_value = final_values.get(key, defaults.get(key))
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "No changes detected")

    audit_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    history_filter = (
        {"history": record["history"]}
        if "history" in record
        else {"history": {"$exists": False}}
    )
    update_filter = {"_id": record["_id"], **history_filter}
    for field in updates:
        update_filter[field] = record[field] if field in record else {"$exists": False}

    result = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": updates, "$push": {"history": audit_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "Attendance record changed concurrently")
    return api_document(result)


# -------------------------- EMPLOYEE MONTHLY ANALYTICS --------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = validate_month(month)

    rows = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    if not rows:
        raise HTTPException(404, "Employee not found")

    row = rows[0]
    metrics = row.get("metrics") or {}
    working_days = row["working_days"]
    present_days = float(metrics.get("present_days", 0.0))

    attendance_pct = None
    if working_days:
        attendance_pct = float(
            (
                Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --------------------------- DEPARTMENT SUMMARY --------------------------

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = validate_month(month)
    month_end = datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=1)

    rows = list(db.employees.aggregate(department_summary_pipeline(
        start,
        end,
        month_end.isoformat(),
        department,
    )))

    items = []
    for row in rows:
        metrics = row
        avg = metrics.get("avg_work_hours")
        if avg is not None:
            avg = float(Decimal(str(avg)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": float(metrics.get("present_days", 0.0)),
                "avg_work_hours": avg,
                "late_count": metrics.get("late_count", 0),
                "total_late_minutes": metrics.get("total_late_minutes", 0),
                "leave_count": metrics.get("leave_count", 0),
                "on_duty_count": metrics.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": items}


# ------------------------------ LATE LEADERBOARD -------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = validate_month(month)

    rows = list(db.attendance_logs.aggregate(
        late_leaderboard_pipeline(start, end, limit, department)
    ))
    return {"month": month, "items": rows}


# ----------------------------- DEPARTMENT TREND --------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
):
    if not from_ or not to_:
        raise HTTPException(422, "from and to are required")

    validate_date(from_)
    validate_date(to_)
    if from_ > to_:
        raise HTTPException(422, "from must be on or before to")

    start_dt = datetime.strptime(from_, "%Y-%m-%d").date()
    end_dt = datetime.strptime(to_, "%Y-%m-%d").date()
    if (end_dt - start_dt).days + 1 > 92:
        raise HTTPException(422, "date range must be 92 days or fewer")

    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Department not found")

    items = list(db.employees.aggregate(
        department_trend_pipeline(department, from_, to_)
    ))

    return api_document({"department": department, "items": items})


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
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    limit: int = Query(10, ge=1, le=50),
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
        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            if status not in VALID_STATUSES:
                raise HTTPException(422, "Invalid attendance status")
            query["status"] = status
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                validate_date(date_from)
                query["date"]["$gte"] = date_from
            if date_to is not None:
                validate_date(date_to)
                query["date"]["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        return explain_result(endpoint, "attendance_logs", {
            "find": "attendance_logs",
            "filter": query,
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        })

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        start, end = validate_month(month)
        pipeline = employee_monthly_pipeline(emp_code, start, end)
        return explain_result(endpoint, "employees", {
            "aggregate": "employees", "pipeline": pipeline, "cursor": {},
        })

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = validate_month(month)
        month_end = (datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        pipeline = department_summary_pipeline(start, end, month_end, department)
        return explain_result(endpoint, "employees", {
            "aggregate": "employees", "pipeline": pipeline, "cursor": {},
        })

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = validate_month(month)
        pipeline = late_leaderboard_pipeline(start, end, limit, department)
        return explain_result(endpoint, "attendance_logs", {
            "aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {},
        })

    if endpoint == "department_trend":
        if department is None or from_ is None or to_ is None:
            raise HTTPException(422, "department, from and to are required")

        validate_date(from_)
        validate_date(to_)
        if from_ > to_:
            raise HTTPException(422, "from must be on or before to")

        start_date = datetime.strptime(from_, "%Y-%m-%d").date()
        end_date = datetime.strptime(to_, "%Y-%m-%d").date()
        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(422, "date range must be 92 days or fewer")
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, "Department not found")

        pipeline = department_trend_pipeline(department, from_, to_)

        return explain_result(endpoint, "employees", {
            "aggregate": "employees", "pipeline": pipeline, "cursor": {},
        })