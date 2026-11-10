"""Real mautrix HTTP delivery retries Matrix 429s without duplicating messages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiohttp import web
from mautrix.api import HTTPAPI
from mautrix.client import Client

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.matrix import adapter as matrix
from plugins.platforms.matrix.adapter_transport import MatrixRateLimitMiddleware, _retry_delay
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def matrix_transport():
    calls = []
    control = SimpleNamespace(reject_edits=0, reject_sends=0, status=429, delay_ms=1)

    async def send(request):
        content = await request.json()
        calls.append((request.path, content))
        field = "reject_edits" if "m.new_content" in content else "reject_sends"
        if getattr(control, field):
            setattr(control, field, getattr(control, field) - 1)
            return web.json_response({"errcode": "M_LIMIT_EXCEEDED", "error": "Too Many Requests",
                                      "retry_after_ms": control.delay_ms}, status=control.status)
        return web.json_response({"event_id": "$edit" if field == "reject_edits" else "$preview"})

    app = web.Application()
    app.router.add_put("/_matrix/client/v3/rooms/{room}/send/{kind}/{txn}", send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    session = matrix.adapter_transport.create_matrix_session(None)
    adapter = matrix.MatrixAdapter(PlatformConfig(token="synthetic-token", extra={
        "homeserver": url, "user_id": "@bot:example.invalid", "e2ee_mode": "off",
    }))
    adapter._client = Client(mxid="@bot:example.invalid", api=HTTPAPI(base_url=url, client_session=session))
    try:
        yield adapter, calls, control
    finally:
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_initial_send_retries_same_transaction(matrix_transport):
    adapter, calls, control = matrix_transport
    control.reject_sends = 1
    result = await adapter.send("!room:example.invalid", "A complete answer")
    assert result.success
    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_final_edit_429_keeps_one_streamed_message(matrix_transport):
    adapter, calls, control = matrix_transport
    consumer = GatewayStreamConsumer(adapter, "!room:example.invalid", StreamConsumerConfig(
        edit_interval=0.1, buffer_threshold=8, cursor="",
    ))
    consumer.on_delta("The beginning of the answer.")
    task = asyncio.create_task(consumer.run())
    try:
        await async_wait_until(lambda: consumer.already_sent, timeout=3)
        control.reject_edits = 1
        consumer.on_delta(" And the completed ending.")
        consumer.finish()
        await asyncio.wait_for(task, timeout=3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert consumer.final_content_delivered
    assert len(calls) == 3
    assert calls[1] == calls[2]
    assert calls[1][1]["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$preview"}
    assert sum("m.new_content" not in content for _, content in calls) == 1


@pytest.mark.asyncio
async def test_exhaustion_is_bounded_and_preserves_retry_delay(matrix_transport):
    adapter, calls, control = matrix_transport
    control.reject_edits = 100
    result = await adapter.edit_message("!room:example.invalid", "$preview", "final answer", finalize=True)
    assert not result.success
    assert len(calls) == 4
    assert result.retry_after == pytest.approx(0.001)
    consumer = GatewayStreamConsumer(adapter, "chat")
    assert consumer._is_flood_error(result)


@pytest.mark.asyncio
async def test_long_cooldown_is_preserved_without_retrying_early(matrix_transport):
    adapter, calls, control = matrix_transport
    control.reject_edits = 1
    control.delay_ms = 60_000
    first = await adapter.edit_message("!room:example.invalid", "$preview", "answer")
    second = await adapter.edit_message("!another:example.invalid", "$other", "another answer")
    assert not first.success and not second.success
    assert first.retry_after == 60.0
    assert second.retry_after > 59.0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_other_http_errors_are_not_retried(matrix_transport):
    adapter, calls, control = matrix_transport
    control.reject_edits = 1
    control.status = 403
    result = await adapter.edit_message("!room:example.invalid", "$preview", "answer")
    assert not result.success
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "body", "expected"), [
    ({"Retry-After": "2"}, {"retry_after_ms": 3000}, 3),
    ({"Retry-After": "4"}, {"retry_after_ms": 500}, 4),
    ({}, {"retry_after_ms": "invalid"}, 1),
    ({"Retry-After": "NaN"}, {"retry_after_ms": float("inf")}, 1),
])
async def test_server_retry_delay_is_respected(headers, body, expected):
    response = SimpleNamespace(headers=headers, json=AsyncMock(return_value=body))
    assert await _retry_delay(response, 0) == expected


@pytest.mark.asyncio
async def test_cooldown_is_shared_by_writes_but_does_not_block_sync():
    clock = SimpleNamespace(now=0.0)
    waits = []

    async def sleep(delay):
        waits.append(delay)
        clock.now += delay

    middleware = MatrixRateLimitMiddleware(clock=lambda: clock.now, sleep=sleep)
    middleware._retry_at = 2.0
    handler = AsyncMock(return_value=SimpleNamespace(status=200))
    sync = SimpleNamespace(method="GET", url=SimpleNamespace(path="/_matrix/client/v3/sync"))
    write = SimpleNamespace(method="PUT", url=SimpleNamespace(path="/_matrix/client/v3/rooms/r/send/m.text/t"))
    await middleware(sync, handler)
    assert waits == []
    await middleware(write, handler)
    assert waits == [2.0]


@pytest.mark.asyncio
async def test_cancel_during_backoff_does_not_retry():
    sleeping = asyncio.Event()
    never = asyncio.Event()

    async def sleep(delay):
        sleeping.set()
        await never.wait()

    middleware = MatrixRateLimitMiddleware(sleep=sleep)
    response = SimpleNamespace(status=429, headers={}, release=Mock(),
                               json=AsyncMock(return_value={"retry_after_ms": 1000}))
    handler = AsyncMock(return_value=response)
    request = SimpleNamespace(method="PUT", url=SimpleNamespace(path="/_matrix/client/v3/rooms/r/send/m.text/t"))
    task = asyncio.create_task(middleware(request, handler))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    handler.assert_awaited_once()
    response.release.assert_called_once()
