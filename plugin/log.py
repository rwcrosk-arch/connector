"""connector.log — the connector's audit log: one JSONL line per hop attempt.

This file is the deck's only source of truth for the message flow. Rotation at
10 MB, three rotated files kept. Single writer (the plugin inside the gateway
process); readers tail only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_LOG_BYTES = 10 * 1024 * 1024
ROTATED_KEEP = 3
PREVIEW_CHARS = 300


def _log_path() -> Path:
    root = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))) / "connector"
    root.mkdir(parents=True, exist_ok=True)
    return root / "log.jsonl"


def rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
            for i in range(ROTATED_KEEP - 1, 0, -1):
                src = path.with_suffix(f".jsonl.{i}")
                if src.exists():
                    src.rename(path.with_suffix(f".jsonl.{i + 1}"))
            path.rename(path.with_suffix(".jsonl.1"))
    except Exception:
        pass  # rotation is best-effort; never break the hop


def log_event(event: dict) -> None:
    """Append one audit line. Never raises: audit failure must not kill a turn."""
    try:
        path = _log_path()
        rotate_if_needed(path)
        line = json.dumps({"ts": time.time(), **event}, ensure_ascii=False)
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_hop(*, direction: str, from_key: str, from_title: str, to_key: str, to_session_id: str,
            hops_in_exchange: int, message: str, accepted: bool, reason: str = "",
            link_id: str = "", link_kind: str = "", link_label: str = "") -> None:
    log_event({
        "kind": "hop", "dir": direction, "from_key": from_key, "from_title": from_title,
        "to_key": to_key, "to_session_id": to_session_id,
        "hops_in_exchange": hops_in_exchange,
        "text_preview": message[:PREVIEW_CHARS],
        "accepted": accepted, "reason": reason,
        "link_id": link_id, "link_kind": link_kind, "link_label": link_label,
    })


def log_admin(*, action: str, link_id: str, detail: str = "") -> None:
    log_event({"kind": "admin", "action": action, "link_id": link_id, "detail": detail})