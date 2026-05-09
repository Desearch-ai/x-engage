from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse
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


# ─────────────────────────────────────────────
# Social OS review-row writer
# ─────────────────────────────────────────────

INACTIVE_SOCIAL_STATUSES = {"rejected", "approved", "posted"}
REGENERATABLE_SOCIAL_STATUSES = {"rejected", "commented", "changes_requested", "revision_requested"}
SOCIAL_REJECTION_FEEDBACK_FIELDS = (
    "operator_feedback",
    "rejection_comment",
    "rejection_comments",
    "rejection_reason",
    "review_notes",
    "feedback",
    "comment",
    "comments",
)
SOCIAL_REGENERATION_LINEAGE_FIELDS = (
    "regenerated_from_post_id",
    "regenerated_from_id",
    "parent_post_id",
    "source_post_id",
)


def _build_social_post_row(item: dict[str, Any], angle: str) -> dict[str, Any]:
    action_types = item.get("action_types", [])
    account_handle = item.get("account_handle") or item.get("account_id", "")
    account_label = item.get("account_label") or f"@{account_handle}"
    lane = item.get("lane", "")
    tweet_text = item.get("tweet_text", "")[:280]
    author = item.get("author", "")
    score = item.get("score", 0)
    signal_url = item.get("source_signal_url") or item.get("tweet_url", "")
    source_signal_id = item.get("source_signal_id") or item.get("tweet_id", "")
    category = item.get("category", "")
    rationale = item.get("rationale", "")
    account_fit_reason = item.get("account_fit_reason") or rationale
    filter_status = item.get("filter_status", "unknown")
    skipped_reason = item.get("skipped_reason")
    writing_standard = item.get("writing_standard_check") or {}
    writing_status = writing_standard.get("status", "unknown")
    writing_summary = writing_standard.get("summary", "")
    confidence = item.get("confidence")
    risk = item.get("risk") or item.get("risk_level") or "low"
    risk_notes = item.get("risk_notes", "")
    duplicate_notes = item.get("duplicate_notes", "")
    priority = item.get("priority", "medium")
    monitored_source = item.get("monitored_source") if isinstance(item.get("monitored_source"), dict) else {}
    matched_keywords = item.get("matched_keywords") or monitored_source.get("matched_keywords") or []
    fingerprint = item.get("generation_fingerprint", "")

    filter_line = f"Filter: {filter_status}"
    if skipped_reason:
        filter_line = f"{filter_line} ({skipped_reason})"
    confidence_line = f"Confidence/risk: {confidence if confidence is not None else 'unknown'} / {risk}"
    keyword_line = ", ".join(str(k) for k in matched_keywords) if matched_keywords else "none recorded"

    content = (
        f"@{account_handle} · {lane} · {', '.join(action_types)}\n\n"
        f"Signal from @{author} [{category}] (score: {score}):\n"
        f'"{tweet_text}"\n\n'
        f"Source: {signal_url}\n\n"
        f"{filter_line}\n"
        f"Account fit: {account_fit_reason}\n"
        f"Writing standard: {writing_status} — {writing_summary}\n"
        f"{confidence_line}\n"
        f"Matched keywords: {keyword_line}\n\n"
        f"Rationale: {rationale}\n"
        f"Risk notes: {risk_notes}\n"
        f"Duplicate notes: {duplicate_notes}\n\n"
        f"[x-engage/{fingerprint}]"
    )
    signal_rationale = (
        f"Filter: {filter_status}. Account fit: {account_fit_reason}. "
        f"Writing standard: {writing_status} — {writing_summary}. "
        f"Source: @{author} score={score}; confidence={confidence if confidence is not None else 'unknown'}; risk={risk}."
    )
    return {
        "platform": "x",
        "angle": angle,
        "content": content,
        "status": "draft",
        "created_by": "x-engage-analyzer",
        "approval_status": "pending",
        "account_handle": account_handle,
        "account_label": account_label,
        "lane": lane,
        "post_type": ",".join(action_types) if action_types else "engagement",
        "rationale": account_fit_reason,
        "signal_rationale": signal_rationale,
        "monitored_account": author,
        "monitored_keyword": ", ".join(str(k) for k in matched_keywords),
        "priority": priority,
        "risk_level": risk,
        "duplicate_note": duplicate_notes,
        "generated_by": "x-engage-analyzer",
        "generation_model": "account_strategy_filter",
        "source_url": signal_url,
        "source_signal_id": source_signal_id,
    }


def _extract_social_fingerprint(content: str) -> str:
    """Extract generation fingerprint from '[x-engage/{fp}]' footer in content."""
    import re
    m = re.search(r"\[x-engage/([a-f0-9]+)\]", content)
    return m.group(1) if m else ""


