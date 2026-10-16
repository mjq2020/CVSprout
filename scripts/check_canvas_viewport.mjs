#!/usr/bin/env node
// Run the real viewport functions against browser-like resize/clamping events.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const functions=source.slice(source.indexOf('function rememberCanvasView()'),source.indexOf('function setCanvasZoom('));
const frame={dataset:{assetId:'7'},style:{}},stage={style:{}};
const scroll={clientWidth:1000,clientHeight:600,scrollLeft:320,scrollTop:210,classList:{toggle(){}},querySelector:s=>s==='.canvas-frame'?frame:s==='.canvas-stage'?stage:null};
const context={canvasView:{assetId:7,zoom:1,fit:true,center:{x:320,y:210},viewport:{width:1000,height:600}},state:{asset:{id:7,width:640,height:420},tool:'select'},document:{querySelector:s=>s==='#canvas-scroll'?scroll:null},canvasGuides:null};
vm.createContext(context);vm.runInContext(functions,context);
context.rememberCanvasView();
assert.equal(context.canvasView.center.x,320);
// Browser clamps offsets when a smaller layout lands, before ResizeObserver runs.
scroll.clientWidth=400;scroll.clientHeight=500;scroll.scrollLeft=100;scroll.scrollTop=70;
context.rememberCanvasView();
assert.equal(context.canvasView.center.x,320,'Resize must not replace the valid image center with a clamped offset');
assert.equal(context.canvasView.center.y,210);
context.canvasView.zoom=.55;context.applyCanvasView();
assert.equal(scroll.scrollLeft,176);assert.ok(Math.abs(scroll.scrollTop-115.5)<1e-8);
assert.equal(context.canvasView.center.x,320);assert.ok(Math.abs(context.canvasView.center.y-210)<1e-8);
assert.equal(frame.style.left,'200px');assert.equal(frame.style.top,'250px');
// User panning within the applied viewport still updates the remembered center.
scroll.scrollLeft=220;scroll.scrollTop=110;context.rememberCanvasView();
assert.ok(Math.abs(context.canvasView.center.x-400)<1e-8);assert.ok(Math.abs(context.canvasView.center.y-200)<1e-8);
// A stale frame from a previous image must not overwrite the new image view.
frame.dataset.assetId='8';scroll.scrollLeft=0;context.rememberCanvasView();assert.ok(Math.abs(context.canvasView.center.x-400)<1e-8);
console.log('PASS resize clamping preserves image center; reapplied viewport centers correctly; normal pan and stale-image guards');
