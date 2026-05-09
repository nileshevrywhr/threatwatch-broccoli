# AGENTS

Quick instructions for AI coding agents working in this repository.

## Project Snapshot

- Python backend for threat monitoring.
- FastAPI app in `main.py`.
- Celery app and schedules in `celery_app.py`.
- Task execution/report generation in `celery_tasks.py`.
- Shared helpers in `utils/`.
- Unit tests in `tests/`.

## Source of Truth

- Prefer implementation over prose when they conflict.
- Main docs: [README.md](README.md)

## Local Dev Commands

- Install deps: `pip install -r requirements.txt`
- Run API: `uvicorn main:app --reload`
- Run worker: `celery -A celery_app worker --loglevel=info`
- Run beat: `celery -A celery_app beat --loglevel=info`
- Run tests: `python -m unittest discover -s tests -p "test_*.py"`

## Required Environment Notes

- `SUPABASE_JWT_SECRET` is required at import/startup time in `main.py`; app fails fast if missing.
- Supabase client initialization in both `main.py` and `celery_tasks.py` depends on `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.
- Rate limiter Redis connection reads `CELERY_BROKER_URL` (falls back to `redis://localhost:6379/0`).

## Architecture Boundaries

- API endpoints should enqueue long-running work to Celery (`scan_monitor_task.delay(...)`) instead of doing heavy work inline.
- Scheduling logic belongs to `utils/schedule_utils.py` and Celery beat schedule in `celery_app.py`.
- Background workflows (search, ranking, report generation, storage, email) belong in `celery_tasks.py`.

## Testing Conventions

- Test suite uses `unittest` and `unittest.mock`.
- External services are mocked in tests; preserve this style for new tests.
- In API tests, auth is bypassed via FastAPI dependency overrides.

## Known Pitfalls

- `README.md` cadence details may drift from code. Current beat schedule in `celery_app.py` runs `scan_due_monitors` every 30 minutes.
- Importing `main.py` without setting `SUPABASE_JWT_SECRET` raises immediately.
- Keep timezone handling UTC-aware; `calculate_next_run_at` assumes/normalizes UTC.

## Change Guidance

- Keep patches focused and small.
- Avoid broad refactors unless explicitly requested.
- When changing behavior in `main.py`, `celery_app.py`, `celery_tasks.py`, or `utils/schedule_utils.py`, add/update targeted tests in `tests/`.
