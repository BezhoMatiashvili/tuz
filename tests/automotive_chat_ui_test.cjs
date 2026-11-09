// Conversation DOM contract; browser layout is verified separately.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.events={};this.value='';this._text='';}
  set value(value) {this._value=String(value);}
  get value() {return this._value;}
  set textContent(value) {this._text=String(value);this.children=[];}
  get textContent() {return this._text+this.children.map(n=>n.textContent).join('');}
  append(...nodes) {nodes.forEach(n=>{n.parent=this;});this.children.push(...nodes);}
  replaceChildren(...nodes) {this._text='';nodes.forEach(n=>{n.parent=this;});this.children=[...nodes];}
  remove() {if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}}
  addEventListener(event,fn) {(this.events[event] ||= []).push(fn);}
  async fire(event,extra={}) {for(const fn of this.events[event] || []) await fn({target:this,preventDefault(){},...extra});}
  querySelectorAll(tag) {return this.children.flatMap(n=>[...(n.tagName===tag?[n]:[]),...n.querySelectorAll(tag)]);}
  contains(n) {return this.children.includes(n);}
  focus() {this.focused=true;}
  requestSubmit() {return this.fire('submit');}
  scrollIntoView() {}
}
const html=fs.readFileSync('automotive/static/index.html','utf8');
const ids=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Node()]));
assert.equal(ids.size,[...html.matchAll(/id="/g)].length);
assert.match(html,/id="chat-result"[^>]+role="log"/);
assert.match(html,/id="direct-search" class="advanced direct-search"><summary>/);
const timers=[],calls=[];
const fixture={status:'COMPLETE',answer:'Here are catalog candidates.',note:"Some sources didn't answer, so the list may be incomplete.",quick_replies:['Front','Rear'],warnings:['SOURCE_FAILED:motorium','VIN_NOT_PROVIDED','NAME_MATCH_ONLY'],vehicle:{make:'Example',model:'Car',model_year:2013},cards:[{product:{source_product_id:'p1',product_name:'<script>injected=true</script>',brand:'Maker',product_code:'AP104/2',matched_codes:[{code:'AP104/2'}],last_observed_at:'2026-01-01'},match_source:'EXACT_CODE',fitment_status:'UNKNOWN',compatible:null,availability:'In the store: 16',availability_status:'IN_STORE',matched_via:['Shell: Helix Ultra 5W-40'],pack_litres:4}],requirements:{grades:['0W-40','5W-40'],capacity_l:6.5,engine_code:'M 276.850',missing:['oil_filter'],coverage:[{brand:'SHELL',grade:'5W-40',litres_store:64,litres_tuz11:0,enough_store:true,enough_with_tuz11:true}]},supplier_only:[{name:'Supplier pad',manufacturer:'Supplier',mpn:'S123'}],total:21,offset:0,next_offset:20};
let responder=async url=>url.endsWith('/conversations')?{conversation_id:'c1'}:url.endsWith('/messages')?{turn_id:'t1',status:'RUNNING'}:fixture;
const ctx=vm.createContext({document:{getElementById:id=>ids.get(id),createElement:tag=>new Node(tag)},URLSearchParams,console,setTimeout:fn=>timers.push(fn),fetch:async(url,options={})=>{
  calls.push({url,options});
  const data=url.endsWith('/status')?{inventory_configured:true,chat_configured:true,vin_configured:true}:await responder(url,options);
  return {ok:true,status:options.method==='DELETE'?204:200,json:async()=>data};
}});
vm.runInContext(fs.readFileSync('automotive/static/app.js','utf8'),ctx);
const field=id=>ids.get(id);
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
(async()=>{
  await flush();
  field('message').value='Find brakes for 1HGCM82633A004352';
  await field('chat-form').fire('submit');
  const sent=calls.find(c=>c.url.endsWith('/messages'));
  assert.equal(JSON.parse(sent.options.body).vin,'1HGCM82633A004352');
  assert.equal(JSON.parse(sent.options.body).language,'auto');
  assert.ok(calls.every(c=>!c.url.includes('1HGCM82633A004352')));
  assert.ok(calls.some(c=>/\/turns\/t1\?offset=0&limit=5$/.test(c.url)),'First page shows 5 parts');
  const transcript=field('chat-result');
  assert.match(transcript.textContent,/Supplier codes · no warehouse match/);
  assert.match(transcript.textContent,/Fitment unknown/);
  assert.match(transcript.textContent,/In the store: 16/);
  assert.doesNotMatch(transcript.textContent,/stock unconfirmed/);
  assert.match(transcript.textContent,/Chosen because: Shell: Helix Ultra 5W-40/);
  assert.match(transcript.textContent,/Pack size: 4 L/);
  assert.match(transcript.textContent,/SHELL 5W-40: 64 L — enough for a full change/);
  assert.match(transcript.textContent,/Some sources didn't answer, so the list may be incomplete\./);
  assert.doesNotMatch(transcript.textContent,/Price|Source not checked/,'One note; no unknown price line');
  assert.doesNotMatch(transcript.textContent,/NAME_MATCH_ONLY/);
  assert.doesNotMatch(transcript.textContent,/SOURCE_FAILED|VIN_NOT_PROVIDED|SOME_SOURCES_UNAVAILABLE/);
  assert.doesNotMatch(transcript.textContent,/typing/,'The typing bubble is gone once the reply is in');
  assert.match(transcript.textContent,/incomplete\.FrontRearSHELL/,'Quick replies sit under the reply text');
  assert.match(transcript.textContent,/2026-01-01/);
  assert.doesNotMatch(transcript.textContent,/1HGCM82633A004352/);
  assert.equal(transcript.querySelectorAll('script').length,0);
  assert.equal(ctx.injected,undefined);
  assert.match(field('selected-vehicle').textContent,/Example Car 2013/);
  responder=async(url)=>{assert.match(url,/offset=20/);return {...fixture,cards:[{...fixture.cards[0],product:{product_code:'PAGE2'}}],offset:20,next_offset:null};};
  await transcript.querySelectorAll('button').find(b=>/^Show more/.test(b.textContent)).fire('click');
  assert.match(transcript.textContent,/PAGE2/);
  responder=async url=>url.endsWith('/messages')?{turn_id:'t2',status:'RUNNING'}:fixture;
  field('message').value='უკანა ხუნდები';
  await field('chat-form').fire('submit');
  assert.equal(calls.filter(c=>c.url.endsWith('/conversations')).length,1,'Follow-up reuses conversation');
  assert.equal(JSON.parse(calls.filter(c=>c.url.endsWith('/messages')).at(-1).options.body).language,'auto');
  assert.equal(transcript.children.length,4,'Two exchanges remain in transcript');
  await field('new-chat').fire('click');
  await flush();
  assert.equal(transcript.textContent,'');
  assert.equal(field('vin').value,'');
  assert.ok(calls.some(c=>c.url.endsWith('/conversations/c1')&&c.options.method==='DELETE'));
  let release;
  responder=()=>new Promise(resolve=>{release=resolve;});
  field('message').value='Find AP104/2';
  const staleCreate=field('chat-form').fire('submit');
  await flush();
  await field('new-chat').fire('click');
  release({conversation_id:'obsolete'});
  await staleCreate;
  await flush();
  assert.equal(transcript.textContent,'');
  assert.ok(calls.some(c=>c.url.endsWith('/conversations/obsolete')&&c.options.method==='DELETE'));
  let gets=0;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'c2'}:url.endsWith('/messages')?{turn_id:'t3'}:(gets++===0?{status:'RUNNING',progress:[{label:'Warehouse',status:'RUNNING'}]}:fixture);
  field('message').value='Find AP104/2';
  const pending=field('chat-form').fire('submit');
  await flush();
  assert.match(field('chat-progress').textContent,/Looking that up/);
  assert.match(transcript.textContent,/Assistant is typing/);
  await field('message').fire('input');
  assert.equal(field('chat-send').disabled,true,'Editing next message preserves pending turn');
  await field('new-chat').fire('click');
  const before=calls.length;
  timers.shift()();
  await pending;
  assert.equal(calls.length,before,'Reset prevents stale polling');
  assert.equal(transcript.textContent,'');
  assert.equal(field('chat-send').disabled,false);
  // Cards found while the turn is still running appear in a live bubble that the final
  // reply replaces; polling starts quickly.
  await field('new-chat').fire('click');
  await flush();
  timers.length=0;
  let livePolls=0;
  const runningFixture={...fixture,status:'RUNNING',answer:''};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'c3'}:url.endsWith('/messages')?{turn_id:'t4'}:(livePolls++===0?runningFixture:fixture);
  field('message').value='Find AP104/2';
  const liveTurn=field('chat-form').fire('submit');
  await flush();
  assert.match(transcript.textContent,/Still checking — found so far/);
  assert.match(transcript.textContent,/In the store: 16/);
  assert.match(field('chat-progress').textContent,/Example Car 2013/);
  timers.shift()();
  await liveTurn;
  await flush();
  assert.doesNotMatch(transcript.textContent,/Still checking/);
  assert.match(transcript.textContent,/Here are catalog candidates/);
  assert.equal(transcript.textContent.split('In the store: 16').length,2,'The live copy is replaced, not duplicated');
  // A quick reply is sent as the customer's next message; Enter sends, Shift+Enter does not.
  responder=async url=>url.endsWith('/messages')?{turn_id:'t5',status:'RUNNING',language:'en'}:{...fixture,quick_replies:[]};
  const chip=transcript.querySelectorAll('button').find(b=>b.textContent==='Rear');
  await chip.fire('click');
  await flush();
  assert.equal(JSON.parse(calls.filter(c=>c.url.endsWith('/messages')).at(-1).options.body).message,'Rear');
  assert.equal(transcript.querySelectorAll('button').filter(b=>b.textContent==='Rear').length,0,'Old chips are cleared');
  const sentBefore=calls.filter(c=>c.url.endsWith('/messages')).length;
  field('message').value='front pads';
  await field('message').fire('keydown',{key:'Enter',shiftKey:true});
  assert.equal(calls.filter(c=>c.url.endsWith('/messages')).length,sentBefore);
  await field('message').fire('keydown',{key:'Enter',shiftKey:false});
  await flush();
  assert.equal(calls.filter(c=>c.url.endsWith('/messages')).length,sentBefore+1);
  console.log('PASS: messenger transcript, typing indicator, single note, quick replies, Enter to send, 5-first pagination, catalog/supplier labels, text-only rendering, VIN privacy, follow-up session reuse, reset and stale-create/poll invalidation.');
})().catch(error=>{console.error(error);process.exitCode=1;});
