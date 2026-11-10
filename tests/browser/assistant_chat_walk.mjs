// A live walk through both chat screens with the assistant engine behind them.
//
// It opens the public chat widget and the staff chat in headless Chrome, types the test
// conversations below as a customer would, taps the answer buttons the assistant offers,
// times every reply, checks the page after each one and saves screenshots.
//
// IT CALLS THE REAL MODEL. Every message is a real turn of the assistant engine on the
// real catalogue: about $0.03 a turn on Sonnet 5.5, a little over $1 for the whole walk (35
// turns or so). It is not part of any test run; start it by hand.
//
// How to run
//   1. Start a server of your own on a spare port, with the staff login off (accepted on
//      loopback only) and both chat logs off, so the test turns stay out of the shop's logs:
//        AUTOMOTIVE_STAFF_AUTH=false AUTOMOTIVE_CHAT_LOG_PATH= AUTOMOTIVE_SITE_CHAT_LOG_PATH= \
//          uv run python -m automotive --port 8799
//   2. node tests/browser/assistant_chat_walk.mjs
//      It refuses port 8767, where the shop's own server runs.
//
// Settings (environment)
//   WALK_ORIGIN        the server to walk (default http://127.0.0.1:8799)
//   WALK_OUT           folder for the screenshots and report.json (default: the folder
//                      assistant-chat-walk in the system's temporary folder)
//   WALK_SCREENS       screens to run, comma-separated: phone-ka, desktop-ka, phone-en, staff
//   WALK_TALKS         conversations to run, comma-separated: a, b, c, d, e, en, f, g, vin, form
//   WALK_DRY=1         answer every chat request from tests/fixtures/chat_ui/turns.json
//                      instead of the engine: no model call and no cost. For trying the
//                      walk itself; the pages still come from the server.
//   PLAYWRIGHT_MODULE  Playwright's index.mjs (Playwright is not a dependency of this
//                      repository; default ~/.cache/mb-brag-6b61bcc/node_modules/playwright)
//   CHROME_BIN         the browser to launch (default /usr/bin/google-chrome)
//
// A typed message is sent three seconds after it is put in the composer, about as fast as
// anyone writes one, and the reply is timed from the sending to the finished reply on screen.
//
// Checked in the page after every reply
//   - a reply that asks has a visible group of at least two answer buttons under its text;
//   - a tapped answer is marked and its whole group switched off, at once and afterwards,
//     and the tap sent the number of that answer;
//   - the car line shows the car the server named;
//   - every part card has a name, a price and an availability label; its confidence label
//     is the level the server sent (none for a level the page does not know) and its reason
//     is said once: on the card, or in one line for its whole group when every part of the
//     group rests on the same ground;
//   - parts that come in groups have their headings;
//   - a note or a warning about old stock figures from the server is on screen;
//   - nothing overflows sideways and the composer stays on screen;
//   - no "undefined", raw key name, row handle of the engine (r3) or word of the other
//     language in the interface;
//   - no console error, failed request or Content-Security-Policy report.
// The process ends with status 1 when a check failed. Which parts and which question a
// reply holds is the engine's work and is only recorded here; `--evaluate-assistant`
// judges that.
//
// Looked at once more, and printed as CHECK lines that do not change the status
//   - oil (conversation b): the oils come as "chosen by specification" with the reason in
//     one line for their group, the filters under one line of their own;
//   - more (public widget): "Show more" is pressed under every reply that has it, and the
//     page is checked again with the parts it brought;
//   - dont-know (conversation f): the "not sure" answer of a question is tapped, and the
//     variants are expected in groups, each "needs checking" with a reason;
//   - vin (conversation vin): a VIN is typed into the public widget. It is made up when the
//     script runs (the shape of a 2019 Jeep Cherokee, a serial number no car has, a correct
//     check digit) and is kept out of report.json; the server asks the vehicle decoder
//     about it as it would for a customer's. The bubble has to show it masked and the
//     reply has to leave a way on;
//   - form (conversation form, staff): the vehicle form holds one car and the message
//     names another: the reply has to be about the car in the message.

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const list = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const ORIGIN = (process.env.WALK_ORIGIN || 'http://127.0.0.1:8799').replace(/\/+$/, '');
const OUT = path.resolve(process.env.WALK_OUT || path.join(tmpdir(), 'assistant-chat-walk'));
const DRY = process.env.WALK_DRY === '1';
const ONLY_SCREENS = list(process.env.WALK_SCREENS);
const ONLY_TALKS = list(process.env.WALK_TALKS);
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE
  || path.join(homedir(), '.cache/mb-brag-6b61bcc/node_modules/playwright/index.mjs');
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';
const FIXTURES = path.join(path.dirname(fileURLToPath(import.meta.url)), '../fixtures/chat_ui/turns.json');
const REPLY_PATIENCE_MS = 100_000;
const USD_PER_TURN = 0.03;
// The public site lets one address send four messages at once and then one every ten
// seconds; the staff API takes ten a minute. The walk stays under both.
const GAP_MS = { widget: 10_500, staff: 6_500 };
// How long a typed message stays in the composer before it is sent. Both screens open the
// conversation with the first letters, and the server uses that time to get ready.
const TYPING_MS = 3_000;
const NOT_SURE = /არ ვიცი|არ მახსოვს|არ ვარ დარწმუნებული|not sure|do(?:n.t| not) know/i;
// The confidence levels both screens have a label for.
const LEVELS = ['confirmed', 'listed', 'check', 'spec'];
// Where a message carries a VIN. The VIN itself is made when the script runs.
const VIN_MARK = '{VIN}';

/**
 * A VIN that belongs to no car, for the conversation that types one: the shape of a 2019
 * Jeep Cherokee, a serial number far above any that was built with three random digits at
 * its end, and the check digit worked out for it (ISO 3779, as North America uses it).
 */
function syntheticVin() {
  const values = { A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, J: 1, K: 2, L: 3, M: 4, N: 5, P: 7, R: 9, S: 2, T: 3, U: 4, V: 5, W: 6, X: 7, Y: 8, Z: 9 };
  const weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];
  const letters = [...`1C4PJMDX0KD987${String(Math.floor(Math.random() * 1000)).padStart(3, '0')}`];
  const sum = letters.reduce((total, letter, place) => total + (values[letter] ?? Number(letter)) * weights[place], 0);
  letters[8] = sum % 11 === 10 ? 'X' : String(sum % 11);
  return letters.join('');
}
const VIN_VALUE = syntheticVin();

