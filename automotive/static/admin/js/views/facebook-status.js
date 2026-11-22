// Facebook > Status & switches: the Messenger bot's main switch (off, test mode for the chats
// marked as testers, or on for everyone, which asks for the Auto reply confirmation), the
// testers, the comment-reply switch, warnings, today's numbers, the latest
// webhook events, the worker's background jobs, and the limits and texts. It refreshes itself
// every 30 seconds while the page is visible, and only redraws when something changed; a
// settings form with unsaved edits is never replaced.

import { ApiError, isAbort } from '../api.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import { fieldMessage, limitedInput } from '../forms.js';
import { dateTime, num } from '../format.js';
import { has, t } from '../strings.js';
import {
  adminOnly, busy, button, card, errorState, field, isForbidden, loadingBlock, notice,
  openDialog, skeleton, switchControl, textInput, toast,
} from '../ui.js';
import {
  describe, fbApi, label, notConfigured, personName, restartCard, restartNeeded, usd,
} from './facebook-shared.js';

const REFRESH_MS = 30000;
// The bounds are the database's own (migration 0013), except Keep bot records for: 30-365
// days (the server refuses more). Messages are kept for good whatever it says.
const NUMBERS = [
  { name: 'max_reply_age_minutes', min: 5, max: 1440 },
  { name: 'daily_turn_limit', min: 0, max: 5000 },
  { name: 'customer_daily_turn_limit', min: 0, max: 500 },
  { name: 'retention_days', min: 30, max: 365 },
];
const TEXTS = [
  { name: 'fallback_ka', max: 500, lang: 'ka' },
  { name: 'fallback_en', max: 500, lang: 'en' },
  { name: 'comment_public_reply', max: 300 },
];
const WARNING_KIND = {
  bot_paused: 'critical', not_subscribed: 'critical', token_broken: 'critical',
  token_expired: 'critical', data_access_expired: 'critical', not_connected: 'warning',
  no_recent_webhook: 'warning', token_expiring: 'warning', data_access_expiring: 'warning',
  auto_reply_not_confirmed: 'info', test_no_testers: 'warning',
};
// Warnings the Connect tab resolves carry a link to it.
const TO_CONNECT = new Set(['not_connected', 'not_subscribed', 'token_broken', 'token_expired',
  'token_expiring', 'data_access_expired', 'data_access_expiring']);
const BOT_GLYPH = { on: 'good', off: 'neutral', paused: 'warning', test: 'pending' };
const MODES = new Set(['off', 'test', 'on']);
const NAMES_SHOWN = 10; // testers named in the test-mode question; the rest are counted

function warningText(warning) {
  if (warning.code === 'no_recent_webhook' && !warning.last) {
    return t('fb.warn.no_webhook_yet');
  }
  const key = `fb.warn.${warning.code}`;
  return has(key) ? t(key, {
    purpose: label('fb.purpose', warning.purpose), days: warning.days,
    time: dateTime(warning.since || warning.last),
  }) : String(warning.code || '');
}

/** Where a webhook item stands; 'claimed' is a lease in progress, not a finished item. */
function outcomeText(row) {
  const outcome = String(row.outcome || '');
  if (outcome === 'claimed') {
    return t('fb.outcome.claimed');
  }
  if (outcome.startsWith('retry')) {
    return t('fb.outcome.retry');
  }
  if (outcome.startsWith('failed')) {
    return t('fb.outcome.failed');
  }
  if (!row.processed_at) {
    return t('fb.outcome.waiting');
  }
  return outcome && outcome !== 'done' ? `${t('fb.outcome.done')} (${outcome})`
    : t('fb.outcome.done');
}

function runText(run) {
  const parts = [label('fb.run', run.status), dateTime(run.finished_at || run.started_at)];
  if (run.records) {
    parts.push(t('fb.jobs.records', { count: num(run.records) }));
  }
  if (run.status === 'error' && run.detail) {
    parts.push(String(run.detail).slice(0, 120));
  }
  return parts.join(' · ');
}

/** Everything but the moment of the check: equal means nothing to redraw. */
function signature(data) {
  return JSON.stringify({ ...data, checked_at: null });
}

