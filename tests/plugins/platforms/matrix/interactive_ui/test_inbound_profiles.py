"""Profile, per-user and thread ownership cannot be inferred from a click."""

import pytest

from .test_inbound import BOB, ROOM, ROOT, Harness
from .test_inbound_recovery import missing_record


@pytest.mark.asyncio
async def test_separate_profiles_cannot_recover_or_act_on_other_card(tmp_path):
    first = Harness(tmp_path / "first.sqlite3", profile="first")
    second = Harness(tmp_path / "second.sqlite3", profile="second")
    try:
        await first.present()
        second.card = first.card
        second.adapter._client.get_event.return_value = first.original
        await second.act()
        second.notice("unknown")
        second.adapter._client.get_event.assert_not_awaited()
        await first.act()
        assert len(first.turns) == 1
        assert first.turns[0].source.profile == "first"
        assert first.store.get_by_event(first.card["event_id"]).session_key.startswith("agent:first:")
    finally:
        first.store.close()
        second.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"thread": "$other"}, {"sender": BOB}, {"room": "!other:example.org"}])
async def test_stored_record_scope_refuses_thread_user_room(tmp_path, override):
    harness = Harness(tmp_path / "ui.sqlite3", extra={"thread_sessions_per_user": True})
    try:
        await harness.present()
        await harness.act(**override)
        harness.notice("unknown" if "room" in override else "other_conversation",
                       room=override.get("room", ROOM), thread=override.get("thread", ROOT))
    finally:
        harness.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["profile", "user", "thread", "unthreaded_user"])
async def test_recovery_never_assigns_clickers_session_or_current_route(tmp_path, change):
    harness = Harness(tmp_path / "ui.sqlite3", profile="original", extra={"thread_sessions_per_user": True})
    try:
        if change == "unthreaded_user":
            await harness.present(thread=None)
            harness.store._db.execute("DELETE FROM uis")
            await harness.act(sender=BOB, thread=None)
        else:
            await missing_record(harness)
            if change == "profile":
                harness.adapter._owner_profile = "changed"
            await harness.act(sender=BOB if change == "user" else "@alice:example.org",
                              thread="$other" if change == "thread" else ROOT)
        harness.notice("unknown", thread=None if change == "unthreaded_user" else "$other" if change == "thread" else ROOT)
        harness.adapter._client.get_event.assert_not_awaited()
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_shared_room_policy_allows_same_session_and_thread_preserved(tmp_path):
    harness = Harness(tmp_path / "ui.sqlite3", extra={"group_sessions_per_user": False,
                                                     "thread_sessions_per_user": False})
    try:
        await harness.present()
        await harness.act(sender=BOB)
        assert len(harness.turns) == 1
        assert harness.turns[0].source.user_id == BOB and harness.turns[0].source.thread_id == ROOT
        assert harness.turns[0].source.chat_id == ROOM
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_canonical_identity_pinned_before_key_and_preserved_for_callback(tmp_path):
    from types import SimpleNamespace
    from gateway.session_identity import identity_of
    harness = Harness(tmp_path / "ui.sqlite3")
    adapter = harness.adapter
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True), _primary_profile_name="default",
        _transport_owner=lambda source: (adapter, None),
        _owning_profile=lambda candidate, platform: (adapter, None),
        _profile_name_for_source=lambda source, **kwargs: "routed",
        _resolve_profile_home_for_source=lambda source: tmp_path / "routed")
    try:
        await harness.present()
        await harness.act()
        assert len(harness.turns) == 1
        source = harness.turns[0].source
        identity = identity_of(source)
        assert identity.runtime_profile == "routed" and identity.transport_profile == "default"
        assert identity.adapter() is adapter
        assert identity.runtime_home == tmp_path / "routed"
        assert harness.store.get_by_event(harness.card["event_id"]).session_key.startswith("agent:routed:")
    finally:
        harness.store.close()


