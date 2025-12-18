from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta

def calculate_next_run_at(frequency: str, last_run_at: datetime) -> datetime:
    """
    Calculates the next scheduled run time based on frequency and the last run time.

    Args:
        frequency: 'daily', 'weekly', or 'monthly'.
        last_run_at: The datetime of the last run. Naive datetimes are assumed to be UTC.

    Returns:
        A timezone-aware UTC datetime representing the next run time.
        The returned time will always be in the future relative to the current time.

    Raises:
        ValueError: If the frequency is not supported.
    """
    # Naive datetimes are assumed to be UTC
    if last_run_at.tzinfo is None:
        last_run_at = last_run_at.replace(tzinfo=timezone.utc)
    else:
        last_run_at = last_run_at.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    next_run = last_run_at

    if frequency == 'daily':
        delta = relativedelta(days=1)
    elif frequency == 'weekly':
        delta = relativedelta(weeks=1)
    elif frequency == 'monthly':
        delta = relativedelta(months=1)
    else:
        raise ValueError(f"Unsupported frequency: {frequency}")

    # Catch-up logic: Ensure next_run is in the future
    while next_run <= now:
        next_run += delta

    return next_run
