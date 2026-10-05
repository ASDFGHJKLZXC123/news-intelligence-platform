"""An explicit authored-time ownership clock for retained historical fixtures.

The authored timestamps still drive run identity, snapshots and accounting. Tracking
claim/retry time supplies their implicit write guards with the same synthetic clock;
production and the Phase 4 expiry/fault tests continue to use the real wall clock.
"""

from __future__ import annotations

import datetime as dt
import sys
from functools import wraps
from types import ModuleType


def install_authored_processing_clock(monkeypatch, module: ModuleType) -> None:
    from services.personal import coordinator, processing, runs

    current: dict[str, dt.datetime | None] = {"now": None}
    real_now = processing.utc_now
    wrappers = {}
    for name in ("create_daily_run", "retry_run", "acquire_run"):
        original = getattr(runs, name)

        def make_wrapper(function):
            @wraps(function)
            def wrapper(*args, **kwargs):
                if kwargs.get("now") is not None:
                    current["now"] = kwargs["now"]
                return function(*args, **kwargs)

            return wrapper

        wrappers[name] = make_wrapper(original)
        targets = [
            loaded
            for loaded_name, loaded in list(sys.modules.items())
            if loaded is not None
            and (
                loaded_name.startswith("tests.integration.test_personal")
                or loaded_name == "apps.api.personal"
            )
        ]
        targets.append(module)
        for loaded in targets:
            for alias, value in list(vars(loaded).items()):
                if value is original:
                    monkeypatch.setattr(loaded, alias, wrappers[name])
        monkeypatch.setattr(runs, name, wrappers[name])
    monkeypatch.setattr(coordinator, "acquire_run", wrappers["acquire_run"])
    monkeypatch.setattr(runs, "utc_now", lambda: current["now"] or real_now())
    monkeypatch.setattr(processing, "utc_now", lambda: current["now"] or real_now())
