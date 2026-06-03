#!/usr/bin/env bash
# Double-clickable launcher for macOS. Place on Desktop or in /Applications.
# When double-clicked, opens a terminal briefly then launches Flip Board.

# Resolve script directory (works even when symlinked or moved)
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ $SCRIPT_PATH != /* ]] && SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_PATH"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"

cd "$SCRIPT_DIR"
exec ./start.sh
