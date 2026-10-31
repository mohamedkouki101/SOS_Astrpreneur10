# Troubleshooting

| Symptom | Check |
|---|---|
| Port 8080 not responding | `docker compose -p hr -f hr.yml ps`; the site may still be in progress (`logs -f create-site`). |
| `create-site` keeps waiting | The db or redis container is unhealthy: `logs db`. |
| New app missing in queue/scheduler | The app was installed in a running container instead of being built into the image. Rebuild. |
| Scheduled jobs not running | Run `enable-scheduler` (it's disabled on a fresh site). |
| Assets/CSS broken after rebuild | Restart `frontend` and `backend` so they share the new image tag. |
| Out of memory on WSL2 | Raise the memory limit in `%UserProfile%\.wslconfig`. |
