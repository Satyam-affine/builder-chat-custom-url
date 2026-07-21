"""Chat-specific settings helpers (Azure OpenAI chat vars only).

Uses LaunchPad host ``config.Settings`` for env loading; chat-specific
validation lives here so LaunchPad core config stays free of chatbot details.
"""

from __future__ import annotations

from config import Settings, _field_name

CHAT_LLM_ENV_VARS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
)


def load_settings() -> Settings:
    """Load settings from the environment without full pipeline validation."""
    return Settings.from_env()


def missing_chat_llm_vars(settings: Settings | None = None) -> list[str]:
    """Return Azure OpenAI chat env var names that are unset."""
    settings = settings or Settings.from_env()
    missing: list[str] = []
    for var in CHAT_LLM_ENV_VARS:
        if not getattr(settings, _field_name(var)).strip():
            missing.append(var)
    return missing


def chat_llm_settings_configured(settings: Settings | None = None) -> bool:
    return len(missing_chat_llm_vars(settings)) == 0
