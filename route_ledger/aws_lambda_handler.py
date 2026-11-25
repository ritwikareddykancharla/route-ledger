"""AWS Lambda Function URL & Local HTTP Handler (`route_ledger/aws_lambda_handler.py`).

Zero-dependency AWS Lambda Function URL (Payload v2.0) and API Gateway (v1.0) handler:
  - `GET  /`            -> Service overview HTML (or `docs/index.html` if bundled)
  - `GET  /api/health`  -> Runtime & supported modes health check
  - `POST /api/run`     -> Live execution of `demo-all`, `router`, `ledger`, `workflow`,
                           or custom single-incident `diagnose` / single-event `settle` requests.
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import platform
import time
from typing import Any, Dict, Optional
import urllib.parse

from . import __version__
from .atrops_pareto import solve_atrops_pareto_workbench
from .cli import SUPPORTED_MODES, execute_cli_mode
from .leo_flight_mcp import run_leo_diagnose_workbench
from .noc_workflow import IncidentTelemetrySnapshot, OperationalWorkflowEngine


DOCS_INDEX_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "docs" / "index.html",
    Path(__file__).resolve().parent / "docs" / "index.html",
)

CORS_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _load_index_html() -> str:
    for candidate in DOCS_INDEX_CANDIDATES:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>RouteLedger API</title></head>"
        "<body style='font-family:monospace;padding:2rem'>"
        "<h1>RouteLedger Live Endpoint</h1>"
        "<p>GET <code>/api/health</code> or POST JSON to <code>/api/run</code> "
        "(<code>{\"mode\": \"demo-all\"}</code>).</p></body></html>"
    )


def _json_response(payload: Dict[str, Any], status_code: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "application/json; charset=utf-8",
        },
        "body": json.dumps(payload, indent=2),
    }


def _html_response(html_body: str, status_code: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
        },
        "body": html_body,
    }


def _execute_live_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    mode = str(body.get("mode", "demo-all")).strip()

    if mode in SUPPORTED_MODES:
        return execute_cli_mode(
            mode,
            seed=int(body.get("seed", 42)),
            demands=max(10, min(500, int(body.get("demands", 300)))),
            events=max(20, min(5000, int(body.get("events", 2000)))),
            incidents=max(10, min(500, int(body.get("incidents", 150)))),
        )

    if mode == "diagnose":
        engine = OperationalWorkflowEngine()
        snap = IncidentTelemetrySnapshot(
            incident_id=str(body.get("incident_id", "INC-LIVE")),
            alarm_type=str(body.get("alarm_type", "GROUND_GATEWAY_SNR_DROP")),
            region=str(body.get("region", "us-west-2")),
            resource_id=str(body.get("resource_id", "gw-us-west-01")),
            metrics=dict(body.get("metrics") or {"rf_lna_bias_ma": 118.0, "rain_rate_mm_hr": 32.0, "diversity_gw_snr_db": 16.5}),
            raw_logs=list(body.get("raw_logs") or []),
        )
        res = engine.diagnose(snap)
        return {
            "service": "RouteLedger",
            "version": __version__,
            "mode": mode,
            "wall_time_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            "diagnosis": res.to_dict(),
        }

    raise ValueError(
        f"Unsupported mode '{mode}'. Choose from {list(SUPPORTED_MODES) + ['diagnose']}."
    )


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """AWS Lambda Function URL (v2.0) and API Gateway (v1.0) entrypoint."""
    req_ctx = event.get("requestContext") or {}
    http_info = req_ctx.get("http") or {}
    method = (http_info.get("method") or event.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or event.get("path") or "/"
    query_params = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if method == "GET" and raw_path in ("/", "/index.html"):
        return _html_response(_load_index_html())

    if method == "GET" and raw_path == "/api/health":
        return _json_response(
            {
                "status": "ok",
                "service": "RouteLedger",
                "version": __version__,
                "python_version": platform.python_version(),
                "supported_modes": list(SUPPORTED_MODES) + ["diagnose"],
            }
        )

    if raw_path == "/api/atrops/pareto" and method in ("GET", "POST"):
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded") and isinstance(raw_body, str):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body_in = json.loads(raw_body) if (method == "POST" and isinstance(raw_body, str)) else {}
        merged = {**query_params, **(body_in if isinstance(body_in, dict) else {})}
        return _json_response(
            solve_atrops_pareto_workbench(
                disruption=str(merged.get("disruption", "nominal")),
                volume_pkgs=int(merged.get("volume_pkgs", 3600)),
                sla_deadline_hr=float(merged.get("sla_deadline_hr", 34.0)),
            ),
            status_code=200,
        )

    if raw_path == "/api/leo/diagnose" and method in ("GET", "POST"):
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded") and isinstance(raw_body, str):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body_in = json.loads(raw_body) if (method == "POST" and isinstance(raw_body, str)) else {}
        merged = {**query_params, **(body_in if isinstance(body_in, dict) else {})}
        return _json_response(
            run_leo_diagnose_workbench(
                scenario_key=str(merged.get("scenario_key", "beam_handover_ephemeris_slip")),
                cpp_preset=str(merged.get("cpp_preset", "compliant_ccsds_framer")),
            ),
            status_code=200,
        )

    if raw_path == "/api/run":
        try:
            if method == "GET":
                payload_in: Dict[str, Any] = dict(query_params)
            else:
                raw_body = event.get("body") or "{}"
                if event.get("isBase64Encoded") and isinstance(raw_body, str):
                    raw_body = base64.b64decode(raw_body).decode("utf-8")
                payload_in = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
            return _json_response(_execute_live_payload(payload_in), status_code=200)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"status": "error", "error": str(exc)}, status_code=400)

    return _json_response({"status": "not_found", "path": raw_path}, status_code=404)


lambda_handler = handler


class _LocalRouteLedgerHTTPHandler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body_str = ""
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            body_str = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"

        resp = handler(
            {
                "rawPath": parsed.path,
                "queryStringParameters": qs,
                "body": body_str,
                "requestContext": {"http": {"method": method}},
            }
        )
        raw_bytes = resp["body"].encode("utf-8")
        self.send_response(int(resp["statusCode"]))
        for k, v in resp.get("headers", {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw_bytes)))
        self.end_headers()
        self.wfile.write(raw_bytes)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run RouteLedger HTTP/Lambda server locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8086)
    args = parser.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), _LocalRouteLedgerHTTPHandler)
    print(f"RouteLedger live server listening on http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
