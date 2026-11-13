// Facebook > Connect: the page connection and its tokens, the checks around it (tokens,
// webhooks, the sending gate), and the form that connects the page with a token pasted from
// Graph API Explorer. The token field is emptied the moment the form is sent, and the token is
// never shown again. When one token reaches several pages it is kept in this tab's memory only
// until the owner picks the page, cancels, or leaves the tab (ten minutes at most).

import { ApiError, isAbort } from '../api.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import { dateTime, longDate } from '../format.js';
import { has, t } from '../strings.js';
import {
  adminOnly, busy, button, card, confirmDialog, errorState, field, isForbidden, loadingBlock,
  notice, select, skeleton, textInput, toast,
} from '../ui.js';
import {
  describe, fbApi, label, notConfigured, restartCard, restartNeeded,
} from './facebook-shared.js';

const EXPLORER_URL = 'https://developers.facebook.com/tools/explorer/';
const PENDING_MS = 10 * 60 * 1000;
const STEPS = [1, 2, 3, 4, 5, 6];

function probeResult(probe) {
  if (probe.result === 'passed') {
    return { kind: 'good', text: t('fb.probe.passed') };
  }
  if (probe.result === 'gated') {
    return { kind: 'critical', text: t('fb.probe.gated') };
  }
  const key = `fb.probe.reason.${probe.reason}`;
  let text = t('fb.probe.inconclusive', { reason: has(key) ? t(key) : String(probe.reason || '') });
  if (probe.code !== null && probe.code !== undefined) {
    const code = probe.subcode ? `${probe.code}/${probe.subcode}` : String(probe.code);
    text = `${text} ${t('fb.meta_code', { code })}`;
  }
  return { kind: 'warning', text };
}

function fact(name, value) {
  return h('div', {}, h('dt', { text: name }), h('dd', {}, value));
}

function tokenRow(row) {
  const ok = row.status === 'ok';
  const parts = [];
  if (!row.expires_at) {
    parts.push(t('fb.token.never_expires'));
  } else if (row.days_left !== null && row.days_left < 0) {
    parts.push(t('fb.token.expired_on', { date: longDate(row.expires_at) }));
  } else {
    parts.push(t('fb.token.expires', { date: longDate(row.expires_at), days: row.days_left ?? 0 }));
  }
  if (row.data_access_expires_at) {
    parts.push(t('fb.token.data_access', { date: longDate(row.data_access_expires_at) }));
  }
  if (row.checked_at) {
    parts.push(t('fb.token.checked', { time: dateTime(row.checked_at) }));
  }
  const missing = Array.isArray(row.missing_scopes) ? row.missing_scopes : [];
  return h('li', { class: 'audit__row' },
    h('p', { class: 'audit__what' },
      statusIcon(!ok ? 'critical' : row.warn ? 'warning' : 'good', 16),
      h('strong', { class: 'audit__who', text: label('fb.purpose', row.purpose) }),
      h('span', { text: t(ok ? 'fb.token.ok' : 'fb.token.broken') }),
      !ok && row.status_detail ? h('span', { class: 'chip code', text: row.status_detail }) : null,
      row.other_app ? h('span', { class: 'chip', text: t('fb.token.other_app') }) : null),
    h('p', { class: 'audit__when', text: parts.join(' · ') }),
    missing.length ? h('p', { class: 'audit__when',
      text: t('fb.token.missing_scopes', { names: missing.join(', ') }) }) : null);
}

