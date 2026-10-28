"""Deterministic lifecycle/control races for P-20260923-01."""

from __future__ import annotations

import asyncio
import pytest

from src.swapper import Swapper
from tests.control_helpers import handle
from tests.quick_switch_helpers import make_config


class TestLifecycleStatePriority:
    @pytest.mark.parametrize(
        ("case", "expected_lifecycle", "expected_active", "expected_pending"),
        [
            ("idle", "idle", None, None),
            ("ready-with-idle-timer", "ready", "a", None),
            ("cleanup-failed", "cleanup_failed", None, None),
            ("loading-over-ready-and-failure", "loading", None, "b"),
            ("unloading-over-loading", "unloading", None, None),
            ("shutdown-over-everything", "shutting_down", None, None),
        ],
    )
    async def test_state_uses_all_six_lifecycle_values_and_priority_order(
        self, case, expected_lifecycle, expected_active, expected_pending
    ):
        swapper = Swapper(make_config(), idle_timeout=60)
        sleeping_timer = None
        tracked = handle("a")
        if case != "idle":
            swapper._current = tracked
            swapper._current_model = "a"
        if case == "ready-with-idle-timer":
            swapper._healthy = True
            sleeping_timer = asyncio.create_task(asyncio.Event().wait())
            swapper._idle_task = sleeping_timer
        elif case == "cleanup-failed":
            swapper._cleanup_failed = True
        elif case == "loading-over-ready-and-failure":
            swapper._healthy = False
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
        elif case == "unloading-over-loading":
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
            swapper._stop_only_operation = "manual"
        elif case == "shutdown-over-everything":
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
            swapper._stop_only_operation = "manual"
            swapper._shutting_down = True

        state = swapper.control_state()
        assert state["lifecycle"] == expected_lifecycle
        assert state["active_model"] == expected_active
        assert state["pending_model"] == expected_pending
        assert (state["pending_model"] is not None) is (
            state["lifecycle"] == "loading"
        )

        if sleeping_timer is not None:
            sleeping_timer.cancel()
            await asyncio.gather(sleeping_timer, return_exceptions=True)
            swapper._idle_task = None
        # Avoid invoking lifecycle behavior after deliberately inconsistent
        # priority-table fixtures have served their purpose.
        swapper._current = None
        swapper._stop_only_operation = None
        swapper._swapping = False
        swapper._shutting_down = False
        await swapper.shutdown()

    async def test_actual_switch_cleanup_reports_loading_with_target(
        self, monkeypatch
    ):
        import src.swapper as module

        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def start(model, cmd, port):
            return handle(model, 2)

        async def ready(*args, **kwargs):
            return None

        async def stop(stopped):
            if stopped.model_name == "a":
                stop_entered.set()
                await release_stop.wait()
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        old = handle("a")
        swapper._current, swapper._current_model, swapper._healthy = old, "a", True
        task = asyncio.create_task(swapper.control_load_model("b"))
        try:
            await asyncio.wait_for(stop_entered.wait(), 1)
            assert swapper.control_state() | {} == {
                "models": ["a", "b", "c"],
                "variants": {},
                "active_model": "a",
                "pending_model": "b",
                "lifecycle": "loading",
                "inflight_requests": 0,
                "settings": {
                    "automatic_switch": False,
                    "quick_switch": False,
                    "idle_timeout_seconds": 0.0,
                },
            }
            release_stop.set()
            assert await asyncio.wait_for(task, 1) == "success"
        finally:
            release_stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await swapper.shutdown()
