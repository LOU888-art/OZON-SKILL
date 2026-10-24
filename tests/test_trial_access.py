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
from ozon_skill.erp.stores import StoreAuthorizationError, StoreRegistry
from ozon_skill.erp.team import TeamError, TeamRegistry


class _OzonClient:
    def category_tree(self, *, language: str) -> dict[str, object]:
        return {"result": [], "language": language}


def _client_factory(client_id: str, api_key: str) -> _OzonClient:
    assert client_id
    assert api_key
    return _OzonClient()


class TrialRoleTests(unittest.TestCase):
    def test_new_member_defaults_to_pricing_and_store_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = TeamRegistry(Path(directory) / "team.json")
            registry.identity("owner")
            member = registry.save_member(
                "owner", {"username": "trial1", "display_name": "试用账号"}
            )

            self.assertEqual(member["role"], "trial")
            self.assertEqual(
                set(member["permissions"]), {"pricing.use", "stores.manage"}
            )
            registry.require("trial1", "pricing.use")
            registry.require("trial1", "stores.manage")
            with self.assertRaises(TeamError) as context:
                registry.require("trial1", "workspace.read")
            self.assertEqual(context.exception.status, 403)
            with self.assertRaises(TeamError) as context:
                registry.save_member(
                    "owner", {"username": "operator1", "role": "operator"}
                )
            self.assertEqual(context.exception.status, 403)

    def test_trial_store_records_are_isolated_by_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = StoreRegistry(
                Path(directory) / "stores.json", client_factory=_client_factory
            )
            registry.authorize(
                {
                    "store_id": "store-a",
                    "name": "A",
                    "client_id": "1",
                    "api_key": "secret-a",
                },
                owner_username="trial1",
            )
            registry.authorize(
                {
                    "store_id": "store-b",
                    "name": "B",
                    "client_id": "2",
                    "api_key": "secret-b",
                },
                owner_username="trial2",
            )

            self.assertEqual(
                [item["store_id"] for item in registry.list_public(owner_username="trial1")],
                ["store-a"],
            )
            self.assertEqual(len(registry.list_public()), 2)
            with self.assertRaises(StoreAuthorizationError):
                registry.credentials("store-b", owner_username="trial1")
            with self.assertRaises(StoreAuthorizationError):
                registry.delete("store-b", owner_username="trial1")
            removed = registry.delete_by_owner("trial1")
            self.assertEqual(removed, ["store-a"])
            self.assertEqual(registry.list_public(owner_username="trial1"), [])
            self.assertEqual(
                [item["store_id"] for item in registry.list_public(owner_username="trial2")],
                ["store-b"],
            )


class TrialHttpAccessTests(unittest.TestCase):
    def test_trial_can_open_pricing_and_stores_but_not_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server("127.0.0.1", 0, Path(directory))
            server.pipeline_service.team.identity("owner")
            server.pipeline_service.team.save_member(
                "owner", {"username": "trial1", "display_name": "Trial"}
            )
            server.pipeline_service.auth.bootstrap(
                "trial1@example.com", "TrialPass123", "trial1"
            )
            token, _ = server.pipeline_service.auth.login(
                "trial1@example.com", "TrialPass123"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            def get(path: str) -> tuple[int, dict[str, object]]:
                request = Request(
                    base_url + path,
                    headers={"Cookie": f"northstar_session={token}"},
                )
                try:
                    with urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            try:
                account_status, account = get("/api/account")
                stores_status, stores = get("/api/stores")
                pricing_status, _ = get("/api/pricing/categories?q=home")
                workspace_status, workspace = get("/api/pipelines")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(account_status, 200)
            self.assertEqual(account["identity"]["role"], "trial")
            self.assertEqual(stores_status, 200)
            self.assertEqual(stores["stores"], [])
            self.assertEqual(pricing_status, 200)
            self.assertEqual(workspace_status, 403)
            self.assertIn("workspace.read", str(workspace["error"]))

    def test_registered_trials_can_only_manage_their_own_verified_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server("127.0.0.1", 0, Path(directory))
            server.pipeline_service.auth.bootstrap(
                "admin@northstar.local", "AdminPass123", "admin"
            )
            server.pipeline_service.team.identity("admin")
            server.pipeline_service.stores.client_factory = _client_factory
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

            def register(identifier: str, display_name: str) -> str:
                status, _, set_cookie = request(
                    "/api/auth/register",
                    method="POST",
                    body={
                        "identifier": identifier,
                        "password": "SellerPass123",
                        "display_name": display_name,
                    },
                )
                self.assertEqual(status, 201)
                parsed = SimpleCookie()
                parsed.load(set_cookie)
                return f"northstar_session={parsed['northstar_session'].value}"

            try:
                first_cookie = register("seller1@example.com", "卖家一")
                second_cookie = register("seller2@example.com", "卖家二")
                create_status, created, _ = request(
                    "/api/stores",
                    method="POST",
                    cookie=first_cookie,
                    body={
                        "store_id": "seller-one",
                        "name": "卖家一的 Ozon 店铺",
                        "client_id": "10001",
                        "api_key": "private-key-one",
                        "currency_code": "RUB",
                    },
                )
                first_status, first_stores, _ = request(
                    "/api/stores", cookie=first_cookie
                )
                second_status, second_stores, _ = request(
                    "/api/stores", cookie=second_cookie
                )
                verify_status, _, _ = request(
                    "/api/stores/seller-one/verify",
                    method="POST",
                    body={},
                    cookie=first_cookie,
                )
                foreign_verify_status, _, _ = request(
                    "/api/stores/seller-one/verify",
                    method="POST",
                    body={},
                    cookie=second_cookie,
                )
                delete_status, _, _ = request(
                    "/api/stores/seller-one",
                    method="DELETE",
                    cookie=first_cookie,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(create_status, 201)
            self.assertEqual(created["store"]["store_id"], "seller-one")
            self.assertNotIn("api_key", created["store"])
            self.assertTrue(created["store"]["api_key_configured"])
            self.assertEqual(first_status, 200)
            self.assertEqual(
                [item["store_id"] for item in first_stores["stores"]],
                ["seller-one"],
            )
            self.assertEqual(second_status, 200)
            self.assertEqual(second_stores["stores"], [])
            self.assertEqual(verify_status, 200)
            self.assertEqual(foreign_verify_status, 422)
            self.assertEqual(delete_status, 200)


if __name__ == "__main__":
    unittest.main()
