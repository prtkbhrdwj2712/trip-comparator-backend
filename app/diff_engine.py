"""
Computes the planned-vs-confirmed diff for a single trip.
This is the same logic used to build the first dashboard demo, adapted to
read from ORM rows instead of the two Excel files.
"""
from datetime import datetime, timezone, timedelta

# If a trip has no confirmed record at all after this long, we no longer
# treat it as merely "awaiting" - matches GIVE_UP_AFTER_HOURS in main.py,
# where the polling system itself stops checking. Once polling has given
# up with zero confirmation ever appearing, the trip is presumed cancelled
# rather than left in permanent limbo. 13 hours is the real maximum a plan
# stays in its lifecycle end-to-end - anything still unconfirmed past that
# genuinely won't be confirmed at all.
UNCONFIRMED_CANCELLATION_HOURS = 13

TRIP_LEVEL_FIELDS = [
    # (baseline_field, confirmed_field, label) - baseline_field == confirmed_field
    # for most, but distance/duration are named differently on each side
    # (planned_ vs actual_).
    ("vehicle_category", "vehicle_category", "Vehicle Category"),
    ("vehicle_id", "vehicle_id", "Vehicle Number"),
    ("driver_name", "driver_name", "Driver"),
    ("trip_weight_kg", "trip_weight_kg", "Total Weight (kg)"),
    ("weight_utilization", "weight_utilization", "Weight Utilization %"),
    ("space_utilization", "space_utilization", "Space Utilization %"),
    ("no_of_stops", "no_of_stops", "Number of Dealers"),
    ("trip_cost", "trip_cost", "Trip Cost"),
    ("planned_trip_distance_km", "actual_trip_distance_km", "Distance (km)"),
    ("planned_trip_duration_h", "actual_trip_duration_h", "Duration (h)"),
]


def _dealer_keys_from_stops(stops):
    """stops: list of simple dicts/tuples with .activity, .ship_to_code (or dict keys)."""
    keys = set()
    for s in stops:
        activity = s["activity"] if isinstance(s, dict) else s.activity
        code = s["ship_to_code"] if isinstance(s, dict) else s.ship_to_code
        if activity == "Drop":
            keys.add(f"{activity}_{code}")
    return keys


def _is_legitimate_confirmation(confirmed_trip):
    """
    A trip that genuinely ran always gets a real cost and a real vehicle
    plate assigned - a null cost, or a vehicle_id still in the synthetic
    planning-placeholder format (e.g. "1607_V735836175095877633_1" - note
    the underscores, which no real Indian registration plate ever has),
    means this trip never actually executed. It most likely got cancelled
    but happened to still appear in a later poll with unchanged data,
    which would otherwise look identical to a genuine no-changes confirmation.
    """
    if confirmed_trip.trip_cost is None:
        return False
    if confirmed_trip.vehicle_id and "_" in confirmed_trip.vehicle_id:
        return False
    return True


def _compute_cpt(cost, weight_kg):
    """Cost per ton = cost / (weight_kg / 1000). None if not computable (missing cost, or zero/missing weight)."""
    if cost is None or not weight_kg:
        return None
    return cost / (weight_kg / 1000)


