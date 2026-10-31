# Setup

## Image build

`images/layered/Containerfile` builds on prebuilt base/build images and installs the apps from `apps.json`, which is passed as a build secret.

| Build arg | Value | Meaning |
|---|---|---|
| `FRAPPE_PATH` | framework git URL | Framework source |
| `FRAPPE_BRANCH` | `version-16` | Framework branch |
| secret `apps_json` | `apps.json` | Apps to install (url + branch) |

Other build files in `images/`: `custom/` (full build from scratch), `production/` and `bench/`. They aren't used by default.

## Generating a Compose stack

`hr.yml` is already generated. To regenerate it or build a variant from the base file:

```sh
docker compose -f compose.yaml \
  -f overrides/compose.mariadb.yaml \
  -f overrides/compose.redis.yaml \
  -f overrides/compose.noproxy.yaml \
  config > compose.custom.yaml
```

Set `CUSTOM_IMAGE=custom` and `CUSTOM_TAG=16` in `.env` (copy it from `example.env`) so the generated file uses your image. `compose.custom.yaml` is gitignored.

## Hiding non-HR modules

The ERP app is a required dependency of the HR app. Hide the modules you don't need through Module Profiles in the UI.
