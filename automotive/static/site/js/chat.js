/**
 * The parts assistant: a client for the public chat endpoints (the same engine the staff
 * workspace uses, behind visitor limits). Loaded when a visitor reaches for the launcher.
 *
 * One <dialog>. On phones it opens modal, as a full-height sheet sized to the visual
 * viewport so the composer stays above the on-screen keyboard, and the back button
 * closes it. On wider screens it is a panel beside the page and is not modal.
 *
 * A reply is always drawn in one order: text, note, the answers to tap when it asks
 * something, the parts under their group headings, "Show more", then the next steps.
 * Why the parts of a group are offered is said once under its heading when it is the same
 * for all of them, and on each card when it differs.
 * Answers stay on screen after a tap (the chosen one marked, the rest switched off) and
 * come back to life when the message did not go through. The view follows a reader who
 * is following and never moves one who has scrolled away.
 *
 * The conversation id lives in this module's memory only. Every string that comes from
 * the server is written with textContent.
 */

import { api, data, el, fill, icon, price, reducedMotion, text, tidy } from './dom.js';

const BASE = '/api/site/chat/conversations';
const LIMIT = 500;
const TURN_PATIENCE_MS = 95000; // a turn may take about a minute on the server
const DOTS_MS = 1200; // only the typing dots at first: a short reply is back by then
const SLOW_MS = 15000; // after this, say that it is taking a while
const COUNT_FROM = 400; // the character counter appears near the limit...
const COUNT_NEAR = 480; // ...and changes colour just before it
const STACK_OVER = 22; // an answer longer than this puts the answers one per row
const NEXT_LIMIT = 4; // next steps under one reply
const END_SLACK = 24; // this close to the end still counts as reading the newest line
const TOP_GAP = 12; // room left above a reply when the view is placed on it
const VIN = /\b[A-HJ-NPR-Z0-9]{17}\b/gi; // no lookbehind: one stops the whole module on older iOS
const STATES = ['in_stock', 'soon', 'unavailable', 'unknown'];
// The confidence levels this page has words for, each with its icon. A part that arrives
// with any other value gets no label at all, never a wrong one.
const FIT_ICONS = new Map([
  ['confirmed', 'check'],
  ['listed', 'info'],
  ['check', 'warning'],
  ['spec', 'gauge'],
]);
const STARTERS = [
  ['chat.starter_oil', 'drop'],
  ['chat.starter_pads', 'disc'],
  ['chat.starter_filter', 'fan'],
  ['chat.starter_vin', 'magnifying-glass'],
];

const dialog = document.getElementById('chat');
const log = dialog.querySelector('.chat__log');
const form = dialog.querySelector('.chat__form');
const input = dialog.querySelector('.chat__input');
const sendButton = dialog.querySelector('.chat__send');
const progress = dialog.querySelector('.chat__progress');
const counter = dialog.querySelector('.chat__count');
const carBar = dialog.querySelector('.chat__car');
const carName = dialog.querySelector('.chat__car-name');
const carChange = dialog.querySelector('[data-chat-car]');
const jump = dialog.querySelector('[data-chat-jump]');
const launcher = document.querySelector('[data-chat-launcher]');
const parts = document.getElementById('parts');
const phone = window.matchMedia('(max-width: 767.98px)');
const viewport = window.visualViewport;
const examplePlaceholder = input.placeholder;
const changeLabel = carChange ? carChange.textContent : '';

let conversation = null; // conversation id, never stored
let opening = null; // the request that opens a conversation ahead of its first message
let spoken = false; // the visitor has sent something in this conversation
let busy = false;
let revision = 0; // "New chat" bumps it; a turn from before stops rendering
let opener = null;
let modal = false;
let inHistory = false;
let greeted = false;
let car = ''; // the short label of the car, as the server last sent it
let editingCar = false; // "Change" was pressed: the next message names another car
let asked = false; // the newest reply is a question that is still open
let closed = false; // the composer is off: daily limit reached, or the chat switched off
let hint = ''; // a placeholder a starter asked for, until the next message
let vinNoted = false; // the "VIN is hidden" line was shown in this conversation
let viaKeyboard = false; // the last answer was tapped from the keyboard
let anchor = null; // the message the view was last placed on
let placedAt = 0; // where that put the log
let away = false; // the reader has scrolled away from there
let unread = null; // the reply the "new reply" button leads to
let serial = 0; // for the ids that label a group

const drawn = new WeakMap(); // a reply's card holder -> the keys of its rows, in the server's order
const listed = new WeakMap(); // a list of cards -> the cards its rows were drawn from, in order
const summaries = new WeakMap(); // a list of cards -> the line above it that speaks for all of them
const headed = new WeakSet(); // the lists of cards that stand under a heading of their own

const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
const announce = (open) => document.dispatchEvent(new CustomEvent('chat:state', { detail: { open } }));

/* --------------------------------------------------------------------- view */

/** Whether the newest line is in view. */
const atEnd = () => log.scrollHeight - log.scrollTop - log.clientHeight <= END_SLACK;

function clearPill() {
  unread = null;
  if (jump) jump.hidden = true;
}

