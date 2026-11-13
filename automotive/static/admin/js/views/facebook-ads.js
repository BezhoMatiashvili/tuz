// Facebook > Ads: Garage's own ads over 7, 30 or 90 days, read-only. Per ad: what it cost, how
// many Messenger chats it started (Meta's count), what a chat cost, its click rate, and how many
// of the chats it brought the bot answered (ours, from the ad id Meta attaches to the chat).
// "Best ads" are the cheapest chats above a minimum spend. Money stays in each ad account's own
// currency and is never added across currencies. Ads are made, paused and paid for in Ads
// Manager; nothing here can change or spend anything.

import { isAbort } from '../api.js';
import { chartCard, chartEmpty, lineChart } from '../charts.js';
import { h, mount, uid } from '../dom.js';
import { dayLabel, dayRange, num, percent } from '../format.js';
import { t } from '../strings.js';
import { button, card, emptyState, iconButton, loadingBlock, skeleton } from '../ui.js';
import {
  activityApi, adMoney, errorCard, excerpt, externalLink, jobButton, picture, rangeControl,
} from './facebook-activity.js';
import { label } from './facebook-shared.js';

let remembered = 30; // the range survives a visit to another tab
// Ad cards drawn at a time. Every picture is a request that shares the dashboard's rate limit
// with its API calls, and the server lists up to 200 ads.
const CARDS = 24;

function nameOf(ad) {
  return ad.name || ad.title || t('fb.ads.unnamed', { id: String(ad.ad_id || '').slice(-6) });
}

/** Per currency: spend, chats Meta counted, and the cost of one chat. */
function totalsByCurrency(ads) {
  const totals = new Map();
  for (const ad of ads) {
    const key = ad.currency || '';
    const entry = totals.get(key) || { currency: key, spend: 0, started: 0 };
    entry.spend += Number(ad.spend) || 0;
    entry.started += Number(ad.messaging_started) || 0;
    totals.set(key, entry);
  }
  return [...totals.values()];
}

