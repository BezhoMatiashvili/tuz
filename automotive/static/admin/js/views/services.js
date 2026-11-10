// Services (admin): the list shown on the public site, with a published switch, reorder
// buttons, an edit form and a confirmed delete.

import { ApiError, api } from '../api.js';
import { h, icon, mount, uid } from '../dom.js';
import { fieldMessage, langPair, langTabs, limitedInput } from '../forms.js';
import { money, toNumber } from '../format.js';
import { t } from '../strings.js';
import {
  adminOnly, announce, busy, button, card, confirmDialog, emptyState, errorState, field,
  iconButton, isForbidden, loadingBlock, notice, select, skeleton, switchControl, toast,
  toastError,
} from '../ui.js';

const LIST_URL = '/admin/content/services';
const FALLBACK_LIMITS = { title_ka: 60, title_en: 60, summary_ka: 200, summary_en: 200 };
const FALLBACK_ICONS = ['wrench'];

function iconName(name) {
  const key = `icon.${name}`;
  const label = t(key);
  return label === key ? name : label;
}

function priceInput(value) {
  const number = toNumber(value);
  return number === null ? '' : String(number);
}

/** The editable fields of a service row, as the API expects them back. */
function fieldsOf(row) {
  return {
    icon: row.icon, title_ka: row.title_ka, title_en: row.title_en,
    summary_ka: row.summary_ka, summary_en: row.summary_en,
    price_from: toNumber(row.price_from), is_published: row.is_published === true,
  };
}

