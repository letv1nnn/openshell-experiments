# Leadership Report Agent

Self-contained agent that fetches Google Meet/Gemini meeting notes, analyzes them via an LLM (vLLM or any OpenAI-compatible endpoint), generates a structured leadership report, and publishes it to a Google Doc.

## Pipeline

```
Google Drive       LLM (vLLM)        Google Docs
 (meeting    --->  (analyze &   --->  (formatted
  notes)            structure)         report)
```

1. **Auth check** — validates Google credentials before doing any work
2. **Fetch** — searches Google Drive for the latest Gemini notes matching the meeting name, exports as plain text
3. **Analyze** — sends notes to the LLM with a structured prompt, receives JSON report back via Chat Completions API with JSON mode
4. **Push** — inserts the formatted report (date header, summary, bulleted items with bold labels) at the top of the target Google Doc

Transient failures (network, server errors) are retried once after 30 seconds.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_BASE_URL` | No | Inference endpoint (default: `http://inference.local/v1` for OpenShell; override for standalone use) |
| `OPENAI_API_KEY` | Yes | API key (`none` for unauthenticated vLLM) |
| `MODEL_ID` | Yes | Model identifier served by the endpoint |
| `LEADERSHIP_DOC_ID` | Yes | Google Doc ID to push the report to (the alphanumeric string from the doc URL) |
| `GOOGLE_CREDENTIALS_FILE` | Yes (K8s) | Path to OAuth2 JSON with `client_id`, `client_secret`, `refresh_token` |
| `GOOGLE_QUOTA_PROJECT` | Yes (K8s) | GCP project with Drive/Docs APIs enabled |
| `MEETING_NAME` | Yes | Google Meet meeting name to search for Gemini notes |

For local development, you can use `gcloud auth login --enable-gdrive-access` instead of the credentials file.

## Running Locally

```shell
export OPENAI_BASE_URL="http://localhost:8000/v1"
export OPENAI_API_KEY="none"
export MODEL_ID="meta-llama/Llama-3.1-8B-Instruct"
export LEADERSHIP_DOC_ID="your-google-doc-id-here"
export MEETING_NAME="Your Meeting Name"

# Install the one dependency
pip install openai

# Run
python3 agent.py
```

## Running as a Container

Build:

```shell
podman build -f Containerfile -t leadership-report-agent .
```

Run:

```shell
podman run --rm \
  -e OPENAI_BASE_URL="http://vllm:8000/v1" \
  -e OPENAI_API_KEY="none" \
  -e MODEL_ID="meta-llama/Llama-3.1-8B-Instruct" \
  -e LEADERSHIP_DOC_ID="your-google-doc-id-here" \
  -e GOOGLE_CREDENTIALS_FILE="/secrets/credentials.json" \
  -e GOOGLE_QUOTA_PROJECT="my-gcp-project" \
  -v /path/to/credentials.json:/secrets/credentials.json:ro \
  leadership-report-agent
```

## Running on OpenShift (via OpenShell)

See the [parent repo README](../README.md) for full OpenShell deployment instructions. This agent runs as a weekly CronJob inside an OpenShell sandbox with vLLM providing inference.

## Report Format

The LLM produces JSON matching this schema:

```json
{
  "date": "Jun 18, 2026",
  "summary": "One paragraph summarizing overall team status.",
  "bullets": [
    {"label": "Category:", "text": " Detail text with attribution. (Person)\n"},
    {"label": "Another:", "text": " More details. (Person, Person)\n"}
  ]
}
```

The push script formats this into the Google Doc with a date header, summary paragraph, and bulleted list with bold category labels.

## Exit Codes

| Code | Meaning | Behavior |
|------|---------|----------|
| 0 | Success | Report published |
| 1 | Usage error | Bad args or invalid input |
| 2 | Permanent failure | Auth invalid, doc missing, bad config |
| 3 | Transient failure | Network/server error — auto-retried once |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `AUTH_FAILED: Refresh token expired` | Re-authenticate and update the K8s secret |
| `FETCH_FAILED: No Google Doc found` | Gemini notes not enabled for this meeting, or wrong `MEETING_NAME` |
| `FETCH_FAILED: Access denied (403)` | Check `GOOGLE_QUOTA_PROJECT` is set and valid |
| `LLM_FAILED: Cannot reach inference endpoint` | Check `OPENAI_BASE_URL` and network connectivity to vLLM |
| `LLM_FAILED: Model returned invalid JSON` | Model may not support JSON mode well — try a larger model |
| `PUSH_FAILED: Could not find insertion point` | The doc's "Purpose" section was removed or changed |

## Standalone Modules

Individual modules can still be run standalone for debugging:

```shell
python3 -m lib.auth                           # Test auth
python3 -m lib.fetch_gdoc /tmp/notes.txt      # Fetch notes only
python3 -m lib.push_to_gdoc /tmp/report.json  # Push a pre-built report
```
