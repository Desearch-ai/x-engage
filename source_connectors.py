"""Bounded Socialos runtime source connectors.

The analyzer still accepts the legacy x-monitor tweet window, but this module makes
source loading explicit: X-Monitor normalized Signal records are one connector and
future sources (markdown/API) can produce the same analyzer-friendly signal shape
without pretending to be X-Monitor.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class SourceLoadResult:
    source_type: str
    signals: list[dict[str, Any]]
    metadata: dict[str, Any]
    errors: list[str]


class SourceConnector(Protocol):
    source_type: str

    def load(self) -> SourceLoadResult:
        """Return normalized analyzer signal dictionaries and non-fatal errors."""


def _clean_handle(value: Any) -> str:
    return str(value or "").strip().lstrip("@") or "unknown"


def _metric(signal: dict[str, Any], metrics: dict[str, Any], key: str) -> int | float:
    value = signal.get(key, metrics.get(key, 0))
    return value if isinstance(value, (int, float)) else 0


def _route_hint_lanes(route_hints: Any) -> list[str]:
    if not isinstance(route_hints, dict):
        return []
    raw_values: list[Any] = []
    for key in ("lanes", "lane", "target_lanes", "target_lane", "account_lanes", "account_lane"):
        value = route_hints.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value:
            raw_values.append(value)
    lanes: list[str] = []
    for value in raw_values:
        lane = str(value or "").strip().lstrip("@")
        if lane and lane not in lanes:
            lanes.append(lane)
    return lanes


def _route_hint_tokens(source: str, route_hints: Any, lanes: list[str]) -> list[str]:
    tokens = [f"{source}/{lane}" for lane in lanes]
    if isinstance(route_hints, dict):
        channel = str(route_hints.get("channel") or "").strip()
        if channel:
            tokens.append(channel)
    return tokens


def _category(signal_type: str, context: dict[str, Any]) -> str:
    watchlist = str(context.get("watchlist_name") or context.get("matched_term") or "").strip()
    return f"{signal_type}:{watchlist}" if watchlist else signal_type


def _is_normalized_signal(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("content"), dict) and any(k in payload for k in ("signal_id", "type", "route_hints", "context"))


def normalize_source_signal(signal: dict[str, Any], *, connector_type: str = "x_monitor_signal") -> dict[str, Any]:
    """Map the Task #1613 Signal contract into the analyzer's tweet-like shape."""
    if not _is_normalized_signal(signal):
        legacy = dict(signal)
        legacy.setdefault("_source_connector", connector_type)
        legacy.setdefault("_monitor_source", legacy.get("source") or "x-monitor")
        return legacy

    content = signal.get("content") if isinstance(signal.get("content"), dict) else {}
    context = signal.get("context") if isinstance(signal.get("context"), dict) else {}
    metrics = signal.get("metrics") if isinstance(signal.get("metrics"), dict) else {}
    route_hints = signal.get("route_hints") if isinstance(signal.get("route_hints"), dict) else {}
    source = str(signal.get("source") or "x-monitor").strip() or "x-monitor"
    lanes = _route_hint_lanes(route_hints)
    signal_id = str(signal.get("signal_id") or signal.get("id") or content.get("url") or "").strip()
    signal_type = str(signal.get("type") or "signal").strip()

    return {
        "id": signal_id,
        "url": str(content.get("url") or signal.get("url") or ""),
        "text": str(content.get("text") or signal.get("text") or ""),
        "user": {"username": _clean_handle(content.get("author") or signal.get("author"))},
        "like_count": _metric(signal, metrics, "like_count"),
        "retweet_count": _metric(signal, metrics, "retweet_count"),
        "reply_count": _metric(signal, metrics, "reply_count"),
        "view_count": _metric(signal, metrics, "view_count"),
        "quote_count": _metric(signal, metrics, "quote_count"),
        "bookmark_count": _metric(signal, metrics, "bookmark_count"),
        "_score": _metric(signal, metrics, "score"),
        "_monitor_category": _category(signal_type, context),
        "_monitor_source": source,
        "_monitor_lanes": lanes,
        "_monitor_route_hints": _route_hint_tokens(source, route_hints, lanes),
        "_normalized_signal": signal,
        "_source_connector": connector_type,
    }


