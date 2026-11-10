/**
 * Shared helpers for the landing page modules: the page data block, interface strings,
 * element building and the JSON API call. Every server string goes through textContent.
 */

const block = document.getElementById('site-data');

/** What the server embedded for the scripts: language, currency, stock count, chat, strings. */
export const data = block ? JSON.parse(block.textContent) : {};

const strings = data.strings || {};
const SVG = 'http://www.w3.org/2000/svg';
const sprite = (document.querySelector('svg.icon use')?.getAttribute('href') || '').split('#')[0];

/**
 * An interface string. `fallback` names an older string to use when this one was added
 * after the running server last loaded its string table.
 */
export function text(key, fallback) {
  return strings[key] || (fallback ? strings[fallback] : '') || '';
}

/** `{name}` placeholders filled from `values`. */
export function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, (_, name) => (name in values ? String(values[name]) : ''));
}

/**
 * The site's copy uses no em or en dashes. Text that comes from elsewhere (assistant
 * replies, catalogue names) is brought in line before it is shown.
 */
export function tidy(value) {
  return String(value ?? '')
    .replace(/\s+[\u2013\u2014]\s+/g, ', ')
    .replace(/[\u2013\u2014]/g, '-');
}

/** 12345 as `12 345`, with a non-breaking space, like the server's `num` filter. */
export function number(value) {
  return String(Math.trunc(value)).replace(/\B(?=(\d{3})+(?!\d))/g, '\u00A0');
}

/** A price with the shop's currency sign, or the "price on request" label. */
export function price(value) {
  if (typeof value !== 'number' || !(value > 0)) return text('search.price_request');
  const whole = Math.trunc(value);
  const cents = Math.round((value - whole) * 100);
  const amount = number(whole) + (cents ? '.' + String(cents).padStart(2, '0') : '');
  const sign = data.currency || '\u20BE';
  // Georgian writes the amount first (25 ₾), English the sign (₾25), as the dashboard does.
  return data.lang === 'ka' ? `${amount}\u00A0${sign}` : sign + amount;
}

export function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) node.textContent = content;
  return node;
}

/** A Phosphor icon from the same sprite the templates use. */
export function icon(name) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('width', '24');
  svg.setAttribute('height', '24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', `${sprite}#${name}`);
  svg.append(use);
  return svg;
}

/**
 * Call the site API. Cookies are never sent. A failure throws an Error carrying
 * `status` and the server's `code` (for example RATE_LIMITED).
 */
export async function api(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(path, {
    method,
    signal,
    credentials: 'omit',
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // An empty body (204) or a proxy's error page: there is nothing to read.
  }
  if (!response.ok) {
    const detail = payload && payload.detail;
    const code = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.code : '';
    const error = new Error(code || `HTTP_${response.status}`);
    error.status = response.status;
    error.code = code || '';
    throw error;
  }
  return payload;
}

export const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
