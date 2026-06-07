# SQLoop — Deployment (Devpost)

> Copy-paste section for the Devpost submission. Shows SQLoop running on Google
> Cloud, which is also how it satisfies the "built on Google Cloud + $100 credits"
> requirement for the Arize track.

## Try it live

**▶️ https://sqloop-448803070690.us-central1.run.app**

Open the **Ask SQLoop** tab, pick a Spider database, ask a question in English — the
agent links the schema, generates SQL on **Vertex AI**, executes it, and answers. The
**Self-improvement** tab shows the rising execution-accuracy curve the loop produced.
*(First load may take ~1 min if the instance is cold.)*

## How it's deployed

SQLoop's Gradio dashboard runs as a container on **Google Cloud Run** (fully managed,
scales to zero), with one command: `./deploy.sh`.

| Layer | What we use | Why it matters |
|-------|-------------|----------------|
| **Compute** | Google Cloud Run (`us-central1`) | Serverless container, public HTTPS, autoscaling — no infra to babysit |
| **LLM / generation** | **Vertex AI**, `gemini-3.5-flash` (location `global`) | The submission backend; this is where the GCP credits are spent |
| **Auth** | Cloud Run runtime **service account** (ADC) | No API keys in the image — Vertex is called with `roles/aiplatform.user` |
| **Secrets** | Secret Manager (`sqloop-phoenix`) | Phoenix key mounted as an env var, never baked into the image |
| **Observability** | OpenInference → **Arize Phoenix Cloud** | Every Router/Schema-Linker/Generator/Executor/Repair step is a live span |

## Engineering details worth noting

- **Lean image, not the whole dataset.** Spider is 1.7 GB; the dashboard only needs the
  20 dev databases it executes against. `deploy.sh` stages a slim build context
  (~110 MB) so the image stays small and builds fast on Cloud Build.
- **Reproducible deps without a mirror.** The repo's lockfile is pinned to a regional
  PyPI mirror; the build exports a clean, index-free `requirements.txt` so Cloud Build
  installs from PyPI directly.
- **One config flips dev ↔ submission.** `GOOGLE_GENAI_USE_VERTEXAI` switches between a
  free AI Studio key (development) and Vertex AI (submission) — same code path, traced
  identically to Phoenix.
- **Region ≠ model location.** Cloud Run runs in `us-central1`, but `gemini-3.5-flash`
  is served from Vertex's `global` location — the deploy sets these independently.

## Reproduce it

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com
# grant the runtime SA roles/aiplatform.user + roles/secretmanager.secretAccessor
# (full commands in DEPLOY.md)
./deploy.sh
```

Full instructions, IAM grants, and knobs: see [`DEPLOY.md`](../DEPLOY.md).