class XMonitorSignalConnector:
    source_type = "x_monitor_signal"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def load(self) -> SourceLoadResult:
        path = Path(str(self.config.get("path") or self.config.get("x_monitor_window_path") or "")).expanduser()
        if not path.exists():
            return SourceLoadResult(self.source_type, [], {"connector": self.source_type, "path": str(path)}, [f"source file not found: {path}"])
        payload = json.loads(path.read_text())
        raw_signals = payload.get("signals") if isinstance(payload, dict) else payload
        if not isinstance(raw_signals, list):
            return SourceLoadResult(self.source_type, [], {"connector": self.source_type, "path": str(path)}, ["source payload must be a list or {signals: [...]} object"])
        signals = [normalize_source_signal(item, connector_type=self.source_type) for item in raw_signals if isinstance(item, dict)]
        return SourceLoadResult(self.source_type, signals, {"connector": self.source_type, "path": str(path), "loaded": len(signals)}, [])


class MarkdownSourceConnector:
    source_type = "markdown"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _paths(self) -> list[Path]:
        configured = self.config.get("paths") or self.config.get("path") or []
        if isinstance(configured, str):
            configured = [configured]
        paths: list[Path] = []
        for item in configured if isinstance(configured, list) else []:
            raw = str(item or "").strip()
            if not raw:
                continue
            matches = glob.glob(str(Path(raw).expanduser()))
            paths.extend(Path(match) for match in (matches or [raw]))
        return paths

    def load(self) -> SourceLoadResult:
        default_lane = str(self.config.get("default_lane") or "general").strip() or "general"
        signals: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in self._paths():
            try:
                text = path.read_text()
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            title = next((line.lstrip("#").strip() for line in text.splitlines() if line.strip()), path.stem)
            signals.append({
                "id": f"markdown:{path.resolve()}",
                "url": path.resolve().as_uri(),
                "text": text[:2000],
                "user": {"username": str(self.config.get("author") or "markdown")},
                "like_count": 0,
                "retweet_count": 0,
                "reply_count": 0,
                "view_count": 0,
                "quote_count": 0,
                "bookmark_count": 0,
                "_score": float(self.config.get("default_score") or 0),
                "_monitor_category": f"markdown:{title}",
                "_monitor_source": "markdown",
                "_monitor_lanes": [default_lane],
                "_monitor_route_hints": [f"markdown/{default_lane}"],
                "_source_connector": self.source_type,
            })
        return SourceLoadResult(self.source_type, signals, {"connector": self.source_type, "loaded": len(signals)}, errors)


def _build_connector(config: dict[str, Any]) -> SourceConnector:
    source_type = str(config.get("type") or config.get("source_type") or "").strip().lower()
    if source_type in {"x_monitor_signal", "x-monitor", "x_monitor", "x-monitor-signal"}:
        return XMonitorSignalConnector(config)
    if source_type in {"markdown", "md"}:
        return MarkdownSourceConnector(config)
    raise ValueError(f"Unsupported source connector: {source_type or 'missing'}")


def _default_connector_configs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(cfg.get("source_connectors"), list):
        return [item for item in cfg["source_connectors"] if isinstance(item, dict)]
    if cfg.get("x_monitor_window_path"):
        return [{"type": "x_monitor_signal", "path": cfg["x_monitor_window_path"]}]
    return []


def load_source_signals(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load all configured source connectors; failures are reported, not raised."""
    signals: list[dict[str, Any]] = []
    connector_reports: list[dict[str, Any]] = []
    for connector_config in _default_connector_configs(cfg):
        source_type = str(connector_config.get("type") or connector_config.get("source_type") or "unknown")
        try:
            result = _build_connector(connector_config).load()
            signals.extend(result.signals)
            connector_reports.append({
                "type": result.source_type,
                "status": "ok" if not result.errors else "partial",
                "loaded": len(result.signals),
                "metadata": result.metadata,
                "errors": result.errors,
            })
        except Exception as exc:
            connector_reports.append({"type": source_type, "status": "error", "loaded": 0, "error": str(exc)})
    return signals, {"loaded": len(signals), "connectors": connector_reports}
