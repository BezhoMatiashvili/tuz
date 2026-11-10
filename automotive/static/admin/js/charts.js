// Hand-built charts: ranked bars, columns, lines and a two-part bar, each with hover and
// keyboard tooltips and a table twin. Marks are SVG; every label is text in a text colour.
// Series colours come from the chart tokens in admin.css (validated for both themes).

import { clear, h, icon, mount, s, uid } from './dom.js';
import { compact, dayLabel, num, percent } from './format.js';
import { t } from './strings.js';
import { announce } from './ui.js';

// -- tooltip ---------------------------------------------------------------------------

let tip = null;
let tipAnchor = null; // the focused mark the tooltip belongs to, if it was opened by focus

function place(x, y) {
  const box = tip.getBoundingClientRect();
  const width = document.documentElement.clientWidth;
  let left = Math.round(x - box.width / 2);
  left = Math.max(8, Math.min(left, width - box.width - 8));
  let top = Math.round(y - box.height - 10);
  if (top < 8) {
    top = Math.round(y + 18);
  }
  tip.style.transform = `translate(${left}px, ${top}px)`;
}

export function hideTip() {
  if (tip) {
    tip.hidden = true;
  }
  tipAnchor = null;
}

function ensureTip() {
  if (tip) {
    return tip;
  }
  tip = h('div', { class: 'tip', hidden: true, 'aria-hidden': 'true' });
  document.body.appendChild(tip);
  window.addEventListener('scroll', () => {
    if (tip.hidden) {
      return;
    }
    if (tipAnchor && document.activeElement === tipAnchor) {
      const box = tipAnchor.getBoundingClientRect();
      place(box.left + box.width / 2, box.top); // Focus scrolled the mark into view.
    } else {
      hideTip();
    }
  }, { passive: true, capture: true });
  document.addEventListener('pointerdown', (event) => {
    if (!(event.target instanceof Element) || !event.target.closest('[data-tip-host]')) {
      hideTip();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      hideTip();
    }
  });
  return tip;
}

/**
 * content: {title, rows: [{series, value, label}], note}. Values lead, labels follow.
 * (x, y) is the viewport point the tooltip sits above.
 */
function showTip(content, x, y, anchor = null) {
  const node = ensureTip();
  mount(node,
    content.title && h('p', { class: 'tip__title', text: content.title }),
    (content.rows || []).map((row) => h('p', { class: 'tip__row' },
      row.series && h('span', { class: `tip__key tip__key--${row.series}` }),
      h('strong', { class: 'tip__value', text: row.value }),
      row.label && h('span', { class: 'tip__label', text: row.label }))),
    content.note && h('p', { class: 'tip__note', text: content.note }));
  node.hidden = false;
  tipAnchor = anchor;
  place(x, y);
}

/**
 * Hover, touch and keyboard focus all show the same tooltip for a mark. Pass
 * `focusable: false` for marks that are not tab stops: Chrome treats an SVG element with
 * a focus listener as focusable, which would put stray stops in the tab order.
 */
function bindMark(node, content, anchorOf = () => node, focusable = true) {
  const fromPointer = (event) => {
    const box = anchorOf().getBoundingClientRect();
    showTip(content(), event.clientX, box.top);
  };
  node.addEventListener('pointerenter', fromPointer);
  node.addEventListener('pointermove', fromPointer);
  node.addEventListener('pointerleave', (event) => {
    if (event.pointerType !== 'touch') {
      hideTip(); // After a tap the tooltip stays until the next tap elsewhere.
    }
  });
  if (!focusable) {
    return;
  }
  node.addEventListener('focus', () => {
    const box = anchorOf().getBoundingClientRect();
    showTip(content(), box.left + box.width / 2, box.top, node);
  });
  node.addEventListener('blur', hideTip);
}

