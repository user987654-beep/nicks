"""Deterministic, immutable, content-addressed evidence bundles."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


class EvidenceError(ValueError):
    """Invalid evidence, bundle structure, or integrity check."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_hash(manifest: dict) -> str:
    try:
        return hashlib.sha256(_canonical(manifest)).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EvidenceError("manifest is not canonical JSON data") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)


def _page_chunks(text: str, limit: int) -> list[bytes]:
    raw = text.encode("utf-8")
    chunks = []
    start = 0
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end > start:
            try:
                raw[start:end].decode("utf-8", "strict")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise EvidenceError("page_bytes is too small for a UTF-8 character")
        chunks.append(raw[start:end])
        start = end
    return chunks


def pack_required(entries: list[dict]) -> list[dict]:
    """Deliver every required original once in a length-framed mandatory stream.

    Individual originals and their hashes remain independently indexed/requestable.
    This changes the transport grouping, never the required content. Labels for
    every occurrence accompany each unique original, including exact duplicates.
    """
    unique = {}
    for entry in entries:
        if entry.get('required', True):
            raw = entry['data']
            key = _digest(raw)
            unique.setdefault(key, {'data': raw, 'labels': []})['labels'].append(entry['label'])
    stream = bytearray(b'Complete mandatory originals; each JSON header gives the following payload length.\n')
    for key, item in unique.items():
        raw = item['data']
        try:
            raw.decode('utf-8', 'strict')
            payload, encoding = raw, 'utf-8'
        except UnicodeDecodeError:
            payload, encoding = base64.b64encode(raw), 'base64'
        header = {'labels': item['labels'], 'sha256': key, 'original_bytes': len(raw),
                  'encoding': encoding, 'payload_bytes': len(payload)}
        stream.extend(_canonical(header) + b'\n' + payload + b'\n')
    return ([{'label': 'Complete mandatory original evidence (lossless framed stream)',
              'data': bytes(stream), 'required': True}] +
            [dict(entry, required=False) for entry in entries])