/** The ON question. The confirm button stays disabled until Auto reply is confirmed off. */
function confirmOn(opener) {
  return openDialog({
    title: t('fb.on.title'),
    opener,
    build: (close) => {
      const go = button(t('fb.on.confirm'), {
        variant: 'primary', disabled: true, onClick: () => close(true),
      });
      const tick = switchControl({
        checked: false, label: t('fb.on.auto_reply'),
        onChange: (on) => {
          go.disabled = !on;
        },
      });
      const hintId = uid('fb');
      tick.input.setAttribute('aria-describedby', hintId);
      return [
        h('p', { class: 'dialog__text', text: t('fb.on.text') }),
        tick.node,
        h('p', { class: 'field__hint', id: hintId, text: t('fb.on.auto_reply_hint') }),
        h('div', { class: 'dialog__actions' },
          button(t('common.cancel'), { onClick: () => close(false), autofocus: true }), go),
      ];
    },
  }).then((value) => value === true);
}

/** The test-mode question: who will be answered (or that nobody is marked yet), and that Meta's
 * Auto reply may stay on. Nothing to tick: every other customer is left to the shop. */
function confirmTest(opener, testers) {
  const named = testers.slice(0, NAMES_SHOWN).map(personName);
  const more = testers.length - named.length;
  const names = more > 0 ? `${named.join(', ')} +${more}` : named.join(', ');
  return openDialog({
    title: t('fb.test.title'),
    opener,
    build: (close) => [
      h('p', { class: 'dialog__text', text: t('fb.test.text') }),
      testers.length
        ? h('p', { class: 'dialog__text', text: t('fb.test.testers', { count: testers.length, names }) })
        : notice('warning', t('fb.test.none')),
      h('p', { class: 'field__hint', text: t('fb.test.auto_reply') }),
      h('div', { class: 'dialog__actions' },
        button(t('common.cancel'), { onClick: () => close(false), autofocus: true }),
        button(t('fb.test.confirm'), { variant: 'primary', onClick: () => close(true) })),
    ],
  }).then((value) => value === true);
}

