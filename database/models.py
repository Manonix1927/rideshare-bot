from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, DateTime, Boolean, ForeignKey, Text
)
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)  # Telegram user ID (64-bit)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=False, default="")
    rating = Column(Float, default=5.0)
    trips_count = Column(Integer, default=0)
    successful_trips = Column(Integer, default=0)
    failed_trips = Column(Integer, default=0)
    is_blocked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    trips = relationship("Trip", back_populates="user", cascade="all, delete-orphan")
    support_tickets = relationship("SupportTicket", back_populates="user")


class Trip(Base):
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False)  # "driver" | "passenger"
    from_address = Column(String, nullable=False)
    from_lat = Column(Float, nullable=False)
    from_lon = Column(Float, nullable=False)
    to_address = Column(String, nullable=False)
    to_lat = Column(Float, nullable=False)
    to_lon = Column(Float, nullable=False)
    departure_time = Column(DateTime, nullable=False)
    price = Column(Float, nullable=True)   # driver → price per seat; passenger → budget
    seats = Column(Integer, nullable=True) # driver → available seats; passenger → pax count
    status = Column(String, default="ACTIVE")  # ACTIVE | MATCHING | CONFIRMED | CLOSED
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="trips")
    driver_matches = relationship(
        "Match", foreign_keys="Match.driver_trip_id",
        back_populates="driver_trip", cascade="all, delete-orphan"
    )
    passenger_matches = relationship(
        "Match", foreign_keys="Match.passenger_trip_id",
        back_populates="passenger_trip", cascade="all, delete-orphan"
    )


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    driver_trip_id = Column(Integer, ForeignKey("trips.id"), nullable=False)
    passenger_trip_id = Column(Integer, ForeignKey("trips.id"), nullable=False)
    driver_confirmed = Column(Boolean, default=False)
    passenger_confirmed = Column(Boolean, default=False)
    status = Column(String, default="PENDING")  # PENDING | CONFIRMED | REJECTED | CANCELLED | CLOSED
    rejection_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Pre-departure flow
    reminder_sent = Column(Boolean, default=False)
    driver_departed = Column(Boolean, default=False)
    passenger_ready = Column(Boolean, default=False)
    cancelled_by = Column(String, nullable=True)   # "driver" | "passenger"
    cancel_reason = Column(String, nullable=True)

    driver_trip = relationship("Trip", foreign_keys=[driver_trip_id], back_populates="driver_matches")
    passenger_trip = relationship("Trip", foreign_keys=[passenger_trip_id], back_populates="passenger_matches")
    ratings = relationship("Rating", back_populates="match", cascade="all, delete-orphan")


class Rating(Base):
    __tablename__ = "ratings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    from_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    to_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    score = Column(Integer, nullable=False)  # 1–5
    created_at = Column(DateTime, default=datetime.utcnow)

    match = relationship("Match", back_populates="ratings")


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    type = Column(String, nullable=False)  # bug | improvement | feedback | contact
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="support_tickets")


class FAQ(Base):
    __tablename__ = "faq"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(String, nullable=False)
    answer = Column(Text, nullable=False)
    order_idx = Column(Integer, default=0)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False, default="")
    description = Column(String, nullable=True)


class DriverLocation(Base):
    __tablename__ = "driver_locations"

    match_id = Column(Integer, ForeignKey("matches.id"), primary_key=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PassengerLocation(Base):
    __tablename__ = "passenger_locations"

    match_id = Column(Integer, ForeignKey("matches.id"), primary_key=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
