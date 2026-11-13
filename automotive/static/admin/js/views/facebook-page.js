// Facebook > Page: the page's followers and views over 7, 30 or 90 days, and its most engaging
// posts with their pictures. The bot service fetches the figures (daily) and the posts (hourly)
// and caches the pictures, because this dashboard may only show same-origin images. When Meta
// refuses the statistics (it can ask for two-factor sign-in first) the tab says so and shows
// what it has.

import { isAbort } from '../api.js';
import {
  chartCard, chartEmpty, lineChart,
} from '../charts.js';
import { h, icon, mount, uid } from '../dom.js';
import { dateTime, dayLabel, dayRange, longDate, num } from '../format.js';
import { t } from '../strings.js';
import { card, emptyState, iconButton, loadingBlock, notice, skeleton } from '../ui.js';
import {
  activityApi, errorCard, excerpt, externalLink, jobButton, picture, rangeControl,
} from './facebook-activity.js';
import { label } from './facebook-shared.js';

const POSTS = 12;
let remembered = 30; // the range survives a visit to another tab

/** {day: value} of a [{day, <key>}] series. */
function byDay(rows, key) {
  const map = new Map();
  for (const row of rows || []) {
    const value = Number(row[key]);
    if (row.day && Number.isFinite(value)) {
      map.set(String(row.day), value);
    }
  }
  return map;
}

/** Every day from the first to the last one that has a figure. */
function spanOf(...maps) {
  const seen = [...new Set(maps.flatMap((map) => [...map.keys()]))].sort();
  return seen.length ? dayRange(seen[0], seen[seen.length - 1]) : [];
}

