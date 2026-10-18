"""
OBS control over obs-websocket v5 (OBS 28+ ships it built in).

Drives the streaming side of Lumi: scene switching, starting and
stopping the stream, and writing text into OBS Text sources (used for
live subtitles, see modules/subtitles.py).

Settings are read from .env on every connect, so edits made on the
Integrations page apply without a restart:

    OBS_HOST              default 127.0.0.1
    OBS_PORT              default 4455
    OBS_PASSWORD          Tools → WebSocket Server Settings in OBS
    OBS_SUBTITLE_SOURCE   name of a Text (GDI+/FreeType) source, optional

obsws-python's client is synchronous and not thread-safe, so every
request goes through one lock. Callers on an asyncio loop should run
these methods in an executor.
"""

import logging
import os
import threading
import time

from dotenv import dotenv_values

from constants import WORKSPACE_ROOT
from modules.module import Module

try:
    import obsws_python as obs
    HAS_OBSWS = True
except ImportError:
    HAS_OBSWS = False

# obsws-python logs the password at INFO on every connect and a full
# traceback for routine request failures ("stream already active").
# Errors are surfaced through OBSError instead.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)

_ENV_PATH = os.path.join(WORKSPACE_ROOT, ".env")

_CONNECT_TIMEOUT = 3
# After a failed connect, background callers (subtitles) skip OBS for
# this long instead of paying the connect timeout on every line.
_QUIET_BACKOFF_SECONDS = 15


class OBSError(Exception):
    """A user-facing failure: message is safe to show in the UI."""


def read_obs_config():
    env = {**dotenv_values(_ENV_PATH), **{
        k: v for k, v in os.environ.items() if k.startswith("OBS_")
    }}
    try:
        port = int(env.get("OBS_PORT") or 4455)
    except ValueError:
        port = 4455
    return {
        "host": (env.get("OBS_HOST") or "127.0.0.1").strip(),
        "port": port,
        "password": env.get("OBS_PASSWORD") or "",
        "subtitle_source": (env.get("OBS_SUBTITLE_SOURCE") or "").strip(),
    }