/** One tab stop per chart; arrow keys, Home and End move between its marks. */
function roving(items, horizontal) {
  const back = horizontal ? 'ArrowLeft' : 'ArrowUp';
  const forward = horizontal ? 'ArrowRight' : 'ArrowDown';
  items.forEach((item, index) => {
    item.setAttribute('tabindex', index === 0 ? '0' : '-1');
    item.addEventListener('keydown', (event) => {
      let target = null;
      if (event.key === forward) {
        target = Math.min(index + 1, items.length - 1);
      } else if (event.key === back) {
        target = Math.max(index - 1, 0);
      } else if (event.key === 'Home') {
        target = 0;
      } else if (event.key === 'End') {
        target = items.length - 1;
      }
      if (target === null) {
        return;
      }
      event.preventDefault();
      if (target !== index) {
        item.setAttribute('tabindex', '-1');
        items[target].setAttribute('tabindex', '0');
        items[target].focus();
      }
    });
  });
}

/** Redraw when the container's width changes. Returns a function that stops observing. */
function onResize(container, draw) {
  let width = -1;
  let frame = 0;
  const run = () => {
    frame = 0;
    if (!container.isConnected) {
      return;
    }
    const next = Math.floor(container.clientWidth);
    if (next > 0 && next !== width) {
      width = next;
      draw(next);
    }
  };
  const observer = new ResizeObserver(() => {
    if (!frame) {
      frame = window.requestAnimationFrame(run);
    }
  });
  observer.observe(container);
  run();
  return () => {
    observer.disconnect();
    if (frame) {
      window.cancelAnimationFrame(frame);
    }
  };
}

// -- table twin and card ---------------------------------------------------------------

/** table: {columns: [{label, numeric}], rows: [[cell, ...]], foot: [cell, ...]}. */
function dataTable(table, caption) {
  const cell = (tag, text, column, extra) => h(tag, {
    class: column && column.numeric ? 'num' : null, ...extra, text: text === null ? '' : text,
  });
  return h('div', { class: 'table-wrap' },
    h('table', { class: 'table' },
      caption && h('caption', { class: 'visually-hidden', text: caption }),
      h('thead', {}, h('tr', {},
        table.columns.map((column) => cell('th', column.label, column, { scope: 'col' })))),
      h('tbody', {}, table.rows.map((row) => h('tr', {},
        row.map((value, index) => cell(index === 0 ? 'th' : 'td', value, table.columns[index],
          index === 0 ? { scope: 'row' } : {}))))),
      table.foot && h('tfoot', {}, h('tr', {},
        table.foot.map((value, index) => cell(index === 0 ? 'th' : 'td', value,
          table.columns[index], index === 0 ? { scope: 'row' } : {}))))));
}

export function chartEmpty(message, title, extra) {
  return h('div', { class: 'chart__empty' },
    h('span', { class: 'chart__empty-icon' }, icon('clock', 24)),
    title && h('p', { class: 'chart__empty-title', text: title }),
    h('p', { class: 'chart__empty-text', text: message }),
    extra && h('p', { class: 'chart__empty-text', text: extra }));
}

/**
 * A chart with a visible title, a one-line subtitle saying what it counts, and a table
 * twin. `draw(body)` builds the chart and may return a cleanup function. `tableMode` is a
 * Set owned by the view, so the choice survives a redraw. Returns {node, dispose}.
 */