@pytest.fixture
def routed_gateway(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import secret_scope
    from gateway.config import GatewayConfig, Platform
    from gateway.pairing import PairingStore
    from gateway.profile_routing import ProfileRoute
    from gateway.run import GatewayRunner
    from agent.i18n import reset_language_cache

    home = tmp_path / "home"
    routed = home / "profiles" / "routed"
    routed.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS", "MATRIX_ALLOWED_USERS", "HERMES_LANGUAGE"):
        monkeypatch.delenv(key, raising=False)
    (home / ".env").write_text("", encoding="utf-8")
    (routed / ".env").write_text("", encoding="utf-8")
    (home / "config.yaml").write_text("display:\n  language: en\n", encoding="utf-8")
    (routed / "config.yaml").write_text("display:\n  language: zh\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: home if name == "default" else routed)
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda *a, **kw: [("default", home), ("routed", routed)])
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = [ProfileRoute(name="thread", platform="matrix", chat_id=ROOM,
                                                thread_id=ROOT, profile="routed")]
    runner._primary_profile_name = "default"
    runner._profile_adapters = {"routed": {}}
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {"default": runner.pairing_store, "routed": PairingStore(profile="routed")}
    harness = Harness(tmp_path / "ui.sqlite3", extra={"allowed_users": []})
    runner.adapters = {Platform.MATRIX: harness.adapter}
    runner.config.platforms = {Platform.MATRIX: harness.adapter.config}
    harness.adapter.gateway_runner = runner
    harness.adapter.set_authorization_check(runner._make_adapter_auth_check(Platform.MATRIX))
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    reset_language_cache()
    yield harness, runner, home, routed
    reset_language_cache()
    secret_scope.set_multiplex_active(previous)
    harness.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", ["default", "routed"])
@pytest.mark.parametrize("callback_only", [False, True])
async def test_canonical_thread_authorization_before_side_effects(routed_gateway, approved, callback_only):
    from unittest.mock import AsyncMock, Mock
    from gateway.session_identity import identity_of
    from .test_inbound import ALICE

    harness, runner, _home, routed = routed_gateway
    runner.pairing_stores[approved]._save_json(runner.pairing_stores[approved]._approved_path("matrix"), {ALICE: {}})
    await harness.present()
    harness.adapter._threads.mark_async = AsyncMock()
    harness.adapter._background_read_receipt = Mock()
    if callback_only:
        del harness.adapter.gateway_runner
        # The producer's canonical owner stays routed; only the callback must reconstruct the route.
        harness.adapter._owner_profile = "routed"
    await harness.act()
    if approved == "default":
        assert not harness.order
        assert harness.store._db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 0
        harness.adapter._threads.mark_async.assert_not_awaited()
        harness.adapter._background_read_receipt.assert_not_called()
    else:
        assert harness.order == ["edit", "turn"]
        assert harness.turns[0].source.profile == "routed"
        if not callback_only:
            assert identity_of(harness.turns[0].source).runtime_home == routed
        harness.adapter._threads.mark_async.assert_awaited_once_with(ROOT)
        harness.adapter._background_read_receipt.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True])
async def test_routed_confirmation_and_notices_use_runtime_locale(routed_gateway, overlay):
    from .test_inbound import ALICE, UI
    from agent.i18n import t
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home

    harness, runner, home, routed = routed_gateway
    runner.pairing_stores["routed"]._save_json(runner.pairing_stores["routed"]._approved_path("matrix"), {ALICE: {}})
    if overlay:
        (routed / "locales").mkdir()
        (routed / "locales" / "zh.yaml").write_text(
            "platform:\n  matrix:\n    interactive_ui:\n      submitted_status: 专属确认\n      invalid: 专属无效\n", encoding="utf-8")
    await harness.present()
    pinned = harness.store._path
    with _profile_runtime_scope(routed, hydrate_secrets=False):
        confirmation = t("platform.matrix.interactive_ui.submitted_status")
        invalid = t("platform.matrix.interactive_ui.invalid")
    with _profile_runtime_scope(home, hydrate_secrets=False):
        assert confirmation != t("platform.matrix.interactive_ui.submitted_status")
        await harness.act(action={"nonce": "short"})
        assert harness.sent[0][1]["body"] == invalid
        harness.reset()
        await harness.act(event_id="$valid")
        assert harness.sent[0][1]["m.new_content"][UI]["status_text"] == confirmation
        assert (routed / "matrix_threads.json").is_file()
        assert not (home / "matrix_threads.json").exists()
        assert get_hermes_home() == home
    assert harness.order == ["edit", "turn"]
    assert harness.store._path == pinned
