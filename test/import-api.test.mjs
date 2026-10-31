import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createSimulatorServer } from '../server.mjs';

const airports=fs.readFileSync(new URL('../examples/airports.csv',import.meta.url),'utf8');
const flights=fs.readFileSync(new URL('../examples/flights.csv',import.meta.url),'utf8');
test('HTTP import workflow is preview-only until explicit selection; persistence and views share state',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'coolnus-import-')),dataFile=path.join(dir,'state.json');
  const instance=createSimulatorServer({dataFile,clock:false});await new Promise(r=>instance.server.listen(0,'127.0.0.1',r));
  const base=`http://127.0.0.1:${instance.server.address().port}`;
  const get=async url=>{const r=await fetch(base+url);return {status:r.status,body:await r.json()};};
  const post=async(url,data)=>{const r=await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});return {status:r.status,body:await r.json()};};
  try{
    const initial=await get('/api/state');
    const preview=await post('/api/import/airports/preview',{text:airports,capacityScope:'airport'});assert.equal(preview.status,200);assert.equal(preview.body.airports.length,20);
    assert.deepEqual((await get('/api/state')).body,initial.body);
    const configured=await post('/api/import/airports/apply',{text:airports,airportCode:'DFW',capacityScope:'airport',expectedRevision:0});assert.equal(configured.status,200);assert.equal(configured.body.airport.gates.length,150);
    const rows=await post('/api/import/flights/preview',{text:flights});assert.equal(rows.body.rowCount,19);assert.equal((await get('/api/state')).body.pilots.length,6);
    const noSelection=await post('/api/import/flights/inject',{text:flights,expectedRevision:1});assert.equal(noSelection.status,400);
    assert.deepEqual((await get('/api/state')).body,configured.body);
    const injected=await post('/api/import/flights/inject',{text:flights,rowIndex:0,expectedRevision:1,rebaseClock:true});assert.equal(injected.status,200);
    const s=injected.body;assert.equal(s.data.flightImports.length,1);assert.equal(s.pilots.length,7);assert.equal(s.airport.gates.filter(g=>!g.occupiedBy&&!g.backgroundOccupied).length,2);
    assert.deepEqual(JSON.parse(fs.readFileSync(dataFile,'utf8')),s);
    const atc=await get('/api/views/atc');assert.equal(atc.body.importedScenarios[0].id,s.data.flightImports[0].id);assert.equal(atc.body.revision,s.revision);
    const pilot=await get('/api/views/pilots/'+s.pilots.at(-1).id);assert.equal(pilot.body.importedScenario.normalized.callsign,'AA5');
    const ground=await get('/api/views/ground/ME-1');assert.equal(ground.body.delayDisruptions[0].appliedDelayMinutes,289);assert.equal(ground.body.pilots,undefined);
    const duplicate=await post('/api/import/flights/inject',{text:flights,rowIndex:0,expectedRevision:s.revision});assert.equal(duplicate.status,400);assert.deepEqual((await get('/api/state')).body,s);
    const stale=await post('/api/import/airports/apply',{text:airports,airportCode:'SFO',expectedRevision:0});assert.equal(stale.status,409);
    const restarted=createSimulatorServer({dataFile,clock:false});assert.deepEqual(restarted.getState(),s);
    const module=await fetch(base+'/imports.js');assert.equal(module.status,200);assert.match(await module.text(),/setupImports/);
    const sample=await fetch(base+'/api/examples/flights.csv');assert.equal(await sample.text(),flights);
    const reset=await post('/api/control',{type:'reset'});assert.equal(reset.body.data.flightImports.length,0);assert.equal(reset.body.data.airportCatalog.length,20);assert.equal(reset.body.pilots.length,6);
  }finally{await new Promise(r=>instance.server.close(r));fs.rmSync(dir,{recursive:true,force:true});}
});
