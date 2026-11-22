// Live chats: the watcher. One poller for the whole dashboard (started by app.js after sign-in),
// so an alarm is heard and counted on every page, not only on the Live page.
//
// It asks GET /api/admin/live/pulse every 3 seconds (10 while the tab is hidden, backing off to
// 30 on errors, once a minute while the server says the feature is not set up). Without the
// Live page open the answer is only counts; when they move, the open alarms are fetched once to
// learn their kind and note. With the Live page open (setDetail) the answer also carries the new
// messages, the chats they belong to and the alarms, and the page subscribes to it.
//
// A new alarm gets a sound, an alarm toast with a link to the chat, and a screen-reader
// announcement; a phone number arriving gets a softer sound; so does a customer who starts
// waiting or writes again while waiting; a reminder ping repeats every minute while anything
// needs a person (an open alarm, a waiting or a stuck chat). A part the bot could not find and
// a chat it hands to staff sound a loud siren that repeats every 15 seconds while the shop is
// open (10:00-19:00, the server's clock), until someone opens that chat on the Live page or
// marks the alarm done. A part not found outside those hours is saved quiet: shown, never heard
// (the server marks it). A poll that hangs for 10 seconds
// counts as offline, and an ended session is shown instead of silencing the alarms (app.js
// shows both on every page). The sound is WebAudio (no audio files, nothing for the CSP to
// allow); a browser lets it play only after the person has used the page, and until then
// `soundLocked` says so, for the page to ask for a click.

import { ApiError, isAbort, leaveForSignIn, request } from './api.js';
import { h, icon, statusIcon } from './dom.js';
import {
  alarmEvents, alertsOfChat, badgeCount, isOpenNow, isQuiet, needsAlerts, newestFirst, nextDelay,
  openTotal, patternOf, reminderCount, sirenAlerts, soundFor, toneOf,
} from './live-model.js';
import { t } from './strings.js';
import { announce } from './ui.js';

const BASE = '/api/admin/live';
const SOUND_KEY = 'garage.staff.alarm_sound';
// The alarms someone opened on the Live page, kept in this browser (every tab of it, and after a
// reload) so the siren does not start again for them. Another computer does not know: there
// only Done stops it.
const SEEN_KEY = 'garage.staff.alarm_seen';
const SEEN_MAX = 200;
const REMINDER_MS = 60000;
const SIREN_MS = 15000; // the siren repeats this often while an alarm waits for a person
// A poll that has not answered in this long counts as a failure (the page says it is offline)
// instead of showing "live" forever on a hung connection.
const PULSE_TIMEOUT_MS = 10000;
const SIGNED_OUT_RETRY_MS = 30000; // signed out but kept on the page (unsaved changes)
const TOAST_MS = { critical: 20000, warning: 14000 };
const SETUP_CODES = { LIVE_NOT_SET_UP: 'not_set_up', LIVE_NOT_CONFIGURED: 'not_configured' };

// Each pattern: a wave, a gain, and either tones [frequency Hz, start s, length s] or one
// sweep, a siren: [from Hz, to Hz, length s] steps played back to back by one oscillator.
// `detune` adds a second oscillator that many cents off, whose beating makes it harsh. All of it
// goes through one compressor, so the loud patterns are as loud as the speakers allow without
// clipping; nothing can be louder than the computer's own volume.
const wail = (low, high, step, times) => Array.from({ length: times * 2 },
  (_, index) => (index % 2 ? [high, low, step] : [low, high, step]));
const SIREN = { gain: 0.85, wave: 'sawtooth', detune: 40, sweep: wail(620, 1500, 0.32, 4) };
const PATTERNS = {
  not_found: SIREN,
  staff: { gain: 0.85, wave: 'square', detune: 30,
    tones: [[960, 0, 0.22], [720, 0.25, 0.22], [960, 0.5, 0.22], [720, 0.75, 0.22],
      [960, 1, 0.22], [720, 1.25, 0.22], [960, 1.5, 0.22], [720, 1.75, 0.3]] },
  visit: { gain: 0.6, wave: 'square',
    tones: [[523, 0, 0.15], [659, 0.18, 0.15], [784, 0.36, 0.28],
      [523, 0.8, 0.15], [659, 0.98, 0.15], [784, 1.16, 0.36]] },
  phone: { gain: 0.4, wave: 'triangle', tones: [[988, 0, 0.16], [1319, 0.2, 0.22]] },
  waiting: { gain: 0.35, wave: 'triangle', tones: [[660, 0, 0.12], [660, 0.16, 0.12]] },
  reminder: { gain: 0.25, wave: 'triangle', tones: [[698, 0, 0.14]] },
  test: SIREN,
};