// The conversations. `say` types a message; `tap` presses an answer button of the open
// question: the first one whose text matches, with 'any' the first that is not a "not
// sure", with 'escape' the "not sure" itself. `ifAsked` makes the step optional: it runs
// only when a question is open. `car` is what the car line has to show from step `carFrom`
// on. `form` fills the staff page's vehicle form before the first message. `check` names
// what is looked at once more when the conversation is over (see CHECKS).
const TALKS = {
  a: { car: 'camry', carFrom: 1, steps: [{ say: 'Camry 2015, წინა ხუნდები' }, { tap: 'any', ifAsked: true }] },
  b: {
    car: 'E\\s?-?400|E-?Class|Mercedes|მერსედეს', carFrom: 1, check: 'oil',
    steps: [{ say: 'მერსედესზე ე400 კუპეზე 2015 წლიანზე 3.0 ძრავით 2 ტურბინით მინდა ზეთის შეცვლა' },
      { tap: 'any', ifAsked: true }],
  },
  c: {
    car: 'E-?Class|Mercedes|მერსედეს|W212', carFrom: 2,
    steps: [{ say: 'ხუნდები მინდა' }, { say: 'მერსედესი ე კლასი 2015' }, { tap: 'ორივე' }],
  },
  d: { steps: [{ say: 'გაქვთ 31835AA030?' }] },
  e: {
    car: 'BMW', carFrom: 2,
    steps: [{ say: 'გამარჯობა' }, { say: 'BMW 5 სერია 2012 ჰაერის ფილტრი' }, { tap: 'any' }],
  },
  en: { car: 'camry', carFrom: 1, steps: [{ say: 'front brake pads for a Toyota Camry 2015' }, { tap: 'any', ifAsked: true }] },
  f: { car: 'BMW', carFrom: 1, check: 'dont-know', steps: [{ say: 'BMW 5 სერია 2012 ჰაერის ფილტრი' }, { tap: 'escape' }] },
  // Three things at once, so that the reply has more parts than its first page of five.
  g: { car: 'camry', carFrom: 1, steps: [{ say: 'Camry 2015: წინა ხუნდები, უკანა ხუნდები და ზეთის ფილტრი' }, { tap: 'any', ifAsked: true }] },
  vin: { check: 'vin', steps: [{ say: VIN_MARK }] },
  form: {
    car: 'camry', carFrom: 1, check: 'form', form: { make: 'Honda', model: 'Fit', model_year: '2010' },
    steps: [{ say: 'Toyota Camry 2015 წინა ხუნდები' }, { tap: 'any', ifAsked: true }],
  },
};

const SCREENS = [
  { name: 'phone-ka', kind: 'widget', path: '/', width: 390, height: 844, mobile: true, language: 'ka', talks: ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'vin'] },
  { name: 'desktop-ka', kind: 'widget', path: '/', width: 1440, height: 900, language: 'ka', talks: ['a', 'b', 'c', 'd', 'e'] },
  { name: 'phone-en', kind: 'widget', path: '/en', width: 390, height: 844, mobile: true, language: 'en', talks: ['en'] },
  { name: 'staff', kind: 'staff', path: '/staff', width: 1440, height: 900, language: 'ka', talks: ['a', 'b', 'c', 'd', 'e', 'form'] },
];

// What the walk needs to find on each screen.
const PARTS = {
  widget: {
    input: '#chat-input', fresh: '[data-chat-new]', reply: '#chat .msg--assistant', answers: '.ask button',
    more: '.msg__more', card: '.card',
  },
  staff: {
    input: '#message', fresh: '#new-chat',
    reply: '#chat-result .chat-message.assistant:not(.typing):not(.live)',
    answers: '.quick-replies:not(.next-steps) button',
    formToggle: '#direct-search > summary', panel: '#selected-vehicle',
  },
};

/**
 * Runs in the page before its own scripts: the reading and checking half of the walk.
 * Everything it reports is read from the document as a visitor has it on screen.
 */
