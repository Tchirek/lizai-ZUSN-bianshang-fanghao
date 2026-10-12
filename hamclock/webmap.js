'use strict';
const stage=document.querySelector('#stage'), clock=document.querySelector('#clock'), ctx=clock.getContext('2d');
const mapElement=document.querySelector('#map'), profileElement=document.querySelector('#profile');
const mapKeys=new Set(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','=','-','PageUp','PageDown']);
const namedKeys=new Set([...mapKeys,'Enter','Tab','Escape','Backspace','Space']);
let session=null, revision=-1, active=true, creating=null, sending=false, events=[], frameTask=null;
let map=null, config=null, view=null, initialResolution=1, featureRequest=null, profileRequest=null, profilePoint=null;
let featureTimer=0, profileSerial=0, lastMinute=-1, heldInteraction=false, lastPadEnter=false, lastPadEscape=false, liveTask=null, mapTask=null;
const keys=new Set(), sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const staticSource=new ol.source.Vector(), gridSource=new ol.source.Vector(), liveSource=new ol.source.Vector();
const ringSource=new ol.source.Vector(), selectedSource=new ol.source.Vector();
const geojson=new ol.format.GeoJSON(), styles=new Map();
const colours={river:'#4678d2',boundary:'#ffffff',field:'rgba(175,180,255,.65)',square:'rgba(175,180,255,.35)',subsquare:'rgba(175,180,255,.18)',grid_name:'#b9beff',subgrid_name:'#9196d2',city:'#e6e6e6',town:'#969696',campuses:'#ff82d2',military:'#ff5546',government:'#f0d682',regulators:'#5ad7ff',embassies:'#96faaa',airport:'#96afff',runway:'#cdcdd2',far:'#bfce25',station:'#ffffff',aprs:'#00b4dc',aprs_path:'rgba(0,180,220,.5)',qso:'#ffffff'};

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
clock.addEventListener('pointerdown',e=>{clock.focus({preventScroll:true});pressed=clockPoint(e);clock.setPointerCapture(e.pointerId);});
clock.addEventListener('pointerup',e=>{const p=clockPoint(e);if(pressed&&Math.hypot(p.x-pressed.x,p.y-pressed.y)<=5)enqueue({type:'tap',...p,button:e.button+1,ctrl:e.ctrlKey});pressed=null;});
clock.addEventListener('pointercancel',()=>{pressed=null;});
clock.addEventListener('pointermove',e=>{if(performance.now()-lastHover>150){lastHover=performance.now();enqueue({type:'hover',...clockPoint(e)});}});
clock.addEventListener('contextmenu',e=>e.preventDefault());

function zoomRatio(){return view?Math.max(1,Math.min(96,initialResolution/view.getResolution())):1;}
function colour(f){return f.get('colour')||colours[f.get('kind')]||'#dddddd';}
function geometryStyle(f){
 const kind=f.get('kind'),type=f.getGeometry().getType(),level=f.get('level'),z=zoomRatio();
 if(type==='Point'||kind==='boundary'&&((level===4&&z>=3)||(level===5&&z>=10)))return null;
 const c=colour(f),width=kind==='runway'?Math.max(2,Math.round((f.get('width')||0)/view.getResolution())):kind==='river'||kind==='boundary'&&level!==6?2:1,key=kind+':'+c+':'+level+':'+f.get('local')+':'+width;
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
 const kind=f.get('kind'),name=f.get('name')||'',c=colour(f),rf=f.get('rf'),country=f.get('country'),key=kind+':'+name+':'+c+':'+rf+':'+country;
 if(!styles.has(key)){
  const grid=kind==='grid_name'||kind==='subgrid_name';
  let image;
  if(country)image=new ol.style.Icon({src:'/map/flags/'+country+'.png',anchor:[1,.5]});
  else if(!grid){
   if(kind==='digital'||kind==='qso'||kind==='regulators')image=new ol.style.RegularShape({points:4,radius:kind==='digital'?5:4,angle:kind==='digital'?0:Math.PI/4,fill:new ol.style.Fill({color:c}),stroke:new ol.style.Stroke({color:'#000',width:1})});
   else image=new ol.style.Circle({radius:kind==='station'?4:kind==='airport'?4:kind==='city'?2:3,fill:new ol.style.Fill({color:kind==='aprs'?(rf?'#00dcff':'#00647d'):c}),stroke:new ol.style.Stroke({color:kind==='airport'?c:'#000',width:1})});
  }
  const text=name?new ol.style.Text({text:name,font:(grid?'bold 11':'bold 13')+'px Arial,"Microsoft YaHei","PingFang SC",sans-serif',fill:new ol.style.Fill({color:c}),stroke:new ol.style.Stroke({color:'#000',width:3}),offsetX:country?5:grid?3:7,offsetY:grid?8:-8,textAlign:'left',overflow:false}):undefined;
  styles.set(key,new ol.style.Style({image,text}));
 }
 return styles.get(key);
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
  if(!signal.aborted){styles.clear();replace(staticSource,features);replace(gridSource,grid);}
 }catch(e){}
}
function scheduleFeatures(){clearTimeout(featureTimer);featureTimer=setTimeout(updateFeatures,180);}
function rings(){
 if(!view)return;
 const wanted=view.getResolution()*180,power=10**Math.floor(Math.log10(wanted)),step=Math.max(...[1,2,5].map(x=>x*power).filter(x=>x<=wanted));
 const e=view.calculateExtent(map.getSize()),centre=view.getCenter(),distance=Math.hypot(...centre),span=Math.hypot(e[2]-e[0],e[3]-e[1])/2;
 const features=[];for(let r=Math.max(step,Math.floor((distance-span)/step)*step);r<distance+span;r+=step)features.push(new ol.Feature(new ol.geom.Circle([0,0],r)));
 ringSource.clear(true);ringSource.addFeatures(features);
 const resolution=view.getResolution(),index=config.levels.findIndex(level=>config.metres_per_pixel/level<=resolution),i=index<0?config.levels.length-1:index;
 document.querySelector('#dem').textContent='DEM '+Math.round(config.dem[i])+' m  '+zoomRatio().toFixed(1)+'×';
}
let nightLayer=null;
function startLive(){if(!liveTask)liveTask=live().finally(()=>{liveTask=null;if(active&&map)startLive();});}
async function live(){
 while(active){
  if(!document.hidden&&map){try{
   const r=await fetch('/map/live');if(!r.ok)throw Error('live');const data=await r.json();replace(liveSource,data.features);
   const sun=document.querySelector('#sun');sun.replaceChildren();const image=document.createElement('img');image.src='/map/sun.png?v='+data.minute;image.style.opacity=data.sun.elevation>-.833?'1':'.43';sun.append(image);
   for(const [name,value] of data.sun.parts){const item=document.createElement('span');item.textContent=name;const b=document.createElement('b');b.textContent=value;item.append(b);sun.append(item);}
   const legend=document.querySelector('#legend');legend.replaceChildren();
   for(const item of [{name:'APRS '+data.legend.aprs,colour:'#00dcff'},{name:'DIGI '+data.legend.digital,colour:'#fff'},{name:'PATH '+data.legend.paths+'/'+data.legend.total,colour:'#999'},...data.legend.bands.map(b=>({name:b.name.endsWith('cm')?b.name:b.name+'m',colour:b.colour}))]){const span=document.createElement('span'),i=document.createElement('i');i.style.background=item.colour;span.append(i,document.createTextNode(item.name));legend.append(span);}
   if(lastMinute!==data.minute){lastMinute=data.minute;nightLayer.setSource(new ol.source.ImageStatic({url:'/map/night.png?v='+lastMinute,projection:config.projection,imageExtent:config.extent}));}
  }catch(e){}}
  await sleep(15000);
 }
}
async function initMap(){
 while(active&&!map){try{
  const r=await fetch('/map/config');if(!r.ok)throw Error('config');config=await r.json();
  config.projection=new ol.proj.Projection({code:'INFLU:AEQD',units:'m',extent:config.extent});
  const resolutions=config.levels.map(level=>config.metres_per_pixel/level);
  const tileGrid=new ol.tilegrid.TileGrid({extent:config.extent,origin:[config.extent[0],config.extent[3]],resolutions,tileSize:config.tile_size});
  const tiles=new ol.layer.Tile({source:new ol.source.XYZ({projection:config.projection,tileGrid,wrapX:false,tileUrlFunction:c=>'/map/tiles/'+config.levels[c[0]]+'/'+c[1]+'/'+c[2]+'.png?v='+config.tile_version})});
  initialResolution=(config.extent[2]-config.extent[0])/mapElement.clientWidth;
  view=new ol.View({projection:config.projection,center:[0,0],extent:config.extent,enableRotation:false,maxResolution:initialResolution,minResolution:initialResolution/96,resolution:initialResolution,constrainResolution:false,smoothExtentConstraint:false});
  const vector=source=>new ol.layer.Vector({source,style:geometryStyle,renderBuffer:150});
  const labels=source=>new ol.layer.Vector({source,style:labelStyle,declutter:'labels',renderBuffer:150});
  nightLayer=new ol.layer.Image();
  map=new ol.Map({target:mapElement,view,controls:[new ol.control.ScaleLine({units:'metric'})],interactions:ol.interaction.defaults.defaults({altShiftDragRotate:false,pinchRotate:false,keyboard:false}),layers:[tiles,vector(staticSource),vector(gridSource),new ol.layer.Vector({source:ringSource,style:new ol.style.Style({stroke:new ol.style.Stroke({color:'rgba(190,190,190,.35)',width:1})})}),vector(liveSource),nightLayer,labels(gridSource),labels(staticSource),labels(liveSource),new ol.layer.Vector({source:selectedSource,style:new ol.style.Style({stroke:new ol.style.Stroke({color:'#e6e6b4',width:1}),image:new ol.style.RegularShape({points:4,radius:6,radius2:0,angle:0,stroke:new ol.style.Stroke({color:'#fff',width:1})})})})]});
  map.on('singleclick',event=>{mapElement.focus({preventScroll:true});inspect(event.coordinate);});
  map.on('moveend',()=>{scheduleFeatures();rings();positionProfile();});
  map.on('postrender',positionProfile);view.on('change:resolution',rings);
  updateFeatures();rings();startLive();
 }catch(e){console.warn('Map setup',e.message);await sleep(1000);}}
}
function startMap(){if(!map&&!mapTask)mapTask=initMap().finally(()=>{mapTask=null;if(active&&!map)startMap();});}

