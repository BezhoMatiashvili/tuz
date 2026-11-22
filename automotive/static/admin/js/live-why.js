// Why the bot found nothing, in words, for the Live chats page: an alarm's `detail`
// (automotive/live_alerts.turn_detail) and a stuck chat's failed reply, as lines for its card.
// Pure: no DOM and no network, and the words come from the `t` and `has` the caller passes, so
// plain Node tests it (tests/admin_live_why_test.mjs).
//
// Facts only: each search the bot ran, with its words and the counts the catalogue itself gave
// (rows that hold the words, in stock, naming the car, words in no row, how the rows that matched
// name their cars), what was out of stock, what a shop rule took out, the limits that cut the turn
// short, and the cause of a turn that failed. Whether a row fits the car is the bot's own
// judgement, and the line that says so says that it is one.

const OK = new Set(['OK', 'CLARIFICATION']);
// What the engine adapter writes for a turn that ended in an error: its class, then its step.
const CAUSE = /^([A-Za-z_][A-Za-z0-9_]*)@([a-z]+)$/;
// The engine's own words for a failed search, as the staff read them.
const TOOL_ERRORS = {
  'The tool did not answer in time.': 'live.why.error.late',
  'The data behind this tool could not be read right now.': 'live.why.error.unreadable',
};
const LIMITS = ['time', 'searches', 'requests', 'checks', 'stale_stock'];

const count = (value) => (Number.isFinite(Number(value)) ? Math.max(0, Math.trunc(Number(value))) : 0);
const words = (list) => (Array.isArray(list)
  ? list.filter((item) => typeof item === 'string' && item.trim()) : []);
const plain = (value) => (typeof value === 'string' ? value.trim() : '');

function worded(t, has, key, fallback) {
  return has(key) ? t(key) : fallback;
}

/** The cause of a turn that produced no answer: its status in words or, for an internal error
 * (the engine records its class only then), the error's class and the step of the turn it came
 * from. The status of an internal error is a generic one and would mislead, so it is not said. */
export function failureLine(status, error, stage, t, has) {
  if (plain(error)) {
    const step = worded(t, has, `live.why.stage.${plain(stage)}`, t('live.why.stage.other'));
    return t('live.why.cause_internal', { error: plain(error), stage: step });
  }
  const code = plain(status);
  const fallback = code || t('live.why.status.FAILED');
  return t('live.why.cause', { cause: worded(t, has, `live.why.status.${code}`, fallback) });
}

function failedSearch(search, about, t) {
  const error = plain(search.error);
  // The template ends the sentence: an error's own full stop is not said twice.
  const said = TOOL_ERRORS[error] ? t(TOOL_ERRORS[error]) : error.replace(/[.\s]+$/, '');
  return t('live.why.search_failed', {
    tool: t(`live.why.tool.${search.tool}`), about, error: said,
  });
}

function catalogueWords(search, t) {
  const groups = [];
  for (const key of ['part', 'car', 'make', 'exclude']) {
    const list = words(search[key]);
    if (list.length) {
      groups.push(t(`live.why.words.${key}`, { words: list.join(', ') }));
    }
  }
  if (search.stock_only === true) {
    groups.push(t('live.why.words.stock_only'));
  }
  return groups.join(' · ');
}

