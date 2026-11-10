// Overview: headline figures, stock charts, and for admins the inferred trade and chat
// reports. Forms follow the figures: single numbers are tiles, rankings are bars in one
// hue, a distribution is columns, change over time is a line.

import { api, isAbort } from '../api.js';
import {
  chartCard, chartEmpty, columnChart, inlineBar, lineChart, rankedBars, splitBar,
} from '../charts.js';
import { h, icon, mount, statusIcon, uid } from '../dom.js';
import {
  dateTime, dayLabel, dayRange, longDate, money, num, percent, qty, relativeTime, toNumber,
} from '../format.js';
import { pick, t } from '../strings.js';
import { card, errorState, loadingBlock, notice, pageHead, skeleton } from '../ui.js';

const DAYS = 30;
const MIN_DAYS = 3; // Fewer days than this is a "collecting" state, not a trend.
// A finished sync is recorded as `partial` by design (it never deactivates products), so
// it is the normal outcome here, not a warning.
const SYNC_KIND = { succeeded: 'good', partial: 'good', failed: 'critical', running: 'pending' };
const SETUP_LINKS = {
  phone: '/admin/content/texts/brand?focus=phone',
  map_url: '/admin/content/texts/brand?focus=map_url',
  address: '/admin/content/texts/brand?focus=address',
  hours: '/admin/content/texts/brand?focus=hours',
  milestones: '/admin/content/texts/story?focus=milestones',
  service_prices: '/admin/content/services',
};

function tile(label, value, note, extra) {
  return h('div', { class: ['tile', extra && extra.wide && 'tile--wide'] },
    h('p', { class: 'tile__label', text: label }),
    h('p', { class: ['tile__value', extra && extra.text && 'tile__value--text'], text: value }),
    extra && extra.status,
    note && h('p', { class: 'tile__note', text: note }));
}

function syncTile(sync) {
  if (!sync || !sync.available) {
    return tile(t('tile.last_sync'), t('sync.unavailable'), null, {
      text: true,
      status: h('p', { class: 'tile__status' }, statusIcon('unknown', 16), t('sync.unknown')),
    });
  }
  if (!sync.last_finished_at) {
    return tile(t('tile.last_sync'), t('sync.never'), null, { text: true });
  }
  const kind = SYNC_KIND[sync.last_status] || 'unknown';
  const label = SYNC_KIND[sync.last_status] ? t(`sync.${sync.last_status}`) : t('sync.unknown');
  return tile(t('tile.last_sync'), relativeTime(sync.last_finished_at), dateTime(sync.last_finished_at), {
    text: true,
    status: h('p', { class: 'tile__status' }, statusIcon(kind, 16), label),
  });
}

function tiles(overview, admin) {
  const figures = overview.inventory.tiles;
  const share = figures.items ? percent(figures.available / figures.items) : '';
  const list = [
    tile(t('tile.items'), num(figures.items)),
    tile(t('tile.available'), num(figures.available), share && t('tile.available_note', { percent: share })),
    tile(t('tile.in_store'), num(figures.in_store), t('tile.in_store_note')),
    tile(t('tile.warehouse_only'), num(figures.warehouse_only), t('tile.warehouse_note')),
    tile(t('tile.out_of_stock'), num(figures.out_of_stock)),
    syncTile(overview.sync),
  ];
  if (admin && overview.inventory.stock_value !== undefined) {
    const unpriced = figures.unpriced
      ? t('tile.unpriced_note', { count: num(figures.unpriced) }) : '';
    list.push(tile(t('tile.stock_value'), money(overview.inventory.stock_value),
      [t('tile.stock_value_note'), unpriced].filter(Boolean).join('. '), { wide: true }));
  }
  return h('section', {
    class: ['tiles', admin ? 'tiles--admin' : 'tiles--staff'], 'aria-label': t('overview.tiles'),
  }, list);
}

function setupCard(codes) {
  const titleId = uid('setup');
  return card({ class: 'setup', 'aria-labelledby': titleId },
    h('h2', { class: 'card__title', id: titleId, text: t('setup.title') }),
    h('p', { class: 'card__text', text: t('setup.text') }),
    h('ul', { class: 'setup__list' }, codes.filter((code) => SETUP_LINKS[code]).map((code) => (
      h('li', {}, h('a', { class: 'setup__item', href: SETUP_LINKS[code], dataset: { link: '' } },
        icon('circle', 18),
        h('span', { class: 'setup__name', text: t(`setup.${code}`) }),
        h('span', { class: 'setup__go' }, t('setup.open'), icon('chevron-right', 16)))))))
  );
}

