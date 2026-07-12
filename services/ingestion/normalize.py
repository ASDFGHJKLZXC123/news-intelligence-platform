"""Pure URL/content normalization and idempotency hashing.

No I/O: these functions are deterministic and unit-tested without a database. ``url_hash``
is the article idempotency key; re-ingesting the same canonical URL is a no-op.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = frozenset({"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "cmpid", "igshid"})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Canonicalize a URL: lowercase scheme/host, drop tracking params, fragment, trailing slash."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = parts.path.rstrip("/") or "/"
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES) and key.lower() not in _TRACKING_KEYS
    ]
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """Idempotency key for an article: sha256 of the canonical URL."""
    return _sha256(normalize_url(url))


def content_hash(title: str, summary: str = "", body: str | None = None) -> str:
    """Stable hash of an article's text, for exact/near-duplicate detection."""
    basis = "\n".join((title.strip(), (body or summary or "").strip()))
    return _sha256(basis)
