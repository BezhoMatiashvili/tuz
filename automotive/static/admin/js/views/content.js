// Content (admin): the host for the Texts, Services and Gallery editors. The sub-tabs are
// ordinary links, so back and forward move between them.

import { api } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { t } from '../strings.js';
import { pageHead, revealCurrentTab } from '../ui.js';
import * as gallery from './gallery.js';
import * as services from './services.js';
import * as texts from './texts.js';

const TABS = { texts, services, gallery };

export function mountView(root, ctx, params) {
  let tab = null;
  let sub = null;
  const body = h('div', { class: 'content__body' });
  const tabs = h('nav', { class: 'subtabs', 'aria-label': t('content.tabs') });
  const head = h('div', {});

  // One fetch of the schema and section documents, shared by the editors of this visit.
  const shared = {
    pending: null,
    content(force = false) {
      if (!this.pending || force) {
        this.pending = api.content().catch((error) => {
          this.pending = null;
          throw error;
        });
      }
      return this.pending;
    },
  };

  function drawFrame() {
    mount(head, pageHead(t('content.title'),
      h('a', { class: 'btn btn--secondary', href: '/', target: '_blank', rel: 'noopener noreferrer' },
        icon('external', 18), h('span', { class: 'btn__label', text: t('nav.view_site') }),
        h('span', { class: 'visually-hidden', text: `(${t('nav.new_tab')})` }))));
    tabs.setAttribute('aria-label', t('content.tabs'));
    mount(tabs, Object.keys(TABS).map((name) => h('a', {
      class: 'subtabs__tab', href: `/admin/content/${name}`, dataset: { link: '' },
      'aria-current': name === tab ? 'page' : null,
    }, t(`content.tab.${name}`))));
    revealCurrentTab(tabs);
  }

  function show(next) {
    // The router sends a bare /admin/content to the texts tab before it gets here.
    const wanted = TABS[next.tab] ? next.tab : 'texts';
    if (wanted === tab && sub && sub.update) {
      sub.update({ rest: next.rest || '' });
      return;
    }
    if (sub) {
      sub.destroy();
      sub = null;
    }
    tab = wanted;
    ctx.setGuard(null); // Each editor registers its own check for unsaved changes.
    drawFrame();
    mount(body);
    sub = TABS[tab].mountView(body, ctx, { rest: next.rest || '', shared });
  }

  mount(root, head, tabs, body);
  show(params);

  return {
    update: show,
    redraw() {
      drawFrame();
      if (sub) {
        sub.redraw();
      }
    },
    destroy() {
      if (sub) {
        sub.destroy();
      }
    },
  };
}
