# Employee Attendance & Analytics API

A REST API built with Python, FastAPI, and MongoDB to manage employee information, attendance records, and attendance analytics.

## Features

- Create and list employees.
- Record employee punch-in and punch-out times.
- Manage attendance records and regularizations.
- Calculate work hours, late minutes, overtime, and half-day status.
- Generate employee and department attendance analytics.
- Provide pagination and database query execution statistics.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo

## Setup and Installation

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   ```

   On Windows:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and configure:

   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

   Environment variables already set by the process take precedence over `.env`.

4. (Optional) Load the sample data into a dedicated, disposable database:

   ```bash
   python sample_seed.py
   ```

   **Warning:** `sample_seed.py` deletes all existing documents in the `employees`
   and `attendance_logs` collections before loading samples. Never point it at a
   production, shared, or otherwise valuable database.

5. Start the API:

   ```bash
   uvicorn app.main:app --port 8000
   ```

## API Documentation

Once the server is running, visit:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Health check: `http://localhost:8000/health`

## Notes

- MongoDB 6.0 or newer is required.
- Keep `.env` and all secrets out of version control.
- The API should be tested against the requirements and larger datasets, not only the provided sample records.
- MongoDB 6.0+ is required for the aggregation operators used by the analytics.
- Run the isolated business-rule tests with `python -m unittest discover -s tests -v`.
- Database-backed behavior, concurrency, and execution plans must also be checked
  against an isolated MongoDB database before release.
