"""Argon2id password hashing (run in a worker thread so the event loop never blocks)."""
from __future__ import annotations

import asyncio
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .. import limits

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# A real hash of a random string - verified against when the account doesn't
# exist, so "no such user" and "wrong password" take the same time.
_DUMMY_HASH = _hasher.hash("elvin-dummy-password-for-timing")

_COMMON = {
    "password", "password1", "password123", "123456789", "1234567890", "qwertyuiop", "iloveyou",
    "letmein123", "welcome123", "admin12345", "passw0rd!", "abc123456", "111111111", "qwerty123",
    "football1", "sunshine1", "princess1", "monkey1234", "dragon1234", "baseball1", "changeme123",
}


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def password_problems(password: str, *, username: str = "", email: str = "") -> list[str]:
    problems: list[str] = []
    if len(password) < limits.PASSWORD_MIN:
        problems.append(f"Use at least {limits.PASSWORD_MIN} characters.")
    if len(password) > limits.PASSWORD_MAX:
        problems.append(f"Use at most {limits.PASSWORD_MAX} characters.")
    lowered = password.lower()
    if lowered in _COMMON or len(set(password)) < 4:
        problems.append("That password is too easy to guess.")
    for part in (username.lower(), email.split("@")[0].lower()):
        if len(part) >= 4 and part in lowered:
            problems.append("Don't include your username or email in the password.")
            break
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[^A-Za-z]", password):
        problems.append("Mix letters with numbers or symbols.")
    return problems
