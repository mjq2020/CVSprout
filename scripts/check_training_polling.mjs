import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const refreshCode = extract('async function refreshPipelineData(', '\nfunction trainingHasActiveWork(');
const pollCode = extract('function trainingHasActiveWork(', '\nfunction bindCommonProjectEvents(');
const applyCode = extract('function applyTrainingListResponse(', '\nasync function loadTrainingList(');
const logsCode = extract('async function loadTrainingLogs(', '\nfunction trainingSyncMarkup(');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
const run = (id, status = 'queued', epoch = 0) => ({id,project_id:1,status,current_epoch:epoch});
function harness() {
  const state = {project:{id:1},selectionEpoch:1,session:{username:'member'},currentSection:'train',trainingPage:{request:1,page:1},trainingFilters:{query:'',status:'all'},trainingSelectionRequest:1,selectedTrainingId:1,selectedTrainingRun:run(1),training:{runs:[run(1)],counts:{active:1},availability:{available:true}},trainingMetricsHistory:{1:{fetchedAt:1}},trainingLogs:{}};
  const calls = [], renders = [], auxiliary = [];
  const globals = {state,Date,AbortController,Promise,Map,console,document:{hidden:false,querySelector:()=>null},trainingSearchTimer:null,pipelineRefreshRequest:null,pipelineRefreshSerial:0,pipelinePollingKey:'',pipelinePollNow:null,trainingListURL:(_id,page,filters)=>`/list?page=${page}&q=${filters.query}`,monitoredTrainingRun:()=>state.selectedTrainingRun?.id===state.selectedTrainingId?state.selectedTrainingRun:null,rememberTrainingNavigation:()=>{},renderTrainingPage:()=>renders.push({status:state.selectedTrainingRun?.status,id:state.selectedTrainingRun?.id,epoch:state.selectedTrainingRun?.current_epoch}),pipelineApi:path=>{const d=deferred();calls.push({path,...d});return d.promise;},loadRunConversions:(id)=>{auxiliary.push(['conversions',id]);return new Promise(()=>{});},loadTrainingMetrics:(id)=>{auxiliary.push(['metrics',id]);return new Promise(()=>{});},loadTrainingLogs:(id)=>{auxiliary.push(['logs',id]);return new Promise(()=>{});},notify:()=>{}};
  const context = vm.createContext(globals);
  vm.runInContext(applyCode+'\n'+refreshCode+'\nglobalThis.refresh=refreshPipelineData;',context);
  const answerList = (status='running',epoch=1) => calls.find(call=>call.path.startsWith('/list')).resolve({runs:[run(1,status,epoch)],counts:{active:status==='completed'?0:1},pagination:{page:1,total:1}});
  return {state,context,calls,renders,auxiliary,answerList,refresh:context.refresh};
}
{
  const h=harness(), request=h.refresh();
  h.calls.find(c=>c.path.endsWith('/1')).resolve(run(1,'running',1));await tick();
  assert(h.renders.some(r=>r.status==='running'),'Status must render while list and conversions remain pending');
  assert(h.auxiliary.some(([kind])=>kind==='metrics'));
  h.answerList();assert.equal(await request,true);
  assert.equal(h.state.training.availability.available,true,'Lightweight lists retain environment availability');
  assert(h.calls[0].path.endsWith('include_availability=false'));
}
{
  const h=harness(), a=h.refresh(), b=h.refresh();assert.equal(h.calls.length,2,'Concurrent refreshes share the same two requests');
  h.answerList();h.calls[1].resolve(run(1,'running'));await Promise.all([a,b]);
}
{
  const h=harness(), old=h.refresh();
  h.state.trainingSelectionRequest++;h.state.selectedTrainingId=2;h.state.selectedTrainingRun=run(2);
  const fresh=h.refresh();h.calls[2].resolve({runs:[run(2,'completed')],counts:{active:0},pagination:{page:1,total:1}});h.calls[3].resolve(run(2,'completed'));await fresh;
  h.calls[0].resolve({runs:[run(1)],counts:{active:1},pagination:{page:1,total:1}});h.calls[1].resolve(run(1));await old;
  assert.equal(h.state.selectedTrainingRun.id,2);assert.equal(h.state.selectedTrainingRun.status,'completed');assert.equal(h.state.training.runs[0].id,2);
}
{
  const h=harness();h.state.trainingPage.loading=true;const request=h.refresh();assert.equal(h.calls.length,1,'Changing list filters must not pause selected run progress');
  h.calls[0].resolve(run(1,'running',3));await request;assert.equal(h.state.selectedTrainingRun.current_epoch,3);
}
{
  const h=harness(), request=h.refresh();h.calls[0].reject(new Error('list offline'));h.calls[1].resolve(run(1,'running'));await assert.rejects(request);
  assert.equal(h.state.selectedTrainingRun.status,'running');assert.equal(h.state.pipelineConnectionLost,true);
  const retry=h.refresh();h.calls[2].resolve({runs:[run(1,'completed')],counts:{active:0},pagination:{page:1,total:1}});h.calls[3].resolve(run(1,'completed'));await retry;
  assert.equal(h.state.pipelineConnectionLost,false);
}
{
  const h=harness(), request=h.refresh();h.state.selectionEpoch++;h.state.currentSection='data';const snapshot=JSON.stringify(h.state);
  h.calls[0].resolve({runs:[],counts:{active:0}});h.calls[1].resolve(run(1,'completed'));await request;assert.equal(JSON.stringify(h.state),snapshot,'Responses after leaving training must be ignored');
}
{
  const h=harness();let time=20000, updates=0, interval;
  h.state.training.counts.active=0;h.state.selectedTrainingRun=run(1,'completed');
  Object.assign(h.context,{Date:{now:()=>time},setInterval:callback=>{interval=callback;return 1;},clearInterval:()=>{}});
  vm.runInContext(pollCode+'\nglobalThis.start=startPipelinePolling;',h.context);
  h.context.refreshPipelineData=async()=>{updates++;return true;};h.context.start();await tick();assert.equal(updates,1,'Opening an idle page must sync immediately');
  time+=3000;interval();await tick();assert.equal(updates,1);
  time+=12000;interval();await tick();assert.equal(updates,2,'Idle page must continue to discover other members\' new work');
  h.state.training.counts.active=1;time+=3000;interval();await tick();assert.equal(updates,3);
  h.context.document.hidden=true;interval();await tick();assert.equal(updates,3);
  h.context.document.hidden=false;await h.context.pipelinePollNow(true);assert.equal(updates,4,'Returning to the page refreshes immediately');
}
{
  const h=harness();vm.runInContext(logsCode+'\nglobalThis.logs=loadTrainingLogs;',h.context);
  const request=h.context.logs(1);h.state.selectionEpoch++;h.state.currentSection='data';h.calls[0].resolve({logs:[{id:1,message:'old'}]});await request;
  assert.equal(h.state.trainingLogs[1].loading,false,'Leaving while logs load must release the stale loading flag');
  h.state.currentSection='train';const next=h.context.logs(1);assert.equal(h.calls.length,2);h.calls[1].resolve({logs:[{id:2,message:'new'}]});await next;assert.equal(h.state.trainingLogs[1].items[0].id,2);
}
{
  let timeout, cleared=false, options;
  const context=vm.createContext({AbortController,setTimeout:cb=>{timeout=cb;return 7;},clearTimeout:id=>{cleared=id===7;},api:(_path,opts)=>{options=opts;return new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('timeout'))));}});
  vm.runInContext(extract('async function pipelineApi(', '\nfunction monitoredTrainingRun(')+'\nglobalThis.get=pipelineApi;',context);
  const pending=context.get('/status');timeout();await assert.rejects(pending);assert.equal(options.cache,'no-store');assert(cleared,'Timeout must release its timer');
}
{
  const h=harness();h.state.training.availability={available:false,reason:'Previously unavailable'};
  Object.assign(h.context,{trainingEnvironmentRequest:null,normalizeTrainingCatalog:raw=>raw,normalizeTrainingResources:raw=>raw});
  vm.runInContext(extract('async function refreshTrainingEnvironment(', '\nasync function refreshTrainingWorkspace(')+'\nglobalThis.environment=refreshTrainingEnvironment;',h.context);
  const pending=h.context.environment(), duplicate=h.context.environment();assert.equal(h.calls.length,2,'Environment checks are shared while running');
  h.calls[1].resolve({worker:{online_count:1}});await tick();assert.equal(h.state.trainingEnvironmentLoading,true);
  h.calls[0].resolve({backend:{available:true,reason:''},models:[{available:true}],conversion_formats:{}});
  await Promise.all([pending,duplicate]);assert.equal(h.state.training.availability.available,true,'A recovered environment must re-enable training readiness');assert.equal(h.state.trainingEnvironmentLoading,false);
  const late=h.context.environment();h.state.selectionEpoch++;h.state.trainingEnvironmentLoading=false;
  const snapshot=JSON.stringify(h.state);h.calls[2].resolve({backend:{available:false}});h.calls[3].resolve({});await late;
  assert.equal(JSON.stringify(h.state),snapshot,'An old environment response must not replace the new page state');
}
for (const catalogFirst of [true,false]) for (const variant of ['catalogOlder','resourcesOlder','untimed','equal']) {
  const h=harness();
  Object.assign(h.context,{trainingEnvironmentRequest:null,normalizeTrainingCatalog:raw=>raw,normalizeTrainingResources:raw=>raw});
  vm.runInContext(extract('async function refreshTrainingEnvironment(', '\nasync function refreshTrainingWorkspace(')+'\nglobalThis.environment=refreshTrainingEnvironment;',h.context);
  const early='2026-10-16T06:00:00Z', late='2026-10-16T06:00:01Z';
  const catalogResources={worker:{online_count:0},snapshot_at:variant==='untimed'?undefined:variant==='catalogOlder'?early:late};
  const resources={worker:{online_count:1},snapshot_at:variant==='untimed'?undefined:variant==='resourcesOlder'?early:late};
  const pending=h.context.environment();
  const responses=[()=>h.calls[0].resolve({backend:{available:true},resources:catalogResources}),()=>h.calls[1].resolve(resources)];
  responses[catalogFirst?0:1]();await tick();responses[catalogFirst?1:0]();await pending;
  assert.equal(h.state.trainingResources.worker.online_count,variant==='resourcesOlder'?0:1,`Newest resource snapshot wins regardless of delivery order (${catalogFirst}, ${variant}); untimed/equal snapshots prefer dedicated resources`);
}
console.log('PASS independent status rendering, lightweight metadata merge, coalescing, stale selection/page guards, retry, idle/focus polling, late logs cleanup, bounded requests and resource snapshot ordering');