/**
 * Put the top of `node` just under the top of the log. A short message lands at the end
 * of the log; a long reply shows where it starts, so its question and its answers are in
 * view. Instant on purpose: during a smooth scroll the position read back is not yet the
 * one asked for, and the reader would be taken for someone who scrolled away.
 */
function reveal(node) {
  anchor = node;
  log.scrollTop = Math.max(0, Math.min(node.offsetTop - TOP_GAP, log.scrollHeight - log.clientHeight));
  placedAt = log.scrollTop;
  away = false;
  clearPill();
}

/** New content: follow it for a reader who was following, tell anyone else that it is there. */
function settleView(node, follow) {
  if (follow) {
    reveal(node);
  } else if (jump) {
    if (!unread || !unread.isConnected) unread = node;
    jump.hidden = false;
  }
}

// The reader moved the log: away from where the view was placed, or back to the end.
log.addEventListener(
  'scroll',
  () => {
    const end = atEnd();
    away = !end && Math.abs(log.scrollTop - placedAt) > 2;
    if (end) clearPill();
  },
  { passive: true },
);

/* ----------------------------------------------------------------- messages */

/** What the visitor wrote, as it is shown: of a VIN only the last four characters. */
const mask = (value) => value.replace(VIN, (vin) => `\u2022\u2022\u2022\u2022${vin.slice(-4)}`);

function shell(role, language) {
  const node = el('div', `msg msg--${role}`);
  if (language) node.lang = language;
  node.append(el('span', 'sr-only', `${text(role === 'user' ? 'chat.you' : 'chat.assistant')}: `));
  return node;
}

function addMessage(role, content, language) {
  // Where the reader is gets asked before the log changes. Their own message is always shown.
  const follow = role === 'user' || !away;
  const node = shell(role, language);
  if (content) node.append(el('p', '', content));
  log.append(node);
  settleView(node, follow);
  return node;
}

/** A line from the page itself: the chat started again, the VIN is hidden. */
function addSystem(content, before = null) {
  if (!content) return;
  const follow = !away;
  const line = el('p', 'chat__sys', content);
  if (before && before.isConnected) before.before(line);
  else log.append(line);
  settleView(line, follow);
}

function addTyping() {
  const follow = !away;
  const node = el('div', 'typing');
  node.append(el('i'), el('i'), el('i'), el('span', 'sr-only', text('chat.typing')));
  log.append(node);
  settleView(node, follow);
  return node;
}

/* -------------------------------------------------------------------- cards */

const cardKey = (card) =>
  [card.group, card.name, card.brand, card.fits, card.sap_code, card.price,
    card.availability, card.confidence, card.reason].join('\u001f');

/** A part's name, price, catalogue details, SAP code and availability. */
function cardRow(card) {
  const row = el('li', 'card');
  const availability = STATES.includes(card.availability) ? card.availability : 'unknown';
  row.dataset.availability = availability;
  const cost = el('span', 'card__price', price(card.price));
  if (!(card.price > 0)) cost.classList.add('card__price--ask');
  row.append(el('span', 'card__name', tidy(card.name)), cost);
  const meta = [card.brand, card.fits].filter(Boolean).join(', ');
  if (meta) row.append(el('span', 'card__meta', tidy(meta)));
  if (card.sap_code) {
    const label = text('chat.sap_code') || 'SAP: {code}';
    row.append(el('span', 'card__sap', fill(label, { code: card.sap_code })));
  }
  // The marker's shape tells the availability as well as its colour does.
  const state = el('span', 'card__state');
  const stock = el('span', 'card__stock', text(`avail.${availability}`) || text('avail.unknown'));
  stock.dataset.availability = availability;
  state.append(stock);
  row.append(state);
  return row;
}

/** The confidence level a card is shown with: one this page has words for, or none. */
const levelOf = (card) => (FIT_ICONS.has(card.confidence) && text(`fit.${card.confidence}`) ? card.confidence : '');

/** The ground a part is offered on. Two cards with the same one say the same two lines. */
const groundOf = (card) => `${levelOf(card)}\u001f${typeof card.reason === 'string' ? card.reason : ''}`;

/** Why a part is offered, as lines: how sure the fit is, then the assistant's reason. */
function groundLines(card) {
  const lines = [];
  const level = levelOf(card);
  if (level) {
    const fit = el('span', 'card__fit');
    fit.dataset.confidence = level;
    fit.append(icon(FIT_ICONS.get(level)), el('span', '', text(`fit.${level}`)));
    lines.push(fit);
  }
  if (card.reason) lines.push(el('span', 'card__why', tidy(card.reason)));
  return lines;
}

/**
 * Say why the parts of one list are offered. When all of them rest on the same ground (the
 * same confidence and the same reason) and `once` allows it, it is said once, above the
 * list and under its heading, and the cards do not repeat it. Otherwise each card says its
 * own, on rows of its own under the availability. Run again whenever the reply gains cards.
 */
