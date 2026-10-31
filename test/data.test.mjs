import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { parseDelimited,parseHhmm,parseAirportTable,normalizeFlight,previewFlights,localToUtc,FLIGHT_COLUMNS } from '../src/data.mjs';
import { applyAirport,injectFlight,migrateState } from '../src/scenario.mjs';
import { initialState,advance,command,observation,assertState } from '../src/simulator.mjs';

const airportText=fs.readFileSync(new URL('../examples/airports.csv',import.meta.url),'utf8');
const flightText=fs.readFileSync(new URL('../examples/flights.csv',import.meta.url),'utf8');
const tsv=fs.readFileSync(new URL('../examples/flight-AA5.tsv',import.meta.url),'utf8');
const catalog=parseAirportTable(airportText).airports;
const rows=parseDelimited(flightText,FLIGHT_COLUMNS).rows;
const csvRow=raw=>FLIGHT_COLUMNS.join(',')+'\n'+FLIGHT_COLUMNS.map(k=>'"'+String(raw[k]??'').replaceAll('"','""')+'"').join(',');
function configured(code='DFW',scope='airport') { const s=initialState();return applyAirport(s,{text:airportText,airportCode:code,expectedRevision:s.revision,capacityScope:scope}); }
function inject(s,raw=rows[0],extra={}) { return injectFlight(s,{text:csvRow(raw),rowIndex:0,expectedRevision:s.revision,rebaseClock:true,...extra}); }
const rawFlight=n=>rows.find(r=>r.FLIGHT_NUMBER===String(n));

