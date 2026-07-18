# Trip Comparator Backend

Receives your two existing webhooks (plan baseline + trip confirmation),
stores both sides, and exposes an API the dashboard reads from.

Tested end-to-end against your real files:
`Chrompet_PL733571187917852672_20260718_063048.xlsx` (baseline, 29 trips) and
a simulated confirmation event — ingestion and diff both verified working.

## What's in here
- `app/main.py` — the FastAPI app: two webhook receivers + two read endpoints
- `app/models.py` — DB tables (trip_baseline, stop_baseline, trip_confirmed, stop_confirmed)
- `app/xlsx_parser.py` — same Excel-parsing logic used to build the first dashboard demo
- `app/diff_engine.py` — planned-vs-confirmed comparison (the 12 fields you listed)
- `app/auth.py` — simple shared-secret (API key) check on incoming webhooks

## 1. Deploy on Render

1. Push this folder to a GitHub repo.
2. In Render: **New +** → **Web Service** → connect the repo.
3. Runtime: Python 3. Build command: `pip install -r requirements.txt`.
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. **New +** → **PostgreSQL** → create a database (any free/starter plan to begin with).
   Copy its **Internal Database URL**.
5. On the Web Service → Environment → add:
   - `DATABASE_URL` = the Postgres Internal Database URL from step 4
   - `WEBHOOK_API_KEY` = any long random string you choose — this is the shared
     secret both webhooks will send back to you
6. Deploy. Check `https://<your-service>.onrender.com/health` returns `{"status":"ok"}`.

## 2. Wire up the baseline webhook

Your existing `mojro-ap-plans-webhook-uploader` service already uploads the
dispatch-summary Excel to Drive when a plan is baselined. Add one more HTTP
call from that same service, right after the Drive upload:

```
POST https://<your-service>.onrender.com/webhooks/plan-baseline
Headers: X-API-Key: <the WEBHOOK_API_KEY you set>
Body: multipart/form-data, field name "file" = the same xlsx file just uploaded to Drive
```

## 3. Wire up the confirmation webhook

Point (or add a second call from) your "Trip Events" flow at:

```
POST https://<your-service>.onrender.com/webhooks/trip-confirmed
Headers: X-API-Key: <the WEBHOOK_API_KEY you set>
Body: JSON — see "Payload mapping" below
```

**Payload mapping — needs a final check.** I don't have a real sample of what
this webhook actually sends (the screenshot showed the config panel, not a
fired event). The receiver currently looks for these keys (accepting a couple
common namings for each):

| Our field | Accepted JSON keys |
|---|---|
| trip id | `trip_id`, `tripId` |
| vehicle category | `vehicle_category`, `vehicleCategory` |
| vehicle number | `vehicle_id`, `vehicleNumber`, `vehicleId` |
| weight utilization | `weight_utilization`, `weightUtilization` |
| total weight | `trip_weight_kg`, `weightKg` |
| dealer/stop list | `stops` or `dealers` — list of `{ship_to_code/dealerCode, ship_to_name/dealerName, sequence, actual_arrival/arrivalTime}` |

**Trigger a real "Trip Confirmed" test event once this is deployed, forward me
the payload it sends, and I'll tighten `app/main.py`'s field mapping to match
exactly** — right now it's a best-effort guess based on common naming
conventions, not your actual schema.

## 4. Read API for the dashboard

- `GET /api/trips` — every trip with its status (`awaiting_confirmation`,
  `confirmed_no_changes`, `confirmed_with_changes`) and diff summary
- `GET /api/trips/{trip_id}` — full baseline + confirmed detail for one trip

The dashboard I built earlier reads from an embedded JSON snapshot — next
step is pointing it at this API instead (either polling `/api/trips` every
N seconds, or we add a websocket/SSE endpoint if you want push updates
instead of polling).

## Notes / open items

- **Grams vs kilograms**: the source column is `trip weight(kg)` and looks
  like kilograms in your real data (e.g. 1712.97). If you actually want grams
  displayed, multiply by 1000 in the dashboard layer — I haven't done the
  conversion here since I wasn't sure which you wanted.
- **Dealer identity**: a few rows in your real file have no dealer code and
  fall back to a raw address string as the "key" — this showed up in testing.
  Worth flagging to whoever owns the plan data, since it'll show up as a
  dealer-diff with a blank name.
- **Auth**: using a simple shared API key rather than replicating the SAP CPI
  OAuth2 client-credentials flow shown in your screenshot — that flow secures
  calls *into* CPI, it doesn't need to be mirrored for CPI calling *out* to us.
  Happy to build full OAuth2 support later if a security review requires it.