function explain(list, once) {
  const cards = listed.get(list);
  const ground = groundOf(cards[0]);
  const lines = once && cards.every((card) => groundOf(card) === ground) ? groundLines(cards[0]) : [];
  const summary = summaries.get(list);
  if (lines.length) {
    // Written once (the log announces every addition): while this line stands, every card
    // of the list rests on the ground it states.
    if (!summary) {
      const line = el('div', 'cards__fit');
      line.append(...lines);
      summaries.set(list, line);
      list.before(line);
    }
    return;
  }
  if (summary) {
    summary.remove();
    summaries.delete(list);
  }
  // A row is drawn without these lines and gets its own the first time no line speaks for
  // its list. A list only grows, so that line never comes back and no row gives them up.
  [...list.children].forEach((row, index) => {
    if (!row.querySelector('.card__fit, .card__why')) row.append(...groundLines(cards[index]));
  });
}

function cardHolder() {
  const holder = el('div', 'cards');
  drawn.set(holder, []);
  return holder;
}

function groupHeading(group) {
  const heading = el('h3', 'cards__group', tidy(group.label));
  heading.dataset.group = group.id;
  if (group.count > 0) heading.append(el('span', 'cards__count', String(group.count)));
  return heading;
}

/**
 * Add `cards` to a reply's holder, each under the heading of its group: a group that is
 * new gets its heading and its own list, a known one is continued. Returns the first row
 * that was added.
 */
function appendCards(holder, cards, groups, animate) {
  const keys = drawn.get(holder);
  const known = Array.isArray(groups) ? groups : [];
  let first = null;
  cards.forEach((card, index) => {
    const id = typeof card.group === 'string' ? card.group : '';
    let list = [...holder.children].find((node) => node.matches('ul') && node.dataset.group === id);
    if (!list) {
      const group = id && known.find((entry) => entry && entry.id === id && entry.label);
      if (group) holder.append(groupHeading(group));
      list = el('ul', 'msg__cards');
      list.lang = 'ka'; // product names are Georgian in either page language
      list.dataset.group = id;
      listed.set(list, []);
      if (group) headed.add(list);
      holder.append(list);
    }
    const row = cardRow(card);
    if (animate) {
      row.classList.add('card--in');
      row.style.animationDelay = `${Math.min(index, 5) * 40}ms`;
    }
    list.append(row);
    listed.get(list).push(card);
    keys.push(cardKey(card));
    first ||= row;
  });
  // The reply says again why its parts are offered: once for a whole list, or card by card.
  // Once only where that line cannot be read onto parts outside the list: when the reply
  // is a single list, or every one of its lists stands under a heading of its own.
  const lists = [...holder.children].filter((node) => node.matches('ul'));
  const once = lists.length === 1 || lists.every((list) => headed.has(list));
  for (const list of lists) explain(list, once);
  return first;
}

/** Bring a holder in line with `cards`: add what is new while the order holds, else redraw. */
function syncCards(holder, cards, groups) {
  const known = drawn.get(holder);
  const keys = cards.map(cardKey);
  if (known.length <= keys.length && known.every((key, index) => key === keys[index])) {
    appendCards(holder, cards.slice(known.length), groups, true);
    return;
  }
  // The server reordered what it had sent: draw the list again, without the entrance.
  holder.replaceChildren();
  drawn.set(holder, []);
  appendCards(holder, cards, groups, false);
}

/* ------------------------------------------------------------------ choices */

/**
 * A button of the conversation. Focus leaves it before it acts, because acting switches
 * it off or takes it away, and focus must never sit on a dead control.
 */
function tapButton(className, onTap) {
  const button = el('button', className);
  button.type = 'button';
  button.addEventListener('click', (event) => {
    viaKeyboard = event.detail === 0;
    log.focus({ preventScroll: true });
    onTap(button);
  });
  return button;
}

function chip(label, onTap) {
  const button = tapButton('chip', onTap);
  button.textContent = tidy(label);
  return button;
}

/** A reply that can be tapped: something to show and something to send. */
const usable = (option) =>
  Boolean(option && option.label && option.value && typeof option.label === 'string' && typeof option.value === 'string');

/**
 * What a turn asks and suggests. A server that tells the two apart numbers its answers by
 * their place, and a tap sends that number along. An older one sends a single list, which
 * is shown as answers and sent as plain text.
 */
function choicesOf(turn) {
  if ('question' in turn || 'suggestions' in turn) {
    const options = turn.question && Array.isArray(turn.question.options) ? turn.question.options : [];
    return {
      answers: options.map((option, index) => ({ option, number: index + 1 })).filter(({ option }) => usable(option)),
      steps: (Array.isArray(turn.suggestions) ? turn.suggestions : []).filter(usable).slice(0, NEXT_LIMIT),
    };
  }
  const replies = Array.isArray(turn.quick_replies) ? turn.quick_replies : [];
  return { answers: replies.filter(usable).map((option) => ({ option, number: 0 })), steps: [] };
}

/** The answers to a reply's question, as one labelled group right under it. */
function answerGroup(answers, lead) {
  const group = el('div', 'ask');
  group.setAttribute('role', 'group');
  const label = text('chat.answers');
  if (label) {
    group.setAttribute('aria-label', label);
    group.setAttribute('aria-describedby', lead.id);
  } else {
    group.setAttribute('aria-labelledby', lead.id);
  }
  if (answers.some(({ option }) => option.label.length > STACK_OVER)) group.classList.add('ask--stack');
  for (const { option, number } of answers) {
    group.append(chip(option.label, (origin) => send(option.value, { shown: option.label, origin, choice: number })));
  }
  return group;
}

