// Tests of why the bot found nothing, in words (live-why.js): an alarm's detail and a stuck
// chat's failed reply, as the Live chats page shows them.
//
//   node tests/admin_live_why_test.mjs
//
// live-why.js has no DOM and no network; the words come from the strings tables passed in.

import assert from 'node:assert/strict';

import { failureLine, searchLine, stuckCause, whyLines } from '../automotive/static/admin/js/live-why.js';
import { STRINGS } from '../automotive/static/admin/js/strings.js';

const tests = [];
const test = (name, body) => tests.push([name, body]);

// The page's own t() and has(), for one table: a key that is missing comes back as itself.
function words(language) {
  const table = STRINGS[language];
  const has = (key) => Object.prototype.hasOwnProperty.call(table, key);
  const t = (key, values) => (has(key) ? table[key] : key).replace(/\{(\w+)\}/g, (match, name) => (
    values && Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match));
  return { t, has };
}
const en = words('en');
const ka = words('ka');
const lines = (alert, { t, has } = en) => whyLines(alert, t, has);
const texts = (alert, language = en) => lines(alert, language).map((line) => line.text);

const catalogue = (extra = {}) => ({
  tool: 'search_catalog', part: ['სანთელი'], car: ['camry'], make: ['toyota'], total: 31,
  available: 12, rows: 30, car_rows: 0, ...extra,
});
const alarm = (detail, extra = {}) => ({
  kind: 'part_not_found', reason: 'nothing_found', summary: 'სანთლები', detail, ...extra,
});

test('an alarm without a detail (older, or a visit request) has no lines', () => {
  assert.deepEqual(lines(alarm({})), []);
  assert.deepEqual(lines(alarm(undefined)), []);
  assert.deepEqual(lines({ kind: 'visit_request', summary: 'tomorrow' }), []);
  assert.deepEqual(lines(null), []);
});

test('rows that hold the part but name no car say so, with the cars they do name', () => {
  const [line] = texts(alarm({ v: 1, outcome: 'nothing_found', searched: 1, searches: [catalogue({
    cars: [['COROLLA 2007-2013', 8, 3], ['RAV4', 5, 1]] })] }));
  assert.equal(line, 'Catalogue search (part «სანთელი» · car «camry» · make «toyota»): rows with '
    + 'the part: 31 (12 in stock), but none of them names this car; those rows name COROLLA '
    + '2007-2013 (8), RAV4 (5).');
});

test('no row with all the words: what each group matches alone, and words in no row at all', () => {
  const [line] = texts(alarm({ searched: 1, searches: [catalogue({
    total: 0, available: 0, car_rows: 0, alone: { part: 0, car: 210, make: 4012 },
    unmatched: ['свеча'], spellings: [['svecha', 'SVECHI', 3]], excluded: 2 })] }));
  assert.equal(line, 'Catalogue search (part «სანთელი» · car «camry» · make «toyota»): no row has '
    + 'all these words; rows with the part words alone: 0, rows with the car words alone: 210, '
    + 'rows with the make alone: 4012; rows the "without" words left out: 2; no row at all has '
    + 'свеча; similar words in the catalogue: SVECHI (3).');
});

test('in stock only: the rows naming the car are counted among those in stock', () => {
  const [line] = texts(alarm({ searched: 1, searches: [catalogue({ stock_only: true })] }));
  assert.match(line, /in stock only\): rows with the part: 31, in stock: 12, but none of those in stock names this car/);
  const [none] = texts(alarm({ searched: 1, searches: [catalogue({ stock_only: true,
    available: 0 })] }));
  assert.match(none, /rows that match: 31, none of them in stock/);
});

test('rows for the car that the bot did not offer are the bot\'s own judgement, said as such', () => {
  const found = texts(alarm({ outcome: 'nothing_found', searched: 1,
    searches: [catalogue({ car_rows: 4 })] }));
  assert.match(found[0], /rows that name this car: 4 \(of 31 with the part, 12 in stock\)/);
  assert.equal(found[1], 'The searches found rows, but the bot judged that none of them fits this '
    + 'request (the bot\'s own judgement).');
  // Nothing for the car: no judgement line, the counts say it.
  assert.equal(texts(alarm({ outcome: 'nothing_found', searched: 1,
    searches: [catalogue()] })).length, 1);
});

