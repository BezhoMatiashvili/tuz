// Facebook (admin): the Garage page's Messenger bot. A host for sub-tabs that are ordinary
// links, so back and forward move between them: /admin/facebook/<tab>[/<id>].
//
// Adding a tab: write views/facebook-<name>.js exporting
// mountView(root, ctx, {rest}) that returns {update?, redraw, destroy}, add it to TABS below
// in the order the tabs should appear, add 'fb.tab.<name>' to both tables in strings.js, and
// preload it in templates/site/admin.html. The route in app.js already accepts any lower-case
// tab name followed by an optional numeric id; widen it there if a tab needs another kind of
// sub-path. API calls go in fbApi (facebook-shared.js); the Comments, Page and Ads tabs keep
// theirs in activityApi (facebook-activity.js). A module that fails to load breaks the whole
// dashboard, so import a tab only once its file exists and links.

import { h, mount } from '../dom.js';
import { t } from '../strings.js';
import { pageHead, revealCurrentTab } from '../ui.js';
import * as ads from './facebook-ads.js';
import * as chats from './facebook-chats.js';
import * as comments from './facebook-comments.js';
import * as connect from './facebook-connect.js';
import * as page from './facebook-page.js';
import * as status from './facebook-status.js';

const TABS = { status, connect, chats, comments, page, ads };
const FIRST = 'status';

export function mountView(root, ctx, params) {
  let tab = null;
  let sub = null;
  const head = h('div', {});
  const tabs = h('nav', { class: 'subtabs' });
  const body = h('div', {});

  function drawFrame() {
    mount(head, pageHead(t('fb.title')));
    tabs.setAttribute('aria-label', t('fb.tabs'));
    mount(tabs, Object.keys(TABS).map((name) => h('a', {
      class: 'subtabs__tab', href: `/admin/facebook/${name}`, dataset: { link: '' },
      'aria-current': name === tab ? 'page' : null,
    }, t(`fb.tab.${name}`))));
    revealCurrentTab(tabs);
  }

  function show(next) {
    const wanted = TABS[next.tab] ? next.tab : FIRST;
    if (wanted !== next.tab) {
      ctx.replaceUrl(`/admin/facebook/${wanted}`); // a bare /admin/facebook says which tab
    }
    if (wanted === tab && sub && sub.update) {
      sub.update({ rest: next.rest || '' });
      return;
    }
    if (sub) {
      sub.destroy();
      sub = null;
    }
    tab = wanted;
    ctx.setGuard(null); // Each tab registers its own check for unsaved changes.
    drawFrame();
    mount(body);
    sub = TABS[tab].mountView(body, ctx, { rest: wanted === next.tab ? next.rest || '' : '' });
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
