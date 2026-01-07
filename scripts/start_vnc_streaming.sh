#!/bin/bash
# Start base VNC display for Skyvern
#
# This script sets up the default Xvfb display (:99) used as fallback.
# VncManager handles per-session VNC (Xvfb, x11vnc, websockify) dynamically.

set -euo pipefail

readonly DISPLAY_NUM="${SKYVERN_DEFAULT_DISPLAY:-:99}"
readonly SCREEN_WIDTH="${BROWSER_WIDTH:-1920}"
readonly SCREEN_HEIGHT="${BROWSER_HEIGHT:-1080}"
readonly SCREEN_GEOMETRY="${SCREEN_WIDTH}x${SCREEN_HEIGHT}x24"

log() {
    printf '%s\n' "$*"
}

is_running_exact() {
    local process_name="$1"
    pgrep -x "$process_name" > /dev/null 2>&1
}

ensure_xvfb_running() {
    if is_running_exact "Xvfb"; then
        log "Xvfb already running"
        return 0
    fi

    log "Starting Xvfb on display ${DISPLAY_NUM}..."
    Xvfb "${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" > /dev/null 2>&1 &
    log "Xvfb started"
}

log "Starting base VNC display for Skyvern..."
log ""

ensure_xvfb_running

export DISPLAY="${DISPLAY_NUM}"

log ""
log "Base VNC display ready!"
log ""
log "Configuration:"
log "  - Xvfb display: ${DISPLAY_NUM}"
log "  - Screen: ${SCREEN_GEOMETRY}"
log ""
log "Note: VncManager creates per-session VNC (x11vnc + websockify) dynamically."