/**
 * A message is on its way: the open answers are switched off (the tapped one marked),
 * and what only made sense before it goes. Returns the group that was open.
 */
function retire(origin) {
  let open = null;
  for (const group of log.querySelectorAll('.ask')) {
    for (const button of group.querySelectorAll('.chip')) {
      if (!button.disabled) open = group;
      if (button === origin) button.setAttribute('aria-pressed', 'true');
      button.disabled = true;
    }
  }
  for (const node of log.querySelectorAll('.ask__hint')) node.hidden = true;
  for (const node of log.querySelectorAll('.next, .starters')) node.remove();
  return open;
}

/** The message did not go through: the question is open again, with nothing marked. */
function restore(group) {
  if (!group || !group.isConnected) return false;
  for (const button of group.querySelectorAll('.chip')) {
    button.disabled = false;
    button.removeAttribute('aria-pressed');
  }
  const after = group.nextElementSibling;
  if (after && after.classList.contains('ask__hint')) after.hidden = false;
  return true;
}

function searchChip() {
  return chip(text('chat.search'), toSearch);
}

/** A link that rings the shop, when the page was given a phone number. */
function callLink() {
  const number = dialog.dataset.phone;
  if (!number) return null;
  const link = el('a', 'chip');
  link.href = `tel:${number}`;
  link.append(icon('phone'), el('span', '', dialog.dataset.call || number));
  return link;
}

function retryChip(node, attempt) {
  return chip(text('search.retry'), () => {
    node.remove();
    send(attempt.content, { quiet: true, shown: attempt.shown, origin: attempt.origin, choice: attempt.choice });
  });
}

/** Close a reply with its next steps, as one quiet labelled group; nothing when there are none. */
function addSteps(node, steps) {
  const group = el('div', 'next');
  group.append(...steps.filter(Boolean));
  if (!group.children.length) return;
  group.setAttribute('role', 'group');
  if (text('chat.next')) group.setAttribute('aria-label', text('chat.next'));
  node.append(group);
}

/* ------------------------------------------------------------------ replies */

function failureText(error) {
  if (error.code === 'CHAT_DAILY_LIMIT') return text('chat.limit');
  if (error.code === 'CHAT_DISABLED') return text('chat.disabled');
  if (error.status === 429) return text('chat.busy');
  if (error.message === 'TIMEOUT') return text('chat.timeout', 'chat.error');
  if (!error.status && window.navigator && window.navigator.onLine === false) return text('chat.offline', 'chat.error');
  return text('chat.error');
}

/** The message was not answered: say so where it happened, with a way on. */
function showFailure(error, attempt) {
  const over = error.code === 'CHAT_DAILY_LIMIT' || error.code === 'CHAT_DISABLED';
  asked = !over && restore(attempt.open);
  const node = addMessage('assistant', failureText(error), data.lang);
  node.classList.add('msg--error');
  if (over) {
    // Nothing more can be sent for now: the part search and the phone still work.
    closed = true;
    input.disabled = true;
    const ways = [parts ? searchChip() : null, callLink()];
    addSteps(node, error.code === 'CHAT_DAILY_LIMIT' ? ways.reverse() : ways);
  } else {
    addSteps(node, [retryChip(node, attempt)]);
  }
  setPlaceholder();
  if (anchor === node) reveal(node); // its buttons made it taller
}

function addPager(reply, holder, turn, ids, rev) {
  reply.querySelector('.msg__more')?.remove();
  reply.querySelector('.msg__fail')?.remove();
  if (typeof turn.next_offset !== 'number') return;
  const remaining = Math.max(0, (turn.total || 0) - turn.next_offset);
  const more = el('button', 'msg__more', fill(text('chat.more'), { count: remaining }));
  more.type = 'button';
  more.addEventListener('click', async () => {
    more.disabled = true;
    reply.querySelector('.msg__fail')?.remove();
    try {
      const page = await api(`${BASE}/${ids.conversation}/turns/${ids.turn}?offset=${turn.next_offset}&limit=10`);
      if (rev !== revision) return;
      const first = appendCards(holder, Array.isArray(page.cards) ? page.cards : [], page.groups || turn.groups, true);
      addPager(reply, holder, page, ids, rev);
      // The button that was pressed is gone: keep the focus where the new parts start.
      if (first) {
        first.tabIndex = -1;
        first.focus({ preventScroll: true });
      }
    } catch (error) {
      if (rev !== revision) return;
      // Said right above the button, which stays live for another try.
      more.disabled = false;
      more.before(el('p', 'msg__fail', text('chat.more_failed') || failureText(error)));
    }
  });
  holder.after(more);
}

