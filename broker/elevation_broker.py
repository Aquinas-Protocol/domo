"""Elevated execution broker for domo.

Runs as its own Windows service installed under LocalSystem (or any account
in the Administrators group with `--RunHighestPrivileges`). The domo
main service stays at its current trust level and posts elevation requests
to the broker over a Windows named pipe after Discord-side approval lands.

Protocol (line-delimited JSON, one request/response per connection):

    bot   →  broker:  {"request_id": "<uuid>"}
    broker→  bot:     {"status": "executed",
                       "exit_code": 0, "stdout": "...", "stderr": "..."}
                  -- or --
                      {"status": "broker_error",
                       "error": "<reason>"}

The bot's payload carries ONLY the uuid. The broker re-reads the canonical
command from `elevation_requests` (status='approved', sha256 matches) and
runs that. The bot can't lie over the pipe to bypass approval.

Deny list is small, conservative, and non-overridable. Editing it requires a
code change + service restart. Intentional — the deny list is a security
boundary, not a configuration knob.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

# Allow `python broker/elevation_broker.py` to import from src/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.elevation_store import ElevationStore  # noqa: E402

PIPE_NAME = r"\\.\pipe\domo-elevation-broker"
LOG_DIR = _REPO_ROOT / "data" / "broker"
LOG_FILE = LOG_DIR / "broker.log"
MAX_OUTPUT_BYTES = 8192

# Catastrophic operations no Discord approval should authorise. Order matters
# only for debug logging — first match wins. Keep this list short and
# conservative; the user is the security boundary, not these patterns.
DENY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("disk_format",
     re.compile(r"\bformat\s+[A-Za-z]:", re.IGNORECASE)),
    ("system_dir_removal",
     re.compile(r"Remove-Item.{0,200}C:\\Windows(\\|\b)", re.IGNORECASE)),
    ("system_hive_delete",
     re.compile(r"\breg\b.*\bdelete\b.*HKLM\\SYSTEM", re.IGNORECASE)),
    ("system_hive_delete_alt",
     re.compile(r"Remove-Item.*HKLM:\\SYSTEM", re.IGNORECASE)),
]

log = logging.getLogger("domo.broker")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # NSSM redirects stdout to a cp1252 pipe; force UTF-8 so emit() survives non-ASCII records.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    root.addHandler(logging.StreamHandler(sys.stdout))


def _check_deny_list(command: str) -> str | None:
    for label, pattern in DENY_PATTERNS:
        if pattern.search(command):
            return label
    return None


def _truncate(text: str, *, limit: int = MAX_OUTPUT_BYTES) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    head = encoded[: limit - 80]
    return head.decode("utf-8", errors="replace") + (
        f"\n… [truncated; {len(encoded) - len(head)} more bytes elided]"
    )


CommandRunner = Callable[[str, str | None, int], Awaitable[tuple[int, str, str]]]


async def _run_powershell(
    command: str, cwd: str | None, timeout_s: int
) -> tuple[int, str, str]:
    """Spawn powershell as a child of the (already-elevated) broker process,
    capture stdout/stderr, enforce timeout. Returns (exit_code, stdout, stderr).
    On timeout, kills the process group and returns exit_code=-1 with a
    diagnostic stderr."""
    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        command,
        cwd=cwd or None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:
            stdout_b, stderr_b = b"", b""
        return -1, stdout_b.decode("utf-8", errors="replace"), (
            stderr_b.decode("utf-8", errors="replace")
            + f"\n[broker: command exceeded {timeout_s}s timeout — killed]"
        )
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def handle_request(
    payload: dict[str, Any],
    store: ElevationStore,
    *,
    command_runner: CommandRunner = _run_powershell,
) -> dict[str, Any]:
    """Validate and execute one elevation request. Returns the JSON response
    dict to ship back over the pipe. Updates the SQLite row to `executed` or
    `broker_error` regardless of outcome.

    Pure (no pipe I/O). The pipe layer is `serve()`.
    """
    uuid = (payload.get("request_id") or "").strip()
    if not uuid:
        log.warning("broker: rejected payload with empty request_id")
        return {"status": "broker_error", "error": "missing request_id"}

    row = store.get(uuid)
    if row is None:
        log.warning("broker: request_id %s not found", uuid)
        return {"status": "broker_error", "error": "unknown request_id"}

    if row["status"] != "approved":
        log.warning(
            "broker: request_id %s status=%s, refusing (must be 'approved')",
            uuid, row["status"],
        )
        return {
            "status": "broker_error",
            "error": f"row status is {row['status']!r}, not 'approved'",
        }

    command = row["command"]
    expected_sha = row["command_sha256"]
    actual_sha = hashlib.sha256(command.encode("utf-8")).hexdigest()
    if actual_sha != expected_sha:
        log.error(
            "broker: request_id %s sha mismatch (stored %s vs actual %s) — refusing",
            uuid, expected_sha, actual_sha,
        )
        store.mark_executed(
            uuid, exit_code=-1, stdout="", stderr="", error="sha256_mismatch"
        )
        return {"status": "broker_error", "error": "command_sha256 mismatch"}

    deny_label = _check_deny_list(command)
    if deny_label:
        log.error("broker: request_id %s blocked by deny pattern %r", uuid, deny_label)
        store.mark_executed(
            uuid, exit_code=-1, stdout="", stderr="",
            error=f"deny_list:{deny_label}",
        )
        return {
            "status": "broker_error",
            "error": f"command matches deny pattern {deny_label!r}",
        }

    cwd = row["cwd"]
    command_timeout_s = int(row["command_timeout_s"])

    log.info(
        "broker: executing request_id=%s reason=%r cwd=%r timeout=%ds",
        uuid, row["reason"], cwd, command_timeout_s,
    )
    try:
        exit_code, stdout, stderr = await command_runner(
            command, cwd, command_timeout_s
        )
    except Exception as e:
        log.exception("broker: subprocess spawn failed for %s", uuid)
        store.mark_executed(
            uuid, exit_code=-1, stdout="", stderr=str(e), error=f"spawn_error:{e}"
        )
        return {"status": "broker_error", "error": f"spawn_error: {e}"}

    stdout_t = _truncate(stdout)
    stderr_t = _truncate(stderr)
    store.mark_executed(
        uuid, exit_code=exit_code, stdout=stdout_t, stderr=stderr_t, error=None
    )
    log.info(
        "broker: completed request_id=%s exit=%d stdout=%dB stderr=%dB",
        uuid, exit_code, len(stdout_t), len(stderr_t),
    )
    return {
        "status": "executed",
        "exit_code": exit_code,
        "stdout": stdout_t,
        "stderr": stderr_t,
    }


# --------------------------- pipe server ---------------------------

async def _serve_one_connection(handle: Any, store: ElevationStore) -> None:
    """Read one JSON line, dispatch, write one JSON line, close.

    Lazy-imports pywin32 so the module can be imported on non-Windows for
    testing handle_request in isolation.
    """
    import pywintypes
    import win32file

    try:
        # Read until newline. Bot writes a single short JSON object terminated
        # by \n; in practice this lands in one ReadFile.
        buf = bytearray()
        while b"\n" not in bytes(buf):
            try:
                _, chunk = win32file.ReadFile(handle, 65536)
            except pywintypes.error as e:
                log.warning("broker: read failed: %s", e)
                return
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > 1_000_000:
                log.warning("broker: incoming request exceeded 1MB; closing")
                return
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("broker: invalid JSON request: %s", e)
            response: dict[str, Any] = {"status": "broker_error", "error": f"invalid JSON: {e}"}
        else:
            response = await handle_request(payload, store)
        out = (json.dumps(response) + "\n").encode("utf-8")
        try:
            win32file.WriteFile(handle, out)
        except pywintypes.error as e:
            log.warning("broker: write failed: %s", e)
    finally:
        try:
            import win32pipe
            win32pipe.DisconnectNamedPipe(handle)
        except Exception:
            pass
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass


async def serve() -> None:
    """Main pipe loop. Creates the named pipe, accepts connections, hands each
    off to `_serve_one_connection`. Runs forever; killed by service stop."""
    import pywintypes
    import win32file
    import win32pipe

    store = ElevationStore()
    log.info("broker: starting; pipe=%s db=%s", PIPE_NAME, store.db_path)

    while True:
        # PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED could give us proper async,
        # but blocking-mode + asyncio.to_thread per accept is simpler and fine
        # for the expected request rate (single-digit per day).
        handle = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            65536, 65536,
            0,
            None,    # FIXME: tighten ACL — for v1 this inherits the broker's
                     # default DACL (LocalSystem service → Administrators+SYSTEM).
                     # Personal-use single-user machines are fine; multi-user
                     # boxes should set a per-bot-account DACL via SECURITY_ATTRIBUTES.
        )
        try:
            await asyncio.to_thread(win32pipe.ConnectNamedPipe, handle, None)
        except pywintypes.error as e:
            log.warning("broker: ConnectNamedPipe failed: %s", e)
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            continue
        # Process this connection concurrently so a slow request doesn't block
        # the next one.
        asyncio.create_task(_serve_one_connection(handle, store))


def main() -> int:
    parser = argparse.ArgumentParser(description="domo elevation broker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="serve a single connection then exit (debug only)",
    )
    args = parser.parse_args()

    _setup_logging()
    log.info("broker: deny patterns loaded: %s", [label for label, _ in DENY_PATTERNS])

    if args.once:
        # Debug helper: serve one connection, useful when iterating manually.
        async def _one():
            store = ElevationStore()
            import pywintypes
            import win32file
            import win32pipe
            handle = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT,
                1, 65536, 65536, 0, None,
            )
            try:
                await asyncio.to_thread(win32pipe.ConnectNamedPipe, handle, None)
            except pywintypes.error as e:
                log.error("broker --once: connect failed: %s", e)
                win32file.CloseHandle(handle)
                return
            await _serve_one_connection(handle, store)
        asyncio.run(_one())
    else:
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            log.info("broker: interrupted; shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
