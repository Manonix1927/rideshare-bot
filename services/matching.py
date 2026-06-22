import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Trip, Match, User
from services.geo import haversine_km, routes_compatible
from config import MATCH_RADIUS_KM, MATCH_TIME_DELTA_HOURS

logger = logging.getLogger(__name__)


async def _occupied_seats(driver_trip_id: int, session: AsyncSession) -> int:
    """Seats already taken by CONFIRMED passengers on this driver trip (committed state)."""
    result = await session.execute(
        select(Match)
        .options(selectinload(Match.passenger_trip))
        .where(Match.driver_trip_id == driver_trip_id, Match.status == "CONFIRMED")
    )
    return sum(m.passenger_trip.seats or 1 for m in result.scalars().all())


async def get_remaining_seats(driver_trip: Trip, session: AsyncSession) -> int:
    """How many seats the driver still has available."""
    occupied = await _occupied_seats(driver_trip.id, session)
    return max(0, (driver_trip.seats or 1) - occupied)


async def find_matches_for_trip(
    trip: Trip, session: AsyncSession
) -> list[Trip]:
    """
    Given a newly created trip (driver OR passenger), find the opposite-role
    active trips whose route and time fall within the allowed tolerances.
    """
    opposite_role = "passenger" if trip.role == "driver" else "driver"

    # Passengers search ACTIVE + MATCHING drivers (multi-passenger: driver may already
    # have confirmed someone but still have remaining seats).
    # Drivers only search ACTIVE passengers (a passenger can't split across two drivers).
    if trip.role == "passenger":
        status_filter = ["ACTIVE", "MATCHING"]
    else:
        status_filter = ["ACTIVE"]

    result = await session.execute(
        select(Trip)
        .where(Trip.role == opposite_role, Trip.status.in_(status_filter))
        .options(selectinload(Trip.user))
    )
    candidates = result.scalars().all()

    matches: list[Trip] = []
    delta = timedelta(hours=MATCH_TIME_DELTA_HOURS)

    logger.info(
        "Matching trip #%d (%s) against %d candidates (radius=%.1f km, delta=%.1f h)",
        trip.id, trip.role, len(candidates), MATCH_RADIUS_KM, MATCH_TIME_DELTA_HOURS,
    )

    for candidate in candidates:
        if candidate.user_id == trip.user_id:
            continue

        # Time check
        time_diff = abs((candidate.departure_time - trip.departure_time).total_seconds())
        if time_diff > delta.total_seconds():
            logger.info("  Skip #%d: time diff %.0f s > %.0f s", candidate.id, time_diff, delta.total_seconds())
            continue

        # Route compatibility
        if not routes_compatible(
            trip.from_lat, trip.from_lon, trip.to_lat, trip.to_lon,
            candidate.from_lat, candidate.from_lon, candidate.to_lat, candidate.to_lon,
            MATCH_RADIUS_KM,
        ):
            logger.info("  Skip #%d: routes not compatible", candidate.id)
            continue

        # Seats check: for multi-passenger drivers check REMAINING seats, not total
        if trip.role == "driver":
            driver_seats = trip.seats or 1
            pax_count    = candidate.seats or 1
            if driver_seats < pax_count:
                logger.info("  Skip #%d: not enough seats", candidate.id)
                continue
        else:
            pax_count    = trip.seats or 1
            remaining    = await get_remaining_seats(candidate, session)
            if remaining < pax_count:
                logger.info("  Skip #%d: remaining seats %d < needed %d", candidate.id, remaining, pax_count)
                continue

        logger.info("  MATCH found: trip #%d ↔ candidate #%d", trip.id, candidate.id)
        matches.append(candidate)

    return matches


async def create_match(
    driver_trip: Trip, passenger_trip: Trip, session: AsyncSession
) -> Optional[Match]:
    """Create a Match record and set both trips to MATCHING status."""
    # Avoid duplicate matches
    existing = await session.execute(
        select(Match).where(
            Match.driver_trip_id == driver_trip.id,
            Match.passenger_trip_id == passenger_trip.id,
            Match.status == "PENDING",
        )
    )
    if existing.scalars().first():
        return None

    match = Match(
        driver_trip_id=driver_trip.id,
        passenger_trip_id=passenger_trip.id,
    )
    session.add(match)

    driver_trip.status = "MATCHING"
    passenger_trip.status = "MATCHING"

    await session.commit()
    await session.refresh(match)
    return match


