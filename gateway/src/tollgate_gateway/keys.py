"""Tenant API keys: generate, hash, recognise (ADR 0010).

A key is "tg_" plus 256 random bits. Only an HMAC-SHA256 of it, keyed with a server-side secret (the
"pepper"), is stored.

.NET: for passwords you'd use PasswordHasher (deliberately slow, because people pick guessable
passwords). These keys are random, so guessing is hopeless and a slow hash would only add latency to
every request. The pepper means a stolen database copy can't be used to test guesses either.
"""

import hashlib
import hmac
import secrets

PREFIX = "tg_"
DISPLAY_LENGTH = 11  # "tg_" + 8 characters: enough to tell keys apart in a listing
_RANDOM_BYTES = 32  # 256 bits -> 43 url-safe base64 characters


def generate_key() -> str:
    """A new random key. Show it to the user once; it is never stored."""
    return PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)


def hash_key(key: str, pepper: bytes) -> str:
    """The value stored and looked up: hex HMAC-SHA256 of the key, keyed with the pepper.

    Deterministic, so the database can find a key by its hash with a plain indexed lookup.
    """
    return hmac.new(pepper, key.encode(), hashlib.sha256).hexdigest()


def display_prefix(key: str) -> str:
    """The start of a key, safe to store and show."""
    return key[:DISPLAY_LENGTH]


def looks_like_key(token: str) -> bool:
    """Whether a bearer token is one of our API keys (as opposed to a Keycloak JWT)."""
    return token.startswith(PREFIX) and len(token) > len(PREFIX) + 16
