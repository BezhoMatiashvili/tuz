/**
 * Part search: three modes (car, VIN, name or brand) over the public catalogue API.
 *
 * A result is the item's name and its price, with one muted line for brand and fitment.
 * Nothing else is shown. Every state is drawn here: idle, loading (skeleton rows the
 * size of real ones), results, empty, error and rate-limited. A new request aborts the
 * one in flight. Loaded on demand by site.js.
 */

import { api, el, fill, icon, number, price, text, tidy } from './dom.js';

const VIN_OK = /^[A-HJ-NPR-Z0-9]{17}$/;
const SKELETON_ROWS = 6;
const SLOW_VIN_MS = 2500;

const form = document.getElementById('parts-form');
const box = document.getElementById('parts-results');
const status = box?.querySelector('.results__status');
const body = box?.querySelector('.results__body');
const field = (id) => document.getElementById(id);
const make = field('pf-make');
const model = field('pf-model');
const year = field('pf-year');
const keyword = field('pf-q');
const brand = field('pf-brand');
const vin = field('pf-vin');
const vinCount = field('pf-vin-count');
const formStatus = field('pf-status');
const submit = form?.querySelector('.pf__submit');

let wired = false;
let options = null; // the make and brand lists, once fetched
let optionsRequest = null;
let searching = null; // AbortController of the search in flight
let decoding = null; // AbortController of the VIN lookup in flight
let modelsFor = ''; // the make whose models are in the select

const mode = () => form.elements.mode.value;

/* ------------------------------------------------------------------ states */

function setState(state, message) {
  box.dataset.state = state;
  status.textContent = message || '';
}

function clearBody() {
  body.replaceChildren();
}

function showIdle() {
  setState('idle', text('search.need'));
  clearBody();
  if (!options || !options.makes.length) return;
  // A short cut for the makes the shop stocks most.
  const group = el('div', 'makes');
  const title = text('search.makes');
  if (title) group.append(el('p', 'makes__title', title));
  const list = el('div', 'makes__list');
  for (const entry of options.makes.slice(0, 8)) {
    const chip = el('button', 'chip', entry.name);
    chip.type = 'button';
    chip.append(el('small', '', number(entry.count)));
    chip.addEventListener('click', () => chooseMake(entry.key));
    list.append(chip);
  }
  group.append(list);
  body.append(group);
}

function showLoading() {
  setState('loading', text('search.loading'));
  const list = el('ul', 'results__list');
  list.setAttribute('aria-hidden', 'true');
  for (let index = 0; index < SKELETON_ROWS; index += 1) {
    const row = el('li', 'result result--skeleton');
    row.append(el('i'), el('i'), el('i'));
    list.append(row);
  }
  body.replaceChildren(list);
}

function resultRow(item) {
  const row = el('li', 'result');
  const cost = el('span', 'result__price', price(item.price));
  if (!(item.price > 0)) cost.classList.add('result__price--ask');
  row.append(el('span', 'result__name', tidy(item.name)), cost);
  // The one place a middle dot is used: brand, then what the part fits.
  const meta = [item.brand, item.fits].filter(Boolean).join(' \u00B7 ');
  if (meta) row.append(el('span', 'result__meta', tidy(meta)));
  return row;
}

function describe(query) {
  const makeName = make.selectedOptions[0]?.textContent || '';
  const modelName = model.selectedOptions[0]?.textContent || '';
  return [query.make ? makeName : '', query.model ? modelName : '', query.year, query.q, query.brand]
    .filter(Boolean)
    .join(' ');
}

function actionRow(...buttons) {
  const row = el('div', 'results__actions');
  row.append(...buttons.filter(Boolean));
  return row;
}

function askButton(query) {
  // site.js opens the chat for any [data-chat-open]; with the chat off there is none.
  if (document.body.dataset.chat !== 'on') return null;
  const button = el('button', 'btn btn--primary');
  button.type = 'button';
  button.dataset.chatOpen = '';
  button.dataset.chatPrefill = describe(query);
  button.append(icon('chat-text'), el('span', '', text('search.ask')));
  return button;
}

function callButton() {
  if (!box.dataset.phone) return null;
  const link = el('a', 'btn btn--ghost');
  link.href = `tel:${box.dataset.phone}`;
  link.append(icon('phone'), el('span', '', box.dataset.call || ''));
  return link;
}

function showEmpty(query) {
  setState('empty', text('search.empty'));
  clearBody();
  const ask = askButton(query);
  const call = callButton();
  if (ask) body.append(el('p', 'results__hint', text('search.empty_hint')));
  if (ask || call) body.append(actionRow(ask, call));
}

function showFailure(error, retry) {
  const limited = error.status === 429;
  setState(limited ? 'busy' : 'error', text(limited ? 'search.busy' : 'search.error'));
  clearBody();
  const again = el('button', 'btn btn--ghost');
  again.type = 'button';
  again.append(icon('arrow-clockwise'), el('span', '', text('search.retry')));
  again.addEventListener('click', retry);
  body.append(actionRow(again, callButton()));
}

