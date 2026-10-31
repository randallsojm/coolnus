import { setupImports } from './imports.js';
import { gateStatus } from './gate-status.js';
const $ = id => document.getElementById(id);
let state, selectedRole = 'atc', selectedId = 'ATC-1', selectedResource = null, toastTimer, jsonText = '';
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const timezone = () => state?.airport.configuration?.timezone || (state?.airport.id === 'DEMO' ? 'Asia/Singapore' : 'UTC');
const clock = iso => iso && Number.isFinite(Date.parse(iso)) ? new Date(iso).toLocaleTimeString('en-GB', { timeZone: timezone(), hour12: false }) : '—';
const shortTime = iso => clock(iso).slice(0, 5);
const now = () => new Date(Date.parse(state.startedAt) + state.elapsedSeconds * 1000).toISOString();
const crews = () => state.ground.crews.flat();
const resources = () => [...state.airport.runways, ...state.airport.taxiways, ...state.airport.gates];
const phase = text => `<span class="badge ${esc(text)}">${esc(text.replaceAll('_', ' '))}</span>`;
function toast(message) { $('toast').textContent = message; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4500); }
async function request(url, body) {
  try {
    const result = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    const data = await result.json(); if (!result.ok) throw new Error(data.error || 'Request failed.'); return data;
  } catch (error) { toast(error.message); throw error; }
}
async function act(command, actor = { role: 'operator', id: 'OPERATOR' }) {
  const result = await request('/api/actions', { actor, expectedRevision: state.revision, command }); render(result);
}
async function control(type, extra = {}) { render(await request('/api/control', { type, ...extra })); }
function fire(fn) { Promise.resolve().then(fn).catch(() => {}); }
function svgMap() {
  const color=r=>r.status==='closed'?'#e7a270':r.occupiedBy?'#91bdda':r.backgroundOccupied?'#617773':'#b8e78e';
  const runways=state.airport.runways, apronY=95+runways.length*80,gateY=apronY+110,height=gateY+100;
  const bucketSize=Math.ceil(state.airport.gates.length/12),buckets=[];
  for(let i=0;i<state.airport.gates.length;i+=bucketSize)buckets.push(state.airport.gates.slice(i,i+bucketSize));
  const gx=i=>130+(i+.5)*(860/buckets.length);
  const runwayShapes=runways.map((r,i)=>{const y=75+i*80;return `<g data-resource="${r.id}" role="button" tabindex="0" aria-label="Inspect runway ${esc(r.name)}"><rect x="90" y="${y}" width="940" height="36" rx="3" fill="#0b1214" stroke="${color(r)}"/><path d="M145 ${y+18}H975" stroke="#d4ddd2" stroke-width="2" stroke-dasharray="30 26" opacity=".6"/><text x="90" y="${y-12}" fill="${color(r)}" font-size="12">${esc(r.name)}</text><text x="1030" y="${y-12}" text-anchor="end" font-size="10" fill="${color(r)}">${r.status==='closed'?'CLOSED':r.occupiedBy?'RESERVED / '+esc(r.occupiedBy):'AVAILABLE'}</text></g>`}).join('');
  const taxishapes=state.airport.taxiways.map((r,i)=>{const x=i?945:175;return `<g data-resource="${r.id}" role="button" tabindex="0" aria-label="Inspect taxiway ${esc(r.name)}"><path d="M${x} 112V${apronY}H560" stroke="#263936" stroke-width="20" fill="none"/><path d="M${x} 112V${apronY}H560" stroke="${color(r)}" stroke-width="2" fill="none"/><text x="${i?800:250}" y="${apronY-15}" fill="${color(r)}" font-size="11">${r.id} / ${r.status.toUpperCase()}</text></g>`}).join('');
  const gateShapes=buckets.map((group,i)=>{const x=gx(i),occupied=group.filter(g=>g.occupiedBy).length,available=group.filter(g=>g.status==='open'&&!g.occupiedBy&&!g.backgroundOccupied).length;return `<g ${bucketSize===1?`data-resource="${group[0].id}"`:`data-gate-group="${i}"`} role="button" tabindex="0" aria-label="Inspect gates ${group[0].id} to ${group.at(-1).id}"><path d="M${x} ${apronY}V${gateY}" stroke="#718d7c" stroke-width="1.5"/><rect x="${x-34}" y="${gateY}" width="68" height="49" rx="4" fill="${occupied?'#1d3038':available?'#1a2c24':'#1c2928'}" stroke="${occupied?'#91bdda':available?'#b8e78e':'#526963'}"/><text x="${x}" y="${gateY+20}" text-anchor="middle" fill="#b4c9bc" font-size="${bucketSize===1?12:10}">${group[0].id}${bucketSize>1?'–'+group.at(-1).id:''}</text><text x="${x}" y="${gateY+37}" text-anchor="middle" fill="#8ca99a" font-size="8">${available} free · ${occupied} sim</text></g>`}).join('');
  const active=state.pilots.filter(p=>!['departed','diverted'].includes(p.phase));
  const planes=active.map((p,i)=>{
    let x=130+(i%6)*150,y=35+Math.floor(i/6)*16,angle=90;
    if(p.phase==='awaiting_stand')return '';
    if(['at_gate','ready'].includes(p.phase)){
      const gateIndex=state.airport.gates.findIndex(g=>g.id===p.gateId),bucket=Math.floor(gateIndex/bucketSize);
      if(gateIndex<0)return '';
      const groupAircraft=active.filter(a=>['at_gate','ready'].includes(a.phase)&&buckets[bucket].some(g=>g.id===a.gateId));
      const rank=groupAircraft.findIndex(a=>a.id===p.id);if(rank>=3)return '';
      x=gx(bucket)-18;y=gateY-22-rank*21;angle=0;
    }
    if(['landing','takeoff'].includes(p.phase)){const duration=p.phase==='landing'?60:45;x=150+(1-p.remainingSeconds/duration)*810;y=93+Math.max(0,runways.findIndex(r=>r.id===p.runwayId))*80;}
    else if(['taxi_in','taxi_out','holding_short'].includes(p.phase)){x=p.taxiwayId==='TWY-A'?175:945;y=apronY-25;}
    return `<g data-pilot="${p.id}" role="button" tabindex="0" aria-label="Inspect pilot ${esc(p.id)}"><path transform="translate(${x} ${y}) rotate(${angle})" d="M0-12 3-3 11 3 11 6 3 3 2 10 5 12 5 14 0 12-5 14-5 12-2 10-3 3-11 6-11 3-3-3Z" fill="#edf1e9"/><text x="${x+18}" y="${y+4}" fill="#c9d9cf" font-size="10">${esc(p.callsign)}</text></g>`;
  }).join('');
  return `<svg viewBox="0 0 1120 ${height}" xmlns="http://www.w3.org/2000/svg" aria-label="Schematic ${esc(state.airport.id)} airport: ${runways.length} runways and ${state.airport.gates.length} gates"><defs><pattern id="grid" width="25" height="25" patternUnits="userSpaceOnUse"><path d="M25 0H0V25" fill="none" stroke="#2b413c" stroke-width=".5" opacity=".4"/></pattern></defs><rect width="1120" height="${height}" fill="url(#grid)"/>${taxishapes}${runwayShapes}${gateShapes}<rect x="110" y="${gateY+65}" width="900" height="25" rx="3" fill="#23302d"/><text x="560" y="${gateY+82}" text-anchor="middle" fill="#93a891" font-size="10" letter-spacing="2">${esc(state.airport.id)} TERMINAL · SCHEMATIC GROUPS / ALL GATES IN INVENTORY</text>${planes}</svg>`;
}

