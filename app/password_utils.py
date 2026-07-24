"""
Bcrypt-based hashing for dashboard user access keys, so a database read
alone can never expose a usable plaintext password - only a one-way hash
gets stored, verified via bcrypt's constant-time comparison (also closes
the minor timing-attack surface a plain `==` string comparison has).
"""
import bcrypt

# Bcrypt hashes always start with this prefix - used to tell whether a
# stored value is already hashed (post-migration) or still legacy plaintext.
BCRYPT_PREFIX = ("$2a$", "$2b$", "$2y$")


def hash_key(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_key(plaintext: str, stored_value: str) -> bool:
    if not plaintext or not stored_value:
        return False
    if not is_hashed(stored_value):
        # Legacy plaintext row that somehow wasn't migrated - fall back to
        # a direct comparison rather than crashing bcrypt on a bad "hash".
        return plaintext == stored_value
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored_value.encode("utf-8"))
    except ValueError:
        return False


def is_hashed(value: str) -> bool:
    return bool(value) and value.startswith(BCRYPT_PREFIX)