export function mountView(root, ctx) {
  const state = {
    status: 'loading', data: null, error: null, results: [], pending: null, focusKey: '',
  };
  let controller = null;
  let pendingTimer = 0;
  let alive = true;
  ctx.setGuard(null);

  /** Drop the token held for a page choice. */
  function forget() {
    state.pending = null;
    window.clearTimeout(pendingTimer);
  }

  /** The outcome notices shown above the cards: {kind, text} items. */
  function report(...items) {
    state.results = items.filter(Boolean);
  }

  // -- actions -------------------------------------------------------------------------

  // Each action ends with its outcome above the cards; focus goes there, so it is read out
  // (the busy button lost focus when it was disabled).
  async function act(trigger, busyLabel, work) {
    const restore = busy(trigger, busyLabel);
    state.focusKey = 'results';
    try {
      await work();
    } catch (error) {
      restore();
      if (alive) {
        report({ kind: 'critical', text: describe(error) });
        draw();
      }
    }
  }

  function checkTokens(trigger) {
    return act(trigger, t('fb.conn.checking'), async () => {
      const rows = (await fbApi.check()).tokens || [];
      const broken = rows.filter((row) => row.status !== 'ok')
        .map((row) => label('fb.purpose', row.purpose));
      report(broken.length
        ? { kind: 'critical', text: t('fb.conn.checked_broken', { names: broken.join(', ') }) }
        : { kind: 'good', text: t('fb.conn.checked_ok') });
      await load(true);
    });
  }

  function repairWebhooks(trigger) {
    return act(trigger, t('fb.conn.subscribing'), async () => {
      const { subscription } = await fbApi.subscribe();
      const fixed = subscription.page_resubscribed || subscription.app_resubscribed;
      report({ kind: 'good', text: t(fixed ? 'fb.conn.subscribe_fixed' : 'fb.conn.subscribe_ok') });
      await load(true);
    });
  }

  async function testSending(trigger) {
    const sure = await confirmDialog({
      opener: trigger, title: t('fb.probe.title'), text: t('fb.probe.text'),
      confirmLabel: t('fb.probe.confirm'),
    });
    if (!sure) {
      return;
    }
    await act(trigger, t('fb.conn.probing'), async () => {
      report(probeResult((await fbApi.probe()).probe));
      await load(true);
    });
  }

  async function disconnect(trigger) {
    const sure = await confirmDialog({
      opener: trigger, title: t('fb.disconnect.title'), text: t('fb.disconnect.text'),
      extra: t('fb.disconnect.extra'), confirmLabel: t('fb.conn.disconnect'), danger: true,
    });
    if (!sure) {
      return;
    }
    await act(trigger, t('fb.conn.disconnecting'), async () => {
      await fbApi.disconnect();
      toast(t('fb.conn.disconnected'));
      report({ kind: 'good', text: t('fb.conn.disconnected_text') });
      await load(true);
    });
  }

  async function connect(token, pageId, trigger) {
    const restore = busy(trigger, t('fb.connect.submitting'));
    try {
      const done = (await fbApi.connect(token, pageId)).connected || {};
      forget();
      if (!alive) {
        return;
      }
      const missing = Array.isArray(done.missing_scopes) ? done.missing_scopes : [];
      report({ kind: 'good', text: t('fb.connect.done', { name: done.page_name || done.page_id }) },
        missing.length ? { kind: 'warning',
          text: t('fb.connect.missing_scopes', { names: missing.join(', ') }) } : null,
        done.subscription && !done.subscription.ok
          ? { kind: 'critical', text: t('fb.connect.subscribe_failed') } : null);
      toast(t('fb.connect.done_toast'));
      state.focusKey = 'results';
      await load(true);
    } catch (error) {
      restore();
      if (!alive) {
        forget();
        return;
      }
      const choices = error instanceof ApiError && error.code === 'MULTIPLE_PAGES' && !pageId
        ? error.fields.choices : null;
      if (Array.isArray(choices) && choices.length) {
        window.clearTimeout(pendingTimer);
        state.pending = { token, choices };
        pendingTimer = window.setTimeout(() => {
          forget();
          if (alive) {
            draw();
          }
        }, PENDING_MS);
        report({ kind: 'info', text: t('fb.err.multiple_pages') });
        draw();
        const choice = root.querySelector('[data-key="page-choice"]');
        if (choice instanceof HTMLElement) {
          choice.focus();
        }
        return;
      }
      forget();
      report({ kind: 'critical', text: describe(error) });
      state.focusKey = 'results';
      draw();
    }
  }

  // -- parts ---------------------------------------------------------------------------

  function connectionCard(data) {
    const titleId = uid('fb');
    const head = h('h2', { class: 'block__title', id: titleId, text: t('fb.conn.title') });
    const page = data.page;
    if (!page) {
      return card({ 'aria-labelledby': titleId }, head,
        h('p', { class: 'card__text', text: t('fb.conn.none') }));
    }
    const fields = Array.isArray(page.subscribed_fields) ? page.subscribed_fields : [];
    const last = data.last_event;
    const tokens = Array.isArray(data.tokens) ? data.tokens : [];
    const actions = [
      ['check', 'fb.conn.check', 'key', 'secondary', checkTokens],
      ['subscribe', 'fb.conn.subscribe', 'refresh', 'secondary', repairWebhooks],
      ['probe', 'fb.conn.probe', 'spark', 'secondary', testSending],
      ['disconnect', 'fb.conn.disconnect', 'signout', 'danger', disconnect],
    ].map(([key, text, iconName, variant, run]) => {
      const node = button(t(text), {
        small: true, variant, iconName, onClick: (event) => run(event.currentTarget),
      });
      node.dataset.key = key;
      return node;
    });
    return card({ 'aria-labelledby': titleId }, head,
      h('dl', { class: 'facts' },
        fact(t('fb.conn.page'), page.page_name || page.page_id),
        fact(t('fb.conn.page_id'), h('span', { class: 'code', text: page.page_id })),
        fact(t('fb.conn.connected'), t('fb.conn.connected_value', {
          time: dateTime(page.connected_at), who: page.connected_by || t('audit.system') })),
        fact(t('fb.conn.webhooks'), fields.length
          ? t('fb.conn.subscribed', { fields: fields.join(', ') })
          : h('span', { class: 'user__state' }, statusIcon('critical', 16),
            h('span', { text: t('fb.conn.not_subscribed') }))),
        fact(t('fb.conn.last_webhook'),
          last ? dateTime(last.received_at) : t('fb.conn.never'))),
      h('div', { class: 'block' },
        h('h3', { class: 'card__title', text: t('fb.conn.tokens') }),
        tokens.length ? h('ul', { class: 'audit', 'aria-label': t('fb.conn.tokens') },
          tokens.map(tokenRow)) : h('p', { class: 'card__text', text: t('fb.conn.no_tokens') })),
      h('div', { class: 'form-actions form-actions--start' }, actions),
      h('p', { class: 'page-note' }, icon('info', 16),
        h('span', { text: t('fb.conn.probe_hint') })));
  }

  function tokenForm() {
    const input = textInput({
      type: 'password', autocomplete: 'off', autocapitalize: 'none', autocorrect: 'off',
      spellcheck: 'false', maxlength: 4096, 'aria-required': 'true',
    });
    input.dataset.key = 'token';
    const tokenField = field({
      label: t('fb.connect.token'), hint: t('fb.connect.token_hint'), control: input,
    });
    input.addEventListener('input', () => tokenField.setError(''));
    const submit = button(t('fb.connect.submit'), {
      variant: 'primary', type: 'submit', iconName: 'key',
    });
    const form = h('form', { class: 'form', novalidate: true, autocomplete: 'off' },
      tokenField.node, h('div', { class: 'form-actions form-actions--start' }, submit));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const token = input.value.trim();
      input.value = ''; // Emptied at once: the token is never shown again.
      if (!token) {
        tokenField.setError(t('fb.connect.token_required'));
        input.focus();
        return;
      }
      connect(token, null, submit);
    });
    return form;
  }

  function choiceForm() {
    const options = state.pending.choices.map((page) => ({
      value: String(page.id), label: page.name ? `${page.name} (${page.id})` : String(page.id),
    }));
    const choice = select(options, options[0].value);
    choice.dataset.key = 'page-choice';
    const choiceField = field({ label: t('fb.connect.page'), control: choice });
    const submit = button(t('fb.connect.choose_submit'), { variant: 'primary', type: 'submit' });
    const cancel = button(t('common.cancel'), {
      onClick: () => {
        forget();
        report();
        state.focusKey = 'token';
        draw();
      },
    });
    const form = h('form', { class: 'form', novalidate: true },
      h('p', { class: 'card__text', text: t('fb.connect.choose') }), choiceField.node,
      h('div', { class: 'form-actions form-actions--start' }, submit, cancel));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (state.pending) {
        connect(state.pending.token, choice.value, submit);
      } else {
        draw();
      }
    });
    return form;
  }

  function connectCard(data) {
    const titleId = uid('fb');
    const names = (Array.isArray(data.required_scopes) ? data.required_scopes : []).join(', ');
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId,
        text: t(data.connected ? 'fb.connect.again' : 'fb.connect.title') }),
      h('p', { class: 'card__text', text: t('fb.connect.intro') }),
      h('ol', { class: 'card__text' },
        STEPS.map((step) => h('li', { text: t(`fb.connect.step${step}`, { names }) }))),
      h('div', { class: 'form-actions form-actions--start' },
        h('a', { class: 'btn btn--secondary', href: EXPLORER_URL, target: '_blank',
          rel: 'noopener noreferrer' }, icon('external', 18),
        h('span', { class: 'btn__label', text: t('fb.connect.explorer') }),
        h('span', { class: 'visually-hidden', text: `(${t('nav.new_tab')})` }))),
      h('div', { class: 'block' }, state.pending ? choiceForm() : tokenForm()));
  }

  // -- drawing -------------------------------------------------------------------------

  function render() {
    if (state.status === 'loading') {
      mount(root, loadingBlock(h('div', { class: 'grid' }, Array.from({ length: 2 }, () => h('div',
        { class: 'card' }, skeleton('title'), skeleton('line', 3), skeleton('input'))))));
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
    if (!state.data.configured) {
      mount(root, notConfigured(state.data));
      return;
    }
    mount(root,
      state.results.length ? h('div', { tabindex: '-1', dataset: { key: 'results' } },
        state.results.map((item) => notice(item.kind, item.text))) : null,
      h('div', { class: 'grid' }, connectionCard(state.data), connectCard(state.data)));
  }

  function draw() {
    const active = document.activeElement;
    const key = state.focusKey
      || (active instanceof HTMLElement && root.contains(active) ? active.dataset.key : '');
    state.focusKey = '';
    render();
    if (key) {
      const again = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again instanceof HTMLElement) {
        // The outcome of an action is scrolled into view; anything else stays where it is.
        again.focus({ preventScroll: key !== 'results' });
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
      draw();
    }
    try {
      const data = await fbApi.status(controller.signal);
      if (!alive) {
        return;
      }
      state.data = data;
      state.status = 'ready';
      state.error = null;
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      if (!(silent && state.data)) {
        state.error = error;
        state.status = 'error';
      }
    }
    draw();
  }

  load(false);
  return {
    redraw: draw,
    destroy() {
      alive = false;
      forget();
      if (controller) {
        controller.abort();
      }
    },
  };
}