const state = {
  running: false,
  detail: false, // the Live page is open
  cursor: { m: null, w: null },
  pulse: null, // the last answer's counts
  alerts: null, // open alarms, newest change first; null until known
  done: [], // handled in the last 24 hours (with detail)
  seen: null, // {alerts, waiting, attention} the next answer is compared with
  offline: false,
  signedOut: false, // the session ended: nothing is heard until the person signs in again
  reason: '', // '', not_set_up or not_configured
  failures: 0,
  updatedAt: null,
  soundOn: true,
  soundLocked: true,
  viewing: '', // the chat open on the Live page: its alarms count as seen
  acked: new Set(), // alarm ids someone has seen on the Live page: the siren leaves them be
};
const listeners = new Set();
let timer = null;
let reminder = null;
let controller = null;
let ticking = false;
let again = false;
let audio = null;
let output = null; // the compressor every sound goes through
let ticks = 0;

// -- sound -------------------------------------------------------------------------------------

function readSound() {
  try {
    return window.localStorage.getItem(SOUND_KEY) !== 'off';
  } catch {
    return true; // Storage can be blocked: the sound stays on for this visit.
  }
}

function readAcked() {
  try {
    const kept = JSON.parse(window.localStorage.getItem(SEEN_KEY) || '[]');
    return new Set(Array.isArray(kept) ? kept.filter(Number.isInteger).slice(-SEEN_MAX) : []);
  } catch {
    return new Set(); // Storage blocked or garbled: this visit starts with nothing seen.
  }
}

function writeAcked() {
  try {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify([...state.acked].slice(-SEEN_MAX)));
  } catch {
    // Not remembered, but applied for this visit.
  }
}

/** Another tab of this browser saw an alarm: this one stops calling for it too. */
function onStorage(event) {
  if (event.key === SEEN_KEY) {
    state.acked = readAcked();
  }
}

function writeSound(on) {
  try {
    window.localStorage.setItem(SOUND_KEY, on ? 'on' : 'off');
  } catch {
    // Not remembered, but applied for this visit.
  }
}

function isLocked() {
  return state.soundOn && !(audio && audio.state === 'running');
}

function gestures(listen) {
  for (const type of ['pointerdown', 'keydown']) {
    if (listen) {
      document.addEventListener(type, unlock, true);
    } else {
      document.removeEventListener(type, unlock, true);
    }
  }
}

function syncLocked() {
  const locked = isLocked();
  gestures(locked && state.running);
  if (locked !== state.soundLocked) {
    state.soundLocked = locked;
    emit();
  }
}

function context() {
  if (audio) {
    return audio;
  }
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) {
    return null;
  }
  try {
    audio = new Context();
    audio.addEventListener('statechange', syncLocked);
    output = audio.createDynamicsCompressor();
    output.threshold.value = -8;
    output.knee.value = 4;
    output.ratio.value = 16;
    output.attack.value = 0.002;
    output.release.value = 0.2;
    output.connect(audio.destination);
  } catch {
    audio = null;
    output = null;
  }
  return audio;
}

/** The first click or key press of the visit: a browser lets sound start only after it. */
function unlock() {
  if (!state.soundOn) {
    return;
  }
  const ctx = context();
  if (ctx && ctx.state !== 'running') {
    try {
      const pending = ctx.resume();
      if (pending && typeof pending.then === 'function') {
        pending.then(syncLocked, syncLocked);
      }
    } catch {
      // It stays locked; the page keeps asking for a click.
    }
  }
  syncLocked();
}

function voice(ctx, start, end, gain, wave, detune, shape) {
  const envelope = ctx.createGain();
  envelope.gain.setValueAtTime(0.0001, start);
  envelope.gain.exponentialRampToValueAtTime(gain, start + 0.015);
  envelope.gain.setValueAtTime(gain, Math.max(start + 0.015, end - 0.03));
  envelope.gain.exponentialRampToValueAtTime(0.0001, end);
  envelope.connect(output || ctx.destination);
  for (const cents of detune ? [0, detune] : [0]) {
    const oscillator = ctx.createOscillator();
    oscillator.type = wave;
    oscillator.detune.value = cents;
    shape(oscillator.frequency);
    oscillator.connect(envelope);
    oscillator.start(start);
    oscillator.stop(end + 0.05);
  }
}