test('RFC-style CSV parser retains quoted commas, escaped quotes, newlines, empties and CRLF',()=>{
  const p=parseDelimited('\uFEFFA,B,C\r\n"a,b","quote ""text""",\r\n"two\nlines",x,y\r\n');
  assert.equal(p.delimiter,'CSV');assert.deepEqual(p.rows[0],{A:'a,b',B:'quote "text"',C:''});assert.equal(p.rows[1].A,'two\nlines');
  assert.throws(()=>parseDelimited('A,B\n"broken,x'),/Unclosed/);assert.throws(()=>parseDelimited('A,B\nx,y,z'),/expected 2 fields/);
  assert.throws(()=>parseDelimited('A,A\nx,y'),/Duplicate/);
});
test('TSV parser preserves leading zeros and source strings',()=>{
  const p=parseDelimited('A\tB\r\n0003\t"a\tb"\r\n');assert.equal(p.delimiter,'TSV');assert.equal(p.rows[0].A,'0003');assert.equal(p.rows[0].B,'a\tb');
  const preview=previewFlights(tsv,catalog);assert.equal(preview.rowCount,1);assert.equal(preview.rows[0].normalized.callsign,'AA5');
});
test('all supplied flight examples parse independently, including both eligibility fixtures',()=>{
  const p=previewFlights(flightText,catalog);assert.equal(p.rowCount,19);assert.ok(p.rows.every(r=>r.errors.length===0));
  assert.ok(p.rows.find(r=>r.normalized.callsign==='AA5').normalized.eligible);
  assert.ok(p.rows.find(r=>r.normalized.callsign==='AA151').normalized.eligible);
});
test('eligibility is strict OR; missing values remain null, never coerced to zero',()=>{
  let f=normalizeFlight({...rows[0],WEATHER_DELAY:'15',LATE_AIRCRAFT_DELAY:'20'},catalog);assert.equal(f.eligible,false);
  f=normalizeFlight({...rows[0],WEATHER_DELAY:'16',LATE_AIRCRAFT_DELAY:''},catalog);assert.equal(f.eligible,true);assert.equal(f.values.LATE_AIRCRAFT_DELAY,null);
  f=normalizeFlight({...rows[0],WEATHER_DELAY:'',LATE_AIRCRAFT_DELAY:'21',DEPARTURE_DELAY:''},catalog);assert.equal(f.eligible,true);assert.equal(f.values.DEPARTURE_DELAY,null);assert.equal(f.replaySupported,false);
});
test('signed delay values are valid source data; durations and counts cannot be negative',()=>{
  const f=normalizeFlight({...rows[0],DEPARTURE_DELAY:'-5',ARRIVAL_DELAY:'-10'},catalog);assert.equal(f.values.DEPARTURE_DELAY,-5);assert.equal(f.replaySupported,false);
  assert.throws(()=>normalizeFlight({...rows[0],TAXI_IN:'-1'},catalog),/TAXI_IN/);
  assert.throws(()=>normalizeFlight({...rows[0],WEATHER_DELAY:'NaN'},catalog),/numeric/);
  assert.throws(()=>normalizeFlight({...rows[0],YEAR:'2015',MONTH:'2',DAY:'30'},catalog),/calendar date/);
});
test('HHMM parsing validates boundaries and explicitly handles 2400',()=>{
  for(const [raw,time] of [['1305','13:05'],['648','06:48'],['24','00:24'],['3','00:03'],['0003','00:03'],['0','00:00']])assert.equal(parseHhmm(raw).localTime,time);
  assert.equal(parseHhmm('2400').dayOffset,1);assert.equal(parseHhmm('2400').localTime,'00:00');assert.equal(parseHhmm(''),null);
  for(const raw of ['2360','2401','2500','13:05','13.05'])assert.throws(()=>parseHhmm(raw),/Invalid/);
});
test('origin and destination local times resolve with timezone-aware dates',()=>{
  const f=normalizeFlight(rows[0],catalog);assert.equal(f.times.scheduledDeparture.utc,'2015-01-01T19:05:00.000Z');
  assert.equal(f.times.actualDeparture.utc,'2015-01-01T23:54:00.000Z');assert.equal(f.times.actualArrival.utc,'2015-01-02T08:40:00.000Z');
  assert.equal(f.times.actualArrival.localDate,'2015-01-01');assert.equal(f.times.actualArrival.timezone,'Pacific/Honolulu');
});
test('midnight rollover works for next-day arrivals, wheels-off, and negative early departure',()=>{
  let f=normalizeFlight(rawFlight(79),catalog);assert.equal(f.times.actualArrival.localTime,'00:03');assert.equal(f.times.actualArrival.localDate,'2015-01-02');
  f=normalizeFlight(rawFlight(81),catalog);assert.equal(f.times.scheduledArrival.localDate,'2015-01-02');assert.equal(f.times.wheelsOn.localDate,'2015-01-02');
  f=normalizeFlight(rawFlight(102),catalog);assert.equal(f.times.wheelsOff.localTime,'00:13');assert.equal(f.times.wheelsOff.localDate,'2015-01-02');
  f=normalizeFlight({...rows[0],SCHEDULED_DEPARTURE:'0000',DEPARTURE_TIME:'2355',DEPARTURE_DELAY:'-5'},catalog);assert.equal(f.times.actualDeparture.localDate,'2014-12-31');
});
test('unknown timezone and absent date anchors retain clock values without guessing UTC',()=>{
  let f=normalizeFlight({...rows[0],ORIGIN_AIRPORT:'ZZZ'},catalog);assert.equal(f.times.scheduledDeparture.utc,null);assert.equal(f.times.scheduledDeparture.localTime,'13:05');assert.equal(f.replaySupported,false);
  f=normalizeFlight({...rows[0],DESTINATION_AIRPORT:'ZZZ'},catalog);assert.equal(f.times.actualArrival.utc,null);assert.equal(f.times.actualArrival.localTime,'22:40');assert.ok(f.replaySupported);
  f=normalizeFlight({...rows[0],SCHEDULED_TIME:'',ELAPSED_TIME:'',ARRIVAL_DELAY:''},catalog);assert.equal(f.times.scheduledArrival.utc,null);assert.equal(f.times.actualArrival.utc,null);
});
test('DST ambiguity and gaps are unresolved; explicit timezone metadata is validated',()=>{
  assert.equal(localToUtc('2015-11-01',parseHhmm('130'),'America/New_York').status,'unresolved');
  assert.equal(localToUtc('2015-03-08',parseHhmm('230'),'America/New_York').status,'unresolved');
  const custom=airportText+'ZZZ,Custom,City,XX,0,Test,1,4,10,5,1\r\n';
  assert.equal(parseAirportTable(custom).airports.at(-1).timezone,null);
  const withZone=airportText.trimEnd().split(/\r?\n/).map((l,i)=>l+(i===0?',TIMEZONE':',America/Chicago')).join('\n');
  assert.equal(parseAirportTable(withZone).airports[0].timezoneSource,'TIMEZONE input column');
  assert.throws(()=>parseAirportTable(withZone.replaceAll('America/Chicago','Invalid/Zone')),/IANA/);
});
test('airport counts and capacity scope come from data, not dataset summary count',()=>{
  for(const [code,count,gates,min] of [['DFW',4,150,45],['SFO',3,70,35]]){
    const s=configured(code);assert.equal(s.airport.runways.length,count);assert.equal(s.airport.gates.length,gates);
    assert.equal(s.airport.gates.filter(g=>!g.occupiedBy&&!g.backgroundOccupied).length,3);
    assert.equal(s.pilots.length,6);assert.equal(s.airport.configuration.min_turnaround_min,min);assert.equal(s.airport.configuration.capacityScope,'airport');assertState(s);
  }
  assert.equal(configured('DFW','per_runway').airport.configuration.capacityScope,'per_runway');
  assert.throws(()=>applyAirport(initialState(),{text:airportText,airportCode:'HNL',expectedRevision:0}),/not present/);
});
test('invalid airport table and insufficient resource inventories do not change state',()=>{
  const s=initialState(),before=structuredClone(s);
  assert.throws(()=>applyAirport(s,{text:airportText.replace('4,150,90,45,3','4,2,90,45,3'),airportCode:'DFW',expectedRevision:0}),/Spare gates/);
  assert.deepEqual(s,before);
});
test('one selected row is injected atomically, preserves background and applies total delay once',()=>{
  const s=configured(),original=structuredClone(s),next=inject(s);
  assert.deepEqual(s,original);assert.equal(next.pilots.length,7);assert.deepEqual(next.pilots.slice(0,6).map(p=>p.id),s.pilots.map(p=>p.id));
  const f=next.data.flightImports[0],d=next.data.disruptions[0],p=next.pilots.at(-1);
  assert.equal(d.appliedDelayMinutes,289);assert.equal(d.delayComponents.lateAircraft,92);assert.equal(d.delayComponents.airline,197);
  assert.equal(Date.parse(p.expectedDeparture)-Date.parse(p.scheduledOffBlockAt),289*60000);
  assert.equal(next.startedAt,f.normalized.times.scheduledDeparture.utc);assert.equal(next.airport.gates.filter(g=>!g.occupiedBy&&!g.backgroundOccupied).length,2);
  assert.equal(next.tasks.length,0);assert.equal(next.incidents.length,0);assert.ok(next.airport.runways.every(r=>r.status==='open'));
  assert.equal(p.simulatedActual.offBlockAt,null);assert.equal(p.expectedArrival,null);assert.equal(p.remainingSeconds,0);
  assert.equal(f.rawRow.CANCELLATION_REASON,'');assert.equal(f.provenance.revision,next.revision);
});
test('multi-row files require an explicit single index and inject only that occurrence',()=>{
  const s=configured('DEN');assert.throws(()=>injectFlight(s,{text:flightText,expectedRevision:s.revision}),/exactly one/);
  assert.throws(()=>injectFlight(s,{text:flightText,rowIndex:[0,1],expectedRevision:s.revision}),/exactly one/);
  const index=rows.findIndex(r=>r.FLIGHT_NUMBER==='151');const n=injectFlight(s,{text:flightText,rowIndex:index,expectedRevision:s.revision,rebaseClock:true});
  assert.equal(n.data.flightImports.length,1);assert.equal(n.data.flightImports[0].normalized.callsign,'AA151');assert.equal(n.data.disruptions[0].appliedDelayMinutes,33);
});
test('duplicate, invalid, cancelled, diverted and mismatched-airport injections leave state unchanged',()=>{
  let s=inject(configured());const before=structuredClone(s);
  assert.throws(()=>inject(s,rows[0],{rebaseClock:false}),/already been injected/);assert.deepEqual(s,before);
  for(const changes of [{CANCELLED:'1'},{DIVERTED:'1'},{DEPARTURE_DELAY:''},{SCHEDULED_DEPARTURE:'9999'},{WEATHER_DELAY:'15',LATE_AIRCRAFT_DELAY:'20'}]){
    const base=configured(),copy=structuredClone(base);assert.throws(()=>inject(base,{...rows[0],...changes}));assert.deepEqual(base,copy);
  }
  assert.throws(()=>inject(configured('SFO')),/matching origin airport DFW/);
});
test('pausing, state revision and explicit first-flight clock alignment are enforced',()=>{
  let s=configured();assert.throws(()=>injectFlight(s,{text:csvRow(rows[0]),rowIndex:0,expectedRevision:0}),/Stale/);
  const running=structuredClone(s);running.running=true;assert.throws(()=>inject(running),/Pause/);
  const unchangedClock=inject(s,rows[0],{rebaseClock:false});assert.equal(unchangedClock.startedAt,s.startedAt);
  s=inject(s);assert.throws(()=>inject(s,rawFlight(51)),/only allowed for the first/);
  const next=inject(s,rawFlight(51),{rebaseClock:false});assert.equal(next.data.flightImports.length,2);assert.equal(next.pilots.length,8);
});
test('no spare gates leaves the imported aircraft waiting without corrupting occupancy',()=>{
  let s=configured();for(const g of s.airport.gates)if(!g.occupiedBy)g.backgroundOccupied=true;
  s=inject(s);assert.equal(s.pilots.at(-1).phase,'awaiting_stand');assert.equal(s.pilots.at(-1).gateId,null);assertState(advance(s,30));
});
test('configured minimum turnaround uses on-block time for known synthetic aircraft only',()=>{
  let s=configured();assert.equal(s.pilots[0].remainingSeconds,45*60);s=advance(s,100);assert.equal(s.pilots[0].phase,'at_gate');
  s=inject(configured());assert.equal(s.pilots.at(-1).remainingSeconds,0);assert.match(s.pilots.at(-1).turnaroundBasis,/missing/);
});
test('airport-wide hourly capacity is enforced across all runways',()=>{
  let s=configured();s.airport.configuration.max_runway_ops_per_hour=1;
  s=advance(s,300);assert.equal(s.airport.operationHistory.length,1);assert.ok(s.pilots.some(p=>p.phase==='holding'));
});
test('replay cannot depart before the once-shifted off-block time; reference actuals stay separate',()=>{
  let s=inject(configured());for(let i=0;i<4;i++)s=advance(s,3600);s=advance(s,2880);
  assert.equal(s.elapsedSeconds,288*60);assert.equal(s.pilots.at(-1).phase,'ready');assert.equal(s.pilots.at(-1).simulatedActual.offBlockAt,null);
  s=advance(s,60);assert.equal(s.pilots.at(-1).simulatedActual.offBlockAt,'2015-01-01T23:54:00.000Z');
  s=advance(s,180);assert.equal(s.pilots.at(-1).phase,'departed');assert.ok(s.pilots.at(-1).simulatedActual.wheelsOffAt);
  assert.equal(s.data.flightImports[0].normalized.historicalActual.departure.utc,'2015-01-01T23:54:00.000Z');
});
test('new observations expose import provenance with per-person and per-flight scoping',()=>{
  const s=inject(configured()),p=s.pilots.at(-1),atc=observation(s,'atc','ATC-1'),pilot=observation(s,'pilot',p.id),other=observation(s,'pilot','P-1'),ground=observation(s,'ground','ME-2');
  assert.equal(atc.importedScenarios.length,1);assert.equal(atc.airport.gates.length,150);assert.equal(atc.agentsImplemented,false);
  assert.equal(pilot.importedScenario.normalized.callsign,'AA5');assert.equal(other.importedScenario,null);assert.equal(pilot.pilots,undefined);
  assert.equal(ground.crew.id,'ME-2');assert.equal(ground.delayDisruptions.length,1);assert.equal(ground.pilots,undefined);
  assert.equal(ground.airportConfiguration.min_turnaround_min,45);
});
test('old saved state migrates without clearing traffic or personnel',()=>{
  const old=initialState();old.schemaVersion='1.0';delete old.data;
  const s=migrateState(old);assert.equal(s.schemaVersion,'1.1');assert.equal(s.pilots.length,6);assert.equal(s.ground.crews.flat().length,6);assertState(s);
});
