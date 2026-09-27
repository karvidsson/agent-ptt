import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../agent_ptt/static/workspace-speech.js', import.meta.url));
const {SpeechQueue, ServerSpeechProvider} = await import(`data:text/javascript;base64,${source.toString('base64')}`);
const tick = () => new Promise(resolve => setImmediate(resolve));
const item = id => ({orgId:'org',channelId:'room',id,text:'hello'});

test('ordered, deduplicated playback and default mute', async () => {
  const log = [];
  const q = new SpeechQueue({provider:{async prepare(i) {
    log.push(`prepare ${i.id}`);
    return {async play(){log.push(`play ${i.id}`);},dispose(){log.push(`dispose ${i.id}`);}};
  }}});
  assert.equal(q.enqueue(item(0)),false);
  q.setMuted(false);
  q.enqueue(item(1)); q.enqueue(item(2)); assert.equal(q.enqueue(item(1)),false);
  await tick();
  assert.deepEqual(log,['prepare 1','play 1','dispose 1','prepare 2','play 2','dispose 2']);
});

test('mute cancels active playback and queued messages; queue is bounded', async () => {
  const log=[]; const errors=[];
  const q=new SpeechQueue({maxPending:1,onError:e=>errors.push(e),provider:{async prepare(i){
    return {play:({signal})=>new Promise((resolve,reject)=>{
      log.push(i.id); signal.addEventListener('abort',()=>reject(new Error('aborted')));
    }),dispose(){log.push('disposed');}};
  }}});
  q.setMuted(false); q.enqueue(item(1)); await tick();
  q.enqueue(item(2)); assert.equal(q.enqueue(item(3)),false);
  q.setMuted(true); await tick();
  assert.deepEqual(log,[1,'disposed']); assert.equal(errors.length,1);
});

test('switch cancels stale preparation, cleans it and plays new context', async () => {
  let ready; const log=[];
  const q=new SpeechQueue({provider:{async prepare(i){
    if(i.orgId==='org') await new Promise(r=>{ready=r;});
    return {async play(){log.push(i.orgId);},dispose(){log.push(`dispose ${i.orgId}`);}};
  }}});
  q.setMuted(false); q.enqueue(item(1)); await tick(); q.cancel();
  q.enqueue({...item(1),orgId:'other'}); ready(); await tick();
  assert.deepEqual(log,['dispose org','other','dispose other']);
});

test('fallback occurs only before playback; failed playback does not replay', async () => {
  const log=[];const errors=[];
  const q=new SpeechQueue({onError:e=>errors.push(e),provider:{async prepare(i){
    if(i.id===1) throw Object.assign(new Error('unsupported'),{fallbackAllowed:true});
    return {async play(){log.push('partial');throw new Error('play failed');},dispose(){}};
  }},fallback:{async prepare(){return {async play(){log.push('fallback');},dispose(){}};}}});
  q.setMuted(false);q.enqueue(item(1));q.enqueue(item(2));await tick();
  assert.deepEqual(log,['fallback','partial']);assert.equal(errors.length,1);
});

test('server provider sends cookies, aborts playback, releases object URL', async () => {
  const calls=[];const audio=new EventTarget();
  audio.play=async()=>{};audio.pause=()=>calls.push('pause');audio.removeAttribute=()=>{};
  const provider=new ServerSpeechProvider({
    fetchImpl:async(path,options)=>{calls.push([path,options.credentials]);return {ok:true,blob:async()=>new Blob(['wav'])};},
    audioFactory:()=>audio,urls:{createObjectURL:()=> 'blob:test',revokeObjectURL:url=>calls.push(url)}
  });
  const control=new AbortController();const clip=await provider.prepare(item(1),{signal:control.signal});
  const playing=clip.play({signal:control.signal}); await tick(); control.abort();
  await assert.rejects(playing,{name:'AbortError'});clip.dispose();clip.dispose();
  assert.deepEqual(calls[0],['/api/workspace/organizations/org/channels/room/messages/1/speech','same-origin']);
  assert.equal(calls.filter(c=>c==='blob:test').length,1);
});

test('default browser fetch retains its global receiver', async t => {
  let fetched = false;
  t.mock.method(globalThis, 'fetch', async function () {
    assert.equal(this, globalThis, 'Window.fetch requires the Window receiver');
    fetched = true;
    return {ok:true, blob:async()=>new Blob(['wav'])};
  });
  const provider = new ServerSpeechProvider({
    audioFactory:()=>({pause(){},removeAttribute(){}}),
    urls:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},
  });
  const clip = await provider.prepare(item(1), {signal:new AbortController().signal});
  clip.dispose();
  assert.equal(fetched, true);
});
