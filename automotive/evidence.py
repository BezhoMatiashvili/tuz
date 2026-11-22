"""Conservative evidence rules shared by API, importer and recommendation services."""

from __future__ import annotations

from typing import Any

TRUSTED_AUTHORITIES = frozenset({'manufacturer', 'official', 'authorized_supplier'})
ALLOWED_REUSE = frozenset({'owned', 'authorized'})
VEHICLE_KEYS = frozenset({
    'make', 'model', 'model_year', 'engine_code', 'fuel', 'transmission', 'market',
    'body', 'drive', 'displacement_l', 'engine_cylinders', 'trim', 'fuel_type', 'dpf',
})


def flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Payload cannot override provenance or trusted review metadata."""
    payload = record.get('payload', {})
    return {**(payload if isinstance(payload, dict) else {}), **record}


def verified_record(record: dict[str, Any]) -> bool:
    """An asserted VERIFIED string is never itself a verification decision."""
    row = flatten_record(record)
    return (
        row.get('authority') in TRUSTED_AUTHORITIES
        and row.get('reuse_permission') in ALLOWED_REUSE
        and row.get('review_status') == 'approved'
        and isinstance(row.get('reviewed_by'), str)
        and bool(row['reviewed_by'].strip())
        and not row.get('superseded_by')
        and not row.get('revoked', False)
    )


def _same(left: Any, right: Any) -> bool:
    return str(left).strip().casefold() == str(right).strip().casefold()


def _same_vehicle_value(key: str, left: Any, right: Any) -> bool:
    if key != 'market':
        return _same(left, right)
    aliases = {'usa': 'us', 'united states': 'us', 'eu': 'europe', 'jp': 'japan'}
    actual, expected = (str(value).strip().casefold() for value in (left, right))
    return aliases.get(actual, actual) == aliases.get(expected, expected)


def applies_to(record: dict[str, Any], vehicle: dict[str, Any] | None) -> bool:
    """Unknown configuration keys or unresolved restrictions never match silently."""
    row = flatten_record(record)
    scope = row.get('applicability', {})
    restrictions = row.get('restrictions', {})
    if not isinstance(scope, dict) or not isinstance(restrictions, dict):
        return False
    if vehicle is None:
        return not scope and not restrictions
    for constraints in (scope, restrictions):
        for key, expected in constraints.items():
            if key in ('model_year_from', 'model_year_to'):
                actual = vehicle.get('model_year')
                if actual is None:
                    return False
                try:
                    if key.endswith('from') and int(actual) < int(expected):
                        return False
                    if key.endswith('to') and int(actual) > int(expected):
                        return False
                except (TypeError, ValueError):
                    return False
            elif key not in VEHICLE_KEYS or vehicle.get(key) in (None, ''):
                return False
            elif isinstance(expected, list):
                if not any(_same_vehicle_value(key, vehicle[key], value) for value in expected):
                    return False
            elif not _same_vehicle_value(key, vehicle[key], expected):
                return False
    return True


def missing_oil_detail(record: dict[str, Any], vehicle: dict[str, Any]) -> str | None:
    """A missing DPF or market value that could make an oil specification applicable."""
    working = dict(vehicle)
    missing = []
    scopes = [record.get(key, {}) for key in ('applicability', 'restrictions')]
    for key in ('dpf', 'market'):
        if working.get(key) not in (None, ''):
            continue
        required = [scope[key] for scope in scopes if isinstance(scope, dict) and key in scope]
        if not required:
            continue
        first = required[0]
        values = first if isinstance(first, list) else [first]
        value = next((value for value in values if value not in (None, '')
                      and all(any(_same_vehicle_value(key, value, option) for option in
                                  (other if isinstance(other, list) else [other]))
                              for other in required)), None)
        if value is None:
            return None
        working[key] = value
        missing.append(key)
    return missing[0] if missing and applies_to(record, working) else None


def evaluate_compatibility(
    records: list[dict[str, Any]], vehicle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep contrary evidence visible and never infer interchange from co-occurrence."""
    rows = [flatten_record(row) for row in records]
    usable = [row for row in rows if applies_to(row, vehicle)
              and row.get('review_status') != 'rejected' and not row.get('superseded_by')
              and not row.get('revoked', False)]
    claims = {row.get('compatible') for row in usable if isinstance(row.get('compatible'), bool)}
    warnings = []
    if len(claims) > 1:
        status = 'CONFLICTED'
        warnings.append('Sources disagree; compatibility is not established.')
    elif claims and any(verified_record(row) and isinstance(row.get('compatible'), bool)
                        for row in usable):
        status = 'VERIFIED'
    elif usable:
        status = 'PROVISIONAL'
    else:
        status = 'UNKNOWN'
    if len(usable) != len(rows):
        warnings.append('Some records do not apply, are superseded, revoked or rejected.')
    if status != 'VERIFIED':
        warnings.append('Do not treat these records as confirmed compatibility.')
    return {
        'status': status, 'compatible': next(iter(claims)) if len(claims) == 1 else None,
        'evidence': rows, 'warnings': warnings,
    }
