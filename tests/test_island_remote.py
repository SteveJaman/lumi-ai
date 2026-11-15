"""Remote island pairing: off by default, Tailscale-only, code shown once and
stored hashed, wrong codes lock an address out, off/new code revoke it.
State lives in a temp file (conftest points STATE_PATH there)."""

import json
import re

import pytest

from modules import island_remote as ir

TS = "100.101.102.103"


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    monkeypatch.setattr(ir, "tailscale_ip", lambda: TS)


def test_off_by_default():
    assert ir.is_enabled() is False and ir.bind_host() is None
    assert ir.verify("ABCD-EFGH-JKMN-PQRS", "100.1.2.3") is False


def test_enable_returns_a_readable_code_once_and_stores_only_a_hash():
    out = ir.enable()
    code = out["code"]
    assert out["host"] == TS and re.fullmatch(r"([A-Z2-9]{4}-){3}[A-Z2-9]{4}", code)
    assert not set(code.replace("-", "")) & set("01OIL")                # nothing easy to misread
    raw = open(ir.STATE_PATH, encoding="utf-8").read()
    assert code not in raw and code.replace("-", "") not in raw
    saved = json.loads(raw)
    assert saved["enabled"] and saved["host"] == TS and len(saved["hash"]) == 64 and saved["salt"]
    assert ir.is_enabled() and ir.bind_host() == TS


def test_status_never_contains_the_code_or_hash():
    code = ir.enable()["code"]
    st = ir.status()
    assert st["enabled"] and st["host"] == TS and st["tailscale_ip"] == TS and isinstance(st["port"], int)
    blob = json.dumps(st)
    assert code not in blob and json.load(open(ir.STATE_PATH))["hash"] not in blob


@pytest.mark.parametrize("variant", [lambda c: c, lambda c: c.lower(), lambda c: c.replace("-", ""),
                                     lambda c: " " + c.replace("-", " ") + " "])
def test_the_code_is_accepted_however_it_was_typed(variant):
    code = ir.enable()["code"]
    assert ir.verify(variant(code), "100.1.2.3") is True


@pytest.mark.parametrize("bad", ["", None, 12345, "ABCD-EFGH-JKMN-PQRS", "-" * 19, {"code": "x"}])
def test_wrong_or_junk_codes_fail(bad):
    ir.enable()
    assert ir.verify(bad, "100.1.2.3") is False


def test_five_wrong_codes_lock_that_address_only():
    code = ir.enable()["code"]
    for _ in range(ir.MAX_FAILURES):
        assert ir.verify("WRNG-WRNG-WRNG-WRNG", "100.9.9.9") is False
    assert ir.locked("100.9.9.9")
    assert ir.verify(code, "100.9.9.9") is False                         # even the right code, while locked
    assert ir.verify(code, "100.8.8.8") is True                          # other devices are unaffected


def test_lockout_expires(monkeypatch):
    ir.enable()
    t = [1000.0]
    monkeypatch.setattr(ir.time, "time", lambda: t[0])
    for _ in range(ir.MAX_FAILURES):
        ir.verify("nope", "100.9.9.9")
    assert ir.locked("100.9.9.9")
    t[0] += max(ir.FAIL_WINDOW_S, ir.LOCKOUT_S) + 1
    assert not ir.locked("100.9.9.9")


def test_a_new_code_revokes_the_old_one():
    old = ir.enable()["code"]
    new = ir.enable()["code"]
    assert old != new
    assert ir.verify(old, "100.1.2.3") is False and ir.verify(new, "100.1.2.3") is True


def test_disable_revokes_everything():
    code = ir.enable()["code"]
    ir.disable()
    assert not ir.is_enabled() and ir.bind_host() is None
    assert ir.verify(code, "100.1.2.3") is False
    assert "hash" not in json.load(open(ir.STATE_PATH))


def test_never_listens_outside_tailscale(monkeypatch):
    for host in ("0.0.0.0", "192.168.1.20", "127.0.0.1", "8.8.8.8", "not-an-ip"):
        with pytest.raises(ir.RemoteError):
            ir.enable(host=host)
    monkeypatch.setattr(ir, "tailscale_ip", lambda: None)
    with pytest.raises(ir.RemoteError, match="Tailscale"):
        ir.enable()
    assert not ir.is_enabled()


def test_a_corrupt_state_file_means_off():
    with open(ir.STATE_PATH, "w") as f:
        f.write("{not json")
    assert ir.is_enabled() is False and ir.verify("x", "100.1.2.3") is False


def test_tests_never_touch_the_real_state_file():
    import os
    from constants import DATA_ROOT
    assert os.path.dirname(ir.STATE_PATH) != os.path.abspath(DATA_ROOT)
