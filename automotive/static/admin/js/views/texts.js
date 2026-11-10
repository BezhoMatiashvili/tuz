// Texts (admin): one form per site section, built from the schema the server describes.
// Georgian and English sit side by side on wide screens and behind KA / EN tabs on phones.

import { ApiError, api, describeError } from '../api.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import { fieldMessage, inputKind, langPair, langTabs, limitedInput } from '../forms.js';
import { pick, t } from '../strings.js';
import {
  adminOnly, announce, busy, button, card, confirmDialog, errorState, iconButton, isForbidden,
  loadingBlock, notice, skeleton, switchControl, toast, toastError,
} from '../ui.js';

const clone = (value) => JSON.parse(JSON.stringify(value));

function isBlank(value) {
  if (value && typeof value === 'object') {
    return Object.values(value).every(isBlank);
  }
  return value === '' || value === null || value === undefined || value === false;
}

/** The document as it is sent: list rows with nothing in them are left out. */
function withoutEmptyRows(document, section) {
  const copy = clone(document);
  for (const def of section.fields) {
    if (def.kind === 'list' && Array.isArray(copy[def.name])) {
      copy[def.name] = copy[def.name].filter((row) => !isBlank(row));
    }
  }
  return copy;
}

function blank(fields) {
  const row = {};
  for (const child of fields) {
    row[child.name] = child.kind === 'i18n' ? { ka: '', en: '' } : child.kind === 'flag' ? false
      : child.kind === 'list' ? [] : '';
  }
  return row;
}

