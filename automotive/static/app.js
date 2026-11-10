'use strict';
const byId = (id) => document.getElementById(id);
const vehicleKeys = ['make', 'model', 'model_year', 'engine_code', 'trim', 'displacement_l', 'fuel', 'transmission', 'market', 'body', 'drive'];
let vehicle = null;
let contextRevision = 0;
let unifiedRevision = 0;
let conversationId = null;
let chatRevision = 0;
let chatBusy = false;
const statuses = {VERIFIED: 'დადასტურებული', PROVISIONAL: 'წინასწარი მონაცემი', CONFLICTED: 'ურთიერთსაწინააღმდეგო წყაროები', UNKNOWN: 'უცნობია', INSUFFICIENT_DATA: 'მონაცემები არასაკმარისია', OK: 'მზადაა', COMPLETE: 'ძიება დასრულდა', PARTIAL: 'ნაწილობრივი შედეგი', RUNNING: 'იძებნება…', PENDING: 'მოლოდინშია', FAILED: 'ძიება ვერ შესრულდა', ERROR: 'მიუწვდომელია', EMPTY: 'ჩანაწერი ვერ მოიძებნა', DISABLED: 'გამორთულია', SKIPPED: 'ვერ შემოწმდა'};
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
// A scroll started from script ignores the CSS reduced-motion rule, so the choice is made here.
function scrollBehavior() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}
async function api(path, body, checkContext = true, method = null) {
  const revision = contextRevision;
  const response = await fetch('/api/v1' + path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  });
  if (!response.ok) {
    // The workspace sits behind the staff login; an expired session goes back to it.
    if (response.status === 401 && typeof location !== 'undefined') location.assign('/login?next=staff');
    const problem = await response.json().catch(() => ({}));
    const codes = {
      SEARCH_EXPIRED: 'ძიების შედეგს ვადა გაუვიდა. ხელახლა გაუშვით VIN-ით ძიება.',
      SEARCH_CAPACITY: 'მიმდინარე ძიებების ლიმიტი ამოიწურა. სცადეთ მოგვიანებით.',
      DATABASE_NOT_CONFIGURED: 'საწყობის მონაცემთა ბაზა ჯერ არ არის დაკავშირებული. საჭიროა ლოკალური გამართვა.',
      MOTORIUM_DISABLED: 'Motorium-ის კავშირი გამორთულია.',
      MOTORIUM_ACCESS_REQUIRED: 'Motorium-მა მოითხოვა ავტორიზაცია ან წვდომის დადასტურება.',
      MOTORIUM_RATE_LIMITED: 'Motorium-ის მოთხოვნების ლიმიტი ამოიწურა. სცადეთ მოგვიანებით.',
      MOTORIUM_INVALID_RESPONSE: 'Motorium-ის პასუხის ფორმატი შეიცვალა; კოდები ვერ დადასტურდა.',
      MOTORIUM_PROVIDER_UNAVAILABLE: 'Motorium ახლა მიუწვდომელია. სცადეთ მოგვიანებით.',
      VIN_PROVIDER_UNAVAILABLE: 'VIN-ის გარე მომწოდებელი ახლა მიუწვდომელია. სცადეთ მოგვიანებით ან მიუთითეთ ავტომობილის დეტალები ხელით.',
    };
    if (codes[problem.detail?.code]) throw new Error(codes[problem.detail.code]);
    const messages = {401: 'საჭიროა ოპერატორის ავტორიზაცია.', 403: 'მოთხოვნის წყარო დაუშვებელია.', 404: 'ჩანაწერი ვერ მოიძებნა.', 422: 'შეამოწმეთ შეყვანილი მონაცემები.', 429: 'მოთხოვნების ლიმიტი ამოიწურა. სცადეთ ერთ წუთში.', 503: 'სერვისი დროებით მიუწვდომელია. შეამოწმეთ კავშირი და კონფიგურაცია.'};
    const error = new Error(messages[response.status] || 'მოთხოვნა ვერ შესრულდა. სცადეთ ხელახლა.');
    error.status = response.status;
    throw error;
  }
  const result = response.status === 204 ? null : await response.json();
  if (checkContext && revision !== contextRevision) throw new Error('STALE_CONTEXT');
  return result;
}
function renderStatus(target, record) {
  const status = record.verification_status || record.status;
  if (status) target.append(element('span', statuses[status] || status, 'status ' + status));
  for (const warning of record.warnings || []) target.append(element('p', warning, 'warning'));
}
function evidenceLink(target, evidence) {
  const id = typeof evidence === 'object' ? evidence.evidence_id : evidence;
  if (!id) return;
  const link = element('a', 'მტკიცებულება #' + id, 'evidence-link');
  link.href = '/api/v1/evidence/' + encodeURIComponent(id);
  link.addEventListener('click', async (event) => {
    event.preventDefault();
    byId('evidence-panel').hidden = false;
    await perform(byId('evidence-result'), async () => {
      const record = await api('/evidence/' + encodeURIComponent(id));
      render(byId('evidence-result'), record);
    });
    byId('evidence-panel').scrollIntoView({behavior: scrollBehavior(), block: 'start'});
  });
  target.append(link);
}
function stockText(stock) {
  if (!stock || typeof stock !== 'object') return 'უცნობია';
  const parts = [];
  if (stock.store != null) parts.push('მაღაზია: ' + stock.store);
  if (stock.tuz11 != null) parts.push('TUZ 11: ' + stock.tuz11 + (stock.tuz11 > 0 ? ' (1–2 საათში)' : ''));
  return parts.join(' · ') || 'უცნობია';
}
function product(target, item) {
  const card = element('article', null, 'product');
  renderStatus(card, item);
  card.append(element('h3', item.product_name || item.name || 'კატალოგის ჩანაწერი'));
  card.append(element('p', item.product_code || item.code || item.source_product_id || '', 'code'));
  if (item.brand) card.append(element('p', 'მწარმოებელი: ' + item.brand));
  if (item.product_model) card.append(element('p', 'კატალოგის აღწერა: ' + item.product_model));
  card.append(element('p', 'ფასი: ' + (item.price == null ? 'უცნობია' : item.price + ' ' + (item.currency || 'ვალუტა უცნობია')) + ' · მარაგი: ' + stockText(item.stock)));
  if (item.last_observed_at) card.append(element('p', 'ბოლო დაკვირვება: ' + item.last_observed_at, 'muted'));
  if (item.matched_codes?.length) card.append(element('pre', JSON.stringify(item.matched_codes, null, 2)));
  for (const evidence of item.evidence || []) evidenceLink(card, evidence);
  target.append(card);
}
const labels = {make: 'მწარმოებელი', model: 'მოდელი', model_year: 'მოდელის წელი', trim: 'ვერსია', engine_code: 'ძრავის კოდი', engine_model: 'ძრავის აღწერა', fuel: 'საწვავი', transmission: 'ტრანსმისია', market: 'ბაზარი', body: 'ძარის ტიპი', drive: 'ამძრავი', displacement_l: 'ძრავის მოცულობა (ლ)', engine_cylinders: 'ცილინდრები', source_name: 'წყარო', source_url: 'წყაროს მისამართი', authority: 'უფლებამოსილება', reuse_permission: 'გამოყენების უფლება', review_status: 'გადახედვის სტატუსი', reviewed_by: 'შემმოწმებელი', reviewed_at: 'გადამოწმების დრო', observed_at: 'დაკვირვების დრო', part_code: 'ნაწილის კოდი', from_code: 'საწყისი კოდი', to_code: 'შემცვლელი კოდი', relation: 'მიმართება', approval: 'დაშვება', approvals: 'დაშვებები', specification: 'სპეციფიკაცია', viscosity: 'სიბლანტე', approval_code: 'დაშვების კოდი', requirements: 'მოთხოვნები', applicability: 'გამოყენების პირობები', restrictions: 'შეზღუდვები', supersession: 'ჩანაცვლების პირობები', compatible: 'თავსებადობა', payload: 'წყაროს მტკიცება', notes: 'შენიშვნა', direction: 'მიმართულება', kind: 'მტკიცებულების ტიპი', value: 'მნიშვნელობა', unit: 'ერთეული'};
const hiddenKeys = new Set(['evidence_id', 'id', 'fingerprint', 'source_fingerprint', 'parser_version', 'status', 'verification_status', 'warnings', 'evidence', 'usage', 'result_id', 'tool']);
function facts(target, record) {
  const list = element('dl', null, 'facts');
  for (const [key, value] of Object.entries(record || {})) {
    if (hiddenKeys.has(key)) continue;
    list.append(element('dt', labels[key] || key.replaceAll('_', ' ')));
    const detail = element('dd');
    if (value === null || value === undefined || value === '') detail.textContent = 'უცნობია';
    else if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === 'object' && item !== null) facts(detail, item);
        else detail.append(element('p', item));
      }
      if (!value.length) detail.textContent = 'მონაცემები არ არის';
    } else if (typeof value === 'object') facts(detail, value);
    else detail.textContent = typeof value === 'boolean' ? (value ? 'დიახ' : 'არა') : String(value);
    list.append(detail);
  }
  target.append(list);
}
function render(target, record) {
  target.replaceChildren();
  renderStatus(target, record);
  const answer = record.answer || record.clarification || record.message;
  if (answer) target.append(element('p', answer));
  if (record.verified_count !== undefined) target.append(element('p', 'დადასტურებული: ' + record.verified_count + ' · შესამოწმებელი კანდიდატი: ' + record.candidate_count));
  if (record.has_more) target.append(element('p', 'მეტი ჩანაწერი არსებობს. დააზუსტეთ კონფიგურაცია ან მოძებნეთ ნაწილის კოდი.', 'muted'));
  if (record.vehicle) facts(target, record.vehicle);
  if (record.checksum) target.append(element('p', 'VIN შემოწმება: ' + JSON.stringify(record.checksum), 'muted'));
  for (const item of record.items || []) {
    if (item.product) {
      const card = element('article', null, 'product');
      renderStatus(card, item);
      if (item.match_type) card.append(element('p', item.match_type === 'VERIFIED_FITMENT' ? 'დადასტურებული კავშირი ამ ავტომობილთან' : 'შესამოწმებელი კანდიდატი — თავსებადობა დაუდასტურებელია', 'muted'));
      product(card, {...item.product, status: item.status});
      for (const reference of item.evidence || []) evidenceLink(card, reference);
      target.append(card);
    } else if ('source_product_id' in item || 'product_code' in item) product(target, item);
    else {
      const card = element('article', null, 'product');
      renderStatus(card, item);
      facts(card, item);
      if (item.evidence_id) evidenceLink(card, item);
      target.append(card);
    }
  }
  if (Array.isArray(record.items) && !record.items.length) target.append(element('p', 'შესაბამისი დადასტურებული ჩანაწერი ვერ მოიძებნა.', 'muted'));
  for (const evidence of record.evidence || []) evidenceLink(target, evidence);
  if (record.compatible === false) target.append(element('p', 'არსებული მტკიცებულება შესაბამისობას უარყოფს.', 'warning'));
  for (const candidate of record.candidates || []) product(target, candidate);
  for (const result of record.results || []) {
    const section = element('section', null, 'product');
    section.append(element('h3', 'კატალოგის პასუხი · ' + (result.result_id || '')));
    const content = element('div');
    render(content, result.data || result);
    section.append(content);
    target.append(section);
  }
  if (record.records) for (const row of record.records) facts(target, row);
  if (record.source_name || record.payload) facts(target, record);
}
async function perform(target, action, form) {
  const loading = element('p', 'მონაცემები იტვირთება…', 'muted');
  target.replaceChildren(loading);
  const buttons = form ? form.querySelectorAll('button') : [];
  buttons.forEach((button) => {button.disabled = true;});
  try { await action(); }
  catch (error) {
    if (error.message !== 'STALE_CONTEXT') target.replaceChildren(element('p', error.message, 'error'));
    else if (target.contains(loading)) loading.remove();
  }
  finally {buttons.forEach((button) => {button.disabled = false;});}
}
function readVehicle() {
  const result = {};
  for (const key of vehicleKeys) {
    const value = byId(key).value.trim();
    if (value) result[key] = ['model_year', 'displacement_l'].includes(key) ? Number(value) : value;
  }
  return Object.keys(result).length ? result : null;
}
function clearDependentResults(clearVehicleParts = false) {
  contextRevision += 1;
  byId('recommendation-result').replaceChildren();
  if (clearVehicleParts === true) {
    resetChat();
    updateSelectedVehicle(readVehicle());
    invalidateUnified();
    byId('vehicle-parts-result').replaceChildren();
    byId('motorium-result').replaceChildren();
  }
}
byId('vin-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  vehicle = null;
  clearDependentResults(true);
  vehicleKeys.forEach((key) => {byId(key).value = '';});
  byId('vehicle-result').replaceChildren();
  byId('context-status').textContent = 'ავტომობილი ჯერ არ არის არჩეული.';
  await runUnifiedSearch(event.target);
});
byId('configuration-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  vehicle = readVehicle();
  clearDependentResults(true);
  byId('context-status').textContent = vehicle ? 'კონტექსტი განახლდა. დამატებული დეტალები დამოუკიდებლად არ არის გადამოწმებული.' : 'ავტომობილი ჯერ არ არის არჩეული.';
  if (byId('vin').value.trim()) await runUnifiedSearch(event.target, vehicle);
  else if (vehicle) await loadVehicleParts();
});
byId('search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  clearDependentResults();
  await perform(byId('search-result'), async () => {
    const params = new URLSearchParams({code: byId('code').value.trim()});
    if (byId('manufacturer').value.trim()) params.set('manufacturer', byId('manufacturer').value.trim());
    render(byId('search-result'), await api('/inventory/search?' + params));
  }, event.target);
});
byId('fitment-button').addEventListener('click', async () => {
  await perform(byId('recommendation-result'), async () => {
    if (!byId('code').value.trim()) throw new Error('ჯერ შეიყვანეთ ნაწილის კოდი.');
    const params = new URLSearchParams({code: byId('code').value.trim()});
    vehicle = readVehicle();
    for (const key of vehicleKeys) if (vehicle?.[key]) params.set(key, vehicle[key]);
    render(byId('recommendation-result'), await api('/parts/fitments?' + params));
  });
});
byId('cross-button').addEventListener('click', async () => {
  await perform(byId('recommendation-result'), async () => {
    if (!byId('code').value.trim()) throw new Error('ჯერ შეიყვანეთ ნაწილის კოდი.');
    render(byId('recommendation-result'), await api('/parts/cross-references?' + new URLSearchParams({code: byId('code').value.trim()})));
  });
});
byId('oil-button').addEventListener('click', async () => {
  await perform(byId('recommendation-result'), async () => {
    vehicle = readVehicle();
    if (!vehicle) throw new Error('ჯერ მიუთითეთ ავტომობილის კონფიგურაცია.');
    render(byId('recommendation-result'), await api('/recommendations/oil', {vehicle}));
  });
});

