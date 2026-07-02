"""
Web admin panel routes for the Car Pool Best Telegram bot.
Mount via setup_admin(app) in main.py.
Auth: ADMIN_TOKEN env var (cookie-based session).
"""
import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from services.timezone import now as _now, KYIV_TZ

import aiohttp_jinja2
import jinja2
from aiohttp import web
from sqlalchemy import select, func, distinct, or_
from sqlalchemy.orm import selectinload

from database.database import AsyncSessionLocal
from database.models import (
    User, Trip, Match, SupportTicket, FAQ, BotSetting, Rating,
    DriverLocation, PassengerLocation,
)

ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "changeme")
_COOKIE = "adm_s"

DEFAULT_SETTINGS: dict[str, str] = {
    # Banner
    "banner_active":        "0",
    "banner_text":          "",
    # Main menu
    "btn_driver":           "🚗 Я водій",
    "btn_passenger":        "🙋 Я пасажир",
    "btn_search":           "🔍 Поїздки поруч",
    "btn_mytrips":          "📋 Мої поїздки",
    "btn_rating":           "⭐ Мій рейтинг",
    "btn_support":          "🛟 Підтримка",
    "btn_faq":              "❓ Часті питання",
    # Match offer
    "btn_confirm":          "✅ Підтвердити",
    "btn_reject":           "❌ Відмовитися",
    # Confirmed — driver
    "btn_departed":         "🚀 Виїхав до попутника",
    "btn_map_driver":       "🗺 Відкрити карту поїздки",
    "btn_cancel_driver":    "❌ Відмінити поїздку",
    # Confirmed — passenger
    "btn_map_passenger":    "🗺 Відстежити водія на карті",
    "btn_cancel_passenger": "❌ Відмінити поїздку",
    # Passenger alert (driver departed)
    "btn_ready":            "✅ Я на місці!",
    "btn_map_pax":          "🗺 Відстежити водія",
    "btn_cancel_pax":       "❌ Відмінити поїздку",
    # Reminder
    "btn_map_rem":          "🗺 Відкрити карту поїздки",
    # Messages
    "msg_welcome":          "Вітаю! Я допоможу знайти попутника або пасажира. Оберіть дію 👇",
    "msg_confirmed_driver": "🎉 Поїздку підтверджено. Натисніть «Виїхав» коли вирушите до пасажира.",
    "msg_confirmed_pax":    "🎉 Поїздку підтверджено. Очікуйте сповіщення від водія.",
    "msg_reminder":         "⏰ Ваша поїздка через 10 хвилин!\n\n🗺 {маршрут}\n🕒 {час}",
    "msg_departed_pax":     "🚗 Водій вже їде до вас! Натисніть «Я на місці» як тільки прийдете.",
    "msg_ready_driver":     "✅ Пасажир вже на місці! Він чекає на вас 🤝",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _token_hash() -> str:
    return hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()


def _is_authed(request: web.Request) -> bool:
    if not ADMIN_TOKEN:
        return True
    return request.cookies.get(_COOKIE, "") == _token_hash()


def _require_auth(handler):
    async def wrapper(request: web.Request):
        if not _is_authed(request):
            raise web.HTTPFound("/admin/login")
        return await handler(request)
    return wrapper


# ── Login / Logout ────────────────────────────────────────────────────────────

async def admin_login_get(request: web.Request) -> web.Response:
    if _is_authed(request):
        raise web.HTTPFound("/admin/")
    return aiohttp_jinja2.render_template("login.html", request, {"error": None})


async def admin_login_post(request: web.Request) -> web.Response:
    data = await request.post()
    if data.get("token", "") == ADMIN_TOKEN:
        resp = web.HTTPFound("/admin/")
        resp.set_cookie(_COOKIE, _token_hash(), httponly=True, max_age=86400 * 30)
        raise resp
    return aiohttp_jinja2.render_template("login.html", request, {"error": "Невірний токен"})


async def admin_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(_COOKIE)
    raise resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _pct(part: int, whole: int) -> int:
    """Integer percentage, safe against division by zero."""
    return round(part / whole * 100) if whole else 0


# ── Date-period filtering for the Dashboard ────────────────────────────────────
# created_at columns are stored as naive UTC (datetime.utcnow default), while all
# user-facing "now"/"today" in this codebase is naive Kyiv time — so period
# boundaries are computed in Kyiv time, then converted to UTC for the DB query.

def _kyiv_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=KYIV_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_kyiv(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(KYIV_TZ).replace(tzinfo=None)


PERIOD_LABELS = {
    "all":        "За весь час",
    "today":      "Сьогодні",
    "month":      "Цього місяця",
    "last_month": "Минулого місяця",
    "year":       "За рік (12 місяців)",
}


def _resolve_period(period: str, from_str: str | None, to_str: str | None):
    """Returns (utc_from: datetime|None, utc_to: datetime|None, label: str, period_key: str)."""
    kn = _now()
    if period == "today":
        k_from = kn.replace(hour=0, minute=0, second=0, microsecond=0)
        return _kyiv_to_utc(k_from), _kyiv_to_utc(kn), PERIOD_LABELS["today"], "today"
    if period == "month":
        k_from = kn.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return _kyiv_to_utc(k_from), _kyiv_to_utc(kn), PERIOD_LABELS["month"], "month"
    if period == "last_month":
        first_this = kn.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_end   = first_this - timedelta(seconds=1)
        last_start = last_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return _kyiv_to_utc(last_start), _kyiv_to_utc(last_end), PERIOD_LABELS["last_month"], "last_month"
    if period == "year":
        k_from = (kn - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)
        return _kyiv_to_utc(k_from), _kyiv_to_utc(kn), PERIOD_LABELS["year"], "year"
    if period == "custom" and from_str and to_str:
        try:
            d_from = datetime.strptime(from_str, "%Y-%m-%d")
            d_to   = datetime.strptime(to_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            if d_from > d_to:
                d_from, d_to = d_to.replace(hour=0, minute=0, second=0), d_from.replace(hour=23, minute=59, second=59)
            label = f"{d_from.strftime('%d.%m.%Y')} — {d_to.strftime('%d.%m.%Y')}"
            return _kyiv_to_utc(d_from), _kyiv_to_utc(d_to), label, "custom"
        except ValueError:
            pass
    return None, None, PERIOD_LABELS["all"], "all"


async def _compute_stats(s, utc_from: datetime | None, utc_to: datetime | None) -> dict:
    """Period-filtered funnel, health KPIs, and cancellation/rejection breakdown."""
    async def cnt(model, *where):
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return (await s.execute(q)).scalar() or 0

    trip_conds  = [Trip.created_at >= utc_from] if utc_from else []
    trip_conds += [Trip.created_at <= utc_to] if utc_to else []
    match_conds  = [Match.created_at >= utc_from] if utc_from else []
    match_conds += [Match.created_at <= utc_to] if utc_to else []
    rating_conds  = [Rating.created_at >= utc_from] if utc_from else []
    rating_conds += [Rating.created_at <= utc_to] if utc_to else []

    trips_total = await cnt(Trip, *trip_conds)
    q = select(Trip.id)
    for w in trip_conds:
        q = q.where(w)
    period_trip_ids = set((await s.execute(q)).scalars().all())

    # Trips (created in period) that ever got at least one match (any time)
    d_ids = set((await s.execute(select(distinct(Match.driver_trip_id)))).scalars().all())
    p_ids = set((await s.execute(select(distinct(Match.passenger_trip_id)))).scalars().all())
    trips_matched = len(period_trip_ids & (d_ids | p_ids))

    matches_total    = await cnt(Match, *match_conds)
    matches_dealt    = await cnt(Match, *match_conds, Match.status.in_(["CONFIRMED", "CLOSED"]))
    matches_departed = await cnt(Match, *match_conds, Match.driver_departed == True)

    q = select(func.count(distinct(Rating.match_id)))
    for w in rating_conds:
        q = q.where(w)
    matches_rated = (await s.execute(q)).scalar() or 0

    match_confirmed = await cnt(Match, *match_conds, Match.status == "CONFIRMED")
    match_cancelled = await cnt(Match, *match_conds, Match.status == "CANCELLED")
    match_closed    = await cnt(Match, *match_conds, Match.status == "CLOSED")

    funnel = [
        ("Поїздок створено", trips_total,      100 if trips_total else 0),
        ("Співпадінь",       trips_matched,    _pct(trips_matched, trips_total)),
        ("Підтверджено",     matches_dealt,    _pct(matches_dealt, trips_total)),
        ("Виїзд відбувся",   matches_departed, _pct(matches_departed, trips_total)),
        ("Оцінено",          matches_rated,    _pct(matches_rated, trips_total)),
    ]
    match_success = _pct(matches_dealt, matches_total)
    completion    = _pct(matches_departed, matches_dealt)
    rating_cov    = _pct(matches_rated, matches_dealt)

    q = select(func.count(distinct(Trip.user_id)))
    for w in trip_conds:
        q = q.where(w)
    active_users = (await s.execute(q)).scalar() or 0

    # Cancellation / rejection reason breakdown — approximated by Match.created_at,
    # since there's no separate "cancelled at" timestamp on the model.
    q = select(Match).where(Match.status.in_(["CANCELLED", "REJECTED"]))
    for w in match_conds:
        q = q.where(w)
    cancel_rows = (await s.execute(q)).scalars().all()

    driver_reasons: dict[str, int] = {}
    passenger_reasons: dict[str, int] = {}
    rejection_counts: dict[str, int] = {}
    for m in cancel_rows:
        cr = m.cancel_reason
        if cr:
            if m.cancelled_by == "driver":
                driver_reasons[cr] = driver_reasons.get(cr, 0) + 1
            elif m.cancelled_by == "passenger":
                passenger_reasons[cr] = passenger_reasons.get(cr, 0) + 1
        rr = m.rejection_reason
        if rr:
            rejection_counts[rr] = rejection_counts.get(rr, 0) + 1

    return {
        "funnel": funnel,
        "match_success": match_success,
        "completion": completion,
        "rating_cov": rating_cov,
        "active_users": active_users,
        "match_confirmed": match_confirmed,
        "match_cancelled": match_cancelled,
        "match_closed": match_closed,
        "driver_reasons":    sorted(driver_reasons.items(),    key=lambda x: -x[1]),
        "passenger_reasons": sorted(passenger_reasons.items(), key=lambda x: -x[1]),
        "rejections":        sorted(rejection_counts.items(),  key=lambda x: -x[1]),
    }


async def _compute_chart(s, period_key: str, utc_from: datetime | None, utc_to: datetime | None) -> dict:
    """New-users / new-trips chart, bucketed hourly (today), daily (month-scale), or
    monthly (year / all-time / long custom range)."""
    kn = _now()
    if period_key == "today":
        bucket_type = "hour"
        start_kyiv, end_kyiv = kn.replace(hour=0, minute=0, second=0, microsecond=0), kn
    elif period_key == "all":
        bucket_type = "month"
        earliest = (await s.execute(select(func.min(Trip.created_at)))).scalar()
        earliest_u = (await s.execute(select(func.min(User.created_at)))).scalar()
        cands = [d for d in (earliest, earliest_u) if d is not None]
        start_kyiv = _utc_to_kyiv(min(cands)) if cands else (kn - timedelta(days=365))
        start_kyiv = max(start_kyiv, kn - timedelta(days=36 * 31))  # cap history to ~36 months
        end_kyiv = kn
    else:
        start_kyiv, end_kyiv = _utc_to_kyiv(utc_from), _utc_to_kyiv(utc_to)
        if period_key in ("month", "last_month"):
            bucket_type = "day"
        elif period_key == "year":
            bucket_type = "month"
        else:  # custom
            bucket_type = "day" if (end_kyiv.date() - start_kyiv.date()).days <= 62 else "month"

    buckets: list[tuple[datetime, datetime, str]] = []
    if bucket_type == "hour":
        day0 = start_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
        for h in range(24):
            bs = day0 + timedelta(hours=h)
            if bs > end_kyiv:
                break
            buckets.append((_kyiv_to_utc(bs), _kyiv_to_utc(bs + timedelta(hours=1)), f"{h:02d}:00"))
    elif bucket_type == "day":
        d = start_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = end_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= end_day:
            buckets.append((_kyiv_to_utc(d), _kyiv_to_utc(d + timedelta(days=1)), d.strftime("%d.%m")))
            d += timedelta(days=1)
    else:  # month
        cur = start_kyiv.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_month = end_kyiv.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months = []
        while cur <= end_month and len(months) < 37:
            months.append(cur)
            cur = (cur.replace(year=cur.year + 1, month=1) if cur.month == 12
                   else cur.replace(month=cur.month + 1))
        for i, m in enumerate(months):
            nxt = months[i + 1] if i + 1 < len(months) else (
                m.replace(year=m.year + 1, month=1) if m.month == 12 else m.replace(month=m.month + 1))
            buckets.append((_kyiv_to_utc(m), _kyiv_to_utc(nxt), m.strftime("%m.%Y")))

    if not buckets:
        return {"labels": [], "users": [], "trips": []}

    range_start, range_end = buckets[0][0], buckets[-1][1]
    user_dts = (await s.execute(
        select(User.created_at).where(User.created_at >= range_start, User.created_at < range_end)
    )).scalars().all()
    trip_dts = (await s.execute(
        select(Trip.created_at).where(Trip.created_at >= range_start, Trip.created_at < range_end)
    )).scalars().all()

    def _idx(dt):
        for i, (bs, be, _l) in enumerate(buckets):
            if bs <= dt < be:
                return i
        return None

    users_counts = [0] * len(buckets)
    for dt in user_dts:
        i = _idx(dt)
        if i is not None:
            users_counts[i] += 1
    trips_counts = [0] * len(buckets)
    for dt in trip_dts:
        i = _idx(dt)
        if i is not None:
            trips_counts[i] += 1

    return {"labels": [b[2] for b in buckets], "users": users_counts, "trips": trips_counts}


@_require_auth
async def admin_dashboard(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        async def cnt(model, *where):
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return (await s.execute(q)).scalar() or 0

        users_total     = await cnt(User)
        users_blocked   = await cnt(User, User.is_blocked == True)
        trips_active    = await cnt(Trip, Trip.status == "ACTIVE")
        trips_confirmed = await cnt(Trip, Trip.status == "CONFIRMED")
        trips_closed    = await cnt(Trip, Trip.status == "CLOSED")
        unread_tickets  = await cnt(SupportTicket, SupportTicket.is_read == False)

        # Avg rating is a reputation snapshot, not a period event — always all-time.
        avg_rating = (await s.execute(
            select(func.avg(User.rating)).where(User.rating.isnot(None))
        )).scalar()

        recent_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.status.in_(["ACTIVE", "CONFIRMED", "MATCHING", "BOARDING", "IN_PROGRESS"]))
            .order_by(Trip.created_at.desc()).limit(5)
        )).scalars().all()

        # Initial paint = "За весь час", matching the default active filter button.
        utc_from, utc_to, label, period_key = _resolve_period("all", None, None)
        period_stats = await _compute_stats(s, utc_from, utc_to)
        chart = await _compute_chart(s, period_key, utc_from, utc_to)

    ctx = {
        "active": "dashboard",
        "users_total": users_total,
        "users_blocked": users_blocked,
        "trips_active": trips_active,
        "trips_confirmed": trips_confirmed,
        "trips_closed": trips_closed,
        "unread_tickets": unread_tickets,
        "recent_trips": recent_trips,
        "avg_rating": f"{avg_rating:.2f}" if avg_rating is not None else "—",
        "period_label": label,
        **period_stats,
        "chart_json": json.dumps(chart, ensure_ascii=False),
    }
    return aiohttp_jinja2.render_template("dashboard.html", request, ctx)


async def api_dashboard_stats(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    period   = request.rel_url.query.get("period", "all")
    from_str = request.rel_url.query.get("from")
    to_str   = request.rel_url.query.get("to")
    utc_from, utc_to, label, period_key = _resolve_period(period, from_str, to_str)
    async with AsyncSessionLocal() as s:
        stats = await _compute_stats(s, utc_from, utc_to)
        chart = await _compute_chart(s, period_key, utc_from, utc_to)
    return web.json_response({"label": label, **stats, "chart": chart})


# ── Trips ─────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_trips(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "ACTIVE")
    async with AsyncSessionLocal() as s:
        q = select(Trip).options(selectinload(Trip.user)).order_by(Trip.departure_time.desc()).limit(200)
        if status_filter != "ALL":
            q = q.where(Trip.status == status_filter)
        trips = (await s.execute(q)).scalars().all()

    trips_json = json.dumps([{
        "id": t.id, "role": t.role, "status": t.status,
        "from_addr": t.from_address or "", "to_addr": t.to_address or "",
        "from_lat": t.from_lat, "from_lon": t.from_lon,
        "to_lat": t.to_lat, "to_lon": t.to_lon,
        "time": t.departure_time.strftime("%d.%m %H:%M"),
        "price": t.price, "seats": t.seats,
        "user": t.user.first_name if t.user else "?",
        "username": t.user.username if t.user else None,
    } for t in trips], ensure_ascii=False)

    return aiohttp_jinja2.render_template("trips.html", request, {
        "active": "trips", "trips": trips,
        "status_filter": status_filter, "trips_json": trips_json,
    })


@_require_auth
async def admin_trip_detail(request: web.Request) -> web.Response:
    tid = int(request.match_info["trip_id"])
    async with AsyncSessionLocal() as s:
        trip = await s.get(Trip, tid, options=[selectinload(Trip.user)])
        if not trip:
            raise web.HTTPFound("/admin/trips")
        # All matches this trip participates in (either side)
        matches = (await s.execute(
            select(Match).options(
                selectinload(Match.driver_trip).selectinload(Trip.user),
                selectinload(Match.passenger_trip).selectinload(Trip.user),
            ).where(or_(Match.driver_trip_id == tid, Match.passenger_trip_id == tid))
            .order_by(Match.created_at.desc())
        )).scalars().all()
    return aiohttp_jinja2.render_template("trip_detail.html", request, {
        "active": "trips", "t": trip, "matches": matches,
    })


@_require_auth
async def admin_trip_close(request: web.Request) -> web.Response:
    tid = int(request.match_info["trip_id"])
    async with AsyncSessionLocal() as s:
        trip = await s.get(Trip, tid)
        if trip:
            trip.status = "CLOSED"
            await s.commit()
    raise web.HTTPFound(request.headers.get("Referer", f"/admin/trips/{tid}"))


# ── Users ─────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_users(request: web.Request) -> web.Response:
    search = request.rel_url.query.get("q", "").strip()
    blocked = request.rel_url.query.get("blocked", "")
    async with AsyncSessionLocal() as s:
        q = select(User).order_by(User.created_at.desc()).limit(300)
        if blocked == "1":
            q = q.where(User.is_blocked == True)
        elif blocked == "0":
            q = q.where(User.is_blocked == False)
        users = (await s.execute(q)).scalars().all()
    if search:
        sl = search.lower()
        users = [u for u in users if sl in (u.username or "").lower()
                 or sl in str(u.id) or sl in u.first_name.lower()]
    return aiohttp_jinja2.render_template("users.html", request, {
        "active": "users", "users": users, "search": search, "blocked": blocked,
    })


@_require_auth
async def admin_user_detail(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    async with AsyncSessionLocal() as s:
        user = await s.get(User, uid)
        if not user:
            raise web.HTTPFound("/admin/users")
        trips = (await s.execute(
            select(Trip).where(Trip.user_id == uid).order_by(Trip.created_at.desc()).limit(30)
        )).scalars().all()
        # Ratings received
        rrows = (await s.execute(
            select(Rating).where(Rating.to_user_id == uid).order_by(Rating.created_at.desc()).limit(30)
        )).scalars().all()
        # Tickets
        tickets = (await s.execute(
            select(SupportTicket).where(SupportTicket.user_id == uid)
            .order_by(SupportTicket.created_at.desc()).limit(20)
        )).scalars().all()
    return aiohttp_jinja2.render_template("user_detail.html", request, {
        "active": "users", "u": user, "trips": trips,
        "ratings": rrows, "tickets": tickets,
        "sent": request.rel_url.query.get("sent"),
    })


@_require_auth
async def admin_user_message(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    data = await request.post()
    text = (data.get("text") or "").strip()
    bot = request.app.get("bot")
    ok = False
    if text and bot:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            ok = True
        except Exception:
            ok = False
    raise web.HTTPFound(f"/admin/users/{uid}?sent={'1' if ok else '0'}")


@_require_auth
async def admin_block_user(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    async with AsyncSessionLocal() as s:
        user = await s.get(User, uid)
        if user:
            user.is_blocked = True
            await s.commit()
    raise web.HTTPFound(request.headers.get("Referer", "/admin/users"))


@_require_auth
async def admin_unblock_user(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    async with AsyncSessionLocal() as s:
        user = await s.get(User, uid)
        if user:
            user.is_blocked = False
            await s.commit()
    raise web.HTTPFound(request.headers.get("Referer", "/admin/users"))


# ── Matches ───────────────────────────────────────────────────────────────────

@_require_auth
async def admin_matches(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "CONFIRMED")
    async with AsyncSessionLocal() as s:
        q = (select(Match)
             .options(
                 selectinload(Match.driver_trip).selectinload(Trip.user),
                 selectinload(Match.passenger_trip).selectinload(Trip.user),
             )
             .order_by(Match.created_at.desc()).limit(100))
        if status_filter != "ALL":
            q = q.where(Match.status == status_filter)
        matches = (await s.execute(q)).scalars().all()
    return aiohttp_jinja2.render_template("matches.html", request, {
        "active": "matches", "matches": matches, "status_filter": status_filter,
    })


@_require_auth
async def admin_match_detail(request: web.Request) -> web.Response:
    mid = int(request.match_info["match_id"])
    async with AsyncSessionLocal() as s:
        match = await s.get(Match, mid, options=[
            selectinload(Match.driver_trip).selectinload(Trip.user),
            selectinload(Match.passenger_trip).selectinload(Trip.user),
        ])
        if not match:
            raise web.HTTPFound("/admin/matches")
        d_loc = await s.get(DriverLocation, mid)
        p_loc = await s.get(PassengerLocation, mid)
    return aiohttp_jinja2.render_template("match_detail.html", request, {
        "active": "matches", "m": match, "d_loc": d_loc, "p_loc": p_loc,
        "done": request.rel_url.query.get("done"),
    })


@_require_auth
async def admin_match_force(request: web.Request) -> web.Response:
    """Admin forces a match to CONFIRMED or CANCELLED and notifies both sides."""
    mid = int(request.match_info["match_id"])
    action = request.match_info["action"]  # "confirm" | "cancel"
    async with AsyncSessionLocal() as s:
        m = await s.get(Match, mid)
        if not m:
            raise web.HTTPFound("/admin/matches")
        driver = await s.get(Trip, m.driver_trip_id)
        passenger = await s.get(Trip, m.passenger_trip_id)

        if action == "confirm":
            m.driver_confirmed = True
            m.passenger_confirmed = True
            m.status = "CONFIRMED"
            if passenger:
                passenger.status = "CONFIRMED"
            if driver:
                driver.status = "CONFIRMED"
            note = "✅ <b>Адміністратор підтвердив вашу поїздку.</b>"
        else:  # cancel
            m.status = "CANCELLED"
            m.cancelled_by = "admin"
            if passenger and passenger.status in ("MATCHING", "CONFIRMED"):
                passenger.status = "ACTIVE"
            if driver and driver.status in ("MATCHING", "CONFIRMED", "BOARDING"):
                driver.status = "ACTIVE"
            note = "❌ <b>Адміністратор скасував цей матч.</b>"
        d_uid = driver.user_id if driver else None
        p_uid = passenger.user_id if passenger else None
        await s.commit()

    bot = request.app.get("bot")
    if bot:
        for uid in (d_uid, p_uid):
            if uid:
                try:
                    await bot.send_message(uid, note, parse_mode="HTML")
                except Exception:
                    pass
    raise web.HTTPFound(f"/admin/matches/{mid}?done={action}")


# ── Tickets ───────────────────────────────────────────────────────────────────

@_require_auth
async def admin_tickets(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        tickets = (await s.execute(
            select(SupportTicket).options(selectinload(SupportTicket.user))
            .order_by(SupportTicket.is_read, SupportTicket.created_at.desc()).limit(200)
        )).scalars().all()
    return aiohttp_jinja2.render_template("tickets.html", request, {
        "active": "tickets", "tickets": tickets,
    })


@_require_auth
async def admin_ticket_read(request: web.Request) -> web.Response:
    tid = int(request.match_info["ticket_id"])
    async with AsyncSessionLocal() as s:
        ticket = await s.get(SupportTicket, tid)
        if ticket:
            ticket.is_read = True
            await s.commit()
    raise web.HTTPFound("/admin/tickets")


# ── FAQ ───────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_faq(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        faqs = (await s.execute(select(FAQ).order_by(FAQ.order_idx, FAQ.id))).scalars().all()
    return aiohttp_jinja2.render_template("faq.html", request, {"active": "faq", "faqs": faqs})


@_require_auth
async def admin_faq_add(request: web.Request) -> web.Response:
    data = await request.post()
    q, a = data.get("question", "").strip(), data.get("answer", "").strip()
    if q and a:
        async with AsyncSessionLocal() as s:
            s.add(FAQ(question=q, answer=a))
            await s.commit()
    raise web.HTTPFound("/admin/faq")


@_require_auth
async def admin_faq_edit(request: web.Request) -> web.Response:
    fid = int(request.match_info["faq_id"])
    data = await request.post()
    q, a = data.get("question", "").strip(), data.get("answer", "").strip()
    async with AsyncSessionLocal() as s:
        faq = await s.get(FAQ, fid)
        if faq and q and a:
            faq.question, faq.answer = q, a
            await s.commit()
    raise web.HTTPFound("/admin/faq")


@_require_auth
async def admin_faq_delete(request: web.Request) -> web.Response:
    fid = int(request.match_info["faq_id"])
    async with AsyncSessionLocal() as s:
        faq = await s.get(FAQ, fid)
        if faq:
            await s.delete(faq)
            await s.commit()
    raise web.HTTPFound("/admin/faq")


# ── Workflow schema ────────────────────────────────────────────────────────────

@_require_auth
async def admin_workflow(request: web.Request) -> web.Response:
    return aiohttp_jinja2.render_template("workflow.html", request, {"active": "workflow"})


# ── Settings ──────────────────────────────────────────────────────────────────

@_require_auth
async def admin_settings(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(BotSetting))).scalars().all()
    db_map = {r.key: r.value for r in rows}
    settings = [{
        "key": k,
        "value": db_map.get(k, v),
        "default": v,
    } for k, v in DEFAULT_SETTINGS.items()]
    return aiohttp_jinja2.render_template("settings.html", request, {
        "active": "settings", "settings": settings,
    })


@_require_auth
async def admin_settings_save(request: web.Request) -> web.Response:
    data = await request.post()
    async with AsyncSessionLocal() as s:
        for key, default in DEFAULT_SETTINGS.items():
            value = data.get(key, "").strip()
            if not value:
                value = default
            row = await s.get(BotSetting, key)
            if row:
                row.value = value
            else:
                s.add(BotSetting(key=key, value=value))
        await s.commit()
    raise web.HTTPFound("/admin/settings")


# ── JSON API ──────────────────────────────────────────────────────────────────

async def api_settings_get(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(BotSetting))).scalars().all()
    db_map = {r.key: r.value for r in rows}
    merged = {k: db_map.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    return web.json_response(merged)


async def api_settings_set(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    key = request.match_info["key"]
    if key not in DEFAULT_SETTINGS:
        return web.json_response({"error": "unknown key"}, status=400)
    try:
        data = await request.json()
        value = str(data.get("value", "")).strip()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not value:
        return web.json_response({"error": "empty value"}, status=400)
    async with AsyncSessionLocal() as s:
        row = await s.get(BotSetting, key)
        if row:
            row.value = value
        else:
            s.add(BotSetting(key=key, value=value))
        await s.commit()
    return web.json_response({"ok": True, "key": key, "value": value})


async def api_trips_map(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    async with AsyncSessionLocal() as s:
        trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.status.in_(["ACTIVE", "CONFIRMED", "MATCHING"]))
        )).scalars().all()
    features = []
    for t in trips:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [t.from_lon, t.from_lat]},
            "properties": {
                "id": t.id, "role": t.role, "status": t.status,
                "from_addr": t.from_address, "to_addr": t.to_address,
                "from_lat": t.from_lat, "from_lon": t.from_lon,
                "to_lat": t.to_lat, "to_lon": t.to_lon,
                "time": t.departure_time.strftime("%d.%m %H:%M"),
                "price": t.price, "seats": t.seats,
                "user": t.user.first_name if t.user else "?",
                "username": t.user.username if t.user else None,
            }
        })
    return web.json_response({"type": "FeatureCollection", "features": features})


# ── Broadcast ────────────────────────────────────────────────────────────────

# Live state of the most recent broadcast (single-flight; process-local).
_broadcast_state: dict = {
    "running": False, "total": 0, "sent": 0, "failed": 0,
    "segment": "", "started_at": None, "finished_at": None,
}

SEGMENT_LABELS = {
    "all":       "Всі користувачі",
    "drivers":   "Водії (мали поїздку-водія)",
    "passengers":"Пасажири (мали заявку)",
    "active7":   "Активні за 7 днів",
    "active30":  "Активні за 30 днів",
    "city":      "За містом",
}


async def _resolve_segment(s, segment: str, city: str) -> list[int]:
    """Return non-blocked user IDs matching the chosen broadcast segment."""
    now = _now()
    if segment == "drivers":
        ids = (await s.execute(select(distinct(Trip.user_id)).where(Trip.role == "driver"))).scalars().all()
    elif segment == "passengers":
        ids = (await s.execute(select(distinct(Trip.user_id)).where(Trip.role == "passenger"))).scalars().all()
    elif segment in ("active7", "active30"):
        days = 7 if segment == "active7" else 30
        ids = (await s.execute(
            select(distinct(Trip.user_id)).where(Trip.created_at >= now - timedelta(days=days))
        )).scalars().all()
    elif segment == "city" and city:
        ids = (await s.execute(
            select(User.id).where(func.lower(User.home_city) == city.lower())
        )).scalars().all()
    else:  # all
        ids = (await s.execute(select(User.id))).scalars().all()

    if not ids:
        return []
    # Drop blocked users
    blocked = set((await s.execute(
        select(User.id).where(User.id.in_(ids), User.is_blocked == True)
    )).scalars().all())
    return [uid for uid in set(ids) if uid not in blocked]


async def _run_broadcast(bot, user_ids: list[int], text: str, parse_mode: str) -> None:
    """Background sender. Updates _broadcast_state as it goes."""
    st = _broadcast_state
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode=parse_mode or None)
            st["sent"] += 1
        except Exception:
            st["failed"] += 1
        await asyncio.sleep(0.05)  # ~20 msg/s, under Telegram's 30/s cap
    st["running"] = False
    st["finished_at"] = _now().strftime("%H:%M:%S")


@_require_auth
async def admin_broadcast_get(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        bt = await s.get(BotSetting, "banner_text")
        ba = await s.get(BotSetting, "banner_active")
    return aiohttp_jinja2.render_template("broadcast.html", request, {
        "active": "broadcast", "error": None,
        "banner_text":   bt.value if bt else "",
        "banner_active": ba.value if ba else "0",
        "state": _broadcast_state, "segment_labels": SEGMENT_LABELS,
    })


@_require_auth
async def admin_broadcast_post(request: web.Request) -> web.Response:
    data = await request.post()
    text = (data.get("text") or "").strip()
    parse_mode = data.get("parse_mode", "HTML")
    segment = data.get("segment", "all")
    city = (data.get("city") or "").strip()

    async with AsyncSessionLocal() as s:
        bt = await s.get(BotSetting, "banner_text")
        ba = await s.get(BotSetting, "banner_active")
    ctx_banner = {"banner_text": bt.value if bt else "", "banner_active": ba.value if ba else "0"}

    def err(msg):
        return aiohttp_jinja2.render_template("broadcast.html", request, {
            "active": "broadcast", "error": msg, "state": _broadcast_state,
            "segment_labels": SEGMENT_LABELS, **ctx_banner,
        })

    if not text:
        return err("Порожнє повідомлення")
    if _broadcast_state["running"]:
        return err("Розсилка вже виконується — дочекайтесь завершення")
    bot = request.app.get("bot")
    if not bot:
        return err("Bot не ініціалізовано — перезапустіть сервер")

    async with AsyncSessionLocal() as s:
        user_ids = await _resolve_segment(s, segment, city)
    if not user_ids:
        return err("За цим сегментом немає отримувачів")

    _broadcast_state.update({
        "running": True, "total": len(user_ids), "sent": 0, "failed": 0,
        "segment": SEGMENT_LABELS.get(segment, segment) + (f" «{city}»" if segment == "city" else ""),
        "started_at": _now().strftime("%H:%M:%S"), "finished_at": None,
    })
    # Fire-and-forget; progress is polled via /admin/api/broadcast/status
    asyncio.create_task(_run_broadcast(bot, user_ids, text, parse_mode))

    raise web.HTTPFound("/admin/broadcast")


async def api_broadcast_status(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(_broadcast_state)


# ── Banner save ───────────────────────────────────────────────────────────────

@_require_auth
async def admin_banner_save(request: web.Request) -> web.Response:
    data = await request.post()
    banner_text   = data.get("banner_text",   "").strip()
    banner_active = "1" if data.get("banner_active", "0") == "1" else "0"
    async with AsyncSessionLocal() as s:
        for key, value in (("banner_text", banner_text), ("banner_active", banner_active)):
            row = await s.get(BotSetting, key)
            if row:
                row.value = value
            else:
                s.add(BotSetting(key=key, value=value))
        await s.commit()
    raise web.HTTPFound("/admin/broadcast")


# ── Trust & Safety ────────────────────────────────────────────────────────────

@_require_auth
async def admin_safety(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        # Rating distribution 1..5
        dist_rows = (await s.execute(
            select(Rating.score, func.count()).group_by(Rating.score)
        )).all()
        dist = {i: 0 for i in range(1, 6)}
        for score, c in dist_rows:
            if score in dist:
                dist[score] = c
        total_ratings = sum(dist.values())
        avg_rating = (await s.execute(
            select(func.avg(Rating.score))
        )).scalar()

        # Per-user received-rating aggregate (avg + count)
        agg = (await s.execute(
            select(Rating.to_user_id, func.count(), func.avg(Rating.score))
            .group_by(Rating.to_user_id)
        )).all()
        # Low-rated users: avg < 4.0 with at least 2 ratings received
        low_ids = [uid for uid, c, avg in agg if avg is not None and avg < 4.0 and c >= 2]
        agg_map = {uid: (c, avg) for uid, c, avg in agg}
        low_users = []
        if low_ids:
            urows = (await s.execute(select(User).where(User.id.in_(low_ids)))).scalars().all()
            for u in urows:
                c, avg = agg_map.get(u.id, (0, None))
                low_users.append({
                    "id": u.id, "name": u.first_name, "username": u.username,
                    "avg": round(avg, 2) if avg is not None else None, "count": c,
                    "failed": u.failed_trips or 0, "blocked": u.is_blocked,
                })
            low_users.sort(key=lambda x: (x["avg"] if x["avg"] is not None else 9))

        # Recent low ratings (≤2) with names
        low_rows = (await s.execute(
            select(Rating).where(Rating.score <= 2).order_by(Rating.created_at.desc()).limit(25)
        )).scalars().all()
        uid_set = set()
        for r in low_rows:
            uid_set.add(r.from_user_id); uid_set.add(r.to_user_id)
        names = {}
        if uid_set:
            for u in (await s.execute(select(User).where(User.id.in_(uid_set)))).scalars().all():
                names[u.id] = u.first_name or str(u.id)
        recent_low = [{
            "score": r.score,
            "from": names.get(r.from_user_id, str(r.from_user_id)),
            "to": names.get(r.to_user_id, str(r.to_user_id)),
            "to_id": r.to_user_id,
            "match_id": r.match_id,
            "date": r.created_at.strftime("%d.%m %H:%M") if r.created_at else "",
        } for r in low_rows]

        # Most-cancelled users (by User.failed_trips)
        worst = (await s.execute(
            select(User).where(User.failed_trips > 0)
            .order_by(User.failed_trips.desc()).limit(10)
        )).scalars().all()
        worst_users = [{
            "id": u.id, "name": u.first_name, "username": u.username,
            "failed": u.failed_trips or 0, "success": u.successful_trips or 0,
            "blocked": u.is_blocked,
        } for u in worst]

    return aiohttp_jinja2.render_template("safety.html", request, {
        "active": "safety",
        "dist": dist, "total_ratings": total_ratings,
        "avg_rating": f"{avg_rating:.2f}" if avg_rating is not None else "—",
        "low_users": low_users,
        "recent_low": recent_low,
        "worst_users": worst_users,
    })


# ── Manual Match ──────────────────────────────────────────────────────────────

@_require_auth
async def admin_manual_match_get(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        driver_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.role == "driver", Trip.status == "ACTIVE")
            .order_by(Trip.departure_time)
        )).scalars().all()
        passenger_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.role == "passenger", Trip.status == "ACTIVE")
            .order_by(Trip.departure_time)
        )).scalars().all()
    return aiohttp_jinja2.render_template("manual_match.html", request, {
        "active": "matches",
        "driver_trips":    driver_trips,
        "passenger_trips": passenger_trips,
        "result": None, "error": None, "match_id": None,
    })


