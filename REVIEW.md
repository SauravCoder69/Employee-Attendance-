# REVIEW.md — Starter Code Review

I reviewed the starter `app/main.py` and compared it with the problem statement, OpenAPI specification, and data model. I found the following issues that need to be fixed.

| # | Where (function / line) | What is wrong | How I'd notice it (test, input, or symptom) | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | The 10-minute grace period is not handled correctly at the exact time boundary. | Test punch-in at 09:40:00 and 09:40:01 for a 09:30 shift. | Compare the punch-in time with the exact grace-period boundary. |
| 2 | `compute_work_hours()` | Python's `round()` does not guarantee the required half-up rounding. | Test a work duration that falls exactly on a rounding boundary. | Use decimal half-up rounding. |
| 3 | `compute_overtime()` | Overtime is counted even when the extra time is less than 30 minutes. | Test punch-out 29 minutes and 30 minutes after shift end. | Apply the 30-minute minimum rule. |
| 4 | `compute_overtime()` | Overnight shifts are not handled correctly. | Test a shift from 22:00 to 06:00. | Calculate the correct shift-end date for overnight shifts. |
| 5 | `health()` | The endpoint returns success without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 if it is unavailable. |
| 6 | `create_employee()` | Concurrent requests can create duplicate employees. | Send two requests with the same employee code at the same time. | Add a unique index on `emp_code` and handle duplicate-key errors. |
| 7 | `EmployeeIn` | Input validation does not cover all the rules in the API contract. | Submit an invalid employee code, date, or shift time. | Add the required field and format validation. |
| 8 | `list_employees()` | The pagination offset is incorrect. | Request page 1 with a page size of 20. | Use `(page - 1) * page_size` for the offset. |
| 9 | `list_employees()` | The total count ignores the department filter. | Request employees from one department and check `total`. | Count only documents matching the filter. |
| 10 | `list_employees()` | Employees are not sorted by `emp_code`. | Check the order of employees returned by the endpoint. | Sort by `emp_code` in ascending order. |
| 11 | Employee and attendance list endpoints | Pagination limits are not validated. | Try page 0 or a page size greater than 100. | Validate page and page-size limits. |
| 12 | `punch_in()` | A missing employee is not handled before accessing shift details. | Punch in with an unknown employee code. | Return 404 when the employee does not exist. |
| 13 | `punch_in()` | Timestamp conversion and validation do not follow the required timezone and precision rules. Timestamp `0` is also treated as missing. | Test zero, invalid timestamps, and timezone boundaries. | Validate epoch milliseconds, handle `None` separately, and apply the required timezone conversion and truncation. |
| 14 | `punch_in()` | The attendance date does not follow the required IST and overnight-shift rules. | Punch in after midnight during an overnight shift. | Calculate the attendance date using the specified rules. |
| 15 | `punch_in()` | Concurrent punch-in requests can create duplicate attendance records. | Send simultaneous punch-in requests for the same employee and date. | Add a unique compound index on `(emp_code, date)` and return 409 for duplicates. |
| 16 | `list_attendance()` | All matching records are loaded into Python before sorting and pagination. | Test the endpoint with a large attendance collection. | Move filtering, sorting, and pagination into MongoDB. |
| 17 | `list_attendance()` | The required secondary sort by employee code is missing. | Check records with the same attendance date. | Sort by date descending and employee code ascending. |
| 18 | `list_attendance()` | The response includes an extra `id` field that is not defined in the attendance response contract. | Compare the returned JSON with the API schema. | Return only the fields required by the contract. |
| 19 | Application startup | Required MongoDB indexes are not created. | Check the collection indexes after starting the application. | Create the required indexes during startup. |
| 20 | Punch-out endpoint | The endpoint is missing. | Call `POST /attendance/punch-out`. | Implement punch-out and the required attendance calculations. |
| 21 | Regularization endpoint | The endpoint for correcting attendance records is missing. | Call the specified PATCH endpoint. | Implement validation, recalculation, audit history, and safe concurrent updates. |
| 22 | Monthly employee analytics | The endpoint is missing. | Call the monthly analytics endpoint. | Implement the required MongoDB aggregation. |
| 23 | Department summary | The endpoint is missing. | Call the department summary endpoint. | Implement the required aggregation using employee and attendance data. |
| 24 | Late leaderboard | The endpoint is missing. | Request the monthly late-arrival leaderboard. | Implement monthly totals, ranking, and tie handling. |
| 25 | Department trend | The endpoint is missing. | Request a date range containing weekends and days without attendance records. | Implement daily metrics, date gap-filling, and the seven-day moving average in MongoDB. |
| 26 | Explain endpoint | The endpoint for checking query execution plans is missing. | Request an explain plan for a supported endpoint. | Implement query explanations with execution statistics. |

## Things I Checked and Did Not Consider Defects

- **`load_dotenv()`**: It is useful for local development, provided secrets are not committed and environment variables take priority.
- **Default shift times**: `09:30` and `18:30` are valid defaults because the times are different.
- **Empty history on punch-in**: Starting with an empty history list is correct because punch-in should not create a regularization entry.
- **Null punch-out time**: This is expected when an employee has punched in but has not punched out yet.
- **Removing `_id` from the employee response**: This is appropriate because MongoDB's internal identifier should not be exposed unless the API contract requires it.
- **Using MongoDB**: The database choice is correct for this assignment. The issues are related to implementation, validation, concurrency, and performance.

This review is based on the original starter code. I will verify each fix against the API contract and test the updated implementation before submission.
