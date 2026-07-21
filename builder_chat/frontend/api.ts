const API_BASE = import.meta.env.VITE_AFFINE_API_BASE ?? "";
const CHAT_TIMEOUT_MS = 90_000;

export type BuilderChatErrorCode =
  | "session_not_found"
  | "workflow_empty"
  | "llm_not_configured"
  | "llm_failed"
  | "invalid_session_id"
  | "invalid_message"
  | "invalid_repo_url"
  | "repo_not_published"
  | "github_not_connected"
  | "repo_fetch_failed"
  | "chat_session_not_found"
  | "builder_chat_error"
  | "unknown";

export type RepoOrigin = "current_project" | "repo_url";

export interface ActiveRepository {
  owner: string;
  repo: string;
  ref: string;
  repo_url: string;
  origin: RepoOrigin;
  pr_url?: string | null;
}

export interface BuilderChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
}

export interface BuilderChatSession {
  id: string;
  active_repository: ActiveRepository;
  repository_identity: string;
  messages: BuilderChatMessage[];
  created_at?: string;
  updated_at?: string;
  label?: string;
}

export type RepoSelection =
  | { mode: "current_project" }
  | { mode: "repo_url"; repo_url: string; ref?: string };

export interface BuilderChatResponse {
  reply: string;
  messages: BuilderChatMessage[];
  context_sources: string[];
  chat_session?: BuilderChatSession;
  active_repository?: ActiveRepository;
  current_session_id?: string | null;
  sessions?: BuilderChatSession[];
}

export interface BuilderChatSessionsResponse {
  sessions: BuilderChatSession[];
  current_session_id: string | null;
  chat_session: BuilderChatSession | null;
  active_repository: ActiveRepository | null;
}

export class BuilderChatError extends Error {
  status: number;
  code: BuilderChatErrorCode;

  constructor(
    message: string,
    status: number,
    code: BuilderChatErrorCode = "unknown",
  ) {
    super(message);
    this.name = "BuilderChatError";
    this.status = status;
    this.code = code;
  }
}

function apiRoot(): string {
  return API_BASE.replace(/\/$/, "");
}

function chatUrl(launchpadSessionId: string): string {
  return `${apiRoot()}/api/sessions/${encodeURIComponent(launchpadSessionId)}/builder/chat`;
}

function sessionsUrl(launchpadSessionId: string): string {
  return `${chatUrl(launchpadSessionId)}/sessions`;
}

function parseErrorDetail(raw: unknown): {
  message: string;
  code: BuilderChatErrorCode;
} {
  if (typeof raw === "string" && raw.trim()) {
    const lower = raw.toLowerCase();
    if (lower.includes("session not found")) {
      return { message: raw, code: "session_not_found" };
    }
    if (lower.includes("internal server error")) {
      return {
        message:
          "The server encountered an unexpected error. Check backend logs and try again.",
        code: "unknown",
      };
    }
    return { message: raw, code: "unknown" };
  }
  if (raw && typeof raw === "object") {
    const obj = raw as { message?: string; code?: string };
    const message =
      typeof obj.message === "string" && obj.message.trim()
        ? obj.message
        : "Builder chat failed";
    const code = (obj.code as BuilderChatErrorCode | undefined) ?? "unknown";
    return { message, code };
  }
  return { message: "Builder chat failed", code: "unknown" };
}

function inferErrorCode(status: number, message: string): BuilderChatErrorCode {
  const lower = message.toLowerCase();
  if (status === 503 && lower.includes("not configured")) {
    return "llm_not_configured";
  }
  if (status === 404 && lower.includes("chat session")) {
    return "chat_session_not_found";
  }
  if (status === 404 && lower.includes("not found")) {
    return "session_not_found";
  }
  if (status === 422 && lower.includes("no workflow")) {
    return "workflow_empty";
  }
  if (status === 422 && lower.includes("published")) {
    return "repo_not_published";
  }
  if (status === 400 && lower.includes("github")) {
    return "invalid_repo_url";
  }
  if (status === 401 && lower.includes("github")) {
    return "github_not_connected";
  }
  if (status === 502 || status === 408) {
    return "llm_failed";
  }
  return "unknown";
}

async function parseErrorResponse(
  res: Response,
): Promise<{ message: string; code: BuilderChatErrorCode }> {
  const raw = await res.text();
  const trimmed = raw.trim();

  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      const parsed = JSON.parse(trimmed) as { detail?: unknown };
      if (parsed.detail !== undefined) {
        return parseErrorDetail(parsed.detail);
      }
    } catch {
      /* fall through */
    }
  }

  if (trimmed) {
    const fromDetail = parseErrorDetail(trimmed);
    if (fromDetail.code !== "unknown") {
      return fromDetail;
    }
    return {
      message: trimmed,
      code: inferErrorCode(res.status, trimmed),
    };
  }

  return {
    message: `Request failed (${res.status} ${res.statusText || "error"})`,
    code: inferErrorCode(res.status, res.statusText || ""),
  };
}

