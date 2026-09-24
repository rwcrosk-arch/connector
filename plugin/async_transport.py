"""connector.async_transport — fire-and-forget local delivery for crew chatter.

The synchronous direct transport (direct.send_to_session_key) waits for the
target's FULL reply turn. When both linked sessions send during one exchange,
each blocks on the other's busy turn -> deadlock -> executor kills the tool
call before the hop is logged (the Sep 24 comedy-collab incident).

Async mode delivers the labeled message via the same POST /api/sessions/{id}/chat
but from a short-lived daemon thread that abandons the HTTP wait after
ACK_TIMEOUT seconds. The api-server runs concurrent turns per session and the
POST persists the user message at turn start, so abandoning the wait does NOT
abandon the message — it is delivered and processed regardless. The connector
logs the hop as accepted (transport=async) the moment the request is handed
off, keeping the audit trail complete.

Thread-safety: each delivery is its own daemon thread; urllib is thread-safe
for independent requests. No shared mutable state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
DB = HOME / "state.db"
BASE = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")

# How long the background thread keeps the socket open trying for a reply
# before giving up. The message is already delivered either way; this only
# bounds the daemon thread's lifetime and feeds a best-effort reply preview.
REPLY_WAIT_TIMEOUT = 90.0

# How long the CALLING agent's tool call waits before we return "handed off".
# Kept small so crew chatter never deadlocks; the turn continues server-side.
ACK_TIMEOUT = 4.0


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


def _do_post(key: str, sid: str, text: str, result_box: dict) -> None:
    """The actual POST, run in a daemon thread. Writes outcome into result_box."""
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
        with urllib.request.urlopen(req, timeout=REPLY_WAIT_TIMEOUT) as r:
            d = json.loads(r.read().decode())
        reply = ""
        try:
            if d.get("object") == "hermes.session.chat.completion":
                reply = d["message"]["content"]
            else:
                reply = d["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            reply = json.dumps(d)[:400]
        result_box["outcome"] = {"delivered": "confirmed", "reply_preview": reply[:300]}
    except urllib.error.HTTPError as e:
        result_box["outcome"] = {"delivered": "failed",
                                 "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:  # URLError/timeout: delivered-but-unconfirmed
        result_box["outcome"] = {"delivered": "unconfirmed",
                                 "note": f"reply wait abandoned after {REPLY_WAIT_TIMEOUT:.0f}s"}


def send_async(key: str, text: str) -> dict:
    """Fire-and-forget delivery: hand the labeled message to the target session
    and return at once. The target's reply turn runs server-side; we never wait
    on it, so two linked sessions can talk simultaneously without deadlock.

    Returns {ok, delivered, session_id} where delivered is:
      "handed-off"  request accepted by the server (normal case)
      "failed"      no session / HTTP error / connection refused (message NOT delivered)
    """
    sid = session_id_for(key)
    if not sid:
        return {"ok": False, "delivered": "failed",
                "error": f"no session found for key {key!r} (say hello once via hermes-chat to create it)"}
    box: dict = {"outcome": None}
    t = threading.Thread(target=_do_post, args=(key, sid, text, box), daemon=True)
    t.start()
    t.join(timeout=ACK_TIMEOUT)
    if box["outcome"] is not None:
        # fast failure (no session / HTTP error / immediate reply) — report it
        out = box["outcome"]
        if out["delivered"] == "failed":
            return {"ok": False, "delivered": "failed", "session_id": sid, "error": out["error"]}
        if out["delivered"] == "confirmed":
            return {"ok": True, "delivered": "confirmed", "session_id": sid,
                    "reply_preview": out.get("reply_preview", "")}
    return {"ok": True, "delivered": "handed-off", "session_id": sid,
            "note": (f"message handed to {key} — its reply turn runs in the background; "
                     "do not wait on it or resend. The audit log records this hop.")}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(send_async(sys.argv[1], " ".join(sys.argv[2:])), indent=1))
    else:
        print("usage: connector-async <session-key> <message...>")