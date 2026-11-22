// Live chats: the pure part. No DOM, no network, no strings, so plain Node can test it
// (tests/admin_live_model_test.mjs). The poller (live-watch.js) and the page (views/live.js)
// build on these: what changed between two answers of the server, which sound a change
// deserves, how messages and conversations merge, how long to wait before the next poll.

export const KINDS = ['part_not_found', 'visit_request', 'service_booking', 'needs_staff'];
export const FILTERS = ['all', 'alerts', 'waiting', 'off'];
export const PHONE = /^\+?[0-9]{7,15}$/;
export const MESSAGE_CAP = 400; // messages one open conversation keeps in memory

export const VISIBLE_MS = 3000;
export const HIDDEN_MS = 10000;
export const BACKOFF_MAX_MS = 30000;
export const SETUP_MS = 60000; // the server says the feature is not set up: ask rarely

/** Only a number the server would also accept becomes a tel: link. */
export function isPhone(value) {
  return typeof value === 'string' && PHONE.test(value);
}

/** critical (red) for what the shop must act on at once, warning (amber) for a customer
 * who wants to come in. */
export function toneOf(kind) {
  return kind === 'visit_request' || kind === 'service_booking' ? 'warning' : 'critical';
}

/** The sound of a kind: not_found (two falling tones), visit (three rising), staff (urgent). */
export function patternOf(kind) {
  if (kind === 'visit_request' || kind === 'service_booking') {
    return 'visit';
  }
  return kind === 'needs_staff' ? 'staff' : 'not_found';
}

/** The kinds whose alarm is a siren that repeats until someone opens the chat on the Live page
 * or marks the alarm done: a part the bot could not find, a chat the bot hands to staff. */
export const SIREN_KINDS = ['part_not_found', 'needs_staff'];

/** An alarm the server saved without a sound: a part not found outside the staff's hours
 * (10:00-19:00 Tbilisi, judged by the server). It is shown, never heard. */
export function isQuiet(alert) {
  return Boolean(alert && alert.quiet);
}

/** Whether the server says the shop is open (its clock, 10:00-19:00 Tbilisi). A server that
 * does not send the field cannot tell night from day, so the siren never repeats for it. */
export function isOpenNow(pulse) {
  return Boolean(pulse && pulse.open_now === true);
}

/**
 * The open alarms the siren keeps sounding for: siren kinds, not quiet, not yet seen on the
 * Live page (`acked`: alarm ids), and none at all while the shop is closed (`openNow` false),
 * so an alarm left open at closing time does not ring all night.
 */
export function sirenAlerts(alerts, { acked = new Set(), openNow = true } = {}) {
  if (!openNow) {
    return [];
  }
  return newestFirst((alerts || []).filter((alert) => SIREN_KINDS.includes(alert.kind)
    && !isQuiet(alert) && !acked.has(alert.alert_id)));
}

const asCount = (value) => (Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0);

export function openTotal(pulse) {
  return pulse && pulse.open ? asCount(pulse.open.total) : 0;
}

/** What the nav badge and the tab title show: open alarms, plus chats waiting for staff,
 * plus chats the bot got stuck in. */
export function badgeCount(pulse) {
  return pulse ? openTotal(pulse) + asCount(pulse.waiting) + asCount(pulse.attention) : 0;
}

/** What the soft once-a-minute reminder counts: the badge without the quiet alarms. */
export function reminderCount(pulse, alerts) {
  const silent = (alerts || []).filter(isQuiet).length;
  return Math.max(0, badgeCount(pulse) - silent);
}

function stampOf(alert) {
  return Date.parse(alert.updated_at || alert.created_at || '') || 0;
}

/** Newest change first. */
export function newestFirst(alerts) {
  return [...(alerts || [])].sort((a, b) => stampOf(b) - stampOf(a) || b.alert_id - a.alert_id);
}

export function alertsOfChat(alerts, key) {
  return (alerts || []).filter((alert) => alert.key === key);
}

/**
 * What changed between two lists of open alarms (by alert_id): `added` are new,
 * `updated` have a phone number newly set (or a different one) or a new note (more
 * occurrences, another summary), `closed` are gone from the open list.
 */
export function diffAlerts(prev, next) {
  const before = new Map((prev || []).map((alert) => [alert.alert_id, alert]));
  const after = new Map((next || []).map((alert) => [alert.alert_id, alert]));
  const added = [];
  const updated = [];
  const closed = [];
  for (const alert of after.values()) {
    const old = before.get(alert.alert_id);
    if (!old) {
      added.push(alert);
      continue;
    }
    const phone = Boolean(alert.phone) && alert.phone !== old.phone;
    const note = asCount(alert.occurrences) > asCount(old.occurrences)
      || alert.summary !== old.summary;
    // A quiet alarm the customer raised again during the staff's hours: it sounds now.
    const woke = isQuiet(old) && !isQuiet(alert);
    if (phone || note || woke) {
      updated.push({ alert, previous: old, phone, note, woke });
    }
  }
  for (const alert of before.values()) {
    if (!after.has(alert.alert_id)) {
      closed.push(alert);
    }
  }
  return { added, updated, closed };
}

/** When the newest message of a waiting chat arrived (the pulse's `waiting_at`), 0 if none. */
export function waitingStamp(snap) {
  return (snap && Date.parse(snap.waiting_at || '')) || 0;
}

/**
 * The events a new answer holds, from two snapshots {alerts, waiting, waiting_at, attention}. `first`:
 * there is no earlier snapshot (the page was just opened), so open alarms count as one
 * event instead of one each.
 */