def _cpt_change_explanation(baseline_cost, baseline_weight, confirmed_cost, confirmed_weight):
    """
    Cost per ton can shift for two independent reasons - the cost changed,
    the weight changed, or both - and a CPT number alone doesn't say which.
    This builds a plain-language note naming whichever factor(s) actually
    moved, so the reason is always visible rather than something you have
    to work out by cross-referencing the cost and weight fields yourself.
    Covers two distinct cases: CPT itself moved (name what drove it), or
    CPT stayed flat even though cost/weight visibly changed (say so
    explicitly - proportional movement is itself a real, useful fact, not
    something to go silent about).
    """
    cost_changed = baseline_cost is not None and confirmed_cost is not None and baseline_cost != confirmed_cost
    weight_changed = bool(baseline_weight) and bool(confirmed_weight) and baseline_weight != confirmed_weight

    if not cost_changed and not weight_changed:
        return None

    parts = []
    if weight_changed:
        parts.append(f"weight {baseline_weight:g}kg → {confirmed_weight:g}kg")
    if cost_changed:
        parts.append(f"cost ₹{baseline_cost:g} → ₹{confirmed_cost:g}")

    baseline_cpt = _compute_cpt(baseline_cost, baseline_weight)
    confirmed_cpt = _compute_cpt(confirmed_cost, confirmed_weight)
    cpt_actually_changed = (
        baseline_cpt is not None and confirmed_cpt is not None
        and round(baseline_cpt, 2) != round(confirmed_cpt, 2)
    )

    if not cpt_actually_changed:
        # Cost and/or weight moved, but proportionally - CPT stayed flat.
        # Worth stating plainly rather than leaving it unexplained.
        return f"CPT unchanged even though {' and '.join(parts)} - they moved proportionally"

    if cost_changed and weight_changed:
        driver = "both cost and weight changing"
    elif weight_changed:
        driver = "weight changing (cost unchanged)"
    else:
        driver = "cost changing (weight unchanged)"

    return f"Driven by {driver}: {', '.join(parts)}"


def _count_orders(stops):
    """
    Counts real orders across Drop stops - reference_order_number can hold
    several orders bundled into one comma-joined string for a single dealer
    stop (e.g. "0448668629_2,0448919058"), so this is NOT the same as the
    number of dealers/stops. Returns 0 gracefully if the field isn't present
    (e.g. bulk-list calls that only pre-fetch a few lightweight columns for
    performance) rather than erroring - order count is really only reliable
    in the single-trip detail view anyway.
    """
    total = 0
    for s in stops:
        activity = s["activity"] if isinstance(s, dict) else s.activity
        if activity != "Drop":
            continue
        ref = s.get("reference_order_number") if isinstance(s, dict) else getattr(s, "reference_order_number", None)
        if ref:
            total += len([x for x in ref.split(",") if x.strip()])
    return total


