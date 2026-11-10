// Shared interface pieces: buttons, fields, switches, dialogs, toasts and the loading,
// empty and error states every view uses.

import { ApiError, describeError } from './api.js';
import { h, icon, statusIcon, uid } from './dom.js';
import { t } from './strings.js';

// -- buttons ---------------------------------------------------------------------------

/** variant: primary | secondary | ghost | danger. */
export function button(label, options = {}) {
  const {
    variant = 'secondary', iconName, type = 'button', onClick, small, block, class: extra, ...rest
  } = options;
  return h('button', {
    type,
    class: ['btn', `btn--${variant}`, small && 'btn--small', block && 'btn--block', extra],
    on: { click: onClick },
    ...rest,
  }, iconName && icon(iconName, 18), h('span', { class: 'btn__label', text: label }));
}

/** A square button that shows only an icon; the label is read by screen readers. */
export function iconButton(iconName, label, options = {}) {
  const { onClick, variant = 'ghost', class: extra, ...rest } = options;
  return h('button', {
    type: 'button', class: ['btn', 'btn--icon', `btn--${variant}`, extra], 'aria-label': label,
    title: label, on: { click: onClick }, ...rest,
  }, icon(iconName, 20));
}

/** Disable a button and swap its label while a request runs. Returns a restore function. */
export function busy(node, label) {
  const text = node.querySelector('.btn__label');
  const before = text ? text.textContent : '';
  node.disabled = true;
  node.setAttribute('aria-busy', 'true');
  if (text && label) {
    text.textContent = label;
  }
  return () => {
    node.disabled = false;
    node.removeAttribute('aria-busy');
    if (text) {
      text.textContent = before;
    }
  };
}

/** An in-app link; the router picks up clicks on [data-link]. */
export function link(href, label, options = {}) {
  const { iconName, trailing, ...rest } = options;
  return h('a', { href, dataset: { link: '' }, ...rest },
    iconName && icon(iconName, 18), h('span', { text: label }), trailing && icon(trailing, 18));
}

// -- form fields -----------------------------------------------------------------------

/**
 * Label above the control, optional hint, counter and error text below it.
 * Returns {node, input, setError, setCounter}. `control` is the input, select or textarea.
 */
export function field({ label, control, hint, badge, counter = false, wrap }) {
  const id = control.id || uid('f');
  control.id = id;
  const hintId = hint ? `${id}-hint` : '';
  const errorId = `${id}-error`;
  const error = h('p', { class: 'field__error', id: errorId, hidden: true },
    statusIcon('critical', 16), h('span', {}));
  const count = counter ? h('span', { class: 'field__counter' }) : null;
  const describe = () => {
    const ids = [hintId, error.hidden ? '' : errorId].filter(Boolean).join(' ');
    if (ids) {
      control.setAttribute('aria-describedby', ids);
    } else {
      control.removeAttribute('aria-describedby');
    }
  };
  describe();
  // Label above; error and hint below on the left, the counter below on the right.
  const node = h('div', { class: 'field' },
    h('label', { class: 'field__label', for: id }, label,
      badge && h('span', { class: 'field__badge', text: badge })),
    wrap || control,
    (hint || count) && h('div', { class: 'field__below' },
      hint ? h('p', { class: 'field__hint', id: hintId, text: hint }) : h('span', {}),
      count),
    error);
  return {
    node,
    input: control,
    setError(message) {
      error.hidden = !message;
      error.lastChild.textContent = message || '';
      node.classList.toggle('field--invalid', Boolean(message));
      if (message) {
        control.setAttribute('aria-invalid', 'true');
      } else {
        control.removeAttribute('aria-invalid');
      }
      describe();
    },
    setCounter(text, over, spoken) {
      if (count) {
        count.textContent = text;
        count.classList.toggle('field__counter--over', Boolean(over));
        if (spoken) {
          count.setAttribute('aria-label', spoken);
        }
      }
    },
  };
}

export function textInput(props = {}) {
  return h('input', { class: 'input', type: 'text', autocomplete: 'off', ...props });
}

export function textArea(props = {}) {
  return h('textarea', { class: 'input input--area', rows: 3, ...props });
}

export function select(options, value, props = {}) {
  const node = h('select', { class: 'input input--select', ...props },
    options.map((option) => h('option', { value: option.value, text: option.label })));
  node.value = value === null || value === undefined ? '' : String(value);
  return node;
}

/** A password input with a show/hide toggle. Returns {wrap, input}. */
export function passwordInput(props = {}) {
  const input = h('input', {
    class: 'input input--with-action', type: 'password', autocapitalize: 'none',
    autocorrect: 'off', spellcheck: 'false', ...props,
  });
  const toggle = h('button', {
    type: 'button', class: 'input-action', 'aria-pressed': 'false',
    'aria-label': t('common.show_password'), title: t('common.show_password'),
  }, icon('eye', 20));
  toggle.addEventListener('click', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(show));
    const label = t(show ? 'common.hide_password' : 'common.show_password');
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
    toggle.replaceChildren(icon(show ? 'eye-off' : 'eye', 20));
  });
  return { wrap: h('div', { class: 'input-wrap' }, input, toggle), input };
}

