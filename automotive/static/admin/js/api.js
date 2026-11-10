// The dashboard's only door to the server. Every unsafe request carries the CSRF header;
// a 401 means the session ended and sends the browser to the sign-in page.

import { t } from './strings.js';

const LOGIN_URL = '/login?next=admin';
const ENDED_KEY = 'garage.staff.session-ended';

let csrf = '';
let user = null;

export class ApiError extends Error {
  constructor(status, code, fields) {
    super(code || `HTTP_${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || '';
    this.fields = fields && typeof fields === 'object' ? fields : {};
  }
}

export function setSession(session) {
  csrf = String(session.csrf || '');
  user = session.user || null;
}

export function setCsrf(value) {
  csrf = String(value || '');
}

export function getUser() {
  return user;
}

export function isAdmin() {
  return Boolean(user && user.role === 'admin');
}

export function isAbort(error) {
  return Boolean(error && error.name === 'AbortError');
}

/** The session is gone: leave for the sign-in page. The returned promise never settles,
 * so callers do not flash an error while the browser navigates. A note for the sign-in
 * page says why we arrived, so it shows its form instead of sending us straight back. */
function sessionEnded() {
  try {
    window.sessionStorage.setItem(ENDED_KEY, String(Date.now()));
  } catch {
    // Without storage the sign-in page simply checks the session itself.
  }
  window.location.assign(LOGIN_URL);
  return new Promise(() => {});
}

function detailOf(payload) {
  const detail = payload && typeof payload === 'object' ? payload.detail : null;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    return { code: String(detail.code || ''), fields: detail.fields };
  }
  // FastAPI's own validation errors arrive as a list.
  return { code: Array.isArray(detail) ? 'INVALID_REQUEST' : '', fields: null };
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function refreshCsrf() {
  const response = await fetch('/api/auth/session', {
    credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    return false;
  }
  const payload = await readJson(response);
  if (!payload || !payload.csrf) {
    return false;
  }
  setSession(payload);
  return true;
}

/**
 * options: body (sent as JSON), raw + type (sent as is), signal, query (object).
 * Resolves with the parsed JSON, or null for an empty response.
 */
export async function request(method, path, options = {}, retried = false) {
  const unsafe = method !== 'GET' && method !== 'HEAD';
  const headers = { Accept: 'application/json' };
  let body;
  if (unsafe) {
    headers['X-CSRF-Token'] = csrf;
    if (options.raw) {
      headers['Content-Type'] = options.type;
      body = options.raw;
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(options.body === undefined ? {} : options.body);
    }
  }
  let url = path;
  if (options.query) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== null && value !== undefined && value !== '') {
        search.set(key, String(value));
      }
    }
    const text = search.toString();
    url = text ? `${path}?${text}` : path;
  }
  let response;
  try {
    response = await fetch(url, {
      method, headers, body, credentials: 'same-origin', cache: 'no-store',
      signal: options.signal,
    });
  } catch (error) {
    if (isAbort(error)) {
      throw error;
    }
    throw new ApiError(0, 'NETWORK');
  }
  if (response.status === 204) {
    return null;
  }
  const payload = await readJson(response);
  if (response.ok) {
    return payload;
  }
  const { code, fields } = detailOf(payload);
  // A wrong current password is also a 401; it is an answer, not an ended session.
  if (response.status === 401 && code !== 'INVALID_CREDENTIALS') {
    return sessionEnded();
  }
  if (response.status === 403 && code === 'CSRF_REQUIRED' && unsafe && !retried) {
    // The session was rotated in another tab: fetch the current token and try once more.
    if (await refreshCsrf()) {
      return request(method, path, options, true);
    }
    return sessionEnded();
  }
  throw new ApiError(response.status, code, fields);
}

/** POST raw bytes with upload progress (fetch cannot report it). */
export function upload(path, file, type, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.responseType = 'text';
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('Content-Type', type);
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    });
    xhr.addEventListener('error', () => reject(new ApiError(0, 'NETWORK')));
    xhr.addEventListener('abort', () => reject(new ApiError(0, 'NETWORK')));
    xhr.addEventListener('load', () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
        return;
      }
      const { code, fields } = detailOf(payload);
      if (xhr.status === 401) {
        sessionEnded();
        return;
      }
      reject(new ApiError(xhr.status, code, fields));
    });
    xhr.send(file);
  });
}

/** A plain sentence for an error, in the interface language. */
export function describeError(error) {
  if (!(error instanceof ApiError)) {
    return t('error.unknown');
  }
  const byCode = {
    NETWORK: 'error.network',
    RATE_LIMITED: 'error.rate',
    TOO_MANY_ATTEMPTS: 'account.throttled',
    ADMIN_ONLY: 'error.forbidden',
    SITE_NOT_CONFIGURED: 'error.not_configured',
    SITE_AUTH_UNAVAILABLE: 'error.server',
    REQUEST_TOO_LARGE: 'error.too_large',
    NOT_FOUND: 'error.not_found',
    VERSION_CONFLICT: 'error.conflict',
    LAST_ADMIN: 'users.last_admin',
    INVALID_FIELDS: 'error.invalid',
    INVALID_REQUEST: 'error.invalid',
    INVALID_IMAGE: 'gallery.err_invalid',
  };
  if (byCode[error.code]) {
    return t(byCode[error.code]);
  }
  if (error.status === 429) {
    return t('error.rate');
  }
  if (error.status === 413) {
    return t('error.too_large');
  }
  if (error.status === 404) {
    return t('error.not_found');
  }
  if (error.status === 403) {
    return t('error.forbidden');
  }
  if (error.status >= 500) {
    return t('error.server');
  }
  return t('error.unknown');
}

export const api = {
  session: () => request('GET', '/api/auth/session'),
  logout: () => request('POST', '/api/auth/logout'),
  changePassword: (current, next) => request('POST', '/api/auth/password',
    { body: { current, new: next } }),

  overview: (signal) => request('GET', '/api/admin/overview', { signal }),
  inventory: (query, signal) => request('GET', '/api/admin/inventory', { query, signal }),
  sales: (days, signal) => request('GET', '/api/admin/reports/sales', { query: { days }, signal }),
  chat: (days, signal) => request('GET', '/api/admin/reports/chat', { query: { days }, signal }),
  audit: (before, signal) => request('GET', '/api/admin/audit', { query: { before }, signal }),

  content: (signal) => request('GET', '/api/admin/content', { signal }),
  saveSection: (key, version, document) => request('PUT',
    `/api/admin/content/${encodeURIComponent(key)}`, { body: { version, document } }),

  services: (signal) => request('GET', '/api/admin/services', { signal }),
  createService: (fields) => request('POST', '/api/admin/services', { body: fields }),
  updateService: (id, fields) => request('PUT', `/api/admin/services/${id}`, { body: fields }),
  deleteService: (id) => request('DELETE', `/api/admin/services/${id}`),
  orderServices: (ids) => request('POST', '/api/admin/services/order', { body: { ids } }),

  media: (signal) => request('GET', '/api/admin/media', { signal }),
  uploadMedia: (file, type, onProgress) => upload('/api/admin/media', file, type, onProgress),
  updateMedia: (id, fields) => request('PATCH', `/api/admin/media/${id}`, { body: fields }),
  deleteMedia: (id) => request('DELETE', `/api/admin/media/${id}`),
  orderMedia: (ids) => request('POST', '/api/admin/media/order', { body: { ids } }),

  users: (signal) => request('GET', '/api/admin/users', { signal }),
  createUser: (fields) => request('POST', '/api/admin/users', { body: fields }),
  updateUser: (id, fields) => request('PATCH', `/api/admin/users/${id}`, { body: fields }),
  setUserPassword: (id, password) => request('POST', `/api/admin/users/${id}/password`,
    { body: { password } }),
  revokeSessions: (id) => request('POST', `/api/admin/users/${id}/sessions/revoke`),
};
