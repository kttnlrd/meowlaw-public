"""resolve_ref.warm_caches primes the loaders fetch uses, and never raises."""
import backend.services.case_db_service as cds
import backend.services.data_loader as dl
from backend.resolve_ref import warm_caches


def test_warm_calls_both_loaders(monkeypatch):
    calls = []
    for mod, name in ((dl, "load_rulings"), (cds, "case_alias_map")):
        def fake(_n=name):
            calls.append(_n)
        fake.__name__ = name
        monkeypatch.setattr(mod, name, fake)
    warm_caches()
    assert calls == ["load_rulings", "case_alias_map"]


def test_warm_swallows_loader_errors(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(dl, "load_rulings", boom)
    called = []
    monkeypatch.setattr(cds, "case_alias_map", lambda: called.append(1))
    warm_caches()  # must not raise, and the second loader still runs
    assert called == [1]


# --- a request during the warm must wait on the in-flight load, not repeat it ---------------

def test_single_flight_shares_inflight_load():
    import functools
    import threading
    import time

    from backend.services.single_flight import single_flight

    runs = []

    @single_flight
    @functools.lru_cache(maxsize=None)
    def slow():
        runs.append(threading.current_thread().name)
        time.sleep(0.3)
        return object()

    out = []
    threads = [threading.Thread(target=lambda: out.append(slow()), name=f"t{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1  # plain lru_cache runs the loader in every thread that missed
    assert len({id(o) for o in out}) == 1
    slow.cache_clear()
    slow()
    assert len(runs) == 2  # cache_clear still reaches the cache


def test_single_flight_failed_load_is_retried():
    import functools

    from backend.services.single_flight import single_flight

    calls = []

    @single_flight
    @functools.lru_cache(maxsize=None)
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first load fails")
        return 42

    try:
        flaky()
    except RuntimeError:
        pass
    assert flaky() == 42 and len(calls) == 2


def test_warmed_loaders_are_single_flight():
    # warm_caches loads exactly these; both must share an in-flight load with request threads
    import importlib

    for mod, name in (("backend.services.data_loader", "load_rulings"),
                      ("backend.services.case_db_service", "case_alias_map")):
        fn = getattr(importlib.import_module(mod), name)
        assert fn.__code__.co_filename.endswith("single_flight.py"), f"{mod}.{name}"
        assert hasattr(fn, "cache_clear")
