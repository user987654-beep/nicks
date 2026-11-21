"""Standalone v2 evidence delivery receipt validation.

``make_scope(job_id, artifact, round, member, stage, purpose)`` builds the
caller-authoritative scope. ``make_step`` creates a v2 step record from actual
prompt/response bytes and a numeric execution directory. ``validate_steps``
accepts the expected scope, bundle location/hash, actual model/provider, ordered
expected ``{'kind': ..., 'page': ...}`` items, and receipt records. Receipt
records are never used to infer the expected scope. The chain links accepted
receipts only; gaps in execution directory numbers are permitted.

This detects accidental or partial transplantation and tampering, not a
consistent rewrite of the whole store. Legacy verification is intentionally a
separate caller-selected path.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .evidence_bundle import load_bundle, read_page

VERSION = 2
_SCOPE_FIELDS = ('job_id', 'artifact', 'round', 'member', 'stage', 'purpose')


class IntegrityError(ValueError):
    """A v2 delivery record or its associated bytes failed validation."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('utf-8')


def make_scope(job_id, artifact, round, member, stage, purpose):
    """Return canonical caller metadata; round is an int (not bool) or None."""
    if not all(isinstance(x, str) and x for x in (job_id, artifact, member, stage, purpose)):
        raise IntegrityError('scope fields must be non-empty strings')
    if round is not None and (isinstance(round, bool) or not isinstance(round, int)):
        raise IntegrityError('round must be an integer or None')
    return dict(job_id=job_id, artifact=artifact, round=round, member=member, stage=stage, purpose=purpose)


def _scope(value):
    if not isinstance(value, dict) or set(value) != set(_SCOPE_FIELDS):
        raise IntegrityError('invalid delivery scope schema')
    expected = make_scope(*(value[k] for k in _SCOPE_FIELDS))
    return expected


def _originals(manifest):
    return [{'document': d['id'], 'sha256': d['sha256']} for d in manifest['documents']]


def make_step(scope, manifest_hash, model, provider, sequence, previous_sha256,
              execution_dir, kind, page, prompt: bytes, response: bytes,
              page_payload: bytes | None, manifest):
    """Build a v2 accepted record; callers persist prompt/response unchanged."""
    scope = _scope(scope)
    if kind not in ('page', 'requests', 'final'):
        raise IntegrityError('unknown step kind')
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise IntegrityError('invalid acceptance sequence')
    if not isinstance(execution_dir, int) or isinstance(execution_dir, bool) or execution_dir < 1:
        raise IntegrityError('execution directory identity must be numeric')
    if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
        raise IntegrityError('actual model and provider are required')
    pages = {p['id'] for p in manifest['pages']}
    if kind == 'page' and (page not in pages or not isinstance(page_payload, bytes)):
        raise IntegrityError('page step requires a known page and payload')
    if kind != 'page' and (page is not None or page_payload is not None):
        raise IntegrityError('non-page step cannot name page content')
    return {'version': VERSION, 'scope': scope, 'manifest': manifest_hash,
            'model': model, 'provider': provider, 'sequence': sequence,
            'previous_sha256': previous_sha256, 'execution_dir': execution_dir,
            'kind': kind, 'page': page,
            'page_sha256': sha256(page_payload) if page_payload is not None else None,
            'originals': _originals(manifest), 'prompt_sha256': sha256(prompt),
            'response_sha256': sha256(response)}


def _read_text(path):
    try:
        return path.read_bytes().decode('utf-8', 'strict')
    except (OSError, UnicodeError) as exc:
        raise IntegrityError('prompt/response must be readable strict UTF-8 bytes') from exc