function locationCard(figures) {
  const store = toNumber(figures.store_units) || 0;
  const warehouse = toNumber(figures.warehouse_units) || 0;
  const total = store + warehouse;
  const unit = (value) => `${value} ${t('chart.location.units')}`;
  const figure = (key, label, value) => h('div', { class: 'figure' },
    h('p', { class: 'figure__label' }, h('span', { class: `swatch ${key}` }), label),
    h('p', { class: 'figure__value', text: qty(value) }),
    h('p', { class: 'figure__note', text: total ? percent(value / total) : '' }));
  const titleId = uid('loc');
  return h('section', { class: 'card chart chart--wide location', 'aria-labelledby': titleId },
    h('div', { class: 'chart__head' }, h('div', { class: 'chart__titles' },
      h('h3', { class: 'chart__title', id: titleId, text: t('chart.location.title') }),
      h('p', { class: 'chart__subtitle', text: t('chart.location.subtitle') }))),
    h('div', { class: 'location__body' },
      h('div', { class: 'location__figures' },
        figure('s1', t('chart.location.store'), store),
        figure('s2', t('chart.location.warehouse'), warehouse)),
      total ? splitBar([
      { key: 's1', label: t('chart.location.store'), value: store },
      { key: 's2', label: t('chart.location.warehouse'), value: warehouse },
      ], t('chart.location.bar', { store: qty(store), warehouse: qty(warehouse) }), unit)
        : chartEmpty(t('chart.empty'))));
}

/** Server rows to ranked-bar rows: readable labels, the remainder last. */
function ranked(rows, labelOf) {
  const named = rows.map((row) => ({
    label: row.key === '__other__' ? t('common.other') : labelOf(row),
    value: toNumber(row.count) || 0,
    other: row.key === '__other__',
  }));
  return [...named.filter((row) => !row.other), ...named.filter((row) => row.other)];
}

function rankedCard({ id, title, subtitle, rows, total, column, tableMode, note }) {
  const items = (value) => t('tip.items', { count: value });
  const columns = [{ label: column }, { label: t('col.items'), numeric: true }];
  if (total) {
    columns.push({ label: t('common.share'), numeric: true });
  }
  return chartCard({
    id, title, subtitle, tableMode, note,
    table: rows.length ? {
      columns,
      rows: rows.map((row) => (total
        ? [row.label, num(row.value), percent(row.value / total)] : [row.label, num(row.value)])),
    } : null,
    draw: (body) => (rows.length
      ? rankedBars(body, { rows, total, unit: items })
      : void body.appendChild(chartEmpty(t('chart.empty')))),
  });
}

function stockCharts(inventory, tableMode) {
  const available = toNumber(inventory.tiles.available) || 0;
  const brands = ranked(inventory.brands || [], (row) => (row.key === '?' ? t('chart.brands.none') : row.key));
  const categories = ranked(inventory.categories || [], (row) => pick(row.label) || row.key);
  const makes = ranked(inventory.makes || [], (row) => row.name || row.key);
  const categorised = categories.reduce((sum, row) => sum + row.value, 0);
  const bins = (inventory.prices || []).map((bin) => ({
    tick: num(bin.from),
    label: bin.to === null || bin.to === undefined
      ? t('chart.prices.open', { from: num(bin.from) })
      : t('chart.prices.range', { from: num(bin.from), to: num(bin.to) }),
    value: toNumber(bin.count) || 0,
  }));
  const priced = bins.reduce((sum, bin) => sum + bin.value, 0);
  const items = (value) => t('tip.items', { count: value });
  const prices = chartCard({
    id: 'prices', title: t('chart.prices.title'), subtitle: t('chart.prices.subtitle'), tableMode,
    table: priced ? {
      columns: [{ label: t('col.price_range') }, { label: t('col.items'), numeric: true },
        { label: t('common.share'), numeric: true }],
      rows: bins.map((bin) => [bin.label, num(bin.value), percent(bin.value / priced)]),
    } : null,
    draw: (body) => (priced
      ? columnChart(body, {
        bins, total: priced, unit: items, axisLabel: t('chart.prices.axis'), label: t('chart.prices.title'),
      })
      : void body.appendChild(chartEmpty(t('chart.empty')))),
  });
  return [
    { node: locationCard(inventory.tiles), dispose() {} },
    prices,
    rankedCard({
      id: 'brands', title: t('chart.brands.title'), subtitle: t('chart.brands.subtitle'),
      rows: brands, total: available, column: t('col.brand'), tableMode,
    }),
    rankedCard({
      id: 'categories', title: t('chart.categories.title'),
      subtitle: t('chart.categories.subtitle'),
      note: inventory.uncategorised ? t('chart.categories.note', { count: num(inventory.uncategorised) }) : '',
      rows: categories, total: categorised, column: t('col.category'), tableMode,
    }),
    rankedCard({
      id: 'makes', title: t('chart.makes.title'), subtitle: t('chart.makes.subtitle'),
      note: t('chart.makes.note'), rows: makes, total: 0, column: t('col.make'), tableMode,
    }),
  ];
}

