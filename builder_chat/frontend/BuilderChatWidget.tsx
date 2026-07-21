import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, MessageCircle } from "lucide-react";
import {
  BuilderChatError,
  builderChatErrorLabel,
  fetchBuilderChatSessions,
  formatActiveRepositoryLabel,
  normalizeBuilderChatMessages,
  openBuilderChatSession,
  selectBuilderChatSession,
  sendBuilderChatMessage,
  type ActiveRepository,
  type BuilderChatErrorCode,
  type BuilderChatMessage,
  type BuilderChatSession,
  type RepoSelection,
} from "./api";
import { ChatInput } from "@/features/launchpad/ChatInput";
import {
  ChatMessage,
  TypingIndicator,
} from "@/features/launchpad/ChatMessage";
import { Button } from "@/components/ui/button";

const STARTER_PROMPTS = [
  "Explain this project.",
  "How do I run it?",
  "Where is authentication implemented?",
  "Which files implement PDF parsing?",
];

interface BuilderChatSharedProps {
  sessionId: string;
  initialMessages?: BuilderChatMessage[];
  onMessagesChange?: (messages: BuilderChatMessage[]) => void;
}

export interface BuilderChatPanelProps extends BuilderChatSharedProps {
  onClose: () => void;
}

export interface BuilderChatLauncherProps {
  open: boolean;
  onOpen: () => void;
  hidden?: boolean;
}

function applySessionState(
  result: {
    chat_session?: BuilderChatSession | null;
    active_repository?: ActiveRepository | null;
    sessions?: BuilderChatSession[];
    current_session_id?: string | null;
    messages?: BuilderChatMessage[];
  },
  setters: {
    setMessages: (m: BuilderChatMessage[]) => void;
    setSessions: (s: BuilderChatSession[]) => void;
    setChatSessionId: (id: string | null) => void;
    setActiveRepo: (r: ActiveRepository | null) => void;
    onMessagesChange?: (messages: BuilderChatMessage[]) => void;
  },
) {
  const session = result.chat_session ?? null;
  const messages =
    result.messages ??
    (session ? normalizeBuilderChatMessages(session.messages) : []);
  setters.setMessages(messages);
  setters.onMessagesChange?.(messages);
  if (result.sessions) setters.setSessions(result.sessions);
  setters.setChatSessionId(
    result.current_session_id ?? session?.id ?? null,
  );
  setters.setActiveRepo(
    result.active_repository ?? session?.active_repository ?? null,
  );
}

