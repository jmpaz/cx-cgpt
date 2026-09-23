from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

SESSION_ENV = "CX_CLAUDE_SESSION_KEY"
ORG_ENV = "CX_CLAUDE_ORG"
PROFILE_ENV = "CX_CLAUDE_CHROME_PROFILE"

_HOSTS = ("claude.ai", ".claude.ai")
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Session:
    key: str = field(repr=False)
    organization: str | None
    source: str


def load_session() -> Session:
    organization = _organization(os.environ.get(ORG_ENV, "").strip() or None, ORG_ENV)
    key = os.environ.get(SESSION_ENV, "").strip()
    if key:
        return Session(key, organization, "environment")
    profile = chrome_profile()
    cookies = read_chrome_cookies(profile, {"sessionKey", "lastActiveOrg"})
    if "sessionKey" not in cookies:
        raise SessionError(
            f"No claude.ai session in Chrome profile {profile}. Sign in at "
            f"claude.ai in that profile, select another with {PROFILE_ENV}, "
            f"or set {SESSION_ENV}."
        )
    organization = organization or _organization(cookies.get("lastActiveOrg"), "lastActiveOrg cookie")
    return Session(cookies["sessionKey"], organization, f"chrome:{profile}")


def _organization(value: str | None, origin: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        raise SessionError(f"{origin} is not a claude.ai organization UUID.") from None


def chrome_profile() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Google/Chrome"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "google-chrome"
    chosen = os.environ.get(PROFILE_ENV, "").strip()
    if not chosen:
        return root / "Default"
    path = Path(chosen).expanduser()
    return path if path.is_absolute() else root / chosen


def read_chrome_cookies(profile: Path, names: set[str]) -> dict[str, str]:
    database = next(
        (path for path in (profile / "Cookies", profile / "Network" / "Cookies") if path.is_file()),
        None,
    )
    if database is None:
        raise SessionError(f"No Chrome cookie store in {profile}; set {PROFILE_ENV} or {SESSION_ENV}.")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        try:
            version_row = connection.execute("select value from meta where key = 'version'").fetchone()
            rows = connection.execute(
                "select host_key, name, value, encrypted_value, expires_utc, has_expires "
                "from cookies where host_key in (?, ?) order by expires_utc",
                _HOSTS,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise SessionError(f"Cannot read Chrome cookie store {database}: {error}") from None
    hashed_hosts = int(version_row[0]) >= 24 if version_row else False
    now = datetime.now(timezone.utc)
    cipher = _ChromeCipher()
    cookies: dict[str, str] = {}
    for host, name, value, encrypted, expires, has_expires in rows:
        if name not in names:
            continue
        if has_expires and _CHROME_EPOCH + timedelta(microseconds=expires) <= now:
            continue
        cookies[name] = cipher.decrypt(host, bytes(encrypted), hashed_hosts) if encrypted else value
    return cookies


class _ChromeCipher:
    def __init__(self) -> None:
        self._keys: dict[bytes, bytes] = {}

    def decrypt(self, host: str, blob: bytes, hashed_host: bool) -> str:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise SessionError("Reading Chrome cookies requires the cryptography package.") from None
        version = blob[:3]
        decryptor = Cipher(algorithms.AES(self._key(version)), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(blob[3:]) + decryptor.finalize()
        if not plain or not 1 <= plain[-1] <= 16:
            raise SessionError("Cannot decrypt Chrome cookies; the Safe Storage key did not match.")
        plain = plain[: -plain[-1]]
        if hashed_host:
            if plain[:32] != hashlib.sha256(host.encode()).digest():
                raise SessionError("Cannot decrypt Chrome cookies; the Safe Storage key did not match.")
            plain = plain[32:]
        try:
            return plain.decode()
        except UnicodeDecodeError:
            raise SessionError("Cannot decrypt Chrome cookies; the Safe Storage key did not match.") from None

    def _key(self, version: bytes) -> bytes:
        if version not in self._keys:
            if sys.platform == "darwin" and version == b"v10":
                self._keys[version] = hashlib.pbkdf2_hmac("sha1", _keychain_password(), b"saltysalt", 1003, 16)
            elif sys.platform != "darwin" and version in (b"v10", b"v11"):
                password = b"peanuts" if version == b"v10" else _keyring_password()
                self._keys[version] = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
            else:
                raise SessionError(f"Unsupported Chrome cookie encryption {version!r}; set {SESSION_ENV}.")
        return self._keys[version]


def _keychain_password() -> bytes:
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SessionError("Cannot run macOS `security` to read Chrome Safe Storage.") from None
    if completed.returncode != 0:
        raise SessionError("Keychain denied access to Chrome Safe Storage.")
    return completed.stdout.strip()


def _keyring_password() -> bytes:
    try:
        import secretstorage
    except ImportError:
        raise SessionError("Reading Chrome cookies on Linux requires the secretstorage package.") from None
    try:
        with closing(secretstorage.dbus_init()) as connection:
            collection = secretstorage.get_default_collection(connection)
            if collection.is_locked():
                raise SessionError("The Secret Service keyring is locked; unlock it and retry.")
            for item in collection.search_items({"application": "chrome"}):
                return item.get_secret()
    except secretstorage.exceptions.SecretStorageException:
        raise SessionError("Cannot reach the Secret Service keyring holding Chrome Safe Storage.") from None
    raise SessionError("The keyring holds no Chrome Safe Storage entry.")
