FROM node:24-alpine

# Install dependencies
# Install Bitwarden CLI
RUN apk add --no-cache curl bash &&  \
    npm install -g @bitwarden/cli

# Create non-root user for security
# Create directory for Bitwarden config
RUN addgroup -g 1001 -S bw && adduser -S bw -u 1001 -G bw && \
    mkdir -p /home/bw/.config && \
    chown -R bw:bw /home/bw/.config

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Switch to non-root user
USER bw
WORKDIR /home/bw

# Expose port for bw serve
EXPOSE 8087

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8087/status || exit 1

ENTRYPOINT ["/entrypoint.sh"]
