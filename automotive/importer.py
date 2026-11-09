"""Bounded operator import for owned or authorized automotive evidence."""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from urllib.parse import urlsplit

KINDS = frozenset({'fitment', 'cross_reference', 'service_specification', 'product_approval'})
MAX_BYTES = 2_000_000
MAX_RECORDS = 1000


def parse_import(content: str, format: str) -> list[dict[str, Any]]:
    """Imported claims always enter pending review, even if the file says VERIFIED."""
    if len(content.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Import exceeds 2 MB.')
    try:
        if format == 'json':
            rows = json.loads(content)
        elif format == 'csv':
            reader = csv.DictReader(io.StringIO(content))
            rows = []
            for row in reader:
                if len(rows) >= MAX_RECORDS:
                    raise ValueError('Import exceeds 1000 records.')
                for key in ('payload', 'applicability', 'restrictions'):
                    if row.get(key):
                        row[key] = json.loads(row[key])
                rows.append(row)
        else:
            raise ValueError('Import format must be csv or json.')
    except (json.JSONDecodeError, csv.Error, RecursionError) as exc:
        raise ValueError('Malformed evidence import.') from exc
    if not isinstance(rows, list) or not rows or len(rows) > MAX_RECORDS:
        raise ValueError('Import requires 1 to 1000 records.')
    validated = []
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') not in KINDS:
            raise ValueError('Every record requires a supported kind.')
        if row.get('reuse_permission') not in ('owned', 'authorized'):
            raise ValueError('Only owned or authorized evidence may be imported.')
        for key in ('source_name', 'authority'):
            if not isinstance(row.get(key), str) or not 1 <= len(row[key].strip()) <= 300:
                raise ValueError(f'Every record requires {key}.')
        source_url = row.get('source_url', '')
        if source_url:
            try:
                url = urlsplit(source_url)
                valid_url = (url.scheme in ('https', 'http') and url.hostname
                             and url.username is None and url.password is None)
            except (ValueError, TypeError):
                valid_url = False
            if not valid_url or len(source_url) > 2000:
                raise ValueError('Source URL must be a public HTTP(S) citation.')
        payload = row.get('payload')
        if not isinstance(payload, dict):
            raise ValueError('Every record requires an object payload.')
        scope = row.get('applicability', payload.get('applicability', {})) or {}
        restrictions = row.get('restrictions', {}) or {}
        if not isinstance(scope, dict) or not isinstance(restrictions, dict):
            raise ValueError('Applicability and restrictions must be objects.')
        # A malicious payload must not smuggle provenance into flattened database results.
        protected = {
            'authority', 'reuse_permission', 'review_status', 'reviewed_by', 'reviewed_at',
            'source_name', 'source_url', 'evidence_id', 'id', 'status',
        }
        clean_payload = {key: value for key, value in payload.items() if key not in protected}
        clean_payload['applicability'] = scope
        validated.append({
            'kind': row['kind'], 'payload': clean_payload,
            'source_name': row['source_name'].strip(), 'title': row['source_name'].strip(),
            'source_url': source_url or '',
            'authority': row['authority'].strip(), 'reuse_permission': row['reuse_permission'],
            'review_status': 'pending', 'reviewed_by': None, 'restrictions': restrictions,
            'applicability': scope,
        })
    return validated
