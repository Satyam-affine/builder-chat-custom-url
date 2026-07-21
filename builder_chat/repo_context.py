"""Fetch context for Builder Chat from a single Active Repository."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

from cursor_codegen.store import get_job_or_none

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
# Full tree for search ranking (not all files are fetched into the LLM context).
_MAX_TREE_PATHS = 5_000
_MAX_FILES = 14
_MAX_DOC_FILES = 2
_MAX_FILE_CHARS = 3_500
_MAX_TOTAL_FILE_CHARS = 36_000
_MAX_CODE_SEARCH_HITS = 20

RepoOrigin = Literal["current_project", "repo_url"]
QuestionIntent = Literal["implementation", "overview", "setup"]

_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".bin",
    ".pyc",
    ".pyo",
}

_IMPL_SUFFIXES = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
}

_DOC_NAMES = {
    "readme",
    "readme.md",
    "readme.rst",
    "readme.txt",
    "changelog",
    "changelog.md",
    "license",
    "license.md",
    "contributing",
    "contributing.md",
    "code_of_conduct.md",
}

_CONFIG_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "cargo.toml",
    "go.mod",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "tsconfig.json",
    ".gitignore",
}

_STOPWORDS = {
    "the",
    "and",
    "for",
    "how",
    "are",
    "is",
    "what",
    "where",
    "when",
    "why",
    "does",
    "do",
    "can",
    "this",
    "that",
    "with",
    "from",
    "into",
    "about",
    "which",
    "have",
    "has",
    "was",
    "were",
    "will",
    "would",
    "could",
    "should",
    "please",
    "explain",
    "show",
    "tell",
    "me",
    "you",
    "your",
    "our",
    "any",
    "all",
    "not",
    "use",
    "using",
    "used",
    "via",
    "per",
    "file",
    "files",
    "code",
    "repo",
    "repository",
    "project",
    "app",
    "application",
}

_INTENT_IMPL_HINTS = {
    "implement",
    "implementation",
    "implemented",
    "parse",
    "parser",
    "parsing",
    "how",
    "where",
    "which",
    "function",
    "class",
    "method",
    "handler",
    "service",
    "logic",
    "algorithm",
    "flow",
    "works",
    "working",
    "called",
    "invoke",
    "import",
    "exports",
    "api",
    "endpoint",
    "route",
    "auth",
    "authentication",
    "validate",
    "validation",
    "transform",
    "convert",
    "graph",
    "ast",
    "tree",
    "token",
    "lexer",
}

_INTENT_SETUP_HINTS = {
    "install",
    "setup",
    "configure",
    "configuration",
    "env",
    "environment",
    "deploy",
    "run",
    "start",
    "requirements",
    "dependency",
    "dependencies",
    "docker",
}

_INTENT_OVERVIEW_HINTS = {
    "overview",
    "summary",
    "explain",
    "architecture",
    "structure",
    "what",
    "purpose",
    "readme",
}

_TOKEN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "parse": ("parser", "parsing", "parsed"),
    "parser": ("parse", "parsing"),
    "parsing": ("parse", "parser"),
    "auth": ("authentication", "authorize", "authorization", "login"),
    "authentication": ("auth", "login", "authorize"),
    "import": ("imports", "importer", "importing"),
    "imports": ("import", "importer"),
    "graph": ("graphs", "graphing"),
    "token": ("tokens", "tokenizer", "tokenize"),
    "validate": ("validation", "validator"),
    "validation": ("validate", "validator"),
    "js": ("javascript", "typescript", "ts"),
    "javascript": ("js", "typescript", "ts"),
    "typescript": ("ts", "js", "javascript"),
    "pdf": ("pdfs"),
}


@dataclass(frozen=True)
class ActiveRepository:
    """Single repository the chat engine analyzes (origin is metadata only)."""

    owner: str
    repo: str
    ref: str
    repo_url: str
    origin: RepoOrigin
    pr_url: str | None = None

    @property
    def identity(self) -> str:
        return repository_identity(self.owner, self.repo, self.ref)

    @property
    def label(self) -> str:
        prefix = (
            "Current project"
            if self.origin == "current_project"
            else "Custom URL"
        )
        return f"{prefix} · {self.owner}/{self.repo}@{self.ref}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Back-compat alias used by older tests/imports.
PublishedRepo = ActiveRepository


class RepoContextError(Exception):
    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def repository_identity(owner: str, repo: str, ref: str) -> str:
    """Normalize repository identity as owner/repo@ref."""
    return (
        f"{owner.strip().lower()}/"
        f"{repo.strip().lower()}@"
        f"{(ref or 'main').strip() or 'main'}"
    )


def active_repository_from_dict(raw: Any) -> ActiveRepository | None:
    if not isinstance(raw, dict):
        return None
    owner = str(raw.get("owner") or "").strip()
    repo = str(raw.get("repo") or "").strip()
    ref = str(raw.get("ref") or "main").strip() or "main"
    repo_url = str(raw.get("repo_url") or "").strip()
    origin_raw = str(raw.get("origin") or "repo_url").strip()
    origin: RepoOrigin = (
        "current_project" if origin_raw == "current_project" else "repo_url"
    )
    if not owner or not repo or not repo_url:
        return None
    pr = raw.get("pr_url")
    return ActiveRepository(
        owner=owner,
        repo=repo,
        ref=ref,
        repo_url=repo_url,
        origin=origin,
        pr_url=str(pr).strip() if pr else None,
    )


def resolve_published_repo(session_id: str) -> ActiveRepository:
    """Return the GitHub repo published by codegen for this LaunchPad session."""
    job = get_job_or_none(session_id)
    if job is None or not (job.repo_url or "").strip():
        raise RepoContextError(
            "No GitHub repository has been published for this session yet. "
            "Generate and publish code first, then ask about the repository.",
            code="repo_not_published",
            status_code=422,
        )

    owner = (job.github_owner or "").strip()
    repo = (job.github_repo or "").strip()
    if not owner or not repo:
        parsed = _parse_github_url(job.repo_url or "")
        if not parsed:
            raise RepoContextError(
                "Published repository metadata is incomplete. Re-run code generation.",
                code="repo_not_published",
                status_code=422,
            )
        owner, repo, ref_from_url = parsed
        ref = (job.repo_branch or "").strip() or ref_from_url or "main"
    else:
        ref = (job.repo_branch or "").strip() or "main"

    return ActiveRepository(
        owner=owner,
        repo=repo,
        ref=ref,
        repo_url=str(job.repo_url).strip(),
        origin="current_project",
        pr_url=(job.pr_url or None),
    )


def resolve_repo_from_url(repo_url: str, *, ref: str | None = None) -> ActiveRepository:
    """Parse a GitHub repository URL into an ActiveRepository."""
    parsed = _parse_github_url(repo_url)
    if not parsed:
        raise RepoContextError(
            "Enter a valid GitHub repository URL "
            "(for example https://github.com/owner/project).",
            code="invalid_repo_url",
            status_code=400,
        )
    owner, repo, ref_from_url = parsed
    resolved_ref = (ref or "").strip() or ref_from_url or "main"
    canonical = f"https://github.com/{owner}/{repo}"
    return ActiveRepository(
        owner=owner,
        repo=repo,
        ref=resolved_ref,
        repo_url=canonical,
        origin="repo_url",
        pr_url=None,
    )


def resolve_active_repository(
    selection: dict[str, Any] | None,
    *,
    launchpad_session_id: str,
) -> ActiveRepository:
    """
    Resolve any supported selection into one ActiveRepository.

    Downstream fetch/LLM code must not branch on origin.
    """
    sel = selection if isinstance(selection, dict) else {}
    mode = str(sel.get("mode") or "current_project").strip().lower()

    if mode in {"repo_url", "url", "custom"}:
        url = str(sel.get("repo_url") or sel.get("url") or "").strip()
        ref = str(sel.get("ref") or "").strip() or None
        return resolve_repo_from_url(url, ref=ref)

    if mode in {"current_project", "current", "project", ""}:
        return resolve_published_repo(launchpad_session_id)

    raise RepoContextError(
        f"Unsupported repository selection mode '{mode}'. "
        "Use 'current_project' or 'repo_url'.",
        code="invalid_repo_url",
        status_code=400,
    )


def _parse_github_url(repo_url: str) -> tuple[str, str, str | None] | None:
    """
    Return (owner, repo, ref_or_none).

    Accepts:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo/tree/<ref>/...
    """
    raw = (repo_url or "").strip()
    if not raw:
        return None
    if "://" not in raw and raw.count("/") == 1:
        raw = f"https://github.com/{raw}"

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host not in {"github.com", "www.github.com"}:
        return None

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None

    ref: str | None = None
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = parts[3]

    return owner, repo, ref


def resolve_github_token(session_id: str) -> str:
    """Sync wrapper around existing GitHub auth helpers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_resolve_github_token_async(session_id))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            asyncio.run, _resolve_github_token_async(session_id)
        ).result()


