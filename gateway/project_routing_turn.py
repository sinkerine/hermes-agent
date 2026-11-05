"""Apply durable conversation workspace pins before messaging agents are initialized."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.config import Platform
from gateway.project_routing import (
    PIN_METADATA_KEY, ProjectPin, ProjectRouteError, binding_chat_id, resolve_turn_pin,
)
from gateway.session import SessionEntry, SessionSource
from gateway.session_lifecycle import SessionRouteChangedError

logger = logging.getLogger("gateway.run")


def project_pin_for_key(
    runner: Any, session_key: Optional[str], expected_session_id: Optional[str] = None,
) -> Optional[ProjectPin]:
    if not session_key:
        return None
    return ProjectPin.from_dict(runner.session_store.get_session_metadata(
        session_key, PIN_METADATA_KEY, expected_session_id=expected_session_id,
    ))


async def apply_turn_project_pin(
    runner: Any, source: SessionSource, session_entry: SessionEntry, history: list,
) -> Optional[str]:
    """Resolve and persist a conversation pin off-loop; return a refusal before any model call."""
    if source.platform == Platform.LOCAL:
        return None

    # SessionEntry is mutable (compression can repoint it); the worker owns this exact snapshot.
    session_key, session_id = session_entry.session_key, session_entry.session_id

    def resolve_and_persist() -> Optional[str]:
        with runner._profile_scope_for_source(source):
            try:
                pin, persist = resolve_turn_pin(
                    runner, source, project_pin_for_key(runner, session_key, session_id),
                    history_empty=not history,
                )
            except ProjectRouteError as exc:
                logger.warning("Project route refused: reason=%s platform=%s room=%s",
                               exc.reason, source.platform.value, binding_chat_id(source))
                return exc.user_message()
            if persist:
                if not runner.session_store.set_session_project_pin(
                    session_key, pin.to_dict(), expected_session_id=session_id, clear_terminal_cwd=not history,
                ):
                    raise SessionRouteChangedError("Session route changed during project resolution")
            else:
                project_pin_for_key(runner, session_key, session_id)
            if pin.bound:
                runner.session_store.update_session_row_cwd(session_key, session_id, pin.cwd)
        return None

    return await asyncio.to_thread(resolve_and_persist)


def stamp_turn_project_cwd(runner: Any, session_entry: SessionEntry) -> None:
    """Hygiene may have adopted a compression child since validation stamped the parent."""
    pin = project_pin_for_key(runner, session_entry.session_key, session_entry.session_id)
    if pin and pin.bound:
        runner.session_store.update_session_row_cwd(session_entry.session_key, session_entry.session_id, pin.cwd)
