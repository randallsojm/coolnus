// Multi-agent negotiation panel. Uses the flight row selected in Scenario Control, asks the Python
// backend to negotiate (ATC / Pilot / Ground), and shows the consensus log. The agreed plan is loaded
// into the simulator; press Run to watch it play out.
const $ = id => document.getElementById(id);
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let current = null, shownLog = null;

function zone() { return current?.airport?.configuration?.timezone || 'UTC'; }
function hhmm(iso) { return iso ? new Date(iso).toLocaleTimeString('en-GB', { timeZone: zone(), hour12: false }).slice(0, 5) : '—'; }

const FUEL = { normal: 'normal fuel', minimum_fuel: 'low fuel (no diversion fuel left)', mayday_fuel: 'emergency fuel', diverted: 'diverted' };
const VERB = { PROPOSE: 'proposes a plan', ACCEPT: 'accepts', COUNTER: 'counter-proposes', REJECT: 'rejects', 'NO MESSAGE': 'sent no valid message' };

function turns(logText) {
  const out = []; let turn = 0;
  for (const line of (logText || '').split('\n')) {
    const t = line.match(/^--- Turn (\d+) ---/); if (t) { turn = t[1]; continue; }
    const m = line.match(/^(ATC|PILOT|GROUND)\s+(PROPOSE|ACCEPT|COUNTER|REJECT|NO MESSAGE)\s*(P\d+)?(?: -> (P\d+): (.*?); hard rules)?/);
    if (m) { out.push({ turn, who: m[1], verb: VERB[m[2]], ref: m[4] || m[3] || '', what: m[5] || '' }); continue; }
    const pr = line.match(/PRIORITY REQUEST (\w+): land by ([\d:]+)/);
    if (pr && out.length) out.at(-1).extra = `asks for priority: ${FUEL[pr[1]] || pr[1]}, must land by ${pr[2]}`;
    if (/INVALID:/.test(line) && out.length) out.at(-1).extra = 'message rejected by the protocol checker';
  }
  return out;
}

function show(s) {
  const n = s?.negotiation;
  if (!n) { $('neg-summary').innerHTML = '<p class="subtle">No negotiation yet. Load the demo flights, select a row in Scenario Control, then negotiate.</p>'; $('neg-log').textContent = ''; shownLog = null; return; }
  const r = n.resolution, o = n.outcome, c = n.consensus;
  const df = s.pilots.find(p => p.id === n.disruptedPilotId);
  const base = (n.logText.match(/Without negotiation \(first come, first served\): DF lands ([\d:]+) \((\w+)\)/) || []);
  const hold = (s.airport.weatherHolds || [])[0];
  const decision = r.flight_plan === 'divert' ? `Divert to ${esc(r.divert_to)}`
    : `Land ${esc(r.revised_arrival_time)} · runway ${esc((r.runway || '').replace('R', ''))} · gate ${esc(r.gate)}${r.departure_time ? ` · take off ${esc(r.departure_time)}` : ''}`;
  const steps = turns(n.logText).map(x => `<li><span class="neg-turn">T${x.turn}</span><b>${x.who}</b> ${x.verb}${x.ref ? ' ' + esc(x.ref) : ''}${x.what ? `: ${esc(x.what.replace(/^DF /, ''))}` : ''}${x.extra ? `<br><span class="neg-note">${esc(x.extra)}</span>` : ''}</li>`).join('');
  $('neg-summary').innerHTML = `
    <div class="neg-head ${c.reached ? 'ok' : 'fb'}">${c.reached ? `✓ Agreement reached in turn ${c.turn}` : '⚠ No agreement: non-AI fallback decided'}</div>
    <div class="neg-decision"><span class="eyebrow">${esc(df?.callsign)} · DECISION</span><strong>${decision}</strong><span>${esc(FUEL[r.fuel_status] || '')}</span></div>
    <div class="neg-grid">
      <div><span class="eyebrow">PROBLEM</span>${hold ? `Weather: no landings ${hhmm(hold.start)}–${hhmm(hold.end)}` : 'Late inbound aircraft'}</div>
      <div><span class="eyebrow">WITHOUT NEGOTIATION</span>${base[1] ? `Lands ${esc(base[1])} · ${esc(FUEL[base[2]] || base[2])}` : '—'}</div>
      <div><span class="eyebrow">COST TO OTHERS</span>${o.others_later ? `${o.others_later} flights land ${Number(o.others_extra_delay_min).toFixed(1)} min later in total` : 'No other flight delayed'}</div>
      <div><span class="eyebrow">SAFETY</span>${c.reached ? 'All hard rules pass' : 'Checked by the fallback'}</div>
    </div>
    <span class="eyebrow">HOW THEY AGREED</span><ol class="neg-steps">${steps}</ol>
    <p class="subtle">Live: ${esc((df?.phase || '').replaceAll('_', ' '))} · ${Math.round(df?.fuelRemainingKg ?? 0)} kg fuel${df?.actualLandingAt ? ` · landed ${hhmm(df.actualLandingAt)}` : ' · press ▶ Run to watch'}</p>
    <details><summary>More detail</summary>
      <p class="subtle">Connections kept: ${o.connections_kept}/${o.connections_total} passengers · scores ATC ${o.scores.ATC} / Pilot ${o.scores.PILOT} / Ground ${o.scores.GROUND} · ${esc(n.agents)} agents</p>
      <p class="subtle">Crew shifts: ${(s.ground.turnaroundCrews || []).map(cr => `${esc(cr.id)} ${hhmm(cr.shiftStart)}–${hhmm(cr.shiftEnd)}`).join(' · ')}</p>
      ${n.notes?.length ? `<p class="subtle">${n.notes.map(esc).join('<br>')}</p>` : ''}
      <p class="subtle">Log file: ${esc(n.logTxt)}</p></details>`;
  if (shownLog !== n.logText) { $('neg-log').textContent = n.logText; shownLog = n.logText; }
}

async function negotiate() {
  $('neg-error').textContent = '';
  const row = $('flight-row').value;
  if (row === '') { $('neg-error').textContent = 'Preview flight data in Scenario Control and select exactly one row first.'; return; }
  if (current?.running) { $('neg-error').textContent = 'Pause the simulation first.'; return; }
  $('neg-run').disabled = true;
  $('neg-status').textContent = $('neg-agents').value === 'claude' ? 'Negotiating with Claude agents… this can take a minute or two.' : 'Negotiating…';
  try {
    const res = await fetch('/api/negotiate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('flight-text').value, rowIndex: Number(row), phase: $('neg-phase').value,
        agents: $('neg-agents').value, lowFuel: $('neg-lowfuel').checked, expectedRevision: current.revision }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Negotiation failed.');
    current = data; show(data);
    $('neg-status').textContent = 'Done. The agreed plan is loaded: press ▶ Run simulation to watch it.';
  } catch (e) { $('neg-error').textContent = e.message; $('neg-status').textContent = ''; }
  finally { $('neg-run').disabled = false; }
}

$('neg-run').onclick = negotiate;
$('neg-demo').onclick = async () => {
  $('flight-text').value = await (await fetch('/api/examples/negotiation_demos.csv')).text();
  $('flight-text').dispatchEvent(new Event('input'));
  $('flight-preview').click();
  $('neg-status').textContent = 'Demo flights loaded. Row 2 (DL1446 PHX→ATL) with "Low fuel" shows the pilot asking ATC for priority.';
};
const stream = new EventSource('/api/events');
stream.onmessage = e => { current = JSON.parse(e.data); show(current); };
fetch('/api/state').then(r => r.json()).then(s => { current = s; show(s); });
