#!/usr/bin/env node
// Offline shortcut regression: node scripts/check_annotation_shortcuts.mjs
// Uses injected DOM/application doubles; does not start a server or write any data.
import assert from 'node:assert/strict';
import {installAnnotationShortcuts,annotationShortcutsHelp} from '../static/annotation-shortcuts.js';
const noop=()=>{}, counter={}, calls=[], listeners=new Map();let modal=false,nav=false,selection='',writable=true,resolveDelete=null;
const state={currentSection:'annotate',project:{id:1,labels:[{id:1,name:'物体',shortcut:'1'},{id:2,name:'第二类',shortcut:'2'}]},asset:{id:1},tool:'select',annotations:[],zoom:1,polygon:[],selectedAnnotation:{id:1,points:[1,2,3,4],keypoints:[{x:2,y:3,visibility:2}]}};
const node={closest:()=>null};const canvas={classList:{toggle:noop,add:noop}};
const doc={activeElement:node,querySelector:s=>s==='#canvas-scroll'?canvas:null};
const win={addEventListener:(k,fn)=>listeners.set(k,fn),removeEventListener:noop,getSelection:()=>({toString:()=>selection})};
const hit=name=>()=>{calls.push(name);counter[name]=(counter[name]||0)+1;};
const deps={state,window:win,document:doc,hasModal:()=>modal,navigationIsOpen:()=>nav,canEditCurrentAsset:()=>writable,projectTask:()=>state.project.task_type||'detect',taskAllowsTool:t=>t==='select'||t==='rectangle',notify:hit('notify'),openModal:hit('help'),closeModal:noop,setTool:t=>{calls.push(t);state.tool=t},setCanvasZoom:z=>{state.zoom=z},fitCanvas:hit('fit'),navigateAsset:hit('navigate'),chooseTaskLabel:hit('label'),undoPolygonPoint:()=>state.polygon.splice(-2),undoKeypointDraft:hit('undoPose'),finishPolygon:hit('finishPolygon'),finishKeypoints:hit('finishPose'),retryPendingAnnotation:hit('retry'),pasteCopiedAnnotation:hit('paste'),deleteSelectedAnnotation:async()=>{hit('delete')();await new Promise(r=>resolveDelete=r)},cancelDrawing:()=>{hit('cancel')();state.polygon=[];state.poseDraft=null;state.drawing=null;state.pendingAnnotation=null},refreshInspector:noop,stopCanvasPan:hit('stopPan'),cancelShapeEdit:hit('cancelEdit'),clearObjectSelection:hit('clearSelection')};
installAnnotationShortcuts(deps);
async function key(k,extra={}){const e={key:k,code:k===' '?'Space':k,ctrlKey:false,metaKey:false,altKey:false,shiftKey:false,repeat:false,defaultPrevented:false,target:node,preventDefault(){this.defaultPrevented=true},...extra};await listeners.get('keydown')(e);return e}
for(const k of ['r','p','f','s','v'])assert.equal((await key(k,{ctrlKey:true})).defaultPrevented,false,k);
assert.deepEqual(calls,[]);await key('c',{ctrlKey:true});assert.equal(state.copiedAnnotation._copiedProjectId,1);state.selectedAnnotation.keypoints[0].x=99;assert.equal(state.copiedAnnotation.keypoints[0].x,2);
selection='选中的文字';assert.equal((await key('c',{ctrlKey:true})).defaultPrevented,false);selection='';
await key('v',{ctrlKey:true});assert.equal(counter.paste,1);assert.equal(state.tool,'select');
for(const role of ['input','textarea','select','contenteditable']){doc.activeElement={closest:s=>s.includes('input')?{}:null,isContentEditable:role==='contenteditable'};await key('1');await key('r');await key('Delete')};doc.activeElement=node;assert.equal(counter.label,undefined);assert.equal(counter.delete,undefined);
modal=true;await key('r');modal=false;nav=true;await key('r');nav=false;await key('r',{isComposing:true});await key('r',{repeat:true});assert.equal(state.tool,'select');
await key('r');assert.equal(state.tool,'rectangle');const selectionBeforeDelete=state.selectedAnnotation;state.selectedAnnotation=null;await key('Backspace');assert.equal(counter.delete,undefined);state.selectedAnnotation=selectionBeforeDelete;
const draws=calls.filter(call=>call==='rectangle').length;state.selectionIntent=true;await key('r');assert.equal(calls.filter(call=>call==='rectangle').length,draws+1,'R resumes drawing after an explicit object selection');state.selectionIntent=false;
state.polygon=[1,2];await key('Enter');assert.deepEqual(state.polygon,[1,2]);await key('d');assert.equal(counter.navigate,undefined);await key('Backspace');assert.deepEqual(state.polygon,[]);
state.polygon=[1,2,3,4,5,6];await key('Enter');await key('Enter',{repeat:true});assert.equal(counter.finishPolygon,1);await key('z',{ctrlKey:true});assert.equal(state.polygon.length,4);await key('Escape');assert.equal(state.polygon.length,0);
await key('1',{repeat:true});assert.equal(counter.label,undefined);await key('1');assert.equal(counter.label,1);
state.pendingTaskEdit={id:1};await key('Enter');assert.equal(counter.retry,1);state.pendingTaskEdit=null;
state.pendingObjectEdit={annotationId:1,labelId:2};await key('Enter');assert.equal(counter.retry,2,'Enter retries a retained object category change');state.pendingObjectEdit=null;
writable=false;await key('Delete');await key('1');await key('v',{ctrlKey:true});assert.equal(counter.delete,undefined);assert.equal(counter.label,1);assert.equal(counter.paste,1);await key('f');await key('+');await key('d');assert.equal(counter.fit,1);assert.equal(counter.navigate,1);assert.ok(state.zoom>1);writable=true;
const deletion=key('Delete');await Promise.resolve();await key('Delete',{repeat:true});await key('Delete');await key('1');assert.equal(counter.delete,1);assert.equal(counter.label,1);resolveDelete();await deletion;
state.workflowBusy=true;await key('Delete');await key('1');assert.equal(counter.delete,1);state.workflowBusy=false;
await key(' ',{code:'Space'});assert.equal(state.spacePressed,true);doc.activeElement={closest:()=>({})};listeners.get('keyup')({code:'Space'});assert.equal(state.spacePressed,false);doc.activeElement=node;
state.drawing={complete:true};state.pendingAnnotation={};listeners.get('blur')();assert.ok(state.drawing);state.drawing=null;state.pendingAnnotation=null;
await key('?');assert.equal(counter.help,1);assert.equal((await key('ArrowRight',{shiftKey:true})).defaultPrevented,false);
for(const task of ['detect','segment','pose','classify']){const help=annotationShortcutsHelp(task,undefined,state.project.labels);assert.ok(help.includes('物体'));assert.ok(help.includes('Delete / Backspace'));assert.ok(help.includes('撤销草稿最后一个点')===['segment','pose'].includes(task));}
state.project.task_type='classify';state.selectedAnnotation=null;state.annotations=[{id:2,shape_type:'classification'}];
const clearCategory=key('Delete');await Promise.resolve();assert.equal(counter.delete,2,'Delete clears a saved image category without selecting an object');resolveDelete();await clearCategory;
state.annotations=[];await key('Delete');assert.equal(counter.delete,2,'Delete on an unclassified image is a no-op');
assert.ok(annotationShortcutsHelp('classify').includes('清除这张图片已保存的类别'));
state.project.task_type='detect';state.selectedAnnotation={id:3};
const backspaceDeletion=key('Backspace');await Promise.resolve();await key('Backspace',{repeat:true});await key('Backspace');assert.equal(counter.delete,3,'Backspace deletes a selected object once');resolveDelete();await backspaceDeletion;
state.polygon=[1,2,3,4];await key('Backspace');assert.deepEqual(state.polygon,[1,2]);assert.equal(counter.delete,3,'Polygon draft Backspace only removes a vertex');state.polygon=[];
state.poseDraft={};await key('Backspace');assert.equal(counter.undoPose,1);assert.equal(counter.delete,3,'Pose draft Backspace only undoes a keypoint');state.poseDraft=null;
for(const pending of ['pendingAnnotation','pendingTaskEdit','pendingObjectEdit','advancedAnnotationDirty']){state[pending]={};await key('Backspace');assert.equal(counter.delete,3,'Pending edits block deletion');state[pending]=null;}
writable=false;await key('Backspace');writable=true;assert.equal(counter.delete,3,'Readonly Backspace never deletes');
for(const role of ['input','textarea','select','contenteditable']){doc.activeElement={closest:s=>s.includes('input')?{}:null,isContentEditable:role==='contenteditable'};await key('Backspace')};doc.activeElement=node;assert.equal(counter.delete,3,'Text editing Backspace never deletes annotations');
modal=true;await key('Backspace');modal=false;assert.equal(counter.delete,3,'Modal Backspace never deletes annotations');
await key('Escape');assert.equal(counter.clearSelection,1,'Escape without a draft clears object selection');
state.polygon=[1,2];await key('Escape');assert.equal(counter.clearSelection,1,'Escape with a draft preserves selected saved object');
console.log('PASS modifiers/native text/dialog/nav/IME; nonrepeating serialized writes; readonly/busy/draft guards; deep clipboard; undo/finish/retry; space cleanup; task-specific help');
