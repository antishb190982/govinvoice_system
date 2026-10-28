"""
Working-time arithmetic for SLA clocks: only configured work-week days and
working hours count, and public holidays (core.Holiday) are skipped.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone


def _holidays():
    from core.models import Holiday

    return set(Holiday.objects.values_list("date", flat=True))


def is_working_day(day, holidays=None):
    holidays = _holidays() if holidays is None else holidays
    return day.weekday() in settings.WORK_WEEK_DAYS and day not in holidays


def _bounds(day, tz):
    start = timezone.make_aware(datetime.combine(day, time(settings.WORK_DAY_START_HOUR)), tz)
    end = timezone.make_aware(datetime.combine(day, time(settings.WORK_DAY_END_HOUR)), tz)
    return start, end


def add_working_hours(start, hours):
    """Return the moment `hours` of working time after `start`."""
    tz = timezone.get_current_timezone()
    holidays = _holidays()
    current = timezone.localtime(start, tz)
    remaining = timedelta(hours=hours)
    for _ in range(3660):
        day = current.date()
        if is_working_day(day, holidays):
            day_start, day_end = _bounds(day, tz)
            if current < day_start:
                current = day_start
            if current < day_end:
                available = day_end - current
                if remaining <= available:
                    return current + remaining
                remaining -= available
        current = _bounds(day + timedelta(days=1), tz)[0]
    raise ValueError("SLA horizon exceeds ten years")


def working_hours_between(start, end):
    """Working hours elapsed between two moments (negative if end < start)."""
    if end < start:
        return -working_hours_between(end, start)
    tz = timezone.get_current_timezone()
    holidays = _holidays()
    start, end = timezone.localtime(start, tz), timezone.localtime(end, tz)
    total = timedelta()
    day = start.date()
    while day <= end.date():
        if is_working_day(day, holidays):
            day_start, day_end = _bounds(day, tz)
            lo, hi = max(day_start, start), min(day_end, end)
            if hi > lo:
                total += hi - lo
        day += timedelta(days=1)
    return total.total_seconds() / 3600


def subtract_working_days(day, days):
    """The working day `days` working days before `day`."""
    holidays = _holidays()
    current = day
    while days > 0:
        current -= timedelta(days=1)
        if is_working_day(current, holidays):
            days -= 1
    return current


def end_of_working_day(day):
    tz = timezone.get_current_timezone()
    return _bounds(day, tz)[1]