/** The next steps under a reply: what the assistant suggests, then the ways out of the chat. */
function nextSteps(reply, turn, steps, attempt, asking) {
  const cards = Array.isArray(turn.cards) ? turn.cards : [];
  const failed = turn.status === 'FAILED';
  // A failed turn gets the retry below, which sends the same message again into the same
  // bubbles. A suggestion from the server that would only do that is not shown beside it
  // (the engine writes the VIN of a message as [VIN]).
  const again = text('search.retry').toLowerCase();
  const resent = [attempt.content, attempt.content.replace(VIN, '[VIN]')];
  const found = [];
  for (const option of steps) {
    if (failed && (option.label.toLowerCase() === again || resent.includes(option.value))) continue;
    found.push(chip(option.label, () => send(option.value, { shown: option.label })));
  }
  if (failed) found.push(retryChip(reply, attempt));
  // An answer with no parts and nothing to choose from: the part search is one tap away.
  const bare = !cards.length && !asking;
  if (bare && parts) found.push(searchChip());
  // The shop can say more than the catalogue about a part that is not plainly in stock.
  const unsure = cards.some((card) => card.availability !== 'in_stock' && card.availability !== 'soon');
  if (bare || unsure) found.push(callLink());
  addSteps(reply, found);
}

/**
 * Draw a finished turn. `attempt.live` is the bubble that showed the parts found so far:
 * the reply is written into it, so what the reader was looking at stays where it is.
 */
function showTurn(turn, ids, rev, attempt) {
  const follow = !away;
  const failed = turn.status === 'FAILED';
  const reply = attempt.live || shell('assistant', turn.language);
  if (!attempt.live) log.append(reply);
  if (turn.language) reply.lang = turn.language;
  reply.removeAttribute('aria-busy');
  reply.classList.toggle('msg--error', failed);

  // A new paragraph, never new text in the old one: the log announces additions only.
  const lead = el('p', '', tidy(turn.answer) || text(failed ? 'chat.error' : 'chat.done'));
  lead.id = `chat-reply-${(serial += 1)}`;
  const old = reply.querySelector('p');
  if (old) old.replaceWith(lead);
  else reply.append(lead);

  // Text, note, then the answers, right under the question they belong to.
  const { answers, steps } = choicesOf(turn);
  const under = [];
  if (turn.note) under.push(el('p', 'msg__note', tidy(turn.note)));
  let group = null;
  if (answers.length) {
    group = answerGroup(answers, lead);
    under.push(group);
    if (text('chat.answer_hint')) under.push(el('p', 'ask__hint', text('chat.answer_hint')));
  }
  lead.after(...under);

  const cards = Array.isArray(turn.cards) ? turn.cards : [];
  let holder = reply.querySelector('.cards');
  if (cards.length) {
    if (!holder) {
      holder = cardHolder();
      reply.append(holder);
    }
    syncCards(holder, cards, turn.groups);
    addPager(reply, holder, turn, ids, rev);
  } else if (holder) {
    holder.remove();
  }
  nextSteps(reply, turn, steps, attempt, answers.length > 0);
  // Parts, more than two answers or a stacked group need the full width of the log.
  const stacked = Boolean(group && group.classList.contains('ask--stack'));
  reply.classList.toggle('msg--wide', cards.length > 0 || answers.length > 2 || stacked);

  if (turn.car) setCar(turn.car);
  // A turn that failed leaves the question before it open, with its answers live again.
  asked = answers.length > 0 || (failed && restore(attempt.open));
  setPlaceholder();
  // Someone answering from the keyboard is taken to the next answers; a pointer is never moved.
  if (group && viaKeyboard && document.activeElement === log) {
    group.querySelector('.chip').focus({ preventScroll: true });
  }
  viaKeyboard = false;
  settleView(reply, follow);
}

/** Close the assistant and take the visitor to the part search. */
function toSearch() {
  const go = () => {
    parts.scrollIntoView({ behavior: reducedMotion.matches ? 'auto' : 'smooth' });
    const heading = parts.querySelector('h2');
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  };
  if (inHistory) {
    // The sheet takes its history entry back as it closes, and the browser then restores
    // the scroll position of that entry: scroll only once that has happened.
    window.addEventListener('popstate', () => window.setTimeout(go, 60), { once: true });
  } else {
    window.setTimeout(go, 0);
  }
  close();
}

/* ---------------------------------------------------------------- protocol */

/** Tell the server a conversation is over. Nothing waits for the answer. */
function release(id) {
  if (id) fetch(`${BASE}/${id}`, { method: 'DELETE', credentials: 'omit', keepalive: true }).catch(() => {});
}

/**
 * Open the conversation while the visitor is still writing. The server gets ready when a
 * conversation opens (the assistant builds its catalogue index and reads its instructions
 * ahead), so the first reply does not have to wait for that as well. Sending the message
 * opens one itself when this did not happen or did not work.
 */
function openAhead() {
  if (conversation || opening || closed) return;
  const rev = revision;
  const request = api(BASE, { method: 'POST' })
    .then((created) => {
      // A "New chat" in between: this conversation is of no use to anybody.
      if (rev !== revision) release(created.conversation_id);
      else conversation = created.conversation_id;
    })
    .catch(() => {
      // The message asks again, and says what is wrong if it still is.
    })
    .finally(() => {
      if (opening === request) opening = null;
    });
  opening = request;
}

