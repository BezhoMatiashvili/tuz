// Tests of the pure part of the Live chats page: what changed between two answers, which
// sound it deserves, how messages and conversations merge, how long to wait to poll again.
//
//   node tests/admin_live_model_test.mjs
//
// live-model.js has no DOM, no network and no strings, so plain Node runs it.

import assert from 'node:assert/strict';

import {
  alarmEvents,
  alertsOfChat,
  backoff,
  badgeCount,
  chatMatches,
  diffAlerts,
  isPhone,
  mergeChats,
  mergeMessages,
  messagesOfChat,
  needsAlerts,
  newestFirst,
  nextDelay,
  openTotal,
  patternOf,
  signature,
  sortChats,
  soundFor,
  toneOf,
} from '../automotive/static/admin/js/live-model.js';

const tests = [];
const test = (name, body) => tests.push([name, body]);

const at = (minutes) => new Date(Date.UTC(2026, 10, 16, 12, minutes)).toISOString();
const alert = (id, extra = {}) => ({
  alert_id: id, key: `m-${id}`, kind: 'part_not_found', summary: 'front pads', occurrences: 1,
  phone: null, updated_at: at(id), created_at: at(id), ...extra,
});
const message = (id, minutes, extra = {}) => ({
  id, key: 'm-1', sender: 'customer', body: id, at: at(minutes), ...extra,
});
const chat = (key, minutes, extra = {}) => ({
  key, last_at: at(minutes), bot_enabled: true, waiting: false, needs_attention: false,
  alerts: [], ...extra,
});

test('a phone number is a tel: link only when the server would accept it', () => {
  for (const good of ['599123456', '+995599123456', '1234567', '123456789012345']) {
    assert.equal(isPhone(good), true, good);
  }
  for (const bad of ['', '12345', '1234567890123456', '+', '599 123 456', 'tel:599123456',
    '599123456;rm', null, undefined, 599123456]) {
    assert.equal(isPhone(bad), false, String(bad));
  }
});

test('kinds have a tone and a sound', () => {
  assert.deepEqual(['part_not_found', 'visit_request', 'service_booking', 'needs_staff']
    .map(toneOf), ['critical', 'warning', 'warning', 'critical']);
  assert.deepEqual(['part_not_found', 'visit_request', 'service_booking', 'needs_staff']
    .map(patternOf), ['not_found', 'visit', 'visit', 'staff']);
  assert.equal(patternOf('something_new'), 'not_found');
});

test('the badge counts open alarms, waiting chats and stuck chats', () => {
  assert.equal(badgeCount(null), 0);
  assert.equal(badgeCount({}), 0);
  assert.equal(badgeCount({ open: { total: 2 }, waiting: 1, attention: 3 }), 6);
  assert.equal(badgeCount({ open: { total: '4' }, waiting: 'x', attention: -2 }), 4);
  assert.equal(openTotal({ open: { total: 5 } }), 5);
  assert.equal(openTotal(undefined), 0);
});

test('alarms are listed newest change first', () => {
  const list = [alert(1), alert(3), alert(2, { updated_at: at(30) })];
  assert.deepEqual(newestFirst(list).map((item) => item.alert_id), [2, 3, 1]);
  assert.deepEqual(list.map((item) => item.alert_id), [1, 3, 2]); // the input is untouched
  assert.deepEqual(alertsOfChat(list, 'm-3').map((item) => item.alert_id), [3]);
});

test('diffAlerts finds new, updated and closed alarms', () => {
  const before = [alert(1), alert(2), alert(3), alert(4)];
  const after = [
    alert(1),
    alert(2, { phone: '599123456' }),
    alert(3, { occurrences: 2 }),
    alert(5),
    alert(4, { summary: 'oil filter' }),
  ];
  const { added, updated, closed } = diffAlerts(before, after);
  assert.deepEqual(added.map((item) => item.alert_id), [5]);
  assert.deepEqual(closed, []);
  assert.deepEqual(updated.map((item) => [item.alert.alert_id, item.phone, item.note]),
    [[2, true, false], [3, false, true], [4, false, true]]);
  assert.deepEqual(diffAlerts(after, before).closed.map((item) => item.alert_id), [5]);
  assert.deepEqual(diffAlerts(before, before), { added: [], updated: [], closed: [] });
  assert.deepEqual(diffAlerts(undefined, undefined), { added: [], updated: [], closed: [] });
});

