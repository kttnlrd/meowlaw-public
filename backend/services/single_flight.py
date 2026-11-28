"""Share one in-flight load between threads for an lru_cache'd zero-argument loader.

functools.lru_cache does not coordinate concurrent misses: two threads that call a cold loader
both run it. The startup warm thread (resolve_ref.warm_caches) and the first MCP request would
then build the same multi-second index twice while competing for the GIL, so the request was
slower than with no warm at all. Wrapping the cached loader in a lock makes a caller that arrives
during the load wait for it and reuse the result.
"""
from __future__ import annotations

import functools
import threading


def single_flight(cached):
    """Decorate an ``functools.lru_cache`` loader taking no arguments.

    The cache stays the source of truth (cache_clear/cache_info are passed through). A failed
    load raises to its caller, releases the lock and is retried by the next call, as before.
    """
    lock = threading.RLock()  # re-entrant: a loader that calls itself must not deadlock

    @functools.wraps(cached)
    def wrapper():
        if cached.cache_info().currsize:
            return cached()
        with lock:
            return cached()

    wrapper.cache_clear = cached.cache_clear
    wrapper.cache_info = cached.cache_info
    return wrapper