for (const key of vehicleKeys) {
  byId(key).addEventListener('input', () => clearDependentResults(true));
}
for (const key of ['code', 'manufacturer']) {
  byId(key).addEventListener('input', clearDependentResults);
}

function clearVinContext() {
  vehicle = null;
  vehicleKeys.forEach((key) => {byId(key).value = '';});
  byId('vehicle-result').replaceChildren();
  byId('context-status').textContent = 'VIN შეიცვალა. ხელახლა ამოიცანით ავტომობილი.';
  clearDependentResults(true);
}
byId('vin').addEventListener('input', clearVinContext);
byId('vin-year').addEventListener('input', clearVinContext);
for (const key of vehicleKeys) {
  byId(key).addEventListener('input', () => {
    byId('context-status').textContent = 'კონტექსტი შეიცვალა; დამატებული მონაცემები არ არის გადამოწმებული.';
  });
}

async function showSetupStatus() {
  const target = byId('setup-status');
  try {
    const response = await fetch('/api/v1/status', {credentials: 'same-origin'});
    if (!response.ok) return;
    const status = await response.json();
    const messages = [];
    if (!status.inventory_configured) messages.push('საწყობის მონაცემთა ბაზა ჯერ არ არის დაკავშირებული. კოდით ძიებისთვის საჭიროა ლოკალური გამართვა.');
    if (!status.chat_configured) messages.push('ჩატი ჯერ არ არის გააქტიურებული — საჭიროა Anthropic-ის API გასაღები.');
    if (messages.length) {
      target.replaceChildren(element('h2', 'გამართვა დასასრულებელია'));
      for (const message of messages) target.append(element('p', message));
      if (status.vin_configured) target.append(element('p', 'VIN-ის ამოცნობა შეგიძლიათ გამოიყენოთ დამოუკიდებლად. ქეშის გარეშე პასუხი არ შეინახება.'));
      target.hidden = false;
    }
  } catch { /* The existing request errors explain a disconnected API. */ }
}
showSetupStatus();

async function loadVehicleParts() {
  await perform(byId('vehicle-parts-result'), async () => {
    const selected = readVehicle();
    if (!selected?.make || !selected?.model || !selected?.model_year) throw new Error('ჯერ ამოიცანით VIN ან მიუთითეთ მწარმოებელი, მოდელი და წელი.');
    render(byId('vehicle-parts-result'), await api('/inventory/vehicle', {vehicle: selected, limit: 30}));
  });
}
byId('vehicle-parts-button').addEventListener('click', loadVehicleParts);

function renderMotorium(target, record) {
  target.replaceChildren();
  const notices = {
    VEHICLE_CONTEXT_MISMATCH: 'Motorium-ის VIN-შედეგი და კატალოგის მოდელი ერთმანეთს არ ემთხვევა. კოდები არ დაბრუნდა.',
    VIN_CONTEXT_NOT_ESTABLISHED: 'Motorium-მა მანქანა ზუსტად ვერ დაუკავშირა კატალოგს. ზოგადი პროდუქტები არ გამოჩნდება.',
    PART_QUERY_NOT_ESTABLISHED: 'დააზუსტეთ ნაწილის სახეობა.',
    PRODUCT_DETAIL_MISMATCH: 'პროდუქტის კოდები ორ წყაროში განსხვავდება. საჭიროა გადამოწმება.',
    PRODUCT_DETAILS_UNAVAILABLE: 'პროდუქტის დეტალები ვერ ჩაიტვირთა; დამატებითი კოდები უცნობია.',
    PRODUCT_METADATA_MISSING: 'სტრუქტურირებული პროდუქტის მონაცემი ვერ მოიძებნა.',
  };
  const localized = (row) => ({...row, warnings: (row.warnings || []).map((w) => notices[w] || w)});
  renderStatus(target, localized(record));
  if (record.clarification) target.append(element('p', record.clarification));
  if (record.vehicle) facts(target, record.vehicle);
  for (const item of record.items || []) {
    const card = element('article', null, 'product');
    renderStatus(card, localized(item));
    card.append(element('h3', [item.manufacturer, item.name].filter(Boolean).join(' · ')));
    card.append(element('p', 'MPN: ' + item.mpn, 'code'));
    card.append(element('p', 'OEM ველი წყაროს მიხედვით: ' + (item.oem_codes?.length ? item.oem_codes.join(', ') : 'უცნობია')));
    if (item.supplier_sku) card.append(element('p', 'Motorium-ის შიდა კოდი: ' + item.supplier_sku));
    if (item.provider_fitment_text) card.append(element('p', 'წყაროს გამოყენების აღწერა: ' + item.provider_fitment_text));
    if (/^https:\/\/motorium\.ge\/product\.php\?id=\d+$/.test(item.source_url)) {
      const link = element('a', 'პროდუქტის წყაროს ნახვა');
      link.href = item.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      card.append(link);
    }
    if (item.status !== 'CONFLICTED') {
      const button = element('button', 'MPN-ით ძიება ჩემს საწყობში', 'secondary');
      button.type = 'button';
      button.addEventListener('click', () => {
        byId('code').value = item.mpn;
        byId('manufacturer').value = item.manufacturer || '';
        byId('search-form').requestSubmit();
        byId('search-result').scrollIntoView({behavior: scrollBehavior(), block: 'start'});
      });
      card.append(button);
    }
    target.append(card);
  }
  if (record.has_more) target.append(element('p', 'წყარომ მეტი პროდუქტი დააბრუნა. დააზუსტეთ ნაწილის სახეობა.', 'muted'));
}
byId('motorium-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  await perform(byId('motorium-result'), async () => {
    const vin = byId('vin').value.trim().toUpperCase();
    if (!vin || vin.length !== 17) throw new Error('ზედა VIN ველში მიუთითეთ 17-სიმბოლოიანი კოდი.');
    renderMotorium(byId('motorium-result'), await api('/vehicles/part-codes', {
      vin, part_query: byId('motorium-part-query').value.trim(), limit: 6,
    }));
  }, event.target);
});
byId('motorium-part-query').addEventListener('input', () => {
  contextRevision += 1;
  byId('motorium-result').replaceChildren();
});

