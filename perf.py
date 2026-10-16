"""
ORBITCAST — response compression and revalidation.

Nothing in front of the app compresses responses, and the two biggest things
every visitor downloads - the page itself and the event catalogue - are a few
hundred kilobytes of text that gzip shrinks five- to ten-fold. At thousands of
visitors that is the difference between a fast first paint on a phone and a
slow one, and between a small and a large egress bill.

Two pieces:
  - gzip_bytes(): compress once, remember the result by content hash. The page
    and the catalogue are identical for everyone until the next deploy or the
    next scrape, so after the first visitor this is a dictionary lookup.
  - init_perf(app): an after_request hook that gzips text responses the client
    accepts gzip for, and gives GET responses an ETag so a browser that already
    has the current copy gets a 304 with no body.
"""

import gzip
import hashlib
import threading
from collections import OrderedDict

from flask import request

_COMPRESSIBLE = {"text/html", "application/json", "text/css", "text/javascript",
                 "application/javascript", "text/calendar", "text/plain", "image/svg+xml"}
_MIN_BYTES = 1024
_MEMO_MAX = 32
_MEMO_FROM_BYTES = 16 * 1024

_memo: "OrderedDict[bytes, bytes]" = OrderedDict()
_memo_lock = threading.Lock()


def content_tag(body: bytes) -> str:
    return hashlib.blake2b(body, digest_size=12).hexdigest()


def gzip_bytes(body: bytes, key: bytes = None) -> bytes:
    """gzip with a small LRU memo keyed on the content hash. Only large bodies
    are memoised - the shared page and catalogue - so small per-person API
    answers never push those out."""
    if len(body) < _MEMO_FROM_BYTES:
        return gzip.compress(body, compresslevel=6, mtime=0)
    key = key or hashlib.blake2b(body, digest_size=16).digest()
    with _memo_lock:
        hit = _memo.get(key)
        if hit is not None:
            _memo.move_to_end(key)
            return hit
    out = gzip.compress(body, compresslevel=6, mtime=0)
    with _memo_lock:
        _memo[key] = out
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)
    return out


def accepts_gzip() -> bool:
    return "gzip" in (request.headers.get("Accept-Encoding") or "").lower()


def init_perf(app):
    @app.after_request
    def compress_and_tag(response):
        if (response.direct_passthrough or response.is_streamed
                or response.status_code != 200
                or "Content-Encoding" in response.headers
                or response.mimetype not in _COMPRESSIBLE):
            return response
        body = response.get_data()
        # ETag + conditional GET: an unchanged page or catalogue costs a 304.
        # Only on GET/HEAD, and never on a response that sets a cookie - that
        # response is personal to this browser and must not be "not modified"
        # into someone else's.
        if request.method in ("GET", "HEAD") and "Set-Cookie" not in response.headers \
                and "ETag" not in response.headers:
            response.set_etag(content_tag(body), weak=True)
            response.make_conditional(request)
            if response.status_code == 304:
                return response
        if len(body) < _MIN_BYTES:
            return response
        response.headers.add("Vary", "Accept-Encoding")
        if not accepts_gzip():
            return response
        response.set_data(gzip_bytes(body))
        response.headers["Content-Encoding"] = "gzip"
        return response
