#!/bin/zsh
# User-operated launcher. Starts the service with live capability; no strategy auto-starts.
exec "$(dirname "$0")/scripts/start.command" --allow-live