export function mountView(root, ctx, params) {
  const state = {
    status: 'loading', error: null, payload: null,
    urlKey: params.rest || '', key: '',
    working: null, original: '', version: 0,
    errors: {}, conflict: false, failure: '', saving: false, lang: 'ka',
  };
  let controls = new Map(); // error path -> {setError, input, lang, kind}
  let tabs = null;
  let bar = null;
  let slot = null;
  let alive = true;

  const dirty = () => Boolean(state.working) && JSON.stringify(state.working) !== state.original;
  ctx.setGuard(dirty);

  function choose(key) {
    const { sections, schema } = state.payload;
    state.key = sections[key] ? key : schema.sections[0].key;
    const entry = sections[state.key];
    state.working = clone(entry.document);
    state.original = JSON.stringify(entry.document);
    state.version = entry.version;
    state.errors = {};
    state.conflict = false;
    state.failure = '';
  }

  // -- save bar ------------------------------------------------------------------------

  function refreshBar() {
    if (!bar) {
      return;
    }
    const errors = Object.keys(state.errors).length;
    const problem = Boolean(state.failure) || errors > 0 || state.conflict;
    const visible = dirty() || problem;
    mount(bar.text, problem ? statusIcon('critical', 18) : icon('edit', 18),
      h('span', { text: state.failure || (state.conflict ? t('texts.conflict')
        : errors ? t('texts.fix', { count: errors }) : t('texts.unsaved')) }));
    bar.node.classList.toggle('savebar--problem', problem);
    bar.save.disabled = state.saving || !dirty() || state.conflict; // A conflict is solved by reloading.
    bar.discard.disabled = state.saving || !dirty();
    // Slide in when the form becomes dirty, out when it is clean again. The wanted state
    // is kept on the bar so a quick clean-then-dirty cannot leave it hidden.
    const current = bar;
    if (visible !== current.shown) {
      current.shown = visible;
      window.clearTimeout(current.timer);
      if (visible) {
        current.node.hidden = false;
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
          current.node.classList.toggle('savebar--in', current.shown);
        }));
      } else {
        current.node.classList.remove('savebar--in');
        current.timer = window.setTimeout(() => {
          current.node.hidden = !current.shown;
        }, 220);
      }
    }
    if (tabs) {
      tabs.mark(['ka', 'en'].filter((lang) => Object.keys(state.errors).some((path) => path.endsWith(`.${lang}`))));
    }
  }

  function touched(path) {
    if (state.errors[path]) {
      delete state.errors[path];
      const control = controls.get(path);
      if (control) {
        control.setError('');
      }
    }
    if (state.failure) {
      state.failure = '';
    }
    refreshBar();
  }

  function dropUnder(prefix) {
    for (const path of [...controls.keys()]) {
      if (path.startsWith(`${prefix}.`)) {
        controls.delete(path);
      }
    }
    for (const path of Object.keys(state.errors)) {
      if (path === prefix || path.startsWith(`${prefix}.`)) {
        delete state.errors[path];
      }
    }
  }

  // -- fields --------------------------------------------------------------------------

  function buildList(def, holder, path) {
    if (!Array.isArray(holder[def.name])) {
      holder[def.name] = [];
    }
    const rows = holder[def.name];
    const key = path.join('.');
    const wrap = h('fieldset', { class: 'list' });

    const render = (focus) => {
      if (focus) {
        dropUnder(key); // Rows moved: paths with the old positions no longer point anywhere.
      }
      const refs = [];
      const error = h('p', { class: 'field__error', hidden: true, role: 'alert' },
        statusIcon('critical', 16), h('span', {}));
      const add = button(t('texts.list.add'), {
        iconName: 'plus', disabled: rows.length >= def.items,
        onClick: () => {
          rows.push(blank(def.fields));
          render({ index: rows.length - 1, part: 'input' });
          refreshBar();
        },
      });
      const items = rows.map((row, index) => {
        const move = (delta) => {
          const target = index + delta;
          [rows[index], rows[target]] = [rows[target], rows[index]];
          render({ index: target, part: delta < 0 ? 'up' : 'down' });
          announce(t('common.moved', { position: target + 1, total: rows.length }));
          refreshBar();
        };
        const up = iconButton('arrow-up', t('common.move_up'), {
          disabled: index === 0, onClick: () => move(-1),
        });
        const down = iconButton('arrow-down', t('common.move_down'), {
          disabled: index === rows.length - 1, onClick: () => move(1),
        });
        const remove = iconButton('trash', t('texts.list.remove'), {
          onClick: () => {
            rows.splice(index, 1);
            render(rows.length ? { index: Math.min(index, rows.length - 1), part: 'remove' } : { add: true });
            refreshBar();
          },
        });
        const node = h('li', { class: 'list__item' },
          h('div', { class: 'list__item-head' },
            h('span', { class: 'list__item-title', text: t('texts.list.item', { n: index + 1 }) }),
            h('div', { class: 'list__item-actions' }, up, down, remove)),
          def.fields.map((child) => buildField(child, row, [...path, index, child.name])));
        refs.push({ up, down, remove, node });
        return node;
      });
      mount(wrap,
        h('legend', { class: 'list__legend' },
          h('span', { class: 'list__name', text: pick(def.label) }),
          h('span', { class: 'list__count', text: t('texts.list.count', { count: rows.length, max: def.items }) })),
        error,
        rows.length ? h('ol', { class: 'list__items' }, items)
          : h('p', { class: 'list__empty', text: t('texts.list.empty') }),
        add);
      controls.set(key, {
        input: add,
        lang: null,
        kind: 'text',
        setError(message) {
          error.hidden = !message;
          error.lastChild.textContent = message || '';
        },
      });
      if (!focus) {
        return;
      }
      if (focus.add) {
        add.focus();
        return;
      }
      const ref = refs[focus.index];
      if (focus.part === 'input') {
        const first = ref.node.querySelector(`.pair__side[data-lang="${state.lang}"] .input, .field > .input`)
          || ref.node.querySelector('.input');
        if (first) {
          first.focus();
        }
      } else if (focus.part === 'remove') {
        ref.remove.focus();
      } else {
        // Keep moving the same item: if this direction ran out, land on the other arrow.
        const wanted = focus.part === 'up' ? ref.up : ref.down;
        const other = focus.part === 'up' ? ref.down : ref.up;
        (wanted.disabled ? other : wanted).focus();
      }
    };
    render(null);
    return wrap;
  }

  function buildField(def, holder, path) {
    const { name } = def;
    const key = path.join('.');
    const label = pick(def.label) || name;
    if (def.kind === 'list') {
      return buildList(def, holder, path);
    }
    if (def.kind === 'flag') {
      const error = h('p', { class: 'field__error', hidden: true }, statusIcon('critical', 16), h('span', {}));
      const control = switchControl({
        checked: holder[name] === true, label,
        onChange: (checked) => {
          holder[name] = checked;
          touched(key);
        },
      });
      controls.set(key, {
        input: control.input,
        lang: null,
        kind: 'text',
        setError(message) {
          error.hidden = !message;
          error.lastChild.textContent = message || '';
        },
      });
      return h('div', { class: 'field field--switch' }, control.node, error);
    }
    if (def.kind === 'i18n') {
      if (!holder[name] || typeof holder[name] !== 'object') {
        holder[name] = { ka: '', en: '' };
      }
      return langPair((lang) => {
        const control = limitedInput({
          label, badge: t(`common.${lang}`), lang, value: holder[name][lang], max: def.max,
          words: def.words, multiline: def.multiline,
          onInput: (value) => {
            holder[name][lang] = value;
            touched(`${key}.${lang}`);
          },
        });
        controls.set(`${key}.${lang}`, { ...control, lang });
        return control.node;
      });
    }
    const control = limitedInput({
      label, value: holder[name], max: def.max, words: def.words, multiline: def.multiline,
      kind: inputKind(name),
      onInput: (value) => {
        holder[name] = value;
        touched(key);
      },
    });
    controls.set(key, { ...control, lang: null });
    return control.node;
  }

  // -- focus helpers -------------------------------------------------------------------

  function reveal(control) {
    if (!control) {
      return false;
    }
    if (control.lang && tabs) {
      tabs.show(control.lang);
    }
    control.input.scrollIntoView({ block: 'center' });
    control.input.focus({ preventScroll: true });
    return true;
  }

  function focusFirstError() {
    for (const [path, control] of controls) {
      if (state.errors[path] && reveal(control)) {
        return;
      }
    }
  }

  function focusNamed(name) {
    if (!name) {
      return;
    }
    reveal(controls.get(name) || controls.get(`${name}.${state.lang}`) || controls.get(`${name}.ka`));
  }

  // -- actions -------------------------------------------------------------------------

  async function save() {
    if (state.saving || !dirty()) {
      return;
    }
    state.saving = true;
    state.failure = '';
    const restore = busy(bar.save, t('common.saving'));
    bar.discard.disabled = true;
    const key = state.key;
    const section = state.payload.schema.sections.find((item) => item.key === key);
    try {
      const result = await api.saveSection(key, state.version, withoutEmptyRows(state.working, section));
      if (!alive) {
        return;
      }
      state.saving = false;
      state.payload.sections[key] = { document: result.document, version: result.version };
      const unchanged = JSON.stringify(result.document) === JSON.stringify(state.working);
      const hadFocus = bar.node.contains(document.activeElement);
      toast(t('common.saved'));
      if (unchanged) {
        // Keep the inputs (and the caret) as they are; only the baseline moves.
        state.original = JSON.stringify(state.working);
        state.version = result.version;
        state.errors = {};
        state.conflict = false;
        restore();
        refreshBar();
        if (hadFocus) {
          slot.querySelector('.texts-form__title').focus();
        }
      } else {
        choose(key);
        drawEditor(); // The server tidied the text (spaces, line ends): show what was stored.
        slot.querySelector('.texts-form__title').focus();
      }
    } catch (error) {
      if (!alive) {
        return;
      }
      state.saving = false;
      if (error instanceof ApiError && error.code === 'INVALID_FIELDS') {
        state.errors = { ...error.fields };
        drawEditor();
        announce(t('texts.fix', { count: Object.keys(state.errors).length }));
        focusFirstError();
      } else if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
        state.conflict = true;
        drawEditor();
        const reload = slot.querySelector('[data-reload]');
        if (reload) {
          reload.focus();
        }
      } else {
        state.failure = describeError(error);
        restore();
        refreshBar();
        toastError(error);
      }
    }
  }

  async function discard() {
    const sure = await confirmDialog({
      title: t('texts.discard_title'), text: t('texts.reload_note'),
      confirmLabel: t('texts.discard'), cancelLabel: t('guard.stay'), danger: true,
    });
    if (sure) {
      choose(state.key);
      drawEditor();
      slot.querySelector('.texts-form__title').focus();
    }
  }

  async function reloadLatest(trigger) {
    const restore = busy(trigger, t('common.loading'));
    try {
      state.payload = await params.shared.content(true);
      if (!alive) {
        return;
      }
      choose(state.key);
      drawEditor();
      slot.querySelector('.texts-form__title').focus();
    } catch (error) {
      restore();
      toastError(error);
    }
  }

  // -- drawing -------------------------------------------------------------------------

  function drawEditor() {
    const section = state.payload.schema.sections.find((item) => item.key === state.key);
    controls = new Map();
    tabs = null;
    const titleId = uid('sec');
    const form = h('form', { class: 'texts-form card', novalidate: true, 'aria-labelledby': titleId });
    const paired = section.fields.some((def) => def.kind === 'i18n'
      || (def.kind === 'list' && def.fields.some((child) => child.kind === 'i18n')));
    if (paired) {
      tabs = langTabs(form, state.lang, (lang) => {
        state.lang = lang;
      });
    }
    const reload = button(t('texts.reload'), { small: true, dataset: { reload: '' } });
    reload.addEventListener('click', () => reloadLatest(reload));
    const text = h('p', { class: 'savebar__text' });
    const discardButton = button(t('texts.discard'), { variant: 'ghost', onClick: discard });
    const saveButton = button(t('common.save'), { variant: 'primary', type: 'submit' });
    bar = {
      text, discard: discardButton, save: saveButton, shown: false, timer: 0,
      node: h('div', { class: 'savebar', hidden: true, role: 'group', 'aria-label': t('texts.unsaved') },
        text, h('div', { class: 'savebar__actions' }, discardButton, saveButton)),
    };
    mount(form,
      h('a', { class: 'texts__back', href: '/admin/content/texts', dataset: { link: '' } },
        icon('chevron-left', 18), h('span', { text: t('texts.back') })),
      h('h2', { class: 'texts-form__title', id: titleId, tabindex: '-1', text: pick(section.label) }),
      state.conflict && notice('critical', `${t('texts.conflict')} ${t('texts.conflict_next')}`, reload),
      tabs && tabs.node,
      h('div', { class: 'texts-form__fields' },
        section.fields.map((def) => buildField(def, state.working, [def.name]))),
      bar.node);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      save();
    });
    form.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        save();
      }
    });
    for (const [path, code] of Object.entries(state.errors)) {
      const control = controls.get(path);
      if (control) {
        control.setError(fieldMessage(code, control.kind));
      }
    }
    if (dirty() || Object.keys(state.errors).length > 0 || state.conflict) {
      bar.shown = true; // Already showing before this redraw: no replay of the slide.
      bar.node.hidden = false;
      bar.node.classList.add('savebar--in');
    }
    mount(slot, form);
    refreshBar();
  }

  function draw() {
    if (state.status === 'loading') {
      mount(root, loadingBlock(h('div', { class: 'texts' },
        h('div', { class: 'card texts__list' }, skeleton('line', 8)),
        h('div', { class: 'card texts-form' }, skeleton('title'), skeleton('input', 4)))));
      return;
    }
    if (state.status === 'error') {
      mount(root, isForbidden(state.error) ? adminOnly() : card({}, errorState(state.error, load)));
      return;
    }
    const list = h('nav', { class: 'card texts__list', 'aria-label': t('texts.sections') },
      h('ul', {}, state.payload.schema.sections.map((section) => h('li', {},
        h('a', {
          class: 'texts__link', href: `/admin/content/texts/${section.key}`, dataset: { link: '' },
          'aria-current': section.key === state.key ? 'true' : null,
        }, h('span', { text: pick(section.label) }), icon('chevron-right', 16))))));
    slot = h('div', { class: 'texts__editor' });
    mount(root,
      state.payload.source === 'defaults' && notice('warning', t('content.defaults')),
      h('div', { class: 'texts', dataset: { pane: state.urlKey ? 'editor' : 'list' } }, list, slot));
    drawEditor();
  }

  async function load() {
    state.status = 'loading';
    state.error = null;
    draw();
    try {
      state.payload = await params.shared.content();
      if (!alive) {
        return;
      }
      choose(state.urlKey);
      state.status = 'ready';
    } catch (error) {
      if (!alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    draw();
    if (state.status === 'ready') {
      focusNamed(new URLSearchParams(window.location.search).get('focus'));
    }
  }

  load();
  return {
    update(next) {
      state.urlKey = next.rest || '';
      if (state.status !== 'ready') {
        return;
      }
      choose(state.urlKey);
      draw();
      const wanted = new URLSearchParams(window.location.search).get('focus');
      if (wanted) {
        focusNamed(wanted);
      } else if (state.urlKey) {
        slot.querySelector('.texts-form__title').focus({ preventScroll: true });
      }
    },
    redraw: draw,
    destroy() {
      alive = false;
    },
  };
}
