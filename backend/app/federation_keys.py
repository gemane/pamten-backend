"""
Ed25519 signing for federation (step 2).

Each instance holds a private signing key (base64 32-byte seed in the
FEDERATION_SIGNING_KEY env var) and publishes the matching public key. An
instance signs its export; a peer verifies the signature against the public key
it has on file, so a pulled contribution is *verifiably* the peer's rather than
merely asserted by whoever sent it.

Signatures are detached and cover the canonical JSON of the snapshot payload
(all fields except the signature envelope), so signer and verifier hash the
exact same bytes.
"""
import base64
import hashlib
import json
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from app.config import settings

log = logging.getLogger(__name__)

ALGORITHM = "ed25519"
# Fields that make up the signature envelope — excluded from the signed bytes.
_ENVELOPE = ("signature", "key_id", "algorithm")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def canonical(payload: dict) -> bytes:
    """Deterministic bytes for signing/verifying (envelope fields excluded)."""
    body = {k: v for k, v in payload.items() if k not in _ENVELOPE}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(public_key_b64: str) -> str:
    """Short, stable key id — first 16 hex of SHA-256 over the raw public key."""
    return hashlib.sha256(_b64d(public_key_b64)).hexdigest()[:16]


def _load_private_key() -> Ed25519PrivateKey | None:
    raw = (settings.FEDERATION_SIGNING_KEY or "").strip()
    if not raw:
        return None
    try:
        seed = _b64d(raw)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:  # noqa: BLE001 - bad key is a config error, not a crash
        log.warning("FEDERATION_SIGNING_KEY is set but invalid: %s", exc)
        return None


def public_key_b64() -> str | None:
    """This instance's public key (base64), or None if no signing key is set."""
    key = _load_private_key()
    if key is None:
        return None
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64e(raw)


def sign(payload: dict) -> dict:
    """Return the signature envelope for a payload, or {} if signing is unavailable."""
    key = _load_private_key()
    if key is None:
        return {}
    sig = key.sign(canonical(payload))
    pub = public_key_b64()
    return {"algorithm": ALGORITHM, "key_id": fingerprint(pub), "signature": _b64e(sig)}


def verify(payload: dict, public_key_b64_str: str) -> bool:
    """True if the payload's signature envelope validates against the given key."""
    sig = payload.get("signature")
    if not sig:
        return False
    if payload.get("key_id") and payload["key_id"] != fingerprint(public_key_b64_str):
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(_b64d(public_key_b64_str))
        pub.verify(_b64d(sig), canonical(payload))
        return True
    except (InvalidSignature, Exception):  # noqa: BLE001 - any failure = not verified
        return False


def generate_keypair() -> tuple[str, str]:
    """Fresh (private_seed_b64, public_key_b64) — used by `manage.py gen-federation-key`."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64e(seed), _b64e(pub)
