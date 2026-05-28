"""Interactive first-run / upgrade wizard for Domo.

Multi-bot edition: points Domo at your second-brain vault, prompts for one
Discord bot token per agent, installs the council personas, and optionally
wires voice transcription, Apple Calendar, the GPT-5 specialist, and the
dashboard. Detects existing .env values and only re-prompts what's missing.

Domo runs on top of a "second-brain" vault. If you don't have one, set it up
first: https://github.com/Aquinas-Protocol/second-brain (see its GUIDE.md).

Run: py wizard.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import httpx
    import yaml
except ImportError:
    print("Dependencies missing. Activate the venv and `pip install -e .` first.")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
AGENTS_YAML = PROJECT_ROOT / "agents.yaml"
REPO_AGENTS_DIR = PROJECT_ROOT / "agents"

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SECOND_BRAIN_REPO = "https://github.com/Aquinas-Protocol/second-brain"
DEFAULT_VAULT_ROOT = Path.home() / "Documents" / "second-brain"
# The bot runs with this as its cwd — a directory junction to the vault, so the
# bot's Claude Code session JSONLs stay isolated from terminal sessions.
DEFAULT_BOT_CWD = Path.home() / "Documents" / "second-brain-bot"


def _banner(title: str) -> None:
    print(f"\n=== {title} ===")


def _load_existing_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _ask(prompt: str, default: str | None = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if val or allow_empty:
            return val


def _yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        val = input(f"{prompt} [{d}]: ").strip().lower()
        if not val:
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False


# ============================ Discord helpers ============================

async def _validate_token(token: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        return resp.json() if resp.status_code == 200 else None


async def _check_channel(token: str, channel_id: int) -> tuple[bool, dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://discord.com/api/v10/channels/{channel_id}",
            headers={"Authorization": f"Bot {token}"},
        )
        if resp.status_code != 200:
            return False, {"error": f"channel fetch {resp.status_code}: {resp.text[:200]}"}
        return True, resp.json()


def _invite_url(application_id: str) -> str:
    """Generate an invite URL with the permissions the council bots need.

    Permissions integer: Send Messages (2048) + Read Message History (65536) +
    Attach Files (32768) + Create Public Threads (34359738368) + Send Messages
    in Threads (274877906944) + Manage Webhooks (536870912 — optional, used by
    main only for cross-channel mirrors).
    """
    perms = 2048 + 65536 + 32768 + 34359738368 + 274877906944 + 536870912
    return (
        f"https://discord.com/api/oauth2/authorize?client_id={application_id}"
        f"&scope=bot%20applications.commands&permissions={perms}"
    )


# ============================ Steps ============================

def step_claude_auth() -> None:
    """Report which Claude auth path is available — without requiring one.

    Domo runs through Claude Code's own authentication: either a logged-in
    Claude subscription (`claude login` -> ~/.claude/.credentials.json) or an
    ANTHROPIC_API_KEY. Either works. We don't set up or prescribe one here; we
    just surface what's present so first launch isn't a surprise.
    """
    _banner("1. Claude authentication")
    has_sub = CREDENTIALS_PATH.exists()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_sub:
        print(f"OK: Claude subscription login found ({CREDENTIALS_PATH}).")
    if has_key:
        print("OK: ANTHROPIC_API_KEY is set (runs bill as API usage).")
    if has_sub and has_key:
        print("Note: when both are present the SDK uses ANTHROPIC_API_KEY; unset it")
        print("      to prefer the subscription login.")
    if not (has_sub or has_key):
        print("No Claude auth detected yet. Before launching the bot, set up one of:")
        print("  - `claude login`  (uses your Claude subscription), or")
        print("  - ANTHROPIC_API_KEY in the environment / .env  (API billing).")
        print("Continuing setup -- just configure auth before first launch.")


def step_vault(existing: dict[str, str]) -> Path:
    """Locate the second-brain vault Domo runs against. Returns its path.

    A real vault has a CLAUDE.md at its root (installed per the second-brain
    GUIDE). If it's missing we point the user at the starter repo and let them
    proceed anyway (advanced) or pick another path.
    """
    _banner("2. Knowledge vault")
    print("Domo runs on top of a 'second-brain' vault — an Obsidian-style knowledge")
    print("base it uses as shared memory (reads CLAUDE.md conventions, loads agent")
    print("personas from <vault>/agents/, reads & writes wiki/ pages).")
    print(f"If you don't have one yet, set up the starter first (~30-45 min):")
    print(f"  {SECOND_BRAIN_REPO}   (follow its GUIDE.md)")
    print()
    default = existing.get("VAULT_ROOT") or str(DEFAULT_VAULT_ROOT)
    while True:
        vault = Path(_ask("Path to your vault (VAULT_ROOT)", default=default)).expanduser()
        if (vault / "CLAUDE.md").exists():
            print(f"OK: vault found at {vault}")
            return vault
        print(f"  ! {vault} has no CLAUDE.md - it doesn't look like a set-up vault yet.")
        if not vault.exists():
            print(f"    (the directory doesn't exist)")
        print(f"    Set one up via {SECOND_BRAIN_REPO} (GUIDE.md), then re-run the wizard.")
        if _yesno("  Use this path anyway and finish setup later?", default=False):
            return vault
        # else loop and re-prompt for a different path


def step_user_id(existing: dict[str, str]) -> int:
    _banner("3. Your Discord user ID")
    if "OPERATOR_USER_ID" in existing:
        return int(existing["OPERATOR_USER_ID"])
    print("Discord -> Settings -> Advanced -> Developer Mode ON -> right-click yourself -> Copy ID")
    return int(_ask("Your Discord user ID"))


def step_per_agent_setup(
    existing: dict[str, str],
) -> tuple[dict[str, dict], list[str]]:
    """Loop through agents.yaml. Per agent: token + channel + optional webhook."""
    _banner("4. Per-agent bot setup")
    if not AGENTS_YAML.exists():
        print(f"FAIL: {AGENTS_YAML} not found.")
        sys.exit(2)
    agent_specs: dict[str, dict] = (
        yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8")).get("agents") or {}
    )
    if not agent_specs:
        print("FAIL: agents.yaml has no agents.")
        sys.exit(2)

    print("Each agent has its own Discord bot account. For each one you'll need:")
    print("  - the bot token (Discord Developer Portal -> your app -> Bot -> Token)")
    print("  - the channel ID for the agent's Discord text channel")
    print("Tip: the operator (main) can re-use an existing bot by re-pasting that token.\n")

    legacy_token = existing.get("DISCORD_TOKEN")
    legacy_channel = existing.get("OPERATOR_CHANNEL_ID")
    results: dict[str, dict] = {}
    skipped: list[str] = []

    for name, ag in agent_specs.items():
        display = ag.get("display_name", name.title())
        token_env = ag.get("token_env")
        channel_env = ag.get("channel_env")
        webhook_env = ag.get("webhook_env")

        print(f"\n--- {display} ({name}) ---")

        # Default-fill main from legacy fields.
        existing_token = existing.get(token_env, "")
        if name == "main" and not existing_token and legacy_token:
            existing_token = legacy_token

        if not existing_token:
            if name != "main" and not _yesno(f"Configure {display} now?", default=True):
                skipped.append(name)
                print(f"  skipped {display}; you can add later by re-running the wizard")
                continue
            print("  Get a token: https://discord.com/developers/applications -> your app -> Bot -> Reset Token")
            existing_token = _ask(f"  Bot token for {display}")

        print("  validating token...")
        me = asyncio.run(_validate_token(existing_token))
        if me is None:
            print(f"  FAIL: token rejected by Discord. Skipping {display}.")
            skipped.append(name)
            continue
        print(f"  OK: bot account = {me.get('username')} (id={me.get('id')})")
        print(f"  invite URL (if not yet in your server):")
        print(f"    {_invite_url(me.get('id'))}")

        # Channel
        existing_channel = existing.get(channel_env, "")
        if name == "main" and not existing_channel and legacy_channel:
            existing_channel = legacy_channel
            print(f"  (using legacy OPERATOR_CHANNEL_ID={legacy_channel} as default)")
        channel_str = _ask(
            f"  Channel ID for {display} ({channel_env})",
            default=existing_channel or None,
        )
        try:
            channel_id = int(channel_str)
        except ValueError:
            print(f"  FAIL: not a number. Skipping {display}.")
            skipped.append(name)
            continue
        ok, info = asyncio.run(_check_channel(existing_token, channel_id))
        if not ok:
            print(f"  FAIL: {info.get('error')}")
            print(f"  Make sure the bot is invited to that server with the link above.")
            skipped.append(name)
            continue
        print(f"  OK: bot can see #{info.get('name')}")

        # Webhook (optional, rarely used now)
        existing_webhook = existing.get(webhook_env, "")
        results[name] = {
            "token_env": token_env,
            "token": existing_token,
            "channel_env": channel_env,
            "channel_id": channel_id,
            "webhook_env": webhook_env,
            "webhook_url": existing_webhook,  # preserve if set; wizard does not auto-create now
        }

    return results, skipped


def step_groq(existing: dict[str, str]) -> str | None:
    _banner("5. Groq API key (optional - voice-note transcription)")
    if "GROQ_API_KEY" in existing:
        return existing["GROQ_API_KEY"]
    if not _yesno("Enable Discord voice-note transcription?", default=True):
        return None
    key = _ask("Paste Groq API key (console.groq.com)", allow_empty=True)
    return key or None


def step_calendar(existing: dict[str, str]) -> dict[str, str]:
    """Optional Apple Calendar (iCloud CalDAV). Returns new env vars to write.

    On re-use (already configured), returns {} — the merge in step_write_env
    preserves the existing ICLOUD_* values.
    """
    _banner("6. Apple Calendar (optional - iCloud CalDAV)")
    if "ICLOUD_APPLE_ID" in existing:
        print("OK: iCloud calendar already configured (kept as-is).")
        return {}
    if not _yesno("Connect Apple Calendar (council reads events; main/comms can write)?", default=False):
        return {}
    apple_id = _ask("  iCloud Apple ID (email)", allow_empty=True)
    if not apple_id:
        return {}
    print("  Generate an app-specific password at appleid.apple.com -> Sign-In & Security.")
    app_pw = _ask("  iCloud app-specific password", allow_empty=True)
    allowed = _ask("  Allowed calendar names (comma-separated; blank = all)", allow_empty=True)
    out: dict[str, str] = {"ICLOUD_APPLE_ID": apple_id}
    if app_pw:
        out["ICLOUD_APP_PASSWORD"] = app_pw
    if allowed:
        out["ICLOUD_ALLOWED_CALENDARS"] = allowed
    return out


def step_specialist(existing: dict[str, str]) -> None:
    """Detect the Codex CLI for the GPT-5 specialist tool. Informational only —
    src/config.py auto-resolves the path at runtime, so no env write is needed
    unless the user wants to pin a non-standard location."""
    _banner("7. GPT-5 specialist (optional - Codex CLI)")
    found = existing.get("CODEX_EXE_PATH") or shutil.which("codex") or shutil.which("codex.cmd")
    appdata = os.environ.get("APPDATA")
    if not found and appdata:
        shim = Path(appdata) / "npm" / "codex.cmd"
        if shim.exists():
            found = str(shim)
    if found:
        print(f"OK: Codex CLI detected ({found}).")
        print("    main/research can consult GPT-5 via the mcp__specialist__query_gpt5 tool.")
    else:
        print("Codex CLI not found - the GPT-5 specialist tool will be unavailable until")
        print("you install Codex and sign in (e.g. `npm i -g @openai/codex`), or set")
        print("CODEX_EXE_PATH in .env. This is optional; the council works without it.")


def step_dashboard(existing: dict[str, str]) -> tuple[str | None, str | None]:
    """Returns (pin_hash, session_secret). Both must be set together to enable
    the dashboard. Defaults to keeping existing values if present."""
    _banner("8. Dashboard PIN (optional - localhost web UI on :8080)")
    if "DASHBOARD_PIN_HASH" in existing and "DASHBOARD_SESSION_SECRET" in existing:
        if _yesno("Re-use existing dashboard PIN?", default=True):
            return existing["DASHBOARD_PIN_HASH"], existing["DASHBOARD_SESSION_SECRET"]
    if not _yesno("Enable the dashboard?", default=True):
        return None, None

    print("Dashboard binds to 127.0.0.1:8080. PIN gates access; stored as a bcrypt hash.")
    import getpass
    pin = ""
    while not pin:
        pin = getpass.getpass("PIN (input hidden): ").strip()
        if not pin:
            print("  empty PIN rejected; try again")
            continue
        confirm = getpass.getpass("Confirm PIN: ").strip()
        if pin != confirm:
            print("  PINs don't match; try again")
            pin = ""

    sys.path.insert(0, str(PROJECT_ROOT))
    from src.web.auth import hash_pin
    pin_hash = hash_pin(pin)

    import secrets
    session_secret = secrets.token_urlsafe(48)
    print("OK: PIN hashed; cookie-signing secret generated")
    return pin_hash, session_secret


def step_write_env(
    existing: dict[str, str],
    vault_root: Path,
    user_id: int,
    agents: dict[str, dict],
    groq: str | None,
    calendar: dict[str, str],
    dashboard_pin_hash: str | None,
    dashboard_secret: str | None,
) -> None:
    """Merge gathered values into any existing .env and rewrite it.

    Starts from `existing` so keys the wizard doesn't manage (set by hand, or by
    a feature not covered here) survive a re-run.
    """
    _banner("9. Writing .env")
    env = dict(existing)
    env["VAULT_ROOT"] = vault_root.as_posix()  # forward slashes: dotenv-safe on Windows
    env["OPERATOR_USER_ID"] = str(user_id)
    for name, info in agents.items():
        env[info["token_env"]] = info["token"]
        env[info["channel_env"]] = str(info["channel_id"])
        if info.get("webhook_url"):
            env[info["webhook_env"]] = info["webhook_url"]
    if groq:
        env["GROQ_API_KEY"] = groq
    env.update(calendar)
    if dashboard_pin_hash and dashboard_secret:
        env["DASHBOARD_PIN_HASH"] = dashboard_pin_hash
        env["DASHBOARD_SESSION_SECRET"] = dashboard_secret

    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            ["icacls", str(ENV_PATH), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME')}:F"],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        pass
    print(f"OK: wrote {ENV_PATH} ({len(env)} keys)")


def step_junction(vault_root: Path) -> None:
    _banner("10. Directory junction (bot working dir -> vault)")
    if not vault_root.exists():
        print(f"  vault {vault_root} doesn't exist yet; skipping junction.")
        print("  Re-run the wizard once the vault is set up to create it.")
        return
    if DEFAULT_BOT_CWD.exists():
        print(f"OK: {DEFAULT_BOT_CWD} already exists.")
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(DEFAULT_BOT_CWD), str(vault_root)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAIL: mklink failed: {result.stderr.strip() or result.stdout.strip()}")
        sys.exit(2)
    print(f"OK: junction {DEFAULT_BOT_CWD} -> {vault_root}")


def step_install_personas(vault_root: Path) -> None:
    """Copy the bundled council personas into <vault>/agents/.

    config.py resolves each agent's persona_file under VAULT_ROOT, so the
    personas must live in the vault, not the repo. Existing files are never
    overwritten — the user's edits win.
    """
    _banner("11. Council personas")
    if not REPO_AGENTS_DIR.exists():
        print("  (no bundled personas found in repo; skipping)")
        return
    if not vault_root.exists():
        print(f"  vault {vault_root} doesn't exist yet; skipping persona install.")
        print("  Re-run the wizard once the vault is set up.")
        return
    dst_dir = vault_root / "agents"
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    kept: list[str] = []
    for src in sorted(REPO_AGENTS_DIR.glob("*.md")):
        dst = dst_dir / src.name
        if dst.exists():
            kept.append(src.name)
        else:
            shutil.copy2(src, dst)
            copied.append(src.name)
    if copied:
        print(f"OK: installed personas into {dst_dir}: {', '.join(copied)}")
    if kept:
        print(f"  kept existing (not overwritten): {', '.join(kept)}")
    print("  Edit these to shape each agent's voice; they're loaded at bot startup.")


def step_elevation_broker() -> None:
    """Inform the user about the optional broker service.

    Detects whether the broker service is registered (via `sc query`) and
    prints either an OK or the install command. Doesn't try to elevate from
    the wizard — broker install requires admin rights and an explicit user
    decision.
    """
    _banner("12. Elevation broker (optional)")
    service_name = "domo-elevation-broker"
    installed = False
    try:
        result = subprocess.run(
            ["sc", "query", service_name],
            capture_output=True, text=True, check=False,
        )
        installed = result.returncode == 0
    except FileNotFoundError:
        pass

    if installed:
        print(f"OK: '{service_name}' is registered.")
        print("    The operator's request_admin_elevation tool will route through it.")
        return

    print(
        f"'{service_name}' is NOT registered. Without it, request_admin_elevation\n"
        f"will return 'broker not reachable'. It runs as LocalSystem (full admin) -\n"
        f"read the Security section of README.md first. To install, from an ELEVATED PowerShell:\n"
    )
    print("    Start-Process -Verb RunAs powershell -ArgumentList \\")
    print(f"        '-File','{PROJECT_ROOT / 'broker' / 'install_broker.ps1'}'")


def step_service_instructions(skipped: list[str], dashboard_enabled: bool) -> None:
    _banner("13. Service")
    print("Install the always-on Windows service from an ELEVATED PowerShell:")
    print(f"    {PROJECT_ROOT / 'install_service.ps1'}")
    print("Or run in the foreground to test first:  py -m src.bot")
    if skipped:
        print(f"\nSkipped agents: {', '.join(skipped)}")
        print("Re-run the wizard later to add them.")
    if dashboard_enabled:
        print("\nAfter the service is up, open http://localhost:8080 and enter your PIN.")


def main() -> None:
    print("Domo wizard (multi-bot)")
    print("=======================\n")
    existing = _load_existing_env()
    if existing:
        print(f"Loaded existing .env with {len(existing)} keys; re-prompting only what's missing.\n")

    step_claude_auth()
    vault_root = step_vault(existing)
    user_id = step_user_id(existing)
    agents, skipped = step_per_agent_setup(existing)
    if not agents:
        print("\nFAIL: no agents configured. At minimum 'main' is required.")
        sys.exit(2)
    if "main" not in agents:
        print("\nFAIL: 'main' agent is required but was not configured.")
        sys.exit(2)
    groq = step_groq(existing)
    calendar = step_calendar(existing)
    step_specialist(existing)
    pin_hash, secret = step_dashboard(existing)
    step_write_env(existing, vault_root, user_id, agents, groq, calendar, pin_hash, secret)
    step_junction(vault_root)
    step_install_personas(vault_root)
    step_elevation_broker()
    step_service_instructions(skipped, dashboard_enabled=bool(pin_hash and secret))

    print("\nDONE.")


if __name__ == "__main__":
    main()
