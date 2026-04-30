from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"
DEFAULT_BROWSER_PROFILE_ROOT = Path.home() / ".x-engage-browser"

SOCIAL_RUNTIME_SERVICE = "x-engage"


def _redact_path(path_value: str | None) -> str | None:
    """Return only the last path component so Social OS never receives local paths."""
    if not path_value:
        return None
    try:
        return Path(path_value).expanduser().name or "configured"
    except Exception:
        return "configured"


def safe_session_health_summary(account: dict[str, Any]) -> dict[str, Any]:
    """Build a Social OS-safe account/session summary without exposing host paths or secrets."""
    raw_profile = str(account.get("browser_profile") or "").strip()
    profile_path = Path(raw_profile).expanduser() if raw_profile else None
    profile_exists = bool(profile_path.exists()) if profile_path else False
    return {
        "account_id": account.get("id"),
        "handle": account.get("handle") or account.get("id"),
        "label": account.get("label"),
        "lane": account.get("lane"),
        "profile_configured": bool(raw_profile),
        "profile_key": _redact_path(raw_profile),
        "profile_exists": profile_exists,
        "action_types": account.get("action_types", []),
    }


def emit_social_runtime_events(events: list[dict[str, Any]]) -> bool:
    """Best-effort batch insert into Social OS social_runtime_events."""
    if not events:
        return False
    url, key = _supabase_runtime_env()
    if not url or not key:
        return False

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for event in events:
        rows.append({
            "service": SOCIAL_RUNTIME_SERVICE,
            "event_type": event.get("event_type", "info"),
            "message": str(event.get("message", ""))[:500],
            "metadata": event.get("metadata") or {},
            "created_at": event.get("created_at") or now,
        })

    try:
        response = requests.post(
            f"{url}/rest/v1/social_runtime_events",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=rows,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[social-os] Could not emit runtime event(s): {exc}", file=sys.stderr)
        return False


def emit_social_runtime_event(event_type: str, message: str, metadata: dict[str, Any] | None = None) -> bool:
    return emit_social_runtime_events([{
        "event_type": event_type,
        "message": message,
        "metadata": metadata or {},
    }])


def _ensure_str(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _ensure_positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return fallback


def _ensure_string_array(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _ensure_string_record(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }


def _ensure_string_array_record(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(k): [item.lstrip("@").strip() for item in _ensure_string_array(v)]
        for k, v in value.items()
        if isinstance(k, str) and k.strip()
    }


def _browser_profile_root() -> Path:
    raw = os.environ.get("X_ENGAGE_BROWSER_PROFILE_ROOT", str(DEFAULT_BROWSER_PROFILE_ROOT))
    return Path(raw).expanduser().resolve()


def _config_path() -> Path:
    return Path(os.environ.get("X_ENGAGE_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()


def load_local_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _looks_like_projection(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("send_window", "lane_routing", "session_mappings", "rate_limits"))


def normalize_x_engage_projection(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("xEngage"), dict):
        payload = payload["xEngage"]
    elif isinstance(payload.get("x_engage"), dict):
        payload = payload["x_engage"]

    if not _looks_like_projection(payload):
        payload = {
            "lane_routing": payload.get("lane_routing"),
            "session_mappings": payload.get("session_mappings"),
            "send_window": {
                "start": payload.get("send_window_start"),
                "end": payload.get("send_window_end"),
                "tz": payload.get("send_window_tz"),
            },
            "rate_limits": {
                "max_posts_per_day": payload.get("max_posts_per_day"),
                "max_replies_per_day": payload.get("max_replies_per_day"),
            },
            "check_interval_seconds": payload.get("engage_check_interval_seconds"),
        }

    send_window = payload.get("send_window") if isinstance(payload.get("send_window"), dict) else {}
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}

    return {
        "lane_routing": _ensure_string_array_record(payload.get("lane_routing")),
        "session_mappings": {
            key.lstrip("@").strip(): value.strip()
            for key, value in _ensure_string_record(payload.get("session_mappings")).items()
        },
        "send_window": {
            "start": _ensure_str(send_window.get("start") or payload.get("send_window_start"), "08:00"),
            "end": _ensure_str(send_window.get("end") or payload.get("send_window_end"), "22:00"),
            "tz": _ensure_str(send_window.get("tz") or payload.get("send_window_tz"), "UTC"),
        },
        "rate_limits": {
            "max_posts_per_day": _ensure_positive_int(rate_limits.get("max_posts_per_day") or payload.get("max_posts_per_day"), 10),
            "max_replies_per_day": _ensure_positive_int(rate_limits.get("max_replies_per_day") or payload.get("max_replies_per_day"), 20),
        },
        "check_interval_seconds": _ensure_positive_int(payload.get("check_interval_seconds") or payload.get("engage_check_interval_seconds"), 3600),
    }


def _supabase_runtime_env() -> tuple[str, str] | tuple[None, None]:
    url = (
        os.environ.get("SOCIAL_OS_SUPABASE_URL")
        or os.environ.get("SUPABASE_URL")
        or os.environ.get("VITE_SUPABASE_URL")
    )
    key = (
        os.environ.get("SOCIAL_OS_SUPABASE_KEY")
        or os.environ.get("SOCIAL_OS_SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("VITE_SUPABASE_ANON_KEY")
    )
    if url and key:
        return url.rstrip("/"), key
    return None, None


def load_managed_runtime_projection() -> tuple[dict[str, Any] | None, str | None]:
    runtime_path = os.environ.get("X_ENGAGE_RUNTIME_PATH")
    if runtime_path:
        payload = json.loads(Path(runtime_path).expanduser().read_text())
        return normalize_x_engage_projection(payload), "managed_file"

    url, key = _supabase_runtime_env()
    if not url or not key:
        return None, None

    response = requests.get(
        f"{url}/rest/v1/social_runtime_configs",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
        params={
            "select": "*",
            "is_active": "eq.true",
            "order": "created_at.asc",
            "limit": "1",
        },
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        return None, None
    return normalize_x_engage_projection(rows[0]), "managed_supabase"


def _find_local_account(
    local_accounts: list[dict[str, Any]],
    handle: str,
    lane: str,
) -> dict[str, Any] | None:
    normalized = handle.lstrip("@").strip()
    for account in local_accounts:
        account_id = str(account.get("id", "")).lstrip("@").strip()
        account_handle = str(account.get("handle", "")).lstrip("@").strip()
        if normalized in {account_id, account_handle}:
            return account

    lane_matches = [account for account in local_accounts if str(account.get("lane", "")).strip() == lane]
    if len(lane_matches) == 1:
        return lane_matches[0]
    return None


def resolve_profile_path(session_mapping: str | None, fallback_profile: str | None, handle: str) -> str:
    raw = (session_mapping or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if raw.startswith("~") or raw.startswith("/") or "/" in raw:
            return str(path.resolve())
        return str((_browser_profile_root() / raw).resolve())

    fallback = (fallback_profile or "").strip()
    if fallback:
        return str(Path(fallback).expanduser().resolve())

    return str((_browser_profile_root() / handle).resolve())


def build_managed_accounts(
    projection: dict[str, Any],
    local_accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    session_mappings = projection.get("session_mappings", {})

    for lane, handles in projection.get("lane_routing", {}).items():
        for raw_handle in handles:
            handle = raw_handle.lstrip("@").strip()
            if not handle or handle in seen:
                continue
            seen.add(handle)
            local = _find_local_account(local_accounts, handle, lane) or {}
            account_id = local.get("id") or handle
            action_types = local.get("action_types") or ["retweet", "quote"]
            account = copy.deepcopy(local)
            account.update({
                "id": account_id,
                "handle": handle,
                "label": local.get("label") or f"@{handle}",
                "lane": lane,
                "browser_profile": resolve_profile_path(
                    session_mappings.get(handle),
                    local.get("browser_profile"),
                    handle,
                ),
                "action_types": action_types,
            })
            account["session_health"] = safe_session_health_summary(account)
            accounts.append(account)

    return accounts


def load_config() -> dict[str, Any]:
    local_config = load_local_config()
    merged = copy.deepcopy(local_config)

    try:
        projection, source = load_managed_runtime_projection()
    except Exception as exc:
        merged["runtime_source"] = "config_fallback"
        merged["runtime_load_error"] = str(exc)
        return merged

    if not projection:
        merged["runtime_source"] = "config_fallback"
        return merged

    merged["runtime_source"] = source
    merged["x_engage_runtime"] = projection
    merged["x_accounts"] = build_managed_accounts(projection, local_config.get("x_accounts", []))
    merged["send_window"] = projection["send_window"]
    merged["engage_check_interval_seconds"] = projection["check_interval_seconds"]
    merged["max_posts_per_day"] = projection["rate_limits"]["max_posts_per_day"]
    merged["max_replies_per_day"] = projection["rate_limits"]["max_replies_per_day"]
    return merged