export function mountView(root) {
  const state = {
    days: remembered, status: 'loading', data: null, daily: [], error: null,
    tableMode: new Set(), shown: CARDS,
  };
  let controller = null;
  let cards = [];
  let alive = true;

  const dispose = () => {
    for (const item of cards) {
      item.dispose();
    }
    cards = [];
  };

  function section(title, ...children) {
    const id = uid('fba');
    return h('section', { class: 'block', 'aria-labelledby': id },
      h('h2', { class: 'block__title', id, text: title }), children);
  }

  function tile(title, value, note) {
    return h('div', { class: 'tile' },
      h('p', { class: 'tile__label', text: title }),
      h('p', { class: ['tile__value', String(value).length > 14 && 'tile__value--text'],
        text: value === '' || value === null ? '—' : value }),
      note ? h('p', { class: 'tile__note', text: note }) : null);
  }

  function tiles(ads) {
    const money = totalsByCurrency(ads);
    const started = ads.reduce((sum, ad) => sum + (Number(ad.messaging_started) || 0), 0);
    const chats = ads.reduce((sum, ad) => sum + (Number(ad.chats) || 0), 0);
    const handled = ads.reduce((sum, ad) => sum + (Number(ad.chats_handled) || 0), 0);
    const perChat = money.filter((entry) => entry.started > 0)
      .map((entry) => adMoney(entry.spend / entry.started, entry.currency));
    return h('div', { class: 'tiles tiles--admin' },
      tile(t('fb.ads.spend'), money.map((entry) => adMoney(entry.spend, entry.currency))
        .join(' · '), t('fb.page.in_days', { days: state.days })),
      tile(t('fb.ads.started'), num(started), t('fb.ads.started_note')),
      tile(t('fb.ads.cost_per_chat'), perChat.join(' · ')),
      tile(t('fb.ads.handled'), chats ? t('fb.ads.handled_value', {
        handled: num(handled), chats: num(chats),
      }) : '—', t('fb.ads.handled_note')));
  }

  function bestCard(best, minSpend) {
    const titleId = uid('best');
    const body = best.length ? h('div', { class: 'table-wrap' },
      h('table', { class: 'table' },
        h('caption', { class: 'visually-hidden', text: t('fb.ads.best') }),
        h('thead', {}, h('tr', {},
          h('th', { scope: 'col', text: t('fb.ads.ad') }),
          h('th', { scope: 'col', class: 'num', text: t('fb.ads.cost_per_chat') }),
          h('th', { scope: 'col', class: 'num', text: t('fb.ads.started') }),
          h('th', { scope: 'col', class: 'num', text: t('fb.ads.spend') }))),
        h('tbody', {}, best.map((ad) => h('tr', {},
          h('th', { scope: 'row', text: nameOf(ad) }),
          h('td', { class: 'num', text: adMoney(ad.cost_per_chat, ad.currency) }),
          h('td', { class: 'num', text: num(ad.messaging_started) }),
          h('td', { class: 'num', text: adMoney(ad.spend, ad.currency) }))))))
      : chartEmpty(t('fb.ads.best_empty', { amount: num(minSpend) }));
    return h('section', { class: 'card chart chart--wide', 'aria-labelledby': titleId },
      h('div', { class: 'chart__head' }, h('div', { class: 'chart__titles' },
        h('h3', { class: 'chart__title', id: titleId, text: t('fb.ads.best') }),
        h('p', { class: 'chart__subtitle', text: t('fb.ads.best_subtitle',
          { amount: num(minSpend) }) }))),
      body);
  }

  function spendCharts(daily) {
    const currencies = [...new Set(daily.map((row) => row.currency || ''))].sort();
    if (!currencies.length) {
      currencies.push('');
    }
    return currencies.map((currency) => {
      const rows = daily.filter((row) => (row.currency || '') === currency);
      const spend = new Map(rows.map((row) => [String(row.day), Number(row.spend) || 0]));
      const started = new Map(rows.map((row) => [String(row.day),
        Number(row.messaging_started) || 0]));
      const seen = [...spend.keys()].sort();
      const days = seen.length ? dayRange(seen[0], seen[seen.length - 1]) : [];
      const values = days.map((day) => spend.get(day) || 0);
      const title = currency && currencies.length > 1
        ? t('fb.ads.daily_title_in', { currency }) : t('fb.ads.daily_title');
      return chartCard({
        id: `fb-spend-${currency || 'none'}`, title, subtitle: t('fb.ads.daily_subtitle'),
        tableMode: state.tableMode,
        table: days.length ? {
          columns: [{ label: t('common.day') }, { label: t('fb.ads.spend'), numeric: true },
            { label: t('fb.ads.started'), numeric: true }],
          rows: days.map((day, index) => [dayLabel(day, true), adMoney(values[index], currency),
            num(started.get(day) || 0)]),
        } : null,
        draw: (body) => {
          if (days.length < 2) {
            body.appendChild(chartEmpty(t('fb.ads.daily_empty')));
            return null;
          }
          return lineChart(body, {
            days, label: title, area: true, format: (value) => adMoney(value, currency),
            series: [{ key: 's1', name: t('fb.ads.spend'), values }],
          });
        },
      });
    });
  }

  function adCard(ad) {
    const text = excerpt(ad.body || ad.title, 160);
    const status = ad.effective_status || ad.status;
    const figures = [
      [t('fb.ads.spend'), adMoney(ad.spend, ad.currency)],
      [t('fb.ads.started'), num(ad.messaging_started)],
      [t('fb.ads.cost_per_chat'), ad.cost_per_chat === null ? '—'
        : adMoney(ad.cost_per_chat, ad.currency)],
      [t('fb.ads.ctr'), ad.ctr === null || ad.ctr === undefined ? '—'
        : percent(Number(ad.ctr) / 100, 2)],
      [t('fb.ads.handled'), t('fb.ads.handled_value', {
        handled: num(ad.chats_handled), chats: num(ad.chats),
      })],
    ];
    return h('li', { class: 'photo' },
      picture(ad.image_url, t('fb.ads.picture', { name: nameOf(ad) })),
      h('div', { class: 'photo__body' },
        h('div', { class: 'photo__top' },
          h('strong', { text: nameOf(ad) }),
          status ? h('span', { class: 'chip', text: label('fb.ads.status', status) }) : null),
        ad.campaign_name ? h('p', { class: 'photo__meta', text: t('fb.ads.campaign',
          { name: ad.campaign_name }) }) : null,
        text ? h('p', { class: 'card__text', text }) : null,
        h('dl', { class: 'facts' }, figures.map(([name, value]) => h('div', {},
          h('dt', { text: name }), h('dd', { text: value })))),
        h('div', { class: 'photo__actions' },
          externalLink(ad.ads_manager_url, t('fb.ads.open_manager')))));
  }

  function accountsCard(accounts) {
    const titleId = uid('acc');
    return h('section', { class: 'card', 'aria-labelledby': titleId },
      h('h3', { class: 'card__title', id: titleId, text: t('fb.ads.accounts') }),
      h('p', { class: 'card__text', text: t('fb.ads.accounts_text') }),
      h('dl', { class: 'facts' }, accounts.map((account) => h('div', {},
        h('dt', { text: account.name || account.account_id }),
        h('dd', { text: t('fb.ads.account_line', {
          count: num(account.garage_ads), currency: account.currency || '—',
        }) })))));
  }

  function render() {
    if (!alive) {
      return;
    }
    dispose();
    const refresh = iconButton('refresh', t('fb.refresh'), { onClick: () => load() });
    const toolbar = h('div', { class: 'toolbar toolbar--wrap' },
      rangeControl(state.days, (days) => {
        state.days = days;
        remembered = days;
        load();
      }), refresh, jobButton('ads'));
    const note = h('p', { class: 'page-note', text: t('fb.ads.read_only') });
    if (state.status === 'loading') {
      mount(root, toolbar, loadingBlock(
        h('div', { class: 'tiles tiles--admin' }, Array.from({ length: 4 }, () => h('div',
          { class: 'tile tile--skeleton' }, skeleton('line'), skeleton('value')))),
        card({}, skeleton('title'), skeleton('line', 3))));
      return;
    }
    if (state.status === 'error') {
      mount(root, toolbar, errorCard(state.error, () => load()));
      return;
    }
    const { ads = [], best = [], accounts = [], min_spend: minSpend = 10 } = state.data || {};
    if (!accounts.length) {
      mount(root, toolbar, card({}, emptyState({
        iconName: 'chart', title: t('fb.ads.no_accounts'), text: t('fb.ads.no_accounts_text'),
      })), note);
      return;
    }
    if (!ads.length) {
      mount(root, toolbar, card({}, emptyState({
        iconName: 'chart', title: t('fb.ads.none'), text: t('fb.ads.none_text'),
      })), accountsCard(accounts), note);
      return;
    }
    const charts = spendCharts(state.daily);
    cards.push(...charts);
    const more = ads.length > state.shown ? h('div', { class: 'results__footer' },
      button(t('fb.ads.more', { count: num(ads.length - state.shown) }), {
        block: true, dataset: { key: 'more-ads' },
        onClick: () => {
          state.shown += CARDS;
          render();
          const next = root.querySelector('[data-key="more-ads"]')
            || root.querySelector('.photos');
          if (next instanceof HTMLElement) {
            next.focus({ preventScroll: true });
          }
        },
      })) : null;
    mount(root, toolbar, tiles(ads),
      section(t('fb.ads.results'), bestCard(best, minSpend),
        h('div', { class: 'grid' }, charts.map((item) => item.node))),
      section(t('fb.ads.all'), h('ul', { class: 'photos', 'aria-label': t('fb.ads.all'),
        tabindex: '-1' }, ads.slice(0, state.shown).map(adCard)), more),
      accountsCard(accounts), note);
  }

  async function load() {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    state.status = 'loading';
    state.error = null;
    state.shown = CARDS;
    render();
    try {
      const [data, daily] = await Promise.all([
        activityApi.ads(state.days, controller.signal),
        activityApi.adsDaily(state.days, controller.signal),
      ]);
      state.data = data;
      state.daily = daily.daily || [];
      state.status = 'ready';
    } catch (error) {
      if (isAbort(error)) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    render();
  }

  load();

  return {
    redraw: render,
    destroy() {
      alive = false;
      dispose();
      if (controller) {
        controller.abort();
      }
    },
  };
}
