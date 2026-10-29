import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import vm from 'node:vm';

const template = await readFile(new URL('../quiz/templates/quiz/exam_run.html', import.meta.url), 'utf8');
const script = template.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/import \{ Proctor \} from [^;]+;/, 'const Proctor = FakeProctor;')
  .replace(/\{% url '(\w+)'[^%]*%\}/g, (_, name) => `/exam/test/${name}/`)
  .replace(/\{\{ session.minutes \}\}/g, '30')
  .replace(/\{\{ attempt.revision \}\}/g, '0')
  .replace(/\{\{ csrf_token \}\}/g, 'test-csrf');

function setup() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'', hidden:false, disabled:false,
      dataset:{}, classList:{toggle(){}}, querySelectorAll:()=>[]});
    return elements.get(id);
  };
  get('qdata').textContent = JSON.stringify([{question:'Q1',options:['A','B']},{question:'Q2',options:['A','B']}]);
  get('saved-answers').textContent = '[1,null]';
  get('saved-events').textContent = '[]'; get('saved-stats').textContent = '{}';
  const calls = [], instances = [], navigations = [];
  let ended = false, failSubmit = false;
  class FakeProctor {
    constructor() {this.state='ok';this.mode='monitoring';instances.push(this);this.stops=0;}
    async start() {}
    snapshot() {return {events:[],stats:{}};}
    stop() {if (!this.final) {this.stops++;this.final=this.snapshot();this.state='stopped';} return this.final;}
    setMode(mode) {this.mode=mode;}
  }
  const context = vm.createContext({
    FakeProctor, URLSearchParams, Date, Math, String, Array, JSON, Promise, encodeURIComponent,
    AbortSignal: {timeout:()=>undefined}, confirm:()=>true,
    location:{search:''},
    document:{getElementById:get, querySelectorAll:()=>[], addEventListener(){}},
    window:{location:{replace:url=>navigations.push(url)},addEventListener(){}},
    setInterval:()=>1,clearInterval(){},
    fetch:async (url, options={}) => {
      const body=options.body ? JSON.parse(options.body) : null;
      calls.push({url,body,options});
      if (url.includes('exam_submit') && failSubmit) throw new Error('offline');
      return {ok:true,json:async()=>({mode:'monitoring',ended,finished:ended||url.includes('exam_submit'),
        deadline:new Date(Date.now()+1800000).toISOString(),began_at:new Date().toISOString(),revision:body?.revision||0})};
    },
  });
  vm.runInContext(script,context);
  return {elements,get,calls,instances,navigations,context,
    end:()=>{ended=true;},fail:()=>{failSubmit=true;},recover:()=>{failSubmit=false;}};
}

// Flush promises initiated by the click handler without adding timer delays.
const settle = async s => {await vm.runInContext('syncChain',s.context);};

test('sharing start registers a server timer and immediately autosaves restored answers',async()=>{
  const s=setup();await s.get('begin').onclick();await settle(s);
  assert.ok(s.calls.some(c=>c.url.includes('exam_start')));
  const save=s.calls.find(c=>c.url.includes('exam_checkpoint'));
  assert.deepEqual(save.body.answers,[1,null]);
  assert.equal(save.body.revision,1);
  assert.equal(s.get('exam').hidden,false);
});

test('an ended control response stops monitoring and opens the frozen student report',async()=>{
  const s=setup();await s.get('begin').onclick();await settle(s);s.end();
  await vm.runInContext('heartbeat()',s.context);await settle(s);
  assert.equal(s.instances[0].state,'stopped');
  assert.ok(s.navigations.at(-1).includes('exam_report'));
  assert.equal(s.calls.filter(c=>c.url.includes('exam_submit')).length,0);
});

test('an exam ended before sharing never starts the student timer',async()=>{
  const s=setup();s.end();await vm.runInContext('heartbeat()',s.context);
  assert.ok(s.navigations.at(-1).includes('exam_report'));
  assert.equal(s.calls.filter(c=>c.url.includes('exam_start')).length,0);
});

test('failed submission freezes answers and retries the same report without restarting monitoring',async()=>{
  const s=setup();await s.get('begin').onclick();await settle(s);s.fail();
  await s.get('submit').onclick();
  assert.equal(s.get('submit').textContent,'Retry submission');
  assert.equal(s.instances[0].stops,1);
  assert.equal(s.navigations.length,0);
  s.recover();await s.get('submit').onclick();
  const submits=s.calls.filter(c=>c.url.includes('exam_submit'));
  assert.equal(submits.length,2);
  assert.deepEqual(submits[0].body.report,submits[1].body.report);
  assert.deepEqual(submits[0].body.answers,submits[1].body.answers);
  assert.equal(s.instances[0].stops,1);
  assert.ok(s.navigations.at(-1).includes('exam_report'));
});