function byDay(rows, field) {
  const map = new Map();
  for (const row of rows || []) {
    map.set(String(row.day), toNumber(row[field]) || 0);
  }
  return map;
}

function tradeCharts(report, tableMode) {
  if (!report || !report.available) {
    const empty = (id, title, subtitle) => chartCard({
      id, title, subtitle, tableMode, table: null,
      draw: (body) => void body.appendChild(chartEmpty(t('trade.unavailable'))),
    });
    return [empty('trade', t('chart.trade.title'), t('chart.trade.subtitle')),
      empty('amount', t('chart.amount.title'), t('chart.amount.subtitle'))];
  }
  const sold = byDay(report.sales, 'units');
  const received = byDay(report.purchases, 'units');
  const amounts = byDay(report.sales, 'amount');
  const seen = [...new Set([...sold.keys(), ...received.keys()])].sort();
  const days = seen.length ? dayRange(seen[0], seen[seen.length - 1]) : [];
  const soldTotal = [...sold.values()].reduce((sum, value) => sum + value, 0);
  const receivedTotal = [...received.values()].reduce((sum, value) => sum + value, 0);
  const amountTotal = [...amounts.values()].reduce((sum, value) => sum + value, 0);
  const since = report.since ? t('trade.collecting', { date: longDate(report.since) }) : '';
  const enough = seen.length >= MIN_DAYS;
  const unitsOf = (value) => `${num(value, 1)} ${t('chart.location.units')}`;

  const trade = chartCard({
    id: 'trade', title: t('chart.trade.title'), subtitle: t('chart.trade.subtitle'), tableMode,
    table: seen.length ? {
      columns: [{ label: t('common.day') }, { label: t('series.sold'), numeric: true },
        { label: t('series.received'), numeric: true }],
      rows: days.map((day) => [dayLabel(day, true), num(sold.get(day) || 0, 1), num(received.get(day) || 0, 1)]),
      foot: [t('common.total'), num(soldTotal, 1), num(receivedTotal, 1)],
    } : null,
    draw: (body) => {
      if (!seen.length) {
        body.appendChild(chartEmpty(t('trade.empty'), since));
        return null;
      }
      if (!enough) {
        body.appendChild(chartEmpty(t('trade.collecting_text'), since,
          t('trade.sofar_units', { sold: num(soldTotal, 1), received: num(receivedTotal, 1) })));
        return null;
      }
      return lineChart(body, {
        days,
        label: t('chart.trade.title'),
        format: unitsOf,
        series: [
          { key: 's1', name: t('series.sold'), values: days.map((day) => sold.get(day) || 0) },
          { key: 's2', name: t('series.received'), values: days.map((day) => received.get(day) || 0) },
        ],
      });
    },
  });
  const amount = chartCard({
    id: 'amount', title: t('chart.amount.title'), subtitle: t('chart.amount.subtitle'), tableMode,
    table: sold.size ? {
      columns: [{ label: t('common.day') }, { label: t('series.amount'), numeric: true }],
      rows: days.map((day) => [dayLabel(day, true), num(amounts.get(day) || 0, 2)]),
      foot: [t('common.total'), num(amountTotal, 2)],
    } : null,
    draw: (body) => {
      if (!sold.size) {
        body.appendChild(chartEmpty(t('trade.empty'), since));
        return null;
      }
      if (!enough) {
        body.appendChild(chartEmpty(t('trade.collecting_text'), since,
          t('trade.sofar_amount', { amount: money(amountTotal) })));
        return null;
      }
      return lineChart(body, {
        days,
        label: t('chart.amount.title'),
        format: (value) => money(value, 2),
        area: true,
        series: [{ key: 's1', name: t('series.amount'), values: days.map((day) => amounts.get(day) || 0) }],
      });
    },
  });
  return [trade, amount];
}

