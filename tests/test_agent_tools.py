"""Tests for the filesystem sandbox — no external services required."""

import os
import pytest

from modules.agent_tools import _safe_path, ToolError
from constants import WORKSPACE_ROOT


def test_safe_path_accepts_workspace_files():
    p = _safe_path("modules/prompter.py")
    assert p.startswith(os.path.abspath(WORKSPACE_ROOT))


def test_safe_path_accepts_backslash_input():
    p = _safe_path("modules\\prompter.py")
    # On Windows, backslashes are path separators; the resolved file
    # should still live inside the workspace.
    assert os.path.abspath(WORKSPACE_ROOT) in p


def test_safe_path_rejects_parent_traversal():
    with pytest.raises(ToolError):
        _safe_path("../etc/passwd")
    with pytest.raises(ToolError):
        _safe_path("../../..")


def test_safe_path_rejects_absolute_outside_root():
    with pytest.raises(ToolError):
        _safe_path("C:/Windows/System32")
    with pytest.raises(ToolError):
        _safe_path("/etc/passwd")


def test_safe_path_strips_quotes():
    p = _safe_path("'modules/memory.py'")
    assert p.endswith("memory.py")

    p2 = _safe_path('"modules/memory.py"')
    assert p2.endswith("memory.py")


def test_safe_path_accepts_dot_as_root():
    p = _safe_path(".")
    assert os.path.isdir(p)
    assert os.path.abspath(p) == os.path.abspath(WORKSPACE_ROOT)


def test_safe_path_accepts_empty_as_root():
    p = _safe_path("")
    assert os.path.abspath(p) == os.path.abspath(WORKSPACE_ROOT)


def test_safe_path_strips_leading_slashes():
    # A user-supplied path like "/modules/memory.py" should resolve
    # inside the workspace, not at filesystem root.
    p = _safe_path("/modules/memory.py")
    assert p.startswith(os.path.abspath(WORKSPACE_ROOT))