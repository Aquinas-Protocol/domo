"""Paths, constants, env loading, and agent registry."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

HOME = Path.home()


def _env_path(var: str, default: Path) -> Path:
    """Resolve a path from env var `var`, falling back to `default` when unset."""
    val = os.getenv(var)
    return Path(val).expanduser() if val else default


# Vault + bot working directory default to the conventional layout under the
# user's home; override via VAULT_ROOT / BOT_CWD to point the bot at a vault
# elsewhere. (`second-brain-bot` is a junction that resolves to the vault.)
VAULT_ROOT = _env_path("VAULT_ROOT", HOME / "Documents" / "second-brain")
BOT_CWD = _env_path("BOT_CWD", HOME / "Documents" / "second-brain-bot")
BOT_SOURCE_DIR = PROJECT_ROOT
AGENTS_YAML = PROJECT_ROOT / "agents.yaml"
# Bot-generated machine artifacts the user can read. Created on first write.
VAULT_DATA_DIR = VAULT_ROOT / "data"

SETTING_SOURCES = ["user", "project", "local"]
PERMISSION_MODE = "bypassPermissions"
SYSTEM_PROMPT_PRESET = {"type": "preset", "preset": "claude_code"}

IDLE_TEARDOWN_MINUTES = 30
OAUTH_SANITY_CHECK_HOURS = 4

# Backpressure defaults — see Phase 2A §6.
MAX_CONCURRENT_RUNS = 3
MAX_QUEUE_DEPTH_PER_AGENT = 5
BACKGROUND_QUEUE_DEPTH_PER_AGENT = 3
RUN_TIMEOUT_SECONDS = 600
SPECIALIST_DEFAULT_TIMEOUT_SECONDS = 600

DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_DB = DATA_DIR / "sessions.sqlite"
RUNTIME_LOG = DATA_DIR / "runtime.log"


def _path_exists_or_inaccessible(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return True


def _resolve_codex_exe_path() -> str | None:
    explicit = os.getenv("CODEX_EXE_PATH")
    if explicit:
        return explicit

    appdata = os.getenv("APPDATA")
    if appdata:
        npm_shim = Path(appdata) / "npm" / "codex.cmd"
        if _path_exists_or_inaccessible(npm_shim):
            return str(npm_shim)

    for candidate in (shutil.which("codex.cmd"), shutil.which("codex")):
        if candidate and "WindowsApps" not in candidate:
            return candidate

    windows_apps = Path(r"C:\Program Files\WindowsApps")
    try:
        matches = sorted(
            windows_apps.glob(
                r"OpenAI.Codex_*_x64__2p2nqsd0c76g0\app\resources\codex.exe"
            )
        )
    except OSError:
        matches = []
    if matches:
        return str(matches[-1])

    return shutil.which("codex")


CODEX_EXE_PATH = _resolve_codex_exe_path()

# Phase 1 legacy: single bot token. With multi-bot (per-agent identities), this
# becomes the fallback for MAIN_DISCORD_TOKEN if the new one isn't set.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPERATOR_USER_ID = int(os.getenv("OPERATOR_USER_ID", "0")) or None
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Phase 2B-1 dashboard
DASHBOARD_BIND_HOST = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_PIN_HASH = os.getenv("DASHBOARD_PIN_HASH")           # bcrypt hash, set by wizard
DASHBOARD_SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET")  # itsdangerous signer key
DASHBOARD_SESSION_TTL_HOURS = int(os.getenv("DASHBOARD_SESSION_TTL_HOURS", "8"))
DASHBOARD_ENABLED = bool(DASHBOARD_PIN_HASH and DASHBOARD_SESSION_SECRET)

# iCloud / Apple Calendar (CalDAV) integration
ICLOUD_APPLE_ID = os.getenv("ICLOUD_APPLE_ID")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD")
ICLOUD_ALLOWED_CALENDARS = [
    s.strip()
    for s in os.getenv("ICLOUD_ALLOWED_CALENDARS", "").split(",")
    if s.strip()
] or None
ICLOUD_READONLY_CALENDARS = [
    s.strip()
    for s in os.getenv("ICLOUD_READONLY_CALENDARS", "").split(",")
    if s.strip()
] or None
CALENDAR_TOKEN_SECRET = os.getenv("CALENDAR_TOKEN_SECRET") or None

# Phase 2C cron / scheduler
CRON_TZ = os.getenv("CRON_TZ", "America/New_York")
CRON_MIN_INTERVAL_MINUTES = int(os.getenv("CRON_MIN_INTERVAL_MINUTES", "5"))
CRON_TICK_SECONDS = int(os.getenv("CRON_TICK_SECONDS", "30"))
CRON_MAX_CONCURRENT = int(os.getenv("CRON_MAX_CONCURRENT", "3"))
CRON_AUTO_PAUSE = os.getenv("CRON_AUTO_PAUSE", "true").lower() in ("true", "1", "yes")

# Anthropic Agent SDK monthly credit (starts 2026-06-15 on Max 5x: $100/mo).
# Resets with the billing cycle (day-of-month), not the calendar month. The
# governor uses BILLING_CYCLE_START_DAY to bucket ledger rows into "this
# cycle" vs "prior cycle" when computing remaining credit.
BILLING_CYCLE_START_DAY = int(os.getenv("BILLING_CYCLE_START_DAY", "15"))
CREDIT_AGENT_SDK_USD = float(os.getenv("CREDIT_AGENT_SDK_USD", "100.0"))
# Observe-only by default until 2026-06-15. When True, AgentRunner refuses
# dispatch on HARD_PAUSE and on REJECT-over-headroom; when False, the same
# checks still run but only log a "would-have-refused" warning. Flip to
# "true" on June 15 once the credit goes live.
CREDIT_GOVERNOR_ENFORCE = os.getenv("CREDIT_GOVERNOR_ENFORCE", "false").lower() in ("true", "1", "yes")


# Reminder routing: cron jobs (and one-time reminders) default to posting their
# result in #inbox — the user's review queue. Explicit destination_channel_id
# on the job overrides this. NULL on legacy rows = unmanaged (e.g. the
# morning-brief job which embeds its own destination in the prompt).
INBOX_CHANNEL_ID = int(os.getenv("INBOX_CHANNEL_ID", "0"))

# Legacy single-channel from Phase 1; kept as fallback for #main routing if
# MAIN_CHANNEL_ID is unset.
LEGACY_OPERATOR_CHANNEL_ID = int(os.getenv("OPERATOR_CHANNEL_ID", "0")) or None


def _claude_project_memory_dir(cwd: Path) -> Path:
    """Locate the Claude Code per-project memory dir for `cwd`.

    Claude Code stores each project's files under ~/.claude/projects/<slug>,
    where <slug> is the absolute cwd with every non-alphanumeric character
    replaced by a dash. The bot runs Claude Code with the vault as cwd, so the
    memory dir is derived from VAULT_ROOT.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return HOME / ".claude" / "projects" / slug / "memory"


