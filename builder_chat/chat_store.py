"""Builder Chat conversation sessions bound to one Active Repository each."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from builder_chat.repo_context import (
    ActiveRepository,
    active_repository_from_dict,
    repository_identity,
)

STORE_KEY = "builder_chat"
LEGACY_MESSAGES_KEY = "builder_chat_messages"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_chat_session_id() -> str:
    return f"bcs_{secrets.token_hex(8)}"


def _new_message_id() -> str:
    return f"bcm_{secrets.token_hex(8)}"


def empty_store() -> dict[str, Any]:
    return {"current_session_id": None, "sessions": {}}


def ensure_store(launchpad_session: dict[str, Any]) -> dict[str, Any]:
    """
    Return the builder_chat store on the LaunchPad session document.

    Migrates legacy flat ``builder_chat_messages`` into a current_project
    chat session when possible (messages kept; repo filled on first resolve).
    """
    raw = launchpad_session.get(STORE_KEY)
    if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
        store = raw
    else:
        store = empty_store()
        launchpad_session[STORE_KEY] = store

    _migrate_legacy_messages(launchpad_session, store)
    return store


def _migrate_legacy_messages(
    launchpad_session: dict[str, Any],
    store: dict[str, Any],
) -> None:
    legacy = launchpad_session.get(LEGACY_MESSAGES_KEY)
    if not isinstance(legacy, list) or not legacy:
        return
    if store.get("sessions"):
        # Already migrated / has sessions; drop legacy key eventually.
        return

    now = _utc_now_iso()
    chat_id = _new_chat_session_id()
    # Placeholder repo until current_project is resolved; identity filled later.
    placeholder = {
        "owner": "",
        "repo": "",
        "ref": "main",
        "repo_url": "",
        "origin": "current_project",
        "pr_url": None,
    }
    store["sessions"][chat_id] = {
        "id": chat_id,
        "active_repository": placeholder,
        "repository_identity": "",
        "messages": _normalize_messages(legacy),
        "created_at": now,
        "updated_at": now,
    }
    store["current_session_id"] = chat_id
    launchpad_session[LEGACY_MESSAGES_KEY] = []


def _normalize_messages(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        rows.append(
            {
                "id": str(item.get("id") or _new_message_id()),
                "role": role,
                "content": content,
                "timestamp": item.get("timestamp") or _utc_now_iso(),
            }
        )
    return rows


def list_chat_sessions(store: dict[str, Any]) -> list[dict[str, Any]]:
    sessions = store.get("sessions") or {}
    rows = [s for s in sessions.values() if isinstance(s, dict)]
    rows.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return [_public_session(s) for s in rows]


def get_chat_session(store: dict[str, Any], chat_session_id: str) -> dict[str, Any] | None:
    sessions = store.get("sessions") or {}
    row = sessions.get(chat_session_id)
    return row if isinstance(row, dict) else None


def get_current_chat_session(store: dict[str, Any]) -> dict[str, Any] | None:
    current_id = store.get("current_session_id")
    if not current_id:
        return None
    return get_chat_session(store, str(current_id))


def resolve_stored_active_repository(
    store: dict[str, Any],
    *,
    prefer_origin: str | None = None,
) -> ActiveRepository | None:
    """
    Recover ActiveRepository from persisted chat sessions.

    Used when the codegen job manifest is missing but Builder Chat already
    stored the project's repository on prior sessions.
    """
    candidates: list[ActiveRepository] = []

    def _consider(raw: Any) -> None:
        repo = active_repository_from_dict(raw)
        if repo is not None and repo.owner and repo.repo:
            candidates.append(repo)

    current = get_current_chat_session(store)
    if current is not None:
        _consider(current.get("active_repository"))

    for row in (store.get("sessions") or {}).values():
        if not isinstance(row, dict):
            continue
        _consider(row.get("active_repository"))

    if not candidates:
        return None
    if prefer_origin:
        for repo in candidates:
            if repo.origin == prefer_origin:
                return repo
        return None
    return candidates[0]


def set_current_chat_session(store: dict[str, Any], chat_session_id: str) -> None:
    store["current_session_id"] = chat_session_id


def find_sessions_for_repository(
    store: dict[str, Any],
    identity: str,
) -> list[dict[str, Any]]:
    identity_norm = identity.strip().lower()
    matches: list[dict[str, Any]] = []
    for row in (store.get("sessions") or {}).values():
        if not isinstance(row, dict):
            continue
        rid = str(row.get("repository_identity") or "").strip().lower()
        if not rid:
            repo = active_repository_from_dict(row.get("active_repository"))
            rid = repo.identity if repo else ""
        if rid == identity_norm:
            matches.append(row)
    matches.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return matches


def create_chat_session(
    store: dict[str, Any],
    active_repo: ActiveRepository,
    *,
    messages: list[dict[str, Any]] | None = None,
    make_current: bool = True,
) -> dict[str, Any]:
    now = _utc_now_iso()
    chat_id = _new_chat_session_id()
    row = {
        "id": chat_id,
        "active_repository": active_repo.to_dict(),
        "repository_identity": active_repo.identity,
        "messages": _normalize_messages(messages or []),
        "created_at": now,
        "updated_at": now,
    }
    sessions = store.setdefault("sessions", {})
    sessions[chat_id] = row
    if make_current:
        store["current_session_id"] = chat_id
    return row


def find_or_create_chat_session(
    store: dict[str, Any],
    active_repo: ActiveRepository,
    *,
    force_new: bool = False,
) -> dict[str, Any]:
    """
    Switch to a chat session for this repository identity.

    By default reopens the most recently updated session for that repo.
    ``force_new=True`` always creates another session (same repo, new thread).
    """
    # Attach identity to any migrated placeholder session for current_project.
    _backfill_placeholder_current_project(store, active_repo)

    if not force_new:
        existing = find_sessions_for_repository(store, active_repo.identity)
        if existing:
            row = existing[0]
            store["current_session_id"] = row["id"]
            # Refresh frozen repo metadata (e.g. pr_url) without changing identity.
            row["active_repository"] = active_repo.to_dict()
            row["repository_identity"] = active_repo.identity
            row["updated_at"] = _utc_now_iso()
            return row

    return create_chat_session(store, active_repo, make_current=True)


def _backfill_placeholder_current_project(
    store: dict[str, Any],
    active_repo: ActiveRepository,
) -> None:
    if active_repo.origin != "current_project":
        return
    for row in (store.get("sessions") or {}).values():
        if not isinstance(row, dict):
            continue
        if str(row.get("repository_identity") or "").strip():
            continue
        repo = row.get("active_repository")
        if not isinstance(repo, dict):
            continue
        if str(repo.get("origin") or "") != "current_project":
            continue
        if str(repo.get("owner") or "").strip() and str(repo.get("repo") or "").strip():
            continue
        row["active_repository"] = active_repo.to_dict()
        row["repository_identity"] = active_repo.identity
        row["updated_at"] = _utc_now_iso()


def append_turn(
    chat_session: dict[str, Any],
    *,
    user_text: str,
    assistant_text: str,
) -> list[dict[str, Any]]:
    messages = _normalize_messages(chat_session.get("messages"))
    now = _utc_now_iso()
    messages.append(
        {
            "id": _new_message_id(),
            "role": "user",
            "content": user_text,
            "timestamp": now,
        }
    )
    messages.append(
        {
            "id": _new_message_id(),
            "role": "assistant",
            "content": assistant_text,
            "timestamp": _utc_now_iso(),
        }
    )
    chat_session["messages"] = messages
    chat_session["updated_at"] = _utc_now_iso()
    return messages


def _public_session(row: dict[str, Any]) -> dict[str, Any]:
    repo = active_repository_from_dict(row.get("active_repository"))
    identity = str(row.get("repository_identity") or "")
    if not identity and repo:
        identity = repo.identity
    return {
        "id": row.get("id"),
        "active_repository": repo.to_dict() if repo else row.get("active_repository"),
        "repository_identity": identity,
        "messages": _normalize_messages(row.get("messages")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "label": repo.label if repo else identity or str(row.get("id")),
    }


def public_session_payload(row: dict[str, Any]) -> dict[str, Any]:
    return _public_session(row)


def sync_legacy_messages_mirror(
    launchpad_session: dict[str, Any],
    store: dict[str, Any],
) -> None:
    """Keep flat builder_chat_messages in sync for older UI readers."""
    current = get_current_chat_session(store)
    if current is None:
        launchpad_session[LEGACY_MESSAGES_KEY] = []
        return
    launchpad_session[LEGACY_MESSAGES_KEY] = _normalize_messages(
        current.get("messages")
    )


def identity_for_repo_dict(repo: dict[str, Any] | None) -> str:
    if not repo:
        return ""
    owner = str(repo.get("owner") or "")
    name = str(repo.get("repo") or "")
    ref = str(repo.get("ref") or "main")
    if not owner or not name:
        return ""
    return repository_identity(owner, name, ref)
