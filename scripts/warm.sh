#!/usr/bin/env bash
#
# Toggle a warm Cloud Run instance for SQLoop.
#
# SQLoop's image is large (Phoenix + ADK + Gradio + scipy/sklearn), so a cold
# start takes ~70s. Keep one instance warm during judging so the first visitor
# doesn't wait. A warm instance bills continuously while idle, so turn it OFF
# again afterwards.
#
#   ./scripts/warm.sh on     # min-instances=1  (judging window)
#   ./scripts/warm.sh off     # min-instances=0  (default; scale to zero, no idle cost)
#   ./scripts/warm.sh status  # show current min-instances
set -euo pipefail

PROJECT="${GCP_PROJECT:-rapid-agent-498122}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-sqloop}"

_min() {
  gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])' 2>/dev/null
}

case "${1:-}" in
  on)  MIN=1 ;;
  off) MIN=0 ;;
  status)
    cur="$(_min)"; echo "min-instances = ${cur:-0}  ($SERVICE @ $REGION)"; exit 0 ;;
  *) echo "usage: $0 on|off|status" >&2; exit 1 ;;
esac

if [[ "$MIN" == 1 ]]; then
  echo "NOTE: a warm instance bills continuously while idle — run '$0 off' after judging."
fi
echo ">> setting min-instances=$MIN on $SERVICE ($REGION, $PROJECT)"
gcloud run services update "$SERVICE" \
  --project "$PROJECT" --region "$REGION" \
  --min-instances="$MIN" \
  --format='value(metadata.name)' >/dev/null
echo ">> done. min-instances now = $(_min)"