function bestSellers(report) {
  const rows = (report && report.available && report.best_sellers) || [];
  const titleId = uid('best');
  const max = Math.max(1, ...rows.map((row) => toNumber(row.units) || 0));
  const body = rows.length ? h('div', { class: 'table-wrap' },
    h('table', { class: 'table table--best' },
      h('caption', { class: 'visually-hidden', text: t('best.title') }),
      h('thead', {}, h('tr', {},
        h('th', { scope: 'col', text: t('best.item') }),
        h('th', { scope: 'col', class: 'num', text: t('best.units') }),
        h('th', { scope: 'col', class: 'num', text: t('best.amount') }))),
      h('tbody', {}, rows.map((row) => {
        const units = toNumber(row.units) || 0;
        const about = [row.fits, row.brand].filter(Boolean).join(' · ');
        return h('tr', {},
          h('th', { scope: 'row' },
            h('span', { class: 'best__name', text: row.name || about || '?' }),
            row.name && about && h('span', { class: 'best__about', text: about })),
          h('td', { class: 'num best__units' },
            h('span', { class: 'best__bar' }, inlineBar(units / max)),
            h('span', { class: 'best__count', text: num(units, 1) })),
          h('td', { class: 'num', text: money(row.amount) }));
      }))))
    : chartEmpty(t('best.empty'));
  return h('section', { class: 'card chart chart--wide', 'aria-labelledby': titleId },
    h('div', { class: 'chart__head' }, h('div', { class: 'chart__titles' },
      h('h3', { class: 'chart__title', id: titleId, text: t('best.title') }),
      h('p', { class: 'chart__subtitle', text: t('best.subtitle') }))),
    body);
}

function chatCharts(report, tableMode) {
  const staff = byDay(report && report.staff && report.staff.daily, 'turns');
  const site = byDay(report && report.site && report.site.daily, 'turns');
  const seen = [...new Set([...staff.keys(), ...site.keys()])].sort();
  const days = seen.length ? dayRange(seen[0], seen[seen.length - 1]) : [];
  const staffTotal = [...staff.values()].reduce((sum, value) => sum + value, 0);
  const siteTotal = [...site.values()].reduce((sum, value) => sum + value, 0);
  const turns = chartCard({
    id: 'chat', title: t('chart.chat.title'), subtitle: t('chart.chat.subtitle'), tableMode,
    table: seen.length ? {
      columns: [{ label: t('common.day') }, { label: t('series.staff_chat'), numeric: true },
        { label: t('series.site_chat'), numeric: true }],
      rows: days.map((day) => [dayLabel(day, true), num(staff.get(day) || 0), num(site.get(day) || 0)]),
      foot: [t('common.total'), num(staffTotal), num(siteTotal)],
    } : null,
    draw: (body) => {
      if (!seen.length) {
        body.appendChild(chartEmpty(t('chat.empty')));
        return null;
      }
      if (seen.length < MIN_DAYS) {
        body.appendChild(chartEmpty(t('chat.collecting_text'), null,
          t('chat.sofar', { staff: num(staffTotal), site: num(siteTotal) })));
        return null;
      }
      return lineChart(body, {
        days,
        small: true,
        label: t('chart.chat.title'),
        format: (value) => num(value),
        series: [
          { key: 's1', name: t('series.staff_chat'), values: days.map((day) => staff.get(day) || 0) },
          { key: 's2', name: t('series.site_chat'), values: days.map((day) => site.get(day) || 0) },
        ],
      });
    },
  });
  // Topics from both chats, merged by key.
  const merged = new Map();
  for (const source of [report && report.staff, report && report.site]) {
    for (const topic of (source && source.topics) || []) {
      const entry = merged.get(topic.key) || { key: topic.key, count: 0, label: topic.label };
      entry.count += toNumber(topic.count) || 0;
      merged.set(topic.key, entry);
    }
  }
  const topicRows = ranked([...merged.values()].sort((a, b) => b.count - a.count),
    (row) => pick(row.label) || row.key);
  const topicTotal = topicRows.reduce((sum, row) => sum + row.value, 0);
  const topics = chartCard({
    id: 'topics', title: t('chart.topics.title'), subtitle: t('chart.topics.subtitle'), tableMode,
    table: topicRows.length ? {
      columns: [{ label: t('col.topic') }, { label: t('col.turns'), numeric: true },
        { label: t('common.share'), numeric: true }],
      rows: topicRows.map((row) => [row.label, num(row.value), percent(row.value / topicTotal)]),
    } : null,
    draw: (body) => (topicRows.length
      ? rankedBars(body, { rows: topicRows, total: topicTotal, unit: (value) => value })
      : void body.appendChild(chartEmpty(t('chat.empty')))),
  });
  return [turns, topics];
}

function skeletonCards(count) {
  return Array.from({ length: count }, () => h('div', { class: 'card chart chart--skeleton', 'aria-hidden': 'true' },
    skeleton('title'), skeleton('line'), h('div', { class: 'skel-bars' }, skeleton('bar', 6))));
}

