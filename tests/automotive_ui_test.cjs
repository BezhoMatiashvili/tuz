// DOM contract checks with synthetic responses; this is not a real browser/layout E2E.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.events = {}; this.value = ''; this._text = ''; }
  set value(value) {this._value = String(value);}
  get value() {return this._value;}
  set textContent(value) {this._text = String(value); this.children = [];}
  get textContent() {return this._text + this.children.map(n => n.textContent).join('');}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this._text = ''; this.children = [...nodes];}
  addEventListener(event, fn) {(this.events[event] ||= []).push(fn);}
  async fire(event) {for (const fn of this.events[event] || []) await fn({target: this, preventDefault() {}});}
  querySelectorAll(tag) {return this.children.flatMap(n => [ ...(n.tagName === tag ? [n] : []), ...n.querySelectorAll(tag)]);}
  contains(n) {return this.children.includes(n);}
  scrollIntoView() {}
  remove() {}
}
const html = fs.readFileSync('automotive/static/index.html', 'utf8');
const ids = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Node()]));
assert.equal(ids.size, [...html.matchAll(/id="/g)].length, 'No duplicate element IDs');
for (const [id, node] of ids) if (id.endsWith('-form')) node.append(new Node('button'));
const timers = [];
const calls = [];
const vehicle = {make:'Example', model:'Car', model_year:2013, engine_code:null};
const part = {part_id:'fixture', name:'<script>injected=true</script>', main_code:'AP104/2', manufacturer:'Maker', status:'PROVISIONAL', compatible:null, warehouse_records:[], supplier_records:[{mpn:'AP104/2', oem_codes:['OE123'], manufacturer:'Maker', source_url:'https://motorium.ge/product.php?id=1', status:'PROVISIONAL', warnings:[]}], source_names:['motorium'], warnings:[]};
const snapshot = (status='COMPLETE', extra={}) => ({search_id:'a'.repeat(32), status, vehicle, sources:[{label:'Motorium',source_id:'motorium',status:status === 'RUNNING' ? 'RUNNING' : 'OK',count:1,total_queries:8,completed_queries:1}], items:[part], total:51, offset:0, next_offset:50, verified_count:0, candidate_count:51, warnings:[], maintenance:null, ...extra});
let responder = async () => snapshot();
const context = vm.createContext({document:{getElementById:id => ids.get(id), createElement:tag => new Node(tag)}, URLSearchParams, console, setTimeout:fn => {timers.push(fn);}, fetch:async (url, options={}) => {
  calls.push({url, options});
  const data = url.endsWith('/status') ? {inventory_configured:true,chat_configured:true,vin_configured:true} : await responder(url, options);
  return {ok:true, status:200, json:async () => data};
}});
vm.runInContext(fs.readFileSync('automotive/static/app.js', 'utf8'), context);
const flush = async () => {for(let i=0; i<15; i++) await Promise.resolve();};
const tick = async () => {assert.ok(timers.length, 'A progress poll was scheduled'); timers.shift()(); await flush();};
const fields = id => ids.get(id);
(async () => {
  await flush();
  fields('vin').value = '1HGCM82633A004352';
  responder = async url => url.endsWith('/vehicles/search') ? snapshot('RUNNING', {items:[]}) : snapshot();
  const running = fields('vin-form').fire('submit');
  await flush();
  assert.match(fields('unified-progress').textContent, /იძებნება/);
  assert.equal(calls.filter(c => c.url.includes('/vehicles/search')).length, 1);
  const post = calls.find(c => c.url.endsWith('/vehicles/search'));
  assert.equal(post.options.method, 'POST');
  assert.equal(JSON.parse(post.options.body).vin, '1HGCM82633A004352');
  assert.ok(!post.url.includes('1HGCM82633A004352'));
  await fields('message').fire('input'); // Editing chat must not cancel VIN federation.
  await tick();
  await running;
  assert.match(fields('unified-result').textContent, /AP104\/2/);
  assert.match(fields('unified-result').textContent, /<script>/);
  assert.equal(fields('unified-result').querySelectorAll('script').length, 0);
  assert.equal(context.injected, undefined);
  assert.match(fields('unified-result').textContent, /ჩვენს საწყობში არსებობა, ფასი და რაოდენობა უცნობია/);
  assert.equal(fields('make').value, 'Example');
  assert.equal(fields('vin-form').querySelectorAll('button')[0].disabled, false);
  responder = async url => {assert.ok(url.includes('offset=50')); return snapshot('COMPLETE', {offset:50,next_offset:null,items:[{...part,main_code:'PAGE2'}]});};
  await fields('unified-pages').querySelectorAll('button')[0].fire('click');
  assert.match(fields('unified-result').textContent, /PAGE2/);
  assert.match(fields('unified-pages').textContent, /51–51 \/ 51/);
  let release;
  responder = () => new Promise(resolve => {release=resolve;});
  const stale = fields('vin-form').fire('submit');
  await flush();
  fields('vin').value = 'NEW';
  await fields('vin').fire('input');
  release(snapshot());
  await stale;
  assert.equal(fields('unified-result').textContent, '');
  assert.equal(fields('make').value, '');
  responder = async () => snapshot('RUNNING');
  const pending = fields('vin-form').fire('submit');
  await flush();
  fields('make').value = 'Changed';
  await fields('make').fire('input');
  const count = calls.length;
  await tick();
  await pending;
  assert.equal(calls.length, count, 'No stale-context poll');
  assert.equal(fields('unified-result').textContent, '');
  assert.equal(fields('make').value, 'Changed');
  console.log('PASS: single VIN request, progress polling, chat independence, source/code rendering, text-only external content, pagination, VIN/configuration stale-response rejection. Synthetic DOM; no browser/layout or live-provider assertion.');
})().catch(error => {console.error(error); process.exitCode = 1;});
