"""HTTP routes owned by the Builder Chat module."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from builder_chat.service import (
    BuilderChatError,
    handle_builder_chat,
    list_builder_chat_state,
    open_chat_session_for_selection,
    select_chat_session,
)
from builder_chat.session import (
    resolve_session_for_builder_chat,
    workflow_plan_node_count,
)
from workflow_builder.workflow_builder_standalone import save_session

builder_chat_router = APIRouter(tags=["builder-chat"])


def _http_from_builder_error(exc: BuilderChatError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _load_launchpad_session(session_id: str) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    sid = session_id.strip()
    if not sid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_session_id",
                "message": "A non-empty session id is required.",
            },
        )

    session, workflow = resolve_session_for_builder_chat(sid)
    if session is None and workflow is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "session_not_found",
                "message": (
                    f"No saved session or workflow found for '{sid}'. "
                    "Open the builder from a completed Launchpad session or saved workflow."
                ),
            },
        )

    if workflow_plan_node_count(workflow, session) == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "workflow_empty",
                "message": (
                    "This session exists but has no workflow steps yet. "
                    "Finish the Launchpad interview and generate an architecture plan first."
                ),
            },
        )

    assert session is not None
    return sid, session, workflow


@builder_chat_router.get("/sessions/{session_id}/builder/chat/sessions")
def list_builder_chat_sessions_api(session_id: str):
    sid, session, _workflow = _load_launchpad_session(session_id)
    del sid
    return list_builder_chat_state(session)


@builder_chat_router.post("/sessions/{session_id}/builder/chat/sessions")
def open_builder_chat_session_api(session_id: str, body: dict[str, Any]):
    sid, session, _workflow = _load_launchpad_session(session_id)
    selection = body.get("selection") if isinstance(body.get("selection"), dict) else body
    if not isinstance(selection, dict):
        selection = {"mode": "current_project"}
    force_new = bool(body.get("force_new"))

    try:
        result = open_chat_session_for_selection(
            session,
            launchpad_session_id=sid,
            selection=selection,
            force_new=force_new,
        )
    except BuilderChatError as exc:
        raise _http_from_builder_error(exc) from exc

    save_session(session, sync_workflow=False)
    return result


@builder_chat_router.post(
    "/sessions/{session_id}/builder/chat/sessions/{chat_session_id}/select"
)
def select_builder_chat_session_api(session_id: str, chat_session_id: str):
    _sid, session, _workflow = _load_launchpad_session(session_id)
    try:
        result = select_chat_session(session, chat_session_id.strip())
    except BuilderChatError as exc:
        raise _http_from_builder_error(exc) from exc

    save_session(session, sync_workflow=False)
    return result


@builder_chat_router.post("/sessions/{session_id}/builder/chat")
def post_builder_chat_api(session_id: str, body: dict[str, Any]):
    sid, session, workflow = _load_launchpad_session(session_id)

    message = str(body.get("message") or "").strip()
    history = body.get("history") if isinstance(body.get("history"), list) else None
    chat_session_id = body.get("chat_session_id") or body.get("chatSessionId")
    chat_session_id = (
        str(chat_session_id).strip() if chat_session_id is not None else None
    )

    # Optional: open/switch repo before sending (compat + convenience).
    selection = body.get("selection")
    if isinstance(selection, dict) and selection.get("mode"):
        try:
            open_chat_session_for_selection(
                session,
                launchpad_session_id=sid,
                selection=selection,
                force_new=bool(body.get("force_new")),
            )
        except BuilderChatError as exc:
            raise _http_from_builder_error(exc) from exc

    try:
        result = handle_builder_chat(
            sid,
            message=message,
            history=history,
            session=session,
            workflow=workflow,
            chat_session_id=chat_session_id,
        )
    except BuilderChatError as exc:
        raise _http_from_builder_error(exc) from exc

    save_session(session, sync_workflow=False)
    return result
