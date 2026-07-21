"""Session/workflow resolution for Builder Chat (LaunchPad storage adapters)."""

from __future__ import annotations

from typing import Any

from workflow_builder.workflow_builder_standalone import (
    get_session,
    load_workflow,
    sync_workflow_from_session,
)


def resolve_session_for_builder_chat(
    session_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Resolve session + workflow for builder chat.

    Uses the same storage paths as GET/PUT ``/sessions/{id}/builder``:
    workflow is loaded first from ``data/workflows/``; session is optional and
    may be synthesized from the saved workflow when no session row exists.
    """
    workflow = load_workflow(session_id)
    session = get_session(session_id)

    if session is None and workflow is not None:
        session = {
            "id": session_id,
            "spec": {
                "problem_statement": (
                    str(workflow.get("problemStatement") or workflow.get("title") or "")
                ),
                "status": "ready",
            },
            "architecture_plan": workflow.get("plan"),
            "builder_chat_messages": [],
        }
    elif session is not None and workflow is None:
        sync_workflow_from_session(session)
        workflow = load_workflow(session_id)

    return session, workflow


def workflow_plan_node_count(
    workflow: dict[str, Any] | None,
    session: dict[str, Any] | None,
) -> int:
    plan = (
        (workflow or {}).get("plan")
        or (session or {}).get("architecture_plan")
        or {}
    )
    graph = plan.get("graph") if isinstance(plan, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    return len(nodes) if isinstance(nodes, list) else 0