function probe() {
  const SELECTORS = {
    widget: {
      root: '#chat', log: '.chat__log', reply: '.msg--assistant', failed: '.msg--error', lead: 'p',
      answers: '.ask', next: '.next', holder: '.cards', card: '.card', name: '.card__name',
      price: '.card__price', stock: '.card__stock', fit: '.card__fit', why: '.card__why',
      line: '.cards__fit', heading: '.cards__group', carBar: '.chat__car', carName: '.chat__car-name',
      progress: '.chat__progress', form: '.chat__form', send: '.chat__send',
      waiting: '.typing, [aria-busy]', details: null, mine: '.msg--user',
      // Text that is the catalogue's or the visitor's own, in whatever letters it comes.
      own: '.card__name, .card__meta, .chat__car-name, .msg--user',
    },
    staff: {
      root: '.chat-panel', log: '#chat-result', reply: '.chat-message.assistant:not(.typing):not(.live)',
      failed: '.failed', lead: '.message-text', answers: '.quick-replies:not(.next-steps)',
      next: '.next-steps', holder: '.chat-cards', card: '.chat-part', name: 'h4', price: '.price',
      stock: '.availability', fit: '.fit', why: '.reason', line: '.chat-group-fit', heading: '.chat-group',
      carBar: '#chat-car', carName: '#chat-car-label', progress: '#chat-progress',
      form: '#chat-form', send: '#chat-send', waiting: '.typing, .live', details: '.part-details',
      mine: '.chat-message.user',
      own: '.chat-part h4, .chat-part .code, #chat-car-label, .chat-message.user',
    },
  };
  const RAW = /\bundefined\b|\bnull\b|\bNaN\b|\[object |\{\w+\}|\b(?:js\.)?(?:chat|fit|avail|search)\.\w+|\b[a-z]+_[a-z_]+\b|\b[A-Z]{2,}_[A-Z0-9_]+\b/;
  const VALUES = /\b(?:COMPLETE|PARTIAL|FAILED|RUNNING|UNKNOWN|PROVISIONAL|IN_STORE|CLARIFICATION)\b/;
  // The engine names a catalogue row by a handle (r3, r17) in its own notes; a customer never should see one.
  const HANDLE = /(?<![\w-])r\d{1,4}(?![\w-])/;
  const ENGLISH = /\b(?:the|and|for|with|from|your|you|this|that|these|front|rear|pads?|brakes?|oil|filters?|engine|petrol|diesel|stock|price|show|more|next|steps?|choose|answer|check|checking|needs?|matches|ask|shop|change|new|chat|try|again|looking|search|both|yes|not|sure|year|model|car|parts?|number|available|unavailable|warehouse|hours?|found|details|source|assistant|catalog|confirmed|listed|offered|asked)\b/i;
  const GEORGIAN = /[Ⴀ-ჿ]/;
  const QUESTION = /[?？]/;

  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node || !node.isConnected) return false;
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    if (!(box.width > 0 && box.height > 0) || style.visibility === 'hidden' || style.display === 'none') return false;
    // The inside of a closed <details> keeps its boxes; only this tells that it is not drawn.
    return typeof node.checkVisibility !== 'function' || node.checkVisibility();
  };
  const describe = (node) => `${node.tagName.toLowerCase()}${node.className && typeof node.className === 'string' ? `.${node.className.trim().split(/\s+/).join('.')}` : ''}`;
  const pressed = (button) => button.getAttribute('aria-pressed') === 'true';

  const walk = {
    csp: [],
    lines: [],
    timer: 0,

    count(kind) {
      const s = SELECTORS[kind];
      const log = document.querySelector(s.log);
      return log ? log.querySelectorAll(s.reply).length : 0;
    },

    /** When the reply to the last message is complete: the time, else 0. */
    settled(kind, before) {
      const s = SELECTORS[kind];
      const log = document.querySelector(s.log);
      if (!log || log.querySelectorAll(s.reply).length <= before || log.querySelector(s.waiting)) return 0;
      if (kind === 'staff' && document.querySelector(s.send).disabled) return 0;
      return Date.now();
    },

    /** Note every change of the progress line from now on. */
    watch(kind) {
      const node = document.querySelector(SELECTORS[kind].progress);
      const started = Date.now();
      const lines = [];
      let last = '';
      clearInterval(walk.timer);
      walk.lines = lines;
      walk.timer = setInterval(() => {
        const value = clean(node && node.textContent);
        if (value === last) return;
        last = value;
        lines.push([Date.now() - started, value]);
      }, 80);
    },

    seen() {
      clearInterval(walk.timer);
      return walk.lines;
    },

    /** The answer buttons of the newest reply that can still be pressed. */
    open(kind) {
      const s = SELECTORS[kind];
      const reply = [...document.querySelector(s.log).querySelectorAll(s.reply)].at(-1);
      const group = reply && reply.querySelector(s.answers);
      if (!group) return [];
      return [...group.querySelectorAll('button')]
        .map((button, index) => ({ index, label: clean(button.textContent), off: button.disabled }))
        .filter((button) => !button.off);
    },

    /** Right after a tap: how the question that was answered looks. */
    tapped(kind) {
      const s = SELECTORS[kind];
      const groups = [...document.querySelector(s.log).querySelectorAll(s.answers)];
      const group = groups.reverse().find((node) => [...node.querySelectorAll('button')].some(pressed));
      if (!group) return { marked: '', off: false, colour: '' };
      const buttons = [...group.querySelectorAll('button')];
      const chosen = buttons.filter(pressed);
      return {
        marked: chosen.map((button) => clean(button.textContent)).join(' | '),
        off: buttons.every((button) => button.disabled),
        colour: getComputedStyle(chosen[0]).backgroundColor,
        plain: getComputedStyle(buttons.find((button) => !pressed(button)) || chosen[0]).backgroundColor,
      };
    },

    toEnd(kind) {
      const log = document.querySelector(SELECTORS[kind].log);
      const room = log.scrollHeight - log.clientHeight - log.scrollTop;
      if (room > 4) log.scrollTop = log.scrollHeight;
      return room > 4;
    },

    /** How the VIN the visitor just typed is shown: the bubble, the note about it, and whether it is anywhere on the page whole. */
    typedVin(kind, vin) {
      const s = SELECTORS[kind];
      const log = document.querySelector(s.log);
      const mine = [...log.querySelectorAll(s.mine)].at(-1);
      return {
        bubble: clean(mine && mine.textContent),
        whole: document.body.innerText.toUpperCase().includes(vin),
        notes: [...log.querySelectorAll('.chat__sys')].filter(visible).map((node) => clean(node.textContent)),
      };
    },

    /** Bring the group of the newest reply whose line says `level` to the top of the conversation. */
    showLine(kind, level) {
      const s = SELECTORS[kind];
      const reply = [...document.querySelector(s.log).querySelectorAll(s.reply)].at(-1);
      const line = reply && [...reply.querySelectorAll(s.line)].find((node) => node.querySelector(`[data-confidence="${level}"], .fit.${level}`));
      if (!line) return false;
      const heading = line.previousElementSibling && line.previousElementSibling.matches(s.heading) ? line.previousElementSibling : line;
      heading.scrollIntoView({ block: 'start' });
      // When the group before it starts close above, show that one from its heading too.
      const headings = [...reply.querySelectorAll(s.heading)];
      const before = headings[headings.indexOf(heading) - 1];
      const room = document.querySelector(s.log).clientHeight * 0.45;
      if (before && heading.getBoundingClientRect().top - before.getBoundingClientRect().top < room) before.scrollIntoView({ block: 'start' });
      return true;
    },

    /** Open the details of the first part card that has any, and return their text. */
    details(kind) {
      const s = SELECTORS[kind];
      const node = s.details && [...document.querySelectorAll(`${s.log} ${s.details}`)].at(-1);
      if (!node) return '';
      node.open = true;
      node.scrollIntoView({ block: 'center' });
      return clean(node.textContent);
    },

    inspect(kind, ctx) {
      const s = SELECTORS[kind];
      const root = document.querySelector(s.root);
      const log = document.querySelector(s.log);
      const reply = [...log.querySelectorAll(s.reply)].at(-1);
      const issues = [];
      const facts = {};
      if (!reply) return { issues: ['there is no reply on the page'], facts };
      const lead = reply.querySelector(s.lead);
      facts.text = clean(lead && lead.textContent);
      facts.failed = reply.matches(s.failed);
      if (facts.failed) issues.push('the reply is an error message');

      // A reply that asks: its answers, as one named group right under the text.
      const group = reply.querySelector(s.answers);
      const buttons = group ? [...group.querySelectorAll('button')] : [];
      const live = buttons.filter((button) => !button.disabled && visible(button));
      const asking = ctx.asks === null ? QUESTION.test(facts.text) : ctx.asks;
      facts.answers = buttons.map((button) => clean(button.textContent));
      if (asking && live.length < 2) issues.push(`the reply asks, but shows ${live.length} answer button(s)`);
      if (QUESTION.test(facts.text) && !asking) issues.push('the text asks something the server did not send as a question');
      if (group) {
        const holder = reply.querySelector(s.holder);
        if (!lead || !(lead.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING)) {
          issues.push('the answer buttons are not under the text');
        }
        if (holder && holder.children.length && !(group.compareDocumentPosition(holder) & Node.DOCUMENT_POSITION_FOLLOWING)) {
          issues.push('the answer buttons come after the parts');
        }
        if (group.getAttribute('role') !== 'group' || !(group.getAttribute('aria-label') || group.getAttribute('aria-labelledby'))) {
          issues.push('the answer group has no name for a screen reader');
        }
        if (!QUESTION.test(clean(reply.textContent))) issues.push('answer buttons under a reply that asks nothing');
      }
      facts.next = [...reply.querySelectorAll(`${s.next} button, ${s.next} a`)].map((node) => clean(node.textContent));

      // Questions answered earlier stay as they were left: switched off, one answer marked.
      const earlier = [...log.querySelectorAll(s.answers)].filter((node) => node !== group);
      for (const node of earlier) {
        const all = [...node.querySelectorAll('button')];
        if (!facts.failed && all.some((button) => !button.disabled)) issues.push('an earlier question still has live buttons');
        if (all.filter(pressed).length > 1) issues.push('more than one answer is marked in one question');
      }
      if (ctx.tapped) {
        const marked = earlier.flatMap((node) => [...node.querySelectorAll('button')].filter(pressed)).map((button) => clean(button.textContent));
        if (marked.at(-1) !== ctx.tapped) issues.push(`the tapped answer "${ctx.tapped}" is no longer marked`);
      }

      // The car line.
      const bar = document.querySelector(s.carBar);
      facts.car = bar && !bar.hidden && visible(bar) ? clean(document.querySelector(s.carName).textContent) : '';
      if (ctx.car && facts.car !== ctx.car) issues.push(`the server named the car "${ctx.car}", the car line shows "${facts.car}"`);
      if (ctx.expectCar && !new RegExp(ctx.expectCar, 'i').test(facts.car)) issues.push(`the car line does not show this car (it shows "${facts.car}")`);

      // Part cards and their headings. Why a part is offered (its confidence label and the
      // reason) stands on the card, or once in a line above the cards of its whole group:
      // above the list in the widget, above the group's first card on the staff screen.
      const lineOver = (card) => {
        let node = card.parentElement.matches(s.holder) ? card : card.parentElement;
        do node = node.previousElementSibling; while (node && node.matches(s.card));
        return node && node.matches(s.line) && visible(node) ? node : null;
      };
      const shown = (within, selector) => {
        const node = within && within.querySelector(selector);
        return node && visible(node) ? node : null;
      };
      // The level a label stands for, as the page marks it up.
      const levelOf = (node) => (node ? node.dataset.confidence || [...node.classList].find((name) => name !== 'fit' && name !== 'card__fit') || '' : '');
      // The heading a card stands under: the nearest one above it, or none above a list of its own.
      const headingOver = (card) => {
        let node = card.parentElement.matches(s.holder) ? card : card.parentElement;
        do node = node.previousElementSibling; while (node && !node.matches(s.heading) && !node.matches('ul'));
        return node && node.matches(s.heading) ? clean(node.textContent) : '';
      };
      facts.cards = [...reply.querySelectorAll(s.card)].map((card) => {
        const part = (selector) => clean((shown(card, selector) || {}).textContent);
        const line = lineOver(card);
        const own = Boolean(shown(card, s.fit) || shown(card, s.why));
        const label = shown(card, s.fit) || shown(line, s.fit);
        const why = clean((shown(card, s.why) || shown(line, s.why) || {}).textContent);
        // Its reason is said once: the folded-away details are not counted.
        const said = why ? [card, line].filter(Boolean).flatMap((within) => [...within.querySelectorAll('*')])
          .filter((node) => visible(node) && !node.children.length && clean(node.textContent).includes(why)).length : 0;
        return {
          name: part(s.name), price: part(s.price), stock: part(s.stock), fit: clean((label || {}).textContent),
          level: levelOf(label), why, said, own, shared: Boolean(line), heading: headingOver(card),
        };
      });
      facts.cards.forEach((card, index) => {
        const sent = (ctx.parts || [])[index] || { priced: true, rated: true };
        const missing = ['name', 'stock'].filter((key) => !card[key]);
        if (!card.price && sent.priced) missing.push('price');
        if (!card.fit && sent.rated) missing.push('confidence label');
        if (missing.length) issues.push(`part ${index + 1} ("${card.name.slice(0, 32)}") shows no ${missing.join(', ')}`);
        if (card.said > 1) issues.push(`part ${index + 1} says its reason ${card.said} times`);
        if (typeof sent.level === 'string' && card.level !== sent.level) {
          issues.push(`part ${index + 1} is labelled "${card.level || 'nothing'}", the server sent "${sent.level || 'no level this page knows'}"`);
        }
        if (card.shared && card.own) issues.push(`part ${index + 1} repeats what the line above its group says`);
        if (sent.shared === true && !card.shared) issues.push(`part ${index + 1} says on its own what its whole group shares: one line above the group was expected`);
        if (sent.shared === false && card.shared) issues.push(`part ${index + 1} is spoken for by a line above it, and its group does not share one ground`);
        // A part stays under the heading of its group, on later pages too. (On the staff screen
        // a card under no header cannot be told from the group above it, so only named groups count.)
        if (typeof sent.heading === 'string' && (sent.heading || kind === 'widget')
            && !(sent.heading ? card.heading.startsWith(sent.heading) : !card.heading)) {
          issues.push(`part ${index + 1} stands under "${card.heading || 'no heading'}", the server put it under "${sent.heading || 'no heading'}"`);
        }
      });
      // What the server said about old stock figures has to reach the reader.
      facts.note = [...reply.querySelectorAll('.msg__note, .note')].filter(visible).map((node) => clean(node.textContent)).join(' | ');
      if (ctx.stale && !facts.note) issues.push('the server warned that the stock figures are old, and the reply does not say so');
      if (ctx.note && !facts.note.includes(ctx.note)) issues.push(`the note the server sent is not on screen: "${ctx.note.slice(0, 60)}"`);
      facts.headings = [...reply.querySelectorAll(s.heading)].filter(visible).map((node) => clean(node.textContent));
      if (ctx.groups !== null && facts.headings.length !== ctx.groups) {
        issues.push(`${ctx.groups} group heading(s) expected for these parts, ${facts.headings.length} shown`);
      }
      const more = [...reply.querySelectorAll('button')].find((button) => /^(?:მეტის ნახვა|კიდევ ნახვა|Show more)/.test(clean(button.textContent)));
      facts.more = more ? clean(more.textContent) : '';

      // Nothing sideways, and the composer in sight.
      const frame = root.getBoundingClientRect();
      if (document.documentElement.scrollWidth > window.innerWidth + 1) issues.push('the page overflows sideways');
      if (log.scrollWidth > log.clientWidth + 1) issues.push(`the conversation overflows sideways (${log.scrollWidth} > ${log.clientWidth})`);
      const sticking = new Set();
      for (const node of root.querySelectorAll('*')) {
        if (!visible(node) || node.closest('.sr-only, svg') || /^(?:TEXTAREA|INPUT|SELECT|OPTION)$/.test(node.tagName)) continue;
        const box = node.getBoundingClientRect();
        if (box.right > frame.right + 1 || box.left < frame.left - 1) sticking.add(`${describe(node)} sticks out of the chat sideways`);
        else if (node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 1) {
          sticking.add(`${describe(node)} is wider inside than its box (${node.scrollWidth} > ${node.clientWidth}): "${clean(node.textContent).slice(0, 40)}"`);
        }
      }
      issues.push(...sticking);
      const form = document.querySelector(s.form);
      const box = form.getBoundingClientRect();
      facts.composer = [Math.round(box.top), Math.round(box.bottom), window.innerHeight];
      if (!visible(form) || box.top < -1 || box.bottom > window.innerHeight + 1) {
        issues.push(`the composer is not fully on screen (${facts.composer[0]} to ${facts.composer[1]} of ${facts.composer[2]})`);
      }

      // Words: nothing raw, nothing in the other language outside the catalogue's own text.
      const words = new Set();
      const seen = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = seen.nextNode(); node; node = seen.nextNode()) {
        const value = clean(node.textContent);
        const parent = node.parentElement;
        if (!value || !parent || parent.closest('.sr-only, script, style') || !visible(parent)) continue;
        const own = Boolean(parent.closest(s.own));
        if (!own) words.add(value);
        if (RAW.test(value) && !own) issues.push(`a raw value or key on screen: "${value.slice(0, 60)}"`);
        if (VALUES.test(value) && !own) issues.push(`an internal value on screen: "${value.slice(0, 60)}"`);
        if (HANDLE.test(value) && !own) issues.push(`a row handle of the engine on screen: "${value.slice(0, 80)}"`);
        if (/[–—]/.test(value) && kind === 'widget') issues.push(`a long dash in site text: "${value.slice(0, 60)}"`);
        if (own) continue;
        if (ctx.language === 'ka' && ENGLISH.test(value)) issues.push(`English on the Georgian page: "${value.slice(0, 80)}"`);
        if (ctx.language === 'en' && GEORGIAN.test(value)) issues.push(`Georgian on the English page: "${value.slice(0, 80)}"`);
      }
      facts.words = [...words].slice(0, 160);
      const input = root.querySelector('textarea');
      facts.placeholder = input ? input.placeholder : '';
      return { issues: [...new Set(issues)], facts };
    },
  };
  window.__walk = walk;
  document.addEventListener('securitypolicyviolation', (event) => {
    walk.csp.push(`${event.violatedDirective}: ${event.blockedURI}`);
  });
}

