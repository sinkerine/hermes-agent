from tools.request_secret_capture import request_secret_capture


def test_arbitrary_destination_does_not_require_skill_metadata():
    seen = []

    result = request_secret_capture(
        "SMB_ARCHIVE_PASSWORD",
        "SMB archive password",
        secret_capture_callback=lambda name, prompt, metadata: (
            seen.append((name, prompt, metadata))
            or {"success": True, "stored_as": name, "validated": True, "value": "must-not-return"}
        ),
    )

    assert '"success": true' in result
    assert "must-not-return" not in result
    assert seen[0][0] == "SMB_ARCHIVE_PASSWORD"
    assert seen[0][2]["requested_by"] == "request_secret_capture"


def test_invalid_destination_name_is_rejected_without_callback():
    called = []
    result = request_secret_capture("PATH=bad", secret_capture_callback=lambda *args: called.append(args))
    assert '"error_code": "invalid_env_name"' in result
    assert called == []
