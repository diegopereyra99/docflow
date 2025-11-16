import os
import io
import json
import uuid
import base64
from typing import Any, Dict, List, Tuple

from flask import Response, Flask, request as flask_request


def _load_dotenv_local() -> None:
    """Load .env from this folder for local runs (no external deps).
    Does not overwrite existing environment variables.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(here, ".env")
        if not os.path.isfile(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv_local()

# Vertex AI imports are optional at local dev time
_VERTEX_AVAILABLE = False
_VERTEX_IMPORT_ERROR: str | None = None
_VERTEX_IMPORT_PATH: str | None = None
try:
    from vertexai.generative_models import (  # type: ignore
        GenerativeModel,
        GenerationConfig,
        Part,
    )
    _VERTEX_AVAILABLE = True
    _VERTEX_IMPORT_PATH = "vertexai.generative_models"
except Exception as e:  # pragma: no cover
    _VERTEX_AVAILABLE = False
    _VERTEX_IMPORT_ERROR = str(e)

DEFAULT_MODEL = os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west4")
USE_VERTEX = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in {"1", "true", "yes"}
OUTPUT_TOPIC = os.environ.get("PUBSUB_OUTPUT_TOPIC", "")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")

# Gemini (Vertex AI) supported MIME types (text, images, docs, audio, video).
# This list is based on public Vertex AI Gemini documentation and can be
# expanded if Google adds more types.
SUPPORTED_MIME_TYPES = {
    # Documents / text
    "application/pdf",
    "text/plain",
    "text/html",
    "text/markdown",
    # Images
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
    # Audio
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/flac",
    # Video
    "video/mp4",
    "video/mpeg",
}

# ~20 MB total payload limit for demo
MAX_TOTAL_UPLOAD_BYTES = int(os.environ.get("MAX_TOTAL_UPLOAD_BYTES", str(20 * 1024 * 1024)))

print(f"Vertex AI available: {_VERTEX_AVAILABLE}")
print(f"Using Vertex AI: {USE_VERTEX}")
print(f"Max total upload bytes: {MAX_TOTAL_UPLOAD_BYTES}")
if USE_VERTEX and not _VERTEX_AVAILABLE:
    try:
        import sys
        print(
            f"WARN: GOOGLE_GENAI_USE_VERTEXAI=true but Vertex SDK import failed: "
            f"{_VERTEX_IMPORT_ERROR or 'unknown error'}. Python={sys.version.split()[0]} Path={sys.executable}",
            file=sys.stderr,
        )
    except Exception:
        pass
else:
    if _VERTEX_IMPORT_PATH:
        print(f"Vertex imports from: {_VERTEX_IMPORT_PATH}")


def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def _error_response(
    trace_id: str,
    http_status: int,
    code: str,
    message: str,
    field: str | None = None,
    details: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    error_obj: Dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if field is not None:
        error_obj["field"] = field
    if details is not None:
        error_obj["details"] = details

    body = {
        "data": None,
        "meta": {
            "status": "error",
            "trace_id": trace_id,
            "http_status": http_status,
        },
        "errors": [error_obj],
    }
    headers = _cors_headers()
    headers["Content-Type"] = "application/json"
    return body, http_status, headers


def _bad_request(
    message: str,
    trace_id: str,
    code: str = "bad_request",
    field: str | None = None,
    details: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    return _error_response(trace_id, 400, code, message, field, details)


def _too_large(message: str, trace_id: str) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    # Use a specific code for payload/file size issues.
    return _error_response(trace_id, 413, "file_too_large", message)


def _server_error(
    message: str,
    trace_id: str,
    code: str = "internal_error",
) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    return _error_response(trace_id, 500, code, message)

    headers = _cors_headers()
    headers["Content-Type"] = "application/json"
    return body, 413, headers


def _server_error(message: str, trace_id: str) -> Tuple[Dict[str, Any], int, Dict[str, str]]:
    body = {
        "ok": False,
        "model": None,
        "data": None,
        "usage": None,
        "trace_id": trace_id,
        "error": message,
    }
    headers = _cors_headers()
    headers["Content-Type"] = "application/json"
    return body, 500, headers


def _parse_schema(schema_str: str) -> Dict[str, Any]:
    """Parse and leniently validate a response schema.

    Accept common JSON Schema/Pydantic constructs and only sanity-check
    structure. The model remains the final authority for validity.
    """
    try:
        schema = json.loads(schema_str)
    except Exception as e:  # invalid JSON
        raise ValueError(f"Invalid schema JSON: {e}")

    if not isinstance(schema, dict):
        raise ValueError("Schema must be a JSON object")

    def _validate_lenient(node: Any, path: str = "$") -> None:
        if not isinstance(node, dict):
            return
        # Respect $ref (do not deep-validate referenced nodes)
        if "$ref" in node and isinstance(node["$ref"], str):
            return
        t = node.get("type")
        # Accept string, list (union), or missing
        if t is not None and not isinstance(t, (str, list)):
            raise ValueError(f"Invalid 'type' at {path}: expected string or list")
        # Objects: properties optional and should be a dict if present
        if (isinstance(t, str) and t.lower() == "object") or ("properties" in node):
            props = node.get("properties", {})
            if props is not None and not isinstance(props, dict):
                raise ValueError(f"'properties' at {path} must be an object if present")
            for key, sub in (props or {}).items():
                if isinstance(sub, dict):
                    _validate_lenient(sub, f"{path}.properties.{key}")
        # Arrays: items may be dict or list (tuple validation)
        if (isinstance(t, str) and t.lower() == "array") or ("items" in node):
            items = node.get("items")
            if isinstance(items, dict):
                _validate_lenient(items, f"{path}.items")
            elif isinstance(items, list):
                for i, sub in enumerate(items):
                    if isinstance(sub, dict):
                        _validate_lenient(sub, f"{path}.items[{i}]")
        # Recurse common schema-combining keywords, but do not enforce content
        for kw in ("allOf", "anyOf", "oneOf"):
            if isinstance(node.get(kw), list):
                for i, sub in enumerate(node[kw]):
                    if isinstance(sub, dict):
                        _validate_lenient(sub, f"{path}.{kw}[{i}]")

    _validate_lenient(schema)
    return schema


def _collect_files_from_multipart(request) -> Tuple[List[Tuple[str, bytes, str]], int]:
    files: List[Tuple[str, bytes, str]] = []
    file_uris: List[Tuple[str, str]] = []
    total = 0
    for f in request.files.getlist("files[]"):
        data = f.read()
        f.seek(0)
        total += len(data)
        files.append((f.filename or "file", data, f.mimetype or "application/octet-stream"))
    return files, total


def _parts_from_files(files: List[Tuple[str, bytes, str]]) -> List["Part"]:
    parts: List[Part] = []
    for name, data, mime in files:
        try:
            parts.append(Part.from_data(mime_type=mime, data=data))
        except Exception:
            parts.append(Part.from_data(mime_type="application/octet-stream", data=data))
    return parts


def _execute_extraction(
    prompt: str,
    system_instruction: str,
    model_name: str,
    schema_str: str,
    files: List[Dict[str, Any]],
    trace_id: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    """Core extraction logic independent from Flask.

    Returns (data, usage, model_used).
    """
    if not schema_str:
        raise ValueError("Missing 'schema'")

    try:
        schema_dict = _parse_schema(schema_str)
    except ValueError as e:
        raise ValueError(str(e))

    if _VERTEX_AVAILABLE and USE_VERTEX:
        data, usage = _maybe_call_vertex(prompt, system_instruction, schema_dict, files, model_name, trace_id)
        return data, usage, model_name
    else:
        data = _stub_generate(schema_dict)
        usage: Dict[str, Any] = {"note": "local stub; set GOOGLE_GENAI_USE_VERTEXAI=true to call Vertex"}
        if USE_VERTEX and not _VERTEX_AVAILABLE and _VERTEX_IMPORT_ERROR:
            usage["vertex_warning"] = f"Vertex not available: {_VERTEX_IMPORT_ERROR}"
        return data, usage, model_name


def _maybe_call_vertex(prompt: str, system_instruction: str, schema_dict: Dict[str, Any],
                       files: List[Dict[str, Any]],
                       model_name: str, trace_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not _VERTEX_AVAILABLE or not USE_VERTEX:
        raise RuntimeError("Vertex not available or disabled")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION)
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")

    import vertexai
    vertexai.init(project=project, location=location)
    model = GenerativeModel(model_name)

    contents: List[Any] = []
    if system_instruction:
        contents.append(system_instruction)
    if prompt:
        contents.append(prompt)
    # Build parts from unified files list
    for f in files or []:
        try:
            if isinstance(f, dict):
                mime = f.get("mime") or "application/octet-stream"
                if "data" in f and isinstance(f.get("data"), (bytes, bytearray)):
                    contents.append(Part.from_data(mime_type=mime, data=f["data"]))
                else:
                    uri = f.get("uri") or f.get("signedUrl")
                    if isinstance(uri, str) and uri:
                        contents.append(Part.from_uri(uri=uri, mime_type=mime))
        except Exception:
            # Skip invalid part
            continue

    cfg = GenerationConfig(
        response_mime_type="application/json",
        response_schema=schema_dict,
    )

    resp = model.generate_content(contents, generation_config=cfg)

    text = getattr(resp, "text", None)
    if not text:
        try:
            text = resp.candidates[0].content.parts[0].text  # type: ignore[attr-defined]
        except Exception:
            raise RuntimeError("No JSON response from model")

    data = json.loads(text)

    usage = {}
    try:
        um = getattr(resp, "usage_metadata", None)
        if um:
            input_tok = getattr(um, "prompt_token_count", None)
            output_tok = getattr(um, "candidates_token_count", None)
            usage = {"input_tokens": input_tok, "output_tokens": output_tok}
    except Exception:
        pass

    return data, usage


def _stub_generate(schema_dict: Dict[str, Any]) -> Any:
    """Generate a minimal stub matching the provided schema.

    - OBJECT → dict with keys from properties; values are stubs
    - ARRAY → empty list []
    - STRING/NUMBER/BOOLEAN/NULL → None
    """
    def _gen(node: Dict[str, Any]) -> Any:
        t = (node.get("type") or "STRING")
        t_upper = t.upper() if isinstance(t, str) else "STRING"
        if t_upper == "OBJECT":
            out: Dict[str, Any] = {}
            props = node.get("properties", {}) or {}
            if isinstance(props, dict):
                for k, v in props.items():
                    if isinstance(v, dict):
                        out[k] = _gen(v)
                    else:
                        out[k] = None
            return out
        if t_upper == "ARRAY":
            # Minimal stub: empty array
            return []
        # Primitives
        return None

    return _gen(schema_dict)


def extract_data(request):
    trace_id = str(uuid.uuid4())

    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())

    if request.method != "POST":
        body, status, headers = _bad_request(
            "Only POST is allowed",
            trace_id,
            code="method_not_allowed",
        )
        return Response(response=json.dumps(body), status=status, headers=headers)

    content_type = request.headers.get("Content-Type", "")
    prompt = ""
    system_instruction = request.form.get(
        "system_instruction",
        "Do not make up data. Use null if information is missing. Respond strictly matching the provided schema.",
    )
    model_name = request.form.get("model", DEFAULT_MODEL)
    schema_str = request.form.get("schema")
    files_unified: List[Dict[str, Any]] = []
    total_bytes = 0

    if content_type.startswith("multipart/form-data"):
        prompt = request.form.get("prompt", "")
        schema_str = request.form.get("schema")
        files, total_bytes = _collect_files_from_multipart(request)
        for name, data, mime in files:
            files_unified.append({"name": name, "data": data, "mime": mime})
    else:
        try:
            payload = request.get_json(force=True, silent=False)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            body, status, headers = _bad_request(
                "Invalid JSON body",
                trace_id,
                code="invalid_json",
            )
            return Response(response=json.dumps(body), status=status, headers=headers)
        prompt = payload.get("prompt", "")
        schema_str = payload.get("schema")
        system_instruction = payload.get(
            "system_instruction",
            "Do not make up data. Use null if information is missing. Respond strictly matching the provided schema.",
        )
        model_name = payload.get("model", DEFAULT_MODEL)
        # Optional: accept URI-based files in JSON body
        body_files = payload.get("files")
        if isinstance(body_files, list):
            for item in body_files:
                if isinstance(item, dict):
                    uri = item.get("uri") or item.get("signedUrl")
                    mime = item.get("mime") or "application/octet-stream"
                    name = item.get("name") or "file"
                    if isinstance(uri, str) and uri:
                        files_unified.append({"name": name, "uri": uri, "mime": mime})

    if not schema_str:
        body, status, headers = _bad_request(
            "Missing 'schema'",
            trace_id,
            code="missing_field",
            field="schema",
        )
        return Response(response=json.dumps(body), status=status, headers=headers)

    if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
        body, status, headers = _too_large(
            "Payload too large for demo; consider using GCS instead of direct upload.",
            trace_id,
        )
        return Response(response=json.dumps(body), status=status, headers=headers)

    # Enforce supported MIME types: any unsupported file makes the whole request fail.
    if files_unified:
        for idx, f in enumerate(files_unified):
            mime = (f.get("mime") or "").strip()
            name = f.get("name") or ""
            if not mime or mime not in SUPPORTED_MIME_TYPES:
                details = {
                    "mime_type": mime or "unknown",
                    "filename": name,
                    "supported_mime_types": sorted(SUPPORTED_MIME_TYPES),
                }
                body, status, headers = _bad_request(
                    f"File type '{mime or 'unknown'}' is not supported",
                    trace_id,
                    code="unsupported_file_type",
                    field=f"files[{idx}]",
                    details=details,
                )
                return Response(response=json.dumps(body), status=status, headers=headers)

    try:
        data, usage, model_used = _execute_extraction(
            prompt=prompt,
            system_instruction=system_instruction,
            model_name=model_name,
            schema_str=schema_str,
            files=files_unified,
            trace_id=trace_id,
        )
    except Exception as e:
        body, status, headers = _server_error(
            f"Model call failed: {e}",
            trace_id,
            code="model_error",
        )
        return Response(response=json.dumps(body), status=status, headers=headers)

    # Build spec-shaped response while preserving internal fields
    meta: Dict[str, Any] = {
        "model": model_used,
        "trace_id": trace_id,
        "status": "ok",
    }
    if isinstance(usage, dict):
        if "input_tokens" in usage:
            meta["tokens_input"] = usage.get("input_tokens")
        if "output_tokens" in usage:
            meta["tokens_output"] = usage.get("output_tokens")
        # Preserve raw usage for debugging/compatibility
        meta["usage_raw"] = usage

    meta["files_processed"] = len(files_unified)

    response_body = {
        "data": data,
        "meta": meta,
        "errors": [],
    }
    headers = _cors_headers()
    headers["Content-Type"] = "application/json"
    return Response(response=json.dumps(response_body), status=200, headers=headers)


# --- Cloud Run app wrapper ---
# Expose a Flask app so this module can run as a Cloud Run service
# while preserving the same HTTP behavior and path.
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Response:
    body = {"status": "healthy"}
    headers = _cors_headers()
    headers["Content-Type"] = "application/json"
    return Response(response=json.dumps(body), status=200, headers=headers)


@app.route("/extract", methods=["POST", "OPTIONS"])
def extract_route():
    return extract_data(flask_request)


@app.route("/extract-data", methods=["POST", "OPTIONS"])
@app.route("/", methods=["POST", "OPTIONS"])  # also accept root for convenience
def _extract_data_route():
    return extract_data(flask_request)


# ---------------------------
# Events: Pub/Sub push router
# ---------------------------

def _decode_pubsub_push(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str], str]:
    """Decode a Pub/Sub push request payload.

    Returns: (envelope_json, attributes, subscription)
    """
    if not isinstance(body, dict):
        raise ValueError("Body must be a JSON object")

    msg = body.get("message")
    if not isinstance(msg, dict):
        raise ValueError("Missing 'message' in Pub/Sub push body")

    data_b64 = msg.get("data")
    if not isinstance(data_b64, str) or not data_b64:
        raise ValueError("Missing 'message.data' in Pub/Sub push body")

    try:
        decoded = base64.b64decode(data_b64)
        envelope = json.loads(decoded.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to decode message.data: {e}")

    attributes: Dict[str, str] = {}
    raw_attrs = msg.get("attributes", {})
    if isinstance(raw_attrs, dict):
        for k, v in raw_attrs.items():
            if isinstance(k, str) and isinstance(v, str):
                attributes[k] = v

    subscription = body.get("subscription") or ""
    subscription = str(subscription)
    return envelope, attributes, subscription


def _validate_envelope(envelope: Dict[str, Any], event_name: str) -> Dict[str, Any]:
    """Validate Envelope v1 and normalize optional fields.

    Expected shape:
    {
      version: "1",
      event: <matches path>,
      requestId?: <string>,
      source?: <string>,
      replyTo?: <string Pub/Sub topic>,
      meta?: <object>,
      payload?: <object>
    }
    """
    if not isinstance(envelope, dict):
        raise ValueError("Envelope must be an object")

    version = envelope.get("version")
    if version != "1":
        raise ValueError("Envelope 'version' must be '1'")

    ev = envelope.get("event")
    if not isinstance(ev, str) or not ev:
        raise ValueError("Envelope 'event' must be a non-empty string")
    if ev != event_name:
        raise ValueError("Path event does not match envelope.event")

    # Normalize optional
    meta = envelope.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("Envelope 'meta' must be an object if provided")
    
    payload = envelope.get("payload")
    if payload is None:
        raise ValueError("Envelope 'payload' must not be empty")
    
    if not isinstance(payload, dict):
        raise ValueError("Envelope 'payload' must be an object if provided")

    envelope["meta"] = meta
    envelope["payload"] = payload
    # requestId normalization deferred to handler (uses provided or derives)
    return envelope


def _publish_to_pubsub(topic: str, data: Dict[str, Any], attrs: Dict[str, str]) -> Tuple[bool, str]:
    """Publish a message to Pub/Sub. Returns (ok, message_id_or_error)."""
    try:
        from google.cloud import pubsub_v1  # type: ignore
    except Exception as e:
        return False, f"Pub/Sub client import failed: {e}"

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not topic:
        return False, "Output topic not configured"

    if "/" in topic or topic.startswith("projects/"):
        topic_path = topic
    else:
        if not project:
            return False, "GOOGLE_CLOUD_PROJECT is not set for short topic name"
        topic_path = f"projects/{project}/topics/{topic}"

    try:
        publisher = pubsub_v1.PublisherClient()
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        future = publisher.publish(topic_path, payload, **attrs)
        msg_id = future.result(timeout=10)
        return True, str(msg_id)
    except Exception as e:
        return False, f"Publish failed: {e}"


def _gcs_client():
    try:
        from google.cloud import storage  # type: ignore
        return storage.Client()
    except Exception as e:
        raise RuntimeError(f"GCS client import failed: {e}")


def _gcs_write_json_create_only(bucket: str, path: str, obj: Dict[str, Any]) -> Tuple[bool, str | None]:
    if not bucket:
        return False, "BUCKET_NAME is not set"
    client = _gcs_client()
    b = client.bucket(bucket)
    blob = b.blob(path)
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
        return True, None
    except Exception as e:
        # If already exists, creation precondition fails
        return False, str(e)


def _gcs_blob_exists(bucket: str, path: str) -> bool:
    if not bucket:
        return False
    client = _gcs_client()
    b = client.bucket(bucket)
    blob = b.blob(path)
    try:
        return blob.exists()
    except Exception:
        return False


def _handle_extractions_request(trace_id: str, envelope: Dict[str, Any], attributes: Dict[str, str], subscription: str) -> Dict[str, Any]:
    meta = envelope.get("meta", {})
    payload = envelope.get("payload", {})
    request_id = envelope.get("requestId") or meta.get("messageKey") or str(uuid.uuid4())
    model = payload.get("model") or DEFAULT_MODEL
    prompt = payload.get("prompt", "")
    system_instruction = payload.get("system_instruction", "")
    schema_val = payload.get("schema")

    warnings: List[str] = []
    files_val = payload.get("files", None)
    # No files: warn but proceed
    if files_val is None or (isinstance(files_val, list) and len(files_val) == 0):
        warnings.append("no files provided in payload.files; continuing")
        files = []
    else:
        # If provided, must be a list of objects with uri or signedUrl
        if not isinstance(files_val, list):
            raise ValueError("payload.files must be a list when provided")
        for idx, f in enumerate(files_val):
            if not isinstance(f, dict):
                raise ValueError("payload.files items must be objects")
            if not (isinstance(f.get("uri"), str) and f.get("uri") or isinstance(f.get("signedUrl"), str) and f.get("signedUrl")):
                raise ValueError("payload.files items must include 'uri' or 'signedUrl'")
        files = files_val

    # Resolve schema: object or $ref to gs://...
    schema_obj: Dict[str, Any] | None = None
    if isinstance(schema_val, dict) and "$ref" in schema_val and isinstance(schema_val["$ref"], str) and schema_val["$ref"].startswith("gs://"):
        # Load schema from GCS
        ref = schema_val["$ref"]
        try:
            from urllib.parse import urlparse
            u = urlparse(ref)
            bucket = u.netloc
            path = u.path.lstrip("/")
            client = _gcs_client()
            b = client.bucket(bucket)
            data = b.blob(path).download_as_text()
            schema_obj = json.loads(data)
        except Exception as e:
            raise ValueError(f"Failed to load schema from {ref}: {e}")
    elif isinstance(schema_val, dict):
        schema_obj = schema_val
    elif isinstance(schema_val, str) and schema_val.startswith("gs://"):
        # String ref to GCS
        ref = schema_val
        try:
            from urllib.parse import urlparse
            u = urlparse(ref)
            bucket = u.netloc
            path = u.path.lstrip("/")
            client = _gcs_client()
            b = client.bucket(bucket)
            data = b.blob(path).download_as_text()
            schema_obj = json.loads(data)
        except Exception as e:
            raise ValueError(f"Failed to load schema from {ref}: {e}")
    else:
        raise ValueError("payload.schema must be an object or a gs:// reference")

    # Validate schema
    try:
        schema_str = json.dumps(schema_obj, ensure_ascii=False)
        _ = _parse_schema(schema_str)
    except Exception as e:
        raise ValueError(f"Invalid schema: {e}")

    # Normalize files list for unified input (URIs only in events path)
    files_unified: List[Dict[str, Any]] = []
    for f in files:
        if isinstance(f, dict):
            u = f.get("uri") or f.get("signedUrl")
            mt = f.get("mime") or "application/octet-stream"
            nm = f.get("name") or "file"
            if isinstance(u, str) and u:
                files_unified.append({"name": nm, "uri": u, "mime": mt})

    try:
        data, usage, model_used = _execute_extraction(
            prompt=prompt,
            system_instruction=system_instruction,
            model_name=model,
            schema_str=schema_str,
            files=files_unified,
            trace_id=trace_id,
        )
    except Exception as e:
        raise RuntimeError(f"Extractor error: {e}")

    # Persist results with idempotency
    result_obj = {
        "ok": True,
        "model": model_used,
        "data": data,
        "usage": usage,
        "trace_id": trace_id,
        "warnings": warnings or None,
    }
    result_path = f"results/{request_id}.json"
    result_uri = f"gs://{BUCKET_NAME}/{result_path}" if BUCKET_NAME else ""

    already_exists = False
    if BUCKET_NAME:
        created, err = _gcs_write_json_create_only(BUCKET_NAME, result_path, result_obj)
        if not created:
            # If error indicates precondition failure, treat as exists
            already_exists = True
    else:
        warnings.append("BUCKET_NAME not set; skipping persistence")

    # Build ready envelope
    ready_envelope = {
        "version": envelope.get("version", "1"),
        "event": "extractions.ready",
        "requestId": request_id,
        "source": envelope.get("source"),
        "replyTo": envelope.get("replyTo") or None,
        "meta": meta,
        "payload": {
            "status": "ok",
            "resultUri": result_uri,
        },
    }

    # Publish to replyTo or default topic
    published = False
    publish_result = None
    publish_topic = envelope.get("replyTo") or OUTPUT_TOPIC
    if publish_topic:
        attrs = {
            "trace_id": trace_id,
            "source": f"events/{envelope.get('event', '')}",
            "eventName": "extractions.ready",
            "subscription": subscription or "",
        }
        # Propagate messageKey if present in meta
        msg_key = None
        try:
            if isinstance(meta, dict):
                msg_key = meta.get("messageKey")
        except Exception:
            msg_key = None
        if isinstance(msg_key, str) and msg_key:
            attrs["messageKey"] = msg_key
        ok, msg = _publish_to_pubsub(publish_topic, ready_envelope, attrs)
        published = ok
        publish_result = msg

    return {
        "ok": True,
        "event": envelope.get("event"),
        "trace_id": trace_id,
        "request_id": request_id,
        "published": published,
        "publish_result": publish_result,
        "ready": ready_envelope,
        "warnings": warnings or None,
        "result_uri": result_uri,
        "already_exists": already_exists,
    }


EVENT_HANDLERS = {
    "extractions.request": _handle_extractions_request,
}


@app.route("/events/<event_name>", methods=["POST", "OPTIONS"])
def events_entrypoint(event_name: str):
    trace_id = str(uuid.uuid4())

    if flask_request.method == "OPTIONS":
        headers = _cors_headers()
        headers["Content-Type"] = "application/json"
        return ("", 204, headers)

    if flask_request.method != "POST":
        body, status, headers = _bad_request("Only POST is allowed", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)

    # Transport: decode Pub/Sub push
    try:
        body_json = flask_request.get_json(force=True, silent=False)
    except Exception:
        body_json = None

    if not isinstance(body_json, dict):
        body, status, headers = _bad_request("Invalid JSON body", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)

    try:
        envelope, attributes, subscription = _decode_pubsub_push(body_json)
    except Exception as e:
        body, status, headers = _bad_request(f"Transport decode error: {e}", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)

    # Validation: generic envelope + schema normalization/validation
    try:
        envelope = _validate_envelope(envelope, event_name)
    except Exception as e:
        body, status, headers = _bad_request(f"Envelope validation error: {e}", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)

    # Route check: event name must match path variable
    env_event = envelope.get("event")

    # Log
    try:
        keys = list(envelope.keys())
        print(f"[{trace_id}] event={env_event} subscription={subscription} keys={keys}")
    except Exception:
        pass

    handler = EVENT_HANDLERS.get(event_name)
    if not handler:
        body, status, headers = _bad_request(f"No handler for event '{event_name}'", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)

    try:
        result = handler(trace_id, envelope, attributes, subscription)
        headers = _cors_headers()
        headers["Content-Type"] = "application/json"
        return Response(response=json.dumps(result), status=200, headers=headers)
    except Exception as e:
        body, status, headers = _server_error(f"Handler error: {e}", trace_id)
        return Response(response=json.dumps(body), status=status, headers=headers)