export function chartCard({ id, title, subtitle, draw, table, tableMode, wide, note }) {
  const body = h('div', { class: 'chart__body' });
  let cleanup = null;
  let toggle = null;
  const render = () => {
    if (cleanup) {
      cleanup();
      cleanup = null;
    }
    hideTip();
    const showTable = Boolean(table) && tableMode.has(id);
    if (showTable) {
      mount(body, dataTable(table, t('common.table_of', { title })));
    } else {
      clear(body);
      cleanup = draw(body) || null;
    }
    if (toggle) {
      mount(toggle, icon(showTable ? 'chart' : 'table', 18),
        h('span', { class: 'btn__label', text: t(showTable ? 'common.show_chart' : 'common.show_table') }));
    }
  };
  if (table) {
    toggle = h('button', { type: 'button', class: 'btn btn--ghost btn--small chart__toggle' });
    toggle.addEventListener('click', () => {
      if (tableMode.has(id)) {
        tableMode.delete(id);
      } else {
        tableMode.add(id);
      }
      render();
    });
  }
  const titleId = uid('chart');
  const node = h('figure', {
    class: ['card', 'chart', wide && 'chart--wide'], 'aria-labelledby': titleId,
    dataset: { chart: id },
  },
  h('figcaption', { class: 'chart__head' },
    h('div', { class: 'chart__titles' },
      h('h3', { class: 'chart__title', id: titleId, text: title }),
      subtitle && h('p', { class: 'chart__subtitle', text: subtitle })),
    toggle),
  body,
  note && h('p', { class: 'chart__note', text: note }));
  render();
  return {
    node,
    dispose() {
      if (cleanup) {
        cleanup();
        cleanup = null;
      }
    },
  };
}

// -- ranked horizontal bars ------------------------------------------------------------

/**
 * rows: [{label, value, other}] already ranked, "Other" last. One hue for every bar; the
 * remainder row is drawn in the neutral tone. `total` (optional) adds a share to the tooltip.
 */
export function rankedBars(container, { rows, total, unit }) {
  const max = Math.max(1, ...rows.map((row) => row.value));
  const items = rows.map((row) => {
    const fraction = Math.max(0, row.value) / max;
    const bar = s('svg', { class: 'bars__bar', height: 12, 'aria-hidden': 'true', focusable: 'false' },
      s('rect', { class: 'bars__fill', width: '100%', height: '100%', rx: 4 }),
      s('rect', { class: 'bars__fill', width: '50%', height: '100%' }));
    // Bar length is a share of the track minus the room kept for the value at the tip.
    bar.style.width = row.value > 0 ? `max(3px, calc((100% - 4.75em) * ${fraction.toFixed(4)}))` : '0px';
    const value = num(row.value);
    const share = total ? percent(row.value / total) : '';
    const spoken = share ? `${row.label}: ${unit(value)}, ${share}` : `${row.label}: ${unit(value)}`;
    const item = h('li', {
      class: ['bars__row', row.other && 'bars__row--other'], 'aria-label': spoken,
      dataset: { tipHost: '' },
    },
    h('span', { class: 'bars__label', text: row.label }),
    h('span', { class: 'bars__track' }, bar, h('span', { class: 'bars__value', text: value })));
    bindMark(item, () => ({
      title: row.label,
      rows: [{ value: unit(value) }],
      note: share ? t('tip.share', { percent: share }) : '',
    }), () => bar);
    return item;
  });
  roving(items, false);
  container.appendChild(h('ol', { class: 'bars' }, items));
  return null;
}

// -- columns ---------------------------------------------------------------------------

function columnPath(x, y, width, base) {
  const radius = Math.max(0, Math.min(4, width / 2, base - y));
  return `M${x},${base}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + width - radius}`
    + `Q${x + width},${y} ${x + width},${y + radius}V${base}Z`;
}

/**
 * bins: [{tick, label, value}] where `tick` is the lower edge printed under the column's
 * left side and `label` is the full range for the tooltip and table. One hue.
 */
