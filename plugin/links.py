"""connector.links — link store, control file, and loop-guard state.

All state lives in $HERMES_HOME/connector/:
  links.json     explicit links between two session keys (the consent mechanism)
  control.json   admin pause/kill switches (fail-closed: unreadable control = refuse relay)
  state.json     loop-guard counters (persisted so gateway restarts don't reset caps)

Design rules (from the implementation plan):
- Links are ALWAYS explicit; nothing auto-creates them.
- One session may hold at most MAX_LINKS_PER_SESSION links.
- Every relay hop checks: link active, not expired, not paused, not killed,
  window/cooldown caps.
- Link kinds: "crew" (standing teammate link, no expiry) and "consult"
  (one-off consultation channel with hard TTL + inactivity close).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_LINKS_PER_SESSION = 4
WINDOW_SECONDS = 300.0
WINDOW_MAX_EVENTS = 20
COOLDOWN_SECONDS = 600.0
EXCHANGE_MAX_HOPS = 4
EXCHANGE_IDLE_SECONDS = 120.0  # hops separated by more than this start a fresh exchange

# consult defaults (admin can override per link via CLI flags)
CONSULT_TTL_MINUTES = 30        # hard wall: consult deactivates this long after creation
CONSULT_IDLE_MINUTES = 10       # closes after this much silence since the last hop


def _root() -> Path:
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))) / "connector"


def _read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ---- links ----

def list_links() -> list[dict]:
    data = _read_json(_root() / "links.json", {"links": []})
    return data.get("links", [])


def save_links(links: list[dict]) -> None:
    _write_json_atomic(_root() / "links.json", {"links": links})


def add_link(a_key: str, b_key: str, label: str = "", kind: str = "crew",
             ttl_minutes: float = 0.0, idle_minutes: float = 0.0) -> tuple[bool, str]:
    """Create an explicit link. kind: "crew" (standing) or "consult" (timed).

    ttl_minutes: hard expiry after creation (0 = never; crew default).
    idle_minutes: auto-close after this much silence (0 = never).
    """
    links = list_links()
    if a_key == b_key:
        return False, "cannot link a session to itself"
    for l in links:
        if {l["a_key"], l["b_key"]} == {a_key, b_key}:
            return False, f"link already exists (id {l['id']}, state {l['state']})"
    held_a = sum(1 for l in links if a_key in (l["a_key"], l["b_key"]) and l.get("state") == "active")
    held_b = sum(1 for l in links if b_key in (l["a_key"], l["b_key"]) and l.get("state") == "active")
    if held_a >= MAX_LINKS_PER_SESSION:
        return False, f"link cap reached for {a_key.split(':')[-1]} ({MAX_LINKS_PER_SESSION} max)"
    if held_b >= MAX_LINKS_PER_SESSION:
        return False, f"link cap reached for {b_key.split(':')[-1]} ({MAX_LINKS_PER_SESSION} max)"
    lid = f"lnk_{int(time.time())}_{len(links) % 1000:03d}"
    link = {
        "id": lid, "a_key": a_key, "b_key": b_key,
        "label": label or f"{a_key.split(':')[-1]}<->{b_key.split(':')[-1]}",
        "state": "active", "kind": kind if kind in ("crew", "consult") else "crew",
        "created_at": time.time(),
    }
    if kind == "consult":
        if ttl_minutes > 0:
            link["expires_at"] = time.time() + ttl_minutes * 60.0
        if idle_minutes > 0:
            link["idle_close_minutes"] = idle_minutes
    links.append(link)
    save_links(links)
    return True, lid


def sweep_expired() -> list[str]:
    """Deactivate timed links whose wall has passed. Returns swept link ids."""
    links = list_links()
    now = time.time()
    swept = []
    changed = False
    for l in links:
        if l.get("state") != "active":
            continue
        expired = False
        exp = l.get("expires_at")
        if isinstance(exp, (int, float)) and now > exp:
            expired = True
        idle_min = l.get("idle_close_minutes")
        if isinstance(idle_min, (int, float)) and idle_min > 0 and l.get("created_at"):
            last = _last_hop_ts(l["id"])
            if last and (now - last) > idle_min * 60.0:
                expired = True
        if expired:
            l["state"] = "expired"
            swept.append(l["id"])
            changed = True
    if changed:
        save_links(links)
    return swept


def _last_hop_ts(link_id: str) -> float | None:
    """Timestamp of this link's last recorded hop (from the audit log, best-effort)."""
    path = _root() / "log.jsonl"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode(errors="replace").splitlines()
        for line in reversed(tail):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("kind") == "hop" and e.get("link_id") == link_id:
                return float(e.get("ts", 0)) or None
    except Exception:
        pass
    return None


def link_expiry_info(link: dict) -> dict:
    """{ttl_remaining_s, idle_remaining_s} for consults (0 = already past, None = n/a)."""
    now = time.time()
    out = {"ttl_remaining_s": None, "idle_remaining_s": None}
    exp = link.get("expires_at")
    if isinstance(exp, (int, float)):
        out["ttl_remaining_s"] = max(0.0, exp - now)
    idle_min = link.get("idle_close_minutes")
    if isinstance(idle_min, (int, float)) and idle_min > 0:
        last = _last_hop_ts(link["id"])
        if last:
            out["idle_remaining_s"] = max(0.0, idle_min * 60.0 - (now - last))
        else:
            out["idle_remaining_s"] = idle_min * 60.0
    return out