/** An on/off switch. Returns {node, input, set}. onChange(checked) may return a promise;
 * if it rejects the switch goes back. */
export function switchControl({ checked, label, hiddenLabel, onChange, disabled }) {
  const id = uid('sw');
  const input = h('input', {
    type: 'checkbox', role: 'switch', class: 'switch__input', id, checked: Boolean(checked),
    disabled: Boolean(disabled), 'aria-label': hiddenLabel || null,
  });
  input.addEventListener('change', async () => {
    if (!onChange) {
      return;
    }
    const wanted = input.checked;
    input.disabled = true;
    try {
      await onChange(wanted);
    } catch {
      input.checked = !wanted;
    } finally {
      input.disabled = Boolean(disabled);
    }
  });
  const node = h('label', { class: 'switch', for: id },
    input, h('span', { class: 'switch__track', 'aria-hidden': 'true' },
      h('span', { class: 'switch__thumb' })),
    label && h('span', { class: 'switch__label', text: label }));
  return {
    node,
    input,
    set(value) {
      input.checked = Boolean(value);
    },
  };
}

/** A radio group drawn as a segmented control. Returns {node, set}. */
export function segmented({ label, options, value, onChange, name = uid('seg') }) {
  const inputs = [];
  const node = h('div', {
    class: 'segmented', role: 'radiogroup', 'aria-label': label,
  }, options.map((option) => {
    const id = uid('seg');
    const input = h('input', {
      type: 'radio', class: 'segmented__input', name, id, value: option.value,
      checked: option.value === value,
    });
    input.addEventListener('change', () => {
      if (input.checked) {
        onChange(option.value);
      }
    });
    inputs.push(input);
    return h('label', { class: 'segmented__option', for: id },
      input, h('span', { class: 'segmented__text' },
        option.marker && h('span', { class: 'segmented__marker', 'aria-label': option.marker }),
        option.label));
  }));
  return {
    node,
    set(next) {
      for (const input of inputs) {
        input.checked = input.value === next;
      }
    },
    inputs,
  };
}

// -- page furniture --------------------------------------------------------------------

export function pageHead(title, ...actions) {
  return h('header', { class: 'page-head' },
    h('h1', { class: 'page-head__title', tabindex: '-1', text: title }),
    actions.length ? h('div', { class: 'page-head__actions' }, actions) : null);
}

export function card(props, ...children) {
  const { class: extra, ...rest } = props || {};
  return h('section', { class: ['card', extra], ...rest }, children);
}

/** Inline message: kind is info | good | warning | critical. */
export function notice(kind, message, ...actions) {
  const glyph = kind === 'info' ? icon('info', 18) : statusIcon(kind, 18);
  return h('div', { class: `notice notice--${kind}`, role: kind === 'critical' ? 'alert' : 'status' },
    glyph, h('p', { class: 'notice__text', text: message }),
    actions.length ? h('div', { class: 'notice__actions' }, actions) : null);
}

export function emptyState({ iconName = 'circle', title, text, action }) {
  return h('div', { class: 'state' },
    h('span', { class: 'state__icon' }, icon(iconName, 28)),
    title && h('p', { class: 'state__title', text: title }),
    text && h('p', { class: 'state__text', text }),
    action || null);
}

export function errorState(error, retry) {
  return h('div', { class: 'state state--error', role: 'alert' },
    h('span', { class: 'state__icon' }, statusIcon('critical', 28)),
    h('p', { class: 'state__title', text: t('error.title') }),
    h('p', { class: 'state__text', text: describeError(error) }),
    retry && button(t('common.retry'), { iconName: 'refresh', onClick: retry }));
}

export function adminOnly() {
  return h('div', { class: 'state', role: 'alert' },
    h('span', { class: 'state__icon' }, icon('lock', 28)),
    h('p', { class: 'state__title', text: t('admin_only.title') }),
    h('p', { class: 'state__text', text: t('admin_only.text') }),
    h('a', { class: 'btn btn--secondary', href: '/admin', dataset: { link: '' } },
      h('span', { class: 'btn__label', text: t('admin_only.back') })));
}

/** True when an API error means "this role may not see it". */
export function isForbidden(error) {
  return error instanceof ApiError && error.status === 403 && error.code === 'ADMIN_ONLY';
}

export function skeleton(kind = 'line', count = 1) {
  return Array.from({ length: count }, () => h('span', {
    class: `skel skel--${kind}`, 'aria-hidden': 'true',
  }));
}

export function loadingBlock(...children) {
  return h('div', { class: 'loading', 'aria-busy': 'true' },
    h('span', { class: 'visually-hidden', role: 'status', text: t('common.loading') }), children);
}

// -- live region and toasts ------------------------------------------------------------

