"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .toll_service import TollService


def toll_route(service: TollService, method: str, path: str, body: dict[str, Any],
               actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """把 /toll 前缀的请求分派到收费清算服务。"""

    parsed = urlparse(path)
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if method == "POST":
        if parsed.path == "/toll/segments":
            return _ok(service.register_segment(actor_id=actor_id, **body))
        if parsed.path == "/toll/segment-versions":
            return _ok(service.update_segment(actor_id=actor_id, **body))
        if parsed.path == "/toll/policies":
            return _ok(service.publish_policy(actor_id=actor_id, **body))
        if parsed.path == "/toll/policy-versions":
            return _ok(service.new_policy_version(actor_id=actor_id, **body))
        if parsed.path == "/toll/policies/withdraw":
            return _ok(service.withdraw_policy(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips":
            return _ok(service.open_trip(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips/pending-evidence":
            return _ok(service.mark_pending_evidence(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips/exit":
            return _ok(service.record_exit(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips/free-pass":
            return _ok(service.record_free_pass(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips/detour":
            return _ok(service.record_detour(actor_id=actor_id, **body))
        if parsed.path == "/toll/trips/settle":
            return _ok(service.settle_trip(actor_id=actor_id, **body))
        if parsed.path == "/toll/refunds":
            return _ok(service.create_refund(actor_id=actor_id, **body))
        if parsed.path == "/toll/surcharges":
            return _ok(service.create_surcharge(actor_id=actor_id, **body))
        if parsed.path == "/toll/disputes":
            return _ok(service.open_dispute(actor_id=actor_id, **body))
        if parsed.path == "/toll/disputes/resolve":
            return _ok(service.resolve_dispute(actor_id=actor_id, **body))
    if method == "GET":
        if parsed.path == "/toll/policies":
            return 200, {"items": service.list_policies(query.get("active_at", [None])[0])}
        if len(parts) == 3 and parts[:2] == ["toll", "policies"]:
            return 200, service.get_policy(parts[2])
        if len(parts) == 3 and parts[:2] == ["toll", "trips"]:
            return 200, service.explain_trip(parts[2])
        if len(parts) == 4 and parts[:2] == ["toll", "trips"] and parts[3] == "ledger":
            return 200, {"items": [entry.__dict__ for entry in service.trip_ledger(parts[2])]}
        if len(parts) == 4 and parts[:2] == ["toll", "trips"] and parts[3] == "replay":
            as_of = query.get("as_of", [""])[0]
            if not as_of:
                raise ValidationError("as_of 不能为空")
            return 200, service.replay_at(parts[2], as_of)
        if len(parts) == 4 and parts[:2] == ["toll", "trips"] and parts[3] == "replay-pricing":
            basis_time = query.get("basis_time", [""])[0]
            if not basis_time:
                raise ValidationError("basis_time 不能为空")
            return 200, service.replay_pricing(parts[2], basis_time)
    return None


def _ok(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, response


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if parsed.path.startswith("/toll/"):
            if not isinstance(service, TollService):
                return 404, {"error": "route_not_found", "message": "接口不存在"}
            toll_result = toll_route(service, method, path, body, actor_id)
            if toll_result is not None:
                return toll_result
            return 404, {"error": "route_not_found", "message": "接口不存在"}
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动综合交通协同（含收费清算）服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = TollService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