def unlink(link_id: str) -> bool:
    links = list_links()
    remaining = [l for l in links if l["id"] != link_id]
    if len(remaining) == len(links):
        return False
    save_links(remaining)
    return True


def set_state(link_id: str, state: str) -> bool:
    links = list_links()
    for l in links:
        if l["id"] == link_id:
            l["state"] = state
            save_links(links)
            return True
    return False


def get_all_for(session_key: str) -> list[dict]:
    """ALL active links involving this session key (crew + consults)."""
    sweep_expired()
    return [l for l in list_links()
            if session_key in (l["a_key"], l["b_key"]) and l.get("state") == "active"]


def get_for(session_key: str) -> dict | None:
    """Sole active link involving this session key, else None (kept for compat)."""
    links = get_all_for(session_key)
    return links[0] if len(links) == 1 else None


def other_side(link: dict, session_key: str) -> str:
    return link["b_key"] if session_key == link["a_key"] else link["a_key"]


# ---- control file (admin deck) ----

def control() -> dict:
    c = _read_json(_root() / "control.json", {})
    if not isinstance(c, dict):
        return {"paused_links": [], "killed_links": []}  # fail-closed handled by caller
    c.setdefault("paused_links", [])
    c.setdefault("killed_links", [])
    return c


def relay_allowed(link: dict) -> tuple[bool, str]:
    """Admin switches + timed-link expiry. Fail-closed on unreadable control."""
    try:
        c = control()
    except Exception:
        return False, "control file unreadable (fail-closed)"
    if link["id"] in c["killed_links"]:
        return False, "killed_by_admin"
    if link["id"] in c["paused_links"]:
        return False, "paused_by_admin"
    exp = link.get("expires_at")
    if isinstance(exp, (int, float)) and time.time() > exp:
        set_state(link["id"], "expired")
        return False, "consult expired (ttl)"
    idle_min = link.get("idle_close_minutes")
    if isinstance(idle_min, (int, float)) and idle_min > 0:
        last = _last_hop_ts(link["id"])
        if last and (time.time() - last) > idle_min * 60.0:
            set_state(link["id"], "expired")
            return False, "consult expired (idle window)"
    return True, "ok"


# ---- loop guard ----
# In-memory sliding window + per-link cooldown + exchange hop cap.
# Counters persist to state.json so a gateway restart does NOT reset caps
# (hardening fix: previously a restart zeroed exchange/window state).

_guards: dict[str, dict] = {}
_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    now = time.time()
    d = _read_json(_root() / "state.json", {})
    for lid, g in (d.get("guards") or {}).items():
        if not isinstance(g, dict):
            continue
        events = [t for t in g.get("events", []) if isinstance(t, (int, float)) and now - t <= WINDOW_SECONDS]
        _guards[lid] = {
            "events": events,
            "cooldown_until": float(g.get("cooldown_until", 0.0)),
            "exchange_hops": int(g.get("exchange_hops", 0)),
            "last_hop": float(g.get("last_hop", 0.0)),
        }


def _guard(link_id: str) -> dict:
    _ensure_loaded()
    return _guards.setdefault(link_id, {"events": [], "cooldown_until": 0.0, "exchange_hops": 0, "last_hop": 0.0})


def check_loop_guard(link_id: str) -> tuple[bool, str]:
    g = _guard(link_id)
    now = time.time()
    if now < g["cooldown_until"]:
        return False, f"cooldown active for another {g['cooldown_until'] - now:.0f}s"
    g["events"] = [t for t in g["events"] if now - t <= WINDOW_SECONDS]
    if len(g["events"]) >= WINDOW_MAX_EVENTS:
        g["cooldown_until"] = now + COOLDOWN_SECONDS
        _persist()
        return False, f"window cap: {WINDOW_MAX_EVENTS} hops in {WINDOW_SECONDS:.0f}s — cooldown {COOLDOWN_SECONDS:.0f}s"
    # exchange = hops closer together than EXCHANGE_IDLE_SECONDS
    if now - g["last_hop"] > EXCHANGE_IDLE_SECONDS:
        g["exchange_hops"] = 0
    if g["exchange_hops"] >= EXCHANGE_MAX_HOPS:
        return False, f"exchange cap: {EXCHANGE_MAX_HOPS} hops per exchange (wait {EXCHANGE_IDLE_SECONDS:.0f}s to start a new exchange)"
    return True, "ok"


def record_hop(link_id: str) -> None:
    g = _guard(link_id)
    now = time.time()
    g["events"].append(now)
    if now - g["last_hop"] > EXCHANGE_IDLE_SECONDS:
        g["exchange_hops"] = 0
    g["exchange_hops"] += 1
    g["last_hop"] = now
    _persist()


def _persist() -> None:
    try:
        _write_json_atomic(_root() / "state.json", {
            "guards": {k: v for k, v in _guards.items()}
        })
    except Exception:
        pass  # counters are best-effort; caps still hold in-memory