def _social_review_state(row: dict[str, Any]) -> str:
    """Return canonical review state for x-engage's Social OS safety gates."""
    status = _clean_social_str(row.get("status")).lower()
    approval_status = _clean_social_str(row.get("approval_status")).lower()
    if status in REGENERATABLE_SOCIAL_STATUSES:
        return status
    return approval_status or status


def _social_operator_feedback(row: dict[str, Any]) -> str:
    """Extract operator reject/comment feedback from supported Social OS handoff fields."""
    for field in SOCIAL_REJECTION_FEEDBACK_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        for field in SOCIAL_REJECTION_FEEDBACK_FIELDS:
            value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def _regeneration_angle(base_angle: str, existing: dict[str, Any], item: dict[str, Any], feedback: str) -> str:
    """Build a stable per-feedback angle so retrying a refill is idempotent."""
    seed = json.dumps(
        {
            "base_angle": base_angle,
            "source_row_id": existing.get("id"),
            "feedback": feedback,
            "generation_fingerprint": item.get("generation_fingerprint", ""),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"{base_angle}:regen:{digest}"


def _apply_social_regeneration_context(row_data: dict[str, Any], existing: dict[str, Any], feedback: str) -> dict[str, Any]:
    """Create a pending regenerated review draft using original signal/account/lane context."""
    source_row_id = _clean_social_str(existing.get("id"))
    feedback_block = "\n\nOperator feedback for regeneration:\n" + f'"{feedback}"'
    if source_row_id:
        feedback_block += f"\nRegenerated from Social OS row: {source_row_id}"

    regenerated = {**row_data}
    regenerated["content"] = f"{regenerated.get('content', '')}{feedback_block}"
    regenerated["rationale"] = f"{regenerated.get('rationale', '')} Operator feedback: {feedback}".strip()
    regenerated["signal_rationale"] = f"{regenerated.get('signal_rationale', '')} Operator feedback: {feedback}".strip()
    regenerated["approval_status"] = "pending"
    regenerated["status"] = "draft"

    if source_row_id:
        for field in SOCIAL_REGENERATION_LINEAGE_FIELDS:
            if field in existing:
                regenerated[field] = source_row_id

    if isinstance(existing.get("metadata"), dict):
        metadata = dict(row_data.get("metadata") or {})
        metadata.update({
            "regenerated_from_post_id": source_row_id,
            "operator_feedback": feedback,
            "source_angle": existing.get("angle"),
        })
        regenerated["metadata"] = metadata

    return regenerated


def write_social_os_review_rows(items: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Upsert Social OS social_posts review rows for queue items.

    Dedup key: angle = 'x-engage:{signal_id}:{account_id}' (stable per signal×account pair).
    - No existing row → INSERT with status='draft'.
    - Existing draft row → UPDATE content (refresh).
    - Existing inactive row (rejected/approved/posted) with same generation fingerprint → SKIP.
    - Existing rejected/commented row with operator feedback → INSERT regenerated pending draft.
    - Existing inactive row with changed fingerprint and no feedback → UPDATE to status='draft'.

    Emits failure telemetry instead of raising on partial write failures.
    Returns counts: created, refreshed, skipped, regenerated, total.
    """
    if not items:
        return {"created": 0, "refreshed": 0, "skipped": 0, "regenerated": 0, "total": 0}

    url, key = _supabase_runtime_env()
    if not url or not key:
        print("[social-os] Supabase env not configured; skipping social_posts write", file=sys.stderr)
        return {"created": 0, "refreshed": 0, "skipped": 0, "regenerated": 0, "total": 0, "skipped_reason": "no_supabase_env"}

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    angle_map: dict[str, dict[str, Any]] = {}
    for item in items:
        signal_id = item.get("source_signal_id") or item.get("tweet_id", "")
        account_id = item.get("account_id", "")
        angle = f"x-engage:{signal_id}:{account_id}"
        angle_map[angle] = item

    angles = list(angle_map.keys())
    angle_list = ",".join(angles)

    try:
        resp = requests.get(
            f"{url}/rest/v1/social_posts",
            headers=headers,
            params={
                "select": "*",
                "platform": "eq.x",
                "created_by": "eq.x-engage-analyzer",
                "angle": f"in.({angle_list})",
            },
            timeout=10,
        )
        resp.raise_for_status()
        existing_by_angle: dict[str, dict[str, Any]] = {row["angle"]: row for row in resp.json()}

        regeneration_angles: list[str] = []
        for angle, item in angle_map.items():
            existing = existing_by_angle.get(angle)
            if not existing:
                continue
            feedback = _social_operator_feedback(existing)
            if _social_review_state(existing) in REGENERATABLE_SOCIAL_STATUSES and feedback:
                regeneration_angles.append(_regeneration_angle(angle, existing, item, feedback))

        if regeneration_angles:
            regen_resp = requests.get(
                f"{url}/rest/v1/social_posts",
                headers=headers,
                params={
                    "select": "id,angle",
                    "platform": "eq.x",
                    "created_by": "eq.x-engage-analyzer",
                    "angle": f"in.({','.join(regeneration_angles)})",
                },
                timeout=10,
            )
            regen_resp.raise_for_status()
            for row in regen_resp.json():
                if isinstance(row, dict) and row.get("angle"):
                    existing_by_angle[row["angle"]] = row
    except Exception as exc:
        emit_social_runtime_event(
            "error",
            f"social_posts dedup query failed: {exc}",
            {"item_count": len(items)},
        )
        return {"created": 0, "refreshed": 0, "skipped": 0, "regenerated": 0, "total": 0, "error": str(exc)}

    to_insert: list[dict[str, Any]] = []
    to_update: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    regenerated = 0

    for angle, item in angle_map.items():
        row_data = _build_social_post_row(item, angle)
        existing = existing_by_angle.get(angle)

        if existing is None:
            to_insert.append(row_data)
            continue

        existing_status = _social_review_state(existing)
        if existing_status in INACTIVE_SOCIAL_STATUSES or existing_status in REGENERATABLE_SOCIAL_STATUSES:
            feedback = _social_operator_feedback(existing)
            if existing_status in REGENERATABLE_SOCIAL_STATUSES and feedback:
                regen_angle = _regeneration_angle(angle, existing, item, feedback)
                if regen_angle in existing_by_angle:
                    skipped += 1
                    continue
                regenerated_row = _build_social_post_row(item, regen_angle)
                to_insert.append(_apply_social_regeneration_context(regenerated_row, existing, feedback))
                regenerated += 1
                continue

            existing_fp = _extract_social_fingerprint(existing.get("content", ""))
            new_fp = item.get("generation_fingerprint", "")
            if existing_fp == new_fp:
                skipped += 1
                continue
            to_update.append((existing["id"], {**row_data, "status": "draft"}))
        else:
            update_data = {k: v for k, v in row_data.items() if k != "status"}
            to_update.append((existing["id"], update_data))

    created = 0
    refreshed = 0

    if to_insert:
        try:
            resp = requests.post(
                f"{url}/rest/v1/social_posts",
                headers={**headers, "Prefer": "return=minimal"},
                json=to_insert,
                timeout=10,
            )
            resp.raise_for_status()
            created = len(to_insert)
        except Exception as exc:
            emit_social_runtime_event(
                "error",
                f"social_posts batch insert failed: {exc}",
                {"count": len(to_insert)},
            )

    for row_id, update_payload in to_update:
        try:
            resp = requests.patch(
                f"{url}/rest/v1/social_posts",
                headers={**headers, "Prefer": "return=minimal"},
                params={"id": f"eq.{row_id}"},
                json=update_payload,
                timeout=10,
            )
            resp.raise_for_status()
            refreshed += 1
        except Exception as exc:
            emit_social_runtime_event(
                "error",
                f"social_posts update failed for row {row_id}: {exc}",
                {"row_id": row_id},
            )

    summary = {
        "created": created,
        "refreshed": refreshed,
        "skipped": skipped,
        "regenerated": regenerated,
        "total": created + refreshed + skipped,
    }
    print(
        f"[social-os] Review rows: {created} created, {refreshed} refreshed, {skipped} skipped, {regenerated} regenerated",
        file=sys.stderr,
    )
    return summary


SOCIAL_OS_APPROVED_SELECT = ",".join((
    "id",
    "angle",
    "platform",
    "post_type",
    "source_signal_id",
    "source_url",
    "account_handle",
    "account_label",
    "lane",
    "approval_status",
    "approval_url",
    "approved_by",
    "approved_at",
    "status",
))


def _clean_social_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_x_status_url(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return "", ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return "", ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts or parts[0].lower() in {"i", "home", "search", "hashtag"}:
        return "", ""
    author = parts[0].lstrip("@")
    tweet_id = ""
    if len(parts) >= 3 and parts[1].lower() == "status":
        tweet_id = parts[2]
    return author, tweet_id


def _author_from_x_url(url: str) -> str:
    return _parse_x_status_url(url)[0]


def _tweet_id_from_x_url(url: str) -> str:
    return _parse_x_status_url(url)[1]


def _social_post_author(row: dict[str, Any]) -> str:
    # monitored_account existed in earlier local/dev schemas. The deployed Social OS schema
    # does not expose it, so derive the source author from source_url when possible.
    return (
        _clean_social_str(row.get("monitored_account")).lstrip("@")
        or _author_from_x_url(_clean_social_str(row.get("source_url")))
        or "unknown"
    )


def _social_post_action(row: dict[str, Any]) -> str:
    raw_post_type = _clean_social_str(row.get("post_type"))
    action = raw_post_type.split(",")[0].strip().lower() if raw_post_type else "retweet"
    return action if action in ("retweet", "quote") else ""


def _social_post_executable_blocker(row: dict[str, Any]) -> str | None:
    tweet_url = _clean_social_str(row.get("source_url"))
    _author, tweet_id_from_url = _parse_x_status_url(tweet_url)
    status = _clean_social_str(row.get("status")).lower()
    approval_status = _clean_social_str(row.get("approval_status")).lower()
    action = _social_post_action(row)

    if _clean_social_str(row.get("platform")).lower() != "x":
        return "platform_not_x"
    if status != "approved":
        return f"status_{status or 'missing'}"
    if approval_status != "approved":
        return f"approval_status_{approval_status or 'missing'}"
    if not tweet_url or not tweet_id_from_url:
        return "source_url_not_x_status"
    if not action:
        return "unsupported_post_type"
    if action == "quote" and not _clean_social_str(row.get("quote_text")):
        return "quote_text_empty"
    return None


def _is_executable_social_post_row(row: dict[str, Any]) -> bool:
    return _social_post_executable_blocker(row) is None


def _social_post_to_action(row: dict[str, Any]) -> dict[str, Any]:
    """Map an approved social_posts row to the action dict format used by execute_actions.py."""
    angle = _clean_social_str(row.get("angle"))
    # angle format: "x-engage:{signal_id}:{account_id}"
    parts = angle.split(":", 2)
    account_id = parts[2] if len(parts) == 3 else (row.get("account_handle") or "default")

    action = _social_post_action(row) or "retweet"

    tweet_url = _clean_social_str(row.get("source_url"))
    tweet_id = _tweet_id_from_x_url(tweet_url)

    return {
        "tweet_id": tweet_id,
        "tweet_url": tweet_url,
        "author": _social_post_author(row),
        "action": action,
        # quote_text is not present in the deployed Social OS schema. Keep this safe/empty
        # instead of reusing review UI content as live publish text.
        "quote_text": _clean_social_str(row.get("quote_text")),
        "account_id": account_id,
        "account_handle": _clean_social_str(row.get("account_handle")),
        "account_label": _clean_social_str(row.get("account_label")),
        "lane": _clean_social_str(row.get("lane")),
        "status": "approved",
        "approval_status": _clean_social_str(row.get("approval_status")),
        "approval_url": _clean_social_str(row.get("approval_url")),
        "approved_by": _clean_social_str(row.get("approved_by")),
        "approved_at": _clean_social_str(row.get("approved_at")),
        "social_os_row_id": row.get("id"),
        "_source": "social_os",
    }


def load_social_os_approved_rows() -> list[dict[str, Any]]:
    """
    Fetch Social OS social_posts rows with approval_status='approved' for x-engage execution.

    Queries the social_posts table for platform='x' rows that have been approved in Social OS
    and are also in status='approved'. Maps each row to the action dict format understood by
    execute_actions.py, preserving approval provenance (approval_url, approved_by).

    Returns [] if Supabase is not configured or the query fails (best-effort, non-fatal).
    """
    url, key = _supabase_runtime_env()
    if not url or not key:
        return []

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(
            f"{url}/rest/v1/social_posts",
            headers=headers,
            params={
                "select": SOCIAL_OS_APPROVED_SELECT,
                "platform": "eq.x",
                "approval_status": "eq.approved",
                "status": "eq.approved",
            },
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            print("[social-os] Unexpected response from social_posts query", file=sys.stderr)
            return []
        executable_rows = []
        excluded_rows = []
        for row in rows:
            if not isinstance(row, dict):
                excluded_rows.append(("unknown", "invalid_row"))
                continue
            blocker = _social_post_executable_blocker(row)
            if blocker:
                excluded_rows.append((str(row.get("id") or "unknown"), blocker))
            else:
                executable_rows.append(row)

        for row_id, blocker in excluded_rows:
            print(f"[social-os] Excluded approved social_posts row {row_id}: {blocker}", file=sys.stderr)

        actions = [_social_post_to_action(row) for row in executable_rows]
        print(f"[social-os] Loaded {len(actions)} approved Social OS row(s) for execution", file=sys.stderr)
        return actions
    except Exception as exc:
        print(f"[social-os] Could not load approved social_posts rows: {exc}", file=sys.stderr)
        return []


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
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("VITE_SUPABASE_SERVICE_KEY")
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
