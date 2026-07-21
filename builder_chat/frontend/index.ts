/**
 * Builder Chat — public frontend API.
 *
 * Prefer importing via the LaunchPad host bridge `@/builder-chat`
 * (see `frontend/src/builder-chat.ts`) so Vite resolves the module reliably.
 * Direct imports from this package remain valid for in-module use.
 */

export {
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
  type BuilderChatResponse,
  type BuilderChatSession,
  type BuilderChatSessionsResponse,
  type RepoOrigin,
  type RepoSelection,
} from "./api";

export {
  BuilderChatLauncher,
  BuilderChatPanel,
} from "./BuilderChatWidget";
