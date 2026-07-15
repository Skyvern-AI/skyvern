#!/bin/bash
# Start VNC streaming services for Skyvern
#
# This script sets up the complete VNC streaming stack:
# - Xvfb (virtual X11 display)
# - x11vnc (VNC server for the display)
# - websockify (WebSocket-to-VNC proxy)

set -euo pipefail

readonly DISPLAY_NUMBER="${SKYVERN_DEFAULT_DISPLAY:-99}"
if [[ ! "$DISPLAY_NUMBER" =~ ^[0-9]+$ ]]; then
    printf 'ERROR: SKYVERN_DEFAULT_DISPLAY must be an unsigned integer; got %q\n' "$DISPLAY_NUMBER" >&2
    exit 1
fi
readonly DISPLAY_NUM=":${DISPLAY_NUMBER}"
readonly SCREEN_GEOMETRY="1920x1080x24"
readonly VNC_PORT="5900"
readonly WS_PORT="6080"

log() {
    printf '%s\n' "$*"
}

is_running_exact() {
    local process_name="$1"
    pgrep -x "$process_name" > /dev/null 2>&1
}

is_running_match() {
    local pattern="$1"
    pgrep -f "$pattern" > /dev/null 2>&1
}

is_display_ready() {
    xdpyinfo -display "$DISPLAY_NUM" > /dev/null 2>&1
}

ensure_daemon_running() {
    local service_label="$1"
    local running_check_type="$2"  # "exact" | "match"
    local check_value="$3"
    shift 3

    if [[ "$running_check_type" == "exact" ]]; then
        if is_running_exact "$check_value"; then
            log "$service_label already running"
            return 0
        fi
    else
        if is_running_match "$check_value"; then
            log "$service_label already running"
            return 0
        fi
    fi

    log "Service $service_label not running. Starting..."
    "$@" > /dev/null 2>&1
    log "$service_label started"
}

log "Starting VNC streaming services for Skyvern..."
log ""

if is_display_ready; then
    log "Xvfb already running on display $DISPLAY_NUM"
else
    log "Xvfb not running on display $DISPLAY_NUM. Starting..."
    Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN_GEOMETRY" > /dev/null 2>&1 &
    log "Xvfb started on display $DISPLAY_NUM"
fi

if [[ "${BROWSER_STREAMING_MODE:-}" != "vnc" ]]; then
    ensure_daemon_running \
      "x11vnc" "exact" "x11vnc" \
      x11vnc -display "$DISPLAY_NUM" -bg -nopw -listen localhost -xkb -forever

    ensure_daemon_running \
      "websockify" "match" "websockify.*${WS_PORT}" \
      websockify "$WS_PORT" "localhost:${VNC_PORT}" --daemon
else
    log "Dynamic VNC mode enabled; per-session VNC ports are started on demand."
fi


log ""
log "🎉 VNC streaming services are now running!"
log ""
log "Configuration:"
log "  - Xvfb display: ${DISPLAY_NUM}"
if [[ "${BROWSER_STREAMING_MODE:-}" != "vnc" ]]; then
    log "  - VNC server: localhost:${VNC_PORT}"
    log "  - WebSocket proxy: localhost:${WS_PORT}"
fi
log ""
log "To stop services:"
if [[ "${BROWSER_STREAMING_MODE:-}" != "vnc" ]]; then
    log "  pkill x11vnc && pkill websockify"
fi
