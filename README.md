# DocFlow

DocFlow is a provider‑agnostic toolkit for schema‑driven document extraction. It includes:
- Core engine (schema parsing, validation, provider abstraction, extraction orchestrator)
- Python SDK + CLI for local or remote workflows
- Optional FastAPI service with `/extract` and profile catalog endpoints

Everything is profile‑driven: a profile bundles schema + prompt + system instruction (+ optional model options) so you can reproduce extractions without re‑specifying inputs.

## Quick Start

Install (editable) and run tests:
```bash
pip install -e .
pytest docflow/tests -q
```

Extract locally (CLI):
```bash
# Use your own JSON Schema
docflow extract --schema path/to/schema.json file1.pdf file2.pdf

# Extract everything (built-in profile)
docflow extract --all file1.pdf file2.pdf

# Describe documents (built-in profile)
docflow describe file1.pdf
```

Extract with Python:
```python
from docflow.sdk import DocflowClient

client = DocflowClient(mode="local")  # or remote with endpoint_url
result = client.extract_all(["file1.pdf", "file2.pdf"], multi_mode="per_file")
print(result)
```

## Profiles

Profiles let you standardize extractions. DocFlow ships with built‑ins under `src/docflow/sdk/builtin_profiles/`:
- `extract` – schema‑driven extraction (you provide the schema)
- `extract_all` – broad schema and prompts to pull as much as possible
- `describe` – concise type/summary output

Resolution order when loading by name:
1) Project `.docflow/profiles/` (in working directory)
2) User `~/.docflow/profiles/`
3) Built‑ins packaged with the SDK

Each profile folder contains:
```
prompt.txt
system_instruction.txt
schema.json
[config.yaml]  # optional (e.g., generation_config: { temperature, top_p, max_output_tokens })
```

See `docs/docflow_profile_spec_v1.md` for the full format.

## CLI Overview

The CLI wraps the SDK and supports local or remote modes.

Basic commands:
- `docflow extract --schema schema.json FILE...` – run schema‑driven extraction
- `docflow extract --all FILE...` – run the built‑in `extract_all`
- `docflow describe FILE...` – run `describe`
- `docflow run PROFILE_NAME FILE...` – run any profile by name

Common options:
- `--multi per_file|aggregate|both` – output shape for multiple files
- `--output-format print|json|excel` – default `print` (Excel exports arrays/objects)
- `--mode local|remote` and `--base-url http://host:8080` – use the HTTP service

SDK config file (optional): `~/.docflow/config.toml`
```
[docflow]
mode = "local"           # or "remote"
endpoint = "http://localhost:8080"  # for remote mode
default_output_format = "print"
profile_dir = "~/work/profiles"     # overrides built-ins for local runs
```
Environment overrides: `DOCFLOW_MODE`, `DOCFLOW_ENDPOINT`, `DOCFLOW_PROFILE_DIR`.

## Service (HTTP)

The optional FastAPI app exposes a remote façade useful for pipelines and non‑Python clients.

Run locally:
```bash
pip install -r service/requirements.txt
pip install -e .
uvicorn service.app:app --host 0.0.0.0 --port 8080
```

Primary endpoints:
- `POST /extract` – profile‑first extraction API (supports `single|per_file|grouped` modes, `workers`, `parameters.temperature|top_p|max_output_tokens`, and `repair.max_attempts`)
- `GET /profiles` – list available profiles (with optional `?include_versions=true`, `?prefix=folder/`)
- `GET /profiles/{profile_path}` – resolved profile metadata (accepts base path; resolves latest version)
- `GET /health`

Profiles backend for the service (choose one):
- FS: `DOCFLOW_PROFILES_BACKEND=fs`, `DOCFLOW_PROFILES_ROOT_DIR=/path/to/root`, `DOCFLOW_PROFILES_PREFIX=profiles/`
- GCS: `DOCFLOW_PROFILES_BACKEND=gcs`, `DOCFLOW_PROFILES_BUCKET=bucket-name`, `DOCFLOW_PROFILES_PREFIX=profiles/`

Example request to `/extract` (per‑file):
```bash
curl -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "profile_path": "invoices/extract/v1",
    "mode": "per_file",
    "files": [
      {"uri": "gs://bucket/doc1.pdf"},
      {"uri": "https://example.com/doc2.txt", "display_name": "doc2.txt"}
    ],
    "parameters": {"temperature": 0.0, "top_p": 0.9}
  }'
```

Response envelope:
```
{ ok: true, data: [ { data: {...}, meta: { model, docs, mode, profile } } ], meta: { model } }
```

More details live in `service/README.md`.

## Repository Layout
- `src/docflow/core` – engine, schemas, providers, documents, errors
- `src/docflow/sdk` – client, CLI, profile loader, built‑in profiles
- `src/docflow/profile_catalog` – shared FS/GCS profile catalog for services
- `service/` – optional HTTP façade (FastAPI)
- `docs/` – concepts/specs and user guide
- `tests/` – unit tests

## Development
```bash
pip install -e .
pytest docflow/tests -q
```

CLI help: `docflow --help`. Service docs: `service/README.md`.
