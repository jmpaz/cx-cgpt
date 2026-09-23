import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from cx_chats.claude import session as claude_session
from cx_chats.claude.session import SessionError, load_session

ORG = "44444444-4444-4444-8444-444444444444"
FUTURE = int((datetime(2100, 1, 1, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1e6)
PAST = int((datetime(2020, 1, 1, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1e6)


def encrypt(value, password, iterations, prefix, host=None):
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, 16)
    plain = (hashlib.sha256(host.encode()).digest() if host else b"") + value.encode()
    padding = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(plain + bytes([padding]) * padding) + encryptor.finalize()


def cookie_store(profile, rows, version=24):
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / "Cookies")
    connection.execute("create table meta (key text, value text)")
    connection.execute("insert into meta values ('version', ?)", (str(version),))
    connection.execute(
        "create table cookies (host_key text, name text, value text, encrypted_value blob, expires_utc integer, has_expires integer)"
    )
    connection.executemany("insert into cookies values (?, ?, '', ?, ?, 1)", rows)
    connection.commit()
    connection.close()


@pytest.fixture
def linux_chrome(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_session.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(claude_session.SESSION_ENV, raising=False)
    monkeypatch.delenv(claude_session.ORG_ENV, raising=False)
    monkeypatch.delenv(claude_session.PROFILE_ENV, raising=False)
    monkeypatch.setattr(claude_session, "_keyring_password", lambda: b"keyring-secret")
    return tmp_path / "google-chrome"


def test_environment_session_needs_no_browser(monkeypatch):
    monkeypatch.setenv(claude_session.SESSION_ENV, "sk-env")
    monkeypatch.setenv(claude_session.ORG_ENV, ORG)
    session = load_session()
    assert (session.key, session.organization, session.source) == ("sk-env", ORG, "environment")
    assert "sk-env" not in repr(session)


def test_linux_keyring_cookies_with_host_digest(linux_chrome):
    cookie_store(linux_chrome / "Default", [
        (".claude.ai", "sessionKey", encrypt("sk-old", b"keyring-secret", 1, b"v11", ".claude.ai"), PAST),
        (".claude.ai", "sessionKey", encrypt("sk-live", b"keyring-secret", 1, b"v11", ".claude.ai"), FUTURE),
        (".claude.ai", "lastActiveOrg", encrypt(ORG, b"keyring-secret", 1, b"v11", ".claude.ai"), FUTURE),
        (".claude.ai", "unrelated", encrypt("ignored", b"wrong", 1, b"v11", ".claude.ai"), FUTURE),
    ])
    session = load_session()
    assert (session.key, session.organization) == ("sk-live", ORG)
    assert session.source == f"chrome:{linux_chrome / 'Default'}"


def test_linux_basic_store_and_older_schema(linux_chrome, monkeypatch):
    monkeypatch.setenv(claude_session.PROFILE_ENV, "Profile 2")
    monkeypatch.setenv(claude_session.ORG_ENV, ORG)
    cookie_store(linux_chrome / "Profile 2", [
        ("claude.ai", "sessionKey", encrypt("sk-basic", b"peanuts", 1, b"v10"), FUTURE),
    ], version=18)
    assert load_session().key == "sk-basic"


def test_macos_keychain_key_derivation(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_session.sys, "platform", "darwin")
    monkeypatch.setattr(claude_session.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(claude_session.SESSION_ENV, raising=False)
    monkeypatch.delenv(claude_session.PROFILE_ENV, raising=False)
    monkeypatch.setattr(claude_session, "_keychain_password", lambda: b"mac-secret")
    cookie_store(tmp_path / "Library/Application Support/Google/Chrome/Default", [
        (".claude.ai", "sessionKey", encrypt("sk-mac", b"mac-secret", 1003, b"v10", ".claude.ai"), FUTURE),
    ])
    assert load_session().key == "sk-mac"


def test_wrong_key_fails_without_revealing_cookie(linux_chrome):
    cookie_store(linux_chrome / "Default", [
        (".claude.ai", "sessionKey", encrypt("sk-secret", b"other-secret", 1, b"v11", ".claude.ai"), FUTURE),
    ])
    with pytest.raises(SessionError, match="Safe Storage key did not match") as error:
        load_session()
    assert "sk-secret" not in str(error.value)


def test_missing_session_names_profile_and_alternatives(linux_chrome):
    cookie_store(linux_chrome / "Default", [])
    with pytest.raises(SessionError, match="No claude.ai session in Chrome profile .*CX_CLAUDE_SESSION_KEY"):
        load_session()


def test_missing_profile_is_explicit(linux_chrome):
    with pytest.raises(SessionError, match="No Chrome cookie store"):
        load_session()
