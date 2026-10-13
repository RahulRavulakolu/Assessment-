# DECISIONS.md

1. **Indexes.**
On `employees`: `{emp_code: 1}` (unique) guarantees uniqueness and accelerates direct lookups; `{department: 1, joined_on: 1, emp_code: 1}` and `{joined_on: 1}` serve filtered listing and department headcount aggregations.
On `attendance_logs`: `{emp_code: 1, date: 1}` (unique) prevents duplicate punches per day and powers PATCH lookups; `{date: -1, emp_code: 1}` powers paginated attendance lists; `{emp_code: 1, punch_in: -1}` allows O(1) lookup of an employee's latest open punch; and `{date: 1, late_minutes: 1, emp_code: 1}` supports the late leaderboard.
I considered an index on `status` alone, but rejected it because low-cardinality fields have poor selectivity and offer no advantage over date-prefixed compound indexes.

2. **Punch-in race.**
Both concurrent requests calculate the attendance date and attempt `db.attendance_logs.insert_one()`. Because of the database-level unique index on `(emp_code, date)`, MongoDB's WiredTiger engine atomically writes the first document and commits it (returning 201). The second request encounters a unique constraint violation, throwing `DuplicateKeyError`. The application catches this error and immediately returns HTTP 409, avoiding check-then-insert race windows.

3. **Ties.**
The leaderboard aggregation uses MongoDB's `$setWindowFields` with the `$rank` operator, implementing standard competition ranking (1, 2, 2, 4) ordered by `total_late_minutes` descending. Because `limit` is applied after ranking (`rank <= limit`) and tied rows are ordered by `emp_code` ascending, any employees tied at or below the cutoff limit are included, legitimately returning more than `limit` rows when ties occur.

4. **Headcount.**
The department summary aggregation originates from the `employees` collection (filtering `joined_on <= month_end`) rather than `attendance_logs`. It joins attendance logs via `$lookup`. When grouping by department, every eligible employee increments `headcount` via `$sum: 1`, ensuring employees with zero logs are fully counted.

5. **One thing you would change for 100x the data.**
At 100x scale (~10 million logs), running cross-collection aggregations and window functions over raw punch logs on every request would degrade database throughput. I would implement incremental daily and monthly rollup collections updated asynchronously or upon punch-out/regularization, allowing analytics endpoints to serve pre-aggregated summaries in O(1) time.