CLAUDE_MEMORY_DIR = _env_path("CLAUDE_MEMORY_DIR", _claude_project_memory_dir(VAULT_ROOT))
MEMORY_FILES = [
    CLAUDE_MEMORY_DIR / "MEMORY.md",
    CLAUDE_MEMORY_DIR / "user_profile.md",
    CLAUDE_MEMORY_DIR / "feedback_karpathy_principles.md",
]


# ----------------------------- Agent registry -----------------------------

@dataclass(frozen=True)
class AgentSpec:
    name: str                          # registry key, e.g. "main"
    display_name: str                  # bot account display, e.g. "Maine"
    persona_file: Path                 # absolute path under VAULT_ROOT
    token: str | None                  # this agent's Discord bot token
    channel_id: int | None             # resolved from channel_env
    webhook_url: str | None            # rarely used with per-agent bots
    can_delegate: bool
    enabled_tools: list[str] | None    # None = inherit defaults
    model: str | None                  # Claude model alias or full name; None = SDK default
    dashboard_visible: bool = True     # False hides this agent from agent tiles + war-room voices
                                       # in Mission Control (state.agents still includes it so
                                       # ledger filter, online counts, etc. stay accurate)
    auto_thread: bool = False          # True: a top-level message in this agent's channel auto-
                                       # creates a public thread anchored to that message; the
                                       # agent's reply (and all follow-ups) live in the thread.
                                       # Off for cron-only agents (e.g. curator).


