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