/**
 * Post the message, into the conversation that was opened ahead or one created now.
 * `choice` is the number of the answer that was tapped, when the message came from one.
 * Returns nothing when a "New chat" overtook it before it was sent.
 */
async function submit(content, choice, rev, retried = false) {
  if (opening) await opening;
  if (rev !== revision) return null;
  if (!conversation) {
    const created = await api(BASE, { method: 'POST' });
    if (rev !== revision) {
      release(created.conversation_id);
      return null;
    }
    conversation = created.conversation_id;
  }
  const id = conversation;
  const body = { message: content, language: 'auto' };
  if (choice) body.choice = choice;
  try {
    const started = await api(`${BASE}/${id}/messages`, { method: 'POST', body });
    spoken = true;
    return { conversation: id, turn: started.turn_id, restarted: false };
  } catch (error) {
    // An expired conversation is replaced, once. The new one never asked the question
    // the number belonged to, so the text goes alone. The visitor is told only when
    // something of theirs went with the old one: one that was merely opened held nothing.
    if (error.status === 404 && !retried && rev === revision) {
      const lost = spoken;
      conversation = null;
      spoken = false;
      const ids = await submit(content, 0, rev, true);
      return ids && { ...ids, restarted: lost };
    }
    throw error;
  }
}

/** Poll quickly at first (small talk answers in milliseconds), then ease off. */
async function poll(ids, rev, onRunning) {
  const url = `${BASE}/${ids.conversation}/turns/${ids.turn}?offset=0&limit=5`;
  const started = Date.now();
  let polls = 0;
  let throttled = 0;
  while (rev === revision) {
    let turn;
    try {
      turn = await api(url);
    } catch (error) {
      // The turn keeps running on the server; a throttled status read is retried.
      throttled += 1;
      if (error.status !== 429 || throttled > 30) throw error;
      await wait(2000);
      continue;
    }
    if (turn.status !== 'RUNNING') return turn;
    onRunning(turn);
    if (Date.now() - started > TURN_PATIENCE_MS) throw new Error('TIMEOUT');
    polls += 1;
    await wait(polls <= 5 ? 150 : polls <= 20 ? 500 : 1000);
  }
  return null;
}

/**
 * What the line under the log says while a turn runs: nothing at first (the dots are
 * enough), then that the assistant is looking, for which car when one is known and how
 * far through its searches, then that it is going through what they found, and after a
 * while that this one is slow. Never a made-up stage: each comes from the server's count.
 */
function progressText(running, elapsed) {
  if (elapsed < DOTS_MS) return '';
  if (elapsed >= SLOW_MS) return text('chat.slow', 'chat.checking');
  // Every search is back: what is left is choosing among the parts they found.
  if (running && running.checking > 0 && running.checked >= running.checking && text('chat.reading')) {
    return text('chat.reading');
  }
  let line = (car && fill(text('chat.checking_car'), { car })) || text('chat.looking', 'chat.checking');
  if (running && running.checking > 0) line += ` ${running.checked}/${running.checking}`;
  return line;
}

/** The bubble for parts found while the slower sources finish. It grows in place. */
function liveBubble(language) {
  const node = shell('assistant', language);
  node.classList.add('msg--wide');
  node.setAttribute('aria-busy', 'true');
  node.append(el('p', ''), cardHolder());
  log.append(node);
  return node;
}

/**
 * Send a message. `shown` is what the visitor's bubble says when that differs from what
 * is sent (a tapped answer shows its label), `origin` the answer button it came from,
 * `choice` that answer's number, and `quiet` skips the bubble for a second try.
 */
async function send(message, { quiet = false, shown = '', origin = null, choice = 0 } = {}) {
  const content = String(message).trim().slice(0, LIMIT);
  if (!content || busy || closed) return;
  busy = true;
  const rev = revision;
  const attempt = { content, shown, origin, choice, open: retire(origin), live: null };
  asked = false;
  hint = '';
  if (editingCar) carEdit(false);
  else setPlaceholder();
  syncSend();
  if (!quiet) {
    const typed = shown ? tidy(shown) : content;
    const masked = mask(typed);
    addMessage('user', masked);
    if (masked !== typed && !vinNoted) {
      vinNoted = true;
      addSystem(text('chat.vin_hidden'));
    }
  }
  let typing = addTyping();
  let live = null;
  let latest = null; // the newest snapshot of the running turn
  const started = Date.now();
  const say = () => {
    if (rev !== revision) return;
    const line = progressText(latest, Date.now() - started);
    if (progress.textContent !== line) progress.textContent = line;
  };
  // The line changes with time as well as with what the server reports.
  const timers = [window.setTimeout(say, DOTS_MS), window.setTimeout(say, SLOW_MS)];
  const settle = () => {
    for (const timer of timers) window.clearTimeout(timer);
    typing?.remove();
    typing = null;
    live?.remove();
    live = null;
    progress.textContent = '';
  };
  try {
    const ids = await submit(content, choice, rev);
    if (rev !== revision || !ids) return;
    if (ids.restarted) {
      // The old conversation had expired and with it the car: say so, once, and start clean.
      setCar('');
      addSystem(text('chat.expired'), typing);
    }
    const turn = await poll(ids, rev, (running) => {
      latest = running;
      if (running.car) setCar(running.car);
      say();
      const cards = Array.isArray(running.cards) ? running.cards : [];
      if (!cards.length) return;
      // Parts found so far appear while the slower sources finish: one bubble, in place.
      const follow = !away;
      if (!live) {
        typing?.remove();
        typing = null;
        live = liveBubble(running.language);
      }
      const count = Math.max(running.total || 0, cards.length);
      const line = fill(text('chat.found_count'), { count }) || text('chat.found', 'chat.checking');
      const lead = live.querySelector('p');
      if (lead.textContent !== line) lead.textContent = line;
      syncCards(live.querySelector('.cards'), cards, running.groups);
      settleView(live, follow);
    });
    if (rev !== revision || !turn) return;
    attempt.live = live;
    live = null; // it becomes the reply: settle() must not take it away
    settle();
    showTurn(turn, ids, rev, attempt);
  } catch (error) {
    if (rev !== revision) return;
    settle();
    showFailure(error, attempt);
  } finally {
    if (rev === revision) {
      settle();
      busy = false;
      syncSend();
    }
  }
}