test('a number that is the same as before is not news', () => {
  const same = diffAlerts([alert(1, { phone: '599123456' })], [alert(1, { phone: '599123456' })]);
  assert.deepEqual(same.updated, []);
  const changed = diffAlerts([alert(1, { phone: '599123456' })], [alert(1, { phone: '599123499' })]);
  assert.equal(changed.updated[0].phone, true);
});

test('the first answer is one event, however many alarms are open', () => {
  const none = alarmEvents(null, { alerts: [], waiting: 4, attention: 1 }, { first: true });
  assert.deepEqual(none, []);
  const events = alarmEvents(null, { alerts: [alert(1), alert(2)], waiting: 0, attention: 0 },
    { first: true });
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'first');
  assert.deepEqual(events[0].alerts.map((item) => item.alert_id), [2, 1]);
  assert.deepEqual(alarmEvents(undefined, { alerts: [alert(1)] }).map((item) => item.type), ['first']);
});

test('later answers give new, phone, update, waiting and stuck events', () => {
  const before = { alerts: [alert(1), alert(2)], waiting: 1, attention: 0 };
  const after = {
    alerts: [alert(1, { phone: '599123456' }), alert(2, { occurrences: 2 }), alert(3), alert(4)],
    waiting: 2,
    attention: 1,
  };
  const events = alarmEvents(before, after);
  assert.deepEqual(events.map((item) => item.type),
    ['new', 'new', 'phone', 'update', 'waiting', 'stuck']);
  assert.deepEqual(events.filter((item) => item.type === 'new').map((item) => item.alert.alert_id),
    [4, 3]);
  assert.deepEqual(alarmEvents(after, after), []);
  // Fewer waiting chats or fewer alarms is not an alarm.
  assert.deepEqual(alarmEvents(after, { alerts: [], waiting: 0, attention: 0 }), []);
});

test('one sound per poll, the most urgent event decides', () => {
  assert.equal(soundFor([]), null);
  assert.equal(soundFor(null), null);
  assert.equal(soundFor([{ type: 'waiting' }, { type: 'update', alert: alert(1) }]), 'waiting');
  assert.equal(soundFor([{ type: 'stuck' }]), 'waiting');
  assert.equal(soundFor([{ type: 'phone', alert: alert(1) }, { type: 'waiting' }]), 'phone');
  assert.equal(soundFor([{ type: 'phone', alert: alert(1) },
    { type: 'new', alert: alert(2, { kind: 'visit_request' }) }]), 'visit');
  assert.equal(soundFor([{ type: 'new', alert: alert(2, { kind: 'needs_staff' }) }]), 'staff');
  const first = { type: 'first', alerts: [alert(1, { kind: 'visit_request' }), alert(2)] };
  assert.equal(soundFor([first]), 'not_found'); // a red alarm outranks an amber one
  assert.equal(soundFor([{ type: 'first', alerts: [alert(1, { kind: 'service_booking' })] }]),
    'visit');
});

test('the alarm list is fetched on the first answer and when the counts or the stamp move', () => {
  const pulse = { open: { total: 1 }, stamp: at(1) };
  assert.equal(needsAlerts(null, pulse), true);
  assert.equal(needsAlerts(pulse, pulse), false);
  assert.equal(needsAlerts(pulse, { ...pulse, stamp: at(2) }), true);
  assert.equal(needsAlerts(pulse, { open: { total: 2 }, stamp: at(1) }), true);
  assert.equal(needsAlerts({ open: { total: 0 }, stamp: null }, { open: { total: 0 }, stamp: null }),
    false);
});