async def confirm_match_side(
    match: Match, user_trip: Trip, session: AsyncSession
) -> bool:
    """
    Mark one side as confirmed. Returns True if both sides are now confirmed.
    """
    if user_trip.role == "driver":
        match.driver_confirmed = True
    else:
        match.passenger_confirmed = True

    if match.driver_confirmed and match.passenger_confirmed:
        match.status = "CONFIRMED"
        driver_trip   = await session.get(Trip, match.driver_trip_id)
        passenger_trip = await session.get(Trip, match.passenger_trip_id)

        if passenger_trip:
            passenger_trip.status = "CONFIRMED"

        if driver_trip:
            # occupied_seats queries committed CONFIRMED matches — this match isn't
            # committed yet, so we add its seats manually.
            occupied_before  = await _occupied_seats(driver_trip.id, session)
            this_seats       = (passenger_trip.seats or 1) if passenger_trip else 1
            total_occupied   = occupied_before + this_seats
            driver_seats     = driver_trip.seats or 1

            if total_occupied >= driver_seats:
                driver_trip.status = "CONFIRMED"   # fully booked
            else:
                driver_trip.status = "ACTIVE"      # still has room for more passengers

        # Cancel ONLY the passenger's other PENDING matches — the driver can still
        # accept other passengers in remaining seats, so leave driver's other matches.
        other_pax = await session.execute(
            select(Match).where(
                Match.id != match.id,
                Match.status == "PENDING",
                Match.passenger_trip_id == match.passenger_trip_id,
            )
        )
        for om in other_pax.scalars().all():
            om.status = "REJECTED"
            om.rejection_reason = "Пасажир підтвердив іншу поїздку"
            # Free the competing driver's trip only if it has no other pending matches
            other_driver_trip = await session.get(Trip, om.driver_trip_id)
            if other_driver_trip and other_driver_trip.status == "MATCHING":
                still_pending = await session.execute(
                    select(Match).where(
                        Match.driver_trip_id == om.driver_trip_id,
                        Match.status == "PENDING",
                        Match.id != om.id,
                    )
                )
                if not still_pending.scalars().first():
                    other_driver_trip.status = "ACTIVE"

        await session.commit()
        return True

    await session.commit()
    return False


async def reject_match(
    match: Match, reason: str, session: AsyncSession
) -> None:
    match.status = "REJECTED"
    match.rejection_reason = reason

    driver_trip   = await session.get(Trip, match.driver_trip_id)
    passenger_trip = await session.get(Trip, match.passenger_trip_id)

    # Restore passenger trip to ACTIVE (passengers can only be in one ride)
    if passenger_trip and passenger_trip.status == "MATCHING":
        passenger_trip.status = "ACTIVE"

    # Restore driver trip to ACTIVE only if they have no other pending matches
    if driver_trip and driver_trip.status == "MATCHING":
        still_pending = await session.execute(
            select(Match).where(
                Match.driver_trip_id == match.driver_trip_id,
                Match.status == "PENDING",
                Match.id != match.id,
            )
        )
        if not still_pending.scalars().first():
            driver_trip.status = "ACTIVE"

    await session.commit()


async def get_match_for_user(
    match_id: int, user_id: int, session: AsyncSession
) -> Optional[tuple[Match, Trip]]:
    """Return (match, user's trip) if user is a participant of this match."""
    match = await session.get(
        Match, match_id,
        options=[selectinload(Match.driver_trip), selectinload(Match.passenger_trip)],
    )
    if not match:
        return None

    driver_trip = await session.get(Trip, match.driver_trip_id)
    passenger_trip = await session.get(Trip, match.passenger_trip_id)

    if driver_trip and driver_trip.user_id == user_id:
        return match, driver_trip
    if passenger_trip and passenger_trip.user_id == user_id:
        return match, passenger_trip
    return None
