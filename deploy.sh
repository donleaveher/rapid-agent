#!/usr/bin/env bash
#
# Deploy the SQLoop dashboard (app.py) to Google Cloud Run.
#
# What it does:
#   1. Export a PyPI-clean requirements.txt from uv.lock (the lock itself is
#      pinned to a China mirror that Cloud Build can't reach).
#   2. Stage a SLIM build context in .deploy/ : code + the 20 Spider dev DBs
#      (~110 MB) instead of the full 1.7 G dataset.
#   3. gcloud run deploy --source .deploy  (Cloud Build builds the image).
#
# Prerequisites (one-time):
#   - gcloud CLI installed + `gcloud auth login` + `gcloud config set project <id>`
#   - APIs enabled:  gcloud services enable run.googleapis.com \
#                      cloudbuild.googleapis.com aiplatform.googleapis.com \
#                      secretmanager.googleapis.com
#   - (vertex backend) the Cloud Run runtime service account has roles/aiplatform.user
#   - (optional) Phoenix tracing secret:
#       printf 'api_key=%s' "<PHOENIX_KEY>" | \
#         gcloud secrets create sqloop-phoenix --data-file=- --replication-policy=automatic
#   - (aistudio backend only) Gemini key secret:
#       printf '%s' "<AI_STUDIO_KEY>" | \
#         gcloud secrets create sqloop-gemini --data-file=- --replication-policy=automatic
#
# Usage:
#   ./deploy.sh                       # vertex backend (submission default)
#   BACKEND=aistudio ./deploy.sh      # free AI Studio key (dev demo)
#   REGION=us-east1 SERVICE=sqloop ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (override via env) ---------------------------------------------
PROJECT="${GCP_PROJECT:-rapid-agent-498122}"
REGION="${REGION:-us-central1}"        # Cloud Run region (independent of Vertex)
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"  # Vertex model location; gemini-3.5-flash is served from `global`, not us-central1
SERVICE="${SERVICE:-sqloop}"
BACKEND="${BACKEND:-vertex}"            # vertex | aistudio
PHOENIX_ENDPOINT="${PHOENIX_ENDPOINT:-https://app.phoenix.arize.com/s/c2303372901}"
PHOENIX_SECRET="${PHOENIX_SECRET:-sqloop-phoenix}"   # Secret Manager name (api_key=...)
GEMINI_SECRET="${GEMINI_SECRET:-sqloop-gemini}"      # aistudio only
# Use the Vertex-committed config so the dashboard shows the submission run.
SQLOOP_CONFIG="${SQLOOP_CONFIG:-data/configs/active.vertex.json}"

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: no GCP project. Run 'gcloud config set project <id>' or set GCP_PROJECT." >&2
  exit 1
fi

# ---- 1. PyPI-clean requirements from the (mirror-pinned) lock ---------------
echo ">> [1/3] exporting requirements.txt from uv.lock (PyPI-resolved)"
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt

# ---- 2. stage a slim build context -----------------------------------------
echo ">> [2/3] staging slim build context in .deploy/"
rm -rf .deploy
mkdir -p .deploy/data/spider/database .deploy/data/configs .deploy/data/optimizer_reports

# code (rsync drops __pycache__ / pyc)
rsync -a --exclude='__pycache__' --exclude='*.pyc' sqloop .deploy/
cp app.py main.py build_memory.py requirements.txt Dockerfile pyproject.toml README.md LICENSE .deploy/
# minimal .dockerignore inside the context (belt-and-suspenders hygiene)
printf '.git\n**/__pycache__/\n*.py[cod]\n.DS_Store\n' > .deploy/.dockerignore

# Spider: dev manifest + ONLY the dev DBs the dashboard executes against (~110 MB)
cp data/spider/dev.json .deploy/data/spider/
for db in $(python3 -c "import json;print(' '.join(sorted({e['db_id'] for e in json.load(open('data/spider/dev.json'))})))"); do
  cp -R "data/spider/database/$db" .deploy/data/spider/database/
done

# self-improvement result artifacts the dashboard renders (all tiny)
cp data/curve.vertex.json data/curve.vertex_pro.json data/curve.flash.json data/curve.pro.json .deploy/data/ 2>/dev/null || true
cp data/memory.json .deploy/data/ 2>/dev/null || true
cp data/configs/active*.json .deploy/data/configs/ 2>/dev/null || true
cp data/optimizer_reports/*.md .deploy/data/optimizer_reports/ 2>/dev/null || true

echo "   context size: $(du -sh .deploy | cut -f1)  ($(find .deploy/data/spider/database -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ') dev DBs)"

# ---- 3. deploy to Cloud Run -------------------------------------------------
# Common env vars. Phoenix endpoint is added below, ONLY when its key secret
# exists -- setting the endpoint without the key 401s on every span export.
ENV_VARS="SQLOOP_CONFIG=${SQLOOP_CONFIG}"
SECRETS=""

case "$BACKEND" in
  vertex)
    # Vertex auth via the Cloud Run runtime service account (ADC) — no API key.
    # GOOGLE_CLOUD_LOCATION (Vertex) is INDEPENDENT of the Cloud Run REGION: the
    # submission model gemini-3.5-flash is served from location=global, not
    # us-central1 (where it 404s). The code default (gemini-flash-lite-latest)
    # also 404s on Vertex. Override either via VERTEX_LOCATION / GEMINI_MODEL.
    ENV_VARS="${ENV_VARS},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}"
    ;;
  aistudio)
    ENV_VARS="${ENV_VARS},GOOGLE_GENAI_USE_VERTEXAI=FALSE,GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}"
    SECRETS="GOOGLE_API_KEY=${GEMINI_SECRET}:latest"
    ;;
  *) echo "ERROR: BACKEND must be 'vertex' or 'aistudio'." >&2; exit 1 ;;
esac

# Wire up Phoenix tracing ONLY when the api_key secret exists. Endpoint + key go
# together: setting the endpoint without the key makes every span batch 401 (see
# CLAUDE.md trap #2). With the secret absent we leave all Phoenix env unset, so
# setup_tracing() sees no endpoint and cleanly no-ops (the app still boots).
if gcloud secrets describe "$PHOENIX_SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  ENV_VARS="${ENV_VARS},PHOENIX_COLLECTOR_ENDPOINT=${PHOENIX_ENDPOINT},PHOENIX_PROJECT_NAME=rapid-agent"
  SECRETS="${SECRETS:+$SECRETS,}PHOENIX_CLIENT_HEADERS=${PHOENIX_SECRET}:latest"
else
  echo "   note: secret '$PHOENIX_SECRET' not found -> deploying with tracing OFF (no Phoenix endpoint set)."
fi

echo ">> [3/3] deploying '$SERVICE' to Cloud Run ($REGION, project $PROJECT, backend=$BACKEND)"
DEPLOY_ARGS=(
  run deploy "$SERVICE"
  --source .deploy
  --project "$PROJECT"
  --region "$REGION"
  --platform managed
  --allow-unauthenticated
  --memory 2Gi --cpu 2 --timeout 600 --port 8080
  --set-env-vars "$ENV_VARS"
)
[[ -n "$SECRETS" ]] && DEPLOY_ARGS+=(--set-secrets "$SECRETS")

gcloud "${DEPLOY_ARGS[@]}"

echo ">> done. Service URL:"
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(status.url)'
