"""In-memory sliding-window rate limiter for FastAPI.

Buckets are keyed by (ip, matched prefix) for the strict prefixes
(/mcp/sse, /mcp/messages, /auth/, /oauth/register) and by (ip, full path)
for the general /api/ and fallback buckets — pooling every SPA call into one
bucket would throttle normal users.
"""

import time
import logging
from collections import defaultdict, deque
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

# (route_prefix, window_seconds, max_requests)
RATE_LIMITS: list[tuple[str, int, int]] = [
    ("/mcp/sse",          60,    60),   # 60 req/min
    ("/mcp/messages",     60,   120),   # 120 req/min
    ("/auth/login/magic", 60,    10),   # 10 req/min — email-send endpoint, tighter
    ("/auth/",            60,    60),   # 60 req/min
    ("/oauth/register", 3600,    10),   # 10 client registrations/hour — dynamic registration
    ("/api/admin/",       60,    60),   # 60 req/min
    ("/api/user/",        60,    60),   # 60 req/min
    ("/api/mcp-token",    60,    60),   # 60 req/min
    ("/api/ruling/",          60, 120),
    ("/api/private-ruling/",  60, 120),
    ("/api/regulatory-guide/", 60, 120),
    ("/api/afsa-guide/",      60, 120),
    ("/api/tax-cases/case/",  60, 120),
    ("/api/",          60, 300),   # 300 req/min — general API
    ("",                  60,   600),   # fallback: 600 req/min
]

# Buckets keyed on the matched prefix (shared by every path under it).
_STRICT_PREFIXES = {"/mcp/sse", "/mcp/messages", "/auth/", "/oauth/register"}

# Bound memory: above this many buckets, sweep out empty / stale deques.
_SWEEP_THRESHOLD = 5000


def _find_limit(path: str) -> tuple[int, int, str]:
    for prefix, window, max_req in RATE_LIMITS:
        if path.startswith(prefix):
            return window, max_req, (prefix or "all")
    return 60, 300, "all"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter keyed by (ip, prefix) or (ip, path)."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        # buckets[(ip, bucket_key)] = deque of timestamps
        self._buckets: dict[tuple[str, str], deque] = defaultdict(deque)

    def _sweep(self, now: float) -> None:
        """Drop buckets whose newest entry is outside every window (bounds memory)."""
        if len(self._buckets) <= _SWEEP_THRESHOLD:
            return
        stale_before = now - max(w for _, w, _ in RATE_LIMITS)
        for key in [k for k, dq in self._buckets.items() if not dq or dq[-1] < stale_before]:
            del self._buckets[key]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        # Skip static assets
        path = request.url.path
        if path.startswith("/assets/"):
            return await call_next(request)

        # Get client IP (respecting Cloudflare / proxy headers)
        ip = request.headers.get("cf-connecting-ip")
        if not ip:
            ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
            ip = ip.split(",")[0].strip()

        window, max_req, prefix = _find_limit(path)
        # One bucket per route family. Keying on the full path gave every distinct document its
        # own bucket, so bulk harvesting (57,608 private rulings, 11,952 public rulings) had no
        # limit at all. Grouping by prefix is what the module docstring always claimed.
        key = (ip, prefix)

        now = time.time()
        bucket = self._buckets[key]

        # Prune timestamps outside the window
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= max_req:
            retry_after = int(bucket[0] + window - now) + 1
            logger.warning(
                "Rate limit hit",
                extra={"extra_info": {"ip": ip, "path": path, "limit": max_req, "window": window}},
            )
            return JSONResponse(
                {"detail": f"Rate limit exceeded. Try again in {retry_after}s."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        self._sweep(now)
        return await call_next(request)
