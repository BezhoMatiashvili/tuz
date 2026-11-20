// Live chats: the alarms for the staff and the conversations of both channels (Messenger and
// the website chat), for both roles. Alarms on top (a part the bot could not find, a customer
// who wants to visit or book service, a bot that needs a human), then the conversation list and
// the open conversation, at /admin/live/<key> (m-<psid> or w-<website visitor>).
//
// The poll itself is live-watch.js (one for the whole dashboard); this page subscribes to it, so
// what it shows moves without a request of its own: alarms, new messages, the chats they belong
// to. Its own requests are the conversation list, one conversation, and the few things a person
// does here: mark an alarm done, switch the bot off for one customer, clear a stuck chat. The page
// is drawn in regions (header, banners, alarms, list, detail, thread) that are redrawn alone and
// only when their data changed, so focus and the scroll position of a thread survive a poll.

import { ApiError, describeError, isAbort, request } from '../api.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import { dateTime, price, relativeTime } from '../format.js';
import {
  FILTERS, alertsOfChat, isPhone, mergeChats, mergeMessages, newestFirst, signature, toneOf,
} from '../live-model.js';
import * as watch from '../live-watch.js';
import { has, t } from '../strings.js';
import {
  busy, button, card, emptyState, errorState, field, iconButton, loadingBlock, notice, pageHead,
  select, skeleton, switchControl, toast,
} from '../ui.js';
import { label, lines } from './facebook-shared.js';

const BASE = '/api/admin/live';
const PAGE = 40;
const MESSAGES = 50;
const FRESH_MS = 6000; // a new alarm is marked this long
const TIMES_MS = 30000; // "3 minutes ago" is redrawn this often
const KEY = /^[mw]-[A-Za-z0-9]{1,40}$/;
const part = (value) => encodeURIComponent(String(value));

const liveApi = {
  chats: (query, signal) => request('GET', `${BASE}/chats`, { query, signal }),
  chat: (key, query, signal) => request('GET', `${BASE}/chats/${part(key)}`, { query, signal }),
  setAlert: (id, status, version) => request('PUT', `${BASE}/alerts/${id}`,
    { body: { status, version } }),
  setBot: (key, enabled, version) => request('PUT', `${BASE}/chats/${part(key)}/bot`,
    { body: version ? { enabled, version } : { enabled } }),
  clearAttention: (key) => request('POST', `${BASE}/chats/${part(key)}/attention/clear`),
  // ``seenId``: the newest stored message the screen showed; one stored after it keeps the
  // chat waiting, whatever its send time.
  dismissWaiting: (key, seenId) => request('POST', `${BASE}/chats/${part(key)}/waiting/dismiss`,
    { body: seenId ? { seen_id: seenId } : {} }),
};

// The section's own error codes; everything else is described by api.js.
const CODES = {
  LIVE_NOT_SET_UP: 'live.not_set_up',
  LIVE_NOT_CONFIGURED: 'live.not_configured',
  LIVE_UNAVAILABLE: 'live.err.unavailable',
  ALREADY_OPEN: 'live.already_open',
  VERSION_CONFLICT: 'live.conflict',
};
const SETUP = { LIVE_NOT_SET_UP: 'not_set_up', LIVE_NOT_CONFIGURED: 'not_configured' };
// The availability the website chat's cards carried, as the dashboard words it.
const AVAILABILITY = {
  IN_STORE: 'in_stock', WITHIN_1_2_HOURS: 'within_1_2_hours', UNAVAILABLE: 'unavailable',
  UNKNOWN: 'unknown',
};

function describe(error) {
  return error instanceof ApiError && CODES[error.code] ? t(CODES[error.code])
    : describeError(error);
}

function setupOf(error) {
  return error instanceof ApiError && error.status === 503 ? SETUP[error.code] || '' : '';
}

function isConflict(error) {
  return error instanceof ApiError && error.status === 409 && error.code === 'VERSION_CONFLICT';
}

/** A customer's name as the dashboard shows it, for an alarm or a conversation. */
function nameOf(item) {
  if (item.channel === 'web') {
    return t('live.visitor', { tail: item.tail });
  }
  return item.name || t('live.customer', { tail: item.tail });
}

function chip(text, tone) {
  return h('span', { class: ['chip', tone && `chip--${tone}`], text });
}

function glyph(kind) {
  return statusIcon(toneOf(kind) === 'critical' ? 'critical' : 'warning', 16);
}

function kindText(kind) {
  return has(`live.kind.${kind}`) ? t(`live.kind.${kind}`) : String(kind);
}

function nearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 80;
}

/** What one card of the website bot's answer said: brand, name, price, availability. */
function offeredText(item) {
  const name = [item.brand, item.name].filter(Boolean).join(' ');
  const cost = typeof item.price === 'number' && item.price > 0 ? price(item.price) : '';
  const key = AVAILABILITY[String(item.status || '').toUpperCase()];
  const stock = key ? t(`live.avail.${key}`) : String(item.availability || '');
  return [name, cost, stock].filter(Boolean).join(' · ');
}

