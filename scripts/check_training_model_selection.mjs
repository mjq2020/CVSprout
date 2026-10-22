// Execute the actual modal/controller, model normalization, and payload construction.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const extract = (start, end) => { const a=source.indexOf(start), b=source.indexOf(end,a); assert(a>=0&&b>a); return source.slice(a,b); };
const code = [extract('const TRAINING_TASK_META =', '\nconst trainingBackgroundConversions'), extract('function openTrainingModal(', '\nfunction openConversionModal('), extract('function trainingRunDetails(', '\nfunction conversionRow(')].join('\n');
const deferred = () => { let resolve,reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
const settle = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
const builtin = (task='detect') => ({id:`base-${task}.pt`,model_arch:`base-${task}.pt`,name:`基础 ${task}`,task,source:'builtin',family:'YOLO',available:true,installed:false,exists:false,loadable:false,supports_scratch:true,recommended:true,size_bytes:12345,sha256:'remote-pin',defaults:{epochs:50,batch_size:16}});
const custom = (overrides={}) => ({id:'own.pt',model_arch:'own.pt',name:'自有模型',task:'detect',source:'custom',sha256:'a'.repeat(64),family:'YOLO',available:true,installed:true,loadable:true,supports_scratch:true,architecture:{backbone:[]},defaults:{epochs:999,batch_size:64},...overrides});
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fixture({admin=true,models=[builtin()],template=null}={}) {
  const state={project:{id:7,name:'测试项目'},currentSection:'train',selectionEpoch:1,session:{role:admin?'admin':'reviewer'},versions:[{id:3,annotation_count:4,asset_count:4,stats:{task_type:'detect'}},{id:4,annotation_count:4,stats:{task_type:'segment'}}],training:{total:0,availability:{available:true}},trainingResources:{default_device:'cpu',devices:[{id:'cpu',name:'CPU'}]}};
  const ui={form:null,opened:[],notifications:[],closed:0,revealed:[]}, server={calls:[],handler:null,catalog:null};
  const document={activeElement:null,querySelector:selector=>selector==='.modal'?ui.form:ui.form?.querySelector(selector)};
  class Element {
    constructor(tag='div',attrs={}){this.tag=tag;this.attrs=attrs;this.id=attrs.id;this.name=attrs.name;this.value=attrs.value??'';this._checked='checked'in attrs;this.disabled='disabled'in attrs;this.hidden='hidden'in attrs;this.isConnected=true;this.children=[];this.textContent='';this.files=[];this.html='';this.classes=new Set(String(attrs.class||'').split(/\s+/).filter(Boolean));this.classList={toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};}
    get checked(){return this._checked;}
    set checked(value){this._checked=value;if(value&&this.name&&this.attrs.type==='radio')ui.form?.querySelectorAll(`input[name="${this.name}"]`).forEach(node=>{if(node!==this)node._checked=false;});}
    setAttribute(name,value){this.attrs[name]=value;}
    set innerHTML(html){this.html=html;this.children=parse(html);this.children.forEach(node=>node.parent=this);}
    get innerHTML(){return this.html;}
    querySelectorAll(selector){const nodes=this.children.flatMap(n=>[n,...n.querySelectorAll('*')]); if(selector==='*')return nodes;return nodes.filter(n=>matches(n,selector));}
    querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
    closest(selector){let node=this;while(node){if(matches(node,selector))return node;node=node.parent;}return null;}
    focus(){document.activeElement=this;}
    addEventListener(type,fn){const previous=this[`on${type}`];this[`on${type}`]=event=>{previous?.(event);fn(event);};}
  }
  function matches(node,selector){if(selector[0]==='.')return node.classes.has(selector.slice(1));if(selector[0]==='#')return node.id===selector.slice(1);const tag=selector.match(/^[a-z]+/)?.[0];if(tag&&node.tag!==tag)return false;for(const [,key,value]of selector.matchAll(/\[([\w-]+)="([^"]*)"\]/g))if(node.attrs[key]!==value)return false;return !selector.includes(':checked')||node.checked;}
  function parse(html){
    const root={children:[]},stack=[root];
    for(const match of html.matchAll(/<(\/?)([a-z][\w-]*)([^>]*)>/g)){
      const[,closing,tag,raw]=match;
      if(closing){for(let i=stack.length-1;i>0;i--)if(stack[i].tag===tag){stack.length=i;break;}continue;}
      const attrs={};for(const attr of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[attr[1]]=attr[2]??'';
      const node=new Element(tag,attrs);node.parent=stack.at(-1);node.parent.children.push(node);
      if(!['input','br','hr','img','meta','link'].includes(tag)&&!raw.trimEnd().endsWith('/'))stack.push(node);
    }
    const visit=node=>{node.children.forEach(visit);if(node.tag==='select'){const options=node.querySelectorAll('option');node.value=(options.find(n=>'selected'in n.attrs)||options[0])?.value??'';}};
    root.children.forEach(visit);return root.children;
  }

  const context=vm.createContext({state,document,console,Promise,Map,AbortController,CSS:{escape:value=>value},FormData:class{constructor(){this.entries=[];}append(...args){this.entries.push(args);}},isAdmin:()=>state.session.role==='admin',canReview:()=>['reviewer','admin'].includes(state.session.role),escapeHtml:esc,icon:()=>'',formatBytes:v=>`${v} B`,formatCompactNumber:String,formatDateTime:()=>'',renderTrainingLogConsole:()=>'',splitAssetCount:()=>1,trainingTaskChoices:task=>['detect','segment'].map(id=>`<input type="radio" name="train-task" value="${id}" ${task===id?'checked':''}>`).join(''),trainingDeviceChoices:()=>'<input type="radio" name="train-device" value="cpu" checked>',trainingResourceProfileOptions:()=>'<option value="balanced">均衡</option>',availableConversionFormats:()=>[],trainingContextGuard:()=>{const {project,selectionEpoch,session}=state;return()=>state.project===project&&state.selectionEpoch===selectionEpoch&&state.session===session&&state.currentSection==='train';},notify:(...args)=>ui.notifications.push(args),revealNewTraining:async id=>ui.revealed.push(id),jsonOptions:(method,payload)=>({method,body:JSON.stringify(payload)}),closeModal:()=>{ui.form?.config.onClose?.();if(ui.form)ui.form.isConnected=false;ui.form=null;ui.closed++;},openModal:config=>{ui.form?.config.onClose?.();if(ui.form)ui.form.isConnected=false;const form=new Element();form.config=config;form.innerHTML=config.body;form.children.push(new Element('button',{id:'modal-submit'}));ui.form=form;ui.opened.push(form);config.onReady();},api:async(path,options={})=>{const call={path,...options};server.calls.push(call);if(server.handler){const result=server.handler(call);if(result!==undefined)return await result;}if(path.includes('training-compatibility'))return{tasks:{detect:{compatible:true},segment:{compatible:true}}};if(path==='/api/model-downloads/import/checkpoint')return{filename:'own.pt',sha256:'a'.repeat(64)};if(path==='/api/training/catalog')return server.catalog;if(path.endsWith('/training-runs'))return{id:18};throw Error(`Unhandled ${path}`);}});
  vm.runInContext(code,context);
  const catalog=(items)=>({models:items,tasks:{detect:{available:true},segment:{available:true}},backend:{available:true}});
  state.trainingCatalog=context.normalizeTrainingCatalog(catalog(models));server.catalog=catalog([...models,custom()]);
  const open=(run=template)=>{context.openTrainingModal(run);return ui.form;};
  const select=(name,value)=>{const nodes=ui.form.querySelectorAll(`input[name="${name}"]`);const node=nodes.find(n=>n.value===value);assert(node,`Missing ${name}=${value}`);nodes.forEach(n=>n.checked=n===node);node.onchange?.();return node;};
  const file=(name='own.pt',size=2048)=>{const input=ui.form.querySelector('#train-checkpoint-file');input.files=[{name,size}];input.onchange?.();};
  return{state,context,ui,server,open,select,file,catalog,document,q:selector=>ui.form?.querySelector(selector)};
}

// Explicit available overrides absent installed weights; local compatibility defaults remain safe.
{
 const h=fixture(),m=h.state.trainingCatalog.models[0];assert.equal(m.available,true);assert.equal(m.installed,false);assert.equal(m.loadable,false);assert.equal(m.source,'builtin');
 assert.equal(h.context.normalizeTrainingModel({id:'old.pt',exists:false}).available,false);
 assert.equal(h.context.normalizeTrainingModel({id:'old.pt',available:true}).source,'local');
 assert.equal(h.context.normalizeTrainingModel({id:'old.pt',available:true}).supports_scratch,false);
 const markup=h.context.trainingModelPickerCard({...m,name:'<script>bad()</script>'},true);assert(!markup.includes('<script>'));assert(!markup.includes('disabled'));assert(markup.includes('首次训练自动下载'));
}
// Defaults, actual payload and scratch mode don't need a downloaded checkpoint or reset user values.
for(const pretrained of [true,false]){
 const h=fixture(),form=h.open();await settle();assert.equal(h.q('#train-pretrained').checked,true);assert.equal(h.q('#modal-submit').disabled,false);
 h.q('#train-name').value='保留名称';h.q('#train-epochs').value='77';h.q('#train-batch').value='3';
 if(!pretrained){h.q('#train-pretrained').checked=false;h.q('#train-pretrained').onchange();await settle();assert(h.q('#train-model-hint').textContent.includes('随机初始化，不下载'));}
 assert.equal(h.q('#train-epochs').value,'77');assert.equal(h.q('#train-batch').value,'3');
 await form.config.onSubmit();const request=h.server.calls.find(c=>c.path.endsWith('/training-runs'));const payload=JSON.parse(request.body);assert.equal(payload.pretrained,pretrained);assert.equal(payload.model_arch,'base-detect.pt');assert.equal(payload.epochs,77);assert.equal(payload.name,'保留名称');assert.equal(h.ui.revealed[0],18);
}
// Cloning honors false. Unsupported scratch never silently becomes pretrained or submits.
for(const support of [true,false]){
 const model=custom({supports_scratch:support}),h=fixture({models:[model],template:{model_arch:'own.pt',version_id:3,settings:{pretrained:false,epochs:87,batch_size:2}}});const form=h.open();await settle();assert.equal(h.q('#train-pretrained').checked,false);assert.equal(h.q('#train-epochs').value,'87');assert.equal(h.q('#modal-submit').disabled,!support);
 if(!support){await assert.rejects(form.config.onSubmit(),/无法从零训练/);assert(!h.server.calls.some(c=>c.path.endsWith('/training-runs')));h.q('#train-pretrained').checked=true;h.q('#train-pretrained').onchange();await settle();assert.equal(h.q('#modal-submit').disabled,false);assert.equal(h.q('#train-pretrained').disabled,true);}
}
// Successful upload selects only verified compatible weights, preserving every existing parameter.
{
 const h=fixture(),form=h.open();await settle();h.q('#train-name').value='上传前设置';h.q('#train-epochs').value='91';h.q('#train-imgsz').value='800';h.q('#train-seed').value='99';h.file();await h.q('#train-upload-button').onclick();await settle();
 assert.equal(h.ui.form,form);assert.equal(h.ui.opened.length,1);assert.equal(h.q('input[name="train-model"]:checked').value,'own.pt');for(const[id,value]of[['name','上传前设置'],['epochs','91'],['imgsz','800'],['seed','99']])assert.equal(h.q(`#train-${id}`).value,value);
 assert(h.q('#train-upload-feedback').textContent.includes('已上传并选择'));assert.equal(h.q('#modal-submit').disabled,false);
 assert(h.server.calls.find(c=>c.path.includes('/import/')).body.entries[0][0]==='file');
}
// Unloadable/wrong-task/unsupported-scratch uploads retain the original model and explicit reason.
for(const mode of ['invalid','offtask','scratch']){
 const h=fixture();h.open();await settle();if(mode==='scratch'){h.q('#train-pretrained').checked=false;h.q('#train-pretrained').onchange();await settle();}
 h.server.catalog=h.catalog([builtin(),custom(mode==='invalid'?{available:false,loadable:false,reason:'checkpoint 校验失败'}:mode==='offtask'?{task:'segment'}:{supports_scratch:false})]);h.file();await h.q('#train-upload-button').onclick();await settle();
 assert.equal(h.q('input[name="train-model"]:checked').value,'base-detect.pt');assert(h.q('#train-upload-feedback').classes.has('is-error'));assert(h.q('#train-upload-feedback').textContent.includes(mode==='invalid'?'checkpoint 校验失败':mode==='offtask'?'不兼容':'无法从零训练'));assert.equal(h.q('#train-pretrained').checked,mode!=='scratch');
}
// Upload lock, no duplicate POST, retry catalog only after successful import, and validation failures.
{
 const h=fixture();h.open();await settle();h.file();const gate=deferred();let calls=0;h.server.handler=call=>{if(call.path.includes('/import/')){calls++;return gate.promise;}};
 const pending=h.q('#train-upload-button').onclick();assert.equal(h.q('#modal-submit').disabled,true);await h.q('#train-upload-button').onclick();assert.equal(calls,1);await assert.rejects(h.ui.form.config.onSubmit(),/上传或检查/);gate.resolve({filename:'own.pt',sha256:'a'.repeat(64)});await pending;assert.equal(h.q('#modal-submit').disabled,false);
}
{
 const h=fixture();h.open();await settle();h.file();let fail=true;h.server.handler=call=>{if(call.path==='/api/training/catalog'&&fail)throw Error('服务暂时离线');};await h.q('#train-upload-button').onclick();assert.equal(h.q('#train-upload-button').textContent,'重新检查');assert(h.q('#train-upload-feedback').textContent.includes('已上传'));fail=false;await h.q('#train-upload-button').onclick();assert.equal(h.server.calls.filter(c=>c.path.includes('/import/')).length,1);assert.equal(h.q('input[name="train-model"]:checked').value,'own.pt');
}
for(const[name,size,message]of [['bad.txt',10,'.pt'],['own.pt',0,'为空'],['own.pt',2*1024**3+1,'2 GiB']]){
 const h=fixture();h.open();await settle();h.file(name,size);await h.q('#train-upload-button').onclick();assert(h.q('#train-upload-feedback').textContent.includes(message));assert(!h.server.calls.some(c=>c.path.includes('/import/')));
}
// Matching names do not prove matching uploaded weights; keep the import digest through GET retries.
{
 const h=fixture();h.open();await settle();h.file();h.server.catalog=h.catalog([builtin(),custom({sha256:'b'.repeat(64)})]);await h.q('#train-upload-button').onclick();
 assert.equal(h.q('input[name="train-model"]:checked').value,'base-detect.pt');assert(h.q('#train-upload-feedback').textContent.includes('其他目录中的权重占用'));assert.equal(h.q('#train-upload-button').textContent,'重新检查');
 await h.q('#train-upload-button').onclick();assert.equal(h.q('input[name="train-model"]:checked').value,'base-detect.pt');assert.equal(h.server.calls.filter(c=>c.path.includes('/import/')).length,1);
 h.server.catalog=h.catalog([builtin(),custom()]);await h.q('#train-upload-button').onclick();assert.equal(h.q('input[name="train-model"]:checked').value,'own.pt');assert.equal(h.server.calls.filter(c=>c.path.includes('/import/')).length,1);
}
// Requests resolved after closing, replacement modal, project change or session change are ignored.
for(const boundary of ['close','replace','project','session','epoch']){
 const h=fixture(),old=h.open();await settle();h.file();const gate=deferred();h.server.handler=call=>call.path.includes('/import/')?gate.promise:undefined;const pending=h.q('#train-upload-button').onclick();const catalog=h.state.trainingCatalog;
 if(boundary==='close')h.context.closeModal();else if(boundary==='replace')h.open();else if(boundary==='project')h.state.project={id:8};else if(boundary==='session')h.state.session={role:'admin'};else h.state.selectionEpoch++;
 await settle();const next=h.ui.form,html=next?.innerHTML;gate.resolve({filename:'own.pt',sha256:'a'.repeat(64)});await pending;assert.equal(h.state.trainingCatalog,catalog);assert.equal(h.ui.form,next);assert.equal(next?.innerHTML,html);assert(!h.server.calls.some(c=>c.path==='/api/training/catalog'));await assert.rejects(old.config.onSubmit(),/已过期/);
}
// Compatibility is deduplicated; stale task results cannot enable the current form.
{
 const h=fixture({models:[builtin(),builtin('segment')]});const gate=deferred();h.server.handler=call=>call.path.includes('/versions/3/')?gate.promise:undefined;h.open();h.q('#train-pretrained').checked=false;h.q('#train-pretrained').onchange();assert.equal(h.server.calls.filter(c=>c.path.includes('/versions/3/')).length,1);h.q('#train-version').value='4';h.q('#train-version').onchange();await settle();assert.equal(h.q('input[name="train-task"]:checked').value,'segment');assert.equal(h.q('#modal-submit').disabled,false);gate.resolve({tasks:{detect:{compatible:false}}});await settle();assert.equal(h.q('#modal-submit').disabled,false);
}
// Reviewers can train without upload, and historical records distinguish initialization mode.
{
 const h=fixture({admin:false});h.open();await settle();assert(!h.q('#train-upload-button'));assert(h.ui.form.innerHTML.includes('请联系管理员上传'));assert.equal(h.q('#modal-submit').disabled,false);
 assert(h.context.trainingRunDetails({id:1,settings:{pretrained:false}},{}).includes('从零训练（随机初始化）'));assert(h.context.trainingRunDetails({id:1,settings:{}},{}).includes('使用预训练权重'));
}
// A 60+ model catalog filters only mounted cards; selected model and native inputs remain stable.
const largeCatalog = ['yolov8','yolo11','yolo26'].flatMap((family, index) => ['detect','segment','classify','pose'].flatMap(task => [...'nsmlx'].map(size => {
 const filename = `${family}${size}${({detect:'',segment:'-seg',classify:'-cls',pose:'-pose'})[task]}.pt`;
 return {...builtin(task),id:filename,model_arch:filename,name:filename.slice(0,-3),family:['YOLOv8','YOLO11','YOLO26'][index],recommended:family==='yolo26'&&size==='n'};
}))).concat(custom({id:'park.pt',model_arch:'park.pt',family:'Custom',name:'园区自有检测'}));
const visibleModels = h => h.q('#training-model-picker').querySelectorAll('input[name="train-model"]').filter(input=>!input.closest('label').hidden);
{
 const h=fixture({models:largeCatalog}),form=h.open();await settle();
 const search=h.q('#train-model-search'),family=h.q('#train-model-family'),selected=h.q('input[name="train-model"]:checked'),cards=h.q('#training-model-picker').querySelectorAll('label');
 assert.equal(largeCatalog.length,61);assert.equal(visibleModels(h).length,16);assert.equal(selected.value,'yolo26n.pt');assert(h.q('#train-model-count').textContent.includes('16 / 16'));
 search.focus();search.selectionStart=3;search.value='YOLO11 m';search.oninput({});assert.equal(visibleModels(h).length,1);assert.equal(visibleModels(h)[0].value,'yolo11m.pt');assert.equal(h.q('#train-model-search'),search);assert.equal(h.document.activeElement,search);assert.equal(search.selectionStart,3);assert.equal(h.q('input[name="train-model"]:checked'),selected);assert.equal(h.q('#train-model-selected-note').hidden,false);assert.equal(h.q('#modal-submit').disabled,false);assert.equal(h.server.calls.filter(c=>c.path.includes('compatibility')).length,1);assert.deepEqual(h.q('#training-model-picker').querySelectorAll('label'),cards);
 family.value='YOLOv8';family.onchange();assert.equal(visibleModels(h).length,0);assert.equal(h.q('#train-model-empty').hidden,false);assert.equal(h.q('#train-model-selected-name').textContent,'yolo26n');assert.equal(h.q('#modal-submit').disabled,false);
 h.q('#train-model-reset').onclick();assert.equal(search.value,'');assert.equal(family.value,'');assert.equal(visibleModels(h).length,16);assert.equal(h.document.activeElement,search);
 search.oncompositionstart();search.value='园区';search.oninput({isComposing:true});assert.equal(visibleModels(h).length,16);search.oncompositionend();assert.equal(visibleModels(h).length,1);assert.equal(visibleModels(h)[0].value,'park.pt');
 search.value='missing-模型';search.oninput({});assert.equal(visibleModels(h).length,0);h.q('#train-epochs').value='82';await form.config.onSubmit();assert.equal(JSON.parse(h.server.calls.find(c=>c.path.endsWith('/training-runs')).body).model_arch,'yolo26n.pt');
}
// Task changes preserve search text, reset a no-longer-present family, and use only the current task.
{
 const h=fixture({models:largeCatalog});h.open();await settle();const search=h.q('#train-model-search');search.value='yolo11';search.oninput({});h.q('#train-model-family').value='Custom';h.q('#train-model-family').onchange();
 h.q('#train-version').value='4';h.q('#train-version').onchange();await settle();assert.equal(h.q('#train-model-search'),search);assert.equal(search.value,'yolo11');assert.equal(h.q('#train-model-family').value,'');assert.equal(visibleModels(h).length,5);assert(visibleModels(h).every(input=>input.value.endsWith('-seg.pt')));assert(h.q('#train-model-count').textContent.includes('5 / 15'));
}
// A cloned or newly imported selection survives filtering and remains the actual submission model.
{
 const h=fixture({models:largeCatalog,template:{model_arch:'yolo11x.pt',version_id:3,settings:{pretrained:false,epochs:93,batch_size:2}}}),form=h.open();await settle();h.q('#train-model-search').value='no-match';h.q('#train-model-search').oninput({});assert.equal(h.q('#train-model-selected-name').textContent,'yolo11x');assert.equal(h.q('#train-pretrained').checked,false);assert.equal(h.q('#train-epochs').value,'93');assert.equal(h.q('#modal-submit').disabled,false);
 h.file();await h.q('#train-upload-button').onclick();assert.equal(h.q('#train-model-search').value,'no-match');assert.equal(visibleModels(h).length,0);assert.equal(h.q('#train-model-selected-name').textContent,'自有模型');assert.equal(h.q('#train-model-selected-note').hidden,false);assert.equal(h.q('#train-epochs').value,'93');await form.config.onSubmit();const payload=JSON.parse(h.server.calls.find(c=>c.path.endsWith('/training-runs')).body);assert.equal(payload.model_arch,'own.pt');assert.equal(payload.pretrained,false);
}
// Model advice is visible; only untouched new-form defaults follow model selection, never manual edits or clones.
{
 const special=custom({id:'rtdetr-l.pt',model_arch:'rtdetr-l.pt',name:'RT-DETR L',family:'RT-DETR',defaults:{batch_size:2,image_size:640,amp:false,deterministic:false},constraints:{min_image_size:128},warnings:['固定尺寸训练建议']}),h=fixture({models:[builtin(),special]});h.open();await settle();h.q('#train-batch').value='7';h.q('#train-batch').oninput();h.q('#train-imgsz').value='64';h.q('#train-amp').checked=true;h.q('#train-amp').oninput();h.q('#train-deterministic').checked=true;h.q('#train-deterministic').onchange();h.select('train-model','rtdetr-l.pt');await settle();
 assert.equal(h.q('#train-imgsz').min,'128');assert.equal(h.q('#train-batch').value,'7');assert.equal(h.q('#train-amp').checked,true);assert.equal(h.q('#train-deterministic').checked,true);assert(h.q('#train-model-defaults').textContent.includes('每批 2 张'));assert(h.q('#train-model-defaults').textContent.includes('关闭自动混合精度'));assert(h.q('#train-model-defaults').textContent.includes('固定尺寸训练建议'));assert.equal(h.q('#modal-submit').disabled,true);assert(h.q('#train-model-hint').textContent.includes('至少为 128'));await assert.rejects(h.ui.form.config.onSubmit(),/128/);
 h.q('#train-imgsz').value='128';h.q('#train-imgsz').oninput();await settle();assert.equal(h.q('#modal-submit').disabled,false);h.select('train-model','base-detect.pt');await settle();assert.equal(h.q('#train-imgsz').min,'32');assert.equal(h.q('#train-imgsz').value,'128');
 const first=fixture({models:[special]});first.open();await settle();assert.equal(first.q('#train-amp').checked,false);assert.equal(first.q('#train-deterministic').checked,false);
 const cloned=fixture({models:[builtin(),special],template:{model_arch:special.id,version_id:3,settings:{amp:true,deterministic:true,batch_size:9}}});cloned.open();await settle();assert.equal(cloned.q('#train-amp').checked,true);assert.equal(cloned.q('#train-deterministic').checked,true);assert.equal(cloned.q('#train-batch').value,'9');cloned.select('train-model','base-detect.pt');await settle();cloned.select('train-model',special.id);await settle();assert.equal(cloned.q('#train-amp').checked,true);assert.equal(cloned.q('#train-deterministic').checked,true);assert.equal(cloned.q('#train-batch').value,'9');
 const clean=fixture({models:[builtin(),special]});clean.open();await settle();assert.equal(clean.q('#train-amp').checked,true);clean.q('#train-epochs').value='81';clean.select('train-model',special.id);await settle();assert.equal(Number(clean.q('#train-batch').value),2);assert.equal(clean.q('#train-amp').checked,false);assert.equal(clean.q('#train-deterministic').checked,false);assert.equal(clean.q('#train-epochs').value,'81');clean.select('train-model','base-detect.pt');await settle();assert.equal(Number(clean.q('#train-batch').value),16);assert.equal(clean.q('#train-amp').checked,true);assert.equal(clean.q('#train-deterministic').checked,true);
 clean.q('#train-batch').value='5';clean.q('#train-batch').onchange();clean.select('train-model',special.id);await settle();assert.equal(clean.q('#train-batch').value,'5');assert.equal(clean.q('#train-amp').checked,false);assert.equal(clean.q('#train-deterministic').checked,false);
}
{
 const h=fixture({models:largeCatalog});h.open();await settle();const old=h.q('#train-model-search');h.open();await settle();old.value='should-not-apply';old.oninput({});assert.equal(h.q('#train-model-search').value,'');assert.equal(visibleModels(h).length,16);
}
console.log('PASS training model selection: remote defaults, explicit pretrained payload, scratch capability/clone, inline upload and retained settings, invalid/task mismatch, retries/duplicates, roles, stale modal/session/project responses, compatibility races, initialization history, 61-model filters/IME/hidden selection/task switch, safe untouched model defaults, preserved edited/cloned settings and constraints');
