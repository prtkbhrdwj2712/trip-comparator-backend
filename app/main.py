import io
import json
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Depends, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import init_db, get_db
from .models import TripBaseline, StopBaseline, TripConfirmed, StopConfirmed, PendingReconfirm, DashboardUser, GeocodeCache, VehicleTransporter, DealerLocation, DeviceToken
from .xlsx_parser import parse_dispatch_workbook
from .diff_engine import compute_diff
from .auth import verify_api_key, verify_dashboard_key
from . import totp_utils
from . import password_utils
from . import geocode as geocode_module

app = FastAPI(title="Trip Comparator Backend")

# Allow the dashboard (wherever it's hosted) to call this API.
# Tighten allow_origins to your actual dashboard domain once you have one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# 1. BASELINE WEBHOOK
#    Called by your plan-webhook-uploader alongside its existing Drive upload.
#    Expects the dispatch-summary Excel file as multipart/form-data.
# ---------------------------------------------------------------------------
@app.post("/webhooks/plan-baseline")
async def receive_plan_baseline(
    file: UploadFile = File(...),
    hierarchy: str = Form(None),  # e.g. "Bhandup" - the friendly DC name, passed by the uploader
    plan_created_at: str = Form(None),  # Mojro's real plan creation time (data.createdAt), ISO format
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    content = await file.read()
    try:
        trips = parse_dispatch_workbook(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse workbook: {e}")

    parsed_plan_created_at = None
    if plan_created_at:
        try:
            parsed_plan_created_at = datetime.fromisoformat(plan_created_at)
            if parsed_plan_created_at.tzinfo is not None:
                # Mojro sends this with its own offset (e.g. +05:30 for IST) -
                # convert to true UTC before storing, then drop the tzinfo
                # marker so it's stored the same naive-but-UTC way as
                # received_at/confirmed_at elsewhere. Skipping this step
                # would silently treat "16:52 IST" as if it were "16:52 UTC" -
                # a 5.5-hour error that would cancel plans hours too early.
                parsed_plan_created_at = parsed_plan_created_at.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, TypeError):
            # Malformed/unexpected format - don't fail the whole ingestion
            # over a timestamp we can fall back to received_at for anyway.
            parsed_plan_created_at = None

    upserted = []
    skipped_existing = []
    for trip_id, t in trips.items():
        existing = db.get(TripBaseline, trip_id)
        if existing:
            # Baseline is write-once: the FIRST time we see a trip_id, that's
            # locked in as "what was planned". If this same webhook fires
            # again later for the same plan (re-optimization, a retry, or
            # because the source endpoint started returning updated/
            # post-confirmation data), we must NOT overwrite it - doing so
            # would silently destroy the planned-vs-confirmed diff.
            skipped_existing.append(trip_id)
            continue

        row = TripBaseline(
            trip_id=trip_id,
            plan_id=t.get("plan_id"),
            trip_name=t.get("trip_name"),
            trip_date=t.get("trip_date"),
            dc_name=hierarchy,
            plan_created_at=parsed_plan_created_at,
            vehicle_category=t.get("vehicle_category"),
            vehicle_id=t.get("vehicle_id"),
            driver_name=t.get("driver_name"),
            planned_trip_distance_km=_to_float(t.get("trip_distance_km")),
            planned_trip_duration_h=_to_float(t.get("trip_duration_h")),
            trip_weight_kg=_to_float(t.get("trip_weight_kg")),
            trip_volume_cm3=_to_float(t.get("trip_volume_cm3")),
            weight_utilization=_to_float(t.get("weight_utilization")),
            space_utilization=_to_float(t.get("space_utilization")),
            distance_utilization=_to_float(t.get("distance_utilization")),
            time_utilization=_to_float(t.get("time_utilization")),
            trip_cost=_to_float(t.get("trip_cost")),
            no_of_stops=t.get("no_of_stops"),
            raw=t,
        )
        db.add(row)
        db.flush()
        for s in t["stops"]:
            db.add(StopBaseline(
                trip_id=trip_id,
                activity=s.get("activity"),
                ship_to_code=s.get("ship_to_code"),
                ship_to_name=s.get("ship_to_name"),
                sequence=_to_float(s.get("sequence")),
                planned_arrival=s.get("arrival"),
                reference_order_number=s.get("reference_order_number"),
                address=s.get("address"),
                weight_kg=_to_float(s.get("weight_kg")),
            ))
        upserted.append(trip_id)

    db.commit()
    return {
        "status": "ok",
        "trips_ingested": upserted,
        "trips_skipped_already_had_baseline": skipped_existing,
    }


# ---------------------------------------------------------------------------
# 2. CONFIRMATION WEBHOOK
#    Called by your Trip Events flow (Trip Confirmed / Started / Completed).
#    NOTE: exact payload schema is unconfirmed - this accepts a generic JSON
#    body and maps commonly-named fields. Once you trigger a real test event,
#    send me that payload and I'll tighten this mapping.
# ---------------------------------------------------------------------------
@app.post("/webhooks/trip-confirmed")
async def receive_trip_confirmed(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    trip_id = payload.get("trip_id") or payload.get("tripId")
    if not trip_id:
        raise HTTPException(status_code=400, detail="Payload missing trip_id / tripId")

    event_type = payload.get("event_type") or payload.get("eventType") or "Trip Confirmed"
    stops_payload = payload.get("stops") or payload.get("dealers") or []

    existing = db.get(TripConfirmed, trip_id)
    if existing:
        db.query(StopConfirmed).filter(StopConfirmed.trip_id == trip_id).delete()
        db.delete(existing)
        db.flush()

    row = TripConfirmed(
        trip_id=trip_id,
        plan_id=payload.get("plan_id") or payload.get("planId"),
        event_type=event_type,
        vehicle_category=payload.get("vehicle_category") or payload.get("vehicleCategory"),
        vehicle_id=payload.get("vehicle_id") or payload.get("vehicleNumber") or payload.get("vehicleId"),
        driver_name=payload.get("driver_name") or payload.get("driverName"),
        actual_trip_distance_km=_to_float(payload.get("actual_distance_km") or payload.get("distanceKm")),
        actual_trip_duration_h=_to_float(payload.get("actual_duration_h") or payload.get("durationH")),
        trip_weight_kg=_to_float(payload.get("trip_weight_kg") or payload.get("weightKg")),
        trip_volume_cm3=_to_float(payload.get("trip_volume_cm3") or payload.get("volumeCm3")),
        weight_utilization=_to_float(payload.get("weight_utilization") or payload.get("weightUtilization")),
        space_utilization=_to_float(payload.get("space_utilization")),
        distance_utilization=_to_float(payload.get("distance_utilization")),
        time_utilization=_to_float(payload.get("time_utilization")),
        trip_cost=_to_float(payload.get("trip_cost")),
        no_of_stops=len(stops_payload) or payload.get("no_of_stops"),
        raw=payload,
    )
    db.add(row)
    db.flush()
    for s in stops_payload:
        db.add(StopConfirmed(
            trip_id=trip_id,
            activity=s.get("activity", "Drop"),
            ship_to_code=s.get("ship_to_code") or s.get("dealerCode"),
            ship_to_name=s.get("ship_to_name") or s.get("dealerName"),
            sequence=_to_float(s.get("sequence")),
            actual_arrival=s.get("actual_arrival") or s.get("arrivalTime"),
            reference_order_number=s.get("reference_order_number"),
            address=s.get("address"),
            weight_kg=_to_float(s.get("weight_kg")),
        ))
    db.commit()
    return {"status": "ok", "trip_id": trip_id}


# ---------------------------------------------------------------------------
# 3. DASHBOARD READ API
# ---------------------------------------------------------------------------
@app.get("/api/trips")
def list_trips(
    date_from: str = None,   # "YYYY-MM-DD" - inclusive
    date_to: str = None,     # "YYYY-MM-DD" - inclusive
    dc: str = None,          # exact dc_name match, e.g. "Bhandup"
    plan_id: str = None,     # partial, case-insensitive match
    trip_id: str = None,     # partial, case-insensitive match
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_dashboard_key),
):
    query = db.query(TripBaseline)
    if date_from:
        query = query.filter(TripBaseline.trip_date >= date_from)
    if date_to:
        # trip_date is stored as "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" - a
        # simple string upper-bound of date_to + a character past any time
        # component keeps this an inclusive same-day match either way.
        query = query.filter(TripBaseline.trip_date <= f"{date_to} 23:59:59")
    if dc:
        query = query.filter(TripBaseline.dc_name == dc)
    if plan_id:
        query = query.filter(TripBaseline.plan_id.ilike(f"%{plan_id}%"))
    if trip_id:
        query = query.filter(TripBaseline.trip_id.ilike(f"%{trip_id}%"))

    baselines = query.all()
    if not baselines:
        return []

    baseline_trip_ids = [b.trip_id for b in baselines]

    # Bulk-fetch confirmed rows for all these trips in ONE query instead of
    # one db.get() per trip (was the main N+1 offender).
    confirmed_rows = db.query(TripConfirmed).filter(TripConfirmed.trip_id.in_(baseline_trip_ids)).all()
    confirmed_by_id = {c.trip_id: c for c in confirmed_rows}
    confirmed_trip_ids = list(confirmed_by_id.keys())

    # Bulk-fetch only the columns needed for the dealer diff (not full stop
    # rows), and only for trips that actually have a confirmed counterpart -
    # this is what was blowing up memory, since accessing .stops on every
    # trip lazy-loads every stop row for every trip on every request.
    baseline_stops_by_trip = {}
    if confirmed_trip_ids:
        rows = (
            db.query(StopBaseline.trip_id, StopBaseline.activity, StopBaseline.ship_to_code, StopBaseline.ship_to_name)
            .filter(StopBaseline.trip_id.in_(confirmed_trip_ids))
            .all()
        )
        for trip_id, activity, code, name in rows:
            baseline_stops_by_trip.setdefault(trip_id, []).append(
                {"activity": activity, "ship_to_code": code, "ship_to_name": name}
            )

        confirmed_stops_by_trip = {}
        rows = (
            db.query(StopConfirmed.trip_id, StopConfirmed.activity, StopConfirmed.ship_to_code, StopConfirmed.ship_to_name)
            .filter(StopConfirmed.trip_id.in_(confirmed_trip_ids))
            .all()
        )
        for trip_id, activity, code, name in rows:
            confirmed_stops_by_trip.setdefault(trip_id, []).append(
                {"activity": activity, "ship_to_code": code, "ship_to_name": name}
            )
    else:
        confirmed_stops_by_trip = {}

    out = []
    for b in baselines:
        confirmed = confirmed_by_id.get(b.trip_id)
        diff = compute_diff(
            b, confirmed,
            baseline_stops=baseline_stops_by_trip.get(b.trip_id, []) if confirmed else None,
            confirmed_stops=confirmed_stops_by_trip.get(b.trip_id, []) if confirmed else None,
        )
        out.append({
            "trip_id": b.trip_id,
            "plan_id": b.plan_id,
            "trip_name": b.trip_name,
            "trip_date": b.trip_date,
            "dc_name": b.dc_name,
            "baseline_unavailable": bool(b.baseline_unavailable),
            "plan_created_at": b.plan_created_at.isoformat() if b.plan_created_at else None,
            **diff,
        })
    return out


@app.get("/api/dcs")
def list_dcs(db: Session = Depends(get_db), _auth: bool = Depends(verify_dashboard_key)):
    """Distinct DC names seen so far, for populating the dashboard's filter dropdown."""
    rows = db.query(TripBaseline.dc_name).filter(TripBaseline.dc_name.isnot(None)).distinct().all()
    return sorted([r[0] for r in rows if r[0]])


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db), _auth: bool = Depends(verify_dashboard_key)):
    """
    Diagnostic breakdown: how many distinct plans and trips landed per DC
    per date. Meant for sanity-checking whether a trip/plan count "looks
    right" - a normal, gradual accumulation should show a fairly even
    spread across dates; a sudden spike on one date/DC combo is worth
    investigating as a possible duplicate-ingestion or retry-storm issue.
    """
    from sqlalchemy import func
    rows = (
        db.query(
            TripBaseline.dc_name,
            TripBaseline.trip_date,
            func.count(func.distinct(TripBaseline.plan_id)).label("plan_count"),
            func.count(TripBaseline.trip_id).label("trip_count"),
        )
        .group_by(TripBaseline.dc_name, TripBaseline.trip_date)
        .order_by(TripBaseline.trip_date.desc())
        .all()
    )
    breakdown = [
        {"dc_name": r.dc_name, "trip_date": r.trip_date, "plan_count": r.plan_count, "trip_count": r.trip_count}
        for r in rows
    ]
    total_plans = db.query(func.count(func.distinct(TripBaseline.plan_id))).scalar()
    total_trips = db.query(func.count(TripBaseline.trip_id)).scalar()
    return {
        "total_plans": total_plans,
        "total_trips": total_trips,
        "breakdown_by_dc_and_date": breakdown,
    }


