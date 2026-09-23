---
name: connector-collab
version: 0.2.0
description: "Use when collaborating over connector links (crew/consult)."
---

# Connector collaboration (crew + consults)

You may be LINKED to other live sessions ("crew members") through the
connector plugin. Links are admin-granted consent — never assume one exists;
always check first.

## Discover your links

Call `connector_links` FIRST whenever a task might involve collaboration. It
returns your links with:
- `index` (1-based) — what you pass to connector_send `index`
- `label` — what you pass to connector_send `to`
- `kind`: `crew` = standing teammate (no expiry); `consult` = one-off channel
  that auto-closes (see `expiry`)
- `counterpart` / `counterpart_name` — who is on the other end

## Sending (crew or consult)

connector_send(message, to=...) — `to` is the link label, counterpart name, or
index. With exactly ONE link you may omit `to`. The `[connector:from <label>]`
attribution prefix is added BY THE PLUGIN — never write labels yourself.

Relay protocol:
- The receiving side sees your message as a user turn labeled with your link
  label. Reply with connector_send over the same link.
- `delivered/ok=true` means dispatched, NOT that a reply turn finished. Do not
  claim success beyond what the tool result states. The audit log is the
  source of record — agents cannot fake it.
- If refused: report the exact error (cooldown, cap, expired, paused) and stop
  retrying. Caps protect everyone; waiting out a cooldown silently wastes turns.

## One-off consultations (occasional collaboration)

connector_consult(peer=<short name from connector_sessions>, question=...)
requests a timed link (default 30 min TTL, 10 min idle-close). It only works
if the admin enabled allow_auto_consult; otherwise ask the human to run:

    hermes connector link <yourKey> <peerKey> --kind consult

Use consults for quick questions, not standing work. A consult that goes idle
or hits TTL deactivates itself; neither side needs to clean up.

## Etiquette for crew work

- One exchange, one topic. Keep each hop a complete, self-contained message:
  the other side has your conversation context ONLY as the labeled messages
  that crossed the link.
- Max 4 hops per exchange, 20 per 5 minutes, then cooldown — design your
  request so one or two round-trips suffice (state the task, the format you
  want back, and the deadline in the FIRST message).
- When a task finishes, say so explicitly ("task complete — no reply needed").
- Never relay another session's message to a third session yourself; routes
  belong to the admin.

## Labels and identity

When relaying, you are identified structurally by the link label (Profile +
session identity), not by prose. Never claim to be someone else on a link;
never strip or alter the attribution prefix you receive — pass context along
with its label intact.

## If you have no link

Say so plainly and continue solo: report what you WOULD have asked, and let
the human decide whether to wire a link. Connector tools failing with
"no active link" is normal for unlinked sessions.
