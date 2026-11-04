"""Recovery uses authenticated room events and immutable durable ownership receipts."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from .test_inbound import ALICE, BOT, ROOM, ROOT, Harness, UI


def wire(event_id, content, sender=BOT, room=ROOM):
    return {"type": "m.room.message", "room_id": room, "event_id": event_id, "sender": sender,
            "origin_server_ts": 1000, "content": deepcopy(content)}


def encrypted(raw):
    return {**raw, "type": "m.room.encrypted", "content": {
        "algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "ciphertext", "session_id": "session",
        "sender_key": "key", "device_id": "device"}}


async def missing_record(harness):
    await harness.present()
    original = wire(harness.card["event_id"], harness.original)
    # Retain the immutable receipt while simulating lost/pruned mutable state.
    harness.store._db.execute("DELETE FROM uis")
    harness.adapter._client.get_event.return_value = original
    harness.adapter._client.api.request.return_value = {"chunk": []}
    return original


@pytest.fixture
def recovery(tmp_path):
    harness = Harness(tmp_path / "ui.sqlite3")
    yield harness
    harness.store.close()


@pytest.mark.asyncio
async def test_missing_record_encrypted_original_latest_edit_paginated_forgery_ignored(recovery):
    original = await missing_record(recovery)
    latest = deepcopy(recovery.original)
    latest.pop("m.relates_to")
    latest[UI]["revision"] = 1
    latest[UI]["title"] = "Bot-authored revision"
    edit_content = {"msgtype": "m.text", "body": "* revised", "m.new_content": latest,
                    "m.relates_to": {"rel_type": "m.replace", "event_id": original["event_id"]}}
    edit = wire("$edit", edit_content)
    forged = wire("$forged", deepcopy(edit_content), sender=ALICE)
    forged["content"]["m.new_content"][UI]["revision"] = 99
    recovery.adapter._client.get_event.return_value = encrypted(original)
    decrypted = {original["event_id"]: original, "$edit": edit}
    recovery.adapter._client.crypto = type("Crypto", (), {})()
    recovery.adapter._client.crypto.decrypt_megolm_event = AsyncMock(
        side_effect=lambda event: decrypted[str(event.event_id)])
    recovery.adapter._client.api.request.side_effect = [
        {"chunk": [forged], "next_batch": "page2"}, {"chunk": [encrypted(edit)]}]
    await recovery.act(action={"revision": 1})
    assert recovery.order == ["edit", "turn"]
    assert "Bot-authored revision" in recovery.turns[0].text
    record = recovery.store.get_by_event(original["event_id"])
    assert record.recovered and record.revision == 2
    calls = recovery.adapter._client.api.request.await_args_list
    assert str(calls[0].args[0]) == "GET"
    assert str(calls[0].args[1]) == f"_matrix/client/v1/rooms/%21room%3Aexample.org/relations/{original['event_id'].replace('$', '%24')}/m.replace"
    assert calls[0].kwargs["query_params"] == {"limit": 50, "dir": "b"}
    assert calls[1].kwargs["query_params"] == {"limit": 49, "dir": "b", "from": "page2"}
    assert recovery.adapter._client.crypto.decrypt_megolm_event.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["nonbot", "edit_id", "crossroom", "redacted", "thread", "ui_id", "no_receipt"])
async def test_reconstruction_spoofs_refused(recovery, bad):
    raw = await missing_record(recovery)
    changes = {
        "nonbot": lambda: raw.update(sender=ALICE),
        "edit_id": lambda: raw["content"].update({"m.relates_to": {"rel_type": "m.replace", "event_id": "$other"}}),
        "crossroom": lambda: raw.update(room_id="!other:example.org"),
        "redacted": lambda: raw.update(unsigned={"redacted_because": {"event_id": "$redaction"}}),
        "thread": lambda: raw["content"]["m.relates_to"].update(event_id="$other"),
        "ui_id": lambda: raw["content"][UI].update(ui_id="other"),
        "no_receipt": lambda: recovery.store._db.execute("DELETE FROM ownership"),
    }
    changes[bad]()
    recovery.adapter._client.get_event.return_value = raw
    await recovery.act()
    recovery.notice("unknown")
    assert recovery.store.get_by_event(recovery.card["event_id"]) is None
    if bad == "no_receipt":
        recovery.adapter._client.get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_relations_at_fifty_refuses_unknown_latest(recovery):
    original = await missing_record(recovery)
    # Even non-bot edits count towards the explicit reconstruction bound.
    forged = wire("$forged", {}, sender=ALICE)
    recovery.adapter._client.api.request.return_value = {"chunk": [forged] * 50, "next_batch": "more"}
    await recovery.act()
    recovery.notice("unknown")
    assert recovery.store.get_by_event(original["event_id"]) is None


@pytest.mark.asyncio
async def test_unreadable_bot_edit_refuses_recovery(recovery):
    await missing_record(recovery)
    recovery.adapter._client.api.request.return_value = {"chunk": [encrypted(wire("$edit", {}))]}
    await recovery.act()
    recovery.notice("unknown")


@pytest.mark.asyncio
async def test_restart_durable_duplicate_silent(tmp_path):
    path = tmp_path / "ui.sqlite3"
    first = Harness(path)
    await first.present()
    await first.act()
    card = first.card
    first.store.close()
    second = Harness(path)
    try:
        second.card = card
        await second.act(event_id="$retry")
        await second.act(event_id="$action", nonce="b" * 22)
        assert not second.order
        second.adapter._client.get_event.assert_not_awaited()
    finally:
        second.store.close()


@pytest.mark.asyncio
async def test_receipt_is_immutable_recovery_requires_it_and_failed_insert_rolls_back(recovery):
    await recovery.present()
    record = recovery.store.get_by_event(recovery.card["event_id"])
    receipt = recovery.store.get_ownership(record.ui_event_id)
    recovery.store._db.execute("DELETE FROM uis")
    record.session_key = "attacker"
    with pytest.raises(ValueError, match="immutable"):
        recovery.store.insert(record)
    assert recovery.store.get_ownership(record.ui_event_id) == receipt
    assert recovery.store.get_by_event(record.ui_event_id) is None
    record.ui_event_id = "$unknown"
    record.recovered = True
    with pytest.raises(ValueError, match="durable ownership"):
        recovery.store.insert(record)
    assert recovery.store.get_ownership("$unknown") is None
    record.recovered = False
    record.session_key = receipt.session_key
    record.ui_event_id = receipt.ui_event_id
    recovery.store.insert(record)
    other = deepcopy(record)
    other.ui_event_id = "$new"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        recovery.store.insert(other)  # ui_id uniqueness fails after receipt insertion
    assert recovery.store.get_ownership("$new") is None


@pytest.mark.asyncio
async def test_receipts_survive_prune_and_existing_rows_backfill_on_reopen(recovery):
    await recovery.present()
    event_id = recovery.card["event_id"]
    path = recovery.store._path
    recovery.store._db.execute("DELETE FROM ownership")
    recovery.store.close()
    from plugins.platforms.matrix.interactive_ui.store import InteractiveUiStore
    recovery.store = InteractiveUiStore(path)
    assert recovery.store.get_ownership(event_id).thread_id == ROOT
    recovery.store._db.execute("UPDATE uis SET updated_at = 0")
    assert recovery.store.prune() == 1
    assert recovery.store.get_by_event(event_id) is None
    assert recovery.store.get_ownership(event_id) is not None


@pytest.mark.asyncio
async def test_repeated_pagination_token_refuses_partial_history(recovery):
    await missing_record(recovery)
    recovery.adapter._client.api.request.return_value = {"chunk": [], "next_batch": "same"}
    await recovery.act()
    recovery.notice("unknown")
    assert recovery.adapter._client.api.request.await_count == 2


@pytest.mark.asyncio
async def test_unthreaded_recovery_with_original_receipt_is_safe(recovery):
    await recovery.present(thread=None)
    original = wire(recovery.card["event_id"], recovery.original)
    recovery.store._db.execute("DELETE FROM uis")
    recovery.adapter._client.get_event.return_value = original
    recovery.adapter._client.api.request.return_value = {"chunk": []}
    await recovery.act(thread=None)
    assert len(recovery.turns) == 1
    assert recovery.turns[0].source.thread_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["delete", "prune"])
async def test_repeatable_replay_history_survives_loss_and_restart(tmp_path, loss):
    path = tmp_path / "ui.sqlite3"
    first = Harness(path)
    await first.present(single_use=False)
    original = wire(first.card["event_id"], first.original)
    await first.act()
    assert len(first.turns) == 1
    if loss == "delete":
        first.store._db.execute("DELETE FROM uis")
    else:
        first.store._db.execute("UPDATE uis SET updated_at = 0")
        assert first.store.prune() == 1
    second = Harness(path)
    second.card = first.card
    first.store.close()
    try:
        second.adapter._client.get_event.return_value = original
        second.adapter._client.api.request.return_value = {"chunk": []}
        await second.act(event_id="$retry")
        await second.act(event_id="$action", nonce="b" * 22)
        assert not second.order
        second.adapter._client.get_event.assert_not_awaited()
        await second.act(event_id="$legitimate", nonce="c" * 22)
        assert second.order == ["turn"]
        assert second.store.get_by_event(second.card["event_id"]).recovered
    finally:
        second.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["delete", "prune"])
async def test_consumed_card_with_both_edits_failed_cannot_recover_old_active_state(tmp_path, loss):
    path = tmp_path / "ui.sqlite3"
    first = Harness(path)
    await first.present()
    original = wire(first.card["event_id"], first.original)
    first.adapter._client.send_message_event.side_effect = OSError("transport unavailable")
    await first.act()
    assert len(first.turns) == 1
    assert first.adapter._client.send_message_event.await_count == 2
    consumed = first.store.get_by_event(first.card["event_id"])
    assert (consumed.revision, consumed.status) == (1, "submitted")
    if loss == "delete":
        first.store._db.execute("DELETE FROM uis")
    else:
        first.store._db.execute("UPDATE uis SET updated_at = 0")
        assert first.store.prune() == 1
    first.store.close()
    second = Harness(path)
    second.card = first.card
    try:
        second.adapter._client.get_event.return_value = original
        second.adapter._client.api.request.return_value = {"chunk": []}
        await second.act(event_id="$retry")
        await second.act(event_id="$action", nonce="b" * 22)
        assert not second.order
        second.adapter._client.get_event.assert_not_awaited()
        await second.act(event_id="$fresh", nonce="c" * 22)
        second.notice("unknown")
        assert second.store.get_by_event(second.card["event_id"]) is None
        assert second.store._db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 1
    finally:
        second.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "expired", "superseded"])
async def test_model_revision_watermark_refuses_room_rollback_after_restart(tmp_path, status):
    path = tmp_path / "ui.sqlite3"
    first = Harness(path)
    await first.present(single_use=False)
    original = wire(first.card["event_id"], first.original)
    result = await first.controller.present(
        chat_id=ROOM, thread_metadata={"thread_id": ROOT},
        session_key=first.adapter._source_session_key(first.source()),
        ui=first.original[UI], replace_ui_id=first.card["ui_id"], status=status)
    assert result["ok"] and result["revision"] == 1
    first.store._db.execute("DELETE FROM uis")
    first.store.close()
    second = Harness(path)
    second.card = first.card
    try:
        second.adapter._client.get_event.return_value = original
        second.adapter._client.api.request.return_value = {"chunk": []}
        await second.act()
        second.notice("unknown")
        assert second.store.get_by_event(second.card["event_id"]) is None
    finally:
        second.store.close()
