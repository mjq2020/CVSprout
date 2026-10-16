import assert from 'node:assert/strict';
import { createGenerationPreviewQueue } from '../static/dataset-generation.js';

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const calls = [], results = [], errors = [], pending = [];
let live = true, active = 0, maximum = 0;
const queue = createGenerationPreviewQueue({
  delay: 10,
  isCurrent: () => live,
  onPending: job => pending.push(job.id),
  onResult: (result, job) => results.push([job.id, result]),
  onError: (error, job) => errors.push([job.id, error.message]),
  request: (job, signal) => {
    active += 1; maximum = Math.max(maximum, active);
    return new Promise((resolve, reject) => calls.push({ id: job.id, signal,
      resolve: result => { active -= 1; resolve(result); },
      reject: error => { active -= 1; reject(error); },
    }));
  },
});
queue.schedule({id:'first'}); queue.schedule({id:'second'}); queue.schedule({id:'latest'});
await pause(30);
assert.deepEqual(calls.map(call => call.id), ['latest'], 'rapid changes should debounce into one request');
calls[0].resolve('fresh'); await pause(0);
assert.deepEqual(results, [['latest', 'fresh']]);
queue.schedule({id:'slow'}, {immediate:true}); await pause(0);
queue.schedule({id:'obsolete'}); queue.schedule({id:'newest'}); await pause(30);
assert.equal(calls.length, 2, 'new input must wait for the existing server request');
assert.equal(calls[1].signal.aborted, false, 'parameter changes must not restart server work');
calls[1].resolve('stale'); await pause(0);
assert.deepEqual(calls.map(call => call.id), ['latest','slow','newest']);
assert.equal(results.length, 1, 'late old response must not replace the current preview');
calls[2].reject(new Error('offline')); await pause(0);
assert.deepEqual(errors, [['newest', 'offline']]);
queue.schedule({id:'cancelled'}); queue.cancel(); await pause(20);
assert.equal(calls.length, 3, 'leaving the preview step cancels pending debounce');
queue.schedule({id:'leaving'}, {immediate:true}); await pause(0); queue.cancel();
assert.equal(calls[3].signal.aborted, false, 'step navigation should let current server work finish');
calls[3].resolve('ignored'); await pause(0); assert.equal(results.length, 1);
queue.schedule({id:'closing'}, {immediate:true}); await pause(0); queue.dispose();
assert.equal(calls[4].signal.aborted, true, 'closing must abort the client wait');
calls[4].resolve('after-close'); await pause(0); queue.schedule({id:'after-dispose'}, {immediate:true});
assert.equal(calls.length, 5); assert.equal(results.length, 1); assert.equal(maximum, 1);
live = false;

const timedCalls = [], timedResults = [], timedErrors = [];
const timedQueue = createGenerationPreviewQueue({
  delay: 1, timeout: 20, isCurrent: () => true, onPending: () => {},
  onResult: (result, job) => timedResults.push([job.id, result]),
  onError: (error, job) => timedErrors.push([job.id, error.message]),
  request: (job, signal) => new Promise(resolve => timedCalls.push({job, signal, resolve})),
});
timedQueue.schedule({id:'timeout-current'}, {immediate:true}); await pause(35);
assert.equal(timedCalls[0].signal.aborted, true, 'timeout must cancel the client wait');
assert.match(timedErrors[0][1], /超时/);
timedQueue.schedule({id:'timeout-old'}, {immediate:true}); await pause(0);
timedQueue.schedule({id:'after-timeout'}, {immediate:true}); await pause(25);
assert.equal(timedCalls[1].signal.aborted, true);
assert.equal(timedCalls[2].job.id, 'after-timeout', 'timeout must release the queue for the latest setting');
assert.equal(timedErrors.length, 1, 'an outdated timeout must not replace current feedback');
timedCalls[2].resolve('recovered'); await pause(0);
timedCalls[0].resolve('late'); timedCalls[1].resolve('also-late'); await pause(0);
assert.deepEqual(timedResults, [['after-timeout','recovered']]);
timedQueue.dispose();
console.log('PASS preview debounce, serialized latest request, stale response rejection, timeout recovery, error propagation and close cleanup');