function showResults(result, query, append) {
  setState('results', fill(text('search.count'), { count: number(result.total) }));
  let list = body.querySelector('.results__list:not([aria-hidden])');
  if (!append || !list) {
    clearBody();
    if (result.freshness === 'confirm' || result.freshness === 'stale') {
      const note = el('p', 'results__note');
      note.append(icon('info'), el('span', '', text(`search.${result.freshness}`)));
      body.append(note);
    }
    list = el('ul', 'results__list');
    // Product names are Georgian whatever the page language is.
    list.lang = 'ka';
    body.append(list);
  }
  body.querySelector('.results__more')?.remove();
  const first = list.children.length;
  for (const item of result.items) list.append(resultRow(item));
  if (result.has_more) {
    const more = el('button', 'btn btn--ghost results__more', text('search.more'));
    more.type = 'button';
    more.addEventListener('click', () => {
      more.disabled = true;
      run({ ...query, page: result.page + 1 }, true);
    });
    body.append(more);
  }
  if (append && list.children[first]) {
    // Keep keyboard users where the new rows start.
    list.children[first].tabIndex = -1;
    list.children[first].focus({ preventScroll: true });
  }
}

/* ------------------------------------------------------------------ search */

function currentQuery() {
  const query = { make: '', model: '', year: '', q: keyword.value.trim(), brand: '', page: 1 };
  if (mode() === 'name') {
    query.brand = brand.value.trim();
  } else {
    query.make = make.value;
    query.model = query.make ? model.value : '';
    query.year = query.model ? year.value : '';
  }
  return query;
}

async function run(query, append = false) {
  searching?.abort();
  if (!query.make && !query.q && !query.brand) {
    showIdle();
    return;
  }
  const controller = new AbortController();
  searching = controller;
  if (append) box.setAttribute('data-busy', '');
  else showLoading();
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value && !(key === 'page' && value === 1)) params.set(key, String(value));
  }
  try {
    const result = await api(`/api/site/parts/search?${params}`, { signal: controller.signal });
    if (controller.signal.aborted) return;
    if (!result.total) showEmpty(query);
    else showResults(result, query, append);
  } catch (error) {
    if (error.name === 'AbortError') return;
    const more = append && body.querySelector('.results__more');
    if (more) {
      // Keep the rows already shown: say what happened and let "show more" try again.
      status.textContent = text(error.status === 429 ? 'search.busy' : 'search.error');
      more.disabled = false;
    } else {
      showFailure(error, () => run(query, false));
    }
  } finally {
    if (searching === controller) {
      searching = null;
      box.removeAttribute('data-busy');
    }
  }
}

const search = () => run(currentQuery());

/* ------------------------------------------------------------- car controls */

function option(value, label) {
  const node = el('option', '', label);
  node.value = value;
  return node;
}

function fillSelect(select, placeholder, entries) {
  select.replaceChildren(option('', placeholder), ...entries.map((entry) => option(entry.key, entry.name)));
}

function loadOptions() {
  optionsRequest ||= api('/api/site/parts/options')
    .then((result) => {
      options = result;
      const chosen = make.value;
      fillSelect(make, make.options[0].textContent, result.makes);
      make.value = chosen;
      field('pf-brands').replaceChildren(...result.brands.map((name) => option(name, '')));
      if (box.dataset.state === 'idle') showIdle();
      return result;
    })
    .catch(() => {
      // Try again on the next visit to the form; the name search works without the lists.
      optionsRequest = null;
      return null;
    });
  return optionsRequest;
}

async function loadModels(key) {
  const placeholder = model.options[0].textContent;
  modelsFor = key;
  model.disabled = true;
  year.disabled = true;
  year.value = '';
  if (!key) {
    fillSelect(model, placeholder, []);
    return;
  }
  model.replaceChildren(option('', text('search.models_loading')));
  let entries = [];
  try {
    entries = (await api(`/api/site/parts/models?make=${encodeURIComponent(key)}`)).models;
  } catch {
    // Without the model list the make alone still searches.
  }
  if (modelsFor !== key) return;
  fillSelect(model, placeholder, entries);
  model.disabled = entries.length === 0;
}

function fillYears() {
  const now = new Date().getFullYear() + 1;
  for (let value = now; value >= 1985; value -= 1) year.append(option(String(value), String(value)));
}

async function chooseMake(key) {
  setMode('car');
  await loadOptions();
  make.value = key;
  await loadModels(make.value);
  search();
  make.focus({ preventScroll: true });
}

/* --------------------------------------------------------------------- mode */

function setMode(next) {
  for (const radio of form.elements.mode) radio.checked = radio.value === next;
  for (const node of form.querySelectorAll('[data-modes]')) {
    node.hidden = !node.dataset.modes.split(' ').includes(next);
  }
  const label = submit.querySelector('span');
  label.textContent = next === 'vin' ? submit.dataset.labelDecode : submit.dataset.labelSearch;
}

