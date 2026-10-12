"""
Model manager for the web client (Phase 10.12).

Wraps the Ollama HTTP API:
  - /api/tags         → installed models
  - /api/ps           → currently loaded models (with VRAM)
  - /api/generate     → load / unload via keep_alive
  - /api/pull         → streaming pull (run in background thread)
  - /api/delete       → remove a model

Routing config
--------------
Reads and writes the model-routing constants in constants.py using a
regex line-patcher that preserves surrounding comments and formatting.
After writing to disk, rebinds the same names in every already-imported
consumer module (prompter, memory, self_model, session_state, heartbeat,
signals) so the change takes effect on the next turn without a restart.

Every write is validated (targets found, resulting file compiles) and
backed up to constants.py.bak before it lands.
"""

import os
import re
import json
import time
import shutil
import threading

import requests

from constants import (
    LLM_ENDPOINT,
    WORKSPACE_ROOT,
    MODEL_NAME,
    FAST_MODEL_NAME,
    FAST_MODEL_ENABLED,
    FAST_MODEL_NUM_CTX,
    MAIN_NUM_CTX,
    FAST_MODEL_KEEP_ALIVE,
)


_CONSTANTS_PATH = os.path.join(WORKSPACE_ROOT, "constants.py")

_EDITABLE_ROUTING = (
    "MODEL_NAME",
    "FAST_MODEL_NAME",
    "FAST_MODEL_ENABLED",
    "FAST_MODEL_NUM_CTX",
    "MAIN_NUM_CTX",
    "FAST_MODEL_KEEP_ALIVE",
)

# Which loaded modules hold their own reference to each constant.
# We have to patch each of these after writing the file, or they'll
# keep using the value captured at import time.
_CONSUMERS = {
    "MODEL_NAME": [
        "modules.prompter",
        "modules.memory",
        "modules.session_state",
        "modules.self_model",
        "modules.heartbeat",
        "signals",
    ],
    "FAST_MODEL_NAME": [
        "modules.prompter",
        "modules.memory",
        "modules.session_state",
        "modules.self_model",
    ],
    "FAST_MODEL_ENABLED": ["modules.prompter"],
    "FAST_MODEL_NUM_CTX": ["modules.prompter"],
    "MAIN_NUM_CTX": ["modules.prompter"],
    "FAST_MODEL_KEEP_ALIVE": ["modules.prompter"],
}

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/]+(?::[A-Za-z0-9._\-/]+)?$")
_KEEP_ALIVE_RE = re.compile(r"^(-1|\d+(s|m|h))$")