export function alarmEvents(before, after, { first = false } = {}) {
  const now = after || { alerts: [], waiting: 0, attention: 0 };
  if (first || !before) {
    return now.alerts.length ? [{ type: 'first', alerts: newestFirst(now.alerts) }] : [];
  }
  const events = [];
  const { added, updated } = diffAlerts(before.alerts, now.alerts);
  for (const alert of newestFirst(added)) {
    events.push({ type: 'new', alert });
  }
  for (const item of updated) {
    let type = item.phone ? 'phone' : 'update';
    if (item.woke) {
      type = 'new';
    }
    events.push({ type, alert: item.alert });
  }
  // A chat that starts waiting, or a new message in one that already waits (the newest waiting
  // message moved although the count did not).
  const newer = asCount(now.waiting) > 0 && waitingStamp(now) > waitingStamp(before);
  if (asCount(now.waiting) > asCount(before.waiting) || newer) {
    events.push({ type: 'waiting', count: asCount(now.waiting) });
  }
  if (asCount(now.attention) > asCount(before.attention)) {
    events.push({ type: 'stuck', count: asCount(now.attention) });
  }
  return events;
}

const PRIORITY = { new: 4, first: 4, phone: 3, update: 2, waiting: 1, stuck: 1 };

/** An event that may be heard: none about a quiet alarm, a 'first' only for its loud alarms. */
function audible(event) {
  if (event.alert) {
    return !isQuiet(event.alert);
  }
  if (event.alerts) {
    return event.alerts.some((alert) => !isQuiet(alert));
  }
  return true;
}

/** The one sound a poll deserves: the most urgent event decides, however many there are.
 * Quiet alarms (a part not found outside the staff's hours) are never heard. */
export function soundFor(events) {
  let best = null;
  for (const event of events || []) {
    if (!audible(event)) {
      continue;
    }
    const rank = PRIORITY[event.type] || 0;
    if (!best || rank > best.rank) {
      best = { rank, event };
    }
  }
  if (!best) {
    return null;
  }
  const { event } = best;
  if (event.type === 'new') {
    return patternOf(event.alert.kind);
  }
  if (event.type === 'first') {
    const loud = event.alerts.filter((alert) => !isQuiet(alert));
    const urgent = loud.find((alert) => toneOf(alert.kind) === 'critical');
    return patternOf((urgent || loud[0]).kind);
  }
  if (event.type === 'phone') {
    return 'phone';
  }
  return 'waiting';
}

/** Whether the alarm list has to be asked for: the first answer, or a change the counts show. */
export function needsAlerts(prev, next) {
  if (!prev || !next) {
    return true;
  }
  return openTotal(prev) !== openTotal(next) || (prev.stamp || '') !== (next.stamp || '');
}

// -- messages and conversations --------------------------------------------------------------

function idNumber(id) {
  return Number(String(id || '').slice(1)) || 0;
}

function compareMessages(a, b) {
  return (Date.parse(a.at) || 0) - (Date.parse(b.at) || 0)
    || String(a.id)[0].localeCompare(String(b.id)[0]) || idNumber(a.id) - idNumber(b.id);
}

/**
 * ``incoming`` merged into ``current``: one entry per message id (a repeat, such as the
 * server's re-read of the last ids, replaces), in time order, the newest ``cap`` kept.
 * `added` are the messages that were not there before.
 */
export function mergeMessages(current, incoming, cap = MESSAGE_CAP) {
  const byId = new Map((current || []).map((message) => [message.id, message]));
  const added = [];
  for (const message of incoming || []) {
    if (!byId.has(message.id)) {
      added.push(message);
    }
    byId.set(message.id, message);
  }
  const list = [...byId.values()].sort(compareMessages);
  return { list: list.length > cap ? list.slice(list.length - cap) : list, added };
}

/** Messages of one conversation. */
export function messagesOfChat(messages, key) {
  return (messages || []).filter((message) => message.key === key);
}

/** Whether a conversation belongs on the list under this filter. */
export function chatMatches(chat, filter) {
  if (filter === 'alerts') {
    return Boolean((chat.alerts && chat.alerts.length) || chat.needs_attention);
  }
  if (filter === 'waiting') {
    return Boolean(chat.waiting);
  }
  if (filter === 'off') {
    return !chat.bot_enabled;
  }
  return true;
}

/** Most recently active first. */
export function sortChats(chats) {
  return [...(chats || [])].sort((a, b) => (Date.parse(b.last_at) || 0) - (Date.parse(a.last_at) || 0)
    || String(a.key).localeCompare(String(b.key)));
}

/**
 * The list with the summaries of the chats a poll touched applied: a summary replaces its
 * row (or joins the list), and one that no longer fits the filter leaves it.
 */
export function mergeChats(list, summaries, filter = 'all', limit = 150) {
  const byKey = new Map((list || []).map((chat) => [chat.key, chat]));
  for (const chat of Object.values(summaries || {})) {
    if (chatMatches(chat, filter)) {
      byKey.set(chat.key, chat);
    } else {
      byKey.delete(chat.key);
    }
  }
  return sortChats([...byKey.values()]).slice(0, limit);
}

/** A string that changes when what a region shows changes: the page redraws only then. */
export function signature(value) {
  return JSON.stringify(value);
}

// -- polling ---------------------------------------------------------------------------------

/** 3 s, 6 s, 12 s, 24 s, then 30 s after the n-th failure in a row. */
export function backoff(failures) {
  return Math.min(BACKOFF_MAX_MS, VISIBLE_MS * 2 ** Math.max(0, failures - 1));
}

/** Milliseconds until the next poll. */
export function nextDelay({ visible = true, failures = 0, setup = false } = {}) {
  if (setup) {
    return SETUP_MS;
  }
  if (failures > 0) {
    return backoff(failures);
  }
  return visible ? VISIBLE_MS : HIDDEN_MS;
}
