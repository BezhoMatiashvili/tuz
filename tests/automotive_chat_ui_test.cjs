// Conversation DOM contract; browser layout is verified separately.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.events={};this.attributes={};this.value='';this._text='';this.scrollTop=0;this.clientHeight=0;this.style={};}
  set value(value) {this._value=String(value);}
  get value() {return this._value;}
  set textContent(value) {this._text=String(value);this.children=[];}
  get textContent() {return this._text+this.children.map(n=>n.textContent).join('');}
  append(...nodes) {nodes.forEach(n=>{n.parent=this;});this.children.push(...nodes);}
  replaceChildren(...nodes) {this._text='';nodes.forEach(n=>{n.parent=this;});this.children=[...nodes];}
  remove() {if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}}
  // A node placed right after or right before this one, as in a browser.
  after(node) {node.remove();node.parent=this.parent;this.parent.children.splice(this.parent.children.indexOf(this)+1,0,node);}
  before(node) {node.remove();node.parent=this.parent;this.parent.children.splice(this.parent.children.indexOf(this),0,node);}
  addEventListener(event,fn) {(this.events[event] ||= []).push(fn);}
  // Like a browser, a disabled button ignores clicks.
  async fire(event,extra={}) {if(event==='click'&&this.disabled)return;for(const fn of this.events[event] || []) await fn({target:this,preventDefault(){},...extra});}
  querySelectorAll(tag) {return this.children.flatMap(n=>[...(n.tagName===tag?[n]:[]),...n.querySelectorAll(tag)]);}
  contains(n) {return this.children.includes(n);}
  setAttribute(name,value) {this.attributes[name]=String(value);}
  getAttribute(name) {return Object.hasOwn(this.attributes,name)?this.attributes[name]:null;}
  removeAttribute(name) {delete this.attributes[name];}
  focus() {this.focused=true;doc.activeElement=this;}
  requestSubmit() {return this.fire('submit');}
  scrollIntoView() {}
  // Layout stand-in: each child of a node is 100 px tall, unless a test sets the figure itself.
  get offsetTop() {return this._offsetTop ?? (this.parent?this.parent.children.indexOf(this)*100:0);}
  set offsetTop(value) {this._offsetTop=value;}
  get scrollHeight() {return this._scrollHeight ?? this.children.length*100;}
  set scrollHeight(value) {this._scrollHeight=value;}
}
const html=fs.readFileSync('automotive/static/index.html','utf8');
const ids=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Node()]));
assert.equal(ids.size,[...html.matchAll(/id="/g)].length);
assert.match(html,/id="chat-result"[^>]+role="log"/);
assert.match(html,/id="direct-search" class="advanced direct-search"><summary>/);
const timers=[],calls=[];
const fixture={status:'COMPLETE',answer:'Here are catalog candidates.',note:"Some sources didn't answer, so the list may be incomplete.",quick_replies:['Front','Rear'],warnings:['SOURCE_FAILED:motorium','VIN_NOT_PROVIDED','NAME_MATCH_ONLY'],vehicle:{make:'Example',model:'Car',model_year:2013},cards:[{product:{source_product_id:'p1',product_name:'<script>injected=true</script>',brand:'Maker',product_code:'AP104/2',matched_codes:[{code:'AP104/2'}],last_observed_at:'2026-01-01'},match_source:'EXACT_CODE',fitment_status:'UNKNOWN',compatible:null,availability:'In the store: 16',availability_status:'IN_STORE',matched_via:['Shell: Helix Ultra 5W-40'],pack_litres:4}],requirements:{grades:['0W-40','5W-40'],capacity_l:6.5,engine_code:'M 276.850',missing:['oil_filter'],coverage:[{brand:'SHELL',grade:'5W-40',litres_store:64,litres_tuz11:0,enough_store:true,enough_with_tuz11:true}]},supplier_only:[{name:'Supplier pad',manufacturer:'Supplier',mpn:'S123'}],total:21,offset:0,next_offset:20};
let responder=async url=>url.endsWith('/conversations')?{conversation_id:'c1'}:url.endsWith('/messages')?{turn_id:'t1',status:'RUNNING'}:fixture;
const doc={activeElement:null,getElementById:id=>ids.get(id),createElement:tag=>new Node(tag)};
const ctx=vm.createContext({document:doc,URLSearchParams,console,setTimeout:fn=>timers.push(fn),fetch:async(url,options={})=>{
  calls.push({url,options});
  const data=url.endsWith('/status')?{inventory_configured:true,chat_configured:true,vin_configured:true}:await responder(url,options);
  // A responder that answers {httpStatus} stands for a request the server refused.
  if(data&&data.httpStatus) return {ok:false,status:data.httpStatus,json:async()=>({})};
  return {ok:true,status:options.method==='DELETE'?204:200,json:async()=>data};
}});
vm.runInContext(fs.readFileSync('automotive/static/app.js','utf8'),ctx);
const field=id=>ids.get(id);
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
// Small readers over the mock tree: nodes by class, sent message bodies, a button group's state.
const all=(root,test)=>root.children.flatMap(n=>[...(test(n)?[n]:[]),...all(n,test)]);
const withClass=(root,name)=>all(root,n=>String(n.className||'').split(' ').includes(name));
const posts=()=>calls.filter(c=>c.url.endsWith('/messages')).map(c=>JSON.parse(c.options.body));
const state=group=>group.children.map(b=>[b.disabled===true,b.getAttribute('aria-pressed')]);
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
  // A turn without the new keys draws what it always drew: one answer group under the note,
  // no group header, amount, fit label, next steps or retry, and nothing sent besides the text.
  const firstReply=transcript.children.at(-1);
  assert.deepEqual(firstReply.children.map(n=>n.className),['message-text','note','quick-replies','requirement','chat-cards','actions chat-pages','supplier-only']);
  assert.deepEqual(withClass(firstReply,'chat-part')[0].children.map(n=>n.tagName),['h4','p','p','p','p','details']);
  for(const name of ['chat-group','price','fit','next-steps','retry','failed']) assert.equal(withClass(transcript,name).length,0,name);
  assert.equal('choice' in JSON.parse(sent.options.body),false);
  const firstAnswers=withClass(firstReply,'quick-replies')[0];
  assert.deepEqual([firstAnswers.getAttribute('role'),firstAnswers.getAttribute('aria-label')],['group','Choose an answer']);
  assert.doesNotMatch(transcript.textContent,/Choose an answer/,'The group is named by an attribute, not by a text node');
  assert.deepEqual(state(firstAnswers),[[false,null],[false,null]]);
  // The car line under the progress line is built from the turn's vehicle.
  assert.equal(field('chat-car').hidden,false);
  assert.equal(field('chat-car-label').textContent,'Example Car 2013');
  const findings =vm.runInContext(`researchSection({claims:[{text:'Supplier assertion <script>evil</script>'}], citations:[{url:'https://www.mercedes-benz.com/catalog',title:'Manufacturer catalog'},{url:'javascript:alert(1)',title:'malicious'}]}, 'en')`,ctx);
  assert.match(findings.textContent,/Supplier assertion/);
  assert.equal(findings.querySelectorAll('script').length,0);
  assert.equal(findings.querySelectorAll('a').length,1);
  assert.equal(findings.querySelectorAll('a')[0].href,'https://www.mercedes-benz.com/catalog');
  const sourceClaims=vm.runInContext(`researchSection({claims:[{statement:'Quoted manufacturer statement',supporting_excerpt:'Manufacturer excerpt'},{kind:'grades',value:['0W-40'],sources:['Shell']},{kind:'capacity_l',value:6.5,sources:['Shell']},{kind:'engine_code',value:'M276.850',sources:['Shell']}],citations:[{source_url:'https://www.shell.com/catalog',source_title:'Shell catalog'}]}, 'en')`,ctx);
  assert.match(sourceClaims.textContent,/Quoted manufacturer statement/);
  assert.match(sourceClaims.textContent,/Manufacturer excerpt/);
  assert.match(sourceClaims.textContent,/Source reports: Oil grade 0W-40/);
  assert.match(sourceClaims.textContent,/Source reports: Oil capacity \(L\) 6.5/);
  assert.match(sourceClaims.textContent,/Source reports: Engine code M276\.850/);
  assert.match(sourceClaims.textContent,/Source assertion; vehicle applicability unconfirmed/);
  assert.doesNotMatch(sourceClaims.textContent,/Verified|Approved/);
  assert.equal(sourceClaims.querySelectorAll('a')[0].textContent,'Shell catalog');
  const variants=vm.runInContext(`resolutionSection({resolution_candidates:[{label:'E 400 petrol'}]}, 'en')`,ctx);
  assert.match(variants.textContent,/E 400 petrol/);
  assert.match(variants.textContent,/unconfirmed/i);
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
  assert.deepEqual(state(firstAnswers),[[true,null],[true,null]],'A typed answer closes the open group with none marked');
  await field('new-chat').fire('click');
  await flush();
  assert.equal(transcript.textContent,'');
  assert.equal(field('chat-car').hidden,true,'New chat clears the car line');
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
  const runningFixture={...fixture,status:'RUNNING',answer:'',claims:[{statement:'Progressive source assertion'}]};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'c3'}:url.endsWith('/messages')?{turn_id:'t4'}:(livePolls++===0?runningFixture:fixture);
  field('message').value='Find AP104/2';
  const liveTurn=field('chat-form').fire('submit');
  await flush();
  assert.match(transcript.textContent,/Still checking — found so far/);
  assert.match(transcript.textContent,/In the store: 16/);
  assert.match(transcript.textContent,/Progressive source assertion/);
  assert.match(field('chat-progress').textContent,/Example Car 2013/);
  timers.shift()();
  await liveTurn;
  await flush();
  assert.doesNotMatch(transcript.textContent,/Still checking/);
  assert.match(transcript.textContent,/Here are catalog candidates/);
  assert.equal(transcript.textContent.split('In the store: 16').length,2,'The live copy is replaced, not duplicated');
  // A tapped answer is sent as the customer's next message. Its group stays where it is, every
  // button disabled and the chosen one pressed, and a draft in the box is left alone.
  // Enter sends, Shift+Enter does not.
  responder=async url=>url.endsWith('/messages')?{turn_id:'t5',status:'RUNNING',language:'en'}:{...fixture,quick_replies:[]};
  const chip=transcript.querySelectorAll('button').find(b=>b.textContent==='Rear');
  const tappedGroup=chip.parent;
  field('message').value='half-typed note';
  await chip.fire('click');
  await flush();
  assert.equal(posts().at(-1).message,'Rear');
  assert.equal('choice' in posts().at(-1),false,'Choices from a server without the question key send text only');
  assert.equal(field('message').value,'half-typed note','A tap leaves the draft in the box');
  assert.ok(tappedGroup.parent&&tappedGroup.parent.children.includes(tappedGroup),'The answered group stays in its reply');
  assert.deepEqual(state(tappedGroup),[[true,null],[true,'true']],'Every button disabled, the chosen one pressed');
  const sentBefore=calls.filter(c=>c.url.endsWith('/messages')).length;
  field('message').value='front pads';
  await field('message').fire('keydown',{key:'Enter',shiftKey:true});
  assert.equal(calls.filter(c=>c.url.endsWith('/messages')).length,sentBefore);
  await field('message').fire('keydown',{key:'Enter',shiftKey:false});
  await flush();
  assert.equal(calls.filter(c=>c.url.endsWith('/messages')).length,sentBefore+1);
  await field('new-chat').fire('click');
  timers.length=0;
  let sourcePolls=0;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'source-only'}:url.endsWith('/messages')?{turn_id:'sources'}:(sourcePolls++===0?{status:'RUNNING',cards:[],claims:[{statement:'Source-only finding while inventory is pending'}]}:{...fixture,quick_replies:[]});
  field('message').value='Check the source';
  const sourceTurn=field('chat-form').fire('submit');
  await flush();
  assert.match(transcript.textContent,/Source-only finding while inventory is pending/);
  assert.doesNotMatch(transcript.textContent,/In the store: 16/);
  timers.shift()();
  await sourceTurn;
  assert.doesNotMatch(transcript.textContent,/Source-only finding while inventory is pending/,'Final response replaces progressive source findings');
  // The new turn keys: a question with answer buttons, grouped and priced cards, next steps.
  await field('new-chat').fire('click');
  timers.length=0;
  const base=fixture.cards[0];
  const sharedWarning='Check the disc size before selling.';
  const modern={...fixture,language:'ka',answer:'Toyota Camry 2015: წინა თუ უკანა?',vehicle:{},car_label:'Toyota Camry 2015',
    question:{options:[{label:'წინა',value:'წინა ხუნდები'},{label:'უკანა',value:'უკანა ხუნდები'}]},
    suggestions:[{label:'ზეთის ფილტრიც',value:'ზეთის ფილტრი'}],quick_replies:['წინა','უკანა'],
    groups:[{id:'g1',label:'წინა ხუნდები',count:2},{id:'g2',label:'უკანა ხუნდები',count:1}],
    cards:[
      {...base,product:{...base.product,product_name:'Front pad A',price:45.5},group:'g1',confidence:'confirmed',reason:'OE 04465-33450 is on the row',warnings:[sharedWarning]},
      {...base,product:{...base.product,product_name:'Front pad B'},group:'g1',confidence:'listed',warnings:[sharedWarning]},
      {...base,product:{...base.product,product_name:'Rear pad'},group:'g2',confidence:'check',warnings:[sharedWarning]}],
    total:3,next_offset:null};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'m1'}:url.endsWith('/messages')?{turn_id:'m1',language:'ka'}:modern;
  field('message').value='Camry 2015 ხუნდები';
  await field('chat-form').fire('submit');
  const asked=transcript.children.at(-1);
  assert.deepEqual(asked.children.map(n=>n.className),['message-text','note','quick-replies','requirement','chat-cards','actions chat-pages','supplier-only','quick-replies next-steps'],'Answers sit under the note; next steps close the reply');
  const answerGroup=asked.children[2],nextGroup=asked.children.at(-1);
  assert.deepEqual([answerGroup.getAttribute('role'),answerGroup.getAttribute('aria-label')],['group','აირჩიეთ პასუხი']);
  assert.deepEqual([nextGroup.getAttribute('role'),nextGroup.getAttribute('aria-label')],['group','შემდეგი ნაბიჯი']);
  assert.deepEqual(answerGroup.children.map(b=>b.textContent),['წინა','უკანა']);
  assert.equal(transcript.querySelectorAll('button').filter(b=>b.textContent==='წინა').length,1,'quick_replies are not drawn again beside the question key');
  assert.doesNotMatch(transcript.textContent,/აირჩიეთ პასუხი|შემდეგი ნაბიჯი/,'Group names are attributes');
  // Cards: one header per group, the amount, the fit label with its reason, one shared warning.
  const cards=asked.children[4];
  // The two front pads rest on different grounds, so each card says its own; the one rear pad's
  // ground is its whole group's and is said under the header.
  assert.deepEqual(cards.children.map(n=>n.className==='chat-group'?n.textContent:n.className),['წინა ხუნდები 2','chat-part','chat-part','უკანა ხუნდები 1','chat-group-fit','chat-part']);
  assert.deepEqual(withClass(cards,'part-fit').map(n=>n.children.map(p=>p.className)),[['fit confirmed','muted reason'],['fit listed'],[]]);
  assert.deepEqual(cards.children[4].children.map(p=>p.className+': '+p.textContent),['fit check: გადასამოწმებელია']);
  // The side panel names the car the chat understood, marked as read from the conversation.
  assert.equal(field('selected-vehicle').textContent,'Toyota Camry 2015საუბრიდან');
  // The starter row and the VIN hint have made room for the transcript.
  assert.deepEqual([field('suggested-prompts').hidden,field('chat-notice').hidden],[true,true]);
  assert.deepEqual(withClass(cards,'price').map(n=>n.textContent),['45.50 ₾'],'Only a known price is shown');
  assert.doesNotMatch(transcript.textContent,/Price|ფასი/);
  assert.deepEqual(withClass(cards,'fit').map(n=>n.textContent),['ნაწილის ნომრით ემთხვევა','კატალოგში ამ მოდელსა და წელზეა','გადასამოწმებელია']);
  assert.match(cards.children[1].textContent,/ნაწილის ნომრით ემთხვევაOE 04465-33450 is on the row/,'The reason sits under the fit label');
  assert.equal(transcript.textContent.split(sharedWarning).length,2,'A warning shared by three cards is said once');
  assert.doesNotMatch(cards.textContent,/შესაბამისობა უცნობია/,'No "fitment unknown" badge under a fit label');
  // The car line shows the engine's own label; "Change" only points at the composer.
  assert.equal(field('chat-car').hidden,false);
  assert.equal(field('chat-car-label').textContent,'Toyota Camry 2015');
  assert.equal(field('chat-car-change').textContent,'შეცვლა');
  const quietCalls=calls.length;
  await field('chat-car-change').fire('click');
  assert.equal(doc.activeElement,field('message'));
  assert.equal(field('message').placeholder,'ჩაწერეთ სხვა მანქანა ან VIN');
  assert.equal(calls.length,quietCalls,'Change sends nothing');
  assert.equal(transcript.children.length,2,'Change resets nothing');
  // A tapped answer sends its value and its 1-based number; the bubble shows what was tapped.
  responder=async url=>url.endsWith('/messages')?{turn_id:'m2',language:'ka'}:{...modern,question:null,cards:[]};
  field('message').value='დაუმთავრებელი ტექსტი';
  await answerGroup.children[1].fire('click');
  assert.deepEqual([posts().at(-1).message,posts().at(-1).choice],['უკანა ხუნდები',2]);
  assert.equal(field('message').value,'დაუმთავრებელი ტექსტი','A tap leaves the draft in the box');
  assert.equal(transcript.children.at(-2).textContent,'უკანა');
  assert.deepEqual(state(answerGroup),[[true,null],[true,'true']]);
  assert.equal(asked.children.includes(answerGroup),true,'Nothing is removed from the answered question');
  assert.equal(asked.children.includes(nextGroup),false,'Next steps go with the next message');
  assert.equal(doc.activeElement,field('message'),'A pointer tap moves no focus');
  assert.notEqual(field('message').placeholder,'ჩაწერეთ სხვა მანქანა ან VIN','The car hint lasts for one message');
  // Typed text and a tapped next step carry no option number.
  field('message').value='ტექსტით';
  await field('chat-form').fire('submit');
  assert.equal('choice' in posts().at(-1),false);
  const steps=transcript.querySelectorAll('button').filter(b=>b.textContent==='ზეთის ფილტრიც');
  assert.equal(steps.length,1,'Only the last reply keeps its next steps');
  await steps[0].fire('click');
  assert.deepEqual([posts().at(-1).message,'choice' in posts().at(-1)],['ზეთის ფილტრი',false]);
  // Answering from the keyboard: focus waits on the transcript, never on a disabled button,
  // and goes to the first answer of the next question.
  responder=async url=>url.endsWith('/messages')?{turn_id:'m5',language:'ka'}:modern;
  field('message').value='ხუნდები';
  await field('chat-form').fire('submit');
  const keyed=withClass(transcript.children.at(-1),'quick-replies')[0];
  keyed.children[0].focus();
  let focusWhileWaiting=null;
  responder=async url=>{if(!url.endsWith('/messages'))return modern;focusWhileWaiting=doc.activeElement;return {turn_id:'m6',language:'ka'};};
  await keyed.children[0].fire('click',{detail:0});
  assert.equal(focusWhileWaiting,transcript);
  assert.equal(doc.activeElement,withClass(transcript.children.at(-1),'quick-replies')[0].children[0]);
  // A failed send: the open group takes answers again; "Try again" repeats the same message
  // without a second bubble, and with the option number the server never received.
  await field('new-chat').fire('click');
  timers.length=0;
  const english={...modern,language:'en',answer:'Front or rear?',question:{options:[{label:'Front',value:'Front pads'},{label:'Rear',value:'Rear pads'}]},suggestions:[],quick_replies:[],cards:[],groups:[]};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'f1'}:url.endsWith('/messages')?{turn_id:'f1',language:'en'}:english;
  field('message').value='Camry 2015 pads';
  await field('chat-form').fire('submit');
  const open=withClass(transcript,'quick-replies')[0];
  responder=async url=>url.endsWith('/messages')?{httpStatus:503}:english;
  await open.children[0].fire('click');
  const failure=transcript.children.at(-1);
  assert.equal(failure.className,'chat-message assistant failed');
  assert.deepEqual(state(open),[[false,null],[false,null]],'A failed send reopens the group with none marked');
  assert.equal(withClass(transcript,'user').length,2);
  assert.equal(field('chat-send').disabled,false);
  responder=async url=>url.endsWith('/messages')?{turn_id:'f2',language:'en'}:{...english,answer:'Here they are.',question:null};
  await failure.querySelectorAll('button').find(b=>b.textContent==='Try again').fire('click');
  assert.deepEqual([posts().at(-1).message,posts().at(-1).choice],['Front pads',1]);
  assert.equal(withClass(transcript,'user').length,2,'The retry adds no second user bubble');
  assert.equal(transcript.children.includes(failure),false,'The failure makes way for the reply');
  assert.deepEqual(state(open),[[true,'true'],[true,null]]);
  assert.match(transcript.children.at(-1).textContent,/Here they are\./);
  // When the server took the message and only its reply was lost, its question may be gone:
  // the repeat, and later taps on that group, send the text alone.
  responder=async url=>url.endsWith('/messages')?{turn_id:'f3',language:'en'}:english;
  field('message').value='and the rear';
  await field('chat-form').fire('submit');
  const lost=withClass(transcript.children.at(-1),'quick-replies')[0];
  responder=async url=>url.endsWith('/messages')?{turn_id:'f4',language:'en'}:{httpStatus:503};
  await lost.children[1].fire('click');
  assert.equal(posts().at(-1).choice,2);
  await transcript.children.at(-1).querySelectorAll('button').find(b=>b.textContent==='Try again').fire('click');
  assert.deepEqual([posts().at(-1).message,'choice' in posts().at(-1)],['Rear pads',false]);
  assert.equal(withClass(transcript,'retry').length,1,'One offer to retry at a time');
  // A turn the server itself reports as failed gets the same offer, and the group reopens.
  responder=async url=>url.endsWith('/messages')?{turn_id:'f5',language:'en'}:{...english,status:'FAILED',answer:'The service is temporarily unavailable. Please try again.',question:null};
  await lost.children[0].fire('click');
  assert.deepEqual([posts().at(-1).message,'choice' in posts().at(-1)],['Front pads',false]);
  const serverFailed=transcript.children.at(-1);
  assert.equal(serverFailed.className,'chat-message assistant failed');
  assert.match(serverFailed.textContent,/temporarily unavailable/);
  assert.equal(withClass(transcript,'retry').length,1,'An older failure keeps its text but not its button');
  assert.equal(withClass(serverFailed,'retry').length,1);
  assert.deepEqual(state(lost),[[false,null],[false,null]]);
  // When the provider's account refuses every request, the shop is told why under the reply.
  responder=async url=>url.endsWith('/messages')?{turn_id:'f6',language:'en'}:{...english,status:'FAILED',answer:'Sorry, the assistant is not available right now.',question:null,suggestions:[],warnings:['PROVIDER_BILLING']};
  field('message').value='Front pads once more';
  await field('chat-form').fire('submit');
  const refused=transcript.children.at(-1);
  assert.equal(refused.className,'chat-message assistant failed');
  assert.match(refused.textContent,/not available right now/);
  assert.match(refused.textContent,/out of credit\. The chat cannot answer until credit is added\./);
  assert.doesNotMatch(refused.textContent,/PROVIDER_BILLING/);
  // A conversation the server has lost cannot take the repeat: "Try again" starts a new one.
  responder=async url=>url.endsWith('/messages')?{httpStatus:404}:english;
  field('message').value='still there?';
  await field('chat-form').fire('submit');
  const created=calls.filter(c=>c.url.endsWith('/conversations')).length;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'f9'}:url.endsWith('/messages')?{turn_id:'f9',language:'en'}:{...english,question:null};
  await transcript.children.at(-1).querySelectorAll('button').find(b=>b.textContent==='Try again').fire('click');
  assert.equal(calls.filter(c=>c.url.endsWith('/conversations')).length,created+1);
  assert.match(calls.filter(c=>c.url.endsWith('/messages')).at(-1).url,/\/conversations\/f9\/messages$/);
  assert.equal(posts().at(-1).message,'still there?');
  // The transcript scrolls itself. A tall reply is shown from its top, a short one lands at
  // the end, and a clerk who scrolled back to read is not moved until they send again.
  await field('new-chat').fire('click');
  timers.length=0;
  transcript.clientHeight=250;
  let phase=0;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'s1'}:url.endsWith('/messages')?(phase=0,{turn_id:'s1'}):(phase++===0?{status:'RUNNING'}:{...fixture,quick_replies:[]});
  const say=async(text,whileWaiting)=>{field('message').value=text;const turn=field('chat-form').fire('submit');await flush();whileWaiting();timers.shift()();await turn;};
  await say('one',()=>{transcript.scrollHeight=900;});
  assert.equal(transcript.scrollTop,92,'The top of a tall reply sits just under the top edge');
  await say('two',()=>{transcript.scrollHeight=undefined;});
  assert.equal(transcript.scrollTop,150,'A short reply lands at the end');
  await say('three',()=>{transcript.scrollTop=20;transcript.scrollHeight=2000;});
  assert.equal(transcript.scrollTop,20,'A clerk reading older messages is left where they are');
  transcript.scrollHeight=undefined;
  await say('four',()=>{assert.equal(transcript.scrollTop,550,'Their own message always shows');});
  // Results that arrive while the turn is still running are shown from their top, and the
  // final reply takes their place without moving the view.
  responder=async url=>url.endsWith('/messages')?(phase=0,{turn_id:'s2'}):(phase++===0?(transcript.scrollHeight=3000,runningFixture):{...fixture,quick_replies:[]});
  await say('five',()=>{assert.equal(transcript.scrollTop,892,'Live results are shown from their top');});
  assert.equal(transcript.scrollTop,892,'The final reply takes their place without moving the view');
  // A failure that arrives while the clerk reads older messages does not move them either.
  responder=async url=>url.endsWith('/messages')?(phase=0,{turn_id:'s3'}):(phase++===0?{status:'RUNNING'}:{httpStatus:503});
  await say('six',()=>{transcript.scrollTop=40;});
  assert.equal(transcript.children.at(-1).className,'chat-message assistant failed');
  assert.equal(transcript.scrollTop,40,'A failure does not move a clerk who scrolled back');
  // A server that sends the new keys empty beside its old lists still gets its answer buttons.
  await field('new-chat').fire('click');
  timers.length=0;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'h1'}:url.endsWith('/messages')?{turn_id:'h1',language:'en'}:{...fixture,question:null,suggestions:[]};
  field('message').value='Find AP104/2';
  await field('chat-form').fire('submit');
  assert.deepEqual(withClass(transcript,'quick-replies').map(group=>group.children.map(b=>b.textContent)),[['Front','Rear']]);
  responder=async url=>url.endsWith('/messages')?{turn_id:'h2',language:'en'}:{...fixture,quick_replies:[],question:null,suggestions:[]};
  await withClass(transcript,'quick-replies')[0].children[0].fire('click');
  assert.deepEqual([posts().at(-1).message,'choice' in posts().at(-1)],['Front',false]);
  // A turn as the assistant engine sends it (found on a live walk): the progress line names the
  // search, the reason it repeats in matched_via is said once, the row's vehicle text and the
  // quoted evidence are shown, the oil figures and the old-stock warning are drawn, and the raw
  // source name "assistant" never reaches the screen.
  await field('new-chat').fire('click');
  timers.length=0;
  const engineCard={product:{source_product_id:'e1',product_name:'ზეთის ფილტრი',product_model:'MB C CLS E GLK M276 2011&gt;',brand:'MANN',product_code:'2060000993',price:50,currency:'GEL',matched_codes:[],last_observed_at:'2026-11-09'},match_source:'assistant',fitment_status:'UNKNOWN',compatible:null,availability:'მაღაზიაშია: 1 ც.',availability_status:'IN_STORE',basis:'check',matched_via:['ძრავი შეამოწმოს მაღაზიამ'],warnings:[],group:'g1',reason:'ძრავი შეამოწმოს მაღაზიამ',confidence:'check',evidence:'M276'};
  const searched=[{stage:'search_catalog',status:'OK',label:'C207, W207 · ზეთის ფილტრი'},{stage:'oil_guide',status:'OK',label:'oil · Mercedes-Benz E-Class 2015'}];
  const engineTurn={status:'COMPLETE',engine_status:'OK',act:'results',outcome:'offered',language:'ka',answer:'E400-ის ზეთის ფილტრი ნაპოვნია.',note:'',vehicle:{},car_label:'Mercedes E400 Coupe 2015',question:null,suggestions:[],quick_replies:[],groups:[{id:'g1',label:'ზეთის ფილტრი',count:1}],cards:[engineCard],supplier_only:[],warnings:['STOCK_MAY_BE_STALE'],requirements:{grades:['0W-40','5W-40'],capacity_l:6.5,engine_code:'M 276.850',sources:['Shell']},progress:searched,total:1,offset:0,next_offset:null};
  const engineReplies=[{status:'RUNNING',language:'ka',progress:[{...searched[0],status:'RUNNING'}]},{status:'RUNNING',language:'ka',progress:searched},engineTurn];
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'e1'}:url.endsWith('/messages')?{turn_id:'e1',language:'ka'}:(engineReplies.length>1?engineReplies.shift():engineReplies[0]);
  field('message').value='E400 კუპე 2015 ზეთის შეცვლა';
  const engineRun=field('chat-form').fire('submit');
  await flush();
  assert.equal(field('chat-progress').textContent,'ვეძებ: C207, W207 · ზეთის ფილტრი…','While a search runs the line names it');
  timers.shift()();
  await flush();
  assert.equal(field('chat-progress').textContent,'ვარჩევ ნაპოვნიდან: C207, W207 · ზეთის ფილტრი | oil · Mercedes-Benz E-Class 2015…','Once the rows are in, the model is choosing');
  timers.shift()();
  await engineRun;
  const engineReply=transcript.children.at(-1);
  assert.deepEqual(engineReply.children.map(n=>n.className),['message-text','note','requirement','chat-cards','actions chat-pages']);
  assert.equal(withClass(engineReply,'note')[0].textContent,'მარაგი სინქრონიზაციის ბოლო მონაცემებითაა; გადაამოწმეთ მაღაზიაში.','The engine sent the warning without a note: it is said here');
  assert.equal(withClass(engineReply,'requirement')[0].textContent,'ზეთი ამ ძრავისთვის (Shell): 0W-40, 5W-40 · 6.5 L · ძრავი M 276.850');
  const enginePart=withClass(engineReply,'chat-part')[0];
  // One part in its group: its label and reason are the group's line, and its own box is empty.
  assert.deepEqual(enginePart.children.map(n=>n.className),['part-head','code','muted fits','availability IN_STORE','part-fit','part-details']);
  assert.equal(withClass(enginePart,'part-fit')[0].children.length,0);
  assert.deepEqual(withClass(engineReply,'chat-group-fit')[0].children.map(p=>p.className+': '+p.textContent),['fit check: გადასამოწმებელია','muted reason: ძრავი შეამოწმოს მაღაზიამ']);
  assert.equal(field('selected-vehicle').textContent,'Mercedes E400 Coupe 2015საუბრიდან','The panel no longer says "no car" beside a chat about one');
  assert.equal(withClass(enginePart,'fits')[0].textContent,'MB C CLS E GLK M276 2011>','The vehicle text of the row, its entity written out');
  assert.equal(engineReply.textContent.split('ძრავი შეამოწმოს მაღაზიამ').length,2,'The reason is said once');
  assert.match(enginePart.textContent,/კატალოგის ჩანაწერში წერია: M276/);
  assert.match(enginePart.textContent,/ასისტენტმა შეარჩია კატალოგის ჩანაწერით/);
  assert.doesNotMatch(enginePart.textContent,/assistant|დამთხვეული კოდები|შერჩეულია/,'No raw source name, no empty code line, no second copy of the reason');
  assert.equal(field('chat-car-label').textContent,'Mercedes E400 Coupe 2015');
  // An older engine's turn carries the same warning and requirement keys without an outcome:
  // its own sentence and note already say them, so nothing is added.
  assert.equal(withClass(firstReply,'requirement').length,1);
  // Why the parts of a group are offered is said once under the group's header while every part
  // in it rests on the same ground, and "Show more" keeps that right. "spec" is the fourth level:
  // a product no car is named on, chosen by its specification.
  await field('new-chat').fire('click');
  timers.length=0;
  assert.deepEqual([field('suggested-prompts').hidden,field('chat-notice').hidden],[false,false],'A new chat brings the starters and the hint back');
  assert.equal(field('selected-vehicle').textContent,'ავტომობილი ჯერ არ არის არჩეული.','And forgets the car the last chat was about');
  const oilReason='0W-20: სიბლანტე, რომელსაც ზეთის შემრჩევი ასახელებს';
  const part=(name,group,confidence,reason='')=>({...engineCard,product:{...engineCard.product,product_name:name,source_product_id:name},group,confidence,reason,matched_via:reason?[reason]:[],evidence:''});
  const oilTurn={...engineTurn,warnings:[],requirements:{},progress:[],answer:'ფილტრი და ზეთი ნაპოვნია.',car_label:'Toyota Camry 2015',
    groups:[{id:'g1',label:'ზეთის ფილტრი',count:2},{id:'g2',label:'ძრავის ზეთი',count:5}],
    cards:[part('Filter A','g1','listed'),part('Filter B','g1','listed'),part('Oil A','g2','spec',oilReason)],total:7,next_offset:3};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'o1'}:url.endsWith('/messages')?{turn_id:'o1',language:'ka'}:oilTurn;
  field('message').value='ზეთის შეცვლა';
  await field('chat-form').fire('submit');
  const oilCards=withClass(transcript.children.at(-1),'chat-cards')[0];
  const outline=()=>oilCards.children.map(n=>n.className==='chat-group'?n.textContent:n.className);
  const groupLines=()=>withClass(oilCards,'chat-group-fit').map(n=>n.children.map(p=>p.className+': '+p.textContent));
  const ownLines=()=>withClass(oilCards,'part-fit').map(n=>n.children.map(p=>p.className));
  assert.deepEqual(outline(),['ზეთის ფილტრი 2','chat-group-fit','chat-part','chat-part','ძრავის ზეთი 5','chat-group-fit','chat-part']);
  assert.deepEqual(groupLines(),[['fit listed: კატალოგში ამ მოდელსა და წელზეა'],['fit spec: შერჩეულია სპეციფიკაციით','muted reason: '+oilReason]]);
  assert.deepEqual(ownLines(),[[],[],[]],'No card repeats its group');
  assert.equal(oilCards.textContent.split('კატალოგში ამ მოდელსა და წელზეა').length,2,'Said once for two filters, not on each');
  const moreParts=()=>transcript.querySelectorAll('button').find(b=>/^კიდევ ნახვა/.test(b.textContent));
  // Another oil on the same ground: the line under the header still speaks for all of them,
  // and it is not written again (the transcript announces every addition).
  const saidOnce=withClass(oilCards,'chat-group-fit')[1].children[0];
  responder=async url=>{assert.match(url,/offset=3/);return {...oilTurn,cards:[part('Oil B','g2','spec',oilReason)],offset:3,next_offset:4};};
  await moreParts().fire('click');
  assert.deepEqual(outline().slice(4),['ძრავის ზეთი 5','chat-group-fit','chat-part','chat-part']);
  assert.equal(withClass(oilCards,'chat-group-fit')[1].children[0],saidOnce);
  assert.deepEqual(ownLines(),[[],[],[],[]]);
  // One that rests on another ground: the shared line goes and each oil says its own, the
  // earlier ones too; the filters keep theirs.
  responder=async url=>{assert.match(url,/offset=4/);return {...oilTurn,cards:[part('Oil C','g2','check','სიბლანტე გადაამოწმეთ')],offset:4,next_offset:5};};
  await moreParts().fire('click');
  assert.deepEqual(outline(),['ზეთის ფილტრი 2','chat-group-fit','chat-part','chat-part','ძრავის ზეთი 5','chat-part','chat-part','chat-part']);
  assert.deepEqual(ownLines(),[[],[],['fit spec','muted reason'],['fit spec','muted reason'],['fit check','muted reason']]);
  assert.equal(oilCards.textContent.split(oilReason).length,3,'Each of the two oils says the reason once');
  // A level this page has no label for gets none, never another level's; its reason still shows.
  responder=async url=>{assert.match(url,/offset=5/);return {...oilTurn,cards:[part('Oil D','g2','exact','ახალი დონე'),part('Oil E','g2',undefined)],offset:5,next_offset:null};};
  await moreParts().fire('click');
  assert.deepEqual(ownLines(),[[],[],['fit spec','muted reason'],['fit spec','muted reason'],['fit check','muted reason'],['muted reason']],'The cards that had their lines keep them as they were; the last card has neither a level nor a reason, so no box at all');
  const unknownLevel=withClass(oilCards,'chat-part').at(-2);
  assert.match(unknownLevel.textContent,/ახალი დონე/);
  assert.doesNotMatch(unknownLevel.textContent,/exact|ემთხვევა|მოდელსა|გადასამოწმებელია|სპეციფიკაციით/);
  // The form's own car wins in the side panel; the chat's car line still shows the engine's name.
  field('make').value='Honda';field('model').value='Fit';
  await field('make').fire('input');
  assert.match(field('selected-vehicle').textContent,/^Honda Fit/);
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'o2'}:url.endsWith('/messages')?{turn_id:'o2',language:'ka'}:{...oilTurn,cards:[],total:0,next_offset:null};
  field('message').value='ზეთის შეცვლა';
  await field('chat-form').fire('submit');
  assert.deepEqual(posts().at(-1).vehicle,{make:'Honda',model:'Fit'});
  assert.match(field('selected-vehicle').textContent,/^Honda Fit/);
  assert.doesNotMatch(field('selected-vehicle').textContent,/საუბრიდან/);
  assert.match(field('selected-vehicle').textContent,/საუბარი სხვა მანქანაზეა: Toyota Camry 2015/,'The panel says the chat is about another car than the form holds');
  assert.equal(field('chat-car-label').textContent,'Toyota Camry 2015');
  field('make').value='';field('model').value='';
  await field('make').fire('input');
  // The composer grows with what is written, up to its limit, and is two rows again once sent.
  const box=field('message');
  box.clientHeight=65;box.scrollHeight=65;box.value='one line';
  await box.fire('input');
  assert.equal(box.style.height,'auto','Two rows are enough: the stylesheet decides');
  box.scrollHeight=120;
  await box.fire('input');
  assert.equal(box.style.height,'122px');
  box.scrollHeight=900;
  await box.fire('input');
  assert.equal(box.style.height,'168px','Longer text scrolls inside the box');
  box.scrollHeight=65;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'o3'}:url.endsWith('/messages')?{turn_id:'o3',language:'en'}:{...fixture,quick_replies:[]};
  await field('chat-form').fire('submit');
  assert.equal(box.style.height,'auto');
  // A part under no header would read as one of the group above it. In a reply that has one, no
  // line speaks for a whole group: each part says its own, and that part, whose level this page
  // has no label for, says nothing. It holds when the part only comes with "Show more".
  await field('new-chat').fire('click');
  timers.length=0;
  const looseTurn={...oilTurn,groups:[oilTurn.groups[0]],cards:[part('Filter A','g1','listed'),part('Filter B','g1','listed')],total:3,next_offset:2};
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'o4'}:url.endsWith('/messages')?{turn_id:'o4',language:'ka'}:looseTurn;
  field('message').value='ფილტრები';
  await field('chat-form').fire('submit');
  const looseCards=withClass(transcript.children.at(-1),'chat-cards')[0];
  const looseOutline=()=>looseCards.children.map(n=>n.className==='chat-group'?n.textContent:n.className);
  assert.deepEqual(looseOutline(),['ზეთის ფილტრი 2','chat-group-fit','chat-part','chat-part']);
  responder=async url=>{assert.match(url,/offset=2/);return {...looseTurn,cards:[part('Loose','','exact')],offset:2,next_offset:null};};
  await moreParts().fire('click');
  assert.deepEqual(looseOutline(),['ზეთის ფილტრი 2','chat-part','chat-part','chat-part']);
  assert.deepEqual(withClass(looseCards,'part-fit').map(n=>n.children.map(p=>p.className)),[['fit listed'],['fit listed']]);
  assert.doesNotMatch(withClass(looseCards,'chat-part').at(-1).textContent,/exact|ემთხვევა|მოდელსა|გადასამოწმებელია|სპეციფიკაციით/);
  // A reply without groups is one list, and its shared line stands above it. The ground is the
  // level and the reason together: one level with two reasons, or one reason under two levels,
  // is said card by card.
  const listOf=async cards=>{
    await field('new-chat').fire('click');
    timers.length=0;
    responder=async url=>url.endsWith('/conversations')?{conversation_id:'o5'}:url.endsWith('/messages')?{turn_id:'o5',language:'ka'}:{...oilTurn,groups:[],cards,total:cards.length,next_offset:null};
    field('message').value='ზეთი';
    await field('chat-form').fire('submit');
    const holder=withClass(transcript.children.at(-1),'chat-cards')[0];
    return [holder.children.map(n=>n.className),withClass(holder,'part-fit').map(n=>n.children.map(p=>p.className))];
  };
  assert.deepEqual(await listOf([part('Oil A','','spec',oilReason),part('Oil B','','spec',oilReason)]),[['chat-group-fit','chat-part','chat-part'],[[],[]]]);
  assert.deepEqual(await listOf([part('Oil A','','spec',oilReason),part('Oil B','','spec','სხვა მიზეზი')]),[['chat-part','chat-part'],[['fit spec','muted reason'],['fit spec','muted reason']]]);
  assert.deepEqual(await listOf([part('Oil A','','listed'),part('Oil B','','check')]),[['chat-part','chat-part'],[['fit listed'],['fit check']]]);
  // The first words of a message open the conversation ahead of it, so the server has made
  // itself ready by the time the message is sent; sending then uses that conversation.
  await field('new-chat').fire('click');
  timers.length=0;
  const opened=()=>calls.filter(c=>c.url.endsWith('/conversations')).length;
  responder=async url=>url.endsWith('/conversations')?{conversation_id:'ahead'}:url.endsWith('/messages')?{turn_id:'a1',language:'en'}:{...fixture,quick_replies:[]};
  const openedBefore=opened(),sentAhead=posts().length;
  field('message').value='   ';
  await field('message').fire('input');
  assert.equal(opened(),openedBefore,'Blank text opens nothing');
  field('message').value='Cam';
  await field('message').fire('input');
  field('message').value='Camry 2015 pads';
  await field('message').fire('input');
  await flush();
  assert.equal(opened(),openedBefore+1,'The first words open one conversation, later ones none');
  assert.equal(posts().length,sentAhead,'Nothing is sent by writing');
  await field('chat-form').fire('submit');
  assert.equal(opened(),openedBefore+1,'Sending uses the conversation that was opened');
  assert.match(calls.filter(c=>c.url.endsWith('/messages')).at(-1).url,/\/conversations\/ahead\/messages$/);
  // A new chat started while that conversation is still opening: the late one is given up.
  await field('new-chat').fire('click');
  let releaseAhead;
  responder=url=>url.endsWith('/conversations')?new Promise(resolve=>{releaseAhead=resolve;}):Promise.resolve({});
  field('message').value='Golf';
  await field('message').fire('input');
  await field('new-chat').fire('click');
  releaseAhead({conversation_id:'late'});
  await flush();
  assert.ok(calls.some(c=>c.url.endsWith('/conversations/late')&&c.options.method==='DELETE'),'A conversation opened for a chat that was reset is deleted');
  assert.equal(transcript.textContent,'');
  console.log('PASS: messenger transcript, typing indicator, single note, answer buttons that stay (pressed, disabled, reopened after a failure), option number only for tapped answers, next steps, group headers, amount, fit labels, one shared warning, retry without a second bubble, car line, transcript scrolling, Enter to send, 5-first pagination, catalog/supplier labels, text-only rendering, VIN privacy, follow-up session reuse, reset and stale-create/poll invalidation, the assistant engine\'s turn (search named in the progress line, reason said once, vehicle text, quoted evidence, oil figures, old-stock note), one confidence line per group (four levels, none for an unknown one, kept right by "Show more", per card beside parts under no header), the chat\'s car in the side panel, starters and hint hidden during a conversation, the growing composer and the conversation opened ahead of the first message.');
})().catch(error=>{console.error(error);process.exitCode=1;});
