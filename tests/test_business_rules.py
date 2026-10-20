import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bson import BSON
from fastapi import HTTPException

os.environ["MONGO_URI"] = "mongodb://127.0.0.1:1"
os.environ["MONGO_DB"] = "attendance_audit_test"

from app import main


class BusinessRuleTests(unittest.TestCase):
    def test_late_minutes_boundaries(self):
        shift_start = datetime(2026, 7, 6, 9, 30, tzinfo=main.IST)
        cases = [
            (datetime(2026, 7, 6, 9, 40, tzinfo=main.IST), 0),
            (datetime(2026, 7, 6, 9, 40, 1, tzinfo=main.IST), 10),
            (datetime(2026, 7, 6, 10, 15, 59, tzinfo=main.IST), 45),
        ]
        for punch_in, expected in cases:
            with self.subTest(punch_in=punch_in):
                self.assertEqual(
                    main.compute_late_minutes(punch_in, shift_start),
                    expected,
                )

    def test_work_hours_use_half_up_rounding(self):
        punch_in = datetime(2026, 7, 6, tzinfo=timezone.utc)
        punch_out = punch_in + timedelta(seconds=18)
        self.assertEqual(main.compute_work_hours(punch_in, punch_out), 0.01)

    def test_overnight_shift_and_overtime_boundaries(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        record = {"date": "2026-07-06", "status": "PRESENT"}
        punch_in = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)
        self.assertEqual(
            main.determine_attendance_date(
                punch_in.astimezone(main.IST),
                employee["shift_start"],
                employee["shift_end"],
            ).isoformat(),
            "2026-07-06",
        )
        self.assertEqual(
            main.determine_attendance_date(
                datetime(2026, 7, 7, 6, 0, tzinfo=main.IST),
                employee["shift_start"],
                employee["shift_end"],
            ).isoformat(),
            "2026-07-07",
        )

        cases = [
            (8 * 3600 + 34 * 60 + 59, 0),
            (8 * 3600 + 35 * 60, 30),
            (8 * 3600 + 36 * 60, 31),
        ]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                result = main.derive_fields(
                    record,
                    employee,
                    punch_in,
                    punch_in + timedelta(seconds=duration),
                )
                self.assertEqual(result["overtime_minutes"], expected)

    def test_half_day_uses_rounded_work_hours(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        record = {"date": "2026-07-06", "status": "PRESENT"}
        punch_in = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        cases = [
            (4 * 3600 + 29 * 60 + 41, True),
            (4 * 3600 + 29 * 60 + 42, False),
            (4 * 3600 + 30 * 60, False),
        ]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                result = main.derive_fields(
                    record,
                    employee,
                    punch_in,
                    punch_in + timedelta(seconds=duration),
                )
                self.assertEqual(result["half_day"], expected)

    def test_epoch_validation_rejects_invalid_values_and_truncates(self):
        for value in (1_783_312_500, 1.0, "1783312500000", True):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as raised:
                    main.validate_epoch_ms(value)
                self.assertEqual(raised.exception.status_code, 422)

        timestamp = 1_783_312_500_999
        self.assertEqual(main.validate_epoch_ms(timestamp).microsecond, 0)
        bson_datetime = datetime(
            2026, 7, 6, 3, 58, 0, 999000, tzinfo=timezone.utc
        )
        self.assertEqual(main.utc_to_ms(bson_datetime) % 1000, 0)

    def test_monthly_attendance_rate_rounds_to_four_places(self):
        fake_db = SimpleNamespace(
            employees=SimpleNamespace(aggregate=lambda pipeline: [{
                "working_days": 3,
                "metrics": {"present_days": 1},
            }])
        )
        with patch.object(main, "db", fake_db):
            result = main.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(result["attendance_pct"], 33.3333)

    def test_analytics_pipelines_are_bson_serializable(self):
        pipelines = [
            main.employee_monthly_pipeline(
                "EMP0001", "2026-07-01", "2026-08-01"
            ),
            main.department_summary_pipeline(
                "2026-07-01", "2026-08-01", "2026-07-31"
            ),
            main.late_leaderboard_pipeline(
                "2026-07-01", "2026-08-01", 10, "Engineering"
            ),
            main.department_trend_pipeline(
                "Engineering", "2026-07-01", "2026-07-31"
            ),
        ]
        for pipeline in pipelines:
            with self.subTest(pipeline=pipeline[0]):
                BSON.encode({"pipeline": pipeline})

        trend_stages = [next(iter(stage)) for stage in pipelines[-1]]
        self.assertIn("$setWindowFields", trend_stages)

    def test_openapi_contains_all_contract_routes(self):
        paths = main.app.openapi()["paths"]
        expected = {
            "/health",
            "/employees",
            "/attendance/punch-in",
            "/attendance",
            "/attendance/punch-out",
            "/attendance/{emp_code}/{date}",
            "/analytics/employees/{emp_code}/monthly",
            "/analytics/departments/summary",
            "/analytics/leaderboard/late",
            "/analytics/departments/{department}/trend",
            "/admin/explain/{endpoint}",
        }
        self.assertTrue(expected.issubset(paths))


if __name__ == "__main__":
    unittest.main()
