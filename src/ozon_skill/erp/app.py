"""Dependency-free HTTP application for the local pipeline ERP."""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import re
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .service import PLAN_CATALOG, PipelineError, PipelineService


_PIPELINE_ROUTE = re.compile(r"^/api/pipelines/([^/]+)$")
_STAGE_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/stages/([1-5])$")
_ARTIFACT_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/artifacts/([1-5])$")
_AUDIT_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/audit$")
_STORE_ROUTE = re.compile(r"^/api/stores/([^/]+)$")
_STORE_VERIFY_ROUTE = re.compile(r"^/api/stores/([^/]+)/verify$")
_AI_PROVIDER_ROUTE = "/api/ai-provider"
_OBJECT_STORAGE_ROUTE = "/api/object-storage"
_DASHBOARD_ROUTE = "/api/dashboard"
_ACCOUNT_ROUTE = "/api/account"
_AUTH_SESSION_ROUTE = "/api/auth/session"
_AUTH_LOGIN_ROUTE = "/api/auth/login"
_AUTH_REGISTER_ROUTE = "/api/auth/register"
_AUTH_LOGOUT_ROUTE = "/api/auth/logout"
_REGISTRATIONS_ROUTE = "/api/registrations"
_REGISTRATION_ACTION_ROUTE = re.compile(
    r"^/api/registrations/([^/]+)/(authorize|revoke)$"
)
_REGISTRATION_ROUTE = re.compile(r"^/api/registrations/([^/]+)$")
_SESSION_COOKIE = "northstar_session"
_PLANS_ROUTE = "/api/plans"
_TEAM_ROUTE = "/api/team"
_TEAM_MEMBER_ROUTE = re.compile(r"^/api/team/([^/]+)$")
_OPERATIONS_ROUTE = "/api/operations"
_FINANCE_SETTINGS_ROUTE = "/api/finance-settings"
_PRICING_ROUTE = "/api/pricing/calculate"
_PRICING_CATEGORIES_ROUTE = "/api/pricing/categories"
_ACTIVITY_ROUTE = "/api/activity"
_NOTIFICATIONS_ROUTE = "/api/notifications"
_NOTIFICATIONS_READ_ROUTE = "/api/notifications/read"
_ALERT_CHANNELS_ROUTE = "/api/alert-channels"
_ALERT_CHANNELS_TEST_ROUTE = "/api/alert-channels/test"
_SUPPLIER_MONITOR_ROUTE = "/api/supplier-monitor"
_SUPPLIER_MONITOR_JOB_ROUTE = re.compile(r"^/api/supplier-monitor/jobs/([a-f0-9]{32})$")
_SUPPLIER_SCHEDULE_ROUTE = "/api/supplier-monitor/schedule"
_BATCH_IMPORTS_ROUTE = "/api/batch-imports"
_BATCH_IMPORT_ROUTE = re.compile(r"^/api/batch-imports/([a-f0-9]{32})$")
_BATCH_PROCESSES_ROUTE = "/api/batch-processes"
_BATCH_PROCESS_ROUTE = re.compile(r"^/api/batch-processes/([a-f0-9]{32})$")
_PRODUCTS_ROUTE = "/api/products"
_RESEARCH_ROUTE = "/api/research"
_RESEARCH_PLUGIN_ROUTE = "/api/research/plugin"
_COLLECTOR_1688_ROUTE = "/api/collector/1688"
_COLLECTOR_1688_PLUGIN_ROUTE = "/api/collector/1688/plugin"
_RESEARCH_RULES_ROUTE = "/api/research/rules"
_RESEARCH_INGEST_ROUTE = "/api/research/ingest"
_RESEARCH_CANDIDATE_ROUTE = re.compile(r"^/api/research/candidates/([^/]+)$")
_RESEARCH_SOURCE_ROUTE = re.compile(r"^/api/research/candidates/([^/]+)/source$")
_PRODUCT_BATCH_EDIT_ROUTE = "/api/products/batch-edit"
_PIPELINE_BATCH_DELETE_ROUTE = "/api/pipelines/batch-delete"
_RESEARCH_BATCH_DELETE_ROUTE = "/api/research/candidates/batch-delete"
_PRODUCT_VERSIONS_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/versions$")
_PRODUCT_VERSION_RESTORE_ROUTE = re.compile(
    r"^/api/pipelines/([^/]+)/versions/([a-f0-9]{24})/restore$"
)
_ASSETS_ROUTE = "/api/assets"
_ASSET_BATCH_DELETE_ROUTE = "/api/assets/batch-delete"
_ASSET_ROUTE = re.compile(r"^/api/assets/([a-f0-9]{24})$")
_ASSET_PREVIEW_ROUTE = re.compile(r"^/api/assets/([a-f0-9]{24})/preview$")
_IMAGES_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/images$")
_SKU_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/skus/([^/]+)$")
_SKU_RESTORE_ROUTE = re.compile(
    r"^/api/pipelines/([^/]+)/skus/([^/]+)/restore$"
)
_REQUIRED_ATTRIBUTES_ROUTE = re.compile(
    r"^/api/pipelines/([^/]+)/required-attributes$"
)
_CATEGORY_OPTIONS_ROUTE = re.compile(
    r"^/api/pipelines/([^/]+)/category-options$"
)
_ATTRIBUTE_VALUES_ROUTE = re.compile(
    r"^/api/pipelines/([^/]+)/attributes/(\d+)/values$"
)
_STATIC_ROOT = Path(__file__).with_name("static")
_PLUGIN_FILES = (
    "manifest.json",
    "content.js",
    "popup.html",
    "popup.css",
    "popup.js",
    "README.md",
)
_COLLECTOR_PLUGIN_FILES = (
    "manifest.json",
    "main-probe.js",
    "content.js",
    "popup.html",
    "popup.css",
    "popup.js",
    "README.md",
)


