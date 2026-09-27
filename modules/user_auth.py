"""
Local user authentication for the web client.

SQLite-backed, argon2id-hashed, offline-only. No network calls, no
third-party identity providers. Sessions are sliding-TTL, stored in
the same DB, and persisted across process restarts.

Designed to be used by modules/socketIO.py, but fully usable from a
terminal for testing:

    python -c "from modules.user_auth import UserDB; \
               db = UserDB(); \
               print(db.register('test', 'hunter2'))"
"""

import os
import re
import json
import time
import sqlite3
import secrets
import threading

# --- Password hashing ----------------------------------------------------
# argon2-cffi is the target. If it's not installed we fall back to
# stdlib pbkdf2_hmac — weaker, but functional and zero-dependency.
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHash
    _PH = PasswordHasher()
    _HAS_ARGON2 = True
except ImportError:
    import hashlib
    _HAS_ARGON2 = False
    print("\033[93mUSER_AUTH: argon2-cffi not installed — "
          "falling back to pbkdf2_hmac. pip install argon2-cffi\033[0m")


DB_PATH = os.path.join("memories", "users.db")

# 30-day sliding TTL
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30

# Rate limiting: 5 failed logins per username per 60s
LOGIN_RATE_WINDOW = 60
LOGIN_RATE_MAX_ATTEMPTS = 5

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{3,32}$")


def _hash_password(password: str) -> str:
    if _HAS_ARGON2:
        return _PH.hash(password)
    import hashlib, secrets as _s
    salt = _s.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${salt.hex()}${dk.hex()}"


def _verify_password(stored: str, password: str) -> bool:
    if _HAS_ARGON2 and stored.startswith("$argon2"):
        try:
            _PH.verify(stored, password)
            return True
        except (VerifyMismatchError, InvalidHash):
            return False
        except Exception:
            return False
    # pbkdf2 path
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        import hashlib
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 120_000
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


class UserDB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._login_attempts = {}  # username -> [timestamps]
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT    UNIQUE NOT NULL,
                    password_hash TEXT    NOT NULL,
                    display_name  TEXT,
                    created_at    REAL    NOT NULL,
                    last_login    REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user
                    ON sessions(user_id);
            """)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------
    def has_users(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()
        return bool(row and row["c"] > 0)

    def user_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------
    def _rate_ok(self, username: str) -> bool:
        now = time.time()
        hist = self._login_attempts.setdefault(username, [])
        # drop expired
        hist[:] = [t for t in hist if now - t < LOGIN_RATE_WINDOW]
        return len(hist) < LOGIN_RATE_MAX_ATTEMPTS

    def _record_failure(self, username: str):
        self._login_attempts.setdefault(username, []).append(time.time())

    def _clear_failures(self, username: str):
        self._login_attempts.pop(username, None)

    # ------------------------------------------------------------------
    # Registration / authentication
    # ------------------------------------------------------------------
    def register(self, username: str, password: str,
                 display_name: str = None):
        """Returns (ok, payload). On success payload = {id, username,
        display_name}."""
        username = (username or "").strip().lower()
        if not _USERNAME_RE.match(username):
            return False, ("Username must be 3–32 chars, "
                           "letters/digits/_/- only.")
        if not password or len(password) < 8:
            return False, "Password must be at least 8 characters."

        pw_hash = _hash_password(password)
        now = time.time()
        display = (display_name or username).strip()[:64]

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, display_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (username, pw_hash, display, now),
                )
                self._conn.commit()
                user_id = cur.lastrowid
            except sqlite3.IntegrityError:
                return False, "That username is taken."

        return True, {
            "id": user_id,
            "username": username,
            "display_name": display,
        }

    def authenticate(self, username: str, password: str):
        username = (username or "").strip().lower()

        if not self._rate_ok(username):
            return False, "Too many failed attempts. Try again later."

        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, display_name "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if not row:
            self._record_failure(username)
            # Don't leak whether the user exists.
            return False, "Invalid username or password."

        if not _verify_password(row["password_hash"], password):
            self._record_failure(username)
            return False, "Invalid username or password."

        self._clear_failures(username)

        with self._lock:
            self._conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            self._conn.commit()

        return True, {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
        }

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions "
                "(token, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, user_id, now, now + SESSION_TTL_SECONDS),
            )
            self._conn.commit()
        return token

    def validate_session(self, token: str):
        """Returns user dict if valid, else None. Slides the TTL."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT s.user_id, s.expires_at, "
                "       u.username, u.display_name "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                self._conn.execute(
                    "DELETE FROM sessions WHERE token = ?", (token,)
                )
                self._conn.commit()
                return None
            # slide TTL
            self._conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token = ?",
                (now + SESSION_TTL_SECONDS, token),
            )
            self._conn.commit()
            return {
                "id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
            }

    def delete_session(self, token: str):
        if not token:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE token = ?", (token,)
            )
            self._conn.commit()

    def delete_all_sessions(self, user_id: int):
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE user_id = ?", (user_id,)
            )
            self._conn.commit()