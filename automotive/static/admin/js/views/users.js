// Users (admin): create accounts, change roles, disable or enable, reset passwords and end
// sessions. Accounts are never deleted; someone who leaves is disabled.

import { ApiError, api } from '../api.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import { fieldMessage } from '../forms.js';
import { dateTime } from '../format.js';
import { t } from '../strings.js';
import {
  adminOnly, busy, button, card, confirmDialog, emptyState, errorState, field, isForbidden,
  loadingBlock, notice, openDialog, pageHead, select, skeleton, textInput, toast, toastError,
} from '../ui.js';

const LOGIN_URL = '/login?next=admin';
// No look-alike characters (0/O, 1/l/I), so a password can be read out at the counter.
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';

function generatePassword() {
  const limit = 256 - (256 % ALPHABET.length); // Reject the tail so every character is equally likely.
  const buffer = new Uint8Array(1);
  const one = () => {
    for (;;) {
      window.crypto.getRandomValues(buffer);
      if (buffer[0] < limit) {
        return ALPHABET[buffer[0] % ALPHABET.length];
      }
    }
  };
  return Array.from({ length: 4 }, () => Array.from({ length: 5 }, one).join('')).join('-');
}

async function copyText(text, input) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard access can be refused; fall back to the selection.
  }
  try {
    input.focus();
    input.select();
    return document.execCommand('copy');
  } catch {
    return false;
  }
}

function roleOptions() {
  return [{ value: 'employee', label: t('role.employee') }, { value: 'admin', label: t('role.admin') }];
}

/** A visible password input with Generate and Copy. Returns the field controller. */
function credentialField() {
  const input = textInput({
    class: 'input code', autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false',
    autocomplete: 'new-password', maxlength: 128,
  });
  const generate = button(t('users.generate'), {
    small: true, iconName: 'spark',
    onClick: () => {
      input.value = generatePassword();
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
      input.select();
    },
  });
  const copy = button(t('users.copy'), {
    small: true, iconName: 'copy',
    onClick: async () => {
      if (!input.value) {
        return;
      }
      const done = await copyText(input.value, input);
      toast(t(done ? 'users.copied' : 'users.copy_failed'), done ? 'good' : 'critical');
    },
  });
  const controller = field({
    label: t('users.password'), control: input, hint: t('users.password_hint'),
    wrap: h('div', { class: 'credential' }, input, h('div', { class: 'credential__actions' }, generate, copy)),
  });
  input.addEventListener('input', () => controller.setError(''));
  return controller;
}

/**
 * What the admin sees after an account or password was saved: the secret, once. It takes
 * the place of the form, so nothing can be submitted a second time.
 */
function showCredentials(form, message, username, password, close) {
  const secret = textInput({ class: 'input code', value: password, readonly: true, 'aria-label': t('users.password') });
  const copy = button(t('users.copy'), {
    iconName: 'copy',
    onClick: async () => {
      const done = await copyText(password, secret);
      toast(t(done ? 'users.copied' : 'users.copy_failed'), done ? 'good' : 'critical');
    },
  });
  const done = button(t('common.close'), { variant: 'primary', onClick: () => close(true) });
  form.replaceWith(h('div', { class: 'dialog__form' },
    notice('good', message),
    h('dl', { class: 'facts facts--stacked' },
      h('div', {}, h('dt', { text: t('users.username') }), h('dd', { class: 'code', text: username })),
      h('div', {}, h('dt', { text: t('users.password') }), h('dd', {}, secret))),
    h('p', { class: 'field__hint', text: t('users.password_once') }),
    h('div', { class: 'dialog__actions' }, copy, done)));
  done.focus();
}

