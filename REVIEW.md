# REVIEW.md — Contract Audit

This review compares the implementation with `openapi.yaml` and
`DATA_MODEL.md`. Findings distinguish code defects from checks that need
MongoDB. No database was contacted during this review.

## Confirmed code defects addressed

| Location | Defect and reproducible symptom | Correction |
|---|---|---|
| `employee_monthly()` | With 1 present day out of 3 working days, the previous calculation returned `33.33`; R8 requires `33.3333`. | Round `attendance_pct` to four decimal places using `Decimal` and `ROUND_HALF_UP`. |
| `department_trend()` | Request `from=2026-07-01&to=2026-07-10`: the previous source assembled daily rows, headcount, rates, and the seven-day average in Python instead of MongoDB. | Generate the 10 dates and calculate all daily output fields with aggregation expressions and a seven-document window. |
| `employee_monthly()` | An employee joining on `2026-07-15` needs working days counted from that date; the previous endpoint iterated over dates in Python. | Calculate working days from `joined_on` inside the employee aggregation. |
| `department_summary()` | An eligible employee with no July attendance must still count toward department headcount; the previous endpoint fetched employees and calculated headcount in Python. | Aggregate eligible employees first, then look up attendance while preserving employees with no logs. |
| `utc_to_ms()` | A BSON datetime ending in `.999` seconds was returned with sub-second milliseconds, contrary to R1's whole-second rule. | Truncate datetimes before converting them to epoch milliseconds. |
| `create_indexes()` | An attendance query filtered by status had no supporting status-leading index. | Add a `(status, date DESC, emp_code ASC)` index. |

The monthly-rate calculation, timestamp truncation, Python boundary helpers,
route presence, and BSON serialization of the constructed pipelines were
checked locally. These checks do not execute MongoDB stages.

## Not verified / remaining evidence gaps

- `tests/test_business_rules.py` covers selected business-rule boundaries and
  pipeline BSON serialization, but there are no MongoDB integration tests.
- MongoDB was unavailable as a safe isolated test target, so startup index
  creation, endpoint database responses, concurrent write races, audit-history
  contention, aggregation results, operator compatibility, and execution plans
  were not run.
- The explain endpoint builds the same pipeline or find command as its
  corresponding endpoint in source. Actual `IXSCAN`/`COLLSCAN`, index bounds,
  keys/documents examined, and 100,000-record performance still require
  `executionStats` from the target MongoDB version and dataset.

Do not treat an aggregation's BSON serialization or a source-level index
definition as proof of database execution behavior.
