import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../agent_ptt/static/workspace.js',import.meta.url),'utf8').replace('setAuthMode(true);renderRosterToggle();boot();','');
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.open=false;this.scrollHeight=0;this.scrollTop=0;this.clientHeight=100;this.className='';this.classList={toggle(){},add(){},remove(){}};}
  append(...nodes){this.children.push(...nodes);for(const node of nodes)node.parent=this;}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  add(node){this.append(node);}
  setAttribute(){} addEventListener(){} focus(){} select(){} click(){}
  showModal(){this.open=true;} close(){this.open=false;}
  querySelector(selector){if(selector==='button')return this.button??=new Node('button');if(selector==='[value="admin"]')return this.admin??=new Node('option');return this.children.find(n=>n.className?.split(' ').includes(selector.slice(1)))||null;}
  querySelectorAll(selector){return this.children.filter(n=>n.className?.split(' ').includes(selector.slice(1)));}
  insertBefore(node,next){if(!next)this.append(node);else this.children.splice(this.children.indexOf(next),0,node);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
}
function harness(){
  const nodes=new Map(),requests=[],waiters=[],events={};
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
  class Option extends Node{constructor(text,value){super('option');this.textContent=text;this.value=value;}}
  const ctx=vm.createContext({document:{getElementById:get,createElement:tag=>new Node(tag),createTextNode:text=>new Node('text'),querySelectorAll:()=>[]},location:{hash:'',pathname:'/workspace/',protocol:'http:',host:'localhost'},history:{replaceState(){}},window:{addEventListener(name,handler){events[name]=handler;}},getComputedStyle:()=>({display:'flex'}),URLSearchParams,Option,crypto:{randomUUID:()=> 'retry-id'},setTimeout:()=>1,clearTimeout(){},console,WebSocket:class{close(){}},fetch:(path,options)=>new Promise(resolve=>{requests.push({path,options});waiters.push(data=>resolve({ok:true,status:200,json:async()=>data}));})});
  const run=code=>vm.runInContext(code,ctx);run(source);run(`state.me={name:'Test',organizations:[{id:'a',name:'Alpha',role:'owner'},{id:'b',name:'Beta',role:'member'}]}`);
  return {run,nodes,get,requests,waiters,events};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('late organization channel response never overwrites current tenant',async()=>{
 const h=harness();const a=h.run('switchOrg("a")');const b=h.run('switchOrg("b")');
 h.waiters[1]([]);await b;h.waiters[0]([{id:'private-a',name:'Private Alpha'}]);await a;
 assert.equal(h.run('state.org.id'),'b');assert.equal(h.run('state.rooms.length'),0);assert.equal(h.get('channels').children.length,0);
 assert.equal(h.get('new-channel').hidden,false);
});
test('switching tenant immediately clears credentials, modal content, recipients and pending sends',()=>{
 const h=harness();h.run(`state.org=state.me.organizations[0];state.pending={text:'private'};state.agents=[{id:'secret-agent'}];state.seen.add(9);state.cursor=9;`);
 h.get('secret-value').value='secret';h.get('secret-dialog').open=true;h.get('message').value='private draft';h.get('profile-body').append(new Node());
 h.run('switchOrg("b")');assert.equal(h.get('secret-value').value,'');assert.equal(h.get('secret-dialog').open,false);assert.equal(h.get('message').value,'');assert.equal(h.run('state.pending'),null);assert.equal(h.run('state.agents.length'),0);assert.equal(h.run('state.seen.size'),0);assert.equal(h.get('profile-body').children.length,0);
});
test('a late agent credential response is discarded after organization switch',async()=>{
 const h=harness();h.run('state.org=state.me.organizations[0]');h.get('agent-name').value='Helper';const form=new Node('form');h.ctxEvent={preventDefault(){},target:form};
 const submit=h.get('agent-form').onsubmit(h.ctxEvent);h.run('switchOrg("b")');h.waiters[0]({token:'must-not-display'});await submit;
 assert.equal(h.get('secret-dialog').open,false);assert.equal(h.get('secret-value').value,'');assert.equal(h.requests[0].path,'/api/workspace/organizations/a/agents');
});
test('room generation rejects history from a previously selected room',async()=>{
 const h=harness();h.run(`state.org=state.me.organizations[0];state.rooms=[{id:'x',name:'X'},{id:'y',name:'Y'}]`);
 const x=h.run('openRoom(state.rooms[0])'),y=h.run('openRoom(state.rooms[1])');h.waiters[1]([]);await y;h.waiters[0]([{id:99,sender_name:'Old room',text:'private',created_at:'2026-09-27T10:00:00Z'}]);await x;
 assert.equal(h.run('state.room.id'),'y');assert.equal(h.run('state.seen.has(99)'),false);assert.equal(h.get('channel-name').textContent,'#Y');
});
test('organization switch cancels optional speech and keeps requests tenant-scoped',()=>{
 const h=harness();h.run(`state.org=state.me.organizations[0];state.room={id:'x'};state.listening=true;state.speech={setMuted(v){this.muted=v},cancel(){this.cancelled=true}};switchOrg('b')`);
 assert.equal(h.run('state.speech.cancelled'),true);assert.equal(h.run('state.speech.muted'),true);assert.equal(h.run('state.listening'),false);assert.ok(h.requests.every(r=>r.path.startsWith('/api/workspace/')));
});
test('presence matches principal and kind, preserves multiple sessions and unknown absence',()=>{
 const h=harness();h.run(`state.presenceStatus='ready';state.presence=[
 {principal_id:'x',kind:'agent',session_id:'one',state:'working',presumed_hung:true},
 {principal_id:'x',kind:'agent',session_id:'two',state:'idle'},
 {principal_id:'x',kind:'human',session_id:'three',state:'offline'}]`);
 assert.equal(h.run("presenceSummary({id:'x',kind:'agent'})"),'2 sessions · working · heartbeat delayed, idle');
 assert.equal(h.run("presenceSummary({id:'x',kind:'human'})"),'offline · reported');
 assert.equal(h.run("presenceSummary({id:'other',kind:'agent',name:'x'})"),'Presence not reported');
 assert.equal(h.run("presenceLabel({state:'working',stale:true,presumed_hung:true})"),'working · inferred offline');
});
test('presence fetch paginates and discards a late page after tenant changes',async()=>{
 const h=harness();h.run('state.org=state.me.organizations[0]');const result=h.run('fetchPresence(context())');
 h.waiters[0](Array.from({length:200},(_,n)=>({session_id:String(n)})));await tick();
 assert.match(h.requests[1].path,/offset=200$/);
 h.run("clearTenant();state.org=state.me.organizations[1]");h.waiters[1]([{session_id:'private'}]);
 assert.equal((await result).length,0);assert.equal(h.run('state.presence.length'),0);
});
test('presence failure leaves roster available without claiming offline',async()=>{
 const h=harness();h.run('state.org=state.me.organizations[0]');const result=h.run('refreshRoster(context())');
 h.waiters[0](null);h.waiters[1]([]);h.waiters[2]([{id:'x',name:'Helper',active:true}]);await result;
 assert.equal(h.run('state.agents.length'),1);assert.equal(h.run('state.presenceStatus'),'unavailable');
 assert.equal(h.run("presenceSummary({id:'x',kind:'agent'})"),'Presence unavailable');
});
test('profile exposes per-session metadata and does not invent harness from agent name',()=>{
 const h=harness();h.run(`state.org=state.me.organizations[0];state.presenceStatus='ready';state.presence=[{principal_id:'x',kind:'agent',session_id:'cli-one',agent:'Codex',state:'working',tokens_in:0}];openProfile({id:'x',name:'Codex',kind:'agent'})`);
 const all=node=>[node.textContent,...node.children.map(all)].join(' ');
 const content=all(h.get('profile-body'));assert.match(content,/cli-one/);assert.match(content,/Harness Not reported/);assert.match(content,/Reported tokens in 0/);assert.equal(h.get('profile').open,true);
});
test('developer invitation posts a bounded batch and discards stale invite links',async()=>{
 const h=harness();h.run('state.org=state.me.organizations[0]');h.get('team-limit').value='8';h.get('team-channel').value='room-a';
 const form=new Node('form');const submit=h.get('team-invite-form').onsubmit({preventDefault(){},target:form});
 assert.deepEqual(JSON.parse(h.requests[0].options.body),{role:'member',agent_limit:8,channel_id:'room-a'});
 h.run('switchOrg("b")');h.waiters[0]({url:'private-invitation'});await submit;
 assert.equal(h.get('secret-dialog').open,false);assert.equal(h.get('secret-value').value,'');
});
test('switching tenant erases the downloadable credential bundle',()=>{
 const h=harness();h.run(`state.org=state.me.organizations[0];showAgentSetup({org_id:'a',launcher:'private tokens',agents:[{key:'agent-1',name:'Planner',harness:'codex'}]})`);
 assert.equal(h.get('setup-dialog').open,true);h.run('switchOrg("b")');
 assert.equal(h.run('setupResult'),null);assert.equal(h.get('setup-dialog').open,false);assert.equal(h.get('setup-commands').children.length,0);
});
test('agent enrollment rejects blank and duplicate names before sending credentials',async()=>{
 const h=harness();h.run(`inviteToken='invite';onboardingRows=[{name:{value:'Same'},harness:{value:'codex'}},{name:{value:'same'},harness:{value:'claude'}}]`);
 await h.get('accept-invite').onclick();assert.equal(h.requests.length,0);assert.match(h.get('invitation-error').textContent,/distinct/);
});
test('stale invitation inspection cannot populate another authenticated session',async()=>{
 const h=harness();h.run("inviteToken='old-token'");const result=h.run('loadInvitation()');h.run("state.authEpoch++;inviteToken='new-token'");
 h.waiters[0]({organization:'Private',role:'member',agent_limit:8,channel:'Private'});await result;
 assert.equal(h.run('onboardingInfo'),null);assert.equal(h.get('onboarding-agents').children.length,0);
});

test('an invitation opened in the same tab is recognized without reloading',async()=>{
 const h=harness();h.run("location.hash='#invite=new-invitation'");h.events.hashchange();
 assert.equal(h.run('inviteToken'),'new-invitation');assert.equal(h.requests[0].path,'/api/workspace/invitations/inspect');
 h.waiters[0]({organization:'Invited org',role:'member',agent_limit:0});await tick();
 assert.match(h.get('invitation-description').textContent,/Invited org/);assert.equal(h.get('accept-invite').disabled,false);
});
test('general join link uses organization enrollment without a channel or agent batch',async()=>{
 const h=harness();h.run("generalJoin=true;inviteToken='"+'a'.repeat(43)+"'");
 const info=h.run('loadInvitation()');assert.equal(h.requests[0].path,'/api/workspace/join/inspect');
 h.waiters.shift()({organization:'Team',role:'member'});await info;
 assert.match(h.get('invitation-description').textContent,/list, create, and join channels/);
 assert.equal(h.get('onboarding-fields').hidden,true);
 assert.match(h.get('invite-agent-command').textContent,/workspace --profile my-agent enroll/);
 const accept=h.get('accept-invite').onclick();
 assert.equal(h.requests[1].path,'/api/workspace/join/person');
 assert.deepEqual(JSON.parse(h.requests[1].options.body),{token:'a'.repeat(43)});
 h.waiters.shift()({org_id:'a'});await tick();h.waiters.shift()({name:'Test',organizations:[]});await accept;
 assert.equal(h.run('inviteToken'),null);
});
test('human and agent choices are explicit and malformed links never produce shell commands',()=>{
 const h=harness();h.run("generalJoin=true;inviteToken='"+'b'.repeat(43)+"';renderJoinChoice()");
 assert.equal(h.get('auth-card').hidden,true);h.get('join-person').onclick();
 assert.equal(h.get('auth-card').hidden,false);h.run('renderJoinChoice()');assert.equal(h.get('auth-card').hidden,false);
 h.get('join-agent').onclick();assert.equal(h.get('join-agent-help').hidden,false);
 h.run(`inviteToken="invalid'$(command)";renderJoinChoice()`);assert.equal(h.get('join-agent-command').textContent,'');
});
test('late general link creation cannot expose another organization secret',async()=>{
 const h=harness();h.run('state.org=state.me.organizations[0]');
 const pending=h.get('create-join-link').onclick();assert.equal(h.requests[0].path,'/api/workspace/organizations/a/join-link');
 h.run('switchOrg("b")');h.waiters[0]({url:'secret-link-a'});await pending;
 assert.equal(h.get('secret-value').value,'');assert.equal(h.get('secret-dialog').open,false);
});
