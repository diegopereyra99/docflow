# DocFlow Service

Optional HTTP façade exposing DocFlow extraction for Cloud Run or local testing.

## Endpoints
- `POST /extract-data` – run an extraction using a provided schema or profile
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
