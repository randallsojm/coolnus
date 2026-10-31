import { clone, timestamp, record, assertState, initialState } from './simulator.mjs';
import { check, parseAirportTable, previewFlights } from './data.mjs';

export function migrateState(input){
  const s=clone(input);s.schemaVersion='1.1';
  s.data??={airportCatalog:[],flightImports:[],disruptions:[],activeFlightId:null};
  s.airport.operationHistory??=[];
  for(const p of s.pilots)p.source??='synthetic';
  return s;
}
function revision(s,expected){check(Number.isInteger(expected)&&s.revision===expected,'Stale revision: refresh the state and preview before applying.');check(!s.running,'Pause the simulation before importing or injecting.');}
function idleSurfaces(s){check(!s.pilots.some(p=>['landing','taxi_in','taxi_out','holding_short','takeoff'].includes(p.phase)),'Wait for active surface movements to clear before changing the airport or clock.');check(!s.incidents.some(i=>i.status==='active'),'Resolve active synthetic incidents before changing airport or clock.');}
const free=g=>g.status==='open'&&!g.occupiedBy&&!g.backgroundOccupied;
export function applyAirport(state,{text,airportCode,capacityScope='airport',expectedRevision,sourceName='pasted airport table'}){
  revision(state,expectedRevision);idleSurfaces(state);
  check(['airport','per_runway'].includes(capacityScope),'Choose an explicit capacity scope.');
  const parsed=text?parseAirportTable(text,capacityScope):{delimiter:'Persisted catalog',airports:(state.data?.airportCatalog||[]).map(a=>({...clone(a),capacityScope}))};
  const config=parsed.airports.find(a=>a.iataCode===airportCode);
  check(config,'Selected airport is not present in the imported table.');
  check(!(state.data?.flightImports.length&&state.airport.id!==config.iataCode),'Reset explicitly before changing airports after flight injection.');
  const s=migrateState(state);
  const parked=s.pilots.filter(p=>['at_gate','ready'].includes(p.phase));
  check(parked.length+config.spare_gates_at_any_time<=config.gates,'Airport has too few gates for parked aircraft plus the supplied spare-gate pool.');
  const r=(id,name)=>({id,name,status:'open',occupiedBy:null,reason:null,synthetic:true});
  s.airport={id:config.iataCode,name:config.name,configuration:config,configurationImportedAt:new Date().toISOString(),configurationSource:sourceName,
    runways:Array.from({length:config.active_runways},(_,i)=>({...r(`RWY-${i+1}`,`Runway ${i+1}`),availableAfter:s.elapsedSeconds})),
    taxiways:[r('TWY-A','Alpha'),r('TWY-B','Bravo')],
    gates:Array.from({length:config.gates},(_,i)=>({...r(`G${i+1}`,`Gate ${i+1}`),taxiwayId:i<Math.ceil(config.gates/2)?'TWY-A':'TWY-B',backgroundOccupied:false})),
    operationHistory:s.airport.id===config.iataCode?(s.airport.operationHistory||[]):[],
    assumptions:['Runway names and geometry are synthetic; two abstract taxiway routes are retained.','Unmodelled gate occupancy is synthetic: the supplied spare-gate pool is initially free in addition to explicitly parked aircraft.',config.capacityScopeSource]};
  const claimed=new Set();
  parked.forEach(p=>{
    const retained=s.airport.gates.find(g=>g.id===p.gateId&&!claimed.has(g.id));
    const gate=retained||s.airport.gates.find(g=>!claimed.has(g.id));claimed.add(gate.id);gate.occupiedBy=p.aircraftId;p.gateId=gate.id;
    if(p.source!=='historical_import'){
      p.onBlockAt??=state.startedAt;
      p.remainingSeconds=Math.max(0,Math.ceil((Date.parse(p.onBlockAt)+config.min_turnaround_min*60000-Date.parse(timestamp(s)))/1000));
      if(p.remainingSeconds>0)p.phase='at_gate';
      p.turnaroundBasis='Synthetic on-block timestamp; configured minimum turnaround applies.';
    }
  });
  let spares=config.spare_gates_at_any_time;
  for(const gate of s.airport.gates.filter(g=>!g.occupiedBy)){if(spares>0)spares--;else gate.backgroundOccupied=true;}
  for(const p of s.pilots){p.runwayId=null;p.taxiwayId=null;delete p.plannedRunwayId;if(!['at_gate','ready'].includes(p.phase))p.gateId=null;}
  s.data.airportCatalog=parsed.airports;
  s.data.airportImport={sourceName,format:parsed.delimiter,importedAt:new Date().toISOString(),selectedAirport:config.iataCode};
  s.assumptions=[...new Set([...s.assumptions,'Background flights are synthetic traffic mapped to the selected airport; not historical flights from the imported dataset.'])];
  record(s,'import',`Loaded ${config.iataCode}: ${config.active_runways} runways, ${config.gates} gates, ${config.spare_gates_at_any_time} spare gates.`,config.iataCode);
  s.revision++;assertState(s);return s;
}
export function injectFlight(state,{text,rowIndex,expectedRevision,rebaseClock=false,sourceName='pasted flight table'}){
  revision(state,expectedRevision);
  check(Number.isInteger(rowIndex)&&rowIndex>=0,'Select exactly one flight row.');
  check(typeof rebaseClock==='boolean','rebaseClock must be a boolean.');
  const preview=previewFlights(text,state.data?.airportCatalog||[]),selection=preview.rows[rowIndex];
  check(selection,'Selected row does not exist.');check(!selection.errors.length,selection.errors.join(' '));
  const flight=selection.normalized;
  check(flight.replaySupported,flight.replayIssues.join(' '));
  check(state.airport.configuration&&state.airport.id===flight.origin,`Load the matching origin airport ${flight.origin} before injecting. No airport substitution is allowed.`);
  check(!state.data?.flightImports.some(f=>f.id===flight.id),'This flight occurrence has already been injected.');
  if(rebaseClock){idleSurfaces(state);check(!state.data?.flightImports.length,'Clock alignment is only allowed for the first imported flight; reset explicitly to start another date.');}
  const s=migrateState(state);
  if(rebaseClock){
    const difference=Date.parse(flight.times.scheduledDeparture.utc)-Date.parse(timestamp(s));
    s.startedAt=new Date(Date.parse(s.startedAt)+difference).toISOString();
    for(const p of s.pilots)if(p.source==='synthetic')for(const key of ['expectedDeparture','expectedArrival','onBlockAt'])if(p[key])p[key]=new Date(Date.parse(p[key])+difference).toISOString();
    record(s,'clock',`Clock aligned to ${flight.callsign} scheduled origin departure; synthetic background plans shifted by ${difference/1000}s.`);
  }
  const pilotId=`P-${flight.id}`,aircraftId=`AC-${flight.id}`,gate=s.airport.gates.find(free);
  const pilot={id:pilotId,name:`${flight.callsign} pilot (placeholder)`,aircraftId,callsign:flight.callsign,source:'historical_import',flightId:flight.id,
    phase:gate?'at_gate':'awaiting_stand',gateId:gate?.id||null,runwayId:null,taxiwayId:null,
    fuelRemainingKg:3800,reserveFuelKg:900,fuelAlerted:false,maintenanceHold:false,remainingSeconds:0,holdUntil:0,
    expectedDeparture:flight.revisedPlan.originOffBlockUtc,expectedArrival:null,
    scheduledOffBlockAt:flight.times.scheduledDeparture.utc,earliestOffBlockAt:flight.revisedPlan.originOffBlockUtc,
    simulatedActual:{offBlockAt:null,wheelsOffAt:null,wheelsOnAt:null,onBlockAt:null},
    turnaroundBasis:'Preceding on-block time is missing; aircraft positioning/readiness is an explicit simulation assumption. No new minimum-turnaround timer is invented.',
    assumptions:['Remaining fuel 3800 kg and reserve 900 kg are existing synthetic simulator defaults, not source-row measurements.','Origin-airport departure replay only; downstream arrival is a plan/reference, not an already-completed event.','Prior turnaround is not evaluable from this row. Aircraft is assumed ready apart from the imported departure delay and stand availability.']};
  if(gate)gate.occupiedBy=aircraftId;
  s.pilots.push(pilot);
  const provenance={sourceName,sourceRowNumber:selection.sourceRowNumber,format:preview.delimiter,importedAt:new Date().toISOString(),injectedAt:timestamp(s),revision:s.revision+1};
  s.data.flightImports.push({id:flight.id,pilotId,rawRow:selection.rawRow,normalized:flight,provenance});
  s.data.disruptions.push({id:`DELAY-${flight.id}`,flightId:flight.id,pilotId,kind:'historical_departure_delay',
    causeLabels:[...(flight.values.WEATHER_DELAY>15?['weather']:[]),...(flight.values.LATE_AIRCRAFT_DELAY>20?['late_aircraft']:[])],
    delayComponents:flight.delayComponents,appliedDelayMinutes:flight.values.DEPARTURE_DELAY,unappliedDelayMinutes:0,
    applicationStatus:'applied_once',beforeOffBlockUtc:flight.times.scheduledDeparture.utc,afterOffBlockUtc:flight.revisedPlan.originOffBlockUtc,
    assumptions:pilot.assumptions,provenance});
  s.data.activeFlightId=flight.id;
  record(s,'import',`${flight.callsign} ${flight.origin} → ${flight.destination}: departure delay ${flight.values.DEPARTURE_DELAY} min applied once; no physical incident inferred.`,pilotId);
  s.revision++;assertState(s);return s;
}
export function resetPreservingCatalog(state){const s=initialState();s.revision=state.revision+1;s.data.airportCatalog=clone(state.data?.airportCatalog||[]);return s;}
