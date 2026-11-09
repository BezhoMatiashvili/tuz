"""Match a decoded vehicle to a supplier's equipment label by trim, fuel and engine code.

Displacement from a VIN decoder is not trusted over the trim: the same engine can be
listed with a different displacement by another source. Labels are supplier-asserted.
"""

from __future__ import annotations

import re
from typing import Any

FUEL_MARKERS = {
    'gasoline': {'P', 'B', 'G'}, 'petrol': {'P', 'B', 'G'}, 'diesel': {'D'},
}
BODY_WORDS = {
    'coupe': r'coup[eé]', 'convertible': r'convertible|cabrio|roadster|spider|spyder',
    'wagon': r'wagon|estate|t-model|touring|avant|variant|tourer',
    'hatchback': r'hatch',
}
MARKER = re.compile(r'\(([A-Z])\)')
ENGINE_CODE = re.compile(r'\(([A-Z]{1,3}\s\d{3}(?:\.\d{1,3})?|[A-Z]{1,3}\d{3}\.\d{1,3})\)')
MODEL_SUFFIX = re.compile(r'(.*?\S)[\s-]*(?:class|series)', re.IGNORECASE)


def trim_groups(trim: Any) -> list[str]:
    return re.findall(r'[A-Za-z]+|\d+', trim) if isinstance(trim, str) else []


def trim_search_text(trim: Any) -> str:
    """`E400` becomes `E 400`, the spelling suppliers usually use in labels."""
    groups = trim_groups(trim)
    return ' '.join(groups) if len(''.join(groups)) >= 3 else ''


def model_base(model: Any) -> str:
    """`E-Class` -> `E`, `3 Series` -> `3`: how Shell and Pakelo start their labels."""
    text = str(model or '').strip()
    found = MODEL_SUFFIX.fullmatch(text)
    return found.group(1) if found else text


def search_terms(model: Any, trim: Any, body: Any = None) -> list[str]:
    """Chooser search text, best first: Shell finds `GLE 350 d`, never `GLE-Class GLE 350 d`.

    A search returns one page, so `E-Class Coupe` keeps the coupes from being cut off.
    """
    full, base, words = str(model or '').strip(), model_base(model), trim_search_text(trim)
    named = next((word for word in ('Coupe', 'Convertible')
                  if re.search(BODY_WORDS[word.casefold()], str(body or ''), re.IGNORECASE)), '')
    terms = [f'{full} {named}'.strip(), full, base]
    if words:
        first = words.split(' ', 1)[0].casefold()
        lead = words if base and first.startswith(base.casefold()) else f'{base} {words}'.strip()
        terms = [lead, f'{full} {words}'.strip()] + terms
    return list(dict.fromkeys(term for term in terms if term))


def engine_option(label: Any) -> dict | None:
    """`E-CLASS Coupe (C207) E 400 245kW (M 276.850) (P) (2015)` -> E 400, petrol."""
    text = str(label or '')
    markers = set(MARKER.findall(text)) & {'P', 'B', 'G', 'D'}
    fuel = 'Diesel' if markers == {'D'} else 'Gasoline' if markers and 'D' not in markers else None
    text = ENGINE_CODE.sub(' ', re.sub(r'\((?:[A-Z]|\d{4})\)', ' ', text))
    tail = text.partition(')')[2]
    text = tail if tail.strip() else re.sub(r'\([^)]*\)', ' ', text)
    trim = ' '.join(re.sub(r'(?<![A-Za-z0-9])\d+\s?kW(?![A-Za-z])', ' ', text,
                           flags=re.IGNORECASE).split())
    return {'trim': trim[:60], 'fuel': fuel} if len(''.join(trim_groups(trim))) >= 2 else None


def engine_options(labels: Any, limit: int = 6) -> list[dict]:
    """The engines a customer can choose from, once each."""
    options: dict[tuple, dict] = {}
    for label in labels:
        option = engine_option(label)
        if option:
            options.setdefault((option['trim'].casefold(), option['fuel']), option)

    def words(option: dict) -> list[str]:
        return [group.casefold() for group in trim_groups(option['trim'])]

    # 'E 350 BlueTEC' already names 'E 350 BlueTEC / d': the shorter choice covers both.
    kept = [option for option in options.values() if not any(
        other is not option and other['fuel'] == option['fuel']
        and len(words(other)) < len(words(option))
        and words(option)[:len(words(other))] == words(other) for other in options.values())]
    return kept[:limit]


def _spelled(value: str) -> str | None:
    """`CR-V` and `crv`, `RX 350` and `RX350` spell one name; `CLE` does not contain `E`."""
    chars = re.sub(r'[^a-z0-9]+', '', value.casefold())
    if not chars:
        return None
    tail = r'(?![0-9])' if chars[-1].isdigit() else r'(?![a-z])'
    return r'(?<![a-z0-9-])' + r'[\s./-]*'.join(map(re.escape, chars)) + tail