@_require_auth
async def admin_manual_match_post(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        dtid = int(data.get("driver_trip_id", 0))
        ptid = int(data.get("passenger_trip_id", 0))
    except (ValueError, TypeError):
        dtid = ptid = 0

    if not dtid or not ptid:
        return aiohttp_jinja2.render_template("manual_match.html", request, {
            "active": "matches", "driver_trips": [], "passenger_trips": [],
            "result": "error", "error": "Оберіть обидві поїздки", "match_id": None,
        })

    async with AsyncSessionLocal() as s:
        driver_trip    = await s.get(Trip, dtid)
        passenger_trip = await s.get(Trip, ptid)

        if not driver_trip or not passenger_trip:
            return aiohttp_jinja2.render_template("manual_match.html", request, {
                "active": "matches", "driver_trips": [], "passenger_trips": [],
                "result": "error", "error": "Поїздку не знайдено", "match_id": None,
            })

        existing = (await s.execute(
            select(Match).where(
                Match.driver_trip_id == dtid,
                Match.passenger_trip_id == ptid,
                Match.status.in_(["PENDING", "CONFIRMED"]),
            )
        )).scalars().first()

        if existing:
            return aiohttp_jinja2.render_template("manual_match.html", request, {
                "active": "matches", "driver_trips": [], "passenger_trips": [],
                "result": "error", "error": f"Матч вже існує (#{existing.id})", "match_id": None,
            })

        match = Match(
            driver_trip_id=dtid,
            passenger_trip_id=ptid,
            status="PENDING",
        )
        s.add(match)
        driver_trip.status    = "MATCHING"
        passenger_trip.status = "MATCHING"
        await s.commit()
        await s.refresh(match)
        match_id = match.id

        d_user_id = driver_trip.user_id
        p_user_id = passenger_trip.user_id
        d_from = driver_trip.from_address.split(",")[0]
        d_to   = driver_trip.to_address.split(",")[0]
        p_from = passenger_trip.from_address.split(",")[0]
        p_to   = passenger_trip.to_address.split(",")[0]

    bot = request.app.get("bot")
    if bot:
        try:
            await bot.send_message(
                d_user_id,
                f"🤝 <b>Новий матч (підібраний адміном)</b>\n\n"
                f"📍 Маршрут пасажира: {p_from} → {p_to}\n\n"
                f"Перевірте деталі та підтвердіть поїздку.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await bot.send_message(
                p_user_id,
                f"🤝 <b>Новий матч (підібраний адміном)</b>\n\n"
                f"🚗 Маршрут водія: {d_from} → {d_to}\n\n"
                f"Перевірте деталі та підтвердіть поїздку.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return aiohttp_jinja2.render_template("manual_match.html", request, {
        "active": "matches", "driver_trips": [], "passenger_trips": [],
        "result": "ok", "match_id": match_id, "error": None,
    })


