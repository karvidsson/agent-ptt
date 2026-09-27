'use strict';
const $ = id => document.getElementById(id);
const state = {me:null,org:null,room:null,rooms:[],members:[],agents:[],presence:[],presenceStatus:'loading',socket:null,generation:0,orgEpoch:0,authEpoch:0,cursor:0,seen:new Set(),signup:true,creating:null,pending:null,rosterTimer:null,speech:null,listening:false,muted:false};
let inviteToken = new URLSearchParams(location.hash.slice(1)).get('invite');
if(inviteToken) history.replaceState(null,'',location.pathname);
window.addEventListener('hashchange',()=>{
  const token=new URLSearchParams(location.hash.slice(1)).get('invite');if(!token)return;
  inviteToken=token;history.replaceState(null,'',location.pathname);$('invitation').hidden=false;
  if(state.me)void loadInvitation();else setAuthMode(state.signup);
});
let noticeTimer;
let onboardingInfo=null, onboardingRows=[], setupResult=null;
function notice(text){$('notice').textContent=text;$('notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('notice').hidden=true,7000);}
function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
async function api(path,method='GET',body){
  const response=await fetch('/api/workspace'+path,{method,credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
  if(response.status===204)return;
  let data;try{data=await response.json();}catch{throw new Error('The server could not complete this request.');}
  if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:'Please check the form and try again.');error.status=response.status;throw error;}
  return data;
}
function context(){return {id:state.org?.id,epoch:state.orgEpoch,generation:state.generation,role:state.org?.role};}
function current(ctx){return !!ctx.id&&ctx.id===state.org?.id&&ctx.epoch===state.orgEpoch;}
function sameRoom(ctx){return current(ctx)&&ctx.generation===state.generation;}
function orgPath(ctx,suffix=''){if(!ctx.id)throw new Error('Choose an organization first.');return '/organizations/'+encodeURIComponent(ctx.id)+suffix;}
function isAdmin(){return state.org&&['owner','admin'].includes(state.org.role);}
function nick(name){return 'nick-'+([...name].reduce((n,c)=>n+c.charCodeAt(0),0)%7);}
function date(value){return value?new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)?value:value+'Z').toLocaleString():'Not recorded';}
function stopSpeech(){state.listening=false;state.muted=false;state.speech?.setMuted(true);state.speech?.cancel();renderSpeech();}
function disconnect(){state.generation++;if(state.socket)state.socket.close();state.socket=null;stopSpeech();}
function closeTenantDialogs(){for(const id of ['manage','profile','name-dialog','secret-dialog','setup-dialog'])if($(id).open)$(id).close();$('secret-value').value='';$('secret-title').textContent='';$('secret-description').textContent='';$('profile-title').textContent='Participant';$('agent-name').value='';$('new-name').value='';$('profile-body').replaceChildren();$('setup-commands').replaceChildren();setupResult=null;state.creating=null;}
function clearTenant(){
  disconnect();state.orgEpoch++;clearTimeout(state.rosterTimer);state.rosterTimer=null;closeTenantDialogs();
  state.room=null;state.rooms=[];state.members=[];state.agents=[];state.presence=[];state.presenceStatus='loading';state.pending=null;state.cursor=0;state.seen.clear();
  for(const id of ['channels','members','agents','pending-invites','roster-list'])$(id).replaceChildren();
  $('message').value='';$('recipient').replaceChildren(new Option('Everyone in this channel',''));$('recipient').disabled=true;
  $('messages').replaceChildren(el('div','Choose a channel to see the conversation.','empty'));
  $('roster-count').textContent='0';$('send').disabled=true;$('message').disabled=true;$('export-log').disabled=true;
  $('channel-name').textContent='Welcome';$('channel-description').textContent='Choose or create an organization to get started.';
  $('connection').textContent='Offline';$('connection').classList.remove('live');$('notice').hidden=true;clearTimeout(noticeTimer);
}
function setAuthMode(signup){
  state.signup=signup;$('name-label').hidden=!signup;$('auth-form').elements.name.required=signup;
  $('organization-label').hidden=!signup||!!inviteToken;$('auth-form').elements.organization.required=signup&&!inviteToken;
  $('password-hint').hidden=!signup;$('auth-form').elements.password.minLength=signup?15:1;$('auth-form').elements.password.autocomplete=signup?'new-password':'current-password';
  $('auth-submit').textContent=signup?(inviteToken?'Create account':'Create your workspace'):'Sign in';
  $('signup-tab').classList.toggle('selected',signup);$('login-tab').classList.toggle('selected',!signup);$('invite-hint').hidden=!inviteToken;$('auth-error').textContent='';
}
$('signup-tab').onclick=()=>setAuthMode(true);$('login-tab').onclick=()=>setAuthMode(false);
$('auth-form').onsubmit=async event=>{
  event.preventDefault();$('auth-submit').disabled=true;$('auth-error').textContent='';
  const data=new FormData(event.target),body={email:data.get('email'),password:data.get('password')};
  if(state.signup){body.name=data.get('name');if(!inviteToken)body.organization=data.get('organization');else body.invitation_token=inviteToken;}
  try{await api(state.signup?'/signup':'/login','POST',body);event.target.reset();await boot();}
  catch(error){$('auth-error').textContent=error.message;}finally{$('auth-submit').disabled=false;}
};
async function boot(preferredOrg){
  const request=++state.authEpoch;
  let me;try{me=await api('/me');}catch(error){
    if(request!==state.authEpoch)return;
    clearTenant();state.org=null;state.me=null;$('app').hidden=true;$('auth').hidden=false;setAuthMode(state.signup);
    if(error.status!==401)notice(error.message);return;
  }
  if(request!==state.authEpoch)return;
  state.me=me;$('auth').hidden=true;$('app').hidden=false;$('user-name').textContent=me.name;$('invitation').hidden=!inviteToken;
  const select=$('organizations');select.replaceChildren();for(const org of me.organizations)select.add(new Option(org.name,org.id));
  if(!me.organizations.length)select.add(new Option('No organization yet',''));
  select.value=preferredOrg||me.organizations[0]?.id||'';if(!select.value&&me.organizations.length)select.value=me.organizations[0].id;
  await switchOrg(select.value);
  if(request===state.authEpoch&&inviteToken)await loadInvitation();
}
$('organizations').onchange=event=>switchOrg(event.target.value).catch(error=>notice(error.message));
async function switchOrg(id){
  clearTenant();state.org=state.me?.organizations.find(org=>org.id===id)||null;
  $('org-name').textContent=state.org?.name||'';$('user-role').textContent=state.org?.role||'';
  $('status-org').textContent=state.org?.name||'No organization';$('status-room').textContent='No channel';$('status-role').textContent=state.org?.role||'';
  $('new-channel').hidden=!isAdmin();$('people').disabled=!state.org;renderSpeech();
  if(!state.org)return;
  const ctx=context();
  try{
    const rooms=await api(orgPath(ctx,'/channels'));if(!current(ctx))return;
    state.rooms=rooms;renderChannels();if(rooms.length)await openRoom(rooms[0]);
    if(current(ctx))refreshRoster(ctx);
  }catch(error){if(current(ctx))notice(error.message);}
}
function renderChannels(){
  $('channels').replaceChildren();for(const room of state.rooms){const button=el('button',room.name);button.dataset.id=room.id;button.title=room.name;button.onclick=()=>openRoom(room);button.classList.toggle('active',room.id===state.room?.id);$('channels').append(button);}
}
async function openRoom(room){
  if(!state.rooms.some(item=>item.id===room.id))return;
  disconnect();state.room=room;state.cursor=0;state.seen.clear();state.pending=null;const ctx=context();
  $('channel-name').textContent='#'+room.name;$('channel-description').textContent='Everyone in this organization · people and agents';$('status-room').textContent='#'+room.name;
  $('message').disabled=false;$('send').disabled=false;$('message').value='';$('recipient').value='';$('recipient').disabled=false;$('export-log').disabled=false;
  $('messages').replaceChildren(el('div','Loading conversation…','empty'));renderChannels();renderSpeech();
  try{const messages=await api(orgPath(ctx,'/channels/'+room.id+'/messages'));if(!sameRoom(ctx))return;
    $('messages').replaceChildren(el('div','No messages yet. Start the conversation.','empty'));renderMessages(messages);connectStream(ctx);
  }catch(error){if(sameRoom(ctx))notice(error.message);}
}
function renderMessages(messages,{speak=false}={}){
  const box=$('messages'),atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100;
  for(const message of messages){
    state.cursor=Math.max(state.cursor,message.id);
    if(state.seen.has(message.id)){const row=[...box.children].find(child=>Number(child.dataset.id)===message.id);if(row)renderReceipt(row,message);continue;}
    state.seen.add(message.id);box.querySelector('.empty')?.remove();
    const row=el('article',undefined,'message'),time=el('time',new Date(message.created_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));time.dateTime=message.created_at;
    const name=el('strong',message.sender_name,'sender '+nick(message.sender_name));name.title=message.sender_name+(message.sender_kind==='agent'?' · Agent':'');
    const body=el('p');if(message.reply_to)body.append(el('small','Reply to message #'+message.reply_to,'reply'));body.append(document.createTextNode(message.text));
    row.append(time,name,body);row.dataset.id=message.id;row.dataset.text=message.text;row.dataset.sender=message.sender_name;row.dataset.time=message.created_at;
    renderReceipt(row,message);const next=[...box.children].find(child=>Number(child.dataset.id)>message.id);box.insertBefore(row,next||null);
    if(speak&&state.listening&&!state.muted)state.speech?.enqueue({orgId:state.org.id,channelId:state.room.id,id:message.id,senderId:message.sender_id,text:message.text});
  }
  if(atBottom)box.scrollTop=box.scrollHeight;
}
function renderReceipt(row,message){
  row.querySelector('.receipt')?.remove();if(!message.deliveries?.length)return;
  const text=message.deliveries.map(item=>`${item.name||'Agent'}: ${item.status==='delivered'?'delivered':'pending'}`).join(' · ');
  const receipt=el('small',text,'receipt');receipt.title='Delivered means the agent received the message, not that it acted on it.';row.append(receipt);
}
function connectStream(ctx){
  if(!sameRoom(ctx)||!state.room)return;
  $('connection').textContent='Connecting';$('connection').classList.remove('live');
  const path='/api/workspace'+orgPath(ctx,'/channels/'+state.room.id+'/stream')+'?after='+state.cursor;
  const socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+path);state.socket=socket;
  socket.onopen=()=>{if(sameRoom(ctx)){$('connection').textContent='Live';$('connection').classList.add('live');}};
  socket.onmessage=event=>{if(!sameRoom(ctx))return;try{renderMessages(JSON.parse(event.data).messages||[],{speak:true});}catch{notice('An unreadable stream update was skipped. Reopen the channel to reload.');}};
  socket.onclose=event=>{
    if(!sameRoom(ctx))return;$('connection').textContent='Disconnected';$('connection').classList.remove('live');
    if([4401,4403].includes(event.code)){$('send').disabled=true;$('message').disabled=true;stopSpeech();notice('Access changed. Sign in again or choose another organization.');return;}
    setTimeout(()=>connectStream(ctx),3000);
  };
}
$('message').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('compose').requestSubmit();}};
$('compose').onsubmit=async event=>{
  event.preventDefault();const text=$('message').value.trim();if(!text||!state.room||$('send').disabled)return;
  const ctx=context(),recipient=$('recipient').value,recipient_ids=recipient?[recipient]:[];
  if(!state.pending||state.pending.text!==text||JSON.stringify(state.pending.recipient_ids)!==JSON.stringify(recipient_ids))state.pending={text,recipient_ids,client_id:crypto.randomUUID()};
  const payload=state.pending;$('send').disabled=true;
  try{const message=await api(orgPath(ctx,'/channels/'+state.room.id+'/messages'),'POST',payload);if(!sameRoom(ctx))return;
    const cursor=state.cursor;renderMessages([message],{speak:true});state.cursor=cursor;$('message').value='';state.pending=null;$('messages').scrollTop=$('messages').scrollHeight;
  }catch(error){if(sameRoom(ctx))notice(error.message);}finally{if(sameRoom(ctx)){$('send').disabled=false;$('message').focus();}}
};
$('logout').onclick=async()=>{try{await api('/logout','POST');state.authEpoch++;clearTenant();state.me=null;state.org=null;await boot();}catch(error){notice(error.message);}};
function createNamed(kind){state.creating={kind,...context()};$('name-title').textContent='Create '+kind;$('new-name').value='';$('name-dialog').showModal();}
$('new-org').onclick=()=>createNamed('organization');$('new-channel').onclick=()=>createNamed('channel');
$('name-form').onsubmit=async event=>{
  event.preventDefault();const creation=state.creating;if(!creation)return;const button=event.target.querySelector('button');button.disabled=true;
  try{const result=await api(creation.kind==='organization'?'/organizations':orgPath(creation,'/channels'),'POST',{name:$('new-name').value});
    if(creation!==state.creating)return;$('name-dialog').close();await boot(creation.kind==='organization'?result.id:creation.id);
    if(creation.kind==='channel'&&state.org?.id===creation.id)await openRoom(result);
  }catch(error){if(creation===state.creating)notice(error.message);}finally{button.disabled=false;}
};
async function loadInvitation(){
  const token=inviteToken,epoch=state.authEpoch;
  $('accept-invite').disabled=true;onboardingInfo=null;onboardingRows=[];$('onboarding-agents').replaceChildren();$('onboarding-fields').hidden=true;$('invitation-error').textContent='';
  try{const info=await api('/invitations/inspect','POST',{token});if(token!==inviteToken||epoch!==state.authEpoch)return;
    onboardingInfo=info;$('invitation-description').textContent=`Join ${info.organization} as ${info.role}${info.agent_limit?` and register up to ${info.agent_limit} agents, starting in #${info.channel}`:''}. This link can be used once.`;
    $('onboarding-fields').hidden=!info.agent_limit;$('onboard-count').max=info.agent_limit;$('onboard-count').value=info.agent_limit;
    onboardingRows=[];renderOnboardingAgents();$('accept-invite').disabled=false;
  }catch(error){if(token===inviteToken&&epoch===state.authEpoch)$('invitation-error').textContent=error.message;}
}
function renderOnboardingAgents(){
  const count=Math.max(0,Math.min(onboardingInfo?.agent_limit||0,Math.floor(Number($('onboard-count').value))||0));
  const previous=onboardingRows.map(row=>({name:row.name.value,harness:row.harness.value}));onboardingRows=[];$('onboarding-agents').replaceChildren();
  for(let index=0;index<count;index++){
    const row=el('div',undefined,'onboarding-row'),name=el('input'),harness=el('select');
    name.value=previous[index]?.name||`Agent ${index+1}`;name.maxLength=80;name.required=true;name.setAttribute('aria-label',`Agent ${index+1} name`);
    harness.setAttribute('aria-label',`Agent ${index+1} CLI`);
    for(const [value,label] of [['codex','Codex'],['claude','Claude Code'],['gemini','Gemini CLI'],['opencode','OpenCode'],['custom','Other CLI']])harness.add(new Option(label,value));
    harness.value=previous[index]?.harness||'codex';row.append(name,harness);$('onboarding-agents').append(row);onboardingRows.push({name,harness});
  }
}
$('onboard-count').onchange=renderOnboardingAgents;
$('accept-invite').onclick=async()=>{
  const token=inviteToken,epoch=state.authEpoch,user=state.me?.id;$('accept-invite').disabled=true;$('invitation-error').textContent='';
  try{
    const agents=onboardingRows.map(row=>({name:row.name.value.trim(),harness:row.harness.value}));
    if(agents.some(agent=>!agent.name)||new Set(agents.map(agent=>agent.name.toLowerCase())).size!==agents.length)throw new Error('Give each agent a distinct, non-empty name.');
    const result=await api('/invitations/accept','POST',{token,agents});if(token!==inviteToken||epoch!==state.authEpoch)return;
    inviteToken=null;onboardingRows=[];onboardingInfo=null;await boot(result.org_id);
    if(state.me?.id!==user||state.org?.id!==result.org_id)return;
    if(result.launcher)showAgentSetup(result);else notice('You joined the organization.');
  }catch(error){if(token===inviteToken&&epoch===state.authEpoch)$('invitation-error').textContent=error.message;}
  finally{$('accept-invite').disabled=false;}
};
function showAgentSetup(result){
  setupResult=result;$('setup-commands').replaceChildren();$('setup-error').textContent='';
  for(const agent of result.agents){const section=el('section');section.append(el('h3',agent.name),el('pre',`python3 agent-ptt-team.py ${agent.key} --check\npython3 agent-ptt-team.py ${agent.key} -- ${agent.harness==='custom'?'YOUR_CLI':agent.harness}`));$('setup-commands').append(section);}
  $('setup-dialog').showModal();
}
$('download-setup').onclick=()=>{
  if(!setupResult||setupResult.org_id!==state.org?.id)return;
  const url=URL.createObjectURL(new Blob([setupResult.launcher],{type:'text/x-python'}));
  const link=el('a');link.href=url;link.download='agent-ptt-team.py';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
$('setup-dialog').addEventListener('close',()=>{setupResult=null;$('setup-commands').replaceChildren();});
$('dismiss-invite').onclick=()=>{inviteToken=null;onboardingInfo=null;onboardingRows=[];$('onboarding-agents').replaceChildren();$('invitation').hidden=true;};
async function refreshRoster(ctx){
  if(!current(ctx))return;
  const presence=fetchPresence(ctx).then(rows=>({rows,status:'ready'}),()=>({rows:[],status:'unavailable'}));
  try{const [members,agents,activity]=await Promise.all([api(orgPath(ctx,'/members')),api(orgPath(ctx,'/agents')),presence]);if(!current(ctx))return;
    state.members=members;state.agents=agents;state.presence=activity.rows;state.presenceStatus=activity.status;renderRoster();renderRecipients();refreshReceipts(context());
  }catch(error){if(current(ctx)){$('presence-note').textContent='Roster unavailable. '+error.message;}}
  if(current(ctx))state.rosterTimer=setTimeout(()=>refreshRoster(ctx),15000);
}
async function fetchPresence(ctx){
  const rows=[],limit=200;
  for(let offset=0;current(ctx);offset+=limit){
    const page=await api(orgPath(ctx,'/sessions?limit='+limit+'&offset='+offset));
    if(!current(ctx))return [];
    rows.push(...page);if(page.length<limit)return rows;
  }
  return [];
}
async function refreshReceipts(ctx){
  if(!sameRoom(ctx)||!state.room)return;
  try{const messages=await api(orgPath(ctx,'/channels/'+state.room.id+'/messages'));if(!sameRoom(ctx))return;
    for(const message of messages){if(!state.seen.has(message.id))continue;const row=[...$('messages').children].find(child=>Number(child.dataset.id)===message.id);if(row)renderReceipt(row,message);}
  }catch{ /* Streaming and text submission remain independent of receipt refresh. */ }
}
function renderRecipients(){const selected=$('recipient').value;$('recipient').replaceChildren(new Option('Everyone in this channel',''));for(const agent of state.agents.filter(a=>a.active))$('recipient').add(new Option('@'+agent.name,agent.id));$('recipient').value=selected;if(!$('recipient').value)$('recipient').value='';}
function renderRoster(){
  const people=[...state.members.map(person=>({...person,kind:'human'})),...state.agents.filter(agent=>agent.active).map(agent=>({...agent,kind:'agent'}))];
  $('roster-count').textContent=people.length;$('roster-list').replaceChildren();
  for(const person of people){const row=el('button',undefined,'roster-person'),meta=el('span',undefined,'person-meta');meta.append(el('strong',person.name),el('small',presenceSummary(person)));row.append(el('span',person.name.slice(0,2).toUpperCase(),'avatar '+nick(person.name)),meta);row.onclick=()=>openProfile(person);$('roster-list').append(row);}
  $('presence-note').textContent=state.presenceStatus==='unavailable'?'Presence unavailable · membership does not indicate activity.':'Reported sessions · delayed heartbeat indicates lost contact, not proven inactivity.';
}
function personSessions(person){return state.presence.filter(session=>session.principal_id===person.id&&session.kind===person.kind);}
function presenceLabel(session){
  if(session.stale&&session.state!=='offline')return session.state+' · inferred offline';
  if(session.presumed_hung&&session.state!=='offline')return session.state+' · heartbeat delayed';
  return session.state+(session.state==='offline'?' · reported':'');
}
function presenceSummary(person){
  if(state.presenceStatus!=='ready')return state.presenceStatus==='loading'?'Presence loading':'Presence unavailable';
  const sessions=personSessions(person);
  if(!sessions.length)return 'Presence not reported';
  return (sessions.length>1?sessions.length+' sessions · ':'')+[...new Set(sessions.map(presenceLabel))].join(', ');
}
function metadataList(values){const list=el('dl');for(const [label,value] of Object.entries(values))list.append(el('dt',label),el('dd',String(value??'Not reported')));return list;}
function openProfile(person){
  $('profile-title').textContent=person.name;
  const sessions=personSessions(person);
  const list=metadataList({Type:person.kind,Role:person.role||'member',ID:person.id,Organization:state.org.name,Harness:person.harness});
  const activity=el('section',undefined,'session-details');activity.append(el('h3','Sessions'));
  if(!sessions.length)activity.append(el('p',presenceSummary(person)));
  for(const session of sessions){
    activity.append(metadataList({'Session ID':session.session_id,Harness:session.harness??person.harness,'Reported state':session.state,'Contact status':session.stale?'Heartbeat expired · inferred offline':session.presumed_hung?'Heartbeat delayed':'No inferred contact loss',Task:session.task,Channel:state.rooms.find(room=>room.id===session.channel_id)?.name??session.channel_id,'State since':date(session.since),'Last heartbeat':date(session.last_refresh),'Reported tokens in':session.tokens_in,'Reported tokens out':session.tokens_out}));
  }
  if(sessions.length)activity.append(el('small','Token counts are client reports, not billing data. Presence is a snapshot from the latest roster refresh.'));
  const details=el('details');details.append(el('summary','All available metadata'),el('pre',JSON.stringify({...person,sessions},null,2)));
  $('profile-body').replaceChildren(list,activity,details);$('profile').showModal();
}
function renderRosterToggle(){const visible=getComputedStyle($('roster')).display!=='none';$('roster-toggle').setAttribute('aria-expanded',String(visible));}
$('roster-toggle').onclick=()=>{const show=getComputedStyle($('roster')).display==='none';$('app').classList.toggle('roster-open',show);$('app').classList.toggle('roster-hidden',!show);renderRosterToggle();};window.addEventListener('resize',renderRosterToggle);
async function loadPeople(ctx=context()){
  const admin=['owner','admin'].includes(ctx.role);
  const [members,agents,invitations]=await Promise.all([api(orgPath(ctx,'/members')),api(orgPath(ctx,'/agents')),admin?api(orgPath(ctx,'/invitations')):Promise.resolve([])]);
  if(!current(ctx))return false;
  state.members=members;state.agents=agents;renderRoster();renderRecipients();$('members').replaceChildren();$('agents').replaceChildren();$('pending-invites').replaceChildren();
  const action=async(suffix,method,body)=>{if(!current(ctx))return;try{await api(orgPath(ctx,suffix),method,body);if(current(ctx))await loadPeople(ctx);}catch(error){if(current(ctx))notice(error.message);}};
  for(const invitation of invitations){const row=el('div',undefined,'member');row.append(el('strong','Pending '+invitation.role+' invitation'+(invitation.agent_limit?' · up to '+invitation.agent_limit+' agents':'')));const revoke=el('button','Revoke');revoke.onclick=()=>action('/invitations/'+invitation.id,'DELETE');row.append(revoke);$('pending-invites').append(row);}
  for(const person of members){
    const row=el('div',undefined,'member');row.append(el('strong',person.name));
    if(ctx.role==='owner'&&person.role!=='owner'){const select=el('select');select.setAttribute('aria-label','Role for '+person.name);for(const role of ['member','admin'])select.add(new Option(role,role));select.value=person.role;select.onchange=()=>action('/members/'+person.id,'PATCH',{role:select.value});row.append(select);}else row.append(el('small',person.role));
    if(admin&&person.role!=='owner'&&(person.role!=='admin'||ctx.role==='owner')){const remove=el('button','Remove');remove.onclick=()=>{if(confirm('Remove '+person.name+' from this organization?'))action('/members/'+person.id,'DELETE');};row.append(remove);}$('members').append(row);
  }
  for(const agent of agents){const row=el('div',undefined,'member');row.append(el('strong',agent.name),el('small',agent.active?'Agent · '+(agent.harness||'CLI not specified')+(agent.onboarded_by?' · '+(members.find(person=>person.id===agent.onboarded_by)?.name||'Former member'):''):'Revoked'));if(admin&&agent.active){const revoke=el('button','Revoke');revoke.onclick=()=>{if(confirm('Revoke access for '+agent.name+'?'))action('/agents/'+agent.id,'DELETE');};row.append(revoke);}$('agents').append(row);}
  if(!agents.length)$('agents').append(el('p','No agents registered yet.','muted'));
  $('team-invite-form').hidden=!admin;$('team-channel').replaceChildren();for(const room of state.rooms)$('team-channel').add(new Option('#'+room.name,room.id));$('team-channel').value=state.room?.id||state.rooms[0]?.id||'';$('invite-form').hidden=!admin;$('agent-form').hidden=!admin;$('invite-role').querySelector('[value="admin"]').disabled=ctx.role!=='owner';$('invite-role').value='member';return true;
}
$('people').onclick=async()=>{const ctx=context();try{if(await loadPeople(ctx)&&current(ctx))$('manage').showModal();}catch(error){if(current(ctx))notice(error.message);}};
function showSecret(ctx,title,description,value){if(!current(ctx))return;$('secret-title').textContent=title;$('secret-description').textContent=description;$('secret-value').value=value;$('secret-dialog').showModal();}
$('invite-form').onsubmit=async event=>{event.preventDefault();const ctx=context(),button=event.target.querySelector('button');button.disabled=true;try{const result=await api(orgPath(ctx,'/invitations'),'POST',{role:$('invite-role').value});if(!current(ctx))return;await loadPeople(ctx);showSecret(ctx,'Invite someone','Share this one-use link with its intended recipient. It expires in 7 days.',result.url);}catch(error){if(current(ctx))notice(error.message);}finally{button.disabled=false;}};
$('team-invite-form').onsubmit=async event=>{
  event.preventDefault();const ctx=context(),button=event.target.querySelector('button');button.disabled=true;
  try{const result=await api(orgPath(ctx,'/invitations'),'POST',{role:'member',agent_limit:Number($('team-limit').value),channel_id:$('team-channel').value});if(!current(ctx))return;await loadPeople(ctx);showSecret(ctx,'Invite a developer with agents','Share this one-use link privately with the developer. It expires in 7 days. She can join, name her agents, and download individual launch configurations.',result.url);}
  catch(error){if(current(ctx))notice(error.message);}finally{button.disabled=false;}
};
$('agent-form').onsubmit=async event=>{event.preventDefault();const ctx=context(),button=event.target.querySelector('button');button.disabled=true;try{const result=await api(orgPath(ctx,'/agents'),'POST',{name:$('agent-name').value});if(!current(ctx))return;$('agent-name').value='';await loadPeople(ctx);showSecret(ctx,'Agent credential','Save this credential now; it is shown only once. Use it as a Bearer token with this organization’s API. It expires in 90 days.',result.token);}catch(error){if(current(ctx))notice(error.message);}finally{button.disabled=false;}};
$('copy-secret').onclick=async()=>{try{await navigator.clipboard.writeText($('secret-value').value);notice('Copied.');}catch{$('secret-value').select();notice('Select and copy the value.');}};
for(const button of document.querySelectorAll('[data-close]'))button.onclick=()=>$(button.dataset.close).close();
$('secret-dialog').addEventListener('close',()=>{$('secret-value').value='';});
$('export-log').onclick=()=>{if(!state.room)return;const text=[...$('messages').querySelectorAll('.message')].map(row=>`${row.dataset.time} ${row.dataset.sender}: ${row.dataset.text}`).join('\n');const url=URL.createObjectURL(new Blob([text],{type:'text/plain'}));const link=el('a');link.href=url;link.download=state.room.name.replace(/[^a-z0-9_-]/gi,'-')+'.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
function renderSpeech(){
  $('speech-status').title='';
  $('listen').disabled=!state.room;$('mute').disabled=!state.listening;$('listen').textContent=state.listening?'Stop listening':'Listen in';$('listen').classList.toggle('listening',state.listening);$('mute').setAttribute('aria-pressed',String(state.muted));$('mute').textContent=state.muted?'Unmute':'Mute';$('speech-status').textContent=state.listening?(state.muted?'Audio muted · text remains live':'Listening · Pocket TTS'):'Text conversation · speech is optional';
}
$('listen').onclick=async()=>{
  if(state.listening){stopSpeech();return;}const ctx=context();$('listen').disabled=true;
  try{if(!state.speech){const {SpeechQueue,ServerSpeechProvider}=await import('/workspace/assets/workspace-speech.js');if(!sameRoom(ctx))return;state.speech=new SpeechQueue({provider:new ServerSpeechProvider(),onError:(error,item)=>{if(state.listening&&item.orgId===state.org?.id&&item.channelId===state.room?.id){$('speech-status').textContent='Audio unavailable for a message · text remains live';$('speech-status').title=error.message;}}});}if(!sameRoom(ctx))return;state.listening=true;state.muted=false;state.speech.setMuted(false);renderSpeech();}
  catch{if(sameRoom(ctx)){$('speech-status').textContent='Audio is not available yet · text remains live';$('listen').disabled=false;}}
};
$('mute').onclick=()=>{state.muted=!state.muted;state.speech?.setMuted(state.muted);renderSpeech();};
setAuthMode(true);renderRosterToggle();boot();