function invalidateUnified() {
  unifiedRevision += 1;
  for (const id of ['unified-progress', 'unified-result', 'unified-pages', 'unified-maintenance']) byId(id).replaceChildren();
}
const sourceErrors = {
  RESULT_LIMIT: 'ჩანაწერების ლიმიტს მიაღწია; დააზუსტეთ ნაწილის სახეობა',
  VIN_REQUIRED: 'ამ წყაროს VIN სჭირდება', NOT_REQUESTED: 'ამ მოთხოვნისთვის საჭირო არ არის', NOT_NEEDED: 'სხვა წყაროებმა საკმარისი მონაცემი მოგვცა',
  MULTIPLE_VEHICLES: 'კატალოგში ამ VIN-ს რამდენიმე ვერსია შეესაბამება',
  VEHICLE_MISMATCH: 'კატალოგის მანქანა VIN-ის ამოცნობას არ ემთხვევა',
  DATABASE_UNAVAILABLE: 'ბაზასთან კავშირი ვერ დამყარდა', DECODE_FAILED: 'VIN-ის ამოცნობა ვერ მოხერხდა',
  TIME_LIMIT: 'დროის ლიმიტს მიაღწია; მიღებული შედეგები შენარჩუნებულია',
  RATE_LIMITED: 'მოთხოვნების ლიმიტს მიაღწია', ACCESS_REQUIRED: 'წყარომ ავტორიზაცია მოითხოვა',
  PROVIDER_UNAVAILABLE: 'გარე წყარო დროებით მიუწვდომელია', INVALID_RESPONSE: 'წყაროს პასუხი ვერ გადამოწმდა',
  VEHICLE_CONTEXT_MISMATCH: 'ავტომობილის მონაცემები არ ემთხვევა; შეუსაბამო კოდები დამალულია',
  MULTIPLE_ENGINES: 'რამდენიმე ძრავა ემთხვევა; ზეთი ან ფილტრი არ შერჩეულა',
  NO_VEHICLE: 'ავტომობილი ვერ დადგინდა', SEARCH_UNAVAILABLE: 'ძიება ვერ დასრულდა',
};
function renderUnified(record, revision) {
  const progress = byId('unified-progress');
  progress.replaceChildren();
  renderStatus(progress, {status: record.status});
  const sourceList = element('ul', null, 'source-progress');
  for (const source of record.sources) {
    const count = source.total_queries ? ` · ${source.completed_queries}/${source.total_queries} მოთხოვნა` : '';
    const item = element('li', `${source.label}: ${statuses[source.status] || source.status} · ${source.count} ჩანაწერი${count}`);
    if (source.error_code) item.append(element('p', sourceErrors[source.error_code] || 'წყარო სრულად ვერ შემოწმდა', 'muted'));
    sourceList.append(item);
  }
  progress.append(sourceList);
  progress.append(element('p', `უნიკალური ჩანაწერები: ${record.total} · დადასტურებული: ${record.verified_count} · შესამოწმებელი: ${record.candidate_count}`));
  const target = byId('unified-result');
  target.replaceChildren();
  for (const warning of record.warnings || []) target.append(element('p', warning === 'USER_CONFIGURATION_MISMATCH' ? 'მითითებული მწარმოებელი, მოდელი ან წელი VIN-ს არ ემთხვევა; ცვლილება არ გამოყენებულა.' : warning, 'warning'));
  for (const item of record.items) {
    const card = element('article', null, 'product');
    renderStatus(card, item);
    card.append(element('h3', [item.manufacturer, item.name].filter(Boolean).join(' · ') || 'ნაწილის ჩანაწერი'));
    card.append(element('p', item.main_code, 'code'));
    const names = {warehouse: 'ჩვენი საწყობის კატალოგი', fitment: 'შესაბამისობის მტკიცებულება', motorium: 'Motorium'};
    card.append(element('p', 'წყაროები: ' + item.source_names.map((name) => names[name] || name).join(' · '), 'muted'));
    if (item.status !== 'VERIFIED') card.append(element('p', 'შესამოწმებელი კანდიდატი — ამ VIN-თან თავსებადობა დაუდასტურებელია.', 'muted'));
    for (const row of item.warehouse_records) {
      product(card, {...row.product, status: row.status});
      for (const ref of row.evidence || []) evidenceLink(card, ref);
    }
    for (const row of item.supplier_records) {
      const details = element('div');
      details.append(element('p', 'Motorium · MPN: ' + row.mpn, 'code'));
      details.append(element('p', 'OEM ველი წყაროს მიხედვით: ' + (row.oem_codes.length ? row.oem_codes.join(', ') : 'უცნობია')));
      if (row.provider_fitment_text) details.append(element('p', 'წყაროს გამოყენების აღწერა: ' + row.provider_fitment_text));
      if (row.supplier_sku) details.append(element('p', 'მომწოდებლის შიდა კოდი: ' + row.supplier_sku));
      for (const warning of row.warnings || []) details.append(element('p', sourceErrors[warning] || 'წყაროს მონაცემი საჭიროებს გადამოწმებას.', 'muted'));
      if (/^https:\/\/motorium\.ge\/product\.php\?id=\d+$/.test(row.source_url)) {
        const link = element('a', 'Motorium-ის წყაროს ნახვა');
        link.href = row.source_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        details.append(link);
      }
      if (!item.warehouse_records.length) {
        details.append(element('p', 'ჩვენს საწყობში არსებობა, ფასი და რაოდენობა უცნობია.', 'muted'));
        const button = element('button', 'MPN-ით ძიება საწყობში', 'secondary');
        button.type = 'button';
        button.addEventListener('click', () => {
          byId('code').value = row.mpn;
          byId('manufacturer').value = row.manufacturer || '';
          byId('search-form').requestSubmit();
          byId('search-result').scrollIntoView({behavior: scrollBehavior(), block: 'start'});
        });
        details.append(button);
      }
      card.append(details);
    }
    target.append(card);
  }
  if (!record.items.length) target.append(element('p', record.status === 'RUNNING' ? 'ნაწილების წყაროები მოწმდება…' : 'ამ ძიებით ნაწილების ჩანაწერი ვერ მოიძებნა.', 'muted'));
  const pages = byId('unified-pages');
  pages.replaceChildren();
  if (record.status !== 'RUNNING') {
    const addPage = (label, offset) => {
      const button = element('button', label, 'secondary');
      button.type = 'button';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const next = await api(`/vehicles/search/${record.search_id}?offset=${offset}&limit=50`, undefined, false);
          if (revision === unifiedRevision) renderUnified(next, revision);
        } catch (error) {
          if (revision === unifiedRevision) pages.append(element('p', error.message, 'error'));
        } finally {button.disabled = false;}
      });
      pages.append(button);
    };
    if (record.offset > 0) addPage('წინა გვერდი', Math.max(0, record.offset - 50));
    if (record.next_offset !== null) addPage('შემდეგი გვერდი', record.next_offset);
    if (record.total) pages.append(element('p', `${record.offset + 1}–${record.offset + record.items.length} / ${record.total}`, 'muted'));
  }
  if (record.cross_references?.length) {
    const references = element('details');
    references.append(element('summary', 'კოდების მიმართულებითი გადაკვეთები · ' + record.cross_references.length));
    const body = element('div');
    render(body, {items: record.cross_references, warnings: ['გადაკვეთის ჩანაწერი VIN-თან თავსებადობას არ ადასტურებს; მისი მიმართულება და შეზღუდვები ცალკე მოწმდება.']});
    references.append(body);
    target.append(references);
  }
  const maintenance = byId('unified-maintenance');
  maintenance.replaceChildren();
  if (record.maintenance) {
    maintenance.append(element('h3', 'სერვისის მოთხოვნები · ზეთი'));
    const details = element('div');
    render(details, record.maintenance);
    maintenance.append(details);
  }
  for (const report of record.fluids || []) {
    maintenance.append(element('h3', report.provider + ' · ' + (report.vehicle_label || 'წყაროს შერჩევა')));
    renderStatus(maintenance, report);
    if (report.clarification) maintenance.append(element('p', report.clarification, 'warning'));
    for (const warning of report.warnings || []) maintenance.append(element('p', warning, 'muted'));
    for (const item of report.items || []) {
      const card = element('article', null, 'product');
      card.append(element('p', [item.component, item.product_name].filter(Boolean).join(' · ')));
      if (item.product_code) card.append(element('p', item.product_code, 'code'));
      const detail = [item.viscosity, item.interval, item.capacity].filter(Boolean).join(' · ');
      if (detail) card.append(element('p', detail, 'muted'));
      card.append(element('p', 'წყაროს რეკომენდაციაა. ჩვენი მარაგი, ფასი და რაოდენობა უცნობია.', 'muted'));
      maintenance.append(card);
    }
  }
}
async function runUnifiedSearch(form, configuration = null) {
  invalidateUnified();
  const revision = unifiedRevision;
  const progress = byId('unified-progress');
  progress.append(element('p', 'წყაროებში ძიება იწყება…', 'muted'));
  const buttons = form.querySelectorAll('button');
  buttons.forEach((button) => {button.disabled = true;});
  let identity = '';
  try {
    const body = {vin: byId('vin').value.trim().toUpperCase(), part_query: byId('unified-query').value.trim()};
    if (byId('vin-year').value) body.model_year = Number(byId('vin-year').value);
    if (configuration) body.configuration = configuration;
    let record = await api('/vehicles/search', body, false);
    while (revision === unifiedRevision) {
      const nextIdentity = JSON.stringify(record.vehicle);
      if (Object.keys(record.vehicle).length && identity !== nextIdentity) {
        identity = nextIdentity;
        render(byId('vehicle-result'), {status: 'PROVISIONAL', vehicle: record.vehicle});
        for (const key of vehicleKeys) byId(key).value = record.vehicle[key] ?? '';
        vehicle = readVehicle();
        updateSelectedVehicle(vehicle);
        byId('context-status').textContent = 'VIN-ის კონტექსტი არჩეულია; წყაროების შესაბამისობა ცალკე მოწმდება.';
      }
      renderUnified(record, revision);
      if (record.status !== 'RUNNING') break;
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (revision !== unifiedRevision) break;
      record = await api(`/vehicles/search/${record.search_id}`, undefined, false);
    }
  } catch (error) {
    if (revision === unifiedRevision) progress.replaceChildren(element('p', error.message, 'error'));
  } finally {buttons.forEach((button) => {button.disabled = false;});}
}
byId('unified-query').addEventListener('input', invalidateUnified);

