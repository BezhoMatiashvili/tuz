// Inventory: search and filter the whole catalogue. A table on wide screens, stacked cards
// on phones; the same rows either way.

import { api, describeError, isAbort } from '../api.js';
import { h, icon, mount, statusIcon } from '../dom.js';
import { num, price, qty } from '../format.js';
import { t } from '../strings.js';
import {
  announce, button, card, emptyState, errorState, field, notice, pageHead, segmented, select,
  skeleton,
} from '../ui.js';

const DEBOUNCE = 250;
const STATES = ['all', 'available', 'out'];
const STATE_ICON = {
  IN_STORE: 'good', WITHIN_1_2_HOURS: 'pending', UNAVAILABLE: 'neutral', UNKNOWN: 'unknown',
};
const WIDE = '(min-width: 768px)';

function stateLabel(code) {
  const known = Object.prototype.hasOwnProperty.call(STATE_ICON, code) ? code : 'UNKNOWN';
  return h('span', { class: `stock stock--${known.toLowerCase()}` },
    statusIcon(STATE_ICON[known], 16), h('span', { text: t(`state.${known}`) }));
}

function priceText(value) {
  return value ? price(value) : t('inventory.no_price');
}

function quantity(value) {
  return value === null || value === undefined ? '?' : qty(value);
}

function tableRows(items) {
  return items.map((item) => h('tr', {},
    h('th', { scope: 'row', class: 'inv__item' },
      h('span', { class: 'inv__name', text: item.name || item.code }),
      item.fits && h('span', { class: 'inv__fits', text: item.fits })),
    h('td', { text: item.brand || '' }),
    h('td', { class: 'code', text: item.code || '' }),
    h('td', { class: ['num', !item.price && 'muted'], text: priceText(item.price) }),
    h('td', { class: 'num', text: quantity(item.store) }),
    h('td', { class: 'num', text: quantity(item.warehouse) }),
    h('td', {}, stateLabel(item.state))));
}

function cardRows(items) {
  return items.map((item) => h('li', { class: 'inv-card' },
    h('div', { class: 'inv-card__top' },
      h('p', { class: 'inv-card__name', text: item.name || item.code }),
      h('p', { class: ['inv-card__price', !item.price && 'muted'], text: priceText(item.price) })),
    item.fits && h('p', { class: 'inv-card__fits', text: item.fits }),
    h('p', { class: 'inv-card__meta' },
      item.brand && h('span', {},
        h('span', { class: 'visually-hidden', text: `${t('inventory.col.brand')}: ` }), item.brand),
      item.brand && item.code && h('span', { 'aria-hidden': 'true', text: ' · ' }),
      item.code && h('span', { class: 'code' },
        h('span', { class: 'visually-hidden', text: `${t('inventory.col.code')}: ` }), item.code)),
    h('div', { class: 'inv-card__bottom' },
      stateLabel(item.state),
      h('dl', { class: 'inv-card__qty' },
        h('div', {}, h('dt', { text: t('inventory.col.store') }), h('dd', { text: quantity(item.store) })),
        h('div', {}, h('dt', { text: t('inventory.col.warehouse') }), h('dd', { text: quantity(item.warehouse) }))))));
}

function skeletonTable(count) {
  return Array.from({ length: count }, () => h('tr', { class: 'inv__skeleton', 'aria-hidden': 'true' },
    h('td', {}, skeleton('line'), skeleton('line-short')), h('td', {}, skeleton('line-short')),
    h('td', {}, skeleton('line-short')), h('td', {}, skeleton('line-short')),
    h('td', {}, skeleton('line-short')), h('td', {}, skeleton('line-short')),
    h('td', {}, skeleton('line'))));
}

function skeletonCards(count) {
  return Array.from({ length: count }, () => h('li', { class: 'inv-card inv-card--skeleton', 'aria-hidden': 'true' },
    skeleton('line'), skeleton('line-short'), skeleton('line-short'), skeleton('line')));
}

