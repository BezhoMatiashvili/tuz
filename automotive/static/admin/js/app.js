// Entry point of the staff dashboard: session bootstrap, the shell (sidebar on desktop,
// bottom tab bar on phones) and a small History API router for /admin/... paths.

import { api, getUser, isAdmin, setSession } from './api.js';
import { hideTip } from './charts.js';
import { h, icon, mount } from './dom.js';
import * as watch from './live-watch.js';
import { getLang, onLangChange, otherLang, setLang, t } from './strings.js';
import {
  adminOnly, busy, card, confirmDialog, emptyState, errorState, notice, pageHead, toastError,
} from './ui.js';
import * as account from './views/account.js';
import * as audit from './views/audit.js';
import * as content from './views/content.js';
import * as facebook from './views/facebook.js';
import * as inventory from './views/inventory.js';
import * as live from './views/live.js';
import * as more from './views/more.js';
import * as overview from './views/overview.js';
import * as prompts from './views/prompts.js';
import * as users from './views/users.js';

const ROUTES = [
  { pattern: /^\/admin$/, view: overview, nav: 'overview', title: 'nav.overview' },
  { pattern: /^\/admin\/inventory$/, view: inventory, nav: 'inventory', title: 'nav.inventory' },
  {
    // Both roles: the alarms are for whoever is at the counter. /admin/live/<m-psid|w-visitor>.
    pattern: /^\/admin\/live(?:\/([mw]-[A-Za-z0-9]{1,40}))?$/, view: live, nav: 'live',
    title: 'live.title', params: (match) => ({ rest: match[1] || '' }),
  },
  {
    pattern: /^\/admin\/content(?:\/([a-z]+)(?:\/([A-Za-z0-9_-]+))?)?$/, view: content, nav: 'content',
    title: 'nav.content', admin: true, params: (match) => ({ tab: match[1] || '', rest: match[2] || '' }),
  },
  {
    pattern: /^\/admin\/facebook(?:\/([a-z]+)(?:\/([0-9]{1,30}))?)?$/, view: facebook, nav: 'facebook',
    title: 'nav.facebook', admin: true, params: (match) => ({ tab: match[1] || '', rest: match[2] || '' }),
  },
  {
    // Every prompt sent to a language model; /admin/prompts/<key> opens one.
    pattern: /^\/admin\/prompts(?:\/([a-z0-9_.]{1,64}))?$/, view: prompts, nav: 'prompts',
    title: 'nav.prompts', admin: true, params: (match) => ({ rest: match[1] || '' }),
  },
  { pattern: /^\/admin\/users$/, view: users, nav: 'users', title: 'nav.users', admin: true },
  { pattern: /^\/admin\/audit$/, view: audit, nav: 'audit', title: 'audit.title', admin: true },
  { pattern: /^\/admin\/account$/, view: account, nav: 'account', title: 'nav.account' },
  { pattern: /^\/admin\/more$/, view: more, nav: 'more', title: 'nav.more' },
];
// Where each sidebar entry lives on the phone tab bar.
const TAB_OF = { overview: 'overview', live: 'live', inventory: 'inventory', content: 'content',
  users: 'more', audit: 'more', account: 'more', more: 'more', facebook: 'more', prompts: 'more' };
const ICON_OF = { overview: 'overview', live: 'bell', inventory: 'inventory', content: 'content',
  users: 'users', audit: 'audit', account: 'account', more: 'more', facebook: 'spark',
  prompts: 'chat' };

const app = document.getElementById('app');
let main = null;
let sidebar = null;
let tabbar = null;
let current = null; // {route, handle}
let guard = null; // set by the view on screen: () => true while it holds unsaved changes
let position = 0;
let restoring = false;
let leaving = false; // the person already agreed to drop unsaved changes for the next move
let alertBar = null; // alarm problems on every page but Live (which shows its own banners)
let alertSignature = '';

