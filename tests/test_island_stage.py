import base64
import json

from modules import avatar_stage, island_stage as ist


def _model(tmp_path, big=False):
    folder = tmp_path / "ariu"
    (folder / "tex").mkdir(parents=True)
    (folder / "ariu.model3.json").write_text(json.dumps({
        "FileReferences": {"Moc": "ariu.moc3", "Textures": ["tex/t0.png"], "Physics": "ariu.physics3.json"}
    }), encoding="utf-8")
    (folder / "ariu.moc3").write_bytes(b"moc")
    (folder / "ariu.physics3.json").write_text("{}", encoding="utf-8")
    (folder / "tex" / "t0.png").write_bytes(bytes(range(256)) * (300 if big else 1))
    (folder / "secret.exe").write_bytes(b"nope")          # not in the model, wrong extension
    return folder


def _point_at(monkeypatch, folder):
    monkeypatch.setattr(avatar_stage, "live2d_folder", lambda: str(folder))


def test_manifest_lists_the_model_and_its_references(tmp_path, monkeypatch):
    folder = _model(tmp_path)
    _point_at(monkeypatch, folder)
    m = ist.manifest()
    keys = {f["key"] for f in m["files"]}
    assert m["model"] is True and m["entry"] == "model/ariu.model3.json"
    assert {"model/ariu.model3.json", "model/ariu.moc3", "model/tex/t0.png", "model/ariu.physics3.json"} <= keys
    assert not any("secret" in k for k in keys)


def test_chunks_rebuild_the_file_exactly(tmp_path, monkeypatch):
    folder = _model(tmp_path, big=True)
    _point_at(monkeypatch, folder)
    want = (folder / "tex" / "t0.png").read_bytes()
    got, offset, pieces = b"", 0, 0
    while True:
        c = ist.read_chunk("model/tex/t0.png", offset)
        assert c is not None and c["size"] == len(want)
        raw = base64.b64decode(c["data"])
        assert len(raw) <= ist.CHUNK
        got += raw
        offset += len(raw)
        pieces += 1
        if c["done"]:
            break
    assert got == want and pieces > 1


def test_unknown_and_escaping_keys_are_refused(tmp_path, monkeypatch):
    folder = _model(tmp_path)
    _point_at(monkeypatch, folder)
    assert ist.read_chunk("model/../../etc/passwd", 0) is None
    assert ist.read_chunk("model/secret.exe", 0) is None
    assert ist.read_chunk("vendor/nope", 0) is None
    assert ist.read_chunk("model/ariu.moc3", "abc") is None           # a non-number offset is refused


def test_vendor_libraries_come_only_when_the_file_exists(tmp_path, monkeypatch):
    _point_at(monkeypatch, _model(tmp_path))
    core = tmp_path / "live2dcubismcore.min.js"
    core.write_text("// core", encoding="utf-8")
    monkeypatch.setattr(ist, "VENDOR", {"core": str(core), "pixi": str(tmp_path / "missing.js")})
    m = ist.manifest()
    assert m["core"] == avatar_stage.core_present()
    keys = {f["key"] for f in m["files"]}
    assert "vendor/core" in keys and "vendor/pixi" not in keys


def test_no_model_configured_means_nothing_to_fetch(monkeypatch):
    monkeypatch.setattr(avatar_stage, "live2d_folder", lambda: None)
    monkeypatch.setattr(ist, "VENDOR", {})
    m = ist.manifest()
    assert m == {"model": False, "entry": "", "core": avatar_stage.core_present(), "files": []}
