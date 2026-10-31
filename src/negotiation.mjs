// Multi-agent negotiation: the simulator is the front end, the Python backend (backend/) is the brain.
// 1. The user selects one flights.csv row in Scenario Control.
// 2. runNegotiation() sends that row to backend/bridge.py, which builds the situation, runs the
//    ATC / Pilot / Ground negotiation (3 turns, constraint checks, fallback) and writes the consensus log.
// 3. buildWorld() loads the result into the simulator: the arrival airport from the workbook, the
//    aircraft with their catalogue fuel burn, the weather hold, crew shifts, and the AGREED landing times,
//    runways and gates. Running the simulation then plays the agreed plan out.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { initialState, record, assertState, clone } from './simulator.mjs';
import { check, parseAirportTable, previewFlights, localToUtc, addDays } from './data.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BACKEND = path.join(root, 'backend');
const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const TIMEOUT_MS = 15 * 60 * 1000;

export function callBackend(payload) {
  return new Promise((resolve, reject) => {
    let child;
    try { child = spawn(PYTHON, ['bridge.py'], { cwd: BACKEND, env: process.env }); }
    catch (e) { return reject(new Error(`Could not start Python (${PYTHON}): ${e.message}`)); }
    let out = '', err = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('Negotiation backend timed out.')); }, TIMEOUT_MS);
    child.stdout.on('data', d => { out += d; });
    child.stderr.on('data', d => { err += d; });
    child.on('error', e => { clearTimeout(timer); reject(new Error(`Could not start Python (${PYTHON}): ${e.message}. Set the PYTHON environment variable.`)); });
    child.on('close', () => {
      clearTimeout(timer);
      const line = out.trim().split('\n').filter(Boolean).at(-1);
      try { resolve(JSON.parse(line)); }
      catch { reject(new Error(`Negotiation backend failed: ${(err || out).trim().slice(-600) || 'no output'}`)); }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

// Backend times are minutes since local midnight at the arrival airport on the flight date.
function isoAt(date, zone, minutes) {
  const whole = Math.floor(minutes), day = Math.floor(whole / 1440), m = whole - day * 1440;
  const r = localToUtc(addDays(date, day), { hour: Math.floor(m / 60), minute: m % 60 }, zone);
  check(r.utc, `Cannot convert a local time at the arrival airport: ${r.reason}`);
  return new Date(Date.parse(r.utc) + Math.round((minutes - whole) * 60000)).toISOString();
}
const gateId = id => `G${parseInt(id.slice(1), 10)}`;      // backend "G050" -> simulator "G50"
const runwayId = id => `RWY-${id.slice(1)}`;               // backend "R1"   -> simulator "RWY-1"

export async function runNegotiation(state, { text, rowIndex, phase = 'airborne', agents = 'rule', lowFuel = false, expectedRevision }) {
  check(Number.isInteger(expectedRevision) && state.revision === expectedRevision, 'Stale revision: refresh and try again.');
  check(!state.running, 'Pause the simulation before negotiating.');
  check(['airborne', 'on_ground_at_origin'].includes(phase), 'Choose a phase.');
  check(['rule', 'claude'].includes(agents), 'Choose rule or claude agents.');
  check(Number.isInteger(rowIndex) && rowIndex >= 0, 'Preview the flight data and select exactly one row.');
  const selection = previewFlights(text, state.data?.airportCatalog || []).rows[rowIndex];
  check(selection, 'Selected row does not exist.');
  const out = await callBackend({ row: selection.rawRow, phase, agents, low_fuel: Boolean(lowFuel) });
  check(out.ok, out.error || 'Negotiation backend failed.');
  return buildWorld(state, out, agents);
}

export function buildWorld(prev, out, agents) {
  const w = out.world;
  const config = parseAirportTable(w.airport_csv, 'airport').airports[0];
  check(config.timezone, `No timezone is known for ${w.iata}; add a TIMEZONE column for it in the airport data.`);
  const t = m => isoAt(w.date, config.timezone, m);
  const s = initialState();
  s.revision = prev.revision + 1;
  s.data.airportCatalog = clone(prev.data?.airportCatalog || []);
  s.startedAt = t(w.start); s.elapsedSeconds = 0; s.running = false; s.events = []; s.nextEventId = 1;
  s.pilots = []; s.incidents = []; s.tasks = [];
  const res = (id, name) => ({ id, name, status: 'open', occupiedBy: null, reason: null, synthetic: true });
  const listed = new Map(w.gates.map(g => [gateId(g.id), g]));
  s.airport = {
    id: config.iataCode, name: config.name, configuration: config, configurationSource: 'Challenge workbook (negotiation backend)',
    configurationImportedAt: s.startedAt,
    runways: w.runways.map(r => ({ ...res(runwayId(r.id), `Runway ${r.id.slice(1)} · ${r.use}`), use: r.use, availableAfter: 0 })),
    taxiways: [res('TWY-A', 'Alpha'), res('TWY-B', 'Bravo')],
    gates: Array.from({ length: config.gates }, (_, i) => {
      const id = `G${i + 1}`, g = listed.get(id);
      return { ...res(id, `Gate ${i + 1}${g ? ' · ' + g.size : ''}`), taxiwayId: i < Math.ceil(config.gates / 2) ? 'TWY-A' : 'TWY-B',
        size: g ? g.size : null, backgroundOccupied: !g };
    }),
    operationHistory: [],
    weatherHolds: w.weather_holds.map(h => ({ start: t(h.start), end: t(h.end) })),
    assumptions: ['Airport limits from the challenge workbook; runway names, gate sizes and geometry are schematic.',
      'Gates not used by the scenario are shown as background occupied.']
  };
  const turnaround = config.min_turnaround_min;
  for (const f of w.flights) {
    const p = {
      id: `P-${f.id}`, name: f.disrupted ? 'Disrupted flight' : f.kind === 'arrival' ? 'Arrival' : 'Departure',
      aircraftId: `AC-${f.id}`, callsign: f.callsign, source: 'negotiation', negotiated: true, disrupted: f.disrupted,
      aircraftType: f.aircraft_type, wake: f.wake, onBlockAt: null, runwayId: null, taxiwayId: null,
      plannedRunwayId: f.runway ? runwayId(f.runway) : undefined, gateId: f.gate ? gateId(f.gate) : null,
      reserveFuelKg: Math.round(f.reserve_kg), cruiseBurnKgPerSec: f.cruise_kg_s, holdBurnKgPerSec: f.hold_kg_s,
      holdUntil: 0, maintenanceHold: false, fuelAlerted: false, remainingSeconds: 0
    };
    if (f.kind === 'arrival') {
      Object.assign(p, {
        phase: 'approach', fuelRemainingKg: Math.round(f.fuel_kg), expectedArrival: t(f.planned),
        expectedDeparture: t((f.in_block ?? f.planned) + turnaround + 30),
        assignedLandingAt: f.landing != null ? t(f.landing) : null,
        taxiInSeconds: Math.max(30, Math.round(f.taxi_in_min * 60) - 60),     // 60 s landing roll + taxi = scenario taxi-in
        airborneFrom: f.airborne_from != null ? t(f.airborne_from) : null,
        divertAt: f.divert_at != null ? t(f.divert_at) : null, divertTo: f.divert_to
      });
    } else {
      Object.assign(p, {
        phase: 'ready', onBlockAt: s.startedAt, fuelRemainingKg: Math.round(f.reserve_kg * 4),
        expectedArrival: s.startedAt, expectedDeparture: t(f.takeoff - 1.5)   // push back 90 s before take-off
      });
      const g = s.airport.gates.find(x => x.id === p.gateId);
      if (g) { g.occupiedBy = p.aircraftId; g.backgroundOccupied = false; }
    }
    s.pilots.push(p);
  }
  s.ground.turnaroundCrews = w.crews.map(c => ({ id: c.id, shiftStart: t(c.shift_start), shiftEnd: t(c.shift_end),
    flights: c.flights.map(f => `P-${f}`) }));
  const r = out.resolution;
  s.negotiation = { agents, phase: w.phase, rowId: out.row_id, consensus: out.consensus, decidedBy: out.decided_by,
    resolution: r, outcome: out.outcome, statedChanges: out.stated_changes, notes: out.notes,
    logText: out.log_text, logJson: out.log_json, logTxt: out.log_txt, disruptedPilotId: 'P-DF' };
  s.assumptions = [...s.assumptions.filter(a => !/Fuel burn|Runway use/.test(a)),
    'Negotiated aircraft land at the agreed times; spacing comes from the workbook wake rules via those times.',
    'Fuel burn from the aircraft catalogue; reserve = the workbook 30-minute final reserve.',
    'Weather hold: no landings while it lasts; runway capacity itself is unchanged (workbook movements/hour).'];
  const df = s.pilots.find(p => p.disrupted);
  record(s, 'system', `Negotiation (${agents} agents) for ${df.callsign}: ${out.consensus.reached ? `consensus in turn ${out.consensus.turn} on ${out.consensus.proposal_id}` : 'no consensus, fallback decided'}.`);
  record(s, 'plan', r.flight_plan === 'divert' ? `${df.callsign}: divert to ${r.divert_to}.`
    : `${df.callsign}: land ${r.revised_arrival_time} on ${r.runway}, gate ${r.gate}${r.departure_time ? `, take-off ${r.departure_time}` : ''}.`);
  for (const h of w.weather_holds) record(s, 'incident', `Weather over ${config.iataCode}: no landings for ${Math.round(h.end - h.start)} min from the start of the hold.`);
  s.revision++;
  assertState(s);
  return s;
}