function forget() {
  const id = conversation;
  conversation = null;
  spoken = false;
  release(id);
}

/* ----------------------------------------------------------------- car line */

/**
 * The example text in the empty composer follows what the chat is waiting for: another
 * car, an answer to the open question, the next part, or (at first) a whole request.
 */
function setPlaceholder() {
  const wanted =
    (hint && text(hint)) ||
    (editingCar && text('chat.car_placeholder')) ||
    (asked && text('chat.placeholder_answer')) ||
    (car && text('chat.placeholder_part')) ||
    examplePlaceholder;
  if (input.placeholder === wanted) return;
  input.placeholder = wanted;
  reserve();
}

/** Show the car the conversation is about. Only a new chat takes the line away again. */
function setCar(label) {
  car = label;
  if (carBar) {
    carName.textContent = car;
    carBar.hidden = !car;
  }
  if (!car && editingCar) carEdit(false);
  else setPlaceholder();
}

/**
 * "Change" sends nothing. It marks the line and asks for another car in the composer;
 * the next message is an ordinary one, which the assistant reads as the new car.
 */
function carEdit(on) {
  editingCar = on;
  if (carBar) {
    carBar.toggleAttribute('data-editing', on);
    carChange.textContent = (on && text('chat.car_cancel')) || changeLabel;
  }
  setPlaceholder();
  if (on) input.focus();
}

/* -------------------------------------------------------------- empty state */

/** The requests people make most, one tap each. A string that is missing draws no button. */
function addStarters() {
  const buttons = [];
  for (const [key, glyph] of STARTERS) {
    const label = text(key);
    if (!label) continue;
    const button = tapButton('starter', () => {
      if (key !== 'chat.starter_vin') {
        send(label);
        return;
      }
      // A VIN has to be typed: point the composer at it and wait.
      hint = 'chat.vin_placeholder';
      setPlaceholder();
      input.focus();
    });
    button.append(icon(glyph), el('span', '', label), icon('arrow-right'));
    buttons.push(button);
  }
  if (!buttons.length) return;
  const group = el('div', 'starters');
  const title = text('chat.starters');
  if (title) {
    const heading = el('p', 'starters__title', title);
    heading.id = `chat-starters-${(serial += 1)}`;
    group.setAttribute('role', 'group');
    group.setAttribute('aria-labelledby', heading.id);
    group.append(heading);
  }
  group.append(...buttons);
  log.append(group);
}

function greet() {
  greeted = true;
  if (data.chat && data.chat.greeting) addMessage('assistant', data.chat.greeting, data.lang);
  addStarters();
}

function reset() {
  revision += 1;
  forget();
  busy = false;
  closed = false;
  asked = false;
  hint = '';
  vinNoted = false;
  viaKeyboard = false;
  anchor = null;
  placedAt = 0;
  away = false;
  clearPill();
  input.disabled = false;
  progress.textContent = '';
  log.replaceChildren();
  editingCar = false;
  if (carBar) {
    carBar.removeAttribute('data-editing');
    carChange.textContent = changeLabel;
  }
  setCar('');
  greet();
  syncSend();
  // On a phone the keyboard would cover the starters before they are read.
  if (modal) log.focus({ preventScroll: true });
  else input.focus();
}

/* ----------------------------------------------------------------- composer */

/** Nothing to send, a turn in flight or a closed composer: the button says so. */
function syncSend() {
  sendButton.disabled = busy || closed || !input.value.trim();
}

/** The counter stays out of the way until the limit is near. */
function showCount() {
  const length = input.value.length;
  counter.textContent = `${length}/${LIMIT}`;
  counter.hidden = length < COUNT_FROM;
  counter.toggleAttribute('data-near', length >= COUNT_NEAR);
}