/** WALK_DRY: answer the chat requests from the shared fixture, a question to a typed message and an offer to a tap. */
async function answerFromFixtures(page, kind) {
  const turns = JSON.parse(readFileSync(FIXTURES, 'utf8'));
  const golden = (id) => structuredClone(turns.find((turn) => turn.id === id)[kind === 'staff' ? 'engine' : 'public']);
  // An oil change comes in two pages, so that "Show more" has something to bring.
  const oils = golden('oil_change');
  const pageOf = (from, to) => ({ ...oils, cards: oils.cards.slice(from, to), offset: from, next_offset: to < oils.cards.length ? to : null });
  let reply = null;
  let polls = 0;
  let serial = 0;
  await page.route(/\/api\/(?:site|v1)\/chat\/conversations/, (route) => {
    const request = route.request();
    const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    // Forgetting a conversation the server never had is harmless, so that one goes through.
    if (request.method() === 'DELETE') return route.continue();
    if (request.method() === 'POST' && request.url().endsWith('/messages')) {
      const sent = JSON.parse(request.postData());
      reply = /ზეთ|\boil\b/i.test(sent.message || '') ? pageOf(0, 3) : golden(sent.choice ? 'offer' : 'question');
      polls = 0;
      serial += 1;
      return json(202, { turn_id: String(serial).padStart(32, 't'), status: 'RUNNING', language: 'ka' });
    }
    if (request.method() === 'POST') return json(201, { conversation_id: 'c'.repeat(43), expires_in: 600 });
    const offset = Number(new URL(request.url()).searchParams.get('offset') || 0);
    if (offset > 0) return json(200, pageOf(offset, oils.cards.length));
    polls += 1;
    return json(200, polls < 6 ? { ...golden('running'), cards: [], vehicle: {}, total: 0 } : reply);
  });
}