# ── CSV Export ────────────────────────────────────────────────────────────────

@_require_auth
async def admin_export_users(request: web.Request) -> web.Response:
    import csv, io
    async with AsyncSessionLocal() as s:
        users = (await s.execute(
            select(User).order_by(User.created_at.desc())
        )).scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Ім'я", "Username", "Рейтинг", "Поїздок", "Успішних", "Заблоковано", "Зареєстрований"])
    for u in users:
        w.writerow([
            u.id, u.first_name, u.username or "",
            f"{u.rating:.1f}" if u.rating is not None else "",
            u.trips_count or 0,
            u.successful_trips or 0,
            "Так" if u.is_blocked else "Ні",
            u.created_at.strftime("%d.%m.%Y %H:%M") if u.created_at else "",
        ])
    return web.Response(
        body=out.getvalue().encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@_require_auth
async def admin_export_trips(request: web.Request) -> web.Response:
    import csv, io
    async with AsyncSessionLocal() as s:
        trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user)).order_by(Trip.created_at.desc())
        )).scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Роль", "Користувач", "Звідки", "Куди", "Час виїзду", "Ціна", "Місця", "Статус", "Створено"])
    for t in trips:
        w.writerow([
            t.id,
            "Водій" if t.role == "driver" else "Пасажир",
            t.user.first_name if t.user else "",
            t.from_address, t.to_address,
            t.departure_time.strftime("%d.%m.%Y %H:%M") if t.departure_time else "",
            t.price or "", t.seats or "", t.status,
            t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else "",
        ])
    return web.Response(
        body=out.getvalue().encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trips.csv"},
    )


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_admin(app: web.Application) -> None:
    templates_path = str(Path(__file__).parent / "templates")
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(templates_path))

    # Auth
    app.router.add_get("/admin/login",   admin_login_get)
    app.router.add_post("/admin/login",  admin_login_post)
    app.router.add_get("/admin/logout",  admin_logout)

    # Pages
    app.router.add_get("/admin",         lambda r: web.HTTPFound("/admin/"))
    app.router.add_get("/admin/",        admin_dashboard)
    app.router.add_get("/admin/trips",   admin_trips)
    app.router.add_get("/admin/trips/{trip_id:\\d+}",        admin_trip_detail)
    app.router.add_post("/admin/trips/{trip_id:\\d+}/close", admin_trip_close)
    app.router.add_get("/admin/users",   admin_users)
    app.router.add_get("/admin/users/{user_id:\\d+}",         admin_user_detail)
    app.router.add_post("/admin/users/{user_id}/message",     admin_user_message)
    app.router.add_post("/admin/users/{user_id}/block",   admin_block_user)
    app.router.add_post("/admin/users/{user_id}/unblock", admin_unblock_user)
    app.router.add_get("/admin/matches", admin_matches)
    app.router.add_get("/admin/matches/{match_id:\\d+}",                 admin_match_detail)
    app.router.add_post("/admin/matches/{match_id:\\d+}/{action}",       admin_match_force)
    app.router.add_get("/admin/safety",  admin_safety)
    app.router.add_get("/admin/tickets", admin_tickets)
    app.router.add_post("/admin/tickets/{ticket_id}/read", admin_ticket_read)
    app.router.add_get("/admin/faq",     admin_faq)
    app.router.add_post("/admin/faq",    admin_faq_add)
    app.router.add_post("/admin/faq/{faq_id}/edit",   admin_faq_edit)
    app.router.add_post("/admin/faq/{faq_id}/delete", admin_faq_delete)
    app.router.add_get("/admin/workflow",  admin_workflow)
    app.router.add_get("/admin/settings",  admin_settings)
    app.router.add_post("/admin/settings", admin_settings_save)

    # Broadcast
    app.router.add_get ("/admin/broadcast",          admin_broadcast_get)
    app.router.add_post("/admin/broadcast",          admin_broadcast_post)
    app.router.add_post("/admin/broadcast/banner",   admin_banner_save)
    app.router.add_get ("/admin/api/broadcast/status", api_broadcast_status)
    # Manual match
    app.router.add_get ("/admin/manual-match",       admin_manual_match_get)
    app.router.add_post("/admin/manual-match",       admin_manual_match_post)
    # CSV export
    app.router.add_get ("/admin/users/export.csv",   admin_export_users)
    app.router.add_get ("/admin/trips/export.csv",   admin_export_trips)
    # JSON API
    app.router.add_get ("/admin/api/settings",       api_settings_get)
    app.router.add_post("/admin/api/settings/{key}", api_settings_set)
    app.router.add_get ("/admin/api/trips/map",      api_trips_map)
    app.router.add_get ("/admin/api/dashboard/stats", api_dashboard_stats)