function chatText(language, ka, en) { return language === 'ka' ? ka : en; }
const sourceNames = {
  vpic: ['vPIC · ავტომობილის ამოცნობა', 'vPIC vehicle decoder'], warehouse: ['ჩვენი საწყობის კატალოგი', 'our warehouse catalog'],
  fitment: ['შესაბამისობის ჩანაწერები', 'fitment records'], maintenance: ['სერვისის მოთხოვნები', 'service requirements'],
  motorium: ['Motorium', 'Motorium'], fuchs: ['Fuchs ზეთის შერჩევა', 'Fuchs oil chooser'], shell: ['Shell ზეთის შერჩევა', 'Shell oil chooser'],
  pakelo: ['Pakelo ზეთის შერჩევა', 'Pakelo oil chooser'], jsfilter: ['JS Asakashi ფილტრები', 'JS Asakashi filters'],
  catalog_codes: ['კოდებით ძიება საწყობში', 'code search in our warehouse'], cross_reference: ['გადაკვეთის ჩანაწერები', 'cross-reference records'],
};
const chatWarnings = {
  SOURCE_UNAVAILABLE: ['ერთი წყარო დროებით მიუწვდომელია; დანარჩენი შედეგები ნაჩვენებია.', 'One source is temporarily unavailable; the other results are shown.'],
  VIN_NOT_PROVIDED: ['VIN არ არის მითითებული; ძიება ავტომობილის აღწერას ეფუძნება.', 'No VIN was given; the lookup uses your vehicle description.'],
  MORE_CATALOG_CANDIDATES: ['კატალოგში ამ ძიებას კიდევ მეტი კანდიდატი აქვს.', 'The catalog has more candidates for this search.'],
  VEHICLE_CONTEXT_CHANGED: ['ავტომობილი შეიცვალა; წინა შედეგები გაუქმდა.', 'The vehicle changed; earlier results were cleared.'],
  USER_CONFIGURATION_MISMATCH: ['მითითებული მწარმოებელი, მოდელი ან წელი VIN-ს არ ემთხვევა; გამოყენებულია VIN-ის მონაცემი.', 'The make, model or year you gave does not match the VIN; the VIN data was used.'],
  FITMENT_UNAVAILABLE: ['შესაბამისობის ჩანაწერები ვერ ჩაიტვირთა; სტატუსი უცნობია.', 'Fitment records could not be loaded; fitment is unknown.'],
  VIN_UNAVAILABLE: ['VIN-ის ამოცნობა ვერ მოხერხდა.', 'The VIN could not be decoded.'],
  NAME_MATCH_ONLY: ['ამ მანქანისთვის კოდები ვერ დადგინდა; ნაჩვენებია მარაგში არსებული ნაწილები მანქანის სახელით. გადაამოწმეთ თავსებადობა.', 'No part codes were found for this car; showing in-stock parts that name it. Please verify compatibility.'],
  STOCK_MAY_BE_STALE: ['მარაგი სინქრონიზაციის ბოლო მონაცემებითაა; გადაამოწმეთ მაღაზიაში.', 'Stock is from the last sync; confirm in the store.'],
  TIMEOUT: ['დროის ლიმიტი ამოიწურა; ნაპოვნი შედეგები ნაჩვენებია.', 'The time limit was reached; the results found so far are shown.'],
  // The provider's account refuses every request: the shop is told why, the customer is not.
  PROVIDER_BILLING: ['Anthropic-ის ანგარიშზე კრედიტი ამოიწურა. ჩატი ვერ უპასუხებს, სანამ ბალანსი არ შეივსება.', 'The Anthropic account is out of credit. The chat cannot answer until credit is added.'],
  AUTHENTICATION_ERROR: ['Anthropic-ის API გასაღები არ მუშაობს. შეამოწმეთ გასაღები კონფიგურაციაში.', 'The Anthropic API key does not work. Check the key in the configuration.'],
  ACCESS_DENIED: ['Anthropic-ის ანგარიშს ამ მოდელზე წვდომა არ აქვს.', 'The Anthropic account has no access to this model.'],
};
const accountWarnings = ['PROVIDER_BILLING', 'AUTHENTICATION_ERROR', 'ACCESS_DENIED'];
// The reply sentence already states the grade, engine and missing parts; only the
// quantity check adds something the customer cannot read elsewhere.
function requirementLines(record, language) {
  const need = record.requirements || {};
  const lines = [];
  // The assistant engine (the one that states an outcome) passes on what the oil selectors say
  // the engine needs. Its reply is the model's own sentence, so the figures are shown as data.
  if (record.outcome && Array.isArray(need.grades) && need.grades.length) {
    const facts = [need.grades.join(', ')];
    if (need.capacity_l) facts.push(need.capacity_l + ' L');
    if (need.engine_code) facts.push(chatText(language, 'ძრავი ', 'engine ') + need.engine_code);
    const sources = (Array.isArray(need.sources) ? need.sources : []).filter(name => typeof name === 'string' && name).join(', ');
    lines.push(chatText(language, 'ზეთი ამ ძრავისთვის', 'Oil for this engine') + (sources ? ` (${sources})` : '') + ': ' + facts.join(' · '));
  }
  for (const group of need.coverage || []) {
    const have = group.litres_store + (group.litres_tuz11 ? ' L + ' + group.litres_tuz11 + ' L TUZ 11' : ' L');
    const enough = group.enough_store ? chatText(language, 'სრული შეცვლისთვის საკმარისია', 'enough for a full change') : (group.enough_with_tuz11 ? chatText(language, 'საკმარისია TUZ 11-ის გათვალისწინებით', 'enough including TUZ 11') : chatText(language, 'არასაკმარისია სრული შეცვლისთვის', 'not enough for a full change'));
    lines.push(`${group.brand} ${group.grade}: ${have} — ${enough}`);
  }
  return lines;
}
function warningText(language, warning) {
  if (typeof warning === 'string' && warning.startsWith('SOURCE_FAILED:')) {
    const id = warning.slice('SOURCE_FAILED:'.length);
    const pair = sourceNames[id];
    const name = pair ? chatText(language, pair[0], pair[1]) : id;
    return chatText(language, `წყარო ვერ შემოწმდა: ${name}. დანარჩენი შედეგები ნაჩვენებია.`, `Source not checked: ${name}. The other results are shown.`);
  }
  const pair = chatWarnings[warning];
  return pair ? chatText(language, pair[0], pair[1]) : warning;
}
function carLabel(selected) {
  return [selected?.make, selected?.model, selected?.model_year].filter(Boolean).join(' ');
}
// The car the conversation is about stays in sight right above the composer.
function showChatCar(label, language = lastLanguage || 'ka') {
  const text = typeof label === 'string' ? label.trim() : '';
  byId('chat-car-label').textContent = text;
  byId('chat-car-change').textContent = chatText(language, 'შეცვლა', 'Change');
  byId('chat-car-change').ariaLabel = chatText(language, 'მანქანის შეცვლა', 'Change the car');
  byId('chat-car').hidden = !text;
}
// The car the assistant understood from the conversation, by its own short name. The side
// panel shows it while the vehicle form names no car, so the panel never says "no car" beside
// a chat that is plainly about one.
let chatCar = '';
// Two short car names are one car when every word of the shorter is among the longer's
// ("Mercedes E-Class 2015" and "Mercedes-Benz E-Class 2015"). Only decides what the panel says.
function sameCar(one, other) {
  const words = text => new Set(String(text).toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean));
  const first = words(one);
  const second = words(other);
  if (!first.size || !second.size) return false;
  const [small, large] = first.size <= second.size ? [first, second] : [second, first];
  return [...small].every(word => large.has(word));
}
function updateSelectedVehicle(selected) {
  const target = byId('selected-vehicle');
  target.replaceChildren();
  const known = Boolean(selected && Object.keys(selected).length);
  showChatCar(known ? carLabel(selected) : chatCar);
  if (!known) {
    if (chatCar) {
      // Plain text, and marked as what it is: read from the conversation, not entered or decoded.
      target.append(element('h3', chatCar), element('p', 'საუბრიდან', 'muted from-chat'));
      return;
    }
    target.append(element('p', 'ავტომობილი ჯერ არ არის არჩეული.', 'muted'));
    return;
  }
  target.append(element('h3', carLabel(selected)));
  const details = {};
  for (const key of vehicleKeys) if (selected[key]) details[key] = selected[key];
  if (!details.fuel && selected.fuel_type) details.fuel = selected.fuel_type;
  facts(target, details);
  // A car typed in a message outranks the form, so the chat may be about another car than
  // the form still holds: the panel says so and never passes for the chat's car.
  if (chatCar && !sameCar(chatCar, carLabel(selected))) {
    target.append(element('p', 'საუბარი სხვა მანქანაზეა: ' + chatCar, 'muted from-chat'));
  }
  target.append(element('p', 'თავსებადობა მოწმდება თითოეული ნაწილისთვის.', 'muted'));
}
function deleteConversation(identifier) {
  if (identifier) api('/chat/conversations/' + encodeURIComponent(identifier), undefined, false, 'DELETE').catch(() => {});
}
let lastLanguage = null;
// The answer buttons still waiting for the clerk, and the quieter next steps of the last reply.
let openChoices = null;
let nextSteps = null;
// Answer groups whose question the server may have moved past: a tap on them sends text only.
const staleQuestions = new WeakSet();
// The "try again" button of the last failed turn; it goes once another message is sent.
let failedRetry = null;
// Where keepInView last left the transcript; a clerk above it is reading older messages.
let transcriptPlace = 0;
// The composer's own placeholder while the "another car" hint stands in for it.
let composerHint = null;
// What each card list has shown so far, so "Show more" continues its groups and warnings.
const cardLists = new WeakMap();
const VIN_IN_TEXT = /\b[A-HJ-NPR-Z0-9]{17}\b/gi;
function guessLanguage(message, previous) {
  if (/[\u10a0-\u10ff]/.test(message)) return 'ka';
  const words = message.replace(VIN_IN_TEXT, ' ').split(/\s+/).filter(word => /^[A-Za-z]{2,}[?!.,]*$/.test(word) && !/^(ok|okay|vin)$/i.test(word.replace(/[?!.,]/g, '')));
  return words.length ? 'en' : (previous || 'ka');
}
function resetChat() {
  chatRevision += 1;
  deleteConversation(conversationId);
  conversationId = null;
  chatBusy = false;
  lastLanguage = null;
  openChoices = null;
  nextSteps = null;
  failedRetry = null;
  transcriptPlace = 0;
  chatCar = '';
  byId('chat-send').disabled = false;
  byId('chat-result').replaceChildren();
  byId('chat-progress').textContent = '';
  restoreComposerHint();
  showStarters(true);
}
// The starter buttons and the VIN hint are for an empty chat. Once it has a message they
// give their room to the transcript; a new chat brings them back.
function showStarters(on) {
  byId('suggested-prompts').hidden = !on;
  byId('chat-notice').hidden = !on;
}
// The composer is two rows tall and grows with what is written in it, up to this height;
// longer text scrolls inside the box.
const COMPOSER_MAX = 168;
function fitComposer() {
  const box = byId('message');
  if (!box.style) return;
  const before = box.style.height;
  box.style.height = 'auto';
  if (box.scrollHeight > box.clientHeight) box.style.height = Math.min(box.scrollHeight + 2, COMPOSER_MAX) + 'px';
  // It grew or shrank under the clerk's hands: the send button stays on screen.
  if (box.style.height !== before) holdComposer(true);
}
// The server gets ready when a conversation opens (the assistant engine builds its catalogue
// index and reads its instructions ahead). Opening it when the clerk starts to write, not when
// the message is sent, keeps that wait out of the first reply.
let opening = null;
function openAhead() {
  if (conversationId || opening || chatBusy) return;
  const revision = chatRevision;
  const request = api('/chat/conversations', {}, false).then((created) => {
    // A new chat was started meanwhile: this conversation is of no use to anybody.
    if (revision !== chatRevision) deleteConversation(created.conversation_id);
    else conversationId = created.conversation_id;
  }).catch(() => {
    // Sending the message asks again, and reports what is wrong if it still is.
  }).finally(() => { if (opening === request) opening = null; });
  opening = request;
}
function restoreComposerHint() {
  if (composerHint === null) return;
  byId('message').placeholder = composerHint;
  composerHint = null;
}
// False once the clerk has scrolled back above the place the transcript was last put in.
// Read it before the transcript changes: taking a bubble out can move the scroll position.
function followingTranscript() {
  return byId('chat-result').scrollTop >= transcriptPlace - 4;
}
// New messages move the transcript itself, at once and never with a glide: the top of a tall
// reply, with its question and answer buttons, lands near the top; a short one at the end.
// Pass `follow` false to leave a clerk who is reading older messages alone; `own` marks the
// clerk's own action, which always shows.
function keepInView(node, follow, own = false) {
  const log = byId('chat-result');
  if (follow) {
    log.scrollTop = Math.max(0, Math.min(node.offsetTop - 8, log.scrollHeight - log.clientHeight));
    transcriptPlace = log.scrollTop;
  }
  if (typeof node.getBoundingClientRect !== 'function') return;
  if (transcriptScrolls()) {
    holdComposer(own);
    return;
  }
  // A narrow screen: the transcript is part of the page, so the page shows the new message from
  // its top, and only while that message is on screen or the clerk has just acted.
  const box = node.getBoundingClientRect();
  if (own || (box.bottom > 0 && box.top < innerHeight)) node.scrollIntoView({block: box.height > innerHeight ? 'start' : 'nearest'});
}
// True where the stylesheet lets the transcript scroll inside itself (wide screens).
function transcriptScrolls() {
  return typeof getComputedStyle === 'function' && getComputedStyle(byId('chat-result')).overflowY !== 'visible';
}
// On such a screen the page moves only to keep the composer in view under a growing transcript,
// and only while the conversation is on screen or the clerk has just acted.
function holdComposer(own = false) {
  const log = byId('chat-result');
  if (typeof log.getBoundingClientRect !== 'function' || !transcriptScrolls()) return;
  const box = log.getBoundingClientRect();
  if (own || (box.bottom > 0 && box.top < innerHeight)) byId('chat-form').scrollIntoView({block: 'nearest'});
}
// Where the composer sits on screen, to notice when something above it pushed it.
function composerTop() {
  const form = byId('chat-form');
  return typeof form.getBoundingClientRect === 'function' ? form.getBoundingClientRect().top : 0;
}
function chatMessage(role, text, language) {
  const section = element('article', null, 'chat-message ' + role);
  section.lang = language;
  if (text) section.append(element('p', text, 'message-text'));
  byId('chat-result').append(section);
  return section;
}
function typingBubble(language) {
  const bubble = element('article', null, 'chat-message assistant typing');
  bubble.lang = language;
  for (let i = 0; i < 3; i += 1) bubble.append(element('span', null, 'dot'));
  bubble.append(element('span', chatText(language, 'ასისტენტი წერს…', 'Assistant is typing…'), 'sr-only'));
  byId('chat-result').append(bubble);
  // The clerk has just sent something: their message and this bubble under it always show.
  keepInView(bubble, true, true);
  return bubble;
}
// A tapped button is sent as the clerk's next message; whatever they were typing stays in the box.
function sendChat(text, options) {
  if (chatBusy) return undefined;
  return runChat(text, options);
}
// Focus never stays on a button that is about to be disabled or removed.
function parkFocus(node) {
  if (node && document.activeElement === node) byId('chat-result').focus({preventScroll: true});
}
// A clerk answering from the keyboard is taken to the next thing to answer. A pointer never
// moves focus, and neither does anything once the clerk has moved it elsewhere.
function followFocus(target) {
  if (target && document.activeElement === byId('chat-result')) target.focus();
}
// One tap or one typed message answers the open question. Its buttons stay where they are, the
// chosen one marked and all of them disabled, so nothing jumps; the next steps of the previous
// reply go. Returns the group, so that a failed turn can open it again.
function retireChoices(origin) {
  if (nextSteps) nextSteps.remove();
  nextSteps = null;
  const group = openChoices;
  openChoices = null;
  if (!group) return null;
  for (const chip of group.querySelectorAll('button')) {
    if (chip === origin) chip.setAttribute('aria-pressed', 'true');
    chip.disabled = true;
  }
  return group;
}
function restoreChoices(group, stale = false) {
  if (!group) return;
  if (stale) staleQuestions.add(group);
  for (const chip of group.querySelectorAll('button')) {
    chip.removeAttribute('aria-pressed');
    chip.disabled = false;
  }
  openChoices = group;
}
// kind: 'question' for the answers of the `question` key (a tap also sends the option's number),
// 'answers' for the same buttons from a server without that key, 'next' for optional next steps.
function choiceGroup(options, kind, language) {
  const group = element('div', null, kind === 'next' ? 'quick-replies next-steps' : 'quick-replies');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', kind === 'next' ? chatText(language, 'შემდეგი ნაბიჯი', 'Next steps') : chatText(language, 'აირჩიეთ პასუხი', 'Choose an answer'));
  (Array.isArray(options) ? options : []).forEach((option, index) => {
    const text = typeof option === 'string' ? option : option?.label || option?.value;
    if (typeof text !== 'string' || !text) return;
    const value = typeof option === 'string' ? option : option.value || option.label;
    const chip = element('button', text, 'chip');
    chip.type = 'button';
    chip.addEventListener('click', (event) => sendChat(String(value).trim() || text, {
      shown: text, origin: chip, keyboard: event.detail === 0,
      choice: kind === 'question' && !staleQuestions.has(group) ? index + 1 : undefined,
    }));
    group.append(chip);
  });
  return group.children.length ? group : null;
}
// A failed turn offers the same message again, without a second copy of it in the transcript.
function retryButton(bubble, language, rerun) {
  const again = element('button', chatText(language, 'ხელახლა ცდა', 'Try again'), 'secondary retry');
  again.type = 'button';
  again.addEventListener('click', (event) => {
    if (chatBusy) return undefined;
    parkFocus(again);
    failedRetry = null;
    bubble.remove();
    return rerun(event.detail === 0);
  });
  bubble.append(again);
  failedRetry = again;
  return again;
}
// How sure a match is, in the shop's fixed labels. The first three speak of the part's fit to
// the car; "spec" is for a product its catalogue text ties to no car (an oil, a fluid) that was
// chosen by its specification. Any other value has no label: none is shown, never a wrong one.
function fitLabel(level, language) {
  const labels = {
    confirmed: chatText(language, 'ნაწილის ნომრით ემთხვევა', 'Matches by part number'),
    listed: chatText(language, 'კატალოგში ამ მოდელსა და წელზეა', 'Listed for this model and year'),
    check: chatText(language, 'გადასამოწმებელია', 'Needs checking'),
    spec: chatText(language, 'შერჩეულია სპეციფიკაციით', 'Chosen by specification'),
  };
  return typeof level === 'string' && Object.hasOwn(labels, level) ? labels[level] : '';
}
// The ground a part is offered on: its level, when that has a label, and the engine's reason.
// Two parts with the same ground say the same two lines.
function groundOf(item, language) {
  return (fitLabel(item.confidence, language) ? item.confidence : '') + '\u001f' + (typeof item.reason === 'string' ? item.reason.trim() : '');
}
// Those lines: the label of the level, then the reason under it.
function groundLines(item, language) {
  const lines = [];
  const label = fitLabel(item.confidence, language);
  if (label) lines.push(element('p', label, 'fit ' + item.confidence));
  const reason = typeof item.reason === 'string' ? item.reason.trim() : '';
  if (reason) lines.push(element('p', reason, 'muted reason'));
  return lines;
}
// Where a card keeps those lines, so a group that says them once can take them off its cards.
const groundBoxes = new WeakMap();
function partCard(item, language, seenWarnings = new Set()) {
  const row = item.product || {};
  const fitmentLabels = {
    VERIFIED: chatText(language, 'დადასტურებული შესაბამისობა', 'Verified fitment'),
    PROVISIONAL: chatText(language, 'კანდიდატი; შესაბამისობა დაუდასტურებელია', 'Candidate; fitment unconfirmed'),
    UNKNOWN: chatText(language, 'შესაბამისობა უცნობია', 'Fitment unknown'),
    CONFLICTED: chatText(language, 'ურთიერთსაწინააღმდეგო მტკიცებულება', 'Conflicting fitment evidence'),
  };
  const matchLabels = {
    OIL_GRADE_BRAND: chatText(language, 'წყაროს რეკომენდებული ზეთის ხაზი', 'Product line the source recommends'),
    OIL_GRADE: chatText(language, 'იგივე სიბლანტე, სხვა ბრენდი', 'Same grade, other brand'),
    ENGINE_FAMILY: chatText(language, 'ძრავის ოჯახის დამთხვევა', 'Engine family match'),
    CODE_CANDIDATE: chatText(language, 'ზუსტი კოდის დამთხვევა', 'Exact code match'),
    assistant: chatText(language, 'ასისტენტმა შეარჩია კატალოგის ჩანაწერით', 'Chosen by the assistant from the catalogue row'),
  };
  const card = element('article', null, 'chat-part');
  const name = element('h4', row.product_name || chatText(language, 'კატალოგის ჩანაწერი', 'Catalog item'));
  if (typeof row.price === 'number' && row.price > 0) {
    // The amount alone, beside the name; a part without a known price gets no line at all.
    const sign = !row.currency || /^(gel|₾)$/i.test(row.currency) ? '₾' : row.currency;
    const head = element('div', null, 'part-head');
    head.append(name, element('p', row.price.toFixed(2) + ' ' + sign, 'price'));
    card.append(head);
  } else card.append(name);
  card.append(element('p', [row.brand, row.product_code].filter(Boolean).join(' · ') || '—', 'code'));
  // Which cars the catalogue row names: the words a fit label and its reason rest on. Some
  // source cells arrive HTML-escaped; this is text, so the few entities are written out.
  const fits = typeof row.product_model === 'string' ? row.product_model.replace(/&(gt|lt|amp|quot|#39);/g, (_, name) => ({gt: '>', lt: '<', amp: '&', quot: '"', '#39': "'"})[name]).trim() : '';
  if (fits) card.append(element('p', fits, 'muted fits'));
  card.append(element('p', item.availability || chatText(language, 'ნაპოვნია ჩვენს კატალოგში; მარაგი დაუდასტურებელია.', 'Found in our catalog; stock unconfirmed.'), 'availability ' + (item.availability_status || 'UNKNOWN')));
  // How sure the match is and the engine's reason get a box of their own here. It starts empty:
  // explainRun fills it, unless the whole group rests on the same ground and says so once.
  const fit = fitLabel(item.confidence, language);
  const reason = typeof item.reason === 'string' ? item.reason.trim() : '';
  if (fit || reason) {
    const ground = element('div', null, 'part-fit');
    groundBoxes.set(card, ground);
    card.append(ground);
  }
  // The assistant engine repeats its reason in matched_via for older screens: it is said once.
  const reasons = (item.matched_via || []).filter(text => text && String(text).trim() !== reason);
  if (reasons.length) card.append(element('p', chatText(language, 'შერჩეულია: ', 'Chosen because: ') + reasons[0], 'muted'));
  if (item.pack_litres) card.append(element('p', chatText(language, 'შეფუთვა: ', 'Pack size: ') + item.pack_litres + ' L', 'muted'));
  // A caveat shared by several cards is said once per reply, on the first card that carries it.
  const warnings = [];
  for (const text of item.warnings || []) {
    if (!text || seenWarnings.has(text)) continue;
    seenWarnings.add(text);
    warnings.push(text);
  }
  if (warnings.length) card.append(element('p', warnings.at(-1), 'warning'));
  // Evidence and matching details stay one tap away instead of crowding the card.
  const details = element('details', null, 'part-details');
  details.append(element('summary', chatText(language, 'დეტალები', 'Details')));
  for (const warning of warnings.slice(0, -1)) details.append(element('p', warning, 'warning'));
  const status = item.fitment_status || 'UNKNOWN';
  // Under a fit label, an "unknown" badge would only contradict it; any other badge still shows.
  if (!fit || status !== 'UNKNOWN' || item.compatible === false) {
    details.append(element('span', item.compatible === false ? chatText(language, 'შესაბამისობა უარყოფილია', 'Fitment excluded') : (fitmentLabels[status] || fitmentLabels.UNKNOWN), 'status ' + status));
  }
  const matches = (row.matched_codes || []).map(code => typeof code === 'object' ? (code.original || code.code || code.normalized_code || code.normalized || code.raw || '') : code).filter(Boolean);
  // The assistant engine matches no codes: it quotes the words of the row its choice rests on.
  const quoted = typeof item.evidence === 'string' ? item.evidence.trim() : '';
  if (quoted) details.append(element('p', chatText(language, 'კატალოგის ჩანაწერში წერია: ', 'The catalogue row says: ') + quoted));
  if (matches.length || !quoted) details.append(element('p', chatText(language, 'დამთხვეული კოდები: ', 'Matched codes: ') + (matches.join(', ') || '—')));
  details.append(element('p', chatText(language, 'დამთხვევის წყარო: ', 'Match source: ') + (matchLabels[item.match_source] || item.match_source || 'catalog')));
  for (const reason of reasons.slice(1)) details.append(element('p', chatText(language, 'შერჩეულია: ', 'Chosen because: ') + reason, 'muted'));
  details.append(element('p', chatText(language, 'ბოლო დაკვირვება: ', 'Observed: ') + (row.last_observed_at || chatText(language, 'უცნობია', 'unknown')), 'muted'));
  card.append(details);
  return card;
}
function renderChatCards(target, record, language, append = false) {
  if (!append) target.replaceChildren();
  let shown = append ? cardLists.get(target) : null;
  if (!shown) {
    shown = {run: null, runs: [], warnings: new Set()};
    cardLists.set(target, shown);
  }
  const groups = new Map((Array.isArray(record.groups) ? record.groups : []).map(group => [group?.id, group]));
  for (const item of record.cards || []) {
    // A header opens each group of cards ("front pads", "rear pads"), on later pages too.
    const id = typeof item.group === 'string' ? item.group : '';
    if (!shown.run || id !== shown.run.group) {
      shown.run = {group: id, header: null, summary: null, parts: []};
      shown.runs.push(shown.run);
      const group = groups.get(id);
      if (typeof group?.label === 'string' && group.label) {
        const header = element('h4', group.label, 'chat-group');
        if (group.count > 0) header.append(element('span', ' ' + group.count));
        target.append(header);
        shown.run.header = header;
      }
    }
    const node = partCard(item, language, shown.warnings);
    target.append(node);
    shown.run.parts.push({item, node});
  }
  // The reply says again why its parts are offered: once for a whole group, or card by card.
  // Once only where that line cannot be read onto parts outside the group: when the reply is
  // a single list, or every one of its groups stands under a header of its own.
  const once = shown.runs.length === 1 || shown.runs.every(run => run.header);
  for (const run of shown.runs) explainRun(run, language, once);
}
// When every part of a group rests on the same ground (the same confidence and the same reason)
// and `once` allows it, that is said once, under the group's header or above a list without
// one, and not on each card; otherwise each card says its own. Run again when "Show more" adds
// parts.
function explainRun(run, language, once) {
  const ground = groundOf(run.parts[0].item, language);
  const shared = once && run.parts.every(part => groundOf(part.item, language) === ground);
  const lines = shared ? groundLines(run.parts[0].item, language) : [];
  if (lines.length) {
    // Written once (the transcript announces every addition): while this line stands, every
    // part of the group rests on the ground it states.
    if (!run.summary) {
      run.summary = element('div', null, 'chat-group-fit');
      run.summary.append(...lines);
      if (run.header) run.header.after(run.summary);
      else run.parts[0].node.before(run.summary);
    }
    return;
  }
  if (run.summary) {
    run.summary.remove();
    run.summary = null;
  }
  // A card's box is filled the first time no line speaks for its group. A group only grows, so
  // that line never comes back and no box is emptied again.
  for (const part of run.parts) {
    const box = groundBoxes.get(part.node);
    if (box && !box.children.length) box.append(...groundLines(part.item, language));
  }
}
function supplierSection(record, language) {
  if (!record.supplier_only?.length) return null;
  const supplier = element('details', null, 'supplier-only');
  supplier.append(element('summary', chatText(language, 'მომწოდებლის კოდები · საწყობში დამთხვევის გარეშე', 'Supplier codes · no warehouse match')));
  for (const item of record.supplier_only) {
    supplier.append(element('p', [item.name, item.manufacturer, item.mpn || item.main_code, ...(item.oem_codes || [])].filter(Boolean).join(' · '), 'code'));
  }
  supplier.append(element('p', chatText(language, 'ჩვენს საწყობში არსებობა და შესაბამისობა დაუდასტურებელია.', 'Warehouse availability and fitment are unconfirmed.'), 'muted'));
  return supplier;
}
function resolutionSection(record, language) {
  if (!record.resolution_candidates?.length) return null;
  const section = element('details', null, 'vehicle-candidates');
  section.append(element('summary', chatText(language, 'ავტომობილის შესაძლო ვარიანტები', 'Possible vehicle variants')));
  for (const candidate of record.resolution_candidates.slice(0, 12)) {
    const car = candidate.vehicle || candidate.configuration || candidate;
    const name = candidate.label || [car.make, car.model, car.trim, car.model_year, car.engine_code, car.fuel || car.fuel_type].filter(Boolean).join(' · ');
    if (name) section.append(element('p', name));
  }
  section.append(element('p', chatText(language, 'ვარიანტი დასაზუსტებელია; ნაწილების შესაბამისობა დაუდასტურებელია.', 'Variant selection and part fitment are unconfirmed.'), 'muted'));
  return section;
}
function researchSection(record, language) {
  if (!record.claims?.length && !record.citations?.length) return null;
  const section = element('details', null, 'research-findings');
  section.append(element('summary', chatText(language, 'წყაროები და დასაბუთება', 'Sources and supporting findings')));
  const oilLabels = {
    grades: chatText(language, 'ზეთის სიბლანტე', 'Oil grade'),
    capacity_l: chatText(language, 'ზეთის მოცულობა (ლ)', 'Oil capacity (L)'),
    engine_code: chatText(language, 'ძრავის კოდი', 'Engine code'),
  };
  for (const claim of (record.claims || []).slice(0, 12)) {
    const text = claim.statement || claim.text || claim.supporting_excerpt;
    if (typeof text === 'string' && text) section.append(element('p', text));
    if (typeof claim.supporting_excerpt === 'string' && claim.supporting_excerpt !== text) {
      section.append(element('p', chatText(language, 'წყაროს ამონარიდი: ', 'Source excerpt: ') + claim.supporting_excerpt, 'muted'));
    }
    const label = Object.hasOwn(oilLabels, claim.kind) ? oilLabels[claim.kind] : null;
    if (label) {
      const values = Array.isArray(claim.value) ? claim.value : [claim.value];
      const value = values.filter(item => typeof item === 'string' || (typeof item === 'number' && Number.isFinite(item))).slice(0, 12).join(', ');
      if (value) section.append(element('p', chatText(language, 'წყაროს მიხედვით: ', 'Source reports: ') + label + ' ' + value));
    }
    const sources = (Array.isArray(claim.sources) ? claim.sources : []).filter(item => typeof item === 'string');
    if (sources.length) section.append(element('p', chatText(language, 'წყარო: ', 'Source: ') + sources.join(', '), 'muted'));
  }
  section.append(element('p', chatText(language, 'წყაროს მტკიცებაა; ამ ავტომობილზე გამოყენება დაუდასტურებელია.', 'Source assertion; vehicle applicability unconfirmed.'), 'muted'));
  const seen = new Set();
  for (const citation of (record.citations || []).slice(0, 20)) {
    const url = citation.url || citation.source_url;
    // Only public HTTPS links; never execute a source-provided scheme or credentials.
    if (typeof url !== 'string' || !/^https:\/\/[a-z0-9][a-z0-9.-]*(?::443)?(?:[/?#][^\u0000-\u0020\\]*)?$/i.test(url) || seen.has(url)) continue;
    seen.add(url);
    const link = element('a', citation.title || citation.source_title || citation.source_name || url, 'source-link');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    section.append(link);
  }
  section.append(element('p', chatText(language, 'წყაროს მტკიცება არ ადასტურებს ჩვენს მარაგს ან ნაწილების ურთიერთჩანაცვლებადობას.', 'Source assertions do not establish warehouse stock or interchangeability.'), 'muted'));
  return section;
}
async function showChatPage(target, pages, record, identifier, turnId, revision, language, append = false) {
  if (revision !== chatRevision) return;
  renderChatCards(target, record, language, append);
  pages.replaceChildren();
  if (record.next_offset != null) {
    const remaining = Math.max(0, (record.total || 0) - record.next_offset);
    const more = element('button', chatText(language, `კიდევ ნახვა (${remaining})`, `Show more (${remaining})`), 'secondary');
    more.type = 'button';
    more.addEventListener('click', async () => {
      more.disabled = true;
      try {
        const next = await api(`/chat/conversations/${encodeURIComponent(identifier)}/turns/${encodeURIComponent(turnId)}?offset=${record.next_offset}&limit=10`, undefined, false);
        await showChatPage(target, pages, next, identifier, turnId, revision, language, true);
      } catch (error) {
        if (revision === chatRevision) pages.append(element('p', error.message, 'error'));
      } finally { more.disabled = false; }
    });
    pages.append(more);
  }
}
function progressLine(record, language) {
  // The assistant engine reports every search it runs, with the words it searches by: first
  // that it is searching, then, once the rows are in, that it is choosing among them.
  const steps = (record.progress || []).filter(item => item && typeof item === 'object' && typeof item.stage === 'string' && !item.source_id);
  if (steps.length) {
    const running = steps.filter(item => item.status === 'RUNNING');
    const words = (running.length ? running : steps).map(item => typeof item.label === 'string' ? item.label.trim() : '').filter(Boolean).slice(0, 3).join(' | ');
    const lead = running.length ? chatText(language, 'ვეძებ', 'Searching') : chatText(language, 'ვარჩევ ნაპოვნიდან', 'Choosing from what was found');
    return words ? `${lead}: ${words.length > 96 ? words.slice(0, 95) + '…' : words}…` : `${lead}…`;
  }
  const car = carLabel(record.vehicle) || (typeof record.car_label === 'string' ? record.car_label.trim() : '');
  const sources = (record.progress || []).filter(item => item && typeof item === 'object' && item.source_id);
  const finished = sources.filter(item => !['RUNNING', 'PENDING'].includes(item.status)).length;
  const base = car ? chatText(language, `ვამოწმებ მარაგს თქვენი ${car}-ისთვის…`, `Checking stock for your ${car}…`)
    : chatText(language, 'ვეძებ…', 'Looking that up…');
  return sources.length ? `${base} ${finished}/${sources.length}` : base;
}
// One turn of the conversation. `shown` is what the clerk's bubble says when a tapped button
// sends other words, `origin` the tapped button, `quiet` a repeat without a second bubble,
// `choice` the 1-based number of a tapped answer, `keyboard` whether the tap came from a key.
async function runChat(message, {shown, origin, quiet = false, choice, keyboard = false} = {}) {
  if (!message || chatBusy) return;
  const revision = chatRevision;
  const chosenLanguage = byId('chat-language').value;
  const fixedLanguage = ['ka', 'en'].includes(chosenLanguage) ? chosenLanguage : null;
  let language = fixedLanguage || guessLanguage(message, lastLanguage);
  chatBusy = true;
  byId('chat-send').disabled = true;
  restoreComposerHint();
  parkFocus(origin);
  if (failedRetry) {
    // An older failure keeps its text; only its offer to repeat goes.
    parkFocus(failedRetry);
    failedRetry.remove();
    failedRetry = null;
  }
  const retired = retireChoices(origin);
  // The conversation has a message now: the starters and the hint make room for it.
  showStarters(false);
  // Raw VIN is sent only in request bodies; redact it from the visible transcript.
  if (!quiet) chatMessage('user', (shown || message).replace(VIN_IN_TEXT, '[VIN]'), language);
  const progress = byId('chat-progress');
  progress.textContent = chatText(language, 'ვეძებ…', 'Looking that up…');
  let typing = typingBubble(language);
  // Cards found so far appear in a live bubble while slower sources finish.
  let live = null;
  let liveCards = null;
  let liveFindings = null;
  const removeTyping = () => { if (typing) { typing.remove(); typing = null; } };
  const removeLive = () => { if (live) { live.remove(); live = null; } };
  // Whether the server may still be waiting for an answer to the open question. Not once it has
  // taken this message, and not when it has lost the conversation: a repeat then sends text only.
  let questionKept = true;
  const rerun = (byKey) => runChat(message, {shown, origin, quiet: true, choice: questionKept ? choice : undefined, keyboard: byKey});
  try {
    // The conversation may already be opening: the clerk had started to write.
    if (opening) {
      await opening;
      if (revision !== chatRevision) return;
    }
    if (!conversationId) {
      const created = await api('/chat/conversations', {}, false);
      if (revision !== chatRevision) { deleteConversation(created.conversation_id); return; }
      conversationId = created.conversation_id;
    }
    const identifier = conversationId;
    // "auto" lets the server follow the customer's letters and keep it for codes and VINs.
    const body = {message, language: fixedLanguage || 'auto'};
    if (Number.isInteger(choice) && choice > 0) body.choice = choice;
    const suppliedVin = message.match(/\b[A-HJ-NPR-Z0-9]{17}\b/i)?.[0];
    const selected = readVehicle();
    if (selected && !suppliedVin) body.vehicle = selected;
    const vin = (suppliedVin || byId('vin').value.trim()).toUpperCase();
    if (/^[A-HJ-NPR-Z0-9]{17}$/.test(vin)) {
      body.vin = vin;
      if (suppliedVin) byId('vin').value = vin;
    }
    const turn = await api(`/chat/conversations/${encodeURIComponent(identifier)}/messages`, body, false);
    questionKept = false;
    if (revision !== chatRevision) return;
    const turnId = turn.turn_id;
    if (turn.language) language = turn.language;
    lastLanguage = language;
    let polls = 0;
    let throttled = 0;
    while (revision === chatRevision) {
      let record;
      try {
        record = await api(`/chat/conversations/${encodeURIComponent(identifier)}/turns/${encodeURIComponent(turnId)}?offset=0&limit=5`, undefined, false);
      } catch (error) {
        // The turn keeps running on the server; a throttled status read is retried.
        if (error.status !== 429 || ++throttled > 30) throw error;
        await new Promise(resolve => setTimeout(resolve, 2000));
        continue;
      }
      if (revision !== chatRevision) return;
      const formTop = composerTop();
      if (record.language) language = lastLanguage = record.language;
      if (record.vehicle && Object.keys(record.vehicle).length) {
        vehicle = record.vehicle;
        for (const key of vehicleKeys) byId(key).value = record.vehicle[key] ?? (key === 'fuel' ? record.vehicle.fuel_type : '') ?? '';
        updateSelectedVehicle(record.vehicle);
      }
      // The engine's own short name for the car wins over the one built from the record. The
      // side panel shows it too while the vehicle form names no car; the form's car wins there.
      if (typeof record.car_label === 'string' && record.car_label.trim()) {
        const named = record.car_label.trim();
        if (named !== chatCar) {
          chatCar = named;
          const held = record.vehicle && Object.keys(record.vehicle).length ? record.vehicle : readVehicle();
          updateSelectedVehicle(held);
        }
        showChatCar(named, language);
      }
      if (record.status !== 'RUNNING' && record.status !== 'PENDING') {
        const follow = followingTranscript();
        removeTyping();
        removeLive();
        const failed = record.status === 'FAILED';
        const reply = chatMessage(failed ? 'assistant failed' : 'assistant', record.answer || chatText(language, 'მზადაა.', 'Done.'), language);
        // One short caveat at most; older servers without it list their warnings.
        if ('note' in record) {
          if (record.note) reply.append(element('p', record.note, 'note'));
          // The assistant engine (the one that states an outcome) writes no note of its own:
          // its warning that the stock figures are old is said here, under the reply's text.
          else if (record.outcome && (record.cards || []).length && (record.warnings || []).includes('STOCK_MAY_BE_STALE')) reply.append(element('p', warningText(language, 'STOCK_MAY_BE_STALE'), 'note'));
          // A turn that failed because the provider's account refuses requests says so to
          // the shop: nobody at the counter can fix it by sending the message again.
          const cause = failed && (record.warnings || []).find(warning => accountWarnings.includes(warning));
          if (cause) reply.append(element('p', warningText(language, cause), 'warning'));
        } else {
          for (const warning of record.warnings || []) reply.append(element('p', warningText(language, warning), 'warning'));
        }
        // Answer buttons sit right under the question they answer, above any cards. A server
        // without the new keys, or one that leaves them empty, sends its choices as plain lists.
        const modern = [record.question?.options, record.suggestions].some(list => Array.isArray(list) && list.length);
        const answers = modern ? choiceGroup(record.question?.options, 'question', language)
          : choiceGroup(record.clarification_choices?.length ? record.clarification_choices : record.quick_replies, 'answers', language);
        if (answers) {
          reply.append(answers);
          openChoices = answers;
        }
        const variants = resolutionSection(record, language);
        if (variants) reply.append(variants);
        const research = researchSection(record, language);
        if (research) reply.append(research);
        for (const line of requirementLines(record, language)) reply.append(element('p', line, 'requirement'));
        const cards = element('div', null, 'chat-cards');
        const pages = element('nav', null, 'actions chat-pages');
        pages.ariaLabel = chatText(language, 'ნაწილების გვერდები', 'Part pages');
        reply.append(cards, pages);
        await showChatPage(cards, pages, record, identifier, turnId, revision, language);
        const supplier = supplierSection(record, language);
        if (supplier) reply.append(supplier);
        let again = null;
        if (failed) {
          // The answer did not go through: the same message can be sent again, or another answer picked.
          again = retryButton(reply, language, rerun);
          if (!answers) restoreChoices(retired, true);
        }
        // Next steps are optional and quieter: they close the reply and go with the next message.
        const next = modern ? choiceGroup(record.suggestions, 'next', language) : null;
        if (next) {
          reply.append(next);
          nextSteps = next;
        }
        keepInView(reply, follow);
        if (keyboard) followFocus(answers?.querySelectorAll('button')[0] || again);
        progress.textContent = '';
        break;
      }
      if ((record.cards || []).length || record.supplier_only?.length || record.claims?.length || record.resolution_candidates?.length) {
        const fresh = !live;
        const follow = fresh && followingTranscript();
        if (!live) {
          removeTyping();
          live = element('article', null, 'chat-message assistant live');
          live.lang = language;
          live.append(element('p', chatText(language, 'ჯერ კიდევ ვამოწმებ — აქამდე ვიპოვე:', 'Still checking — found so far:'), 'message-text'));
          liveCards = element('div', null, 'chat-cards');
          liveFindings = element('div', null, 'live-findings');
          live.append(liveCards, liveFindings);
          byId('chat-result').append(live);
        }
        renderChatCards(liveCards, record, language);
        liveFindings.replaceChildren();
        for (const section of [resolutionSection(record, language), researchSection(record, language), supplierSection(record, language)]) {
          if (section) liveFindings.append(section);
        }
        // Shown from its top when it first appears; later results grow below without moving the view.
        if (fresh) keepInView(live, follow);
      }
      progress.textContent = progressLine(record, language);
      // Live results or a newly named car may have pushed the composer down: keep it in view.
      if (composerTop() !== formTop) holdComposer();
      // Poll quickly at first: small talk and code answers finish in milliseconds.
      polls += 1;
      await new Promise(resolve => setTimeout(resolve, polls < 6 ? 150 : polls < 20 ? 500 : 1000));
    }
  } catch (error) {
    if (revision === chatRevision) {
      const follow = followingTranscript();
      removeTyping();
      removeLive();
      if (error.status === 404) {
        // The server no longer has this conversation: the repeat has to start a new one.
        deleteConversation(conversationId);
        conversationId = null;
        questionKept = false;
      }
      // Nothing was answered: the open question takes answers again, and the message can be repeated.
      restoreChoices(retired, !questionKept);
      const failure = chatMessage('assistant failed', error.message, language);
      const again = retryButton(failure, language, rerun);
      keepInView(failure, follow);
      if (keyboard) followFocus(again);
      progress.textContent = chatText(language, 'ვერ შესრულდა. სცადეთ ხელახლა ან დაიწყეთ ახალი ჩატი.', 'Request failed. Try again or start a new chat.');
    }
  } finally {
    if (revision === chatRevision) {
      removeTyping();
      chatBusy = false;
      byId('chat-send').disabled = false;
    }
  }
}
byId('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = byId('message').value.trim();
  if (!message || chatBusy) return;
  byId('message').value = '';
  fitComposer();
  await runChat(message);
});
// The first words of a message open the conversation ahead of it (see openAhead), and the box
// grows with what is written.
byId('message').addEventListener('input', () => {
  fitComposer();
  if (byId('message').value.trim()) openAhead();
});
// Like a messenger: Enter sends, Shift+Enter starts a new line.
byId('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!chatBusy) byId('chat-form').requestSubmit();
  }
});
byId('new-chat').addEventListener('click', () => {
  resetChat();
  vehicle = null;
  contextRevision += 1;
  invalidateUnified();
  vehicleKeys.forEach(key => { byId(key).value = ''; });
  byId('vin').value = '';
  byId('message').value = '';
  fitComposer();
  byId('vehicle-result').replaceChildren();
  byId('context-status').textContent = 'ავტომობილი ჯერ არ არის არჩეული.';
  updateSelectedVehicle(null);
  byId('message').focus();
});
// "Change" only points at the composer: the next message names the other car. The vehicle
// form is left alone, because typing there starts the whole chat over.
byId('chat-car-change').addEventListener('click', () => {
  const box = byId('message');
  if (composerHint === null) composerHint = box.placeholder ?? '';
  box.placeholder = chatText(lastLanguage || 'ka', 'ჩაწერეთ სხვა მანქანა ან VIN', 'Type another car or a VIN');
  box.focus();
});
for (const [id, prompt] of [['prompt-brakes', 'წინა ხუნდები მჭირდება'], ['prompt-code', 'ჰაერის ფილტრი მჭირდება'], ['prompt-oil', 'ზეთის შეცვლა მინდა']]) {
  byId(id).addEventListener('click', () => sendChat(prompt));
}
