"""Loopback Matrix Client-Server API for the real mautrix-backed gateway adapter.

Sync tokens are event sequence positions, so a restarted client can replay history and
incremental long polls only deliver newer events. Unknown routes fail visibly rather
than disguising a missing homeserver operation as success.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from tests.fakes.platforms._standin import Call, StandinServer

BOT_USER_ID = "@hermes:standin.local"


@dataclass
class MatrixCall(Call):
    request_index: int = 0


class _CallLog(list[Call]):
    """Keep the shared stand-in's list interface while supporting calls(method)."""

    def __call__(self, method: str) -> list[Call]:
        return [call for call in self.copy() if call.method == method]


class MatrixStandin(StandinServer):
    def __init__(self, token: str = "matrix-standin-token", bot_user_id: str = BOT_USER_ID) -> None:
        super().__init__()
        self.calls = _CallLog()
        self._request_index = 0
        self.token = token
        self.bot_user_id = bot_user_id
        self._media: dict[str, bytes] = {}
        self._seq = 0
        self._rooms: dict[str, dict[str, Any]] = {}
        self._account_data: dict[tuple[str, str], dict[str, Any]] = {}
        self._transactions: dict[tuple[str, str, str], str] = {}
        self._changed = asyncio.Event()
        self._closing = False

    def checkpoint(self) -> int:
        """Exclude all requests already entered, including unfinished long polls."""
        with self._lock:
            return self._request_index

    def calls_after(self, checkpoint: int) -> list[MatrixCall]:
        with self._lock:
            return [call for call in self.calls
                    if isinstance(call, MatrixCall) and call.request_index > checkpoint]

    def _record_request(self, request_index: int, method: str, params: dict[str, Any],
                        body: Any, *, faulted: bool = False) -> None:
        with self._lock:
            self.calls.append(MatrixCall(method, params, time.monotonic(), body, faulted, request_index))

    def _event(self, room_id: str, sender: str, event_type: str, content: dict[str, Any],
               *, state_key: str | None = None) -> dict[str, Any]:
        self._seq += 1
        event = {"event_id": f"$standin{self._seq}", "room_id": room_id, "sender": sender,
                 "type": event_type, "content": copy.deepcopy(content),
                 "origin_server_ts": int(time.time() * 1000)}
        if state_key is not None:
            event["state_key"] = state_key
        return event

    def _wake(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._changed.set)

    def add_room(self, room_id: str, name: str, members: list[str]) -> None:
        with self._lock:
            state = [self._event(room_id, self.bot_user_id, "m.room.name", {"name": name}, state_key="")]
            state.extend(self._event(room_id, user, "m.room.member",
                                     {"membership": "join", "displayname": self._displayname(user)},
                                     state_key=user) for user in members)
            self._rooms[room_id] = {"state": state, "timeline": [], "revision": self._seq}
        self._wake()

    def _append(self, room_id: str, sender: str, event_type: str, content: dict[str, Any]) -> str:
        with self._lock:
            event = self._event(room_id, sender, event_type, content)
            self._rooms[room_id]["timeline"].append((self._seq, event))
        self._wake()
        return event["event_id"]

    def post(self, room_id: str, sender: str, body: str, *, thread_root: str | None = None) -> str:
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if thread_root is not None:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root,
                                       "is_falling_back": True,
                                       "m.in_reply_to": {"event_id": thread_root}}
        return self.post_content(room_id, sender, content)

    def post_content(self, room_id: str, sender: str, content: dict) -> str:
        return self._append(room_id, sender, "m.room.message", content)

    def put_media(self, data: bytes, *, media_id: str | None = None) -> str:
        with self._lock:
            media_id = media_id if media_id is not None else f"media{len(self._media) + 1}"
            self._media[media_id] = data
        return f"mxc://standin.local/{media_id}"

    def state_events(self, room_id: str, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy([event for event in self._rooms[room_id]["state"]
                                  if event["type"] == event_type])

    def _replace_state(self, room_id: str, sender: str, event_type: str, state_key: str,
                       content: dict[str, Any]) -> str:
        with self._lock:
            room = self._rooms[room_id]
            event = self._event(room_id, sender, event_type, content, state_key=state_key)
            room["state"] = [old for old in room["state"]
                             if (old["type"], old["state_key"]) != (event_type, state_key)]
            room["state"].append(event)
            room["timeline"].append((self._seq, event))
            room["revision"] = self._seq
        self._wake()
        return event["event_id"]

    def set_power_levels(self, room_id: str, users: dict[str, int], state_default: int = 50) -> str:
        return self._replace_state(room_id, self.bot_user_id, "m.room.power_levels", "",
                                   {"users": users, "state_default": state_default})

    def bot_messages(self, room_id: str, *, thread_root: str | None = None) -> list[dict[str, Any]]:
        """Return visible events with final content, preserving the original thread relation."""
        visible: dict[str, dict[str, Any]] = {}
        with self._lock:
            events = copy.deepcopy(self._rooms[room_id]["timeline"])
        for _, event in events:
            if event["sender"] != self.bot_user_id or event["type"] != "m.room.message":
                continue
            content = event["content"]
            relation = content.get("m.relates_to", {})
            if relation.get("rel_type") == "m.replace":
                original = visible.get(relation.get("event_id"))
                if original is not None and isinstance(content.get("m.new_content"), dict):
                    old_relation = original["content"].get("m.relates_to")
                    original["content"] = copy.deepcopy(content["m.new_content"])
                    if old_relation is not None:
                        original["content"]["m.relates_to"] = old_relation
                continue
            visible[event["event_id"]] = event
        return [event for event in visible.values()
                if event["content"].get("m.relates_to", {}).get("event_id") == thread_root
                and (thread_root is None or
                     event["content"].get("m.relates_to", {}).get("rel_type") == "m.thread")]

    @staticmethod
    def _displayname(user_id: str) -> str:
        return user_id.removeprefix("@").split(":", 1)[0]

    def build_app(self) -> web.Application:
        app = web.Application()
        routes = [
            ("GET", "/versions", "versions"),
            ("GET", "/v1/media/download/{server_name}/{media_id}", "media_download"),
            ("GET", "/v3/account/whoami", "whoami"),
            ("POST", "/v3/user/{user_id}/filter", "filter"),
            ("GET", "/v3/sync", "sync"),
            ("PUT", "/v3/rooms/{room_id}/send/{event_type}/{txn}", "send"),
            ("GET", "/v3/rooms/{room_id}/state", "room_state"),
            ("PUT", "/v3/rooms/{room_id}/state/{event_type}/{state_key:.*}", "put_state"),
            ("GET", "/v3/rooms/{room_id}/state/{event_type}", "state"),
            ("GET", "/v3/rooms/{room_id}/state/{event_type}/{state_key:.*}", "state"),
            ("GET", "/v3/rooms/{room_id}/joined_members", "joined_members"),
            ("GET", "/v3/rooms/{room_id}/members", "members"),
            ("PUT", "/v3/rooms/{room_id}/typing/{user_id}", "typing"),
            ("POST", "/v3/rooms/{room_id}/receipt/{receipt_type}/{event_id}", "receipt"),
            ("POST", "/v3/rooms/{room_id}/read_markers", "read_markers"),
            ("PUT", "/v3/presence/{user_id}/status", "presence"),
            ("GET", "/v3/user/{user_id}/account_data/{event_type}", "account_data"),
            ("PUT", "/v3/user/{user_id}/account_data/{event_type}", "account_data"),
            ("GET", "/v3/profile/{user_id}", "profile"),
            ("GET", "/v3/profile/{user_id}/displayname", "profile"),
            ("POST", "/v3/join/{room_id}", "join"),
            ("PUT", "/v3/rooms/{room_id}/redact/{event_id}/{txn}", "redact"),
        ]
        for verb, path, method in routes:
            app.router.add_route(verb, "/_matrix/client" + path, self._handler(method))
        app.router.add_route("*", "/{path:.*}", self._unknown)
        return app

    def _handler(self, method: str):
        async def handle(request: web.Request) -> web.Response:
            with self._lock:
                self._request_index += 1
                request_index = self._request_index
            content = await request.json() if request.can_read_body else {}
            params = {**dict(request.query), **dict(request.match_info), "content": content,
                      "http_method": request.method}
            fault = self.take_fault(method, params)
            if fault is not None:
                self._record_request(request_index, method, params, fault.body, faulted=True)
                return web.json_response(fault.body, status=fault.status)
            if method != "versions" and request.headers.get("Authorization") != f"Bearer {self.token}":
                body = {"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid access token"}
                self._record_request(request_index, method, params, body, faulted=True)
                return web.json_response(body, status=401)
            try:
                body = await getattr(self, "_api_" + method)(params)
            except _MatrixError as exc:
                self._record_request(request_index, method, params, exc.body, faulted=True)
                return web.json_response(exc.body, status=exc.status)
            self._record_request(request_index, method, params, body)
            if method == "media_download":
                return web.Response(body=body, content_type="application/octet-stream")
            return web.json_response(body)
        return handle

    async def _unknown(self, request: web.Request) -> web.Response:
        body = {"errcode": "M_UNRECOGNIZED", "error": "Unrecognized request"}
        self.record(f"{request.method} {request.path}", dict(request.query), body, faulted=True)
        return web.json_response(body, status=404)

    def _room(self, params: dict[str, Any]) -> dict[str, Any]:
        room = self._rooms.get(params["room_id"])
        if room is None:
            raise _MatrixError("M_NOT_FOUND", "Unknown room")
        return room

    async def _api_media_download(self, params: dict[str, Any]) -> bytes:
        with self._lock:
            if params["server_name"] != "standin.local" or params["media_id"] not in self._media:
                raise _MatrixError("M_NOT_FOUND", "Unknown media")
            return self._media[params["media_id"]]

    async def _api_versions(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"versions": ["v1.1", "v1.11"], "unstable_features": {}}

    async def _api_whoami(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"user_id": self.bot_user_id, "device_id": "STANDIN", "is_guest": False}

    async def _api_filter(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"filter_id": "0"}

    async def _api_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        since = int(params.get("since", "0"))
        timeout = max(0, int(params.get("timeout", "0"))) / 1000
        self._changed.clear()
        with self._lock:
            caught_up = since >= self._seq
        if "since" in params and caught_up and not self._closing:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
        with self._lock:
            joined = {}
            for room_id, room in self._rooms.items():
                events = [event for seq, event in room["timeline"] if seq > since]
                full_state = "since" not in params or params.get("full_state") == "true"
                state = room["state"] if full_state or room["revision"] > since else []
                if state or events or full_state:
                    joined[room_id] = {"state": {"events": copy.deepcopy(state)},
                                       "timeline": {"events": copy.deepcopy(events), "limited": False,
                                                    "prev_batch": str(since)},
                                       "ephemeral": {"events": []}, "account_data": {"events": []}}
            return {"next_batch": str(self._seq), "rooms": {"join": joined, "invite": {}, "leave": {}},
                    "account_data": {"events": []}, "to_device": {"events": []},
                    "presence": {"events": []}}

    async def _api_send(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._room(params)
            key = (params["room_id"], params["event_type"], params["txn"])
            if key not in self._transactions:
                self._transactions[key] = self._append(params["room_id"], self.bot_user_id,
                                                        params["event_type"], params["content"])
            return {"event_id": self._transactions[key]}

    async def _api_room_state(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._room(params)["state"])

    async def _api_put_state(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._room(params)["state"]
            levels = next((event["content"] for event in state
                           if event["type"] == "m.room.power_levels"), None)
            create = next((event for event in state if event["type"] == "m.room.create"), {})
            creation = create.get("content", {})
            creator = creation.get("room_version") == "12" and self.bot_user_id in (
                create.get("sender"), *creation.get("additional_creators", []))
            if levels is not None and not creator:
                required = levels.get("events", {}).get(params["event_type"],
                                                        levels.get("state_default", 50))
                have = levels.get("users", {}).get(self.bot_user_id, levels.get("users_default", 0))
                if int(have) < int(required):
                    raise _MatrixError("M_FORBIDDEN", "Insufficient power level", 403)
            event_id = self._replace_state(params["room_id"], self.bot_user_id,
                                           params["event_type"], params["state_key"], params["content"])
            return {"event_id": event_id}

    async def _api_state(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for event in self._room(params)["state"]:
                if event["type"] == params["event_type"] and event["state_key"] == params.get("state_key", ""):
                    return copy.deepcopy(event["content"])
        raise _MatrixError("M_NOT_FOUND", "State event not found")

    async def _api_members(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return {"chunk": copy.deepcopy([event for event in self._room(params)["state"]
                                            if event["type"] == "m.room.member"])}

    async def _api_joined_members(self, params: dict[str, Any]) -> dict[str, Any]:
        members = (await self._api_members(params))["chunk"]
        return {"joined": {event["state_key"]: {"display_name": event["content"]["displayname"],
                                               "avatar_url": None}
                           for event in members if event["content"]["membership"] == "join"}}

    async def _api_account_data(self, params: dict[str, Any]) -> dict[str, Any]:
        key = (params["user_id"], params["event_type"])
        with self._lock:
            if params["http_method"] == "PUT":
                self._account_data[key] = copy.deepcopy(params["content"])
                return {}
            if key not in self._account_data:
                raise _MatrixError("M_NOT_FOUND", "Account data not found")
            return copy.deepcopy(self._account_data[key])

    async def _api_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"displayname": self._displayname(params["user_id"])}

    async def _api_join(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            room = self._room(params)
            if not any(event.get("state_key") == self.bot_user_id for event in room["state"]):
                room["state"].append(self._event(params["room_id"], self.bot_user_id, "m.room.member",
                                                {"membership": "join", "displayname": "hermes"},
                                                state_key=self.bot_user_id))
                room["revision"] = self._seq
        self._wake()
        return {"room_id": params["room_id"]}

    async def _api_redact(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"event_id": self._append(params["room_id"], self.bot_user_id, "m.room.redaction",
                                         {"redacts": params["event_id"], **params["content"]})}

    async def _api_ok(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {}

    _api_typing = _api_receipt = _api_read_markers = _api_presence = _api_ok

    async def on_shutdown(self) -> None:
        self._closing = True
        self._changed.set()


class _MatrixError(Exception):
    def __init__(self, errcode: str, error: str, status: int = 404) -> None:
        super().__init__(error)
        self.body = {"errcode": errcode, "error": error}
        self.status = status
