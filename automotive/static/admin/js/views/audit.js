// Audit log (admin): who did what, newest first, with "load older" paging.

import { api, describeError, isAbort } from '../api.js';
import { h, mount } from '../dom.js';
import { dateTime } from '../format.js';
import { has, t } from '../strings.js';
import {
  adminOnly, button, card, emptyState, errorState, isForbidden, loadingBlock, notice, pageHead,
  skeleton,
} from '../ui.js';

const PAGE = 50;

function actionLabel(action) {
  const key = `audit.action.${action}`;
  return has(key) ? t(key) : String(action || '');
}

function detailText(detail) {
  if (!detail || typeof detail !== 'object') {
    return '';
  }
  return Object.entries(detail).map(([key, value]) => {
    const label = has(`audit.detail.${key}`) ? t(`audit.detail.${key}`) : key;
    let shown = String(value);
    if (typeof value === 'boolean') {
      shown = t(value ? 'audit.yes' : 'audit.no');
    } else if (key === 'role' && has(`role.${value}`)) {
      shown = t(`role.${value}`);
    }
    return `${label}: ${shown}`;
  }).join(', ');
}

export function mountView(root, ctx) {
  const state = { status: 'loading', error: null, rows: [], more: 'idle', done: false };
  let controller = null;
  ctx.setGuard(null);

  function entry(row) {
    const detail = detailText(row.detail);
    return h('li', { class: 'audit__row' },
      h('p', { class: 'audit__what' },
        h('strong', { class: 'audit__who', text: row.actor_username || t('audit.system') }),
        h('span', { text: actionLabel(row.action) }),
        row.target && h('span', { class: 'chip code', text: row.target })),
      h('p', { class: 'audit__when' },
        h('time', { datetime: row.occurred_at, text: dateTime(row.occurred_at) }),
        detail && h('span', { text: ` · ${detail}` })));
  }

  function draw(focusId) {
    const head = pageHead(t('audit.title'));
    head.appendChild(h('p', { class: 'page-head__note', text: t('audit.intro') }));
    if (state.status === 'loading') {
      mount(root, head, loadingBlock(h('div', { class: 'card audit' },
        Array.from({ length: 8 }, () => h('div', { class: 'audit__row' }, skeleton('line'), skeleton('line-short'))))));
      return;
    }
    if (state.status === 'error') {
      mount(root, head, isForbidden(state.error) ? adminOnly() : card({}, errorState(state.error, () => load(true))));
      return;
    }
    if (!state.rows.length) {
      mount(root, head, card({}, emptyState({ iconName: 'audit', title: t('audit.empty') })));
      return;
    }
    const items = state.rows.map((row) => {
      const node = entry(row);
      node.dataset.audit = String(row.audit_id);
      return node;
    });
    let footer;
    if (state.more === 'error') {
      footer = notice('critical', describeError(state.error),
        button(t('common.retry'), { small: true, onClick: () => load(false) }));
    } else if (state.done) {
      footer = h('p', { class: 'results__end', text: t('audit.end') });
    } else {
      footer = button(state.more === 'loading' ? t('common.loading') : t('audit.older'), {
        block: true, disabled: state.more === 'loading', onClick: () => load(false),
      });
    }
    mount(root, head,
      h('ol', { class: 'card audit', 'aria-label': t('audit.list') }, items),
      h('div', { class: 'results__footer' }, footer));
    if (focusId) {
      const first = items.find((node) => node.dataset.audit === String(focusId));
      if (first) {
        first.setAttribute('tabindex', '-1');
        first.focus({ preventScroll: true }); // Keyboard users continue from the first new row.
      }
    }
  }

  async function load(reset) {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    const before = reset || !state.rows.length ? null : state.rows[state.rows.length - 1].audit_id;
    if (reset) {
      state.status = 'loading';
    } else {
      state.more = 'loading';
    }
    state.error = null;
    draw();
    try {
      const response = await api.audit(before, controller.signal);
      const rows = Array.isArray(response.rows) ? response.rows : [];
      state.rows = reset ? rows : [...state.rows, ...rows];
      state.done = rows.length < PAGE;
      state.status = 'ready';
      state.more = 'idle';
      draw(reset || !rows.length ? null : rows[0].audit_id);
    } catch (error) {
      if (isAbort(error)) {
        return;
      }
      state.error = error;
      if (reset) {
        state.status = 'error';
      } else {
        state.more = 'error';
      }
      draw();
    }
  }

  load(true);
  return {
    redraw: () => draw(),
    destroy() {
      if (controller) {
        controller.abort();
      }
    },
  };
}
