import { gateStatus } from './gate-status.mjs';
export const CREW_TYPES = ['emergency_response', 'mechanic', 'inspector'];
export const clone = value => structuredClone(value);
const START = '2026-10-31T04:00:00.000Z';
const live = s => s.pilots.filter(p => !['departed', 'diverted'].includes(p.phase));
export const allCrews = s => s.ground.crews.flat();
export const timestamp = s => new Date(Date.parse(s.startedAt) + s.elapsedSeconds * 1000).toISOString();
export function record(s, kind, message, entityId = null) {
  s.events.unshift({ id: s.nextEventId++, at: timestamp(s), kind, message, entityId });
  s.events = s.events.slice(0, 200);
}
export function initialState() {
  const s = {
    schemaVersion: '1.1', revision: 0, startedAt: START, elapsedSeconds: 0,
    running: false, speed: 5, autoDispatch: true, nextEventId: 1, nextIncidentId: 1,
    units: { fuel: 'kg', durations: 'seconds', times: 'ISO-8601 UTC' },
    assumptions: ['Synthetic training airport; not operational aviation software.', 'Runway use: 60s arrival, 45s departure, 30s minimum reuse gap.', 'One aircraft reserves a taxiway for its complete taxi movement.', 'Fuel burn: airborne 0.7 kg/s, taxi/active runway 0.15 kg/s; reserve 900 kg.', 'Emergency, repair and inspection durations are scenario assumptions.'],
    airport: {
      id: 'DEMO', name: 'CoolNUS International',
      runways: [resource('RWY-1', '09L / 27R'), resource('RWY-2', '09R / 27L')].map(r => ({ ...r, availableAfter: 0 })),
      taxiways: [resource('TWY-A', 'Alpha'), resource('TWY-B', 'Bravo')],
      gates: Array.from({ length: 6 }, (_, i) => ({ ...resource(`G${i + 1}`, `Gate ${i + 1}`), taxiwayId: i < 3 ? 'TWY-A' : 'TWY-B' }))
    },
    data: { airportCatalog: [], flightImports: [], disruptions: [], activeFlightId: null },
    atc: { id: 'ATC-1', name: 'Airport-wide traffic coordinator' },
    ground: {
      crewTypeOrder: CREW_TYPES,
      // Each index is a separate list of personnel, not a single representative.
      crews: CREW_TYPES.map((type, index) => Array.from({ length: 2 }, (_, i) => ({
        id: `${['ER', 'ME', 'IN'][index]}-${i + 1}`, name: `${['Response', 'Mechanic', 'Inspector'][index]} ${i + 1}`,
        type, status: 'available', location: 'Operations base', currentTaskId: null,
        completedTasks: 0
      })))
    },
    pilots: Array.from({ length: 6 }, (_, i) => ({
      id: `P-${i + 1}`, name: ['Aisha Tan', 'Daniel Lim', 'Priya Nair', 'Noah Chen', 'Sara Wong', 'Ethan Lee'][i],
      aircraftId: `AC-${i + 1}`, callsign: ['SG218', 'AX604', 'SQ382', 'TR119', 'JL708', 'QF036'][i],
      source: 'synthetic', onBlockAt: i < 2 ? START : null,
      phase: i < 2 ? 'at_gate' : 'approach', fuelRemainingKg: i < 2 ? 3800 : 2600 + i * 120,
      reserveFuelKg: 900, expectedArrival: new Date(Date.parse(START) + (i < 2 ? 0 : (i - 1) * 80000)).toISOString(),
      expectedDeparture: new Date(Date.parse(START) + (i < 2 ? 180 + i * 100 : 700 + i * 130) * 1000).toISOString(),
      gateId: i < 2 ? `G${i + 1}` : null, runwayId: null, taxiwayId: null,
      remainingSeconds: i < 2 ? 90 + i * 60 : 0, holdUntil: 0, maintenanceHold: false, fuelAlerted: false
    })), incidents: [], tasks: [], events: []
  };
  s.airport.gates[0].occupiedBy = 'AC-1'; s.airport.gates[1].occupiedBy = 'AC-2';
  record(s, 'system', 'Simulation ready. Six flights and six ground personnel are on station.');
  return s;
}
function resource(id, name) { return { id, name, status: 'open', occupiedBy: null, reason: null }; }
function findResource(s, id) { return [...s.airport.runways, ...s.airport.taxiways, ...s.airport.gates].find(r => r.id === id); }
function requireValue(condition, message) { if (!condition) throw new Error(message); }
function available(r) { return r.status === 'open' && !r.occupiedBy && !r.backgroundOccupied; }
function readyTask(s, t) { return t.status === 'queued' && t.dependsOn.every(id => s.tasks.find(x => x.id === id)?.status === 'completed'); }
function assignTask(s, crew, task) {
  requireValue(crew.status === 'available', 'Crew member is not available.');
  requireValue(crew.type === task.requiredType, 'This task requires a different crew type.');
  requireValue(readyTask(s, task), 'Task is unavailable or its prerequisites are incomplete.');
  task.status = 'active'; task.assigneeId = crew.id;
  crew.status = 'busy'; crew.currentTaskId = task.id; crew.location = task.location;
  record(s, 'ground', `${crew.id} assigned: ${task.title}.`, crew.id);
}
function dispatch(s) {
  for (const task of s.tasks) if (readyTask(s, task)) {
    const crew = allCrews(s).find(c => c.type === task.requiredType && c.status === 'available');
    if (crew) assignTask(s, crew, task);
  }
}
function completeTasks(s) {
  for (const task of s.tasks.filter(t => t.status === 'active')) {
    task.remainingSeconds = Math.max(0, task.remainingSeconds - 1);
    if (task.remainingSeconds) continue;
    task.status = 'completed'; task.completedAt = timestamp(s);
    const crew = allCrews(s).find(c => c.id === task.assigneeId);
    crew.status = 'available'; crew.currentTaskId = null; crew.completedTasks++;
    record(s, 'ground', `${crew.id} completed: ${task.title}.`, crew.id);
  }
  for (const incident of s.incidents.filter(i => i.status === 'active')) {
    if (!incident.taskIds.every(id => s.tasks.find(t => t.id === id)?.status === 'completed')) continue;
    incident.status = 'resolved'; incident.resolvedAt = timestamp(s);
    const target = findResource(s, incident.targetId);
    if (target && !s.incidents.some(i => i.status === 'active' && i.targetId === target.id)) {
      target.status = 'open'; target.reason = null;
    }
    const pilot = s.pilots.find(p => p.id === incident.targetId);
    if (pilot && !s.incidents.some(i => i.status === 'active' && i.targetId === pilot.id)) pilot.maintenanceHold = false;
    record(s, 'resolved', `${incident.title} resolved; ${incident.targetId} released.`, incident.targetId);
  }
}
function releaseRunway(s, p) {
  const runway = findResource(s, p.runwayId);
  runway.occupiedBy = null; runway.availableAfter = s.elapsedSeconds + (p.negotiated ? 0 : 30);
}
export function weatherHoldActive(s) {
  const now = Date.parse(timestamp(s));
  return (s.airport.weatherHolds || []).some(h => now >= Date.parse(h.start) && now < Date.parse(h.end));
}
function chooseRunway(s, p) {
  return s.airport.runways.find(r => available(r) && r.availableAfter <= s.elapsedSeconds && (!p.plannedRunwayId || r.id === p.plannedRunwayId) && capacityAvailable(s,r));
}
function capacityAvailable(s, runway) {
  const config=s.airport.configuration;if(!config)return true;
  const recent=(s.airport.operationHistory||[]).filter(o=>o.atSeconds>s.elapsedSeconds-3600 && (config.capacityScope==='airport'||o.runwayId===runway.id));
  return recent.length<config.max_runway_ops_per_hour;
}
function logOperation(s,p,kind) {
  s.airport.operationHistory??=[];
  s.airport.operationHistory=s.airport.operationHistory.filter(o=>o.atSeconds>s.elapsedSeconds-3600);
  s.airport.operationHistory.push({atSeconds:s.elapsedSeconds,runwayId:p.runwayId,aircraftId:p.aircraftId,kind});
}
function burnFuel(s, p) {
  if (['departed', 'diverted', 'at_gate', 'ready', 'awaiting_stand'].includes(p.phase)) return;
  if (p.airborneFrom && Date.parse(timestamp(s)) < Date.parse(p.airborneFrom)) return; // still on the ground at origin
  // Negotiated aircraft burn their catalogue rates (cruise on approach, holding rate while holding).
  const rate = p.phase === 'approach' ? (p.cruiseBurnKgPerSec ?? 0.7) : p.phase === 'holding' ? (p.holdBurnKgPerSec ?? 0.7) : 0.15;
  p.fuelRemainingKg = Math.max(0, p.fuelRemainingKg - rate);
  if (p.fuelRemainingKg <= p.reserveFuelKg && !p.fuelAlerted) {
    p.fuelAlerted = true;
    record(s, 'critical', `${p.callsign}: simulated reserve threshold reached.`, p.id);
  }
  if (p.fuelRemainingKg <= p.reserveFuelKg && ['approach', 'holding'].includes(p.phase)) {
    p.phase = 'diverted';
    record(s, 'critical', `${p.callsign} removed from arrival queue by reserve-fuel fallback (diversion abstracted).`, p.id);
  }
}
function tickPilot(s, p) {
  burnFuel(s, p);
  if (['departed', 'diverted'].includes(p.phase)) return;
  if (p.remainingSeconds > 0) p.remainingSeconds--;
  if (p.phase === 'awaiting_stand') {
    const gate=s.airport.gates.find(available);if(!gate)return;
    gate.occupiedBy=p.aircraftId;p.gateId=gate.id;p.phase='at_gate';
    record(s,'movement',`${p.callsign} assigned ${gate.id}; imported delay still governs departure.`,p.id);return;
  }
  if (p.phase === 'landing' && p.remainingSeconds === 0) {
    releaseRunway(s, p); p.phase = 'taxi_in'; p.remainingSeconds = p.taxiInSeconds ?? 90;
    record(s, 'movement', `${p.callsign} landed; taxiing to ${p.gateId}.`, p.id); return;
  }
  if (p.phase === 'taxi_in' && p.remainingSeconds === 0) {
    if (p.taxiwayId) findResource(s, p.taxiwayId).occupiedBy = null; p.phase = 'at_gate'; p.remainingSeconds = s.airport.configuration ? s.airport.configuration.min_turnaround_min * 60 : 180;
    p.onBlockAt=timestamp(s);if(p.simulatedActual)p.simulatedActual.onBlockAt=timestamp(s);
    record(s, 'movement', `${p.callsign} arrived at ${p.gateId}; turnaround started.`, p.id); return;
  }
  if (p.phase === 'at_gate' && p.remainingSeconds === 0 && !p.maintenanceHold) {
    p.phase = 'ready'; record(s, 'movement', `${p.callsign} ready for departure.`, p.id);
  }
  if (p.phase === 'ready' && !p.maintenanceHold && s.elapsedSeconds >= p.holdUntil && Date.parse(timestamp(s)) >= Math.max(Date.parse(p.expectedDeparture),Date.parse(p.earliestOffBlockAt||p.expectedDeparture))) {
    const gate = findResource(s, p.gateId), taxiway = findResource(s, gate.taxiwayId);
    if (gate.status !== 'open' || (!p.negotiated && !available(taxiway))) return;
    gate.occupiedBy = null; if (!p.negotiated) { taxiway.occupiedBy = p.aircraftId; p.taxiwayId = taxiway.id; }
    p.phase = 'taxi_out'; p.remainingSeconds = 90;
    if(p.simulatedActual)p.simulatedActual.offBlockAt=timestamp(s);
    record(s, 'movement', `${p.callsign} pushed back; outbound on ${taxiway.name}.`, p.id); return;
  }
  if (p.phase === 'taxi_out' && p.remainingSeconds === 0) p.phase = 'holding_short';
  if (p.phase === 'holding_short' && s.elapsedSeconds >= p.holdUntil && !p.maintenanceHold) {
    const runway = chooseRunway(s, p);
    if (!runway) return;
    runway.occupiedBy = p.aircraftId; if (p.taxiwayId) findResource(s, p.taxiwayId).occupiedBy = null;
    p.runwayId = runway.id; p.phase = 'takeoff'; p.remainingSeconds = 45;
    logOperation(s,p,'departure');
    record(s, 'movement', `${p.callsign} cleared for simulated departure on ${runway.name}.`, p.id); return;
  }
  if (p.phase === 'takeoff' && p.remainingSeconds === 0) {
    releaseRunway(s, p); p.phase = 'departed'; p.actualDeparture = timestamp(s);
    if(p.simulatedActual)p.simulatedActual.wheelsOffAt=timestamp(s);
    record(s, 'movement', `${p.callsign} departed.`, p.id); return;
  }
  if (['approach', 'holding'].includes(p.phase)) {
    const now = Date.parse(timestamp(s));
    if (p.divertAt && now >= Date.parse(p.divertAt)) {
      p.phase = 'diverted';
      record(s, 'critical', `${p.callsign} diverts${p.divertTo ? ' to ' + p.divertTo : ''} (${p.disrupted ? 'agreed plan' : 'spare fuel used up in the weather hold'}).`, p.id); return;
    }
    if (now < Date.parse(p.expectedArrival)) return;
    if (p.phase === 'approach') { p.phase = 'holding'; if (p.negotiated) record(s, 'plan', `${p.callsign} holding.`, p.id); }
    if (s.elapsedSeconds < p.holdUntil || p.maintenanceHold) return;
    if (weatherHoldActive(s)) return;                                         // no landings while weather is overhead
    if (p.assignedLandingAt && now < Date.parse(p.assignedLandingAt)) return; // agreed landing order
    const taxiOk = g => p.negotiated || available(findResource(s, g.taxiwayId));
    const gate = p.gateId ? s.airport.gates.find(g => g.id === p.gateId && available(g) && taxiOk(g))
      : s.airport.gates.find(g => available(g) && taxiOk(g));
    const runway = chooseRunway(s, p);
    if (!gate || !runway) return;
    // Reserve a complete inbound route and stand before beginning the movement.
    p.gateId = gate.id; p.taxiwayId = p.negotiated ? null : gate.taxiwayId; p.runwayId = runway.id;
    gate.occupiedBy = p.aircraftId; if (p.taxiwayId) findResource(s, p.taxiwayId).occupiedBy = p.aircraftId; runway.occupiedBy = p.aircraftId;
    if (p.negotiated) p.actualLandingAt = timestamp(s);
    p.phase = 'landing'; p.remainingSeconds = 60;
    logOperation(s,p,'arrival');
    record(s, 'movement', `${p.callsign} cleared to land on ${runway.name}${p.taxiwayId ? ' → ' + p.taxiwayId : ''} → ${gate.id}.`, p.id);
  }
}
export function advance(state, seconds = 1) {
  requireValue(Number.isInteger(seconds) && seconds > 0 && seconds <= 3600, 'Advance must be 1–3600 whole seconds.');
  const s = clone(state);
  for (let i = 0; i < seconds; i++) {
    s.elapsedSeconds++;
    completeTasks(s);
    if (s.autoDispatch) dispatch(s);
    // Stable, fuel-priority order for arrivals; no random changes between runs.
    const ordered = [...s.pilots].sort((a, b) => {
      const aAir = ['approach', 'holding'].includes(a.phase), bAir = ['approach', 'holding'].includes(b.phase);
      if (!(aAir && bAir)) return Number(aAir) - Number(bAir);
      const ta = a.assignedLandingAt ? Date.parse(a.assignedLandingAt) : null, tb = b.assignedLandingAt ? Date.parse(b.assignedLandingAt) : null;
      if (ta !== null && tb !== null && ta !== tb) return ta - tb;   // the negotiated landing order
      if ((ta === null) !== (tb === null)) return ta === null ? 1 : -1;
      return a.fuelRemainingKg - b.fuelRemainingKg || a.id.localeCompare(b.id);
    });
    ordered.forEach(p => tickPilot(s, p));
  }
  s.revision++;
  assertState(s);
  return s;
}
const INCIDENTS = {
  bird_strike: { title: 'Bird strike / runway inspection', types: ['inspector'], durations: [180] },
  disabled_aircraft: { title: 'Disabled aircraft', types: ['emergency_response', 'mechanic', 'inspector'], durations: [90, 180, 120] },
  surface_debris: { title: 'Surface debris inspection and clearance', types: ['inspector'], durations: [120] },
  aircraft_fault: { title: 'Aircraft maintenance fault', types: ['mechanic'], durations: [240] }
};
export function command(state, action, actor = { role: 'operator', id: 'OPERATOR' }, expectedRevision) {
  requireValue(action && typeof action.type === 'string', 'A command type is required.');
  if (expectedRevision !== undefined) requireValue(state.revision === expectedRevision, 'Stale revision: fetch a fresh observation before acting.');
  requireValue(actor && ['operator', 'atc', 'pilot', 'ground'].includes(actor.role), 'Unknown actor role.');
  if (actor.role === 'atc') requireValue(actor.id === state.atc.id, 'Unknown ATC ID.');
  if (actor.role === 'pilot') requireValue(state.pilots.some(p => p.id === actor.id), 'Unknown pilot ID.');
  if (actor.role === 'ground') requireValue(allCrews(state).some(c => c.id === actor.id), 'Unknown crew ID.');
  const permissions = { operator: ['resource_status', 'incident', 'hold', 'set_plan', 'assign_task', 'crew_availability', 'auto_dispatch'], atc: ['resource_status', 'set_plan'], pilot: ['hold'], ground: ['assign_task', 'crew_availability'] };
  requireValue(permissions[actor.role].includes(action.type), 'This role is not allowed to issue that command.');
  const s = clone(state);
  switch (action.type) {
    case 'resource_status': {
      const r = findResource(s, action.targetId);
      requireValue(r, 'Unknown resource.'); requireValue(['open', 'closed'].includes(action.status), 'Status must be open or closed.');
      requireValue(!r.occupiedBy && !r.backgroundOccupied, 'Resource is occupied or reserved; wait until the movement clears.');
      requireValue(!s.incidents.some(i => i.status === 'active' && i.targetId === r.id), 'Active incident: crew tasks must be completed before resource release.');
      r.status = action.status; r.reason = action.status === 'closed' ? 'Operator restriction' : null;
      record(s, 'resource', `${r.name} ${r.status}.`, r.id); break;
    }
    case 'incident': {
      const spec = INCIDENTS[action.kind]; requireValue(spec, 'Unknown incident kind.');
      const pilot = s.pilots.find(p => p.id === action.targetId), target = findResource(s, action.targetId);
      if (action.kind === 'aircraft_fault') {
        requireValue(pilot && ['at_gate', 'ready'].includes(pilot.phase), 'Aircraft fault injection requires a pilot whose aircraft is at a stand.');
        requireValue(!pilot.maintenanceHold, 'Aircraft already has a maintenance hold.'); pilot.maintenanceHold = true;
      } else {
        requireValue(target && !target.occupiedBy && !target.backgroundOccupied && target.status === 'open', 'Incident requires a free, open resource.');
        if (['bird_strike', 'disabled_aircraft'].includes(action.kind)) requireValue(s.airport.runways.includes(target), 'This scenario requires a runway.');
        if (action.kind === 'surface_debris') requireValue(s.airport.taxiways.includes(target) || s.airport.runways.includes(target), 'Debris scenario requires a runway or taxiway.');
        target.status = 'closed'; target.reason = spec.title;
      }
      const incident = { id: `INC-${s.nextIncidentId++}`, kind: action.kind, title: spec.title, targetId: action.targetId, status: 'active', createdAt: timestamp(s), taskIds: [] };
      spec.types.forEach((type, index) => {
        const task = { id: `${incident.id}-T${index + 1}`, incidentId: incident.id, title: `${{ emergency_response: 'Secure incident area', mechanic: 'Assess and repair / recover aircraft', inspector: 'Inspect and release surface', }[type]}`,
          requiredType: type, status: 'queued', assigneeId: null, location: action.targetId,
          totalSeconds: spec.durations[index], remainingSeconds: spec.durations[index], dependsOn: index ? [incident.taskIds[index - 1]] : [] };
        if (action.kind === 'aircraft_fault') task.title = 'Inspect, repair and release aircraft';
        s.tasks.push(task); incident.taskIds.push(task.id);
      });
      s.incidents.unshift(incident); record(s, 'incident', `${spec.title} at ${action.targetId}.`, action.targetId);
      if (s.autoDispatch) dispatch(s); break;
    }
    case 'hold': {
      const p = s.pilots.find(p => p.id === action.pilotId); requireValue(p, 'Unknown pilot.');
      requireValue(actor.role !== 'pilot' || actor.id === p.id, 'Pilots can only act for their own aircraft.');
      requireValue(['approach', 'holding', 'ready', 'holding_short'].includes(p.phase), 'Cannot apply a hold during an active movement.');
      requireValue(Number.isInteger(action.seconds) && action.seconds >= 0 && action.seconds <= 900, 'Hold must be 0–900 seconds.');
      p.holdUntil = s.elapsedSeconds + action.seconds;
      record(s, 'plan', `${p.callsign}: hold ${action.seconds}s requested by ${actor.id}.`, p.id); break;
    }
    case 'set_plan': {
      const p = s.pilots.find(p => p.id === action.pilotId); requireValue(p, 'Unknown pilot.');
      requireValue(['approach', 'holding', 'at_gate', 'ready', 'holding_short'].includes(p.phase), 'Cannot change a plan during an active movement.');
      if ('gateId' in action) {
        requireValue(['approach', 'holding'].includes(p.phase), 'Gate reassignment is available before landing only.');
        const g = s.airport.gates.find(g => g.id === action.gateId); requireValue(g && available(g), 'Gate must be free and open.'); p.gateId = g.id;
      }
      if ('runwayId' in action) {
        requireValue(s.airport.runways.some(r => r.id === action.runwayId && r.status === 'open'), 'Planned runway must be open.'); p.plannedRunwayId = action.runwayId;
      }
      for (const field of ['expectedArrival', 'expectedDeparture']) if (field in action) {
        requireValue(typeof action[field] === 'string' && Number.isFinite(Date.parse(action[field])), 'Expected times must be valid ISO dates.'); p[field] = new Date(action[field]).toISOString();
      }
      record(s, 'plan', `${p.callsign} plan updated by ${actor.id}.`, p.id); break;
    }
    case 'assign_task': {
      const crew = allCrews(s).find(c => c.id === action.crewId), task = s.tasks.find(t => t.id === action.taskId);
      requireValue(crew && task, 'Unknown crew member or task.');
      requireValue(actor.role !== 'ground' || actor.id === crew.id, 'Crew agents can only assign themselves.');
      assignTask(s, crew, task); break;
    }
    case 'crew_availability': {
      const crew = allCrews(s).find(c => c.id === action.crewId); requireValue(crew, 'Unknown crew.');
      requireValue(actor.role !== 'ground' || actor.id === crew.id, 'Crew agents can only update themselves.');
      requireValue(crew.status !== 'busy', 'Busy crew cannot be removed from an active task.');
      requireValue(typeof action.available === 'boolean', 'available must be a boolean.'); crew.status = action.available ? 'available' : 'off_shift';
      record(s, 'ground', `${crew.id} is ${crew.status}.`, crew.id); break;
    }
    case 'auto_dispatch': requireValue(typeof action.enabled === 'boolean', 'enabled must be a boolean.'); s.autoDispatch = action.enabled; break;
  }
  s.revision++; assertState(s); return s;
}
export function observation(s, role, id) {
  const base = { schemaVersion: s.schemaVersion, revision: s.revision, simulationTime: timestamp(s), elapsedSeconds: s.elapsedSeconds, units: s.units, role, actorId: id, assumptions: s.assumptions, futureAgentInterface: true, agentsImplemented: false, airportConfiguration: s.airport.configuration || null };
  if (role === 'atc') {
    requireValue(id === s.atc.id, 'Unknown ATC.');
    return clone({ ...base, airport: s.airport, pilots: s.pilots, gateOperations: s.pilots.map(p => ({pilotId:p.id, callsign:p.callsign, gateId:p.gateId, status:gateStatus(s,p)})).filter(p=>p.status), ground: s.ground, incidents: s.incidents, tasks: s.tasks, importedScenarios: s.data?.flightImports || [], delayDisruptions: s.data?.disruptions || [], recentEvents: s.events.slice(0, 20), allowedCommands: ['resource_status', 'set_plan'] });
  }
  if (role === 'pilot') {
    const p = s.pilots.find(p => p.id === id); requireValue(p, 'Unknown pilot.');
    const ids = [p.gateId, p.runwayId, p.taxiwayId, p.plannedRunwayId];
    return clone({ ...base, pilot: p, gateStatus:gateStatus(s,p), importedScenario: s.data?.flightImports.find(f=>f.pilotId===id)||null, delayDisruptions: s.data?.disruptions.filter(d=>d.pilotId===id)||[], estimatedAirborneSecondsAboveReserve: Math.max(0, Math.floor((p.fuelRemainingKg - p.reserveFuelKg) / 0.7)),
      assignedResources: [...s.airport.runways, ...s.airport.taxiways, ...s.airport.gates].filter(r => ids.includes(r.id)),
      ownIncidents: s.incidents.filter(i => i.targetId === id), airportRestrictions: [...s.airport.runways, ...s.airport.taxiways].filter(r => r.status !== 'open'), allowedCommands: ['hold'] });
  }
  requireValue(role === 'ground', 'Unknown observation role.');
  const crew = allCrews(s).find(c => c.id === id); requireValue(crew, 'Unknown ground personnel ID.');
  const own = s.tasks.find(t => t.id === crew.currentTaskId);
  return clone({ ...base, crew, delayDisruptions: s.data?.disruptions.map(d=>({id:d.id,flightId:d.flightId,kind:d.kind,appliedDelayMinutes:d.appliedDelayMinutes,afterOffBlockUtc:d.afterOffBlockUtc}))||[], currentTask: own || null,
    incident: own ? s.incidents.find(i => i.id === own.incidentId) : null,
    availableTasks: s.tasks.filter(t => t.requiredType === crew.type && readyTask(s, t)),
    completedTasks: s.tasks.filter(t => t.assigneeId === id && t.status === 'completed'), allowedCommands: ['assign_task', 'crew_availability'] });
}
export function assertState(s) {
  requireValue(['1.0','1.1'].includes(s.schemaVersion) && Number.isInteger(s.revision) && s.revision >= 0 && Number.isFinite(Date.parse(s.startedAt)) && Number.isInteger(s.elapsedSeconds) && s.elapsedSeconds >= 0, 'Invalid state header.');
  requireValue(Array.isArray(s.ground.crews) && s.ground.crews.length === 3 && s.ground.crews.every(Array.isArray), 'Ground crews must be three nested lists.');
  const crews = allCrews(s);
  requireValue(new Set(crews.map(c => c.id)).size === crews.length, 'Crew IDs must be unique.');
  s.ground.crews.forEach((group, i) => group.forEach(c => requireValue(c.type === CREW_TYPES[i], 'Crew type is in the wrong nested list.')));
  requireValue(new Set(s.pilots.map(p => p.id)).size === s.pilots.length, 'Pilot IDs must be unique.');
  requireValue(new Set(s.pilots.map(p => p.aircraftId)).size === s.pilots.length, 'Aircraft IDs must be unique.');
  for (const p of s.pilots) requireValue(Number.isFinite(p.fuelRemainingKg) && p.fuelRemainingKg >= 0, 'Invalid fuel.');
  for (const r of [...s.airport.runways, ...s.airport.taxiways, ...s.airport.gates]) {
    requireValue(['open', 'closed'].includes(r.status), 'Invalid resource status.');
    requireValue(!r.occupiedBy || live(s).some(p => p.aircraftId === r.occupiedBy), 'Resource has an invalid occupant.');
    requireValue(!(r.status === 'closed' && r.occupiedBy), 'Closed resource cannot have an aircraft reservation.');
    requireValue(!(r.occupiedBy && r.backgroundOccupied),'Gate cannot contain both a modelled aircraft and background occupancy.');
  }
  for (const t of s.tasks.filter(t => t.status === 'active')) {
    const c = crews.find(c => c.id === t.assigneeId);
    requireValue(c && c.type === t.requiredType && c.currentTaskId === t.id && c.status === 'busy', 'Active task assignment is inconsistent.');
    requireValue(t.dependsOn.every(id => s.tasks.some(x => x.id === id && x.status === 'completed')), 'Task prerequisites incomplete.');
  }
  if(s.airport.configuration){
    const c=s.airport.configuration;requireValue(s.airport.runways.length===c.active_runways && s.airport.gates.length===c.gates,'Airport counts disagree with imported configuration.');
    requireValue(['airport','per_runway'].includes(c.capacityScope),'Airport capacity scope is required.');
  }
  if(s.data){
    requireValue(new Set(s.data.flightImports.map(f=>f.id)).size===s.data.flightImports.length,'Duplicate imported flight occurrence.');
    for(const f of s.data.flightImports)requireValue(s.pilots.some(p=>p.id===f.pilotId&&p.flightId===f.id),'Imported flight has no associated pilot.');
  }
  return true;
}