function sound(ctx, pattern, origin) {
  const { gain, wave, detune = 0 } = pattern;
  if (pattern.sweep) {
    const length = pattern.sweep.reduce((sum, step) => sum + step[2], 0);
    voice(ctx, origin, origin + length, gain, wave, detune, (frequency) => {
      let at = origin;
      frequency.setValueAtTime(pattern.sweep[0][0], at);
      for (const [, to, step] of pattern.sweep) {
        at += step;
        frequency.linearRampToValueAtTime(to, at);
      }
    });
    return;
  }
  for (const [hz, start, length] of pattern.tones) {
    voice(ctx, origin + start, origin + start + length, gain, wave, detune,
      (frequency) => frequency.setValueAtTime(hz, origin + start));
  }
}

/** 'played', 'off' (muted) or 'blocked' (the browser has not allowed sound yet). */
function play(name) {
  if (!state.soundOn) {
    return 'off';
  }
  const ctx = context();
  if (!ctx) {
    return 'blocked';
  }
  if (ctx.state !== 'running') {
    try {
      ctx.resume();
    } catch {
      // Still locked.
    }
    syncLocked();
    return 'blocked';
  }
  sound(ctx, PATTERNS[name] || PATTERNS.not_found, ctx.currentTime + 0.02);
  return 'played';
}

// -- alarm toasts ------------------------------------------------------------------------------

let box = null;

function raise() {
  if (typeof box.showPopover !== 'function') {
    return;
  }
  try {
    if (box.matches(':popover-open')) {
      box.hidePopover(); // shown again so it sits above a dialog that opened meanwhile
    }
    box.showPopover();
  } catch {
    box.removeAttribute('popover'); // An ordinary fixed element instead.
  }
}

/** A toast with a link to the chat: the ordinary toasts cannot be clicked. */
function alarmToast(text, href, tone_) {
  if (!box) {
    box = h('div', { class: 'toasts toasts--alarm', popover: 'manual' });
    document.body.appendChild(box);
  }
  while (box.children.length >= 3) {
    box.firstChild.remove();
  }
  const leave = (node) => {
    node.classList.remove('toast--in');
    window.setTimeout(() => node.remove(), 220);
  };
  const node = h('div', { class: `toast toast--${tone_}` },
    statusIcon(tone_ === 'critical' ? 'critical' : 'warning', 18),
    h('a', { class: 'toast__link', href, dataset: { link: '' }, text }),
    h('button', { type: 'button', class: 'toast__close', 'aria-label': t('common.close') },
      icon('close', 16)));
  node.querySelector('a').addEventListener('click', () => node.remove());
  node.querySelector('button').addEventListener('click', () => leave(node));
  box.appendChild(node);
  raise();
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    node.classList.add('toast--in');
  }));
  window.setTimeout(() => leave(node), TOAST_MS[tone_]);
}

function describe(event) {
  const alert = event.alert;
  if (event.type === 'new') {
    return {
      text: t('live.toast.new', { kind: t(`live.kind.${alert.kind}`), note: alert.summary }),
      href: `/admin/live/${alert.key}`, tone: toneOf(alert.kind),
    };
  }
  if (event.type === 'phone') {
    return {
      text: t('live.toast.phone', { note: alert.summary }), href: `/admin/live/${alert.key}`,
      tone: 'warning',
    };
  }
  if (event.type === 'update') {
    return {
      text: t('live.toast.update', { note: alert.summary }), href: `/admin/live/${alert.key}`,
      tone: toneOf(alert.kind),
    };
  }
  if (event.type === 'first') {
    return {
      text: t('live.toast.first', { count: event.alerts.length }), href: '/admin/live',
      tone: event.alerts.some((item) => toneOf(item.kind) === 'critical') ? 'critical' : 'warning',
    };
  }
  return {
    text: t(event.type === 'waiting' ? 'live.toast.waiting' : 'live.toast.stuck'),
    href: '/admin/live', tone: 'warning',
  };
}

/** The alarms of the chat open on the Live page count as seen while the tab is in view. */
function ackViewing() {
  if (state.viewing && visible()) {
    let added = false;
    for (const alert of alertsOfChat(state.alerts, state.viewing)) {
      if (!state.acked.has(alert.alert_id)) {
        state.acked.add(alert.alert_id);
        added = true;
      }
    }
    if (added) {
      writeAcked();
    }
  }
}

/** Seen marks kept only for open alarms; a loud change to one (new, the customer again, a phone
 * number) makes the siren call for it again. */
