# DECISIONS.md

### 1. Indexes

Startup creates unique indexes on `employees.emp_code` and
`attendance_logs.(emp_code, date)`. Attendance indexes also cover date ordering,
employee punch-in lookup, and status/date sorting. Employee indexes support
department and join-date filters. These indexes trade storage and write cost for
read performance; actual plan use must be checked on MongoDB.

### 2. Punch-in race

The API does not rely on check-then-insert. MongoDB's unique
`(emp_code, date)` index allows one insert for that attendance day; duplicate
key errors become `409 Conflict`. This is the database-level concurrency
guard, but simultaneous requests still need an isolated integration test.

### 3. Ties

The late leaderboard uses `$rank` over descending total late minutes, then sorts
ties by employee code. It filters on `rank <= limit` after ranking, preserving
all employees tied at a qualifying rank.

### 4. Headcount

Monthly department analytics aggregate from eligible employees and look up
attendance, preserving employees with no logs. Trend headcount is calculated
per date from each employee's `joined_on` value.

### 5. Scaling to 100x data

Use the explain endpoint with `executionStats` and representative data to check
index scans, examined keys/documents, and sort stages. Load-test the pipelines
before considering caching; no performance claim is made without those results.
