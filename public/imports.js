// Imported by app.js through an explicit setup callback; no second application state.
export function setupImports({ $, esc, getState, render, request, fire, toast, selectPilot }) {
  let airports=[],airportParsedText=null,airportSource='pasted airport table',flights=null,flightParsedText=null,flightSource='pasted flight table';
  let flightToken=0,airportToken=0;
  const message=(id,text,success=false)=>{ $(id).textContent=text;$(id).classList.toggle('success',success); };
  async function run(id,fn){try{message(id,'');await fn();}catch(e){message(id,e.message);}}
  function airportOptions(){
    const state=getState(),list=airports.length?airports:state?.data?.airportCatalog||[],previous=$('airport-select').value;
    const html=list.map(a=>`<option value="${esc(a.iataCode)}">${esc(a.iataCode+' · '+a.name)}</option>`).join('')||'<option value="">Import or load airport data first</option>';
    if($('airport-select').innerHTML!==html){$('airport-select').innerHTML=html;if(list.some(a=>a.iataCode===previous))$('airport-select').value=previous;}
    $('airport-apply').disabled=!$('airport-select').value||getState().running||($('airport-text').value.trim()!==''&&airportParsedText!==$('airport-text').value);
  }
  function renderFlightPreview(){
    const s=getState(),index=$('flight-row').value,selection=index===''?null:flights?.rows[Number(index)];
    $('flight-inject').disabled=true;
    if(!selection){$('flight-preview-card').textContent=flights?`Choose exactly one of ${flights.rowCount} rows. No rows have been injected.`:'No flight selected. Historical actual times are reference outcomes.';return;}
    if(selection.errors.length){$('flight-preview-card').innerHTML=`<strong>Unsupported source row ${selection.sourceRowNumber}</strong><p>${esc(selection.errors.join(' '))}</p><details><summary>Original fields</summary><pre>${esc(JSON.stringify(selection.rawRow,null,2))}</pre></details>`;return;}
    const f=selection.normalized,problems=[...f.replayIssues];
    if(!s.airport.configuration||s.airport.id!==f.origin)problems.push(`Load origin airport ${f.origin} before injection.`);
    if(s.data?.flightImports.some(i=>i.id===f.id))problems.push('This flight occurrence has already been injected.');
    if(s.running)problems.push('Pause before injecting.');
    if(flightParsedText!==$('flight-text').value)problems.push('Input changed; preview the data again.');
    const display=t=>`${t.localDate||'date unresolved'} ${t.localTime||'—'} (${t.timezone||'timezone unresolved'})`;
    $('flight-preview-card').innerHTML=`<div class="preview-top"><strong>${esc(f.callsign)} · ${esc(f.origin)} → ${esc(f.destination)}</strong><span class="badge ${f.eligible?'available':'closed'}">${f.eligible?'Eligible':'Not eligible'}</span></div><p>Service date ${f.serviceDate} · origin-airport replay: <strong>${esc(f.origin)}</strong></p><div class="preview-values"><span>Total departure delay<strong>${f.values.DEPARTURE_DELAY??'missing'} min</strong></span><span>Late aircraft<strong>${f.values.LATE_AIRCRAFT_DELAY??'missing'} min</strong></span><span>Weather<strong>${f.values.WEATHER_DELAY??'missing'} min</strong></span></div><p>Scheduled off-block: ${esc(display(f.times.scheduledDeparture))}<br>Replayed off-block UTC: ${esc(f.revisedPlan.originOffBlockUtc||'unresolved')}<br>Historical actual departure: ${esc(display(f.times.actualDeparture))}<br>Historical actual arrival: ${esc(display(f.times.actualArrival))}</p><p class="preview-explanation">Only total DEPARTURE_DELAY is applied, once. Cause components are retained without summing them. Prior on-block time is unavailable; turnaround is not inferred for this flight.</p>${problems.length?`<ul class="validation-list">${problems.map(p=>`<li>${esc(p)}</li>`).join('')}</ul>`:''}${f.warnings.length?`<details><summary>${f.warnings.length} unresolved time field(s)</summary><ul>${f.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}<details><summary>Original row and normalised time data</summary><pre>${esc(JSON.stringify({rawRow:selection.rawRow,times:f.times},null,2))}</pre></details>`;
    $('flight-inject').disabled=problems.length>0;
  }
  async function previewAirports(){
    const token=++airportToken,text=$('airport-text').value;
    const result=await request('/api/import/airports/preview',{text,capacityScope:$('capacity-scope').value});
    if(token!==airportToken)return;
    airports=result.airports;airportParsedText=text;airportOptions();message('airport-error',`${airports.length} airport configuration(s) validated. Choose an airport, then apply.`,true);
  }
  async function previewFlights(){
    const token=++flightToken,text=$('flight-text').value;
    const result=await request('/api/import/flights/preview',{text});if(token!==flightToken)return;
    flights=result;flightParsedText=text;
    $('flight-row').innerHTML=(result.rowCount>1?'<option value="">Choose exactly one row</option>':'')+result.rows.map(r=>`<option value="${r.index}">Row ${r.sourceRowNumber} · ${esc(r.normalized?r.normalized.callsign+' / '+r.normalized.origin+' → '+r.normalized.destination:r.rawRow.AIRLINE+' '+r.rawRow.FLIGHT_NUMBER+' / invalid row')}</option>`).join('');
    if(result.rowCount===1)$('flight-row').value='0';
    renderFlightPreview();message('flight-error',`${result.rowCount} row(s) parsed. ${result.rowCount>1?'Choose one; nothing is injected automatically.':'Review the row before injecting.'}`,true);
  }
  async function loadFile(file,id,kind){
    if(!file)return;if(file.size>5*1024*1024)throw new Error('File exceeds 5 MiB. Export a smaller CSV/TSV selection first.');
    if(!/\.(csv|tsv|txt)$/i.test(file.name))throw new Error('Upload CSV, TSV or TXT; export spreadsheet files as CSV first.');
    $(id).value=await file.text();
    if(kind==='airport'){airportSource=file.name;airportParsedText=null;await previewAirports();}
    else{flightSource=file.name;flightParsedText=null;await previewFlights();}
  }
  $('airport-preview').onclick=()=>fire(()=>run('airport-error',previewAirports));
  $('flight-preview').onclick=()=>fire(()=>run('flight-error',previewFlights));
  $('airport-file').onchange=e=>fire(()=>run('airport-error',()=>loadFile(e.target.files[0],'airport-text','airport')));
  $('flight-file').onchange=e=>fire(()=>run('flight-error',()=>loadFile(e.target.files[0],'flight-text','flight')));
  $('airport-text').oninput=()=>{airportToken++;airportParsedText=null;airportSource='pasted airport table';airportOptions();message('airport-error','Input changed. Preview before applying.');};
  $('flight-text').oninput=()=>{flightToken++;flightParsedText=null;flightSource='pasted flight table';renderFlightPreview();message('flight-error','Input changed. Preview before injecting.');};
  $('airport-select').onchange=airportOptions;
  $('capacity-scope').onchange=airportOptions;
  $('flight-row').onchange=renderFlightPreview;
  $('airport-example').onclick=()=>fire(()=>run('airport-error',async()=>{const response=await fetch('/api/examples/airports.csv');$('airport-text').value=await response.text();airportSource='supplied airport examples';await previewAirports();}));
  $('flight-example').onclick=()=>fire(()=>run('flight-error',async()=>{const response=await fetch('/api/examples/flights.csv');$('flight-text').value=await response.text();flightSource='supplied flight examples';await previewFlights();}));
  $('airport-apply').onclick=()=>fire(()=>run('airport-error',async()=>{
    const result=await request('/api/import/airports/apply',{text:$('airport-text').value||undefined,airportCode:$('airport-select').value,capacityScope:$('capacity-scope').value,expectedRevision:getState().revision,sourceName:airportSource});
    render(result);message('airport-error',`${result.airport.id} applied. All underlying resources updated.`,true);toast('Airport configuration applied.');
    if(flights&&flightParsedText===$('flight-text').value)await previewFlights();
  }));
  $('flight-inject').onclick=()=>fire(()=>run('flight-error',async()=>{
    if($('flight-row').value==='')throw new Error('Choose exactly one flight row.');
    const result=await request('/api/import/flights/inject',{text:$('flight-text').value,rowIndex:Number($('flight-row').value),rebaseClock:$('rebase-clock').checked,sourceName:flightSource,expectedRevision:getState().revision});
    const imported=result.data.flightImports.at(-1);selectPilot(imported.pilotId);render(result);
    $('injection-result').textContent=`Injected ${imported.normalized.callsign}: ${imported.normalized.values.DEPARTURE_DELAY} minutes applied once. Historical actuals retained; no physical incident generated.`;
    message('flight-error','Injection saved to shared state.',true);toast('Selected flight injected.');
  }));
  return function renderImportState(){
    const s=getState(),c=s.airport.configuration;airportOptions();
    $('airport-summary').textContent=c?`${s.airport.id} · ${c.active_runways} runways · ${c.gates} gates`:'Import a table to replace demo resources';
    $('config-summary').textContent=c?`${c.iataCode} / ${c.name} · ${c.max_runway_ops_per_hour} ops/hour (${c.capacityScope==='airport'?'airport-wide':'per runway'} assumption) · minimum turnaround ${c.min_turnaround_min} min · initial spare pool ${c.spare_gates_at_any_time} · timezone ${c.timezone||'unresolved'} · background occupancy synthetic`:'Demo airport · synthetic resource counts and timings';
    const first=!s.data?.flightImports.length;$('rebase-clock').disabled=!first;if(!first)$('rebase-clock').checked=false;
    renderFlightPreview();
  };
}