function reack(events) {
  const open = new Set((state.alerts || []).map((alert) => alert.alert_id));
  const before = state.acked.size;
  for (const id of [...state.acked]) {
    if (!open.has(id)) {
      state.acked.delete(id);
    }
  }
  // ('first', the page opening, names no single alarm: what was seen before stays seen.)
  for (const event of events) {
    if (event.alert && !isQuiet(event.alert)) {
      state.acked.delete(event.alert.alert_id);
    }
  }
  if (state.acked.size !== before) {
    writeAcked();
  }
  ackViewing();
}

/** Every 15 seconds: the siren while an alarm still waits for a person during the staff's
 * hours, otherwise a soft ping once a minute while anything else needs one. */
function remind() {
  ticks += 1;
  const loud = sirenAlerts(state.alerts, { acked: state.acked, openNow: isOpenNow(state.pulse) });
  if (loud.length) {
    play(patternOf(loud[0].kind));
    return;
  }
  if (ticks % (REMINDER_MS / SIREN_MS) === 0 && reminderCount(state.pulse, state.alerts) > 0) {
    play('reminder');
  }
}

function react(events) {
  if (!events.length) {
    return;
  }
  // Opening a page sounds only for the alarms nobody in this browser has seen yet.
  const name = soundFor(events.map((event) => (event.type === 'first'
    ? { ...event, alerts: event.alerts.filter((alert) => !state.acked.has(alert.alert_id)) }
    : event)));
  if (name) {
    play(name);
  }
  const shown = events.slice(0, 3).map(describe);
  for (const item of shown) {
    alarmToast(item.text, item.href, item.tone);
  }
  announce(shown.map((item) => item.text).join('. '));
}

// -- polling -----------------------------------------------------------------------------------

function snapshot(extra) {
  return {
    pulse: state.pulse, alerts: state.alerts || [], done: state.done, badge: badgeCount(state.pulse),
    offline: state.offline, signedOut: state.signedOut, reason: state.reason, soundOn: state.soundOn,
    soundLocked: state.soundLocked, updatedAt: state.updatedAt, detail: state.detail,
    messages: [], chats: {}, ...extra,
  };
}

function emit(extra) {
  const value = snapshot(extra);
  for (const listener of [...listeners]) {
    try {
      listener(value);
    } catch (error) {
      window.setTimeout(() => {
        throw error; // A faulty subscriber must not stop the poll; the console still shows it.
      });
    }
  }
}

function visible() {
  return document.visibilityState !== 'hidden';
}

function schedule(ms) {
  window.clearTimeout(timer);
  timer = state.running ? window.setTimeout(tick, ms) : null;
}

async function accept(pulse, detailed, signal) {
  const { alerts: openAlerts, messages, chats, ...counts } = pulse;
  let alerts = state.alerts;
  let extra = {};
  if (detailed && openAlerts) {
    alerts = newestFirst(openAlerts.open || []);
    state.done = openAlerts.done || [];
    state.cursor = { m: pulse.cursor.m, w: pulse.cursor.w };
    extra = { messages: messages || [], chats: chats || {} };
  } else if (openTotal(counts) === 0) {
    alerts = [];
  } else if (alerts === null || needsAlerts(state.pulse, counts)) {
    const answer = await request('GET', `${BASE}/alerts`, {
      query: { status: 'open', limit: 100 }, signal, keepOnSessionEnd: true,
    });
    alerts = newestFirst(answer.alerts || []);
  }
  const next = {
    alerts, waiting: counts.waiting, waiting_at: counts.waiting_at, attention: counts.attention,
  };
  const events = alarmEvents(state.seen, next, { first: state.seen === null });
  state.seen = next;
  state.pulse = counts;
  state.alerts = alerts;
  state.failures = 0;
  state.offline = false;
  state.signedOut = false;
  state.reason = '';
  state.updatedAt = new Date();
  reack(events);
  react(events);
  emit(extra);
}

function failed(error) {
  if (error instanceof ApiError && error.status === 401) {
    // The session ended. Leave for the sign-in page once; a form with unsaved changes may
    // keep the person here, and then every page says the alarms stopped instead of going quiet.
    const first = !state.signedOut;
    state.signedOut = true;
    state.offline = false;
    emit();
    if (first) {
      leaveForSignIn();
    }
    return SIGNED_OUT_RETRY_MS;
  }
  if (error instanceof ApiError && error.status === 503 && SETUP_CODES[error.code]) {
    state.reason = SETUP_CODES[error.code];
    state.offline = false;
    state.failures = 0;
    state.pulse = null;
    emit();
    return nextDelay({ setup: true });
  }
  state.failures += 1;
  state.offline = true;
  state.reason = '';
  emit();
  return nextDelay({ visible: visible(), failures: state.failures });
}

