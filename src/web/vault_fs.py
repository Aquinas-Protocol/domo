"""Read-only vault navigation for the dashboard browser.

Every path the user requests is resolved with `Path().resolve()` and checked
to be inside `VAULT_ROOT.resolve()`. Anything else returns 403. Symlinks
inside the vault still resolve to under VAULT_ROOT (junction is a special
case — `second-brain-bot` resolves to `second-brain` which IS the vault root).

Tests in tests/test_vault_browser.py exercise the deny paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config import VAULT_ROOT

# Folders that are visible in the dashboard tree.
VISIBLE_TOP_LEVEL = {"wiki", "raw", "content", "journal", "agents"}

# Hide noise from the tree (caches, hidden dirs, etc.)
HIDDEN_NAMES = {".obsidian", ".trash", ".git", "__pycache__", ".DS_Store"}


@dataclass(frozen=True)
class VaultPath:
    """Validated path inside the vault. Construct via `safe_resolve`."""
    relative: str
    absolute: Path
    is_dir: bool


def safe_resolve(rel: str) -> VaultPath:
    """Resolve `rel` (a vault-relative POSIX path) to an absolute path
    guaranteed to be under VAULT_ROOT. Raises ValueError on traversal.

    Examples:
        ""              -> vault root
        "wiki"          -> wiki/ folder
        "wiki/log.md"   -> file
        "../etc/passwd" -> ValueError (traversal)
        "/abs/path"     -> ValueError (must be relative)
    """
    rel = (rel or "").strip().replace("\\", "/")
    if rel.startswith("/") or rel.startswith(".."):
        raise ValueError(f"path traversal rejected: {rel!r}")
    if ".." in rel.split("/"):
        raise ValueError(f"path traversal rejected: {rel!r}")

    root = VAULT_ROOT.resolve()
    candidate = (root / rel).resolve() if rel else root
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes vault root: {rel!r}")

    if not candidate.exists():
        raise FileNotFoundError(rel)

    return VaultPath(
        relative=rel,
        absolute=candidate,
        is_dir=candidate.is_dir(),
    )


def list_dir(vp: VaultPath) -> list[dict]:
    """Return tree-friendly entries for a directory: name, is_dir, rel."""
    if not vp.is_dir:
        raise ValueError("not a directory")

    # At root, restrict to whitelisted top-level folders so we don't expose
    # things like the .obsidian folder or pasted images at vault root.
    if vp.relative == "":
        allowed = VISIBLE_TOP_LEVEL
    else:
        allowed = None

    entries: list[dict] = []
    for child in sorted(vp.absolute.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in HIDDEN_NAMES or child.name.startswith("."):
            continue
        if allowed is not None and child.name not in allowed:
            continue
        rel = child.relative_to(VAULT_ROOT.resolve()).as_posix()
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "rel": rel,
        })
    return entries


def read_text(vp: VaultPath, max_bytes: int = 256_000) -> str:
    """Read a file's contents as UTF-8 text. Caps size at 256KB by default
    so the dashboard never tries to render a 50MB blob."""
    if vp.is_dir:
        raise ValueError("not a file")
    size = vp.absolute.stat().st_size
    if size > max_bytes:
        head = vp.absolute.read_bytes()[:max_bytes]
        return head.decode("utf-8", errors="replace") + f"\n\n…\n[truncated at {max_bytes} bytes; full size {size}]"
    return vp.absolute.read_text(encoding="utf-8", errors="replace")