async def _resolve_github_token_async(session_id: str) -> str:
    from github_integration import (
        get_installation,
        get_installation_token,
        resolve_user_access_token,
    )

    user_token = await resolve_user_access_token(session_id)
    if user_token:
        return user_token

    installation = get_installation(session_id)
    if not installation:
        raise RepoContextError(
            "GitHub is not connected for this session. Connect GitHub, then try again.",
            code="github_not_connected",
            status_code=401,
        )

    try:
        return await get_installation_token(int(installation["installationId"]))
    except Exception as exc:
        raise RepoContextError(
            "Could not obtain a GitHub access token for this session.",
            code="github_not_connected",
            status_code=401,
        ) from exc


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Agentic-LaunchPad-BuilderChat",
    }


def fetch_repo_tree(owner: str, repo: str, ref: str, token: str) -> list[str]:
    """Return blob paths for the repo at ref (recursive tree)."""
    url = (
        f"{_GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/git/trees/{quote(ref, safe='')}"
    )
    try:
        response = httpx.get(
            url,
            headers=_github_headers(token),
            params={"recursive": "1"},
            timeout=45.0,
        )
    except httpx.HTTPError as exc:
        raise RepoContextError(
            f"Failed to list files in {owner}/{repo}: {exc}",
            code="repo_fetch_failed",
            status_code=502,
        ) from exc

    if response.status_code == 404:
        raise RepoContextError(
            f"Repository tree not found for {owner}/{repo}@{ref}.",
            code="repo_fetch_failed",
            status_code=404,
        )
    if response.status_code >= 400:
        raise RepoContextError(
            f"GitHub returned {response.status_code} listing {owner}/{repo}.",
            code="repo_fetch_failed",
            status_code=502,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RepoContextError(
            "Invalid GitHub tree response.",
            code="repo_fetch_failed",
            status_code=502,
        ) from exc

    paths: list[str] = []
    for item in payload.get("tree") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "").strip()
        if not path or _is_skipped_path(path):
            continue
        paths.append(path)
        if len(paths) >= _MAX_TREE_PATHS:
            break
    return paths


