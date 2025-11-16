# AI Document Analyzer Service (HTTP)

HTTP service that exposes `/extract` for structured document extraction and `/health` for health checks.  
It uses Vertex AI Gemini via service account when enabled; otherwise it returns a local stub response
that matches the requested schema. All responses share a unified JSON envelope:

```jsonc
{
  "data": { /* extracted data or null on error */ },
  "meta": {
    "status": "ok" | "error",
    "trace_id": "uuid",
    "model": "gemini-2.5-flash",
    "tokens_input": 123,
    "tokens_output": 45,
    "files_processed": 1,
    "http_status": 400 // only for errors
  },
  "errors": [
    {
      "code": "unsupported_file_type",
      "message": "File type 'application/zip' is not supported",
      "field": "files[0]",
      "details": {
        "mime_type": "application/zip",
        "filename": "archive.zip",
        "supported_mime_types": ["application/pdf", "..."]
      }
    }
  ]
}
```

This folder is self-contained. You can copy it elsewhere and use it independently of the rest of the repo.

## Contents
- `main.py` — Cloud Run/Flask HTTP app (`/extract`, `/health`, `/events`).
- `requirements.txt` — Python dependencies.
- `README.md` — This guide.

## Prerequisites
- Python 3.11+
- Optional for cloud deploy: Google Cloud project with billing enabled and `gcloud` CLI installed and authenticated (`gcloud init` / `gcloud auth application-default login`).

## Environment Variables
- `GOOGLE_GENAI_USE_VERTEXAI` — set to `true` to call Vertex AI; otherwise returns a local stub that matches your schema.
- `GOOGLE_CLOUD_PROJECT` — your GCP project ID (required when using Vertex AI or deploying).
- `GOOGLE_CLOUD_LOCATION` — Vertex AI location (default: `europe-west4`).
- `DEFAULT_GEMINI_MODEL` — model name (default: `gemini-2.5-flash`).
- `MAX_TOTAL_UPLOAD_BYTES` — optional cap for total uploaded bytes (default: ~20MB).
- `PUBSUB_OUTPUT_TOPIC` — optional; if set, downstream events are published to this Pub/Sub topic (accepts short name or full `projects/<id>/topics/<name>` path).
- `BUCKET_NAME` — optional; if set, events adapter persists results at `gs://$BUCKET_NAME/results/<requestId>.json` with idempotency.

Schema validation
- The service parses `schema` and performs lenient structural checks compatible with common JSON Schema/Pydantic constructs (integer/number/string/boolean/null/object/array, union types, $ref, anyOf/oneOf/allOf). The model remains the final authority for validity.

## Supported file types (Gemini)

The `/extract` endpoint enforces a strict whitelist of MIME types. If any file in the request is not
in this list, the entire request fails with:

- HTTP 400
- `errors[0].code = "unsupported_file_type"`
- `errors[0].details.supported_mime_types` listing all allowed types.

Currently allowed MIME types:

- Documents / text:
  - `application/pdf`
  - `text/plain`
  - `text/html`
  - `text/markdown`
- Images:
  - `image/png`
  - `image/jpeg`
  - `image/webp`
  - `image/heic`
  - `image/heif`
- Audio:
  - `audio/wav`
  - `audio/x-wav`
  - `audio/mpeg`
  - `audio/mp3`
  - `audio/ogg`
  - `audio/flac`
- Video:
  - `video/mp4`
  - `video/mpeg`

Examples of rejected types:

- `application/zip`
- `application/vnd.ms-excel`
- `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`

For these, you will see a self-explanatory error with the unsupported MIME and the whitelist.

## Run Locally
1) Copy and edit env file:
  ```bash
  cd extract-data-http
   cp .env.example .env
   # edit .env as needed
   ```

