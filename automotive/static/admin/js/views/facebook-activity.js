// Facebook > Comments, Page and Ads: the calls to their routes and what the three tabs share.
// The server answers with codes; the sentences live in strings.js (fb.act.*, fb.comments.*,
// fb.page.*, fb.ads.*). Money is shown in each ad's own currency, never converted or summed
// across currencies.

import { ApiError, request } from '../api.js';
import { h, icon } from '../dom.js';
import { money, num } from '../format.js';
import { t } from '../strings.js';
import {
  adminOnly, busy, button, card, errorState, isForbidden, notice, segmented, toast,
} from '../ui.js';
import { describe, fbApi, restartCard, restartNeeded } from './facebook-shared.js';

const BASE = '/api/admin/facebook';
const id = (value) => encodeURIComponent(String(value));

/** The day ranges the Page and Ads tabs offer; the server accepts only these. */
export const RANGES = [7, 30, 90];

export const activityApi = {
  events: (limit, signal) => request('GET', `${BASE}/events`, { query: { limit }, signal }),
  comments: (query, signal) => request('GET', `${BASE}/comments`, { query, signal }),
  hideComment: (commentId, hidden) => request('POST',
    `${BASE}/comments/${id(commentId)}/hide`, { body: { hidden } }),
  replyToComment: (commentId, text) => request('POST',
    `${BASE}/comments/${id(commentId)}/reply`, { body: { text } }),
  pageStats: (days, signal) => request('GET', `${BASE}/page-stats`, { query: { days }, signal }),
  posts: (days, limit, signal) => request('GET', `${BASE}/posts`,
    { query: { days, limit }, signal }),
  ads: (days, signal) => request('GET', `${BASE}/ads`, { query: { days }, signal }),
  adsDaily: (days, signal) => request('GET', `${BASE}/ads/daily`, { query: { days }, signal }),
};

// Errors of the comment actions that the shared describe() does not know.
const ACTION_CODES = {
  META_REFUSED: 'fb.act.err.meta_refused',
  TOKEN_DEAD: 'fb.act.err.token_dead',
  NETWORK: 'fb.act.err.network',
};

/** A plain sentence for a failed comment action. When Meta's answer was lost the change may
 * already be on Facebook, and the sentence says to look there before trying again. */
export function describeAction(error) {
  if (!(error instanceof ApiError)) {
    return describe(error);
  }
  if (error.fields && error.fields.maybe_applied === true) {
    return t('fb.act.err.maybe_applied');
  }
  // status 0 is the browser's own network failure (api.js), not Meta's.
  if (!ACTION_CODES[error.code] || error.status === 0) {
    return describe(error);
  }
  let text = t(ACTION_CODES[error.code]);
  const meta = error.fields && typeof error.fields.meta === 'object' ? error.fields.meta : null;
  if (meta && meta.code !== null && meta.code !== undefined) {
    const code = meta.subcode ? `${meta.code}/${meta.subcode}` : String(meta.code);
    text = `${text} ${t('fb.meta_code', { code })}`;
  }
  return text;
}

/** The card a tab shows when its data could not be loaded. */
export function errorCard(error, retry) {
  if (isForbidden(error)) {
    return card({}, adminOnly());
  }
  if (restartNeeded(error)) {
    return restartCard();
  }
  if (error instanceof ApiError && error.code === 'META_NOT_CONFIGURED') {
    return card({}, notice('warning', t('fb.not_configured')));
  }
  return card({}, errorState(error, retry));
}

/** 7, 30 or 90 days, as a segmented control. */
export function rangeControl(value, onChange) {
  return segmented({
    label: t('fb.range.label'),
    options: RANGES.map((days) => ({ value: String(days), label: t('fb.range.days', { days }) })),
    value: String(value),
    onChange: (next) => onChange(Number(next)),
  }).node;
}

/** An amount in the ad account's own currency: lari as everywhere else, others by code. */
export function adMoney(value, currency) {
  if (value === null || value === undefined) {
    return '';
  }
  if (!currency || currency === 'GEL') {
    return money(value, 2);
  }
  return `${num(value, 2)} ${currency}`;
}

/** Ask the bot service to run one of its sync jobs now (it reads Facebook, not this page).
 * `text` names what is fetched when a tab offers more than one such button. */
export function jobButton(job, text) {
  const node = button(text || t('fb.act.fetch'), {
    iconName: 'refresh', small: true,
    'aria-label': t('fb.jobs.run_label', { job: t(`fb.job.${job}`) }),
  });
  node.addEventListener('click', async () => {
    const restore = busy(node, t('fb.act.fetching'));
    try {
      await fbApi.runJob(job);
      toast(t('fb.act.fetch_queued', { job: t(`fb.job.${job}`) }), 'info');
    } catch (error) {
      toast(describe(error), 'critical');
    } finally {
      restore();
    }
  });
  return node;
}

/** A link that opens Facebook or Ads Manager in a new tab; nothing for a non-https address. */
export function externalLink(url, labelText, small = true) {
  if (typeof url !== 'string' || !url.startsWith('https://')) {
    return null;
  }
  return h('a', {
    class: ['btn', 'btn--ghost', small && 'btn--small'], href: url, target: '_blank',
    rel: 'noopener noreferrer',
  }, icon('external', 16), h('span', { class: 'btn__label', text: labelText }),
  h('span', { class: 'visually-hidden', text: `(${t('nav.new_tab')})` }));
}

/** A cached picture, or a quiet placeholder when there is none. */
export function picture(url, alt) {
  return h('div', { class: 'photo__frame' }, url
    ? h('img', { class: 'photo__image', src: url, alt: alt || '', loading: 'lazy',
      decoding: 'async' })
    : h('span', { class: 'photo__badge' }, icon('image', 16),
      h('span', { text: t('fb.act.no_picture') })));
}

/** A short excerpt of a longer text, cut at a word. */
export function excerpt(text, limit = 180) {
  const words = String(text || '').replace(/\s+/g, ' ').trim();
  if (words.length <= limit) {
    return words;
  }
  const cut = words.slice(0, limit);
  const space = cut.lastIndexOf(' ');
  return `${(space > limit * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}
