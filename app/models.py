"""
SQLAlchemy models.

Two "sides" of every trip are stored separately, exactly like the plan vs
actual Excel files we started from:
  - TripBaseline / StopBaseline  -> populated when the plan webhook fires
  - TripConfirmed / StopConfirmed -> populated when the trip-events webhook fires

Nothing is overwritten: if a trip gets re-confirmed (e.g. Trip Started then
Trip Completed), we keep the latest confirmed snapshot per trip_id but you
could just as easily version these if you want full history later.
"""
from sqlalchemy import (
    Column, String, Float, Integer, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class TripBaseline(Base):
    __tablename__ = "trip_baseline"

    trip_id = Column(String, primary_key=True)
    plan_id = Column(String, index=True, nullable=False)
    trip_name = Column(String)
    trip_date = Column(String)

    vehicle_category = Column(String)
    vehicle_id = Column(String)
    driver_name = Column(String)

    planned_trip_distance_km = Column(Float)
    planned_trip_duration_h = Column(Float)
    trip_weight_kg = Column(Float)
    trip_volume_cm3 = Column(Float)

    weight_utilization = Column(Float)
    space_utilization = Column(Float)
    distance_utilization = Column(Float)
    time_utilization = Column(Float)

    trip_cost = Column(Float, nullable=True)
    no_of_stops = Column(Integer)

    raw = Column(JSON)  # full row payload, for anything not modeled above
    received_at = Column(DateTime, default=utcnow)

    stops = relationship("StopBaseline", back_populates="trip", cascade="all, delete-orphan")


class StopBaseline(Base):
    __tablename__ = "stop_baseline"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String, ForeignKey("trip_baseline.trip_id"), index=True)

    activity = Column(String)          # Pickup / Drop
    ship_to_code = Column(String)      # dealer code -> used for the dealer diff
    ship_to_name = Column(String)
    sequence = Column(Float)
    planned_arrival = Column(String)
    weight_kg = Column(Float)

    trip = relationship("TripBaseline", back_populates="stops")


class TripConfirmed(Base):
    __tablename__ = "trip_confirmed"

    trip_id = Column(String, primary_key=True)
    plan_id = Column(String, index=True, nullable=True)
    event_type = Column(String)  # Trip Confirmed / Trip Started / Trip Completed

    vehicle_category = Column(String)
    vehicle_id = Column(String)
    driver_name = Column(String)

    actual_trip_distance_km = Column(Float, nullable=True)
    actual_trip_duration_h = Column(Float, nullable=True)
    trip_weight_kg = Column(Float)
    trip_volume_cm3 = Column(Float, nullable=True)

    weight_utilization = Column(Float)
    space_utilization = Column(Float, nullable=True)
    distance_utilization = Column(Float, nullable=True)
    time_utilization = Column(Float, nullable=True)

    trip_cost = Column(Float, nullable=True)
    no_of_stops = Column(Integer)

    raw = Column(JSON)
    confirmed_at = Column(DateTime, default=utcnow)

    stops = relationship("StopConfirmed", back_populates="trip", cascade="all, delete-orphan")


class StopConfirmed(Base):
    __tablename__ = "stop_confirmed"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String, ForeignKey("trip_confirmed.trip_id"), index=True)

    activity = Column(String)
    ship_to_code = Column(String)
    ship_to_name = Column(String)
    sequence = Column(Float)
    actual_arrival = Column(String)
    weight_kg = Column(Float)

    trip = relationship("TripConfirmed", back_populates="stops")