export function mountView(root, ctx, params) {
  const state = {
    status: 'loading', error: null, services: [], icons: FALLBACK_ICONS, limits: FALLBACK_LIMITS,
    rest: params.rest || '', form: null, lang: 'ka', focusId: null,
  };
  let alive = true;
  let controls = new Map();
  let tabs = null;

  const formDirty = () => Boolean(state.form) && JSON.stringify(state.form.values) !== state.form.original;
  ctx.setGuard(formDirty);

  const find = (id) => state.services.find((row) => String(row.service_id) === String(id));

  function openForm() {
    if (!state.rest) {
      state.form = null;
      return;
    }
    const row = state.rest === 'new' ? null : find(state.rest);
    if (state.rest !== 'new' && !row) {
      state.form = { missing: true, values: {}, original: '{}' };
      return;
    }
    const values = row ? {
      icon: row.icon, title_ka: row.title_ka || '', title_en: row.title_en || '',
      summary_ka: row.summary_ka || '', summary_en: row.summary_en || '',
      price: priceInput(row.price_from), is_published: row.is_published === true,
    } : {
      icon: state.icons[0] || 'wrench', title_ka: '', title_en: '', summary_ka: '', summary_en: '',
      price: '', is_published: true,
    };
    state.form = {
      id: row ? row.service_id : null, version: row ? row.version : 0, values,
      original: JSON.stringify(values), errors: {}, conflict: false, saving: false,
    };
  }

  // -- list ----------------------------------------------------------------------------

  async function reorder(index, delta) {
    const target = index + delta;
    const before = [...state.services];
    const next = [...state.services];
    [next[index], next[target]] = [next[target], next[index]];
    state.services = next;
    drawList({ id: next[target].service_id, part: delta < 0 ? 'up' : 'down' });
    announce(t('common.moved', { position: target + 1, total: next.length }));
    try {
      await api.orderServices(next.map((row) => row.service_id));
    } catch (error) {
      if (!alive) {
        return;
      }
      state.services = before;
      drawList(null);
      toastError(error);
    }
  }

  async function publish(row, checked) {
    try {
      const saved = await api.updateService(row.service_id,
        { ...fieldsOf(row), is_published: checked, version: row.version });
      state.services = state.services.map((item) => (item.service_id === row.service_id ? saved : item));
      toast(t(checked ? 'services.shown' : 'services.hidden_toast'));
      if (alive && !state.form) {
        drawList({ id: row.service_id, part: 'switch' });
      }
    } catch (error) {
      toastError(error);
      if (error instanceof ApiError && (error.code === 'VERSION_CONFLICT' || error.status === 404)) {
        load(); // Someone else changed the list: show what is stored now.
      }
      throw error; // The switch goes back.
    }
  }

  async function remove(row, trigger) {
    const sure = await confirmDialog({
      title: t('services.delete_title'),
      text: t('services.delete_text', { name: row.title_ka || row.title_en }),
      confirmLabel: t('common.delete'), danger: true, opener: trigger,
    });
    if (!sure) {
      return;
    }
    const restore = busy(trigger, t('common.deleting'));
    try {
      await api.deleteService(row.service_id);
    } catch (error) {
      restore();
      if (!(error instanceof ApiError && error.status === 404)) {
        toastError(error);
        return;
      }
    }
    const index = state.services.findIndex((item) => item.service_id === row.service_id);
    state.services = state.services.filter((item) => item.service_id !== row.service_id);
    toast(t('services.deleted'));
    const neighbour = state.services[Math.min(index, state.services.length - 1)];
    drawList(neighbour ? { id: neighbour.service_id, part: 'edit' } : { add: true });
  }

  function drawList(focus) {
    const add = h('a', { class: 'btn btn--primary', href: `${LIST_URL}/new`, dataset: { link: '' } },
      icon('plus', 18), h('span', { class: 'btn__label', text: t('services.add') }));
    const refs = new Map();
    const rows = state.services.map((row, index) => {
      const name = row.title_ka || row.title_en || `#${row.service_id}`;
      const up = iconButton('arrow-up', `${t('common.move_up')}: ${name}`, {
        disabled: index === 0, onClick: () => reorder(index, -1),
      });
      const down = iconButton('arrow-down', `${t('common.move_down')}: ${name}`, {
        disabled: index === state.services.length - 1, onClick: () => reorder(index, 1),
      });
      const toggle = switchControl({
        checked: row.is_published === true, label: t('services.publish'),
        hiddenLabel: t('services.publish_for', { name }),
        onChange: (checked) => publish(row, checked),
      });
      const edit = h('a', {
        class: 'btn btn--secondary btn--small', href: `${LIST_URL}/${row.service_id}`, dataset: { link: '' },
        'aria-label': `${t('common.edit')}: ${name}`,
      }, icon('edit', 18), h('span', { class: 'btn__label', text: t('common.edit') }));
      const del = iconButton('trash', `${t('common.delete')}: ${name}`, { onClick: () => remove(row, del) });
      const priceNumber = toNumber(row.price_from);
      refs.set(row.service_id, { up, down, edit, switch: toggle.input });
      return h('li', { class: ['svc', !row.is_published && 'svc--hidden'] },
        h('div', { class: 'svc__order' },
          h('span', { class: 'svc__position', 'aria-hidden': 'true', text: String(index + 1) }), up, down),
        h('div', { class: 'svc__main' },
          h('p', { class: 'svc__title', lang: 'ka', text: row.title_ka || '?' }),
          h('p', { class: 'svc__title-en', lang: 'en', text: row.title_en || '?' }),
          h('p', { class: 'svc__meta' },
            h('span', { class: 'chip' }, h('span', { class: 'visually-hidden', text: `${t('services.icon')}: ` }),
              iconName(row.icon)),
            h('span', { class: priceNumber === null ? 'muted' : null, text: priceNumber === null
              ? t('services.no_price') : t('services.from', { price: money(priceNumber, 2) }) }))),
        h('div', { class: 'svc__side' }, toggle.node, h('div', { class: 'svc__actions' }, edit, del)));
    });
    mount(root,
      h('div', { class: 'toolbar' }, add),
      state.services.length
        ? h('ol', { class: 'svc-list card', 'aria-label': t('services.list') }, rows)
        : card({}, emptyState({ iconName: 'content', title: t('services.empty') })));
    if (!focus) {
      return;
    }
    if (focus.add) {
      add.focus();
      return;
    }
    const ref = refs.get(focus.id);
    if (!ref) {
      return;
    }
    if (focus.part === 'up' || focus.part === 'down') {
      const wanted = focus.part === 'up' ? ref.up : ref.down;
      const other = focus.part === 'up' ? ref.down : ref.up;
      (wanted.disabled ? other : wanted).focus();
    } else if (ref[focus.part]) {
      ref[focus.part].focus();
    }
  }

  // -- form ----------------------------------------------------------------------------

  function showErrors() {
    const { errors } = state.form;
    for (const [name, control] of controls) {
      control.setError(errors[name] ? fieldMessage(errors[name], control.kind) : '');
    }
    if (tabs) {
      tabs.mark(['ka', 'en'].filter((lang) => Object.keys(errors).some((name) => name.endsWith(`_${lang}`))));
    }
  }

  function focusFirstError() {
    for (const [name, control] of controls) {
      if (state.form.errors[name]) {
        if (control.lang && tabs) {
          tabs.show(control.lang);
        }
        control.input.scrollIntoView({ block: 'center' });
        control.input.focus({ preventScroll: true });
        return;
      }
    }
  }

  async function save(submit) {
    const { form } = state;
    if (form.saving) {
      return;
    }
    const { values } = form;
    form.errors = {};
    let priceValue = null;
    const typed = values.price.trim().replace(',', '.');
    if (typed) {
      priceValue = /^\d{1,7}(\.\d{1,2})?$/.test(typed) ? Number(typed) : NaN;
      if (Number.isNaN(priceValue)) {
        form.errors.price_from = 'INVALID';
        showErrors();
        focusFirstError();
        return;
      }
    }
    const payload = {
      icon: values.icon, title_ka: values.title_ka, title_en: values.title_en,
      summary_ka: values.summary_ka, summary_en: values.summary_en,
      price_from: priceValue, is_published: values.is_published,
    };
    form.saving = true;
    const restore = busy(submit, t('common.saving'));
    try {
      const saved = form.id === null
        ? await api.createService(payload)
        : await api.updateService(form.id, { ...payload, version: form.version });
      if (!alive) {
        return;
      }
      if (form.id === null) {
        state.services = [...state.services, saved];
      } else {
        state.services = state.services.map((row) => (row.service_id === saved.service_id ? saved : row));
      }
      toast(t(form.id === null ? 'services.created' : 'services.saved'));
      state.focusId = saved.service_id;
      state.form = null;
      ctx.navigate(LIST_URL, { force: true });
    } catch (error) {
      if (!alive) {
        return;
      }
      form.saving = false;
      restore();
      if (error instanceof ApiError && error.code === 'INVALID_FIELDS') {
        form.errors = { ...error.fields };
        showErrors();
        focusFirstError();
      } else if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
        form.conflict = true;
        drawForm();
      } else if (error instanceof ApiError && error.status === 404) {
        state.services = state.services.filter((row) => row.service_id !== form.id);
        state.form = { missing: true, values: {}, original: '{}' };
        drawForm();
      } else {
        toastError(error);
      }
    }
  }

  async function reloadLatest(trigger) {
    const restore = busy(trigger, t('common.loading'));
    try {
      const response = await api.services();
      if (!alive) {
        return;
      }
      state.services = response.services || [];
      openForm();
      drawForm();
    } catch (error) {
      restore();
      toastError(error);
    }
  }

  function drawForm() {
    const { form } = state;
    const back = h('a', { class: 'texts__back texts__back--always', href: LIST_URL, dataset: { link: '' } },
      icon('chevron-left', 18), h('span', { text: t('services.back') }));
    if (form.missing) {
      mount(root, back, card({}, emptyState({ iconName: 'content', title: t('services.missing') })));
      return;
    }
    controls = new Map();
    const { values } = form;
    const titleId = uid('svc');
    const node = h('form', { class: 'card texts-form svc-form', novalidate: true, 'aria-labelledby': titleId });
    tabs = langTabs(node, state.lang, (lang) => {
      state.lang = lang;
    });
    const clearError = (name) => {
      if (form.errors[name]) {
        delete form.errors[name];
        showErrors();
      }
    };
    const icons = state.icons.includes(values.icon) ? state.icons : [values.icon, ...state.icons];
    const iconSelect = select(icons.map((name) => ({ value: name, label: iconName(name) })), values.icon);
    iconSelect.addEventListener('change', () => {
      values.icon = iconSelect.value;
      clearError('icon');
    });
    const iconField = field({ label: t('services.icon'), control: iconSelect });
    controls.set('icon', { ...iconField, lang: null, kind: 'text' });
    const sided = (name, label, max, multiline) => langPair((lang) => {
      const key = `${name}_${lang}`;
      const control = limitedInput({
        label, badge: t(`common.${lang}`), lang, value: values[key], max: state.limits[key] || max,
        multiline, required: name === 'title',
        onInput: (value) => {
          values[key] = value;
          clearError(key);
        },
      });
      controls.set(key, { ...control, lang });
      return control.node;
    });
    const priceControl = limitedInput({
      label: t('services.price'), badge: t('common.optional'), value: values.price, kind: 'price',
      hint: t('services.price_hint'), placeholder: '50',
      onInput: (value) => {
        values.price = value;
        clearError('price_from');
      },
    });
    controls.set('price_from', { ...priceControl, lang: null });
    const published = switchControl({
      checked: values.is_published, label: t('services.publish'),
      onChange: (checked) => {
        values.is_published = checked;
      },
    });
    const submit = button(t('common.save'), { variant: 'primary', type: 'submit' });
    const reload = button(t('texts.reload'), { small: true });
    reload.addEventListener('click', () => reloadLatest(reload));
    mount(node,
      h('h2', { class: 'texts-form__title', id: titleId, tabindex: '-1',
        text: t(form.id === null ? 'services.new_title' : 'services.edit_title') }),
      form.conflict && notice('critical', `${t('services.conflict')} ${t('texts.conflict_next')}`, reload),
      tabs.node,
      h('div', { class: 'texts-form__fields' },
        iconField.node,
        sided('title', t('services.title'), 60, false),
        sided('summary', t('services.summary'), 200, true),
        priceControl.node,
        h('div', { class: 'field field--switch' }, published.node)),
      h('div', { class: 'form-actions' },
        h('a', { class: 'btn btn--ghost', href: LIST_URL, dataset: { link: '' } },
          h('span', { class: 'btn__label', text: t('common.cancel') })),
        submit));
    node.addEventListener('submit', (event) => {
      event.preventDefault();
      save(submit);
    });
    node.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        save(submit);
      }
    });
    mount(root, back, node);
    showErrors();
  }

  function draw() {
    if (state.status === 'loading') {
      mount(root, loadingBlock(h('div', { class: 'card svc-list' },
        Array.from({ length: 5 }, () => h('div', { class: 'svc svc--skeleton' }, skeleton('line'), skeleton('line-short'))))));
      return;
    }
    if (state.status === 'error') {
      mount(root, isForbidden(state.error) ? adminOnly() : card({}, errorState(state.error, load)));
      return;
    }
    if (state.form) {
      drawForm();
    } else {
      drawList(null);
    }
  }

  async function load() {
    state.status = 'loading';
    state.error = null;
    draw();
    try {
      const [list, content] = await Promise.all([
        api.services(),
        params.shared.content().catch(() => null), // Icon names and limits; defaults work without it.
      ]);
      if (!alive) {
        return;
      }
      state.services = list.services || [];
      if (content && content.schema) {
        state.icons = Array.isArray(content.schema.icons) && content.schema.icons.length
          ? content.schema.icons : FALLBACK_ICONS;
        state.limits = content.schema.service_limits || FALLBACK_LIMITS;
      }
      state.status = 'ready';
      openForm();
    } catch (error) {
      if (!alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    draw();
    if (state.status === 'ready' && state.form && !state.form.missing) {
      const first = root.querySelector('.texts-form__title');
      if (first) {
        first.focus({ preventScroll: true });
      }
    }
  }

  load();
  return {
    update(next) {
      // Coming back from the form, focus returns to the row that was open.
      const cameFrom = state.focusId || (state.form && state.form.id) || null;
      state.focusId = null;
      state.rest = next.rest || '';
      if (state.status !== 'ready') {
        return;
      }
      openForm();
      if (state.form) {
        drawForm();
        const title = root.querySelector('.texts-form__title');
        if (title) {
          title.focus({ preventScroll: true });
        }
      } else {
        drawList(cameFrom ? { id: cameFrom, part: 'edit' } : null);
      }
    },
    redraw: draw,
    destroy() {
      alive = false;
    },
  };
}
