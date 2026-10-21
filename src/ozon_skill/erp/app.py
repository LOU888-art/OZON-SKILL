"""Dependency-free HTTP application for the local pipeline ERP."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .service import PipelineError, PipelineService


_PIPELINE_ROUTE = re.compile(r"^/api/pipelines/([^/]+)$")
_STAGE_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/stages/([1-5])$")
_ARTIFACT_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/artifacts/([1-5])$")
_AUDIT_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/audit$")
_STORE_ROUTE = re.compile(r"^/api/stores/([^/]+)$")
_STORE_VERIFY_ROUTE = re.compile(r"^/api/stores/([^/]+)/verify$")
_AI_PROVIDER_ROUTE = "/api/ai-provider"
_IMAGES_ROUTE = re.compile(r"^/api/pipelines/([^/]+)/images$")
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


class ERPRequestHandler(BaseHTTPRequestHandler):
    server_version = "OzonPipelineERP/1.0"

    @property
    def service(self) -> PipelineService:
        return self.server.pipeline_service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _headers(self, status: int, content_type: str, length: int) -> None:
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
        self.end_headers()

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
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
        if filename not in {"index.html", "app.js", "styles.css"}:
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
            if path == "/api/health":
                self._json({"status": "ok", "service": "1688-to-ozon-erp"})
            elif path == "/api/config-status":
                self._json(self.service.config_status())
            elif path == "/api/pipelines":
                self._json({"pipelines": self.service.list_pipelines()})
            elif path == "/api/stores":
                self._json({"stores": self.service.list_stores()})
            elif path == _AI_PROVIDER_ROUTE:
                self._json({"provider": self.service.ai_provider_status()})
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
                self._serve_static("index.html")
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
        try:
            body = self._body()
            if path == "/api/stores":
                self._json({"store": self.service.authorize_store(body)}, 201)
                return
            if path == _AI_PROVIDER_ROUTE:
                self._json(
                    {"provider": self.service.configure_ai_provider(body)}, 201
                )
                return
            if store_match := _STORE_VERIFY_ROUTE.fullmatch(path):
                self._json(
                    {"store": self.service.verify_store(unquote(store_match.group(1)))}
                )
                return
            if path == "/api/pipelines":
                self._json(self.service.run_stage_1(body.get("offer_id")), 201)
                return
            if image_match := _IMAGES_ROUTE.fullmatch(path):
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
                    media_mode=body.get("media_mode", "alphashop_translate"),
                    text_mode=body.get("text_mode", "built_in"),
                )
            elif stage == 4:
                result = self.service.run_stage_4(
                    pipeline_id, store_config=body.get("store_config")
                )
            else:
                result = self.service.run_stage_5(
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
        try:
            if path == _AI_PROVIDER_ROUTE:
                self.service.delete_ai_provider()
                self._json({"status": "deleted"})
                return
            match = _STORE_ROUTE.fullmatch(path)
            if not match:
                self._error("Not found", 404)
                return
            self.service.delete_store(unquote(match.group(1)))
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
