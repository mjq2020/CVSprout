import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/setup-ui.js', import.meta.url), 'utf8').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const helper = fs.readFileSync(new URL('../static/model-preparation-ui.js', import.meta.url), 'utf8').replace(/^export /gm, '');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; };
const qwen = { id: 'qwen', name: 'Qwen', size_bytes: 2 * 1024 ** 3, installed: false, state: { status: 'missing' } };
const sam = { id: 'sam', name: 'SAM', download_policy: 'manual', preparation: { mode: 'manual', directory: '/models/sam', files: ['sam.pt'], missing_files: ['sam.pt'] }, state: { status: 'missing' } };
function fixture() {
  const timers = new Map(), calls = [];
  let current = true, handler, nextTimer = 0;
  const matches = (node, selector) => {
    if (selector === '*') return true;
    if (selector.startsWith('.')) return String(node.attrs.class || '').split(/\s+/).includes(selector.slice(1));
    const tag = selector.match(/^[a-z]+/)?.[0]; if (tag && node.tag !== tag) return false;
    for (const [, key, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) if (!(key in node.attrs) || value !== undefined && node.attrs[key] !== value) return false;
    return true;
  };
  class Element {
    constructor(tag = 'div', attrs = {}) { this.tag=tag; this.attrs=attrs; this.children=[]; this.textContent=''; this.isConnected=true; this.hidden='hidden' in attrs; this.disabled='disabled' in attrs; this.dataset=Object.fromEntries(Object.entries(attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v])); }
    set innerHTML(html) {
      this.html=html; this.children=[]; const stack=[this];
      for (const [, closing, tag, raw] of html.matchAll(/<(\/?)([a-z][\w-]*)([^>]*)>/g)) {
        if (closing) { for (let i=stack.length-1;i>0;i--) if(stack[i].tag===tag){stack.length=i;break;} continue; }
        const attrs={};for(const [,key,value]of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[key]=value??'';
        const node=new Element(tag,attrs);stack.at(-1).children.push(node);if(!['br','input','img'].includes(tag))stack.push(node);
      }
    }
    get innerHTML(){return this.html || '';}
    querySelectorAll(selector){return this.children.flatMap(node=>[node,...node.querySelectorAll('*')]).filter(node=>matches(node,selector));}
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  }
  const container=new Element();
  const context=vm.createContext({URL,AbortController,setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(helper+'\n'+source,context);
  const controller=context.mountModelDownloads(container,{escapeHtml:esc,isCurrent:()=>current,api:async(path,options={})=>{const call={path,...options};calls.push(call);return handler ? handler(call) : [qwen,sam];}});
  return {container,controller,timers,calls,set handler(value){handler=value;},set current(value){current=value;},q:selector=>container.querySelector(selector),async tick(){const callbacks=[...timers.values()];timers.clear();callbacks.forEach(fn=>fn());await settle();}};
}
// SAM exposes an inspect-only action even if a caller invokes the handler directly.
{
  const h=fixture();await settle();const manual=h.q('[data-download-row="sam"]');
  assert.equal(manual.querySelector('[data-download-action]').textContent,'重新检查文件');assert(manual.querySelector('[data-download-files]').innerHTML.includes('/models/sam'));assert(manual.querySelector('[data-download-files]').innerHTML.includes('sam.pt'));
  await manual.querySelector('[data-download-action]').onclick();assert(h.calls.every(call=>!call.method || call.method==='GET'));
}
// Refresh changes only status text/buttons, keeps native nodes/details, and ends polling at completion.
{
  const h=fixture();await settle();const row=h.q('[data-download-row="qwen"]'),button=row.querySelector('[data-download-action]'),manual=h.q('[data-download-row="sam"]');let downloading=false;
  h.handler=call=>{if(call.method==='POST'){downloading=true;return {status:'downloading'};}return[{...qwen,state:{status:downloading?'downloading':'complete',downloaded:1024**3,total:2*1024**3},installed:!downloading},sam];};
  await button.onclick();assert.equal(h.calls.filter(call=>call.method==='POST').length,1);assert.equal(button.disabled,true);assert.equal(h.timers.size,1);
  await h.tick();assert.equal(h.q('[data-download-row="qwen"]'),row);assert.equal(h.q('[data-download-row="sam"]'),manual);assert.equal(row.querySelector('[data-download-action]'),button);assert(row.querySelector('[data-download-status]').textContent.includes('50%'));
  downloading=false;await h.tick();assert.equal(button.textContent,'校验模型');assert.equal(h.timers.size,0);
}
// Rapid clicks share one request; failed POST remains visible without losing the list.
{
  const h=fixture();await settle();const button=h.q('[data-download-row="qwen"]').querySelector('[data-download-action]'),gate=deferred();h.handler=()=>gate.promise;
  const request=button.onclick();await button.onclick();assert.equal(h.calls.filter(call=>call.method==='POST').length,1);
  gate.reject(Error('下载服务暂不可用'));await request;assert.equal(h.q('[data-download-error]').textContent,'下载服务暂不可用');assert.equal(h.q('[data-download-error]').hidden,false);assert.equal(button.disabled,false);
}
// A failed GET has an explicit read-only retry; late results cannot repaint after close or navigation.
{
  const h=fixture();await settle();h.handler=()=>{throw Error('目录暂不可用');};await h.controller.refresh();assert(h.q('[data-download-retry]'));h.handler=()=>[qwen,sam];h.q('[data-download-retry]').onclick();await settle();assert.equal(h.q('[data-download-error]').hidden,true);
}
for(const boundary of ['dispose','route','disconnect']){
  const h=fixture();await settle();const row=h.q('[data-download-row="qwen"]'),gate=deferred();h.handler=()=>gate.promise;const request=h.controller.refresh();
  if(boundary==='dispose')h.controller.dispose();else if(boundary==='route')h.current=false;else h.container.isConnected=false;
  gate.resolve([{...qwen,state:{status:'downloading'}},sam]);await request;assert.equal(h.q('[data-download-row="qwen"]'),row);assert.equal(h.timers.size,0);if(boundary==='dispose')assert.equal(h.calls.at(-1).signal.aborted,true);
}
console.log('PASS download panel: manual SAM inspect-only policy, supported downloads, progress/completion, stable nodes, duplicate suppression, inline failures/retry and stale/closed session guards');