test('part numbers: on rows, on none, too short, other revisions and cross numbers', () => {
  const line = searchLine({ tool: 'lookup_codes', numbers: [
    { number: '90919-01253', rows: 0 }, { number: '0242236565', rows: 2, cross: 1 },
    { number: 'AB', short: true }, { number: '1EA698151D', rows: 0, revision: 1 }] }, en.t, en.has);
  assert.equal(line, 'Part number search: 90919-01253 is on no row; 0242236565: rows 2 (cross '
    + 'numbers in the catalogue: 1); AB is too short to look up; 1EA698151D is on no row (another '
    + 'revision of the number: 1).');
});

test('a search that failed says which and why, in the staff\'s words for known errors', () => {
  assert.equal(searchLine({ tool: 'oil_guide', label: 'oil · Toyota Camry 2020',
    error: 'The tool did not answer in time.' }, en.t, en.has),
  'Oil selectors (oil · Toyota Camry 2020) failed: it did not answer in time.');
  assert.equal(searchLine({ tool: 'search_catalog', part: ['pads'],
    error: 'The data behind this tool could not be read right now.' }, en.t, en.has),
  'Catalogue search (part «pads») failed: its data could not be read.');
  assert.equal(searchLine({ tool: 'research', label: 'x', error: 'Something new.' }, en.t, en.has),
    'Web research (x) failed: Something new.');
});

test('the outside sources say what they answered', () => {
  const say = (search) => searchLine(search, en.t, en.has);
  assert.equal(say({ tool: 'supplier_parts', label: 'oil filter · Toyota Camry 2020',
    status: 'complete', numbers: 4, rows: 0 }),
  'Outside catalogues (oil filter · Toyota Camry 2020): part numbers for this car: 4; rows in '
    + 'the shop with them: 0.');
  assert.match(say({ tool: 'supplier_parts', label: 'x', status: 'still_running' }),
    /did not finish in time/);
  assert.match(say({ tool: 'supplier_parts', label: 'x', status: 'complete', numbers: 0 }),
    /no part numbers for this car/);
  assert.equal(say({ tool: 'oil_guide', label: 'oil · BMW X5 2012', status: 'engine_ambiguous' }),
    'Oil selectors (oil · BMW X5 2012): several engines fit and the engine is not known.');
  assert.match(say({ tool: 'car_numbers', label: 'BMW X5', vehicles: 2, parts: 0, rows: 0 }),
    /vehicles 2, parts 0, rows in the shop 0/);
  assert.match(say({ tool: 'research', label: 'x', status: 'answered', rows: 1 }),
    /answered; rows in the shop: 1/);
  assert.match(say({ tool: 'equivalents', label: 'BOSCH 0451103316', rows: 0 }),
    /Same part under other numbers \(BOSCH 0451103316\): rows 0/);
});

test('a turn that failed names its cause first: an internal error by class and step', () => {
  const found = lines(alarm({ status: 'DATA_UNAVAILABLE', error: 'ValueError', stage: 'model',
    searched: 0 }, { kind: 'needs_staff', reason: 'failed' }));
  assert.deepEqual(found, [{ tone: 'cause',
    text: 'Cause: the bot hit an internal error (ValueError) while reading the AI\'s reply.' }]);
  assert.equal(failureLine('TIMEOUT', '', '', en.t, en.has),
    'Cause: The bot ran out of time before it could answer.');
  assert.equal(failureLine('SOMETHING_NEW', '', '', en.t, en.has), 'Cause: SOMETHING_NEW');
  assert.equal(failureLine('', '', '', en.t, en.has), 'Cause: The bot could not answer.');
  assert.equal(failureLine('', 'KeyError', 'nowhere', en.t, en.has),
    'Cause: the bot hit an internal error (KeyError) while at another step.');
});