export function mountView(root, ctx) {
  const state = { status: 'loading', data: null, error: null, loadedAt: null };
  const form = { node: null, dirty: false };
  let controller = null;
  let timer = 0;
  let hint = null;
  let working = false; // a change is on its way: no silent refresh until it lands
  let alive = true;
  // A busy or switching control is disabled while its request runs, and the browser drops
  // its focus; the next draw puts focus back on the control with this data-key.
  let wantedKey = '';
  ctx.setGuard(() => form.dirty);

  function updatedText() {
    return state.loadedAt ? t('fb.updated', { time: dateTime(state.loadedAt) }) : '';
  }

  // -- actions -------------------------------------------------------------------------

  /** After a failed request: focus back on the control with ``key`` once it is enabled again,
   * unless the person has moved on to something else meanwhile. */
  function refocus(key) {
    window.setTimeout(() => {
      const active = document.activeElement;
      if (alive && key && (!active || active === document.body)) {
        const node = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
        if (node instanceof HTMLElement) {
          node.focus({ preventScroll: true });
        }
      }
    }, 0);
  }

  /** Save switches through PUT /settings. Resolves true when the server accepted it. */
  async function change(trigger, fields, done, busyLabel, key = '') {
    const restore = trigger ? busy(trigger, busyLabel) : () => {};
    const focusKey = key || (trigger && trigger.dataset.key) || '';
    working = true;
    try {
      const result = await fbApi.saveSettings({ version: state.data.switches.version, ...fields });
      if (alive) {
        state.data.switches = result.switches;
        toast(done);
        wantedKey = focusKey;
        draw();
        load(true); // warnings and numbers follow the new state
      }
      return true;
    } catch (error) {
      restore();
      if (alive) {
        if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
          toast(t('fb.conflict'), 'critical');
          await load(true);
        } else {
          toast(describe(error), 'critical');
        }
        refocus(focusKey);
      }
      return false;
    } finally {
      working = false;
    }
  }

  // After a mode change the pressed button is gone; focus goes to the stop (or the start).
  async function turnOn(trigger) {
    if (await confirmOn(trigger)) {
      await change(trigger, { bot_mode: 'on', auto_reply_confirmed_off: true },
        t('fb.bot.turned_on'), t('fb.bot.turning_on'), 'bot-off');
    }
  }

  async function turnTest(trigger) {
    const testers = Array.isArray(state.data.testers) ? state.data.testers : [];
    if (await confirmTest(trigger, testers)) {
      await change(trigger, { bot_mode: 'test' }, t('fb.bot.turned_test'),
        t('fb.bot.turning_test'), 'bot-off');
    }
  }

  // Off is never questioned: it is the emergency stop, and the server accepts it from a
  // page that is out of date. It ends test mode too.
  function turnOff(trigger) {
    return change(trigger, { bot_mode: 'off' }, t('fb.bot.turned_off'), t('fb.bot.turning_off'),
      'bot-on');
  }

  async function removeTester(row, trigger) {
    const restore = busy(trigger, t('common.saving'));
    working = true;
    try {
      await fbApi.saveThread(row.psid, { bot_tester: false, version: row.version });
      if (alive) {
        toast(t('fb.testers.removed', { name: personName(row) }));
        wantedKey = 'testers';
        await load(true);
      }
    } catch (error) {
      restore();
      if (alive) {
        toast(describe(error), 'critical');
        refocus(trigger.dataset.key);
        if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
          load(true);
        }
      }
    } finally {
      working = false;
    }
  }

  async function resumeBot(trigger) {
    const restore = busy(trigger, t('fb.bot.resuming'));
    working = true;
    try {
      const result = await fbApi.resume();
      if (alive) {
        state.data.switches = result.switches;
        toast(t('fb.bot.resumed'));
        wantedKey = 'bot-off'; // the resume button is gone; the stop is next to it
        draw();
        load(true);
      }
    } catch (error) {
      restore();
      toast(describe(error), 'critical');
      refocus('resume');
    } finally {
      working = false;
    }
  }

  async function runJob(job, trigger) {
    const restore = busy(trigger, t('common.loading'));
    try {
      await fbApi.runJob(job);
      toast(t('fb.jobs.queued', { job: label('fb.job', job) }));
    } catch (error) {
      toast(describe(error), 'critical');
    } finally {
      restore();
      refocus(trigger.dataset.key);
    }
  }

  async function saveSettings(controls, save, before) {
    const fields = {};
    const problems = [];
    for (const spec of NUMBERS) {
      const raw = controls[spec.name].input.value.trim();
      const value = Number(raw);
      if (!/^-?[0-9]+$/.test(raw) || value < spec.min || value > spec.max) {
        controls[spec.name].setError(fieldMessage('OUT_OF_RANGE'));
        problems.push(spec.name);
      } else if (value !== before[spec.name]) {
        fields[spec.name] = value;
      }
    }
    for (const spec of TEXTS) {
      const value = controls[spec.name].input.value;
      if (value.trim() !== String(before[spec.name] || '')) {
        fields[spec.name] = value;
      }
    }
    if (problems.length) {
      controls[problems[0]].input.focus();
      return;
    }
    if (!Object.keys(fields).length) {
      form.dirty = false;
      toast(t('fb.limits.unchanged'), 'info');
      return;
    }
    const restore = busy(save, t('common.saving'));
    working = true;
    try {
      // The version the form was filled from: a change made since then is not overwritten.
      const result = await fbApi.saveSettings({ version: before.version, ...fields });
      if (alive) {
        state.data.switches = result.switches;
        form.dirty = false;
        form.node = null;
        toast(t('fb.limits.saved'));
        wantedKey = 'save';
        draw();
      }
    } catch (error) {
      restore();
      if (!alive) {
        return;
      }
      const codes = error instanceof ApiError && error.code === 'INVALID_FIELDS' ? error.fields : {};
      const names = Object.keys(codes || {}).filter((name) => controls[name]);
      if (names.length) {
        for (const name of names) {
          controls[name].setError(fieldMessage(codes[name]));
        }
        controls[names[0]].input.focus();
      } else if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
        form.dirty = false;
        form.node = null;
        toast(t('fb.limits.conflict'), 'critical');
        await load(true);
        refocus('save');
      } else {
        toast(describe(error), 'critical');
        refocus('save');
      }
    } finally {
      working = false;
    }
  }

  // -- parts ---------------------------------------------------------------------------

  function toolbar() {
    const refresh = button(t('fb.refresh'), {
      small: true, variant: 'ghost', iconName: 'refresh', onClick: () => load(true),
    });
    refresh.dataset.key = 'refresh';
    hint = h('p', { class: 'toolbar__hint', text: updatedText() });
    return h('div', { class: 'toolbar' }, hint, refresh);
  }

  /** The chats an admin marked as testers: each links to its chat and can be removed here;
   * adding one happens in the chat itself (the customer must have written to the page). */
  function testersBlock(testers) {
    const titleId = uid('fb');
    const items = testers.map((row) => {
      const remove = button(t('fb.testers.remove'), {
        small: true, variant: 'ghost', iconName: 'close',
        'aria-label': t('fb.testers.remove_label', { name: personName(row) }),
        onClick: (event) => removeTester(row, event.currentTarget),
      });
      remove.dataset.key = `tester-${row.psid}`;
      return h('li', { class: 'audit__row' },
        h('p', { class: 'audit__what' },
          h('a', { class: 'audit__link', href: `/admin/facebook/chats/${row.psid}`,
            dataset: { link: '' }, text: personName(row) }),
          remove),
        row.bot_enabled === false
          ? h('p', { class: 'audit__when', text: t('fb.testers.chat_off') }) : null);
    });
    return [
      h('h2', { class: 'card__title', id: titleId, tabindex: '-1', dataset: { key: 'testers' },
        text: `${t('fb.testers.title')} (${testers.length})` }),
      h('p', { class: 'card__text', text: t('fb.testers.hint') }),
      items.length ? h('ul', { class: 'audit', 'aria-labelledby': titleId }, items)
        : h('p', { class: 'card__text', text: t('fb.testers.empty') }),
      h('div', {}, h('a', {
        class: 'btn btn--secondary btn--small', href: '/admin/facebook/chats',
        dataset: { link: '', key: 'open-chats' },
      }, h('span', { class: 'btn__label', text: t('fb.testers.open_chats') }))),
    ];
  }

  function switchesCard(data) {
    const s = data.switches;
    const bot = BOT_GLYPH[s.bot_state] ? s.bot_state : 'off';
    const mode = MODES.has(s.bot_mode) ? s.bot_mode : (s.bot_enabled ? 'on' : 'off');
    const testers = Array.isArray(data.testers) ? data.testers : [];
    const titleId = uid('fb');
    const stateId = uid('fb');
    const control = (key, label, variant, iconName, run) => {
      const node = button(label, {
        variant, block: true, iconName, onClick: (event) => run(event.currentTarget),
      });
      node.dataset.key = key;
      node.setAttribute('aria-describedby', stateId);
      return node;
    };
    // Off: on for everyone, or test mode. Test: on for everyone, or off. On: test, or off.
    const controls = [
      mode !== 'on' ? control('bot-on', t('fb.bot.turn_on'), 'primary', 'check', turnOn) : null,
      mode !== 'test' ? control('bot-test', t(mode === 'on' ? 'fb.bot.to_test' : 'fb.bot.turn_test'),
        'secondary', 'users', turnTest) : null,
      mode !== 'off' ? control('bot-off', t('fb.bot.turn_off'), 'danger', 'lock', turnOff) : null,
    ];
    const since = { on: 'fb.bot.since_on', test: 'fb.bot.since_test', off: 'fb.bot.since_off' };
    let resume = null;
    if (bot === 'paused') {
      resume = button(t('fb.bot.resume'), {
        small: true, iconName: 'refresh', onClick: (event) => resumeBot(event.currentTarget),
      });
      resume.dataset.key = 'resume';
    }
    const who = s.bot_changed_by || t('audit.system');
    const commentsId = uid('fb');
    const commentsHint = uid('fb');
    const comments = switchControl({
      checked: s.comments_enabled, label: t('fb.comments.label'),
      onChange: async (on) => {
        const saved = await change(null, { comments_enabled: on },
          t(on ? 'fb.comments.on' : 'fb.comments.off'), '', 'comments');
        if (!saved) {
          throw new Error('not saved'); // the switch goes back
        }
      },
    });
    comments.input.dataset.key = 'comments';
    comments.input.setAttribute('aria-describedby', commentsHint);
    return card({ class: 'stack', 'aria-labelledby': titleId },
      h('h2', { class: 'card__title', id: titleId, text: t('fb.bot.title') }),
      h('div', { id: stateId },
        h('p', { class: 'who' }, statusIcon(BOT_GLYPH[bot], 26),
          h('span', { class: 'tile__value', text: t(`fb.bot.${bot}`) })),
        h('p', { class: 'card__text', text: t(`fb.bot.${bot}_text`) })),
      s.bot_changed_at ? h('p', {
        class: 'tile__note',
        text: t(since[mode], { who, time: dateTime(s.bot_changed_at) }),
      }) : null,
      mode === 'on' && s.bot_enabled_at ? h('p', {
        class: 'tile__note', text: t('fb.bot.answers_after', { time: dateTime(s.bot_enabled_at) }),
      }) : null,
      mode === 'test' && s.bot_test_at ? h('p', {
        class: 'tile__note',
        text: t('fb.bot.answers_after_test', { time: dateTime(s.bot_test_at) }),
      }) : null,
      resume ? notice('critical', t('fb.bot.paused_text'), resume) : null,
      ...controls,
      h('p', { class: 'page-note' }, icon('info', 16),
        h('span', { text: t(mode === 'test' ? 'fb.bot.test_explain' : 'fb.bot.explain') })),
      ...testersBlock(testers),
      h('h2', { class: 'card__title', id: commentsId, text: t('fb.comments.title') }),
      comments.node,
      h('p', { class: 'card__text', id: commentsHint, text: t('fb.comments.hint') }));
  }

  function warningsCard(data) {
    const list = Array.isArray(data.warnings) ? data.warnings : [];
    const titleId = uid('fb');
    const link = () => h('a', {
      class: 'btn btn--secondary btn--small', href: '/admin/facebook/connect', dataset: { link: '' },
    }, h('span', { class: 'btn__label', text: t('fb.warn.open_connect') }));
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.warnings') }),
      list.length ? list.map((warning) => notice(WARNING_KIND[warning.code] || 'warning',
        warningText(warning), ...(TO_CONNECT.has(warning.code) ? [link()] : [])))
        : notice('good', t('fb.no_warnings')));
  }

  function todayBlock(data) {
    const today = data.today || {};
    const tiles = [
      ['fb.today.customer_messages', num(today.customer_messages || 0)],
      ['fb.today.replies_sent', num(today.replies_sent || 0)],
      ['fb.today.turns', num(today.turns || 0),
        t('fb.today.turns_note', { limit: num(data.switches.daily_turn_limit || 0) })],
      ['fb.today.cost', usd(today.cost_usd)],
      ['fb.today.new_chats', num(today.new_chats || 0)],
      ['fb.today.attention', num(today.attention || 0), t('fb.today.attention_note')],
      ['fb.today.failed', num(today.failed || 0)],
      ['fb.today.fallbacks', num(today.fallbacks || 0)],
    ];
    const titleId = uid('fb');
    return h('section', { class: 'block', 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.today.title') }),
      h('div', { class: 'tiles tiles--admin' }, tiles.map(([key, value, note]) => h('div',
        { class: 'tile' },
        h('p', { class: 'tile__label', text: t(key) }),
        h('p', { class: 'tile__value', text: value }),
        note ? h('p', { class: 'tile__note', text: note }) : null))));
  }

  function eventsCard(data) {
    const rows = Array.isArray(data.events) ? data.events : [];
    const titleId = uid('fb');
    const last = data.last_event;
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.events.title') }),
      h('p', { class: 'card__text', text: last
        ? t('fb.events.last', { time: dateTime(last.received_at) }) : t('fb.events.empty') }),
      rows.length ? h('ol', { class: 'audit', 'aria-label': t('fb.events.title') },
        rows.map((row) => h('li', { class: 'audit__row' },
          h('p', { class: 'audit__what' },
            h('strong', { class: 'audit__who', text: label('fb.field', row.field) }),
            h('span', { text: outcomeText(row) })),
          h('p', { class: 'audit__when' },
            h('time', { datetime: row.received_at, text: dateTime(row.received_at) }),
            row.attempts > 1 ? ` · ${t('fb.events.attempts', { count: row.attempts })}` : null))))
        : null,
      h('p', { class: 'page-note' }, icon('info', 16), h('span', { text: t('fb.events.hint') })));
  }

  function jobsCard(data) {
    const runs = new Map((Array.isArray(data.runs) ? data.runs : []).map((run) => [run.job, run]));
    const titleId = uid('fb');
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.jobs.title') }),
      h('p', { class: 'card__text', text: t('fb.jobs.hint') }),
      h('ul', { class: 'audit', 'aria-label': t('fb.jobs.title') }, (data.jobs || []).map((job) => {
        const run = runs.get(job);
        const go = button(t('fb.jobs.run'), {
          small: true, variant: 'ghost', iconName: 'refresh',
          'aria-label': t('fb.jobs.run_label', { job: label('fb.job', job) }),
          onClick: (event) => runJob(job, event.currentTarget),
        });
        go.dataset.key = `job-${job}`;
        return h('li', { class: 'audit__row' },
          h('p', { class: 'audit__what' },
            h('strong', { class: 'audit__who', text: label('fb.job', job) }), go),
          h('p', { class: 'audit__when', text: run ? runText(run) : t('fb.jobs.never') }));
      })));
  }

  function settingsCard(switches) {
    const titleId = uid('fb');
    const controls = {};
    const markDirty = () => {
      form.dirty = true;
    };
    const numbers = NUMBERS.map((spec) => {
      const input = textInput({
        type: 'number', inputmode: 'numeric', min: spec.min, max: spec.max, step: 1,
        value: String(switches[spec.name] ?? ''),
      });
      const controller = field({
        label: t(`fb.limits.${spec.name}`), hint: t(`fb.limits.${spec.name}_hint`), control: input,
      });
      input.addEventListener('input', () => {
        controller.setError('');
        markDirty();
      });
      controls[spec.name] = controller;
      return controller.node;
    });
    const texts = TEXTS.map((spec) => {
      const controller = limitedInput({
        label: t(`fb.limits.${spec.name}`), hint: t(`fb.limits.${spec.name}_hint`),
        value: switches[spec.name] || '', max: spec.max, multiline: true, lang: spec.lang,
        onInput: () => {
          controller.setError('');
          markDirty();
        },
      });
      controls[spec.name] = controller;
      return controller.node;
    });
    const save = button(t('common.save'), { variant: 'primary', type: 'submit' });
    save.dataset.key = 'save';
    const body = h('form', { class: 'form', novalidate: true, 'aria-labelledby': titleId },
      h('div', { class: 'grid' }, numbers), texts,
      h('div', { class: 'form-actions form-actions--start' }, save));
    body.addEventListener('submit', (event) => {
      event.preventDefault();
      saveSettings(controls, save, switches);
    });
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.limits.title') }), body);
  }

  // -- drawing -------------------------------------------------------------------------

  function render() {
    hint = null;
    if (state.status === 'loading') {
      mount(root, loadingBlock(h('div', { class: 'grid' }, Array.from({ length: 2 }, () => h('div',
        { class: 'card' }, skeleton('title'), skeleton('value'), skeleton('line'),
        skeleton('line-short'))))));
      return;
    }
    if (state.status === 'error') {
      if (isForbidden(state.error)) {
        mount(root, card({}, adminOnly()));
      } else if (restartNeeded(state.error)) {
        mount(root, restartCard());
      } else {
        mount(root, card({}, errorState(state.error, () => load(false))));
      }
      return;
    }
    const { data } = state;
    if (!data.configured) {
      mount(root, notConfigured(data));
      return;
    }
    // Rebuilt from fresh values unless someone is typing in it.
    const keep = form.node && (form.dirty || form.node.contains(document.activeElement));
    if (!keep) {
      form.node = settingsCard(data.switches);
    }
    mount(root,
      toolbar(),
      h('div', { class: 'grid' }, switchesCard(data), warningsCard(data)),
      todayBlock(data),
      h('div', { class: 'block' }, h('div', { class: 'grid' }, eventsCard(data), jobsCard(data))),
      h('div', { class: 'block' }, form.node));
  }

  /** Redraw, keeping keyboard focus on the same control when it is drawn again. */
  function draw() {
    const active = document.activeElement;
    const key = wantedKey
      || (active instanceof HTMLElement && root.contains(active) ? active.dataset.key : '');
    wantedKey = '';
    render();
    if (key) {
      const again = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again instanceof HTMLElement) {
        again.focus({ preventScroll: true });
      }
    }
  }

  async function load(silent) {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    if (!silent) {
      state.status = 'loading';
      state.error = null;
      draw();
    }
    try {
      const data = await fbApi.status(controller.signal);
      if (!alive) {
        return;
      }
      const same = state.status === 'ready' && state.data && signature(data) === signature(state.data);
      state.data = data;
      state.status = 'ready';
      state.error = null;
      state.loadedAt = new Date();
      if (same && hint) {
        hint.textContent = updatedText();
        return;
      }
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      if (silent && state.data && state.status === 'ready') {
        return; // Keep the last good picture; the next refresh tries again.
      }
      state.error = error;
      state.status = 'error';
    }
    draw();
  }

  load(false);
  timer = window.setInterval(() => {
    if (alive && !working && document.visibilityState === 'visible') {
      load(true);
    }
  }, REFRESH_MS);

  return {
    redraw: draw,
    destroy() {
      alive = false;
      window.clearInterval(timer);
      if (controller) {
        controller.abort();
      }
    },
  };
}