function catalogueLine(search, t) {
  const asked = catalogueWords(search, t);
  if (plain(search.error)) {
    return failedSearch(search, asked ? ` (${asked})` : '', t);
  }
  if (!('total' in search)) {
    return t('live.why.catalog.asked', { words: asked });
  }
  const total = count(search.total);
  const available = count(search.available);
  const carWords = words(search.car).length > 0;
  const stockOnly = search.stock_only === true;
  const parts = [];
  if (total === 0) {
    parts.push(t('live.why.catalog.none'));
    const alone = search.alone && typeof search.alone === 'object' ? search.alone : {};
    const groups = ['part', 'car', 'make'].filter((group) => group in alone).map((group) => t(
      'live.why.alone', { group: t(`live.why.group.${group}`), count: count(alone[group]) }));
    if (groups.length) {
      parts.push(groups.join(', '));
    }
    if (count(search.excluded)) {
      parts.push(t('live.why.catalog.excluded', { count: count(search.excluded) }));
    }
  } else if (stockOnly && available === 0) {
    parts.push(t('live.why.catalog.none_in_stock', { total }));
  } else if (carWords && count(search.car_rows) === 0) {
    // With "in stock only" the rows naming the car are counted among those in stock alone.
    parts.push(t(stockOnly ? 'live.why.catalog.car_none_stock' : 'live.why.catalog.car_none',
      { total, available }));
    const cars = (Array.isArray(search.cars) ? search.cars : [])
      .filter((item) => Array.isArray(item) && typeof item[0] === 'string')
      .map((item) => t('live.why.catalog.car_item', { name: item[0], count: count(item[1]) }));
    if (cars.length) {
      parts.push(t('live.why.catalog.cars', { list: cars.join(', ') }));
    }
  } else if (carWords) {
    parts.push(t('live.why.catalog.car_some', { rows: count(search.car_rows), total, available }));
  } else {
    parts.push(t('live.why.catalog.found', { total, available }));
  }
  const unmatched = words(search.unmatched);
  if (unmatched.length) {
    parts.push(t('live.why.catalog.unmatched', { words: unmatched.join(', ') }));
  }
  const spellings = (Array.isArray(search.spellings) ? search.spellings : [])
    .filter((item) => Array.isArray(item) && typeof item[1] === 'string')
    .map((item) => t('live.why.catalog.car_item', { name: item[1], count: count(item[2]) }));
  if (spellings.length) {
    parts.push(t('live.why.catalog.spellings', { list: spellings.join(', ') }));
  }
  for (const key of ['near', 'no_make', 'elsewhere']) {
    if (count(search[key])) {
      parts.push(t(`live.why.catalog.${key}`, { count: count(search[key]) }));
    }
  }
  return t('live.why.catalog', { words: asked, result: parts.join('; ') });
}

function numbersLine(search, t) {
  const items = (Array.isArray(search.numbers) ? search.numbers : [])
    .filter((item) => item && typeof item.number === 'string' && item.number)
    .map((item) => {
      if (item.short) {
        return t('live.why.number.short', { number: item.number });
      }
      if (!('rows' in item)) {
        return t('live.why.number.asked', { number: item.number });
      }
      const found = count(item.rows);
      let line = found ? t('live.why.number.rows', { number: item.number, rows: found })
        : t('live.why.number.none', { number: item.number });
      if (count(item.revision)) {
        line += ` ${t('live.why.number.revision', { count: count(item.revision) })}`;
      }
      if (count(item.cross)) {
        line += ` ${t('live.why.number.cross', { count: count(item.cross) })}`;
      }
      return line;
    });
  if (plain(search.error)) {
    return failedSearch(search, items.length ? ` (${items.join(', ')})` : '', t);
  }
  return t('live.why.numbers', { list: items.join('; ') });
}

function sourceLine(search, t, has) {
  const label = plain(search.label);
  const about = label ? ` (${label})` : '';
  if (plain(search.error)) {
    return failedSearch(search, about, t);
  }
  const rows = count(search.rows);
  if (search.tool === 'equivalents') {
    return t('live.why.equivalents', { about, rows });
  }
  if (search.tool === 'supplier_parts') {
    let result = t('live.why.supplier.none');
    if (search.status === 'still_running') {
      result = t('live.why.supplier.running');
    } else if (count(search.numbers)) {
      result = t('live.why.supplier.numbers', { numbers: count(search.numbers), rows });
    }
    return t('live.why.supplier', { about, result });
  }
  if (search.tool === 'oil_guide') {
    const status = plain(search.status);
    return t('live.why.oil', { about, result: worded(t, has, `live.why.oil.${status}`, status) });
  }
  if (search.tool === 'car_numbers') {
    return t('live.why.car_numbers', {
      about, vehicles: count(search.vehicles), parts: count(search.parts), rows,
    });
  }
  if (search.tool === 'research') {
    return t('live.why.research', {
      about, result: search.status === 'answered' ? t('live.why.research.answered', { rows })
        : t('live.why.research.none'),
    });
  }
  return '';
}

/** One search of the turn in words: what it looked for and what it found. */
export function searchLine(search, t, has) {
  if (!search || typeof search !== 'object') {
    return '';
  }
  if (search.tool === 'search_catalog') {
    return catalogueLine(search, t);
  }
  if (search.tool === 'lookup_codes') {
    return numbersLine(search, t);
  }
  return sourceLine(search, t, has);
}

