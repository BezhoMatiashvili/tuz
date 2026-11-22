// Prompts (admin): every text this project sends to a language model. The live bot's
// instructions can be edited here; everything else is shown so the owner can read it.
// /admin/prompts lists them by where they run; /admin/prompts/<key> opens one.

import { ApiError, api } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { dateTime, num } from '../format.js';
import { pick, t } from '../strings.js';
import {
  adminOnly, announce, busy, button, card, confirmDialog, emptyState, errorState, isForbidden,
  loadingBlock, notice, pageHead, skeleton, toast, toastError,
} from '../ui.js';

const KEY = /^[a-z][a-z0-9_.]{1,63}$/;

/** The server runs code from before this page: it has no /api/admin/prompts route yet. */
function notInstalled(error) {
  return error instanceof ApiError && error.status === 404 && error.code !== 'NOT_FOUND';
}

function storeNotice(store) {
  if (store === 'unavailable') {
    return notice('warning', t('prompts.store_unavailable'));
  }
  if (store === 'off') {
    return notice('warning', t('prompts.store_off'));
  }
  return null;
}

function stateChip(item) {
  if (!item.editable) {
    return h('span', { class: 'chip', text: t('prompts.view_only') });
  }
  return item.edited
    ? h('span', { class: 'chip chip--warning', text: t('prompts.edited') })
    : h('span', { class: 'chip', text: t('prompts.original') });
}

function editedText(item) {
  return t('prompts.edited_by', {
    who: item.updated_by || t('audit.system'), when: dateTime(item.updated_at),
  });
}

function backLink() {
  return h('div', { class: 'toolbar' }, h('a', {
    class: 'btn btn--ghost btn--small', href: '/admin/prompts', dataset: { link: '' },
  }, icon('chevron-left', 18), h('span', { class: 'btn__label', text: t('prompts.back') })));
}

