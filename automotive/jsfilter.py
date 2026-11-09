"""Public JS Asakashi catalogue. VIN lookup returns HTML; model text is not guessed."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from time import monotonic

from automotive.public_http import (
    SUPPLIER_WARNING,
    PublicHttp,
    PublicSourceError,
    fluid_product,
    fluid_report,
    redact,
)
from automotive.vin import validate_vin

ORIGIN = 'https://www.jsfilter.jp'
CODE = re.compile(r'^[A-Z0-9][A-Z0-9-]{1,24}$')
KIND = {'OIL': 'oil', 'AIR': 'filter', 'FUEL': 'filter', 'CABIN': 'filter', 'TRANS': 'filter'}


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._title = ''
        self._text: list[str] = []
        self._codes: list[str] = []
        self._in_cell = False
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == 'tr' and 'model-title' not in (values.get('class') or ''):
            self._row = {}
        elif tag == 'td' and self._row is not None:
            self._in_cell = True
            self._title = (values.get('data-title') or '').upper()
            self._text, self._codes = [], []
        elif tag == 'a' and self._in_cell:
            self._in_link = True

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._text.append(data)
        if self._in_link:
            code = re.sub(r'\s+', '', data).upper()
            if CODE.fullmatch(code):
                self._codes.append(code)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a':
            self._in_link = False
        elif tag == 'td' and self._in_cell and self._row is not None:
            text = redact(' '.join(self._text), 80) or ''
            if self._title in KIND:
                self._row[self._title] = self._codes[:4]
            elif self._title in {'YEAR', 'ENG VOL', 'ENG NO'}:
                self._row[self._title] = text
            self._in_cell = False
        elif tag == 'tr' and self._row is not None:
            if any(self._row.get(name) for name in KIND):
                self.rows.append(self._row)
            self._row = None


class JsFilterClient:
    def __init__(self) -> None:
        self.http = PublicHttp(ORIGIN)

    def lookup(self, vin: str, vehicle: dict, *, deadline: float | None = None) -> dict:
        del vehicle
        vin = validate_vin(vin)
        if deadline is not None and monotonic() >= deadline:
            raise PublicSourceError('TIME_LIMIT')
        self.http.reserve()
        status, body = self.http.read(
            'POST', ORIGIN + '/application/get_applications_by_vin/',
            data={'vin_number': vin},
            headers={
                'Origin': ORIGIN, 'Referer': ORIGIN + '/catalogue/',
                'X-Requested-With': 'XMLHttpRequest', 'Accept': 'text/html',
            },
            deadline=deadline,
        )
        if status != 200:
            raise PublicSourceError('PROVIDER_UNAVAILABLE')
        parser = _Table()
        parser.feed(body.decode('utf-8', 'replace'))
        parser.close()
        if len(parser.rows) > 1:
            labels = [redact(' '.join(row.get(key, '') for key in ('YEAR', 'ENG NO')), 80)
                      for row in parser.rows[:8]]
            return fluid_report(
                'JS Asakashi', 'CONFLICTED',
                clarification='რამდენიმე ძრავა ემთხვევა. ფილტრი არ შერჩეულა: ' + '; '.join(
                    label for label in labels if label),
                warnings=['ძრავა ცალსახა არ არის, ამიტომ ფილტრის კოდი არ დაბრუნდა.'],
            )
        if len(parser.rows) != 1:
            return fluid_report(
                'JS Asakashi', 'EMPTY',
                warnings=['VIN ამ კატალოგში ფილტრის ცხრილს არ აბრუნებს.'],
            )
        items = []
        for title, codes in parser.rows[0].items():
            if title not in KIND or not isinstance(codes, list):
                continue
            for code in codes[:4]:
                row = fluid_product(code, code, component=title, kind=KIND[title])
                if row:
                    items.append(row)
        if not items:
            return fluid_report('JS Asakashi', 'EMPTY')
        return fluid_report('JS Asakashi', 'PROVISIONAL', items=items[:12],
                            warnings=[SUPPLIER_WARNING])