export function mountView(root, ctx) {
  const state = { status: 'loading', error: null, users: [], notes: new Map() };
  let alive = true;
  ctx.setGuard(null);
  const me = ctx.user.user_id;

  const replace = (saved) => {
    state.users = state.users.map((row) => (row.user_id === saved.user_id ? saved : row));
  };
  const nameOf = (row) => row.display_name || row.username;

  async function patch(row, fields, message, focus) {
    try {
      const saved = await api.updateUser(row.user_id, fields);
      state.notes.delete(row.user_id);
      const lostAccess = row.user_id === me && (saved.role !== 'admin' || !saved.is_active);
      if (lostAccess) {
        window.location.assign(LOGIN_URL); // This session no longer exists.
        return;
      }
      replace(saved);
      toast(message);
    } catch (error) {
      if (error instanceof ApiError && error.code === 'LAST_ADMIN') {
        state.notes.set(row.user_id, t('users.last_admin'));
      } else {
        toastError(error);
      }
    }
    if (alive) {
      drawList(focus);
    }
  }

  async function changeRole(row, wanted, trigger) {
    const self = row.user_id === me;
    const sure = await confirmDialog({
      opener: trigger,
      title: self ? t('users.self_title') : t('users.role_title'),
      text: self ? t('users.self_role_text')
        : t('users.role_text', { name: nameOf(row), role: t(`role.${wanted}`) }),
      danger: self,
    });
    if (!sure) {
      drawList({ id: row.user_id, part: 'role' });
      return;
    }
    await patch(row, { role: wanted }, t('users.role_changed'), { id: row.user_id, part: 'role' });
  }

  async function setActive(row, active, trigger) {
    if (!active) {
      const self = row.user_id === me;
      const sure = await confirmDialog({
        opener: trigger,
        title: self ? t('users.self_title') : t('users.disable_title'),
        text: self ? t('users.self_disable_text') : t('users.disable_text', { name: nameOf(row) }),
        confirmLabel: t('users.disable'), danger: true,
      });
      if (!sure) {
        return;
      }
    }
    await patch(row, { is_active: active }, t(active ? 'users.enabled_toast' : 'users.disabled_toast'),
      { id: row.user_id, part: 'active' });
  }

  async function revoke(row, trigger) {
    const self = row.user_id === me;
    const sure = await confirmDialog({
      opener: trigger,
      title: t('users.revoke_title'), text: t('users.revoke_text', { name: nameOf(row) }),
      extra: self ? t('users.revoke_self_text') : '', confirmLabel: t('users.revoke'), danger: true,
    });
    if (!sure) {
      return;
    }
    try {
      await api.revokeSessions(row.user_id);
      if (self) {
        window.location.assign(LOGIN_URL);
        return;
      }
      toast(t('users.revoked'));
    } catch (error) {
      toastError(error);
    }
  }

  function resetPassword(row, trigger) {
    return openDialog({
      opener: trigger,
      keepOpen: true,
      title: t('users.reset_title', { name: nameOf(row) }),
      build: (close) => {
        const password = credentialField();
        const submit = button(t('users.reset_submit'), { variant: 'primary', type: 'submit' });
        const form = h('form', { class: 'dialog__form', novalidate: true },
          h('p', { class: 'dialog__text', text: t('users.reset_text') }),
          password.node,
          h('div', { class: 'dialog__actions' },
            button(t('common.cancel'), { onClick: () => close(null) }), submit));
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const value = password.input.value;
          const restore = busy(submit, t('common.saving'));
          close.lock(true);
          try {
            await api.setUserPassword(row.user_id, value);
            close.lock(false);
            showCredentials(form, t('users.reset_done'), row.username, value, close);
          } catch (error) {
            close.lock(false);
            restore();
            if (error instanceof ApiError && error.code === 'INVALID_FIELDS') {
              password.setError(fieldMessage(error.fields.password || 'INVALID', 'password'));
              password.input.focus();
            } else {
              toastError(error);
            }
          }
        });
        window.setTimeout(() => password.input.focus(), 0);
        return form;
      },
    });
  }

  function createUser(event) {
    return openDialog({
      opener: event && event.currentTarget,
      // The list is redrawn when a user is created, so the button that opened this is gone.
      fallback: () => root.querySelector('.page-head__actions .btn'),
      keepOpen: true,
      title: t('users.new_title'),
      build: (close) => {
        const username = field({
          label: t('users.username'), hint: t('users.username_hint'),
          control: textInput({
            maxlength: 32, autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false',
            autocomplete: 'off', autofocus: true,
          }),
        });
        const display = field({
          label: t('users.display_name'), badge: t('common.optional'),
          control: textInput({ maxlength: 80, autocomplete: 'off' }),
        });
        const role = field({ label: t('users.role'), control: select(roleOptions(), 'employee') });
        const password = credentialField();
        const controls = { username, display_name: display, role, password };
        const kinds = { username: 'username', password: 'password' };
        for (const controller of [username, display, role]) {
          controller.input.addEventListener('input', () => controller.setError(''));
        }
        const submit = button(t('users.create'), { variant: 'primary', type: 'submit' });
        const form = h('form', { class: 'dialog__form', novalidate: true },
          username.node, display.node, role.node, password.node,
          h('div', { class: 'dialog__actions' },
            button(t('common.cancel'), { onClick: () => close(null) }), submit));
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const secret = password.input.value;
          const restore = busy(submit, t('common.saving'));
          close.lock(true);
          try {
            const created = await api.createUser({
              username: username.input.value.trim().toLowerCase(),
              display_name: display.input.value, role: role.input.value, password: secret,
            });
            close.lock(false);
            state.users = [...state.users, created].sort((a, b) => a.username.localeCompare(b.username));
            if (alive) {
              drawList(null);
            }
            showCredentials(form, t('users.created'), created.username, secret, close);
          } catch (error) {
            close.lock(false);
            restore();
            const fields = error instanceof ApiError ? error.fields : {};
            const names = Object.keys(fields).filter((name) => controls[name]);
            if (names.length) {
              for (const name of names) {
                controls[name].setError(fieldMessage(fields[name], kinds[name] || 'text'));
              }
              controls[names[0]].input.focus();
            } else {
              toastError(error);
            }
          }
        });
        return form;
      },
    });
  }

  function drawList(focus) {
    const add = button(t('users.add'), { variant: 'primary', iconName: 'plus', onClick: createUser });
    const refs = new Map();
    const rows = state.users.map((row) => {
      const self = row.user_id === me;
      const roleId = uid('role');
      const role = select(roleOptions(), row.role, { id: roleId, disabled: !row.is_active });
      role.addEventListener('change', () => changeRole(row, role.value, role));
      const active = button(t(row.is_active ? 'users.disable' : 'users.enable'), {
        small: true, variant: row.is_active ? 'ghost' : 'secondary', iconName: row.is_active ? 'lock' : 'check',
        onClick: (event) => setActive(row, !row.is_active, event.currentTarget),
      });
      // Your own password is changed on the Account page, where the current one is asked for.
      const reset = self ? h('a', { class: 'btn btn--ghost btn--small', href: '/admin/account', dataset: { link: '' } },
        icon('key', 18), h('span', { class: 'btn__label', text: t('account.change_password') }))
        : button(t('users.reset_password'), {
          small: true, variant: 'ghost', iconName: 'key',
          onClick: (event) => resetPassword(row, event.currentTarget),
        });
      const end = button(t('users.revoke'), {
        small: true, variant: 'ghost', iconName: 'signout', onClick: (event) => revoke(row, event.currentTarget),
      });
      const note = state.notes.get(row.user_id);
      refs.set(row.user_id, { role, active });
      return h('li', { class: ['user', !row.is_active && 'user--disabled'] },
        h('div', { class: 'user__main' },
          h('p', { class: 'user__name' }, h('span', { text: nameOf(row) }),
            self && h('span', { class: 'chip', text: t('users.you') })),
          h('p', { class: 'user__username code', text: row.username }),
          h('p', { class: 'user__state' },
            statusIcon(row.is_active ? 'good' : 'neutral', 16),
            h('span', { text: t(row.is_active ? 'users.active' : 'users.disabled') })),
          h('p', { class: 'user__seen', text: row.last_login_at
            ? t('users.last_login', { time: dateTime(row.last_login_at) }) : t('users.never') })),
        h('div', { class: 'user__controls' },
          h('div', { class: 'field user__role' },
            h('label', { class: 'field__label', for: roleId }, t('users.role'),
              h('span', { class: 'visually-hidden', text: `: ${nameOf(row)}` })),
            role),
          h('div', { class: 'user__buttons' }, reset, end, active)),
        note && h('div', { class: 'user__note' }, notice('critical', note)));
    });
    mount(root,
      pageHead(t('users.title'), add),
      state.users.length
        ? h('ul', { class: 'users card', 'aria-label': t('users.list') }, rows)
        : card({}, emptyState({ iconName: 'users', title: t('users.empty') })),
      h('p', { class: 'page-note' }, icon('info', 16), h('span', { text: t('users.no_delete') })));
    const ref = focus && refs.get(focus.id);
    if (ref && ref[focus.part]) {
      ref[focus.part].focus();
    }
  }

  function draw() {
    if (state.status === 'loading') {
      mount(root, pageHead(t('users.title')), loadingBlock(h('div', { class: 'card users' },
        Array.from({ length: 3 }, () => h('div', { class: 'user user--skeleton' },
          skeleton('line'), skeleton('line-short'))))));
      return;
    }
    if (state.status === 'error') {
      mount(root, pageHead(t('users.title')),
        isForbidden(state.error) ? adminOnly() : card({}, errorState(state.error, load)));
      return;
    }
    drawList(null);
  }

  async function load() {
    state.status = 'loading';
    state.error = null;
    draw();
    try {
      const response = await api.users();
      if (!alive) {
        return;
      }
      state.users = response.users || [];
      state.status = 'ready';
    } catch (error) {
      if (!alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    draw();
  }

  load();
  return {
    redraw: draw,
    destroy() {
      alive = false;
    },
  };
}
