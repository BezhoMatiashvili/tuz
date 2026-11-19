// Sign-in page. The form posts with fetch (a classic form post is refused by the server's
// Origin check) and then follows the landing path the server names.

import { getLang, otherLang, setLang, t } from './strings.js';

const LANDINGS = { admin: '/admin', staff: '/staff' };
const ENDED_KEY = 'garage.staff.session-ended';
const ERROR_KEYS = {
  INVALID_CREDENTIALS: 'invalid',
  TOO_MANY_ATTEMPTS: 'throttled',
  RATE_LIMITED: 'throttled',
  SITE_AUTH_NOT_CONFIGURED: 'not_configured',
  SITE_NOT_CONFIGURED: 'not_configured',
  SITE_AUTH_UNAVAILABLE: 'unavailable',
};

function readSettings() {
  try {
    return JSON.parse(document.getElementById('login-data').textContent) || {};
  } catch {
    return {};
  }
}

const settings = readSettings();
const tables = { ka: settings.strings || {}, en: settings.stringsEn || {} };
const next = Object.prototype.hasOwnProperty.call(LANDINGS, settings.next) ? settings.next : 'admin';

const form = document.getElementById('login-form');
const username = document.getElementById('login-username');
const password = document.getElementById('login-password');
const submit = document.getElementById('login-submit');
const submitLabel = submit.querySelector('[data-label]');
const error = document.getElementById('login-error');
const errorText = error.querySelector('[data-text]');
const toggle = document.getElementById('login-toggle');
const language = document.getElementById('login-language');
let working = false;
let shownError = '';

/** A label from the server's string table, in the chosen language. */
function label(key) {
  return tables[getLang()][key] || tables.ka[key] || tables.en[key] || '';
}

/** Only ever leave for one of the landings: never a path the answer names, so a reply can
 * never send the browser to "//another-host". */
function leave(path) {
  const target = Object.values(LANDINGS).includes(path) ? path : LANDINGS[next];
  window.location.assign(target);
}

function showError(key) {
  shownError = key;
  if (!key) {
    error.hidden = true;
    errorText.textContent = '';
    return;
  }
  errorText.textContent = key === 'network' ? t('login.network')
    : key === 'error' ? t('login.error') : label(key);
  error.hidden = false;
}

function setWorking(value) {
  working = value;
  submit.disabled = value;
  submit.setAttribute('aria-busy', String(value));
  submitLabel.textContent = label(value ? 'working' : 'submit');
}

function applyLanguage() {
  const lang = getLang();
  document.documentElement.lang = lang;
  document.title = `${label('title')} | Garage 1886`;
  for (const node of document.querySelectorAll('[data-string]')) {
    const text = label(node.dataset.string);
    if (text) {
      node.textContent = text;
    }
  }
  submitLabel.textContent = label(working ? 'working' : 'submit');
  language.textContent = t('login.language_switch');
  language.lang = otherLang();
  language.setAttribute('aria-label', t('login.language_switch_label'));
  const shown = password.type === 'text';
  toggle.setAttribute('aria-label', t(shown ? 'common.hide_password' : 'common.show_password'));
  if (shownError) {
    showError(shownError);
  }
}

toggle.addEventListener('click', () => {
  const show = password.type === 'password';
  password.type = show ? 'text' : 'password';
  toggle.setAttribute('aria-pressed', String(show));
  toggle.dataset.shown = String(show);
  toggle.setAttribute('aria-label', t(show ? 'common.hide_password' : 'common.show_password'));
});

language.addEventListener('click', () => {
  setLang(otherLang());
  applyLanguage();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (working) {
    return;
  }
  if (!username.value.trim()) {
    username.focus();
    return;
  }
  if (!password.value) {
    password.focus();
    return;
  }
  showError('');
  setWorking(true);
  let response;
  try {
    response = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ username: username.value.trim(), password: password.value, next }),
    });
  } catch {
    setWorking(false);
    showError('network');
    return;
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.ok && payload && payload.user) {
    leave(payload.next); // The button stays in its checking state while the page changes.
    return;
  }
  setWorking(false);
  const detail = payload && payload.detail && !Array.isArray(payload.detail) ? payload.detail : {};
  let key = ERROR_KEYS[detail.code];
  if (!key) {
    key = response.status === 401 || response.status === 422 ? 'invalid'
      : response.status === 429 ? 'throttled'
        : response.status === 503 ? 'unavailable' : 'error';
  }
  showError(key);
  if (key === 'invalid') {
    password.value = '';
    password.focus();
  }
});

for (const input of [username, password]) {
  input.addEventListener('input', () => {
    if (shownError) {
      showError('');
    }
  });
}

/** True when the dashboard sent us here a moment ago because the server ended the session. */
function arrivedSignedOut() {
  try {
    const stamp = Number(window.sessionStorage.getItem(ENDED_KEY));
    window.sessionStorage.removeItem(ENDED_KEY);
    return Number.isFinite(stamp) && stamp > 0 && Date.now() - stamp < 15000;
  } catch {
    return false;
  }
}

/**
 * Is this browser already signed in? Asking /api/auth/session outright would answer a
 * signed-out visitor with a 401 on every visit, so the dashboard page is asked first: it
 * redirects when there is no session, and a redirect is not followed here.
 */
async function alreadySignedIn() {
  try {
    const page = await fetch(LANDINGS.admin, {
      method: 'HEAD', redirect: 'manual', credentials: 'same-origin', cache: 'no-store',
    });
    if (!page.ok) {
      return false;
    }
    const session = await fetch('/api/auth/session', {
      credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
    });
    return session.ok;
  } catch {
    return false;
  }
}

applyLanguage();
toggle.hidden = false;
language.hidden = false;
if (document.activeElement === document.body) {
  username.focus();
}
// Sent here because the session ended: show the form. Otherwise, if this browser is
// already signed in, there is nothing to do on this page.
if (!arrivedSignedOut()) {
  alreadySignedIn().then((signedIn) => {
    if (signedIn) {
      leave(LANDINGS[next]);
    }
  });
}