function closeProfile(){profileSerial++;if(profileRequest)profileRequest.abort();profilePoint=null;profileElement.hidden=true;selectedSource.clear();}
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
 profilePoint=point.slice();profileElement.hidden=false;table({heights:[10,20,40,80],frequencies:[144.174,144.640,145.025,435]});
 for(const id of ['position','loss','nearby','conditions'])document.querySelector('#'+id).textContent='';
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
stage.addEventListener('keydown',e=>{
 const name=keyName(e);if(e.target.closest('button')&&name!=='Escape')return;
 if(mapKeys.has(name)&&view){e.preventDefault();keys.add(name);return;}
 if(name==='Escape'&&profilePoint){e.preventDefault();closeProfile();return;}
 if(name==='Enter'&&view&&mapElement.contains(document.activeElement)){e.preventDefault();inspect(view.getCenter());return;}
 if(namedKeys.has(name)||name.length===1&&name.charCodeAt(0)>=33&&name.charCodeAt(0)<=126){e.preventDefault();enqueue({type:'key',key:name,down:true,ctrl:e.ctrlKey,shift:e.shiftKey});}
});
stage.addEventListener('keyup',e=>{const name=keyName(e);if(mapKeys.has(name)){e.preventDefault();keys.delete(name);}else if(namedKeys.has(name)||name.length===1&&name.charCodeAt(0)>=33&&name.charCodeAt(0)<=126)enqueue({type:'key',key:name,down:false});});
function padState(){
 let x=0,y=0,z=0,enter=false,escape=false;
 if(navigator.getGamepads)for(const pad of navigator.getGamepads()){
  if(!pad||!pad.connected)continue;const b=i=>!!(pad.buttons[i]&&pad.buttons[i].pressed),axis=i=>{const v=pad.axes[i]||0;return Math.abs(v)<.2?0:Math.sign(v)*(Math.abs(v)-.2)/.8;};
  x+=axis(0)+(b(15)?1:0)-(b(14)?1:0);y+=axis(1)+(b(13)?1:0)-(b(12)?1:0);z+=(b(5)||b(7)?1:0)-(b(4)||b(6)?1:0);enter=enter||b(0);escape=escape||b(1);
 }
 return {x:Math.max(-1,Math.min(1,x)),y:Math.max(-1,Math.min(1,y)),z:Math.max(-1,Math.min(1,z)),enter,escape};
}
let lastTick=performance.now();
function controls(now){
 const dt=Math.min(.05,(now-lastTick)/1000);lastTick=now;
 if(view&&!document.hidden&&document.hasFocus()){
  let pad={x:0,y:0,z:0,enter:false,escape:false};try{pad=padState();}catch(e){}
  const x=(keys.has('ArrowRight')?1:0)-(keys.has('ArrowLeft')?1:0)+pad.x,y=(keys.has('ArrowDown')?1:0)-(keys.has('ArrowUp')?1:0)+pad.y,z=(keys.has('+')||keys.has('=')||keys.has('PageUp')?1:0)-(keys.has('-')||keys.has('PageDown')?1:0)+pad.z;
  const moving=!!(x||y||z);if(moving&&!heldInteraction){view.beginInteraction();heldInteraction=true;}
  if(x||y){const resolution=view.getResolution();view.adjustCenter([x*700*dt*resolution,-y*700*dt*resolution]);}
  if(z)view.adjustResolution(Math.exp(-z*3*.18*dt),view.getCenter());
  if(!moving&&heldInteraction){heldInteraction=false;view.endInteraction(0);}
  if(pad.enter&&!lastPadEnter)inspect(view.getCenter());if(pad.escape&&!lastPadEscape)closeProfile();lastPadEnter=pad.enter;lastPadEscape=pad.escape;
 }
 requestAnimationFrame(controls);
}
function release(){keys.clear();if(heldInteraction&&view){heldInteraction=false;view.endInteraction(0);}enqueue({type:'release'});}
window.addEventListener('blur',release);
document.addEventListener('visibilitychange',()=>{if(document.hidden)release();else scheduleFeatures();});
window.addEventListener('pagehide',()=>{active=false;release();if(featureRequest)featureRequest.abort();if(profileRequest)profileRequest.abort();if(session)navigator.sendBeacon('/close',new Blob([JSON.stringify({session})],{type:'application/json'}));});
window.addEventListener('pageshow',e=>{if(e.persisted){active=true;session=null;startFrames();startMap();scheduleFeatures();startLive();}});
startFrames();startMap();requestAnimationFrame(controls);
