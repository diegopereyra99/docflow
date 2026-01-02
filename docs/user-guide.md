# DocFlow User Guide

This guide focuses on how to use DocFlow as a developer or operator: the CLI, Python SDK, and the HTTP service.

## Install
```bash
pip install -e docflow
```

Optional: service dependencies for running the FastAPI app
```bash
pip install -r docflow/service/requirements.txt
```

## Profiles

Profiles are folders with at least:
```
prompt.txt
system_instruction.txt
schema.json
[config.yaml]  # optional generation config
```

Lookup order when loading profiles by name:
1) Project `.docflow/profiles` under the current working directory
2) User `~/.docflow/profiles` under your home directory
3) Built‑in profiles packaged with the SDK (`docflow.sdk.builtin_profiles`)

You can reference a specific version in the name (e.g., `invoices/extract/v2`). When omitted, the latest is selected.

## CLI

Common commands:
- `docflow extract --schema schema.json FILE...`
- `docflow extract --all FILE...`
- `docflow describe FILE...`
- `docflow run PROFILE_NAME FILE...`

Flags:
- `--multi per_file|aggregate|both`
- `--output-format print|json|excel` and `--output-path path.xlsx`
- `--mode local|remote` and `--base-url http://localhost:8080`

Config file: `~/.docflow/config.toml`
```
[docflow]
mode = "local"
endpoint = "http://localhost:8080"
default_output_format = "print"
profile_dir = "~/work/profiles"
```

Environment overrides: `DOCFLOW_MODE`, `DOCFLOW_ENDPOINT`, `DOCFLOW_PROFILE_DIR`.

## Python SDK

```python
from docflow.sdk import DocflowClient

client = DocflowClient(mode="local")

# Built‑in profiles
client.extract_all(["file.pdf"])              # extract everything
client.describe(["file.pdf"])                 # describe content
client.run_profile("describe", ["file.pdf"])  # run any profile by name

# Custom schema
schema = {"type":"object","properties":{"total":{"type":"number"}}}
client.extract(schema, ["invoice1.pdf","invoice2.pdf"], multi_mode="per_file")
```

Remote mode uses the HTTP service under the hood; set `mode="remote"` and pass an `endpoint_url`.

## HTTP Service

Start the service locally:
```bash
pip install -r docflow/service/requirements.txt
pip install -e docflow
uvicorn docflow/service/app.py:app --host 0.0.0.0 --port 8080
```

Enable profile catalog:
- FS backend: set `DOCFLOW_PROFILES_BACKEND=fs`, `DOCFLOW_PROFILES_ROOT_DIR=/abs/path`, `DOCFLOW_PROFILES_PREFIX=profiles/`
- GCS backend: set `DOCFLOW_PROFILES_BACKEND=gcs`, `DOCFLOW_PROFILES_BUCKET=bucket-name`, `DOCFLOW_PROFILES_PREFIX=profiles/`

Endpoints:
- `POST /extract`
- `GET /profiles` and `GET /profiles/{profile_path}`
- `GET /health`

Example request:
```bash
curl -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "profile_path": "invoices/extract/v1",
    "mode": "per_file",
    "files": [{"uri": "gs://your-bucket/a.pdf"}],
    "parameters": {"temperature": 0.0, "top_p": 0.9}
  }'
```

Responses always follow the DocFlow envelope:
```
{ ok: true, data: <result>, meta: { model } }
```

Where `<result>` is:
- per_file: list of `{ data, meta{ model, docs, mode, profile } }`
- single: a single `{ data, meta{...} }` with `mode=aggregate`
- grouped: `{ groups: [{ group_id, result: { data, meta{...} } }] }`

## Notes
- Provider defaults (Gemini): model `gemini-2.5-flash`, temperature `0.0`, `top_p` unset, `max_output_tokens` unset.
- Service envs: `DOCFLOW_DEFAULT_MODEL`, `DOCFLOW_LOCATION`, `DOCFLOW_MAX_WORKERS`, `DOCFLOW_DEFAULT_WORKERS`.
- Profiles can include `config.yaml` with a `generation_config` block to set defaults per-profile.
