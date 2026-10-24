from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ozon_skill.erp.app import create_server
from ozon_skill.erp.auth import AuthError, AuthRegistry


class AuthRegistryTests(unittest.TestCase):
    def test_password_is_hashed_and_session_can_be_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            registry = AuthRegistry(path)
            user = registry.register(
                {
                    "identifier": "Seller@Example.com",
                    "password": "Secure123",
                    "display_name": "测试卖家",
                }
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("Secure123", raw)
            self.assertEqual(user["identifier"], "se***@example.com")

            token, authenticated = registry.login(
                "seller@example.com", "Secure123"
            )
            self.assertEqual(authenticated["username"], user["username"])
            self.assertIsNotNone(registry.session(token))
            registry.logout(token)
            self.assertIsNone(registry.session(token))

    def test_registration_validates_identifier_and_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AuthRegistry(Path(directory) / "auth.json")
            with self.assertRaises(AuthError):
                registry.register(
                    {
                        "identifier": "not-an-email",
                        "password": "Secure123",
                        "display_name": "测试卖家",
                    }
                )

    def test_primary_login_rebinds_owner_and_revokes_old_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AuthRegistry(Path(directory) / "auth.json")
            registry.bootstrap("admin@example.com", "AdminPass123", "owner")
            old_token, _ = registry.login("admin@example.com", "AdminPass123")

            account = registry.set_primary_login(
                "19603830666", "NewOwner123", "owner"
            )

            self.assertEqual(account["username"], "owner")
            self.assertEqual(account["identifier"], "196****0666")
            self.assertIsNone(registry.session(old_token))
            _, logged_in = registry.login("19603830666", "NewOwner123")
            self.assertEqual(logged_in["username"], "owner")

    def test_primary_login_absorbs_existing_trial_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AuthRegistry(Path(directory) / "auth.json")
            registry.bootstrap("admin@example.com", "AdminPass123", "owner")
            trial = registry.register(
                {
                    "identifier": "19603830666",
                    "password": "TrialPass123",
                    "display_name": "运营账号",
                }
            )
            trial_token, _ = registry.login("19603830666", "TrialPass123")

            owner = registry.set_primary_login(
                "19603830666", "OwnerPass123", "owner"
            )

            self.assertEqual(owner["migrated_from"], trial["username"])
            self.assertIsNone(registry.session(trial_token))
            _, logged_in = registry.login("19603830666", "OwnerPass123")
            self.assertEqual(logged_in["username"], "owner")
            with self.assertRaises(AuthError):
                registry.register(
                    {
                        "identifier": "+8613800000000",
                        "password": "onlyletters",
                        "display_name": "测试卖家",
                    }
                )

    def test_authorization_revokes_sessions_and_can_be_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AuthRegistry(Path(directory) / "auth.json")
            account = registry.register(
                {
                    "identifier": "seller@example.com",
                    "password": "SellerPass123",
                    "display_name": "测试卖家",
                }
            )
            token, _ = registry.login("seller@example.com", "SellerPass123")

            revoked = registry.set_authorized(account["username"], False)

            self.assertFalse(revoked["authorized"])
            self.assertIsNone(registry.session(token))
            with self.assertRaises(AuthError) as context:
                registry.login("seller@example.com", "SellerPass123")
            self.assertEqual(context.exception.status, 403)
            registry.set_authorized(account["username"], True)
            _, restored = registry.login("seller@example.com", "SellerPass123")
            self.assertTrue(restored["authorized"])


class PublicRegistrationHttpTests(unittest.TestCase):
    def test_registration_creates_trial_session_and_protects_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server("127.0.0.1", 0, Path(directory))
            server.pipeline_service.auth.bootstrap(
                "admin@northstar.local", "AdminPass123", "admin"
            )
            server.pipeline_service.team.identity("admin")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            def request(
                path: str, *, body: dict[str, object] | None = None, cookie: str = ""
            ) -> tuple[int, dict[str, object], str]:
                data = json.dumps(body).encode() if body is not None else None
                headers = {
                    "Content-Type": "application/json",
                    "X-Forwarded-Proto": "https",
                }
                if cookie:
                    headers["Cookie"] = cookie
                value = Request(base_url + path, data=data, headers=headers)
                try:
                    with urlopen(value, timeout=5) as response:
                        return (
                            response.status,
                            json.loads(response.read()),
                            response.headers.get("Set-Cookie", ""),
                        )
                except HTTPError as exc:
                    return exc.code, json.loads(exc.read()), ""

            try:
                anonymous_status, _, _ = request("/api/account")
                status, _, set_cookie = request(
                    "/api/auth/register",
                    body={
                        "identifier": "+8613800000000",
                        "password": "SellerPass123",
                        "display_name": "新卖家",
                    },
                )
                parsed = SimpleCookie()
                parsed.load(set_cookie)
                cookie = f"northstar_session={parsed['northstar_session'].value}"
                account_status, account, _ = request("/api/account", cookie=cookie)
                workspace_status, _, _ = request("/api/pipelines", cookie=cookie)
                stores_status, _, _ = request("/api/stores", cookie=cookie)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(anonymous_status, 401)
            self.assertEqual(status, 201)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Secure", set_cookie)
            self.assertEqual(account_status, 200)
            self.assertEqual(account["identity"]["role"], "trial")
            self.assertEqual(workspace_status, 403)
            self.assertEqual(stores_status, 200)

    def test_owner_can_manage_registered_account_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server("127.0.0.1", 0, Path(directory))
            server.pipeline_service.auth.bootstrap(
                "admin@northstar.local", "AdminPass123", "admin"
            )
            server.pipeline_service.team.identity("admin")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            def request(
                path: str,
                *,
                method: str = "GET",
                body: dict[str, object] | None = None,
                cookie: str = "",
            ) -> tuple[int, dict[str, object], str]:
                data = json.dumps(body).encode() if body is not None else None
                headers = {"Content-Type": "application/json"}
                if cookie:
                    headers["Cookie"] = cookie
                value = Request(
                    base_url + path, data=data, headers=headers, method=method
                )
                try:
                    with urlopen(value, timeout=5) as response:
                        return (
                            response.status,
                            json.loads(response.read()),
                            response.headers.get("Set-Cookie", ""),
                        )
                except HTTPError as exc:
                    return exc.code, json.loads(exc.read()), ""

            def cookie_from(header: str) -> str:
                parsed = SimpleCookie()
                parsed.load(header)
                return f"northstar_session={parsed['northstar_session'].value}"

            try:
                owner_status, _, owner_header = request(
                    "/api/auth/login",
                    method="POST",
                    body={
                        "identifier": "admin@northstar.local",
                        "password": "AdminPass123",
                    },
                )
                owner_cookie = cookie_from(owner_header)
                register_status, registered, trial_header = request(
                    "/api/auth/register",
                    method="POST",
                    body={
                        "identifier": "seller@example.com",
                        "password": "SellerPass123",
                        "display_name": "注册卖家",
                    },
                )
                trial_cookie = cookie_from(trial_header)
                target = registered["user"]["username"]
                list_status, listing, _ = request(
                    "/api/registrations", cookie=owner_cookie
                )
                forbidden_status, _, _ = request(
                    "/api/registrations", cookie=trial_cookie
                )
                revoke_status, _, _ = request(
                    f"/api/registrations/{target}/revoke",
                    method="POST",
                    body={},
                    cookie=owner_cookie,
                )
                revoked_session_status, _, _ = request(
                    "/api/account", cookie=trial_cookie
                )
                revoked_login_status, _, _ = request(
                    "/api/auth/login",
                    method="POST",
                    body={
                        "identifier": "seller@example.com",
                        "password": "SellerPass123",
                    },
                )
                authorize_status, _, _ = request(
                    f"/api/registrations/{target}/authorize",
                    method="POST",
                    body={},
                    cookie=owner_cookie,
                )
                restored_login_status, _, _ = request(
                    "/api/auth/login",
                    method="POST",
                    body={
                        "identifier": "seller@example.com",
                        "password": "SellerPass123",
                    },
                )
                delete_status, deleted, _ = request(
                    f"/api/registrations/{target}",
                    method="DELETE",
                    cookie=owner_cookie,
                )
                deleted_login_status, _, _ = request(
                    "/api/auth/login",
                    method="POST",
                    body={
                        "identifier": "seller@example.com",
                        "password": "SellerPass123",
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(owner_status, 200)
            self.assertEqual(register_status, 201)
            self.assertEqual(list_status, 200)
            self.assertEqual(listing["total"], 2)
            self.assertEqual(forbidden_status, 403)
            self.assertEqual(revoke_status, 200)
            self.assertEqual(revoked_session_status, 401)
            self.assertEqual(revoked_login_status, 403)
            self.assertEqual(authorize_status, 200)
            self.assertEqual(restored_login_status, 200)
            self.assertEqual(delete_status, 200)
            self.assertEqual(deleted["status"], "deleted")
            self.assertEqual(deleted_login_status, 401)


if __name__ == "__main__":
    unittest.main()