export function builderChatErrorLabel(code: BuilderChatErrorCode): string {
  switch (code) {
    case "session_not_found":
      return "Session not found";
    case "workflow_empty":
      return "No workflow steps";
    case "llm_not_configured":
      return "AI not configured";
    case "llm_failed":
      return "AI request failed";
    case "invalid_session_id":
      return "Invalid session";
    case "invalid_repo_url":
      return "Invalid repository URL";
    case "repo_not_published":
      return "Repository not published";
    case "github_not_connected":
      return "GitHub not connected";
    case "repo_fetch_failed":
      return "Could not read repository";
    case "chat_session_not_found":
      return "Chat session not found";
    default:
      return "Chat error";
  }
}

export function formatActiveRepositoryLabel(
  repo: ActiveRepository | null | undefined,
): string {
  if (!repo?.owner || !repo?.repo) return "No repository selected";
  const badge =
    repo.origin === "current_project" ? "Current project" : "Custom URL";
  return `${badge} · ${repo.owner}/${repo.repo}@${repo.ref || "main"}`;
}

async function requestJson<T>(
  url: string,
  init: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers || {}),
      },
    });
    if (!res.ok) {
      const parsed = await parseErrorResponse(res);
      throw new BuilderChatError(parsed.message, res.status, parsed.code);
    }
    return (await res.json()) as T;
  } catch (error) {
    if (error instanceof BuilderChatError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new BuilderChatError("Request timed out", 408, "llm_failed");
    }
    if (error instanceof TypeError) {
      throw new BuilderChatError(
        "Could not reach the backend. Check that the API server is running.",
        0,
        "unknown",
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchBuilderChatSessions(
  launchpadSessionId: string,
): Promise<BuilderChatSessionsResponse> {
  const id = launchpadSessionId.trim();
  if (!id) {
    throw new BuilderChatError(
      "No session id is available for builder chat.",
      400,
      "invalid_session_id",
    );
  }
  return requestJson<BuilderChatSessionsResponse>(sessionsUrl(id), {
    method: "GET",
  });
}

export async function openBuilderChatSession(
  launchpadSessionId: string,
  selection: RepoSelection,
  options?: { forceNew?: boolean },
): Promise<BuilderChatSessionsResponse & { chat_session: BuilderChatSession }> {
  const id = launchpadSessionId.trim();
  if (!id) {
    throw new BuilderChatError(
      "No session id is available for builder chat.",
      400,
      "invalid_session_id",
    );
  }
  return requestJson(sessionsUrl(id), {
    method: "POST",
    body: JSON.stringify({
      selection,
      force_new: Boolean(options?.forceNew),
    }),
  });
}

export async function selectBuilderChatSession(
  launchpadSessionId: string,
  chatSessionId: string,
): Promise<BuilderChatSessionsResponse> {
  const id = launchpadSessionId.trim();
  const chatId = chatSessionId.trim();
  if (!id || !chatId) {
    throw new BuilderChatError(
      "Chat session id is required.",
      400,
      "invalid_session_id",
    );
  }
  return requestJson(
    `${sessionsUrl(id)}/${encodeURIComponent(chatId)}/select`,
    { method: "POST", body: "{}" },
  );
}

export async function sendBuilderChatMessage(
  launchpadSessionId: string,
  message: string,
  history: BuilderChatMessage[],
  options?: {
    chatSessionId?: string | null;
    selection?: RepoSelection;
  },
): Promise<BuilderChatResponse> {
  const trimmedSessionId = launchpadSessionId.trim();
  if (!trimmedSessionId) {
    throw new BuilderChatError(
      "No session id is available for builder chat.",
      400,
      "invalid_session_id",
    );
  }

  const url = chatUrl(trimmedSessionId);
  if (import.meta.env.DEV) {
    console.debug("[BuilderChat] POST", {
      sessionId: trimmedSessionId,
      chatSessionId: options?.chatSessionId,
      url,
      historyTurns: history.length,
      messagePreview: message.slice(0, 120),
    });
  }

  return requestJson<BuilderChatResponse>(url, {
    method: "POST",
    body: JSON.stringify({
      message,
      history: history.map(({ role, content }) => ({ role, content })),
      chat_session_id: options?.chatSessionId || undefined,
      selection: options?.selection,
    }),
  });
}

export function normalizeBuilderChatMessages(
  raw: unknown,
): BuilderChatMessage[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (item): item is BuilderChatMessage =>
        Boolean(item) &&
        typeof item === "object" &&
        (item as BuilderChatMessage).role !== undefined &&
        typeof (item as BuilderChatMessage).content === "string",
    )
    .map((item) => ({
      id: item.id || `bcm_${crypto.randomUUID().slice(0, 8)}`,
      role: item.role,
      content: item.content,
      timestamp: item.timestamp,
    }));
}
