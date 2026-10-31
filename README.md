# SOS_Astrpreneur1

Container setup for the HR system of record behind **SOS_Astrpreneur1**, a secure AI HR assistant. AI agents screen and rank resumes, digest the inbox, draft replies and answer employee questions. A human approves every action.

This repository holds the Docker images and Compose configuration that run the HR platform (HR + ERP apps, version 16) locally.

## Contents

```
.
├── apps.json        # Apps baked into the custom image
├── hr.yml           # Ready-to-run Compose stack (project "hr", site "frontend")
├── compose.yaml     # Base Compose file used to generate stacks
├── overrides/       # Compose overrides (database, redis, proxy, TLS, ...)
├── images/          # Container build files (layered build is the default)
├── resources/       # Entrypoints and nginx templates copied into images
├── example.env      # Example environment variables
└── docs/            # Setup, operations, troubleshooting
```

## Prerequisites

- Docker with Compose v2 (on Windows: WSL2 Ubuntu)
- git
- ~8 GB free RAM, ~20 GB disk

## Setup

1. Clone and enter the repo:

   ```sh
   git clone https://github.com/mohamedkouki101/SOS_Astrpreneur1.git
   cd SOS_Astrpreneur1
   ```

2. Build the image with the apps listed in `apps.json`. This takes 10–15 min:

   ```sh
   docker build \
     --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
     --build-arg=FRAPPE_BRANCH=version-16 \
     --secret=id=apps_json,src=apps.json \
     --tag=custom:16 \
     --file=images/layered/Containerfile .
   ```

3. Start the stack:

   ```sh
   docker compose -p hr -f hr.yml up -d
   ```

   The first start creates the site. Follow it with `docker compose -p hr -f hr.yml logs -f create-site`.

4. Check that the versions are all 16.x:

   ```sh
   docker compose -p hr -f hr.yml exec backend bench version --format table
   ```

## Usage

- Open http://localhost:8080 and log in as `Administrator` / `admin`.
- **Change this password right away.** The database root password in `hr.yml` is also a local-demo default.
- Enable background jobs (needed for scheduled tasks):

  ```sh
  docker compose -p hr -f hr.yml exec backend bench --site frontend enable-scheduler
  ```

- After changing apps: add them to `apps.json`, rebuild with a new tag (`custom:N`), update the tags in `hr.yml`, then run:

  ```sh
  docker compose -p hr -f hr.yml up -d
  docker compose -p hr -f hr.yml exec backend bench --site frontend migrate
  ```

  Never install apps only inside a running container. The queue and scheduler containers won't see them.

- API check (create an API key for a user first):

  ```sh
  curl -H "Authorization: token KEY:SECRET" http://localhost:8080/api/resource/Employee
  ```

More: [docs/setup.md](docs/setup.md), [docs/operations.md](docs/operations.md), [docs/troubleshooting.md](docs/troubleshooting.md).

## License

MIT. See [LICENSE](LICENSE). The apps installed into the image (`apps.json`) keep their own licenses.
