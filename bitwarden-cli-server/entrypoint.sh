#!/bin/bash
set -euo pipefail

# Color codes for better logging
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Update log function to use color codes
log() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log "Starting entrypoint script..."
log "Current user: $(whoami)"
log "Current directory: $(pwd)"

# Check required environment variables
if [[ -z "${BW_HOST:-}" ]]; then
    log_error "BW_HOST environment variable is required"
    exit 1
fi

if [[ -z "${BW_CLIENTID:-}" ]]; then
    log_error "BW_CLIENTID environment variable is required"
    exit 1
fi

if [[ -z "${BW_CLIENTSECRET:-}" ]]; then
    log_error "BW_CLIENTSECRET environment variable is required"
    exit 1
fi

if [[ -z "${BW_PASSWORD:-}" ]]; then
    log_error "BW_PASSWORD environment variable is required"
    exit 1
fi

# Test network connectivity first
log "Testing connectivity to vaultwarden server: $BW_HOST"
if ! curl -s --connect-timeout 10 "$BW_HOST" > /dev/null; then
    log_warning "Cannot reach $BW_HOST - this might be normal if the server doesn't respond to GET requests"
fi

# Logout first to clear any existing session
log "Logging out to clear any existing session..."
bw logout > /dev/null 2>&1 || true  # Ignore errors if not logged in

# Configure Bitwarden CLI to use vaultwarden server
log "Configuring Bitwarden CLI to use server: $BW_HOST"

# Temporarily disable pipefail to capture the output properly
set +e
config_output=$(bw config server "$BW_HOST" 2>&1)
config_result=$?
set -e

log "Config command result: $config_result"
log "Config command output: $config_output"

if [[ $config_result -ne 0 ]]; then
    log_error "Failed to configure server. Error output:"
    log_error "$config_output"
    exit 1
fi

log_success "Server configuration successful"

# Login using API key with retry logic for rate limiting
log "Logging in to Bitwarden using API key..."

# Retry login with exponential backoff
max_retries=3
retry_count=0
login_success=false

while [[ $retry_count -lt $max_retries ]]; do
    if [[ $retry_count -gt 0 ]]; then
        delay=$((retry_count * retry_count * 5))  # 5, 20, 45 seconds
        log "Rate limited. Waiting ${delay} seconds before retry $((retry_count + 1))/$max_retries..."
        sleep $delay
    fi

    set +e
    login_output=$(bw login --apikey 2>&1)
    login_result=$?
    set -e

    log "Login attempt $((retry_count + 1)): result=$login_result"
    log "Login output: '$login_output'"

    if [[ $login_result -eq 0 ]]; then
        login_success=true
        break
    elif [[ "$login_output" == *"Rate limit exceeded"* ]]; then
        log_warning "Rate limit exceeded on attempt $((retry_count + 1))"
        ((retry_count++))
    else
        log_error "Failed to login with API key. Error output:"
        log_error "$login_output"
        log_error "Please check:"
        log_error "1. BW_HOST is correct and accessible: $BW_HOST"
        log_error "2. BW_CLIENTID is valid: ${BW_CLIENTID:0:20}..."
        log_error "3. BW_CLIENTSECRET is correct"
        log_error "4. API key is enabled in vaultwarden"
        exit 1
    fi
done

if [[ "$login_success" != "true" ]]; then
    log_error "Failed to login after $max_retries attempts due to rate limiting"
    log_error "Please wait a few minutes and try again"
    exit 1
fi

log_success "Successfully logged in"

# Now unlock to get the session token
log "Unlocking vault to get session token..."
set +e
unlock_output=$(bw unlock --passwordenv BW_PASSWORD --raw 2>&1)
unlock_result=$?
set -e

log "Unlock command result: $unlock_result"
log "Unlock command output: '$unlock_output'"

if [[ $unlock_result -ne 0 ]]; then
    log_error "Failed to unlock vault. Error output:"
    log_error "$unlock_output"
    log_error "Please check BW_PASSWORD is correct"
    exit 1
fi

# Extract session token from unlock output
export BW_SESSION="$unlock_output"
log "Session token length: ${#BW_SESSION}"

if [[ -z "$BW_SESSION" ]]; then
    log_error "Session token is empty after unlock"
    log_error "Raw unlock output was: '$unlock_output'"
    exit 1
fi

log_success "Vault unlocked successfully"

# Sync vault
log "Syncing vault..."
bw sync --session "$BW_SESSION" > /dev/null 2>&1 || {
    log_warning "Sync failed, but continuing anyway"
}

log_success "Vault sync completed"

# Start the server
log "Starting Bitwarden CLI server on port 8087..."
log "Server will be accessible at http://localhost:8087"
log "Available endpoints:"
log "  - GET  /status         - Check server status"
log "  - POST /unlock         - Unlock vault"
log "  - GET  /list/object/items - List vault items"
log "  - GET  /object/item/{id} - Get specific item"
log "  - And more..."

# Start bw serve with proper error handling
exec bw serve --hostname 0.0.0.0 --port 8087 --session "$BW_SESSION" || {
    log_error "Failed to start Bitwarden CLI server"
    exit 1
}
