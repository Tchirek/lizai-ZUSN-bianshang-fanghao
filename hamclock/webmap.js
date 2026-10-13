'use strict';
const stage=document.querySelector('#stage'), clock=document.querySelector('#clock'), ctx=clock.getContext('2d');
const mapElement=document.querySelector('#map'), profileElement=document.querySelector('#profile');
const zoomIn=new Set(['+','=','PageUp']), zoomOut=new Set(['-','_','PageDown']);
const mapKeys=new Set(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown',...zoomIn,...zoomOut]);
const namedKeys=new Set([...mapKeys,'Enter','Tab','Escape','Backspace','Space']);
let session=null, revision=-1, active=true, creating=null, sending=false, events=[], frameTask=null;
let map=null, config=null, view=null, initialResolution=1, featureRequest=null, profileRequest=null, profilePoint=null;
let featureTimer=0, profileSerial=0, lastMinute=-1, heldInteraction=false, lastPadEnter=false, lastPadEscape=false, liveTask=null, mapTask=null;
const keys=new Set(), sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const staticSource=new ol.source.Vector(), gridSource=new ol.source.Vector(), liveSource=new ol.source.Vector();
const ringSource=new ol.source.Vector(), selectedSource=new ol.source.Vector(), tagSource=new ol.source.Vector(), markSource=new ol.source.Vector();
const geojson=new ol.format.GeoJSON(), styles=new Map();
const colours={river:'#4678d2',boundary:'#ffffff',field:'rgba(175,180,255,.65)',square:'rgba(175,180,255,.35)',subsquare:'rgba(175,180,255,.18)',grid_name:'#b9beff',subgrid_name:'#9196d2',city:'#e6e6e6',town:'#969696',campuses:'#ff82d2',military:'#ff5546',government:'#f0d682',regulators:'#5ad7ff',embassies:'#96faaa',airport:'#96afff',runway:'#cdcdd2',far:'#bfce25',station:'#ffffff',aprs:'#00b4dc',aprs_path:'rgba(0,180,220,.5)',qso:'#ffffff',qso_path:'rgba(255,255,255,.9)'};
const LABEL_FONT='px Arial,"Microsoft YaHei","PingFang SC",sans-serif', TAG_FONT='italic bold 13'+LABEL_FONT, SUN_GOLD='rgb(255,210,120)';
let veilSource=null, lineSource=null, tag=null, liveLegend=null;
const measure=document.createElement('canvas').getContext('2d'), widths=new Map();
function textWidth(text,font){const key=font+'\n'+text;if(!widths.has(key)){measure.font=font;widths.set(key,measure.measureText(text).width);}return widths.get(key);}

async function create(){
 if(creating)return creating;
 creating=(async()=>{const r=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error('session');const s=await r.json();session=s.session;revision=-1;events=[];})();
 try{await creating;}finally{creating=null;}
}
async function frames(){
 while(active){
  try{
   if(!session)await create();
   const r=await fetch('/frame.png?session='+encodeURIComponent(session)+'&after='+revision,{cache:'no-store'});
   if(r.status===410){session=null;continue;}
   if(r.status===200){
    const rects=r.headers.get('X-Frame-Rects'), image=await createImageBitmap(await r.blob());
    if(rects===null)ctx.drawImage(image,0,0);
    else if(Number(r.headers.get('X-Frame-Base'))===revision){let sy=0;for(const rect of rects.split(';')){const [x,y,w,h]=rect.split(',').map(Number);ctx.drawImage(image,0,sy,w,h,x,y,w,h);sy+=h;}}
    else{image.close();revision=-1;continue;}
    image.close();revision=Number(r.headers.get('X-Frame-Revision'));
   }
  }catch(e){await sleep(1000);}
  await sleep(document.hidden?1000:150);
 }
}
function startFrames(){if(!frameTask)frameTask=frames().finally(()=>{frameTask=null;if(active)startFrames();});}
function enqueue(event){if(!session)return;if(event.type==='hover'){const i=events.findIndex(e=>e.type==='hover');if(i>=0)events.splice(i,1);}events.push(event);flush();}
async function flush(){
 if(sending)return;sending=true;
 try{while(active&&session&&events.length){const batch=events.splice(0,64),r=await fetch('/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session,events:batch})});if(r.status===410){session=null;events=[];break;}if(!r.ok){events=[];break;}}}
 catch(e){events=[];}finally{sending=false;}
}
function clockPoint(e){const r=clock.getBoundingClientRect();return {x:Math.max(0,Math.min(799.999,(e.clientX-r.left)*800/r.width)),y:Math.max(0,Math.min(479.999,(e.clientY-r.top)*480/r.height))};}
let pressed=null,lastHover=0;
clock.addEventListener('pointerdown',e=>{if(searchElement.hidden)clock.focus({preventScroll:true});pressed=clockPoint(e);clock.setPointerCapture(e.pointerId);});
clock.addEventListener('pointerup',e=>{const p=clockPoint(e);if(pressed&&Math.hypot(p.x-pressed.x,p.y-pressed.y)<=5)enqueue({type:'tap',...p,button:e.button+1,ctrl:e.ctrlKey});pressed=null;});
clock.addEventListener('pointercancel',()=>{pressed=null;});
clock.addEventListener('pointermove',e=>{if(performance.now()-lastHover>150){lastHover=performance.now();enqueue({type:'hover',...clockPoint(e)});}});
clock.addEventListener('contextmenu',e=>e.preventDefault());

function zoomRatio(){return view?Math.max(1,Math.min(96,initialResolution/view.getResolution())):1;}
function colour(f){return f.get('colour')||colours[f.get('kind')]||'#dddddd';}
function geometryStyle(f){
 const kind=f.get('kind'),type=f.getGeometry().getType(),level=f.get('level'),z=zoomRatio();
 if(type==='Point'||kind==='boundary'&&((level===4&&z>=3)||(level===5&&z>=10)))return null;
 const c=colour(f),width=kind==='runway'?Math.max(2,Math.round((f.get('width')||0)/view.getResolution())):kind==='river'||kind==='qso_path'||kind==='boundary'&&level!==6?2:1,key=kind+':'+c+':'+level+':'+f.get('local')+':'+width;
 if(!styles.has(key)){
  const fill=type==='Polygon'?new ol.style.Fill({color:kind==='military'?'rgba(255,85,70,.13)':'rgba(220,220,220,.08)'}):undefined;
  const boundary=level===4?'rgba(255,255,255,.75)':level===5?'rgba(255,255,255,.6)':'rgba(255,255,255,.27)';
  const stroke=new ol.style.Stroke({color:kind==='boundary'?boundary:kind==='digital_path'?(f.get('local')?c:c+'50'):c,width,lineDash:kind==='boundary'?(level===4?[9,3,2,3]:level===5?[7,4]:undefined):undefined});
  const result=[new ol.style.Style({stroke,fill})];
  if(kind==='river')result.unshift(new ol.style.Style({stroke:new ol.style.Stroke({color:'#13131c',width:4})}));
  styles.set(key,result);
 }
 return styles.get(key);
}
function labelStyle(f){
 if(f.getGeometry().getType()!=='Point')return null;
 const kind=f.get('kind'),name=f.get('name')||'',c=colour(f),label=f.get('label')||c,rf=f.get('rf'),country=f.get('country'),key=kind+':'+name+':'+c+':'+label+':'+rf+':'+country;
 if(!styles.has(key)){
  const grid=kind==='grid_name'||kind==='subgrid_name';
  let image;
  if(country)image=new ol.style.Icon({src:'/map/flags/'+country+'.png',anchor:[1,.5]});
  else if(!grid){
   if(kind==='digital'||kind==='qso'||kind==='regulators')image=new ol.style.RegularShape({points:4,radius:kind==='digital'?5:4,angle:kind==='digital'?0:Math.PI/4,fill:new ol.style.Fill({color:c}),stroke:new ol.style.Stroke({color:'#000',width:1})});
   else image=new ol.style.Circle({radius:kind==='station'?4:kind==='airport'?4:kind==='city'?2:3,fill:new ol.style.Fill({color:kind==='aprs'?(rf?'#00dcff':'#00647d'):c}),stroke:new ol.style.Stroke({color:kind==='airport'?c:'#000',width:1})});
  }
  const text=name?new ol.style.Text({text:name,font:labelFont(grid),fill:new ol.style.Fill({color:label}),stroke:new ol.style.Stroke({color:'#000',width:3}),offsetX:labelOffset(kind,country)[0],offsetY:labelOffset(kind,country)[1],textAlign:'left',overflow:false}):undefined;
  styles.set(key,new ol.style.Style({image,text}));
 }
 return styles.get(key);
}
function labelFont(grid){return (grid?'bold 11':'bold 13')+LABEL_FONT;}
function labelOffset(kind,country){const grid=kind==='grid_name'||kind==='subgrid_name';return [country?5:grid?3:7,grid?8:-8];}
function inView(f,e){const p=f.getGeometry().getCoordinates();return p[0]>=e[0]&&p[0]<=e[2]&&p[1]>=e[1]&&p[1]<=e[3];}
// Where the names in view are (CSS px boxes), as labelStyle() sets them out.
function labelBoxes(){
 const boxes=[],e=view.calculateExtent(map.getSize());
 for(const source of [gridSource,staticSource,liveSource])for(const f of source.getFeatures()){
  const name=f.get('name');if(!name||f.getGeometry().getType()!=='Point'||!inView(f,e))continue;
  const kind=f.get('kind'),grid=kind==='grid_name'||kind==='subgrid_name',size=grid?11:13,[dx,dy]=labelOffset(kind,f.get('country'));
  const p=map.getPixelFromCoordinate(f.getGeometry().getCoordinates());
  boxes.push([p[0]+dx,p[1]+dy-size/2-2,p[0]+dx+textWidth(name,labelFont(grid))+2,p[1]+dy+size/2+2]);
 }
 return boxes;
}
// SUNSET or SUNRISE in gold italics on the terminator, the line its baseline, reading down it, at
// the first place from the top where it covers the least of the names, clear of the sun box and
// the foot: as the native map places it (map_view.Scene.draw_terminator).
function placeTag(){
 tagSource.clear(true);
 if(!tag||!map||!view)return;
 const [w,h]=map.getSize(),rad=tag.angle*Math.PI/180,up=[Math.sin(rad),-Math.cos(rad)],along=[Math.cos(rad),Math.sin(rad)];
 const tw=textWidth(tag.name,TAG_FONT)+2,th=14,hw=(Math.abs(along[0])*tw+Math.abs(up[0])*th)/2,hh=(Math.abs(along[1])*tw+Math.abs(up[1])*th)/2;
 const taken=labelBoxes();let best=null,least=Infinity;
 for(const point of tag.points){
  const p=map.getPixelFromCoordinate(point);if(!p)continue;
  const base=[p[0]+up[0]*tag.lift,p[1]+up[1]*tag.lift],mid=[base[0]+up[0]*th*.35,base[1]+up[1]*th*.35];
  const box=[mid[0]-hw,mid[1]-hh,mid[0]+hw,mid[1]+hh];
  if(box[0]<4||box[2]>w-4||box[1]<28||box[3]>h-30)continue;
  let cover=0;for(const t of taken){const x=Math.min(box[2],t[2])-Math.max(box[0],t[0]),y=Math.min(box[3],t[3])-Math.max(box[1],t[1]);if(x>0&&y>0)cover+=x*y;}
  if(cover<least){least=cover;best=base;}
 }
 if(!best)return;
 const f=new ol.Feature(new ol.geom.Point(map.getCoordinateFromPixel(best)));
 f.setStyle(new ol.style.Style({text:new ol.style.Text({text:tag.name,font:TAG_FONT,rotation:rad,textAlign:'center',textBaseline:'alphabetic',fill:new ol.style.Fill({color:SUN_GOLD}),stroke:new ol.style.Stroke({color:'#000',width:3})})}));
 tagSource.addFeature(f);
}
// The night as the native map lays it on: worked out on the station for this view at the page's
// own px, the veil under everything drawn on the ground, the terminator's gold line over the rivers
// and paths. Each picture reaches a quarter view past each side, so a short pan needs no new one.
function nightExtent(extent,resolution){const w=(extent[2]-extent[0])/4,h=(extent[3]-extent[1])/4,e=[extent[0]-w,extent[1]-h,extent[2]+w,extent[3]+h];return {e,w:Math.round((e[2]-e[0])/resolution),h:Math.round((e[3]-e[1])/resolution)};}
function nightQuery(q,ratio){return '?bbox='+q.e.map(v=>v.toFixed(1)).join(',')+'&w='+q.w+'&h='+q.h+'&ratio='+ratio+'&v='+lastMinute;}
async function veilLoader(extent,resolution){
 const q=nightExtent(extent,resolution),r=await fetch('/map/veil.png'+nightQuery(q,1));if(!r.ok)throw Error('veil');
 return {image:await createImageBitmap(await r.blob()),extent:q.e,resolution,pixelRatio:1};
}
async function lineLoader(extent,resolution,pixelRatio){
 const q=nightExtent(extent,resolution);let ratio=Math.min(2,Math.max(1,pixelRatio||1));
 while(q.w*q.h*ratio*ratio>12e6&&ratio>1)ratio=Math.max(1,ratio-.25);
 const r=await fetch('/map/terminator'+nightQuery(q,ratio));if(!r.ok)throw Error('terminator');const d=await r.json();
 const canvas=document.createElement('canvas');canvas.width=Math.round(q.w*ratio);canvas.height=Math.round(q.h*ratio);
 if(d.line){const image=new Image();image.src=d.line.png;await image.decode();canvas.getContext('2d').drawImage(image,d.line.x,d.line.y);}
 tag=d.tag||null;placeTag();
 return {image:canvas,extent:q.e,resolution,pixelRatio:ratio};
}
function updateLegend(){
 const legend=document.querySelector('#legend');legend.replaceChildren();
 if(!liveLegend||!view)return;
 const e=view.calculateExtent(map.getSize()),r=view.getResolution()*4,inner=[e[0]+r,e[1]+r,e[2]-r,e[3]-r];
 let aprs=0,digital=0;for(const f of liveSource.getFeatures()){const kind=f.get('kind');if((kind==='aprs'||kind==='digital')&&inView(f,inner)){if(kind==='aprs')aprs++;else digital++;}}
 for(const item of [{name:'APRS '+aprs,colour:'#00ffff'},{name:'DIGI '+digital,colour:'#fff'},{name:'PATH '+liveLegend.paths+'/'+liveLegend.total,colour:'#969696'},...liveLegend.bands.map(b=>({name:b.name.endsWith('cm')?b.name:b.name+'m',colour:b.colour}))]){const span=document.createElement('span'),i=document.createElement('i');i.style.background=item.colour;span.append(i,document.createTextNode(item.name));legend.append(span);}
}
function readFeatures(data){return geojson.readFeatures(data,{dataProjection:config.projection,featureProjection:config.projection});}
function replace(source,data){source.clear(true);source.addFeatures(readFeatures(data));}
async function updateFeatures(){
 if(!map||document.hidden)return;
 if(featureRequest)featureRequest.abort();featureRequest=new AbortController();
 const signal=featureRequest.signal,bbox=view.calculateExtent(map.getSize()),margin=view.getResolution()*150;
 const query='?bbox='+[bbox[0]-margin,bbox[1]-margin,bbox[2]+margin,bbox[3]+margin].join(',')+'&zoom='+zoomRatio();
 try{
  const [features,grid]=await Promise.all(['/map/features','/map/grid'].map(async path=>{const r=await fetch(path+query,{signal});if(!r.ok)throw Error('layers');return r.json();}));
  if(!signal.aborted){styles.clear();replace(staticSource,features);replace(gridSource,grid);placeTag();}
 }catch(e){}
}
function scheduleFeatures(){clearTimeout(featureTimer);featureTimer=setTimeout(updateFeatures,180);}
function rings(){
 if(!view||!map)return;          // the view may change while the map is still being made
 const wanted=view.getResolution()*180,power=10**Math.floor(Math.log10(wanted)),step=Math.max(...[1,2,5].map(x=>x*power).filter(x=>x<=wanted));
 const e=view.calculateExtent(map.getSize()),centre=view.getCenter(),distance=Math.hypot(...centre),span=Math.hypot(e[2]-e[0],e[3]-e[1])/2;
 const features=[];for(let r=Math.max(step,Math.floor((distance-span)/step)*step);r<distance+span;r+=step)features.push(new ol.Feature(new ol.geom.Circle([0,0],r)));
 ringSource.clear(true);ringSource.addFeatures(features);
 // scale bar and terrain detail as the native map's foot: a bar of 1, 2 or 5 x 10^n km near a tenth of the map's width
 const resolution=view.getResolution(),index=config.levels.findIndex(level=>config.metres_per_pixel/level<=resolution),i=index<0?config.levels.length-1:index;
 const most=130*mapElement.clientWidth/1320*resolution/1000,decade=10**Math.floor(Math.log10(most)),km=Math.max(...[1,2,5].map(x=>x*decade).filter(x=>x<=most));
 document.querySelector('#km').textContent=+km.toPrecision(3)+' km';document.querySelector('#bar').style.width=Math.round(km*1000/resolution)+'px';
 document.querySelector('#dem').textContent='DEM '+Math.round(config.dem[i])+' m  '+zoomRatio().toFixed(1)+'x';
}
function startLive(){if(!liveTask)liveTask=live().finally(()=>{liveTask=null;if(active&&map)startLive();});}
async function live(){
 while(active){
  if(!document.hidden&&map){try{
   const r=await fetch('/map/live');if(!r.ok)throw Error('live');const data=await r.json();replace(liveSource,data.features);
   const sun=document.querySelector('#sun');sun.replaceChildren();const image=document.createElement('img');image.src='/map/sun.png?v='+data.minute;image.style.opacity=data.sun.elevation>-.833?'1':'.43';sun.append(image);
   for(const [name,value] of data.sun.parts){const item=document.createElement('span');item.textContent=name;const b=document.createElement('b');b.textContent=value;item.append(b);sun.append(item);}
   liveLegend=data.legend;updateLegend();placeTag();
   if(lastMinute!==data.minute){lastMinute=data.minute;veilSource.refresh();lineSource.refresh();}
  }catch(e){}}
  await sleep(15000);
 }
}
// 1x shows the whole map's width, as on the native screen: worked out again whenever the map's
// box changes size (a window resized, a phone turned), keeping the centre and the zoom.
function makeView(center,ratio){
 initialResolution=(config.extent[2]-config.extent[0])/mapElement.clientWidth;
 const made=new ol.View({projection:config.projection,center,extent:config.extent,enableRotation:false,maxResolution:initialResolution,minResolution:initialResolution/96,resolution:initialResolution/ratio,constrainResolution:false,smoothExtentConstraint:false});
 made.on('change:resolution',rings);
 return made;
}
function resized(){
 if(!view||!mapElement.clientWidth)return;
 const ratio=zoomRatio(),center=view.getCenter();
 if(heldInteraction){heldInteraction=false;view.endInteraction(0);}
 view=makeView(center,ratio);map.setView(view);rings();scheduleFeatures();updateLegend();
}
async function initMap(){
 while(active&&!map){try{
  mapElement.replaceChildren();  // a try that failed half way may have left a map's viewport behind
  const r=await fetch('/map/config');if(!r.ok)throw Error('config');config=await r.json();
  config.projection=new ol.proj.Projection({code:'INFLU:AEQD',units:'m',extent:config.extent});
  const resolutions=config.levels.map(level=>config.metres_per_pixel/level);
  const tileGrid=new ol.tilegrid.TileGrid({extent:config.extent,origin:[config.extent[0],config.extent[3]],resolutions,tileSize:config.tile_size});
  const tiles=new ol.layer.Tile({source:new ol.source.XYZ({projection:config.projection,tileGrid,wrapX:false,tileUrlFunction:c=>'/map/tiles/'+config.levels[c[0]]+'/'+c[1]+'/'+c[2]+'.png?v='+config.tile_version})});
  lastMinute=Math.floor(Date.now()/60000);
  veilSource=new ol.source.Image({projection:config.projection,loader:veilLoader});
  lineSource=new ol.source.Image({projection:config.projection,loader:lineLoader});
  view=makeView([0,0],1);
  const vector=source=>new ol.layer.Vector({source,style:geometryStyle,renderBuffer:150});
  const labels=source=>new ol.layer.Vector({source,style:labelStyle,declutter:'labels',renderBuffer:150});
  // in the native map's order: ground and night, what is drawn on the ground, the terminator, names
  map=new ol.Map({target:mapElement,view,controls:[],interactions:ol.interaction.defaults.defaults({altShiftDragRotate:false,pinchRotate:false,keyboard:false}),layers:[tiles,new ol.layer.Image({source:veilSource}),vector(staticSource),vector(gridSource),new ol.layer.Vector({source:ringSource,style:new ol.style.Style({stroke:new ol.style.Stroke({color:'rgba(190,190,190,.35)',width:1})})}),vector(liveSource),new ol.layer.Image({source:lineSource}),labels(gridSource),labels(staticSource),labels(liveSource),new ol.layer.Vector({source:markSource,style:markStyle,updateWhileAnimating:true,updateWhileInteracting:true}),new ol.layer.Vector({source:symbolSource,style:symbolStyle,updateWhileAnimating:true,updateWhileInteracting:true}),new ol.layer.Vector({source:handSource,style:handStyle,updateWhileAnimating:true,updateWhileInteracting:true}),new ol.layer.Vector({source:selectedSource,style:new ol.style.Style({stroke:new ol.style.Stroke({color:'#e6e6b4',width:1}),image:new ol.style.RegularShape({points:4,radius:6,radius2:0,angle:0,stroke:new ol.style.Stroke({color:'#fff',width:1})})})}),new ol.layer.Vector({source:tagSource})]});
  map.addInteraction(new SymbolHand());
  map.on('pointermove',event=>{symbolPointer=event.coordinate;if(armed||carried)showHand();});
  mapElement.addEventListener('pointerleave',()=>{symbolPointer=null;showHand();});
  map.on('singleclick',event=>{if(touching()||Date.now()<symbolQuiet)return;if(searchElement.hidden)mapElement.focus({preventScroll:true});inspect(event.coordinate);});
  map.on('moveend',()=>{scheduleFeatures();rings();positionProfile();updateLegend();placeTag();});
  map.on('postrender',positionProfile);map.on('change:size',resized);
  updateFeatures();rings();startLive();
  if(document.activeElement===document.body)mapElement.focus({preventScroll:true});   // keys work at once
 }catch(e){console.warn('Map setup',e.message);await sleep(1000);}}
}
// the middle button makes the touchpad a touch screen for the map, as on the X230: the pointer
// is locked and hidden and its moves drag the map along under the finger, pinches and the wheel
// zoom as ever; once more (or Esc, the browser's) and it is back
const TOUCH_GAIN=0.15;
function touching(){return document.pointerLockElement===mapElement;}
mapElement.addEventListener('mousedown',e=>{if(e.button===1)e.preventDefault();});        // no autoscroll
mapElement.addEventListener('auxclick',e=>{
 if(e.button!==1||!view)return;e.preventDefault();
 if(touching())document.exitPointerLock();else mapElement.requestPointerLock();
});
let touchRest=0;
document.addEventListener('mousemove',e=>{
 if(!touching()||!view)return;
 if(!touchRest)view.beginInteraction();                        // one drag while the finger moves
 clearTimeout(touchRest);touchRest=setTimeout(()=>{touchRest=0;view.endInteraction(0);},150);
 const [x,y]=view.getCenter(),r=view.getResolution()*TOUCH_GAIN;
 view.setCenter([x-e.movementX*r,y+e.movementY*r]);
});
// MIL-STD-2525 / APP-6 symbols, drawn from the station's geometry (milsym.py) and shared with the
// X230's map: hold F1..F4 (friend, hostile, neutral, unknown) and press 1..9, 0 for one; Enter or
// Space puts it down at the pointer, once a press; a click picks one up and the next puts it down,
// a drag moves it; Delete or Backspace throws away the one in hand, Ctrl+Z undoes, Esc lets go
const JITTER=5,SYMBOL_HELD=140/255,symbolSource=new ol.source.Vector(),handSource=new ol.source.Vector(),symbolImages=new Map();
let milsym=null,symbolRev=-1,armed=null,carried=null,symbolPointer=null,symbolPress=null,symbolQuiet=0;
const symbolDone=[],affiliationKeys=[];
fetch('/map/milsym').then(r=>r.ok?r.json():null).then(data=>{milsym=data;symbolSource.changed();}).catch(()=>{});
// a symbol as an image: its frame filled and outlined, its icon stroked, drawn at full strength
// and then laid down see-through when it is in hand
function symbolImage(affiliation,fn,alpha){
 const s=milsym.size*dotPx()*devicePixelRatio,key=[affiliation,fn,alpha,s.toFixed(3)].join();
 if(!symbolImages.has(key)){
  const shape=milsym.shapes[affiliation][fn],size=Math.ceil(200*s),solid=document.createElement('canvas'),c=solid.getContext('2d');
  solid.width=solid.height=size;c.scale(s,s);c.lineWidth=milsym.line;c.lineJoin=c.lineCap='round';c.strokeStyle=c.fillStyle='#000';
  const path=(points,closed)=>{c.beginPath();points.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));if(closed)c.closePath();};
  path(shape.frame,true);c.fillStyle=shape.fill;c.fill();c.stroke();c.fillStyle='#000';
  for(const stroke of shape.icon){path(stroke.points,stroke.closed);if(stroke.filled)c.fill();else c.stroke();}
  let image=solid;
  if(alpha<1){image=document.createElement('canvas');image.width=image.height=size;const d=image.getContext('2d');d.globalAlpha=alpha;d.drawImage(solid,0,0);}
  symbolImages.set(key,new ol.style.Style({image:new ol.style.Icon({img:image,scale:1/devicePixelRatio})}));
 }
 return symbolImages.get(key);
}
function symbolStyle(feature){return milsym&&!(carried&&carried.id===feature.getId())?symbolImage(feature.get('affiliation'),feature.get('function'),1):null;}
function handStyle(feature){return milsym?symbolImage(feature.get('affiliation'),feature.get('function'),SYMBOL_HELD):null;}
// the symbol in hand, or the one about to be put down, at the pointer; off the map, one in hand
// shows where it was
function showHand(){
 handSource.clear();
 const held=carried||armed,at=symbolPointer||carried?.point;
 if(!held||!at)return;
 const f=new ol.Feature(new ol.geom.Point(at));f.set('affiliation',held.affiliation);f.set('function',held.function);handSource.addFeature(f);
}
function showSymbols(data){
 if(data.rev<symbolRev)return;                       // an answer overtaken by a later one
 symbolRev=data.rev;
 symbolSource.clear();
 symbolSource.addFeatures(data.symbols.map(s=>{const f=new ol.Feature(new ol.geom.Point(s.point));f.setId(s.id);for(const k of ['affiliation','function','placed'])f.set(k,s[k]);return f;}));
 if(carried&&!symbolSource.getFeatureById(carried.id))carried=null;      // thrown away elsewhere
 showHand();
}
async function changeSymbol(body,undo){
 try{
  const r=await fetch('/map/symbols',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok)return;const data=await r.json();showSymbols(data);
  if(undo)symbolDone.push({op:body.op,id:body.id,before:data.before});
 }catch(e){}
}
(async()=>{                                          // the server answers as soon as the board changes
 while(true){
  if(document.hidden||!map){await sleep(500);continue;}
  try{const r=await fetch('/map/symbols?rev='+symbolRev);if(r.status===200)showSymbols(await r.json());}catch(e){await sleep(2000);}
 }
})();
function symbolAt(pixel){return map.forEachFeatureAtPixel(pixel,f=>f,{layerFilter:l=>l.getSource()===symbolSource,hitTolerance:2})||null;}
function heldSymbol(f){return {id:f.getId(),affiliation:f.get('affiliation'),function:f.get('function'),placed:f.get('placed'),point:f.getGeometry().getCoordinates()};}
function dropSymbol(at){
 const held=carried;carried=null;
 if(held&&at)symbolSource.getFeatureById(held.id)?.getGeometry().setCoordinates(at);   // there at once, not back first
 showHand();symbolSource.changed();
 if(held&&at)changeSymbol({op:'move',id:held.id,x:at[0],y:at[1]},true);
}
function undoSymbol(){
 const last=symbolDone.pop();if(!last)return;
 if(last.op==='add')changeSymbol({op:'remove',id:last.id});
 else if(last.before){const b=last.before;changeSymbol({op:last.op==='remove'?'add':'move',id:b.id,affiliation:b.affiliation,function:b.function,placed:b.placed,x:b.point[0],y:b.point[1]});}
}
// presses on the map: on a symbol it is picked up (and moved, if dragged); with one in hand, it goes down
class SymbolHand extends ol.interaction.Pointer{
 handleDownEvent(e){
  if(!milsym||touching()||e.originalEvent.button!==0)return false;
  if(carried){symbolPress={at:e.pixel,moved:false,dropping:true};return true;}
  const f=symbolAt(e.pixel);if(!f)return false;
  carried=heldSymbol(f);symbolPress={at:e.pixel,moved:false,dropping:false};symbolPointer=e.coordinate;symbolSource.changed();showHand();return true;
 }
 handleDragEvent(e){
  symbolPointer=e.coordinate;
  if(Math.hypot(e.pixel[0]-symbolPress.at[0],e.pixel[1]-symbolPress.at[1])>JITTER)symbolPress.moved=true;
  showHand();
 }
 handleUpEvent(e){
  const press=symbolPress;symbolPress=null;symbolQuiet=Date.now()+600;     // not a tap on the map as well
  if(press.dropping||press.moved)dropSymbol(e.coordinate);
  return false;
 }
}
document.addEventListener('keydown',e=>{
 if(!milsym||!view||!searchElement.hidden||e.target.closest('input,textarea,select,[contenteditable]'))return;
 const key=e.key,affiliation=/^F[1-4]$/.test(key)?milsym.affiliations[Number(key[1])-1]:null;
 let done=true;
 if(affiliation){if(!affiliationKeys.includes(affiliation))affiliationKeys.push(affiliation);}
 else if(/^[0-9]$/.test(key)&&affiliationKeys.length){armed={affiliation:affiliationKeys.at(-1),function:milsym.functions[(Number(key)+9)%10]};showHand();}
 else if((key==='Enter'||key===' ')&&armed&&symbolPointer){if(!e.repeat)changeSymbol({op:'add',affiliation:armed.affiliation,function:armed.function,x:symbolPointer[0],y:symbolPointer[1]},true);}
 else if((key==='Delete'||key==='Backspace')&&carried){const held=carried;carried=null;showHand();changeSymbol({op:'remove',id:held.id},true);}
 else if((e.ctrlKey||e.metaKey)&&key.toLowerCase()==='z'){undoSymbol();}
 else if(key==='Escape'&&(carried||armed)){if(carried){carried=null;symbolSource.changed();}else armed=null;showHand();}
 else done=false;
 if(done){e.preventDefault();e.stopImmediatePropagation();}
},true);
document.addEventListener('keyup',e=>{const i=/^F[1-4]$/.test(e.key)?affiliationKeys.indexOf(milsym?.affiliations[Number(e.key[1])-1]):-1;if(i>=0){affiliationKeys.splice(i,1);e.preventDefault();}},true);
window.addEventListener('blur',()=>{affiliationKeys.length=0;});
function startMap(){if(!map&&!mapTask)mapTask=initMap().finally(()=>{mapTask=null;if(active&&!map)startMap();});}

// HamClock's weather pane shows the open popup's place, DE's again once it closes (dxwx.py)
function weather(body){if(session)fetch('/map/dx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session,...body})}).catch(()=>{});}
function closeProfile(){if(profilePoint)weather({close:true});profileSerial++;if(profileRequest)profileRequest.abort();profilePoint=null;profileElement.hidden=true;selectedSource.clear();}
document.querySelector('#profile-close').addEventListener('click',()=>{closeProfile();mapElement.focus({preventScroll:true});});
function positionProfile(){
 if(!profilePoint||profileElement.hidden||!map)return;
 const p=map.getPixelFromCoordinate(profilePoint);if(!p)return;
 const rect=mapElement.getBoundingClientRect(),root=stage.getBoundingClientRect(),w=profileElement.offsetWidth,h=profileElement.offsetHeight;
 profileElement.style.left=(rect.left-root.left+Math.min(Math.max(0,p[0]+12),Math.max(0,rect.width-w)))+'px';
 profileElement.style.top=(rect.top-root.top+Math.min(Math.max(0,p[1]+12),Math.max(0,rect.height-h)))+'px';
}
function table(data){
 const element=document.querySelector('#reach-table');element.replaceChildren();const header=document.createElement('tr');
 for(const title of ['MHz',...data.heights.map(h=>h+'m')]){const th=document.createElement('th');th.textContent=title;header.append(th);}element.append(header);
 data.frequencies.forEach((frequency,i)=>{const row=document.createElement('tr'),label=document.createElement('td');label.textContent=frequency.toFixed(3);row.append(label);for(let j=0;j<data.heights.length;j++){const cell=document.createElement('td'),value=data.status?data.status[i][j]:'—';cell.textContent=value;cell.className=value;row.append(cell);}element.append(row);});
}
async function inspect(point){
 if(!config||point[0]<config.extent[0]||point[0]>config.extent[2]||point[1]<config.extent[1]||point[1]>config.extent[3])return;
 const serial=++profileSerial;if(profileRequest)profileRequest.abort();profileRequest=new AbortController();
 profilePoint=point.slice();profileElement.hidden=false;table({heights:[10,20,40,80],frequencies:[144.174,144.640,145.025,435]});weather({x:point[0],y:point[1]});
 for(const id of ['loss','nearby','conditions'])document.querySelector('#'+id).textContent='';
 // distance and bearing are the projection's own (azimuthal equidistant about DE): shown at once
 document.querySelector('#position').textContent=(Math.hypot(...point)/1000).toFixed(1)+' km  '+Math.round((Math.atan2(point[0],point[1])*180/Math.PI+360)%360)+'°';
 const chart=document.querySelector('#terrain'),c=chart.getContext('2d');c.clearRect(0,0,chart.width,chart.height);
 selectedSource.clear();selectedSource.addFeatures([new ol.Feature(new ol.geom.LineString([[0,0],point])),new ol.Feature(new ol.geom.Point(point))]);positionProfile();
 try{
  const r=await fetch('/map/reachability',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x:point[0],y:point[1]}),signal:profileRequest.signal});if(!r.ok)throw Error('地形数据暂不可用');const d=await r.json();if(serial!==profileSerial)return;
  table(d);document.querySelector('#position').textContent=d.distance.toFixed(1)+' km  '+Math.round(d.bearing)+'°  '+d.lat.toFixed(2)+','+d.lon.toFixed(2);
  document.querySelector('#loss').textContent='CLR10m '+Math.round(d.clearance)+'m  FSL '+Math.round(d.fspl[0])+'–'+Math.round(d.fspl.at(-1))+' dB';
  document.querySelector('#nearby').textContent='20km: '+d.calls.length+' stations  '+d.paths+' paths'+(d.calls.length?' · '+d.calls.join(' '):'');
  document.querySelector('#conditions').textContent='DE '+d.our_agl+'m  DX AGL  k=4/3  F1 60%  DEM '+Math.round(d.dem_m)+'m';
  const low=Math.min(...d.ground,...d.ray),high=Math.max(...d.ground,...d.ray),xy=(v,i)=>[d.d[i]/Math.max(.001,d.d.at(-1))*chart.width,chart.height-3-(v-low)/Math.max(1,high-low)*(chart.height-6)];
  c.beginPath();c.moveTo(0,chart.height);d.ground.forEach((v,i)=>c.lineTo(...xy(v,i)));c.lineTo(chart.width,chart.height);c.fillStyle='#50412d';c.fill();c.beginPath();d.ray.forEach((v,i)=>i?c.lineTo(...xy(v,i)):c.moveTo(...xy(v,i)));c.strokeStyle='#fff';c.stroke();positionProfile();
 }catch(e){if(serial===profileSerial&&e.name!=='AbortError')document.querySelector('#conditions').textContent=e.message;}
}

function keyName(e){return e.key===' '?'Space':e.key;}
const searchElement=document.querySelector('#search'),searchInput=document.querySelector('#search-query'),searchResults=document.querySelector('#search-results'),searchNote=document.querySelector('#search-note'),searchCanvas=document.querySelector('#search-dots');
let searchRows=[],searchIndex=0,searchHover=null,searchTimer=0,searchRequest=null,searchFocus=null;
// The box is the native one (map_search.py): drawn in HamClock's own 1600x960 pixels, scaled with the
// clock, its letters lit dot by dot from the station's tables (/map/dots), fetched as characters appear
const SEARCH={width:600,row:30,pad:6,gap:12,cursor:8,page:8,scroll:90},ELLIPSIS='…',dotTable=new Map(),dotAsked=new Set();
async function needDots(text){
 const missing=[...new Set(text)].filter(c=>!dotAsked.has(c));if(!missing.length)return false;
 missing.forEach(c=>dotAsked.add(c));
 try{
  const r=await fetch('/map/dots?t='+encodeURIComponent(missing.join('')));if(!r.ok)throw Error(r.status);
  for(const [c,[xo,yo,adv,w,h,hex]] of Object.entries(await r.json()))dotTable.set(c,{xo,yo,adv,w,h,bits:Uint8Array.from(hex.match(/../g)||[],b=>parseInt(b,16))});
  return true;
 }catch(e){missing.forEach(c=>dotAsked.delete(c));return false;}
}
function dotWidth(text){let w=0;for(const c of text)w+=dotTable.get(c)?.adv??0;return w;}
function dotFit(text,room){
 if(dotWidth(text)<=room)return text;
 const tail=dotTable.has(ELLIPSIS)?ELLIPSIS:'...',chars=[...text];
 while(chars.length&&dotWidth(chars.join('')+tail)>room)chars.pop();
 return dotWidth(tail)<=room?chars.join('')+tail:'';
}
function dotColour(colour){const [r,g,b]=[1,3,5].map(i=>parseInt(colour.slice(i,i+2),16));return (255<<24|b<<16|g<<8|r)>>>0;}
function dotFill(image,x,y,w,h,colour){
 const px=new Uint32Array(image.data.buffer),c=dotColour(colour);
 for(let row=y;row<y+h;row++)px.fill(c,row*image.width+x,row*image.width+x+w);
}
// as HamClock's plotChar: the cursor at the top of the line, a dot wherever a glyph's bit is set
function dotPrint(image,text,x,y,colour,right=image.width){
 const px=new Uint32Array(image.data.buffer),c=dotColour(colour);
 for(const ch of text){
  const g=dotTable.get(ch);if(!g)continue;
  for(let i=0;i<g.w*g.h;i++)if(g.bits[i>>3]&128>>(i&7)){
   const dx=x+g.xo+i%g.w,dy=y+g.yo+Math.floor(i/g.w);
   if(dx>=0&&dx<right&&dy>=0&&dy<image.height)px[dy*image.width+dx]=c;
  }
  x+=g.adv;
 }
}
function drawSearch(){
 if(searchElement.hidden)return;
 const {width,row,pad,gap,cursor}=SEARCH,status=searchNote.textContent,[first,page]=searchPage(),lines=1+page.length+(status?1:0);
 const image=new ImageData(width,row*lines);dotFill(image,0,0,width,row*lines,'#000000');
 // the ruler, as a pager's: which of how many are on the page
 const ruler=searchRows.length>SEARCH.page?`${first+1}-${first+page.length}/${searchRows.length}`:'',room=width-pad-(ruler?dotWidth(ruler)+gap:0);
 dotPrint(image,ruler,width-pad-dotWidth(ruler),cursor,'#969696');
 // >text_ with the _ where the caret is; the start scrolls off to keep the caret in the box
 const value=searchInput.value,at=searchInput.selectionStart??value.length;
 let before=[...value.slice(0,at)];
 while(before.length&&dotWidth('>'+before.join('')+'_')>room-pad)before.shift();
 dotPrint(image,'>'+before.join('')+value.slice(at),pad,cursor,'#ffffff',room);
 dotPrint(image,'_',pad+dotWidth('>'+before.join('')),cursor,'#ffffff',room);
 const lit=searchHover??searchIndex;
 page.forEach((r,j)=>{
  const i=first+j,y=(j+1)*row,right=width-pad-dotWidth(r.position);
  if(i===lit)dotFill(image,1,y,width-2,row,'#2d2d2d');
  dotPrint(image,dotFit(r.name,right-gap-pad),pad,y+cursor,'#dddddd');
  dotPrint(image,r.position,right,y+cursor,r.colour);
 });
 if(status)dotPrint(image,status,pad,row+cursor,'#969696');
 dotFill(image,0,0,width,1,'#888888');dotFill(image,0,row*lines-1,width,1,'#888888');
 dotFill(image,0,0,1,row*lines,'#888888');dotFill(image,width-1,0,1,row*lines,'#888888');
 searchCanvas.width=width;searchCanvas.height=row*lines;searchCanvas.style.setProperty('--lines',lines);
 searchCanvas.getContext('2d').putImageData(image,0,0);
}
// the lit result on the map, as the native one: a box round a point, slow stripes drifting
// across an area, in its colour; the map is left where it is, shown or not
const MARK=9,STRIPE=14,DRIFT=8,DRIFT_FPS=15,stripeTiles=new Map();
let markedRow=null,markTimer=0;
function dotPx(){return stage.clientWidth/1600;}
function markSearch(){
 const row=searchElement.hidden?null:searchRows[searchHover??searchIndex]??null;
 if(row===markedRow)return;
 markedRow=row;markSource.clear();clearInterval(markTimer);
 if(!row)return;
 // an area as its outline, and its point too, boxed while the area is too small to stripe
 const features=[new ol.Feature(new ol.geom.Point(row.point)),...row.rings.length?[new ol.Feature(new ol.geom.Polygon(row.rings))]:[]];
 for(const f of features){f.set('colour',row.colour);f.set('area',row.rings.length?features[1]:null);}
 markSource.addFeatures(features);
 if(row.rings.length)markTimer=setInterval(()=>markSource.changed(),1000/DRIFT_FPS);
}
function stripeTile(period,colour){
 const key=period+colour;
 if(!stripeTiles.has(key)){
  const tile=document.createElement('canvas'),c=tile.getContext('2d'),image=c.createImageData(period,period),rgb=[1,3,5].map(i=>parseInt(colour.slice(i,i+2),16));
  tile.width=tile.height=period;
  for(let y=0;y<period;y++)for(let x=0;x<period;x++)if((x+y)%period<period*0.4){const o=(y*period+x)*4;image.data.set([...rgb,110],o);}
  c.putImageData(image,0,0);stripeTiles.set(key,tile);
 }
 return stripeTiles.get(key);
}
function markStyle(feature){
 const colour=feature.get('colour'),k=dotPx();
 if(feature.getGeometry().getType()==='Point'){
  const area=feature.get('area');
  const extent=area&&area.getGeometry().getExtent();
  if(area&&Math.max(ol.extent.getWidth(extent),ol.extent.getHeight(extent))/view.getResolution()>=3*STRIPE*k)return null;
  const square=(stroke,width)=>new ol.style.Style({image:new ol.style.RegularShape({points:4,radius:MARK*k*Math.SQRT2,angle:Math.PI/4,stroke:new ol.style.Stroke({color:stroke,width})})});
  return [square('#000',4),square(colour,2)];
 }
 return new ol.style.Style({renderer:(rings,state)=>{
  const c=state.context,s=state.pixelRatio*k,period=Math.max(4,Math.round(STRIPE*s)),pattern=c.createPattern(stripeTile(period,colour),'repeat');
  pattern.setTransform(new DOMMatrix().translate(Math.floor(performance.now()/1000*DRIFT*s)%period,0));
  c.save();c.beginPath();
  for(const ring of rings){ring.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));c.closePath();}
  c.fillStyle=pattern;c.fill('evenodd');c.strokeStyle=colour;c.lineWidth=state.pixelRatio;c.stroke();c.restore();
 }});
}
function closeSearch(){
 clearTimeout(searchTimer);if(searchRequest)searchRequest.abort();searchElement.hidden=true;searchInput.setAttribute('aria-expanded','false');markSearch();
 searchInput.blur();(searchFocus&&searchFocus!==document.body?searchFocus:mapElement).focus({preventScroll:true});
}
function showSearch(){
 if(!view)return;release();searchFocus=document.activeElement;searchInput.value='';searchElement.hidden=false;searchInput.focus({preventScroll:true});queueSearch();
}
// the row under the pointer is lit while it is there; otherwise the one the arrow keys chose
function highlightSearch(){
 const index=searchHover??searchIndex;
 [...searchResults.children].forEach((item,i)=>item.setAttribute('aria-selected',String(i===index)));
 if(searchRows.length)searchInput.setAttribute('aria-activedescendant',searchResults.children[index].id);
 else searchInput.removeAttribute('aria-activedescendant');
 drawSearch();markSearch();
}
function selectSearch(index){searchIndex=index;highlightSearch();}
// the keys go on from the row lit, the pointer's or their own, and take over from the pointer
function stepSearch(delta){if(searchRows.length){const from=searchHover??searchIndex;searchHover=null;selectSearch((from+delta+searchRows.length)%searchRows.length);}}
// a page at a time, as a pager shows a file: the page of the row the keys chose
function searchPage(){const first=Math.floor(searchIndex/SEARCH.page)*SEARCH.page;return [first,searchRows.slice(first,first+SEARCH.page)];}
function turnSearch(delta){
 const from=Math.floor((searchHover??searchIndex)/SEARCH.page),to=Math.min(Math.max(from+delta,0),Math.ceil(searchRows.length/SEARCH.page)-1);
 if(searchRows.length&&to!==from){searchHover=null;selectSearch(to*SEARCH.page);}
}
function chooseSearch(index){
 const row=searchRows[index];if(!row)return;
 closeSearch();view.setResolution(Math.min(view.getResolution(),initialResolution/8));view.setCenter(row.point);inspect(row.point);mapElement.focus({preventScroll:true});
}
function queueSearch(){
 clearTimeout(searchTimer);if(searchRequest)searchRequest.abort();searchRows=[];searchHover=null;searchResults.replaceChildren();searchNote.textContent='';selectSearch(0);searchInput.setAttribute('aria-expanded','false');
 const query=searchInput.value.trim();if(!query)return;
 const request=searchRequest=new AbortController();
 searchTimer=setTimeout(async()=>{
  try{
   const r=await fetch('/map/search?q='+encodeURIComponent(query),{signal:request.signal});if(!r.ok)throw Error('Search unavailable');const rows=await r.json();
   await needDots(rows.map(row=>row.name+row.position).join(''));if(request.signal.aborted)return;
   searchRows=rows;rows.forEach((row,i)=>{const item=document.createElement('div');item.id='search-result-'+i;item.setAttribute('role','option');item.textContent=row.name+' '+row.position;item.addEventListener('click',()=>chooseSearch(i));searchResults.append(item);});
   searchInput.setAttribute('aria-expanded',String(rows.length>0));searchNote.textContent=rows.length?'':'No matches';selectSearch(0);
  }catch(e){if(!request.signal.aborted){searchNote.textContent='Search unavailable';drawSearch();}}
 },150);
}
needDots(String.fromCharCode(...Array.from({length:95},(_,i)=>32+i))+ELLIPSIS).then(drawSearch);
searchInput.addEventListener('input',e=>{drawSearch();needDots(searchInput.value).then(fetched=>fetched&&drawSearch());if(!e.isComposing)queueSearch();});
for(const event of ['select','keyup','click'])searchInput.addEventListener(event,drawSearch);
document.addEventListener('selectionchange',()=>{if(document.activeElement===searchInput)drawSearch();});
function searchLine(e){const box=searchCanvas.getBoundingClientRect();return Math.floor((e.clientY-box.top)*searchCanvas.height/box.height/SEARCH.row)-1;}
searchCanvas.addEventListener('mousedown',e=>e.preventDefault());           // the field keeps the keyboard
// the map moves and answers taps beside an open search, which keeps the keys
mapElement.addEventListener('mousedown',e=>{if(!searchElement.hidden)e.preventDefault();});
searchCanvas.addEventListener('pointermove',e=>{const i=searchLine(e),[first,page]=searchPage(),hover=i>=0&&i<page.length?first+i:null;if(hover!==searchHover){searchHover=hover;highlightSearch();}});
searchCanvas.addEventListener('pointerleave',()=>{if(searchHover!==null){searchHover=null;highlightSearch();}});
searchCanvas.addEventListener('click',e=>{const i=searchLine(e),[first,page]=searchPage();if(i>=0&&i<page.length)chooseSearch(first+i);else searchInput.focus({preventScroll:true});});
// a wheel's notch turns a page; a touchpad's scroll once it has gone far enough
let searchScrolled=0;
searchElement.addEventListener('wheel',e=>{
 e.preventDefault();e.stopPropagation();
 if(e.deltaMode){turnSearch(Math.sign(e.deltaY));return;}
 searchScrolled+=e.deltaY;if(Math.abs(searchScrolled)>=SEARCH.scroll){turnSearch(Math.sign(searchScrolled));searchScrolled=0;}
},{passive:false});
searchInput.addEventListener('compositionstart',()=>{clearTimeout(searchTimer);if(searchRequest)searchRequest.abort();});
searchInput.addEventListener('compositionend',queueSearch);
searchElement.addEventListener('keydown',e=>{
 e.stopPropagation();if(e.isComposing||e.keyCode===229)return;
 if(e.key==='Escape'){e.preventDefault();closeSearch();}
 // Backspace held empties the field and stops there; a fresh press on an empty field closes it
 else if(e.target===searchInput&&e.key==='Backspace'&&!searchInput.value){e.preventDefault();if(!e.repeat)closeSearch();}
 else if(e.target===searchInput&&['ArrowUp','ArrowDown','Tab','Enter'].includes(e.key)){
  e.preventDefault();if(e.key==='Enter')chooseSearch(searchHover??searchIndex);else stepSearch(e.key==='ArrowUp'||e.key==='Tab'&&e.shiftKey?-1:1);
 }
 // a page on or back, as less and vi turn them
 else if(e.target===searchInput&&(['PageUp','PageDown'].includes(e.key)||e.ctrlKey&&['b','f'].includes(e.key.toLowerCase()))){e.preventDefault();turnSearch(['PageDown','f','F'].includes(e.key)?1:-1);}
});
searchElement.addEventListener('keyup',e=>e.stopPropagation());
document.addEventListener('keydown',e=>{
 if(e.defaultPrevented||e.isComposing||e.ctrlKey||e.altKey||e.metaKey||e.target.closest('input,textarea,select,[contenteditable]'))return;
 if((e.key.toLowerCase()==='s'||e.key==='/')&&view){e.preventDefault();if(!e.repeat)showSearch();}
},true);
stage.addEventListener('keydown',e=>{
 if(e.defaultPrevented||e.isComposing||!searchElement.hidden||e.target.closest('input,textarea,select,[contenteditable]')||e.ctrlKey||e.altKey||e.metaKey)return;
 shiftDown=e.shiftKey;
 const name=keyName(e);if(e.target.closest('button')&&name!=='Escape')return;
 if(mapKeys.has(name)&&view){
  e.preventDefault();
  if(!e.repeat&&!e.shiftKey&&(zoomIn.has(name)||zoomOut.has(name)))zoomStep(zoomIn.has(name)?1:-1);
  keys.add(name);return;
 }
 if(name==='Escape'&&profilePoint){e.preventDefault();closeProfile();return;}
 if(name==='Enter'&&view&&mapElement.contains(document.activeElement)){e.preventDefault();inspect(view.getCenter());return;}
 if(namedKeys.has(name)||name.length===1&&name.charCodeAt(0)>=33&&name.charCodeAt(0)<=126){e.preventDefault();enqueue({type:'key',key:name,down:true,ctrl:e.ctrlKey,shift:e.shiftKey});}
});
stage.addEventListener('keyup',e=>{shiftDown=e.shiftKey;const name=keyName(e);keys.delete(name);if(zoomIn.has(name)||zoomOut.has(name))for(const k of [...zoomIn,...zoomOut])keys.delete(k);if(!searchElement.hidden||name.toLowerCase()==='s'||name==='/'||e.target.closest('input,textarea,select,[contenteditable]'))return;if(mapKeys.has(name))e.preventDefault();else if(namedKeys.has(name)||name.length===1&&name.charCodeAt(0)>=33&&name.charCodeAt(0)<=126)enqueue({type:'key',key:name,down:false});});
function padState(){
 let x=0,y=0,z=0,enter=false,escape=false;
 if(navigator.getGamepads)for(const pad of navigator.getGamepads()){
  if(!pad||!pad.connected)continue;const b=i=>!!(pad.buttons[i]&&pad.buttons[i].pressed),axis=i=>{const v=pad.axes[i]||0;return Math.abs(v)<.2?0:Math.sign(v)*(Math.abs(v)-.2)/.8;};
  x+=axis(0)+(b(15)?1:0)-(b(14)?1:0);y+=axis(1)+(b(13)?1:0)-(b(12)?1:0);z+=(b(5)||b(7)?1:0)-(b(4)||b(6)?1:0);enter=enter||b(0);escape=escape||b(1);
 }
 return {x:Math.max(-1,Math.min(1,x)),y:Math.max(-1,Math.min(1,y)),z:Math.max(-1,Math.min(1,z)),enter,escape};
}
let lastTick=performance.now();
// a press of + or - zooms a step (x1.44, as two wheel notches on the native map); only held with
// Shift do they zoom on and on (with Shift, = and - come as + and _)
let shiftDown=false;
function zoomStep(step){view.animate({resolution:view.getResolution()*Math.exp(-step*2*.18),duration:150});}
function zooming(name){return shiftDown&&keys.has(name);}
function controls(now){
 const dt=Math.min(.05,(now-lastTick)/1000);lastTick=now;
 if(view&&searchElement.hidden&&!document.hidden&&document.hasFocus()){
  let pad={x:0,y:0,z:0,enter:false,escape:false};try{pad=padState();}catch(e){}
  const x=(keys.has('ArrowRight')?1:0)-(keys.has('ArrowLeft')?1:0)+pad.x,y=(keys.has('ArrowDown')?1:0)-(keys.has('ArrowUp')?1:0)+pad.y,z=([...zoomIn].some(zooming)?1:0)-([...zoomOut].some(zooming)?1:0)+pad.z;
  const moving=!!(x||y||z);if(moving&&!heldInteraction){view.beginInteraction();heldInteraction=true;}
  if(x||y){const resolution=view.getResolution();view.adjustCenter([x*700*dt*resolution,-y*700*dt*resolution]);}
  if(z)view.adjustResolution(Math.exp(-z*3*.18*dt),view.getCenter());
  if(!moving&&heldInteraction){heldInteraction=false;view.endInteraction(0);}
  if(pad.enter&&!lastPadEnter)inspect(view.getCenter());if(pad.escape&&!lastPadEscape)closeProfile();lastPadEnter=pad.enter;lastPadEscape=pad.escape;
 }
 requestAnimationFrame(controls);
}
function release(){keys.clear();shiftDown=false;if(heldInteraction&&view){heldInteraction=false;view.endInteraction(0);}enqueue({type:'release'});}
window.addEventListener('blur',release);
document.addEventListener('visibilitychange',()=>{if(document.hidden)release();else scheduleFeatures();});
window.addEventListener('pagehide',()=>{active=false;release();if(featureRequest)featureRequest.abort();if(profileRequest)profileRequest.abort();if(session)navigator.sendBeacon('/close',new Blob([JSON.stringify({session})],{type:'application/json'}));});
window.addEventListener('pageshow',e=>{if(e.persisted){active=true;session=null;startFrames();startMap();scheduleFeatures();startLive();}});
startFrames();startMap();requestAnimationFrame(controls);
