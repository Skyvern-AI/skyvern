# Bitwarden CLI Server for Skyvern

This Docker setup provides a Bitwarden CLI server with `bw serve` functionality that enables Skyvern to work with vaultwarden (or official Bitwarden) instances.

## Architecture

```text
Usual setup (in cloud):
Skyvern → official Bitwarden

Local from docker compose:
Skyvern → bw serve (CLI Server) → vaultwarden Server
```

The CLI server acts as a bridge between Skyvern and vaultwarden, providing the REST API endpoints that Skyvern expects.

## Setup

This container is part of the main Skyvern Docker Compose setup. Configure your environment variables in the main `.env` file:

```bash
# Skyvern Bitwarden Configuration
SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID=your-org-id-here
SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD=your-master-password-here
SKYVERN_AUTH_BITWARDEN_CLIENT_ID=user.your-client-id-here
SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET=your-client-secret-here

# Vaultwarden Configuration
BW_HOST=https://your-vaultwarden-server.com
BW_CLIENTID=${SKYVERN_AUTH_BITWARDEN_CLIENT_ID}
BW_CLIENTSECRET=${SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET}
BW_PASSWORD=${SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD}
```

Then start the service:

```bash
docker-compose up -d bitwarden-cli
```

## Available Endpoints

Once running, the CLI server provides these endpoints on port 8002:

- `GET /status` - Check server status
- `POST /unlock` - Unlock vault
- `GET /list/object/items` - List vault items
- `GET /object/item/{id}` - Get specific item
- `POST /object/item` - Create new item
- `GET /object/template/item` - Get item template
- And more...

## Troubleshooting

### Container won't start

1. **Check logs**:
   ```bash
   docker-compose -f docker-compose.bitwarden.yml logs bitwarden-cli
   ```

2. **Common issues**:
   - Invalid API credentials
   - Wrong vaultwarden server URL
   - Network connectivity issues
   - Incorrect master password

### Health check fails

The container includes a health check that calls `/status`. If it fails:

1. Check if the CLI server is actually running inside the container
2. Verify the unlock process succeeded
3. Check network configuration

### API calls fail

1. **Test the CLI server directly**:
   ```bash
   # Check status
   curl http://localhost:8002/status

   # List items (after unlock)
   curl http://localhost:8002/list/object/items
   ```

2. **Check Skyvern configuration**:
   - Ensure `BITWARDEN_SERVER` points to the CLI server
   - Verify `BITWARDEN_SERVER_PORT` is correct

## Security Notes

- The container runs as a non-root user for security
- Only binds to localhost by default
- API credentials are passed via environment variables
- Consider using Docker secrets for production deployments

## Production Considerations

1. **Secrets Management**: Use Docker secrets or external secret management
2. **Monitoring**: Add proper logging and monitoring
3. **Backup**: Ensure your vaultwarden instance is properly backed up
4. **Updates**: Regularly update the Bitwarden CLI version
5. **Network Security**: Use proper network isolation and firewalls