# Skyvern Production Deployment Guide

## Architecture Overview

```
+-------------------+     +------------------------+     +------------------+
|  app.jadong.shop  | --> |      Vercel CDN        | --> |  React Frontend  |
|   (Frontend)      |     |   (Edge Network)       |     |                  |
+-------------------+     +------------------------+     +------------------+
         |
         | API Calls
         v
+-------------------+     +------------------------+     +------------------+
|  api.jadong.shop  | --> |  Cloudflare Tunnel     | --> |  WSL2 Ubuntu     |
|   (Backend API)   |     |  (skyvern-prod)        |     |  localhost:8000  |
+-------------------+     +------------------------+     +------------------+
```

## Domain Configuration

| Domain | Service | Platform |
|--------|---------|----------|
| `app.jadong.shop` | Skyvern Frontend | Vercel |
| `api.jadong.shop` | Skyvern Backend API | Cloudflare Tunnel → WSL |

## DNS Records (Cloudflare)

| Type | Name | Target | Proxy |
|------|------|--------|-------|
| CNAME | `app` | `cname.vercel-dns.com` | DNS only (gray) |
| CNAME | `api` | `046e6aee-...cfargotunnel.com` | Proxied (orange) |

## Environment Variables

### Vercel (Frontend)
- `VITE_API_BASE_URL`: `https://api.jadong.shop/api/v1`
- `VITE_WSS_BASE_URL`: `wss://api.jadong.shop/api/v1`
- `VITE_ARTIFACT_API_BASE_URL`: `https://api.jadong.shop`
- `VITE_SKYVERN_API_KEY`: (API key from backend)

### WSL Backend (.env)
- `PORT`: `8000`
- `SKYVERN_API_KEY`: (generated key)

## Running the Backend

### Start Skyvern Server
```bash
cd ~/projects/skyvern
uv run skyvern run server
```

### Cloudflare Tunnel (systemd service)
```bash
# Check status
sudo systemctl status cloudflared

# Restart if needed
sudo systemctl restart cloudflared
```

## Vercel Deployment

```bash
cd ~/projects/skyvern/skyvern-frontend
npm run build
npx vercel --prod
```

## Troubleshooting

### 502 Bad Gateway
- Check if Skyvern is running: `ss -tlnp | grep 8000`
- Check cloudflared status: `sudo systemctl status cloudflared`
- Restart cloudflared: `sudo systemctl restart cloudflared`

### API Connection Issues
- Verify DNS: `ping api.jadong.shop`
- Test local: `curl http://localhost:8000/docs`
- Test tunnel: `curl https://api.jadong.shop/docs`

### Frontend Not Loading
- Check Vercel deployment status
- Verify environment variables in Vercel dashboard
- Redeploy: `npx vercel --prod`

## Quick Start Commands

```bash
# Start everything
cd ~/projects/skyvern
nohup uv run skyvern run server > skyvern-server.log 2>&1 &

# Check status
curl https://api.jadong.shop/docs
curl https://app.jadong.shop
```

## Created: 2025-11-30
