"""Sarvam Vision — Document OCR.

LIVE mode: calls Sarvam's vision/document-intelligence endpoint.
MOCK mode: pattern-matches the uploaded image filename + a few cheap
           heuristics to pick a realistic fixture from
           data/vision_fixtures.json — letting you exercise the full
           UI without an API key.

PII rules:
  - Full Aadhaar number is NEVER persisted. Only last 4 digits + the
    DigiLocker vault reference. Mock returns masked value already.
  - PAN logged in audit trail as last 4 digits only.
  - Citizen sees full fields in their chat; logs/observability see redacted.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .config import settings
from .http_client import httpx_client_kwargs

log = logging.getLogger("vision")


SUPPORTED_DOC_TYPES = [
    "pan", "aadhaar", "driving_licence", "voter_id",
    "ration_card_image", "patta_image", "auto",
]

SUPPORTED_VISION_LANGUAGES = {
    "hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN",
    "or-IN", "od-IN", "pa-IN", "ta-IN", "te-IN", "ur-IN", "as-IN",
    "bodo-IN", "doi-IN", "ks-IN", "kok-IN", "mai-IN", "mni-IN", "ne-IN",
    "sa-IN", "sat-IN", "sd-IN",
}

VISION_LANGUAGE_ALIASES = {
    "od-in": "or-IN",
}

TERMINAL_JOB_STATES = {"Completed", "PartiallyCompleted", "Failed"}


@dataclass
class OCRResult:
    document_type: str
    fields: dict
    confidence: float
    language: str
    raw_text: str = ""
    mock: bool = False

    def redacted_for_logs(self) -> dict:
        """Strip / mask PII before logging."""
        out = {"document_type": self.document_type,
               "confidence": self.confidence, "language": self.language,
               "mock": self.mock, "fields": {}}
        for k, v in self.fields.items():
            if k in ("aadhaar", "aadhaar_number"):
                out["fields"][k] = "[REDACTED]"
            elif k == "pan_number" and isinstance(v, str) and len(v) >= 4:
                out["fields"][k] = "****" + v[-4:]
            elif k == "dl_number" and isinstance(v, str) and len(v) >= 4:
                out["fields"][k] = "****" + v[-4:]
            else:
                out["fields"][k] = v
        return out


# ---------------------------------------------------------------------------
# Mock-mode fixture picker
# ---------------------------------------------------------------------------

def _fixtures_path() -> Path:
    p = Path(settings.data_dir) / "vision_fixtures.json"
    if not p.exists():
        p = Path(__file__).resolve().parent.parent / "data" / "vision_fixtures.json"
    return p


def _load_fixtures() -> dict:
    p = _fixtures_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pick_fixture(filename: str, hint_type: str | None, content_size: int) -> tuple[str, dict]:
    """Choose a fixture based on (a) explicit hint, (b) filename keywords."""
    fixtures = _load_fixtures()
    if not fixtures:
        return "document", {}

    fn = (filename or "").lower()

    if hint_type and hint_type in fixtures:
        return hint_type, fixtures[hint_type]

    keyword_map = [
        ("pan", "pan"),
        ("aadhaar", "aadhaar"), ("aadhar", "aadhaar"), ("uid", "aadhaar"),
        ("driving", "driving_licence"), ("dl_", "driving_licence"),
        ("licence", "driving_licence"), ("license", "driving_licence"),
        ("voter", "voter_id"), ("epic", "voter_id"),
        ("ration", "ration_card_image"), ("pds", "ration_card_image"),
        ("patta", "patta_image"), ("chitta", "patta_image"),
        ("land", "patta_image"),
    ]
    for kw, fid in keyword_map:
        if kw in fn:
            return fid, fixtures.get(fid, fixtures.get("_default", {}))

    default = fixtures.get("_default", {})
    return default.get("document_type", "document"), default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_document(
    *, image_bytes: bytes, mime_type: str,
    filename: str = "", hint_type: str | None = None,
    language_hint: str = "",
) -> OCRResult:
    """OCR + structured extraction."""
    if settings.mock_mode or not image_bytes:
        if not settings.allow_mock_providers:
            raise RuntimeError(
                "Vision OCR unavailable: SARVAM_API_KEY is required and mock fallback is disabled"
            )
        return await _mock_extract(image_bytes, mime_type, filename, hint_type)

    try:
        return await _extract_document_live(
            image_bytes=image_bytes,
            mime_type=mime_type,
            filename=filename,
            hint_type=hint_type,
            language_hint=language_hint,
        )
    except Exception as e:
        log.error("Sarvam Vision call failed: %s", e)
        if not settings.allow_mock_providers:
            raise
        return await _mock_extract(image_bytes, mime_type, filename, hint_type)


async def _extract_document_live(
    *,
    image_bytes: bytes,
    mime_type: str,
    filename: str,
    hint_type: str | None,
    language_hint: str = "",
) -> OCRResult:
    """Run the real Sarvam Document Intelligence job flow."""
    language = _normalize_vision_language(language_hint or "hi-IN")
    upload_bytes, upload_name, upload_mime = _prepare_document_for_upload(
        image_bytes=image_bytes,
        mime_type=mime_type,
        filename=filename,
    )
    if hint_type and hint_type not in SUPPORTED_DOC_TYPES:
        hint_type = None

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=120.0),
        headers={"api-subscription-key": settings.sarvam_api_key},
        base_url=settings.sarvam_base_url,
        **httpx_client_kwargs(),
    ) as api_client:
        job = await _create_document_job(api_client, language=language)
        job_id = job["job_id"]
        await _upload_presigned_file(
            upload_name=upload_name,
            upload_bytes=upload_bytes,
            upload_mime=upload_mime,
            api_client=api_client,
            job_id=job_id,
        )
        await _start_document_job(api_client, job_id)
        status = await _poll_document_job_status(api_client, job_id)
        if status.get("job_state") not in TERMINAL_JOB_STATES:
            raise RuntimeError(f"Sarvam OCR job did not finish cleanly: {status.get('job_state')}")
        if status.get("job_state") == "Failed":
            raise RuntimeError(status.get("error_message") or "Sarvam OCR job failed")
        downloaded = await _download_document_outputs(api_client, job_id)

    fields, raw_text, inferred_type = _parse_document_outputs(downloaded)
    combined_text = raw_text or _extract_fallback_text(fields)
    document_type = _guess_document_type(
        text=combined_text,
        filename=filename,
        hint_type=hint_type or inferred_type,
    )
    confidence = _estimate_confidence(fields, combined_text, document_type)
    return OCRResult(
        document_type=document_type,
        fields=fields,
        confidence=confidence,
        language=language,
        raw_text=combined_text,
        mock=False,
    )


async def _create_document_job(client: httpx.AsyncClient, *, language: str) -> dict:
    payload = {"job_parameters": {"language": language, "output_format": "md"}}
    resp = await client.post("/doc-digitization/job/v1", json=payload)
    resp.raise_for_status()
    return resp.json()


async def _get_upload_urls(client: httpx.AsyncClient, job_id: str, upload_name: str) -> dict:
    resp = await client.post(
        "/doc-digitization/job/v1/upload-files",
        json={"job_id": job_id, "files": [upload_name]},
    )
    resp.raise_for_status()
    return resp.json()


async def _upload_presigned_file(
    *,
    upload_name: str,
    upload_bytes: bytes,
    upload_mime: str,
    api_client: httpx.AsyncClient,
    job_id: str,
) -> None:
    upload_payload = await _get_upload_urls(api_client, job_id, upload_name)
    upload_urls = upload_payload.get("upload_urls") or {}
    upload_info = upload_urls.get(upload_name)
    if upload_info is None and upload_urls:
        upload_info = next(iter(upload_urls.values()))
    if upload_info is None:
        raise RuntimeError("Sarvam OCR did not return an upload URL")

    if isinstance(upload_info, dict):
        upload_url = (
            upload_info.get("upload_url")
            or upload_info.get("file_url")
            or upload_info.get("url")
        )
        method = (upload_info.get("method") or upload_info.get("http_method") or "PUT").upper()
        extra_headers = upload_info.get("headers") or upload_info.get("request_headers") or {}
    else:
        upload_url = str(upload_info)
        method = "PUT"
        extra_headers = {}
    if not upload_url:
        raise RuntimeError("Sarvam OCR upload URL missing from response")

    base_headers = dict(extra_headers) if isinstance(extra_headers, dict) else {}
    candidate_methods = [method]
    for fallback_method in ("PUT", "POST"):
        if fallback_method not in candidate_methods:
            candidate_methods.append(fallback_method)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=120.0),
        **httpx_client_kwargs(),
    ) as upload_client:
        last_error: Exception | None = None
        for candidate in candidate_methods:
            headers = dict(base_headers)
            if candidate == "PUT":
                headers.setdefault("x-ms-blob-type", "BlockBlob")
                headers.setdefault("Content-Type", upload_mime or "application/pdf")
            try:
                resp = await upload_client.request(
                    candidate,
                    upload_url,
                    content=upload_bytes,
                    headers=headers or None,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                return
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error


async def _start_document_job(client: httpx.AsyncClient, job_id: str) -> dict:
    resp = await client.post(f"/doc-digitization/job/v1/{job_id}/start", json={})
    resp.raise_for_status()
    return resp.json()


async def _poll_document_job_status(client: httpx.AsyncClient, job_id: str) -> dict:
    deadline = time.monotonic() + 75.0
    delay = 1.0
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(f"/doc-digitization/job/v1/{job_id}/status")
        resp.raise_for_status()
        status = resp.json()
        state = status.get("job_state") or status.get("state") or ""
        if state in TERMINAL_JOB_STATES:
            return status
        await asyncio.sleep(delay)
        delay = min(delay + 0.5, 3.0)
    raise TimeoutError(f"Sarvam OCR job timed out waiting for completion: {job_id}")


async def _download_document_outputs(client: httpx.AsyncClient, job_id: str) -> list[tuple[str, dict, bytes]]:
    resp = await client.post(f"/doc-digitization/job/v1/{job_id}/download-files", json={})
    resp.raise_for_status()
    payload = resp.json()
    download_urls = payload.get("download_urls") or {}
    out: list[tuple[str, dict, bytes]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), **httpx_client_kwargs()) as file_client:
        for file_name, meta in download_urls.items():
            if isinstance(meta, dict):
                file_url = meta.get("file_url") or meta.get("url")
                file_meta = meta.get("file_metadata") or {}
            else:
                file_url = str(meta)
                file_meta = {}
            if not file_url:
                continue
            resp = await file_client.get(file_url, follow_redirects=True)
            resp.raise_for_status()
            out.append((file_name, file_meta, resp.content))
    return out


def _normalize_vision_language(language_hint: str | None) -> str:
    raw = (language_hint or "").strip().replace("_", "-")
    if not raw:
        return "hi-IN"
    alias = VISION_LANGUAGE_ALIASES.get(raw.lower())
    if alias:
        return alias
    for supported in SUPPORTED_VISION_LANGUAGES:
        if supported.lower() == raw.lower():
            return supported
    return "hi-IN"


def _prepare_document_for_upload(
    *,
    image_bytes: bytes,
    mime_type: str,
    filename: str,
) -> tuple[bytes, str, str]:
    mime_l = (mime_type or "").lower()
    name = Path(filename or "document").name or "document"
    if mime_l.startswith("image/") or Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        try:
            return _image_bytes_to_pdf_bytes(image_bytes), _ensure_suffix(name, ".pdf"), "application/pdf"
        except Exception as exc:
            log.warning("Could not convert image upload to PDF; falling back to original bytes: %s", exc)
    if Path(name).suffix:
        upload_name = name
    elif mime_l == "application/pdf":
        upload_name = "document.pdf"
    else:
        upload_name = f"{name}.pdf" if not mime_l or mime_l.startswith("image/") else name
    upload_mime = "application/pdf" if upload_name.lower().endswith(".pdf") else mime_type or "application/octet-stream"
    return image_bytes, upload_name, upload_mime


def _image_bytes_to_pdf_bytes(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PDF")
        return buf.getvalue()


def _ensure_suffix(name: str, suffix: str) -> str:
    path = Path(name)
    if path.suffix.lower() == suffix.lower():
        return path.name
    return path.with_suffix(suffix).name


def _parse_document_outputs(outputs: list[tuple[str, dict, bytes]]) -> tuple[dict, str, str]:
    fields: dict[str, Any] = {}
    texts: list[str] = []
    doc_type = ""
    for file_name, meta, blob in outputs:
        parsed_fields, parsed_text, parsed_type = _parse_output_blob(
            file_name=file_name,
            file_meta=meta,
            blob=blob,
        )
        fields.update(parsed_fields)
        if parsed_text:
            texts.append(parsed_text)
        if not doc_type and parsed_type:
            doc_type = parsed_type
    return fields, "\n".join(texts).strip(), doc_type


def _parse_output_blob(
    *,
    file_name: str,
    file_meta: dict,
    blob: bytes,
) -> tuple[dict, str, str]:
    content_type = (file_meta.get("contentType") or file_meta.get("content_type") or "").lower()
    lower = file_name.lower()

    if lower.endswith(".zip") or "zip" in content_type:
        return _parse_zip_blob(blob)

    text = _decode_text_blob(blob)
    if lower.endswith(".json") or "json" in content_type:
        try:
            obj = json.loads(text)
        except Exception:
            return {}, text, ""
        fields, extracted_text = _flatten_json_fields(obj)
        return fields, extracted_text or text, _guess_document_type(extracted_text or text, file_name, "")

    if lower.endswith(".html") or "html" in content_type:
        stripped = _strip_html(text)
        return _extract_kv_fields(stripped), stripped, _guess_document_type(stripped, file_name, "")

    if lower.endswith(".md") or lower.endswith(".markdown") or "markdown" in content_type or lower.endswith(".txt"):
        return _extract_kv_fields(text), text, _guess_document_type(text, file_name, "")

    return {}, text, _guess_document_type(text, file_name, "")


def _parse_zip_blob(blob: bytes) -> tuple[dict, str, str]:
    fields: dict[str, Any] = {}
    texts: list[str] = []
    doc_type = ""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            inner_name = Path(info.filename).name
            inner_blob = zf.read(info)
            parsed_fields, parsed_text, parsed_type = _parse_output_blob(
                file_name=inner_name,
                file_meta={"contentType": _content_type_for_name(inner_name)},
                blob=inner_blob,
            )
            fields.update(parsed_fields)
            if parsed_text:
                texts.append(parsed_text)
            if not doc_type and parsed_type:
                doc_type = parsed_type
    return fields, "\n".join(texts).strip(), doc_type


def _content_type_for_name(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".html", ".htm"}:
        return "text/html"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".zip":
        return "application/zip"
    return ""


def _decode_text_blob(blob: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return blob.decode(enc)
        except Exception:
            continue
    return blob.decode("utf-8", errors="ignore")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _flatten_json_fields(obj: Any, prefix: str = "") -> tuple[dict, str]:
    fields: dict[str, Any] = {}
    texts: list[str] = []

    def _walk(value: Any, key_prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                next_prefix = f"{key_prefix}.{key}" if key_prefix else str(key)
                _walk(child, next_prefix)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                next_prefix = f"{key_prefix}[{idx}]" if key_prefix else f"[{idx}]"
                _walk(child, next_prefix)
        else:
            if key_prefix:
                fields[key_prefix] = value
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())

    _walk(obj, prefix)
    return fields, "\n".join(texts).strip()


def _extract_kv_fields(text: str) -> dict:
    out: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^([A-Za-z0-9_/().,' -]{2,80}?)[\s]*[:\-]\s*(.+)$", stripped)
        if m:
            key = re.sub(r"\s+", " ", m.group(1)).strip(" -:")
            value = m.group(2).strip()
            if key and value:
                out[key] = value
    return out


def _extract_fallback_text(fields: dict) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        parts.append(f"{key}: {value}")
    return "\n".join(parts).strip()


def _guess_document_type(text: str, filename: str, hint_type: str | None) -> str:
    hint = (hint_type or "").strip().lower()
    if hint in SUPPORTED_DOC_TYPES and hint not in {"", "auto"}:
        return hint

    hay = f"{filename}\n{text}\n{hint}".lower()
    rules = [
        ("aadhaar", "aadhaar"),
        ("aadhar", "aadhaar"),
        ("uidai", "aadhaar"),
        ("unique identification authority", "aadhaar"),
        ("permanent account number", "pan"),
        ("income tax department", "pan"),
        ("pan card", "pan"),
        ("driving licence", "driving_licence"),
        ("driving license", "driving_licence"),
        ("transport department", "driving_licence"),
        ("epic", "voter_id"),
        ("voter", "voter_id"),
        ("election commission", "voter_id"),
        ("ration", "ration_card_image"),
        ("nfsa", "ration_card_image"),
        ("public distribution", "ration_card_image"),
        ("patta", "patta_image"),
        ("chitta", "patta_image"),
        ("adangal", "patta_image"),
    ]
    for needle, doc_type in rules:
        if needle in hay:
            return doc_type
    return "document"


def _estimate_confidence(fields: dict, text: str, document_type: str) -> float:
    score = 0.45
    if text.strip():
        score += 0.2
    if fields:
        score += 0.2
    if document_type and document_type != "document":
        score += 0.1
    if len(text) > 100:
        score += 0.05
    return min(score, 0.99)


async def _mock_extract(
    image_bytes: bytes, mime_type: str, filename: str, hint_type: str | None,
) -> OCRResult:
    await asyncio.sleep(0.7 + random.random() * 0.6)
    fid, fixture = _pick_fixture(filename, hint_type, len(image_bytes))
    return OCRResult(
        document_type=fixture.get("document_type", fid or "document"),
        fields=fixture.get("fields", {}),
        confidence=fixture.get("confidence", 0.8),
        language=fixture.get("language", "en-IN"),
        raw_text=fixture.get("raw_text", ""),
        mock=True,
    )