export function mountView(root, ctx) {
  const state = {
    status: 'loading', error: null, overview: null,
    sales: { status: 'loading', report: null, error: null },
    chat: { status: 'loading', report: null, error: null },
    tableMode: new Set(),
  };
  let controller = null;
  let cards = [];

  const disposeCards = () => {
    for (const item of cards) {
      item.dispose();
    }
    cards = [];
  };

  const section = (title, ...children) => {
    const id = uid('sec');
    return h('section', { class: 'block', 'aria-labelledby': id },
      h('h2', { class: 'block__title', id, text: title }), children);
  };

  const reportBlock = (slot, build, reload, count) => {
    if (slot.status === 'loading') {
      return h('div', { class: 'grid grid--2', 'aria-busy': 'true' }, skeletonCards(count));
    }
    if (slot.status === 'error') {
      return card({}, errorState(slot.error, reload));
    }
    return build();
  };

  function draw() {
    disposeCards();
    const head = pageHead(t('overview.title'));
    if (state.status === 'loading') {
      mount(root, head, loadingBlock(
        h('div', { class: ['tiles', ctx.isAdmin ? 'tiles--admin' : 'tiles--staff'] },
          Array.from({ length: ctx.isAdmin ? 7 : 6 }, (unused, index) => h('div', {
            class: ['tile', 'tile--skeleton', ctx.isAdmin && index === 6 && 'tile--wide'],
          }, skeleton('line'), skeleton('value')))),
        h('div', { class: 'grid grid--2' }, skeletonCards(4))));
      return;
    }
    if (state.status === 'error') {
      mount(root, head, card({}, errorState(state.error, load)));
      return;
    }
    const { overview } = state;
    const inventory = overview.inventory;
    const notes = [];
    if (!overview.catalog_ready) {
      notes.push(notice('info', t('overview.catalog_loading')));
    } else if (inventory.freshness === 'confirm' || inventory.freshness === 'stale') {
      notes.push(notice(inventory.freshness === 'stale' ? 'critical' : 'warning',
        t(`fresh.${inventory.freshness}`)));
    }
    if (inventory.observed_at) {
      head.appendChild(h('p', { class: 'page-head__note',
        text: t('overview.stock_as_of', { time: dateTime(inventory.observed_at) }) }));
    }
    const stock = stockCharts(inventory, state.tableMode);
    cards.push(...stock);
    const blocks = [
      head, notes, tiles(overview, ctx.isAdmin),
      ctx.isAdmin && overview.setup && overview.setup.length ? setupCard(overview.setup) : null,
      section(t('section.stock'), h('div', { class: 'grid grid--2' }, stock.map((item) => item.node))),
    ];
    if (ctx.isAdmin) {
      blocks.push(section(t('section.trade'), reportBlock(state.sales, () => {
        const charts = tradeCharts(state.sales.report, state.tableMode);
        cards.push(...charts);
        return [h('div', { class: 'grid grid--2' }, charts.map((item) => item.node)),
          bestSellers(state.sales.report)];
      }, loadSales, 2)));
      blocks.push(section(t('section.chat'), reportBlock(state.chat, () => {
        const charts = chatCharts(state.chat.report, state.tableMode);
        cards.push(...charts);
        return h('div', { class: 'grid grid--2' }, charts.map((item) => item.node));
      }, loadChat, 2)));
    }
    mount(root, blocks);
  }

  async function loadReport(slot, fetcher) {
    slot.status = 'loading';
    slot.error = null;
    try {
      slot.report = await fetcher(controller.signal);
      slot.status = 'ready';
    } catch (error) {
      if (isAbort(error)) {
        return;
      }
      slot.error = error;
      slot.status = 'error';
    }
    if (state.status === 'ready') {
      draw();
    }
  }

  function loadSales() {
    const pending = loadReport(state.sales, (signal) => api.sales(DAYS, signal));
    draw();
    return pending;
  }

  function loadChat() {
    const pending = loadReport(state.chat, (signal) => api.chat(DAYS, signal));
    draw();
    return pending;
  }

  async function load() {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    state.status = 'loading';
    state.error = null;
    draw();
    if (ctx.isAdmin) {
      loadReport(state.sales, (signal) => api.sales(DAYS, signal));
      loadReport(state.chat, (signal) => api.chat(DAYS, signal));
    }
    try {
      state.overview = await api.overview(controller.signal);
      state.status = 'ready';
    } catch (error) {
      if (isAbort(error)) {
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
      if (controller) {
        controller.abort();
      }
      disposeCards();
    },
  };
}