@app.get("/internal/pending-reconfirm-status/{plan_id}")
def pending_reconfirm_status(plan_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Diagnostic: shows exactly why a plan is (or isn't) being polled for
    confirmation - whether it's registered at all, when it was first seen,
    when it was last checked, and roughly when the next check is due.
    Use this whenever a trip seems like it should have confirmed by now
    but the dashboard still shows "awaiting".
    """
    tracker = db.get(PendingReconfirm, plan_id)
    baseline_count = db.query(TripBaseline).filter(TripBaseline.plan_id == plan_id).count()
    confirmed_count = (
        db.query(TripConfirmed)
        .join(TripBaseline, TripConfirmed.trip_id == TripBaseline.trip_id)
        .filter(TripBaseline.plan_id == plan_id)
        .count()
    )

    if not tracker:
        return {
            "plan_id": plan_id,
            "registered_for_polling": False,
            "explanation": (
                "This plan was never registered for confirmation polling. "
                "This usually means the baseline webhook's background call to "
                "register-pending-reconfirm failed silently (e.g. a network "
                "hiccup) when this plan was first ingested. It will never be "
                "auto-checked unless manually registered."
            ),
            "baseline_trip_count": baseline_count,
            "confirmed_trip_count": confirmed_count,
        }

    now = datetime.now(timezone.utc)
    first_dl = _as_utc(tracker.first_downloaded_at)
    last_checked = _as_utc(tracker.last_checked_at) if tracker.last_checked_at else None

    age_minutes = (now - first_dl).total_seconds() / 60
    if tracker.done:
        status_explanation = "Marked done - either all trips confirmed, or gave up after 24 hours."
    elif age_minutes < FIRST_CHECK_DELAY_MINUTES:
        status_explanation = f"Too new - first check happens {FIRST_CHECK_DELAY_MINUTES} min after baseline; only {age_minutes:.0f} min have passed."
    elif last_checked and (now - last_checked).total_seconds() / 60 < RECHECK_INTERVAL_MINUTES:
        mins_since_check = (now - last_checked).total_seconds() / 60
        status_explanation = f"Recently checked {mins_since_check:.0f} min ago - waits {RECHECK_INTERVAL_MINUTES} min between checks."
    else:
        status_explanation = "Due for a check now - should be picked up on the next cron run."

    return {
        "plan_id": plan_id,
        "registered_for_polling": True,
        "done": bool(tracker.done),
        "attempts_so_far": tracker.attempts,
        "first_downloaded_at": tracker.first_downloaded_at.isoformat() if tracker.first_downloaded_at else None,
        "last_checked_at": tracker.last_checked_at.isoformat() if tracker.last_checked_at else None,
        "age_minutes": round(age_minutes, 1),
        "baseline_trip_count": baseline_count,
        "confirmed_trip_count": confirmed_count,
        "explanation": status_explanation,
    }


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_dashboard_key)):
    b = db.get(TripBaseline, trip_id)
    if not b:
        raise HTTPException(status_code=404, detail="Trip not found in baseline")
    confirmed = db.get(TripConfirmed, trip_id)
    diff = compute_diff(b, confirmed)

    # Transporter isn't in the dispatch summary data at all - looked up
    # separately from the vehicle_transporter table, maintained via
    # /admin/vehicle-transporters. The planned and confirmed vehicle can be
    # different vehicles entirely, so each side gets its own lookup.
    baseline_transporter = None
    if b.vehicle_id:
        vt = db.get(VehicleTransporter, b.vehicle_id)
        baseline_transporter = vt.transporter_name if vt else None

    confirmed_transporter = None
    if confirmed and confirmed.vehicle_id:
        vt = db.get(VehicleTransporter, confirmed.vehicle_id)
        confirmed_transporter = vt.transporter_name if vt else None

    return {
        "trip_id": trip_id,
        "plan_id": b.plan_id,
        "trip_name": b.trip_name,
        "dc_name": b.dc_name,
        "baseline_unavailable": bool(b.baseline_unavailable),
        "plan_created_at": b.plan_created_at.isoformat() if b.plan_created_at else None,
        "baseline": {
            "vehicle_category": b.vehicle_category,
            "vehicle_id": b.vehicle_id,
            "transporter_name": baseline_transporter,
            "driver_name": b.driver_name,
            "weight_utilization": b.weight_utilization,
            "space_utilization": b.space_utilization,
            "trip_weight_kg": b.trip_weight_kg,
            "trip_cost": b.trip_cost,
            "distance_km": b.planned_trip_distance_km,
            "duration_h": b.planned_trip_duration_h,
            "no_of_stops": b.no_of_stops,
            "received_at": b.received_at.isoformat() if b.received_at else None,
            "stops": [
                {"code": s.ship_to_code, "name": s.ship_to_name, "sequence": s.sequence,
                 "arrival": s.planned_arrival, "reference_order_number": s.reference_order_number,
                 "weight_kg": s.weight_kg}
                for s in b.stops if s.activity == "Drop"
            ],
        },
        "confirmed": None if not confirmed else {
            "vehicle_category": confirmed.vehicle_category,
            "vehicle_id": confirmed.vehicle_id,
            "transporter_name": confirmed_transporter,
            "driver_name": confirmed.driver_name,
            "weight_utilization": confirmed.weight_utilization,
            "space_utilization": confirmed.space_utilization,
            "trip_weight_kg": confirmed.trip_weight_kg,
            "trip_cost": confirmed.trip_cost,
            "distance_km": confirmed.actual_trip_distance_km,
            "duration_h": confirmed.actual_trip_duration_h,
            "no_of_stops": confirmed.no_of_stops,
            "confirmed_at": confirmed.confirmed_at.isoformat() if confirmed.confirmed_at else None,
            "stops": [
                {"code": s.ship_to_code, "name": s.ship_to_name, "sequence": s.sequence,
                 "arrival": s.actual_arrival, "reference_order_number": s.reference_order_number,
                 "weight_kg": s.weight_kg}
                for s in confirmed.stops if s.activity == "Drop"
            ],
        },
        "diff": diff,
    }


# ---------------------------------------------------------------------------
# VEHICLE -> TRANSPORTER LOOKUP
#    Not in the dispatch summary data at all - maintained separately here.
#    Protected by WEBHOOK_API_KEY since it's admin data management, not a
#    dashboard read.
# ---------------------------------------------------------------------------
@app.post("/admin/vehicle-transporters")
def upsert_vehicle_transporter(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """Body: {"vehicle_id": "TN03AM4386", "transporter_name": "ABC Logistics"}. Creates or updates."""
    vehicle_id = (payload.get("vehicle_id") or "").strip()
    transporter_name = (payload.get("transporter_name") or "").strip()
    if not vehicle_id or not transporter_name:
        raise HTTPException(status_code=400, detail="vehicle_id and transporter_name are both required")

    existing = db.get(VehicleTransporter, vehicle_id)
    if existing:
        existing.transporter_name = transporter_name
        existing.updated_at = datetime.now(timezone.utc)
    else:
        db.add(VehicleTransporter(vehicle_id=vehicle_id, transporter_name=transporter_name))
    db.commit()
    return {"status": "ok", "vehicle_id": vehicle_id, "transporter_name": transporter_name}


@app.post("/admin/vehicle-transporters/bulk")
def bulk_upsert_vehicle_transporters(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """Body: {"mappings": [{"vehicle_id": "...", "transporter_name": "..."}, ...]}"""
    mappings = payload.get("mappings", [])
    updated = 0
    created = 0
    for m in mappings:
        vehicle_id = (m.get("vehicle_id") or "").strip()
        transporter_name = (m.get("transporter_name") or "").strip()
        if not vehicle_id or not transporter_name:
            continue
        existing = db.get(VehicleTransporter, vehicle_id)
        if existing:
            existing.transporter_name = transporter_name
            existing.updated_at = datetime.now(timezone.utc)
            updated += 1
        else:
            db.add(VehicleTransporter(vehicle_id=vehicle_id, transporter_name=transporter_name))
            created += 1
    db.commit()
    return {"status": "ok", "created": created, "updated": updated}


@app.get("/admin/vehicle-transporters")
def list_vehicle_transporters(db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    rows = db.query(VehicleTransporter).order_by(VehicleTransporter.vehicle_id).all()
    return [
        {"vehicle_id": r.vehicle_id, "transporter_name": r.transporter_name,
         "updated_at": r.updated_at.isoformat() if r.updated_at else None}
        for r in rows
    ]


@app.delete("/admin/vehicle-transporters/{vehicle_id}")
def delete_vehicle_transporter(vehicle_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    row = db.get(VehicleTransporter, vehicle_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"No transporter mapping for vehicle '{vehicle_id}'")
    db.delete(row)
    db.commit()
    return {"status": "ok", "vehicle_id": vehicle_id, "deleted": True}


# ---------------------------------------------------------------------------
# DEALER LOCATIONS (authoritative, supplied directly)
#    Checked first by the map endpoint, before falling back to free
#    Nominatim geocoding. Keyed by dealer_code, matching ship_to_code.
# ---------------------------------------------------------------------------
@app.post("/admin/dealer-locations/bulk")
def bulk_upsert_dealer_locations(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """Body: {"mappings": [{"dealer_code": "594242", "latitude": 19.07, "longitude": 72.87, "dealer_name": "..."}, ...]}"""
    mappings = payload.get("mappings", [])
    created = 0
    updated = 0
    skipped = 0
    for m in mappings:
        dealer_code = str(m.get("dealer_code") or "").strip()
        lat = m.get("latitude")
        lng = m.get("longitude")
        if not dealer_code or lat is None or lng is None:
            skipped += 1
            continue
        existing = db.get(DealerLocation, dealer_code)
        if existing:
            existing.latitude = lat
            existing.longitude = lng
            existing.dealer_name = m.get("dealer_name") or existing.dealer_name
            existing.updated_at = datetime.now(timezone.utc)
            updated += 1
        else:
            db.add(DealerLocation(
                dealer_code=dealer_code, latitude=lat, longitude=lng,
                dealer_name=m.get("dealer_name"),
            ))
            created += 1
    db.commit()
    return {"status": "ok", "created": created, "updated": updated, "skipped_invalid": skipped}


@app.get("/admin/dealer-locations")
def list_dealer_locations(db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    rows = db.query(DealerLocation).order_by(DealerLocation.dealer_code).all()
    return [
        {"dealer_code": r.dealer_code, "latitude": r.latitude, "longitude": r.longitude,
         "dealer_name": r.dealer_name, "updated_at": r.updated_at.isoformat() if r.updated_at else None}
        for r in rows
    ]


@app.delete("/admin/dealer-locations/{dealer_code}")
def delete_dealer_location(dealer_code: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    row = db.get(DealerLocation, dealer_code)
    if not row:
        raise HTTPException(status_code=404, detail=f"No location for dealer '{dealer_code}'")
    db.delete(row)
    db.commit()
    return {"status": "ok", "dealer_code": dealer_code, "deleted": True}


@app.get("/api/trips/{trip_id}/map-data")
def get_trip_map_data(trip_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_dashboard_key)):
    """
    Geocodes (with caching) every dealer address for this trip's baseline
    and confirmed stops, and returns lat/lng points for plotting a route
    map. Only ever calls the free Nominatim geocoder for addresses not
    already in the cache - most calls after the first few weeks of real
    usage should be pure cache hits, since dealer addresses repeat heavily
    across trips.

    This can be slow on first use for a trip with many never-seen-before
    addresses (Nominatim's policy caps us at 1 request/second) - that's why
    this is its own endpoint, only called when someone actually opens the
    map, rather than something every trip list load has to wait on.
    """
    b = db.get(TripBaseline, trip_id)
    if not b:
        raise HTTPException(status_code=404, detail="Trip not found in baseline")
    confirmed = db.get(TripConfirmed, trip_id)

    def resolve_points(stops):
        points = []
        for s in stops:
            if s.activity != "Drop":
                continue

            # 1. Authoritative source first - real supplied coordinates,
            #    keyed by dealer code (exact match, no ambiguity).
            dealer_loc = db.get(DealerLocation, s.ship_to_code) if s.ship_to_code else None
            if dealer_loc:
                points.append({
                    "code": s.ship_to_code, "name": s.ship_to_name,
                    "sequence": s.sequence, "lat": dealer_loc.latitude, "lng": dealer_loc.longitude,
                })
                continue

            # 2. Fall back to free-text geocoding only for dealers not in
            #    the authoritative table.
            if not s.address:
                continue
            cached = db.get(GeocodeCache, s.address)
            if cached and cached.latitude is not None:
                lat, lng = cached.latitude, cached.longitude
            elif cached:
                # Previously looked up and failed to resolve - don't retry
                # every single time; skip it.
                continue
            else:
                lat, lng = geocode_module.geocode_address(s.address)
                db.add(GeocodeCache(address=s.address, latitude=lat, longitude=lng))
                db.commit()
            if lat is not None:
                points.append({
                    "code": s.ship_to_code, "name": s.ship_to_name,
                    "sequence": s.sequence, "lat": lat, "lng": lng,
                })
        points.sort(key=lambda p: p["sequence"] or 0)
        return points

    baseline_points = resolve_points(b.stops)
    confirmed_points = resolve_points(confirmed.stops) if confirmed else []

    return {
        "trip_id": trip_id,
        "baseline_points": baseline_points,
        "confirmed_points": confirmed_points,
    }


@app.delete("/admin/trips/{trip_id}")
def reset_trip(trip_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Manually wipe a trip's baseline + confirmed rows. Use this to clear out
    test data, or to intentionally let a trip re-ingest a fresh baseline
    (bypassing the normal write-once protection) if you're certain that's
    what you want.
    """
    b = db.get(TripBaseline, trip_id)
    c = db.get(TripConfirmed, trip_id)
    if not b and not c:
        raise HTTPException(status_code=404, detail="No baseline or confirmed record found for this trip_id")

    if b:
        db.query(StopBaseline).filter(StopBaseline.trip_id == trip_id).delete()
        db.delete(b)
    if c:
        db.query(StopConfirmed).filter(StopConfirmed.trip_id == trip_id).delete()
        db.delete(c)
    db.commit()
    return {"status": "ok", "trip_id": trip_id, "cleared_baseline": bool(b), "cleared_confirmed": bool(c)}


# ---------------------------------------------------------------------------
# DASHBOARD USER MANAGEMENT
#    Per-person access keys, on top of the single master DASHBOARD_ACCESS_KEY.
#    All protected by WEBHOOK_API_KEY (the "owner" key) since managing who
#    can see the dashboard is an admin action, not a dashboard-read action.
# ---------------------------------------------------------------------------
@app.post("/admin/dashboard-users")
def create_dashboard_user(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """
    Creates a new dashboard user. Body: {"name": "alice", "access_key": "whatever-you-want"}.
    access_key is optional - if you leave it out, a random secure one gets
    generated for you instead. Set your own if you'd rather pick something
    memorable than deal with a long generated string.
    """
    name = payload.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    existing = db.get(DashboardUser, name)
    if existing and not existing.revoked:
        raise HTTPException(status_code=400, detail=f"User '{name}' already exists and is active")

    custom_key = (payload.get("access_key") or "").strip()
    new_key = custom_key if custom_key else secrets.token_urlsafe(24)
    hashed = password_utils.hash_key(new_key)

    if existing:
        # Re-activating a previously revoked name with a (possibly custom) new key.
        existing.access_key = hashed
        existing.revoked = 0
        existing.created_at = datetime.now(timezone.utc)
        existing.failed_login_attempts = 0
        existing.locked_until = None
    else:
        db.add(DashboardUser(name=name, access_key=hashed))

    db.commit()
    return {"name": name, "access_key": new_key}


@app.post("/admin/dashboard-users/{name}/set-key")
def set_dashboard_user_key(
    name: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """
    Changes an existing user's access key - to something you choose, or
    leave access_key blank to get a fresh random one instead. Use this to
    fix a hard-to-type generated key without deleting and recreating the
    user (which would also reset their 2FA setup).
    """
    user = db.get(DashboardUser, name)
    if not user:
        raise HTTPException(status_code=404, detail=f"No dashboard user named '{name}'")

    custom_key = (payload.get("access_key") or "").strip()
    new_key = custom_key if custom_key else secrets.token_urlsafe(24)
    user.access_key = password_utils.hash_key(new_key)
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    return {"name": name, "access_key": new_key}


@app.get("/admin/dashboard-users")
def list_dashboard_users(db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Lists every dashboard user and their status. Does NOT return the access
    key itself anymore (now stored hashed - a hash isn't useful to show
    anyway, and there's no way to recover the plaintext). Use
    /admin/dashboard-users/{name}/set-key if you need to issue a new one.
    """
    users = db.query(DashboardUser).order_by(DashboardUser.created_at.desc()).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "name": u.name,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "revoked": bool(u.revoked),
            "totp_enabled": bool(u.totp_secret),
            "currently_locked_out": bool(u.locked_until and _as_utc(u.locked_until) > now),
            "locked_until": u.locked_until.isoformat() if u.locked_until else None,
        }
        for u in users
    ]


@app.delete("/admin/dashboard-users/{name}")
def revoke_dashboard_user(name: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Revokes a user's access immediately - their key stops working right away,
    without affecting anyone else. The row is kept (not deleted) as an audit
    trail of who used to have access.
    """
    user = db.get(DashboardUser, name)
    if not user:
        raise HTTPException(status_code=404, detail=f"No dashboard user named '{name}'")
    user.revoked = 1
    db.commit()
    return {"status": "ok", "name": name, "revoked": True}


@app.post("/admin/dashboard-users/{name}/enable-2fa")
def enable_2fa(name: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Generates a new TOTP secret for this user and returns a QR code (base64
    PNG) to scan into any standard authenticator app (Google Authenticator,
    Authy, etc.). Once this is set, that user's login requires a 6-digit
    code in addition to their access key.
    """
    user = db.get(DashboardUser, name)
    if not user:
        raise HTTPException(status_code=404, detail=f"No dashboard user named '{name}'")

    secret = totp_utils.generate_secret()
    user.totp_secret = secret
    db.commit()

    qr_b64 = totp_utils.get_provisioning_qr_code_base64(secret, name)
    return {
        "name": name,
        "totp_secret": secret,  # shown once in case they need to enter it manually
        "qr_code_base64": qr_b64,
    }


@app.post("/api/setup-my-2fa")
def setup_my_2fa(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Self-service 2FA setup - any dashboard user can call this themselves
    (proving their own name+access_key, no admin key needed) to generate
    and see their own QR code directly, instead of needing an admin to run
    an API call and hand them the result manually. Not protected by
    verify_api_key/verify_dashboard_key on purpose - it does its own
    credential check, same as /api/login.
    """
    name = (payload.get("name") or "").strip()
    access_key = payload.get("access_key") or ""
    if not name:
        raise HTTPException(status_code=400, detail="Username is required")

    user = db.get(DashboardUser, name)
    if not user or user.revoked or not password_utils.verify_key(access_key, user.access_key):
        raise HTTPException(status_code=401, detail="Incorrect username or access key")

    if user.totp_secret:
        raise HTTPException(
            status_code=400,
            detail="2FA is already enabled for this account. Ask an admin to reset it first if you need a new QR code.",
        )

    secret = totp_utils.generate_secret()
    user.totp_secret = secret
    db.commit()

    qr_b64 = totp_utils.get_provisioning_qr_code_base64(secret, name)
    return {
        "name": name,
        "totp_secret": secret,
        "qr_code_base64": qr_b64,
    }


@app.post("/admin/dashboard-users/{name}/disable-2fa")
def disable_2fa(name: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    user = db.get(DashboardUser, name)
    if not user:
        raise HTTPException(status_code=404, detail=f"No dashboard user named '{name}'")
    user.totp_secret = None
    db.commit()
    return {"status": "ok", "name": name, "totp_disabled": True}


MAX_FAILED_LOGIN_ATTEMPTS = 3
DEVICE_TOKEN_HOURS = 12  # "remember this device" window for 2FA
LOCKOUT_MINUTES = 15


@app.post("/api/login")
def dashboard_login(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Real login check: validates that the given name+access_key belong
    together (not just that the key is valid for SOME user), and enforces
    a TOTP code if that user has 2FA enabled. The master DASHBOARD_ACCESS_KEY
    still works as a no-username-needed fallback, same as before.

    Access keys are stored hashed (bcrypt) and verified via password_utils,
    never compared as plaintext. After too many wrong attempts in a row for
    a given username, that account is temporarily locked out regardless of
    whether the next attempt would've been correct - slows down guessing.

    "Remember this device" (device_token): if the browser already holds a
    valid, unexpired token for this username, the 2FA code isn't required
    again - the token is genuinely time-limited (DEVICE_TOKEN_HOURS from
    when it was issued) rather than a rolling window, so it resets exactly
    once that window passes. A fresh token is issued and returned whenever
    a code is verified successfully, for the frontend to store client-side.

    This endpoint itself isn't protected by verify_dashboard_key (that would
    be circular) - it's the thing that decides whether the key the user is
    about to use for real API calls is actually valid for them.
    """
    from .auth import DASHBOARD_KEY

    name = (payload.get("name") or "").strip()
    access_key = payload.get("access_key") or ""
    totp_code = payload.get("totp_code")
    device_token = payload.get("device_token")

    if access_key == DASHBOARD_KEY:
        return {"status": "ok", "requires_totp": False}

    if not name:
        raise HTTPException(status_code=400, detail="Username is required")

    user = db.get(DashboardUser, name)
    if not user or user.revoked:
        raise HTTPException(status_code=401, detail="Incorrect username or access key")

    now = datetime.now(timezone.utc)
    if user.locked_until and _as_utc(user.locked_until) > now:
        minutes_left = int((_as_utc(user.locked_until) - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=401,
            detail=f"Too many failed attempts. Try again in {minutes_left} minute(s).",
        )

    if not password_utils.verify_key(access_key, user.access_key):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_attempts = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Incorrect username or access key")

    # Correct password - reset any lockout tracking.
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()

    if user.totp_secret:
        # Opportunistic cleanup - remove this user's expired tokens so the
        # table doesn't grow unbounded; cheap since it's scoped to one user.
        db.query(DeviceToken).filter(
            DeviceToken.username == name, DeviceToken.expires_at < now,
        ).delete()
        db.commit()

        remembered = None
        if device_token:
            remembered = db.query(DeviceToken).filter(
                DeviceToken.token == device_token,
                DeviceToken.username == name,
                DeviceToken.expires_at > now,
            ).first()

        if not remembered:
            if not totp_code:
                # Correct username/key, but 2FA is required, no valid
                # remembered device, and no code sent yet - frontend should
                # now show the code-entry step.
                return {"status": "totp_required", "requires_totp": True}
            if not totp_utils.verify_totp_code(user.totp_secret, totp_code):
                raise HTTPException(status_code=401, detail="Incorrect 2FA code")

            # Code verified - issue a fresh 12-hour device token.
            new_token = secrets.token_urlsafe(32)
            db.add(DeviceToken(
                token=new_token, username=name,
                expires_at=now + timedelta(hours=DEVICE_TOKEN_HOURS),
            ))
            db.commit()
            return {"status": "ok", "requires_totp": True, "device_token": new_token}

    return {"status": "ok", "requires_totp": bool(user.totp_secret)}




# ---------------------------------------------------------------------------
# 4. POLLING-BASED CONFIRMATION (no CPI webhook available)
#    Since a real "Trip Confirmed" event can't be wired in, we instead:
#      a) register every baselined plan here as "pending recheck"
#      b) a scheduled job (Render Cron) periodically asks what's due
#      c) that job re-downloads the plan and posts it to /webhooks/plan-reconfirm
#      d) we only treat a trip as confirmed if its own `status` column says so
# ---------------------------------------------------------------------------
RECHECK_INTERVAL_MINUTES = 15   # don't re-check the same plan more than this often
FIRST_CHECK_DELAY_MINUTES = 60  # don't bother checking until this long after baseline
GIVE_UP_AFTER_HOURS = 24        # stop rechecking a plan after this long either way


@app.post("/internal/register-pending-reconfirm")
def register_pending_reconfirm(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    plan_id = payload.get("plan_id")
    hierarchy = payload.get("hierarchy")
    hierarchy_id = payload.get("hierarchy_id")
    if not plan_id or not hierarchy or not hierarchy_id:
        raise HTTPException(status_code=400, detail="plan_id, hierarchy, and hierarchy_id are all required")

    existing = db.get(PendingReconfirm, plan_id)
    if existing:
        # Already tracking this plan - don't reset its clock.
        return {"status": "ok", "already_tracked": True, "plan_id": plan_id}

    row = PendingReconfirm(plan_id=plan_id, hierarchy=hierarchy, hierarchy_id=hierarchy_id)
    db.add(row)
    db.commit()
    return {"status": "ok", "already_tracked": False, "plan_id": plan_id}


# Mirrors the uploader's HIERARCHY_MAP display names -> real hierarchy_id.
# Needed here so we can bulk-backfill plans that were baselined before (or
# despite) the registration call succeeding, without asking for each one
# manually. If a new DC gets added on the uploader side, add it here too.
KNOWN_DC_HIERARCHY_IDS = {
    "Bhandup": "NGU4MmI1NjctMWVhMi0xMWYxLTkxNGEtMDAwZDNhMDE1NTA5",
    "Mayapuri (1506)": "YWYzZWFhNDctMjk4YS0xMWVmLTg3NGItMDAwZDNhMDE1NTA5",
    "Chrompet": "ZWJhMjMzMDAtMWRmMy0xMWYxLWExNTktMDAwZDNhMDYwMGQy",
    "APIL Siliguri (1539)": "ZjY5MjVjMmItODA4NC0xMWVmLTg5NTEtMDAwZDNhMDYwMGQy",
    "Life Care Logistic (1559)": "MjA1MmNjNzctMjk3ZS0xMWVmLTlhMWItMDAwZDNhMDYwMGQy",
    "Karimnagar": "Njg3YzI5ZTgtMWUxNS0xMWYxLWExNTktMDAwZDNhMDYwMGQy",
}


@app.post("/internal/backfill-pending-reconfirm")
def backfill_pending_reconfirm(db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    """
    Finds every plan_id that has baseline trips but was never registered for
    confirmation polling (the gap that's been causing "stuck on awaiting"
    reports), and registers the ones we can - i.e. where dc_name is set and
    matches a known DC. Plans with no dc_name (baselined before that field
    existed) can't be auto-matched to a hierarchy_id and are reported
    separately rather than guessed at.
    """
    all_plan_rows = (
        db.query(TripBaseline.plan_id, TripBaseline.dc_name)
        .distinct(TripBaseline.plan_id)
        .all()
    )
    already_registered = {p.plan_id for p in db.query(PendingReconfirm.plan_id).all()}

    registered = []
    skipped_unknown_dc = []
    for plan_id, dc_name in all_plan_rows:
        if plan_id in already_registered:
            continue
        hierarchy_id = KNOWN_DC_HIERARCHY_IDS.get(dc_name) if dc_name else None
        if not hierarchy_id:
            skipped_unknown_dc.append({"plan_id": plan_id, "dc_name": dc_name})
            continue
        db.add(PendingReconfirm(plan_id=plan_id, hierarchy=dc_name, hierarchy_id=hierarchy_id))
        registered.append({"plan_id": plan_id, "dc_name": dc_name})

    db.commit()
    return {
        "newly_registered_count": len(registered),
        "newly_registered": registered,
        "skipped_unknown_dc_count": len(skipped_unknown_dc),
        "skipped_unknown_dc": skipped_unknown_dc,
    }


@app.get("/internal/due-reconfirms")
def due_reconfirms(db: Session = Depends(get_db), _auth: bool = Depends(verify_api_key)):
    now = datetime.now(timezone.utc)
    candidates = db.query(PendingReconfirm).filter(PendingReconfirm.done == 0).all()

    due = []
    for p in candidates:
        first_dl = _as_utc(p.first_downloaded_at)
        if now - first_dl < timedelta(minutes=FIRST_CHECK_DELAY_MINUTES):
            continue
        if now - first_dl > timedelta(hours=GIVE_UP_AFTER_HOURS):
            p.done = 1  # give up - stop checking a plan forever
            continue
        if p.last_checked_at is not None:
            last = _as_utc(p.last_checked_at)
            if now - last < timedelta(minutes=RECHECK_INTERVAL_MINUTES):
                continue
        due.append({"plan_id": p.plan_id, "hierarchy": p.hierarchy, "hierarchy_id": p.hierarchy_id})

    db.commit()  # persist any give-up flags set above
    return {"due": due}


@app.post("/webhooks/plan-reconfirm")
async def receive_plan_reconfirm(
    plan_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    """
    Receives a re-downloaded dispatch summary for a plan already being
    tracked. Confirmed here just means "appears in this later export" -
    the source system drops a trip out of the dispatch summary entirely
    once it's confirmed, rather than flipping a status field on it. So
    every trip found in this file is treated as confirmed; any baseline
    trip for this plan_id that's NOT in this file is still pending and
    gets left alone until the next scheduled recheck.
    """
    content = await file.read()
    try:
        trips = parse_dispatch_workbook(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse workbook: {e}")

    # The full set of trips we're expecting for this plan, from baseline.
    baseline_trip_ids = {
        row.trip_id for row in db.query(TripBaseline).filter(TripBaseline.plan_id == plan_id).all()
    }

    # DC name for this plan (all trips in a plan share the same DC) - reused
    # below for any newly-discovered trip, so it's tagged consistently with
    # its siblings rather than left null.
    sibling_dc_row = db.query(TripBaseline.dc_name).filter(TripBaseline.plan_id == plan_id).first()
    plan_dc_name = sibling_dc_row[0] if sibling_dc_row else None

    # Same idea for plan_created_at - a late-discovered trip belongs to the
    # same plan as its siblings, so it shares the same real creation time,
    # even though we never saw the original webhook for THIS trip specifically.
    sibling_created_row = db.query(TripBaseline.plan_created_at).filter(
        TripBaseline.plan_id == plan_id, TripBaseline.plan_created_at.isnot(None),
    ).first()
    plan_created_at_for_siblings = sibling_created_row[0] if sibling_created_row else None

    newly_confirmed = []
    newly_discovered = []

    for trip_id, t in trips.items():
        if trip_id not in baseline_trip_ids:
            # A trip added to this plan AFTER our original baseline capture
            # (e.g. a re-optimization on Mojro's side) - we never had a
            # chance to record its original planned state, so the first
            # time we see it becomes its baseline instead. This is honestly
            # a "first known state", not a true pre-optimization snapshot,
            # but it's the best we can do without ever having seen it before.
            # It is NOT marked confirmed yet - same rule as everything else,
            # it only becomes confirmed if it later disappears from a
            # subsequent re-download.
            row = TripBaseline(
                trip_id=trip_id,
                plan_id=t.get("plan_id"),
                trip_name=t.get("trip_name"),
                trip_date=t.get("trip_date"),
                dc_name=plan_dc_name,
                baseline_unavailable=1,
                plan_created_at=plan_created_at_for_siblings,
                vehicle_category=t.get("vehicle_category"),
                vehicle_id=t.get("vehicle_id"),
                driver_name=t.get("driver_name"),
                planned_trip_distance_km=_to_float(t.get("trip_distance_km")),
                planned_trip_duration_h=_to_float(t.get("trip_duration_h")),
                trip_weight_kg=_to_float(t.get("trip_weight_kg")),
                trip_volume_cm3=_to_float(t.get("trip_volume_cm3")),
                weight_utilization=_to_float(t.get("weight_utilization")),
                space_utilization=_to_float(t.get("space_utilization")),
                distance_utilization=_to_float(t.get("distance_utilization")),
                time_utilization=_to_float(t.get("time_utilization")),
                trip_cost=_to_float(t.get("trip_cost")),
                no_of_stops=t.get("no_of_stops"),
                raw=t,
            )
            db.add(row)
            db.flush()
            for s in t["stops"]:
                db.add(StopBaseline(
                    trip_id=trip_id,
                    activity=s.get("activity"),
                    ship_to_code=s.get("ship_to_code"),
                    ship_to_name=s.get("ship_to_name"),
                    sequence=_to_float(s.get("sequence")),
                    planned_arrival=s.get("arrival"),
                    reference_order_number=s.get("reference_order_number"),
                    address=s.get("address"),
                    weight_kg=_to_float(s.get("weight_kg")),
                ))
            baseline_trip_ids.add(trip_id)
            newly_discovered.append(trip_id)
            continue

        existing = db.get(TripConfirmed, trip_id)
        if existing:
            db.query(StopConfirmed).filter(StopConfirmed.trip_id == trip_id).delete()
            db.delete(existing)
            db.flush()

        row = TripConfirmed(
            trip_id=trip_id,
            plan_id=t.get("plan_id"),
            event_type="Confirmed",
            vehicle_category=t.get("vehicle_category"),
            vehicle_id=t.get("vehicle_id"),
            driver_name=t.get("driver_name"),
            actual_trip_distance_km=_to_float(t.get("trip_distance_km")),
            actual_trip_duration_h=_to_float(t.get("trip_duration_h")),
            trip_weight_kg=_to_float(t.get("trip_weight_kg")),
            trip_volume_cm3=_to_float(t.get("trip_volume_cm3")),
            weight_utilization=_to_float(t.get("weight_utilization")),
            space_utilization=_to_float(t.get("space_utilization")),
            distance_utilization=_to_float(t.get("distance_utilization")),
            time_utilization=_to_float(t.get("time_utilization")),
            trip_cost=_to_float(t.get("trip_cost")),
            no_of_stops=t.get("no_of_stops"),
            raw=t,
        )
        db.add(row)
        db.flush()
        for s in t["stops"]:
            db.add(StopConfirmed(
                trip_id=trip_id,
                activity=s.get("activity"),
                ship_to_code=s.get("ship_to_code"),
                ship_to_name=s.get("ship_to_name"),
                sequence=_to_float(s.get("sequence")),
                actual_arrival=s.get("arrival"),
                reference_order_number=s.get("reference_order_number"),
                address=s.get("address"),
                weight_kg=_to_float(s.get("weight_kg")),
            ))
        newly_confirmed.append(trip_id)

    still_planned = sorted(baseline_trip_ids - set(newly_confirmed) - {
        row.trip_id for row in db.query(TripConfirmed).filter(TripConfirmed.plan_id == plan_id).all()
    })

    # Update the tracker: mark done once every baseline trip for this plan
    # has a confirmed record, otherwise just record that we checked.
    tracker = db.get(PendingReconfirm, plan_id)
    if tracker:
        tracker.last_checked_at = datetime.now(timezone.utc)
        tracker.attempts = (tracker.attempts or 0) + 1
        if not still_planned:
            tracker.done = 1

    db.commit()
    return {
        "status": "ok",
        "plan_id": plan_id,
        "newly_confirmed": newly_confirmed,
        "still_planned": still_planned,
        "newly_discovered": newly_discovered,
    }


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/internal/geocode-test")
def geocode_test(address: str = "Mumbai, India", _auth: bool = Depends(verify_api_key)):
    """
    Diagnostic: tries geocoding a single address right now, bypassing the
    cache entirely, and reports exactly what happened. Use this to tell
    apart "Nominatim is reachable but this specific messy address doesn't
    parse well" from "something's blocking geocoding entirely from Render".
    Try the default (a simple, well-known place) first - if that also
    fails, the problem isn't address quality, it's connectivity/blocking.
    """
    try:
        lat, lng = geocode_module.geocode_address(address)
        return {
            "address_tried": address,
            "resolved": lat is not None,
            "lat": lat,
            "lng": lng,
        }
    except Exception as e:
        return {"address_tried": address, "resolved": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 5. DATA RETENTION - keep only a rolling recent window
#    Deletes trips (and everything linked to them) older than N days, based
#    on trip_date. Defaults to a DRY RUN that reports what would be deleted
#    without touching anything - pass confirm=true to actually delete.
#    This is irreversible, so the dry-run default is intentional and should
#    stay that way even if this gets automated via a cron job later.
# ---------------------------------------------------------------------------
@app.post("/internal/cleanup-old-data")
def cleanup_old_data(
    days: int = 2,
    confirm: bool = False,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    old_trip_ids = [
        row.trip_id for row in
        db.query(TripBaseline.trip_id).filter(TripBaseline.trip_date < cutoff_date).all()
    ]
    old_plan_ids = sorted({
        row.plan_id for row in
        db.query(TripBaseline.plan_id).filter(TripBaseline.trip_date < cutoff_date).distinct().all()
    })

    if not old_trip_ids:
        return {
            "dry_run": not confirm,
            "cutoff_date": cutoff_date,
            "message": f"Nothing older than {cutoff_date} found - already clean.",
            "trips_affected": 0,
            "plans_affected": 0,
        }

    if not confirm:
        return {
            "dry_run": True,
            "cutoff_date": cutoff_date,
            "message": "This is a preview only - nothing was deleted. Re-run with confirm=true to actually delete.",
            "trips_affected": len(old_trip_ids),
            "plans_affected": len(old_plan_ids),
            "sample_plan_ids": old_plan_ids[:10],
        }

    # Actual deletion, in dependency order, using bulk SQL deletes (not
    # loading each row as a Python object) so this stays memory-safe even
    # for a large cleanup - same lesson learned from the OOM issue earlier.
    stop_baseline_deleted = (
        db.query(StopBaseline).filter(StopBaseline.trip_id.in_(old_trip_ids))
        .delete(synchronize_session=False)
    )
    stop_confirmed_deleted = (
        db.query(StopConfirmed).filter(StopConfirmed.trip_id.in_(old_trip_ids))
        .delete(synchronize_session=False)
    )
    trip_confirmed_deleted = (
        db.query(TripConfirmed).filter(TripConfirmed.trip_id.in_(old_trip_ids))
        .delete(synchronize_session=False)
    )
    trip_baseline_deleted = (
        db.query(TripBaseline).filter(TripBaseline.trip_id.in_(old_trip_ids))
        .delete(synchronize_session=False)
    )
    pending_reconfirm_deleted = (
        db.query(PendingReconfirm).filter(PendingReconfirm.plan_id.in_(old_plan_ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    return {
        "dry_run": False,
        "cutoff_date": cutoff_date,
        "message": f"Deleted all data with trip_date before {cutoff_date}.",
        "trips_affected": len(old_trip_ids),
        "plans_affected": len(old_plan_ids),
        "rows_deleted": {
            "trip_baseline": trip_baseline_deleted,
            "stop_baseline": stop_baseline_deleted,
            "trip_confirmed": trip_confirmed_deleted,
            "stop_confirmed": stop_confirmed_deleted,
            "pending_reconfirm": pending_reconfirm_deleted,
        },
    }


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_utc(dt):
    """Make sure a datetime is timezone-aware UTC before comparing/subtracting."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
