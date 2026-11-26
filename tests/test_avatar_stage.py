import os

import pytest

from modules import avatar_stage as av


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "avatars"
    d.mkdir()
    monkeypatch.setattr(av, "FOLDER", str(d))
    return d


def test_no_file_means_no_model(folder):
    assert av.model_size() is None
    assert av.status() == {"model": False, "file": "lumi.vrm"}


def test_an_empty_file_is_not_a_model(folder):
    (folder / "lumi.vrm").write_bytes(b"")
    assert av.model_size() is None


def test_a_real_file_is_a_model(folder):
    (folder / "lumi.vrm").write_bytes(b"glTF" + b"\0" * 64)
    assert av.model_size() == 68
    assert av.status()["model"] is True


def test_the_file_name_is_fixed_so_no_request_can_pick_a_path():
    assert os.path.basename(av.model_path()) == "lumi.vrm"


def test_the_model_is_not_on_the_remote_allow_list():
    from modules import socketIO
    assert not any("/api/avatar" in str(x) for x in socketIO._REMOTE_API_ALLOW)


# ─── Live2D ─────────────────────────────────────────────────────────

import json as _json


@pytest.fixture
def live2d(tmp_path, monkeypatch):
    cfg = tmp_path / "avatars"
    cfg.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    (model / "demo.model3.json").write_text("{}", encoding="utf-8")
    (model / "demo.moc3").write_bytes(b"moc")
    (model / "tex").mkdir()
    (model / "tex" / "t0.png").write_bytes(b"png")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(av, "FOLDER", str(cfg))
    monkeypatch.setattr(av, "CORE_FILE", str(tmp_path / "live2dcubismcore.min.js"))

    def configure(folder):
        (cfg / "live2d.json").write_text(_json.dumps({"folder": folder}), encoding="utf-8")
    return model, configure, tmp_path


def test_no_config_means_no_live2d_model(live2d):
    model, configure, tmp = live2d
    assert av.live2d_folder() is None
    assert av.live2d_status() == {"model": False, "entry": "", "core": False}


def test_a_folder_without_a_model3_file_is_not_a_model(live2d, tmp_path):
    model, configure, tmp = live2d
    empty = tmp_path / "empty"
    empty.mkdir()
    configure(str(empty))
    assert av.live2d_folder() is None


def test_a_configured_model_is_found_with_its_entry_file(live2d):
    model, configure, tmp = live2d
    configure(str(model))
    assert av.live2d_folder() == str(model)
    assert av.live2d_status() == {"model": True, "entry": "demo.model3.json", "core": False}


def test_the_core_file_is_reported_only_when_present(live2d):
    model, configure, tmp = live2d
    (tmp / "live2dcubismcore.min.js").write_text("//core", encoding="utf-8")
    assert av.core_present() is True


def test_files_inside_the_model_folder_are_served(live2d):
    model, configure, tmp = live2d
    assert av.safe_file(str(model), "demo.model3.json") == os.path.realpath(model / "demo.model3.json")
    assert av.safe_file(str(model), "tex/t0.png") is not None


def test_files_outside_the_model_folder_are_refused(live2d):
    model, configure, tmp = live2d
    assert av.safe_file(str(model), "../outside.txt") is None
    assert av.safe_file(str(model), str(tmp / "outside.txt")) is None
    assert av.safe_file(str(model), r"..\outside.txt") is None


def test_only_model_extensions_are_served(live2d):
    model, configure, tmp = live2d
    (model / "run.exe").write_bytes(b"MZ")
    assert av.safe_file(str(model), "run.exe") is None
    assert av.safe_file(str(model), "demo.moc3") is not None


def test_the_live2d_routes_are_host_only():
    from modules import socketIO
    assert not any("/api/avatar" in str(x) for x in socketIO._REMOTE_API_ALLOW)