test('a turn that searched nothing says so, unless it failed', () => {
  assert.deepEqual(texts(alarm({ outcome: 'nothing_found', searched: 0 })),
    ['The bot did not search for the part in this turn.']);
  assert.equal(texts(alarm({ status: 'TIMEOUT', searched: 0 })).length, 1);
});

test('rows chosen and then not shown, out of stock, shop rules, limits and the note', () => {
  const found = texts(alarm({
    outcome: 'nothing_found', chosen: 'offered', searched: 3, note: 'Camry 2020 სანთლები',
    searches: [catalogue({ car_rows: 2 })],
    unavailable: [{ brand: 'DENSO', code: 'SK20R11', name: 'სანთელი', store: 0, warehouse: 0 }],
    barred: ['Not for hybrids'], limits: ['stale_stock', 'time', 'bogus'],
  }));
  assert.deepEqual(found.slice(1), [
    '…and 2 more.',
    'The bot chose rows, but none of them could be shown.',
    'Out of stock: DENSO SK20R11 სანთელი (0 in the store, 0 in the warehouse).',
    'Taken out by a shop rule: Not for hybrids.',
    'Time ran out: the bot answered with what it had found by then.',
    'The stock figures are old: the real stock may differ.',
    'The bot\'s note: Camry 2020 სანთლები',
  ]);
  // A note that is the alarm's own summary is not said twice.
  assert.equal(texts(alarm({ searched: 0, note: 'სანთლები' })).some(
    (text) => text.includes('note')), false);
});

test('a partial reply joins the parts it did not offer to the rows the searches returned', () => {
  const partial = (extra) => texts(alarm({ outcome: 'offered', missing: ['ზეთის ფილტრი'],
    searched: 1, searches: [catalogue({ part: ['ზეთის ფილტრი'], car: ['elantra'],
      make: ['hyundai'], total: 51, available: 12, car_rows: 7 })], ...extra },
  { reason: 'partial', summary: 'ზეთის ფილტრი' }));
  assert.deepEqual(partial({}).slice(1), ['The reply did not offer ზეთის ფილტრი: where a search '
    + 'above returned rows for this car, leaving them out was the bot\'s own judgement.']);
  // Out of stock is not a judgement: the rows below say it.
  assert.equal(partial({ outcome: 'sold_out' }).length, 1);
  // No rows came back for the car: nothing was left out.
  assert.equal(texts(alarm({ outcome: 'offered', missing: ['x'], searched: 1,
    searches: [catalogue()] })).length, 1);
});

test('a stuck chat names the cause its failed reply recorded', () => {
  const say = (chat) => stuckCause(chat, en.t, en.has);
  assert.deepEqual(say({ failed_engine: 'DATA_UNAVAILABLE', failed_error: 'ValueError@model' }),
    ['Cause: the bot hit an internal error (ValueError) while reading the AI\'s reply.']);
  assert.deepEqual(say({ failed_engine: 'TIMEOUT', failed_error: '' }),
    ['Cause: The bot ran out of time before it could answer.']);
  // The engine answered and Meta refused the send: the chat's reason says so, nothing more
  // (Meta's own words stay in the admin's Facebook section; the server does not send them).
  assert.deepEqual(say({ failed_engine: 'OK',
    failed_error: '(#551) This person isn\'t available right now.' }), []);
  // The worker's own one-word codes add nothing to the chat's reason.
  assert.deepEqual(say({ failed_engine: 'OK', failed_error: 'send_token_unavailable' }), []);
  assert.deepEqual(say({ failed_engine: '', failed_error: '' }), []);
  assert.deepEqual(say(null), []);
});

