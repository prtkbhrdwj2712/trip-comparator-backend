import io
import json
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Depends, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import init_db, get_db
from .models import TripBaseline, StopBaseline, TripConfirmed, StopConfirmed, PendingReconfirm
from .xlsx_parser import parse_dispatch_workbook
from .diff_engine import compute_diff
from .auth import verify_api_key

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
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_api_key),
):
    content = await file.read()
    try:
        trips = parse_dispatch_workbook(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse workbook: {e}")

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
            weight_kg=_to_float(s.get("weight_kg")),
        ))
    db.commit()
    return {"status": "ok", "trip_id": trip_id}


# ---------------------------------------------------------------------------
# 3. DASHBOARD READ API
# ---------------------------------------------------------------------------
@app.get("/api/trips")
def list_trips(db: Session = Depends(get_db)):
    baselines = db.query(TripBaseline).all()
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
            **diff,
        })
    return out


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str, db: Session = Depends(get_db)):
    b = db.get(TripBaseline, trip_id)
    if not b:
        raise HTTPException(status_code=404, detail="Trip not found in baseline")
    confirmed = db.get(TripConfirmed, trip_id)
    diff = compute_diff(b, confirmed)
    return {
        "trip_id": trip_id,
        "plan_id": b.plan_id,
        "trip_name": b.trip_name,
        "baseline": {
            "vehicle_category": b.vehicle_category,
            "vehicle_id": b.vehicle_id,
            "driver_name": b.driver_name,
            "weight_utilization": b.weight_utilization,
            "trip_weight_kg": b.trip_weight_kg,
            "no_of_stops": b.no_of_stops,
            "stops": [
                {"code": s.ship_to_code, "name": s.ship_to_name, "sequence": s.sequence,
                 "arrival": s.planned_arrival}
                for s in b.stops if s.activity == "Drop"
            ],
        },
        "confirmed": None if not confirmed else {
            "vehicle_category": confirmed.vehicle_category,
            "vehicle_id": confirmed.vehicle_id,
            "driver_name": confirmed.driver_name,
            "weight_utilization": confirmed.weight_utilization,
            "trip_weight_kg": confirmed.trip_weight_kg,
            "no_of_stops": confirmed.no_of_stops,
            "stops": [
                {"code": s.ship_to_code, "name": s.ship_to_name, "sequence": s.sequence,
                 "arrival": s.actual_arrival}
                for s in confirmed.stops if s.activity == "Drop"
            ],
        },
        "diff": diff,
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

    newly_confirmed = []

    for trip_id, t in trips.items():
        if trip_id not in baseline_trip_ids:
            # A trip we've never seen baselined for this plan - ignore rather
            # than guess; shouldn't normally happen.
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
    }


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


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