2) Create and activate a virtualenv, then start the local server:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   # Local stub mode (no Vertex calls)
   export GOOGLE_GENAI_USE_VERTEXAI=false
   python -m flask --app service.main run --port=8080
   ```

3) Call the API
   - JSON example (no files):
     ```bash
     curl -s -X POST http://localhost:8080/extract \
       -H 'Content-Type: application/json' \
       -d '{
         "prompt":"Extract basic fields",
         "system_instruction":"Do not make up data; return JSON",
         "schema":"{\"type\":\"OBJECT\",\"properties\":{\"name\":{\"type\":\"STRING\"},\"total\":{\"type\":\"NUMBER\"}},\"required\":[\"name\"]}"
       }' | jq .
     ```
   - JSON with URI-based files (Vertex mode only):
     ```bash
     curl -s -X POST http://localhost:8080/extract \
       -H 'Content-Type: application/json' \
       -d '{
         "prompt":"Extract from URI",
         "schema":"{\"type\":\"OBJECT\",\"properties\":{}}",
         "model":"gemini-2.5-flash",
         "files":[{"uri":"gs://your-bucket/path/file.pdf","mime":"application/pdf"}]
       }' | jq .
     ```
   - Multipart with a file:
     ```bash
     curl -s -X POST http://localhost:8080/extract \
       -H 'Content-Type: multipart/form-data' \
       -F 'prompt=Extract fields from file' \
       -F 'schema={"type":"OBJECT","properties":{"name":{"type":"STRING"}},"required":["name"]}' \
       -F 'files[]=@./README.md;type=text/markdown' | jq .
     ```

## Enable Vertex AI (Optional)
Set these before starting the server to call real models via Vertex AI:
```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=europe-west4
export GOOGLE_GENAI_USE_VERTEXAI=true
```

If you run locally with Vertex AI, your ADC credentials must authorize access to Vertex AI in the project. Run `gcloud auth application-default login` if needed.

## Service Account Setup (Recommended for Cloud Run/Functions)
Create a dedicated service account and grant the minimal roles to call Vertex AI.

1) Set project and enable required APIs:
   ```bash
   gcloud config set project $GOOGLE_CLOUD_PROJECT
   gcloud services enable \
     aiplatform.googleapis.com \
     cloudfunctions.googleapis.com \
     run.googleapis.com \
     eventarc.googleapis.com \
     logging.googleapis.com
   ```

2) Create the service account:
   ```bash
   SA_ID=gemini-extractor-sa
   SA_EMAIL="$SA_ID@$GOOGLE_CLOUD_PROJECT.iam.gserviceaccount.com"
   gcloud iam service-accounts create "$SA_ID" \
     --description="Service account for extract data HTTP API" \
     --display-name="Gemini Extractor SA"
   ```

3) Grant roles needed for Vertex AI and logging:
   ```bash
   gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
     --member="serviceAccount:$SA_EMAIL" \
     --role="roles/aiplatform.user"

   gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
     --member="serviceAccount:$SA_EMAIL" \
     --role="roles/logging.logWriter"
   ```

4) (Optional) Allow public invocation of the deployed HTTP endpoint. You can skip this if you plan to keep it private and use IAM/OIDC instead:
   ```bash
   gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
     --member="allUsers" \
     --role="roles/run.invoker"
   ```

## Deploy as Cloud Function (Gen2)
Run from the repo root and use your existing env values (from `functions/extract-data-http/.env`):

```bash
# Variables (taken from your env)
PROJECT_ID="$GOOGLE_CLOUD_PROJECT"
REGION="$GOOGLE_CLOUD_LOCATION"
SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud functions deploy extract-data \
  --gen2 \
  --region="$REGION" \
  --runtime=python311 \
  --entry-point=extract_data \
  --source=functions/extract-data-http \
  --trigger-http \
  --allow-unauthenticated \
  --service-account="$SA_EMAIL" \
  --memory=512MiB \
  --timeout=60s \
  --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-false},GOOGLE_CLOUD_LOCATION=$REGION,DEFAULT_GEMINI_MODEL=${DEFAULT_GEMINI_MODEL:-gemini-2.5-flash},MAX_TOTAL_UPLOAD_BYTES=${MAX_TOTAL_UPLOAD_BYTES:-20971520},PUBSUB_OUTPUT_TOPIC=${PUBSUB_OUTPUT_TOPIC:-},BUCKET_NAME=${BUCKET_NAME:-}

# Increase `--memory` (e.g., `1Gi`) or `--timeout` (e.g., `300s`) if needed.

