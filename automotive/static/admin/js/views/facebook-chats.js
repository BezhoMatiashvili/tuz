// Facebook > Chats: the page's Messenger conversations. The list filters by state and grows
// with "show more"; a chat opens at /admin/facebook/chats/<id> with its messages (customer,
// bot, shop), its own bot switch, the tester flag (in test mode the bot answers only testers),
// the admin-account flag that lets that Facebook account send "#bot off", "#bot on", "#bot test"
// and "#bot status", "needs attention", clearing the bot's memory of a tester's chat, and
// deleting the customer's data.

import { ApiError, isAbort } from '../api.js';
import { h, icon, mount, uid } from '../dom.js';
import { dateTime, relativeTime } from '../format.js';
import { t } from '../strings.js';
import {
  adminOnly, busy, button, card, confirmDialog, emptyState, errorState, field, iconButton,
  isForbidden, loadingBlock, notice, select, skeleton, switchControl, toast,
} from '../ui.js';
import {
  describe, fbApi, label, lines, maskPsid, personName, restartCard, restartNeeded,
} from './facebook-shared.js';

const PAGE = 30;
const MESSAGES = 50;
const FILTERS = ['all', 'attention', 'off', 'ads', 'admin', 'tester'];
const PSID = /^[0-9]{1,30}$/;

function attachmentsText(items) {
  const types = (Array.isArray(items) ? items : []).map((item) => label('fb.attachment',
    item && item.type));
  return types.length ? t('fb.chat.attachments', { types: types.join(', ') }) : '';
}

function errorCard(error, retry) {
  if (isForbidden(error)) {
    return card({}, adminOnly());
  }
  if (restartNeeded(error)) {
    return restartCard();
  }
  if (error instanceof ApiError && error.code === 'META_NOT_CONFIGURED') {
    return card({}, notice('warning', t('fb.not_configured')));
  }
  return card({}, errorState(error, retry));
}

/** Draw, then put keyboard focus back on the control with the same data-key. */
function redrawKeeping(root, render, wanted) {
  const active = document.activeElement;
  const key = wanted || (active instanceof HTMLElement && root.contains(active)
    ? active.dataset.key : '');
  render();
  if (key) {
    const again = root.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (again instanceof HTMLElement) {
      again.focus({ preventScroll: true });
    }
  }
}