function pathOf(url) {
  return (url.pathname.replace(/\/+$/, '') || '/admin') + url.search;
}

function match(pathname) {
  const clean = pathname.replace(/\/+$/, '') || '/admin';
  for (const route of ROUTES) {
    const found = route.pattern.exec(clean);
    if (found) {
      return { route, params: route.params ? route.params(found) : {} };
    }
  }
  return null;
}

// -- shell -----------------------------------------------------------------------------

/** The count of alarms on the Live item: drawn again by paintBadges when it moves, and read
 * from the watcher whenever the shell is redrawn (a language change), so it never goes missing. */
function fillBadge(node, count) {
  node.hidden = count < 1;
  node.replaceChildren(
    h('span', { 'aria-hidden': 'true', text: count > 99 ? '99+' : String(count) }),
    h('span', { class: 'visually-hidden', text: t('live.badge', { count }) }));
}

function paintBadges() {
  const count = watch.badge();
  for (const node of document.querySelectorAll('[data-badge]')) {
    fillBadge(node, count);
  }
}

function badgeNode() {
  const node = h('span', { class: 'nav-badge', dataset: { badge: '' } });
  fillBadge(node, watch.badge());
  return node;
}

function navLink(name, active, compact) {
  return h('a', {
    class: compact ? 'tabbar__item' : 'sidebar__item', href: name === 'overview' ? '/admin' : `/admin/${name}`,
    dataset: { link: '' }, 'aria-current': active ? 'page' : null,
  }, icon(ICON_OF[name], compact ? 22 : 20), h('span', { text: t(`nav.${name}`) }),
  name === 'live' ? badgeNode() : null);
}

function drawShell() {
  const user = getUser();
  const admin = isAdmin();
  const active = current && current.route ? current.route.nav : '';
  const side = ['overview', 'live', 'inventory', admin && 'content', admin && 'facebook',
    admin && 'prompts', admin && 'users', admin && 'audit', 'account'].filter(Boolean);
  const tabs = ['overview', 'live', 'inventory', admin && 'content', 'more'].filter(Boolean);
  const signOutButton = h('button', { type: 'button', class: 'sidebar__item sidebar__item--button' },
    icon('signout', 20), h('span', { text: t('nav.sign_out') }));
  signOutButton.addEventListener('click', () => signOut(signOutButton));
  mount(sidebar,
    h('a', { class: 'brand', href: '/admin', dataset: { link: '' } },
      h('span', { class: 'brand__mark', 'aria-hidden': 'true' }),
      h('span', { class: 'brand__text' },
        h('span', { class: 'brand__name', text: t('app.name') }),
        h('span', { class: 'brand__sub', text: t('app.staff') }))),
    h('nav', { class: 'sidebar__nav', 'aria-label': t('nav.main') },
      h('ul', {}, side.map((name) => h('li', {}, navLink(name, name === active, false))))),
    h('div', { class: 'sidebar__foot' },
      h('a', { class: 'sidebar__item', href: '/', target: '_blank', rel: 'noopener noreferrer' },
        icon('external', 20), h('span', { text: t('nav.view_site') }),
        h('span', { class: 'visually-hidden', text: `(${t('nav.new_tab')})` })),
      h('button', {
        type: 'button', class: 'sidebar__item sidebar__item--button', lang: otherLang(),
        'aria-label': t('nav.language_switch_label'), on: { click: toggleLanguage },
      }, icon('globe', 20), h('span', { text: t('nav.language_switch') })),
      signOutButton,
      h('div', { class: 'sidebar__user' },
        h('p', { class: 'sidebar__user-name', text: user.display_name || user.username }),
        h('p', { class: 'sidebar__user-role', text: t(`role.${user.role}`) }))));
  mount(tabbar, h('ul', { class: `tabbar__list tabbar__list--${tabs.length}` },
    tabs.map((name) => h('li', {}, navLink(name, TAB_OF[active] === name, true)))));
  tabbar.setAttribute('aria-label', t('nav.main'));
}

