"""
Rich Message trip cards using Bot API 10.1 sendRichMessage.
Falls back to plain HTML answer() if the API call fails.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram.types import InputRichMessage, InlineKeyboardMarkup

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message
    from database.models import Trip, User


def _esc(text: str) -> str:
    return html.escape(str(text))


def fmt_rating(rating: float | None) -> str:
    """Format rating for display: '4.8' or 'Без рейтингу'."""
    return f"{rating:.1f}" if rating is not None else "Без рейтингу"


def short_addr(address: str) -> str:
    """Compact address that always keeps the settlement so two trips' city/village
    can be compared at a glance: 'вул. Одеська, 70, Крюківщина'. The stored address
    is 'street, house, city' (street+house carry an internal comma), so first two
    tokens are the street+house and the last is the settlement."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) <= 2:
        return ", ".join(parts)
    return f"{parts[0]}, {parts[1]}, {parts[-1]}"


def trip_card_html(
    trip: "Trip",
    user: "User",
    dist_km: float | None = None,
    remaining_seats: int | None = None,
    status_label: str | None = None,
) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    role_label = "Водій" if trip.role == "driver" else "Пасажир"
    price_label = f"{trip.price:.0f} грн" if trip.role == "driver" else f"до {trip.price:.0f} грн"
    if trip.role == "driver" and remaining_seats is not None:
        total = trip.seats or 1
        booked = total - remaining_seats
        seats_header = "💺 Заброньовано місць"
        seats_label = f"{booked}/{total}"
    elif trip.role == "driver":
        seats_header = "💺 Місця"
        seats_label = f"{trip.seats} місць"
    else:
        seats_header = "👥 Місця"
        seats_label = f"{trip.seats} пас."
    from_addr = short_addr(trip.from_address)
    to_addr = short_addr(trip.to_address)

    dist_row = f"<tr><th>📍 Відстань</th><td>{dist_km:.1f} км від вас</td></tr>" if dist_km is not None else ""
    status_row = f"<tr><th>📊 Статус</th><td>{_esc(status_label)}</td></tr>" if status_label else ""

    return (
        f"<h2>{role_emoji} {_esc(role_label)}</h2>"
        f"<table>"
        f"<tr><th>Звідки</th><td>{_esc(from_addr)}</td></tr>"
        f"<tr><th>Куди</th><td>{_esc(to_addr)}</td></tr>"
        f"<tr><th>🕒 Час</th><td>{_esc(trip.departure_time.strftime('%d.%m.%Y %H:%M'))}</td></tr>"
        f"<tr><th>💰 Ціна</th><td>{_esc(price_label)}</td></tr>"
        f"<tr><th>{_esc(seats_header)}</th><td>{_esc(seats_label)}</td></tr>"
        f"<tr><th>⭐ Рейтинг</th><td>{_esc(fmt_rating(user.rating))}</td></tr>"
        f"{dist_row}"
        f"{status_row}"
        f"</table>"
    )


def trip_card_plain(
    trip: "Trip",
    user: "User",
    dist_km: float | None = None,
    remaining_seats: int | None = None,
    status_label: str | None = None,
) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    price_str = f"{trip.price:.0f} грн" if trip.role == "driver" else f"до {trip.price:.0f} грн"
    if trip.role == "driver" and remaining_seats is not None:
        total = trip.seats or 1
        booked = total - remaining_seats
        seats_str = f"💺 Заброньовано: {booked}/{total}"
    elif trip.role == "driver":
        seats_str = f"💺 {trip.seats} місць"
    else:
        seats_str = f"👥 {trip.seats} пас."
    from_addr = short_addr(trip.from_address)
    to_addr = short_addr(trip.to_address)
    dist_part = f"  📍 {dist_km:.1f} км" if dist_km is not None else ""
    status_part = f"\n📊 {status_label}" if status_label else ""
    return (
        f"{role_emoji} {from_addr} → {to_addr}\n"
        f"🕒 {trip.departure_time.strftime('%d.%m %H:%M')}  💰 {price_str}  {seats_str}\n"
        f"⭐ {fmt_rating(user.rating)}{dist_part}{status_part}"
    )


async def send_trip_card(
    bot: "Bot",
    chat_id: int,
    trip: "Trip",
    user: "User",
    dist_km: float | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    extra_text: str = "",
    remaining_seats: int | None = None,
    status_label: str | None = None,
) -> None:
    rich_html = trip_card_html(trip, user, dist_km, remaining_seats, status_label)
    if extra_text:
        rich_html += f"<p><i>{_esc(extra_text)}</i></p>"

    try:
        await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(html=rich_html),
            reply_markup=reply_markup,
        )
    except Exception:
        plain = trip_card_plain(trip, user, dist_km, remaining_seats, status_label)
        if extra_text:
            plain += f"\n<i>{extra_text}</i>"
        await bot.send_message(
            chat_id=chat_id,
            text=plain,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
