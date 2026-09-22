from __future__ import annotations

import threading

from tools import secret_gateway


def teardown_function():
    secret_gateway.clear()
    secret_gateway.set_notify_callback(None)


def test_single_pending_capture_and_metadata_only_result():
    seen = []

    def handler(value, redacted):
        seen.append((value, redacted))
        return secret_gateway.SecretCaptureResult(True, "JACKETT_API_KEY", validated=True)

    entry = secret_gateway.register(
        env_var="JACKETT_API_KEY",
        prompt="Jackett API key",
        skill_name="jackett-media-search",
        destination_home="/tmp/entertainment",
        handler=handler,
    )
    assert entry is not None
    assert secret_gateway.register(
        env_var="OTHER_KEY",
        prompt="Other",
        skill_name=None,
        destination_home="/tmp/other",
        handler=handler,
    ) is None

    result_holder = []
    thread = threading.Thread(target=lambda: result_holder.append(secret_gateway.wait(entry)))
    thread.start()
    result = secret_gateway.resolve_with_value(entry.capture_id, "opaque-random-key", redacted=False)
    thread.join(timeout=2)

    assert result.success is True
    assert result_holder[0].stored_as == "JACKETT_API_KEY"
    assert "opaque-random-key" not in repr(result)
    assert seen == [("opaque-random-key", False)]
    assert secret_gateway.get_pending() is None


def test_cancel_unblocks_waiter_without_secret_in_result():
    entry = secret_gateway.register(
        env_var="JACKETT_API_KEY",
        prompt="Jackett API key",
        skill_name=None,
        destination_home="/tmp/entertainment",
        handler=lambda value, redacted: secret_gateway.SecretCaptureResult(True, "JACKETT_API_KEY"),
    )
    assert entry is not None
    result_holder = []
    thread = threading.Thread(target=lambda: result_holder.append(secret_gateway.wait(entry)))
    thread.start()
    assert secret_gateway.cancel(entry.capture_id, "cancelled") is True
    thread.join(timeout=2)
    assert result_holder[0].skipped is True
    assert result_holder[0].error_code == "cancelled"
