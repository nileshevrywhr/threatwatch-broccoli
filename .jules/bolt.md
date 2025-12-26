## 2024-05-22 - Scheduler N+1 Write Bottleneck
**Learning:** The `scan_due_monitors` task performs an individual `update` HTTP request for every due monitor to set its `next_run_at`. In high-load scenarios (e.g., thousands of monitors due simultaneously), this N+1 write pattern will block the scheduler for a significant time, potentially exceeding the Celery task time limit or delaying subsequent runs.
**Action:** Use Supabase's `upsert` (PostgREST bulk update) to update all `next_run_at` fields in a single HTTP request. This transforms O(N) network requests into O(1).
