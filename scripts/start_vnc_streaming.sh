#!/bin/bash
# Start VNC streaming services for Skyvern
#
# This script sets up the complete VNC streaming stack:
# - Xvfb (virtual X11 display)
# - x11vnc (VNC server for the display)
# - websockify (WebSocket-to-VNC proxy)

set -euo pipefail

readonly DISPLAY_NUM=":99"
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

ensure_running() {
    local service_label="$1"
    local running_check_type="$2"  # "exact" | "match"
    local check_value="$3"
    local start_cmd="$4"

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
    eval "$start_cmd"
    log "$service_label started"
}

log "Starting VNC streaming services for Skyvern..."
log ""

ensure_running \
  "Xvfb" "exact" "Xvfb" \
  "Xvfb $DISPLAY_NUM -screen 0 $SCREEN_GEOMETRY > /dev/null 2>&1 &"

ensure_running \
  "x11vnc" "exact" "x11vnc" \
  "x11vnc -display $DISPLAY_NUM -bg -nopw -listen localhost -xkb -forever > /dev/null 2>&1"

ensure_running \
  "websockify" "match" "websockify.*${WS_PORT}" \
  "websockify $WS_PORT localhost:${VNC_PORT} --daemon > /dev/null 2>&1"


log ""
log "🎉 VNC streaming services are now running!"
log ""
log "Configuration:"
log "  - Xvfb display: ${DISPLAY_NUM}"
log "  - VNC server: localhost:${VNC_PORT}"
log "  - WebSocket proxy: localhost:${WS_PORT}"
log ""
log "To stop services:"
log "  pkill x11vnc && pkill websockify"