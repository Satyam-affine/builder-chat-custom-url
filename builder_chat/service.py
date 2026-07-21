"""Builder Chat — codebase assistant grounded in one Active Repository."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from openai import APIError, APITimeoutError, AzureOpenAI, RateLimitError

from builder_chat.chat_store import (
    append_turn,
    ensure_store,
    find_or_create_chat_session,
    get_chat_session,
    get_current_chat_session,
    list_chat_sessions,
    public_session_payload,
    resolve_stored_active_repository,
    set_current_chat_session,
    sync_legacy_messages_mirror,
)
from builder_chat.config import (
    chat_llm_settings_configured,
    load_settings,
    missing_chat_llm_vars,
)
from builder_chat.repo_context import (
    ActiveRepository,
    RepoContextError,
    active_repository_from_dict,
    build_github_repo_context,
    resolve_active_repository,
)
from config import Settings
from services.llm import make_client

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_MAX_CONTEXT_CHARS = 48_000
_MAX_HISTORY_TURNS = 10
_LLM_BACKOFF = (2, 4, 8)
_LLM_MAX_RETRIES = 3


class BuilderChatError(Exception):
    """Raised when builder chat cannot run."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        code: str = "builder_chat_error",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def chat_llm_configured(settings: Settings | None = None) -> bool:
    return chat_llm_settings_configured(settings)


def _load_system_prompt() -> str:
    return (_PROMPTS_DIR / "builder_chat_system.txt").read_text(encoding="utf-8")


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _truncate_json(payload: Any, max_chars: int) -> str:
    text = json.dumps(payload, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    return _truncate_text(text, max_chars)


def build_builder_chat_context(
    active_repo: ActiveRepository,
    question: str,
    *,
    launchpad_session_id: str,
) -> tuple[str, list[str]]:
    """Assemble repo-only JSON context for the active repository."""
    try:
        payload, sources = build_github_repo_context(
            active_repo,
            question,
            launchpad_session_id=launchpad_session_id,
        )
    except RepoContextError as exc:
        raise BuilderChatError(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
        ) from exc
    return _truncate_json(payload, _MAX_CONTEXT_CHARS), sources


def _call_llm_conversation(
    client: AzureOpenAI,
    settings: Settings,
    system_prompt: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
) -> str:
    last_error: Exception | None = None
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=[{"role": "system", "content": system_prompt}, *messages],
                temperature=temperature,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from chat completion")
            return content.strip()
        except (RateLimitError, APITimeoutError, APIError) as exc:
            last_error = exc
            wait = _LLM_BACKOFF[min(attempt, len(_LLM_BACKOFF) - 1)]
            logger.warning(
                "builder_chat LLM error (attempt %d/%d): %s — retrying in %ds",
                attempt + 1,
                _LLM_MAX_RETRIES,
                exc,
                wait,
            )
            time.sleep(wait)
    raise last_error or RuntimeError("LLM call failed after retries")


def _normalize_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in history or []:
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        rows.append({"role": role, "content": content})
    return rows[-(_MAX_HISTORY_TURNS * 2) :]


def _repo_error(exc: RepoContextError) -> BuilderChatError:
    return BuilderChatError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
    )


def open_chat_session_for_selection(
    launchpad_session: dict[str, Any],
    *,
    launchpad_session_id: str,
    selection: dict[str, Any],
    force_new: bool = False,
) -> dict[str, Any]:
    """Resolve Active Repository and find-or-create a bound chat session."""
    store = ensure_store(launchpad_session)
    try:
        active_repo = resolve_active_repository(
            selection,
            launchpad_session_id=launchpad_session_id,
        )
    except RepoContextError as exc:
        # Current project: fall back to a repo already stored on chat sessions
        # when the codegen job manifest is temporarily missing.
        mode = str((selection or {}).get("mode") or "current_project").strip()
        if mode == "current_project" and exc.code == "repo_not_published":
            stored = resolve_stored_active_repository(
                store, prefer_origin="current_project"
            )
            if stored is not None:
                active_repo = stored
            else:
                raise _repo_error(exc) from exc
        else:
            raise _repo_error(exc) from exc

    row = find_or_create_chat_session(
        store, active_repo, force_new=force_new
    )
    sync_legacy_messages_mirror(launchpad_session, store)
    return {
        "chat_session": public_session_payload(row),
        "active_repository": active_repo.to_dict(),
        "sessions": list_chat_sessions(store),
        "current_session_id": store.get("current_session_id"),
    }


def list_builder_chat_state(launchpad_session: dict[str, Any]) -> dict[str, Any]:
    store = ensure_store(launchpad_session)
    current = get_current_chat_session(store)
    active = (
        active_repository_from_dict(current.get("active_repository"))
        if current
        else None
    )
    return {
        "sessions": list_chat_sessions(store),
        "current_session_id": store.get("current_session_id"),
        "chat_session": public_session_payload(current) if current else None,
        "active_repository": active.to_dict() if active else None,
    }