test('every line has words in both languages: no key is left raw', () => {
  const details = [
    { outcome: 'nothing_found', chosen: 'offered', searched: 9, note: 'n', status: 'TIMEOUT',
      searches: [catalogue({ cars: [['A', 1, 1]], unmatched: ['x'], spellings: [['a', 'B', 1]],
        near: 1, no_make: 1, elsewhere: 1, excluded: 1 }),
      catalogue({ total: 0, alone: { part: 1, car: 2, make: 3 } }),
      catalogue({ car: [], total: 5 }), catalogue({ car_rows: 3 }),
      catalogue({ stock_only: true }), catalogue({ stock_only: true, available: 0 }),
      { tool: 'lookup_codes', numbers: [{ number: 'A1234', rows: 0, revision: 1, cross: 1 },
        { number: 'B', short: true }, { number: 'C1234', rows: 1 }] },
      { tool: 'lookup_codes', numbers: [{ number: 'D1234' }], error: 'x' }],
      unavailable: [{ brand: 'B', code: 'C', name: 'N', store: 0, warehouse: 0 }],
      barred: ['r'], limits: ['time', 'searches', 'requests', 'checks', 'stale_stock'] },
    { status: 'DATA_UNAVAILABLE', error: 'ValueError', stage: 'tools', searched: 0 },
    { outcome: 'offered', missing: ['m'], searched: 1, searches: [catalogue({ car_rows: 2 })] },
    { outcome: 'nothing_found', searched: 6, searches: [
      { tool: 'equivalents', label: 'l', rows: 0 },
      { tool: 'supplier_parts', label: 'l', status: 'complete', numbers: 2, rows: 1 },
      { tool: 'supplier_parts', label: 'l', status: 'still_running' },
      { tool: 'oil_guide', label: 'l', status: 'no_answer' },
      { tool: 'car_numbers', label: 'l', vehicles: 1, parts: 1, rows: 1 },
      { tool: 'research', label: 'l', status: 'no_answer' },
      { tool: 'search_catalog', error: 'The tool did not answer in time.' }] },
  ];
  for (const language of [en, ka]) {
    for (const detail of details) {
      for (const line of texts(alarm(detail), language)) {
        assert.doesNotMatch(line, /live\.why\.|\{\w+\}/, line);
      }
    }
    for (const status of ['TIMEOUT', 'TURN_DEADLINE', 'PROVIDER_BILLING', 'AUTHENTICATION_ERROR',
      'ACCESS_DENIED', 'PROVIDER_RATE_LIMITED', 'PROVIDER_UNAVAILABLE', 'DATA_UNAVAILABLE',
      'UNGROUNDED_RESPONSE', 'REFUSED', 'LIMIT_EXHAUSTED', 'NOT_CONFIGURED', 'CHAT_UNAVAILABLE',
      'CHAT_CLOSED', 'EMPTY_ANSWER', 'CONFLICTING_VINS', 'FAILED']) {
      assert.ok(language.has(`live.why.status.${status}`), status);
    }
    for (const stage of ['model', 'tools', 'checks', 'answer', 'context', 'other']) {
      assert.ok(language.has(`live.why.stage.${stage}`), stage);
    }
    for (const oil of ['answered', 'sources_disagree', 'engine_ambiguous', 'still_running',
      'no_answer']) {
      assert.ok(language.has(`live.why.oil.${oil}`), oil);
    }
  }
});

test('the Georgian lines are Georgian', () => {
  const [line] = texts(alarm({ searched: 1, searches: [catalogue()] }), ka);
  assert.equal(line, 'კატალოგში ძებნა (ნაწილი «სანთელი» · მანქანა «camry» · მარკა «toyota»): '
    + 'ნაწილი 31 ჩანაწერშია (მარაგში 12), მაგრამ არცერთი ამ მანქანას არ ასახელებს.');
});

test('values that are not what the server sends are ignored, never thrown on', () => {
  const odd = { searched: 'x', searches: [null, 7, { tool: 'search_catalog', total: '12',
    car: 'camry', cars: 'no', spellings: [null] }, { tool: 'unknown_tool' }],
  unavailable: [null, 'x'], barred: 'no', limits: 'time', note: 5 };
  assert.doesNotThrow(() => texts(alarm(odd)));
  assert.doesNotThrow(() => stuckCause({ failed_engine: 5, failed_error: {} }, en.t, en.has));
});

let failed = 0;
for (const [name, body] of tests) {
  try {
    body();
    console.log(`ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL ${name}\n${error.stack}`);
  }
}
console.log(`${tests.length - failed} of ${tests.length} passed`);
process.exit(failed ? 1 : 0);
