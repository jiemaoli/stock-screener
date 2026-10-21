# VPS deployment

The production deployment follows the existing `/root/apps` and
`/root/infrastructure` layout:

- Application: `/root/apps/stock-screener`
- Compose file: `/root/apps/stock-screener/deploy/docker-compose.yml`
- Persistent data: `/root/apps/stock-screener/data`
- Shared Docker network: `infrastructure_infra-net`
- Reverse proxy: the existing Nginx container at `stock-min.hhxs.top`

The `dashboard` service serves the Flask application with Gunicorn. The
`scheduler` service runs a conservative full-market scan at 18:15 New York
time on weekdays. Both services share the persistent `data` directory.

Useful commands:

```bash
cd /root/apps/stock-screener
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f dashboard
docker compose -f deploy/docker-compose.yml logs -f scheduler
docker exec stock-screener-scheduler python run_optimized_scan.py --test-mode --git-storage
```

## Package deployment (same pattern as finTwt)

Build the deployment archive from the current local working tree. GitHub is not
part of the production deployment path:

```powershell
.\scripts\package_deploy.ps1
scp -i C:\Users\Jerry\.ssh\id_ed25519 -P 56787 `
  ..\stock_screener_deploy.tar.gz `
  root@192.210.254.31:/root/apps/
ssh -i C:\Users\Jerry\.ssh\id_ed25519 -p 56787 root@192.210.254.31 `
  "tar -xzf /root/apps/stock_screener_deploy.tar.gz -C /root/apps/stock-screener ./scripts/deploy.sh && chmod +x /root/apps/stock-screener/scripts/deploy.sh && /root/apps/stock-screener/scripts/deploy.sh"
```

The package excludes `.git`, `.env`, runtime data, logs, caches, and
credentials. The VPS preserves its existing `.env` and `data/`, builds the
Docker image locally, reloads Nginx, deletes Git metadata, and removes the
uploaded archive after a successful deployment.
