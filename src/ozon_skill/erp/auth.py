"""Application-owned accounts, password hashing and revocable sessions."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE = re.compile(r"^\+?[1-9]\d{7,14}$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuthError("登录会话数据无效", status=500) from exc


def normalize_identifier(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "@" in raw:
        normalized = raw.casefold()
        if len(normalized) > 254 or not _EMAIL.fullmatch(normalized):
            raise AuthError("请输入有效的邮箱地址")
        return normalized, "email"
    normalized = re.sub(r"[\s()-]", "", raw)
    if not _PHONE.fullmatch(normalized):
        raise AuthError("请输入有效的手机号，国际号码请包含国家区号")
    return normalized, "phone"


def _validate_password(value: Any) -> str:
    password = str(value or "")
    if not 8 <= len(password) <= 128:
        raise AuthError("密码长度必须为 8–128 个字符")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise AuthError("密码必须同时包含字母和数字")
    return password


def _password_record(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return {
        "algorithm": "scrypt",
        "salt": salt.hex(),
        "digest": digest.hex(),
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
    }


def _verify_password(password: str, record: Mapping[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["digest"]))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(record.get("n") or _SCRYPT_N),
            r=int(record.get("r") or _SCRYPT_R),
            p=int(record.get("p") or _SCRYPT_P),
            dklen=len(expected),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


class AuthRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()
        self._failed_logins: dict[str, list[datetime]] = {}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "users": [], "sessions": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError("账户数据无法读取", status=500) from exc
        if not isinstance(value, dict):
            raise AuthError("账户数据格式无效", status=500)
        value.setdefault("users", [])
        value.setdefault("sessions", [])
        if not isinstance(value["users"], list) or not isinstance(value["sessions"], list):
            raise AuthError("账户数据格式无效", status=500)
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _public(user: Mapping[str, Any]) -> dict[str, Any]:
        identifier = str(user.get("identifier") or "")
        if user.get("identifier_type") == "email":
            name, _, domain = identifier.partition("@")
            masked = f"{name[:2]}***@{domain}" if domain else identifier
        else:
            masked = f"{identifier[:3]}****{identifier[-4:]}" if len(identifier) >= 8 else identifier
        return {
            "username": user.get("username"),
            "identifier": masked,
            "identifier_type": user.get("identifier_type"),
            "display_name": user.get("display_name"),
            "created_at": user.get("created_at"),
        }

    def register(self, value: Mapping[str, Any]) -> dict[str, Any]:
        identifier, identifier_type = normalize_identifier(value.get("identifier"))
        password = _validate_password(value.get("password"))
        display_name = str(value.get("display_name") or "").strip()
        if not 2 <= len(display_name) <= 80:
            raise AuthError("姓名或昵称长度必须为 2–80 个字符")
        with self._lock:
            document = self._read()
            if any(
                str(item.get("identifier") or "").casefold() == identifier.casefold()
                for item in document["users"]
            ):
                raise AuthError("该邮箱或手机号已经注册", status=409)
            timestamp = _timestamp()
            user = {
                "user_id": uuid4().hex,
                "username": f"user_{uuid4().hex[:16]}",
                "identifier": identifier,
                "identifier_type": identifier_type,
                "display_name": display_name,
                "password": _password_record(password),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            document["users"].append(user)
            self._write(document)
            return self._public(user)

    def first_user(self) -> dict[str, Any] | None:
        with self._lock:
            users = self._read()["users"]
            return self._public(users[0]) if users else None

    def bootstrap(self, identifier: str, password: str, username: str = "admin") -> dict[str, Any]:
        normalized, identifier_type = normalize_identifier(identifier)
        password = _validate_password(password)
        with self._lock:
            document = self._read()
            if document["users"]:
                return self._public(document["users"][0])
            timestamp = _timestamp()
            user = {
                "user_id": uuid4().hex,
                "username": username,
                "identifier": normalized,
                "identifier_type": identifier_type,
                "display_name": "系统管理员",
                "password": _password_record(password),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            document["users"].append(user)
            self._write(document)
            return self._public(user)

    def delete_user(self, username: str) -> None:
        with self._lock:
            document = self._read()
            document["users"] = [
                item for item in document["users"] if item.get("username") != username
            ]
            document["sessions"] = [
                item for item in document["sessions"] if item.get("username") != username
            ]
            self._write(document)

    def reset_password(self, identifier_value: Any, password_value: Any) -> dict[str, Any]:
        identifier, _ = normalize_identifier(identifier_value)
        password = _validate_password(password_value)
        with self._lock:
            document = self._read()
            user = next(
                (
                    item
                    for item in document["users"]
                    if str(item.get("identifier") or "").casefold()
                    == identifier.casefold()
                ),
                None,
            )
            if user is None:
                raise AuthError("未找到该邮箱或手机号", status=404)
            user["password"] = _password_record(password)
            user["updated_at"] = _timestamp()
            document["sessions"] = [
                item
                for item in document["sessions"]
                if item.get("username") != user.get("username")
            ]
            self._write(document)
            return self._public(user)

    def login(self, identifier_value: Any, password_value: Any) -> tuple[str, dict[str, Any]]:
        identifier, _ = normalize_identifier(identifier_value)
        password = str(password_value or "")
        cutoff = _now() - timedelta(minutes=15)
        attempts = [item for item in self._failed_logins.get(identifier, []) if item > cutoff]
        if len(attempts) >= 8:
            raise AuthError("登录尝试过多，请 15 分钟后再试", status=429)
        with self._lock:
            document = self._read()
            user = next(
                (
                    item
                    for item in document["users"]
                    if str(item.get("identifier") or "").casefold() == identifier.casefold()
                ),
                None,
            )
            if user is None or not _verify_password(password, user.get("password") or {}):
                attempts.append(_now())
                self._failed_logins[identifier] = attempts
                raise AuthError("邮箱/手机号或密码错误", status=401)
            self._failed_logins.pop(identifier, None)
            token = secrets.token_urlsafe(32)
            expires_at = _now() + timedelta(days=30)
            document["sessions"] = [
                item
                for item in document["sessions"]
                if _parse_timestamp(item.get("expires_at")) > _now()
            ][-199:]
            document["sessions"].append(
                {
                    "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "username": user["username"],
                    "created_at": _timestamp(),
                    "expires_at": _timestamp(expires_at),
                }
            )
            self._write(document)
            return token, self._public(user)

    def session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            document = self._read()
            session = next(
                (item for item in document["sessions"] if hmac.compare_digest(str(item.get("token_hash") or ""), token_hash)),
                None,
            )
            if session is None or _parse_timestamp(session.get("expires_at")) <= _now():
                return None
            user = next(
                (item for item in document["users"] if item.get("username") == session.get("username")),
                None,
            )
            return self._public(user) if user else None

    def logout(self, token: str) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            document = self._read()
            document["sessions"] = [
                item
                for item in document["sessions"]
                if not hmac.compare_digest(str(item.get("token_hash") or ""), token_hash)
            ]
            self._write(document)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the ERP application login")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    registry = AuthRegistry(args.path)
    if args.reset:
        registry.reset_password(args.identifier, args.password)
    else:
        registry.bootstrap(args.identifier, args.password, args.username)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
