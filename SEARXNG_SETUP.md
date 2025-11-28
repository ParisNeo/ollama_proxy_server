# SearXNG Stack Setup

This directory contains the configuration for the SearXNG search engine with Caddy reverse proxy.

## Architecture

- **SearXNG**: Runs on port 8080 (internal HTTP)
- **Caddy**: Reverse proxy on port 7019 (external HTTPS)
- **Network**: Both containers on `searxng-network`

## Quick Fix

If SearXNG stops working, run:

**Windows (PowerShell):**
```powershell
.\fix_searxng_stack.ps1
```

**Linux/Mac (Bash):**
```bash
chmod +x fix_searxng_stack.sh
./fix_searxng_stack.sh
```

## Manual Setup

1. **Create network:**
   ```bash
   podman network create searxng-network
   ```

2. **Start SearXNG:**
   ```bash
   podman run -d \
     --name searxng \
     --network searxng-network \
     -p 8080:8080 \
     -v searxng-data:/etc/searxng \
     -v searxng-config:/var/log/searxng \
     -e SEARXNG_HOSTNAME=localhost:7019 \
     --restart unless-stopped \
     searxng/searxng:latest
   ```

3. **Start Caddy:**
   ```bash
   podman run -d \
     --name caddy-searxng \
     --network searxng-network \
     -p 7019:7019 \
     -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" \
     -v caddy-data:/data \
     -v caddy-config:/config \
     --restart unless-stopped \
     caddy:latest \
     caddy run --config /etc/caddy/Caddyfile
   ```

## Testing

Test the connection:
```bash
curl -k https://localhost:7019/
```

Or test the search API:
```bash
curl -k -X POST https://localhost:7019/search \
  -d "q=test&format=json"
```

## Troubleshooting

1. **Check container status:**
   ```bash
   podman ps --filter "name=searxng"
   podman ps --filter "name=caddy"
   ```

2. **View logs:**
   ```bash
   podman logs caddy-searxng
   podman logs searxng
   ```

3. **Restart containers:**
   ```bash
   podman restart caddy-searxng
   podman restart searxng
   ```

4. **If still not working, recreate:**
   ```bash
   podman stop caddy-searxng searxng
   podman rm caddy-searxng searxng
   # Then run fix_searxng_stack script again
   ```

## Configuration Files

- `Caddyfile`: Caddy reverse proxy configuration
- `docker-compose.searxng.yml`: Docker Compose configuration (alternative)
- `fix_searxng_stack.ps1`: Windows PowerShell fix script
- `fix_searxng_stack.sh`: Linux/Mac Bash fix script

## Environment Variable

The application uses `SEARXNG_URL` environment variable (default: `https://localhost:7019`).

Set it in your `.env` file:
```
SEARXNG_URL=https://localhost:7019
```

