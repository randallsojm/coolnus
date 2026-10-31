// Pure import/normalisation functions. Historical rows never directly mutate the simulation.
export const MAX_IMPORT_BYTES = 5 * 1024 * 1024;
export const MAX_ROWS = 10000;
export const AIRPORT_COLUMNS = 'IATA_CODE AIRPORT CITY STATE delayed_departures_in_flights_csv size_class active_runways gates max_runway_ops_per_hour min_turnaround_min spare_gates_at_any_time'.split(' ');
export const FLIGHT_COLUMNS = 'YEAR MONTH DAY DAY_OF_WEEK AIRLINE FLIGHT_NUMBER TAIL_NUMBER ORIGIN_AIRPORT DESTINATION_AIRPORT SCHEDULED_DEPARTURE DEPARTURE_TIME DEPARTURE_DELAY TAXI_OUT WHEELS_OFF SCHEDULED_TIME ELAPSED_TIME AIR_TIME DISTANCE WHEELS_ON TAXI_IN SCHEDULED_ARRIVAL ARRIVAL_TIME ARRIVAL_DELAY DIVERTED CANCELLED CANCELLATION_REASON AIR_SYSTEM_DELAY SECURITY_DELAY AIRLINE_DELAY LATE_AIRCRAFT_DELAY WEATHER_DELAY'.split(' ');
// Explicit, curated airport metadata; not inferred from the browser or an IATA prefix.
export const AIRPORT_TIMEZONES = {
  DFW:'America/Chicago',ORD:'America/Chicago',MDW:'America/Chicago',ATL:'America/New_York',DEN:'America/Denver',
  LAX:'America/Los_Angeles',SFO:'America/Los_Angeles',LAS:'America/Los_Angeles',LGA:'America/New_York',
  JFK:'America/New_York',EWR:'America/New_York',IAH:'America/Chicago',MCO:'America/New_York',PHX:'America/Phoenix',
  BOS:'America/New_York',BWI:'America/New_York',DTW:'America/Detroit',MSP:'America/Chicago',MIA:'America/New_York',
  SAN:'America/Los_Angeles',DAL:'America/Chicago',HNL:'Pacific/Honolulu',HDN:'America/Denver',SJC:'America/Los_Angeles',
  SLC:'America/Denver',SMF:'America/Los_Angeles',TUS:'America/Phoenix',IAD:'America/New_York',DCA:'America/New_York'
};
export function check(condition, message) { if(!condition) throw new Error(message); }
export function parseDelimited(input, required = []) {
  check(typeof input === 'string' && input.trim(), 'Paste or upload a nonempty CSV/TSV table with a header.');
  check(new TextEncoder().encode(input).length <= MAX_IMPORT_BYTES, 'Import exceeds 5 MiB. Export a smaller flight selection first.');
  const text=input.replace(/^\uFEFF/,'');
  let quoted=false, commas=0,tabs=0;
  for(let i=0;i<text.length;i++) { const c=text[i]; if(c==='"') { if(quoted&&text[i+1]==='"')i++;else quoted=!quoted; } else if(!quoted) { if(c==='\n'||c==='\r')break; if(c===',')commas++;if(c==='\t')tabs++; } }
  const delimiter=tabs>commas?'\t':',';
  const records=[]; let row=[],value='',inQuotes=false,afterQuote=false,started=false;
  function cell(){row.push(value);value='';afterQuote=false;started=false;}
  function line(){cell();if(row.some(c=>c.trim()!==''))records.push(row);row=[];check(records.length<=MAX_ROWS+1,`Import exceeds ${MAX_ROWS} rows. Select a smaller file.`);}
  for(let i=0;i<text.length;i++) {
    const c=text[i];
    if(inQuotes){ if(c==='"'){if(text[i+1]==='"'){value+='"';i++;}else{inQuotes=false;afterQuote=true;}}else value+=c;continue; }
    if(afterQuote && ![delimiter,'\r','\n',' ','\t'].includes(c)) throw new Error('Unexpected characters after a quoted field.');
    if(c===delimiter){cell();continue;}
    if(c==='\n'||c==='\r'){line();if(c==='\r'&&text[i+1]==='\n')i++;continue;}
    if(afterQuote){continue;}
    if(c==='"'){check(!started && value.trim()==='', 'Quotes must enclose a whole field.');value='';inQuotes=true;started=true;continue;}
    value+=c;started=true;
  }
  check(!inQuotes,'Unclosed quoted field.');
  if(value!==''||row.length||afterQuote)line();
  check(records.length>=2,'A header and at least one data row are required.');
  const headers=records.shift().map(s=>s.trim());
  check(headers.every(h=>h&&!['__proto__','constructor','prototype'].includes(h)),'Invalid or empty column name.');
  check(new Set(headers).size===headers.length,'Duplicate column names are not allowed.');
  const missing=required.filter(h=>!headers.includes(h));check(!missing.length,`Missing columns: ${missing.join(', ')}.`);
  const rows=records.map((cells,index)=>{check(cells.length===headers.length,`Row ${index+2}: expected ${headers.length} fields, received ${cells.length}.`);return Object.fromEntries(headers.map((h,i)=>[h,cells[i]]));});
  return {delimiter:delimiter==='\t'?'TSV':'CSV',headers,rows};
}
function number(raw,key,{integer=false,min=-Infinity,max=Infinity,required=false}={}) {
  const value=raw[key]?.trim();if(value===undefined||value===''){check(!required,`${key} is required.`);return null;}
  check(/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(value),`${key} must be numeric.`);
  const n=Number(value);check(Number.isFinite(n)&&(!integer||Number.isInteger(n))&&n>=min&&n<=max,`${key} must be ${integer?'a whole number':'a number'} between ${min} and ${max}.`);return n;
}
export function validTimezone(zone) {try {new Intl.DateTimeFormat('en',{timeZone:zone}).format();return true;}catch{return false;}}
export function parseAirportTable(text,capacityScope='airport') {
  check(['airport','per_runway'].includes(capacityScope),'Choose an explicit capacity scope.');
  const table=parseDelimited(text,AIRPORT_COLUMNS),seen=new Set();
  const airports=table.rows.map((raw,index)=>{
    try{
      const code=raw.IATA_CODE.trim().toUpperCase();check(/^[A-Z]{3}$/.test(code),'IATA_CODE must contain three letters.');
      check(!seen.has(code),`Duplicate airport ${code}.`);seen.add(code);check(raw.AIRPORT.trim(),'AIRPORT name is required.');
      const counts={};for(const key of ['active_runways','gates','max_runway_ops_per_hour','min_turnaround_min','spare_gates_at_any_time'])counts[key]=number(raw,key,{required:true,integer:true,min:key==='min_turnaround_min'||key==='spare_gates_at_any_time'?0:1,max:key==='active_runways'?16:key==='gates'?2000:10000});
      check(counts.spare_gates_at_any_time<=counts.gates,'Spare gates cannot exceed total gates.');
      const explicit=(raw.TIMEZONE||raw.timezone||'').trim();check(!explicit||validTimezone(explicit),'TIMEZONE must be a valid IANA timezone.');
      return {iataCode:code,name:raw.AIRPORT.trim(),city:raw.CITY.trim(),region:raw.STATE.trim(),sizeClass:raw.size_class.trim(),
        delayedDeparturesInDataset:number(raw,'delayed_departures_in_flights_csv',{integer:true,min:0}),...counts,
        capacityScope,capacityScopeSource:'Operator-selected assumption; source column does not define scope.',
        timezone:explicit||AIRPORT_TIMEZONES[code]||null,timezoneSource:explicit?'TIMEZONE input column':AIRPORT_TIMEZONES[code]?'Curated airport timezone metadata':'Unresolved',rawRow:raw};
    }catch(e){throw new Error(`Airport row ${index+2}: ${e.message}`);}
  });return {delimiter:table.delimiter,airports};
}
export function parseHhmm(value) {
  if(value===null||value===undefined||String(value).trim()==='')return null;
  const str=String(value).trim();check(/^\d{1,4}$/.test(str),`Invalid HHMM time: ${str}.`);
  const n=Number(str);if(n===2400)return {localTime:'00:00',hour:0,minute:0,dayOffset:1,raw:str};
  const hour=Math.floor(n/100),minute=n%100;check(hour<24&&minute<60,`Invalid HHMM time: ${str}.`);
  return {localTime:`${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`,hour,minute,dayOffset:0,raw:str};
}
const dateCache=new Map();
function parts(epoch,zone){
  if(!dateCache.has(zone))dateCache.set(zone,new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}));
  return Object.fromEntries(dateCache.get(zone).formatToParts(new Date(epoch)).filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]));
}
export function addDays(date,days){return new Date(Date.parse(date+'T00:00:00Z')+days*86400000).toISOString().slice(0,10);}
export function localToUtc(date,time,zone){
  if(!zone)return {utc:null,status:'unresolved',reason:'Airport timezone metadata is unavailable.'};
  const [year,month,day]=date.split('-').map(Number);const target=Date.UTC(year,month-1,day,time.hour,time.minute);
  const offsets=new Set();
  for(const h of [-36,-12,0,12,36]){const sample=target+h*3600000,p=parts(sample,zone);offsets.add(Date.UTC(p.year,p.month-1,p.day,p.hour,p.minute,p.second)-sample);}
  const candidates=[...offsets].map(o=>target-o).filter(t=>{const p=parts(t,zone);return p.year===year&&p.month===month&&p.day===day&&p.hour===time.hour&&p.minute===time.minute;});
  if(candidates.length!==1)return {utc:null,status:'unresolved',reason:candidates.length?'Ambiguous local time during a daylight-saving transition.':'Nonexistent local time during a daylight-saving transition.'};
  return {utc:new Date(candidates[0]).toISOString(),status:'resolved',reason:null};
}
function instant(raw,date,zone,anchor,requiresAnchor=false){
  const time=parseHhmm(raw);if(!time)return {raw:raw??'',localTime:null,localDate:null,timezone:zone,utc:null,status:'missing',reason:'No source time.'};
  const base={raw:time.raw,localTime:time.localTime,localDate:requiresAnchor?null:addDays(date,time.dayOffset),timezone:zone,utc:null,status:'unresolved',reason:null};
  if(!zone)return {...base,reason:'Airport timezone metadata is unavailable; local clock retained.'};
  if(anchor===null||anchor===undefined){
    if(requiresAnchor)return {...base,reason:'Date rollover cannot be resolved without a duration/delay anchor.'};
    return {...base,...localToUtc(base.localDate,time,zone)};
  }
  // A reliable duration/delay anchors the date. Require agreement, not a guess across timezones.
  const p=parts(anchor,zone),candidateDate=`${p.year}-${String(p.month).padStart(2,'0')}-${String(p.day).padStart(2,'0')}`;
  const resolved=localToUtc(candidateDate,time,zone);
  if(resolved.utc && Math.abs(Date.parse(resolved.utc)-anchor)>60000)return {...base,reason:'Source clock disagrees with its supplied duration/delay anchor.'};
  return {...base,localDate:candidateDate,...resolved};
}
export function normalizeFlight(raw,airportCatalog=[]){
  const values={};
  for(const key of ['YEAR','MONTH','DAY','DAY_OF_WEEK','FLIGHT_NUMBER'])values[key]=number(raw,key,{required:key!=='DAY_OF_WEEK',integer:true,min:1,max:key==='YEAR'?9999:key==='MONTH'?12:key==='DAY'?31:key==='DAY_OF_WEEK'?7:999999});
  const date=`${String(values.YEAR).padStart(4,'0')}-${String(values.MONTH).padStart(2,'0')}-${String(values.DAY).padStart(2,'0')}`;
  check(new Date(date+'T00:00:00Z').toISOString().slice(0,10)===date,'Invalid flight calendar date.');
  for(const key of ['AIRLINE','ORIGIN_AIRPORT','DESTINATION_AIRPORT'])check(raw[key]?.trim(),`${key} is required.`);
  const origin=raw.ORIGIN_AIRPORT.trim().toUpperCase(),destination=raw.DESTINATION_AIRPORT.trim().toUpperCase();
  for(const key of ['DEPARTURE_DELAY','ARRIVAL_DELAY'])values[key]=number(raw,key);
  for(const key of ['TAXI_OUT','SCHEDULED_TIME','ELAPSED_TIME','AIR_TIME','DISTANCE','TAXI_IN','AIR_SYSTEM_DELAY','SECURITY_DELAY','AIRLINE_DELAY','LATE_AIRCRAFT_DELAY','WEATHER_DELAY'])values[key]=number(raw,key,{min:0});
  for(const key of ['DIVERTED','CANCELLED'])values[key]=number(raw,key,{integer:true,min:0,max:1});
  for(const key of ['SCHEDULED_DEPARTURE','DEPARTURE_TIME','WHEELS_OFF','WHEELS_ON','SCHEDULED_ARRIVAL','ARRIVAL_TIME'])parseHhmm(raw[key]);
  const originTimezone=airportCatalog.find(a=>a.iataCode===origin)?.timezone||AIRPORT_TIMEZONES[origin]||null;
  const destinationTimezone=airportCatalog.find(a=>a.iataCode===destination)?.timezone||AIRPORT_TIMEZONES[destination]||null;
  const scheduledDeparture=instant(raw.SCHEDULED_DEPARTURE,date,originTimezone);
  const plus=(time,minutes)=>time?.utc&&minutes!==null?Date.parse(time.utc)+minutes*60000:null;
  const actualDeparture=instant(raw.DEPARTURE_TIME,date,originTimezone,plus(scheduledDeparture,values.DEPARTURE_DELAY),true);
  const scheduledArrival=instant(raw.SCHEDULED_ARRIVAL,date,destinationTimezone,plus(scheduledDeparture,values.SCHEDULED_TIME),true);
  const actualArrival=instant(raw.ARRIVAL_TIME,date,destinationTimezone,plus(actualDeparture,values.ELAPSED_TIME)??plus(scheduledArrival,values.ARRIVAL_DELAY),true);
  const wheelsOff=instant(raw.WHEELS_OFF,date,originTimezone,plus(actualDeparture,values.TAXI_OUT),true);
  const wheelsOn=instant(raw.WHEELS_ON,date,destinationTimezone,plus(actualArrival,values.TAXI_IN===null?null:-values.TAXI_IN)??plus(wheelsOff,values.AIR_TIME),true);
  const times={scheduledDeparture,actualDeparture,wheelsOff,scheduledArrival,wheelsOn,actualArrival};
  const eligible=(values.WEATHER_DELAY??-Infinity)>15||(values.LATE_AIRCRAFT_DELAY??-Infinity)>20;
  const replayIssues=[];
  if(!eligible)replayIssues.push('Requires WEATHER_DELAY > 15 or LATE_AIRCRAFT_DELAY > 20.');
  if(values.CANCELLED!==0)replayIssues.push(values.CANCELLED===1?'Cancelled flights are not replayed in this phase.':'CANCELLED status is missing.');
  if(values.DIVERTED!==0)replayIssues.push(values.DIVERTED===1?'Diverted flights are not replayed in this phase.':'DIVERTED status is missing.');
  if(values.DEPARTURE_DELAY===null||values.DEPARTURE_DELAY<0)replayIssues.push('Replay requires a nonnegative DEPARTURE_DELAY.');
  if(!scheduledDeparture.utc)replayIssues.push('Scheduled departure UTC is unresolved: '+(scheduledDeparture.reason||'source time missing.'));
  const airline=raw.AIRLINE.trim().toUpperCase();check(/^[A-Z0-9]{2,3}$/.test(airline),'AIRLINE must be a 2–3 character code.');
  check(/^[A-Z0-9]{3,5}$/.test(origin)&&/^[A-Z0-9]{3,5}$/.test(destination),'Airport codes must contain 3–5 alphanumeric characters.');
  const id=`FL-${date.replaceAll('-','')}-${airline}-${values.FLIGHT_NUMBER}-${origin}-${destination}-${raw.SCHEDULED_DEPARTURE.trim().padStart(4,'0')||'unknown'}`;
  return {id,serviceDate:date,airline,flightNumber:values.FLIGHT_NUMBER,callsign:airline+values.FLIGHT_NUMBER,tailNumber:raw.TAIL_NUMBER?.trim()||null,origin,destination,
    values,times,eligible,replaySupported:!replayIssues.length,replayIssues,
    warnings:Object.entries(times).filter(([,t])=>t.status==='unresolved').map(([key,t])=>`${key}: ${t.reason}`),
    delayComponents:{weather:values.WEATHER_DELAY,lateAircraft:values.LATE_AIRCRAFT_DELAY,airline:values.AIRLINE_DELAY,airSystem:values.AIR_SYSTEM_DELAY,security:values.SECURITY_DELAY},
    revisedPlan:{originOffBlockUtc:plus(scheduledDeparture,values.DEPARTURE_DELAY)!==null?new Date(plus(scheduledDeparture,values.DEPARTURE_DELAY)).toISOString():null,arrivalUtc:null},
    historicalActual:{departure:actualDeparture,wheelsOff,wheelsOn,arrival:actualArrival}};
}
export function previewFlights(text,catalog=[]){
  const table=parseDelimited(text,FLIGHT_COLUMNS);
  return {delimiter:table.delimiter,rowCount:table.rows.length,rows:table.rows.map((rawRow,index)=>{
    try{return {index,sourceRowNumber:index+2,rawRow,normalized:normalizeFlight(rawRow,catalog),errors:[]};}
    catch(e){return {index,sourceRowNumber:index+2,rawRow,normalized:null,errors:[e.message]};}
  })};
}
