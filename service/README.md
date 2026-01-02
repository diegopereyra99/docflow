# DocFlow Service

Optional HTTP façade exposing DocFlow extraction for Cloud Run or local testing.

## Endpoints
- `POST /extract` – profile-first extraction API (modes: `single`, `per_file`, `grouped`; supports `workers`, `parameters.temperature|top_p|max_output_tokens`, and `repair.max_attempts`)
- `GET /profiles` – list available profile base paths (add `?include_versions=true` to include versions; optional `?prefix=folder/`)
- `GET /profiles/{profile_path}` – resolved profile metadata (accepts base path without version)
- `GET /health` – health check
- `POST /events/{event_name}` – placeholder for Pub/Sub push handlers

## Run locally
```bash
pip install -r service/requirements.txt
pip install -e .
uvicorn service.app:app --host 0.0.0.0 --port 8080
```

Set environment variables as needed:
- `DOCFLOW_DEFAULT_MODEL` (default: gemini-2.5-flash)
- `DOCFLOW_GCP_PROJECT`
- `DOCFLOW_LOCATION`
- `DOCFLOW_PUBSUB_TOPIC_RESULTS`
- `DOCFLOW_PROFILES_BACKEND` (`fs` or `gcs`) – enable profile catalog endpoints
- `DOCFLOW_PROFILES_PREFIX` (default: `profiles/`)
- `DOCFLOW_PROFILES_BUCKET` (for `gcs` backend)
- `DOCFLOW_PROFILES_ROOT_DIR` (for `fs` backend; defaults to CWD)
- `DOCFLOW_CATALOG_CACHE_TTL` (seconds; default 600)
- `DOCFLOW_MAX_WORKERS` (default: 8) and `DOCFLOW_DEFAULT_WORKERS` (default: 4) – apply to `/extract`

## Examples

Per-file extraction:
```bash
curl -s -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "profile_path": "invoices/extract",   
    "mode": "per_file",
    "files": [
      {"uri": "gs://your-bucket/a.pdf"},
      {"uri": "https://example.com/b.txt", "display_name": "b.txt"}
    ],
    "parameters": {"temperature": 0.0, "top_p": 0.9}
  }' | jq .
```

Single (aggregate) extraction:
```bash
curl -s -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "profile_path": "describe",
    "mode": "single",
    "files": [{"uri": "gs://your-bucket/doc.pdf"}],
    "parameters": {"temperature": 0.0}
  }' | jq .
```

Grouped extraction:
```bash
curl -s -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "profile_path": "invoices/extract/v1",
    "mode": "grouped",
    "groups": [
      {"id": "g1", "files": [{"uri": "https://example.com/x"}]},
      {"id": "g2", "files": [{"uri": "gs://your-bucket/y.pdf", "display_name": "y.pdf"}]}
    ],
    "parameters": {"temperature": 0.0}
  }' | jq .
```

Response envelope examples:
- per_file → `data` is a list of `{ data, meta{model, docs, mode, profile} }`
- single → `data` is `{ data, meta{...} }` with `mode=aggregate`
- grouped → `data` is `{ groups: [{ group_id, result: { data, meta{...} } }] }`
