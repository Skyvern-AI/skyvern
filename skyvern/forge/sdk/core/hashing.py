import hashlib
import hmac

from skyvern.config import settings

# Domain separator so this fingerprint can never collide with any other use of SECRET_KEY (e.g. auth
# token signing). Bump the version suffix if the construction ever changes.
_FP_DOMAIN = b"skyvern.download_suffix.diagnostic_fingerprint.v1"


def generate_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _fingerprint_key() -> bytes | None:
    """The server-side HMAC key, or None when no real secret is configured (fail closed).

    Compares against the ``SECRET_KEY`` field default (rather than a hardcoded literal) so it keeps
    fail-closing if that placeholder default ever changes.
    """
    key = settings.SECRET_KEY
    if not key or key == type(settings).model_fields["SECRET_KEY"].default:
        return None
    return key.encode("utf-8")


def diagnostic_fingerprint(value: str | None) -> str:
    """Keyed, non-reversible tag for correlating a possibly-sensitive string across logs.

    Returns ``none`` for None and ``empty:0`` for the empty string. Otherwise returns
    ``<hmac_sha256(SECRET_KEY, value)[:12]>:<len>`` — keyed by the deployment secret so equal values map
    to equal tags for correlation, while the raw value (a download_suffix is often a low-entropy,
    account-number-bearing filename) cannot be recovered by brute force from log access alone; a bare
    unsalted hash of a low-entropy value is trivially reversible. Fails closed to ``unkeyed`` when no
    server-side key is configured rather than emitting a brute-forceable bare hash. The raw value and the
    key never appear in the output. ``surrogatepass`` keeps invalid-UTF-8 on-disk filenames from raising.
    """
    if value is None:
        return "none"
    if not value:
        return "empty:0"
    key = _fingerprint_key()
    if key is None:
        return "unkeyed"
    digest = hmac.new(key, _FP_DOMAIN + b"\x00" + value.encode("utf-8", "surrogatepass"), hashlib.sha256)
    return f"{digest.hexdigest()[:12]}:{len(value)}"
