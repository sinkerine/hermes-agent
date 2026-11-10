"""Edit streaming respects both configured pacing and newly buffered text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import StreamingConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig, _Tick


@pytest.fixture
def paced_stream(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("gateway.stream_consumer.time.monotonic", lambda: clock.now)
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview")),
        edit_message=AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview")),
    )

    def make(interval, threshold):
        settings = StreamingConfig.from_dict({
            "enabled": True, "edit_interval": interval, "buffer_threshold": threshold,
        })
        consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(
            edit_interval=settings.edit_interval, buffer_threshold=settings.buffer_threshold, cursor="",
        ))
        return consumer

    return clock, adapter, make


@pytest.mark.asyncio
@pytest.mark.parametrize(("interval", "threshold"), [(0.8, 24), (2.0, 64)])
async def test_large_buffer_cannot_bypass_configured_edit_interval(paced_stream, interval, threshold):
    clock, adapter, make = paced_stream
    consumer = make(interval, threshold)
    consumer._append_accumulated("a" * threshold)
    assert consumer._should_edit(_Tick())
    await consumer._push_update(_Tick())

    consumer._append_accumulated("b" * threshold * 3)
    clock.now += interval / 2
    assert not consumer._should_edit(_Tick())
    clock.now += interval / 2 + 0.01
    assert consumer._should_edit(_Tick())
    await consumer._push_update(_Tick())
    adapter.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_new_text_counts_toward_buffer_and_final_flushes_short_tail(paced_stream):
    clock, adapter, make = paced_stream
    consumer = make(1.0, 32)
    consumer._append_accumulated("a" * 64)
    await consumer._push_update(_Tick())

    clock.now += 2
    consumer._append_accumulated("b")
    assert not consumer._should_edit(_Tick())
    consumer._append_accumulated("c" * 31)
    assert consumer._should_edit(_Tick())
    await consumer._push_update(_Tick())
    assert not consumer._should_edit(_Tick())

    consumer._append_accumulated("tail")
    final = _Tick(got_done=True)
    assert consumer._should_edit(final)
    await consumer._push_update(final)
    assert adapter.edit_message.await_args.kwargs["content"].endswith("tail")
    assert adapter.edit_message.await_args.kwargs["finalize"] is True
