"""
Computes the planned-vs-confirmed diff for a single trip.
This is the same logic used to build the first dashboard demo, adapted to
read from ORM rows instead of the two Excel files.
"""

TRIP_LEVEL_FIELDS = [
    ("vehicle_category", "Vehicle Category"),
    ("vehicle_id", "Vehicle Number"),
    ("driver_name", "Driver"),
    ("trip_weight_kg", "Total Weight (kg)"),
    ("weight_utilization", "Weight Utilization %"),
    ("no_of_stops", "Number of Dealers"),
    ("trip_cost", "Trip Cost"),
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
        return {"status": "awaiting_confirmation"}

    def _norm(v):
        # Treat None and "" as the same "empty" value so a blank baseline
        # field doesn't get flagged as "changed" against a null confirmed
        # field (or vice versa).
        return "" if v is None else str(v)

    trip_diffs = []
    for field, label in TRIP_LEVEL_FIELDS:
        bv = getattr(baseline_trip, field, None)
        av = getattr(confirmed_trip, field, None)
        if _norm(bv) != _norm(av):
            trip_diffs.append({"field": field, "label": label, "planned": bv, "confirmed": av})

    b_stops = baseline_stops if baseline_stops is not None else [
        {"activity": s.activity, "ship_to_code": s.ship_to_code, "ship_to_name": s.ship_to_name}
        for s in baseline_trip.stops
    ]
    a_stops = confirmed_stops if confirmed_stops is not None else [
        {"activity": s.activity, "ship_to_code": s.ship_to_code, "ship_to_name": s.ship_to_name}
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

    has_changes = bool(trip_diffs) or bool(removed) or bool(added)

    return {
        "status": "confirmed_with_changes" if has_changes else "confirmed_no_changes",
        "trip_level_diff": trip_diffs,
        "dealers_baseline_count": len(b_keys),
        "dealers_confirmed_count": len(a_keys),
        "dealers_dropped": removed_list,     # req #9
        "dealers_added": added_list,         # req #10
        "baseline_total_weight_kg": baseline_trip.trip_weight_kg,
        "confirmed_total_weight_kg": confirmed_trip.trip_weight_kg,
    }
