// Facebook section: the calls to /api/admin/facebook and what its tabs share. The server
// answers with codes; the sentences for them live here and in strings.js.

import { ApiError, describeError, request } from '../api.js';
import { h, icon } from '../dom.js';
import { getLang, has, t } from '../strings.js';
import { card, notice } from '../ui.js';

const BASE = '/api/admin/facebook';
const id = (value) => encodeURIComponent(String(value));

export const fbApi = {
  status: (signal) => request('GET', `${BASE}/status`, { signal }),
  saveSettings: (fields) => request('PUT', `${BASE}/settings`, { body: fields }),
  resume: () => request('POST', `${BASE}/resume`),
  connect: (userToken, pageId) => request('POST', `${BASE}/connect`, {
    body: pageId ? { user_token: userToken, page_id: pageId } : { user_token: userToken },
  }),
  check: () => request('POST', `${BASE}/check`),
  subscribe: () => request('POST', `${BASE}/subscribe`),
  probe: () => request('POST', `${BASE}/probe`),
  disconnect: () => request('POST', `${BASE}/disconnect`, { body: { confirm: true } }),
  runJob: (job) => request('POST', `${BASE}/jobs/${id(job)}`),
  threads: (query, signal) => request('GET', `${BASE}/threads`, { query, signal }),
  thread: (psid, query, signal) => request('GET', `${BASE}/threads/${id(psid)}`, { query, signal }),
  saveThread: (psid, fields) => request('PUT', `${BASE}/threads/${id(psid)}`, { body: fields }),
  deleteThread: (psid) => request('DELETE', `${BASE}/threads/${id(psid)}`,
    { body: { confirm: true } }),
  // A tester's chat only: the bot forgets it and reads nothing written before.
  clearContext: (psid, version) => request('POST', `${BASE}/threads/${id(psid)}/clear-context`,
    { body: { confirm: true, version } }),
};

// The section's own error codes; everything else is described by api.js.
const CODES = {
  META_NOT_CONFIGURED: 'fb.err.not_configured',
  AUTO_REPLY_NOT_CONFIRMED: 'fb.err.auto_reply',
  NOT_CONNECTED: 'fb.err.not_connected',
  TOKEN_UNREADABLE: 'fb.err.token_unreadable',
  CONFIRM_REQUIRED: 'fb.err.confirm',
  NOT_TESTER: 'fb.err.not_tester',
  SUBSCRIBE_FAILED: 'fb.err.subscribe_failed',
  CONNECT_FAILED: 'fb.err.connect_failed',
  TOKEN_INVALID: 'fb.err.token_invalid',
  WRONG_APP: 'fb.err.wrong_app',
  NOT_USER_TOKEN: 'fb.err.not_user_token',
  MISSING_SCOPES: 'fb.err.missing_scopes',
  EXCHANGE_FAILED: 'fb.err.exchange_failed',
  NO_PAGES: 'fb.err.no_pages',
  MULTIPLE_PAGES: 'fb.err.multiple_pages',
  PAGE_TOKEN_INVALID: 'fb.err.page_token_invalid',
};

/** A plain sentence for an error, with Meta's own code when Meta refused something. */
export function describe(error) {
  if (!(error instanceof ApiError) || !CODES[error.code]) {
    return describeError(error);
  }
  const fields = error.fields || {};
  let text = t(CODES[error.code], { names: (fields.missing || []).join(', ') });
  const meta = fields.meta && typeof fields.meta === 'object' ? fields.meta : null;
  if (meta && meta.code !== null && meta.code !== undefined) {
    const code = meta.subcode ? `${meta.code}/${meta.subcode}` : String(meta.code);
    text = `${text} ${t('fb.meta_code', { code })}`;
  }
  return text;
}

/** The old server answers an unknown /api/admin path with a bare 404: not restarted yet. */
export function restartNeeded(error) {
  return error instanceof ApiError && error.status === 404 && !error.code;
}

/** A string-table label for a code from the server, or the code itself when there is none. */
export function label(prefix, code) {
  const key = `${prefix}.${code}`;
  return has(key) ? t(key) : String(code || '');
}

export function maskPsid(psid) {
  return psid ? `…${String(psid).slice(-4)}` : '';
}

export function personName(thread) {
  return thread.customer_name || t('fb.chats.unnamed', { id: maskPsid(thread.psid) });
}

/** US dollars with two decimals: 0,06 $ in Georgian, $0.06 in English. */
export function usd(value) {
  const number = Number(value);
  const text = (Number.isFinite(number) ? number : 0).toFixed(2);
  return getLang() === 'ka' ? `${text.replace('.', ',')} $` : `$${text}`;
}

/** What to show while the Meta block of the server's .env is incomplete. */
export function notConfigured(data) {
  const missing = data.missing || [];
  const invalid = data.invalid || [];
  return card({ class: 'stack' },
    notice('warning', t('fb.not_configured')),
    missing.length ? h('p', { class: 'card__text', text: t('fb.not_configured_missing',
      { names: missing.join(', ') }) }) : null,
    invalid.length ? h('p', { class: 'card__text', text: t('fb.not_configured_invalid',
      { names: invalid.join(', ') }) }) : null,
    h('p', { class: 'page-note' }, icon('info', 16),
      h('span', { text: t('fb.not_configured_next') })));
}

/** The tab's own notice for a server that still runs the code from before this section. */
export function restartCard() {
  return card({}, notice('warning', t('fb.restart_needed')));
}

/** Text with its line breaks kept, as text nodes and <br> elements (never markup). */
export function lines(text) {
  const parts = [];
  String(text || '').split(/\r\n|\r|\n/).forEach((line, index) => {
    if (index) {
      parts.push(h('br', {}));
    }
    parts.push(line);
  });
  return parts;
}
