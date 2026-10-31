import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { initialState, advance, command, observation, assertState, allCrews } from '../src/simulator.mjs';
import { createSimulatorServer } from '../server.mjs';

test('Ground is three independent lists with multiple identifiable personnel',()=>{
  const s=initialState();assert.equal(s.ground.crews.length,3);s.ground.crews.forEach(g=>assert.equal(g.length,2));
  assert.deepEqual(s.ground.crews.map(g=>g[0].type),['emergency_response','mechanic','inspector']);
  assert.equal(new Set(allCrews(s).map(c=>c.id)).size,6);
});
test('advancing is deterministic and fuel/time update together',()=>{
  const s=initialState();assert.deepEqual(advance(s,180),advance(s,180));
  const a=advance(s,30);assert.equal(a.elapsedSeconds,30);assert.ok(Math.abs(a.pilots[2].fuelRemainingKg-(s.pilots[2].fuelRemainingKg-21))<1e-8);
  assert.equal(s.elapsedSeconds,0);
});
test('closed runways prevent landings and resource reservations cannot be closed',()=>{
  let s=initialState();for(const id of ['RWY-1','RWY-2'])s=command(s,{type:'resource_status',targetId:id,status:'closed'});
  s=advance(s,150);assert.equal(s.pilots[2].phase,'holding');assert.ok(s.airport.runways.every(r=>!r.occupiedBy));
  s=command(s,{type:'resource_status',targetId:'RWY-1',status:'open'});s=advance(s,1);
  assert.equal(s.pilots[2].phase,'landing');assert.throws(()=>command(s,{type:'resource_status',targetId:'RWY-1',status:'closed'}),/occupied/);
});
test('staged incident uses responder, mechanic, inspector in order before reopening',()=>{
  let s=command(initialState(),{type:'incident',kind:'disabled_aircraft',targetId:'RWY-1'});
  assert.equal(s.tasks[0].assigneeId,'ER-1');assert.equal(s.tasks[1].status,'queued');
  assert.throws(()=>command(s,{type:'resource_status',targetId:'RWY-1',status:'open'}),/Active incident/);
  s=advance(s,90);assert.equal(s.tasks[1].assigneeId,'ME-1');assert.equal(s.tasks[2].status,'queued');
  s=advance(s,180);assert.equal(s.tasks[2].assigneeId,'IN-1');assert.equal(s.airport.runways[0].status,'closed');
  s=advance(s,120);assert.equal(s.incidents[0].status,'resolved');assert.equal(s.airport.runways[0].status,'open');
});
test('parallel inspections dispatch different inspectors, not one shared person',()=>{
  let s=command(initialState(),{type:'incident',kind:'bird_strike',targetId:'RWY-1'});
  s=command(s,{type:'incident',kind:'bird_strike',targetId:'RWY-2'});
  assert.deepEqual(s.tasks.map(t=>t.assigneeId),['IN-1','IN-2']);
});
test('manual dispatch checks specialties, dependencies and individual permissions',()=>{
  let s=command(initialState(),{type:'auto_dispatch',enabled:false});
  s=command(s,{type:'incident',kind:'disabled_aircraft',targetId:'RWY-1'});
  const tasks=s.tasks;
  assert.throws(()=>command(s,{type:'assign_task',crewId:'ME-1',taskId:tasks[0].id}),/different crew/);
  assert.throws(()=>command(s,{type:'assign_task',crewId:'ME-1',taskId:tasks[1].id}),/prerequisites/);
  assert.throws(()=>command(s,{type:'assign_task',crewId:'ER-2',taskId:tasks[0].id},{role:'ground',id:'ER-1'}),/themselves/);
  s=command(s,{type:'assign_task',crewId:'ER-2',taskId:tasks[0].id},{role:'ground',id:'ER-2'});
  assert.equal(s.tasks[0].assigneeId,'ER-2');
});
test('individual agent observations are scoped; ATC includes all nested crews',()=>{
  const s=initialState(),atc=observation(s,'atc','ATC-1'),pilot=observation(s,'pilot','P-1'),crew=observation(s,'ground','ME-2');
  assert.equal(atc.ground.crews.flat().length,6);assert.equal(atc.pilots.length,6);
  assert.equal(pilot.pilot.id,'P-1');assert.equal(pilot.pilots,undefined);assert.equal(pilot.ground,undefined);
  assert.equal(crew.crew.id,'ME-2');assert.equal(crew.pilots,undefined);assert.equal(crew.ground,undefined);
  atc.pilots[0].fuelRemainingKg=0;assert.ok(s.pilots[0].fuelRemainingKg>0);
});
test('agent actions reject stale state and cross-role commands',()=>{
  const s=initialState();assert.throws(()=>command(s,{type:'hold',pilotId:'P-2',seconds:60},{role:'pilot',id:'P-1'}),/own aircraft/);
  assert.throws(()=>command(s,{type:'incident',kind:'bird_strike',targetId:'RWY-1'},{role:'pilot',id:'P-1'}),/not allowed/);
  assert.throws(()=>command(s,{type:'hold',pilotId:'P-3',seconds:60},{role:'pilot',id:'P-3'},99),/Stale revision/);
});
test('aircraft fault pins turnaround until mechanic release',()=>{
  let s=command(initialState(),{type:'incident',kind:'aircraft_fault',targetId:'P-1'});
  s=advance(s,200);assert.equal(s.pilots[0].phase,'at_gate');assert.equal(s.pilots[0].maintenanceHold,true);
  s=advance(s,40);assert.equal(s.pilots[0].maintenanceHold,false);assert.notEqual(s.pilots[0].phase,'at_gate');
});
test('long running scenario preserves resource and crew invariants',()=>{
  let s=command(initialState(),{type:'incident',kind:'disabled_aircraft',targetId:'RWY-1'});
  for(let i=0;i<120;i++){s=advance(s,30);assertState(s);}
  assert.ok(s.pilots.every(p=>['departed','diverted'].includes(p.phase)));
  assert.ok([...s.airport.runways,...s.airport.taxiways,...s.airport.gates].every(r=>!r.occupiedBy));
});
test('fuel reserve fallback removes blocked arrivals from queue',()=>{
  let s=initialState();s.pilots[2].fuelRemainingKg=901;
  s=advance(s,2);assert.equal(s.pilots[2].phase,'diverted');assert.ok(s.events.some(e=>e.kind==='critical'));
});
test('HTTP views, scoped actions, validation, reset and restart persistence',async()=>{
  const dir=mkdtempSync(path.join(tmpdir(),'coolnus-test-'));const dataFile=path.join(dir,'state.json');
  const a=createSimulatorServer({clock:false,dataFile});await new Promise(r=>a.server.listen(0,'127.0.0.1',r));
  const base=`http://127.0.0.1:${a.server.address().port}`;
  try {
    const agents=await (await fetch(base+'/api/agents')).json();assert.equal(agents.ground[1][1].id,'ME-2');
    const post=(endpoint,data)=>fetch(base+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const step=await post('/api/control',{type:'step',seconds:30});assert.equal(step.status,200);
    let s=await step.json();assert.equal(s.elapsedSeconds,30);
    const stale=await post('/api/actions',{actor:{role:'pilot',id:'P-3'},expectedRevision:0,command:{type:'hold',pilotId:'P-3',seconds:30}});assert.equal(stale.status,409);
    const view=await (await fetch(base+'/api/views/ground/ME-2')).json();assert.equal(view.crew.id,'ME-2');assert.equal(view.pilots,undefined);
    const invalid=await post('/api/control',{type:'step',seconds:-1});assert.equal(invalid.status,400);
    const b=createSimulatorServer({clock:false,dataFile});assert.equal(b.getState().elapsedSeconds,30);assert.equal(b.getState().running,false);
    const oldRevision=s.revision;s=await (await post('/api/control',{type:'reset'})).json();assert.ok(s.revision>oldRevision);assert.equal(s.elapsedSeconds,0);
  }finally{await new Promise(r=>a.server.close(r));rmSync(dir,{recursive:true,force:true});}
});