def fetch_file_text(
    owner: str,
    repo: str,
    path: str,
    ref: str,
    token: str,
) -> str | None:
    url = (
        f"{_GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/contents/{quote(path.replace(chr(92), '/'), safe='/')}"
    )
    try:
        response = httpx.get(
            url,
            headers=_github_headers(token),
            params={"ref": ref},
            timeout=30.0,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("encoding") or "").lower() != "base64":
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        raw = base64.b64decode(content)
    except Exception:
        return None
    if b"\x00" in raw[:1024]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    if len(text) > _MAX_FILE_CHARS:
        return text[: _MAX_FILE_CHARS - 3].rstrip() + "..."
    return text


def select_paths_for_question(
    paths: list[str],
    question: str,
    *,
    code_search_hits: list[str] | None = None,
) -> list[str]:
    """
    Rank repository paths for a question.

    Implementation questions prioritize source files that match intent tokens
    and code-search hits. README / package.json are supplemental only.
    """
    intent = classify_question_intent(question)
    tokens = _question_tokens(question)
    hit_set = {h.replace("\\", "/") for h in (code_search_hits or []) if h}

    scored: list[tuple[int, str]] = []
    for path in paths:
        score = _path_score(path, tokens, intent=intent)
        if path in hit_set or path.replace("\\", "/") in hit_set:
            score += 40
        if score > 0:
            scored.append((score, path))

    # Also score code-search hits that might not be in the truncated tree list.
    path_set = {p.replace("\\", "/") for p in paths}
    for hit in hit_set:
        if hit not in path_set:
            score = _path_score(hit, tokens, intent=intent) + 40
            scored.append((score, hit))

    scored.sort(key=lambda item: (-item[0], item[1]))

    selected: list[str] = []
    seen: set[str] = set()
    doc_count = 0

    def _add(path: str) -> bool:
        nonlocal doc_count
        norm = path.replace("\\", "/")
        if norm in seen:
            return False
        if _is_doc_or_config_path(norm):
            if intent == "implementation" and doc_count >= _MAX_DOC_FILES:
                return False
            if doc_count >= _MAX_DOC_FILES + (1 if intent != "implementation" else 0):
                return False
            doc_count += 1
        seen.add(norm)
        selected.append(norm)
        return True

    for _score, path in scored:
        _add(path)
        if len(selected) >= _MAX_FILES:
            break

    # Expand to sibling / related implementation files for top hits.
    if intent == "implementation":
        for related in _related_paths(selected, paths):
            if len(selected) >= _MAX_FILES:
                break
            _add(related)

    if not selected:
        # Last resort: prefer implementation sources over shallow docs.
        impl = [p for p in paths if _is_impl_path(p)]
        pool = impl or paths
        shallow = sorted(pool, key=lambda p: (p.count("/"), p))[:_MAX_FILES]
        selected = shallow

    return selected[:_MAX_FILES]


