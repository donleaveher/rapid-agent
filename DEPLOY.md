# Deploying SQLoop to Google Cloud Run

The dashboard (`app.py`) deploys to **Cloud Run** as a container. This also satisfies
the hackathon requirement of running on Google Cloud (Vertex AI backend + $100 credits).

## TL;DR

```bash
gcloud config set project rapid-agent-498122
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com

# Grant the Cloud Run runtime service account what it needs.
NUM=$(gcloud projects describe rapid-agent-498122 --format='value(projectNumber)')
SA="${NUM}-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding rapid-agent-498122 \
  --member="serviceAccount:$SA" --role="roles/aiplatform.user"          # call Vertex

# (recommended) Phoenix tracing secret — derive the full "api_key=..." header
# straight from .env so you never paste the key by hand (see Tracing section):
printf '%s' "$(grep '^PHOENIX_CLIENT_HEADERS=' .env | cut -d= -f2-)" | \
  gcloud secrets create sqloop-phoenix --data-file=- --replication-policy=automatic
gcloud secrets add-iam-policy-binding sqloop-phoenix \
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"  # read the secret

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
  NUM=$(gcloud projects describe rapid-agent-498122 --format='value(projectNumber)')
  gcloud projects add-iam-policy-binding rapid-agent-498122 \
    --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/aiplatform.user"
  ```
  (Replace with a dedicated service account via `--service-account` on deploy if you
  don't want to use the default compute SA.)

### AI Studio (free dev demo — `BACKEND=aistudio ./deploy.sh`)
- Sets `GOOGLE_GENAI_USE_VERTEXAI=FALSE` and reads the key from a secret (the runtime
  service account needs read access on it, same as the Phoenix secret):
  ```bash
  printf '%s' "<AI_STUDIO_KEY>" | \
    gcloud secrets create sqloop-gemini --data-file=- --replication-policy=automatic
  NUM=$(gcloud projects describe rapid-agent-498122 --format='value(projectNumber)')
  gcloud secrets add-iam-policy-binding sqloop-gemini \
    --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
  BACKEND=aistudio ./deploy.sh
  ```

## Tracing (recommended)

Tracing is the core of the Arize-track story, so you almost certainly want it on: with
the secret present, every Router → Schema-Linker → Generator → Executor → Repair step of
a live "Ask" turn streams to Phoenix Cloud. Without it the app still boots and the
dashboard (including the self-improvement curve, which renders baked-in artifacts) works —
live turns just aren't traced.

`deploy.sh` mounts the `sqloop-phoenix` secret into `PHOENIX_CLIENT_HEADERS` if it exists.
The Cloud Run runtime service account must be allowed to *read* it, or the deploy fails
with `Permission denied on secret ...`:

```bash
NUM=$(gcloud projects describe rapid-agent-498122 --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding sqloop-phoenix \
  --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

(Same grant on `sqloop-gemini` if you use the AI Studio backend.)

**The secret must hold the FULL header value `api_key=<key>`** — not the bare key — because
the app parses `api_key=` out of `PHOENIX_CLIENT_HEADERS` (`instrumentation._api_key_from_env`).

- Easiest (no manual key handling): the TL;DR command above pipes the exact value out of `.env`.
- By hand: `printf 'api_key=%s' "<BARE_KEY>"` — put **only** the raw key in the placeholder;
  the `api_key=` prefix is added for you, so don't paste `api_key=...` or you'll double it.

Verify the format (prints `api_key=`, not the secret itself):

```bash
gcloud secrets versions access latest --secret=sqloop-phoenix | head -c 8
```

## Knobs

| Env var          | Default                          | Notes                                  |
|------------------|----------------------------------|----------------------------------------|
| `REGION`         | `us-central1`                    | Cloud Run + Vertex region              |
| `SERVICE`        | `sqloop`                         | Cloud Run service name                 |
| `BACKEND`        | `vertex`                         | `vertex` \| `aistudio`                 |
| `GEMINI_MODEL`   | `gemini-2.5-flash` (vertex)      | must exist on Vertex here; `gemini-3.5-flash` 404s |
| `SQLOOP_CONFIG`  | `data/configs/active.vertex.json`| committed config the live pipeline uses|
| `GCP_PROJECT`    | `rapid-agent-498122`             | target project                         |

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
