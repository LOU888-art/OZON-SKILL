"""Persistent team membership and role-based access control for the ERP."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
ROLE_DEFINITIONS = {
    "owner": {
        "name": "所有者",
        "permissions": ["workspace.read", "pipeline.create", "stage.run", "images.manage", "listing.publish", "stores.manage", "ai.manage", "team.manage", "billing.view"],
    },
    "admin": {
        "name": "管理员",
        "permissions": ["workspace.read", "pipeline.create", "stage.run", "images.manage", "listing.publish", "stores.manage", "ai.manage", "team.manage"],
    },
    "operator": {
        "name": "运营",
        "permissions": ["workspace.read", "pipeline.create", "stage.run", "images.manage", "listing.publish"],
    },
    "designer": {
        "name": "设计",
        "permissions": ["workspace.read", "images.manage"],
    },
    "reviewer": {
        "name": "审核",
        "permissions": ["workspace.read", "stage.run"],
    },
    "viewer": {"name": "只读", "permissions": ["workspace.read"]},
}


class TeamError(RuntimeError):
    """A user-facing membership or permission failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TeamRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "members": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TeamError("团队权限文件无法读取", status=500) from exc
        if not isinstance(value, dict) or not isinstance(value.get("members"), list):
            raise TeamError("团队权限文件格式无效", status=500)
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
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
    def _safe_username(value: Any) -> str:
        username = str(value or "").strip()
        if not _USERNAME.fullmatch(username):
            raise TeamError("登录用户名格式无效", status=401)
        return username

    @staticmethod
    def _public(member: Mapping[str, Any]) -> dict[str, Any]:
        role = str(member.get("role") or "viewer")
        definition = ROLE_DEFINITIONS.get(role, ROLE_DEFINITIONS["viewer"])
        return {
            "username": member.get("username"),
            "display_name": member.get("display_name") or member.get("username"),
            "role": role,
            "role_name": definition["name"],
            "permissions": list(definition["permissions"]),
            "created_at": member.get("created_at"),
            "updated_at": member.get("updated_at"),
        }

    def identity(self, username: Any) -> dict[str, Any]:
        username = self._safe_username(username or os.environ.get("ERP_OWNER_USERNAME") or "admin")
        with self._lock:
            document = self._read()
            if not document["members"]:
                timestamp = _now()
                document["members"].append({"username": username, "display_name": username, "role": "owner", "created_at": timestamp, "updated_at": timestamp})
                self._write(document)
            for member in document["members"]:
                if str(member.get("username") or "").casefold() == username.casefold():
                    return self._public(member)
        raise TeamError("当前登录账号尚未加入此工作区", status=403)

    def require(self, username: Any, permission: str) -> dict[str, Any]:
        identity = self.identity(username)
        if permission not in identity["permissions"]:
            raise TeamError(f"当前角色无权执行此操作：{permission}", status=403)
        return identity

    def list_public(self, username: Any) -> dict[str, Any]:
        self.require(username, "workspace.read")
        with self._lock:
            return {"members": [self._public(item) for item in self._read()["members"]], "roles": ROLE_DEFINITIONS}

    def save_member(self, actor: Any, value: Mapping[str, Any]) -> dict[str, Any]:
        actor_identity = self.require(actor, "team.manage")
        username = self._safe_username(value.get("username"))
        display_name = str(value.get("display_name") or username).strip()[:80]
        role = str(value.get("role") or "viewer").strip().lower()
        if role not in ROLE_DEFINITIONS or role == "owner":
            raise TeamError("成员角色必须是管理员、运营、设计、审核或只读")
        with self._lock:
            document = self._read()
            existing = next((item for item in document["members"] if str(item.get("username") or "").casefold() == username.casefold()), None)
            if existing and existing.get("role") == "owner":
                raise TeamError("不能修改工作区所有者", status=403)
            timestamp = _now()
            record = {"username": username, "display_name": display_name, "role": role, "created_at": existing.get("created_at") if existing else timestamp, "updated_at": timestamp, "updated_by": actor_identity["username"]}
            if existing:
                document["members"][document["members"].index(existing)] = record
            else:
                document["members"].append(record)
            self._write(document)
            return self._public(record)

    def delete_member(self, actor: Any, username: Any) -> None:
        self.require(actor, "team.manage")
        username = self._safe_username(username)
        with self._lock:
            document = self._read()
            target = next((item for item in document["members"] if str(item.get("username") or "").casefold() == username.casefold()), None)
            if target is None:
                raise TeamError("团队成员不存在", status=404)
            if target.get("role") == "owner":
                raise TeamError("不能删除工作区所有者", status=403)
            document["members"].remove(target)
            self._write(document)