function listView(root, memory) {
  const state = { status: 'loading', rows: [], hasMore: false, more: 'idle', error: null };
  let controller = null;
  let alive = true;

  function row(thread) {
    const preview = thread.last_sender
      ? `${label('fb.sender', thread.last_sender)}: ${thread.last_body || t('fb.chats.attachment')}`
      : t('fb.chats.no_messages');
    const when = thread.last_message_at || thread.last_customer_at || thread.first_seen_at;
    return h('li', {}, h('a', {
      class: 'menu__item', href: `/admin/facebook/chats/${thread.psid}`,
      dataset: { link: '', key: `chat-${thread.psid}` },
    },
    h('span', { class: 'menu__label' },
      h('span', { class: 'user__name' }, h('span', { text: personName(thread) }),
        thread.needs_attention ? h('span', { class: 'chip', text: t('fb.chip.attention') }) : null,
        thread.bot_enabled ? null : h('span', { class: 'chip', text: t('fb.chip.bot_off') }),
        thread.is_admin ? h('span', { class: 'chip', text: t('fb.chip.admin') }) : null,
        thread.bot_tester ? h('span', { class: 'chip', text: t('fb.chip.tester') }) : null,
        thread.source === 'ads' ? h('span', { class: 'chip', text: t('fb.chip.ad') }) : null),
      h('span', { class: 'user__username', text: preview })),
    h('span', { class: 'menu__trail' },
      h('span', { class: 'user__seen', text: relativeTime(when) }))));
  }

  function render() {
    const filter = select(FILTERS.map((value) => ({
      value, label: t(`fb.chats.filter.${value}`),
    })), memory.filter);
    filter.dataset.key = 'filter';
    filter.addEventListener('change', () => {
      memory.filter = filter.value;
      load(true);
    });
    const refresh = iconButton('refresh', t('fb.refresh'), { onClick: () => load(true) });
    refresh.dataset.key = 'refresh';
    let body;
    if (state.status === 'loading') {
      body = loadingBlock(h('div', { class: 'card users' }, Array.from({ length: 4 },
        () => h('div', { class: 'user user--skeleton' }, skeleton('line'),
          skeleton('line-short')))));
    } else if (state.status === 'error') {
      body = errorCard(state.error, () => load(true));
    } else if (!state.rows.length) {
      body = card({}, emptyState({ iconName: 'search', title: t(memory.filter === 'all'
        ? 'fb.chats.empty' : 'fb.chats.empty_filter') }));
    } else {
      body = h('div', { class: 'card menu' },
        h('ul', { 'aria-label': t('fb.chats.list') }, state.rows.map(row)));
    }
    let footer = null;
    if (state.status === 'ready' && state.rows.length) {
      if (state.more === 'error') {
        footer = notice('critical', describe(state.error),
          button(t('common.retry'), { small: true, onClick: () => load(false) }));
      } else if (state.hasMore) {
        footer = button(state.more === 'loading' ? t('common.loading') : t('fb.chats.more'), {
          block: true, disabled: state.more === 'loading', onClick: () => load(false),
        });
      } else {
        footer = h('p', { class: 'results__end', text: t('fb.chats.end') });
      }
    }
    mount(root,
      h('div', { class: 'toolbar toolbar--wrap' },
        field({ label: t('fb.chats.filter'), control: filter }).node, refresh),
      body,
      footer ? h('div', { class: 'results__footer' }, footer) : null);
  }

  async function load(reset, focusKey) {
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
    redrawKeeping(root, render);
    let wanted = focusKey;
    try {
      const response = await fbApi.threads({ filter: memory.filter, limit: PAGE, offset },
        controller.signal);
      if (!alive) {
        return;
      }
      const rows = Array.isArray(response.threads) ? response.threads : [];
      state.rows = reset ? rows : [...state.rows, ...rows];
      state.hasMore = Boolean(response.has_more);
      state.status = 'ready';
      state.more = 'idle';
      if (!reset && rows.length) {
        wanted = `chat-${rows[0].psid}`; // keyboard users go on from the first new chat
      }
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      state.error = error;
      if (reset) {
        state.status = 'error';
      } else {
        state.more = 'error';
      }
    }
    redrawKeeping(root, render, wanted);
  }

  load(true, memory.focus || (memory.lastChat ? `chat-${memory.lastChat}` : ''));
  memory.focus = '';
  return {
    redraw: () => redrawKeeping(root, render),
    destroy() {
      alive = false;
      if (controller) {
        controller.abort();
      }
    },
  };
}