export function mountView(root, ctx, params) {
  const rest = params.rest || '';
  const key = KEY.test(rest) ? rest : '';
  const state = { list: null, item: null, original: '', saving: false };
  let alive = true;
  let controller = null;
  let editor = null;
  let counter = null;
  let saveButton = null;
  let discardButton = null;
  ctx.setGuard(null);

  const dirty = () => Boolean(editor && state.item && state.item.editable
    && editor.value !== state.original);

  function failed(error, retry) {
    const top = pageHead(t('prompts.title'));
    if (isForbidden(error)) {
      mount(root, top, card({}, adminOnly()));
    } else if (notInstalled(error)) {
      mount(root, top, notice('warning', t('prompts.not_installed')));
    } else {
      mount(root, key ? backLink() : null, top, card({}, errorState(error, retry)));
    }
  }

  // -- the list ---------------------------------------------------------------------------

  async function loadList() {
    controller = new AbortController();
    mount(root, pageHead(t('prompts.title')), loadingBlock(h('div', { class: 'card' },
      skeleton('title'), skeleton('line', 6))));
    try {
      state.list = await api.prompts(controller.signal);
    } catch (error) {
      if (alive && error.name !== 'AbortError') {
        failed(error, loadList);
      }
      return;
    }
    if (alive) {
      drawList();
    }
  }

  function drawList() {
    const payload = state.list;
    const top = pageHead(t('prompts.title'));
    top.appendChild(h('p', { class: 'page-head__note', text: t('prompts.intro') }));
    const groups = payload.groups.map((group) => {
      const items = payload.prompts.filter((item) => item.group === group.key);
      if (!items.length) {
        return null;
      }
      const titleId = `prompts-group-${group.key}`;
      return card({ class: 'prompt-group', 'aria-labelledby': titleId },
        h('h2', { class: 'card__title', id: titleId, text: pick(group.title) }),
        h('p', { class: 'prompt-group__about', text: pick(group.about) }),
        h('ul', { class: 'prompt-list' }, items.map((item) => h('li', {},
          h('a', { class: 'prompt-row', href: `/admin/prompts/${item.key}`, dataset: { link: '' } },
            h('span', { class: 'prompt-row__body' },
              h('span', { class: 'prompt-row__title', text: pick(item.title) }),
              h('span', { class: 'prompt-row__about', text: pick(item.about) }),
              item.edited && h('span', { class: 'prompt-row__meta', text: editedText(item) })),
            stateChip(item),
            h('span', { class: 'prompt-row__go' }, icon('chevron-right', 16)))))));
    });
    mount(root, top, storeNotice(payload.store), h('div', { class: 'prompt-groups' }, groups));
  }

  // -- one prompt -------------------------------------------------------------------------

  async function loadOne() {
    controller = new AbortController();
    mount(root, backLink(), loadingBlock(h('div', { class: 'card' },
      skeleton('title'), skeleton('line', 10))));
    let item;
    try {
      item = await api.prompt(key, controller.signal);
    } catch (error) {
      if (alive && error.name !== 'AbortError') {
        failed(error, loadOne);
      }
      return;
    }
    if (alive) {
      state.item = item;
      state.original = item.text;
      drawOne();
    }
  }

  function refreshControls() {
    if (!editor || !saveButton) {
      return;
    }
    const length = editor.value.length;
    const max = state.item.max_chars;
    counter.textContent = t('prompts.chars', { count: num(length), max: num(max) });
    counter.classList.toggle('field__counter--over', length > max);
    const changed = dirty();
    saveButton.disabled = !changed || state.saving || !editor.value.trim() || length > max;
    discardButton.disabled = !changed || state.saving;
  }

  function saveFailed(error) {
    if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
      toast(t('prompts.conflict'), 'critical');
    } else if (error instanceof ApiError && error.code === 'PROMPTS_UNAVAILABLE') {
      toast(t('prompts.store_unavailable'), 'critical');
    } else if (error instanceof ApiError && error.fields && error.fields.text === 'REQUIRED') {
      toast(t('prompts.err_blank'), 'critical');
    } else if (error instanceof ApiError && error.fields && error.fields.text === 'TOO_LONG') {
      toast(t('prompts.err_long'), 'critical');
    } else {
      toastError(error);
    }
  }

  async function save() {
    if (!dirty() || state.saving || !editor.value.trim()) {
      return;
    }
    state.saving = true;
    const restore = busy(saveButton, t('common.saving'));
    try {
      const saved = await api.savePrompt(state.item.key, state.item.version, editor.value);
      const message = t(saved.edited ? 'prompts.saved' : 'prompts.saved_original');
      toast(message);
      announce(message);
      state.saving = false;
      state.original = editor.value; // nothing unsaved while the page reloads
      await loadOne();
      return;
    } catch (error) {
      saveFailed(error);
    }
    state.saving = false;
    restore();
    refreshControls();
  }

  async function reset(opener) {
    const ok = await confirmDialog({
      title: t('prompts.reset_title'), text: t('prompts.reset_text'),
      confirmLabel: t('prompts.reset'), danger: true, opener,
    });
    if (!ok || !alive) {
      return;
    }
    const restore = busy(opener, t('common.saving'));
    try {
      await api.resetPrompt(state.item.key, state.item.version);
      toast(t('prompts.reset_done'));
      announce(t('prompts.reset_done'));
      state.original = editor.value;
      await loadOne();
    } catch (error) {
      restore();
      saveFailed(error);
    }
  }

  /** Put ``text`` in the editor as an unsaved change. */
  function loadText(text) {
    editor.value = text;
    refreshControls();
    editor.focus();
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
    announce(t('prompts.loaded'));
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t('prompts.copied'));
    } catch {
      editor.focus();
      editor.select();
      toast(t('prompts.copy_failed'), 'critical');
    }
  }

  function historyCard(item) {
    if (!item.editable || !item.history || !item.history.length) {
      return null;
    }
    return card({ class: 'prompt-history', 'aria-labelledby': 'prompt-history-title' },
      h('h2', { class: 'card__title', id: 'prompt-history-title', text: t('prompts.history') }),
      h('ol', { class: 'prompt-history__list' }, item.history.map((row) => h('li', {
        class: 'prompt-history__row',
      },
      h('div', { class: 'prompt-history__body' },
        h('p', { class: 'prompt-history__what' },
          h('strong', { text: row.saved_by || t('audit.system') }),
          h('span', { text: t(row.action === 'reset' ? 'prompts.history_reset'
            : 'prompts.history_saved', { version: row.version }) })),
        h('p', { class: 'prompt-history__when' },
          h('time', { datetime: row.saved_at, text: dateTime(row.saved_at) }),
          row.action === 'saved' && h('span', {
            text: ` · ${t('prompts.chars_short', { count: num(row.body.length) })}`,
          }))),
      row.action === 'saved' && button(t('prompts.load_version'), {
        small: true, onClick: () => loadText(row.body),
      })))));
  }

  function drawOne() {
    const { item } = state;
    const top = pageHead(pick(item.title));
    top.appendChild(h('p', { class: 'page-head__note', text: pick(item.about) }));
    const status = h('p', { class: 'prompt-status' }, stateChip(item),
      item.edited && h('span', { text: editedText(item) }),
      item.edited && h('span', { text: t('prompts.version', { version: item.version }) }));

    editor = h('textarea', {
      class: 'input input--area prompt-editor', id: 'prompt-text', spellcheck: 'false',
      autocomplete: 'off', autocapitalize: 'off', rows: 24, dir: 'auto',
      readonly: item.editable ? null : true, 'aria-describedby': 'prompt-text-count',
    });
    editor.value = item.text;
    counter = h('span', { class: 'field__counter', id: 'prompt-text-count' });
    const label = h('label', {
      class: 'field__label', for: 'prompt-text',
      text: t(item.editable ? 'prompts.text' : 'prompts.text_read_only'),
    });

    if (!item.editable) {
      saveButton = null;
      counter.textContent = t('prompts.chars_short', { count: num(item.text.length) });
      mount(root, backLink(), top, status, notice('info', t('prompts.read_only_note')),
        card({ class: 'prompt-card' },
          h('div', { class: 'field' }, label, editor,
            h('div', { class: 'field__below' },
              button(t('prompts.copy'), { small: true, iconName: 'copy',
                onClick: () => copy(item.text) }),
              counter))));
      return;
    }

    saveButton = button(t('common.save'), { variant: 'primary', iconName: 'check', onClick: save });
    discardButton = button(t('prompts.discard'), { onClick: () => loadText(state.original) });
    let resetButton = null;
    if (item.edited) {
      resetButton = button(t('prompts.reset'), { variant: 'ghost', iconName: 'refresh' });
      resetButton.addEventListener('click', () => reset(resetButton));
    }
    const original = item.edited && h('details', { class: 'prompt-original' },
      h('summary', { text: t('prompts.show_original') }),
      h('div', { class: 'prompt-original__actions' },
        button(t('prompts.load_original'), { small: true, onClick: () => loadText(item.default) })),
      h('pre', { class: 'prompt-original__text', tabindex: '0', text: item.default }));

    editor.addEventListener('input', refreshControls);
    editor.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        save();
      }
    });
    mount(root, backLink(), top, status, storeNotice(item.store),
      notice('info', t(item.group === 'research' ? 'prompts.applies_research'
        : 'prompts.applies_bot')),
      card({ class: 'prompt-card' },
        h('div', { class: 'field' }, label, editor,
          h('div', { class: 'field__below' },
            h('p', { class: 'field__hint', text: t('prompts.hint') }), counter)),
        h('div', { class: 'prompt-actions' },
          h('div', { class: 'prompt-actions__extra' }, resetButton),
          h('div', { class: 'prompt-actions__main' }, discardButton, saveButton)),
        original),
      historyCard(item));
    refreshControls();
    ctx.setGuard(dirty);
  }

  if (key) {
    loadOne();
  } else if (rest) {
    mount(root, backLink(), pageHead(t('prompts.title')),
      card({}, emptyState({ iconName: 'info', title: t('error.not_found') })));
  } else {
    loadList();
  }

  return {
    redraw() { // the interface language changed: same data, same draft
      if (state.item && editor) {
        const draft = editor.value;
        drawOne();
        editor.value = draft;
        refreshControls();
      } else if (state.list) {
        drawList();
      }
    },
    destroy() {
      alive = false;
      if (controller) {
        controller.abort();
      }
    },
  };
}