let activeGateGroup=null;
function renderGateInventory(){
  const all=state.airport.gates,size=Math.ceil(all.length/12),query=$('gate-search').value.trim().toLowerCase();
  const list=all.filter((g,i)=>(activeGateGroup===null||Math.floor(i/size)===activeGateGroup)&&(!query||g.id.toLowerCase()===query||g.name.toLowerCase().includes(query)));
  $('gate-inventory-count').textContent=`(${all.length} total)`;
  $('gate-list').innerHTML=`<span class="gate-caption">${list.length} matching gates${list.length>50?' · showing first 50; search for any gate ID':''}. Background occupancy is assumed.</span>`+list.slice(0,50).map(g=>`<button data-resource="${esc(g.id)}">${esc(g.id)}<span>${g.occupiedBy?'Reserved / '+esc(g.occupiedBy):g.backgroundOccupied?'Background occupied':g.status==='closed'?'Closed':'Available'}</span></button>`).join('');
}
$('gate-search').oninput=()=>{activeGateGroup=null;renderGateInventory();};$('all-gates').onclick=()=>{activeGateGroup=null;$('gate-search').value='';renderGateInventory();};
const renderImportState=setupImports({$,esc,getState:()=>state,render,request,fire,toast,selectPilot:id=>{selectedRole='pilot';selectedId=id;}});
function render(s) {
  if (!s) return; state=s;
  $('clock').textContent=clock(now()); $('elapsed').textContent=`T+ ${String(Math.floor(s.elapsedSeconds/60)).padStart(2,'0')}:${String(s.elapsedSeconds%60).padStart(2,'0')}`;
  $('play').textContent=s.running?'Ⅱ Pause simulation':'▶ Run simulation'; $('step').disabled=s.running; $('step-long').disabled=s.running; $('speed').value=String(s.speed); $('auto-dispatch').checked=s.autoDispatch;
  const active=s.pilots.filter(p=>!['departed','diverted'].includes(p.phase)).length, runwayCount=s.airport.runways.filter(r=>r.status==='open').length;
  const freeGates=s.airport.gates.filter(g=>g.status==='open'&&!g.occupiedBy&&!g.backgroundOccupied).length, incidents=s.incidents.filter(i=>i.status==='active').length;
  $('metrics').innerHTML=[['ACTIVE AIRCRAFT',active,`${s.pilots.length} flights · ${s.data?.flightImports.length||0} imported`],['OPEN RUNWAYS',`${runwayCount}<em>/ ${s.airport.runways.length}</em>`,'Availability excludes reservations'],['AVAILABLE STANDS',`${freeGates}<em>/ ${s.airport.gates.length}</em>`,'Excludes synthetic background occupancy'],['DISRUPTIONS',incidents+(s.data?.disruptions.length||0),`${s.data?.disruptions.length||0} imported · ${incidents} active physical`]].map(([label,n,note])=>`<div class="metric"><span class="metric-label">${label}</span><div class="metric-number" ${label==='DISRUPTIONS'&&(incidents||s.data?.disruptions.length)?'style="color:var(--orange)"':''}>${n}</div><small>${note}</small></div>`).join('');
  $('map').innerHTML=svgMap(); renderGateInventory(); renderImportState(); renderGateOperations();
  $('clock-zone').textContent=`${new Date(now()).toLocaleDateString('en-CA',{timeZone:timezone()})} · ${timezone()}`; $('flight-timezone').textContent=`Fuel kg · times ${timezone()}`; $('airport-tag').textContent=`${s.airport.id} / SCHEMATIC`; $('map-label').textContent=`${s.airport.id} / SCHEMATIC`;
  if(selectedResource) renderResource();else $('resource-detail').textContent='Select a runway, taxiway, or gate group to inspect its state.';
  $('flight-count').textContent=String(s.pilots.length);
  $('flights').innerHTML=s.pilots.map(p=>`<tr data-pilot="${p.id}" tabindex="0"><td><strong>${esc(p.callsign)}</strong><small>${p.source==='historical_import'?'IMPORTED / '+esc(p.flightId):'SYNTHETIC / '+p.id}</small></td><td>${phase(p.phase)}${['at_gate','ready'].includes(p.phase)?`<small>${gateStatus(state,p).turnaroundRemainingSeconds===null?'Turnaround unknown':`Turnaround ${duration(gateStatus(state,p).turnaroundRemainingSeconds)}`}</small>`:''}${p.maintenanceHold?'<small style="color:var(--orange)">Maintenance hold</small>':''}</td><td>${Math.round(p.fuelRemainingKg).toLocaleString()}<div class="fuel-bar"><i style="width:${Math.min(100,p.fuelRemainingKg/5000*100)}%;${p.fuelRemainingKg<1200?'background:var(--orange)':''}"></i></div></td><td>${shortTime(p.expectedArrival)}</td><td>${shortTime(p.expectedDeparture)}</td><td>${esc(p.gateId||'—')}</td></tr>`).join('');
  const titles=['Emergency response','Mechanics','Inspectors'];
  $('crews').innerHTML=s.ground.crews.map((group,i)=>`<div class="crew-group"><div class="crew-title"><i class="dot ${['orange','blue','green'][i]}"></i>${titles[i]} <span>${group.length}</span></div>${group.map(c=>{const t=s.tasks.find(t=>t.id===c.currentTaskId);return `<div class="crew-card" data-crew="${c.id}" role="button" tabindex="0"><div class="crew-card-top"><span class="crew-id">${c.id}</span>${phase(c.status)}</div><p>${t?esc(t.title):esc(c.name)}<br>${esc(c.location)}${t?` · ${t.remainingSeconds}s remaining`:''}</p>${t?`<div class="task-progress"><i style="width:${(1-t.remainingSeconds/t.totalSeconds)*100}%"></i></div>`:''}</div>`}).join('')}</div>`).join('');
  $('events').innerHTML=s.events.map(e=>`<div class="event ${esc(e.kind)}"><time>${clock(e.at)}</time><i class="event-dot"></i><span>${esc(e.message)}</span></div>`).join(''); $('event-count').textContent=`${s.events.length} events retained`;
  renderTargets(); renderAgents(); void refreshObservation();
}
const duration = seconds => `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
function renderGateOperations() {
  const parked=state.pilots.filter(p=>['at_gate','ready','awaiting_stand'].includes(p.phase));
  const select=$('gate-flight-select'),previous=select.value;
  $('gate-operation-count').textContent=`${parked.length} PARKED / WAITING`;
  const options=parked.map(p=>`<option value="${esc(p.id)}">${esc(p.callsign)} · ${esc(p.gateId||'Awaiting stand')}</option>`).join('');
  if(select.innerHTML!==options){select.innerHTML=options;if(parked.some(p=>p.id===previous))select.value=previous;}
  select.disabled=!parked.length;
  const p=parked.find(p=>p.id===select.value);
  if(!p){$('gate-operations').innerHTML='<p class="gate-empty">No parked aircraft. A countdown appears when an aircraft reaches its gate.</p>';return;}
  const status=gateStatus(state,p),unknown=status.turnaroundRemainingSeconds===null;
  const label=p.phase==='awaiting_stand'?'Waiting for a stand':unknown?'Turnaround history unavailable':status.turnaroundRemainingSeconds>0?'Turnaround remaining':'Turnaround complete';
  $('gate-operations').innerHTML=`<div class="gate-status-top"><div><span>${esc(p.callsign)} · ${esc(p.gateId||'No stand assigned')}</span><strong>${esc(label)}</strong></div><div class="gate-countdown ${unknown?'unknown':''}">${unknown?'Unknown':duration(status.turnaroundRemainingSeconds)}<small>${unknown?'No preceding on-block time':'minutes : seconds'}</small></div></div>
    <div class="gate-release"><strong>${status.readyForPushback?'Ready for pushback on next simulation step':'Pushback waiting'}</strong><span>${status.blockers.map(esc).join(' · ')||'Timing, readiness and outbound route checks passed.'}</span>${status.departureWaitSeconds?`<span>Departure timing release ${shortTime(status.departureReleaseAt)} · ${duration(status.departureWaitSeconds)} away</span>`:''}</div>
    <p class="gate-assumption">${esc(status.basis)} ${unknown?'The imported departure delay still applies.':'This timer advances with simulation time. Gate occupancy continues until pushback; a completed countdown does not override departure restrictions.'}</p>
    <button id="inspect-gate-pilot" class="small-btn quiet">Inspect ${esc(p.callsign)} JSON</button>`;
  $('inspect-gate-pilot').onclick=()=>{selectedRole='pilot';selectedId=p.id;renderAgents();void refreshObservation();};
}
$('gate-flight-select').onchange=renderGateOperations;

function renderTargets() {
  const kind=$('incident-kind').value, previous=$('incident-target').value;
  const list=kind==='aircraft_fault'?state.pilots.filter(p=>['at_gate','ready'].includes(p.phase)&&!p.maintenanceHold).map(p=>({id:p.id,name:p.callsign})):
    (kind==='surface_debris'?[...state.airport.runways,...state.airport.taxiways]:state.airport.runways).filter(r=>r.status==='open'&&!r.occupiedBy);
  const html=list.map(r=>`<option value="${r.id}">${esc(r.name)} · ${r.id}</option>`).join('')||'<option value="">No eligible target</option>';
  if ($('incident-target').innerHTML!==html) { $('incident-target').innerHTML=html; if(list.some(r=>r.id===previous)) $('incident-target').value=previous; }
  $('inject').disabled=!$('incident-target').value;
}
function renderResource() {
  const r=resources().find(r=>r.id===selectedResource); if(!r)return;
  const incident=state.incidents.some(i=>i.status==='active'&&i.targetId===r.id);
  $('resource-detail').innerHTML=`<strong>${esc(r.id)}</strong><span>${esc(r.name)} · ${r.status.toUpperCase()} · ${esc(r.occupiedBy?'Reserved by '+r.occupiedBy:r.backgroundOccupied?'Synthetic background occupancy':'No reservation')}</span><button id="resource-toggle" ${r.occupiedBy||r.backgroundOccupied||incident?'disabled':''}>${r.status==='open'?'Close resource':'Reopen resource'}</button>`;
  $('resource-toggle').onclick=()=>fire(()=>act({type:'resource_status',targetId:r.id,status:r.status==='open'?'closed':'open'}));
}
function renderAgents() {
  document.querySelectorAll('[data-role]').forEach(b=>b.classList.toggle('active',b.dataset.role===selectedRole));
  const options=selectedRole==='atc'?[{id:'ATC-1',label:'ATC-1 · Entire airport'}]:selectedRole==='pilot'?state.pilots.map(p=>({id:p.id,label:`${p.id} · ${p.callsign} · ${p.name}`})):crews().map(c=>({id:c.id,label:`${c.id} · ${c.type.replaceAll('_',' ')}`}));
  if(!options.some(o=>o.id===selectedId))selectedId=options[0].id;
  const html=options.map(o=>`<option value="${o.id}">${esc(o.label)}</option>`).join('');
  if($('agent-select').innerHTML!==html)$('agent-select').innerHTML=html;
  $('agent-select').value=selectedId;
  $('scope-card').innerHTML=selectedRole==='atc'?'<strong>Global observation</strong>All flights, resources, incidents and the three nested crew lists, in one canonical format.':selectedRole==='pilot'?'<strong>Individual pilot observation</strong>Own aircraft, fuel, expected times, assigned resources and relevant restrictions.':'<strong>Individual personnel observation</strong>Own availability, location and assignment. Queued tasks are filtered to this person’s crew type.';
}
let observationRequest=0;
async function refreshObservation() {
  const index=++observationRequest;
  const endpoint=selectedRole==='atc'?'/api/views/atc':`/api/views/${selectedRole==='pilot'?'pilots':'ground'}/${selectedId}`;
  try {
    const response=await fetch(endpoint);const obs=await response.json();if(!response.ok||index!==observationRequest)return;
    jsonText=JSON.stringify(obs,null,2);const scroll=$('json').scrollTop;$('json').textContent=jsonText;$('json').scrollTop=scroll;
    $('revision').textContent=`REVISION ${obs.revision}`;$('endpoint').textContent=`GET ${endpoint}`;
    renderAgentActions(obs);
  }catch{ /* EventSource will report a disconnection; no repeated toast on polling. */ }
}
function renderAgentActions(obs) {
  if(selectedRole==='pilot') {
    const can=['approach','holding','ready','holding_short'].includes(obs.pilot.phase);
    $('agent-actions').innerHTML=`<span class="eyebrow">MANUAL ADAPTER / NO LLM AGENT</span><button id="pilot-hold" ${!can?'disabled':''}>Request a 60-second hold</button>`;
    $('pilot-hold').onclick=()=>fire(()=>act({type:'hold',pilotId:selectedId,seconds:60},{role:'pilot',id:selectedId}));
  } else if(selectedRole==='ground') {
    const crew=obs.crew;
    $('agent-actions').innerHTML=`<span class="eyebrow">MANUAL ADAPTER / NO LLM AGENT</span>${obs.availableTasks.length?`<select id="task-select" aria-label="Task to claim">${obs.availableTasks.map(t=>`<option value="${t.id}">${esc(t.id+' · '+t.title)}</option>`).join('')}</select><button id="claim-task" ${crew.status!=='available'?'disabled':''}>Assign this person to selected task</button>`:''}<button id="crew-availability" ${crew.status==='busy'?'disabled':''}>${crew.status==='off_shift'?'Return to available':'Mark off shift'}</button>`;
    if($('claim-task'))$('claim-task').onclick=()=>fire(()=>act({type:'assign_task',crewId:selectedId,taskId:$('task-select').value},{role:'ground',id:selectedId}));
    $('crew-availability').onclick=()=>fire(()=>act({type:'crew_availability',crewId:selectedId,available:crew.status==='off_shift'},{role:'ground',id:selectedId}));
  } else $('agent-actions').innerHTML='';
}
document.addEventListener('click',e=>{
  const group=e.target.closest('[data-gate-group]');if(group){activeGateGroup=Number(group.dataset.gateGroup);$('gate-inventory').open=true;renderGateInventory();}
  const resource=e.target.closest('[data-resource]'),pilot=e.target.closest('[data-pilot]'),crew=e.target.closest('[data-crew]'),tab=e.target.closest('[data-role]');
  if(resource){selectedResource=resource.dataset.resource;renderResource();}
  if(pilot){selectedRole='pilot';selectedId=pilot.dataset.pilot;renderAgents();void refreshObservation();}
  if(crew){selectedRole='ground';selectedId=crew.dataset.crew;renderAgents();void refreshObservation();}
  if(tab){selectedRole=tab.dataset.role;renderAgents();void refreshObservation();}
});
document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-resource],[data-pilot],[data-crew],[data-gate-group]')){e.preventDefault();e.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}});
$('play').onclick=()=>fire(()=>control(state.running?'pause':'play'));
$('step').onclick=()=>fire(()=>control('step',{seconds:30}));
$('step-long').onclick=()=>fire(()=>control('step',{seconds:1800}));
$('speed').onchange=e=>fire(()=>control('speed',{speed:Number(e.target.value)}));
$('reset').onclick=()=>{if(confirm('Reset the scenario, including incidents, crew assignments and simulation time?')){selectedResource=null;fire(()=>control('reset'));}};
$('incident-kind').onchange=renderTargets;
$('inject').onclick=()=>fire(async()=>{await act({type:'incident',kind:$('incident-kind').value,targetId:$('incident-target').value});toast('Disruption injected. Track the response in Ground personnel.');});
$('auto-dispatch').onchange=e=>fire(()=>act({type:'auto_dispatch',enabled:e.target.checked}));
$('agent-select').onchange=e=>{selectedId=e.target.value;void refreshObservation();};
$('copy').onclick=()=>fire(async()=>{await navigator.clipboard.writeText(jsonText);toast('Agent observation copied.');});
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(state,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='meridian-state.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
const stream=new EventSource('/api/events');
stream.onmessage=e=>{render(JSON.parse(e.data));$('connection').textContent='STATE CONNECTED';};
stream.onerror=()=>{$('connection').textContent='RECONNECTING';};
fire(async()=>render(await request('/api/state')));
