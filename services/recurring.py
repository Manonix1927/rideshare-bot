"""
Recurring trips: a RecurringTrip template keeps re-creating a plain one-off Trip
on the same schedule. spawn_next() is called from every place that closes a Trip
(scheduled auto-close, rating-prompt completion, manual cancel/delete) so the
template survives regardless of what happens to any single day's instance —
the only way to stop it is the explicit "Скасувати регулярність" action.
"""
from datetime import datetime, timedelta, time as _time

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Trip, RecurringTrip
from services.timezone import now as _now

_DAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

MASK_DAILY = "1111111"
MASK_WEEKDAYS = "1111100"


def mask_label(mask: str) -> str:
    if mask == MASK_DAILY:
        return "Щодня"
    if mask == MASK_WEEKDAYS:
        return "Пн–Пт"
    days = [_DAYS_UA[i] for i, c in enumerate(mask) if c == "1"]
    return ", ".join(days) if days else "—"


def next_occurrence(mask: str, hour: int, minute: int, ref: datetime) -> datetime | None:
    """First datetime matching the mask that's strictly after `ref` (searches up to 7
    days ahead; starts at ref+1 day since the just-closed instance already covered
    today/its own date)."""
    for delta in range(1, 8):
        d = (ref + timedelta(days=delta)).date()
        if mask[d.weekday()] == "1":
            return datetime.combine(d, _time(hour, minute))
    return None


async def spawn_next(session: AsyncSession, trip: Trip) -> Trip | None:
    """If `trip` belongs to an active recurring template, create the next instance.
    No-op (returns None) if the trip isn't recurring or its template was cancelled."""
    if not trip.recurring_id:
        return None
    rt = await session.get(RecurringTrip, trip.recurring_id)
    if not rt or not rt.is_active:
        return None

    next_dt = next_occurrence(rt.days_mask, rt.departure_hour, rt.departure_minute, _now())
    if not next_dt:
        return None

    new_trip = Trip(
        user_id=rt.user_id,
        role=rt.role,
        from_address=rt.from_address, from_lat=rt.from_lat, from_lon=rt.from_lon,
        to_address=rt.to_address, to_lat=rt.to_lat, to_lon=rt.to_lon,
        departure_time=next_dt,
        price=rt.price,
        seats=rt.seats,
        status="ACTIVE",
        recurring_id=rt.id,
    )
    session.add(new_trip)
    await session.flush()
    return new_trip
