"""
Minimal shared-secret auth.

Two separate keys, kept deliberately distinct:
  - WEBHOOK_API_KEY: for inbound webhooks and admin/internal endpoints
    (unchanged from before).
  - DASHBOARD_ACCESS_KEY: for the dashboard's own read endpoints
    (/api/trips, /api/dcs, /api/stats). Kept separate so that if the
    dashboard is ever shared more widely, that key can be rotated/shared
    independently without touching the webhook integrations.

Set both as environment variables on Render, and configure the matching
value as a header on the relevant caller (e.g. `X-API-Key` on the
plan-uploader's calls, `X-Dashboard-Key` on the dashboard's fetch calls).

This is intentionally simple - a static shared secret, checked on every
request - rather than the full OAuth2 client-credentials flow shown in the
Trip Events screenshot. That flow authenticates calls INTO your CPI system;
it doesn't need to be replicated for calls CPI makes OUT to us. If you want
parity later (e.g. because of a security review), this is the one file that
would need to grow into a proper OAuth2 token verifier.
"""
import os
from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session

from .database import get_db

API_KEY = os.environ.get("WEBHOOK_API_KEY", "change-me-before-deploying")
DASHBOARD_KEY = os.environ.get("DASHBOARD_ACCESS_KEY", "change-me-before-deploying")


def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    return True


def verify_dashboard_key(x_dashboard_key: str = Header(None), db: Session = Depends(get_db)):
    if x_dashboard_key == DASHBOARD_KEY:
        return True
    if not x_dashboard_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Dashboard-Key header")

    from .models import DashboardUser
    from . import password_utils

    # Access keys are stored hashed (bcrypt, salted) - the same plaintext
    # produces a different hash each time, so we can't query for a direct
    # match. Instead, check the submitted key against each active user's
    # stored hash. Fine at the scale of a handful of named users; would
    # need a different approach (e.g. a fast lookup-then-verify by a
    # separate non-secret identifier) if this ever grew to many users.
    for user in db.query(DashboardUser).filter(DashboardUser.revoked == 0).all():
        if password_utils.verify_key(x_dashboard_key, user.access_key):
            return True

    raise HTTPException(status_code=401, detail="Invalid or missing X-Dashboard-Key header")
