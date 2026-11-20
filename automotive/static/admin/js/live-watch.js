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
// needs a person (an open alarm, a waiting or a stuck chat). A poll that hangs for 10 seconds
// counts as offline, and an ended session is shown instead of silencing the alarms (app.js
// shows both on every page). The sound is WebAudio (no audio files, nothing for the CSP to
// allow); a browser lets it play only after the person has used the page, and until then
// `soundLocked` says so, for the page to ask for a click.

import { ApiError, isAbort, leaveForSignIn, request } from './api.js';
import { h, icon, statusIcon } from './dom.js';
import {
  alarmEvents, badgeCount, needsAlerts, newestFirst, nextDelay, openTotal, soundFor, toneOf,
} from './live-model.js';
import { t } from './strings.js';
import { announce } from './ui.js';

const BASE = '/api/admin/live';
const SOUND_KEY = 'garage.staff.alarm_sound';
const REMINDER_MS = 60000;
// A poll that has not answered in this long counts as a failure (the page says it is offline)
// instead of showing "live" forever on a hung connection.
const PULSE_TIMEOUT_MS = 10000;
const SIGNED_OUT_RETRY_MS = 30000; // signed out but kept on the page (unsaved changes)
const TOAST_MS = { critical: 20000, warning: 14000 };
const SETUP_CODES = { LIVE_NOT_SET_UP: 'not_set_up', LIVE_NOT_CONFIGURED: 'not_configured' };

// Each pattern: tones as [frequency Hz, start s, length s], a gain, and a wave.
const PATTERNS = {
  not_found: { gain: 0.24, wave: 'sine', tones: [[880, 0, 0.2], [587, 0.24, 0.32]] },
  visit: { gain: 0.22, wave: 'sine', tones: [[523, 0, 0.15], [659, 0.18, 0.15], [784, 0.36, 0.28]] },
  staff: { gain: 0.26, wave: 'triangle', tones: [[740, 0, 0.13], [740, 0.2, 0.13]] },
  phone: { gain: 0.14, wave: 'sine', tones: [[988, 0, 0.16]] },
  waiting: { gain: 0.15, wave: 'sine', tones: [[660, 0, 0.11], [660, 0.15, 0.11]] },
  reminder: { gain: 0.1, wave: 'sine', tones: [[698, 0, 0.13]] },
  test: { gain: 0.22, wave: 'sine', tones: [[523, 0, 0.15], [659, 0.18, 0.15], [784, 0.36, 0.28]] },
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
};
const listeners = new Set();
let timer = null;
let reminder = null;
let controller = null;
let ticking = false;
let again = false;
let audio = null;

// -- sound -------------------------------------------------------------------------------------

function readSound() {
  try {
    return window.localStorage.getItem(SOUND_KEY) !== 'off';
  } catch {
    return true; // Storage can be blocked: the sound stays on for this visit.
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
  } catch {
    audio = null;
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

function tone(ctx, start, frequency, length, gain, wave) {
  const oscillator = ctx.createOscillator();
  const envelope = ctx.createGain();
  oscillator.type = wave;
  oscillator.frequency.value = frequency;
  envelope.gain.setValueAtTime(0.0001, start);
  envelope.gain.exponentialRampToValueAtTime(gain, start + 0.02);
  envelope.gain.exponentialRampToValueAtTime(0.0001, start + length);
  oscillator.connect(envelope);
  envelope.connect(ctx.destination);
  oscillator.start(start);
  oscillator.stop(start + length + 0.05);
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
  const pattern = PATTERNS[name] || PATTERNS.not_found;
  const origin = ctx.currentTime + 0.02;
  for (const [frequency, start, length] of pattern.tones) {
    tone(ctx, origin + start, frequency, length, pattern.gain, pattern.wave);
  }
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

function react(events) {
  if (!events.length) {
    return;
  }
  const name = soundFor(events);
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
  gestures(state.soundLocked);
  document.addEventListener('visibilitychange', onVisibility);
  // Every minute while anything needs a person: an open alarm, a customer nobody answered, a
  // chat the bot got stuck in.
  reminder = window.setInterval(() => {
    if (badgeCount(state.pulse) > 0) {
      play('reminder');
    }
  }, REMINDER_MS);
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