export function mountView(root, ctx) {
  const query = new URLSearchParams(window.location.search);
  const state = {
    q: (query.get('q') || '').slice(0, 80),
    brand: query.get('brand') || '',
    make: query.get('make') || '',
    stock: STATES.includes(query.get('state')) ? query.get('state') : 'all',
    items: [], page: 0, total: 0, hasMore: false, options: null,
    status: 'loading', // loading | ready | error
    more: 'idle', // idle | loading | error
    error: null,
  };
  const media = window.matchMedia(WIDE);
  let controller = null;
  let timer = 0;
  let results = null;
  let brandSelect = null;
  let makeSelect = null;

  const filtered = () => Boolean(state.q || state.brand || state.make || state.stock !== 'all');

  function syncUrl() {
    const search = new URLSearchParams();
    if (state.q) {
      search.set('q', state.q);
    }
    if (state.brand) {
      search.set('brand', state.brand);
    }
    if (state.make) {
      search.set('make', state.make);
    }
    if (state.stock !== 'all') {
      search.set('state', state.stock);
    }
    const text = search.toString();
    ctx.replaceUrl(text ? `/admin/inventory?${text}` : '/admin/inventory');
  }

  function fillSelects() {
    if (!brandSelect || !makeSelect) {
      return;
    }
    const brands = ((state.options && state.options.brands) || [])
      .map((entry) => (typeof entry === 'string' ? entry : entry && (entry.name || entry.key)))
      .filter(Boolean);
    if (state.brand && !brands.includes(state.brand)) {
      brands.unshift(state.brand);
    }
    const makes = ((state.options && state.options.makes) || []).filter((entry) => entry && entry.key);
    if (state.make && !makes.some((entry) => entry.key === state.make)) {
      makes.unshift({ key: state.make, name: state.make });
    }
    mount(brandSelect, h('option', { value: '', text: t('inventory.all_brands') }),
      brands.map((name) => h('option', { value: name, text: name })));
    brandSelect.value = state.brand;
    mount(makeSelect, h('option', { value: '', text: t('inventory.all_makes') }),
      makes.map((entry) => h('option', {
        value: entry.key,
        text: entry.count ? `${entry.name || entry.key} (${num(entry.count)})` : (entry.name || entry.key),
      })));
    makeSelect.value = state.make;
  }

  function drawResults() {
    if (!results) {
      return;
    }
    const wide = media.matches;
    if (state.status === 'error') {
      mount(results, card({}, errorState(state.error, () => load(true))));
      return;
    }
    const loading = state.status === 'loading';
    if (!loading && !state.items.length) {
      mount(results, card({}, emptyState({
        iconName: 'search', title: t('inventory.empty'), text: filtered() ? t('inventory.empty_hint') : '',
        action: filtered() ? button(t('inventory.clear'), { onClick: clearFilters }) : null,
      })));
      return;
    }
    const first = loading && !state.items.length;
    const pending = state.more === 'loading';
    const count = h('p', { class: 'results__count', text: first ? t('common.loading')
      : t('inventory.count', { shown: num(state.items.length), total: num(state.total) }) });
    let list;
    if (wide) {
      list = h('div', { class: 'card table-wrap' },
        h('table', { class: 'table inv', 'aria-busy': String(loading) },
          h('caption', { class: 'visually-hidden', text: t('inventory.results') }),
          h('thead', {}, h('tr', {},
            h('th', { scope: 'col', text: t('inventory.col.name') }),
            h('th', { scope: 'col', text: t('inventory.col.brand') }),
            h('th', { scope: 'col', text: t('inventory.col.code') }),
            h('th', { scope: 'col', class: 'num', text: t('inventory.col.price') }),
            h('th', { scope: 'col', class: 'num', text: t('inventory.col.store') }),
            h('th', { scope: 'col', class: 'num', text: t('inventory.col.warehouse') }),
            h('th', { scope: 'col', text: t('inventory.col.state') }))),
          h('tbody', {}, first ? skeletonTable(10) : tableRows(state.items),
            pending && skeletonTable(3))));
    } else {
      list = h('ul', { class: 'inv-cards', 'aria-label': t('inventory.results'), 'aria-busy': String(loading) },
        first ? skeletonCards(6) : cardRows(state.items), pending && skeletonCards(2));
    }
    if (loading && !first) {
      list.classList.add('is-stale'); // Keep the previous rows in place while the new ones load.
    }
    const footer = [];
    if (state.more === 'error') {
      footer.push(notice('critical', describeError(state.error),
        button(t('common.retry'), { small: true, onClick: () => load(false) })));
    } else if (state.hasMore && !loading) {
      footer.push(button(pending ? t('common.loading') : t('inventory.more'), {
        block: true, disabled: pending, onClick: () => load(false), class: 'results__more',
      }));
    }
    mount(results, count, list, footer.length ? h('div', { class: 'results__footer' }, footer) : null);
  }

  async function load(reset) {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    const { signal } = controller;
    const page = reset ? 1 : state.page + 1;
    if (reset) {
      state.status = 'loading';
      state.more = 'idle';
    } else {
      state.more = 'loading';
    }
    state.error = null;
    drawResults();
    try {
      const response = await api.inventory({
        q: state.q.trim(), brand: state.brand, make: state.make, state: state.stock, page,
      }, signal);
      const items = Array.isArray(response.items) ? response.items : [];
      state.items = reset ? items : [...state.items, ...items];
      state.page = response.page || page;
      state.total = response.total || 0;
      state.hasMore = Boolean(response.has_more);
      if (response.options) {
        state.options = response.options;
        fillSelects();
      }
      state.status = 'ready';
      state.more = 'idle';
      if (reset) {
        announce(state.items.length
          ? t('inventory.count', { shown: num(state.items.length), total: num(state.total) })
          : t('inventory.empty'));
      }
    } catch (error) {
      if (isAbort(error)) {
        return;
      }
      state.error = error;
      if (reset) {
        state.status = 'error';
        state.items = []; // The old rows answered an older question: a retry starts clean.
        state.total = 0;
        state.hasMore = false;
      } else {
        state.more = 'error';
      }
    }
    drawResults();
  }

  function changed(immediate) {
    window.clearTimeout(timer);
    syncUrl();
    if (immediate) {
      load(true);
    } else {
      timer = window.setTimeout(() => load(true), DEBOUNCE);
    }
  }

  function clearFilters() {
    state.q = '';
    state.brand = '';
    state.make = '';
    state.stock = 'all';
    draw();
    changed(true);
    root.querySelector('.input--search').focus(); // The button that was pressed is gone.
  }

  function draw() {
    const search = h('input', {
      class: 'input input--search', type: 'search', maxlength: 80, value: state.q,
      placeholder: t('inventory.search_placeholder'), autocomplete: 'off', autocapitalize: 'none',
      spellcheck: 'false', enterkeyhint: 'search',
    });
    search.addEventListener('input', () => {
      state.q = search.value;
      changed(false);
    });
    brandSelect = select([], '', {});
    brandSelect.addEventListener('change', () => {
      state.brand = brandSelect.value;
      changed(true);
    });
    makeSelect = select([], '', {});
    makeSelect.addEventListener('change', () => {
      state.make = makeSelect.value;
      changed(true);
    });
    fillSelects();
    const stock = segmented({
      label: t('inventory.state'), value: state.stock,
      options: [
        { value: 'all', label: t('inventory.state_all') },
        { value: 'available', label: t('inventory.state_available') },
        { value: 'out', label: t('inventory.state_out') },
      ],
      onChange: (value) => {
        state.stock = value;
        changed(true);
      },
    });
    const searchField = field({
      label: t('inventory.search'), control: search,
      wrap: h('div', { class: 'input-wrap input-wrap--lead' }, icon('search', 18), search),
    });
    const form = h('form', { class: 'filters card', role: 'search', 'aria-label': t('inventory.filters') },
      h('div', { class: 'filters__search' }, searchField.node),
      field({ label: t('inventory.brand'), control: brandSelect }).node,
      field({ label: t('inventory.make'), control: makeSelect }).node,
      h('div', { class: 'field filters__state' },
        h('span', { class: 'field__label', 'aria-hidden': 'true', text: t('inventory.state') }), stock.node));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      changed(true);
    });
    results = h('div', { class: 'results' });
    mount(root, pageHead(t('inventory.title')), form, results);
    drawResults();
  }

  const onMedia = () => drawResults();
  media.addEventListener('change', onMedia);
  draw();
  load(true);

  return {
    redraw: draw,
    destroy() {
      window.clearTimeout(timer);
      media.removeEventListener('change', onMedia);
      if (controller) {
        controller.abort();
      }
    },
  };
}