def _load_agents() -> dict[str, AgentSpec]:
    if not AGENTS_YAML.exists():
        return {}
    raw = yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8"))
    out: dict[str, AgentSpec] = {}
    for name, spec in (raw.get("agents") or {}).items():
        channel_env = spec.get("channel_env")
        webhook_env = spec.get("webhook_env")
        token_env = spec.get("token_env")
        channel_str = os.getenv(channel_env, "") if channel_env else ""

        # Token: read agent-specific env first; for "main" fall back to legacy
        # DISCORD_TOKEN so Phase 1 installs don't lose Maine's identity.
        token = os.getenv(token_env) if token_env else None
        if not token and name == "main" and DISCORD_TOKEN:
            token = DISCORD_TOKEN

        model_raw = spec.get("model")
        out[name] = AgentSpec(
            name=name,
            display_name=spec.get("display_name", name.title()),
            persona_file=VAULT_ROOT / spec.get("persona_file", f"agents/{name}.md"),
            token=token or None,
            channel_id=int(channel_str) if channel_str else None,
            webhook_url=(os.getenv(webhook_env) if webhook_env else None) or None,
            can_delegate=bool(spec.get("can_delegate", False)),
            enabled_tools=spec.get("enabled_tools"),
            model=(model_raw or None) if isinstance(model_raw, str) else None,
            dashboard_visible=bool(spec.get("dashboard_visible", True)),
            auto_thread=bool(spec.get("auto_thread", False)),
        )
    return out


AGENTS: dict[str, AgentSpec] = _load_agents()

# Convenience: channel_id -> agent_name. Used by bot.py routing.
CHANNEL_TO_AGENT: dict[int, str] = {
    spec.channel_id: name
    for name, spec in AGENTS.items()
    if spec.channel_id is not None
}

# Backwards-compat: if MAIN_CHANNEL_ID is unset but the legacy OPERATOR_CHANNEL_ID
# is set, route legacy operator channel posts to "main". Prevents Phase 1
# installs from breaking on first restart after this update.
if (
    LEGACY_OPERATOR_CHANNEL_ID is not None
    and "main" in AGENTS
    and AGENTS["main"].channel_id is None
):
    CHANNEL_TO_AGENT[LEGACY_OPERATOR_CHANNEL_ID] = "main"
    # Reconstruct main's spec with the resolved channel_id so on_ready logging
    # and ThreadSpawnContext both see it.
    _main = AGENTS["main"]
    AGENTS["main"] = AgentSpec(
        name=_main.name,
        display_name=_main.display_name,
        persona_file=_main.persona_file,
        token=_main.token,
        channel_id=LEGACY_OPERATOR_CHANNEL_ID,
        webhook_url=_main.webhook_url,
        can_delegate=_main.can_delegate,
        enabled_tools=_main.enabled_tools,
        model=_main.model,
        dashboard_visible=_main.dashboard_visible,
        auto_thread=_main.auto_thread,
    )


def agent_for_channel(channel_id: int) -> str | None:
    return CHANNEL_TO_AGENT.get(channel_id)


def get_agent(name: str) -> AgentSpec | None:
    return AGENTS.get(name)


# ----------------------- Persona / system prompts -----------------------

OPERATOR_BASE_APPEND = """
You are running inside a Discord-facing Claude Code bot. You talk to one user via DMs and a per-agent Discord channel. Your working directory is a junction that resolves to the second-brain vault — always honor the vault's CLAUDE.md conventions.

Keep Discord-facing responses concise. Users see your text in a chat window, not a terminal. Avoid dumping raw tool output; summarize.
"""


def build_system_prompt_append(agent_name: str, persona_text: str, memory_text: str = "") -> str:
    """Compose the per-agent system_prompt append.

    Order: base bot guidance -> persona file contents -> persistent memory.
    Persona text is the soul; memory injection is for the operator/main agent
    only (callers pass empty string for sub-agents).
    """
    parts = [OPERATOR_BASE_APPEND.strip(), "", "## Persona", persona_text.strip()]
    if memory_text.strip():
        parts.extend(["", "## Persistent memory (injected at boot)", memory_text.strip()])
    return "\n\n".join(parts)
