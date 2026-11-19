// Account (both roles): who is signed in, a change-password form, language and sign out.

import { ApiError, api, setCsrf } from '../api.js';
import { h, icon, mount, uid } from '../dom.js';
import { fieldMessage } from '../forms.js';
import { t } from '../strings.js';
import { busy, button, card, field, pageHead, passwordInput, toast, toastError } from '../ui.js';

export function mountView(root, ctx) {
  const values = { current: '', next: '' };
  ctx.setGuard(null);

  function passwordForm() {
    const titleId = uid('pw');
    const current = passwordInput({ autocomplete: 'current-password', value: values.current });
    const next = passwordInput({ autocomplete: 'new-password', value: values.next, maxlength: 128 });
    const currentField = field({ label: t('account.current'), control: current.input, wrap: current.wrap });
    const nextField = field({
      label: t('account.new'), control: next.input, wrap: next.wrap, hint: t('account.new_hint'),
    });
    current.input.addEventListener('input', () => {
      values.current = current.input.value;
      currentField.setError('');
    });
    next.input.addEventListener('input', () => {
      values.next = next.input.value;
      nextField.setError('');
    });
    const submit = button(t('account.submit'), { variant: 'primary', type: 'submit' });
    // A hidden username field lets password managers file the new password correctly.
    const form = h('form', { class: 'card form', novalidate: true, 'aria-labelledby': titleId },
      h('h2', { class: 'card__title', id: titleId, text: t('account.change_password') }),
      h('input', {
        type: 'text', class: 'visually-hidden', autocomplete: 'username', value: ctx.user.username,
        tabindex: '-1', 'aria-hidden': 'true', readonly: true,
      }),
      currentField.node, nextField.node,
      h('div', { class: 'form-actions form-actions--start' }, submit));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!values.current) {
        currentField.setError(t('field.REQUIRED'));
        current.input.focus();
        return;
      }
      if (!values.next) {
        nextField.setError(t('field.REQUIRED'));
        next.input.focus();
        return;
      }
      const restore = busy(submit, t('common.saving'));
      try {
        const result = await api.changePassword(values.current, values.next);
        setCsrf(result.csrf); // The session was rotated: the old token no longer works.
        values.current = '';
        values.next = '';
        current.input.value = '';
        next.input.value = '';
        restore();
        toast(t('account.changed'));
      } catch (error) {
        restore();
        if (error instanceof ApiError && error.code === 'INVALID_CREDENTIALS') {
          currentField.setError(t('account.wrong_current'));
          current.input.focus();
          current.input.select();
        } else if (error instanceof ApiError && error.code === 'TOO_MANY_ATTEMPTS') {
          currentField.setError(t('account.throttled'));
        } else if (error instanceof ApiError && error.status === 422) {
          nextField.setError(fieldMessage(error.code || 'INVALID', 'password'));
          next.input.focus();
        } else {
          toastError(error);
        }
      }
    });
    return form;
  }

  function draw() {
    const { user } = ctx;
    const signOut = button(t('nav.sign_out'), { iconName: 'signout', onClick: () => ctx.signOut(signOut) });
    mount(root,
      pageHead(t('account.title')),
      h('div', { class: 'grid grid--2 settings-grid' },
        card({ 'aria-label': t('account.signed_in') },
          h('h2', { class: 'card__title', text: t('account.signed_in') }),
          h('dl', { class: 'facts' },
            h('div', {}, h('dt', { text: t('account.display_name') }),
              h('dd', { text: user.display_name || user.username })),
            h('div', {}, h('dt', { text: t('account.username') }),
              h('dd', { class: 'code', text: user.username })),
            h('div', {}, h('dt', { text: t('account.role') }),
              h('dd', { text: t(`role.${user.role}`) })))),
        passwordForm(),
        card({},
          h('h2', { class: 'card__title', text: t('account.preferences') }),
          h('div', { class: 'pref' },
            h('span', { class: 'pref__label', text: t('account.language') }),
            button(t('nav.language_switch'), {
              iconName: 'globe', 'aria-label': t('nav.language_switch_label'),
              lang: ctx.otherLang(), onClick: ctx.toggleLanguage,
            })),
          h('div', { class: 'pref' },
            h('a', { class: 'btn btn--secondary', href: '/', target: '_blank', rel: 'noopener noreferrer' },
              icon('external', 18), h('span', { class: 'btn__label', text: t('nav.view_site') }),
              h('span', { class: 'visually-hidden', text: `(${t('nav.new_tab')})` })),
            signOut))));
  }

  draw();
  return { redraw: draw, destroy() {} };
}
