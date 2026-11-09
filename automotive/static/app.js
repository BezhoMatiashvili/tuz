'use strict';
const byId = (id) => document.getElementById(id);
const vehicleKeys = ['make', 'model', 'model_year', 'engine_code', 'fuel', 'transmission', 'market', 'body', 'drive'];
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
async function api(path, body, checkContext = true, method = null) {
  const revision = contextRevision;
  const response = await fetch('/api/v1' + path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'omit',
  });
  if (!response.ok) {
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
    throw new Error(messages[response.status] || 'მოთხოვნა ვერ შესრულდა. სცადეთ ხელახლა.');
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
    byId('evidence-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  target.append(link);
}
function product(target, item) {
  const card = element('article', null, 'product');
  renderStatus(card, item);
  card.append(element('h3', item.product_name || item.name || 'კატალოგის ჩანაწერი'));
  card.append(element('p', item.product_code || item.code || item.source_product_id || '', 'code'));
  if (item.brand) card.append(element('p', 'მწარმოებელი: ' + item.brand));
  if (item.product_model) card.append(element('p', 'კატალოგის აღწერა: ' + item.product_model));
  card.append(element('p', 'ფასი: ' + (item.price == null ? 'უცნობია' : item.price + ' ' + (item.currency || 'ვალუტა უცნობია')) + ' · მარაგი: ' + (item.stock == null ? 'უცნობია' : item.stock)));
  if (item.last_observed_at) card.append(element('p', 'ბოლო დაკვირვება: ' + item.last_observed_at, 'muted'));
  if (item.matched_codes?.length) card.append(element('pre', JSON.stringify(item.matched_codes, null, 2)));
  for (const evidence of item.evidence || []) evidenceLink(card, evidence);
  target.append(card);
}
const labels = {make: 'მწარმოებელი', model: 'მოდელი', model_year: 'მოდელის წელი', engine_code: 'ძრავის კოდი', engine_model: 'ძრავის აღწერა', fuel: 'საწვავი', transmission: 'ტრანსმისია', market: 'ბაზარი', body: 'ძარის ტიპი', drive: 'ამძრავი', displacement_l: 'ძრავის მოცულობა (ლ)', engine_cylinders: 'ცილინდრები', source_name: 'წყარო', source_url: 'წყაროს მისამართი', authority: 'უფლებამოსილება', reuse_permission: 'გამოყენების უფლება', review_status: 'გადახედვის სტატუსი', reviewed_by: 'შემმოწმებელი', reviewed_at: 'გადამოწმების დრო', observed_at: 'დაკვირვების დრო', part_code: 'ნაწილის კოდი', from_code: 'საწყისი კოდი', to_code: 'შემცვლელი კოდი', relation: 'მიმართება', approval: 'დაშვება', approvals: 'დაშვებები', specification: 'სპეციფიკაცია', viscosity: 'სიბლანტე', approval_code: 'დაშვების კოდი', requirements: 'მოთხოვნები', applicability: 'გამოყენების პირობები', restrictions: 'შეზღუდვები', supersession: 'ჩანაცვლების პირობები', compatible: 'თავსებადობა', payload: 'წყაროს მტკიცება', notes: 'შენიშვნა', direction: 'მიმართულება', kind: 'მტკიცებულების ტიპი', value: 'მნიშვნელობა', unit: 'ერთეული'};
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
    if (value) result[key] = key === 'model_year' ? Number(value) : value;
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
    const response = await fetch('/api/v1/status', {credentials: 'omit'});
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
        byId('search-result').scrollIntoView({behavior: 'smooth', block: 'start'});
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
  DATABASE_UNAVAILABLE: 'ბაზასთან კავშირი ვერ დამყარდა', DECODE_FAILED: 'VIN-ის ამოცნობა ვერ მოხერხდა',
  TIME_LIMIT: 'დროის ლიმიტს მიაღწია; მიღებული შედეგები შენარჩუნებულია',
  RATE_LIMITED: 'მოთხოვნების ლიმიტს მიაღწია', ACCESS_REQUIRED: 'წყარომ ავტორიზაცია მოითხოვა',
  PROVIDER_UNAVAILABLE: 'გარე წყარო დროებით მიუწვდომელია', INVALID_RESPONSE: 'წყაროს პასუხი ვერ გადამოწმდა',
  VEHICLE_CONTEXT_MISMATCH: 'ავტომობილის მონაცემები არ ემთხვევა; შეუსაბამო კოდები დამალულია',
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
          byId('search-result').scrollIntoView({behavior: 'smooth', block: 'start'});
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
function updateSelectedVehicle(selected) {
  const target = byId('selected-vehicle');
  target.replaceChildren();
  if (!selected || !Object.keys(selected).length) {
    target.append(element('p', 'ავტომობილი ჯერ არ არის არჩეული. / No vehicle selected yet.', 'muted'));
    return;
  }
  target.append(element('h3', [selected.make, selected.model, selected.model_year].filter(Boolean).join(' ')));
  const details = {};
  for (const key of vehicleKeys) if (selected[key]) details[key] = selected[key];
  facts(target, details);
  target.append(element('p', 'თავსებადობა მოწმდება თითოეული ნაწილისთვის. / Fitment is checked for each part.', 'muted'));
}
function deleteConversation(identifier) {
  if (identifier) api('/chat/conversations/' + encodeURIComponent(identifier), undefined, false, 'DELETE').catch(() => {});
}
function resetChat() {
  chatRevision += 1;
  deleteConversation(conversationId);
  conversationId = null;
  chatBusy = false;
  byId('chat-send').disabled = false;
  byId('chat-result').replaceChildren();
  byId('chat-progress').textContent = '';
}
function chatMessage(role, text, language) {
  const section = element('article', null, 'chat-message ' + role);
  section.lang = language;
  section.append(element('h3', role === 'user' ? chatText(language, 'თქვენ', 'You') : chatText(language, 'ასისტენტი', 'Assistant'), 'message-author'));
  if (text) section.append(element('p', text, 'message-text'));
  byId('chat-result').append(section);
  return section;
}
function renderChatCards(target, record, language) {
  target.replaceChildren();
  const fitmentLabels = {
    VERIFIED: chatText(language, 'დადასტურებული შესაბამისობა', 'Verified fitment'),
    PROVISIONAL: chatText(language, 'კანდიდატი; შესაბამისობა დაუდასტურებელია', 'Candidate; fitment unconfirmed'),
    UNKNOWN: chatText(language, 'შესაბამისობა უცნობია', 'Fitment unknown'),
    CONFLICTED: chatText(language, 'ურთიერთსაწინააღმდეგო მტკიცებულება', 'Conflicting fitment evidence'),
  };
  for (const item of record.cards || []) {
    const row = item.product || {};
    const card = element('article', null, 'chat-part');
    card.append(element('h4', row.product_name || chatText(language, 'კატალოგის ჩანაწერი', 'Catalog item')));
    card.append(element('p', row.brand || chatText(language, 'მწარმოებელი უცნობია', 'Manufacturer unknown'), 'muted'));
    card.append(element('p', chatText(language, 'ძირითადი კოდი: ', 'Main code: ') + (row.product_code || '—'), 'code'));
    const matches = (row.matched_codes || []).map(code => typeof code === 'object' ? (code.original || code.code || code.normalized_code || code.normalized || code.raw || '') : code).filter(Boolean);
    card.append(element('p', chatText(language, 'დამთხვეული კოდები: ', 'Matched codes: ') + (matches.join(', ') || '—')));
    card.append(element('p', chatText(language, 'დამთხვევის წყარო: ', 'Match source: ') + (item.match_source || 'catalog')));
    const status = item.fitment_status || 'UNKNOWN';
    card.append(element('span', item.compatible === false ? chatText(language, 'შესაბამისობა უარყოფილია', 'Fitment excluded') : (fitmentLabels[status] || fitmentLabels.UNKNOWN), 'status ' + status));
    card.append(element('p', chatText(language, 'ნაპოვნია ჩვენს კატალოგში; მარაგი დაუდასტურებელია.', 'Found in our catalog; stock unconfirmed.'), 'availability'));
    card.append(element('p', chatText(language, 'ფასი: უცნობია', 'Price: unknown'), 'muted'));
    card.append(element('p', chatText(language, 'ბოლო დაკვირვება: ', 'Observed: ') + (row.last_observed_at || chatText(language, 'უცნობია', 'unknown')), 'muted'));
    for (const warning of item.warnings || []) card.append(element('p', warning, 'warning'));
    target.append(card);
  }
  if (record.supplier_only?.length) {
    const supplier = element('section', null, 'supplier-only');
    supplier.append(element('h4', chatText(language, 'მომწოდებლის კოდები · საწყობში დამთხვევის გარეშე', 'Supplier codes · no warehouse match')));
    for (const item of record.supplier_only) {
      supplier.append(element('p', [item.name, item.manufacturer, item.mpn || item.main_code, ...(item.oem_codes || [])].filter(Boolean).join(' · '), 'code'));
    }
    supplier.append(element('p', chatText(language, 'ჩვენს საწყობში არსებობა და შესაბამისობა დაუდასტურებელია.', 'Warehouse availability and fitment are unconfirmed.'), 'muted'));
    target.append(supplier);
  }
}
async function showChatPage(target, pages, record, identifier, turnId, revision, language) {
  if (revision !== chatRevision) return;
  renderChatCards(target, record, language);
  pages.replaceChildren();
  if (record.next_offset != null) {
    const more = element('button', chatText(language, 'შემდეგი ნაწილები', 'Next parts'), 'secondary');
    more.type = 'button';
    more.addEventListener('click', async () => {
      more.disabled = true;
      try {
        const next = await api(`/chat/conversations/${encodeURIComponent(identifier)}/turns/${encodeURIComponent(turnId)}?offset=${record.next_offset}&limit=20`, undefined, false);
        await showChatPage(target, pages, next, identifier, turnId, revision, language);
      } catch (error) {
        if (revision === chatRevision) pages.append(element('p', error.message, 'error'));
      } finally { more.disabled = false; }
    });
    pages.append(more);
  }
  const offset = record.offset || 0;
  if (record.total) pages.append(element('p', `${offset + 1}–${offset + (record.cards || []).length} / ${record.total}`, 'muted'));
}
byId('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = byId('message').value.trim();
  if (!message || chatBusy) return;
  const revision = chatRevision;
  const chosenLanguage = byId('chat-language').value;
  const language = ['ka', 'en'].includes(chosenLanguage) ? chosenLanguage : (/[\u10a0-\u10ff]/.test(message) ? 'ka' : 'en');
  chatBusy = true;
  byId('chat-send').disabled = true;
  // Raw VIN is sent only in request bodies; redact it from the visible transcript.
  chatMessage('user', message.replace(/\b[A-HJ-NPR-Z0-9]{17}\b/gi, '[VIN]'), language);
  byId('message').value = '';
  const progress = byId('chat-progress');
  progress.textContent = chatText(language, 'კითხვა მუშავდება…', 'Working on your request…');
  try {
    if (!conversationId) {
      const created = await api('/chat/conversations', {}, false);
      if (revision !== chatRevision) { deleteConversation(created.conversation_id); return; }
      conversationId = created.conversation_id;
    }
    const identifier = conversationId;
    const body = {message, language};
    const suppliedVin = message.match(/\b[A-HJ-NPR-Z0-9]{17}\b/i)?.[0];
    const selected = readVehicle();
    if (selected && !suppliedVin) body.vehicle = selected;
    const vin = (suppliedVin || byId('vin').value.trim()).toUpperCase();
    if (/^[A-HJ-NPR-Z0-9]{17}$/.test(vin)) {
      body.vin = vin;
      if (suppliedVin) byId('vin').value = vin;
    }
    const turn = await api(`/chat/conversations/${encodeURIComponent(identifier)}/messages`, body, false);
    if (revision !== chatRevision) return;
    const turnId = turn.turn_id;
    while (revision === chatRevision) {
      const record = await api(`/chat/conversations/${encodeURIComponent(identifier)}/turns/${encodeURIComponent(turnId)}?offset=0&limit=20`, undefined, false);
      if (revision !== chatRevision) return;
      if (record.vehicle && Object.keys(record.vehicle).length) {
        vehicle = record.vehicle;
        for (const key of vehicleKeys) byId(key).value = record.vehicle[key] ?? '';
        updateSelectedVehicle(record.vehicle);
      }
      if (record.status !== 'RUNNING' && record.status !== 'PENDING') {
        const reply = chatMessage('assistant', record.answer || chatText(language, 'მოთხოვნა დასრულდა.', 'Your request is complete.'), language);
        for (const warning of record.warnings || []) reply.append(element('p', warning, 'warning'));
        const cards = element('div', null, 'chat-cards');
        const pages = element('nav', null, 'actions chat-pages');
        pages.ariaLabel = chatText(language, 'ნაწილების გვერდები', 'Part pages');
        reply.append(cards, pages);
        await showChatPage(cards, pages, record, identifier, turnId, revision, language);
        progress.textContent = record.status === 'PARTIAL' ? chatText(language, 'ნაწილობრივი შედეგი; იხილეთ გაფრთხილებები.', 'Partial results; see the warnings.') : chatText(language, 'პასუხი მზადაა.', 'Reply ready.');
        break;
      }
      const sources = (record.progress || []).map(item => typeof item === 'string' ? item : [item.label || item.source_id, item.status].filter(Boolean).join(': '));
      progress.textContent = sources.join(' · ') || chatText(language, 'წყაროები მოწმდება…', 'Checking sources…');
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  } catch (error) {
    if (revision === chatRevision) {
      chatMessage('assistant', error.message, language);
      progress.textContent = chatText(language, 'ვერ შესრულდა. სცადეთ ხელახლა ან დაიწყეთ ახალი ჩატი.', 'Request failed. Try again or start a new chat.');
    }
  } finally {
    if (revision === chatRevision) {
      chatBusy = false;
      byId('chat-send').disabled = false;
    }
  }
});
byId('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
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
  byId('vehicle-result').replaceChildren();
  byId('context-status').textContent = 'ავტომობილი ჯერ არ არის არჩეული.';
  updateSelectedVehicle(null);
  byId('message').focus();
});
for (const [id, prompt] of [['prompt-brakes', 'მუხრუჭების მოძებნა ჩემი ავტომობილისთვის'], ['prompt-code', 'Find AP104/2'], ['prompt-oil', 'რომელი ძრავის ზეთი მჭირდება?']]) {
  byId(id).addEventListener('click', () => {
    byId('message').value = prompt;
    byId('message').focus();
  });
}
