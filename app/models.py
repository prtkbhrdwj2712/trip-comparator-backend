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
    reference_order_number = Column(String, nullable=True)
    address = Column(String, nullable=True)  # for geocoding into the route map
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
    reference_order_number = Column(String, nullable=True)
    address = Column(String, nullable=True)  # for geocoding into the route map
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


class DashboardUser(Base):
    """
    Per-person dashboard access keys, on top of the single master
    DASHBOARD_ACCESS_KEY env var (which keeps working regardless, as a
    fallback that always works even if this table is empty or has issues).
    Managed via /admin/dashboard-users, protected by WEBHOOK_API_KEY.
    """
    __tablename__ = "dashboard_user"

    name = Column(String, primary_key=True)
    access_key = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    revoked = Column(Integer, default=0)  # 0/1 - revoked keys are kept (not deleted) for an audit trail
    totp_secret = Column(String, nullable=True)  # set once the user completes 2FA setup; null = 2FA not enabled


class GeocodeCache(Base):
    """
    Caches address -> lat/lng lookups so the same dealer address (which
    repeats across many trips/plans) only ever gets geocoded once against
    the free Nominatim service, which has a strict 1-request/second policy.
    """
    __tablename__ = "geocode_cache"

    address = Column(String, primary_key=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    looked_up_at = Column(DateTime, default=utcnow)


class VehicleTransporter(Base):
    """
    A separately-maintained vehicle -> transporter lookup, since this isn't
    in the dispatch summary data at all. Populated via /admin/vehicle-
    transporters (bulk upload or one at a time), and used to enrich trip
    responses by looking up whichever vehicle_id ended up on each side
    (planned vehicle vs confirmed vehicle can be different transporters).
    """
    __tablename__ = "vehicle_transporter"

    vehicle_id = Column(String, primary_key=True)
    transporter_name = Column(String, nullable=False)
    updated_at = Column(DateTime, default=utcnow)


class DealerLocation(Base):
    """
    Authoritative dealer_code -> lat/lng, supplied directly rather than
    guessed via free-text geocoding. Checked FIRST by the map endpoint;
    free Nominatim geocoding (GeocodeCache) is only a fallback for any
    dealer codes not present here. Keyed by dealer code (matches
    ship_to_code on StopBaseline/StopConfirmed exactly) rather than
    address text, which is a much more reliable join.
    """
    __tablename__ = "dealer_location"

    dealer_code = Column(String, primary_key=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    dealer_name = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow)

