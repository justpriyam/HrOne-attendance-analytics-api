# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I created: unique on `employees.emp_code` (enforces the business key and prevents duplicate creates under concurrency); compound unique on `attendance_logs.(emp_code, date)` (one record per employee per day — prevents duplicate punch-ins atomically); single-field on `attendance_logs.date`, `attendance_logs.emp_code`, `attendance_logs.status`, and `employees.department` (used by the attendance list, analytics filters and the department summary). I considered a compound index on `(date, emp_code)` for the sorted attendance list but the single-field indexes already cover the `$match` stage, and the sort is on two fields that are both indexed individually. A full compound covering index would help at 100k+ scale but is not essential here.

2. **Punch-in race.** Both requests pass validation, both call `insert_one`. The unique index on `(emp_code, date)` lets exactly one succeed. The second gets a `DuplicateKeyError` which is caught and returned as HTTP 409. No `find_one` check is needed.

3. **Ties.** I use `$setWindowFields` with `$rank` (standard competition ranking). Two employees tied on `total_late_minutes` share the same rank and the next rank is skipped (1, 2, 2, 4). If both sit at the `limit` boundary, both are returned — the response may contain more rows than `limit`.

4. **Headcount.** The department summary queries the `employees` collection for `joined_on <= month_end`. This returns all employees who existed by the end of the month, whether or not they have any attendance logs. The count of those employees is the headcount.

5. **One thing I would change:** I would move the analytics computations into pre-aggregated materialized views or summary collections. At 100x scale (~10M records), the current per-request aggregation pipelines would be too slow even with indexes. A background worker could maintain rolling monthly summaries that the API reads directly.