def select_chat_session(
    launchpad_session: dict[str, Any],
    chat_session_id: str,
) -> dict[str, Any]:
    store = ensure_store(launchpad_session)
    row = get_chat_session(store, chat_session_id)
    if row is None:
        raise BuilderChatError(
            f"Chat session '{chat_session_id}' was not found.",
            status_code=404,
            code="chat_session_not_found",
        )
    set_current_chat_session(store, chat_session_id)
    sync_legacy_messages_mirror(launchpad_session, store)
    active = active_repository_from_dict(row.get("active_repository"))
    return {
        "chat_session": public_session_payload(row),
        "active_repository": active.to_dict() if active else None,
        "sessions": list_chat_sessions(store),
        "current_session_id": store.get("current_session_id"),
    }


def handle_builder_chat(
    launchpad_session_id: str,
    *,
    message: str,
    history: list[dict[str, Any]] | None,
    session: dict[str, Any],
    workflow: dict[str, Any] | None = None,
    chat_session_id: str | None = None,
) -> dict[str, Any]:
    """
    Run one Q&A turn grounded only in the chat session's Active Repository.
    """
    del workflow  # retained for call-site compatibility; unused
    user_text = message.strip()
    if not user_text:
        raise BuilderChatError(
            "Message is required", status_code=400, code="invalid_message"
        )

    store = ensure_store(session)
    chat_row: dict[str, Any] | None = None
    if chat_session_id:
        chat_row = get_chat_session(store, chat_session_id)
        if chat_row is None:
            raise BuilderChatError(
                f"Chat session '{chat_session_id}' was not found.",
                status_code=404,
                code="chat_session_not_found",
            )
        set_current_chat_session(store, chat_session_id)
    else:
        chat_row = get_current_chat_session(store)

    if chat_row is None:
        # Auto-open current project session for backward compatibility.
        try:
            opened = open_chat_session_for_selection(
                session,
                launchpad_session_id=launchpad_session_id,
                selection={"mode": "current_project"},
            )
        except BuilderChatError:
            raise
        chat_row = get_chat_session(store, str(opened["current_session_id"]))
        if chat_row is None:
            raise BuilderChatError(
                "Could not open a chat session for the current project.",
                status_code=500,
                code="builder_chat_error",
            )

    active_repo = active_repository_from_dict(chat_row.get("active_repository"))
    if active_repo is None or not active_repo.owner or not active_repo.repo:
        # Migrated placeholder — resolve current project now.
        try:
            active_repo = resolve_active_repository(
                {"mode": "current_project"},
                launchpad_session_id=launchpad_session_id,
            )
        except RepoContextError as exc:
            raise _repo_error(exc) from exc
        chat_row["active_repository"] = active_repo.to_dict()
        chat_row["repository_identity"] = active_repo.identity

    settings = load_settings()
    if not chat_llm_configured(settings):
        missing = ", ".join(missing_chat_llm_vars(settings))
        raise BuilderChatError(
            "Azure OpenAI chat is not configured. Set "
            f"{missing} in .env before using the codebase assistant.",
            status_code=503,
            code="llm_not_configured",
        )

    context_text, sources = build_builder_chat_context(
        active_repo,
        user_text,
        launchpad_session_id=launchpad_session_id,
    )
    system_prompt = (
        _load_system_prompt()
        + "\n\n## Repository contents\n\n```json\n"
        + context_text
        + "\n```"
    )

    # Prefer stored session messages over client history so repos never mix.
    stored_for_llm = chat_row.get("messages") or history
    prior = _normalize_history(
        stored_for_llm if isinstance(stored_for_llm, list) else history
    )
    # Drop the trailing duplicate if client also sent the new user message.
    if prior and prior[-1]["role"] == "user" and prior[-1]["content"] == user_text:
        prior = prior[:-1]

    llm_messages = [*prior, {"role": "user", "content": user_text}]
    client = make_client(settings)
    try:
        reply = _call_llm_conversation(
            client, settings, system_prompt, llm_messages
        )
    except Exception as exc:
        logger.exception(
            "builder_chat LLM call failed for launchpad session %s chat %s",
            launchpad_session_id,
            chat_row.get("id"),
        )
        raise BuilderChatError(
            "The AI service failed to generate a reply. Try again in a moment.",
            status_code=502,
            code="llm_failed",
        ) from exc

    messages = append_turn(chat_row, user_text=user_text, assistant_text=reply)
    sync_legacy_messages_mirror(session, store)

    return {
        "reply": reply,
        "messages": messages,
        "context_sources": sources,
        "chat_session": public_session_payload(chat_row),
        "active_repository": active_repo.to_dict(),
        "current_session_id": store.get("current_session_id"),
        "sessions": list_chat_sessions(store),
    }