def classify_question_intent(question: str) -> QuestionIntent:
    tokens = _question_tokens(question)
    if not tokens:
        return "overview"
    if tokens & _INTENT_SETUP_HINTS and not (tokens & _INTENT_IMPL_HINTS):
        return "setup"
    if tokens & _INTENT_IMPL_HINTS:
        return "implementation"
    if tokens & _INTENT_OVERVIEW_HINTS and len(tokens) <= 4:
        return "overview"
    # Default to implementation so we fetch source, not only README.
    return "implementation"


def search_repo_code(
    owner: str,
    repo: str,
    question: str,
    token: str,
) -> list[str]:
    """
    Use GitHub code search to find files whose contents match the question.

    Returns relative paths within the repo. Best-effort: empty on failure.
    """
    tokens = sorted(_question_tokens(question), key=len, reverse=True)[:5]
    if not tokens:
        return []

    # Prefer distinctive technical tokens for the query.
    query_terms = [t for t in tokens if t not in _INTENT_OVERVIEW_HINTS][:4]
    if not query_terms:
        query_terms = tokens[:3]
    q = " ".join(query_terms) + f" repo:{owner}/{repo}"
    url = f"{_GITHUB_API}/search/code"
    try:
        response = httpx.get(
            url,
            headers={
                **_github_headers(token),
                "Accept": "application/vnd.github.text-match+json",
            },
            params={"q": q, "per_page": _MAX_CODE_SEARCH_HITS},
            timeout=25.0,
        )
    except httpx.HTTPError:
        logger.debug("GitHub code search request failed", exc_info=True)
        return []

    if response.status_code >= 400:
        logger.debug(
            "GitHub code search status %s for %s/%s",
            response.status_code,
            owner,
            repo,
        )
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    hits: list[str] = []
    seen: set[str] = set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path or path in seen or _is_skipped_path(path):
            continue
        seen.add(path)
        hits.append(path)
        if len(hits) >= _MAX_CODE_SEARCH_HITS:
            break
    return hits