function grow() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight + 2, 132)}px`;
}

/**
 * Keep the empty composer as tall as its example text. On a narrow phone the example
 * wraps onto a second line, which a one-line box would cut off. Measured with the box
 * itself, and only while it is empty, so nothing a visitor typed is ever touched.
 */
function reserve() {
  if (input.value || !input.placeholder) return;
  input.style.minHeight = '';
  input.style.height = 'auto';
  input.value = input.placeholder;
  const needed = input.scrollHeight + 2;
  input.value = '';
  input.style.minHeight = `${Math.min(needed, 132)}px`;
  grow();
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const value = input.value;
  if (!value.trim() || busy || closed) return;
  input.value = '';
  grow();
  showCount();
  send(value);
});

// Like a messenger: Enter sends, Shift+Enter starts a new line.
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener('input', () => {
  grow();
  showCount();
  syncSend();
  // The first words of a message: the server can get ready while the rest is written.
  if (input.value.trim()) openAhead();
});

showCount();
syncSend();

/* ------------------------------------------------------------ open and close */

/**
 * A phone's keyboard covers the bottom of the page without resizing it: only the part that
 * can be seen (the visual viewport) shrinks, and the browser may slide that part up to the
 * field and back. Keep the sheet exactly on it. Zoomed in with two fingers the sheet is
 * left as the stylesheet has it: the visitor is moving around inside it. Returns whether
 * the sheet was placed.
 */
function place() {
  if (!modal || !dialog.open || !viewport) return false;
  const zoomed = Math.abs(viewport.scale - 1) > 0.01;
  const height = zoomed ? '' : `${Math.round(viewport.height)}px`;
  const top = zoomed ? '' : `${Math.round(viewport.offsetTop)}px`;
  if (dialog.style.height !== height) dialog.style.height = height;
  if (dialog.style.top !== top) dialog.style.top = top;
  return !zoomed;
}

/** Back on the message the view was last placed on, unless the reader has moved since. */
function replace() {
  if (anchor && anchor.isConnected && !away) reveal(anchor);
}

/** The visible part changed size (keyboard, rotation): place the sheet, keep the reply in view. */
function fit() {
  if (!place()) return;
  reserve();
  replace();
}

viewport?.addEventListener('resize', fit);
// It also slides without changing size: after the keyboard has gone, or under a finger.
viewport?.addEventListener('scroll', place);

/**
 * Open the assistant. `prefill` puts text in the composer without sending it (the part
 * search passes what was searched). `opener` gets the focus back afterwards.
 */
export function open({ opener: from = null, prefill = '' } = {}) {
  const wasOpen = dialog.open;
  if (!wasOpen) {
    opener = from && from.isConnected ? from : launcher;
    modal = phone.matches;
    if (modal) {
      dialog.showModal();
      fit();
      // One history entry, so the phone's back button closes the sheet.
      window.history.pushState({ chat: true }, '');
      inHistory = true;
    } else {
      dialog.show();
    }
    announce(true);
    if (!greeted) greet();
    reserve();
  }
  if (prefill) {
    input.value = prefill.slice(0, LIMIT);
    grow();
    showCount();
    syncSend();
    if (input.value.trim()) openAhead(); // words handed over are about to be sent
  }
  // On a phone the keyboard would cover half the sheet before anything in it is read:
  // the composer takes the focus there only when it was handed something to send.
  if (modal && !prefill) log.focus({ preventScroll: true });
  else input.focus();
  // A closed dialog forgets where its log was: it comes back on the reply it last showed.
  if (!wasOpen && anchor && anchor.isConnected) reveal(anchor);
  else replace();
}

export function close() {
  if (dialog.open) dialog.close();
}

dialog.addEventListener('close', () => {
  announce(false);
  dialog.style.height = '';
  dialog.style.top = '';
  if (inHistory) {
    // Closed with the button or Escape: drop the entry the sheet added.
    inHistory = false;
    window.history.back();
  }
  // A modal dialog hands focus back by itself; the panel has to be told.
  if (!modal) {
    const target = opener && opener.isConnected && !opener.hidden ? opener : launcher;
    target?.focus({ preventScroll: true });
  }
});

window.addEventListener('popstate', () => {
  if (dialog.open && inHistory) {
    inHistory = false;
    dialog.close();
  }
});

dialog.addEventListener('keydown', (event) => {
  // Escape closes a modal dialog natively; the panel needs it spelled out.
  if (event.key === 'Escape' && !modal) {
    event.preventDefault();
    close();
  }
});

dialog.addEventListener('click', (event) => {
  if (event.target.closest('[data-chat-close]')) {
    close();
  } else if (event.target.closest('[data-chat-new]')) {
    reset();
  } else if (event.target.closest('[data-chat-car]')) {
    carEdit(!editingCar);
  } else if (event.target.closest('[data-chat-jump]')) {
    // The button hides itself: the focus goes to the log it leads into.
    const target = unread && unread.isConnected ? unread : log.lastElementChild;
    log.focus({ preventScroll: true });
    if (target) reveal(target);
  }
});

// Sheet and panel are different layouts: crossing the breakpoint closes whichever is open.
phone.addEventListener('change', close);

// Free the conversation when the page goes away.
window.addEventListener('pagehide', forget);
