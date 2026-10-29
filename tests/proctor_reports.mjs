import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
const source = await readFile(new URL('../quiz/static/quiz/proctor.js', import.meta.url), 'utf8');
const {Proctor} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

function running() {
  const p = new Proctor({});
  p.startedAt = Date.now() - 60000;
  p._modeSince = Date.now() - 10000;
  p.mode = 'monitoring'; p.state = 'ok';
  return p;
}

test('autosave snapshot keeps monitoring running and includes live statistics', () => {
  const p = running();
  p.frames = 20;
  p.events = [{type:'other_window_visible', seconds:12}];
  const report = p.snapshot();
  assert.equal(p.state, 'ok');
  assert.equal(report.stats.frames_checked, 20);
  assert.equal(report.stats.confirmed_events, 1);
  assert.equal(report.stats.flagged_seconds, 12);
  report.events[0].seconds = 99;
  assert.equal(p.events[0].seconds, 12);
});

test('pending tab absence appears once in each snapshot without duplicating the live log', () => {
  const p = running(); p.hiddenSince = Date.now() - 15000;
  assert.equal(p.snapshot().stats.tab_aways, 1);
  assert.equal(p.snapshot().events.at(-1).type, 'left_exam_tab');
  assert.equal(p.snapshot().events.length, 1);
  assert.equal(p.events.length, 0);
});

test('permitted time and windows are included without adding a monitoring flag', () => {
  const p = running(); p.mode = 'permitted'; p.detectedSince = Date.now() - 5000;
  const report = p.snapshot();
  assert.equal(report.events.at(-1).type, 'permitted_window_visible');
  assert.equal(report.stats.confirmed_events, 0);
  assert.ok(report.stats.permitted_s >= 10);
  assert.ok(report.stats.monitored_s < report.stats.duration_s);
  assert.equal(p.permittedMs, 0);
});

test('submission retries reuse a frozen report and stop screen sharing once', () => {
  const p = running(); let stopped = 0;
  p.stream = {getTracks: () => [{stop: () => stopped++}]};
  const first = p.stop();
  assert.equal(p.stop(), first);
  assert.equal(p.state, 'stopped'); assert.equal(stopped, 1);
  assert.equal(p.stream, null);
});

test('an in-flight classifier result cannot add events after the exam has stopped', async () => {
  const p = running(); p.video = {readyState:2};
  p._classify = async () => {p.stop(); return {p:1,label:'window'};};
  await p._tick();
  assert.equal(p.state, 'stopped');
  assert.equal(p.frames, 0); assert.equal(p.events.length, 0);
});
