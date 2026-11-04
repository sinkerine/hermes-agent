"""Re-send exported action content as Alice using a persistent, separate E2EE device."""

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import time

from common import ACTION_KEY, HOME, HS, ROOT, SB, entrypoint, private_write, read_json, request, write_json

sys.path.insert(0, str(ROOT))


def login():
    path = SB / "replay/login.json"
    if path.exists():
        return read_json(path)
    alice = read_json(SB / "accounts.json")["alice"]
    result = request(HS + "/_matrix/client/v3/login", {
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": alice["user_id"]},
        "password": alice["password"], "initial_device_display_name": "Sandbox Alice replay only",
    })
    write_json(path, result)
    return result


async def replay(args, credentials, content):
    import aiohttp
    from mautrix.api import HTTPAPI
    from mautrix.client import Client
    from mautrix.client.state_store import MemoryStateStore
    from mautrix.crypto import OlmMachine
    from mautrix.crypto.store.asyncpg import PgCryptoStore
    from mautrix.errors import MNotFound
    from mautrix.types import EventType, RoomID, TrustState, UserID
    from mautrix.util.async_db import Database
    from plugins.platforms.matrix.adapter_crypto import _CryptoStateStore

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        api = HTTPAPI(base_url=HS, token=credentials["access_token"], client_session=session)
        state = MemoryStateStore()
        client = Client(mxid=UserID("@alice:localhost"), device_id=credentials["device_id"], api=api, state_store=state)
        joined = await client.get_joined_rooms()
        assert args.room in joined, "Alice must join the sandbox room in Element first"
        members = await client.get_joined_members(RoomID(args.room))
        assert "@hermes-bot:localhost" in members
        assert set(members).issubset({"@alice:localhost", "@bob:localhost", "@hermes-bot:localhost"})
        db = Database.create(f"sqlite:///{SB / 'replay/crypto.db'}", upgrade_table=PgCryptoStore.upgrade_table)
        await db.start()
        store = PgCryptoStore(account_id="@alice:localhost", pickle_key=credentials["device_id"], db=db)
        await store.open()
        crypto = OlmMachine(client, store, _CryptoStateStore(state, set(joined), client))
        crypto.share_keys_min_trust = TrustState.UNVERIFIED
        crypto.send_keys_min_trust = TrustState.UNVERIFIED
        try:
            await crypto.load()
            client.crypto = crypto
            await crypto.share_keys()
            sync = await client.sync(timeout=1000, full_state=True)
            tasks = client.handle_sync(sync)
            if tasks:
                await asyncio.gather(*tasks)
            try:
                encryption_info = await client.get_state_event(RoomID(args.room), EventType.ROOM_ENCRYPTION)
            except MNotFound:
                encryption_info = None
            encrypted = encryption_info is not None
            if encrypted:
                await state.set_encryption_info(RoomID(args.room), encryption_info)
            if not encrypted:
                assert args.allow_unencrypted_sibling, "Unencrypted room requires explicit limitation flag"
            event_id = await client.send_message_event(RoomID(args.room), EventType.ROOM_MESSAGE, content)
            receipt = {"event_id": str(event_id), "room_id": args.room, "mode": args.mode,
                       "encrypted": bool(encrypted), "time": time.time(),
                       "limitation": None if encrypted else "Unencrypted sibling; encrypted replay unverified"}
            await asyncio.to_thread(write_json, SB / f"evidence/replay-{args.mode}-{time.time_ns()}.json", receipt)
        finally:
            await db.stop()


def prepare_content(content, room, mode, store_path=None):
    content = deepcopy(content)
    action = content[ACTION_KEY]
    assert content["msgtype"] == "m.text" and action["ui_event_id"].startswith("$")
    assert len(action["nonce"]) >= 22
    if mode == "duplicate":
        return content
    path = store_path or HOME / "platforms/matrix/store/interactive_ui.sqlite3"
    assert path.is_file() and path.resolve().is_relative_to(SB)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        row = connection.execute(
            "SELECT room_id,ui_id,revision,status FROM uis WHERE ui_event_id=?", (action["ui_event_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row and row[0] == room and row[1] == action["ui_id"], "Export must match stored room/event/ui identity"
    revision, status = row[2:]
    assert type(revision) is int and revision > 0, "Probe requires a verified newer revision"
    assert status in ("submitted", "expired", "superseded"), "Probe requires a stored inactive card"
    action["nonce"] = secrets.token_urlsafe(24)
    action["revision"] = revision - 1 if mode == "stale" else revision
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", required=True, help="Disposable room ID only")
    parser.add_argument("--content", type=Path, default=SB / "evidence/action-content.json")
    parser.add_argument("--mode", choices=("duplicate", "stale", "inactive"), default="duplicate")
    parser.add_argument("--allow-unencrypted-sibling", action="store_true")
    args = parser.parse_args()
    assert args.content.resolve().is_relative_to(SB)
    content = prepare_content(read_json(args.content), args.room, args.mode)
    private_write(SB / "replay/.sandbox-owner", str(ROOT))
    asyncio.run(asyncio.wait_for(replay(args, login(), content), timeout=90))
    print("Replay sent as disposable Alice; inspect independent gateway and Matrix evidence.")


if __name__ == "__main__":
    entrypoint(main)
