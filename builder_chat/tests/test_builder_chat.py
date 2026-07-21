"""Tests for Builder Chat (Active Repository + chat sessions)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from builder_chat import (
    BuilderChatError,
    build_builder_chat_context,
    chat_llm_configured,
    handle_builder_chat,
    open_chat_session_for_selection,
    resolve_active_repository,
    resolve_session_for_builder_chat,
    workflow_plan_node_count,
)
from builder_chat.chat_store import (
    ensure_store,
    find_or_create_chat_session,
    find_sessions_for_repository,
)
from builder_chat.repo_context import (
    ActiveRepository,
    RepoContextError,
    resolve_published_repo,
    resolve_repo_from_url,
    select_paths_for_question,
)


def test_resolve_published_repo_requires_job_repo_url() -> None:
    with patch(
        "builder_chat.repo_context.get_job_or_none",
        return_value=None,
    ):
        with pytest.raises(RepoContextError) as exc:
            resolve_published_repo("sess-missing")
    assert exc.value.code == "repo_not_published"


def test_resolve_published_repo_from_job_fields() -> None:
    job = SimpleNamespace(
        repo_url="https://github.com/acme/kyc-bot",
        github_owner="acme",
        github_repo="kyc-bot",
        repo_branch="main",
        pr_url="https://github.com/acme/kyc-bot/pull/1",
    )
    with patch("builder_chat.repo_context.get_job_or_none", return_value=job):
        published = resolve_published_repo("sess-ok")
    assert published.owner == "acme"
    assert published.repo == "kyc-bot"
    assert published.ref == "main"
    assert published.origin == "current_project"
    assert published.identity == "acme/kyc-bot@main"


def test_resolve_repo_from_url_and_tree_path() -> None:
    repo = resolve_repo_from_url("https://github.com/acme/kyc-bot")
    assert repo.identity == "acme/kyc-bot@main"
    assert repo.origin == "repo_url"

    branched = resolve_repo_from_url(
        "https://github.com/acme/kyc-bot/tree/develop/src"
    )
    assert branched.ref == "develop"
    assert branched.identity == "acme/kyc-bot@develop"


def test_resolve_active_repository_hides_origin_branching() -> None:
    job = SimpleNamespace(
        repo_url="https://github.com/acme/kyc-bot",
        github_owner="acme",
        github_repo="kyc-bot",
        repo_branch="main",
        pr_url=None,
    )
    with patch("builder_chat.repo_context.get_job_or_none", return_value=job):
        current = resolve_active_repository(
            {"mode": "current_project"},
            launchpad_session_id="sess-ok",
        )
    custom = resolve_active_repository(
        {"mode": "repo_url", "repo_url": "https://github.com/other/lib"},
        launchpad_session_id="sess-ok",
    )
    assert isinstance(current, ActiveRepository)
    assert isinstance(custom, ActiveRepository)
    assert current.origin == "current_project"
    assert custom.origin == "repo_url"
    assert current.identity != custom.identity


def test_select_paths_prefers_implementation_over_readme() -> None:
    paths = [
        "README.md",
        "package.json",
        "src/index.ts",
        "src/auth/login.py",
        "src/pdf/parser.py",
        "docs/unused.md",
    ]
    selected = select_paths_for_question(
        paths, "Where is authentication implemented?"
    )
    assert "src/auth/login.py" in selected
    # Docs may appear as supplemental, but must not crowd out the impl hit.
    assert selected.index("src/auth/login.py") < selected.index("README.md") if "README.md" in selected else True


def test_select_paths_for_js_import_parsing_question() -> None:
    paths = [
        "README.md",
        "package.json",
        "src/services/parseService.ts",
        "src/services/graphService.ts",
        "src/parser/parser.ts",
        "src/parser/treeSitter.ts",
        "src/types/graph.types.ts",
        "src/utils/format.ts",
    ]
    selected = select_paths_for_question(
        paths,
        "How are JavaScript imports parsed?",
        code_search_hits=["src/services/parseService.ts", "src/parser/parser.ts"],
    )
    assert "src/services/parseService.ts" in selected
    assert "src/parser/parser.ts" in selected
    # Related companions should be pulled in.
    assert any("graph" in p.lower() or "tree" in p.lower() for p in selected)
    # README must not dominate.
    assert selected[0] != "README.md"
    assert selected[0] != "package.json"


def test_classify_question_intent_implementation() -> None:
    from builder_chat.repo_context import classify_question_intent

    assert (
        classify_question_intent("How are JavaScript imports parsed?")
        == "implementation"
    )
    assert classify_question_intent("What does this project do?") in {
        "overview",
        "implementation",
    }

def test_build_builder_chat_context_repo_only() -> None:
    active = ActiveRepository(
        owner="acme",
        repo="kyc-bot",
        ref="main",
        repo_url="https://github.com/acme/kyc-bot",
        origin="current_project",
    )
    payload = {
        "github_repo": {
            "owner": "acme",
            "repo": "kyc-bot",
            "ref": "main",
            "repo_url": "https://github.com/acme/kyc-bot",
            "pr_url": None,
            "file_tree": ["README.md", "main.py"],
            "files": {"README.md": "# KYC bot\n", "main.py": "print('hi')\n"},
        }
    }
    with patch(
        "builder_chat.service.build_github_repo_context",
        return_value=(payload, ["github_repo"]),
    ):
        context_text, sources = build_builder_chat_context(
            active,
            "Explain this project.",
            launchpad_session_id="sess-qa",
        )

    assert sources == ["github_repo"]
    assert "kyc-bot" in context_text
    assert "KYC bot" in context_text


def test_chat_sessions_are_bound_per_repository() -> None:
    session: dict = {"id": "sess-chat"}
    store = ensure_store(session)
    repo_a = ActiveRepository(
        owner="acme",
        repo="alpha",
        ref="main",
        repo_url="https://github.com/acme/alpha",
        origin="repo_url",
    )
    repo_b = ActiveRepository(
        owner="acme",
        repo="beta",
        ref="main",
        repo_url="https://github.com/acme/beta",
        origin="repo_url",
    )
    a1 = find_or_create_chat_session(store, repo_a)
    a1["messages"] = [
        {
            "id": "1",
            "role": "user",
            "content": "alpha only",
            "timestamp": "t",
        }
    ]
    b1 = find_or_create_chat_session(store, repo_b)
    b1["messages"] = [
        {
            "id": "2",
            "role": "user",
            "content": "beta only",
            "timestamp": "t",
        }
    ]
    a_again = find_or_create_chat_session(store, repo_a)

    assert a_again["id"] == a1["id"]
    assert a_again["messages"][0]["content"] == "alpha only"
    assert b1["id"] != a1["id"]
    assert len(find_sessions_for_repository(store, repo_a.identity)) == 1
    assert len(find_sessions_for_repository(store, repo_b.identity)) == 1

    a_new = find_or_create_chat_session(store, repo_a, force_new=True)
    assert a_new["id"] != a1["id"]
    assert a_new["repository_identity"] == repo_a.identity
    assert len(find_sessions_for_repository(store, repo_a.identity)) == 2


def test_open_chat_session_for_url(monkeypatch) -> None:
    session: dict = {"id": "sess-chat"}
    monkeypatch.setattr(
        "builder_chat.service.resolve_active_repository",
        lambda selection, launchpad_session_id: ActiveRepository(
            owner="acme",
            repo="lib",
            ref="main",
            repo_url="https://github.com/acme/lib",
            origin="repo_url",
        ),
    )
    result = open_chat_session_for_selection(
        session,
        launchpad_session_id="sess-chat",
        selection={"mode": "repo_url", "repo_url": "https://github.com/acme/lib"},
    )
    assert result["active_repository"]["repo"] == "lib"
    assert result["chat_session"]["messages"] == []
    assert result["current_session_id"] == result["chat_session"]["id"]


def test_handle_builder_chat_repo_not_published(monkeypatch) -> None:
    from config import Settings

    settings = Settings(
        azure_openai_endpoint="https://x",
        azure_openai_api_key="k",
        azure_openai_api_version="v",
        azure_openai_chat_deployment="chat",
        azure_openai_embedding_deployment="",
        azure_search_endpoint="",
        azure_search_api_key="",
        azure_search_index_name="",
        pdf_path="",
        log_level="INFO",
    )
    monkeypatch.setattr("builder_chat.service.load_settings", lambda: settings)
    monkeypatch.setattr(
        "builder_chat.service.open_chat_session_for_selection",
        lambda *_a, **_k: (_ for _ in ()).throw(
            BuilderChatError(
                "No GitHub repository has been published for this session yet.",
                status_code=422,
                code="repo_not_published",
            )
        ),
    )

    with pytest.raises(BuilderChatError) as exc:
        handle_builder_chat(
            "sess-chat",
            message="Explain this project.",
            history=[],
            session={"id": "sess-chat"},
            workflow=None,
        )
    assert exc.value.code == "repo_not_published"


def test_handle_builder_chat_llm_not_configured_without_crash(monkeypatch) -> None:
    from config import Settings

    empty = Settings(
        azure_openai_endpoint="",
        azure_openai_api_key="",
        azure_openai_api_version="",
        azure_openai_chat_deployment="",
        azure_openai_embedding_deployment="",
        azure_search_endpoint="",
        azure_search_api_key="",
        azure_search_index_name="",
        pdf_path="",
        log_level="INFO",
    )
    monkeypatch.setattr("builder_chat.service.load_settings", lambda: empty)

    session: dict = {"id": "sess-chat"}
    store = ensure_store(session)
    find_or_create_chat_session(
        store,
        ActiveRepository(
            owner="acme",
            repo="lib",
            ref="main",
            repo_url="https://github.com/acme/lib",
            origin="repo_url",
        ),
    )

    with pytest.raises(BuilderChatError) as exc:
        handle_builder_chat(
            "sess-chat",
            message="Explain this project.",
            history=[],
            session=session,
            workflow=None,
        )

    assert exc.value.code == "llm_not_configured"
    assert exc.value.status_code == 503


def test_handle_builder_chat_uses_session_repo_only() -> None:
    session: dict = {"id": "sess-chat"}
    store = ensure_store(session)
    row = find_or_create_chat_session(
        store,
        ActiveRepository(
            owner="acme",
            repo="kyc-bot",
            ref="main",
            repo_url="https://github.com/acme/kyc-bot",
            origin="current_project",
        ),
    )

    with patch(
        "builder_chat.service.load_settings",
    ) as mock_settings, patch(
        "builder_chat.service.make_client",
    ), patch(
        "builder_chat.service._call_llm_conversation",
        return_value="This repo has a README and main.py.",
    ), patch(
        "builder_chat.service.build_builder_chat_context",
        return_value=('{"github_repo":{"repo":"kyc-bot"}}', ["github_repo"]),
    ) as mock_ctx, patch(
        "builder_chat.service._load_system_prompt",
        return_value="system",
    ):
        settings = mock_settings.return_value
        settings.azure_openai_endpoint = "https://example.openai.azure.com"
        settings.azure_openai_api_key = "key"
        settings.azure_openai_api_version = "2024-02-01"
        settings.azure_openai_chat_deployment = "gpt-4o"

        result = handle_builder_chat(
            "sess-chat",
            message="Explain this project.",
            history=[],
            session=session,
            workflow=None,
            chat_session_id=row["id"],
        )

    assert result["reply"] == "This repo has a README and main.py."
    assert result["active_repository"]["repo"] == "kyc-bot"
    assert result["chat_session"]["id"] == row["id"]
    assert len(result["messages"]) == 2
    called_repo = mock_ctx.call_args.args[0]
    assert called_repo.identity == "acme/kyc-bot@main"


def test_chat_llm_configured_requires_chat_vars() -> None:
    from config import Settings

    assert chat_llm_configured(
        Settings(
            azure_openai_endpoint="https://x",
            azure_openai_api_key="k",
            azure_openai_api_version="v",
            azure_openai_chat_deployment="chat",
            azure_openai_embedding_deployment="",
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            pdf_path="",
            log_level="INFO",
        )
    )


def test_resolve_session_from_workflow_only(tmp_path: Path, monkeypatch) -> None:
    workflow = {
        "sessionId": "sess-workflow-only",
        "problemStatement": "KYC PDF review",
        "plan": {
            "graph": {
                "nodes": [{"id": "ingest", "label": "PDF Intake Gateway"}],
                "edges": [],
            }
        },
    }
    monkeypatch.setattr(
        "builder_chat.session.load_workflow",
        lambda sid: workflow if sid == "sess-workflow-only" else None,
    )
    monkeypatch.setattr(
        "builder_chat.session.get_session",
        lambda sid: None,
    )

    session, loaded = resolve_session_for_builder_chat("sess-workflow-only")
    assert session is not None
    assert session["id"] == "sess-workflow-only"
    assert (
        session["architecture_plan"]["graph"]["nodes"][0]["label"]
        == "PDF Intake Gateway"
    )
    assert loaded == workflow
    assert workflow_plan_node_count(loaded, session) == 1


def test_read_json_session_falls_back_to_blob_mirror(tmp_path: Path, monkeypatch) -> None:
    from workflow_builder import launchpad_storage as storage

    mirror_root = tmp_path / "blob_mirror"
    session_id = "mirror-session-1"
    sid = storage.safe_id(session_id)
    mirror_path = mirror_root / "launchpad" / "sessions" / f"{sid}.json"
    mirror_path.parent.mkdir(parents=True)
    payload = {"id": session_id, "spec": {"problem_statement": "from mirror"}}
    mirror_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(storage, "LOCAL_MIRROR", mirror_root)
    monkeypatch.setattr(storage, "LOCAL_SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(storage, "LOCAL_WORKFLOWS", tmp_path / "workflows")
    monkeypatch.setattr(storage, "storage_backend_name", lambda: "local")

    doc = storage.read_json("session", session_id)
    assert doc is not None
    assert doc["spec"]["problem_statement"] == "from mirror"
