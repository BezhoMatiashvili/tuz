// Facebook > Comments: customers' comments on the page's posts and ads, newest first, with what
// the bot did about each (a private reply in Messenger, or why it did not answer). The admin can
// hide or show a comment on Facebook and post a public reply typed here; both act on Facebook at
// once and are written to the audit log. A public reply is posted once: when Meta's answer is
// lost the dialog says the reply may already be there and offers no second try.

import { isAbort } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { dateTime, num, relativeTime } from '../format.js';
import { t } from '../strings.js';
import {
  busy, button, card, emptyState, field, iconButton, link, loadingBlock, notice, openDialog,
  select, skeleton, textArea, toast,
} from '../ui.js';
import {
  activityApi, describeAction, errorCard, excerpt, externalLink, jobButton,
} from './facebook-activity.js';
import { describe, label, lines } from './facebook-shared.js';

const PAGE = 30;
const FILTERS = ['all', 'unreplied'];
const REPLY_LIMIT = 2000;
// Reply statuses that mean the customer got nothing from the bot.
const PROBLEMS = new Set(['failed', 'blocked_by_meta', 'abandoned', 'paused_provider']);

export function mountView(root) {
  const state = {
    filter: 'all', status: 'loading', rows: [], total: 0, unreplied: 0, hasMore: false,
    more: 'idle', error: null, switch: null,
  };
  let controller = null;
  let alive = true;

  function chips(row) {
    const found = [];
    if (row.is_hidden) {
      found.push(t('fb.comments.chip.hidden'));
    }
    if (row.public_reply_id || row.replied_publicly) {
      found.push(t('fb.comments.chip.public_reply'));
    }
    if (row.source === 'backfill') {
      found.push(t('fb.comments.chip.backfill'));
    }
    return found.map((text) => h('span', { class: 'chip', text }));
  }

  function botLine(row) {
    if (!row.reply_status) {
      return null;
    }
    const kind = row.reply_status === 'sent' ? 'good'
      : PROBLEMS.has(row.reply_status) ? 'critical' : 'neutral';
    return h('p', { class: 'page-note' }, icon(kind === 'good' ? 'check' : 'info', 16),
      h('span', { text: label('fb.comments.status', row.reply_status) }));
  }

  async function toggleHidden(row, trigger) {
    const restore = busy(trigger);
    try {
      const result = await activityApi.hideComment(row.comment_id, !row.is_hidden);
      row.is_hidden = Boolean(result && result.hidden);
      toast(t(row.is_hidden ? 'fb.comments.hidden_done' : 'fb.comments.shown_done'));
      if (alive) {
        render(`hide-${row.comment_id}`);
      }
    } catch (error) {
      restore();
      toast(describeAction(error), 'critical');
    }
  }

  function replyDialog(row, opener) {
    return openDialog({
      title: t('fb.comments.reply_title'),
      opener,
      keepOpen: true,
      build: (close) => {
        const input = textArea({ maxlength: REPLY_LIMIT, rows: 5, required: true });
        const box = field({
          label: t('fb.comments.reply_label'), control: input, counter: true,
          hint: t('fb.comments.reply_hint'),
        });
        const count = () => box.setCounter(`${input.value.length} / ${REPLY_LIMIT}`,
          input.value.length > REPLY_LIMIT);
        input.addEventListener('input', () => {
          box.setError('');
          count();
        });
        count();
        const message = h('div', {});
        const cancel = button(t('common.cancel'), { onClick: () => close(null) });
        const post = button(t('fb.comments.reply_post'), { variant: 'primary', type: 'submit' });
        const actions = h('div', { class: 'dialog__actions' }, cancel, post);
        const form = h('form', { class: 'dialog__form', novalidate: true },
          h('p', { class: 'dialog__text', text: t('fb.comments.reply_to',
            { name: row.author_name || t('fb.comments.someone') }) }),
          h('blockquote', { class: 'dialog__text muted' }, lines(excerpt(row.body, 300))),
          box.node, message, actions);
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const text = input.value.trim();
          if (!text) {
            box.setError(t('fb.comments.reply_empty'));
            input.focus();
            return;
          }
          close.lock(true);
          const restore = busy(post, t('fb.comments.reply_posting'));
          try {
            const result = await activityApi.replyToComment(row.comment_id, text);
            // Meta may post the reply without returning its id; it is there all the same.
            row.public_reply_id = (result && result.public_reply_id) || row.public_reply_id;
            row.replied_publicly = true;
            close.lock(false);
            close(true);
          } catch (error) {
            close.lock(false);
            if (error && error.fields && error.fields.maybe_applied === true) {
              // It may be on Facebook already: say so, and offer no second try.
              mount(message, notice('critical', describeAction(error)));
              mount(actions, button(t('common.close'), { onClick: () => close(null) }));
              return;
            }
            restore();
            box.setError(describeAction(error));
          }
        });
        return form;
      },
    }).then((posted) => {
      if (posted === true) {
        toast(t('fb.comments.reply_done'));
        if (alive) {
          render(`reply-${row.comment_id}`);
        }
      }
    });
  }

  function item(row) {
    const hide = iconButton(row.is_hidden ? 'eye' : 'eye-off',
      t(row.is_hidden ? 'fb.comments.show' : 'fb.comments.hide'));
    hide.dataset.key = `hide-${row.comment_id}`;
    hide.addEventListener('click', () => toggleHidden(row, hide));
    const reply = button(t('fb.comments.reply'), { small: true, iconName: 'edit' });
    reply.dataset.key = `reply-${row.comment_id}`;
    reply.addEventListener('click', () => replyDialog(row, reply));
    const when = row.created_time;
    return h('li', {}, card({ class: 'stack' },
      h('div', { class: 'photo__top' },
        h('span', { class: 'user__name' }, h('strong', {
          text: row.author_name || t('fb.comments.someone'),
        }), chips(row)),
        h('time', { class: 'photo__meta', datetime: when, title: dateTime(when),
          text: relativeTime(when) })),
      h('p', { class: 'card__text' }, lines(row.body || t('fb.comments.no_text'))),
      row.post_excerpt ? h('p', { class: 'page-note' }, icon('content', 16),
        h('span', { text: t('fb.comments.on_post', { text: excerpt(row.post_excerpt, 120) }) }))
        : null,
      botLine(row),
      h('div', { class: 'photo__actions' },
        reply, hide,
        row.replied_psid ? link(`/admin/facebook/chats/${row.replied_psid}`,
          t('fb.comments.open_chat'), { class: 'btn btn--ghost btn--small' }) : null,
        externalLink(row.link, t('fb.act.open_facebook')))));
  }

  function notices() {
    const found = [];
    const sw = state.switch;
    if (sw && sw.bot_paused_reason) {
      found.push(notice('warning', t('fb.comments.paused')));
    } else if (sw && !sw.comments_enabled) {
      found.push(notice('info', t('fb.comments.switch_off')));
    }
    return found;
  }

  function render(focusKey) {
    if (!alive) {
      return;
    }
    const active = document.activeElement;
    const key = focusKey || (active instanceof HTMLElement && root.contains(active)
      ? active.dataset.key : '');
    const filter = select(FILTERS.map((value) => ({
      value, label: t(`fb.comments.filter.${value}`),
    })), state.filter);
    filter.dataset.key = 'filter';
    filter.addEventListener('change', () => {
      state.filter = filter.value;
      load(true);
    });
    const refresh = iconButton('refresh', t('fb.refresh'), { onClick: () => load(true) });
    refresh.dataset.key = 'refresh';
    let body;
    if (state.status === 'loading') {
      body = loadingBlock(card({ class: 'stack' }, skeleton('line', 3), skeleton('line-short')));
    } else if (state.status === 'error') {
      body = errorCard(state.error, () => load(true));
    } else if (!state.rows.length) {
      body = card({}, emptyState({
        iconName: 'content',
        title: t(state.filter === 'all' ? 'fb.comments.empty' : 'fb.comments.empty_unreplied'),
        text: state.filter === 'all' ? t('fb.comments.empty_text') : null,
      }));
    } else {
      body = h('ul', { class: 'list__items', 'aria-label': t('fb.comments.list') },
        state.rows.map(item));
    }
    let footer = null;
    if (state.status === 'ready' && state.rows.length) {
      if (state.more === 'error') {
        footer = notice('critical', describe(state.error),
          button(t('common.retry'), { small: true, onClick: () => load(false) }));
      } else if (state.hasMore) {
        footer = button(state.more === 'loading' ? t('common.loading') : t('fb.comments.more'), {
          block: true, disabled: state.more === 'loading', onClick: () => load(false),
        });
      } else {
        footer = h('p', { class: 'results__end', text: t('fb.comments.end') });
      }
    }
    mount(root,
      h('div', { class: 'toolbar toolbar--wrap' },
        field({ label: t('fb.comments.filter'), control: filter }).node, refresh,
        jobButton('comments')),
      notices(),
      state.status === 'ready' ? h('p', { class: 'page-head__note', text: t('fb.comments.summary',
        { total: num(state.total), unreplied: num(state.unreplied) }) }) : null,
      body,
      footer ? h('div', { class: 'results__footer' }, footer) : null);
    if (key) {
      const again = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again instanceof HTMLElement) {
        again.focus({ preventScroll: true });
      }
    }
  }

  async function load(reset) {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    const offset = reset ? 0 : state.rows.length;
    if (reset) {
      state.status = 'loading';
    } else {
      state.more = 'loading';
    }
    state.error = null;
    render();
    try {
      const data = await activityApi.comments({
        limit: PAGE, offset, unreplied: state.filter === 'unreplied' ? 'true' : null,
      }, controller.signal);
      state.rows = reset ? data.comments : [...state.rows, ...data.comments];
      state.total = data.total;
      state.unreplied = data.unreplied;
      state.hasMore = Boolean(data.has_more);
      state.switch = data.switch || null;
      state.status = 'ready';
      state.more = 'idle';
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
    }
    render();
  }

  load(true);

  return {
    redraw: () => render(),
    destroy() {
      alive = false;
      if (controller) {
        controller.abort();
      }
    },
  };
}