def _research_plugin_archive() -> bytes:
    return _extension_archive("northstar-ozon-research", _PLUGIN_FILES)


def _collector_1688_plugin_archive() -> bytes:
    return _extension_archive("northstar-1688-collector", _COLLECTOR_PLUGIN_FILES)


def _extension_archive(folder: str, files: Sequence[str]) -> bytes:
    candidates = (
        Path.cwd() / "browser-extension" / folder,
        Path(__file__).resolve().parents[3]
        / "browser-extension"
        / folder,
    )
    extension_root = next(
        (path for path in candidates if all((path / name).is_file() for name in files)),
        None,
    )
    if extension_root is None:
        raise PipelineError(f"服务器缺少插件文件：{folder}，请重新部署当前版本", status=500)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(
                extension_root / name,
                arcname=f"{folder}/{name}",
            )
    return output.getvalue()


class ERPRequestHandler(BaseHTTPRequestHandler):
    server_version = "OzonPipelineERP/1.0"

    @property
    def service(self) -> PipelineService:
        return self.server.pipeline_service  # type: ignore[attr-defined]

    def _username(self) -> str:
        user = self._current_user()
        return str(user.get("username") or "") if user else ""

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(_SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _current_user(self) -> dict[str, Any] | None:
        if hasattr(self, "_authenticated_user"):
            return self._authenticated_user
        self._authenticated_user = self.service.auth_session(self._session_token())
        return self._authenticated_user

    def _require_authenticated(self) -> dict[str, Any]:
        user = self._current_user()
        if user is None:
            raise PipelineError("请先登录后再继续", status=401)
        return user

    def _require(self, permission: str) -> None:
        self.service.require_permission(self._username(), permission)

    def _begin_activity(self, path: str) -> None:
        if path != _PRICING_ROUTE:
            self._pending_activity = path

    def _finish_activity(self, status: int) -> None:
        path = getattr(self, "_pending_activity", None)
        if not path:
            return
        self._pending_activity = None
        try:
            self.service.record_activity(
                actor=self._username(),
                method=self.command,
                path=path,
                status=status,
                remote_address=self.headers.get("X-Real-IP") or self.client_address[0],
            )
        except Exception:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra_headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'",
        )
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()

    def _json(
        self,
        value: Any,
        status: int = 200,
        *,
        extra_headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._finish_activity(status)
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self._headers(
            status, "application/json; charset=utf-8", len(body), extra_headers
        )
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _session_cookie(self, token: str, *, clear: bool = False) -> tuple[str, str]:
        secure = self.headers.get("X-Forwarded-Proto", "").casefold() == "https"
        flags = f"HttpOnly; {'Secure; ' if secure else ''}SameSite=Lax"
        value = (
            f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; {flags}"
            if clear
            else f"{_SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; {flags}"
        )
        return "Set-Cookie", value

    def _plugin_download(self) -> None:
        body = _research_plugin_archive()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            'attachment; filename="northstar-ozon-research.zip"',
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _collector_1688_plugin_download(self) -> None:
        body = _collector_1688_plugin_archive()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            'attachment; filename="northstar-1688-collector.zip"',
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _image(self, body: bytes, content_type: str) -> None:
        self._finish_activity(200)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message, "status": status}, status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PipelineError("无效 Content-Length") from exc
        if length > 2 * 1024 * 1024:
            raise PipelineError("请求体超过 2 MiB", status=413)
        if length == 0:
            return {}
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise PipelineError("请求必须使用 application/json", status=415)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError("请求体不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise PipelineError("请求体必须是 JSON 对象")
        return value

    def _serve_static(self, relative: str) -> None:
        filename = relative or "index.html"
        if filename not in {
            "index.html",
            "app.js",
            "styles.css",
            "progress.css",
            "commercial.css",
            "assets.css",
            "tech-ui.css",
            "login.html",
            "auth.css",
            "auth.js",
        }:
            self._error("Not found", 404)
            return
        path = _STATIC_ROOT / filename
        if not path.exists():
            self._error("Static asset missing", 500)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/") and path not in {
                "/api/health",
                _AUTH_SESSION_ROUTE,
            }:
                self._require_authenticated()
            if path.startswith("/api/") and path not in {
                "/api/health",
                _AUTH_SESSION_ROUTE,
                _ACCOUNT_ROUTE,
                _PLANS_ROUTE,
                _TEAM_ROUTE,
                "/api/stores",
                _PRICING_CATEGORIES_ROUTE,
                _REGISTRATIONS_ROUTE,
            }:
                self._require("workspace.read")
            if path == "/api/health":
                self._json({"status": "ok", "service": "1688-to-ozon-erp"})
            elif path == _AUTH_SESSION_ROUTE:
                user = self._current_user()
                self._json({"authenticated": user is not None, "user": user})
            elif path == "/api/config-status":
                self._json(self.service.config_status())
            elif path == "/api/pipelines":
                self._json({"pipelines": self.service.list_pipelines()})
            elif path == _DASHBOARD_ROUTE:
                self._json(self.service.dashboard())
            elif path == _ACCOUNT_ROUTE:
                self._json(
                    self.service.commercial_status(
                        self._username()
                    )
                )
            elif path == _PLANS_ROUTE:
                self._json({"plans": PLAN_CATALOG})
            elif path == _TEAM_ROUTE:
                self._json(self.service.team_status(self._username()))
            elif path == _REGISTRATIONS_ROUTE:
                self._require("team.manage")
                self._json(self.service.registered_accounts(self._username()))
            elif path == _OPERATIONS_ROUTE:
                self._require("orders.read")
                self._require("inventory.read")
                self._json(
                    self.service.operations_dashboard(
                        store_id=str((query.get("store_id") or [""])[0]),
                        days=(query.get("days") or ["30"])[0],
                    )
                )
            elif path == _FINANCE_SETTINGS_ROUTE:
                self._require("finance.read")
                self._json(
                    {
                        "settings": self.service.finance_settings(
                            str((query.get("store_id") or [""])[0])
                        )
                    }
                )
            elif path == _PRICING_CATEGORIES_ROUTE:
                self._require("pricing.use")
                self._json(
                    self.service.pricing_categories(
                        (query.get("q") or [""])[0],
                        (query.get("limit") or ["30"])[0],
                    )
                )
            elif path == _ACTIVITY_ROUTE:
                self._require("audit.read")
                self._json(
                    self.service.activity_log((query.get("limit") or ["100"])[0])
                )
            elif path == _NOTIFICATIONS_ROUTE:
                self._json(self.service.notification_center(self._username()))
            elif path == _ALERT_CHANNELS_ROUTE:
                self._require("team.manage")
                self._json({"channels": self.service.alert_channels_status()})
            elif path == _SUPPLIER_MONITOR_ROUTE:
                self._json(self.service.supplier_monitor_status())
            elif path == _SUPPLIER_SCHEDULE_ROUTE:
                self._require("team.manage")
                self._json({"schedule": self.service.supplier_schedule_status()})
            elif match := _SUPPLIER_MONITOR_JOB_ROUTE.fullmatch(path):
                self._json(self.service.supplier_monitor_job_status(match.group(1)))
            elif path == _BATCH_IMPORTS_ROUTE:
                self._json(
                    self.service.list_batch_imports(
                        (query.get("limit") or ["20"])[0]
                    )
                )
            elif match := _BATCH_IMPORT_ROUTE.fullmatch(path):
                self._json(self.service.batch_import_status(match.group(1)))
            elif path == _BATCH_PROCESSES_ROUTE:
                self._json(
                    self.service.list_batch_processes(
                        (query.get("limit") or ["20"])[0]
                    )
                )
            elif match := _BATCH_PROCESS_ROUTE.fullmatch(path):
                self._json(self.service.batch_process_status(match.group(1)))
            elif path == _PRODUCTS_ROUTE:
                self._json(self.service.product_catalog())
            elif path == _RESEARCH_ROUTE:
                self._json(self.service.market_research_status())
            elif path == _RESEARCH_PLUGIN_ROUTE:
                self._plugin_download()
            elif path == _COLLECTOR_1688_PLUGIN_ROUTE:
                self._collector_1688_plugin_download()
            elif match := _PRODUCT_VERSIONS_ROUTE.fullmatch(path):
                self._json(
                    self.service.product_versions(unquote(match.group(1)))
                )
            elif path == _ASSETS_ROUTE:
                self._json(self.service.asset_library())
            elif match := _ASSET_PREVIEW_ROUTE.fullmatch(path):
                body, content_type = self.service.asset_preview(match.group(1))
                self._image(body, content_type)
            elif path == "/api/stores":
                self._require("stores.manage")
                self._json(
                    {"stores": self.service.list_stores(self._username())}
                )
            elif path == _AI_PROVIDER_ROUTE:
                self._json({"provider": self.service.ai_provider_status()})
            elif path == _OBJECT_STORAGE_ROUTE:
                self._json({"storage": self.service.object_storage_status()})
            elif match := _PIPELINE_ROUTE.fullmatch(path):
                self._json(self.service.get_pipeline(unquote(match.group(1))))
            elif match := _ARTIFACT_ROUTE.fullmatch(path):
                self._json(self.service.read_artifact(unquote(match.group(1)), int(match.group(2))))
            elif match := _AUDIT_ROUTE.fullmatch(path):
                self._json(self.service.audit(unquote(match.group(1))))
            elif match := _IMAGES_ROUTE.fullmatch(path):
                self._json(self.service.list_images(unquote(match.group(1))))
            elif match := _CATEGORY_OPTIONS_ROUTE.fullmatch(path):
                self._json(
                    self.service.category_options(
                        unquote(match.group(1)),
                        query=str((query.get("q") or [""])[0]),
                        store_id=str((query.get("store_id") or [""])[0]),
                    )
                )
            elif match := _REQUIRED_ATTRIBUTES_ROUTE.fullmatch(path):
                self._json(
                    self.service.required_attribute_options(unquote(match.group(1)))
                )
            elif match := _ATTRIBUTE_VALUES_ROUTE.fullmatch(path):
                self._json(
                    self.service.attribute_dictionary_options(
                        unquote(match.group(1)), int(match.group(2))
                    )
                )
            elif path in {"/", "/index.html"}:
                if self._current_user() is None:
                    self._redirect("/login")
                else:
                    self._serve_static("index.html")
            elif path in {"/login", "/login.html"}:
                if self._current_user() is not None:
                    self._redirect("/")
                else:
                    self._serve_static("login.html")
            elif path.startswith("/static/"):
                self._serve_static(path.removeprefix("/static/"))
            else:
                self._error("Not found", 404)
        except PipelineError as exc:
            self._error(str(exc), exc.status)
        except Exception as exc:
            self._error(f"服务器处理失败：{exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._begin_activity(path)
        try:
            body = self._body()
            if path == _AUTH_REGISTER_ROUTE:
                result = self.service.register_account(body)
                self._json(
                    {"user": result["user"], "member": result["member"]},
                    201,
                    extra_headers=[self._session_cookie(result["token"])],
                )
                return
            if path == _AUTH_LOGIN_ROUTE:
                result = self.service.login_account(body)
                self._json(
                    {"user": result["user"]},
                    extra_headers=[self._session_cookie(result["token"])],
                )
                return
            if path == _AUTH_LOGOUT_ROUTE:
                self.service.logout_account(self._session_token())
                self._json(
                    {"status": "logged_out"},
                    extra_headers=[self._session_cookie("", clear=True)],
                )
                return
            self._require_authenticated()
            if registration_match := _REGISTRATION_ACTION_ROUTE.fullmatch(path):
                self._require("team.manage")
                action = registration_match.group(2)
                self._json(
                    {
                        "account": self.service.set_account_authorized(
                            self._username(),
                            unquote(registration_match.group(1)),
                            action == "authorize",
                        )
                    }
                )
                return
            if path == _NOTIFICATIONS_READ_ROUTE:
                self._json(
                    self.service.mark_notifications_read(self._username(), body)
                )
                return
            if path == _ALERT_CHANNELS_ROUTE:
                self._require("team.manage")
                self._json(
                    {"channels": self.service.configure_alert_channels(body)}
                )
                return
            if path == _ALERT_CHANNELS_TEST_ROUTE:
                self._require("team.manage")
                self._json(self.service.test_alert_channels())
                return
            if path == _SUPPLIER_MONITOR_ROUTE:
                self._require("pipeline.create")
                self._json(self.service.start_supplier_monitor(body, self._username()), 202)
                return
            if path == _SUPPLIER_SCHEDULE_ROUTE:
                self._require("team.manage")
                self._json({"schedule": self.service.save_supplier_schedule(body, self._username())})
                return
            if path == _BATCH_IMPORTS_ROUTE:
                self._require("pipeline.create")
                self._json(
                    self.service.start_batch_import(body, self._username()), 202
                )
                return
            if path == _BATCH_PROCESSES_ROUTE:
                self._require("stage.run")
                if int(body.get("target_stage") or 0) >= 3:
                    self._require("images.manage")
                self._json(
                    self.service.start_batch_process(body, self._username()), 202
                )
                return
            if path == _PRODUCT_BATCH_EDIT_ROUTE:
                self._require("stage.run")
                self._json(
                    self.service.batch_edit_products(body, self._username())
                )
                return
            if path == _PIPELINE_BATCH_DELETE_ROUTE:
                self._require("pipeline.create")
                self._json(
                    self.service.delete_pipelines(body, self._username())
                )
                return
            if path == _RESEARCH_BATCH_DELETE_ROUTE:
                self._require("stage.run")
                self._json(self.service.delete_market_candidates(body))
                return
            if path == _ASSET_BATCH_DELETE_ROUTE:
                self._require("storage.manage")
                self._json(self.service.delete_assets(body))
                return
            if path == _RESEARCH_RULES_ROUTE:
                self._require("stage.run")
                self._json({"rules": self.service.save_market_research_rules(body, self._username())})
                return
            if path == _RESEARCH_INGEST_ROUTE:
                self._require("pipeline.create")
                self._json(self.service.ingest_market_candidates(body, self._username()))
                return
            if source_match := _RESEARCH_SOURCE_ROUTE.fullmatch(path):
                self._require("pipeline.create")
                self._json(self.service.source_market_candidate(
                    unquote(source_match.group(1)), body, self._username()
                ), 201)
                return
            if candidate_match := _RESEARCH_CANDIDATE_ROUTE.fullmatch(path):
                self._require("stage.run")
                self._json({"candidate": self.service.update_market_candidate(
                    unquote(candidate_match.group(1)), body, self._username()
                )})
                return
            if version_match := _PRODUCT_VERSION_RESTORE_ROUTE.fullmatch(path):
                self._require("stage.run")
                self._json(
                    self.service.restore_product_version(
                        unquote(version_match.group(1)),
                        version_match.group(2),
                        self._username(),
                    )
                )
                return
            if path == _TEAM_ROUTE:
                self._json(
                    {"member": self.service.save_team_member(self._username(), body)},
                    201,
                )
                return
            if path == "/api/stores":
                self._require("stores.manage")
                self._json(
                    {
                        "store": self.service.authorize_store(
                            body, self._username()
                        )
                    },
                    201,
                )
                return
            if path == _AI_PROVIDER_ROUTE:
                self._require("ai.manage")
                self._json(
                    {"provider": self.service.configure_ai_provider(body)}, 201
                )
                return
            if path == _OBJECT_STORAGE_ROUTE:
                self._require("storage.manage")
                self._json(
                    {"storage": self.service.configure_object_storage(body)}, 201
                )
                return
            if path == _FINANCE_SETTINGS_ROUTE:
                self._require("finance.manage")
                store_id = str(body.get("store_id") or "").strip()
                self._json(
                    {
                        "settings": self.service.save_finance_settings(
                            store_id, body.get("settings") or {}
                        )
                    }
                )
                return
            if path == _PRICING_ROUTE:
                self._require("pricing.use")
                self._json({"calculation": self.service.pricing_calculation(body)})
                return
            if store_match := _STORE_VERIFY_ROUTE.fullmatch(path):
                self._require("stores.manage")
                self._json(
                    {
                        "store": self.service.verify_store(
                            unquote(store_match.group(1)), self._username()
                        )
                    }
                )
                return
            if path == "/api/pipelines":
                self._require("pipeline.create")
                self._json(self.service.run_stage_1(body.get("offer_id")), 201)
                return
            if path == _COLLECTOR_1688_ROUTE:
                self._require("pipeline.create")
                self._json(
                    self.service.ingest_1688_browser_capture(
                        body, self._username()
                    ),
                    201,
                )
                return
            if sku_restore_match := _SKU_RESTORE_ROUTE.fullmatch(path):
                self._require("stage.run")
                self._json(
                    self.service.set_sku_excluded(
                        unquote(sku_restore_match.group(1)),
                        unquote(sku_restore_match.group(2)),
                        excluded=False,
                        actor=self._username(),
                    )
                )
                return
            if image_match := _IMAGES_ROUTE.fullmatch(path):
                self._require("images.manage")
                self._json(
                    self.service.save_image_selection(
                        unquote(image_match.group(1)), body.get("image_plan")
                    )
                )
                return
            match = _STAGE_ROUTE.fullmatch(path)
            if not match:
                self._error("Not found", 404)
                return
            pipeline_id = unquote(match.group(1))
            stage = int(match.group(2))
            if stage == 1:
                self._require("pipeline.create")
            elif stage == 3:
                self._require("images.manage")
            elif stage == 5:
                self._require("listing.publish")
            else:
                self._require("stage.run")
            if stage == 1:
                result = self.service.run_stage_1(pipeline_id)
            elif stage == 2:
                result = self.service.run_stage_2(
                    pipeline_id,
                    confidence_threshold=body.get("confidence_threshold", 0.70),
                    store_id=body.get("store_id"),
                    manual_category_id=body.get("manual_category_id"),
                    manual_type_id=body.get("manual_type_id"),
                )
            elif stage == 3:
                result = self.service.start_stage_3(
                    pipeline_id,
                    media_manifest=body.get("media_manifest"),
                    media_mode=body.get("media_mode", "image_plan"),
                    text_mode=body.get("text_mode", "ai_generate"),
                )
            elif stage == 4:
                result = self.service.run_stage_4(
                    pipeline_id, store_config=body.get("store_config")
                )
            else:
                result = self.service.start_stage_5(
                    pipeline_id, request_execute=body.get("execute") is True
                )
            self._json(result)
        except PipelineError as exc:
            self._error(str(exc), exc.status)
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._error(f"阶段执行失败：{exc}", 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._begin_activity(path)
        try:
            self._require_authenticated()
            if registration_match := _REGISTRATION_ROUTE.fullmatch(path):
                self._require("team.manage")
                self._json(
                    self.service.delete_registered_account(
                        self._username(), unquote(registration_match.group(1))
                    )
                )
                return
            if path == _AI_PROVIDER_ROUTE:
                self._require("ai.manage")
                self.service.delete_ai_provider()
                self._json({"status": "deleted"})
                return
            if path == _ALERT_CHANNELS_ROUTE:
                self._require("team.manage")
                self.service.delete_alert_channels()
                self._json({"status": "deleted"})
                return
            if path == _SUPPLIER_SCHEDULE_ROUTE:
                self._require("team.manage")
                self.service.delete_supplier_schedule()
                self._json({"status": "deleted"})
                return
            if candidate_match := _RESEARCH_CANDIDATE_ROUTE.fullmatch(path):
                self._require("stage.run")
                self.service.delete_market_candidate(unquote(candidate_match.group(1)))
                self._json({"status": "deleted"})
                return
            if asset_match := _ASSET_ROUTE.fullmatch(path):
                self._require("storage.manage")
                self._json(self.service.delete_asset(asset_match.group(1)))
                return
            if path == _OBJECT_STORAGE_ROUTE:
                self._require("storage.manage")
                self.service.delete_object_storage()
                self._json({"status": "deleted"})
                return
            if team_match := _TEAM_MEMBER_ROUTE.fullmatch(path):
                self.service.delete_team_member(
                    self._username(), unquote(team_match.group(1))
                )
                self._json({"status": "deleted"})
                return
            if sku_match := _SKU_ROUTE.fullmatch(path):
                self._require("stage.run")
                self._json(
                    self.service.set_sku_excluded(
                        unquote(sku_match.group(1)),
                        unquote(sku_match.group(2)),
                        excluded=True,
                        actor=self._username(),
                    )
                )
                return
            match = _STORE_ROUTE.fullmatch(path)
            if not match:
                self._error("Not found", 404)
                return
            self._require("stores.manage")
            self.service.delete_store(unquote(match.group(1)), self._username())
            self._json({"status": "deleted"})
        except PipelineError as exc:
            self._error(str(exc), exc.status)
        except Exception as exc:
            self._error(f"服务器处理失败：{exc}", 500)


class ERPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: PipelineService) -> None:
        self.pipeline_service = service
        super().__init__(address, ERPRequestHandler)


def create_server(host: str = "127.0.0.1", port: int = 8080, work_root: Path | None = None) -> ERPServer:
    return ERPServer((host, port), PipelineService(work_root or Path("work")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the 1688 to OZON web ERP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--work-root", type=Path, default=Path("work"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    server = create_server(args.host, args.port, args.work_root)
    print(f"ERP listening on http://{args.host}:{args.port}")
    print(f"Artifacts: {args.work_root.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
