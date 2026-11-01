#!/usr/bin/env bash
# Builds the HR Flow image used by hr.yml (same tag as in hr.yml), in three layers:
#   1. Frappe v16 + ERPNext + HRMS (versions pinned in apps.json; hrms v16.53.0 = ../hrms)
#   2. + hr_ai        (../hr_ai: AI Action queue, HR AI page, HR Chat, Execution Gateway)
#   3. + hrflow_theme (../hrflow_theme: HR Flow look)
# About 15 minutes the first time. Run from anywhere: bash platform/build-images.sh
set -euo pipefail
cd "$(dirname "$0")"
TAG="${TAG:-$(grep -m1 -oE 'image: custom:[A-Za-z0-9._-]+' hr.yml | cut -d" " -f2)}"

docker build \
  --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg=FRAPPE_BRANCH=version-16 \
  --secret=id=apps_json,src=apps.json \
  --tag=rhflow-base:16 \
  --file=images/layered/Containerfile .

docker build -f images/hr-ai/Containerfile --build-arg BASE=rhflow-base:16 -t rhflow-hrai:16 ../hr_ai
docker build -f images/hrflow-theme/Containerfile --build-arg BASE=rhflow-hrai:16 -t "$TAG" ../hrflow_theme

echo "Built $TAG. Start the stack with: docker compose -p hr -f platform/hr.yml up -d"
