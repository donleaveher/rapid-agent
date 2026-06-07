# Deploying SQLoop to Google Cloud Run

The dashboard (`app.py`) deploys to **Cloud Run** as a container. This also satisfies
the hackathon requirement of running on Google Cloud (Vertex AI backend + $100 credits).

## TL;DR

```bash
gcloud config set project rapid-agent-498122
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com

# (optional) Phoenix tracing secret — the app boots fine without it
printf 'api_key=%s' "<PHOENIX_KEY>" | \
  gcloud secrets create sqloop-phoenix --data-file=- --replication-policy=automatic

./deploy.sh           # vertex backend (submission default)
```

`deploy.sh` prints the public HTTPS URL at the end.

## What gets deployed

The Docker image is **not** the whole repo. `deploy.sh` stages a slim context in
`.deploy/`:

| Included (~110 MB)                                   | Excluded (the 1.7 G we skip)        |
|-----------------------------------------------------|-------------------------------------|
| `sqloop/`, `app.py`, `main.py`, `build_memory.py`   | `data/spider/test_database/` (871 M)|
| the **20 Spider dev DBs** the dashboard runs        | the other ~146 non-dev DBs (740 M)  |
| `data/spider/dev.json`                              | `train_*.json`, `test*.json`, gold  |
| result artifacts: `curve.*.json`, `optimizer_reports/`, `configs/`, `memory.json` | `.git`, `.venv`, `.env` |

Dependencies install from `requirements.txt` (a PyPI-resolved export of `uv.lock`).
We don't `uv sync` in the image because the lock is pinned to a China mirror that
Cloud Build can't reach.

## Backends

### Vertex (submission default — `./deploy.sh`)
- Sets `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`.
- **No API key.** Vertex authenticates via the Cloud Run runtime service account (ADC).
  Grant it once:
  ```bash
  PROJECT=$(gcloud config get-value project)
  NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/aiplatform.user"
  ```
  (Replace with a dedicated service account via `--service-account` on deploy if you
  don't want to use the default compute SA.)

### AI Studio (free dev demo — `BACKEND=aistudio ./deploy.sh`)
- Sets `GOOGLE_GENAI_USE_VERTEXAI=FALSE` and reads the key from a secret:
  ```bash
  printf '%s' "<AI_STUDIO_KEY>" | \
    gcloud secrets create sqloop-gemini --data-file=- --replication-policy=automatic
  BACKEND=aistudio ./deploy.sh
  ```

## Tracing (optional)

If the `sqloop-phoenix` secret exists, `PHOENIX_CLIENT_HEADERS` is wired up and every
turn traces to Phoenix Cloud. If it's missing, `deploy.sh` says so and deploys without
tracing — the app still works (see `instrumentation.setup_tracing()`: no endpoint → no-op).

## Knobs

| Env var          | Default                          | Notes                                  |
|------------------|----------------------------------|----------------------------------------|
| `REGION`         | `us-central1`                    | Cloud Run + Vertex region              |
| `SERVICE`        | `sqloop`                         | Cloud Run service name                 |
| `BACKEND`        | `vertex`                         | `vertex` \| `aistudio`                 |
| `SQLOOP_CONFIG`  | `data/configs/active.vertex.json`| committed config the live pipeline uses|
| `GCP_PROJECT`    | `gcloud config` project          | target project                         |

## Cold starts

The image is large (Phoenix + ADK + Gradio + scipy/sklearn). With `min-instances=0`
the first hit after idle is ~15–30 s. For live judging, keep one warm:

```bash
gcloud run services update sqloop --region us-central1 --min-instances=1
```

(Costs a bit while idle — turn it back to 0 afterwards.)

## Re-deploy

Just re-run `./deploy.sh`. It re-exports requirements, re-stages `.deploy/`, and
ships a new revision. Both `.deploy/` and `requirements.txt` are git-ignored.