def model_matches(label: Any, model: Any) -> bool:
    """The supplier's model names this one: `E-Class` is `E-CLASS (W212)` and Shell's
    `GLE (W166)` is `GLE-Class`, but `CLE-CLASS` is not `E-Class`."""
    text = str(label or '')
    full = _spelled(str(model or ''))
    if full and re.search(full, text, re.IGNORECASE):
        return True
    base = model_base(model)
    lead = _spelled(base) if base != str(model or '').strip() else None
    return bool(lead and re.match(r'\s*' + lead, text, re.IGNORECASE))


def _trim_pattern(groups: list[str]) -> str:
    # `400` is not the `400kW` in a label.
    return (r'(?<![A-Za-z0-9])' + r'[\s/-]*'.join(re.escape(g) for g in groups)
            + r'(?![0-9])(?!\s?kw)')


def trim_matches(label: Any, trim: Any) -> bool:
    groups = trim_groups(trim)
    if len(''.join(groups)) < 3:
        return False
    text = str(label or '')
    if re.search(_trim_pattern(groups), text, re.IGNORECASE):
        return True
    # Shell writes `GLE (W166) 350 d`, `IS II 250` and `G Coupe 37`: the letters lead the
    # label and the number follows later.
    lead = re.match(r'\s*([A-Za-z]+)', text)
    return (len(groups) >= 2 and groups[0].isalpha() and groups[1].isdigit()
            and bool(lead) and lead.group(1).casefold() == groups[0].casefold()
            and bool(re.search(_trim_pattern(groups[1:]), text, re.IGNORECASE)))


def fuel_matches(label: Any, fuel: Any) -> bool:
    """A label with a fuel marker must agree with the decoded fuel; no marker means unknown."""
    allowed = FUEL_MARKERS.get(str(fuel or '').strip().casefold())
    markers = set(MARKER.findall(str(label or ''))) & {'P', 'B', 'G', 'D'}
    return not (allowed and markers and not markers & allowed)


def engine_code(label: Any) -> str | None:
    found = ENGINE_CODE.search(str(label or ''))
    return found.group(1) if found else None


def engine_family(code: Any) -> str | None:
    """`M 276.850` -> `M276`: the part before the dot, spaces removed."""
    base = re.sub(r'\s+', '', str(code or '').split('.', 1)[0])
    return base if len(trim_groups(base)) >= 2 else None


def engine_matches(label: Any, code: Any) -> bool:
    """The label names the engine family the customer stated (`M276` for `M 276.850`)."""
    family = engine_family(code)
    return bool(family) and engine_family(engine_code(label)) == family


def by_engine(items: list[dict], label_of: Any, code: Any) -> list[dict]:
    """Keep labels naming the stated engine family; labels without a code stay as unknown."""
    if not engine_family(code):
        return items
    named = [item for item in items if engine_matches(label_of(item), code)]
    return named or [item for item in items if engine_code(label_of(item)) is None]


def by_body(items: list[dict], label_of: Any, body: Any) -> list[dict]:
    """Keep labels naming the decoded body type; unchanged when no label names it."""
    text = str(body or '').casefold()
    for word, pattern in BODY_WORDS.items():
        if re.search(pattern, text):
            named = [item for item in items if re.search(pattern, str(label_of(item) or ''),
                                                         re.IGNORECASE)]
            return named or items
    return items


def narrow(items: list[dict], label_of: Any, vehicle: dict) -> list[dict]:
    """Prefer trim matches, then fuel and body, dropping labels that contradict the vehicle."""
    items = by_engine(items, label_of, vehicle.get('engine_code'))
    by_trim = [item for item in items if trim_matches(label_of(item), vehicle.get('trim'))]
    fuel = vehicle.get('fuel') or vehicle.get('fuel_type')
    if by_trim:
        # A trim that contradicts the fuel is a disagreement between sources: no match.
        chosen = [item for item in by_trim if fuel_matches(label_of(item), fuel)]
        return by_body(chosen, label_of, vehicle.get('body'))
    compatible = [item for item in items if fuel_matches(label_of(item), fuel)]
    return by_body(compatible or items, label_of, vehicle.get('body'))


def signature(report: dict) -> tuple:
    return tuple(sorted((str(item.get('product_name')), str(item.get('viscosity')),
                         str(item.get('capacity'))) for item in report.get('items') or []))


def identical(reports: list[dict]) -> bool:
    """Several equipment variants may share one recommendation (2WD and 4-matic)."""
    return bool(reports) and all(r.get('status') == 'PROVISIONAL' for r in reports) and len(
        {signature(r) for r in reports}) == 1
