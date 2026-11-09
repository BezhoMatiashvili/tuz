'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {token: '', tables: [], current: null, page: 0, rows: [], columns: [], query: '', queryPage: 0, queryHasMore: false, suggestions: [], suggestionIndex: 0};
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const format = (value) => value === null ? '<span class="null">NULL</span>' : escapeHTML(typeof value === 'object' ? JSON.stringify(value) : String(value));
const identifier = (name) => `"${name.replaceAll('"', '""')}"`;
const qualified = (table) => `${identifier(table.schema)}.${identifier(table.name)}`;
async function api(path, body = {}) {
  const response = await fetch(`/api/${path}`, {method:'POST', headers:{'Content-Type':'application/json', 'X-DB-UI-Token':state.token}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed.');
  return result;
}
function setView(view) {
  document.querySelectorAll('.view').forEach((section) => section.hidden = section.id !== `${view}-view`);
  document.querySelectorAll('.nav').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
  $('#crumb').textContent = view === 'table' ? state.current.name : view === 'sql' ? 'SQL editor' : 'Overview';
  window.scrollTo(0, 0);
}
function showNotice(message) {const node = $('#notice'); node.hidden = !message; node.textContent = message || '';}
function renderTables() {
  const search = $('#search').value.toLowerCase();
  const tables = state.tables.filter((table) => `${table.schema}.${table.name}`.toLowerCase().includes(search));
  const groups = new Map();
  tables.forEach((table) => {if (!groups.has(table.schema)) groups.set(table.schema, []); groups.get(table.schema).push(table);});
  $('#table-list').innerHTML = tables.length ? [...groups].map(([schema, items]) => `<div class="schema-label">${escapeHTML(schema)}</div>${items.map((t) => `<button class="table-link ${state.current === t ? 'selected' : ''}" data-table="${state.tables.indexOf(t)}"><span>▦</span> ${escapeHTML(t.name)}</button>`).join('')}`).join('') : '<p class="sidebar-hint">No tables found.</p>';
  $('#directory').innerHTML = tables.length ? tables.map((table) => `<button class="directory-row" data-table="${state.tables.indexOf(table)}"><div class="directory-name"><span class="table-icon">▦</span><div><strong>${escapeHTML(table.name)}</strong><small>${escapeHTML(table.description)}</small></div></div><span class="schema-badge">${escapeHTML(table.schema)}</span><span class="col-count">${table.columns.length} fields</span><span class="row-arrow">↗</span></button>`).join('') : '<div class="empty">No tables found. Check the connection or your role permissions.</div>';
  $('#table-count').textContent = state.tables.length.toString().padStart(2, '0');
  $('#schema-count').textContent = new Set(state.tables.map((t) => t.schema)).size.toString().padStart(2, '0');
  $('#directory-count').textContent = `${tables.length} ${tables.length === 1 ? 'TABLE' : 'TABLES'}`;
}
async function refresh() {
  try {
    state.tables = await api('catalog');
    $('#connection-title').textContent = 'Connected'; $('#connection-subtitle').textContent = 'PostgreSQL · read only'; $('.status-dot').classList.remove('off'); showNotice(''); renderTables();
  } catch (error) {
    $('#connection-title').textContent = 'Connection unavailable'; $('#connection-subtitle').textContent = 'Check database settings'; $('.status-dot').classList.add('off'); showNotice(error.message); renderTables();
  }
}
function activateTab(tab) {
  document.querySelectorAll('.tab').forEach((button) => button.classList.toggle('active', button.dataset.tab === tab));
  $('#data-panel').hidden = tab !== 'data'; $('#schema-panel').hidden = tab !== 'schema';
}
async function openTable(index) {
  const table = state.tables[index]; if (!table) return;
  state.current = table; state.page = 0; renderTables();
  $('#table-path').textContent = `${table.schema} / TABLE`; $('#table-title').textContent = table.name; $('#table-description').textContent = table.description;
  $('#column-tab-count').textContent = table.columns.length; $('#row-tab-count').textContent = '';
  $('#columns-list').innerHTML = table.columns.map((col) => `<div class="column-row"><strong>${escapeHTML(col.name)} ${col.primary_key ? '<span class="key">PRIMARY KEY</span>' : ''}</strong><div class="column-type">${escapeHTML(col.type)}<br><small>${col.nullable ? 'nullable' : 'not null'}</small></div><p>${escapeHTML(col.description)}${col.default ? `<br>Default: <code>${escapeHTML(col.default)}</code>` : ''}</p></div>`).join('');
  activateTab('data'); setView('table'); await loadRows();
}
function renderGrid(target, columns, rows, clickable = false) {
  if (!columns.length) {target.innerHTML = '<div class="empty">Statement completed without a result set.</div>'; return;}
  if (!rows.length) {target.innerHTML = '<div class="empty">No rows returned.</div>'; return;}
  target.innerHTML = `<table class="data-table"><thead><tr>${columns.map((c) => `<th>${escapeHTML(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((row, index) => `<tr ${clickable ? `class="clickable" tabindex="0" data-row="${index}"` : ''}>${row.map((cell) => `<td>${format(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
async function loadRows() {
  const table = state.current; const page = state.page;
  $('#table-data').innerHTML = '<div class="empty">Loading rows…</div>'; $('#prev-page').disabled = true; $('#next-page').disabled = true;
  try {
    const data = await api('browse', {schema:table.schema, table:table.name, page});
    if (table !== state.current || page !== state.page) return;
    state.rows = data.rows; state.columns = data.columns; renderGrid($('#table-data'), data.columns, data.rows, true);
    $('#page-number').textContent = `Page ${page + 1}`; $('#prev-page').disabled = page === 0; $('#next-page').disabled = !data.has_more; $('#row-tab-count').textContent = data.rows.length;
  } catch (error) {$('#table-data').innerHTML = `<div class="error">${escapeHTML(error.message)}</div>`; $('#prev-page').disabled = page === 0;}
}
function inspectRow(index) {
  const row = state.rows[index]; if (!row) return;
  $('#row-detail').innerHTML = state.columns.map((name, i) => {const col = state.current.columns.find((c) => c.name === name); const val = row[i]; return `<div class="detail-row"><div><strong>${escapeHTML(name)}</strong><small>${escapeHTML(col?.description || 'No description available for this field.')}</small></div><div class="detail-value">${val === null ? '<span class="null">NULL</span>' : escapeHTML(typeof val === 'object' ? JSON.stringify(val, null, 2) : String(val))}</div></div>`;}).join('');
  $('#row-dialog').showModal();
}
async function runQuery(page = 0, query = $('#query').value) {
  const button = $('#run-query'); if (button.disabled) return; button.disabled = true; $('#query-status').textContent = 'Running query…'; $('#query-results').classList.remove('empty-state');
  $('#query-actions').hidden = true;
  const start = performance.now();
  try {
    const result = await api('query', {query, page});
    state.query = query; state.queryPage = page; state.queryHasMore = result.has_more;
    renderGrid($('#query-results'), result.columns, result.rows);
    const first = result.rows.length ? page * 200 + 1 : 0;
    const last = result.rows.length ? page * 200 + result.rows.length : 0;
    $('#query-status').textContent = `${first}–${last} rows${result.has_more ? ' · more available' : ''} · ${((performance.now() - start) / 1000).toFixed(2)}s`;
    $('#query-page').textContent = `Page ${page + 1}`;
    $('#query-prev').disabled = page === 0; $('#query-next').disabled = !result.has_more;
    $('#query-actions').hidden = !result.columns.length;
  } catch (error) {$('#query-results').innerHTML = `<div class="error">${escapeHTML(error.message)}</div>`; $('#query-status').textContent = 'Query could not be completed.';}
  finally {button.disabled = false;}
}
async function exportResults(button) {
  const type = button.dataset.export;
  const table = state.current;
  const keys = table?.columns.filter((col) => col.primary_key).map((col) => identifier(col.name)) || [];
  const query = button.dataset.source === 'query' ? state.query : `SELECT * FROM ${qualified(table)}${keys.length ? ` ORDER BY ${keys.join(', ')}` : ''}`;
  button.disabled = true; showNotice('');
  try {
    const response = await fetch('/api/export', {method:'POST', headers:{'Content-Type':'application/json', 'X-DB-UI-Token':state.token}, body:JSON.stringify({query, format:type})});
    if (!response.ok) {const result = await response.json(); throw new Error(result.error || 'Export failed.');}
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = `warehouse-results.${type}`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {showNotice(error.message); window.scrollTo(0, 0);}
  finally {button.disabled = false;}
}
const keywords = 'SELECT FROM WHERE JOIN LEFT RIGHT INNER OUTER ON AS AND OR NOT NULL IS IN LIKE ILIKE ORDER BY GROUP HAVING LIMIT OFFSET DISTINCT COUNT SUM AVG MIN MAX ASC DESC CASE WHEN THEN ELSE END UNION ALL WITH'.split(' ');
function completionContext() {
  const input = $('#query'), before = input.value.slice(0, input.selectionStart);
  const match = before.match(/[A-Za-z_][A-Za-z0-9_]*$/); return match ? {prefix:match[0], start:input.selectionStart - match[0].length, end:input.selectionStart} : null;
}
function updateSuggestions() {
  const context = completionContext(), box = $('#suggestions');
  if (!context || context.prefix.length < 1) {box.hidden = true; return;}
  const candidates = [...keywords.map((name) => ({name, type:'keyword'})), ...state.tables.flatMap((t) => [{name:t.schema, type:'schema'}, {name:t.name, type:'table'}, ...t.columns.map((c) => ({name:c.name, type:'column'}))])];
  const seen = new Set();
  state.suggestions = candidates.filter((c) => c.name.toLowerCase().startsWith(context.prefix.toLowerCase()) && c.name.toLowerCase() !== context.prefix.toLowerCase() && !seen.has(c.name.toLowerCase()) && seen.add(c.name.toLowerCase())).slice(0, 9);
  state.suggestionIndex = 0; drawSuggestions();
}
function drawSuggestions() {
  const box = $('#suggestions'); box.hidden = !state.suggestions.length;
  box.innerHTML = state.suggestions.map((s, index) => `<button class="suggestion ${index === state.suggestionIndex ? 'active' : ''}" data-suggestion="${index}"><span>${escapeHTML(s.name)}</span><small>${s.type}</small></button>`).join('');
}
function acceptSuggestion(index) {
  const context = completionContext(), choice = state.suggestions[index]; if (!context || !choice) return;
  const input = $('#query'); input.setRangeText(choice.name, context.start, context.end, 'end'); $('#suggestions').hidden = true; input.focus();
}
document.addEventListener('click', (event) => {
  const table = event.target.closest('[data-table]'); if (table) openTable(Number(table.dataset.table));
  const nav = event.target.closest('[data-view]'); if (nav) setView(nav.dataset.view);
  const tab = event.target.closest('[data-tab]'); if (tab) activateTab(tab.dataset.tab);
  const row = event.target.closest('[data-row]'); if (row) inspectRow(Number(row.dataset.row));
  const suggestion = event.target.closest('[data-suggestion]'); if (suggestion) acceptSuggestion(Number(suggestion.dataset.suggestion));
  const exportButton = event.target.closest('[data-export]'); if (exportButton) exportResults(exportButton);
});
$('#table-data').addEventListener('keydown', (event) => {const row = event.target.closest('[data-row]'); if (row && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); inspectRow(Number(row.dataset.row));}});
$('#search').addEventListener('input', renderTables); $('#refresh').addEventListener('click', refresh); $('#open-sql').addEventListener('click', () => setView('sql')); $('#back').addEventListener('click', () => setView('overview'));
$('#table-query').addEventListener('click', () => {$('#query').value = `SELECT *\nFROM ${qualified(state.current)};`; setView('sql');});
$('#prev-page').addEventListener('click', () => {if (state.page > 0) {state.page--; loadRows();}}); $('#next-page').addEventListener('click', () => {state.page++; loadRows();});
$('#run-query').addEventListener('click', () => runQuery()); $('#query-prev').addEventListener('click', () => {if (state.queryPage > 0) runQuery(state.queryPage - 1, state.query);}); $('#query-next').addEventListener('click', () => {if (state.queryHasMore) runQuery(state.queryPage + 1, state.query);}); $('#close-dialog').addEventListener('click', () => $('#row-dialog').close());
$('#query').addEventListener('input', updateSuggestions);
$('#query').addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {event.preventDefault(); $('#suggestions').hidden = true; runQuery(); return;}
  if (!$('#suggestions').hidden && ['ArrowDown','ArrowUp','Tab','Enter','Escape'].includes(event.key)) {event.preventDefault(); if (event.key === 'Escape') $('#suggestions').hidden = true; else if (event.key === 'Tab' || event.key === 'Enter') acceptSuggestion(state.suggestionIndex); else {state.suggestionIndex = (state.suggestionIndex + (event.key === 'ArrowDown' ? 1 : -1) + state.suggestions.length) % state.suggestions.length; drawSuggestions();}}
});
fetch('/api/session').then((response) => response.json()).then((data) => {state.token = data.token; return refresh();}).catch(() => showNotice('Could not connect to the local explorer.'));
