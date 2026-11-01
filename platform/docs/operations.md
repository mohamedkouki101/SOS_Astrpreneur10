# Operations

All commands assume project `hr`, file `hr.yml`, site `frontend`.

| Task | Command |
|---|---|
| Start | `docker compose -p hr -f hr.yml up -d` |
| Stop | `docker compose -p hr -f hr.yml down` |
| Logs | `docker compose -p hr -f hr.yml logs -f backend` |
| Shell | `docker compose -p hr -f hr.yml exec backend bash` |
| Versions | `docker compose -p hr -f hr.yml exec backend bench version --format table` |
| Migrate | `docker compose -p hr -f hr.yml exec backend bench --site frontend migrate` |
| Enable scheduler | `docker compose -p hr -f hr.yml exec backend bench --site frontend enable-scheduler` |
| Backup | `docker compose -p hr -f hr.yml exec backend bench --site frontend backup --with-files` |

Backups are written inside the `sites` volume under `frontend/private/backups`.

`down -v` deletes the volumes, and with them the database and site files. Only use it to start from scratch.
