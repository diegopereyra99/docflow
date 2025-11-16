# AI Document Analyzer – Technical Specification

## 1. Purpose

AI Document Analyzer is a generic, reusable service for **document understanding and structured extraction** using LLMs (Gemini or any future model). It is designed to be product-grade and client-agnostic. Scienza is simply one of the consumers of this service.

The system consists of:

1. A **deployable HTTP service** (Cloud Run compatible):
   - Accepts documents + schema + instructions.
   - Runs extraction via Gemini.
   - Returns structured JSON aligned with the schema.

2. A **Python SDK**:
   - Provides a high-level API for calling the service.
   - Includes a profile system (predefined + custom).
   - Handles file uploading (local paths, URLs, bytes).

3. Documentation + examples.

---

## 2. Repository Structure

Suggested repo name: *to be chosen later* (placeholder: `ai-document-analyzer`).

```
ai-document-analyzer/
  service/
    main.py
    requirements.txt
    README.md
  python-sdk/
    document_analyzer/
      __init__.py
      client.py
    pyproject.toml
    README.md
  docs/
    overview.md
    api-http.md
    sdk-python.md
  examples/
    files/
    schemas/
```

### Relationship to existing code

- You will migrate the actual logic from:
  - `scienza-pipeline/functions/extract-data-http`
  - or `ai-extraction-demo/api`
- Scienza will use the **new generic** HTTP endpoint + **the SDK**.
- No more custom extractor logic inside Scienza repos.

---

## 3. HTTP Service Specification

### 3.1 Endpoints

#### `GET /health`
Response:
```json
{"status":"healthy"}
```

#### `POST /extract`
Core extraction endpoint.

##### Request format

```jsonc
{
  "schema": { /* JSON schema */ },

  "files": [
    {
      "gcs_uri": "gs://bucket/file.pdf",
      "url": "https://example.com/doc.pdf",
      "content": "base64-encoded-blob",
      "mime_type": "application/pdf",
      "filename": "invoice_001.pdf"
    }
  ],

  "prompt": "Optional user prompt",
  "system_instruction": "Optional system instruction",

  "extra_params": {
    "language": "es",
    "model": "gemini-1.5-pro",
    "profile_name": "invoice"
  }
}
```

Only one of `gcs_uri`, `url`, or `content` is required per file.

##### Response format

```jsonc
{
  "data": { /* structured output according to schema */ },

  "meta": {
    "model": "gemini-1.5-pro",
    "elapsed_ms": 812,
    "tokens_input": 1500,
    "tokens_output": 300,
    "files_processed": 1,
    "profile_name": "invoice"
  },

  "errors": [
    /* optional list of warnings or partial extraction errors */
  ]
}
```

---

## 4. Service Internal Logic

High-level flow:

1. Validate incoming JSON.
2. Normalize file inputs:
   - fetch from GCS if `gcs_uri`;
   - decode base64 for `content`;
   - (optional) fetch from URL if supported.
3. Build final prompt using:
   - `system_instruction`,
   - `prompt`,
   - internal extraction templates.
4. Call Gemini with the provided `schema` using structured output.
5. Return `data`, `meta`, and any warnings.

### TODO – service implementation

- [ ] Integrate actual Gemini extraction logic.
- [ ] Robust error handling (timeouts, rate limits).
- [ ] Structured logging.
- [ ] Metrics (time, size, tokens).
- [ ] Add authentication (see next section).

---

## 5. Authentication & Security

### Current state

- Early deployments use `--allow-unauthenticated` (not acceptable for production).

### Requirements

The service must support:

- **Secure internal calls** from GCP resources (Cloud Functions, Cloud Run, Agents).
- **Controlled external access** (local dev or client systems).

### Recommended solution

#### Phase 1 (Minimum viable production)

1. **Disable anonymous access**  
   Deploy Cloud Run without `--allow-unauthenticated`.

2. **IAM-based authentication** (GCP internal traffic)
   - Internal components use service accounts.
   - Requests include Identity Tokens automatically.

3. **API key for external clients**
   - Environment variable: `DOCUMENT_ANALYZER_API_KEY`.
   - Incoming requests validated via header:
     ```
     Authorization: Bearer <API_KEY>
     ```

This hybrid model covers all use cases without extra complexity.

#### Phase 2 (optional)
- API Gateway or Cloud Endpoints with JWT / OAuth2.
- Identity-Aware Proxy (IAP) for enterprise-level security.

---

## 6. Python SDK Specification

### 6.1 Configuration

SDK reads config from:

```bash
export DOCUMENT_ANALYZER_URL="https://your-cloud-run-url"
export DOCUMENT_ANALYZER_API_KEY="..."
```

Or passed directly to the constructor.

### 6.2 Public API

```python
from document_analyzer import (
    DocumentAnalyzerClient,
    register_profile,
    extract_with_profile
)
```

#### `DocumentAnalyzerClient`

```python
client = DocumentAnalyzerClient(
    base_url="https://...",
    api_key="optional",
    timeout=120
)
```

#### `client.extract(...)`
Sends normalized files + prompts + schema to the service.

Supported file types:

- Local paths  
- `Path` objects  
- `bytes`  
- URLs (optional support)  

#### Profiles

```python
register_profile(
    name="invoice",
    schema=...,
    default_prompt="...",
    default_system_instruction="..."
)
```

Use:

```python
extract_with_profile(client, "invoice", files=[...])
```

### 6.3 Built‑in default profiles

SDK should pre-register:

#### **1. summarize**
```jsonc
{
  "type": "object",
  "properties": { "summary": { "type": "string" } },
  "required": ["summary"]
}
```

#### **2. describe**
```jsonc
{
  "type": "object",
  "properties": {
    "document_type": { "type": "string" },
    "language": { "type": "string" },
    "short_description": { "type": "string" },
    "topics": {
      "type": "array",
      "items": { "type": "string" }
    }
  },
  "required": ["document_type", "short_description"]
}
```

#### **3. extract_all**
```jsonc
{
  "type": "object",
  "properties": {
    "fields": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": { "type": "string" },
          "value": { "type": "string" },
          "confidence": { "type": "number" }
        },
        "required": ["name", "value"]
      }
    }
  },
  "required": ["fields"]
}
```

---

## 7. Integration with Scienza

Once AI Document Analyzer is ready:

### Step 1 — Deploy service to Cloud Run
- Disable unauthenticated access
- Set environment variables:
  - `DOCUMENT_ANALYZER_API_KEY`
  - Model configuration (optional)

### Step 2 — Install SDK in Scienza
```
pip install -e ~/Desktop/ai/ai-document-analyzer/python-sdk
```

### Step 3 — Register Scienza-specific profiles
- invoice
- packing_list
- coa
- purchase_order

Use them within:

- Scienza pipeline
- Scienza agent

---

## 8. Roadmap

### Service

- [ ] Replace stub code with real Gemini call.
- [ ] Add authentication (IAM + API key).
- [ ] Add logging + metrics.
- [ ] Expand schema validation.
- [ ] Optional: streaming extraction for large docs.

### SDK

- [ ] Add built‑in profiles.
- [ ] Improve error classes.
- [ ] Add support for remote URLs if needed.
- [ ] Validate schemas before sending.
- [ ] Write end‑to‑end tests.

### Documentation

- [ ] Full API docs.
- [ ] Usage tutorials.
- [ ] Examples for invoices, contracts, misc documents.

---

This spec defines the architecture, interfaces, roadmap, and all remaining work required for a clean, reusable, production‑ready AI Document Analyzer system.