let live = null;
/** Say something to screen readers without moving focus. */
export function announce(message) {
  if (!live) {
    live = h('div', { class: 'visually-hidden', 'aria-live': 'polite', role: 'status' });
    document.body.appendChild(live);
  }
  live.textContent = '';
  window.setTimeout(() => {
    live.textContent = message;
  }, 50);
}

let toasts = null;
const toastTimers = new WeakMap();

function expireToast(node, life) {
  window.clearTimeout(toastTimers.get(node));
  toastTimers.set(node, window.setTimeout(() => {
    node.classList.remove('toast--in');
    window.setTimeout(() => node.remove(), 220);
  }, life));
}

/** Toasts live in a popover so an open dialog's backdrop never hides them; showing it
 * again moves it above a dialog that opened in the meantime. */
function raiseToasts() {
  if (typeof toasts.showPopover !== 'function') {
    return;
  }
  try {
    if (toasts.matches(':popover-open')) {
      toasts.hidePopover();
    }
    toasts.showPopover();
  } catch {
    toasts.removeAttribute('popover'); // Fall back to an ordinary fixed element.
  }
}

/** kind: good | critical | info. */
export function toast(message, kind = 'good') {
  if (!toasts) {
    toasts = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite', popover: 'manual' });
    document.body.appendChild(toasts);
  }
  const life = kind === 'critical' ? 6000 : 2800;
  // The same message twice in a row (two fields saved one after the other) stays one toast.
  const latest = toasts.lastElementChild;
  if (latest && latest.dataset.message === message && latest.classList.contains('toast--in')) {
    expireToast(latest, life);
    raiseToasts();
    return;
  }
  while (toasts.children.length >= 3) {
    toasts.firstChild.remove();
  }
  const node = h('div', { class: `toast toast--${kind}`, dataset: { message } },
    kind === 'info' ? icon('info', 18) : statusIcon(kind, 18),
    h('span', { class: 'toast__text', text: message }));
  toasts.appendChild(node);
  raiseToasts();
  // Two frames so the entering state is painted before the transition starts.
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    node.classList.add('toast--in');
  }));
  expireToast(node, life);
}

export function toastError(error) {
  toast(describeError(error), 'critical');
}

// -- dialogs ---------------------------------------------------------------------------

/**
 * A native modal dialog. `build(close)` returns the body nodes; `close(value)` closes it.
 * Resolves with the value passed to close, or null when dismissed. Focus returns to
 * `opener` (the control that asked for the dialog), or to whatever had focus before.
 * Passing the opener matters in Safari, where a clicked button does not take focus.
 */
export function openDialog({ title, build, wide, opener: trigger, fallback, keepOpen = false }) {
  return new Promise((resolve) => {
    const opener = trigger || document.activeElement;
    let pressedBackdrop = false;
    const titleId = uid('dlg');
    let result = null;
    let locked = false;
    const dialog = h('dialog', { class: ['dialog', wide && 'dialog--wide'], 'aria-labelledby': titleId });
    const close = (value = null) => {
      result = value;
      dialog.close();
    };
    close.lock = (value) => {
      locked = Boolean(value);
    };
    const body = h('div', { class: 'dialog__body' },
      h('h2', { class: 'dialog__title', id: titleId, text: title }), build(close));
    dialog.appendChild(body);
    dialog.addEventListener('cancel', (event) => {
      if (locked) {
        event.preventDefault();
      }
    });
    // A press and release on the backdrop dismisses a question. Dialogs that hold typed
    // input set keepOpen, and a text selection that merely ends outside does not count.
    dialog.addEventListener('pointerdown', (event) => {
      pressedBackdrop = event.target === dialog;
    });
    dialog.addEventListener('click', (event) => {
      // The dialog element itself is only hit on the backdrop; the body covers the rest.
      if (event.target === dialog && pressedBackdrop && !locked && !keepOpen) {
        close(null);
      }
    });
    dialog.addEventListener('close', () => {
      dialog.remove();
      // Back to the control that opened it; if a redraw removed that control, to the
      // caller's stand-in, and failing that to the page heading. Never to nowhere.
      const stand = opener instanceof HTMLElement && document.contains(opener) ? opener
        : (fallback && fallback()) || document.querySelector('#main h1');
      if (stand instanceof HTMLElement) {
        stand.focus();
      }
      resolve(result);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

/** Ask before a destructive or surprising action. Resolves true when confirmed. */
export function confirmDialog({ title, text, confirmLabel, cancelLabel, danger = false, extra, opener }) {
  return openDialog({
    title,
    opener,
    build: (close) => [
      h('p', { class: 'dialog__text', text }),
      extra ? h('p', { class: 'dialog__text', text: extra }) : null,
      h('div', { class: 'dialog__actions' },
        button(cancelLabel || t('common.cancel'), { onClick: () => close(false), autofocus: true }),
        button(confirmLabel || t('common.confirm'), {
          variant: danger ? 'danger' : 'primary', onClick: () => close(true),
        })),
    ],
  }).then((value) => value === true);
}