export function columnChart(container, { bins, total, unit, axisLabel, label }) {
  const host = h('div', { class: 'plot', dataset: { tipHost: '' } });
  container.appendChild(host);
  const top = 24;
  const bottom = axisLabel ? 46 : 30;
  const max = Math.max(1, ...bins.map((bin) => bin.value));
  const draw = (width) => {
    const height = Math.round(Math.max(210, Math.min(320, width * 0.72)));
    const side = 10;
    const band = (width - side * 2) / bins.length;
    const thick = Math.max(6, Math.min(24, band * 0.6));
    const base = height - bottom;
    const svg = s('svg', {
      class: 'plot__svg', width, height, viewBox: `0 0 ${width} ${height}`, role: 'group',
      'aria-label': label,
    });
    svg.appendChild(s('line', { class: 'plot__axis', x1: side, x2: width - side, y1: base + 0.5, y2: base + 0.5 }));
    if (axisLabel) {
      svg.appendChild(s('text', { class: 'plot__label', x: width / 2, y: height - 6, 'text-anchor': 'middle' }, axisLabel));
    }
    const hits = [];
    bins.forEach((bin, index) => {
      const left = side + band * index;
      const x = left + (band - thick) / 2;
      const y = bin.value > 0 ? Math.min(base - 2, top + (base - top) * (1 - bin.value / max)) : base;
      const group = s('g', { class: 'plot__col' });
      if (bin.value > 0) {
        group.appendChild(s('path', { class: 'plot__mark s1', d: columnPath(x, y, thick, base) }));
      }
      group.appendChild(s('text', {
        class: 'plot__value', x: left + band / 2, y: y - 7, 'text-anchor': 'middle',
      }, num(bin.value)));
      svg.appendChild(s('line', { class: 'plot__tick', x1: left + 0.5, x2: left + 0.5, y1: base, y2: base + 4 }));
      svg.appendChild(s('text', {
        class: 'plot__label', x: left, y: base + 18, 'text-anchor': index === 0 ? 'start' : 'middle',
      }, bin.tick));
      const share = total ? percent(bin.value / total) : '';
      const count = unit(num(bin.value));
      const hit = s('rect', {
        class: 'plot__hit', x: left, y: 0, width: band, height: base, role: 'img',
        'aria-label': share ? `${bin.label}: ${count}, ${share}` : `${bin.label}: ${count}`,
      });
      const ring = s('rect', {
        class: 'plot__ring', x: left + 2, y: 2, width: Math.max(0, band - 4), height: base - 2, rx: 4,
      });
      group.append(hit, ring);
      bindMark(hit, () => ({
        title: bin.label,
        rows: [{ value: count }],
        note: share ? t('tip.share', { percent: share }) : '',
      }), () => (group.querySelector('.plot__mark') || hit));
      hits.push(hit);
      svg.appendChild(group);
    });
    roving(hits, true);
    mount(host, svg);
  };
  return onResize(host, draw);
}

// -- lines over days -------------------------------------------------------------------

function niceStep(max, count) {
  const raw = Math.max(max, 1) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const scaled = raw / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
  return Math.max(1, step * magnitude);
}

function textWidth(svg, cls, text) {
  const probe = s('text', { class: cls, x: 0, y: 0 }, text);
  svg.appendChild(probe);
  const width = probe.getComputedTextLength();
  probe.remove();
  return width;
}

/**
 * days: ['2026-11-01', ...]; series: [{key: 's1' | 's2', name, values: [...]}].
 * One y axis for every series. Two or more series get a legend; each line carries its
 * last value at its end. `format` turns a value into tooltip text.
 */