/** What stops the alarms from being heard, on whatever page staff are: the server cannot be
 * reached, the session ended (a form with unsaved changes kept the person here), or the
 * browser blocks the sound while something needs a person. The Live page says it itself. */
function drawAlertBar() {
  if (!alertBar) {
    return;
  }
  const snap = watch.getState();
  const onLive = Boolean(current && current.route && current.route.view === live);
  const items = onLive ? [] : [
    snap.signedOut ? 'signed_out' : '',
    !snap.signedOut && snap.offline ? 'offline' : '',
    !snap.signedOut && !snap.offline && snap.soundOn && snap.soundLocked && snap.badge > 0
      ? 'sound_blocked' : '',
  ].filter(Boolean);
  const signature = `${getLang()}|${items.join(',')}`;
  if (signature === alertSignature) {
    return; // unchanged: drawing it again would announce it again
  }
  alertSignature = signature;
  mount(alertBar, ...items.map((item) => notice(item === 'sound_blocked' ? 'warning' : 'critical',
    t(`live.${item}`))));
}

function buildShell() {
  sidebar = h('aside', { class: 'sidebar' });
  main = h('main', { class: 'main', id: 'main', tabindex: '-1' });
  tabbar = h('nav', { class: 'tabbar' });
  alertBar = h('div', { class: 'shell-alerts' });
  document.body.appendChild(alertBar);
  const skip = h('a', { class: 'skip', href: '#main', text: t('nav.skip') });
  skip.addEventListener('click', (event) => {
    event.preventDefault();
    main.focus();
  });
  mount(app, skip, h('div', { class: 'shell' }, sidebar, main, tabbar));
  app.dataset.state = 'ready';
}

// -- router ----------------------------------------------------------------------------

const ctx = {
  get user() {
    return getUser();
  },
  get isAdmin() {
    return isAdmin();
  },
  navigate,
  setGuard(check) {
    guard = check || null;
  },
  /** Change the address without drawing anything (filters kept in the URL). */
  replaceUrl(path) {
    window.history.replaceState({ position }, '', path);
  },
  signOut,
  toggleLanguage,
  otherLang,
};

function setTitle(route) {
  const name = route ? t(route.title) : t('notfound.title');
  const alarms = watch.badge(); // "(2) " in the tab's title while something needs a person
  document.title = `${alarms > 0 ? `(${alarms}) ` : ''}${name} | Garage 1886`;
}

function render(moveFocus) {
  hideTip();
  const found = match(window.location.pathname);
  // A bare /admin/content means the texts tab; say so in the address.
  if (found && found.route.view === content && !found.params.tab) {
    window.history.replaceState({ position }, '', '/admin/content/texts');
    found.params.tab = 'texts';
  }
  const route = found ? found.route : null;
  const allowed = route && (!route.admin || isAdmin());
  if (allowed && current && current.route === route && current.handle && current.handle.update) {
    if (moveFocus) {
      window.scrollTo(0, 0);
    }
    current.handle.update(found.params); // Same view, new sub-path: no remount, no refetch.
    drawShell();
    setTitle(route);
    return;
  }
  if (current && current.handle) {
    current.handle.destroy();
  }
  guard = null;
  current = { route, handle: null };
  drawShell();
  setTitle(route);
  drawAlertBar();
  mount(main);
  if (!route) {
    mount(main, pageHead(t('notfound.title')), card({}, emptyState({
      iconName: 'search', title: t('notfound.text'),
      action: h('a', { class: 'btn btn--secondary', href: '/admin', dataset: { link: '' } },
        h('span', { class: 'btn__label', text: t('admin_only.back') })),
    })));
  } else if (!allowed) {
    mount(main, pageHead(t(route.title)), card({}, adminOnly()));
  } else {
    current.handle = route.view.mountView(main, ctx, found.params);
  }
  if (moveFocus) {
    window.scrollTo(0, 0);
    const heading = main.querySelector('h1');
    (heading || main).focus({ preventScroll: true });
  }
}

