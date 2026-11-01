# platform — the HR Flow HR platform (Frappe HR v16 in Docker)

HR Flow is **Frappe Framework v16 + ERPNext v16 + Frappe HR (HRMS) v16**, rebranded, with our two
Frappe apps baked into the image:

| App | Source | What it adds |
|---|---|---|
| `erpnext`, `hrms` | upstream, pinned in [`apps.json`](apps.json) (`v16.53.0`; the same HRMS source is in [`../hrms`](../hrms)) | HR system of record |
| `hr_ai` | [`../hr_ai`](../hr_ai) | AI Action queue, HR AI decision page, HR Chat, Inbound Email, Execution Gateway |
| `hrflow_theme` | [`../hrflow_theme`](../hrflow_theme) | HR Flow look (Desk, login, website) |

This folder is based on [frappe_docker](https://github.com/frappe/frappe_docker) (MIT, see [LICENSE](LICENSE)).
What we changed or added:

| File | Change |
|---|---|
| `hr.yml` | Ready-to-run stack: project `hr`, site `frontend`, UI on **port 8001**, image `custom:N`, **Mailpit** test mail server (8025 / 1025) |
| `apps.json` | ERPNext + HRMS pinned to `v16.53.0` |
| `images/hr-ai/`, `images/hrflow-theme/` | Image layers that add `hr_ai` and `hrflow_theme` |
| `build-images.sh` | Builds the three layers in one command |
| `Data_set/` | Fictional demo company **Bara3am** + loader (`seed.yml`) + HR Flow branding |

## Build and start

From the repository root (WSL / Linux, Docker with Compose v2, ~8 GB RAM, ~20 GB disk):

```sh
bash platform/build-images.sh                      # ~15 min the first time -> the image named in hr.yml
docker compose -p hr -f platform/hr.yml up -d      # first start creates the site (1–3 min)
docker compose -p hr -f platform/hr.yml logs -f create-site
docker compose -p hr -f platform/hr.yml exec backend bench version --format table   # all 16.x
```

Then open http://localhost:8001, log in as `Administrator` / `admin` and finish the setup wizard
(company **Bara3am**, country Tunisia, currency TND). Load the demo data:

```sh
ADMIN_PASSWORD=admin docker compose -p hr -f platform/hr.yml -f platform/Data_set/seed.yml run --rm --build seed
```

The rest of the configuration (our apps, keys, mail account, demo users) is in
[`../JURY_GUIDE.md`](../JURY_GUIDE.md).

## Day-to-day

- After changing `hr_ai` or `hrflow_theme`: rebuild (`bash platform/build-images.sh`), then
  `docker compose -p hr -f platform/hr.yml up -d` and `... exec backend bench --site frontend migrate`.
  Never install apps only inside a running container: the queue and scheduler containers would not see them.
- Reset everything (deletes the database): `docker compose -p hr -f platform/hr.yml down -v`.
- `admin` and the database root password in `hr.yml` are local-demo defaults. Change them on any shared machine.

More: [docs/setup.md](docs/setup.md), [docs/operations.md](docs/operations.md), [docs/troubleshooting.md](docs/troubleshooting.md),
[Data_set/README.md](Data_set/README.md).