export function lineChart(container, { days, series, format, area = false, small = false, label }) {
  const legend = series.length > 1 ? h('ul', { class: 'legend' },
    series.map((item) => h('li', { class: 'legend__item' },
      h('span', { class: `legend__key legend__key--${item.key}` }), item.name))) : null;
  const host = h('div', { class: 'plot', dataset: { tipHost: '' } });
  if (legend) {
    container.appendChild(legend);
  }
  container.appendChild(host);
  const height = small ? 176 : 232;
  const top = 12;
  const bottom = 28;
  const max = Math.max(1, ...series.flatMap((item) => item.values));
  const step = niceStep(max, 3);
  const ceiling = Math.ceil(max / step) * step;
  const tickText = (value) => (ceiling >= 10000 ? compact(value) : num(value));
  const ticks = [];
  for (let value = 0; value <= ceiling + step / 2; value += step) {
    ticks.push(value);
  }
  const last = days.length - 1;

  const draw = (width) => {
    const svg = s('svg', {
      class: 'plot__svg', width, height, viewBox: `0 0 ${width} ${height}`, tabindex: '0', role: 'img',
      'aria-label': label,
    });
    mount(host, svg);
    const left = Math.ceil(Math.max(...ticks.map((value) => textWidth(svg, 'plot__label', tickText(value))))) + 10;
    const ends = series.map((item) => compact(item.values[last]));
    const right = Math.ceil(Math.max(...ends.map((text) => textWidth(svg, 'plot__value', text)))) + 16;
    const base = height - bottom;
    const plotWidth = Math.max(40, width - left - right);
    const xOf = (index) => left + (last === 0 ? plotWidth / 2 : (plotWidth * index) / last);
    const yOf = (value) => top + (base - top) * (1 - value / ceiling);

    for (const value of ticks) {
      const y = Math.round(yOf(value)) + 0.5;
      svg.appendChild(s('line', {
        class: value === 0 ? 'plot__axis' : 'plot__grid', x1: left, x2: left + plotWidth, y1: y, y2: y,
      }));
      svg.appendChild(s('text', { class: 'plot__label', x: left - 8, y: y + 4, 'text-anchor': 'end' },
        tickText(value)));
    }
    // Day labels: as many as fit, counted back from the latest day so it is always named.
    const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotWidth / 68))));
    days.forEach((day, index) => {
      if ((last - index) % every !== 0) {
        return;
      }
      const anchor = index === last && last > 0 ? 'end' : index === 0 && last > 0 ? 'start' : 'middle';
      const x = index === last && last > 0 ? xOf(index) + 4 : xOf(index);
      svg.appendChild(s('text', { class: 'plot__label', x, y: base + 19, 'text-anchor': anchor }, dayLabel(day)));
    });

    const cross = s('line', { class: 'plot__cross', y1: top, y2: base, x1: 0, x2: 0, visibility: 'hidden' });
    svg.appendChild(cross);

    // End labels sit beside the last point; close neighbours are nudged apart, never stacked.
    const endY = series.map((item) => yOf(item.values[last]));
    const order = endY.map((y, index) => index).sort((a, b) => endY[a] - endY[b]);
    const labelY = [...endY];
    for (let i = 1; i < order.length; i += 1) {
      const above = order[i - 1];
      const below = order[i];
      if (labelY[below] - labelY[above] < 14) {
        const middle = (labelY[below] + labelY[above]) / 2;
        labelY[above] = middle - 7;
        labelY[below] = middle + 7;
      }
    }

    const dots = [];
    series.forEach((item, index) => {
      const points = item.values.map((value, at) => `${xOf(at).toFixed(1)},${yOf(value).toFixed(1)}`);
      if (area) {
        svg.appendChild(s('path', {
          class: `plot__area ${item.key}`,
          d: `M${xOf(0).toFixed(1)},${base}L${points.join('L')}L${xOf(last).toFixed(1)},${base}Z`,
        }));
      }
      svg.appendChild(s('path', { class: `plot__line ${item.key}`, d: `M${points.join('L')}` }));
      svg.appendChild(s('circle', {
        class: `plot__dot ${item.key}`, cx: xOf(last), cy: yOf(item.values[last]), r: 4,
      }));
      svg.appendChild(s('text', {
        class: 'plot__value', x: xOf(last) + 9, y: Math.max(top + 4, Math.min(base, labelY[index])) + 4,
      }, ends[index]));
      const dot = s('circle', { class: `plot__dot plot__dot--hover ${item.key}`, r: 4, visibility: 'hidden' });
      svg.appendChild(dot);
      dots.push(dot);
    });

    let active = -1;
    const content = (index) => ({
      title: dayLabel(days[index], true),
      rows: series.map((item) => ({
        series: item.key, value: format(item.values[index]), label: series.length > 1 ? item.name : '',
      })),
    });
    const focusAt = (index, viaKeyboard) => {
      active = Math.max(0, Math.min(last, index));
      const x = xOf(active);
      cross.setAttribute('x1', x);
      cross.setAttribute('x2', x);
      cross.setAttribute('visibility', 'visible');
      let highest = base;
      series.forEach((item, at) => {
        const y = yOf(item.values[active]);
        highest = Math.min(highest, y);
        dots[at].setAttribute('cx', x);
        dots[at].setAttribute('cy', y);
        dots[at].setAttribute('visibility', 'visible');
      });
      const box = svg.getBoundingClientRect();
      const tipContent = content(active);
      showTip(tipContent, box.left + x, box.top + highest - 4, viaKeyboard ? svg : null);
      if (viaKeyboard) {
        announce(`${tipContent.title}: ${tipContent.rows.map((row) => (
          row.label ? `${row.label} ${row.value}` : row.value)).join(', ')}`);
      }
    };
    const blur = () => {
      active = -1;
      cross.setAttribute('visibility', 'hidden');
      dots.forEach((dot) => dot.setAttribute('visibility', 'hidden'));
      hideTip();
    };
    const nearest = (event) => {
      const box = svg.getBoundingClientRect();
      const ratio = (event.clientX - box.left - left) / plotWidth;
      return Math.round(Math.max(0, Math.min(1, ratio)) * last);
    };
    // The crosshair finds the day: the pointer only has to be nearest, never on a line.
    svg.addEventListener('pointermove', (event) => focusAt(nearest(event), false));
    svg.addEventListener('pointerdown', (event) => focusAt(nearest(event), false));
    svg.addEventListener('pointerleave', (event) => {
      if (event.pointerType !== 'touch' && document.activeElement !== svg) {
        blur();
      }
    });
    svg.addEventListener('focus', () => {
      if (svg.matches(':focus-visible')) {
        focusAt(active < 0 ? last : active, true);
      }
    });
    svg.addEventListener('blur', blur);
    svg.addEventListener('keydown', (event) => {
      const from = active < 0 ? last : active;
      const moves = { ArrowLeft: from - 1, ArrowRight: from + 1, Home: 0, End: last };
      if (Object.prototype.hasOwnProperty.call(moves, event.key)) {
        event.preventDefault();
        focusAt(moves[event.key], true);
      }
    });
  };
  return onResize(host, draw);
}