def compute_diff(baseline_trip, confirmed_trip, baseline_stops=None, confirmed_stops=None):
    """
    baseline_trip: TripBaseline ORM instance
    confirmed_trip: TripConfirmed ORM instance, or None
    baseline_stops / confirmed_stops: optional pre-fetched lists of simple
        dicts ({"activity", "ship_to_code", "ship_to_name"}) - pass these
        when calling this for many trips at once (e.g. the /api/trips list)
        to avoid triggering a lazy-load of the full .stops relationship for
        every single trip, which is what was driving memory usage way up.
        If omitted, falls back to the ORM relationship (fine for one-off
        single-trip lookups like /api/trips/{trip_id}).
    """
    if confirmed_trip is None:
        # Prefer the real plan creation time (from Mojro's own webhook) -
        # only fall back to our own ingestion timestamp for older trips
        # baselined before this field existed, since our ingestion time is
        # just a proxy and can run a bit later than the true creation time.
        reference_time = baseline_trip.plan_created_at or baseline_trip.received_at
        if reference_time is not None:
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - reference_time
            if age > timedelta(hours=UNCONFIRMED_CANCELLATION_HOURS):
                return {"status": "cancelled"}
        return {"status": "awaiting_confirmation"}

    if not _is_legitimate_confirmation(confirmed_trip):
        return {
            "status": "likely_cancelled",
            "trip_level_diff": [],
            "dealers_baseline_count": None,
            "dealers_confirmed_count": None,
            "dealers_dropped": [],
            "dealers_added": [],
            "baseline_total_weight_kg": baseline_trip.trip_weight_kg,
            "confirmed_total_weight_kg": confirmed_trip.trip_weight_kg,
        }

    def _norm(v):
        # Treat None and "" as the same "empty" value so a blank baseline
        # field doesn't get flagged as "changed" against a null confirmed
        # field (or vice versa).
        return "" if v is None else str(v)

    trip_diffs = []
    for baseline_field, confirmed_field, label in TRIP_LEVEL_FIELDS:
        bv = getattr(baseline_trip, baseline_field, None)
        av = getattr(confirmed_trip, confirmed_field, None)
        if _norm(bv) != _norm(av):
            # Use a normalized field key (not the raw attribute name, which
            # can differ between baseline/confirmed) so callers can match on
            # one consistent identifier regardless of which side it came from.
            if "distance" in baseline_field:
                normalized_key = "distance_km"
            elif "duration" in baseline_field:
                normalized_key = "duration_h"
            else:
                normalized_key = baseline_field
            trip_diffs.append({"field": normalized_key, "label": label, "planned": bv, "confirmed": av})

    b_stops = baseline_stops if baseline_stops is not None else [
        {"activity": s.activity, "ship_to_code": s.ship_to_code, "ship_to_name": s.ship_to_name,
         "reference_order_number": s.reference_order_number}
        for s in baseline_trip.stops
    ]
    a_stops = confirmed_stops if confirmed_stops is not None else [
        {"activity": s.activity, "ship_to_code": s.ship_to_code, "ship_to_name": s.ship_to_name,
         "reference_order_number": s.reference_order_number}
        for s in confirmed_trip.stops
    ]

    b_keys = _dealer_keys_from_stops(b_stops)
    a_keys = _dealer_keys_from_stops(a_stops)

    removed = b_keys - a_keys   # dealers in baseline, not in actual
    added = a_keys - b_keys     # dealers in actual, not in baseline

    b_by_key = {f"{s['activity']}_{s['ship_to_code']}": s for s in b_stops}
    a_by_key = {f"{s['activity']}_{s['ship_to_code']}": s for s in a_stops}

    removed_list = [{"code": b_by_key[k]["ship_to_code"], "name": b_by_key[k]["ship_to_name"]} for k in removed]
    added_list = [{"code": a_by_key[k]["ship_to_code"], "name": a_by_key[k]["ship_to_name"]} for k in added]

    baseline_order_count = _count_orders(b_stops)
    confirmed_order_count = _count_orders(a_stops)

    has_changes = bool(trip_diffs) or bool(removed) or bool(added)

    baseline_cpt = _compute_cpt(baseline_trip.trip_cost, baseline_trip.trip_weight_kg)
    confirmed_cpt = _compute_cpt(confirmed_trip.trip_cost, confirmed_trip.trip_weight_kg)
    cpt_explanation = _cpt_change_explanation(
        baseline_trip.trip_cost, baseline_trip.trip_weight_kg,
        confirmed_trip.trip_cost, confirmed_trip.trip_weight_kg,
    )
    if cpt_explanation is not None:
        # Note: this fires whenever cost and/or weight changed at all - even
        # if CPT itself stayed flat (proportional movement). cpt_value_changed
        # is tracked separately so callers can distinguish "CPT actually
        # moved" from "cost/weight moved but cancelled out" for styling.
        cpt_value_changed = (
            baseline_cpt is not None and confirmed_cpt is not None
            and round(baseline_cpt, 2) != round(confirmed_cpt, 2)
        )
        trip_diffs.append({
            "field": "cost_per_ton",
            "label": "Cost per Ton",
            "planned": round(baseline_cpt, 2) if baseline_cpt is not None else None,
            "confirmed": round(confirmed_cpt, 2) if confirmed_cpt is not None else None,
            "explanation": cpt_explanation,
            "cpt_value_changed": cpt_value_changed,
        })
        if cpt_value_changed:
            has_changes = True

    return {
        "status": "confirmed_with_changes" if has_changes else "confirmed_no_changes",
        "trip_level_diff": trip_diffs,
        "dealers_baseline_count": len(b_keys),
        "dealers_confirmed_count": len(a_keys),
        "dealers_dropped": removed_list,     # req #9
        "dealers_added": added_list,         # req #10
        "baseline_total_weight_kg": baseline_trip.trip_weight_kg,
        "confirmed_total_weight_kg": confirmed_trip.trip_weight_kg,
        "baseline_cost_per_ton": round(baseline_cpt, 2) if baseline_cpt is not None else None,
        "confirmed_cost_per_ton": round(confirmed_cpt, 2) if confirmed_cpt is not None else None,
        "baseline_order_count": baseline_order_count,
        "confirmed_order_count": confirmed_order_count,
    }
