"""Vault browser path-traversal safety + dir/file dispatch."""

from __future__ import annotations

import pytest

from src.config import VAULT_ROOT
from src.web.vault_fs import VISIBLE_TOP_LEVEL, list_dir, read_text, safe_resolve


def test_root_resolves():
    vp = safe_resolve("")
    assert vp.is_dir
    assert vp.relative == ""
    assert vp.absolute == VAULT_ROOT.resolve()


def test_known_subdir_resolves():
    vp = safe_resolve("wiki")
    assert vp.is_dir
    assert vp.relative == "wiki"


def test_dotdot_traversal_rejected():
    with pytest.raises(ValueError, match="traversal"):
        safe_resolve("../etc/passwd")


def test_dotdot_buried_rejected():
    with pytest.raises(ValueError, match="traversal"):
        safe_resolve("wiki/../../etc/passwd")


def test_absolute_path_rejected():
    with pytest.raises(ValueError, match="traversal"):
        safe_resolve("/etc/passwd")


def test_windows_drive_letter_does_not_escape():
    # If the user passes "C:\\Users\\..." we lstrip /, but the drive letter
    # makes the join produce a non-vault-root path which should be caught.
    with pytest.raises(ValueError):
        safe_resolve("C:\\Windows\\System32\\config")


def test_root_listing_only_shows_whitelisted():
    vp = safe_resolve("")
    entries = list_dir(vp)
    names = {e["name"] for e in entries}
    # Only shows whitelisted top-level dirs that actually exist
    for n in names:
        assert n in VISIBLE_TOP_LEVEL


def test_subdir_listing_unrestricted():
    """Below the root we don't apply the top-level whitelist."""
    vp = safe_resolve("wiki")
    entries = list_dir(vp)
    # Should see at least concepts/ or projects/ or similar
    assert len(entries) > 0


def test_read_existing_file():
    vp = safe_resolve("CLAUDE.md")
    content = read_text(vp)
    assert "Second Brain" in content


def test_read_directory_rejected():
    vp = safe_resolve("wiki")
    with pytest.raises(ValueError, match="not a file"):
        read_text(vp)


def test_nonexistent_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        safe_resolve("does/not/exist.md")


def test_hidden_files_not_shown():
    """The .obsidian / .git / etc. should never appear in listings."""
    vp = safe_resolve("")
    entries = list_dir(vp)
    names = {e["name"] for e in entries}
    assert ".obsidian" not in names
    assert ".git" not in names