def validate_steps(bundle_dir, expected_manifest_hash, expected_scope, model, provider,
                   expected_steps, records):
    """Validate ordered accepted v2 records against explicit caller expectations.

    ``records`` are dicts pairing ``record`` with ``directory`` (a step folder).
    ``expected_steps`` is the caller's ordered list of kind/page objects; page
    items may also provide exact original document IDs in ``originals``.
    """
    scope = _scope(expected_scope)
    manifest = load_bundle(Path(bundle_dir), expected_manifest_hash, scope['artifact'])
    page_map = {p['id']: p for p in manifest['pages']}
    original_map = {d['id']: d for d in manifest['documents']}
    if not isinstance(expected_steps, list) or not isinstance(records, list) or len(expected_steps) != len(records):
        raise IntegrityError('accepted step sequence is missing, duplicated, or reordered')
    previous = None
    last_execution = 0
    receipt_hashes = []
    required_fields = {'version','scope','manifest','model','provider','sequence','previous_sha256',
                       'execution_dir','kind','page','page_sha256','originals','prompt_sha256','response_sha256'}
    for i, (expected, entry) in enumerate(zip(expected_steps, records), 1):
        if not isinstance(expected, dict) or set(expected) - {'kind','page','originals'}:
            raise IntegrityError('invalid expected step contract')
        if not isinstance(entry, dict) or set(entry) != {'record','directory'}:
            raise IntegrityError('invalid record input')
        step, directory = entry['record'], Path(entry['directory'])
        if not isinstance(step, dict) or set(step) != required_fields or step.get('version') != VERSION:
            raise IntegrityError('unknown or mixed delivery receipt schema')
        if step['scope'] != scope or step['manifest'] != expected_manifest_hash:
            raise IntegrityError('delivery scope or manifest mismatch')
        if step['model'] != model or step['provider'] != provider:
            raise IntegrityError('actual model/provider mismatch')
        if step['sequence'] != i or step['previous_sha256'] != previous:
            raise IntegrityError('accepted sequence or predecessor mismatch')
        execution = step['execution_dir']
        if isinstance(execution, bool) or not isinstance(execution, int) or execution <= last_execution:
            raise IntegrityError('execution directory identity mismatch')
        if directory.name != f'{execution:06d}' and directory.name != str(execution):
            raise IntegrityError('record does not match numeric execution directory')
        last_execution = execution
        kind, page = expected.get('kind'), expected.get('page')
        if kind not in ('page','requests','final') or (step['kind'], step['page']) != (kind, page):
            raise IntegrityError('unexpected step kind or page')
        originals = step['originals']
        if not isinstance(originals, list) or originals != _originals(manifest):
            raise IntegrityError('original document associations mismatch')
        wanted_originals = expected.get('originals')
        if wanted_originals is not None and [x['document'] for x in originals] != wanted_originals:
            raise IntegrityError('unexpected original document association')
        prompt_path, response_path = directory / 'prompt.txt', directory / 'response.txt'
        prompt_raw, response_raw = prompt_path.read_bytes(), response_path.read_bytes()
        prompt = _read_text(prompt_path)
        _read_text(response_path)
        if sha256(prompt_raw) != step['prompt_sha256'] or sha256(response_raw) != step['response_sha256']:
            raise IntegrityError('prompt/response byte hash mismatch')
        if kind == 'page':
            if page not in page_map:
                raise IntegrityError('unknown expected page')
            content = read_page(Path(bundle_dir), manifest, page)
            payload = content.encode('utf-8')
            if sha256(payload) != step['page_sha256']:
                raise IntegrityError('page payload hash mismatch')
            frame = f'ORIGINAL EVIDENCE PAGE {page} BEGIN\n{content}\nORIGINAL EVIDENCE PAGE END'
            if frame not in prompt:
                raise IntegrityError('actual prompt lacks exact original page frame')
            for ident in {d['id'] for d in manifest['documents'] if page in d['pages']}:
                raw = (Path(bundle_dir) / 'sources' / original_map[ident]['sha256']).read_bytes()
                if sha256(raw) != original_map[ident]['sha256']:
                    raise IntegrityError('original source hash mismatch')
        elif step['page_sha256'] is not None:
            raise IntegrityError('non-page record has page payload hash')
        previous = sha256(canonical(step))
        receipt_hashes.append(previous)
    return receipt_hashes
