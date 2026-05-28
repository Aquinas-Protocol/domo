"""PreToolUse hook: deny writes to vault read-only zones, deny reads of secret
files, fence agents away from bot internals, Claude config, and NSSM service
registry.

`bypassPermissions` skips permission prompts, but hooks still fire. This hook is
the mechanical floor for the vault's "raw is read-only" invariant, the fence
around files the agents must not modify (their own personas, the bot's source,
Claude credentials), and the fence around files the agents must not even read
(secrets: `.env`, OAuth tokens, Claude credentials).

Tested in tests/test_pretooluse_guard.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from src.config import BOT_SOURCE_DIR, VAULT_ROOT

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}

# Absolute path roots that are read-only at agent runtime.
READ_ONLY_ROOTS: list[Path] = [
    VAULT_ROOT / "raw",
    VAULT_ROOT / "agents",
    BOT_SOURCE_DIR,                                       # whole repo
    Path.home() / ".claude" / "projects",
]

# Specific file paths (not directories) that are always read-only.
READ_ONLY_FILES: list[Path] = [
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / ".credentials.json",
    BOT_SOURCE_DIR / ".env",
    BOT_SOURCE_DIR / "agents.yaml",
]

# Filenames that may NEVER be read by agents (secrets). Matched by basename
# regardless of directory — defends against relative paths, copies, etc.
READ_DENY_FILE_NAMES = frozenset({
    ".env",
    ".credentials.json",
    "gmail-token.json",
    "gmail-client.json",
    # Phase 2 email-cull modify-scope token. Different path than the readonly
    # token; only GmailExecutor (a non-MCP class) reads it. No agent should
    # ever load this file.
    "gmail-modify-token.json",
})

# Directory roots whose contents are entirely off-limits to agent reads
# (credential storage, OAuth tokens, etc.).
READ_DENY_DIRS: list[Path] = [
    Path.home() / ".config" / "domo",
]

# Bash command patterns that signal a destructive write/move/delete.
BASH_DESTRUCTIVE_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:rm|del|Remove-Item|rmdir|rd|mv|move|Move-Item)(?:\s|$)"
    r"|(?:^|\s)(?:>|>>)\s*\S",
    re.IGNORECASE,
)

# NOTE: We deliberately do NOT gate the Bash secret-fence on a read-verb regex.
# The set of commands that can read or exfiltrate a file is open-ended
# (cat/head/sed/awk/cp/scp/dd/xxd/base64/python -c/ruby -e/perl/[IO.File]::ReadAllText/...),
# so verb-based detection fails open under adversarial framing. Instead, the
# Bash branch denies any command that *references* a sensitive filename or
# credential directory, regardless of what the surrounding command does. This
# accepts a small number of false positives (e.g. `echo .env` would be denied)
# in exchange for closing the Codex-flagged bypass class.

# Filename suffixes / patterns that should never be touched via Bash redirects.
PROTECTED_BASH_SUFFIXES = (
    ".service",
    ".plist",
    "install_service.ps1",
    "install_dashboard.ps1",
)


def _resolve(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    try:
        return Path(path_str).expanduser().resolve()
    except (OSError, ValueError):
        return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _denied_reason_for_path(path: Path) -> str | None:
    """Return a human-readable reason if `path` is in a read-only zone."""
    for ro_file in READ_ONLY_FILES:
        try:
            if path == ro_file.resolve():
                return f"{path.name} is protected from modification by agents."
        except OSError:
            continue
    for root in READ_ONLY_ROOTS:
        if _is_under(path, root):
            if root == VAULT_ROOT / "raw":
                return ("raw/ is read-only source material per the vault CLAUDE.md. "
                        "Write to wiki/ or content/ instead.")
            if root == VAULT_ROOT / "agents":
                return ("agents/ holds persona files edited only by the user. "
                        "Agents do not modify their own personas.")
            if root == BOT_SOURCE_DIR:
                return ("The bot's own source directory is protected from self-modification. "
                        "Ask the user to edit the domo repo directly if a code change is needed.")
            if root == Path.home() / ".claude" / "projects":
                return ("Claude Code session storage is protected from modification by agents.")
    return None


def _read_denied_reason_for_path(path: Path) -> str | None:
    """Return a reason if reading `path` is forbidden (secrets/credentials)."""
    name = path.name
    if name in READ_DENY_FILE_NAMES or name.startswith(".env."):
        return f"{name!r} is a secret file and may not be read by agents."
    for d in READ_DENY_DIRS:
        if _is_under(path, d):
            return (
                f"{d.name}/ holds OAuth tokens / credentials and may not be "
                f"read by agents."
            )
    return None


def _bash_references_secrets(command: str) -> str | None:
    """If a bash command references any sensitive filename or credential dir,
    deny — regardless of which verb wraps it.

    Filename-first (no verb gate). Justified because:
    - Read/exfil verbs are open-ended (cat, head, sed, awk, cp, scp, dd, xxd,
      base64, python -c, ruby -e, perl, [IO.File]::ReadAllText, ...). Listing
      "all" is impossible; verb-based gating fails open under adversarial
      framing.
    - The agents have no legitimate need to mention these filenames.
    - False positives (e.g. `echo .env > log.txt`) are an acceptable cost
      because they fail safe — the agent retries with different phrasing.

    For credential DIRECTORIES, we match a path-shaped substring
    (parent_name + separator + dir_name) rather than just the bare directory
    basename. This avoids false positives where the directory name is also
    an unrelated word — e.g. `domo` appears in the bot's User-Agent
    string and project path, but `.config/domo` is unique to the
    credential location.
    """
    lowered = command.lower()
    for forbidden in READ_DENY_FILE_NAMES:
        if forbidden.lower() in lowered:
            return f"Bash command references secret file {forbidden!r}."
    if ".env." in lowered:
        return "Bash command references a .env.* file."
    for d in READ_DENY_DIRS:
        parent_name = d.parent.name  # e.g. ".config"
        distinctives = (
            f"{parent_name}/{d.name}".lower(),
            f"{parent_name}\\{d.name}".lower(),
        )
        if any(s in lowered for s in distinctives):
            return (
                f"Bash command references credential directory "
                f"{d.name!r}."
            )
    return None


def _bash_touches_protected(command: str) -> str | None:
    """If the bash command would write/delete in a protected zone, return a reason."""
    if not BASH_DESTRUCTIVE_PATTERN.search(command):
        return None

    # Cheap suffix check first — covers service files even without absolute paths.
    lowered = command.lower()
    for suffix in PROTECTED_BASH_SUFFIXES:
        if suffix.lower() in lowered:
            return f"Destructive bash command targeting protected file pattern '{suffix}'."

    # Token-resolution check: extract path-like tokens, resolve, test against roots.
    tokens = re.findall(r"[A-Za-z]:[\\/][^\s\"';|&]+|[./\\][^\s\"';|&]+", command)
    for token in tokens:
        resolved = _resolve(token)
        if resolved is None:
            continue
        reason = _denied_reason_for_path(resolved)
        if reason:
            return f"Destructive bash command on a protected path: {reason}"

    # Substring check on raw/ for cases where the path token regex misses (e.g. relative).
    if "raw/" in lowered or "raw\\" in lowered or "agents/" in lowered or "agents\\" in lowered:
        return ("Destructive bash command appears to target raw/ or agents/. "
                "Both are read-only at agent runtime.")
    return None


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {}) or {}

    if tool_name in WRITE_TOOLS:
        target_str = tool_input.get("file_path") or tool_input.get("notebook_path")
        target = _resolve(target_str)
        if target is not None:
            reason = _denied_reason_for_path(target)
            if reason:
                return _deny(reason)
            # Writes to credential files are also blocked under the read fence
            # (a write to ~/.config/domo/oauth-token.json from an agent
            # would be exfil-shaped — clobber, then read back via Bash).
            reason = _read_denied_reason_for_path(target)
            if reason:
                return _deny(reason)

    if tool_name in READ_TOOLS:
        target_str = tool_input.get("file_path") or tool_input.get("notebook_path")
        target = _resolve(target_str)
        if target is not None:
            reason = _read_denied_reason_for_path(target)
            if reason:
                return _deny(reason)

    if tool_name == "Grep":
        # Grep is a content-reading tool (ripgrep wrapper). When `path` resolves
        # to a sensitive file or a credential directory, the matched lines
        # would surface secret content to the agent. Block. (No-path Grep
        # respects .gitignore, which excludes .env in this repo.)
        target_str = tool_input.get("path")
        target = _resolve(target_str)
        if target is not None:
            reason = _read_denied_reason_for_path(target)
            if reason:
                return _deny(reason)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        reason = _bash_touches_protected(command) or _bash_references_secrets(command)
        if reason:
            return _deny(reason)

    return {}