/** What the server sent, in the terms the page check needs. */
function expectations(kind, turn) {
  if (!turn) return { asks: null, car: null, groups: null, parts: null, stale: false, note: '' };
  const cards = Array.isArray(turn.cards) ? turn.cards : [];
  const named = new Set((turn.groups || []).filter((group) => group && group.label).map((group) => group.id));
  const headings = [];
  for (const card of cards) {
    // A heading opens every run of cards of one group, as both screens draw them.
    if (named.has(card.group) && headings.at(-1) !== card.group) headings.push(card.group);
  }
  // Why a part is offered: its level, when it is one the screens have a label for, and its
  // reason. The parts of a run that all rest on the same ground say it in one line above
  // them, where that line cannot be read onto parts outside the run: in a reply that is a
  // single run, or whose runs all have a heading.
  const level = (card) => (LEVELS.includes(card.confidence) ? card.confidence : '');
  const reason = (card) => (typeof card.reason === 'string' ? (kind === 'staff' ? card.reason.trim() : card.reason) : '');
  const ground = (card) => `${level(card)}\u001f${reason(card)}`;
  const runs = [];
  const runOf = [];
  for (const card of cards) {
    const id = typeof card.group === 'string' ? card.group : '';
    if (!runs.length || runs.at(-1).id !== id) runs.push({ id, headed: named.has(id), grounds: new Set() });
    runs.at(-1).grounds.add(ground(card));
    runOf.push(runs.at(-1));
  }
  const once = runs.length === 1 || runs.every((run) => run.headed);
  const shared = cards.map((card, index) => once && runOf[index].grounds.size === 1 && ground(card) !== '\u001f');
  // The heading a card belongs under, as the screen writes it (the widget writes no long dash).
  const written = (label) => (kind === 'widget' ? label.replace(/\s+[–—]\s+/g, ', ').replace(/[–—]/g, '-') : label).replace(/\s+/g, ' ').trim();
  const headingOf = new Map((turn.groups || []).filter((group) => group && group.label).map((group) => [group.id, written(String(group.label))]));
  return {
    asks: Boolean(turn.question && (turn.question.options || []).length),
    car: (kind === 'staff' ? turn.car_label : turn.car) || null,
    groups: headings.length,
    // Only the staff turn carries the warning itself; the public one arrives with its note written.
    stale: kind === 'staff' && cards.length > 0 && (turn.warnings || []).includes('STOCK_MAY_BE_STALE'),
    note: typeof turn.note === 'string' ? turn.note.replace(/\s+/g, ' ').trim() : '',
    parts: cards.map((card, index) => ({
      priced: kind === 'staff' ? typeof (card.product || {}).price === 'number' && card.product.price > 0 : true,
      rated: Boolean(level(card)), level: level(card), shared: shared[index], heading: headingOf.get(card.group) || '',
    })),
  };
}

const lastSend = { widget: 0, staff: 0 };

