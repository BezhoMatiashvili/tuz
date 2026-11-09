"""NHTSA vPIC identification, with keyed caching and no persisted raw VIN."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import psycopg
import requests

VPIC_URL = 'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/'
VIN_PATTERN = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')
VIN_IN_TEXT = re.compile(r'(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])', re.IGNORECASE)
TRANSLITERATION = dict(zip('ABCDEFGHJKLMNPRSTUVWXYZ',
                           (1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9),
                           strict=True))
WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
FIELDS = {
    'Make': 'make', 'Model': 'model', 'ModelYear': 'model_year', 'Trim': 'trim',
    'EngineModel': 'engine_model', 'DisplacementL': 'displacement_l',
    'EngineCylinders': 'engine_cylinders', 'FuelTypePrimary': 'fuel',
    'TransmissionStyle': 'transmission', 'DriveType': 'drive', 'BodyClass': 'body',
}


class ProviderUnavailable(RuntimeError):
    """Decoder failure whose details are safe to return to a customer."""


def validate_vin(vin: str) -> str:
    value = vin.strip().upper()
    if not VIN_PATTERN.fullmatch(value):
        raise ValueError('VIN must contain 17 letters/digits and must not contain I, O or Q.')
    return value


def vin_checksum(vin: str) -> dict[str, str]:
    value = validate_vin(vin)
    total = sum((int(char) if char.isdigit() else TRANSLITERATION[char]) * weight
                for char, weight in zip(value, WEIGHTS, strict=True))
    expected = 'X' if total % 11 == 10 else str(total % 11)
    return {
        'status': 'MATCH' if value[8] == expected else 'MISMATCH',
        'applicability': ('NORTH_AMERICAN_WMI' if value[0] in '12345'
                          else 'MARKET_DEPENDENT'),
    }


def _text(value: Any, vin: str) -> str | None:
    if not isinstance(value, str | int | float) or not str(value).strip():
        return None
    return VIN_IN_TEXT.sub('[redacted]', str(value).replace(vin, '[redacted]'))[:1000]


class VinService:
    def __init__(self, repository: Any, hmac_key: str) -> None:
        if len(hmac_key.encode('utf-8')) < 32:
            raise ValueError('VIN cache HMAC key must be at least 32 bytes.')
        self.repository = repository
        self.hmac_key = hmac_key.encode('utf-8')

    def decode(self, vin: str, model_year: int | None = None,
               *, deadline: float | None = None) -> dict[str, Any]:
        value = validate_vin(vin)
        if model_year is not None and (isinstance(model_year, bool)
                                       or not 1886 <= model_year <= datetime.now(UTC).year + 2):
            raise ValueError('Model year is outside the supported range.')
        fingerprint = hmac.new(
            self.hmac_key, f'vpic-v1:{value}:{model_year or ""}'.encode(), hashlib.sha256,
        ).hexdigest()
        cache_available = True
        try:
            cached = self.repository.get_cached_vin(fingerprint)
        except (RuntimeError, psycopg.Error):
            # Decoding does not depend on a cache. Never echo connection details
            # or persist the VIN elsewhere when the private cache is unavailable.
            cached = None
            cache_available = False
        if cached is not None:
            return {**cached, 'cached': True}
        response = None
        try:
            remaining = deadline - monotonic() if deadline is not None else 15.0
            if remaining <= 0:
                raise ProviderUnavailable('Vehicle decoder time limit reached.')
            response = requests.post(
                VPIC_URL, data={'format': 'json', 'data': f'{value},{model_year or ""}'},
                timeout=(min(3.05, remaining), min(15, remaining)),
                allow_redirects=False, stream=True,
            )
            if response.status_code != 200:
                raise ProviderUnavailable('Vehicle decoder is unavailable; retry later.')
            response.raise_for_status()
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if deadline is not None and monotonic() >= deadline:
                    raise ProviderUnavailable('Vehicle decoder time limit reached.')
                if len(buffer) + len(chunk) > 1_000_000:
                    raise ProviderUnavailable('Vehicle decoder returned an oversized response.')
                buffer.extend(chunk)
            body = json.loads(buffer)
            results = body.get('Results') if isinstance(body, dict) else None
            if (not isinstance(results, list) or len(results) != 1
                    or not isinstance(results[0], dict)):
                raise ProviderUnavailable('Vehicle decoder returned an invalid response.')
            source = results[0]
        except (requests.RequestException, ValueError, RecursionError) as exc:
            raise ProviderUnavailable('Vehicle decoder is unavailable; retry later.') from exc
        finally:
            if response is not None:
                response.close()
        vehicle: dict[str, Any] = {
            key: _text(source.get(field), value) for field, key in FIELDS.items()
        }
        # vPIC EngineModel is descriptive and is not a certified engine-code identification.
        vehicle['engine_code'] = None
        if vehicle.get('model_year') and str(vehicle['model_year']).isdigit():
            vehicle['model_year'] = int(vehicle['model_year'])
        checksum = vin_checksum(value)
        warnings = ['Engine code and maintenance specifications require independent evidence.']
        if checksum['status'] == 'MISMATCH':
            warnings.append('VIN check digit differs from the North American calculation; '
                            'applicability depends on vehicle market.')
        error_code = _text(source.get('ErrorCode'), value)
        if error_code not in (None, '0'):
            warnings.append(f'vPIC error codes: {error_code}')
        for field in ('ErrorText', 'AdditionalErrorText', 'SuggestedVIN'):
            message = _text(source.get(field), value)
            if message:
                # Suggested VIN is intentionally not returned; even corrected identifiers are PII.
                warnings.append('vPIC suggests a VIN correction; verify the supplied VIN.'
                                if field == 'SuggestedVIN' else message)
        observed_at = datetime.now(UTC).isoformat()
        payload = {
            'status': 'PROVISIONAL' if vehicle.get('make') and vehicle.get('model') else 'UNKNOWN',
            'vehicle': vehicle, 'checksum': checksum, 'warnings': warnings,
            'provider': 'vPIC', 'observed_at': observed_at, 'cached': False,
            'evidence': [{
                'source_name': 'NHTSA vPIC', 'source_url': 'https://vpic.nhtsa.dot.gov/api/',
                'authority': 'official', 'verification_status': 'PROVISIONAL',
                'observed_at': observed_at,
            }],
        }
        if cache_available:
            try:
                self.repository.cache_vin(fingerprint, payload)
            except (RuntimeError, psycopg.Error):
                cache_available = False
        if not cache_available:
            payload['warnings'].append(
                'VIN-ის ქეში მიუწვდომელია; მიღებული პასუხი არ შეინახა.',
            )
        return payload
