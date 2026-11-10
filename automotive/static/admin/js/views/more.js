// More: the phone menu behind the fourth tab. Everything the sidebar offers on desktop.

import { h, icon, mount } from '../dom.js';
import { t } from '../strings.js';
import { pageHead } from '../ui.js';

export function mountView(root, ctx) {
  ctx.setGuard(null);

  function row(iconName, label, props, trailing = 'chevron-right') {
    const tag = props.href ? 'a' : 'button';
    return h('li', {}, h(tag, { class: 'menu__item', type: props.href ? null : 'button', ...props },
      h('span', { class: 'menu__icon' }, icon(iconName, 20)),
      h('span', { class: 'menu__label', text: label }),
      trailing && h('span', { class: 'menu__trail' }, icon(trailing, 16))));
  }

  function draw() {
    const { user } = ctx;
    const signOut = row('signout', t('nav.sign_out'), {}, null);
    signOut.firstChild.addEventListener('click', () => ctx.signOut(signOut.firstChild));
    const language = row('globe', t('nav.language_switch'),
      { 'aria-label': t('nav.language_switch_label'), lang: ctx.otherLang() }, null);
    language.firstChild.addEventListener('click', ctx.toggleLanguage);
    mount(root,
      pageHead(t('more.title')),
      h('div', { class: 'stack stack--narrow' },
        h('div', { class: 'card who' },
          h('span', { class: 'who__mark', 'aria-hidden': 'true' }),
          h('div', {},
            h('p', { class: 'who__name', text: user.display_name || user.username }),
            h('p', { class: 'who__role', text: `${user.username} · ${t(`role.${user.role}`)}` }))),
        h('nav', { class: 'card menu', 'aria-label': t('more.title') }, h('ul', {},
          ctx.isAdmin && row('users', t('nav.users'), { href: '/admin/users', dataset: { link: '' } }),
          ctx.isAdmin && row('audit', t('audit.title'), { href: '/admin/audit', dataset: { link: '' } }),
          row('account', t('nav.account'), { href: '/admin/account', dataset: { link: '' } }),
          row('external', t('nav.view_site'),
            { href: '/', target: '_blank', rel: 'noopener noreferrer' }, null))),
        h('div', { class: 'card menu' }, h('ul', {}, language, signOut))));
  }

  draw();
  return { redraw: draw, destroy() {} };
}
