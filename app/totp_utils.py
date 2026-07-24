"""
TOTP-based 2FA (standard authenticator-app style: Google Authenticator,
Authy, etc.) for dashboard users. Uses the industry-standard pyotp library
rather than anything custom.
"""
import io
import base64
import pyotp
import qrcode


def generate_secret():
    return pyotp.random_base32()


def get_provisioning_qr_code_base64(secret, username, issuer="Trip Comparator"):
    """
    Returns a base64-encoded PNG of a QR code the user scans into their
    authenticator app. Encodes the standard otpauth:// URI, not anything
    custom - any TOTP-compatible app can read it.
    """
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=username, issuer_name=issuer)

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def verify_totp_code(secret, code):
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)  # allow 1 step (~30s) of clock drift