URL=$(gcloud functions describe extract-data --region="$REGION" --gen2 --format='value(serviceConfig.uri)')
curl -s -X POST "$URL/extract-data" \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt":"Extract basic fields",
    "schema":"{\"type\":\"OBJECT\",\"properties\":{\"name\":{\"type\":\"STRING\"}},\"required\":[\"name\"]}"
  }' | jq .
```

## Deploy to Cloud Run
You can deploy the same HTTP endpoint as a standalone Cloud Run service. Commands are intended to be run from the repo root and use your existing env vars.

Variables used (from your env)
- `PROJECT_ID="$GOOGLE_CLOUD_PROJECT"`
- `REGION="$GOOGLE_CLOUD_LOCATION"`
- `SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"`

Option A — Source deploy (no image flag)
```bash
PROJECT_ID="$GOOGLE_CLOUD_PROJECT"
REGION="$GOOGLE_CLOUD_LOCATION"
SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run deploy extract-data-http \
  --region "$REGION" \
  --source functions/extract-data-http \
  --allow-unauthenticated \
  --service-account "$SA_EMAIL" \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-false},GOOGLE_CLOUD_LOCATION=$REGION,DEFAULT_GEMINI_MODEL=${DEFAULT_GEMINI_MODEL:-gemini-2.5-flash},MAX_TOTAL_UPLOAD_BYTES=${MAX_TOTAL_UPLOAD_BYTES:-20971520},PUBSUB_OUTPUT_TOPIC=${PUBSUB_OUTPUT_TOPIC:-},BUCKET_NAME=${BUCKET_NAME:-}
```

Option B — Image deploy (explicit image)
```bash
PROJECT_ID="$GOOGLE_CLOUD_PROJECT"
REGION="$GOOGLE_CLOUD_LOCATION"
SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="gcr.io/$PROJECT_ID/extract-data-http"

# Build container image from source
gcloud builds submit --tag "$IMAGE" functions/extract-data-http

# Deploy the built image
gcloud run deploy extract-data-http \
  --region "$REGION" \
  --image "$IMAGE" \
  --allow-unauthenticated \
  --service-account "$SA_EMAIL" \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-false},GOOGLE_CLOUD_LOCATION=$REGION,DEFAULT_GEMINI_MODEL=${DEFAULT_GEMINI_MODEL:-gemini-2.5-flash},MAX_TOTAL_UPLOAD_BYTES=${MAX_TOTAL_UPLOAD_BYTES:-20971520},PUBSUB_OUTPUT_TOPIC=${PUBSUB_OUTPUT_TOPIC:-},BUCKET_NAME=${BUCKET_NAME:-}

# Alternative Artifact Registry image (uncomment to use):
# IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/default/extract-data-http:latest"
# gcloud builds submit --tag "$IMAGE" functions/extract-data-http
# gcloud run deploy extract-data-http --region "$REGION" --image "$IMAGE" ...
```

3) Invoke the service:
   ```bash
   URL=$(gcloud run services describe extract-data-http --region "$REGION" --format 'value(status.url)')
   curl -s -X POST "$URL/extract-data" \
     -H 'Content-Type: application/json' \
     -d '{
       "prompt":"Extract basic fields",
       "schema":"{\"type\":\"OBJECT\",\"properties\":{\"name\":{\"type\":\"STRING\"}},\"required\":[\"name\"]}"
     }' | jq .
   ```

Notes:
- The Flask app routes both `/extract-data` and `/` to the same handler to preserve the original behavior.
- Set `GOOGLE_GENAI_USE_VERTEXAI=true` and `GOOGLE_CLOUD_PROJECT` on the service to call real models via Vertex AI.

## Events Router (Pub/Sub Push)
The service exposes a generic events router at `/events/<event_name>` that accepts Pub/Sub push messages. Currently supported handler:
- `extractions.request` → validates payload, executes extraction, persists results idempotently to GCS (if `BUCKET_NAME` is set), and emits `extractions.ready` to `replyTo` if provided or to `PUBSUB_OUTPUT_TOPIC` if set.

Transport (Pub/Sub push wrapper):
```json
{
  "message": {
    "data": "<base64-encoded envelope>",
    "attributes": {}
  },
  "subscription": "<subscription name>"
}
```