def build_github_repo_context(
    active_repo: ActiveRepository,
    question: str,
    *,
    launchpad_session_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """
    Build repo-only context for the LLM from one ActiveRepository.

    ``launchpad_session_id`` is used only for GitHub token resolution.
    """
    token = resolve_github_token(launchpad_session_id)
    tree = fetch_repo_tree(
        active_repo.owner, active_repo.repo, active_repo.ref, token
    )
    if not tree:
        raise RepoContextError(
            f"Repository {active_repo.owner}/{active_repo.repo} has no readable files yet.",
            code="repo_fetch_failed",
            status_code=422,
        )

    code_hits: list[str] = []
    intent = classify_question_intent(question)
    if intent == "implementation":
        code_hits = search_repo_code(
            active_repo.owner, active_repo.repo, question, token
        )

    selected = select_paths_for_question(
        tree, question, code_search_hits=code_hits
    )
    files: dict[str, str] = {}
    total = 0
    for path in selected:
        text = fetch_file_text(
            active_repo.owner,
            active_repo.repo,
            path,
            active_repo.ref,
            token,
        )
        if not text:
            continue
        files[path] = text
        total += len(text)
        if total >= _MAX_TOTAL_FILE_CHARS:
            break

    if not files:
        raise RepoContextError(
            "Could not read any text files from the active repository.",
            code="repo_fetch_failed",
            status_code=502,
        )

    payload: dict[str, Any] = {
        "github_repo": {
            "owner": active_repo.owner,
            "repo": active_repo.repo,
            "ref": active_repo.ref,
            "repo_url": active_repo.repo_url,
            "pr_url": active_repo.pr_url,
            "origin": active_repo.origin,
            "identity": active_repo.identity,
            "question_intent": intent,
            "retrieval": {
                "selected_paths": list(files.keys()),
                "code_search_hits": code_hits[:_MAX_CODE_SEARCH_HITS],
            },
            "file_tree": tree[:_MAX_TREE_PATHS],
            "files": files,
        }
    }
    return payload, ["github_repo"]


def _is_skipped_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    skip_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "coverage",
        ".mypy_cache",
        ".pytest_cache",
        ".next",
        ".turbo",
        "vendor",
    }
    if any(part in skip_dirs for part in parts):
        return True
    lower = path.lower()
    return any(lower.endswith(ext) for ext in _BINARY_SUFFIXES)


def _path_suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _is_impl_path(path: str) -> bool:
    return _path_suffix(path) in _IMPL_SUFFIXES


def _is_doc_or_config_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _DOC_NAMES or name in _CONFIG_NAMES:
        return True
    if "/docs/" in f"/{path.lower()}/" or path.lower().startswith("docs/"):
        return True
    if name.endswith(".md") or name.endswith(".rst"):
        return True
    return False


def _question_tokens(question: str) -> set[str]:
    raw = question or ""
    # Split camelCase / snake_case before lowercasing.
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    spaced = spaced.replace("_", " ").replace("-", " ").replace(".", " ")
    base = {
        t
        for t in re.split(r"[^a-z0-9]+", spaced.lower())
        if len(t) >= 2 and t not in _STOPWORDS
    }
    expanded: set[str] = set(base)
    for token in list(base):
        for extra in _TOKEN_EXPANSIONS.get(token, ()):
            expanded.add(extra)
        # Light stemming for common -ing/-ed/-er/-s endings.
        for suffix in ("ing", "ers", "er", "ed", "es", "s"):
            if len(token) > len(suffix) + 2 and token.endswith(suffix):
                stem = token[: -len(suffix)]
                if len(stem) >= 3:
                    expanded.add(stem)
    return expanded


