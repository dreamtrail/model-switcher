from __future__ import annotations

from aiohttp.test_utils import TestClient

from src.control import CONTROL_HEADER
from src.process_manager import ProcessHandle
from tests.quick_switch_helpers import FakeProcess


HEADERS = {CONTROL_HEADER: "1"}


async def put_model(client: TestClient, model: str):
    return await client.put(
        "/api/v1/control/model", json={"model": model}, headers=HEADERS
    )


async def delete_model(client: TestClient):
    return await client.delete("/api/v1/control/model", headers=HEADERS)


def handle(model: str, pid: int = 1) -> ProcessHandle:
    ports = {"a": 1001, "b": 1002, "c": 1003}
    return ProcessHandle(model, ports[model], FakeProcess(pid))