test('messages merge by id, in time order, and report what was new', () => {
  const current = [message('m1', 1), message('m3', 3)];
  const { list, added } = mergeMessages(current, [message('m3', 3, { body: 'again' }),
    message('m2', 2), message('w1', 2)]);
  assert.deepEqual(list.map((item) => item.id), ['m1', 'm2', 'w1', 'm3']);
  assert.equal(list.find((item) => item.id === 'm3').body, 'again');
  assert.deepEqual(added.map((item) => item.id), ['m2', 'w1']);
  assert.deepEqual(current.map((item) => item.id), ['m1', 'm3']); // the input is untouched
  assert.deepEqual(mergeMessages(undefined, undefined), { list: [], added: [] });
});

test('messages in the same minute keep their id order and a cap drops the oldest', () => {
  const same = mergeMessages([], [message('m10', 1), message('m9', 1), message('m11', 1)]).list;
  assert.deepEqual(same.map((item) => item.id), ['m9', 'm10', 'm11']);
  const many = Array.from({ length: 12 }, (_, index) => message(`m${index + 1}`, index));
  const capped = mergeMessages([], many, 5);
  assert.deepEqual(capped.list.map((item) => item.id), ['m8', 'm9', 'm10', 'm11', 'm12']);
  assert.equal(capped.added.length, 12);
  assert.deepEqual(messagesOfChat([message('m1', 1), message('m2', 2, { key: 'm-2' })], 'm-2')
    .map((item) => item.id), ['m2']);
});

test('a conversation belongs on the list by its filter', () => {
  assert.equal(chatMatches(chat('m-1', 1), 'all'), true);
  assert.equal(chatMatches(chat('m-1', 1), 'alerts'), false);
  assert.equal(chatMatches(chat('m-1', 1, { alerts: ['visit_request'] }), 'alerts'), true);
  assert.equal(chatMatches(chat('m-1', 1, { needs_attention: true }), 'alerts'), true);
  assert.equal(chatMatches(chat('m-1', 1, { waiting: true }), 'waiting'), true);
  assert.equal(chatMatches(chat('m-1', 1), 'waiting'), false);
  assert.equal(chatMatches(chat('m-1', 1, { bot_enabled: false }), 'off'), true);
  assert.equal(chatMatches(chat('m-1', 1), 'off'), false);
});

test('conversations sort by their last message, newest first', () => {
  const list = [chat('m-2', 2), chat('w-1', 9), chat('m-1', 2)];
  assert.deepEqual(sortChats(list).map((item) => item.key), ['w-1', 'm-1', 'm-2']);
  assert.deepEqual(list.map((item) => item.key), ['m-2', 'w-1', 'm-1']);
  assert.deepEqual(sortChats(undefined), []);
});

test('a poll updates the list: replaced rows, new rows, rows that leave the filter', () => {
  const list = [chat('m-1', 1, { last_body: 'old' }), chat('m-2', 2)];
  const next = mergeChats(list, {
    'm-1': chat('m-1', 5, { last_body: 'new' }),
    'm-3': chat('m-3', 4),
  });
  assert.deepEqual(next.map((item) => item.key), ['m-1', 'm-3', 'm-2']);
  assert.equal(next[0].last_body, 'new');
  const off = mergeChats([chat('m-1', 1, { bot_enabled: false })], {
    'm-1': chat('m-1', 5, { bot_enabled: true }),
    'm-2': chat('m-2', 6, { bot_enabled: false }),
  }, 'off');
  assert.deepEqual(off.map((item) => item.key), ['m-2']);
  assert.equal(mergeChats(list, { 'm-9': chat('m-9', 9) }, 'all', 2).length, 2);
  assert.deepEqual(mergeChats(undefined, undefined), []);
});

test('a signature changes only when the data does', () => {
  assert.equal(signature([alert(1)]), signature([alert(1)]));
  assert.notEqual(signature([alert(1)]), signature([alert(1, { phone: '599123456' })]));
});

test('polling slows down on failures and after a "not set up" answer', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map(backoff), [3000, 3000, 6000, 12000, 24000, 30000, 30000]);
  assert.equal(nextDelay({}), 3000);
  assert.equal(nextDelay({ visible: false }), 10000);
  assert.equal(nextDelay({ failures: 3 }), 12000);
  assert.equal(nextDelay({ failures: 3, visible: false }), 12000);
  assert.equal(nextDelay({ setup: true, failures: 2 }), 60000);
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