/** Full-height chat that fills the right Inspector column. */
export function BuilderChatPanel({
  sessionId,
  initialMessages = [],
  onMessagesChange,
  onClose,
}: BuilderChatPanelProps) {
  const [messages, setMessages] = useState<BuilderChatMessage[]>(initialMessages);
  const [sessions, setSessions] = useState<BuilderChatSession[]>([]);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [activeRepo, setActiveRepo] = useState<ActiveRepository | null>(null);
  const [sourceMode, setSourceMode] = useState<"current_project" | "repo_url">(
    "current_project",
  );
  const [repoUrlDraft, setRepoUrlDraft] = useState("");
  const [draft, setDraft] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [isSwitching, setIsSwitching] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<BuilderChatErrorCode | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const stateSetters = {
    setMessages,
    setSessions,
    setChatSessionId,
    setActiveRepo,
    onMessagesChange,
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setBootstrapping(true);
      setError(null);
      setErrorCode(null);
      try {
        const state = await fetchBuilderChatSessions(sessionId);
        if (cancelled) return;
        if (state.chat_session) {
          applySessionState(state, stateSetters);
          if (state.active_repository?.origin === "repo_url") {
            setSourceMode("repo_url");
            setRepoUrlDraft(state.active_repository.repo_url || "");
          } else {
            setSourceMode("current_project");
          }
        } else {
          // Default: open Current Project (may 422 if unpublished — show error).
          try {
            const opened = await openBuilderChatSession(sessionId, {
              mode: "current_project",
            });
            if (cancelled) return;
            applySessionState(opened, stateSetters);
            setSourceMode("current_project");
          } catch (err) {
            if (cancelled) return;
            if (initialMessages.length) {
              setMessages(initialMessages);
            }
            if (err instanceof BuilderChatError) {
              setError(err.message);
              setErrorCode(err.code);
            }
          }
        }
      } catch (err) {
        if (cancelled) return;
        if (initialMessages.length) setMessages(initialMessages);
        if (err instanceof BuilderChatError) {
          setError(err.message);
          setErrorCode(err.code);
        }
      } finally {
        if (!cancelled) setBootstrapping(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- bootstrap once per LaunchPad session
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isTyping]);

  const handleOpenSelection = useCallback(
    async (selection: RepoSelection, forceNew = false) => {
      setIsSwitching(true);
      setError(null);
      setErrorCode(null);
      try {
        const opened = await openBuilderChatSession(sessionId, selection, {
          forceNew,
        });
        applySessionState(opened, stateSetters);
        if (selection.mode === "repo_url") {
          setSourceMode("repo_url");
          setRepoUrlDraft(selection.repo_url);
        } else {
          setSourceMode("current_project");
        }
      } catch (err) {
        if (err instanceof BuilderChatError) {
          setError(err.message);
          setErrorCode(err.code);
        } else {
          setError(
            err instanceof Error ? err.message : "Failed to switch repository",
          );
          setErrorCode("unknown");
        }
      } finally {
        setIsSwitching(false);
      }
    },
    [sessionId, onMessagesChange],
  );

  const handleSelectSession = useCallback(
    async (id: string) => {
      if (!id || id === chatSessionId) return;
      setIsSwitching(true);
      setError(null);
      setErrorCode(null);
      try {
        const state = await selectBuilderChatSession(sessionId, id);
        applySessionState(state, stateSetters);
        const origin = state.active_repository?.origin;
        if (origin === "repo_url") {
          setSourceMode("repo_url");
          setRepoUrlDraft(state.active_repository?.repo_url || "");
        } else {
          setSourceMode("current_project");
        }
      } catch (err) {
        if (err instanceof BuilderChatError) {
          setError(err.message);
          setErrorCode(err.code);
        }
      } finally {
        setIsSwitching(false);
      }
    },
    [chatSessionId, sessionId, onMessagesChange],
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isTyping || isSwitching) return;
      setError(null);
      setErrorCode(null);
      setDraft("");
      setIsTyping(true);
      try {
        const result = await sendBuilderChatMessage(
          sessionId,
          trimmed,
          messages,
          { chatSessionId },
        );
        applySessionState(result, stateSetters);
      } catch (err) {
        if (err instanceof BuilderChatError) {
          setError(err.message);
          setErrorCode(err.code);
        } else {
          setError(
            err instanceof Error ? err.message : "Failed to send message",
          );
          setErrorCode("unknown");
        }
      } finally {
        setIsTyping(false);
      }
    },
    [
      chatSessionId,
      isSwitching,
      isTyping,
      messages,
      onMessagesChange,
      sessionId,
    ],
  );

  const busy = isTyping || isSwitching || bootstrapping;
  const repoLabel = formatActiveRepositoryLabel(activeRepo);

  return (
    <div
      className="flex h-full min-h-0 w-full flex-col bg-background"
      role="region"
      aria-label="Workflow codebase assistant"
    >
      <header className="flex shrink-0 flex-col gap-2 border-b border-border bg-surface px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={onClose}
            aria-label="Back to inspector"
          >
            <ArrowLeft size={16} />
          </Button>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold leading-tight">
              Codebase assistant
            </p>
            <p
              className="truncate text-[11px] text-muted-foreground"
              title={repoLabel}
            >
              {bootstrapping ? "Loading…" : repoLabel}
            </p>
          </div>
        </div>

        <div className="space-y-2 pl-10">
          <div
            className="inline-flex rounded-md border border-border bg-background p-0.5 text-[11px]"
            role="group"
            aria-label="Repository source"
          >
            <button
              type="button"
              disabled={busy}
              className={`rounded px-2 py-1 transition-colors disabled:opacity-50 ${
                sourceMode === "current_project"
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted/80"
              }`}
              onClick={() => {
                setSourceMode("current_project");
                void handleOpenSelection({ mode: "current_project" });
              }}
            >
              Current project
            </button>
            <button
              type="button"
              disabled={busy}
              className={`rounded px-2 py-1 transition-colors disabled:opacity-50 ${
                sourceMode === "repo_url"
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted/80"
              }`}
              onClick={() => setSourceMode("repo_url")}
            >
              Repository URL
            </button>
          </div>

          {sourceMode === "repo_url" ? (
            <div className="flex gap-1.5">
              <input
                type="url"
                value={repoUrlDraft}
                onChange={(e) => setRepoUrlDraft(e.target.value)}
                disabled={busy}
                placeholder="https://github.com/owner/project"
                className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-[11px] outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                aria-label="GitHub repository URL"
              />
              <Button
                type="button"
                size="sm"
                className="h-7 shrink-0 px-2 text-[11px]"
                disabled={busy || !repoUrlDraft.trim()}
                onClick={() =>
                  void handleOpenSelection({
                    mode: "repo_url",
                    repo_url: repoUrlDraft.trim(),
                  })
                }
              >
                Use Repository
              </Button>
            </div>
          ) : null}

          {sessions.length > 1 ? (
            <label className="flex min-w-0 flex-col gap-0.5 text-[11px] text-muted-foreground">
              <span>Chat session</span>
              <select
                className="rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                value={chatSessionId ?? ""}
                disabled={busy}
                onChange={(e) => void handleSelectSession(e.target.value)}
                aria-label="Switch chat session"
              >
                {sessions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label ||
                      formatActiveRepositoryLabel(s.active_repository)}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          {activeRepo ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="rounded-full border border-border bg-card px-2 py-0.5 text-[10px] font-medium text-foreground/90">
                {activeRepo.origin === "current_project"
                  ? "Current project"
                  : "Custom URL"}
              </span>
              <a
                href={activeRepo.repo_url}
                target="_blank"
                rel="noreferrer"
                className="truncate text-[10px] text-primary underline-offset-2 hover:underline"
              >
                {activeRepo.owner}/{activeRepo.repo}@{activeRepo.ref}
              </a>
              <button
                type="button"
                disabled={busy}
                className="text-[10px] text-muted-foreground underline-offset-2 hover:underline disabled:opacity-50"
                onClick={() =>
                  void handleOpenSelection(
                    activeRepo.origin === "repo_url"
                      ? {
                          mode: "repo_url",
                          repo_url: activeRepo.repo_url,
                          ref: activeRepo.ref,
                        }
                      : { mode: "current_project" },
                    true,
                  )
                }
              >
                New chat
              </button>
            </div>
          ) : null}
        </div>
      </header>

      <div
        className="flex-1 min-h-0 overflow-y-auto px-3 py-3 space-y-3 bg-[var(--lp-chat-bg,transparent)]"
        style={{ "--lp-chat-max-width": "100%" } as React.CSSProperties}
      >
        {messages.length === 0 && !isTyping && !bootstrapping ? (
          <div className="space-y-3 px-1">
            <p className="text-xs text-muted-foreground leading-relaxed">
              Ask questions about the active repository shown above. Answers are
              grounded only in that codebase—switch repository to start or reopen
              another chat session.
            </p>
            <div className="flex flex-wrap gap-2">
              {STARTER_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  disabled={busy || !activeRepo}
                  onClick={() => void send(prompt)}
                  className="rounded-full border border-border/80 bg-card px-2.5 py-1 text-[11px] text-foreground/90 hover:bg-muted/80 transition-colors disabled:opacity-50"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {messages.map((msg) => (
          <ChatMessage
            key={msg.id}
            message={{
              id: msg.id,
              role: msg.role,
              content: msg.content,
              timestamp: msg.timestamp,
            }}
          />
        ))}

        {isTyping ? <TypingIndicator /> : null}
        {error ? (
          <div
            className="rounded-lg border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-xs text-destructive"
            role="alert"
          >
            {errorCode ? (
              <p className="font-semibold mb-0.5">
                {builderChatErrorLabel(errorCode)}
              </p>
            ) : null}
            <p className="leading-relaxed">{error}</p>
          </div>
        ) : null}
        <div ref={bottomRef} aria-hidden />
      </div>

      <div className="shrink-0 [&_.lp-chat-composer]:border-t [&_.lp-chat-composer]:px-2 [&_.lp-chat-composer]:py-2">
        <ChatInput
          value={draft}
          onChange={setDraft}
          onSend={() => void send(draft)}
          disabled={busy || !activeRepo}
          placeholder="Ask about the active repository…"
        />
      </div>
    </div>
  );
}

/** Floating launcher — opens chat into the Inspector panel. */
export function BuilderChatLauncher({
  open,
  onOpen,
  hidden = false,
}: BuilderChatLauncherProps) {
  if (hidden || open) return null;
  if (typeof document === "undefined") return null;

  return createPortal(
    <div className="fixed bottom-6 right-6 z-[250] pointer-events-none">
      <Button
        type="button"
        size="icon"
        className="pointer-events-auto h-24 w-24 rounded-full shadow-lg [&_svg]:!size-[42px]"
        onClick={onOpen}
        aria-label="Open codebase assistant"
        aria-expanded={open}
      >
        <MessageCircle size={42} />
      </Button>
    </div>,
    document.body,
  );
}

export { normalizeBuilderChatMessages };