class OBSControl(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self._client = None
        self._client_key = None
        self._lock = threading.Lock()
        self._quiet_until = 0.0
        self.last_error = ""
        signals.obs = self

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _drop_locked(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._client_key = None

    def _connect_locked(self, cfg):
        key = (cfg["host"], cfg["port"], cfg["password"])
        if self._client is not None and self._client_key == key:
            return self._client
        self._drop_locked()
        try:
            self._client = obs.ReqClient(
                host=cfg["host"], port=cfg["port"],
                password=cfg["password"], timeout=_CONNECT_TIMEOUT,
            )
        except Exception as e:
            raise OBSError(_describe_connect_error(e, cfg)) from None
        self._client_key = key
        return self._client

    def _call(self, fn, quiet=False):
        """Run fn(client) under the lock, reconnecting once if the
        cached connection went stale (OBS restarted, laptop slept)."""
        if not self.enabled:
            raise OBSError("OBS control is disabled.")
        if not HAS_OBSWS:
            raise OBSError("obsws-python is not installed "
                           "(pip install obsws-python).")
        if quiet and time.time() < self._quiet_until:
            raise OBSError(self.last_error or "OBS unreachable.")

        cfg = read_obs_config()
        with self._lock:
            for attempt in (1, 2):
                try:
                    client = self._connect_locked(cfg)
                    result = fn(client)
                    self.last_error = ""
                    return result
                except OBSError as e:
                    self.last_error = str(e)
                    self._quiet_until = time.time() + _QUIET_BACKOFF_SECONDS
                    raise
                except Exception as e:
                    if _is_request_error(e):
                        raise OBSError(_describe_request_error(e)) from None
                    # Socket-level failure on a cached client: reconnect
                    # once, then give up.
                    self._drop_locked()
                    if attempt == 2:
                        self.last_error = f"Lost connection to OBS: {e}"
                        self._quiet_until = (time.time()
                                             + _QUIET_BACKOFF_SECONDS)
                        raise OBSError(self.last_error) from None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def status(self):
        """Snapshot for the Stream page. Never raises."""
        out = {
            "enabled": self.enabled,
            "installed": HAS_OBSWS,
            "connected": False,
            "error": "",
            "obs_version": "",
            "scenes": [],
            "current_scene": "",
            "streaming": False,
            "timecode": "",
            "subtitle_source": read_obs_config()["subtitle_source"],
        }

        def _probe(c):
            version = c.get_version()
            scenes = c.get_scene_list()
            stream = c.get_stream_status()
            return version, scenes, stream

        try:
            version, scenes, stream = self._call(_probe)
        except OBSError as e:
            out["error"] = str(e)
            return out

        out["connected"] = True
        out["obs_version"] = getattr(version, "obs_version", "")
        # OBS lists scenes bottom-up; reverse to match its scene dock.
        out["scenes"] = [s.get("sceneName", "")
                         for s in reversed(scenes.scenes or [])]
        out["current_scene"] = scenes.current_program_scene_name
        out["streaming"] = bool(stream.output_active)
        out["timecode"] = stream.output_timecode or ""
        return out

    def switch_scene(self, name):
        name = (name or "").strip()
        if not name:
            raise OBSError("Scene name is required.")
        self._call(lambda c: c.set_current_program_scene(name))

    def start_stream(self):
        def _start(c):
            if c.get_stream_status().output_active:
                return
            c.start_stream()
        self._call(_start)

    def stop_stream(self):
        def _stop(c):
            if not c.get_stream_status().output_active:
                return
            c.stop_stream()
        self._call(_stop)

    def set_text_source(self, source, text, quiet=False):
        source = (source or "").strip()
        if not source:
            raise OBSError("Text source name is required.")
        self._call(
            lambda c: c.set_input_settings(source, {"text": text}, True),
            quiet=quiet,
        )

    def set_subtitle(self, text):
        """Best-effort push to OBS_SUBTITLE_SOURCE. Silent when it is
        unset or OBS is down; the subtitle file is the fallback."""
        source = read_obs_config()["subtitle_source"]
        if not source or not self.enabled or not HAS_OBSWS:
            return
        try:
            self.set_text_source(source, text, quiet=True)
        except OBSError:
            pass

    async def run(self):
        if self.enabled and not HAS_OBSWS:
            print("\033[93mOBS: obsws-python not installed — stream "
                  "controls disabled (pip install obsws-python)\033[0m")


# ======================================================================
# Error text
# ======================================================================
def _is_request_error(e):
    return HAS_OBSWS and isinstance(e, obs.error.OBSSDKRequestError)


def _describe_request_error(e):
    code = getattr(e, "code", None)
    req = getattr(e, "req_name", "request")
    if code == 600:
        return f"OBS couldn't find that resource ({req})."
    if code == 500:
        return "OBS reports the output is already in that state."
    return f"OBS rejected {req}: {e}"


def _describe_connect_error(e, cfg):
    where = f"{cfg['host']}:{cfg['port']}"
    if isinstance(e, (ConnectionRefusedError, OSError)) \
            and not isinstance(e, TimeoutError):
        return (f"OBS isn't reachable at {where}. Is OBS running with "
                f"Tools → WebSocket Server Settings → Enable enabled?")
    text = str(e).lower()
    if "no password provided" in text:
        return "OBS requires a password. Set OBS_PASSWORD."
    # A wrong password makes OBS close the socket mid-handshake, which
    # obsws-python reports as a failure to identify.
    if "failed to identify" in text or "closed" in text:
        return "OBS refused the login. Check OBS_PASSWORD."
    if isinstance(e, TimeoutError) or "timed out" in text:
        return f"Timed out connecting to OBS at {where}."
    return f"Couldn't connect to OBS at {where}: {e}"
