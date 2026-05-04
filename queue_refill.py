#!/usr/bin/env python3
"""Social OS → x-engage review queue refill contract.

This module is intentionally review-queue-only. It calls analyze.run() with
review_queue_only=True so x-engage owns x-monitor window loading, filtering,
routing, pending queue generation, Social OS review-row writes, and telemetry.
It never imports or invokes execute_actions.py, Playwright, or live X actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import analyze

ALLOWED_TRIGGERS = {"manual", "scheduled"}
REQUIRED_MODE = "review_queue_only"
DEFAULT_SOURCE = "social-os-ui"


def _json_response(ok: bool, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "status": status, "message": message, **extra}


def _coerce_bool_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}:
        return True
    return False


def _clean_request_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(payload.get("source") or DEFAULT_SOURCE),
        "mode": str(payload.get("mode") or REQUIRED_MODE),
        "allow_live_actions": False,
        "requested_by": str(payload.get("requested_by") or "unknown"),
        "requested_at": str(payload.get("requested_at") or datetime.now(timezone.utc).isoformat()),
    }


def _validate_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "Request body must be a JSON object."
    mode = str(payload.get("mode") or "").strip()
    if mode != REQUIRED_MODE:
        return False, "mode must be review_queue_only."
    if not _coerce_bool_false(payload.get("allow_live_actions")):
        return False, "allow_live_actions=false is required; live X actions are outside the refill contract."
    trigger = str(payload.get("trigger") or "manual").strip().lower()
    if trigger not in ALLOWED_TRIGGERS:
        return False, "trigger must be manual or scheduled."
    return True, "accepted"


def _stable_run_id(payload: dict[str, Any]) -> str:
    raw = str(payload.get("run_id") or "").strip()
    if raw:
        return raw
    trigger = str(payload.get("trigger") or "manual").strip().lower()
    requested_at = str(payload.get("requested_at") or datetime.now(timezone.utc).isoformat())
    source = str(payload.get("source") or DEFAULT_SOURCE)
    requested_by = str(payload.get("requested_by") or "unknown")
    compact = requested_at.replace(":", "").replace("-", "").replace("+", "Z")[:24]
    digest = hashlib.sha256(
        json.dumps(
            {"trigger": trigger, "requested_at": requested_at, "source": source, "requested_by": requested_by},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:8]
    return f"x-engage-{trigger}-{compact}-{digest}"


def _normalize_response(result: dict[str, Any], run_id: str) -> dict[str, Any]:
    social_summary = result.get("social_os_summary") or {}
    queue_summary = result.get("queue_summary") or {}
    trigger_info = result.get("trigger") or {}
    created = int(social_summary.get("created") or 0)
    refreshed = int(social_summary.get("refreshed") or 0)
    skipped = int(social_summary.get("skipped") or 0)
    response = _json_response(
        True,
        "accepted",
        f"x-engage review queue refill accepted: {created} created, {refreshed} refreshed, {skipped} skipped.",
        created=created,
        refreshed=refreshed,
        skipped=skipped,
        total=int(social_summary.get("total") or created + refreshed + skipped),
        run_id=str(trigger_info.get("run_id") or run_id),
        trigger=str(trigger_info.get("trigger") or "manual"),
        owner="x-engage",
        service="x-engage",
        mode=REQUIRED_MODE,
        allow_live_actions=False,
        queue_summary=queue_summary,
        social_os_summary=social_summary,
        signal_counts=(result.get("trigger") or {}).get("signal_counts") or result.get("signal_counts"),
        filter_summary=result.get("filter_summary"),
    )
    if trigger_info.get("next_run_at"):
        response["next_run_at"] = trigger_info["next_run_at"]
    else:
        response["schedule_not_configured"] = True
    return response


def handle_refill_request(payload: dict[str, Any]) -> dict[str, Any]:
    valid, message = _validate_payload(payload)
    if not valid:
        return _json_response(False, "rejected", message, created=0, refreshed=0, skipped=0)

    trigger = str(payload.get("trigger") or "manual").strip().lower()
    run_id = _stable_run_id(payload)
    request_metadata = _clean_request_metadata(payload)
    result = analyze.run(
        dry_run=False,
        skip_llm=bool(payload.get("skip_llm", True)),
        trigger=trigger,
        run_id=run_id,
        review_queue_only=True,
        request_metadata=request_metadata,
    )
    return _normalize_response(result, run_id)


class _RefillHandler(BaseHTTPRequestHandler):
    server_version = "x-engage-queue-refill/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in {"/health", "/queue-refill/health"}:
            self._send(404, _json_response(False, "not_found", "Use POST /queue-refill."))
            return
        self._send(200, _json_response(True, "ok", "x-engage queue refill endpoint is healthy.", service="x-engage"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in {"/queue-refill", "/refill"}:
            self._send(404, _json_response(False, "not_found", "Use POST /queue-refill."))
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            response = handle_refill_request(payload)
            self._send(202 if response.get("ok") else 400, response)
        except Exception as exc:
            self._send(500, _json_response(False, "error", str(exc), created=0, refreshed=0, skipped=0))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[queue-refill-http] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send(self, status_code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), _RefillHandler)
    print(f"[queue-refill-http] listening on http://{host}:{port}/queue-refill", file=sys.stderr)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="x-engage Social OS review queue refill contract")
    parser.add_argument("--serve", action="store_true", help="Run a tiny HTTP server exposing POST /queue-refill")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host for --serve")
    parser.add_argument("--port", type=int, default=8788, help="HTTP bind port for --serve")
    parser.add_argument("--payload", help="JSON payload string; defaults to stdin for one-shot CLI mode")
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.host, args.port)
        return 0

    raw = args.payload if args.payload is not None else sys.stdin.read()
    payload = json.loads(raw or "{}")
    response = handle_refill_request(payload)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