export function mountView(root) {
  const state = {
    days: remembered, status: 'loading', stats: null, posts: [], error: null,
    tableMode: new Set(),
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

  function tile(title, value, note) {
    return h('div', { class: 'tile' },
      h('p', { class: 'tile__label', text: title }),
      h('p', { class: 'tile__value', text: value === null || value === undefined ? '—' : value }),
      note ? h('p', { class: 'tile__note', text: note }) : null);
  }

  function tiles(summary) {
    const change = summary.followers_change;
    const changeText = change === null || change === undefined ? ''
      : t(change >= 0 ? 'fb.page.change_up' : 'fb.page.change_down',
        { count: num(Math.abs(change)), days: state.days });
    const total = (name) => {
      const value = summary.totals ? summary.totals[name] : null;
      return value === null || value === undefined ? null : num(value);
    };
    return h('div', { class: 'tiles tiles--admin' },
      tile(t('fb.page.followers'), summary.followers === null || summary.followers === undefined
        ? null : num(summary.followers), changeText),
      tile(t('fb.page.views'), total('page_media_view'),
        t('fb.page.in_days', { days: state.days })),
      tile(t('fb.page.engagements'), total('page_post_engagements'),
        t('fb.page.in_days', { days: state.days })),
      tile(t('fb.page.new_follows'), total('page_daily_follows_unique'),
        t('fb.page.in_days', { days: state.days })));
  }

  function notices(stats) {
    const found = [];
    const page = stats.status && stats.status.page;
    const posts = stats.status && stats.status.posts;
    if (!page && !posts) {
      found.push(notice('info', t('fb.page.nothing_yet')));
      return found;
    }
    const refused = (page && page.refused) || (posts && posts.refused);
    if (refused) {
      found.push(notice('warning', `${t('fb.page.refused')} ${t('fb.meta_code', {
        code: refused.subcode ? `${refused.code}/${refused.subcode}` : String(refused.code),
      })}`));
    }
    const missing = [...new Set([...(page && page.unavailable) || [],
      ...(posts && posts.unavailable) || []])];
    if (missing.length) {
      found.push(h('p', { class: 'page-note' }, icon('info', 16), h('span', {
        text: t('fb.page.unavailable', {
          names: missing.map((name) => label('fb.page.metric', name)).join(', '),
        }),
      })));
    }
    if (page && page.fetched_at) {
      found.push(h('p', { class: 'page-note' }, icon('clock', 16), h('span', {
        text: t('fb.page.fetched', { time: dateTime(page.fetched_at) }),
      })));
    }
    return found;
  }

  function followersChart(stats) {
    const counts = byDay(stats.followers, 'followers');
    const days = spanOf(counts);
    // A day without a count (the laptop was off) keeps the day before's.
    let last = null;
    const values = days.map((day) => {
      if (counts.has(day)) {
        last = counts.get(day);
      }
      return last === null ? 0 : last;
    });
    return chartCard({
      id: 'fb-followers', title: t('fb.page.followers_title'),
      subtitle: t('fb.page.followers_subtitle'), tableMode: state.tableMode,
      table: days.length ? {
        columns: [{ label: t('common.day') }, { label: t('fb.page.followers'), numeric: true }],
        rows: days.map((day, index) => [dayLabel(day, true), num(values[index])]),
      } : null,
      draw: (body) => {
        if (days.length < 2) {
          body.appendChild(chartEmpty(t('fb.page.collecting')));
          return null;
        }
        return lineChart(body, {
          days, label: t('fb.page.followers_title'), format: (value) => num(value),
          series: [{ key: 's1', name: t('fb.page.followers'), values }],
        });
      },
    });
  }

  function viewsChart(stats) {
    const metrics = stats.metrics || {};
    const views = byDay(metrics.page_media_view, 'value');
    const viewers = byDay(metrics.page_total_media_view_unique, 'value');
    const days = spanOf(views, viewers);
    const series = [
      { key: 's1', name: t('fb.page.views'), values: days.map((day) => views.get(day) || 0) },
    ];
    if (viewers.size) {
      series.push({ key: 's2', name: t('fb.page.viewers'),
        values: days.map((day) => viewers.get(day) || 0) });
    }
    return chartCard({
      id: 'fb-views', title: t('fb.page.views_title'), subtitle: t('fb.page.views_subtitle'),
      tableMode: state.tableMode,
      table: days.length ? {
        columns: [{ label: t('common.day') },
          ...series.map((item) => ({ label: item.name, numeric: true }))],
        rows: days.map((day, index) => [dayLabel(day, true),
          ...series.map((item) => num(item.values[index]))]),
      } : null,
      draw: (body) => {
        if (days.length < 2) {
          body.appendChild(chartEmpty(t('fb.page.collecting')));
          return null;
        }
        return lineChart(body, {
          days, label: t('fb.page.views_title'), format: (value) => num(value), series,
        });
      },
    });
  }

  function post(row) {
    const text = excerpt(row.message, 220);
    const figures = [
      [t('fb.page.reactions'), row.reactions], [t('fb.page.comments'), row.comments],
      [t('fb.page.shares'), row.shares], [t('fb.page.views'), row.views],
    ];
    return h('li', { class: 'photo' },
      picture(row.image_url, text ? t('fb.page.post_picture', { text: excerpt(text, 80) })
        : t('fb.page.post_picture_plain')),
      h('div', { class: 'photo__body' },
        h('div', { class: 'photo__top' },
          h('time', { class: 'photo__meta', datetime: row.created_time,
            text: longDate(row.created_time) })),
        h('p', { class: 'card__text', text: text || t('fb.page.no_text') }),
        h('dl', { class: 'facts' }, figures.map(([name, value]) => h('div', {},
          h('dt', { text: name }),
          h('dd', { text: value === null || value === undefined ? '—' : num(value) })))),
        h('div', { class: 'photo__actions' },
          externalLink(row.permalink, t('fb.act.open_facebook')))));
  }

  function section(title, ...children) {
    const id = uid('fbp');
    return h('section', { class: 'block', 'aria-labelledby': id },
      h('h2', { class: 'block__title', id, text: title }), children);
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
      }), refresh, jobButton('page_stats', t('fb.page.fetch_stats')),
    jobButton('posts', t('fb.page.fetch_posts')));
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
    const { stats } = state;
    const charts = [followersChart(stats), viewsChart(stats)];
    cards.push(...charts);
    mount(root, toolbar, notices(stats), tiles(stats.summary || {}),
      section(t('fb.page.trends'), h('div', { class: 'grid' }, charts.map((item) => item.node))),
      section(t('fb.page.top_posts'), state.posts.length
        ? h('ul', { class: 'photos', 'aria-label': t('fb.page.top_posts') },
          state.posts.map(post))
        : card({}, emptyState({ iconName: 'image', title: t('fb.page.no_posts'),
          text: t('fb.page.no_posts_text') }))));
  }

  async function load() {
    if (controller) {
      controller.abort();
    }
    controller = new AbortController();
    state.status = 'loading';
    state.error = null;
    render();
    try {
      const [stats, posts] = await Promise.all([
        activityApi.pageStats(state.days, controller.signal),
        activityApi.posts(state.days, POSTS, controller.signal),
      ]);
      state.stats = stats;
      state.posts = posts.posts || [];
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
