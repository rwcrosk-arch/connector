#!/usr/bin/env bash
# connector installer — copies the plugin + deck into place and checks prerequisites.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SRC="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HERMES_HOME/plugins" "$HOME/.local/bin"

cp -r "$SRC/plugin" "$HERMES_HOME/plugins/connector"
chmod +x "$HERMES_HOME/plugins/connector/"*.py
cp "$SRC/deck/connector-deck" "$HOME/.local/bin/connector-deck"
chmod +x "$HOME/.local/bin/connector-deck"
cp "$SRC/skills/connector-collab" "$HERMES_HOME/skills/autonomous-ai-agents/" 2>/dev/null || true

echo "✓ plugin  -> $HERMES_HOME/plugins/connector"
echo "✓ deck    -> $HOME/.local/bin/connector-deck"

if command -v hermes >/dev/null 2>&1; then
    echo "next steps:"
    echo "  hermes plugins enable connector"
    echo "  hermes plugins validate $HERMES_HOME/plugins/connector"
    echo "  hermes config set plugins.entries.connector.allow_gateway_injection true"
    echo "  # restart the gateway, then: hermes connector link <keyA> <keyB>"
else
    echo "⚠ 'hermes' not on PATH — install Hermes Agent first" >&2
fi