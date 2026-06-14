import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Trip, Match, User
from services.geo import haversine_km
from config import MATCH_RADIUS_KM, MATCH_TIME_DELTA_HOURS

logger = logging.getLogger(__name__)


async def find_matches_for_trip(
    trip: Trip, session: AsyncSession
) -> list[Trip]:
    """
    Given a newly created trip (driver OR passenger), find the opposite-role
    active trips whose route and time fall within the allowed tolerances.
    """
    opposite_role = "passenger" if trip.role == "driver" else "driver"

    result = await session.execute(
        select(Trip)
        .where(Trip.role == opposite_role, Trip.status == "ACTIVE")
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

        # Origin distance check
        dist_from = haversine_km(
            trip.from_lat, trip.from_lon,
            candidate.from_lat, candidate.from_lon,
        )
        if dist_from > MATCH_RADIUS_KM:
            logger.info("  Skip #%d: origin dist %.2f km > %.1f km", candidate.id, dist_from, MATCH_RADIUS_KM)
            continue

        # Destination distance check
        dist_to = haversine_km(
            trip.to_lat, trip.to_lon,
            candidate.to_lat, candidate.to_lon,
        )
        if dist_to > MATCH_RADIUS_KM:
            logger.info("  Skip #%d: dest dist %.2f km > %.1f km", candidate.id, dist_to, MATCH_RADIUS_KM)
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
        # Load trips to update status
        driver_trip = await session.get(Trip, match.driver_trip_id)
        passenger_trip = await session.get(Trip, match.passenger_trip_id)
        if driver_trip:
            driver_trip.status = "CONFIRMED"
        if passenger_trip:
            passenger_trip.status = "CONFIRMED"
        await session.commit()
        return True

    await session.commit()
    return False


async def reject_match(
    match: Match, reason: str, session: AsyncSession
) -> None:
    match.status = "REJECTED"
    match.rejection_reason = reason

    driver_trip = await session.get(Trip, match.driver_trip_id)
    passenger_trip = await session.get(Trip, match.passenger_trip_id)

    if driver_trip and driver_trip.status == "MATCHING":
        driver_trip.status = "ACTIVE"
    if passenger_trip and passenger_trip.status == "MATCHING":
        passenger_trip.status = "ACTIVE"

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