function bubble(message) {
  const types = (Array.isArray(message.attachments) ? message.attachments : [])
    .map((type) => label('fb.attachment', type));
  const offered = (Array.isArray(message.offered) ? message.offered : [])
    .filter((item) => item && typeof item === 'object');
  return h('li', { class: ['bubble', `bubble--${message.sender}`] },
    h('p', { class: 'bubble__meta' },
      h('strong', { text: label('fb.sender', message.sender) }),
      h('time', { datetime: message.at, text: dateTime(message.at) })),
    message.body ? h('p', { class: 'bubble__text' }, lines(message.body)) : null,
    types.length ? h('p', { class: 'bubble__meta',
      text: t('fb.chat.attachments', { types: types.join(', ') }) }) : null,
    offered.length ? h('ul', { class: 'bubble__offered', 'aria-label': t('live.offered') },
      offered.map((item) => h('li', { text: offeredText(item) }))) : null);
}

export function mountView(root, ctx, params) {
  const state = {
    status: 'loading', // the conversation list: loading, ready or error
    error: null,
    setup: '', // not_set_up or not_configured: the server cannot serve this page yet
    filter: 'all',
    limit: PAGE,
    chats: [],
    snap: watch.getState(), // alarms, handled alarms, counts, offline and sound state
    stuck: [], // Messenger chats the bot got stuck in
    stuckFor: -1, // the count of stuck chats the list was read for
    key: '',
    detail: null,
    doneOpen: false,
    fresh: new Set(), // alarms that arrived while the page was open
    working: new Set(), // alarms with a request running
    pending: new Map(), // chat key -> the bot switch position a request is changing it to
    focusTitle: false,
  };
  const el = {};
  const signatures = {};
  const timers = new Set();
  let alive = true;
  let listController = null;
  let chatController = null;
  let stuckController = null;
  let unsubscribe = null;
  let pressing = false; // a pointer is down: a redraw now could swallow its click
  let pressTimer = null;
  const deferred = new Map(); // redraws waiting for the pointer to go up: render -> focus key

  /**
   * Draw, then put keyboard focus back on the control with the same data-key. While a pointer
   * is pressed the draw waits for its release: a control replaced between the press and the
   * release of a click never gets the click, and a text selection would be lost.
   */
  function keep(render, wanted) {
    if (pressing) {
      deferred.set(render, wanted || deferred.get(render) || '');
      return;
    }
    const active = document.activeElement;
    const key = wanted || (active instanceof HTMLElement && root.contains(active)
      ? active.dataset.key : '');
    render();
    if (key) {
      const again = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again instanceof HTMLElement && again !== document.activeElement) {
        again.focus({ preventScroll: true });
      }
    }
  }

  function later(callback, ms) {
    const id = window.setTimeout(() => {
      timers.delete(id);
      callback();
    }, ms);
    timers.add(id);
  }

  function release() {
    window.clearTimeout(pressTimer);
    pressing = false;
    if (deferred.size && alive) {
      later(() => {
        const jobs = [...deferred];
        deferred.clear();
        for (const [render, wanted] of jobs) {
          keep(render, wanted);
        }
      }, 0);
    }
  }

  function press() {
    pressing = true;
    window.clearTimeout(pressTimer);
    pressTimer = window.setTimeout(release, 1500); // a press whose release never arrives
  }

  // -- header and banners ----------------------------------------------------------------------

  async function runTest(event) {
    const trigger = event.currentTarget;
    const restore = busy(trigger);
    const result = await watch.testAlarm();
    restore();
    toast(t(result === 'played' ? 'live.test_played'
      : result === 'off' ? 'live.test_off' : 'live.test_blocked'),
    result === 'played' ? 'good' : 'critical');
  }

  function drawTools() {
    const { snap } = state;
    const sound = switchControl({
      checked: snap.soundOn, label: t('live.sound'),
      onChange: async (on) => {
        watch.setSound(on);
        toast(t(on ? 'live.sound_toast.on' : 'live.sound_toast.off'));
      },
    });
    sound.input.dataset.key = 'sound';
    const test = button(t('live.test'), { small: true, iconName: 'bell', onClick: runTest });
    test.dataset.key = 'test';
    mount(el.tools,
      h('p', { class: 'live__status', role: 'status' },
        h('span', { class: ['live__dot', snap.offline ? 'live__dot--off' : 'live__dot--on'],
          'aria-hidden': 'true' }),
        h('span', { text: t(snap.offline ? 'live.status.offline' : 'live.status.live') })),
      sound.node, test);
  }

  function drawBanners() {
    const { snap } = state;
    const reason = state.setup || snap.reason;
    const bot = snap.pulse ? snap.pulse.messenger_bot : '';
    mount(el.banners,
      snap.signedOut ? notice('critical', t('live.signed_out')) : null,
      reason ? notice('warning', t(`live.${reason}`)) : null,
      snap.offline ? notice('critical', t('live.offline')) : null,
      !reason && (bot === 'off' || bot === 'paused')
        ? notice('warning', t(bot === 'off' ? 'live.global_off' : 'live.global_paused'),
          ctx.isAdmin ? h('a', {
            class: 'btn btn--secondary btn--small', href: '/admin/facebook',
            dataset: { link: '' },
          }, h('span', { class: 'btn__label', text: t('live.global_link') })) : null) : null,
      snap.soundOn && snap.soundLocked ? notice('warning', t('live.sound_blocked')) : null);
  }

  // -- alarms ----------------------------------------------------------------------------------

  function applyAlert(alert) {
    const { snap } = state;
    const open = snap.alerts.filter((item) => item.alert_id !== alert.alert_id);
    const done = snap.done.filter((item) => item.alert_id !== alert.alert_id);
    (alert.status === 'open' ? open : done).unshift(alert);
    state.snap = { ...snap, alerts: newestFirst(open), done: newestFirst(done) };
  }

  async function setStatus(alert, status, trigger) {
    const restore = busy(trigger);
    state.working.add(alert.alert_id);
    try {
      const answer = await liveApi.setAlert(alert.alert_id, status, alert.version);
      toast(t(status === 'done' ? 'live.done_toast' : 'live.reopened'));
      applyAlert(answer.alert);
    } catch (error) {
      restore();
      toast(describe(error), 'critical');
    } finally {
      state.working.delete(alert.alert_id);
    }
    if (alive) {
      keep(drawAlarms);
      keep(fillChatCard);
      watch.refresh();
    }
  }

  /** The bot's switch for one customer, from an alarm or from the conversation. */
  async function setBot(key, enabled, version) {
    // While the request runs a redraw (the poll brings one every few seconds) must draw the
    // switch where the person put it, not where the server last said it was.
    state.pending.set(key, enabled);
    let chat;
    try {
      ({ chat } = await liveApi.setBot(key, enabled, version));
    } catch (error) {
      state.pending.delete(key);
      if (alive) {
        keep(drawAlarms);
        keep(fillChatCard);
      }
      if (isConflict(error)) {
        toast(t('live.conflict'), 'critical');
        if (state.key === key) {
          openChat(key, false);
        }
      } else {
        toast(describe(error), 'critical');
      }
      throw error; // the switch goes back
    }
    state.pending.delete(key);
    toast(t(enabled ? 'live.bot_on' : 'live.bot_off'));
    const mark = (item) => (item.key === key
      ? { ...item, bot_enabled: chat.bot_enabled, version: chat.version } : item);
    state.chats = state.chats.map(mark);
    if (state.detail && state.detail.chat && state.detail.chat.key === key) {
      state.detail.chat = mark(state.detail.chat);
    }
    state.snap = { ...state.snap,
      alerts: state.snap.alerts.map((item) => (item.key === key
        ? { ...item, bot_enabled: chat.bot_enabled, chat_version: chat.version } : item)) };
    if (alive) {
      keep(drawAlarms);
      keep(drawList);
      keep(fillChatCard);
      watch.refresh();
    }
  }

  function botSwitch(item, id) {
    const hintId = uid('live');
    const moving = state.pending.has(item.key);
    const control = switchControl({
      checked: moving ? state.pending.get(item.key) : item.bot_enabled, disabled: moving,
      label: t('live.bot_switch'),
      // An alarm carries its chat's version (0 when the chat is gone: then none is sent).
      onChange: (on) => setBot(item.key, on, id ? item.chat_version || null : item.version),
    });
    control.input.dataset.key = id ? `bot-${id}` : 'bot';
    control.input.setAttribute('aria-describedby', hintId);
    return h('div', { class: 'field field--switch' }, control.node,
      h('p', { class: 'field__hint', id: hintId, text: t(`live.bot_hint.${item.channel}`) }));
  }

  function earlier(alert) {
    const items = (Array.isArray(alert.items) ? alert.items : []).filter((item) => item && item.note);
    if (!(alert.occurrences > 1) || !items.length) {
      return null;
    }
    return h('details', { class: 'alarm__earlier' },
      h('summary', { text: t('live.earlier') }),
      h('ul', {}, [...items].reverse().map((item) => h('li', { text: item.note }))));
  }

  function phoneLine(alert) {
    if (alert.phone) {
      return h('p', { class: 'alarm__phone' }, icon('phone', 18),
        h('span', { class: 'visually-hidden', text: t('live.phone') }),
        isPhone(alert.phone) ? h('a', {
          class: 'alarm__tel', href: `tel:${alert.phone}`,
          'aria-label': t('live.call', { phone: alert.phone }), text: alert.phone,
        }) : h('span', { text: alert.phone }));
    }
    if (alert.kind === 'visit_request' || alert.kind === 'service_booking') {
      return h('p', { class: 'alarm__phone alarm__phone--missing' }, icon('phone', 18),
        h('span', { text: t('live.phone_missing') }));
    }
    return null;
  }

  /** One alarm. Open: open the chat, the bot's switch and "done". Handled: "reopen". */
  function alarmCard(alert, compact = false) {
    const tone = toneOf(alert.kind);
    const titleId = uid('alarm');
    const closed = alert.status === 'done';
    const reason = alert.reason && has(`live.reason.${alert.reason}`)
      ? t(`live.reason.${alert.reason}`) : '';
    const working = state.working.has(alert.alert_id);
    const act = closed
      ? button(t('live.reopen'), {
        small: true, iconName: 'refresh', disabled: working,
        onClick: (event) => setStatus(alert, 'open', event.currentTarget),
      })
      : button(t('live.done'), {
        small: true, variant: 'primary', iconName: 'check', disabled: working,
        onClick: (event) => setStatus(alert, 'done', event.currentTarget),
      });
    // The same alarm is also drawn inside the open conversation (compact): the two copies need
    // different keys, or a redraw would put keyboard focus on the other one.
    act.dataset.key = `${compact ? 'chat-' : ''}${closed ? 'reopen' : 'done'}-${alert.alert_id}`;
    return h('article', {
      class: ['alarm', `alarm--${tone}`, closed && 'alarm--done', compact && 'alarm--compact',
        state.fresh.has(alert.alert_id) && 'alarm--fresh'],
      'aria-labelledby': titleId, dataset: { alert: alert.alert_id },
    },
    h('div', { class: 'alarm__head' },
      h('span', { class: `chip chip--${tone} alarm__kind`, id: titleId }, glyph(alert.kind),
        h('span', { text: kindText(alert.kind) })),
      compact ? null : chip(t(`live.channel.${alert.channel}`)),
      alert.occurrences > 1 ? chip(t('live.times', { count: alert.occurrences })) : null,
      h('time', { class: 'alarm__time', datetime: alert.updated_at,
        text: relativeTime(alert.updated_at) })),
    compact ? null : h('p', { class: 'alarm__who', text: nameOf(alert) }),
    h('p', { class: 'alarm__note', text: alert.summary }),
    reason ? h('p', { class: 'alarm__meta', text: reason }) : null,
    alert.car_label ? h('p', { class: 'alarm__meta', text: t('live.car', { car: alert.car_label }) })
      : null,
    earlier(alert),
    phoneLine(alert),
    closed ? h('p', { class: 'alarm__meta', text: t('live.handled_by', {
      name: alert.handled_by, when: relativeTime(alert.handled_at) }) }) : null,
    h('div', { class: 'alarm__actions' },
      compact ? null : h('a', {
        class: 'btn btn--secondary btn--small', href: `/admin/live/${alert.key}`,
        dataset: { link: '', key: `open-${alert.alert_id}` },
      }, icon('chat', 18), h('span', { class: 'btn__label', text: t('live.open_chat') })),
      act,
      closed || compact ? null : botSwitch(alert, alert.alert_id)));
  }

  function stuckCard(chat) {
    return h('article', { class: 'alarm alarm--critical', dataset: { stuck: chat.key } },
      h('div', { class: 'alarm__head' },
        h('span', { class: 'chip chip--critical alarm__kind' }, statusIcon('critical', 16),
          h('span', { text: t('live.stuck.title') })),
        chip(t(`live.channel.${chat.channel}`))),
      h('p', { class: 'alarm__who', text: nameOf(chat) }),
      h('p', { class: 'alarm__note', text: t('live.stuck.text', {
        reason: label('fb.reason', chat.attention_reason) }) }),
      h('div', { class: 'alarm__actions' },
        h('a', {
          class: 'btn btn--secondary btn--small', href: `/admin/live/${chat.key}`,
          dataset: { link: '', key: `stuck-open-${chat.key}` },
        }, icon('chat', 18), h('span', { class: 'btn__label', text: t('live.open_chat') })),
        button(t('live.clear'), {
          small: true, iconName: 'check', dataset: { key: `stuck-clear-${chat.key}` },
          onClick: (event) => clearStuck(chat, event.currentTarget),
        })));
  }

  async function dismissWaiting(chat, trigger) {
    const restore = busy(trigger);
    try {
      // last_id: the newest stored message this screen showed.
      const result = await liveApi.dismissWaiting(chat.key, chat.last_id);
      if (result && result.waiting) {
        restore();  // a newer message came in first: it still waits, and shows on the refresh
        toast(t('live.handled_newer'), 'warning');
      } else {
        toast(t('live.handled_marked'));
        if (state.detail && state.detail.chat && state.detail.chat.key === chat.key) {
          state.detail.chat = { ...state.detail.chat, waiting: false };
        }
        state.chats = state.chats.map((item) => (item.key === chat.key
          ? { ...item, waiting: false } : item));
      }
      watch.refresh();
    } catch (error) {
      restore();
      toast(describe(error), 'critical');
    }
    if (alive) {
      keep(drawDetail);
      keep(drawList);
    }
  }

  async function clearStuck(chat, trigger) {
    const restore = busy(trigger);
    try {
      await liveApi.clearAttention(chat.key);
      toast(t('live.cleared'));
      state.stuck = state.stuck.filter((item) => item.key !== chat.key);
      state.chats = state.chats.map((item) => (item.key === chat.key
        ? { ...item, needs_attention: false, attention_reason: '' } : item));
      watch.refresh();
    } catch (error) {
      restore();
      toast(describe(error), 'critical');
    }
    if (alive) {
      keep(drawAlarms);
      keep(drawList);
    }
  }

  function drawAlarms() {
    const { snap } = state;
    if (state.setup || snap.reason) {
      mount(el.alarms);
      return;
    }
    const titleId = uid('alarms');
    const open = snap.alerts;
    const known = snap.pulse !== null || snap.offline;
    let body;
    if (!known) {
      body = loadingBlock(h('div', { class: 'alarms' }, Array.from({ length: 2 }, () => card({},
        skeleton('title'), skeleton('line', 2)))));
    } else if (!open.length) {
      body = card({}, emptyState({
        iconName: 'check', title: t('live.alarms.empty'), text: t('live.alarms.empty_text'),
      }));
    } else {
      body = h('div', { class: 'alarms' }, open.map((alert) => alarmCard(alert)));
    }
    const waiting = snap.pulse ? snap.pulse.waiting : 0;
    const handled = snap.done;
    mount(el.alarms,
      h('div', { class: 'live__heading' },
        h('h2', { class: 'block__title', id: titleId, text: t('live.alarms') }),
        open.length ? h('span', { class: 'chip chip--critical', text: String(open.length),
          'aria-label': t('live.alarms.count', { count: open.length }) }) : null),
      waiting > 0 ? notice('warning', t('live.waiting_banner', { count: waiting }),
        button(t('live.filter'), {
          small: true, onClick: () => {
            state.filter = 'waiting';
            loadChats(true);
          },
        })) : null,
      body,
      state.stuck.length ? h('div', { class: 'alarms alarms--stuck' }, state.stuck.map(stuckCard))
        : null,
      handled.length ? h('details', {
        class: 'live__handled', open: state.doneOpen,
        on: { toggle: (event) => { state.doneOpen = event.currentTarget.open; } },
      }, h('summary', { text: t('live.handled', { count: handled.length }) }),
      h('div', { class: 'alarms' }, handled.map((alert) => alarmCard(alert)))) : null);
  }

  // -- the conversation list -------------------------------------------------------------------

  function chatRow(chat) {
    const open = chat.key === state.key;
    const preview = chat.last_sender
      ? `${label('fb.sender', chat.last_sender)}: ${chat.last_body || t('fb.chats.attachment')}`
      : t('fb.chats.no_messages');
    return h('li', {}, h('a', {
      class: ['menu__item', 'live__row', open && 'live__row--open'],
      href: `/admin/live/${chat.key}`, dataset: { link: '', key: `chat-${chat.key}` },
      'aria-current': open ? 'page' : null,
    },
    h('span', { class: 'menu__label' },
      h('span', { class: 'user__name' }, h('span', { text: nameOf(chat) }),
        chip(t(`live.channel.${chat.channel}`)),
        chat.alerts && chat.alerts.length ? chip(t('live.chip.alarm'), 'critical') : null,
        chat.needs_attention ? chip(t('fb.chip.attention'), 'critical') : null,
        chat.waiting ? chip(t('live.chip.waiting'), 'warning') : null,
        chat.bot_enabled ? null : chip(t('fb.chip.bot_off'))),
      h('span', { class: 'user__username live__preview', text: preview })),
    h('span', { class: 'menu__trail' },
      h('span', { class: 'user__seen', text: relativeTime(chat.last_at) }))));
  }

  function drawList() {
    if (state.setup || state.snap.reason) {
      mount(el.list);
      return;
    }
    const filter = select(FILTERS.map((value) => ({ value, label: t(`live.filter.${value}`) })),
      state.filter);
    filter.dataset.key = 'filter';
    filter.addEventListener('change', () => {
      state.filter = filter.value;
      state.limit = PAGE;
      loadChats(true);
    });
    const refresh = iconButton('refresh', t('fb.refresh'), { onClick: () => loadChats(true) });
    refresh.dataset.key = 'refresh';
    let body;
    if (state.status === 'loading') {
      body = loadingBlock(h('div', { class: 'card users' }, Array.from({ length: 4 },
        () => h('div', { class: 'user user--skeleton' }, skeleton('line'),
          skeleton('line-short')))));
    } else if (state.status === 'error') {
      body = card({}, errorState(state.error, () => loadChats(true)));
    } else if (!state.chats.length) {
      body = card({}, emptyState({ iconName: 'chat', title: t(state.filter === 'all'
        ? 'live.chats.empty' : 'live.chats.empty_filter') }));
    } else {
      body = h('div', { class: 'card menu' },
        h('ul', { 'aria-label': t('live.chats.list') }, state.chats.map(chatRow)));
    }
    const more = state.status === 'ready' && state.chats.length >= state.limit
      ? button(t('live.chats.more'), {
        block: true, onClick: () => {
          state.limit += PAGE;
          loadChats(false);
        },
      }) : null;
    mount(el.list,
      h('h2', { class: 'block__title', text: t('live.chats') }),
      h('div', { class: 'toolbar toolbar--wrap' },
        field({ label: t('live.filter'), control: filter }).node, refresh),
      body, more ? h('div', { class: 'results__footer' }, more) : null);
  }

  async function loadChats(reset) {
    if (listController) {
      listController.abort();
    }
    const mine = new AbortController();
    listController = mine;
    if (reset) {
      state.status = 'loading';
      keep(drawList);
    }
    try {
      const answer = await liveApi.chats({ filter: state.filter, limit: state.limit },
        mine.signal);
      if (!alive || listController !== mine) {
        return;
      }
      state.chats = Array.isArray(answer.chats) ? answer.chats : [];
      state.status = 'ready';
      state.error = null;
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
      state.setup = setupOf(error) || state.setup;
    }
    keep(drawList);
    if (state.setup) {
      keep(drawBanners);
      keep(drawAlarms);
    }
  }

  async function loadStuck() {
    if (stuckController) {
      stuckController.abort();
    }
    const mine = new AbortController();
    stuckController = mine;
    state.stuckFor = state.snap.pulse ? state.snap.pulse.attention : 0;
    try {
      const answer = await liveApi.chats({ filter: 'stuck', limit: 50 }, mine.signal);
      if (!alive || stuckController !== mine) {
        return;
      }
      state.stuck = (answer.chats || []).filter((chat) => chat.needs_attention);
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      state.stuck = [];
    }
    keep(drawAlarms);
  }

  // -- one conversation ------------------------------------------------------------------------

  function backLink() {
    return h('div', { class: 'toolbar live__back' }, h('a', {
      class: 'btn btn--ghost btn--small', href: '/admin/live', dataset: { link: '', key: 'back' },
    }, icon('chevron-left', 18), h('span', { class: 'btn__label', text: t('live.back') })));
  }

  function chatAlerts(key) {
    const { snap } = state;
    if (!snap.pulse && state.detail) {
      return state.detail.alerts || [];
    }
    return alertsOfChat([...snap.alerts, ...snap.done], key);
  }

  function fillChatCard() {
    const d = state.detail;
    if (!el.chatCard || !d || d.status !== 'ready') {
      return;
    }
    const { chat } = d;
    const titleId = uid('live');
    const facts = [t(`live.channel.${chat.channel}`)];
    if (chat.car_label) {
      facts.push(t('live.car', { car: chat.car_label }));
    }
    if (chat.first_seen_at) {
      facts.push(t('live.detail.first_seen', { time: dateTime(chat.first_seen_at) }));
    }
    const own = chatAlerts(chat.key);
    mount(el.chatCard, card({ class: 'stack', 'aria-labelledby': titleId },
      h('h2', { class: 'card__title', id: titleId, tabindex: '-1', dataset: { key: 'title' },
        text: nameOf(chat) }),
      h('p', { class: 'card__text', text: facts.join(' · ') }),
      h('div', { class: 'live__chips' },
        chat.alerts && chat.alerts.length ? chip(t('live.chip.alarm'), 'critical') : null,
        chat.needs_attention ? chip(t('fb.chip.attention'), 'critical') : null,
        chat.waiting ? chip(t('live.chip.waiting'), 'warning') : null),
      // A closing "thanks" needs no answer: staff say so, and it waits again on a new message.
      chat.waiting && chat.channel === 'messenger' ? button(t('live.handled_mark'), {
        small: true, iconName: 'check', dataset: { key: 'dismiss-waiting' },
        onClick: (event) => dismissWaiting(chat, event.currentTarget),
      }) : null,
      botSwitch(chat, 0),
      ctx.isAdmin && chat.channel === 'messenger' ? h('div', {}, h('a', {
        class: 'btn btn--ghost btn--small', href: `/admin/facebook/chats/${chat.key.slice(2)}`,
        dataset: { link: '', key: 'facebook' },
      }, icon('spark', 18), h('span', { class: 'btn__label', text: t('live.detail.facebook') })))
        : null,
      own.length ? h('div', { class: 'alarms alarms--compact' },
        own.map((alert) => alarmCard(alert, true))) : null));
  }

  function fillOlder() {
    const d = state.detail;
    if (!el.older || !d) {
      return;
    }
    if (!d.hasOlder) {
      mount(el.older);
    } else if (d.older === 'error') {
      mount(el.older, notice('critical', describe(d.error),
        button(t('common.retry'), { small: true, onClick: loadOlder })));
    } else {
      const more = button(d.older === 'loading' ? t('common.loading') : t('live.detail.older'), {
        block: true, small: true, disabled: d.older === 'loading', onClick: loadOlder,
      });
      more.dataset.key = 'older';
      mount(el.older, more);
    }
  }

  /** The bubbles. ``stick``: keep the newest message in view; otherwise the scroll position stays. */
  function fillThread(stick) {
    const d = state.detail;
    const node = el.thread;
    if (!node || !d || d.status !== 'ready') {
      return;
    }
    const top = node.scrollTop;
    mount(node, d.messages.length ? d.messages.map(bubble)
      : h('li', { class: 'thread__empty muted', text: t('live.detail.no_messages') }));
    if (stick) {
      node.scrollTop = node.scrollHeight;
      d.pill = false;
    } else {
      node.scrollTop = top;
    }
    if (el.pill) {
      el.pill.hidden = !d.pill;
    }
  }

  async function loadOlder() {
    const d = state.detail;
    if (!d || d.older === 'loading' || !d.messages.length) {
      return;
    }
    d.older = 'loading';
    keep(fillOlder);
    const height = el.thread.scrollHeight;
    const top = el.thread.scrollTop;
    try {
      const answer = await liveApi.chat(state.key, { limit: MESSAGES, before: d.messages[0].id });
      if (!alive || state.detail !== d) {
        return;
      }
      d.messages = mergeMessages(d.messages, answer.messages).list;
      d.hasOlder = Boolean(answer.has_older);
      d.older = 'idle';
      fillThread(false);
      el.thread.scrollTop = top + (el.thread.scrollHeight - height);
    } catch (error) {
      if (!alive || state.detail !== d) {
        return;
      }
      d.error = error;
      d.older = 'error';
    }
    keep(fillOlder);
  }

  function drawDetail() {
    const d = state.detail;
    el.chatCard = null;
    el.older = null;
    el.thread = null;
    el.pill = null;
    if (!state.key) {
      mount(el.detail, card({ class: 'live__empty' }, emptyState({
        iconName: 'chat', title: t('live.detail.select') })));
      return;
    }
    if (state.setup || state.snap.reason) {
      mount(el.detail, backLink());
      return;
    }
    if (!d || d.status === 'loading') {
      mount(el.detail, backLink(), loadingBlock(card({}, skeleton('title'), skeleton('line', 3))));
      return;
    }
    if (d.status === 'missing') {
      mount(el.detail, backLink(), card({}, emptyState({
        iconName: 'search', title: t('live.detail.missing') })));
      return;
    }
    if (d.status === 'error') {
      mount(el.detail, backLink(), card({}, errorState(d.error, () => openChat(state.key, false))));
      return;
    }
    const titleId = uid('live');
    el.chatCard = h('div', {});
    el.older = h('div', {});
    el.thread = h('ol', {
      class: 'thread', tabindex: '0', role: 'log', 'aria-live': 'off',
      'aria-labelledby': titleId,
    });
    el.thread.addEventListener('scroll', () => {
      if (d.pill && nearBottom(el.thread)) {
        d.pill = false;
        el.pill.hidden = true;
      }
    });
    el.pill = button(t('live.detail.new'), {
      small: true, variant: 'primary', iconName: 'arrow-down', hidden: true,
      class: 'live__pill', onClick: () => {
        d.pill = false;
        el.pill.hidden = true;
        el.thread.scrollTop = el.thread.scrollHeight;
        el.thread.focus({ preventScroll: true });
      },
    });
    el.pill.dataset.key = 'pill';
    mount(el.detail, backLink(), h('div', { class: 'stack' }, el.chatCard,
      card({ class: 'live__messages', 'aria-labelledby': titleId },
        h('h2', { class: 'block__title', id: titleId, text: t('live.detail.messages') }),
        el.older, h('div', { class: 'live__threadwrap' }, el.thread, el.pill))));
    fillChatCard();
    fillOlder();
    fillThread(true);
  }

  async function openChat(key, focus) {
    if (chatController) {
      chatController.abort();
    }
    const mine = new AbortController();
    chatController = mine;
    state.detail = {
      status: 'loading', messages: [], hasOlder: false, older: 'idle', pill: false, chat: null,
      alerts: [], error: null,
    };
    state.focusTitle = Boolean(focus);
    keep(drawDetail);
    keep(drawList);
    try {
      const answer = await liveApi.chat(key, { limit: MESSAGES }, mine.signal);
      if (!alive || chatController !== mine || state.key !== key) {
        return;
      }
      Object.assign(state.detail, {
        status: 'ready', chat: answer.chat, alerts: answer.alerts || [],
        messages: mergeMessages([], answer.messages).list, hasOlder: Boolean(answer.has_older),
      });
    } catch (error) {
      if (isAbort(error) || !alive || chatController !== mine) {
        return;
      }
      state.detail.error = error;
      state.detail.status = error instanceof ApiError && error.status === 404 ? 'missing' : 'error';
      state.setup = setupOf(error) || state.setup;
    }
    keep(drawDetail, state.focusTitle ? 'title' : '');
    state.focusTitle = false;
  }

  // -- what the poll brings --------------------------------------------------------------------

  /** What each region shows, as a string: a region is redrawn only when its string changed. */
  function sigs() {
    const { snap } = state;
    const d = state.detail;
    return {
      tools: signature([snap.offline, snap.soundOn]),
      banners: signature([snap.offline, snap.signedOut, snap.reason, state.setup, snap.soundOn,
        snap.soundLocked, snap.pulse ? snap.pulse.messenger_bot : '']),
      alarms: signature([snap.alerts, snap.done, snap.pulse ? snap.pulse.waiting : 0,
        state.stuck, [...state.fresh], snap.offline, snap.pulse === null]),
      list: signature([state.status, state.chats, state.key]),
      card: signature([d && d.status, d && d.chat, state.key, d ? chatAlerts(state.key) : 0]),
    };
  }

  function onWatch(snap) {
    if (!alive) {
      return;
    }
    const previous = state.snap;
    state.snap = snap;
    if (snap.reason) {
      state.setup = snap.reason;
    } else if (snap.pulse) {
      state.setup = ''; // a good answer: the migration is there now (applied while open)
    }
    if (previous.pulse) {
      for (const alert of snap.alerts) {
        if (!previous.alerts.some((item) => item.alert_id === alert.alert_id)) {
          state.fresh.add(alert.alert_id);
          later(() => {
            state.fresh.delete(alert.alert_id);
            const node = root.querySelector(`[data-alert="${alert.alert_id}"]`);
            if (node) {
              node.classList.remove('alarm--fresh');
            }
          }, FRESH_MS);
        }
      }
    }
    if (state.status === 'ready' && Object.keys(snap.chats).length) {
      state.chats = mergeChats(state.chats, snap.chats, state.filter, state.limit);
    }
    const d = state.detail;
    if (d && d.status === 'ready') {
      if (snap.chats[state.key]) {
        d.chat = { ...d.chat, ...snap.chats[state.key] };
      }
      const incoming = snap.messages.filter((message) => message.key === state.key);
      if (incoming.length) {
        const near = el.thread ? nearBottom(el.thread) : true;
        const merged = mergeMessages(d.messages, incoming);
        if (merged.added.length) {
          d.messages = merged.list;
          d.pill = !near;
          keep(() => fillThread(near));
        }
      }
    }
    if (snap.pulse && snap.pulse.attention !== state.stuckFor) {
      loadStuck();
    }
    const next = sigs();
    if (next.tools !== signatures.tools) {
      keep(drawTools);
    }
    if (next.banners !== signatures.banners) {
      keep(drawBanners);
    }
    if (next.alarms !== signatures.alarms) {
      keep(drawAlarms);
    }
    if (next.list !== signatures.list) {
      keep(drawList);
    }
    if (next.card !== signatures.card) {
      keep(fillChatCard);
    }
    Object.assign(signatures, next);
  }

  // -- the page --------------------------------------------------------------------------------

  function frame() {
    el.tools = h('div', { class: 'live__tools' });
    el.banners = h('div', { class: 'live__banners' });
    el.alarms = h('section', { class: 'live__alarms' });
    el.list = h('section', { class: 'live__list' });
    el.detail = h('section', { class: 'live__detail' });
    el.wrap = h('div', { class: ['live', state.key && 'live--detail'] }, el.banners, el.alarms,
      h('div', { class: 'live__body' }, el.list, el.detail));
    mount(root, pageHead(t('live.title'), el.tools), el.wrap);
  }

  function draw() {
    frame();
    drawTools();
    drawBanners();
    drawAlarms();
    drawList();
    drawDetail();
    Object.assign(signatures, sigs()); // what is on screen now: unchanged data is not redrawn
  }

  /** The address changed: another conversation, or none. */
  function show(next) {
    const wanted = next && KEY.test(next.rest || '') ? next.rest : '';
    if (wanted === state.key && el.wrap) {
      return;
    }
    const left = state.key;
    state.key = wanted;
    if (!el.wrap) {
      draw();
    }
    el.wrap.classList.toggle('live--detail', Boolean(wanted));
    if (wanted) {
      openChat(wanted, true);
    } else {
      if (chatController) {
        chatController.abort();
      }
      state.detail = null;
      keep(drawDetail);
      keep(drawList, left ? `chat-${left}` : '');
    }
  }

  ctx.setGuard(null);
  document.addEventListener('pointerdown', press, true);
  for (const type of ['pointerup', 'pointercancel']) {
    document.addEventListener(type, release, true);
  }
  window.addEventListener('blur', release);
  watch.setDetail(true);
  unsubscribe = watch.subscribe(onWatch);
  timers.add(window.setInterval(() => {
    keep(drawAlarms);
    keep(drawList);
  }, TIMES_MS));
  show(params);
  loadChats(false);
  return {
    update: show,
    redraw() {
      keep(draw);
    },
    destroy() {
      alive = false;
      document.removeEventListener('pointerdown', press, true);
      for (const type of ['pointerup', 'pointercancel']) {
        document.removeEventListener(type, release, true);
      }
      window.removeEventListener('blur', release);
      window.clearTimeout(pressTimer);
      deferred.clear();
      watch.setDetail(false);
      if (unsubscribe) {
        unsubscribe();
      }
      for (const controller of [listController, chatController, stuckController]) {
        if (controller) {
          controller.abort();
        }
      }
      for (const id of timers) {
        window.clearTimeout(id);
        window.clearInterval(id);
      }
      timers.clear();
    },
  };
}