// -- two-part bar ----------------------------------------------------------------------

/** A thin bar split into parts; the figures beside it carry the numbers. */
export function splitBar(parts, spoken, unit) {
  const total = parts.reduce((sum, part) => sum + Math.max(0, part.value), 0);
  const node = h('div', {
    class: 'split', role: 'img', 'aria-label': spoken, dataset: { tipHost: '' },
  });
  for (const part of parts) {
    if (!(part.value > 0)) {
      continue; // A part at zero draws nothing.
    }
    const piece = s('svg', { class: 'split__part', height: 8, 'aria-hidden': 'true', focusable: 'false' },
      s('rect', { class: `split__fill ${part.key}`, width: '100%', height: '100%' }));
    piece.style.flexGrow = String(part.value);
    bindMark(piece, () => ({
      title: part.label,
      rows: [{ series: part.key, value: unit(num(part.value, 1)) }],
      note: total ? t('tip.share', { percent: percent(part.value / total) }) : '',
    }), () => piece, false);
    node.appendChild(piece);
  }
  return node;
}

/** A thin inline bar for a table cell: how this row compares with the largest row. */
export function inlineBar(fraction) {
  const bar = s('svg', { class: 'inline-bar', height: 4, 'aria-hidden': 'true', focusable: 'false' },
    s('rect', { class: 'inline-bar__fill s1', width: '100%', height: '100%', rx: 2 }));
  bar.style.width = `${Math.max(2, Math.round(Math.max(0, Math.min(1, fraction)) * 100))}%`;
  return h('span', { class: 'inline-bar__track' }, bar);
}