async function tick() {
  timer = null;
  if (!state.running) {
    return;
  }
  if (ticking) {
    again = true;
    return;
  }
  ticking = true;
  const mine = new AbortController();
  controller = mine;
  const detailed = state.detail;
  let delay = nextDelay({ visible: visible() });
  let timedOut = false;
  const deadline = window.setTimeout(() => {
    timedOut = true;
    mine.abort();
  }, PULSE_TIMEOUT_MS);
  try {
    const query = detailed
      ? { detail: 1, after_m: state.cursor.m, after_w: state.cursor.w } : {};
    const pulse = await request('GET', `${BASE}/pulse`, {
      query, signal: mine.signal, keepOnSessionEnd: true,
    });
    if (state.running && !mine.signal.aborted) {
      await accept(pulse, detailed, mine.signal);
    }
  } catch (error) {
    // An aborted poll was replaced on purpose (the page opened or closed): that call decides
    // when the next one happens. One that hung past the deadline is a failure.
    if (timedOut) {
      delay = failed(new ApiError(0, 'TIMEOUT'));
    } else {
      delay = isAbort(error) ? null : failed(error);
    }
  } finally {
    window.clearTimeout(deadline);
    ticking = false;
    if (controller === mine) {
      controller = null;
    }
  }
  if (!state.running) {
    return;
  }
  if (again) {
    again = false;
    schedule(0);
  } else if (delay !== null) {
    schedule(delay);
  }
}

function onVisibility() {
  if (state.running && visible()) {
    ackViewing();
    schedule(0);
  }
}

// -- what the dashboard uses -------------------------------------------------------------------

/** Start polling. Called once after sign-in; safe to call again. */
export function start() {
  if (state.running) {
    return;
  }
  state.running = true;
  state.soundOn = readSound();
  state.failures = 0;
  state.seen = null;
  state.pulse = null;
  state.alerts = null;
  state.soundLocked = isLocked();
  state.acked = readAcked();
  gestures(state.soundLocked);
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('storage', onStorage);
  // The siren every 15 seconds for an alarm nobody has looked at, a soft ping every minute
  // while anything else needs a person: a customer nobody answered, a chat the bot got stuck in.
  ticks = 0;
  reminder = window.setInterval(remind, SIREN_MS);
  schedule(0);
}

export function stop() {
  state.running = false;
  window.clearTimeout(timer);
  window.clearInterval(reminder);
  timer = null;
  reminder = null;
  if (controller) {
    controller.abort();
  }
  gestures(false);
  document.removeEventListener('visibilitychange', onVisibility);
  window.removeEventListener('storage', onStorage);
}

/** The Live page is open (true) or closed (false): the answer then carries messages too. */
export function setDetail(on) {
  const next = Boolean(on);
  if (state.detail === next) {
    return;
  }
  state.detail = next;
  state.cursor = { m: null, w: null }; // the first detailed call starts from the server's cursor
  if (state.running) {
    if (controller) {
      controller.abort();
    }
    schedule(0);
  }
}

/** The chat open on the Live page ('' when none): its alarms stop the siren. */
export function setViewing(key) {
  state.viewing = key || '';
  ackViewing();
}

/** Ask now instead of waiting, after something the person did (an alarm marked done). */
export function refresh() {
  if (state.running) {
    schedule(0);
  }
}

/** fn(snapshot) after every answer and when the offline or sound state changes. Returns a
 * function that unsubscribes. */
export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getState() {
  return snapshot();
}

/** What the nav badge and the tab title show. */
export function badge() {
  return badgeCount(state.pulse);
}

export function isSoundOn() {
  return state.soundOn;
}

export function setSound(on) {
  state.soundOn = Boolean(on);
  writeSound(state.soundOn);
  if (state.soundOn) {
    unlock(); // a click turned it on: the right moment for the browser to allow it
  }
  syncLocked();
  emit();
  return state.soundOn;
}

/** Play the test pattern from a click. Resolves 'played', 'off' or 'blocked'. */
export async function testAlarm() {
  if (!state.soundOn) {
    return 'off';
  }
  const ctx = context();
  if (!ctx) {
    return 'blocked';
  }
  try {
    await ctx.resume();
  } catch {
    // Reported below as blocked when it did not start.
  }
  syncLocked();
  return play('test');
}

// Read once at load so the first render of the shell already knows the person's choice.
state.soundOn = readSound();
