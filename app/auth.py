"""
Minimal shared-secret auth for the two inbound webhooks.

Set WEBHOOK_API_KEY as an environment variable on Render, and configure the
same value as a header (e.g. `X-API-Key`) on the outbound call from your
plan-uploader service and your Trip Events / SAP CPI flow.

This is intentionally simple - a static shared secret, checked on every
request - rather than the full OAuth2 client-credentials flow shown in the
Trip Events screenshot. That flow authenticates calls INTO your CPI system;
it doesn't need to be replicated for calls CPI makes OUT to us. If you want
parity later (e.g. because of a security review), this is the one file that
would need to grow into a proper OAuth2 token verifier.
"""
import os
from fastapi import Header, HTTPException

API_KEY = os.environ.get("WEBHOOK_API_KEY", "change-me-before-deploying")


def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    return True
