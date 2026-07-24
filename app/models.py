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
    dc_name = Column(String, nullable=True, index=True)  # e.g. "Bhandup" - passed by the uploader, not the xlsx itself

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


class PendingReconfirm(Base):
    """
    Tracks a plan that's been baselined and needs to be periodically
    re-downloaded to see if any of its trips have moved past 'Planned'.
    This exists because we don't have a real confirmation webhook - a
    scheduled job re-checks these on an interval instead.
    """
    __tablename__ = "pending_reconfirm"

    plan_id = Column(String, primary_key=True)
    hierarchy = Column(String)
    hierarchy_id = Column(String)
    first_downloaded_at = Column(DateTime, default=utcnow)
    last_checked_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0)
    done = Column(Integer, default=0)  # 0/1 - all trips for this plan confirmed, or gave up