async function walkScreen(browser, screen, report) {
  const context = await browser.newContext({
    viewport: { width: screen.width, height: screen.height }, deviceScaleFactor: screen.mobile ? 2 : 1,
    hasTouch: Boolean(screen.mobile), isMobile: Boolean(screen.mobile), locale: screen.language === 'ka' ? 'ka-GE' : 'en-GB',
  });
  await context.addInitScript(probe);
  const page = await context.newPage();
  const { kind } = screen;
  const parts = PARTS[kind];
  const problems = [];
  const run = { final: null, running: 0, sent: [], pages: [], vinInAddress: false, closing: false };
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) problems.push(`console ${message.type()}: ${message.text()}`);
  });
  page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
  page.on('requestfailed', async (request) => {
    // Chrome reports a request answered with 204 (forgetting a conversation) as aborted
    // although the page got its answer: only a request that got none has failed.
    if (run.closing || (await request.response().catch(() => null))) return;
    const reason = request.failure() ? request.failure().errorText : '';
    problems.push(`request failed (${reason}): ${request.method()} ${new URL(request.url()).pathname.replace(/[\w-]{32,}/g, ':id')}`);
  });
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/messages')) run.sent.push(JSON.parse(request.postData()));
    // A VIN travels in the body of a message and nowhere else.
    if (decodeURIComponent(request.url()).toUpperCase().includes(VIN_VALUE)) run.vinInAddress = true;
  });
  page.on('response', async (response) => {
    const url = new URL(response.url());
    if (response.status() >= 400) problems.push(`HTTP ${response.status()}: ${response.request().method()} ${url.pathname.replace(/[\w-]{32,}/g, ':id')}`);
    if (response.request().method() !== 'GET' || !url.pathname.includes('/turns/')) return;
    try {
      const body = await response.json();
      // A later page of a finished turn is what "Show more" asked for.
      if (url.searchParams.get('offset') !== '0') run.pages.push(body);
      else if (body.status === 'RUNNING' || body.status === 'PENDING') run.running += 1;
      else run.final = body;
    } catch {
      // The page was closed while the body was read.
    }
  });
  if (DRY) await answerFromFixtures(page, kind);

  const call = (name, ...rest) => page.evaluate(([method, args]) => window.__walk[method](...args), [name, [kind, ...rest]]);
  const shot = async (label) => {
    const file = path.join(OUT, `${screen.name}-${label}.png`);
    await page.screenshot({ path: file });
    return path.basename(file);
  };
  const press = (locator) => (screen.mobile ? locator.tap() : locator.click());

  await page.goto(ORIGIN + screen.path, { waitUntil: 'networkidle' });
  if (kind === 'widget') {
    // At the top of the page the launcher steps aside for the hero's buttons: use one of those.
    await press(page.locator('.hero [data-chat-open]').first());
    await page.waitForSelector('#chat[open] .starters');
  } else if (!(await page.locator(parts.input).count())) {
    throw new Error('the staff page did not open: start the server with AUTOMOTIVE_STAFF_AUTH=false');
  }
  await delay(500);
  report.shots.push(await shot('start'));

  /** One message and its reply: send, time, look, record. */
  async function exchange(talkId, stepNumber, action) {
    const talk = TALKS[talkId];
    const id = `${talkId}${stepNumber}`;
    const wait = lastSend[kind] + (DRY ? 0 : GAP_MS[kind]) - Date.now();
    if (wait > 0) await delay(wait);
    const before = await call('count');
    const asked = run.final && run.final.question ? run.final.question.options || [] : [];
    run.final = null;
    run.running = 0;
    const sentBefore = run.sent.length;
    if (action.say) {
      // Nobody sends a message in the instant its first letter appears.
      await page.locator(parts.input).fill(action.say.replace(VIN_MARK, VIN_VALUE));
      if (!DRY) await delay(TYPING_MS);
    }
    await call('watch');
    const started = Date.now();
    lastSend[kind] = started;
    if (action.say) await page.locator(parts.input).press('Enter');
    else await press(page.locator(parts.reply).last().locator(parts.answers).nth(action.button.index));
    const record = {
      screen: screen.name, talk: talkId, step: stepNumber,
      did: action.say ? `typed "${action.say}"` : `tapped "${action.button.label}"`, issues: [],
    };
    if (action.button) {
      await delay(250); // the fill of the chosen answer is a 120 ms transition
      const state = await call('tapped');
      record.tapState = state;
      if (state.marked !== action.button.label) record.issues.push(`after the tap the marked answer is "${state.marked}", not "${action.button.label}"`);
      if (!state.off) record.issues.push('after the tap the other answers can still be pressed');
      if (state.colour === state.plain) record.issues.push('the tapped answer looks like the others');
    }
    const settling = page.waitForFunction(([screenKind, count]) => window.__walk.settled(screenKind, count), [kind, before],
      { timeout: REPLY_PATIENCE_MS, polling: 50 }).then((handle) => handle.jsonValue());
    settling.catch(() => {});
    const early = await Promise.race([settling.then(() => true, () => true), delay(1700).then(() => false)]);
    if (!early) report.shots.push(await shot(`${id}-waiting`));
    let at = 0;
    try {
      at = await settling;
    } catch {
      record.issues.push(`no reply within ${REPLY_PATIENCE_MS / 1000} s`);
      record.seconds = null;
      report.steps.push(record);
      return record;
    }
    record.seconds = Math.round((at - started) / 100) / 10;
    record.progress = await call('seen');
    for (let tries = 0; tries < 30 && !run.final; tries += 1) await delay(100);
    await delay(600); // the entrance of the cards, 220 ms and a stagger
    const turn = run.final;
    record.polls = run.running;
    record.sent = run.sent[sentBefore] || null;
    if (action.button && record.sent) {
      const number = asked.findIndex((option) => option && option.label === action.button.label) + 1;
      if (number && record.sent.choice !== number) record.issues.push(`the tap sent choice ${record.sent.choice}, the answer is number ${number}`);
    }
    const expected = expectations(kind, turn);
    const from = talk.carFrom || Infinity;
    // The fixture's replies are Georgian and about one car, whatever was typed.
    const context = {
      ...expected, language: DRY ? null : screen.language, tapped: action.button ? action.button.label : null,
      expectCar: !DRY && stepNumber >= from ? talk.car : null,
    };
    run.context = { ...context, tapped: null };
    const looked = await call('inspect', context);
    record.issues.push(...looked.issues);
    record.facts = looked.facts;
    record.turn = turn;
    report.shots.push(await shot(id));
    if (await call('toEnd')) {
      await delay(250);
      report.shots.push(await shot(`${id}-end`));
    }
    report.steps.push(record);
    const shownFirst = (looked.facts.cards || []).length;
    if (kind === 'widget' && looked.facts.more) await showMore(record, id, turn, context);
    const cards = record.facts.cards || [];
    console.log(`${screen.name} ${id} ${String(record.seconds).padStart(5)} s  ${record.did}  ->  ${shownFirst} part(s)`
      + `${cards.length > shownFirst ? `, ${cards.length} after "Show more"` : ''}`
      + `${looked.facts.answers && looked.facts.answers.length ? `, asks [${looked.facts.answers.join(' | ')}]` : ''}`
      + `${record.issues.length ? `  !! ${record.issues.length} issue(s)` : ''}`);
    return record;
  }

  /**
   * Press "Show more" under the newest reply until its list is whole (three times at most)
   * and look at the page again each time, with the parts the server sent for that page.
   */
  async function showMore(record, id, turn, context) {
    let cards = Array.isArray(turn && turn.cards) ? [...turn.cards] : [];
    record.more = [];
    for (let presses = 0; presses < 3; presses += 1) {
      const reply = page.locator(parts.reply).last();
      const button = reply.locator(parts.more);
      if (!(await button.count())) break;
      const before = await reply.locator(parts.card).count();
      const known = run.pages.length;
      await press(button);
      try {
        await page.waitForFunction(([selector, card, count]) => {
          const last = [...document.querySelectorAll(selector)].at(-1);
          return Boolean(last) && last.querySelectorAll(card).length > count;
        }, [parts.reply, parts.card, before], { timeout: 15_000, polling: 100 });
      } catch {
        record.issues.push('"Show more" brought no parts within 15 s');
        break;
      }
      await delay(700); // the entrance of the new cards
      const brought = run.pages.slice(known).flatMap((body) => (Array.isArray(body.cards) ? body.cards : []));
      cards = cards.concat(brought);
      const looked = await call('inspect', { ...context, ...expectations(kind, { ...turn, cards }) });
      const fresh = looked.issues.filter((issue) => !record.issues.includes(issue)).map((issue) => `after "Show more": ${issue}`);
      record.issues.push(...fresh);
      record.more.push({ before, after: looked.facts.cards.length, brought: brought.length, issues: fresh });
      record.facts = looked.facts;
    }
    if (record.more.length) report.shots.push(await shot(`${id}-more`));
  }

  /** Fill the staff page's vehicle form, which is folded away under the chat, and come back to the chat. */
  async function fillForm(values) {
    if (!(await page.evaluate(() => document.getElementById('direct-search').open))) await press(page.locator(parts.formToggle));
    for (const [field, value] of Object.entries(values)) await page.locator(`#${field}`).fill(value);
    await page.evaluate(() => document.querySelector('.chat-panel').scrollIntoView({ block: 'start' }));
    await delay(300);
  }

  const tally = (values) => [...values.reduce((counts, value) => counts.set(value, (counts.get(value) || 0) + 1), new Map())]
    .map(([value, count]) => `${value || 'none'} x${count}`).join(', ');

  // What is looked at once more when a conversation is over. Each takes the conversation's
  // turns and returns one line: "pass: ..." or what happened instead.
  const checks = {
    async oil(steps) {
      const last = steps.at(-1);
      const cards = (last.facts && last.facts.cards) || [];
      if (!cards.length) return `no parts came: "${String((last.facts || {}).text || '').slice(0, 80)}"`;
      const groups = new Map();
      for (const card of cards) {
        const entry = groups.get(card.heading) || { count: 0, levels: new Set(), lines: 0, why: '' };
        entry.count += 1;
        entry.levels.add(card.level || 'none');
        entry.lines += card.shared ? 1 : 0;
        entry.why ||= card.why;
        groups.set(card.heading, entry);
      }
      const only = (level) => [...groups.values()].filter((entry) => entry.levels.size === 1 && entry.levels.has(level));
      const [chosen, listed] = [only('spec'), only('listed')];
      const passed = chosen.length > 0 && chosen.every((entry) => entry.lines === entry.count && entry.why)
        && listed.length > 0 && listed.every((entry) => entry.lines === entry.count);
      if (await call('showLine', 'spec')) {
        await delay(250);
        report.shots.push(await shot(`${last.talk}${last.step}-spec`));
      }
      return `${passed ? 'pass' : 'not as expected'}: ${[...groups].map(([heading, entry]) => `"${heading || 'no heading'}" ${entry.count} part(s), `
        + `${[...entry.levels].join('/')}, ${entry.lines === entry.count ? 'one line for the group' : entry.lines ? 'some cards under a line' : 'a line on each card'}`
        + `${entry.why ? `, reason "${entry.why.slice(0, 70)}"` : ', no reason'}`).join('; ')}`;
    },

    async more() {
      const pressed = report.steps.filter((step) => step.more && step.more.length);
      if (!pressed.length) return 'not run: no reply had more parts than its first page';
      const faults = pressed.flatMap((step) => step.more.flatMap((entry) => entry.issues.map((issue) => `${step.talk}${step.step}: ${issue}`)));
      const told = pressed.map((step) => `${step.talk}${step.step} ${step.more.map((entry) => `${entry.before} to ${entry.after}`).join(', then ')} parts`
        + ` under [${[...new Set(step.facts.cards.map((card) => card.heading || 'no heading'))].join(' | ')}]`).join('; ');
      return `${faults.length ? `not as expected: ${faults.join('; ')}` : 'pass'}: ${told}`;
    },

    async 'dont-know'(steps) {
      const tap = steps.find((step) => step.escape);
      if (!tap) return `not run: no "not sure" answer was offered (${steps.at(-1).facts ? `answers [${(steps.at(-1).facts.answers || []).join(' | ')}]` : 'no reply'})`;
      const facts = tap.facts || {};
      const cards = facts.cards || [];
      const sideways = tap.issues.filter((issue) => /sideways|sticks out|wider inside/.test(issue));
      const passed = cards.length > 0 && (facts.headings || []).length >= 2 && cards.every((card) => card.level === 'check' && card.why) && !sideways.length;
      return `${passed ? 'pass' : 'not as expected'}: tapped "${tap.did.replace(/^tapped "|"$/g, '')}", ${cards.length} part(s) in ${(facts.headings || []).length} group(s)`
        + ` [${(facts.headings || []).join(' | ')}], levels ${tally(cards.map((card) => card.level)) || 'none'}, ${cards.filter((card) => !card.why).length} without a reason,`
        + ` ${sideways.length ? sideways.join('; ') : 'nothing overflows'}${(facts.answers || []).length ? `, asks again [${facts.answers.join(' | ')}]` : ''}`;
    },

    async vin(steps) {
      const turn = steps[0];
      const facts = turn.facts || {};
      const seen = await call('typedVin', VIN_VALUE);
      const faults = [];
      if (seen.whole) faults.push('the whole VIN is on the page');
      if (!seen.bubble.includes(`••••${VIN_VALUE.slice(-4)}`)) faults.push(`the bubble reads "${seen.bubble}"`);
      if (!seen.notes.length) faults.push('no line says that the VIN is hidden');
      if (run.vinInAddress) faults.push('the VIN was sent in an address');
      if (!turn.sent || !String(turn.sent.message || '').toUpperCase().includes(VIN_VALUE)) faults.push('the message did not carry the VIN to the server');
      if (facts.failed || turn.seconds === null) faults.push('no reply, or an error message');
      const ways = [(facts.answers || []).length >= 2 && `asks [${facts.answers.join(' | ')}]`, (facts.next || []).length && `suggests [${facts.next.join(' | ')}]`,
        (facts.cards || []).length && `${facts.cards.length} part(s)`, /[?？]/.test(facts.text || '') && 'its text asks'].filter(Boolean);
      if (!ways.length) faults.push('the reply leaves no way on: no question, no next step, no parts');
      return `${faults.length ? `not as expected: ${faults.join('; ')}` : 'pass'}: bubble "${seen.bubble}", car line "${facts.car || ''}", reply "${String(facts.text || '').slice(0, 110)}", ${ways.join(', ') || 'nothing offered'}`;
    },

    async form(steps) {
      const [first, last] = [steps[0], steps.at(-1)];
      const sent = first.sent && first.sent.vehicle;
      if (!sent || !sent.make) return 'not run: the page did not send the form\'s car with the message';
      const label = String((last.turn && last.turn.car_label) || '');
      const told = await page.evaluate(([selector, panel]) => {
        const reply = [...document.querySelectorAll(selector)].at(-1);
        const fits = reply ? [...reply.querySelectorAll('.chat-part')].map((card) => card.textContent) : [];
        return {
          typed: fits.filter((text) => /camry/i.test(text)).length, other: fits.filter((text) => /honda|\bfit\b|jazz/i.test(text)).length,
          panel: document.querySelector(panel).innerText.replace(/\s+/g, ' ').trim(),
          form: ['make', 'model', 'model_year'].map((field) => document.getElementById(field).value).join(' ').trim(),
        };
      }, [parts.reply, parts.panel]);
      const passed = /camry/i.test(label) && !/honda|\bfit\b/i.test(label) && !told.other;
      return `${passed ? 'pass' : 'not as expected'}: the form held ${[sent.make, sent.model, sent.model_year].filter(Boolean).join(' ')}, the reply is about "${label}"`
        + ` (${told.typed} part(s) that name the typed car, ${told.other} that name the form's), car line "${(last.facts || {}).car || ''}",`
        + ` side panel "${told.panel.slice(0, 70)}", form afterwards "${told.form}"`;
    },
  };

  const wanted = screen.talks.filter((id) => !ONLY_TALKS.length || ONLY_TALKS.includes(id));
  for (const [place, talkId] of wanted.entries()) {
    const talk = TALKS[talkId];
    if (place > 0) {
      await press(page.locator(parts.fresh));
      await delay(300);
    }
    if (talk.form) await fillForm(talk.form);
    let number = 0;
    const turns = [];
    const turn = async (action) => {
      number += 1;
      const record = await exchange(talkId, number, action);
      turns.push(record);
      return record;
    };
    for (const step of talk.steps) {
      if (step.say) {
        await turn({ say: step.say });
        continue;
      }
      let buttons = await call('open');
      if (!buttons.length) {
        if (!step.ifAsked) report.notes.push(`${screen.name} ${talkId}: nothing was asked, so there was no answer to tap`);
        continue;
      }
      if (step.tap === 'escape') {
        // The "not sure" answer of the question that is open, and only of that one.
        const button = buttons.find((entry) => NOT_SURE.test(entry.label));
        if (button) (await turn({ button })).escape = true;
        else report.notes.push(`${screen.name} ${talkId}: the question had no "not sure" answer [${buttons.map((entry) => entry.label).join(' | ')}]`);
        continue;
      }
      const pick = (entries) => (step.tap === 'any'
        ? entries.find((button) => !NOT_SURE.test(button.label)) || entries[0]
        : entries.find((button) => button.label.includes(step.tap)));
      if (!pick(buttons)) {
        // The open question is about something else: answer it with its first button and look once more.
        await turn({ button: buttons.find((button) => !NOT_SURE.test(button.label)) || buttons[0] });
        buttons = await call('open');
      }
      const button = pick(buttons);
      if (button) await turn({ button });
      else report.notes.push(`${screen.name} ${talkId}: no answer button with "${step.tap}" was offered`);
    }
    if (talk.check && turns.length && turns.at(-1).facts) {
      report.checks.push({ name: talk.check, talk: talkId, result: await checks[talk.check](turns) });
    }
  }
  if (kind === 'widget') report.checks.push({ name: 'more', talk: '', result: await checks.more() });

  // A part card keeps its fine print folded away: open one and read it like the rest.
  const details = await call('details');
  if (details && run.context) {
    report.details = details;
    await delay(200);
    report.shots.push(await shot('details'));
    const known = new Set(report.steps.flatMap((step) => step.issues));
    const opened = await call('inspect', run.context);
    problems.push(...opened.issues.filter((issue) => !known.has(issue)).map((issue) => `in a part's details: ${issue}`));
  }
  const csp = await page.evaluate(() => window.__walk.csp);
  report.problems = [...new Set([...problems, ...csp.map((entry) => `Content-Security-Policy: ${entry}`)])];
  run.closing = true;
  await context.close();
}