function detailView(root, ctx, psid, memory) {
  const state = {
    status: 'loading', thread: null, messages: [], hasOlder: false, older: 'idle', error: null,
  };
  let controller = null;
  let alive = true;
  let first = true;
  memory.lastChat = psid;

  /** Focus the control with ``key`` once its switch is enabled again (a switch is disabled,
   * and so loses focus, while its request runs). */
  function refocus(key) {
    window.setTimeout(() => {
      const node = alive ? root.querySelector(`[data-key="${CSS.escape(key)}"]`) : null;
      const active = document.activeElement;
      if (node instanceof HTMLElement && (!active || active === document.body)) {
        node.focus({ preventScroll: true });
      }
    }, 0);
  }

  async function save(fields, message, key) {
    try {
      const result = await fbApi.saveThread(psid, { ...fields, version: state.thread.version });
      if (alive) {
        state.thread = result.thread;
        toast(message);
        redrawKeeping(root, render, key);
      }
    } catch (error) {
      if (alive) {
        if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
          toast(t('fb.chat.conflict'), 'critical');
          await load(false);
        } else {
          toast(describe(error), 'critical');
        }
        refocus(key);
      }
      throw error; // the switch goes back
    }
  }

  async function removeData(trigger) {
    const sure = await confirmDialog({
      opener: trigger, title: t('fb.chat.delete_title'), text: t('fb.chat.delete_text'),
      extra: t('fb.chat.delete_extra'), confirmLabel: t('common.delete'), danger: true,
    });
    if (!sure) {
      return;
    }
    const restore = busy(trigger, t('common.deleting'));
    try {
      await fbApi.deleteThread(psid);
      memory.lastChat = '';
      memory.focus = 'filter'; // the chat's row is gone; the list starts at its filter
      toast(t('fb.chat.deleted'));
      ctx.navigate('/admin/facebook/chats', { replace: true });
    } catch (error) {
      restore();
      toast(describe(error), 'critical');
    }
  }

  /** A tester's chat: the bot forgets it and reads nothing written before. The messages stay. */
  async function clearMemory(trigger) {
    const sure = await confirmDialog({
      opener: trigger, title: t('fb.chat.clear_title'), text: t('fb.chat.clear_text'),
      extra: t('fb.chat.clear_extra'), confirmLabel: t('fb.chat.clear_confirm'),
    });
    if (!sure) {
      return;
    }
    const restore = busy(trigger, t('fb.chat.clearing'));
    try {
      const result = await fbApi.clearContext(psid, state.thread.version);
      if (alive) {
        state.thread = result.thread;
        toast(t('fb.chat.cleared'));
        redrawKeeping(root, render, 'clear');
      }
    } catch (error) {
      restore();
      if (!alive) {
        return;
      }
      if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
        toast(t('fb.chat.conflict'), 'critical');
        await load(false);
      } else {
        toast(describe(error), 'critical');
      }
      refocus('clear');
    }
  }

  async function loadOlder() {
    if (!state.messages.length) {
      return;
    }
    state.older = 'loading';
    redrawKeeping(root, render, 'older');
    try {
      const response = await fbApi.thread(psid, {
        limit: MESSAGES, before: state.messages[0].message_id });
      if (!alive) {
        return;
      }
      const seen = new Set(state.messages.map((message) => message.message_id));
      const older = (response.messages || []).filter((message) => !seen.has(message.message_id));
      state.messages = [...older, ...state.messages];
      state.hasOlder = Boolean(response.has_older);
      state.older = 'idle';
    } catch (error) {
      if (!alive) {
        return;
      }
      state.error = error;
      state.older = 'error';
    }
    redrawKeeping(root, render, 'older');
  }

  function toggle(key, checked, labelKey, hintKey, onChange) {
    const hintId = uid('fb');
    const control = switchControl({ checked, label: t(labelKey), onChange });
    control.input.dataset.key = key;
    control.input.setAttribute('aria-describedby', hintId);
    return {
      control,
      node: h('div', { class: 'field field--switch' }, control.node,
        h('p', { class: 'field__hint', id: hintId, text: t(hintKey) })),
    };
  }

  function chatCard(thread) {
    const titleId = uid('fb');
    const facts = [t('fb.chat.id', { id: maskPsid(thread.psid) })];
    if (thread.first_seen_at) {
      facts.push(t('fb.chat.first_seen', { time: dateTime(thread.first_seen_at) }));
    }
    if (thread.source === 'ads' && thread.source_ad_id) {
      facts.push(t('fb.chat.from_ad', { id: thread.source_ad_id }));
    }
    const bot = toggle('bot', thread.bot_enabled, 'fb.chat.bot_switch', 'fb.chat.bot_hint',
      (on) => save({ bot_enabled: on }, t(on ? 'fb.chat.bot_on' : 'fb.chat.bot_off'), 'bot'));
    const admin = toggle('admin', thread.is_admin, 'fb.chat.admin_switch', 'fb.chat.admin_hint',
      async (on) => {
        if (on && !(await confirmDialog({
          opener: admin.control.input, title: t('fb.chat.admin_title'),
          text: t('fb.chat.admin_text', { name: personName(thread) }),
          confirmLabel: t('fb.chat.admin_confirm'),
        }))) {
          refocus('admin');
          throw new Error('cancelled'); // the switch goes back
        }
        await save({ is_admin: on }, t(on ? 'fb.chat.admin_on' : 'fb.chat.admin_off'), 'admin');
      });
    const tester = toggle('tester', Boolean(thread.bot_tester), 'fb.chat.tester_switch',
      'fb.chat.tester_hint', (on) => save({ bot_tester: on },
        t(on ? 'fb.chat.tester_on' : 'fb.chat.tester_off'), 'tester'));
    const attention = toggle('attention', thread.needs_attention, 'fb.chat.attention_switch',
      'fb.chat.attention_hint', (on) => save({ needs_attention: on },
        t(on ? 'fb.chat.attention_on' : 'fb.chat.attention_off'), 'attention'));
    let memoryNode = null;
    if (thread.bot_tester) { // only a tester's chat can be cleared (the server refuses others)
      const hintId = uid('fb');
      const hint = [t('fb.chat.clear_hint')];
      if (thread.context_cleared_at) {
        hint.push(t('fb.chat.clear_last', { time: dateTime(thread.context_cleared_at) }));
      }
      const clear = button(t('fb.chat.clear'), {
        iconName: 'refresh', 'aria-describedby': hintId,
        onClick: (event) => clearMemory(event.currentTarget),
      });
      clear.dataset.key = 'clear';
      memoryNode = h('div', { class: 'field' }, h('div', {}, clear),
        h('p', { class: 'field__hint', id: hintId, text: hint.join(' ') }));
    }
    return card({ class: 'stack', 'aria-labelledby': titleId },
      h('h2', { class: 'card__title', id: titleId, tabindex: '-1', dataset: { key: 'title' },
        text: personName(thread) }),
      h('p', { class: 'card__text', text: facts.join(' · ') }),
      thread.needs_attention && thread.attention_reason ? notice('warning', t('fb.chat.reason',
        { reason: label('fb.reason', thread.attention_reason) })) : null,
      bot.node, tester.node, memoryNode, admin.node, attention.node);
  }

  /** Where the bot's memory was last cleared, among the messages shown: the index of the first
   * message sent after it (the line goes before that one, or after the last message), or -1
   * when the clear lies before everything loaded so far. */
  function clearedBefore() {
    const cleared = Date.parse(state.thread && state.thread.context_cleared_at);
    if (Number.isNaN(cleared)) {
      return -1;
    }
    const index = state.messages.findIndex((message) => Date.parse(message.sent_at) > cleared);
    if (index === 0) {
      return -1;
    }
    return index < 0 ? state.messages.length : index;
  }

  function messagesCard() {
    const titleId = uid('fb');
    let older = null;
    if (state.hasOlder && state.older === 'error') {
      older = notice('critical', describe(state.error),
        button(t('common.retry'), { small: true, onClick: loadOlder }));
    } else if (state.hasOlder) {
      older = button(state.older === 'loading' ? t('common.loading') : t('fb.chat.older'), {
        block: true, small: true, disabled: state.older === 'loading', onClick: loadOlder,
      });
      older.dataset.key = 'older';
    }
    const items = state.messages.map((message) => {
      const attached = attachmentsText(message.attachments);
      // The bot's rows are ordered just after the message they answer; stored_at is when the
      // reply was actually sent.
      const when = message.sender === 'bot' && message.stored_at ? message.stored_at
        : message.sent_at;
      return h('li', { class: 'audit__row' },
        h('p', { class: 'audit__what' },
          h('strong', { class: 'audit__who', text: label('fb.sender', message.sender) }),
          h('time', { class: 'muted', datetime: when, text: dateTime(when) })),
        message.body ? h('p', {}, lines(message.body)) : null,
        attached ? h('p', { class: 'audit__when', text: attached }) : null,
        message.referral_ad_id ? h('p', { class: 'audit__when',
          text: t('fb.chat.from_ad', { id: message.referral_ad_id }) }) : null);
    });
    const cut = clearedBefore();
    if (cut > 0) {
      items.splice(cut, 0, h('li', { class: 'audit__row audit__row--cleared' },
        h('p', { class: 'audit__when', text: t('fb.chat.cleared_line',
          { time: dateTime(state.thread.context_cleared_at) }) })));
    }
    return card({ 'aria-labelledby': titleId },
      h('h2', { class: 'block__title', id: titleId, text: t('fb.chat.messages') }),
      older,
      items.length ? h('ol', { class: 'audit', 'aria-label': t('fb.chat.messages') }, items)
        : h('p', { class: 'card__text', text: t('fb.chat.no_messages') }));
  }

  function dataCard() {
    const titleId = uid('fb');
    const remove = button(t('fb.chat.delete'), {
      variant: 'danger', iconName: 'trash', onClick: (event) => removeData(event.currentTarget),
    });
    return card({ class: 'stack', 'aria-labelledby': titleId },
      h('h2', { class: 'card__title', id: titleId, text: t('fb.chat.data') }),
      h('p', { class: 'card__text', text: t('fb.chat.data_text') }),
      h('div', {}, remove));
  }

  function render() {
    const back = h('div', { class: 'toolbar' }, h('a', {
      class: 'btn btn--ghost btn--small', href: '/admin/facebook/chats',
      dataset: { link: '', key: 'back' },
    }, icon('chevron-left', 18), h('span', { class: 'btn__label', text: t('fb.chat.back') })));
    if (state.status === 'loading') {
      mount(root, back, loadingBlock(h('div', { class: 'card' }, skeleton('title'),
        skeleton('line', 3))));
      return;
    }
    if (state.status === 'error') {
      const missing = state.error instanceof ApiError && state.error.status === 404
        && state.error.code === 'NOT_FOUND';
      mount(root, back, missing ? card({}, emptyState({ iconName: 'search',
        title: t('fb.chat.missing') })) : errorCard(state.error, () => load(true)));
      return;
    }
    mount(root, back, h('div', { class: 'stack' }, chatCard(state.thread), messagesCard(),
      dataCard()));
  }

  async function load(full) {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    if (full) {
      state.status = 'loading';
      redrawKeeping(root, render);
    }
    try {
      const response = await fbApi.thread(psid, { limit: MESSAGES }, controller.signal);
      if (!alive) {
        return;
      }
      state.thread = response.thread;
      state.messages = Array.isArray(response.messages) ? response.messages : [];
      state.hasOlder = Boolean(response.has_older);
      state.status = 'ready';
      state.older = 'idle';
      state.error = null;
    } catch (error) {
      if (isAbort(error) || !alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    // Arriving from the list, keyboard and screen-reader users start at the chat's name.
    redrawKeeping(root, render, first && state.status === 'ready' ? 'title' : '');
    first = false;
  }

  load(true);
  return {
    redraw: () => redrawKeeping(root, render),
    destroy() {
      alive = false;
      if (controller) {
        controller.abort();
      }
    },
  };
}

export function mountView(root, ctx, params) {
  // Kept while the Chats tab is open: the filter, the chat to return to, a focus request.
  const memory = { filter: 'all', lastChat: '', focus: '' };
  let view = null;

  function show(next) {
    const psid = next && PSID.test(next.rest || '') ? next.rest : '';
    if (view) {
      view.destroy();
    }
    ctx.setGuard(null);
    view = psid ? detailView(root, ctx, psid, memory) : listView(root, memory);
  }

  show(params);
  return {
    update: show,
    redraw() {
      if (view) {
        view.redraw();
      }
    },
    destroy() {
      if (view) {
        view.destroy();
      }
    },
  };
}
