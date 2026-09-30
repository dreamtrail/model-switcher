"""P-20260930-02: utilized (busy) time next to Uptime.

Busy time is the union of wall-clock periods with at least one request in
flight, from validation to finish, including queue and model-load wait.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
import pytest

from src.process_manager import ProcessHandle
from src.stats import StatsCollector
from src.swapper import MAX_QUEUE, Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    eventually,
    make_config,
)
from tests.test_stats_page import (
    FULL,
    FRESH,
    STATS_HTML,
    _execute,
    _lines,
    _render,
    _text,
)


TIP = (
    "Time with at least one request in progress (including waiting for a "
    "model to load); overlapping requests count once"
)


# ---------------------------------------------------------------------------
# Collector timer (controllable monotonic clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr("src.stats.time.monotonic", fake)
    return fake


class TestBusyTimer:
    def test_fresh_collector_is_idle(self, clock):
        collector = StatsCollector()
        clock.now += 10.0
        assert collector.snapshot()["utilization"] == {
            "busy_seconds": 0.0, "busy_share": 0.0,
        }

    def test_zero_uptime_share_is_null(self, clock):
        collector = StatsCollector()
        assert collector.snapshot()["utilization"] == {
            "busy_seconds": 0.0, "busy_share": None,
        }

    def test_single_period_accumulates_on_falling_edge(self, clock):
        collector = StatsCollector()
        clock.now += 10.0
        collector.observe_busy(1)
        clock.now += 5.0
        collector.observe_busy(0)
        clock.now += 5.0
        util = collector.snapshot()["utilization"]
        assert util["busy_seconds"] == pytest.approx(5.0)
        assert util["busy_share"] == pytest.approx(0.25)

    def test_overlapping_requests_count_once(self, clock):
        collector = StatsCollector()
        collector.observe_busy(1)
        clock.now += 2.0
        collector.observe_busy(2)  # second request overlaps
        clock.now += 3.0
        collector.observe_busy(1)
        clock.now += 1.0
        collector.observe_busy(0)
        clock.now += 4.0
        collector.observe_busy(1)  # a second, separate period
        clock.now += 2.0
        collector.observe_busy(0)
        util = collector.snapshot()["utilization"]
        assert util["busy_seconds"] == pytest.approx(8.0)
        assert util["busy_share"] == pytest.approx(8.0 / 12.0)

    def test_running_period_included_in_snapshot(self, clock):
        collector = StatsCollector()
        collector.observe_busy(1)
        clock.now += 1.0
        collector.observe_busy(0)
        clock.now += 1.0
        collector.observe_busy(1)
        clock.now += 3.0
        util = collector.snapshot()["utilization"]
        assert util["busy_seconds"] == pytest.approx(4.0)
        assert util["busy_share"] == pytest.approx(0.8)
        # Snapshots do not close the running period.
        clock.now += 5.0
        util = collector.snapshot()["utilization"]
        assert util["busy_seconds"] == pytest.approx(9.0)
        assert collector._busy_since is not None

    def test_share_is_clamped_to_one(self, clock):
        collector = StatsCollector()
        # Busy period that starts before the monotonic start (only possible
        # with a misbehaving clock) must not push the share above 1.
        collector._busy_total = 50.0
        clock.now += 10.0
        assert collector.snapshot()["utilization"]["busy_share"] == 1.0

    def test_repeated_idle_notifications_are_harmless(self, clock):
        collector = StatsCollector()
        collector.observe_busy(0)
        clock.now += 2.0
        collector.observe_busy(0)
        assert collector.snapshot()["utilization"]["busy_seconds"] == 0.0

    def test_existing_top_level_values_unchanged(self, clock):
        snap = StatsCollector().snapshot(current_model="m", inflight=3)
        assert snap["concurrency"] == {"active": 3, "peak": 0}
        assert snap["current_model"] == "m"
        assert list(snap)[-1] == "utilization"


# ---------------------------------------------------------------------------
# Swapper: where the in-flight counter starts and ends
# ---------------------------------------------------------------------------


def _record_busy(swapper: Swapper) -> list[int]:
    """Record every busy count the swapper reports to the collector."""
    seen: list[int] = []
    original = swapper.stats.observe_busy

    def observe(n: int) -> None:
        seen.append(n)
        original(n)

    swapper.stats.observe_busy = observe
    return seen


class _NoModelRequest(StubRequest):
    def __init__(self, body: bytes = b"{}"):
        self._body = body


def _install(monkeypatch, *, ready=None, proxy=None, start=None):
    import src.swapper as module

    async def default_start(model, cmd, port):
        return ProcessHandle(model, port, FakeProcess(1))

    async def default_ready(*args, **kwargs):
        return None

    async def default_proxy(*args, **kwargs):
        return web.json_response({"ok": True})

    async def stop(handle):
        handle.process.returncode = 0

    monkeypatch.setattr(module, "start_process", start or default_start)
    monkeypatch.setattr(module, "wait_for_ready", ready or default_ready)
    monkeypatch.setattr(module, "stop_process", stop)
    monkeypatch.setattr(module, "proxy_request", proxy or default_proxy)


def _seed(swapper: Swapper, model: str = "a") -> None:
    swapper._current = ProcessHandle(model, 1001, FakeProcess(7))
    swapper._current_model = model
    swapper._healthy = True


def _assert_released(swapper: Swapper, seen: list[int]) -> None:
    assert swapper._busy_requests == 0
    assert swapper._active_requests == 0
    assert swapper.stats._busy_since is None
    assert seen and seen[-1] == 0
    # Every increment has exactly one matching release.
    assert seen.count(0) == sum(
        1 for prev, cur in zip([0] + seen, seen) if cur > prev and prev == 0
    )


class TestSwapperBusyCounter:
    async def test_model_load_wait_counts_as_busy_but_not_active(
        self, monkeypatch
    ):
        loading = asyncio.Event()
        release = asyncio.Event()

        async def ready(*args, **kwargs):
            loading.set()
            await release.wait()

        _install(monkeypatch, ready=ready)
        swapper = Swapper(make_config())
        seen = _record_busy(swapper)
        task = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(loading.wait(), 1)
            assert swapper._busy_requests == 1
            # "Active now" is unchanged: load wait is not active.
            assert swapper._active_requests == 0
            assert swapper.control_state()["inflight_requests"] == 0
            await asyncio.sleep(0.05)
            util = swapper.stats.snapshot()["utilization"]
            assert util["busy_seconds"] >= 0.05  # running period included
            release.set()
            response = await asyncio.wait_for(task, 1)
            assert response.status == 200
            _assert_released(swapper, seen)
            assert seen == [1, 0]
            util = swapper.stats.snapshot()["utilization"]
            assert util["busy_seconds"] >= 0.05
            assert 0.0 < util["busy_share"] <= 1.0
        finally:
            release.set()
            await swapper.shutdown()

    async def test_busy_during_proxy_alongside_active(self, monkeypatch):
        proxying = asyncio.Event()
        release = asyncio.Event()

        async def proxy(*args, **kwargs):
            proxying.set()
            await release.wait()
            return web.json_response({"ok": True})

        _install(monkeypatch, proxy=proxy)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        task = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(proxying.wait(), 1)
            assert swapper._busy_requests == 1
            assert swapper._active_requests == 1
            release.set()
            assert (await asyncio.wait_for(task, 1)).status == 200
            _assert_released(swapper, seen)
        finally:
            release.set()
            await swapper.shutdown()

    async def test_overlapping_requests_count_once(self, monkeypatch):
        proxying = asyncio.Event()
        release = asyncio.Event()
        entered = []

        async def proxy(*args, **kwargs):
            entered.append(1)
            if len(entered) == 2:
                proxying.set()
            await release.wait()
            return web.json_response({"ok": True})

        _install(monkeypatch, proxy=proxy)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        tasks = [
            asyncio.create_task(swapper.handle_request(StubRequest("a")))
            for _ in range(2)
        ]
        try:
            await asyncio.wait_for(proxying.wait(), 1)
            assert swapper._busy_requests == 2
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
            assert seen == [1, 2, 1, 0]
            _assert_released(swapper, seen)
        finally:
            release.set()
            await swapper.shutdown()

    async def test_upstream_error_releases(self, monkeypatch):
        async def proxy(*args, **kwargs):
            return web.json_response({"error": "boom"}, status=500)

        _install(monkeypatch, proxy=proxy)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 500
            assert seen == [1, 0]
            _assert_released(swapper, seen)
        finally:
            await swapper.shutdown()

    async def test_proxy_exception_releases(self, monkeypatch):
        async def proxy(*args, **kwargs):
            raise RuntimeError("boom")

        _install(monkeypatch, proxy=proxy)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        try:
            with pytest.raises(RuntimeError):
                await swapper.handle_request(StubRequest("a"))
            assert seen == [1, 0]
            _assert_released(swapper, seen)
        finally:
            await swapper.shutdown()

    async def test_queue_full_rejection_releases(self, monkeypatch):
        _install(monkeypatch)
        swapper = Swapper(make_config())
        seen = _record_busy(swapper)
        swapper._swapping = True
        loop = asyncio.get_running_loop()
        for _ in range(MAX_QUEUE):
            swapper._queue.append(loop.create_future())
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 503
            assert seen == [1, 0]
            assert swapper._busy_requests == 0
            assert swapper.stats._busy_since is None
        finally:
            swapper._queue.clear()
            swapper._swapping = False
            await swapper.shutdown()

    async def test_model_start_failure_releases(self, monkeypatch):
        async def start(model, cmd, port):
            raise OSError("cannot start")

        _install(monkeypatch, start=start)
        swapper = Swapper(make_config())
        seen = _record_busy(swapper)
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status >= 400
            assert seen == [1, 0]
            _assert_released(swapper, seen)
        finally:
            await swapper.shutdown()

    async def test_cancelled_waiting_request_releases(self, monkeypatch):
        loading = asyncio.Event()
        release = asyncio.Event()

        async def ready(*args, **kwargs):
            loading.set()
            await release.wait()

        _install(monkeypatch, ready=ready)
        swapper = Swapper(make_config())
        seen = _record_busy(swapper)
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(loading.wait(), 1)
            waiter = asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            )
            await eventually(lambda: len(swapper._queue) == 1)
            assert swapper._busy_requests == 2
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert swapper._busy_requests == 1
            assert swapper.stats._busy_since is not None  # owner still busy
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
            assert seen == [1, 2, 1, 0]
            _assert_released(swapper, seen)
        finally:
            release.set()
            await swapper.shutdown()

    async def test_cancelled_during_proxy_releases(self, monkeypatch):
        """Client disconnect cancels the handler while proxying."""
        proxying = asyncio.Event()

        async def proxy(*args, **kwargs):
            proxying.set()
            await asyncio.Event().wait()

        _install(monkeypatch, proxy=proxy)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        task = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(proxying.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert seen == [1, 0]
            _assert_released(swapper, seen)
        finally:
            await swapper.shutdown()

    async def test_no_model_request_to_running_model_counts(self, monkeypatch):
        _install(monkeypatch)
        swapper = Swapper(make_config())
        _seed(swapper)
        seen = _record_busy(swapper)
        try:
            response = await swapper.handle_request(_NoModelRequest())
            assert response.status == 200
            assert seen == [1, 0]
            _assert_released(swapper, seen)
        finally:
            await swapper.shutdown()

    @pytest.mark.parametrize(
        ("request_factory", "status"),
        [
            (lambda: _NoModelRequest(b"{not json"), 400),
            (lambda: _NoModelRequest(b"{}"), 400),
            (lambda: StubRequest("ghost"), 404),
        ],
    )
    async def test_validation_rejections_do_not_count(
        self, monkeypatch, request_factory, status
    ):
        _install(monkeypatch)
        swapper = Swapper(make_config())
        seen = _record_busy(swapper)
        try:
            response = await swapper.handle_request(request_factory())
            assert response.status == status
            assert seen == []
            assert swapper._busy_requests == 0
            util = swapper.stats.snapshot()["utilization"]
            assert util["busy_seconds"] == 0.0
        finally:
            await swapper.shutdown()


# ---------------------------------------------------------------------------
# /stats page: Uptime tile sub-line
# ---------------------------------------------------------------------------


class TestUptimeBusySubLine:
    def test_rerender_switches_between_lines_and_fallback(self):
        output = _execute(
            "render(" + json.dumps(FULL) + ");\n"
            "const busy = snapshotPage();\n"
            "render({});\n"
            "const fallback = snapshotPage();\n"
            "render(" + json.dumps(FULL) + ");\n"
            "console.log(JSON.stringify({busy, fallback, again: snapshotPage()}));\n"
        )
        assert _lines(output["busy"], "kpi-uptime-sub") == [
            "busy 1h 4m", "33% of uptime",
        ]
        assert _text(output["fallback"], "kpi-uptime-sub") == "since start"
        assert output["fallback"]["kpi-uptime-sub"]["children"] == []
        assert _lines(output["again"], "kpi-uptime-sub") == [
            "busy 1h 4m", "33% of uptime",
        ]

    def test_markup_has_id_default_text_and_tooltip(self):
        tiles = STATS_HTML.split('<section class="kpis"', 1)[1].split(
            "</section>", 1
        )[0]
        assert f'<div class="card tile" title="{TIP}">' in tiles
        assert '<div id="kpi-uptime-sub" class="sub">since start</div>' in tiles
        assert tiles.count('class="card tile') == 7

    def test_full_fixture_shows_busy_time_and_percent(self):
        page = _render(FULL)
        assert _text(page, "kpi-uptime") == "3h 12m"
        assert _lines(page, "kpi-uptime-sub") == ["busy 1h 4m", "33% of uptime"]

    def test_fresh_fixture_shows_zero_busy(self):
        page = _render(FRESH)
        assert _lines(page, "kpi-uptime-sub") == ["busy 0s", "0% of uptime"]

    @pytest.mark.parametrize(
        ("utilization", "expected"),
        [
            ({"busy_seconds": 90061, "busy_share": 1.0}, ["busy 1d 1h", "100% of uptime"]),
            ({"busy_seconds": 86399, "busy_share": 1.0}, ["busy 23h 59m", "100% of uptime"]),
            ({"busy_seconds": 125.9, "busy_share": 0.005}, ["busy 2m 5s", "1% of uptime"]),
            ({"busy_seconds": 12, "busy_share": None}, ["busy 12s"]),
            ({"busy_seconds": 12}, ["busy 12s"]),
            ({"busy_seconds": 12, "busy_share": "0.5"}, ["busy 12s"]),
            ({"busy_seconds": 12, "busy_share": 1.5}, ["busy 12s"]),
            ({"busy_seconds": 12, "busy_share": -0.1}, ["busy 12s"]),
        ],
    )
    def test_sub_line_formats(self, utilization, expected):
        page = _render({"uptime_seconds": 100, "utilization": utilization})
        assert _lines(page, "kpi-uptime-sub") == expected
        assert "·" not in _text(page, "kpi-uptime-sub")

    @pytest.mark.parametrize(
        "utilization",
        [
            "__missing__",
            None,
            [],
            "busy",
            {},
            {"busy_seconds": None, "busy_share": 0.5},
            {"busy_seconds": "5", "busy_share": 0.5},
            {"busy_seconds": -1, "busy_share": 0.5},
            {"busy_seconds": float("nan"), "busy_share": 0.5},
            {"busy_seconds": float("inf"), "busy_share": 0.5},
        ],
    )
    def test_missing_or_malformed_falls_back_to_since_start(self, utilization):
        data = {"uptime_seconds": 100}
        if utilization != "__missing__":
            data["utilization"] = utilization
        payload = json.dumps(data, allow_nan=True)
        page = _execute(
            "render(" + payload + ");\n"
            "console.log(JSON.stringify(snapshotPage()));\n"
        )
        assert _text(page, "kpi-uptime-sub") == "since start"
        assert page["kpi-uptime-sub"]["children"] == []