# ===============================================================
# Pull state
# ===============================================================
class _PullState:
    def __init__(self):
        self.active = False
        self.model = None
        self.status = ""
        self.digest = ""
        self.total = 0
        self.completed = 0
        self.error = None
        self.done = False
        self.started_at = 0.0
        self.finished_at = 0.0

    def reset(self, model_name):
        self.active = True
        self.model = model_name
        self.status = "starting"
        self.digest = ""
        self.total = 0
        self.completed = 0
        self.error = None
        self.done = False
        self.started_at = time.time()
        self.finished_at = 0.0

    def to_dict(self):
        pct = (self.completed / self.total * 100.0) if self.total > 0 else 0.0
        return {
            "active": self.active,
            "model": self.model,
            "status": self.status,
            "digest": self.digest,
            "total": self.total,
            "completed": self.completed,
            "percent": round(pct, 2),
            "error": self.error,
            "done": self.done,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ===============================================================
# Manager
# ===============================================================
class ModelManager:
    def __init__(self, endpoint=None):
        self.endpoint = (endpoint or LLM_ENDPOINT).rstrip("/")
        self._pull_lock = threading.Lock()
        self._pull_state = _PullState()
        self._pull_thread = None

    # -----------------------------------------------------------
    # Reads
    # -----------------------------------------------------------
    def _get(self, path, timeout=5):
        r = requests.get(f"{self.endpoint}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def list_installed(self):
        try:
            data = self._get("/api/tags")
        except Exception as e:
            return {"error": str(e), "models": []}
        out = []
        for m in data.get("models", []) or []:
            d = m.get("details") or {}
            out.append({
                "name": m.get("name") or m.get("model") or "",
                "size": int(m.get("size") or 0),
                "modified_at": m.get("modified_at") or "",
                "digest": m.get("digest") or "",
                "family": d.get("family") or "",
                "parameter_size": d.get("parameter_size") or "",
                "quantization_level": d.get("quantization_level") or "",
            })
        out.sort(key=lambda x: x["name"])
        return {"models": out}

    def list_loaded(self):
        try:
            data = self._get("/api/ps")
        except Exception as e:
            return {"error": str(e), "models": []}
        out = []
        for m in data.get("models", []) or []:
            d = m.get("details") or {}
            out.append({
                "name": m.get("name") or m.get("model") or "",
                "size": int(m.get("size") or 0),
                "size_vram": int(m.get("size_vram") or 0),
                "expires_at": m.get("expires_at") or "",
                "family": d.get("family") or "",
                "parameter_size": d.get("parameter_size") or "",
                "quantization_level": d.get("quantization_level") or "",
            })
        out.sort(key=lambda x: x["name"])
        return {"models": out}

    # -----------------------------------------------------------
    # Load / unload
    # -----------------------------------------------------------
    def preload(self, model_name):
        """Ask Ollama to load a model into VRAM and keep it warm."""
        try:
            r = requests.post(
                f"{self.endpoint}/api/generate",
                json={
                    "model": model_name,
                    "prompt": "",
                    "keep_alive": "30m",
                    "stream": False,
                },
                timeout=180,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, None
        except Exception as e:
            return False, str(e)

    def unload(self, model_name):
        """Ask Ollama to unload a model immediately."""
        try:
            r = requests.post(
                f"{self.endpoint}/api/generate",
                json={"model": model_name, "keep_alive": 0},
                timeout=20,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, None
        except Exception as e:
            return False, str(e)

    def delete_model(self, model_name):
        try:
            r = requests.delete(
                f"{self.endpoint}/api/delete",
                json={"name": model_name},
                timeout=20,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, None
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------
    # Pull
    # -----------------------------------------------------------
    def start_pull(self, model_name):
        model_name = (model_name or "").strip()
        if not model_name:
            return False, "Model name cannot be empty."
        if not _MODEL_NAME_RE.match(model_name):
            return False, ("Model name contains invalid characters. "
                           "Use letters, digits, dot, dash, underscore, "
                           "slash, and an optional ':tag'.")

        with self._pull_lock:
            if self._pull_state.active:
                return False, f"Already pulling '{self._pull_state.model}'."
            self._pull_state.reset(model_name)

        self._pull_thread = threading.Thread(
            target=self._pull_worker,
            args=(model_name,),
            daemon=True,
            name="OllamaPull",
        )
        self._pull_thread.start()
        return True, None

    def get_pull_status(self):
        with self._pull_lock:
            return self._pull_state.to_dict()

    def _pull_worker(self, model_name):
        state = self._pull_state
        try:
            with requests.post(
                f"{self.endpoint}/api/pull",
                json={"name": model_name, "stream": True},
                stream=True,
                timeout=(10, 3600),
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                for raw_line in resp.iter_lines(
                    chunk_size=1024, decode_unicode=True
                ):
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue

                    if isinstance(ev, dict) and ev.get("error"):
                        with self._pull_lock:
                            state.error = str(ev["error"])
                        break

                    status = ev.get("status") or ""
                    with self._pull_lock:
                        if status:
                            state.status = status
                        if ev.get("digest"):
                            state.digest = ev["digest"]
                        if isinstance(ev.get("total"), int):
                            state.total = ev["total"]
                        if isinstance(ev.get("completed"), int):
                            state.completed = ev["completed"]

                    if status == "success":
                        break
        except Exception as e:
            with self._pull_lock:
                state.error = str(e)
        finally:
            with self._pull_lock:
                state.active = False
                state.done = True
                state.finished_at = time.time()

    # -----------------------------------------------------------
    # Routing config
    # -----------------------------------------------------------
    def get_routing(self):
        import constants
        return {
            "MODEL_NAME": constants.MODEL_NAME,
            "FAST_MODEL_NAME": constants.FAST_MODEL_NAME,
            "FAST_MODEL_ENABLED": bool(constants.FAST_MODEL_ENABLED),
            "FAST_MODEL_NUM_CTX": int(constants.FAST_MODEL_NUM_CTX),
            "MAIN_NUM_CTX": int(constants.MAIN_NUM_CTX),
            "FAST_MODEL_KEEP_ALIVE": str(constants.FAST_MODEL_KEEP_ALIVE),
        }

    def save_routing(self, payload):
        patch, errors = self._validate_routing(payload or {})
        if errors:
            return False, "; ".join(errors)
        if not patch:
            return True, None

        ok, err = _patch_constants_file(patch)
        if not ok:
            return False, err

        # Live-rebind in every module that imported these by value.
        import constants
        for name, value in patch.items():
            setattr(constants, name, value)
        _rebind_consumers(patch)

        return True, None

    def _validate_routing(self, payload):
        patch = {}
        errors = []

        if "MODEL_NAME" in payload:
            v = str(payload["MODEL_NAME"] or "").strip()
            if not v:
                errors.append("MODEL_NAME cannot be empty.")
            elif not _MODEL_NAME_RE.match(v):
                errors.append("MODEL_NAME has invalid characters.")
            else:
                ok, err = self._model_is_installed(v)
                if not ok:
                    errors.append(f"MODEL_NAME: {err}")
                else:
                    patch["MODEL_NAME"] = v

        if "FAST_MODEL_NAME" in payload:
            v = str(payload["FAST_MODEL_NAME"] or "").strip()
            if not v:
                errors.append("FAST_MODEL_NAME cannot be empty.")
            elif not _MODEL_NAME_RE.match(v):
                errors.append("FAST_MODEL_NAME has invalid characters.")
            else:
                ok, err = self._model_is_installed(v)
                if not ok:
                    errors.append(f"FAST_MODEL_NAME: {err}")
                else:
                    patch["FAST_MODEL_NAME"] = v

        if "FAST_MODEL_ENABLED" in payload:
            patch["FAST_MODEL_ENABLED"] = bool(payload["FAST_MODEL_ENABLED"])

        for key in ("FAST_MODEL_NUM_CTX", "MAIN_NUM_CTX"):
            if key in payload:
                try:
                    n = int(payload[key])
                except (TypeError, ValueError):
                    errors.append(f"{key} must be an integer.")
                    continue
                if n < 2048 or n > 131072:
                    errors.append(
                        f"{key} must be between 2048 and 131072."
                    )
                    continue
                patch[key] = n

        if "FAST_MODEL_KEEP_ALIVE" in payload:
            v = str(payload["FAST_MODEL_KEEP_ALIVE"] or "").strip()
            if not _KEEP_ALIVE_RE.match(v):
                errors.append(
                    'FAST_MODEL_KEEP_ALIVE must be like "5m", "30s", '
                    '"2h", or "-1" (never unload).'
                )
            else:
                patch["FAST_MODEL_KEEP_ALIVE"] = v

        return patch, errors

    def _model_is_installed(self, name):
        # Cloud specs (e.g. "gemini:gemini-2.5-flash") are remote — they
        # can't be pulled into Ollama. Skip the local check for anything
        # that parses to a non-ollama provider.
        try:
            from modules.llm_providers import parse_model_spec
            provider, _model = parse_model_spec(name)
        except Exception:
            provider = "ollama"
        if provider != "ollama":
            return True, None

        try:
            data = self._get("/api/tags", timeout=3)
        except Exception as e:
            return False, f"cannot reach Ollama ({e})"
        names = {(m.get("name") or m.get("model") or "")
                for m in data.get("models", []) or []}
        if name not in names:
            return False, f"'{name}' is not installed — pull it first."
        return True, None


# ===============================================================
# constants.py line patcher
# ===============================================================
def _format_value(name, value):
    if name == "FAST_MODEL_ENABLED":
        return "True" if value else "False"
    if name in ("FAST_MODEL_NUM_CTX", "MAIN_NUM_CTX"):
        return str(int(value))
    return f'"{value}"'


def _patch_constants_file(patch):
    try:
        with open(_CONSTANTS_PATH, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        return False, f"Could not read constants.py: {e}"

    updated = src

    # Only target unindented top-level assignments: `^NAME = <value>`.
    # Anything nested inside a class or indented is left alone.
    for name, value in patch.items():
        new_repr = _format_value(name, value)
        pattern = re.compile(
            r"^" + re.escape(name) + r"\s*=\s*(.*?)\s*(#.*)?$",
            re.MULTILINE,
        )

        def _repl(m, _name=name, _new=new_repr):
            comment = m.group(2) or ""
            if comment:
                return f"{_name} = {_new}  {comment}"
            return f"{_name} = {_new}"

        updated, count = pattern.subn(_repl, updated, count=1)
        if count == 0:
            return False, f"Could not find `{name} = ...` in constants.py"

    # Refuse to write anything that won't even parse.
    try:
        compile(updated, _CONSTANTS_PATH, "exec")
    except SyntaxError as e:
        return False, f"Patched constants.py would not compile: {e}"

    # Back up before overwriting.
    try:
        shutil.copy2(_CONSTANTS_PATH, _CONSTANTS_PATH + ".bak")
    except Exception:
        pass

    try:
        with open(_CONSTANTS_PATH, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        return False, f"Could not write constants.py: {e}"

    return True, None


# ===============================================================
# Live rebind
# ===============================================================
def _rebind_consumers(patch):
    import sys
    for name, value in patch.items():
        for mod_name in _CONSUMERS.get(name, []):
            mod = sys.modules.get(mod_name)
            if mod is not None:
                try:
                    setattr(mod, name, value)
                except Exception:
                    pass

    # Prompter caches whether the fast model is present; reset it so the
    # next routing decision re-checks against the new name.
    prompter = sys.modules.get("modules.prompter")
    if prompter is not None and hasattr(prompter, "_FAST_MODEL_AVAILABLE"):
        try:
            prompter._FAST_MODEL_AVAILABLE = None
        except Exception:
            pass