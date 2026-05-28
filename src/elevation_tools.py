"""MCP tool: request_admin_elevation — Discord-approved Windows elevation.

Registered for the operator agent only. Posts an Approve/Deny embed to
Maine's channel, awaits the user's decision via an asyncio.Event set by the
ElevationView button handler, then talks to the elevated broker service over
a Windows named pipe to actually run the command.

Approval and execution are split: the bot owns approval (Discord, audit,
embed, persistent view); the broker owns execution (privilege, deny list,
subprocess capture). The bot never executes the elevated command itself.

The pipe message carries only the request UUID. The broker re-reads the
canonical row from SQLite — the bot can't bypass approval by lying over
the pipe.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import uuid as _uuid_mod
from typing import Any

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.config import AGENTS, RUN_TIMEOUT_SECONDS
from src.elevation_store import ElevationStore
from src.elevation_view import ElevationView

log = logging.getLogger(__name__)

BROKER_PIPE_NAME = r"\\.\pipe\domo-elevation-broker"

# Timeout caps. Sum must stay below RUN_TIMEOUT_SECONDS minus a safety margin
# so Maine's whole turn doesn't die mid-elevation.
SAFETY_MARGIN_S = 60
MAX_TIMEOUT_TOTAL_S = RUN_TIMEOUT_SECONDS - SAFETY_MARGIN_S
MAX_PER_TIMEOUT_S = 240
DEFAULT_APPROVAL_TIMEOUT_S = 120
DEFAULT_COMMAND_TIMEOUT_S = 120

EMBED_COMMAND_PREVIEW_CHARS = 1500
PIPE_CONNECT_RETRIES = 3
PIPE_CONNECT_BACKOFF_S = 0.25

# Best-effort vault-write precheck. Refuses the most obvious patterns before
# anything reaches Discord. Not a security boundary — the user is. The embed
# still carries an explicit warning that elevation bypasses the vault guard.
_VAULT_PATH_FRAGMENTS = (
    r"second-brain[\\/]raw",
    r"second-brain[\\/]agents",
    r"second-brain-bot[\\/]raw",
    r"second-brain-bot[\\/]agents",
    # repo-relative shapes; covers cd-into-vault commands.
    r"\braw[\\/]",
    r"\bagents[\\/]",
)
_WRITE_VERBS = (
    r"Remove-Item",
    r"\brm\b",
    r"\bdel\b",
    r"Set-Content",
    r"Out-File",
    r"New-Item",
    r"Move-Item",
    r"Copy-Item",
    r"Add-Content",
    r"Clear-Content",
    r">>",
    r">",
)
_VAULT_WRITE_RE = re.compile(
    "(" + "|".join(_WRITE_VERBS) + r").{0,160}?(" + "|".join(_VAULT_PATH_FRAGMENTS) + ")",
    re.IGNORECASE,
)


def _compute_sha256(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _validate_timeouts(approval_s: int, command_s: int) -> str | None:
    if approval_s <= 0 or command_s <= 0:
        return f"timeouts must be positive (got approval={approval_s}, command={command_s})"
    if approval_s > MAX_PER_TIMEOUT_S:
        return f"approval_timeout_s={approval_s} exceeds cap of {MAX_PER_TIMEOUT_S}s"
    if command_s > MAX_PER_TIMEOUT_S:
        return f"command_timeout_s={command_s} exceeds cap of {MAX_PER_TIMEOUT_S}s"
    if approval_s + command_s > MAX_TIMEOUT_TOTAL_S:
        return (
            f"approval_timeout_s + command_timeout_s = {approval_s + command_s} "
            f"exceeds {MAX_TIMEOUT_TOTAL_S}s (RUN_TIMEOUT_SECONDS - {SAFETY_MARGIN_S}s "
            f"safety margin). Lower one or both."
        )
    return None


def _vault_write_precheck(command: str) -> str | None:
    if _VAULT_WRITE_RE.search(command):
        return (
            "command appears to write to the vault's read-only folders "
            "(raw/ or agents/). Elevation does NOT bypass the vault's "
            "read-only policy — use the standard SDK Edit/Write tools "
            "instead, or restructure the command to avoid those paths."
        )
    return None


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _build_embed(
    *,
    uuid: str,
    reason: str,
    command: str,
    cwd: str,
    approval_timeout_s: int,
    command_timeout_s: int,
) -> tuple[discord.Embed, discord.File | None]:
    """Build the approval embed. Returns (embed, optional file attachment).

    If the command exceeds EMBED_COMMAND_PREVIEW_CHARS, the embed shows a
    preview and the full command is attached as a .txt file so the user can
    review it before approving.
    """
    if len(command) > EMBED_COMMAND_PREVIEW_CHARS:
        cmd_field = command[:EMBED_COMMAND_PREVIEW_CHARS] + "\n… (truncated; see attached file)"
        attachment = discord.File(
            fp=io.BytesIO(command.encode("utf-8")),
            filename=f"elevation-{uuid}.ps1.txt",
        )
    else:
        cmd_field = command
        attachment = None

    embed = discord.Embed(
        title="Admin elevation requested",
        color=discord.Color.orange(),
        description=f"**Reason:** {reason}",
    )
    embed.add_field(
        name="Command",
        value=f"```powershell\n{cmd_field}\n```"[:1024],
        inline=False,
    )
    embed.add_field(name="cwd", value=f"`{cwd or '(broker default)'}`", inline=True)
    embed.add_field(
        name="Timeouts",
        value=f"approval {approval_timeout_s}s · command {command_timeout_s}s",
        inline=True,
    )
    embed.add_field(name="Executor", value="domo-elevation-broker (LocalSystem)", inline=True)
    embed.add_field(name="Request", value=f"`{uuid}`", inline=False)
    embed.set_footer(
        text=(
            "⚠ Elevation bypasses the domo vault guard. "
            "Confirm this command does not write to raw/ or agents/."
        )
    )
    return embed, attachment


# --------------------------- broker IPC ---------------------------

def _talk_to_broker_blocking(uuid: str, *, total_timeout_s: int) -> dict[str, Any]:
    """Open the named pipe, send the request_id, read one JSON response.

    Runs synchronously; callers should wrap in `asyncio.to_thread`. Returns a
    dict with at least `status` ('executed' | 'broker_error') and the broker's
    payload fields, or raises on transport failure.
    """
    # Lazy import — pywin32 is Windows-only and the test suite must be able to
    # import this module on any platform.
    import pywintypes
    import win32file
    import win32pipe
    import winerror

    handle = None
    last_exc: Exception | None = None
    for attempt in range(PIPE_CONNECT_RETRIES):
        try:
            # WaitNamedPipe so we don't error out if the broker is momentarily
            # busy with another client. 1 second is generous for a short IPC.
            try:
                win32pipe.WaitNamedPipe(BROKER_PIPE_NAME, 1000)
            except pywintypes.error as e:
                # ERROR_FILE_NOT_FOUND means the broker isn't running at all.
                if e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                    raise FileNotFoundError(
                        f"broker pipe not found ({BROKER_PIPE_NAME}); "
                        "is the domo-elevation-broker service running?"
                    ) from e
                raise
            handle = win32file.CreateFile(
                BROKER_PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            break
        except FileNotFoundError:
            raise
        except pywintypes.error as e:
            last_exc = e
            if attempt + 1 < PIPE_CONNECT_RETRIES:
                import time
                time.sleep(PIPE_CONNECT_BACKOFF_S)
                continue
            raise OSError(f"broker pipe connect failed after retries: {e}") from e

    assert handle is not None
    try:
        payload = (json.dumps({"request_id": uuid}) + "\n").encode("utf-8")
        win32file.WriteFile(handle, payload)

        # Read until newline. Broker writes one JSON object terminated by \n.
        buf = bytearray()
        deadline = total_timeout_s + 5  # generous; broker enforces command_timeout itself
        # win32file.ReadFile is blocking but the broker should respond promptly.
        # We rely on the surrounding asyncio.wait_for in the caller to bound
        # total time; this just streams bytes.
        while b"\n" not in bytes(buf):
            try:
                _, chunk = win32file.ReadFile(handle, 65536)
            except pywintypes.error as e:
                # Broker closed the pipe; if we have data, parse it; else fail.
                if buf:
                    break
                raise OSError(f"broker pipe read failed: {e}") from e
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > 1_000_000:
                raise OSError("broker response exceeded 1MB; aborting")
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace")
        return json.loads(line)
    finally:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass


async def _talk_to_broker(uuid: str, command_timeout_s: int) -> dict[str, Any]:
    """Async wrapper around the blocking pipe IPC. Caller must enforce a
    higher-level timeout via asyncio.wait_for if desired."""
    total = command_timeout_s + 10  # broker's own command timeout + IPC overhead
    return await asyncio.to_thread(_talk_to_broker_blocking, uuid, total_timeout_s=total)


# --------------------------- MCP context + tool ---------------------------

class ElevationContext:
    def __init__(
        self,
        *,
        bots: dict[str, Any],   # dict[str, AgentBot]; typed loosely to avoid cycle
        store: ElevationStore,
    ):
        self.bots = bots
        self.store = store

    def as_mcp_server(self):
        ctx = self

        @tool(
            "request_admin_elevation",
            (
                "Request Windows Administrator elevation for a single command. "
                "Posts an Approve/Deny prompt in the operator's Discord channel "
                "and waits for the user's decision. On approval, an elevated "
                "broker service runs the command and returns its captured "
                "exit_code, stdout, and stderr. On deny, timeout, or broker "
                "error, returns a 'not executed' error. Use sparingly — only "
                "when an unprivileged shell would fail and there is no "
                "user-scope alternative. Defaults: approval_timeout_s=120, "
                "command_timeout_s=120 (sum must stay <= ~540s). Pass cwd as "
                "an empty string to use the broker default. NEVER use this to "
                "write to raw/ or agents/ — those folders are read-only by "
                "design and elevation does not change that policy."
            ),
            {
                "reason": str,
                "command": str,
                "cwd": str,
                "approval_timeout_s": int,
                "command_timeout_s": int,
            },
        )
        async def request_admin_elevation(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx._handle(args)

        return create_sdk_mcp_server(
            name="elevation",
            version="0.1.0",
            tools=[request_admin_elevation],
        )

    async def _handle(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = (args.get("reason") or "").strip()
        command = args.get("command") or ""
        cwd = (args.get("cwd") or "").strip()
        # Use defaults only when the field is missing/empty — not when the caller
        # explicitly passed 0. An explicit 0 is wrong (and probably a bug in the
        # caller); fall through to _validate_timeouts to surface a clear error.
        raw_approval = args.get("approval_timeout_s")
        if raw_approval is None or raw_approval == "":
            approval_timeout_s = DEFAULT_APPROVAL_TIMEOUT_S
        else:
            approval_timeout_s = int(raw_approval)
        raw_command_to = args.get("command_timeout_s")
        if raw_command_to is None or raw_command_to == "":
            command_timeout_s = DEFAULT_COMMAND_TIMEOUT_S
        else:
            command_timeout_s = int(raw_command_to)

        if not reason:
            return _err("reason is required")
        if not command.strip():
            return _err("command is required")

        timeout_err = _validate_timeouts(approval_timeout_s, command_timeout_s)
        if timeout_err:
            return _err(timeout_err)

        precheck = _vault_write_precheck(command)
        if precheck:
            return _err(precheck)

        spec = AGENTS.get("main")
        if spec is None or spec.channel_id is None:
            return _err("operator agent has no channel configured; cannot post approval prompt")
        bot = self.bots.get("main")
        if bot is None:
            return _err("operator bot not running; cannot post approval prompt")

        uuid = str(_uuid_mod.uuid4())
        sha = _compute_sha256(command)
        self.store.create(
            uuid=uuid,
            reason=reason,
            command=command,
            command_sha256=sha,
            cwd=cwd or None,
            requested_by="main",
            approval_timeout_s=approval_timeout_s,
            command_timeout_s=command_timeout_s,
        )

        # Pre-create the event so the button handler and our await use the
        # same instance regardless of click/await ordering.
        event = self.store.event_for(uuid)

        embed, file = _build_embed(
            uuid=uuid,
            reason=reason,
            command=command,
            cwd=cwd,
            approval_timeout_s=approval_timeout_s,
            command_timeout_s=command_timeout_s,
        )
        view = ElevationView(uuid, self.store)

        try:
            channel = bot.get_channel(spec.channel_id) or await bot.fetch_channel(spec.channel_id)
            send_kwargs: dict[str, Any] = {"embed": embed, "view": view}
            if file is not None:
                send_kwargs["file"] = file
            msg = await channel.send(**send_kwargs)
        except Exception as e:
            log.exception("elevation: failed to post approval embed")
            self.store.forget_event(uuid)
            return _err(f"failed to post approval embed in operator channel: {e}")

        self.store.attach_message(uuid, channel_id=spec.channel_id, message_id=msg.id)

        jump_url = msg.jump_url

        # --- await user's decision ---
        try:
            await asyncio.wait_for(event.wait(), timeout=approval_timeout_s)
        except asyncio.TimeoutError:
            self.store.mark_timed_out(uuid)
            self.store.forget_event(uuid)
            await self._render_timeout_state(msg, uuid, approval_timeout_s)
            return _err(
                f"Elevation timed out — no user decision within {approval_timeout_s}s. "
                f"The Approve/Deny embed is at {jump_url} (buttons now disabled). "
                "Task blocked; do not retry without user direction."
            )

        # Event was set. Re-read the row to learn the resolution.
        row = self.store.get(uuid)
        self.store.forget_event(uuid)

        if row is None:
            return _err(f"internal error: elevation row {uuid} disappeared after approval")

        if row["status"] == "denied":
            return _err(
                f"Elevation denied by user — task blocked. The denied embed is at "
                f"{jump_url}. Do not retry; discuss with the user before re-issuing."
            )

        if row["status"] != "approved":
            # Race or restart-induced expiration after our event woke us.
            return _err(
                f"Elevation resolved as {row['status']!r} — not executed. "
                f"Embed at {jump_url}."
            )

        # --- approved: hand off to broker ---
        try:
            response = await asyncio.wait_for(
                _talk_to_broker(uuid, command_timeout_s),
                timeout=command_timeout_s + 15,
            )
        except FileNotFoundError as e:
            self.store.mark_executed(
                uuid, exit_code=-1, stdout="", stderr="", error="broker_unavailable"
            )
            return _err(
                f"Elevated execution failed — broker not reachable: {e} "
                f"(approved embed: {jump_url})"
            )
        except asyncio.TimeoutError:
            self.store.mark_executed(
                uuid, exit_code=-1, stdout="", stderr="", error="broker_timeout"
            )
            return _err(
                f"Elevated execution exceeded command_timeout_s + 15s overhead "
                f"({command_timeout_s + 15}s) without broker response. "
                f"Command may still have run; check the SQLite row. "
                f"(approved embed: {jump_url})"
            )
        except Exception as e:
            log.exception("elevation: broker IPC failure")
            self.store.mark_executed(
                uuid, exit_code=-1, stdout="", stderr="", error=f"ipc_error:{e}"
            )
            return _err(
                f"Elevated execution failed — broker IPC error: {e} "
                f"(approved embed: {jump_url})"
            )

        # Broker responded. It already updated the row; we just render.
        if response.get("status") == "broker_error":
            return _err(
                f"Broker rejected the command: {response.get('error', 'unknown')} "
                f"(approved embed: {jump_url})"
            )

        exit_code = int(response.get("exit_code", -1))
        stdout = response.get("stdout", "") or ""
        stderr = response.get("stderr", "") or ""
        body = (
            f"Exit {exit_code}\n\n"
            f"STDOUT:\n{stdout if stdout else '(empty)'}\n\n"
            f"STDERR:\n{stderr if stderr else '(empty)'}\n\n"
            f"(embed: {jump_url})"
        )
        return _err(body) if exit_code != 0 else _ok(body)

    async def _render_timeout_state(
        self, msg: discord.Message, uuid: str, approval_timeout_s: int
    ) -> None:
        """Edit the original embed in place: preserve all fields, switch to a
        TIMED OUT banner + grey color, leave the buttons attached but disabled.

        Replacing the embed (the previous implementation) made the timeout state
        easy to miss — a plain grey block with no buttons looks like any other
        bot message. Preserving fields + disabled buttons keeps the original
        request visible so the user can see what they missed approving.
        """
        try:
            original = msg.embeds[0] if msg.embeds else None
            if original is None:
                return
            overlay = discord.Embed(
                title=f"⏱ {original.title or 'Admin elevation'} — TIMED OUT",
                color=discord.Color.dark_grey(),
                description=(
                    f"**No decision within {approval_timeout_s}s.** "
                    "Buttons disabled.\n\n" + (original.description or "")
                ).rstrip(),
            )
            for f in original.fields:
                overlay.add_field(name=f.name, value=f.value, inline=f.inline)
            if original.footer and original.footer.text:
                overlay.set_footer(text=original.footer.text)
            disabled_view = ElevationView(uuid, self.store)
            for child in disabled_view.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await msg.edit(embed=overlay, view=disabled_view)
        except Exception:
            # Best-effort: row is already marked expired and the tool will
            # still return the proper error to Maine. Don't let an embed-edit
            # hiccup mask the real result.
            log.exception("elevation: timeout embed edit failed")
