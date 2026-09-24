# connector — live-session crew for Hermes Agent

Connect live Hermes gateway sessions into a **crew**: persistent teammate links
("employees") plus one-off **consults** (timed channels that close themselves).
Every hop is labeled, loop-capped, audited, and visible to the human via the
admin deck. Built on the sanctioned `ctx.inject_message` plugin API and, for
local api-server sessions, direct HTTP turns — no chat-platform dependency
(works fully offline; Telegram not required).

Status: working on Hermes Agent v0.21.3 (validated end-to-end 2026-09-24).
Companion tool: [hsx](https://github.com/rwcrosk-arch/hsx) — the profile/session launcher.

## Components

| Piece | Path | What it is |
|-------|------|------------|
| Plugin | `$HERMES_HOME/plugins/connector/` | tools + admin CLI, loaded by the gateway |
| Audit log | `$HERMES_HOME/connector/log.jsonl` | source of record for every hop |
| Admin deck | `~/.local/bin/connector-deck` | status/usage/tail/serve/smoke |

## The model: crew + consults

- **crew link** — standing teammate. No expiry. For steady collaboration:
  `hermes connector link <keyA> <keyB> [label]` (kind defaults to `crew`)
- **consult link** — one-off consultation. Auto-closes after a TTL (default
  30 min) or an idle window (default 10 min):
  `hermes connector link <keyA> <keyB> --kind consult --ttl 30 --idle 10`

A session can hold several links at once (max 4); `connector_send` targets one
by `to=<label or counterpart>` or `index=N` (as listed by `connector_links`).

## Agent-facing tools (auto-visible in gateway sessions)

- `connector_links` — your links: kind, label, counterpart, expiry countdown
- `connector_send(message, to=|index=)` — relay over a link (labeled, capped)
- `connector_sessions` — which sessions are reachable right now
- `connector_consult(peer, question=, ttl_minutes=)` — request a one-off link
  (only if the admin set `allow_auto_consult: true` in
  `$HERMES_HOME/connector/config.json`; default OFF — consent stays with the human)

Agents should load the `connector-collab` skill (installed alongside) for
etiquette: check links first, self-contained messages, no label forging, no
false success claims.

## Admin CLI

```
hermes connector link <keyA> <keyB> [label] [--kind crew|consult] [--ttl MIN] [--idle MIN]
hermes connector list            # kinds + expiry countdowns
hermes connector sweep           # deactivate expired consults now
hermes connector pause/resume/unlink <linkId>
```

## Admin deck (the human's evaluation view)

```
connector-deck status          # links + recent events
connector-deck usage --hours 24
connector-deck tail
connector-deck serve           # http://127.0.0.1:8765/?t=<token from connector/deck.token>
connector-deck smoke           # 5 health checks, exit 0/1
```

The web deck has three panels:
- **usage (evaluation)** — accepted/refused hop counts, per-link hop volume,
  characters relayed, which sessions talked, in a time window
- **character stage** — each agent as an animated character with a speech
  bubble that lights up when it sends; counterpart gets an animated "…" while
  a hop is in flight (agent labels = session short name + link label)
- **live flow** — raw audit stream via SSE

## Safety architecture

- Links are ALWAYS explicit (admin CLI); nothing auto-creates crew links
- Consults are the only agent-requestable links, behind an off-by-default flag
- Fail-closed control file (`connector/control.json`): unreadable = refuse
- Loop caps: 4 hops/exchange (120 s idle = new exchange), 20 per 5 min,
  10 min cooldown — counters persist across gateway restarts
- Every hop attempt (accepted or refused, with reason) is appended to the
  audit log; agents cannot fake it
- Deck binds 127.0.0.1 only; token in `connector/deck.token` (0600)

## How relaying works

1. Sender calls `connector_send` inside its live gateway turn
2. Plugin resolves the link (admin consent), checks admin control, loop guard
3. Label `[connector:from <label>]` is added by the plugin (never by agents)
4. Transport: local api-server sessions → synchronous
   `POST /api/sessions/{id}/chat` (reply preview inline); everything else →
   `ctx.inject_message` (async, gateway-routed)
5. Hop appended to `log.jsonl`; deck streams it to the character stage

## Install

Copy `plugin/` into `$HERMES_HOME/plugins/connector/`, then:

```
hermes plugins enable connector
hermes plugins validate $HERMES_HOME/plugins/connector
hermes config set plugins.entries.connector.allow_gateway_injection true
# restart the gateway (it caches plugin code at startup)
```

Then link two sessions and watch them meet each other:

```
hermes connector link <keyA> <keyB> "duo"
connector-deck tail
```

## Roadmap (annotated, per Ross's spec — do not build yet)

- **Native Linux GUI** replacing the web deck: system-tray icon, popup
  notifications when the connector is in use, window mode showing each
  connector instance with agent labels (Profile + session ID), animated
  characters with speech bubbles flowing per message. The web deck's
  character stage is the design prototype (same event model, same labels).
- **Hub-and-spoke / group topologies** — current design is pairwise 1:1;
  the links.json schema can grow a `topology` field without migration pain.
- **allow_auto_consult UI** — a deck button to flip the consult flag.

## Known quirks

- **hermes-chat / api_server sessions:** connector tools arrive deferred there
  — agents must call them through the `tool_call` bridge rather than invoking
  directly ("Tool 'connector_links' does not exist" = use tool_call). Normal
  deferred-tool behavior, not a connector fault.
- **Gateway restarts:** the gateway caches plugin code at startup — after any
  plugin edit, restart the gateway or the old code keeps serving.

## License

MIT — see [LICENSE](LICENSE).