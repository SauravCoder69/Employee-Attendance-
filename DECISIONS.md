# DECISIONS.md

### 1. Indexes

I use a unique index on `employees.emp_code` to prevent duplicate employees. A unique compound index on `attendance_logs.emp_code` and `date` ensures that an employee has only one attendance record per day. I also use indexes for attendance date, status, and employee-based queries to improve filtering and sorting. I would avoid adding unnecessary indexes because they increase storage and write costs.

### 2. Punch-in race

If two identical punch-in requests arrive together, both may initially check for an existing record. The unique compound index allows only one insert to succeed. The other request receives a duplicate-key error, which the API converts into a `409 Conflict` response.

### 3. Ties

The leaderboard uses competition ranking. Employees tied on late minutes receive the same rank, and the next rank is skipped. The query applies the limit after ranking, so employees tied at the cutoff are included even if the result contains more rows than the requested limit.

### 4. Headcount

The department summary starts from employees who joined by the end of the selected month and then combines their attendance data. This includes employees who have no attendance logs and excludes employees who joined later.

### 5. Scaling to 100x data

I would review query execution plans, improve indexes based on actual query patterns, and load-test aggregation pipelines. I would also consider caching frequently requested analytics if the data freshness requirements allow it.