def create_bundle(destination: Path, artifact: str, entries: list[dict], page_bytes: int = 120000) -> dict:
    destination = Path(destination)
    if not isinstance(artifact, str) or not artifact:
        raise EvidenceError("artifact must be a non-empty string")
    if isinstance(page_bytes, bool) or not isinstance(page_bytes, int) or page_bytes <= 0:
        raise EvidenceError("page_bytes must be a positive integer")
    if not isinstance(entries, list):
        raise EvidenceError("entries must be a list")
    manifest = {"version": 1, "artifact": artifact, "page_bytes": page_bytes,
                "documents": [], "pages": []}
    pages_by_hash: dict[str, bytes] = {}
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not isinstance(entry.get("data"), bytes):
            raise EvidenceError("each entry requires a string label and bytes data")
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise EvidenceError("required must be a boolean")
        raw = entry["data"]
        try:
            text = raw.decode("utf-8", "strict")
            encoding = "utf-8"
            payload = text
        except UnicodeDecodeError:
            encoding = "base64"
            payload = base64.b64encode(raw).decode("ascii")
        doc_pages = []
        if payload:
            for chunk in _page_chunks(payload, page_bytes):
                page_id = _digest(chunk)
                pages_by_hash[page_id] = chunk
                doc_pages.append(page_id)
        manifest["documents"].append({"id": f"d{i:04d}", "label": entry["label"], "sha256": _digest(raw),
                                      "size_bytes": len(raw), "encoding": encoding, "required": required,
                                      "pages": doc_pages})
    manifest["pages"] = [{"id": page_id, "size_bytes": len(data)}
                         for page_id, data in sorted(pages_by_hash.items())]
    manifest_data = _canonical(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        try:
            load_bundle(destination, manifest_hash(manifest), artifact)
        except EvidenceError as exc:
            raise EvidenceError(f"existing destination is not the requested immutable bundle: {exc}") from exc
        return manifest
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        written_sources = set()
        for doc, entry in zip(manifest["documents"], entries):
            if doc["sha256"] not in written_sources:
                _write(staging / "sources" / doc["sha256"], entry["data"])
                written_sources.add(doc["sha256"])
        for page_id, data in pages_by_hash.items():
            _write(staging / "pages" / page_id, data)
        _write(staging / "manifest.json", manifest_data)
        try:
            os.rename(staging, destination)
        except FileExistsError:
            load_bundle(destination, manifest_hash(manifest), artifact)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def _read_hashed(root: Path, folder: str, ident: str, size: int | None = None) -> bytes:
    if not _hex(ident):
        raise EvidenceError("unsafe content identifier")
    directory = root / folder
    path = directory / ident
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise EvidenceError(f"missing or unsafe {folder} content {ident}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read {folder} content {ident}") from exc
    if _digest(data) != ident or (size is not None and len(data) != size):
        raise EvidenceError(f"{folder} content hash or size mismatch: {ident}")
    return data


def _validate_schema(m: dict) -> None:
    if not isinstance(m, dict) or set(m) != {"version", "artifact", "page_bytes", "documents", "pages"}:
        raise EvidenceError("malformed manifest fields")
    if m["version"] != 1 or not isinstance(m["artifact"], str) or not m["artifact"]:
        raise EvidenceError("unsupported manifest version or artifact")
    if isinstance(m["page_bytes"], bool) or not isinstance(m["page_bytes"], int) or m["page_bytes"] <= 0:
        raise EvidenceError("invalid page_bytes")
    if not isinstance(m["documents"], list) or not isinstance(m["pages"], list):
        raise EvidenceError("documents and pages must be lists")
    seen_docs, seen_pages = set(), set()
    for i, d in enumerate(m["documents"], 1):
        if not isinstance(d, dict) or set(d) != {"id", "label", "sha256", "size_bytes", "encoding", "required", "pages"}:
            raise EvidenceError("malformed document record")
        if d["id"] != f"d{i:04d}" or not isinstance(d["label"], str) or not _hex(d["sha256"]):
            raise EvidenceError("invalid document identity")
        if isinstance(d["size_bytes"], bool) or not isinstance(d["size_bytes"], int) or d["size_bytes"] < 0:
            raise EvidenceError("invalid document size")
        if d["encoding"] not in ("utf-8", "base64") or not isinstance(d["required"], bool):
            raise EvidenceError("invalid document encoding or required flag")
        if not isinstance(d["pages"], list) or any(not _hex(p) for p in d["pages"]):
            raise EvidenceError("invalid document page references")
    for p in m["pages"]:
        if not isinstance(p, dict) or set(p) != {"id", "size_bytes"} or not _hex(p["id"]):
            raise EvidenceError("malformed page record")
        if p["id"] in seen_pages or isinstance(p["size_bytes"], bool) or not isinstance(p["size_bytes"], int) or p["size_bytes"] < 0:
            raise EvidenceError("invalid or duplicate page record")
        seen_pages.add(p["id"])
    for d in m["documents"]:
        if not set(d["pages"]).issubset(seen_pages):
            raise EvidenceError("document references an unknown page")


def load_bundle(destination: Path, expected_hash: str, artifact: str) -> dict:
    root = Path(destination)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError("bundle destination is missing or unsafe")
    mp = root / "manifest.json"
    if mp.is_symlink() or not mp.is_file():
        raise EvidenceError("manifest is missing or unsafe")
    try:
        raw = mp.read_bytes()
        m = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("manifest is unreadable or invalid JSON") from exc
    _validate_schema(m)
    if _canonical(m) != raw:
        raise EvidenceError("manifest is not canonical JSON")
    if not isinstance(expected_hash, str) or manifest_hash(m) != expected_hash:
        raise EvidenceError("manifest hash mismatch")
    if m["artifact"] != artifact:
        raise EvidenceError("artifact mismatch")
    page_sizes = {p["id"]: p["size_bytes"] for p in m["pages"]}
    page_texts = {}
    for pid, size in page_sizes.items():
        data = _read_hashed(root, "pages", pid, size)
        try:
            page_texts[pid] = data.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise EvidenceError("page is not valid UTF-8") from exc
    for doc in m["documents"]:
        original = _read_hashed(root, "sources", doc["sha256"], doc["size_bytes"])
        try:
            payload = "".join(page_texts[p] for p in doc["pages"])
            rebuilt = payload.encode("utf-8") if doc["encoding"] == "utf-8" else base64.b64decode(payload.encode("ascii"), validate=True)
        except (UnicodeError, binascii.Error, ValueError) as exc:
            raise EvidenceError("invalid document page encoding") from exc
        if rebuilt != original:
            raise EvidenceError(f"reconstructed document mismatch: {doc['id']}")
        if doc["encoding"] == "utf-8":
            try:
                original.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise EvidenceError("UTF-8 document contains invalid text") from exc
    return m


def read_page(destination: Path, manifest: dict, page_id: str) -> str:
    _validate_schema(manifest)
    record = next((p for p in manifest["pages"] if p["id"] == page_id), None)
    if record is None:
        raise EvidenceError("unknown page id")
    data = _read_hashed(Path(destination), "pages", page_id, record["size_bytes"])
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise EvidenceError("page is not valid UTF-8") from exc


def check_request(text: str, max_bytes: int = 600000) -> str:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise EvidenceError("max_bytes must be a non-negative integer")
    if not isinstance(text, str):
        raise EvidenceError("request must be a string")
    try:
        size = len(text.encode("utf-8", "strict"))
    except UnicodeEncodeError as exc:
        raise EvidenceError("request contains invalid Unicode") from exc
    if size > max_bytes or len(text) > max_bytes:
        raise EvidenceError(f"request exceeds {max_bytes} byte/character limit")
    return text