/** Whether a search handed the bot rows to judge (for the car, when car words were given). */
function foundRows(search) {
  if (!search || plain(search.error)) {
    return false;
  }
  if (search.tool === 'search_catalog') {
    const rows = words(search.car).length ? search.car_rows : search.total;
    return count(rows) > 0 && !(search.stock_only === true && count(search.available) === 0);
  }
  if (search.tool === 'lookup_codes') {
    return (Array.isArray(search.numbers) ? search.numbers : []).some(
      (item) => item && (count(item.rows) || count(item.revision) || count(item.cross)));
  }
  return count(search.rows) > 0;
}

function stockItem(item, t) {
  const name = [item.brand, item.code, item.name].map(plain).filter(Boolean).join(' ');
  return t('live.why.unavailable.item', {
    name, store: count(item.store), warehouse: count(item.warehouse),
  });
}

/**
 * Why an alarm's bot found nothing, as lines for its card: [{ text, tone }], in the order the
 * staff read them (the cause of a failed turn; each search; what the bot judged or chose; what
 * was out of stock or taken out by a shop rule; what cut the turn short; the bot's own note).
 * Nothing for an alarm without a detail (raised before migration 0017, or a visit request).
 */
export function whyLines(alert, t, has) {
  const detail = alert && alert.detail && typeof alert.detail === 'object' ? alert.detail : null;
  if (!detail || !Object.keys(detail).length) {
    return [];
  }
  const lines = [];
  const add = (text, tone = '') => {
    if (text) {
      lines.push({ text, tone });
    }
  };
  if (plain(detail.status) && !OK.has(plain(detail.status))) {
    add(failureLine(detail.status, detail.error, detail.stage, t, has), 'cause');
  }
  const searches = Array.isArray(detail.searches) ? detail.searches : [];
  for (const search of searches) {
    add(searchLine(search, t, has), 'search');
  }
  const more = count(detail.searched) - searches.length;
  if (more > 0) {
    add(t('live.why.more', { count: more }), 'search');
  }
  if ('searched' in detail && count(detail.searched) === 0 && !plain(detail.status)) {
    add(t('live.why.none'));
  }
  const missing = words(detail.missing);
  if (detail.chosen === 'offered' && detail.outcome !== 'offered') {
    add(t('live.why.chosen'));
  } else if (detail.outcome === 'nothing_found' && searches.some(foundRows)) {
    add(t('live.why.judged'));
  } else if (missing.length && detail.outcome !== 'sold_out' && searches.some(foundRows)) {
    // Other parts were offered: no search is tied to a part, so the sentence says no more than
    // the facts do. (Out of stock is said by the rows below, not judged.)
    add(t('live.why.judged_missing', { list: missing.join(', ') }));
  }
  const unavailable = (Array.isArray(detail.unavailable) ? detail.unavailable : [])
    .filter((item) => item && typeof item === 'object');
  if (unavailable.length) {
    add(t('live.why.unavailable', { list: unavailable.map((item) => stockItem(item, t)).join('; ') }));
  }
  const barred = words(detail.barred);
  if (barred.length) {
    add(t('live.why.barred', { list: barred.join('; ') }));
  }
  for (const limit of LIMITS) {
    if (words(detail.limits).includes(limit)) {
      add(t(`live.why.limit.${limit}`), 'limit');
    }
  }
  const note = plain(detail.note);
  if (note && note !== plain(alert.summary)) {
    add(t('live.why.note', { note }), 'note');
  }
  return lines;
}

/** Why the bot could not answer a stuck Messenger chat: its newest failed reply, as the worker
 * recorded it (the engine's status, or an internal error's class and step). */
export function stuckCause(chat, t, has) {
  if (!chat) {
    return [];
  }
  const engine = plain(chat.failed_engine);
  const error = plain(chat.failed_error);
  const cause = CAUSE.exec(error);
  const lines = [];
  // (The server passes only the engine's own cause: Meta's words for a refused send stay in the
  // admin's Facebook section, and the chat's reason already names that failure.)
  if (engine && !OK.has(engine)) {
    lines.push(failureLine(engine, cause ? cause[1] : '', cause ? cause[2] : '', t, has));
  }
  return lines;
}
