from modules import agent_tools as at


def test_build_and_dependency_folders_are_skipped_by_search(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "WORKSPACE_ROOT", str(tmp_path))
    skipped = ("node_modules", "dist", "build", ".next", "env", ".venv")
    for d in skipped:
        (tmp_path / d).mkdir()
        (tmp_path / d / "hit.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.txt").write_text("needle here\n", encoding="utf-8")
    out = at.search_files("needle", str(tmp_path))
    assert "real.txt" in out
    for d in skipped:
        assert f"{d}" not in out