def _path_score(
    path: str,
    tokens: set[str],
    *,
    intent: QuestionIntent,
) -> int:
    if not tokens and intent == "overview":
        return 1 if _is_doc_or_config_path(path) else 0

    norm = path.replace("\\", "/")
    name = norm.rsplit("/", 1)[-1]
    stem_raw = name.rsplit(".", 1)[0] if "." in name else name
    # Preserve camelCase boundaries before lowercasing (parseService → parse, service).
    stem_spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem_raw)
    stem_parts = [
        p
        for p in re.split(r"[^a-z0-9]+", stem_spaced.lower())
        if len(p) >= 2
    ]
    stem = stem_raw.lower()
    dirs = norm.rsplit("/", 1)[0].lower() if "/" in norm else ""
    parts = [p for p in re.split(r"[^a-z0-9]+", norm.lower()) if len(p) >= 2]
    parts = list({*parts, *stem_parts})

    score = 0
    for token in tokens:
        if token == stem or token in stem:
            score += 12
        elif any(
            token == sp or sp.startswith(token) or token.startswith(sp)
            for sp in stem_parts
        ):
            score += 10
        elif stem.startswith(token) or token.startswith(stem):
            score += 8
        if token in dirs.split("/"):
            score += 6
        elif token in dirs:
            score += 3
        if any(
            token == part or part.startswith(token) or token.startswith(part)
            for part in parts
        ):
            score += 2

    lower_path = norm.lower()
    if _is_impl_path(path):
        if intent == "implementation":
            score += 8
        elif intent == "setup":
            score += 1
        else:
            score += 3
    elif _is_doc_or_config_path(path):
        if intent == "implementation":
            score -= 15
        elif intent == "setup":
            score += 6
        else:
            score += 4

    if intent == "implementation":
        for marker in (
            "/src/",
            "/lib/",
            "/app/",
            "/server/",
            "/backend/",
            "/services/",
            "/core/",
            "/pkg/",
            "/internal/",
        ):
            if marker in f"/{lower_path}/" or lower_path.startswith(marker.strip("/")):
                score += 4
                break
        for marker in ("/test/", "/tests/", "/__tests__/", "/spec/", "/fixtures/"):
            if marker in f"/{lower_path}/":
                score -= 4
                break

    return score


def _related_paths(selected: list[str], all_paths: list[str]) -> list[str]:
    """Pull in siblings / type companions for selected implementation files."""
    by_dir: dict[str, list[str]] = {}
    for path in all_paths:
        norm = path.replace("\\", "/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
        by_dir.setdefault(parent, []).append(norm)

    related: list[str] = []
    selected_set = {p.replace("\\", "/") for p in selected}

    def _stem_roots(filename: str) -> set[str]:
        stem_raw = filename.rsplit(".", 1)[0]
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem_raw)
        parts = {
            p
            for p in re.split(r"[^a-z0-9]+", spaced.lower())
            if len(p) >= 3
        }
        stem = stem_raw.lower()
        roots = {stem, *parts}
        for suffix in (
            "service",
            "controller",
            "handler",
            "util",
            "utils",
            "helper",
            "helpers",
            "types",
            "type",
            "model",
            "models",
            "test",
            "spec",
        ):
            if stem.endswith(suffix) and len(stem) > len(suffix) + 2:
                roots.add(stem[: -len(suffix)])
        return roots

    for path in list(selected):
        if not _is_impl_path(path):
            continue
        norm = path.replace("\\", "/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
        roots = _stem_roots(norm.rsplit("/", 1)[-1])
        siblings = by_dir.get(parent, [])
        dir_impl = [s for s in siblings if _is_impl_path(s) and s not in selected_set]
        for sibling in dir_impl:
            if sibling in selected_set:
                continue
            sib_roots = _stem_roots(sibling.rsplit("/", 1)[-1])
            if roots & sib_roots:
                related.append(sibling)
                selected_set.add(sibling)
                continue
            # Small module folders: include neighboring implementation files.
            if len(dir_impl) <= 6:
                related.append(sibling)
                selected_set.add(sibling)
    return related