function askToLeave() {
  return confirmDialog({
    title: t('guard.title'), text: t('guard.text'), confirmLabel: t('guard.leave'),
    cancelLabel: t('guard.stay'), danger: true,
  });
}

/** Go to an /admin path. Resolves false when the person chose to stay on a dirty form. */
async function navigate(path, options = {}) {
  if (!options.force && guard && guard()) {
    if (!(await askToLeave())) {
      return false;
    }
  }
  // The guard itself stays: a view that only changes its sub-path keeps guarding. A view
  // that is replaced loses it in render().
  const same = path === pathOf(window.location);
  if (options.replace || same) {
    window.history.replaceState({ position }, '', path);
  } else {
    position += 1;
    window.history.pushState({ position }, '', path);
  }
  render(true);
  return true;
}

async function onPopState(event) {
  const target = event.state && typeof event.state.position === 'number' ? event.state.position : 0;
  if (restoring) {
    restoring = false;
    return;
  }
  if (!leaving && guard && guard()) {
    // The address already moved. Put it back, ask, and only then repeat the move.
    const delta = target - position;
    if (delta !== 0) {
      restoring = true;
      window.history.go(-delta);
    }
    if (await askToLeave()) {
      if (delta !== 0) {
        leaving = true;
        window.history.go(delta);
      } else {
        render(true);
      }
    }
    return;
  }
  leaving = false;
  position = target;
  render(true);
}

function onClick(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
      || event.shiftKey || event.altKey) {
    return;
  }
  const anchor = event.target instanceof Element ? event.target.closest('a[data-link]') : null;
  if (!anchor || anchor.target === '_blank') {
    return;
  }
  const url = new URL(anchor.href, window.location.href);
  if (url.origin !== window.location.origin || !/^\/admin(\/|$)/.test(url.pathname)) {
    return;
  }
  event.preventDefault();
  navigate(pathOf(url));
}

// -- session actions -------------------------------------------------------------------

async function signOut(trigger) {
  if (guard && guard() && !(await askToLeave())) {
    return;
  }
  const kept = guard;
  guard = null; // Agreed to leave: the browser's own "leave site?" prompt must not ask again.
  const restore = trigger instanceof HTMLButtonElement ? busy(trigger, t('nav.signing_out')) : () => {};
  try {
    await api.logout();
    watch.stop();
    window.location.assign('/login');
  } catch (error) {
    guard = kept;
    restore();
    toastError(error);
  }
}

function toggleLanguage() {
  setLang(otherLang());
}

onLangChange(() => {
  drawShell();
  setTitle(current ? current.route : null);
  drawAlertBar();
  if (current && current.handle) {
    current.handle.redraw();
  } else {
    render(false);
  }
});

async function start() {
  document.documentElement.lang = getLang();
  try {
    setSession(await api.session());
  } catch (error) {
    mount(app, h('div', { class: 'boot' }, card({}, errorState(error, () => {
      mount(app, h('div', { class: 'boot' }, h('p', { class: 'boot__text', text: t('common.loading') })));
      start();
    }))));
    return;
  }
  const state = window.history.state;
  position = state && typeof state.position === 'number' ? state.position : 0;
  window.history.replaceState({ position }, '', window.location.href);
  buildShell();
  render(false);
  // Alarms are counted and heard on every page, so the one poller starts with the shell.
  let shown = -1;
  watch.subscribe(() => {
    const count = watch.badge();
    if (count !== shown) {
      shown = count;
      paintBadges();
      setTitle(current ? current.route : null);
    }
    drawAlertBar();
  });
  watch.start();
  window.addEventListener('popstate', onPopState);
  document.addEventListener('click', onClick);
  window.addEventListener('beforeunload', (event) => {
    if (guard && guard()) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
}

start();
