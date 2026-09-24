"""connector — live-session connector plugin (v0.2.0: crew + consults).

Links gateway sessions and relays labeled messages between them via the
sanctioned ctx.inject_message path (direct HTTP for local api_server sessions).
Every hop: explicit link -> admin control -> loop guard -> inject -> audit log.
Links are never auto-created.

Link kinds:
  crew    — standing teammate link (no expiry); steady collaboration
  consult — one-off consultation channel with TTL/idle auto-close

Tools (agent-facing, registered into the `default` composite so every gateway
session sees them; they fail with a clear message outside gateway sessions):
  connector_send      relay a message to a linked session (target by label/index)
  connector_links     this session's links, with kind, expiry, counterpart labels
  connector_sessions  list injectable gateway sessions
  connector_consult   request a one-off consultation link to another session
                      (created only if the admin config allows auto-consents)

Admin CLI (hermes connector ...):
  link <keyA> <keyB> [label] [--kind crew|consult] [--ttl MIN] [--idle MIN]
  list | pause <id> | resume <id> | unlink <id> | sweep
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

try:
    from . import links as L
    from . import log as LG
    from . import direct
    from . import async_transport as AT
except ImportError:  # direct-file import (tests, validation probe)
    import links as L
    import log as LG
    import direct
    import async_transport as AT


def _hermes_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))


def _connector_root() -> Path:
    return _hermes_home() / "connector"


# ---- configuration ----

def _config() -> dict:
    c = L._read_json(_connector_root() / "config.json", {})
    return c if isinstance(c, dict) else {}


def _allow_auto_consult() -> bool:
    """Consult links may be auto-created by agents ONLY if the admin allowed it."""
    return bool(_config().get("allow_auto_consult", False))


# ---- session discovery (read-only; the plan's one fragile joint lives here) ----

SESSION_QUERY = """
SELECT session_key, source, profile_name, title, chat_type, last_activity_at
FROM sessions
WHERE session_key IS NOT NULL
ORDER BY last_activity_at DESC
"""


def list_injectable_sessions(max_age_minutes: int = 60) -> list[dict]:
    db = _hermes_home() / "state.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(SESSION_QUERY).fetchall()
    finally:
        con.close()
    now = time.time()
    out = []
    for key, source, profile, title, chat_type, last_act in rows:
        age = (now - (last_act or 0)) / 60.0
        if age > max_age_minutes:
            continue  # stale routing entry -> injection would fail as unroutable
        out.append({
            "session_key": key, "source": source, "profile": profile or "default",
            "title": title or "", "chat_type": chat_type or "", "age_minutes": round(age, 1),
        })
    return out


def _session_source(key: str) -> str | None:
    db = _hermes_home() / "state.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT source FROM sessions WHERE session_key = ? ORDER BY "
            "COALESCE(NULLIF(last_activity_at,0), started_at) DESC LIMIT 1", (key,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _label(link: dict) -> str:
    """Structural attribution, added by the plugin. Agents never compose it."""
    return f"[connector:from {link['label']}]"


def _short(key: str) -> str:
    return key.split(":")[-1] if key else "?"


# ---- tools ----

SEND_SCHEMA = {
    "name": "connector_send",
    "description": (
        "Send a message over a connector link to a live session you are LINKED to "
        "(see connector_links). With several links, target by 'to' (link label or "
        "counterpart name) or 'index' (1-based). The connector adds the attribution "
        "label itself; do not add labels. Rate-capped per exchange and window. "
        "Delivery mode: async (default, best for conversation) hands the message to "
        "the target and returns immediately — never wait for or resend after an "
        "async send; sync waits for the target's full reply turn (use only when you "
        "need the reply in-hand and know the target is idle)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message text to relay. No attribution prefixes."},
            "to": {"type": "string", "description": "Optional link label or counterpart session short-name to target (required when you hold several links)."},
            "index": {"type": "integer", "description": "Optional 1-based link index (as listed by connector_links). Used when 'to' is absent."},
            "mode": {"type": "string", "enum": ["async", "sync"], "description": "async (default): fire-and-forget, returns after hand-off. sync: wait for the target's complete reply turn (deadlocks if both sides send at once)."},
        },
    },
}

LINKS_SCHEMA = {
    "name": "connector_links",
    "description": (
        "List this session's connector links: kind (crew/consult), counterpart, label, "
        "state, and for consults the remaining TTL/idle time. Use 'to' in connector_send "
        "to pick a link when you hold more than one."
    ),
    "parameters": {"type": "object", "properties": {}},
}

SESSIONS_SCHEMA = {
    "name": "connector_sessions",
    "description": "List gateway sessions reachable by the connector (active within the last hour).",
    "parameters": {"type": "object", "properties": {}},
}

CONSULT_SCHEMA = {
    "name": "connector_consult",
    "description": (
        "Request a one-off consultation link to another live session (auto-closes by "
        "TTL/idle). Succeeds only if the admin enabled allow_auto_consult in "
        "~/.hermes/connector/config.json; otherwise the error explains how to ask the "
        "admin to create the link."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string", "description": "Target session key or its short name from connector_sessions."},
            "question": {"type": "string", "description": "The question to send once the link exists."},
            "ttl_minutes": {"type": "integer", "description": "Hard lifetime for this consult (default 30, max 240)."},
        },
        "required": ["peer"],
    },
}


def _json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)


def _resolve_link(me: str, args: dict) -> tuple[dict | None, str]:
    """Pick the link to use for a send. Multi-link sessions must disambiguate."""
    my_links = L.get_all_for(me)
    if not my_links:
        return None, "no active link for this session — admin runs `hermes connector link <thisKey> <otherKey>`; see connector_sessions for candidates"
    if len(my_links) == 1:
        return my_links[0], ""
    # multiple links: disambiguate
    to = str(args.get("to") or "").strip().lower()
    idx = args.get("index")
    if to:
        for i, l in enumerate(my_links, 1):
            label = (l.get("label") or "").lower()
            counterpart = L.other_side(l, me)
            if to == label or to == counterpart.lower() or to == _short(counterpart).lower() or to == str(i):
                return l, ""
        return None, f"no link matches to={to!r} — call connector_links to see your links"
    if isinstance(idx, int):
        if 1 <= idx <= len(my_links):
            return my_links[idx - 1], ""
        return None, f"index {idx} out of range (you hold {len(my_links)} links)"
    return None, ("you hold several links — pass 'to' (link label or counterpart name) "
                  "or 'index'; call connector_links to list them")


def make_send_handler(ctx):
    def handler(args, **kwargs):
        from tools.approval_context import get_current_session_key
        me = get_current_session_key("")
        message = str(args.get("message") or "").strip()
        if not me:
            return _json({"ok": False, "error": "this session has no gateway session key (CLI sessions cannot use the connector)"})
        if not message:
            return _json({"ok": False, "error": "message required"})
        link, why = _resolve_link(me, args)
        if link is None:
            return _json({"ok": False, "error": why})
        allowed, why = L.relay_allowed(link)
        if not allowed:
            LG.log_event({"kind": "hop", "dir": "refused", "link_id": link["id"], "from_key": me, "reason": why})
            return _json({"ok": False, "error": f"relay refused: {why}"})
        ok, why = L.check_loop_guard(link["id"])
        if not ok:
            LG.log_event({"kind": "hop", "dir": "refused", "link_id": link["id"], "from_key": me, "reason": why})
            return _json({"ok": False, "error": f"relay refused: {why}"})
        target = L.other_side(link, me)
        content = f"{_label(link)} {message}"
        # Transport selection: local named sessions get a direct turn on the target
        # session (no routing index needed); everything else rides the gateway
        # injector. Mode: async (default, fire-and-forget) or sync (wait for the
        # target's full reply — use only when the target is known idle).
        mode = str(args.get("mode") or "async").lower()
        if mode not in ("async", "sync"):
            mode = "async"
        src = _session_source(target)
        if src in LOCAL_SOURCES:
            if mode == "async":
                result = AT.send_async(target, content)
                delivered = bool(result.get("ok"))
                reason = "" if delivered else str(result.get("error", "async_failed"))[:120]
                reply_preview = str(result.get("reply_preview", ""))[:300]
                delivery_state = str(result.get("delivered", "handed-off"))
            else:
                result = direct.send_to_session_key(target, content)
                delivered = bool(result.get("ok"))
                reason = "" if delivered else str(result.get("error", "direct_failed"))[:120]
                reply_preview = str(result.get("reply_preview", ""))[:300]
                delivery_state = "confirmed" if delivered else "failed"
        else:
            delivered = bool(ctx.inject_message(content, role="user", session_key=target))
            reason = "" if delivered else "not_routed_by_gateway"
            reply_preview = ""
            delivery_state = "scheduled" if delivered else "failed"
        if delivered:
            L.record_hop(link["id"])
        LG.log_hop(
            direction="a->b" if me == link["a_key"] else "b->a",
            from_key=me, from_title=_short(me), to_key=target, to_session_id="",
            hops_in_exchange=L._guard(link["id"])["exchange_hops"],
            message=message, accepted=delivered,
            reason=reason,
            link_id=link["id"], link_kind=link.get("kind", "crew"), link_label=link.get("label", ""),
        )
        out = {
            "ok": delivered,
            "transport": ("async" if (src in LOCAL_SOURCES and mode == "async")
                          else "direct" if src in LOCAL_SOURCES else "inject"),
            "delivery": delivery_state,
            "link_id": link["id"],
            "link_kind": link.get("kind", "crew"),
            "target": target,
            "target_name": _short(target),
            "hops_in_exchange": L._guard(link["id"])["exchange_hops"],
        }
        if reply_preview:
            out["reply_preview"] = reply_preview
        if out["transport"] == "async":
            out["note"] = ("message handed off; the target's reply turn runs in the background. "
                           "Do not wait on it and do NOT resend — duplicates become ghost messages. "
                           "The target will reply over the link when ready.")
        elif out["transport"] == "inject":
            out["note"] = "accepted means scheduled for async dispatch, not that the turn completed"
        return _json(out)

    return handler


def make_links_handler(ctx):
    def handler(args, **kwargs):
        from tools.approval_context import get_current_session_key
        me = get_current_session_key("")
        if not me:
            return _json({"this_session": None, "links": [], "error": "no gateway session key (CLI sessions cannot use the connector)"})
        my_links = L.get_all_for(me)
        out = []
        for l in my_links:
            entry = {
                "index": len(out) + 1,
                "link_id": l["id"], "label": l.get("label", ""), "kind": l.get("kind", "crew"),
                "state": l.get("state", "?"),
                "counterpart": L.other_side(l, me), "counterpart_name": _short(L.other_side(l, me)),
            }
            if l.get("kind") == "consult":
                entry["expiry"] = L.link_expiry_info(l)
            out.append(entry)
        return _json({"this_session": me, "links": out})

    return handler


def make_sessions_handler(ctx):
    def handler(args, **kwargs):
        from tools.approval_context import get_current_session_key
        me = get_current_session_key("")
        link = L.get_for(me) if me else None
        return _json({
            "this_session": me or None,
            "link": ({k: (link.get("kind", "crew") if k == "kind" else link[k]) for k in ("id", "label", "state", "kind")} if link else None),
            "sessions": list_injectable_sessions(),
        })

    return handler


def make_consult_handler(ctx):
    def handler(args, **kwargs):
        from tools.approval_context import get_current_session_key
        me = get_current_session_key("")
        if not me:
            return _json({"ok": False, "error": "no gateway session key (CLI sessions cannot consult)"})
        peer = str(args.get("peer") or "").strip()
        question = str(args.get("question") or "").strip()
        if not peer:
            return _json({"ok": False, "error": "peer required (session key or short name from connector_sessions)"})
        if not _allow_auto_consult():
            return _json({
                "ok": False,
                "error": ("auto-consult is disabled by the admin. Ask the human to run: "
                          f"hermes connector link <thisKey> <peerKey> --kind consult  "
                          "(or set allow_auto_consult=true in ~/.hermes/connector/config.json)"),
            })
        # resolve peer: exact key, or unique short-name match among injectable sessions
        sessions = list_injectable_sessions()
        peer_key = None
        if any(s["session_key"] == peer for s in sessions):
            peer_key = peer
        else:
            matches = [s for s in sessions if _short(s["session_key"]).lower() == peer.lower()]
            if len(matches) == 1:
                peer_key = matches[0]["session_key"]
            elif matches:
                return _json({"ok": False, "error": f"ambiguous peer {peer!r}: several sessions share that short name"})
        if not peer_key:
            return _json({"ok": False, "error": f"peer not found or not injectable (stale?): {peer!r}"})
        if peer_key == me:
            return _json({"ok": False, "error": "cannot consult yourself"})
        ttl = min(240, max(1, int(args.get("ttl_minutes") or 30)))
        idle = max(2, min(60, int(args.get("idle_minutes") or 10)))
        ok, detail = L.add_link(me, peer_key, label=f"consult:{_short(me)}->{_short(peer_key)}",
                                kind="consult", ttl_minutes=ttl, idle_minutes=idle)
        LG.log_admin(action="consult_created" if ok else "consult_refused",
                     link_id=detail if ok else "", detail=f"{_short(me)} -> {peer_key}")
        if not ok:
            return _json({"ok": False, "error": detail})
        # fire the question immediately if one was provided
        fired = None
        if question:
            fired = make_send_handler(ctx)(  # reuse the send path (guards, labels, audit)
                {"message": question, "to": detail}, **kwargs)
        return _json({"ok": True, "link_id": detail, "kind": "consult",
                      "ttl_minutes": ttl, "idle_minutes": idle, "send_result": fired})

    return handler


# ---- admin CLI ----

def register_cli(ctx) -> None:
    def setup(sub):
        sub.add_argument("action", choices=["link", "list", "pause", "resume", "unlink", "sweep"])
        sub.add_argument("argv", nargs="*",
                         help="link: keyA keyB [label] | pause/resume/unlink: linkId")
        sub.add_argument("--kind", default="crew", choices=["crew", "consult"])
        sub.add_argument("--ttl", type=float, default=0.0, help="consult ttl minutes (0=default 30)")
        sub.add_argument("--idle", type=float, default=0.0, help="consult idle-close minutes (0=default 10)")

    def handler(ns) -> None:
        action, argv = ns.action, ns.argv
        if action == "link":
            if len(argv) < 2:
                print("usage: hermes connector link <keyA> <keyB> [label] [--kind crew|consult] [--ttl MIN] [--idle MIN]")
                return
            ttl = ns.ttl if ns.ttl > 0 else (30.0 if ns.kind == "consult" else 0.0)
            idle = ns.idle if ns.idle > 0 else (10.0 if ns.kind == "consult" else 0.0)
            ok, detail = L.add_link(argv[0], argv[1], argv[2] if len(argv) > 2 else "",
                                    kind=ns.kind, ttl_minutes=ttl, idle_minutes=idle)
            LG.log_admin(action="link" if ok else "link_failed", link_id=detail if ok else "",
                         detail=" ".join(argv[:2]) + (f" kind={ns.kind}" if ok else ""))
            print(("linked: " if ok else "refused: ") + detail)
        elif action == "list":
            L.sweep_expired()
            links = L.list_links()
            if not links:
                print("no links. create one: hermes connector link <keyA> <keyB>")
            for l in links:
                extra = ""
                if l.get("kind") == "consult":
                    info = L.link_expiry_info(l)
                    ttl = info.get("ttl_remaining_s")
                    extra = f"  [consult"
                    if ttl is not None:
                        extra += f" ttl={int(ttl // 60)}m{int(ttl % 60)}s"
                    idle = info.get("idle_remaining_s")
                    if idle is not None:
                        extra += f" idle={int(idle // 60)}m"
                    extra += "]"
                print(f"{l['id']}  {l['state']:8s}  {l.get('kind', 'crew'):7s}  {l['label']}{extra}  "
                      f"{l['a_key']} <-> {l['b_key']}")
        elif action == "sweep":
            swept = L.sweep_expired()
            print("expired: " + (", ".join(swept) if swept else "none"))
        elif action in ("pause", "resume", "unlink"):
            if not argv:
                print(f"usage: hermes connector {action} <linkId>")
                return
            lid = argv[0]
            if action == "pause":
                ok = L.set_state(lid, "paused")
                _control_edit(lid, "paused_links", add=True)
            elif action == "resume":
                _control_edit(lid, "paused_links", add=False)
                ok = L.set_state(lid, "active")
            else:
                ok = L.unlink(lid)
                _control_edit(lid, "killed_links", add=False)
            LG.log_admin(action=action, link_id=lid)
            print(f"{action} {lid}: {'done' if ok else 'FAILED (no such link?)'}")

    ctx.register_cli_command(name="connector", help="manage connector session links",
                             setup_fn=setup, handler_fn=handler)


def _control_edit(link_id: str, key: str, add: bool) -> None:
    path = _connector_root() / "control.json"
    c = L.control()
    if add and link_id not in c[key]:
        c[key].append(link_id)
    if not add and link_id in c[key]:
        c[key].remove(link_id)
    L._write_json_atomic(path, c)


def register(ctx) -> None:
    ctx.register_tool(name="connector_send", toolset="default", schema=SEND_SCHEMA,
                      handler=make_send_handler(ctx),
                      description="relay a message over a link to a live session", emoji="⇄")
    ctx.register_tool(name="connector_links", toolset="default", schema=LINKS_SCHEMA,
                      handler=make_links_handler(ctx),
                      description="list this session's connector links (kind/expiry)", emoji="🔗")
    ctx.register_tool(name="connector_sessions", toolset="default", schema=SESSIONS_SCHEMA,
                      handler=make_sessions_handler(ctx),
                      description="list injectable sessions + link status", emoji="📋")
    ctx.register_tool(name="connector_consult", toolset="default", schema=CONSULT_SCHEMA,
                      handler=make_consult_handler(ctx),
                      description="request a one-off consultation link to another session", emoji="💬")
    register_cli(ctx)


# keep LOCAL_SOURCES importable from __init__ for older callers/tests
from .direct import LOCAL_SOURCES  # noqa: E402  (must stay after defs; direct.py defines it)