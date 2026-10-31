// Read-only view of the existing simulator timers; does not start new servicing.
export function gateStatus(s, p) {
  if (!['at_gate', 'ready', 'awaiting_stand'].includes(p.phase)) return null;
  const now = Date.parse(s.startedAt) + s.elapsedSeconds * 1000;
  const knownTurnaround = p.phase !== 'awaiting_stand' && (p.source !== 'historical_import' || Boolean(p.onBlockAt));
  const remaining = knownTurnaround ? Math.max(0, p.remainingSeconds) : null;
  const gate = s.airport.gates.find(g => g.id === p.gateId);
  const taxiway = gate && s.airport.taxiways.find(t => t.id === gate.taxiwayId);
  const releaseTime = Math.max(0, ...[p.expectedDeparture, p.earliestOffBlockAt].map(x => x ? Date.parse(x) : 0));
  const departureWaitSeconds = Math.max(0, Math.ceil((releaseTime - now) / 1000));
  const holdRemainingSeconds = Math.max(0, p.holdUntil - s.elapsedSeconds);
  const blockers = [];
  if (!gate) blockers.push('Waiting for an available stand');
  else if (gate.status !== 'open') blockers.push('Gate closed');
  if (remaining > 0) blockers.push('Turnaround countdown running');
  if (p.maintenanceHold) blockers.push('Maintenance hold');
  if (departureWaitSeconds > 0) blockers.push('Waiting for departure time');
  if (holdRemainingSeconds > 0) blockers.push('Departure hold');
  if (gate && (!taxiway || taxiway.status !== 'open' || taxiway.occupiedBy)) blockers.push('Outbound taxiway unavailable');
  return {
    turnaroundStatus: !knownTurnaround ? 'unknown' : remaining > 0 ? 'in_progress' : 'completed',
    turnaroundRemainingSeconds: remaining,
    basis: knownTurnaround ? 'Existing simulated turnaround timer.' : 'Prior on-block time is unavailable. No new turnaround timer is inferred; existing readiness is assumed.',
    departureReleaseAt: new Date(releaseTime).toISOString(), departureWaitSeconds, holdRemainingSeconds,
    readyForPushback: blockers.length === 0, blockers
  };
}
