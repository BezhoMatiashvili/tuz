// Widget tests for the parts assistant on the public site.
//
//   node tests/site_chat_widget_test.mjs
//
// chat.js is an ES module that reads the page while it loads, so a small stand-in for
// the DOM goes on globalThis first. Its dialog is built from the real template: a hook
// the script queries and the template lost fails here. The module is then driven the
// way a visitor drives it (taps, typed messages, the keyboard) against a scripted
// server and a clock the test moves by hand. Replies come from the golden turns in
// tests/fixtures/chat_ui/turns.json wherever one fits, the same file the Python
// contract tests check the server against.
//
// The module keeps its state for as long as it is loaded: every test starts with the
// "New chat" button. Layout is not simulated beyond the few numbers the scroll rule
// reads; what a browser draws is checked separately.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const ROOT = new URL('../', import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), 'utf8');
const TEMPLATE = read('automotive/templates/site/partials/chat.html');
const SCRIPT = read('automotive/static/site/js/chat.js');
const TURNS = JSON.parse(read('tests/fixtures/chat_ui/turns.json'));

// ------------------------------------------------------------------ strings

// What the template is rendered with (the server's `t`), in English.
const PAGE = {
  'chat.title': 'Parts assistant',
  'chat.new': 'New chat',
  'chat.close': 'Close chat',
  'chat.open': 'Open chat',
  'chat.message': 'Message',
  'chat.placeholder': 'For example Toyota Camry 2015, front pads',
  'chat.send': 'Send',
  'chat.car': 'Your car',
  'chat.car_change': 'Change',
  'chat.jump': 'New reply',
  'nav.call': 'Call',
};

// What the page hands its scripts (the `js.` strings). The first block is what a server
// that has not restarted since this work still has; the second is new with it.
const OLD_STRINGS = {
  'chat.typing': 'Assistant is typing...',
  'chat.checking': 'Checking stock...',
  'chat.error': 'No answer came back. Please try again.',
  'chat.busy': 'The assistant is busy right now. Try again in a minute.',
  'chat.limit': "Today's limit is used up. Call us or write again tomorrow.",
  'chat.disabled': 'The chat is switched off for now.',
  'chat.more': 'Show more ({count})',
  'chat.you': 'You',
  'chat.assistant': 'Assistant',
  'chat.found': 'Still checking. Found so far:',
  'chat.done': 'Done.',
  'chat.search': 'Open the part search',
  'avail.in_stock': 'In stock',
  'avail.soon': 'In 1-2 hours',
  'avail.unavailable': 'Out of stock',
  'avail.unknown': 'To be confirmed',
  'search.retry': 'Try again',
  'search.price_request': 'Price on request',
};
const NEW_STRINGS = {
  'chat.starters': 'Popular requests',
  'chat.starter_oil': 'Oil change',
  'chat.starter_pads': 'Front brake pads',
  'chat.starter_filter': 'Air filter',
  'chat.starter_vin': 'Search by VIN',
  'chat.vin_placeholder': 'Type the VIN, 17 characters',
  'chat.answers': 'Choose an answer',
  'chat.answer_hint': 'Or type your answer below',
  'chat.next': 'Next steps',
  'chat.placeholder_answer': 'Pick above or type your answer',
  'chat.placeholder_part': 'Which part do you need?',
  'chat.car_placeholder': 'Type another car or a VIN',
  'chat.car_cancel': 'Cancel',
  'chat.looking': 'Looking it up...',
  'chat.checking_car': 'Checking stock for {car}',
  'chat.reading': 'Checking the parts we found...',
  'chat.slow': 'Still checking. This can take up to a minute.',
  'chat.found_count': '{count} found so far. Still checking the rest.',
  'chat.more_failed': 'The rest did not load. Try again.',
  'chat.timeout': 'That took longer than usual. Please try again.',
  'chat.offline': 'You are offline. Check the connection and try again.',
  'chat.expired': 'The chat was idle for a while and has started again. Please tell me the car once more.',
  'chat.vin_hidden': 'We hide the VIN on screen. It is only used to identify your car.',
  'fit.confirmed': 'Matches by part number',
  'fit.listed': 'Listed for this model and year',
  'fit.check': 'Needs checking',
  'fit.spec': 'Chosen by specification',
  'avail.soon': 'At the warehouse, 1-2 hours',
  'avail.unknown': 'Ask the shop',
};
const GREETING = 'Tell me your car and the part you need. A VIN works best.';
const PHONE = '+995322123456';
const VIN = '4T1BF1FK5FU000001';

// ------------------------------------------------------------- clock, timers

let now = 1_700_000_000_000;
let timerIds = 0;
const timers = new Map();
Date.now = () => now;

const flush = async () => {
  for (let turn = 0; turn < 200; turn += 1) await Promise.resolve();
};

/** Move the clock on by `ms`, running the timers that fall due in their order. */
async function tick(ms = 0) {
  const until = now + ms;
  await flush();
  for (;;) {
    const due = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    if (!due) break;
    now = Math.max(now, due[1].at);
    timers.delete(due[0]);
    due[1].run();
    await flush();
  }
  now = until;
  await flush();
}

// ------------------------------------------------------------------ the DOM

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.detail = 0;
    this.defaultPrevented = false;
    Object.assign(this, init);
  }

  preventDefault() {
    this.defaultPrevented = true;
  }
}

class Target {
  constructor() {
    this.listeners = new Map();
    this.parentNode = null;
  }

  addEventListener(type, run, options) {
    const once = Boolean(options && typeof options === 'object' && options.once);
    this.listeners.set(type, [...(this.listeners.get(type) || []), { run, once }]);
  }

  /** Runs the listeners here and on every ancestor, like an event that bubbles. */
  dispatchEvent(event) {
    if (event instanceof FakeEvent && !event.target) event.target = this;
    for (let node = this; node; node = node.parentNode) {
      for (const entry of node.listeners.get(event.type) || []) {
        if (entry.once) node.listeners.set(event.type, node.listeners.get(event.type).filter((other) => other !== entry));
        entry.run(event);
      }
    }
    return !event.defaultPrevented;
  }
}

const REFLECTED = { id: 'id', className: 'class', lang: 'lang', type: 'type', href: 'href', placeholder: 'placeholder' };
const camel = (name) => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
const kebab = (name) => name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);

/** One compound selector: a tag, classes, an id and attribute tests, in any mix. */
function compound(text) {
  const wanted = { tag: '', classes: [], id: '', attributes: [] };
  const token = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/gy;
  let match;
  let end = 0;
  while ((match = token.exec(text))) {
    end = token.lastIndex;
    if (match[1]) wanted.tag = match[1].toLowerCase();
    else if (match[2]) wanted.classes.push(match[2]);
    else if (match[3]) wanted.id = match[3];
    else wanted.attributes.push([match[4], match[5]]);
  }
  assert.equal(end, text.length, `this test's DOM cannot read the selector "${text}"`);
  return wanted;
}

class Element extends Target {
  constructor(tag) {
    super();
    this.localName = tag.toLowerCase();
    this.attributes = new Map();
    this.kids = [];
    this.words = '';
    this.value = '';
    this.style = {};
    this.open = false;
    this.clientHeight = 0;
    this.scrolled = 0;
    this.dataset = new Proxy(this, {
      get: (node, name) => node.attributes.get(`data-${kebab(name)}`),
      set: (node, name, value) => node.attributes.set(`data-${kebab(name)}`, String(value)),
      has: (node, name) => node.attributes.has(`data-${kebab(name)}`),
    });
    const classes = () => (this.attributes.get('class') || '').split(/\s+/).filter(Boolean);
    const write = (list) => this.attributes.set('class', list.join(' '));
    this.classList = {
      contains: (name) => classes().includes(name),
      add: (...names) => write([...new Set([...classes(), ...names])]),
      remove: (...names) => write(classes().filter((name) => !names.includes(name))),
      toggle: (name, force) => {
        const on = force === undefined ? !classes().includes(name) : Boolean(force);
        write(on ? [...new Set([...classes(), name])] : classes().filter((other) => other !== name));
        return on;
      },
    };
  }

  // Attributes, and the properties that mirror them.
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  toggleAttribute(name, force) {
    const on = force === undefined ? !this.attributes.has(name) : Boolean(force);
    if (on) this.attributes.set(name, this.attributes.get(name) || '');
    else this.attributes.delete(name);
    return on;
  }

  get hidden() {
    return this.attributes.has('hidden');
  }

  set hidden(value) {
    this.toggleAttribute('hidden', Boolean(value));
  }

  get disabled() {
    return this.attributes.has('disabled');
  }

  set disabled(value) {
    this.toggleAttribute('disabled', Boolean(value));
  }

  get tabIndex() {
    return Number(this.attributes.get('tabindex') ?? -1);
  }

  set tabIndex(value) {
    this.attributes.set('tabindex', String(value));
  }

  // The tree.
  get children() {
    return [...this.kids];
  }

  get firstElementChild() {
    return this.kids[0] || null;
  }

  get lastElementChild() {
    return this.kids[this.kids.length - 1] || null;
  }

  get nextElementSibling() {
    const siblings = this.parentNode ? this.parentNode.kids : [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node === document.documentElement) return true;
    return false;
  }

  contains(other) {
    for (let node = other; node; node = node.parentNode) if (node === this) return true;
    return false;
  }

  detach() {
    if (!this.parentNode) return;
    // Like a browser: focus does not stay on something that left the page.
    if (this.contains(document.activeElement)) document.activeElement = document.body;
    this.parentNode.kids = this.parentNode.kids.filter((node) => node !== this);
    this.parentNode = null;
  }

  insert(index, nodes) {
    for (const node of nodes) {
      assert.ok(node instanceof Element, 'only elements are inserted: text goes through textContent');
      node.detach();
    }
    const at = index < 0 ? this.kids.length : Math.min(index, this.kids.length);
    this.kids.splice(at, 0, ...nodes);
    for (const node of nodes) node.parentNode = this;
  }

  append(...nodes) {
    this.insert(-1, nodes);
  }

  before(...nodes) {
    const later = nodes.filter((node) => node !== this);
    for (const node of later) node.detach();
    this.parentNode.insert(this.parentNode.kids.indexOf(this), later);
  }

  after(...nodes) {
    for (const node of nodes) node.detach();
    this.parentNode.insert(this.parentNode.kids.indexOf(this) + 1, nodes);
  }

  replaceWith(node) {
    const parent = this.parentNode;
    const index = parent.kids.indexOf(this);
    this.detach();
    parent.insert(index, [node]);
  }

  replaceChildren(...nodes) {
    for (const node of this.children) node.detach();
    this.words = '';
    this.insert(-1, nodes);
  }

  remove() {
    this.detach();
  }

  get textContent() {
    return this.words + this.kids.map((node) => node.textContent).join('');
  }

  set textContent(value) {
    for (const node of this.children) node.detach();
    this.words = String(value);
  }

