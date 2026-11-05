"""Matrix stand-in configuration for GatewayUnderTest's disposable HOME.

The real adapter uses its configured homeserver URL directly; no SDK shim or
adapter overrides are needed. Rooms have three members to exercise group routing.

Consumers (including T5's restart journey) must call arm_readiness() before EVERY
GatewayUnderTest.start(), including the initial launch. Keep the stand-in and driver
alive across restarts: arming preserves their complete call history for diagnostics.
"""

from __future__ import annotations

from typing import Any

from tests.fakes.platforms.matrix_standin import BOT_USER_ID, MatrixStandin


class MatrixDriver:
    name = "matrix"
    bot_id = BOT_USER_ID
    user_id = "@admin:standin.local"
    other_user_id = "@guest:standin.local"
    room_id = "!alpha:standin.local"
    other_room_id = "!beta:standin.local"

    def __init__(self) -> None:
        self.standin = MatrixStandin()
        self._readiness_checkpoint: int | None = None
        members = [self.bot_id, self.user_id, self.other_user_id]
        self.standin.add_room(self.room_id, "Alpha", members)
        self.standin.add_room(self.other_room_id, "Beta", members)

    def start(self) -> None:
        self.standin.start()

    def stop(self) -> None:
        self.standin.stop()

    def gateway_config(self) -> dict[str, Any]:
        return {"platforms": {"matrix": {"enabled": True, "extra": {
            "homeserver": self.standin.base_url,
            "group_allow_admin_from": [self.user_id],
        }}}, "terminal": {"backend": "local"}}

    def gateway_env(self) -> dict[str, str]:
        return {
            "MATRIX_HOMESERVER": self.standin.base_url,
            "MATRIX_ACCESS_TOKEN": self.standin.token,
            "MATRIX_USER_ID": self.bot_id,
            "MATRIX_ALLOWED_USERS": ",".join((self.user_id, self.other_user_id)),
            "MATRIX_E2EE_MODE": "off",
            "MATRIX_REQUIRE_MENTION": "false",
            "MATRIX_SESSION_SCOPE": "room",
            "MATRIX_AUTO_THREAD": "false",
            "HERMES_MATRIX_TEXT_BATCH_DELAY_SECONDS": "0",
            "HERMES_MATRIX_TEXT_BATCH_SPLIT_DELAY_SECONDS": "0",
        }

    def arm_readiness(self) -> None:
        """Require fresh successful authentication and incremental sync for the next launch."""
        self._readiness_checkpoint = self.standin.checkpoint()

    def connected(self) -> bool:
        if self._readiness_checkpoint is None:
            return False
        calls = [call for call in self.standin.calls_after(self._readiness_checkpoint)
                 if not call.faulted]
        # Request indices exclude old polls even when they finish after arming.
        authenticated = [call.request_index for call in calls if call.method == "whoami"]
        return bool(authenticated and any(
            call.method == "sync" and "since" in call.params
            and call.request_index > min(authenticated) for call in calls))

    def describe(self) -> str:
        return self.standin.describe()
