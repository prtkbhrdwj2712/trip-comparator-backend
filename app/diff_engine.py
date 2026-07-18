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


def _dealer_keys(stops):
    return {f"{s.activity}_{s.ship_to_code}" for s in stops if s.activity == "Drop"}


def compute_diff(baseline_trip, confirmed_trip):
    """
    baseline_trip: TripBaseline ORM instance (with .stops loaded)
    confirmed_trip: TripConfirmed ORM instance (with .stops loaded), or None
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

    b_keys = _dealer_keys(baseline_trip.stops)
    a_keys = _dealer_keys(confirmed_trip.stops)

    removed = b_keys - a_keys   # dealers in baseline, not in actual
    added = a_keys - b_keys     # dealers in actual, not in baseline

    b_by_key = {f"{s.activity}_{s.ship_to_code}": s for s in baseline_trip.stops}
    a_by_key = {f"{s.activity}_{s.ship_to_code}": s for s in confirmed_trip.stops}

    removed_list = [{"code": b_by_key[k].ship_to_code, "name": b_by_key[k].ship_to_name} for k in removed]
    added_list = [{"code": a_by_key[k].ship_to_code, "name": a_by_key[k].ship_to_name} for k in added]

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