Envelope v1 (base64-decoded JSON in `message.data`):
```json
{
  "version": "1",
  "event": "extractions.request",
  "requestId": "<optional idempotency key>",
  "source": "<optional>",
  "replyTo": "projects/<id>/topics/<name>",
  "meta": { "messageKey": "<optional>" },
  "payload": {
    "prompt": "<string>",
    "schema": { "type": "OBJECT", "properties": {} } | "gs://bucket/path/to/schema.json" | {"$ref":"gs://bucket/path/to/schema.json"},
    "system_instruction": "<string>",
    "model": "<string>",
    "files": [ { "uri": "gs://..." } | { "signedUrl": "https://..." } ]
  }
}
```

Validation and behavior:
- `event` must match the path variable.
- `payload.schema` must be a JSON object or a `gs://` reference (string or `{ "$ref": "gs://..." }`). The schema is loaded from GCS if referenced and validated using the same validator as `/extract-data`.
- `payload.files` rules:
  - If omitted or empty → the request proceeds with a warning (no files provided).
  - If present → must be a list; each item must be an object with `uri` or `signedUrl`.
- Idempotency key = `requestId` if provided, else `meta.messageKey`, else a generated UUID.
- Results are written create-only to `gs://$BUCKET_NAME/results/<requestId>.json` when `BUCKET_NAME` is set. If the object already exists, processing is skipped and a ready event is re-emitted.
- Publish target = `replyTo` if present; otherwise `PUBSUB_OUTPUT_TOPIC` (if set). If neither is set, no publish occurs.
- Pub/Sub attributes on publish: `trace_id`, `source` (e.g., `events/extractions.request`), `eventName` (`extractions.ready`), `subscription`, and `messageKey` (from `meta.messageKey` when present).

Ready envelope (payload) published downstream:
```json
{
  "version": "1",
  "event": "extractions.ready",
  "requestId": "<same idempotency key>",
  "source": "<propagated>",
  "replyTo": null,
  "meta": { /* propagated unmodified */ },
  "payload": { "status": "ok", "resultUri": "gs://<bucket>/results/<requestId>.json" }
}
```

Downstream compatibility (Smartsheet upserter)
- If you plan to feed the Smartsheet upserter, shape your extraction schema so the result JSON contains rows at one of these paths:
  - `data.items` (array of row objects) — recommended
  - `items` (array)
  - Or a single object at `data`/top-level
- Each row can be a plain object; the upserter will flatten nested fields using dot notation and derive a unique key per row.

Manual test (build a push body and post it):
```bash
SERVICE_URL=https://<your-service-url>
DATA=$(python3 - <<'PY'
import json,base64,pathlib
schema = pathlib.Path('examples/invoice.json').read_text()
env = {
  "version": "1",
  "event": "extractions.request",
  "requestId": "example-req-1",
  "source": "manual-test",
  "meta": {"messageKey":"examples-key-1"},
  "payload": {
    "prompt": "Extract from examples",
    "schema": json.loads(schema),
    "system_instruction": "",
    "model": "gemini-2.5-flash",
    "files": []
  }
}
print(base64.b64encode(json.dumps(env).encode()).decode())
PY
)
curl -sS -X POST -H 'Content-Type: application/json' "$SERVICE_URL/events/extractions.request" \
  -d "{\"message\": {\"data\": \"$DATA\", \"attributes\": {}}, \"subscription\": \"manual-test\"}"
```

## API Contract
- Method: `POST /extract-data` (also supports `OPTIONS` for CORS preflight)
- Content types:
  - `application/json` with fields: `prompt` (string), `schema` (stringified JSON schema), optional `system_instruction`, optional `model`.
  - `multipart/form-data` with fields: `prompt` (string), `schema` (JSON string), and one or more `files[]` parts.
- Response: JSON object `{ ok, model, data, usage, trace_id, error }` where `data` adheres to your provided schema. In local stub mode, values are minimal placeholders (objects with nested nulls, arrays as [], primitives as null).

## Notes
- CORS is permissive for demo (`*`). Restrict for production.
- To use different regions/models, adjust `GOOGLE_CLOUD_LOCATION` and `DEFAULT_GEMINI_MODEL`.
- For larger files or many pages, consider moving to GCS-based ingestion; this demo limits total upload size via `MAX_TOTAL_UPLOAD_BYTES`.