function say(message, tone) {
  formStatus.textContent = message || '';
  if (tone) formStatus.dataset.tone = tone;
  else delete formStatus.dataset.tone;
}

/* ---------------------------------------------------------------------- VIN */

/** Keep only what a VIN can hold. I, O and Q are never used: they are 1, 0 and 0 mistyped. */
function cleanVin(value) {
  return value
    .toUpperCase()
    .replace(/[IOQ]/g, (letter) => (letter === 'I' ? '1' : '0'))
    .replace(/[^A-HJ-NPR-Z0-9]/g, '')
    .slice(0, 17);
}

function showVinCount() {
  vinCount.textContent = fill(text('vin.count') || '{count}/17', { count: vin.value.length });
}

function carLabel(vehicle) {
  // The decoder reports displacement to nine places (2.998832712 L): one is plenty.
  const engine = String(vehicle.engine || '').replace(/\d+\.\d{2,}/g, (litres) => Number(litres).toFixed(1));
  return [vehicle.make, vehicle.model, vehicle.year, engine].filter(Boolean).join(' ');
}

async function decode() {
  const value = cleanVin(vin.value);
  vin.value = value;
  showVinCount();
  if (!VIN_OK.test(value)) {
    say(text('vin.invalid'), 'error');
    vin.setAttribute('aria-invalid', 'true');
    vin.focus();
    return;
  }
  vin.removeAttribute('aria-invalid');
  decoding?.abort();
  const controller = new AbortController();
  decoding = controller;
  submit.disabled = true;
  say(text('vin.working'));
  // The decoder is a public service and can be slow: say so rather than look stuck.
  const slow = window.setTimeout(() => say(text('vin.slow', 'vin.working')), SLOW_VIN_MS);
  try {
    const result = await api('/api/site/vin', { method: 'POST', body: { vin: value }, signal: controller.signal });
    if (controller.signal.aborted) return;
    const label = carLabel(result.vehicle || {});
    setMode('car');
    if (!result.make_key) {
      // Read, but not a make the shop lists: say which car it is and ask for the make.
      make.value = '';
      await loadModels('');
      showIdle();
      const found = label ? fill(text('vin.found'), { car: label }) : '';
      say(found ? `${found}. ${text('vin.no_make', 'search.need')}` : text('vin.unknown'), found ? '' : 'error');
      make.focus();
      return;
    }
    await loadOptions();
    make.value = result.make_key;
    await loadModels(make.value);
    if (!make.value) {
      say(text('vin.unknown'), 'error');
      make.focus();
      return;
    }
    if (result.model_key) {
      // The year stays open: narrowing by it is the visitor's call, and it often empties
      // a list the model alone would fill.
      model.value = result.model_key;
      year.disabled = !model.value;
    }
    const found = fill(text('vin.found'), { car: label });
    say(result.model_key && model.value ? found : `${found}. ${text('vin.no_parts')}`, 'ok');
    search();
  } catch (error) {
    if (error.name === 'AbortError') return;
    if (error.status === 422) say(text('vin.invalid'), 'error');
    else if (error.status === 429 && error.code !== 'VIN_BUDGET_EXHAUSTED') say(text('search.busy'), 'error');
    else say(text('vin.unavailable'), 'error');
  } finally {
    window.clearTimeout(slow);
    if (decoding === controller) {
      decoding = null;
      submit.disabled = false;
    }
  }
}

/* ------------------------------------------------------------------- wiring */

/** Attach the form. Safe to call more than once. */
export function init() {
  if (wired || !form || !box) return;
  wired = true;
  fillYears();
  setMode(mode());

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (mode() === 'vin') decode();
    else search();
  });
  form.addEventListener('change', (event) => {
    const target = event.target;
    if (target.name === 'mode') {
      decoding?.abort();
      say('');
      setMode(target.value);
    } else if (target === make) {
      say('');
      loadModels(make.value).then(search);
    } else if (target === model) {
      year.disabled = !model.value;
      if (!model.value) year.value = '';
      search();
    } else if (target === year) {
      search();
    }
  });
  vin.addEventListener('input', () => {
    const cleaned = cleanVin(vin.value);
    if (cleaned !== vin.value) vin.value = cleaned;
    vin.removeAttribute('aria-invalid');
    showVinCount();
  });
  // maxlength would cut a pasted VIN with spaces or dashes short before it is cleaned.
  vin.addEventListener('paste', (event) => {
    const pasted = event.clipboardData?.getData('text');
    if (!pasted) return;
    event.preventDefault();
    vin.value = cleanVin(pasted);
    vin.removeAttribute('aria-invalid');
    showVinCount();
  });
  form.addEventListener('focusin', loadOptions, { once: true });
}

/** Fetch the make and brand lists (the section is near, or about to be used). */
export function prepare() {
  init();
  return loadOptions();
}

/** Search by name for `query`: what a part link elsewhere on the page asks for. */
export async function searchFor(query) {
  init();
  setMode('name');
  say('');
  keyword.value = String(query || '').slice(0, 80);
  brand.value = '';
  await search();
}
