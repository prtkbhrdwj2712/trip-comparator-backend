"""
Free geocoding via OpenStreetMap's Nominatim service.

Nominatim's usage policy requires:
  - Max 1 request per second
  - A descriptive User-Agent identifying the application
  - Results should be cached, not re-fetched for the same query repeatedly

This module respects both by rate-limiting requests and caching every
result in the GeocodeCache table, keyed by the exact address string - since
the same dealer address repeats across many trips/plans, most lookups after
the first few weeks should hit the cache instead of calling Nominatim again.

Known limitation: free-tier geocoding accuracy on Indian addresses varies -
some may fail to resolve or land imprecisely. This is the tradeoff of the
no-cost option versus a paid service like Google Maps.
"""
import time
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "TripComparatorDashboard/1.0 (internal logistics tool)"

_last_request_time = 0.0


def geocode_address(address):
    """
    Returns (lat, lng) or (None, None) if it couldn't be resolved.
    Blocks briefly if called less than 1 second after the previous call,
    to respect Nominatim's rate limit - callers should only reach this
    function for cache misses, not on every request.
    """
    global _last_request_time
    if not address:
        return None, None

    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        _last_request_time = time.time()
        if response.status_code != 200:
            return None, None
        results = response.json()
        if not results:
            return None, None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        _last_request_time = time.time()
        return None, None