  // Selectors: compounds joined by spaces (descendants), lists joined by commas.
  matches(selector) {
    return selector.split(',').some((option) => {
      const steps = option.trim().split(/\s+/).map(compound);
      const fits = (node, wanted) =>
        (!wanted.tag || node.localName === wanted.tag) &&
        (!wanted.id || node.getAttribute('id') === wanted.id) &&
        wanted.classes.every((name) => node.classList.contains(name)) &&
        wanted.attributes.every(([name, value]) => node.hasAttribute(name) && (value === undefined || node.getAttribute(name) === value));
      if (!fits(this, steps[steps.length - 1])) return false;
      let node = this.parentNode;
      for (let step = steps.length - 2; step >= 0; step -= 1) {
        while (node && !fits(node, steps[step])) node = node.parentNode;
        if (!node) return false;
        node = node.parentNode;
      }
      return true;
    });
  }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.kids) {
        if (child.matches(selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    for (let node = this; node; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }

  // Focus, and the taps a test makes.
  focus(options) {
    if (this.disabled || !this.isConnected) return;
    document.activeElement = this;
    this.focusedWith = options || {};
  }

  /** A tap or click (`detail` 1), or the keyboard's Enter or Space (`detail` 0). */
  click(detail = 1) {
    if (this.disabled) return;
    this.dispatchEvent(new FakeEvent('click', { detail }));
  }

  requestSubmit() {
    this.dispatchEvent(new FakeEvent('submit'));
  }

  show() {
    this.open = true;
    this.shownAs = 'panel';
  }

  showModal() {
    this.open = true;
    this.shownAs = 'sheet';
  }

  close() {
    this.open = false;
    this.dispatchEvent(new FakeEvent('close'));
  }

  scrollIntoView() {
    this.scrolledIntoView = (this.scrolledIntoView || 0) + 1;
  }

  // The few numbers the scroll rule reads. Only the log has a layout: its children are
  // stacked with the stylesheet's 16 px padding and 12 px gap, each as tall as `tall` says.
  get scrollHeight() {
    if (!this.tall) return 20;
    const stack = this.kids.reduce((sum, node) => sum + this.tall(node), 0) + 12 * Math.max(0, this.kids.length - 1);
    return Math.max(this.clientHeight, 32 + stack);
  }

  get offsetTop() {
    const parent = this.parentNode;
    if (!parent || !parent.tall) return 0;
    const earlier = parent.kids.slice(0, parent.kids.indexOf(this));
    return 16 + earlier.reduce((sum, node) => sum + parent.tall(node) + 12, 0);
  }

  get scrollTop() {
    return Math.max(0, Math.min(this.scrolled, this.scrollHeight - this.clientHeight));
  }

  set scrollTop(value) {
    this.scrolled = Math.max(0, Math.min(Number(value), this.scrollHeight - this.clientHeight));
  }
}

for (const [property, attribute] of Object.entries(REFLECTED)) {
  Object.defineProperty(Element.prototype, property, {
    get() {
      return this.attributes.get(attribute) || '';
    },
    set(value) {
      this.attributes.set(attribute, String(value));
    },
  });
}

/** The template as the server would send it, with the strings above. */
function render(template) {
  let html = template.replace(/\{#[\s\S]*?#\}/g, '');
  html = html.replace(/\{%[\s\S]*?%\}/g, ''); // the chat is on and the shop has a phone
  html = html.replace(/\{\{\s*icon\("([\w-]+)"\)\s*\}\}/g, '<svg class="icon"><use href="/assets/b1/site/vendor/phosphor/sprite.svg#$1"></use></svg>');
  html = html.replace(/\{\{\s*c\.brand\.phone\|tel\s*\}\}/g, PHONE);
  const string = (key) => {
    assert.ok(key in PAGE, `the template asks for "${key}", which this test does not render`);
    return PAGE[key];
  };
  html = html.replace(/\{\{\s*tt\(\s*["']([\w.]+)["'][^}]*\)\s*\}\}/g, (_, key) => string(key));
  html = html.replace(/\{\{\s*(?:c\.chat\.launcher or )?t\[\s*["']([\w.]+)["']\s*\]\s*\}\}/g, (_, key) => string(key));
  assert.doesNotMatch(html, /\{\{|\{%/, 'the template uses something this test cannot render');
  return html;
}

/** Elements from markup: enough for a template of plain tags, attributes and text. */
function build(html, into) {
  const token = /<\/([\w-]+)\s*>|<([\w-]+)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*>|([^<]+)/g;
  let open = into;
  let match;
  while ((match = token.exec(html))) {
    if (match[1]) {
      assert.equal(open.localName, match[1], 'the template closes its tags in order');
      open = open.parentNode;
    } else if (match[2]) {
      const node = new Element(match[2]);
      for (const [, name, value] of match[3].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) node.setAttribute(name, value ?? '');
      open.append(node);
      open = node;
    } else if (match[4].trim()) {
      open.words += match[4].trim();
    }
  }
  assert.equal(open, into, 'the template closes every tag');
}

const document = new Target();
document.documentElement = new Element('html');
document.body = new Element('body');
document.documentElement.append(document.body);
document.activeElement = document.body;
document.createElement = (tag) => new Element(tag);
document.createElementNS = (_, tag) => new Element(tag);
document.querySelector = (selector) => document.documentElement.querySelector(selector);
document.querySelectorAll = (selector) => document.documentElement.querySelectorAll(selector);
document.getElementById = (id) => document.documentElement.querySelectorAll('[id]').find((node) => node.id === id) || null;

build('<section id="parts"><h2>Parts</h2></section>', document.body);
build(render(TEMPLATE), document.body);
build('<script id="site-data" type="application/json"></script>', document.body);
document.getElementById('site-data').textContent = JSON.stringify({
  lang: 'en',
  currency: '₾',
  inStock: 6,
  chat: { enabled: true, greeting: GREETING },
  strings: { ...OLD_STRINGS, ...NEW_STRINGS },
});

const media = new Map();
const window = new Target();
window.matchMedia = (query) => {
  if (!media.has(query)) media.set(query, Object.assign(new Target(), { matches: false, media: query }));
  return media.get(query);
};
window.setTimeout = (run, ms = 0) => {
  timerIds += 1;
  timers.set(timerIds, { at: now + ms, run });
  return timerIds;
};
window.clearTimeout = (id) => timers.delete(id);
window.history = { pushed: 0, backs: 0, pushState() { this.pushed += 1; }, back() { this.backs += 1; } };
window.navigator = { onLine: true };
window.visualViewport = Object.assign(new Target(), { height: 800, offsetTop: 0, scale: 1 });

// -------------------------------------------------------------- the server

const BASE = '/api/site/chat/conversations';
const calls = [];
const server = {
  created: 0,
  started: 0,
  replies: [], // what a status read answers, in order; the last one repeats
  pages: new Map(), // "Show more": offset -> page, or an Error
  refuse: null, // (call) => a response to give instead, or nothing
};

const older = (changes = {}) => ({
  status: 'COMPLETE', answer: 'Done.', note: '', language: 'en', quick_replies: [], cards: [],
  total: 0, offset: 0, next_offset: null, checked: 0, checking: 0, ...changes,
});
const reply = (changes = {}) => older({ car: '', question: null, suggestions: [], groups: [], ...changes });
const golden = (id, changes = {}) => ({ ...structuredClone(TURNS.find((turn) => turn.id === id).public), ...changes });
const problem = (status, code = '') => ({ status, body: { detail: code ? { code } : 'Internal Server Error' } });

function answer(call) {
  const refused = server.refuse && server.refuse(call);
  if (refused) return refused;
  if (call.method === 'DELETE') return { status: 204 };
  if (call.method === 'POST' && call.url === BASE) {
    server.created += 1;
    return { status: 201, body: { conversation_id: `conversation-${server.created}`, expires_in: 600 } };
  }
  if (call.method === 'POST') {
    server.started += 1;
    return { status: 202, body: { turn_id: `turn-${server.started}`, status: 'RUNNING', language: 'en' } };
  }
  const offset = Number(new URL(call.url, 'http://site.test').searchParams.get('offset'));
  if (offset > 0) {
    const page = server.pages.get(offset);
    if (page instanceof Error) throw page;
    return page && page.status >= 400 ? page : { status: 200, body: page };
  }
  const next = server.replies.length > 1 ? server.replies.shift() : server.replies[0];
  return { status: 200, body: next || reply() };
}

globalThis.window = window;
globalThis.document = document;
globalThis.fetch = async (url, options = {}) => {
  const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined };
  calls.push(call);
  const { status, body } = answer(call);
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      if (body === undefined) throw new SyntaxError('no body');
      return structuredClone(body);
    },
  };
};

// ------------------------------------------------------------ the module

const { data } = await import('../automotive/static/site/js/dom.js');
const chat = await import('../automotive/static/site/js/chat.js');

// The string table the module reads. dom.js keeps the object it parsed, so a test can
// take strings away (a server that has not restarted yet) and the next test gets them back.
const table = data.strings;
function useStrings(...sources) {
  for (const key of Object.keys(table)) delete table[key];
  Object.assign(table, ...sources);
}

const dialog = document.getElementById('chat');
const log = dialog.querySelector('.chat__log');
const form = dialog.querySelector('.chat__form');
const input = dialog.querySelector('.chat__input');
const sendButton = dialog.querySelector('.chat__send');
const progress = dialog.querySelector('.chat__progress');
const counter = dialog.querySelector('.chat__count');
const carBar = dialog.querySelector('.chat__car');
const carChange = dialog.querySelector('[data-chat-car]');
const jump = dialog.querySelector('[data-chat-jump]');
const launcher = document.querySelector('[data-chat-launcher]');
const phone = media.get('(max-width: 767.98px)');
const viewport = window.visualViewport;

// A message is one line tall, plus its parts, headings and buttons.
log.clientHeight = 320;
log.tall = (node) => {
  if (node.classList.contains('typing')) return 44;
  if (node.classList.contains('starters')) return 260;
  return (
    44 +
    70 * node.querySelectorAll('.card').length +
    30 * node.querySelectorAll('.cards__group').length +
    (node.querySelector('.ask') ? 60 : 0) +
    (node.querySelector('.msg__more') ? 44 : 0) +
    (node.querySelector('.next') ? 52 : 0)
  );
};

const messages = (role) => log.children.filter((node) => node.classList.contains(`msg--${role}`));
const newest = () => messages('assistant').at(-1);
const labels = (nodes) => nodes.map((node) => node.textContent);
const shape = (node) => node.children.map((child) => child.attributes.get('class') || child.localName);
const posted = () => calls.filter((call) => call.method === 'POST' && call.url.endsWith('/messages'));
const sameNodes = (actual, expected) => actual.length === expected.length && actual.every((node, index) => node === expected[index]);
const scrollTo = (top) => {
  log.scrollTop = top;
  log.dispatchEvent(new FakeEvent('scroll'));
};

/** A new conversation, on a wide screen unless the test says otherwise. */
async function fresh({ onPhone = false } = {}) {
  if (dialog.open) chat.close();
  phone.matches = onPhone;
  calls.length = 0;
  Object.assign(server, { created: 0, started: 0, replies: [], refuse: null });
  server.pages.clear();
  window.navigator.onLine = true;
  useStrings(OLD_STRINGS, NEW_STRINGS);
  Object.assign(viewport, { height: 800, offsetTop: 0, scale: 1 });
  log.clientHeight = 320;
  input.value = '';
  chat.open({ opener: launcher });
  dialog.querySelector('[data-chat-new]').click();
  await tick(0);
  calls.length = 0;
}

async function type(words) {
  input.value = words;
  input.dispatchEvent(new FakeEvent('input'));
  form.requestSubmit();
  await tick(0);
}

async function tap(button, { keyboard = false } = {}) {
  assert.ok(button, 'the button to tap exists');
  button.click(keyboard ? 0 : 1);
  await tick(0);
}

/** Send `words` and let the server answer with `turns`, one status read after another. */
async function ask(words, ...turns) {
  server.replies = turns;
  await type(words);
  for (let read = 1; read < turns.length; read += 1) await tick(150);
}

// ---------------------------------------------------------------- the runner

let passed = 0;
const failures = [];
async function test(name, body) {
  try {
    await fresh();
    await body();
    passed += 1;
  } catch (error) {
    failures.push(`${name}\n    ${String(error.stack || error.message).split('\n').slice(0, 12).join('\n    ')}`);
  }
}

// ------------------------------------------------------- template and strings

await test('the template has every hook the script reaches for', () => {
  const queried = new Set();
  for (const match of SCRIPT.matchAll(/(?:dialog|document)\.querySelector\('([^']+)'\)|\.closest\('([^']+)'\)/g)) {
    queried.add(match[1] || match[2]);
  }
  assert.ok(queried.size >= 12, `only ${queried.size} selectors were found in the script`);
  for (const selector of queried) {
    assert.ok(document.querySelector(selector), `the template has nothing for ${selector}`);
  }
  for (const id of ['chat', 'parts']) assert.ok(document.getElementById(id));
  // The parts of the dialog the script shows on its own start hidden.
  assert.equal(carBar.hidden, true);
  assert.equal(jump.hidden, true);
  assert.equal(carBar.getAttribute('role'), 'group');
  assert.equal(carBar.getAttribute('aria-label'), 'Your car');
  assert.equal(jump.textContent, 'New reply');
  assert.equal(log.getAttribute('role'), 'log');
  assert.equal(log.getAttribute('aria-relevant'), 'additions');
  assert.equal(log.getAttribute('tabindex'), '0', 'the log can take the focus');
  // The label is there for screen readers; the row above the field is otherwise empty.
  const label = dialog.querySelector('label');
  assert.ok(label.classList.contains('sr-only'));
  assert.equal(label.getAttribute('for'), input.id);
  assert.equal(dialog.dataset.phone, PHONE);
  assert.equal(dialog.dataset.call, 'Call');
  // House rules of the site's templates: no inline style, no inline handler.
  assert.doesNotMatch(TEMPLATE, /\sstyle\s*=|\son[a-z]+\s*=/i);
  // Only icons the sprite has.
  const sprite = read('automotive/static/site/vendor/phosphor/sprite.svg');
  const icons = [...TEMPLATE.matchAll(/icon\("([\w-]+)"\)/g), ...SCRIPT.matchAll(/icon\('([\w-]+)'\)/g)].map((match) => match[1]);
  for (const name of [...icons, 'check', 'info', 'warning', 'gauge', 'drop', 'disc', 'fan', 'magnifying-glass']) {
    assert.ok(sprite.includes(`id="${name}"`), `the sprite has no "${name}" icon`);
  }
});

await test('this test knows every string the script reads', () => {
  const read = new Set([...SCRIPT.matchAll(/['`]((?:chat|search|avail|fit)\.[a-z_]+)['`]/g)].map((match) => match[1]));
  assert.ok(read.size >= 30, `only ${read.size} string keys were found in the script`);
  for (const key of read) assert.ok(key in table, `"${key}" is read by the script and missing here`);
  for (const key of ['fit.confirmed', 'fit.listed', 'fit.check', 'fit.spec', 'avail.in_stock', 'avail.soon', 'avail.unavailable', 'avail.unknown']) {
    assert.ok(key in table, key);
  }
});

// ------------------------------------------------------------- opening, empty

await test('the empty chat greets and offers four starters', () => {
  assert.equal(dialog.open, true);
  assert.equal(dialog.shownAs, 'panel');
  assert.deepEqual(shape(log), ['msg msg--assistant', 'starters']);
  assert.equal(log.children[0].querySelector('p').textContent, GREETING);
  const starters = log.querySelector('.starters');
  assert.equal(starters.getAttribute('role'), 'group');
  const title = starters.querySelector('.starters__title');
  assert.equal(title.textContent, 'Popular requests');
  assert.equal(starters.getAttribute('aria-labelledby'), title.id);
  const buttons = starters.querySelectorAll('.starter');
  assert.deepEqual(labels(buttons), ['Oil change', 'Front brake pads', 'Air filter', 'Search by VIN']);
  for (const button of buttons) {
    assert.equal(button.type, 'button');
    assert.equal(button.querySelectorAll('svg').length, 2, 'an icon before the label and an arrow after it');
  }
  // Nothing to send yet, nothing asked of the server yet.
  assert.equal(sendButton.disabled, true);
  assert.equal(counter.hidden, true);
  assert.equal(input.placeholder, PAGE['chat.placeholder']);
  assert.equal(calls.length, 0);
});

await test('a starter sends its label; the VIN one only points the composer at a VIN', async () => {
  const [oil, , , vin] = log.querySelectorAll('.starter');
  await tap(vin);
  assert.equal(posted().length, 0, 'a VIN has to be typed: nothing is sent');
  assert.equal(input.placeholder, 'Type the VIN, 17 characters');
  assert.equal(document.activeElement, input);
  assert.ok(log.querySelector('.starters'), 'the starters are still there');
  server.replies = [reply({ answer: 'Which car is it for?' })];
  await tap(oil);
  assert.deepEqual(posted()[0].body, { message: 'Oil change', language: 'auto' });
  assert.equal(log.querySelector('.starters'), null, 'the starters go with the first message');
  assert.deepEqual(labels(messages('user').map((node) => node.querySelector('p'))), ['Oil change']);
  assert.equal(input.placeholder, PAGE['chat.placeholder'], 'the VIN example went with the message');
});

await test('a wide screen focuses the composer, a phone the log unless text was handed over', async () => {
  assert.equal(document.activeElement, input);
  await fresh({ onPhone: true });
  assert.equal(dialog.shownAs, 'sheet');
  assert.equal(document.activeElement, log, 'a new chat on a phone: the keyboard would cover the starters');
  assert.equal(dialog.style.height, '800px', 'the sheet sits on the visible part of the screen');
  // Opening it is the same: the log takes the focus, not the composer.
  chat.close();
  launcher.focus();
  chat.open({ opener: launcher });
  assert.equal(document.activeElement, log);
  assert.deepEqual(log.focusedWith, { preventScroll: true });
  assert.equal(window.history.pushed > 0, true, 'the back button can close the sheet');
  chat.close();
  chat.open({ opener: launcher, prefill: 'front pads for a Camry' });
  assert.equal(document.activeElement, input, 'there is something to send: the composer has the focus');
  assert.equal(input.value, 'front pads for a Camry');
  assert.equal(sendButton.disabled, false);
  assert.equal(posted().length, 0, 'a prefill is never sent by itself');
});

// ------------------------------------------------------------------ questions

await test('a question is drawn with its answers in a labelled group right under it', async () => {
  await ask('front pads for a Camry', golden('question'));
  const bubble = newest();
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'ask', 'ask__hint']);
  const [, lead, group, hint] = bubble.children;
  assert.equal(lead.textContent, 'Toyota Camry 2015. წინა თუ უკანა ხუნდები გჭირდებათ?');
  assert.equal(group.getAttribute('role'), 'group');
  assert.equal(group.getAttribute('aria-label'), 'Choose an answer');
  assert.equal(group.getAttribute('aria-describedby'), lead.id);
  assert.ok(lead.id, 'the question can be pointed at');
  const chips = group.querySelectorAll('.chip');
  assert.deepEqual(labels(chips), ['წინა', 'უკანა', 'ორივე']);
  for (const button of chips) {
    assert.equal(button.localName, 'button');
    assert.equal(button.type, 'button');
    assert.equal(button.disabled, false);
    assert.equal(button.getAttribute('aria-pressed'), null);
  }
  assert.equal(hint.textContent, 'Or type your answer below');
  assert.equal(bubble.lang, 'ka');
  assert.ok(bubble.classList.contains('msg--wide'), 'three answers take the full width');
  assert.ok(!group.classList.contains('ask--stack'), 'short answers sit side by side');
  // While it is open the question is all there is to do: no next steps beside it.
  assert.equal(bubble.querySelector('.next'), null);
  assert.equal(input.placeholder, 'Pick above or type your answer');
});

await test('a tap sends the value and the number, and the answers stay with the chosen one marked', async () => {
  const options = [
    { label: 'Front', value: 'front brake pads' },
    { label: 'Rear', value: 'rear brake pads' },
    { label: 'Both', value: 'both axles' },
  ];
  await ask('pads for a Camry', reply({ answer: 'Front or rear?', question: { options } }));
  const group = newest().querySelector('.ask');
  const chips = group.querySelectorAll('.chip');
  server.replies = [reply({ answer: 'Here are the rear pads.' })];
  await tap(chips[1]);
  assert.deepEqual(posted().at(-1).body, { message: 'rear brake pads', language: 'auto', choice: 2 });
  // Nothing is taken away, so nothing jumps: the group is where it was.
  assert.ok(group.isConnected);
  assert.ok(sameNodes(group.querySelectorAll('.chip'), chips));
  assert.deepEqual(chips.map((button) => button.disabled), [true, true, true]);
  assert.deepEqual(chips.map((button) => button.getAttribute('aria-pressed')), [null, 'true', null]);
  assert.equal(group.nextElementSibling.hidden, true, 'the hint to type is no longer true');
  // The visitor reads what they tapped; the server got what it stands for.
  assert.equal(messages('user').at(-1).querySelector('p').textContent, 'Rear');
  // Focus left the button before it was switched off, and stayed in the chat.
  assert.equal(document.activeElement, log);
  assert.deepEqual(log.focusedWith, { preventScroll: true });
  assert.equal(newest().querySelector('p').textContent, 'Here are the rear pads.');
  assert.equal(input.placeholder, PAGE['chat.placeholder']);
});

await test('a typed answer closes the question with nothing marked and sends no number', async () => {
  await ask('pads for a Camry', golden('question'));
  const chips = newest().querySelectorAll('.ask .chip');
  await ask('the front ones please', reply({ answer: 'Here they are.' }));
  assert.deepEqual(posted().at(-1).body, { message: 'the front ones please', language: 'auto' });
  assert.deepEqual(chips.map((button) => button.disabled), [true, true, true]);
  assert.deepEqual(chips.map((button) => button.getAttribute('aria-pressed')), [null, null, null]);
});

await test('an older offer is answered with yes or no above its parts', async () => {
  await ask('front pads', golden('legacy_offer'));
  const bubble = newest();
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'ask', 'ask__hint', 'cards'], 'the answers come before the parts');
  assert.deepEqual(labels(bubble.querySelectorAll('.ask .chip')), ['დიახ', 'არა']);
  assert.deepEqual(labels(bubble.querySelectorAll('.card__fit')), ['Matches by part number', 'Needs checking']);
  assert.deepEqual(labels(bubble.querySelectorAll('.card__why')), ['კატალოგში მითითებულია ამ მოდელზე']);
  server.replies = [reply({ answer: 'უკანა ხუნდებიც ვიპოვე.', language: 'ka' })];
  await tap(bubble.querySelector('.ask .chip'));
  assert.deepEqual(posted().at(-1).body, { message: 'დიახ', language: 'auto', choice: 1 });
});

await test('a long answer stacks the group one per row', async () => {
  const options = [
    { label: 'Petrol 2.5 (2012 to 2017)', value: 'petrol 2.5' },
    { label: 'Hybrid', value: 'hybrid' },
  ];
  await ask('oil filter', reply({ answer: 'Which engine?', question: { options } }));
  const bubble = newest();
  assert.ok(bubble.querySelector('.ask').classList.contains('ask--stack'));
  assert.ok(bubble.classList.contains('msg--wide'));
  await ask('pads', reply({ answer: 'Front or rear?', question: { options: [{ label: 'Front', value: 'front' }, { label: 'Rear', value: 'rear' }] } }));
  assert.ok(!newest().classList.contains('msg--wide'), 'two short answers fit a narrow bubble');
});

await test('an answer given from the keyboard takes the focus to the next answers; a pointer is left alone', async () => {
  const question = (text) => reply({ answer: text, question: { options: [{ label: 'Yes', value: 'yes' }, { label: 'No', value: 'no' }] } });
  await ask('pads', question('Front ones?'));
  server.replies = [question('The rear ones too?')];
  await tap(newest().querySelector('.ask .chip'), { keyboard: true });
  const next = newest().querySelectorAll('.ask .chip');
  assert.equal(document.activeElement, next[0], 'the keyboard is on the first new answer');
  assert.deepEqual(next[0].focusedWith, { preventScroll: true });
  server.replies = [question('Discs as well?')];
  await tap(next[1]);
  assert.equal(document.activeElement, log, 'someone tapping is not moved');
});

// ------------------------------------------------------------------- failures

await test('a message that fails reopens the question, and the retry does not repeat the bubble', async () => {
  await ask('pads for a Camry', golden('question'));
  const group = newest().querySelector('.ask');
  const chips = group.querySelectorAll('.chip');
  server.refuse = (call) => (call.method === 'POST' && call.url.endsWith('/messages') ? problem(500) : null);
  await tap(chips[0]);
  const failed = newest();
  assert.ok(failed.classList.contains('msg--error'));
  assert.equal(failed.querySelector('p').textContent, 'No answer came back. Please try again.');
  assert.deepEqual(chips.map((button) => button.disabled), [false, false, false], 'the answers are live again');
  assert.deepEqual(chips.map((button) => button.getAttribute('aria-pressed')), [null, null, null]);
  assert.equal(group.nextElementSibling.hidden, false, 'so is the hint');
  assert.equal(input.placeholder, 'Pick above or type your answer');
  assert.equal(messages('user').length, 2);
  assert.equal(progress.textContent, '');
  const retry = failed.querySelector('.next .chip');
  assert.equal(retry.textContent, 'Try again');
  assert.equal(failed.querySelector('.next').getAttribute('role'), 'group');
  // The retry sends the same answer again, number included, into the same bubbles.
  server.refuse = null;
  server.replies = [reply({ answer: 'Here are the front pads.' })];
  const before = posted().length;
  await tap(retry);
  assert.equal(failed.isConnected, false, 'the error leaves when it is retried');
  assert.equal(posted().length, before + 1);
  assert.deepEqual(posted().at(-1).body, { message: 'წინა', language: 'auto', choice: 1 });
  assert.equal(messages('user').length, 2, 'no second copy of what the visitor said');
  assert.deepEqual(chips.map((button) => button.getAttribute('aria-pressed')), ['true', null, null]);
  assert.deepEqual(chips.map((button) => button.disabled), [true, true, true]);
  assert.equal(newest().querySelector('p').textContent, 'Here are the front pads.');
  assert.equal(document.activeElement, log, 'focus was not left on the retry button that went away');
});

await test('a busy server, a lost connection and a slow turn each say what happened', async () => {
  server.refuse = (call) => (call.url.endsWith('/messages') ? problem(429, 'CHAT_WORKER_CAPACITY') : null);
  await type('pads');
  assert.equal(newest().querySelector('p').textContent, 'The assistant is busy right now. Try again in a minute.');
  assert.equal(newest().querySelector('.next .chip').textContent, 'Try again');
  // Offline: the request never leaves.
  window.navigator.onLine = false;
  server.refuse = () => {
    throw new TypeError('fetch failed');
  };
  await tap(newest().querySelector('.next .chip'));
  assert.equal(newest().querySelector('p').textContent, 'You are offline. Check the connection and try again.');
  assert.equal(messages('assistant').filter((node) => node.classList.contains('msg--error')).length, 1);
  // A turn the server never finishes: the visitor is told after a minute and a half.
  window.navigator.onLine = true;
  server.refuse = null;
  server.replies = [reply({ status: 'RUNNING', answer: '' })];
  await tap(newest().querySelector('.next .chip'));
  await tick(96000);
  assert.equal(newest().querySelector('p').textContent, 'That took longer than usual. Please try again.');
  assert.equal(log.querySelector('.typing'), null);
  assert.equal(progress.textContent, '');
  assert.equal(sendButton.disabled, true, 'nothing typed');
  assert.equal(messages('user').length, 1, 'three tries, one bubble');
});

await test('a turn the engine failed keeps its sentence and offers a retry and the part search', async () => {
  await ask('pads for a Camry', golden('question'));
  const chips = newest().querySelectorAll('.ask .chip');
  // The engine offers its own "send it again" with such a reply: one retry is enough.
  const suggestions = [{ label: 'Try again', value: 'ორივე' }];
  server.replies = [reply({ status: 'FAILED', answer: 'The service is temporarily unavailable. Please try again.', suggestions })];
  await tap(chips[2]);
  const failed = newest();
  assert.ok(failed.classList.contains('msg--error'));
  assert.equal(failed.querySelector('p').textContent, 'The service is temporarily unavailable. Please try again.');
  assert.deepEqual(labels(failed.querySelectorAll('.next .chip')), ['Try again', 'Open the part search', 'Call']);
  assert.deepEqual(chips.map((button) => button.disabled), [false, false, false], 'the question is open again');
  server.replies = [reply({ answer: 'Both axles, then.' })];
  await tap(failed.querySelector('.next .chip'));
  assert.equal(failed.isConnected, false);
  assert.deepEqual(posted().at(-1).body, { message: 'ორივე', language: 'auto', choice: 3 });
  assert.equal(messages('user').length, 2);
  // In another language, with the VIN written as the engine writes it, it is still the same retry;
  // a suggestion that does something else stays.
  const others = [{ label: 'Попробовать снова', value: 'масло для [VIN]' }, { label: 'Search by name', value: 'oil filter' }];
  await ask(`масло для ${VIN}`, reply({ status: 'FAILED', answer: 'Извините, сейчас не получилось.', suggestions: others }));
  assert.deepEqual(labels(newest().querySelectorAll('.next .chip')), ['Search by name', 'Try again', 'Open the part search', 'Call']);
});

await test('the daily limit closes the composer and leaves the phone and the part search', async () => {
  server.refuse = (call) => (call.url.endsWith('/messages') ? problem(429, 'CHAT_DAILY_LIMIT') : null);
  await type('pads');
  const bubble = newest();
  assert.equal(bubble.querySelector('p').textContent, "Today's limit is used up. Call us or write again tomorrow.");
  const [call, search] = bubble.querySelector('.next').children;
  assert.equal(call.localName, 'a');
  assert.equal(call.href, `tel:${PHONE}`);
  assert.equal(call.textContent, 'Call');
  assert.equal(search.textContent, 'Open the part search');
  assert.equal(bubble.querySelectorAll('.next .chip').length, 2, 'no retry: it would fail again');
  assert.equal(input.disabled, true);
  assert.equal(sendButton.disabled, true);
  input.value = 'again';
  form.requestSubmit();
  await tick(0);
  assert.equal(posted().length, 1, 'a closed composer sends nothing');
  // The ways out still work: the part search closes the chat and goes to the search.
  const visits = document.getElementById('parts').scrolledIntoView || 0;
  await tap(search);
  assert.equal(dialog.open, false);
  assert.equal(document.getElementById('parts').scrolledIntoView, visits + 1);
  // A new chat opens it again.
  await fresh();
  assert.equal(input.disabled, false);
  server.refuse = (request) => (request.url.endsWith('/messages') ? problem(503, 'CHAT_DISABLED') : null);
  await type('pads');
  assert.equal(newest().querySelector('p').textContent, 'The chat is switched off for now.');
  assert.deepEqual(labels(newest().querySelector('.next').children), ['Open the part search', 'Call']);
  assert.equal(input.disabled, true);
});

await test('the part search shortcut closes the chat and goes to the search', async () => {
  await ask('hello', reply({ answer: 'How can I help?' }));
  const search = newest().querySelectorAll('.next .chip').find((button) => button.textContent === 'Open the part search');
  await tap(search);
  assert.equal(dialog.open, false);
  assert.ok(document.getElementById('parts').scrolledIntoView >= 1);
  assert.equal(document.activeElement, document.querySelector('#parts h2'), 'focus is on the search heading');
});

// --------------------------------------------------------------- parts, groups

await test('an offer lists its parts under group headings, then the next steps', async () => {
  await ask('front pads and an oil filter', golden('offer'));
  const bubble = newest();
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'msg__note', 'cards', 'next']);
  assert.ok(bubble.classList.contains('msg--wide'));
  const holder = bubble.querySelector('.cards');
  // The two pads rest on different grounds and each says its own; the one filter's ground
  // is the whole group's and is said under its heading.
  assert.deepEqual(shape(holder), ['cards__group', 'msg__cards', 'cards__group', 'cards__fit', 'msg__cards']);
  const [first, pads, second, why, filters] = holder.children;
  assert.equal(first.localName, 'h3');
  assert.equal(first.words, 'წინა ხუნდები');
  assert.equal(first.querySelector('.cards__count').textContent, '2');
  assert.equal(second.words, 'ზეთის ფილტრი');
  assert.equal(pads.lang, 'ka');
  assert.equal(pads.children.length, 2);
  assert.equal(filters.children.length, 1);
  // One card: name and price, brand and what it fits, then rows of their own for the
  // availability, for how sure the fit is and for the reason.
  const row = pads.children[0];
  assert.deepEqual(shape(row), ['card__name', 'card__price', 'card__meta', 'card__state', 'card__fit', 'card__why']);
  assert.deepEqual(shape(row.querySelector('.card__state')), ['card__stock'], 'availability shares its row with nothing');
  assert.deepEqual(shape(pads.children[1]), ['card__name', 'card__price', 'card__meta', 'card__state', 'card__fit', 'card__why']);
  assert.deepEqual(shape(why), ['card__fit', 'card__why']);
  assert.equal(why.textContent, 'Needs checkingძრავი ჩანაწერში არ წერია. გადაამოწმეთ ძველი ფილტრით.');
  assert.deepEqual(shape(filters.children[0]), ['card__name', 'card__price card__price--ask', 'card__meta', 'card__state'], 'the card does not repeat its group');
  assert.equal(row.querySelector('.card__name').textContent, 'სამუხრუჭე ხუნდი წინა');
  assert.equal(row.querySelector('.card__price').textContent, '₾45.50');
  assert.equal(row.querySelector('.card__meta').textContent, 'SATO, TOYOTA CAMRY 2012-2017');
  assert.equal(row.dataset.availability, 'in_stock');
  const stock = row.querySelector('.card__stock');
  assert.equal(stock.textContent, 'In stock');
  assert.equal(stock.dataset.availability, 'in_stock', 'the marker takes its shape from this');
  const fit = row.querySelector('.card__fit');
  assert.equal(fit.textContent, 'Matches by part number');
  assert.equal(fit.dataset.confidence, 'confirmed');
  assert.match(fit.querySelector('use').getAttribute('href'), /#check$/);
  assert.equal(row.querySelector('.card__why').textContent, 'ნომერი ემთხვევა ამ მანქანის კატალოგის ჩანაწერს.');
  assert.deepEqual(labels(holder.querySelectorAll('.card__stock')), ['In stock', 'At the warehouse, 1-2 hours', 'At the warehouse, 1-2 hours']);
  assert.deepEqual(labels(holder.querySelectorAll('.card__fit')), ['Matches by part number', 'Listed for this model and year', 'Needs checking']);
  // No price in the catalogue is said in words, in a quieter style.
  const onRequest = filters.children[0].querySelector('.card__price');
  assert.equal(onRequest.textContent, 'Price on request');
  assert.ok(onRequest.classList.contains('card__price--ask'));
  // The next steps come last, as their own quieter group.
  const next = bubble.querySelector('.next');
  assert.equal(next.getAttribute('role'), 'group');
  assert.equal(next.getAttribute('aria-label'), 'Next steps');
  assert.deepEqual(labels(next.children), ['უკანა ხუნდებიც', 'ჰაერის ფილტრი'], 'parts in stock: no call link, no part search');
  assert.equal(input.placeholder, 'Which part do you need?', 'the car is known and nothing is asked');
  // A next step sends its text and no number, and the row goes with it.
  server.replies = [reply({ answer: 'უკანა ხუნდები.', language: 'ka' })];
  await tap(next.children[0]);
  assert.deepEqual(posted().at(-1).body, { message: 'უკანა ხუნდებიც', language: 'auto' });
  assert.equal(next.isConnected, false);
  assert.equal(document.activeElement, log);
});

await test('nothing found, or a part to ask the shop about, adds the ways out of the chat', async () => {
  await ask('rear shock absorber', golden('nothing_found'));
  assert.deepEqual(labels(newest().querySelector('.next').children), ['VIN კოდით შემოწმება', 'წინა ამორტიზატორი', 'Open the part search', 'Call']);
  assert.equal(newest().querySelector('.cards'), null);
  const unsure = golden('offer');
  unsure.cards[2].availability = 'unknown';
  unsure.cards[1].availability = 'unavailable';
  await ask('pads', unsure);
  const steps = newest().querySelector('.next').children;
  assert.deepEqual(labels(steps), ['უკანა ხუნდებიც', 'ჰაერის ფილტრი', 'Call'], 'with parts listed the search is not offered');
  assert.equal(steps[2].href, `tel:${PHONE}`);
  assert.deepEqual(labels(newest().querySelectorAll('.card__stock')), ['In stock', 'Out of stock', 'Ask the shop']);
  assert.equal(newest().querySelectorAll('.card')[1].dataset.availability, 'unavailable');
  // Without a phone number there is simply no link.
  dialog.removeAttribute('data-phone');
  await ask('hello', reply({ answer: 'How can I help?' }));
  assert.deepEqual(labels(newest().querySelector('.next').children), ['Open the part search']);
  dialog.setAttribute('data-phone', PHONE);
});

await test('"Show more" keeps the groups, and says so right there when it fails', async () => {
  const offer = golden('offer');
  const [padA, padB, filter] = offer.cards;
  const groups = [{ id: 'g1', label: 'წინა ხუნდები', count: 3 }, { id: 'g2', label: 'ზეთის ფილტრი', count: 2 }];
  await ask('pads and a filter', { ...offer, groups, cards: [padA, padB], total: 5, next_offset: 2 });
  const bubble = newest();
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'msg__note', 'cards', 'msg__more', 'next']);
  const more = bubble.querySelector('.msg__more');
  assert.equal(more.textContent, 'Show more (3)');
  const holder = bubble.querySelector('.cards');
  const firstRows = holder.querySelectorAll('.card');
  // The first try fails: a line above the button, which stays live. The status line is not used.
  server.pages.set(2, problem(500));
  await tap(more);
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'msg__note', 'cards', 'msg__fail', 'msg__more', 'next']);
  assert.equal(bubble.querySelector('.msg__fail').textContent, 'The rest did not load. Try again.');
  assert.equal(more.disabled, false);
  assert.equal(progress.textContent, '');
  assert.equal(holder.querySelectorAll('.card').length, 2);
  // The second try brings one more pad and two filters.
  const third = { ...padB, name: 'სამუხრუჭე ხუნდი წინა, კერამიკული' };
  server.pages.set(2, { ...offer, groups, cards: [third, filter, { ...filter, brand: 'SAKURA' }], total: 5, offset: 2, next_offset: null });
  await tap(more);
  assert.equal(calls.at(-1).url, `${BASE}/conversation-1/turns/turn-1?offset=2&limit=10`);
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'msg__note', 'cards', 'next'], 'the failure line and the button are gone');
  assert.deepEqual(shape(holder), ['cards__group', 'msg__cards', 'cards__group', 'cards__fit', 'msg__cards']);
  const [, pads, heading, , filters] = holder.children;
  assert.equal(pads.children.length, 3, 'the pad joined its own group');
  assert.equal(heading.words, 'ზეთის ფილტრი');
  assert.equal(heading.querySelector('.cards__count').textContent, '2');
  assert.equal(filters.children.length, 2);
  assert.ok(sameNodes(holder.querySelectorAll('.card').slice(0, 2), firstRows), 'the rows that were there were not redrawn');
  // The button that was pressed is gone: focus is where the new parts start.
  assert.equal(document.activeElement, pads.children[2]);
  assert.deepEqual(pads.children[2].focusedWith, { preventScroll: true });
  assert.equal(pads.children[2].getAttribute('tabindex'), '-1');
});

await test('a group whose parts rest on one ground says it once, under its heading', async () => {
  // Two filters listed for the car, two oils chosen by their specification: on the live
  // walk every card of such a group repeated the same line.
  await ask('oil change', golden('oil_change'));
  const holder = newest().querySelector('.cards');
  assert.deepEqual(shape(holder), ['cards__group', 'cards__fit', 'msg__cards', 'cards__group', 'cards__fit', 'msg__cards']);
  const [, listed, filters, , chosen, oils] = holder.children;
  assert.deepEqual(shape(listed), ['card__fit'], 'no reason came with it: the label alone');
  assert.equal(listed.textContent, 'Listed for this model and year');
  // The fourth level: a product no car is named on, chosen because its specification fits.
  assert.deepEqual(shape(chosen), ['card__fit', 'card__why']);
  const badge = chosen.querySelector('.card__fit');
  assert.equal(badge.textContent, 'Chosen by specification');
  assert.equal(badge.dataset.confidence, 'spec');
  assert.match(badge.querySelector('use').getAttribute('href'), /#gauge$/);
  assert.equal(chosen.querySelector('.card__why').textContent, '0W-20: სიბლანტე, რომელსაც ზეთის შემრჩევი ამ ძრავისთვის ასახელებს.');
  // The cards under it are all alike: name and price, brand and fits, availability.
  for (const row of [...filters.children, ...oils.children]) {
    assert.deepEqual(shape(row).map((name) => name.split(' ')[0]), ['card__name', 'card__price', 'card__meta', 'card__state']);
    assert.deepEqual(shape(row.querySelector('.card__state')), ['card__stock']);
  }
  assert.deepEqual(labels(holder.querySelectorAll('.card__fit')), ['Listed for this model and year', 'Chosen by specification'], 'said twice in all, not four times');
  // A list without a heading gets the line above it.
  const [oilA, oilB] = golden('oil_change').cards.slice(2).map((card) => ({ ...card, group: '' }));
  await ask('just the oils', reply({ answer: 'Two oils.', cards: [oilA, oilB], total: 2 }));
  assert.deepEqual(shape(newest().querySelector('.cards')), ['cards__fit', 'msg__cards']);
  assert.equal(newest().querySelectorAll('.card .card__fit, .card .card__why').length, 0);
  // The ground is the level and the reason together: one reason under two levels, or one
  // level with two reasons, is said card by card.
  await ask('two levels', reply({ answer: 'Two oils.', cards: [{ ...oilA, confidence: 'listed', reason: '' }, { ...oilB, confidence: 'check', reason: '' }], total: 2 }));
  assert.deepEqual(shape(newest().querySelector('.cards')), ['msg__cards']);
  assert.deepEqual(labels(newest().querySelectorAll('.card .card__fit')), ['Listed for this model and year', 'Needs checking']);
  await ask('two reasons', reply({ answer: 'Two oils.', cards: [oilA, { ...oilB, reason: 'სხვა მიზეზი.' }], total: 2 }));
  assert.deepEqual(shape(newest().querySelector('.cards')), ['msg__cards']);
  assert.deepEqual(labels(newest().querySelectorAll('.card .card__fit')), ['Chosen by specification', 'Chosen by specification']);
  assert.deepEqual(labels(newest().querySelectorAll('.card .card__why')), [oilA.reason, 'სხვა მიზეზი.']);
});

await test('"Show more" keeps that line right: said once while it holds, on the cards once it does not', async () => {
  const [, , oilA, oilB] = golden('oil_change').cards;
  const groups = [{ id: 'g2', label: 'ძრავის ზეთი', count: 4 }];
  const page = (cards, more) => reply({ answer: 'Oils.', groups, cards, total: 4, ...more });
  // One oil on the first page, a second on the same ground with the next one.
  await ask('oils', page([oilA], { next_offset: 1 }));
  const holder = newest().querySelector('.cards');
  const summary = holder.querySelector('.cards__fit');
  const said = summary.children;
  assert.deepEqual(shape(holder), ['cards__group', 'cards__fit', 'msg__cards']);
  server.pages.set(1, page([oilB], { offset: 1, next_offset: 2 }));
  await tap(newest().querySelector('.msg__more'));
  assert.deepEqual(shape(holder), ['cards__group', 'cards__fit', 'msg__cards']);
  assert.ok(sameNodes(holder.querySelector('.cards__fit').children, said), 'the line was not written again: the log would announce it twice');
  assert.equal(holder.querySelectorAll('.card .card__fit, .card .card__why').length, 0);
  // The third rests on another ground: now each card says its own, the earlier ones too.
  const rows = holder.querySelectorAll('.card');
  const other = { ...oilB, name: 'ძრავის ზეთი 5W-30 4L', confidence: 'check', reason: 'სიბლანტე გადაამოწმეთ.' };
  server.pages.set(2, page([other], { offset: 2, next_offset: 3 }));
  await tap(newest().querySelector('.msg__more'));
  assert.deepEqual(shape(holder), ['cards__group', 'msg__cards'], 'the shared line is gone');
  assert.equal(summary.isConnected, false);
  assert.ok(sameNodes(holder.querySelectorAll('.card').slice(0, 2), rows), 'the rows that were there were not redrawn');
  assert.deepEqual(labels(holder.querySelectorAll('.card .card__fit')), ['Chosen by specification', 'Chosen by specification', 'Needs checking']);
  assert.deepEqual(holder.querySelectorAll('.card').map((row) => shape(row).slice(3)), Array(3).fill(['card__state', 'card__fit', 'card__why']));
  // A fourth like the first two changes nothing back: the cards keep their own lines.
  server.pages.set(3, page([{ ...oilA, brand: 'PAKELO' }], { offset: 3, next_offset: null }));
  await tap(newest().querySelector('.msg__more'));
  assert.deepEqual(shape(holder), ['cards__group', 'msg__cards']);
  assert.equal(holder.querySelectorAll('.card .card__fit').length, 4);
  assert.equal(holder.querySelectorAll('.card .card__why').length, 4, 'one reason a card, none twice');
});

await test('a line never speaks for a whole list beside parts that stand under no heading', async () => {
  const [filterA, filterB, oil] = golden('oil_change').cards;
  const groups = [{ id: 'g1', label: 'ზეთის ფილტრი', count: 2 }];
  const fits = (holder) => holder.querySelectorAll('.card').map((row) => labels(row.querySelectorAll('.card__fit')));
  // A part outside every heading, with a level this page has no words for and no reason:
  // drawn right under the filters, the filters' line would read as its own.
  const loose = { ...oil, group: '', confidence: 'exact', reason: '' };
  await ask('filters and one more', reply({ answer: 'Parts.', groups, cards: [filterA, filterB, loose], total: 3 }));
  const holder = newest().querySelector('.cards');
  assert.deepEqual(shape(holder), ['cards__group', 'msg__cards', 'msg__cards']);
  assert.deepEqual(fits(holder), [['Listed for this model and year'], ['Listed for this model and year'], []]);
  // The same when that part only comes with "Show more": the line under the heading goes
  // back onto the cards it spoke for.
  await ask('filters', reply({ answer: 'Filters.', groups, cards: [filterA, filterB], total: 3, next_offset: 2 }));
  const later = newest().querySelector('.cards');
  assert.deepEqual(shape(later), ['cards__group', 'cards__fit', 'msg__cards']);
  assert.deepEqual(fits(later), [[], []]);
  server.pages.set(2, reply({ answer: 'Filters.', groups, cards: [loose], total: 3, offset: 2, next_offset: null }));
  await tap(newest().querySelector('.msg__more'));
  assert.deepEqual(shape(later), ['cards__group', 'msg__cards', 'msg__cards']);
  assert.deepEqual(fits(later), [['Listed for this model and year'], ['Listed for this model and year'], []]);
  // Two lists that both have a reason to give and no heading: each card says its own too.
  const [oilA, oilB] = golden('oil_change').cards.slice(2);
  await ask('two kinds', reply({ answer: 'Parts.', cards: [{ ...oilA, group: 'x' }, { ...oilB, group: 'y', confidence: 'check' }], total: 2 }));
  assert.deepEqual(shape(newest().querySelector('.cards')), ['msg__cards', 'msg__cards']);
  assert.deepEqual(fits(newest().querySelector('.cards')), [['Chosen by specification'], ['Needs checking']]);
});

await test('a level this page has no words for draws no label, never another level\'s', async () => {
  const [, , oil] = golden('oil_change').cards;
  // A server ahead of this page: its fifth level is unknown here.
  await ask('oils', reply({ answer: 'An oil.', cards: [{ ...oil, group: '', confidence: 'exact' }, { ...oil, group: '', confidence: 'exact', brand: 'PAKELO' }], total: 2 }));
  const bubble = newest();
  assert.equal(bubble.querySelector('.card__fit'), null);
  assert.equal(bubble.querySelectorAll('.card__why').length, 1, 'the reason is still said, once for both');
  assert.doesNotMatch(bubble.textContent, /Listed|Matches|Needs checking|Chosen|exact/);
  // And a page whose server has not got the new words yet: "spec" gets no label there.
  useStrings(OLD_STRINGS, { ...NEW_STRINGS, 'fit.spec': '' });
  await ask('oils again', golden('oil_change'));
  assert.deepEqual(labels(newest().querySelectorAll('.card__fit')), ['Listed for this model and year']);
  assert.equal(newest().querySelectorAll('.card__why').length, 1);
});

// ---------------------------------------------------------------- live results

await test('parts found while the turn runs stay in one bubble that becomes the reply', async () => {
  const offer = golden('offer');
  const running = (count) => ({ ...offer, status: 'RUNNING', answer: '', note: '', suggestions: [], cards: offer.cards.slice(0, count), total: count, checked: 1, checking: 2 });
  server.replies = [running(0), running(1), running(2), offer];
  await type('front pads and an oil filter');
  assert.ok(log.querySelector('.typing'), 'only the dots at first');
  assert.equal(log.querySelector('.msg--assistant[aria-busy]'), null);
  assert.equal(progress.textContent, '');
  await tick(150);
  const live = log.querySelector('.msg--assistant[aria-busy]');
  assert.ok(live, 'the first part opened a bubble');
  assert.equal(live.getAttribute('aria-busy'), 'true');
  assert.equal(log.querySelector('.typing'), null, 'which took the place of the dots');
  const lead = live.querySelector('p');
  assert.equal(lead.textContent, '1 found so far. Still checking the rest.');
  const firstRow = live.querySelector('.card');
  assert.ok(firstRow.classList.contains('card--in'));
  await tick(150);
  assert.equal(log.querySelector('.msg--assistant[aria-busy]'), live, 'the same bubble');
  assert.equal(live.querySelector('p'), lead, 'whose first line was changed, not replaced');
  assert.equal(lead.textContent, '2 found so far. Still checking the rest.');
  assert.deepEqual(live.querySelectorAll('.card').length, 2);
  assert.equal(live.querySelector('.card'), firstRow, 'the first row was not redrawn');
  const bubbles = messages('assistant').length;
  await tick(150);
  assert.equal(messages('assistant').length, bubbles, 'the reply is written into the bubble, not after it');
  assert.equal(newest(), live);
  assert.equal(live.getAttribute('aria-busy'), null);
  // The final text is a new paragraph: the log announces additions, not changes.
  assert.notEqual(live.querySelector('p'), lead);
  assert.equal(lead.isConnected, false);
  assert.equal(live.querySelector('p').textContent, offer.answer);
  assert.deepEqual(shape(live), ['sr-only', 'p', 'msg__note', 'cards', 'next']);
  assert.equal(live.querySelectorAll('.card').length, 3);
  assert.equal(live.querySelector('.card'), firstRow, 'the rows the reader was looking at are the same nodes');
  assert.equal(progress.textContent, '');
  assert.equal(sendButton.disabled, true);
});

await test('cards the server reordered are redrawn without the entrance', async () => {
  const [padA, padB] = golden('offer').cards;
  const running = (cards) => reply({ status: 'RUNNING', answer: '', cards, total: cards.length });
  server.replies = [running([padA, padB]), running([padB, padA]), reply({ answer: 'Two pads.', cards: [padB, padA], total: 2 })];
  await type('pads');
  const before = log.querySelectorAll('.card');
  assert.deepEqual(before.map((row) => row.classList.contains('card--in')), [true, true]);
  await tick(150);
  const after = log.querySelectorAll('.card');
  assert.deepEqual(labels(after.map((row) => row.querySelector('.card__stock'))), ['At the warehouse, 1-2 hours', 'In stock']);
  assert.deepEqual(after.map((row) => row.classList.contains('card--in')), [false, false]);
  assert.ok(!before.includes(after[0]));
  await tick(150);
  assert.ok(sameNodes(log.querySelectorAll('.card'), after), 'the reply kept the rows it already showed');
});

await test('the line under the log says what is being done, in stages', async () => {
  const running = (changes = {}) => reply({ status: 'RUNNING', answer: '', ...changes });
  server.replies = [running()];
  await type('front pads');
  assert.equal(progress.textContent, '', 'the dots are enough for the first second');
  await tick(1100);
  assert.equal(progress.textContent, '');
  await tick(150);
  assert.equal(progress.textContent, 'Looking it up...');
  // Once the car is known and the sources are counted, both are named.
  server.replies = [running({ car: 'Toyota Camry 2015', checked: 2, checking: 5 })];
  await tick(1000);
  assert.equal(progress.textContent, 'Checking stock for Toyota Camry 2015 2/5');
  assert.equal(carBar.hidden, false, 'the car is shown while the turn still runs');
  await tick(14000);
  assert.equal(progress.textContent, 'Still checking. This can take up to a minute.');
  server.replies = [reply({ answer: 'Here they are.', car: 'Toyota Camry 2015' })];
  await tick(1000);
  assert.equal(progress.textContent, '');
  assert.equal(newest().querySelector('p').textContent, 'Here they are.');
});

await test('once every search is back the line says the finds are being gone through', async () => {
  // The assistant engine searches in a moment and then reads what it found for seconds:
  // the count alone ("1/1") would look finished while the visitor still waits.
  const running = (changes = {}) => reply({ status: 'RUNNING', answer: '', ...changes });
  server.replies = [running({ checked: 0, checking: 2 })];
  await type('oil change for an E 400');
  await tick(1300);
  assert.equal(progress.textContent, 'Looking it up... 0/2');
  server.replies = [running({ checked: 1, checking: 2 })];
  await tick(1000);
  assert.equal(progress.textContent, 'Looking it up... 1/2', 'one search is still out');
  server.replies = [running({ checked: 2, checking: 2 })];
  await tick(1000);
  assert.equal(progress.textContent, 'Checking the parts we found...');
  // It searches again after reading: the line goes back to the count.
  server.replies = [running({ checked: 2, checking: 3, car: 'Mercedes-Benz E 400 2015' })];
  await tick(1000);
  assert.equal(progress.textContent, 'Checking stock for Mercedes-Benz E 400 2015 2/3');
  server.replies = [running({ checked: 3, checking: 3, car: 'Mercedes-Benz E 400 2015' })];
  await tick(1000);
  assert.equal(progress.textContent, 'Checking the parts we found...');
  await tick(12000);
  assert.equal(progress.textContent, 'Still checking. This can take up to a minute.', 'a slow turn still says so');
  // A page whose server has not got the new words yet keeps the count.
  await fresh();
  useStrings(OLD_STRINGS);
  server.replies = [running({ checked: 2, checking: 2 })];
  await type('pads');
  await tick(1300);
  assert.equal(progress.textContent, 'Checking stock... 2/2');
});

await test('a new chat drops a turn that is still running', async () => {
  server.replies = [reply({ status: 'RUNNING', answer: '' })];
  await type('front pads');
  assert.ok(log.querySelector('.typing'));
  server.replies = [reply({ answer: 'A late answer.' })];
  dialog.querySelector('[data-chat-new]').click();
  await tick(2000);
  assert.deepEqual(shape(log), ['msg msg--assistant', 'starters'], 'only the greeting and the starters');
  assert.doesNotMatch(log.textContent, /late answer/);
  assert.equal(progress.textContent, '');
  assert.ok(calls.some((call) => call.method === 'DELETE' && call.url === `${BASE}/conversation-1`), 'the old conversation was given up');
  assert.equal(sendButton.disabled, true);
});

// --------------------------------------------------------------------- scroll

await test('a long reply is shown from its top, a short one lands at the end', async () => {
  await ask('front pads and a filter', golden('offer'));
  const tall = newest();
  assert.ok(log.tall(tall) > log.clientHeight, 'this reply is taller than the log');
  assert.equal(tall.offsetTop, 128, 'under the greeting and the visitor\'s message, by this test\'s layout');
  assert.equal(log.scrollTop, tall.offsetTop - 12, 'its top is 12 px under the top of the log');
  assert.ok(log.scrollTop < log.scrollHeight - log.clientHeight, 'which is not the end');
  assert.equal(jump.hidden, true);
  // A short reply fits: it lands at the end of the log, like any chat.
  await ask('thanks', reply({ answer: 'You are welcome.' }));
  const end = log.scrollHeight - log.clientHeight;
  assert.ok(end > 0 && newest().offsetTop - 12 > end, 'the log scrolls, and the reply is in its last screen');
  assert.equal(log.scrollTop, end);
  assert.equal(jump.hidden, true);
});

await test('a reader who scrolled up is not moved: a button says there is a new reply', async () => {
  await ask('front pads and a filter', golden('offer'));
  await ask('and the rear ones', golden('offer'));
  server.replies = [reply({ status: 'RUNNING', answer: '' }), golden('offer', { answer: 'A third list.' })];
  await type('once more');
  // While the assistant works the reader goes back up to an earlier reply.
  scrollTo(40);
  await tick(150);
  assert.equal(newest().querySelector('p').textContent, 'A third list.');
  assert.equal(log.scrollTop, 40, 'the view did not move');
  assert.equal(jump.hidden, false);
  // The button takes them to the top of that reply and steps aside.
  jump.focus();
  await tap(jump);
  assert.equal(log.scrollTop, newest().offsetTop - 12);
  assert.equal(jump.hidden, true);
  assert.equal(document.activeElement, log, 'focus did not stay on a hidden button');
  // Scrolling to the end by hand hides it too.
  server.replies = [reply({ status: 'RUNNING', answer: '' }), reply({ answer: 'A short one.' })];
  await type('and again');
  scrollTo(0);
  await tick(150);
  assert.equal(jump.hidden, false);
  assert.equal(log.scrollTop, 0);
  scrollTo(log.scrollHeight);
  assert.equal(jump.hidden, true);
  // What they send themselves is always brought into view.
  scrollTo(0);
  await ask('thanks', reply({ answer: 'Any time.' }));
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
});

await test('the keyboard resizing the sheet keeps the reply in view, unless the reader moved', async () => {
  await fresh({ onPhone: true });
  await ask('front pads and a filter', golden('offer'));
  const top = newest().offsetTop - 12;
  assert.equal(log.scrollTop, top);
  // The keyboard takes half the sheet.
  viewport.height = 400;
  log.clientHeight = 120;
  log.scrolled = 9999;
  viewport.dispatchEvent(new FakeEvent('resize'));
  assert.equal(dialog.style.height, '400px');
  assert.equal(log.scrollTop, top, 'back on the top of the reply, not at the end');
  scrollTo(30);
  viewport.height = 800;
  log.clientHeight = 320;
  viewport.dispatchEvent(new FakeEvent('resize'));
  assert.equal(log.scrollTop, 30, 'someone reading further up is left there');
  viewport.height = 800;
});

// ------------------------------------------------------------------- car line

await test('the car line names the car and lets the visitor change it without sending anything', async () => {
  assert.equal(carBar.hidden, true);
  await ask('front pads for a Camry', golden('question'));
  assert.equal(carBar.hidden, false);
  assert.equal(carBar.querySelector('.chat__car-name').textContent, 'Toyota Camry 2015');
  assert.equal(carChange.textContent, 'Change');
  const sent = posted().length;
  await tap(carChange);
  assert.equal(posted().length, sent, '"Change" sends nothing');
  assert.equal(carBar.hasAttribute('data-editing'), true);
  assert.equal(carChange.textContent, 'Cancel');
  assert.equal(input.placeholder, 'Type another car or a VIN');
  assert.equal(document.activeElement, input);
  await tap(carChange);
  assert.equal(carBar.hasAttribute('data-editing'), false);
  assert.equal(carChange.textContent, 'Change');
  assert.equal(input.placeholder, 'Pick above or type your answer', 'back to what the chat was waiting for');
  // The next message is an ordinary one, and ends the change.
  await tap(carChange);
  await ask('Honda Fit 2008', reply({ answer: 'Honda Fit 2008. Which part?', car: 'Honda Fit 2008' }));
  assert.deepEqual(posted().at(-1).body, { message: 'Honda Fit 2008', language: 'auto' });
  assert.equal(carBar.hasAttribute('data-editing'), false);
  assert.equal(carChange.textContent, 'Change');
  assert.equal(carBar.querySelector('.chat__car-name').textContent, 'Honda Fit 2008');
  assert.equal(input.placeholder, 'Which part do you need?');
  // A reply that names no car leaves the line as it is; only a new chat clears it.
  await ask('thanks', reply({ answer: 'Any time.' }));
  assert.equal(carBar.hidden, false);
  await fresh();
  assert.equal(carBar.hidden, true);
  assert.equal(carBar.querySelector('.chat__car-name').textContent, '');
  assert.equal(input.placeholder, PAGE['chat.placeholder']);
});

// ------------------------------------------------------------------------ VIN

await test('a VIN is masked in the bubble, sent whole in the body, and explained once', async () => {
  await ask(`pads for ${VIN} please`, reply({ answer: 'Toyota Camry 2015.', car: 'Toyota Camry 2015' }));
  const mine = messages('user')[0].querySelector('p').textContent;
  assert.equal(mine, 'pads for ••••0001 please');
  assert.equal(posted()[0].body.message, `pads for ${VIN} please`);
  assert.ok(calls.every((call) => !call.url.includes(VIN)), 'never in a URL');
  assert.doesNotMatch(log.textContent, new RegExp(VIN));
  const notices = () => log.querySelectorAll('.chat__sys');
  assert.deepEqual(labels(notices()), ['We hide the VIN on screen. It is only used to identify your car.']);
  assert.deepEqual(shape(log).slice(1, 3), ['msg msg--user', 'chat__sys'], 'right under the message it is about');
  // Lower case, and with a Georgian ending attached, it is still a VIN; the note is not repeated.
  await ask(`${VIN.toLowerCase()}ზე ზეთის ფილტრი`, reply({ answer: 'ზეთის ფილტრი.' }));
  assert.equal(messages('user')[1].querySelector('p').textContent, '••••0001ზე ზეთის ფილტრი');
  assert.equal(posted()[1].body.message, `${VIN.toLowerCase()}ზე ზეთის ფილტრი`);
  assert.equal(notices().length, 1);
  // Sixteen or eighteen characters are not a VIN.
  await ask('code 4T1BF1FK5FU00000 and 4T1BF1FK5FU0000012', reply());
  assert.equal(messages('user')[2].querySelector('p').textContent, 'code 4T1BF1FK5FU00000 and 4T1BF1FK5FU0000012');
});

// ------------------------------------------------------------------- protocol

await test('an expired conversation starts again, says so, and forgets the car and the number', async () => {
  await ask('pads for a Camry', golden('question'));
  assert.equal(carBar.hidden, false);
  let refused = 0;
  server.refuse = (call) => {
    if (!call.url.endsWith('/messages') || refused) return null;
    refused += 1;
    return problem(404, 'CONVERSATION_EXPIRED');
  };
  server.replies = [reply({ answer: 'Which car is it for?' })];
  await tap(newest().querySelector('.ask .chip'));
  const [first, second] = posted().slice(-2);
  assert.equal(first.url, `${BASE}/conversation-1/messages`);
  assert.deepEqual(first.body, { message: 'წინა', language: 'auto', choice: 1 });
  assert.equal(second.url, `${BASE}/conversation-2/messages`);
  assert.deepEqual(second.body, { message: 'წინა', language: 'auto' }, 'the new conversation never asked that question');
  assert.deepEqual(labels(log.querySelectorAll('.chat__sys')), [NEW_STRINGS['chat.expired']]);
  assert.equal(carBar.hidden, true);
  assert.equal(newest().querySelector('p').textContent, 'Which car is it for?');
  assert.equal(messages('user').length, 2);
});

// The server gets ready when a conversation opens, so the widget opens it with the first
// words of a message and not with the message itself (found on a live walk: the first
// reply of a chat waited for that as well).
const opened = () => calls.filter((call) => call.method === 'POST' && call.url === BASE).length;
const write = (words) => {
  input.value = words;
  input.dispatchEvent(new FakeEvent('input'));
};

await test('the first words open the conversation ahead of the message, and sending uses it', async () => {
  write('   ');
  await tick(0);
  assert.equal(opened(), 0, 'blank text opens nothing');
  write('C');
  write('Camry 2015');
  await tick(0);
  assert.equal(opened(), 1, 'the first words open one conversation, later ones none');
  assert.equal(posted().length, 0, 'nothing is sent by writing');
  server.replies = [reply({ answer: 'Front or rear?' })];
  form.requestSubmit();
  await tick(0);
  assert.equal(opened(), 1, 'sending uses the conversation that was opened');
  assert.equal(posted()[0].url, `${BASE}/conversation-1/messages`);
  assert.equal(newest().querySelector('p').textContent, 'Front or rear?');
  // Words handed over by the part search are about to be sent as well.
  await fresh();
  chat.close();
  chat.open({ opener: launcher, prefill: 'front pads for a Camry' });
  await tick(0);
  assert.equal(opened(), 1);
  assert.equal(posted().length, 0, 'a prefill is still never sent by itself');
});

await test('a conversation that could not be opened ahead is asked for again with the message', async () => {
  let refused = 0;
  server.refuse = (call) => {
    if (call.method !== 'POST' || call.url !== BASE || refused) return null;
    refused += 1;
    return problem(429, 'RATE_LIMITED');
  };
  server.replies = [reply({ answer: 'Which car is it for?' })];
  await type('pads');
  assert.equal(opened(), 2, 'refused while writing, asked for again on sending');
  assert.equal(log.querySelector('.msg--error'), null, 'the visitor never saw the first refusal');
  assert.equal(newest().querySelector('p').textContent, 'Which car is it for?');
  // One that was opened ahead and expired before anything was sent held nothing of the
  // visitor's: it is replaced without the "started again" notice.
  await fresh();
  write('pads');
  await tick(0);
  let lost = 0;
  server.refuse = (call) => {
    if (!call.url.endsWith('/messages') || lost) return null;
    lost += 1;
    return problem(404, 'CONVERSATION_EXPIRED');
  };
  server.replies = [reply({ answer: 'Which car is it for?' })];
  form.requestSubmit();
  await tick(0);
  assert.equal(posted().at(-1).url, `${BASE}/conversation-2/messages`);
  assert.equal(log.querySelector('.chat__sys'), null, 'no notice: nothing was lost');
  assert.equal(newest().querySelector('p').textContent, 'Which car is it for?');
  assert.equal(messages('user').length, 1);
});

await test('a new chat gives up a conversation that is still opening and a message that waits for it', async () => {
  write('Camry');
  dialog.querySelector('[data-chat-new]').click(); // before the server has answered
  await tick(0);
  assert.ok(calls.some((call) => call.method === 'DELETE' && call.url === `${BASE}/conversation-1`), 'the late conversation is given up');
  server.replies = [reply({ answer: 'Which car is it for?' })];
  await type('pads');
  assert.equal(posted().at(-1).url, `${BASE}/conversation-2/messages`, 'the new chat has a conversation of its own');
  // A message sent while its conversation is still opening, then overtaken by a new chat,
  // must not land in that new chat.
  await fresh();
  write('pads for a Camry');
  form.requestSubmit();
  dialog.querySelector('[data-chat-new]').click();
  await tick(0);
  assert.equal(posted().length, 0, 'the overtaken message was not sent');
  assert.deepEqual(shape(log), ['msg msg--assistant', 'starters'], 'the new chat is empty');
  assert.equal(sendButton.disabled, true);
});

await test('markup from the server is shown as text and never becomes elements', async () => {
  const hostile = '<img src=x onerror=alert(1)><script>alert(2)</script>';
  await ask(hostile, reply({
    answer: hostile, note: hostile, car: hostile,
    question: { options: [{ label: hostile, value: 'a' }, { label: 'b', value: hostile }] },
    suggestions: [{ label: hostile, value: 'c' }],
    groups: [{ id: 'g1', label: hostile, count: 1 }],
    cards: [{ name: hostile, fits: hostile, brand: hostile, price: 10, availability: 'in_stock', group: 'g1', reason: hostile, confidence: 'check' }],
    total: 1,
  }));
  const bubble = newest();
  for (const selector of ['p', '.msg__note', '.ask .chip', '.next .chip', '.card__name', '.card__why']) {
    assert.equal(bubble.querySelector(selector).textContent, hostile, selector);
  }
  assert.equal(bubble.querySelector('.cards__group').words, hostile);
  assert.equal(carBar.querySelector('.chat__car-name').textContent, hostile);
  assert.equal(messages('user')[0].querySelector('p').textContent, hostile);
  assert.deepEqual(dialog.querySelectorAll('img, script, iframe'), []);
  // A level or a state the widget does not know draws no label and falls back to "ask".
  await ask('more', reply({ cards: [{ name: 'x', fits: '', brand: '', price: null, availability: hostile, group: '', reason: '', confidence: hostile }], total: 1 }));
  const row = newest().querySelector('.card');
  assert.equal(row.dataset.availability, 'unknown');
  assert.equal(row.querySelector('.card__stock').textContent, 'Ask the shop');
  assert.equal(row.querySelector('.card__fit'), null);
  assert.equal(row.querySelector('.card__why'), null);
});

// ------------------------------------------------------------- older servers

await test('a server that sends only quick replies still gets buttons, sent as plain text', async () => {
  const card = { name: 'Brake pad set', fits: 'TOYOTA CAMRY 2012-2017', brand: 'SATO', price: 45.5, availability: 'soon' };
  await ask('pads for a Camry', older({ answer: 'Front or rear?', quick_replies: [{ label: 'Front', value: 'front pads' }, { label: 'Rear', value: 'rear pads' }], cards: [card], total: 1 }));
  const bubble = newest();
  assert.deepEqual(shape(bubble), ['sr-only', 'p', 'ask', 'ask__hint', 'cards']);
  assert.deepEqual(labels(bubble.querySelectorAll('.ask .chip')), ['Front', 'Rear']);
  assert.equal(carBar.hidden, true, 'it names no car');
  // Its cards are one plain list: no heading, no fit label, no reason.
  assert.deepEqual(shape(bubble.querySelector('.cards')), ['msg__cards']);
  assert.deepEqual(shape(bubble.querySelector('.card')), ['card__name', 'card__price', 'card__meta', 'card__state']);
  assert.deepEqual(shape(bubble.querySelector('.card__state')), ['card__stock']);
  server.replies = [older({ answer: 'Here are the front pads.' })];
  await tap(bubble.querySelector('.ask .chip'));
  assert.deepEqual(posted().at(-1).body, { message: 'front pads', language: 'auto' }, 'no number: it never numbered them');
  assert.deepEqual(bubble.querySelectorAll('.ask .chip').map((button) => button.getAttribute('aria-pressed')), ['true', null]);
  assert.deepEqual(labels(newest().querySelectorAll('.next .chip')), ['Open the part search', 'Call']);
  assert.doesNotMatch(log.textContent, /undefined|null/);
});

await test('with an older string table nothing reads "undefined" and nothing new is half drawn', async () => {
  useStrings(OLD_STRINGS);
  dialog.querySelector('[data-chat-new]').click();
  await tick(0);
  assert.deepEqual(shape(log), ['msg msg--assistant'], 'no starters: a missing string draws no button');
  const offer = golden('offer');
  server.replies = [{ ...offer, status: 'RUNNING', answer: '', cards: offer.cards.slice(0, 1), total: 1, checking: 2, checked: 1 }];
  await type(`pads for ${VIN}`);
  await tick(2000);
  assert.equal(progress.textContent, 'Checking stock... 1/2', 'the older wording, still with the count');
  assert.equal(log.querySelector('.msg--assistant[aria-busy] p').textContent, 'Still checking. Found so far:');
  const seen = [log.textContent, progress.textContent, input.placeholder];
  await tick(20000);
  seen.push(progress.textContent, log.textContent);
  server.replies = [{ ...offer, next_offset: 3, total: 9, question: { options: offer.suggestions } }];
  await tick(1000);
  const bubble = newest();
  assert.equal(bubble.querySelector('p').textContent, offer.answer);
  // The answers are still a labelled group, by the question itself.
  const group = bubble.querySelector('.ask');
  assert.equal(group.getAttribute('aria-labelledby'), bubble.querySelector('p').id);
  assert.equal(group.getAttribute('aria-label'), null);
  assert.equal(bubble.querySelector('.ask__hint'), null);
  assert.deepEqual(shape(bubble.querySelector('.card__state')), ['card__stock'], 'no fit label without its words');
  assert.equal(log.querySelector('.chat__sys'), null, 'no notice without its words');
  // "Show more" fails, the car is changed, a message fails: all with the old words.
  server.pages.set(3, problem(500));
  await tap(bubble.querySelector('.msg__more'));
  assert.equal(bubble.querySelector('.msg__fail').textContent, 'No answer came back. Please try again.');
  await tap(carChange);
  seen.push(carChange.textContent, input.placeholder);
  assert.equal(carBar.hasAttribute('data-editing'), true);
  server.refuse = () => {
    throw new TypeError('fetch failed');
  };
  window.navigator.onLine = false;
  await type('again');
  seen.push(log.textContent, progress.textContent, input.placeholder, carChange.textContent, counter.textContent);
  for (const node of dialog.querySelectorAll('[aria-label], [aria-labelledby], [aria-describedby], [placeholder]')) {
    for (const name of ['aria-label', 'aria-labelledby', 'aria-describedby', 'placeholder']) seen.push(node.getAttribute(name) || '');
  }
  for (const words of seen) assert.doesNotMatch(words, /undefined|null|\{count\}|\{car\}/);
  assert.equal(input.placeholder, PAGE['chat.placeholder']);
});

// ------------------------------------------------------------------- composer

await test('the composer counts only near the limit and sends only when there is something to send', async () => {
  const write = (words) => {
    input.value = words;
    input.dispatchEvent(new FakeEvent('input'));
  };
  write('   ');
  assert.equal(sendButton.disabled, true, 'blank is nothing');
  write('x'.repeat(399));
  assert.equal(sendButton.disabled, false);
  assert.equal(counter.hidden, true);
  write('x'.repeat(400));
  assert.equal(counter.hidden, false);
  assert.equal(counter.textContent, '400/500');
  assert.equal(counter.hasAttribute('data-near'), false);
  write('x'.repeat(480));
  assert.equal(counter.hasAttribute('data-near'), true);
  write('front pads');
  assert.equal(counter.hidden, true);
  // Enter sends, Shift+Enter and an IME's Enter do not.
  const key = (init) => {
    const event = new FakeEvent('keydown', { key: 'Enter', shiftKey: false, isComposing: false, ...init });
    input.dispatchEvent(event);
    return event;
  };
  assert.equal(key({ shiftKey: true }).defaultPrevented, false);
  assert.equal(key({ isComposing: true }).defaultPrevented, false);
  assert.equal(posted().length, 0);
  server.replies = [reply({ status: 'RUNNING', answer: '' }), reply({ answer: 'Front or rear?' })];
  assert.equal(key({}).defaultPrevented, true);
  await tick(0);
  assert.equal(posted().length, 1);
  assert.equal(input.value, '', 'the composer is emptied');
  // While the turn runs a second message cannot be sent, typed or not.
  write('and the rear ones');
  assert.equal(sendButton.disabled, true);
  form.requestSubmit();
  await tick(0);
  assert.equal(posted().length, 1);
  assert.equal(input.value, 'and the rear ones', 'what was typed meanwhile is kept');
  await tick(150);
  assert.equal(sendButton.disabled, false, 'and can be sent once the reply is in');
});

// -----------------------------------------------------------------------------

if (failures.length) {
  console.error(`${failures.length} failed, ${passed} passed\n`);
  for (const failure of failures) console.error(`not ok  ${failure}\n`);
  process.exit(1);
}
console.log(`ok  ${passed} chat widget tests`);
