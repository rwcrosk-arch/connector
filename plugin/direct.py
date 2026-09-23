#!/usr/bin/env python3
"""connector-direct — local transport helper for the connector.

For LOCAL named sessions (api_server), the relay doesn't need the gateway
injector at all: it POSTs to /api/sessions/{id}/chat on the target session —
same labeled user turn, same transcript, same model call. This module is the
single place that knows how; the deck's `peek`/`say` subcommands use it too.

Session ids come from state.db (sessions table, session_key IN (...), mode=ro).
"""
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
DB = HOME / "state.db"
BASE = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")

# Sources relayed via direct HTTP (not the gateway injector)
LOCAL_SOURCES = {"api_server"}


def api_key():
    k = os.environ.get("HERMES_API_KEY")
    if k:
        return k
    env = HOME / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no API key: set HERMES_API_KEY or API_SERVER_KEY in ~/.hermes/.env")


def session_id_for(key: str) -> str | None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT id FROM sessions WHERE session_key = ? ORDER BY "
            "COALESCE(NULLIF(last_activity_at,0), started_at) DESC LIMIT 1", (key,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def send_to_session_key(key: str, text: str, timeout: int = 300) -> dict:
    """Start a turn in the target named session. Returns {ok, session_id, reply}."""
    sid = session_id_for(key)
    if not sid:
        return {"ok": False, "error": f"no session found for key {key!r} (say hello once via hermes-chat to create it)"}
    body = {"message": text}
    req = urllib.request.Request(
        f"{BASE}/api/sessions/{sid}/chat",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + api_key(),
            "Content-Type": "application/json",
            "X-Hermes-Session-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        reply = ""
        try:
            if d.get("object") == "hermes.session.chat.completion":
                reply = d["message"]["content"]
            else:
                reply = d["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            reply = json.dumps(d)[:400]
        return {"ok": True, "session_id": sid, "reply_preview": reply[:300]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"connection failed: {e.reason}"}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(send_to_session_key(sys.argv[1], " ".join(sys.argv[2:])), indent=1))
    else:
        print("usage: connector-direct <session-key> <message...>")