async function main() {
  if (!DRY && /:8767$/.test(ORIGIN)) {
    console.error('Port 8767 is the shop\'s own server: its chat logs would fill with test turns. Start one on a spare port.');
    process.exit(2);
  }
  mkdirSync(OUT, { recursive: true });
  const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const reports = [];
  try {
    for (const screen of SCREENS) {
      if (ONLY_SCREENS.length && !ONLY_SCREENS.includes(screen.name)) continue;
      const report = { screen: screen.name, size: `${screen.width}x${screen.height}`, steps: [], checks: [], notes: [], problems: [], shots: [] };
      reports.push(report);
      try {
        await walkScreen(browser, screen, report);
      } catch (error) {
        report.problems.push(`the walk stopped: ${String(error.message).split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close();
  }
  const steps = reports.flatMap((report) => report.steps);
  // The made-up VIN stays out of the file, in the message that carried it too.
  const written = JSON.stringify({ origin: ORIGIN, dry: DRY, at: new Date().toISOString(), reports }, null, 1);
  writeFileSync(path.join(OUT, 'report.json'), written.replace(new RegExp(VIN_VALUE, 'gi'), '[VIN]'));

  const median = (values) => {
    const sorted = values.filter((value) => typeof value === 'number').sort((a, b) => a - b);
    return sorted.length ? Math.round((sorted[(sorted.length - 1) >> 1] + sorted[sorted.length >> 1]) * 5) / 10 : null;
  };
  console.log('\nSeconds to the final reply');
  for (const report of reports) {
    const byTalk = new Map();
    for (const step of report.steps) byTalk.set(step.talk, [...(byTalk.get(step.talk) || []), step.seconds === null ? 'none' : step.seconds]);
    console.log(`  ${report.screen.padEnd(11)} ${[...byTalk].map(([talk, seconds]) => `${talk}: ${seconds.join(', ')}`).join('   ')}`
      + `   median ${median(report.steps.map((step) => step.seconds))}`);
  }
  console.log(`  all         median ${median(steps.map((step) => step.seconds))}, slowest ${Math.max(0, ...steps.map((step) => step.seconds || 0))}`);
  for (const report of reports) {
    for (const check of report.checks) console.log(`CHECK ${report.screen} ${check.name}${check.talk ? ` (${check.talk})` : ''}: ${check.result}`);
  }
  let failed = 0;
  for (const report of reports) {
    for (const step of report.steps) {
      for (const issue of step.issues) {
        failed += 1;
        console.log(`ISSUE ${report.screen} ${step.talk}${step.step} (${step.did}): ${issue}`);
      }
    }
    for (const problem of report.problems) {
      failed += 1;
      console.log(`PROBLEM ${report.screen}: ${problem}`);
    }
    for (const note of report.notes) console.log(`NOTE ${note}`);
  }
  console.log(`\n${steps.length} turn(s)${DRY ? ' answered from the fixture, no model call' : `, about $${(steps.length * USD_PER_TURN).toFixed(2)} at $${USD_PER_TURN} a turn`}.`);
  console.log(`Screenshots and report.json: ${OUT}`);
  process.exit(failed ? 1 : 0);
}

await main();
