// Run from jobsweep with: node tests/test_triage_sync.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const TriageSync = require('../src/jobsweep/web/static/triage-sync.js');
const clone = value => JSON.parse(JSON.stringify(value));
const memory = () => {
  const values = {};
  return {get:(key,fallback)=>key in values ? clone(values[key]) : fallback,
    set:(key,value)=>{values[key]=clone(value);return true;}};
};
const response = (body,status=200) => ({ok:status>=200&&status<300,status,json:async()=>clone(body)});
const deferred = () => {let resolve; const promise = new Promise(done=>{resolve=done;}); return {promise,resolve};};
function backend(initial=[]) {
  const records = Object.fromEntries(initial.map(record=>[record.uid,clone(record)]));
  const sent = [];
  return {records,sent,async fetcher(url,opts) {
    assert.equal(url,'/api/triage');
    if (opts.method !== 'PUT') return response({records:Object.values(records)});
    const changes = JSON.parse(opts.body).changes;
    sent.push(clone(changes));
    const returned = [], conflicts = [];
    for (const change of changes) {
      const current = records[change.uid];
      if (change.expected_version !== (current?.version || 0)) {
        conflicts.push(change.uid);
        if (current) returned.push(current);
      } else {
        const {expected_version,...fields} = change;
        const saved = {...fields,version:expected_version+1,updated_at:'2026-10-25T21:00:00Z'};
        records[change.uid] = saved;
        returned.push(saved);
      }
    }
    return response({records:returned,conflicts},conflicts.length ? 409 : 200);
  }};
}
const snapshot = {uid:'job:1',title:'Research Analyst',company:'Example',lanes:['research'],groups:['policy-research'],
  score:70,suitability:'eligible',description:'Role description',posted_at_source:'greenhouse:first_published'};

(async () => {
  // An offline edit survives a new client instance and only clears after server acknowledgement.
  const storage = memory();
  let sync = new TriageSync({storage,fetcher:async()=>{throw new Error('offline');}});
  sync.stage('job:1','applied','Referral received',snapshot);
  await sync.refresh();
  assert.equal(sync.pending['job:1'].note,'Referral received');
  assert.notEqual(sync.label(),'Synced privately across your devices');
  const server = backend();
  sync = new TriageSync({storage,fetcher:server.fetcher});
  assert.equal(sync.view()['job:1'].job.title,'Research Analyst');
  await sync.refresh();
  assert.equal(Object.keys(sync.pending).length,0);
  assert.equal(server.records['job:1'].note,'Referral received');
  assert.equal(sync.records['job:1'].version,1);
  assert.equal(sync.records['job:1'].updated_at,'2026-10-25T21:00:00Z');
  assert.equal(sync.label(),'Synced privately across your devices');

  // A second edit while PUT is in flight must be sent with the acknowledgement's new version.
  const firstPut = deferred(), releasePut = deferred();
  let puts = 0;
  const inflightServer = backend();
  const inflight = new TriageSync({storage:memory(),fetcher:async(url,opts)=>{
    if (opts.method === 'PUT' && ++puts === 1) {firstPut.resolve(); await releasePut.promise;}
    return inflightServer.fetcher(url,opts);
  }});
  inflight.stage('job:1','saved','First note',snapshot);
  const running = inflight.refresh();
  await firstPut.promise;
  inflight.stage('job:1','applied','Newer note while saving',snapshot);
  releasePut.resolve();
  await running;
  assert.equal(inflightServer.sent.length,2);
  assert.equal(inflightServer.sent[1][0].expected_version,1);
  assert.equal(inflightServer.records['job:1'].note,'Newer note while saving');
  assert.equal(inflightServer.records['job:1'].status,'applied');
  assert.equal(Object.keys(inflight.pending).length,0);

  // Conflicts preserve both versions, including across reload, and never auto-overwrite the server.
  const conflictStorage = memory();
  const conflictServer = backend([{uid:'job:1',status:'interviewing',note:'Cloud edit',version:2,job:snapshot}]);
  let conflict = new TriageSync({storage:conflictStorage,fetcher:conflictServer.fetcher});
  conflict.stage('job:1','saved','Device draft',snapshot);
  await conflict.refresh();
  assert.equal(conflict.pending['job:1'].note,'Device draft');
  assert.equal(conflict.conflicts['job:1'].note,'Cloud edit');
  assert.equal(conflictServer.records['job:1'].note,'Cloud edit');
  conflict = new TriageSync({storage:conflictStorage,fetcher:conflictServer.fetcher});
  assert.equal(conflict.conflicts['job:1'].version,2);
  await conflict.resolve('job:1',true);
  assert.equal(conflictServer.records['job:1'].note,'Device draft');
  assert.equal(conflictServer.records['job:1'].version,3);
  assert.equal(Object.keys(conflict.conflicts).length,0);

  const keepCloudServer = backend([{uid:'job:1',status:'applied',note:'Server note',version:4,job:snapshot}]);
  const keepCloud = new TriageSync({storage:memory(),fetcher:keepCloudServer.fetcher});
  keepCloud.stage('job:1','saved','Device note',snapshot);
  await keepCloud.refresh();
  keepCloudServer.records['job:1'] = {...keepCloudServer.records['job:1'],note:'Latest server note',version:5};
  await keepCloud.refresh();
  assert.equal(keepCloud.conflicts['job:1'].note,'Latest server note'); // dialog must show current server evidence
  await keepCloud.resolve('job:1',false);
  assert.equal(keepCloud.view()['job:1'].note,'Latest server note');
  assert.equal(Object.keys(keepCloud.pending).length,0);

  // A missing acknowledgement is a recoverable failure, not evidence that the note was saved.
  const noAck = new TriageSync({storage:memory(),fetcher:async(url,opts)=>response(opts.method==='PUT'
    ? {records:[],conflicts:[]} : {records:[]})});
  noAck.stage('job:1','saved','Keep me',snapshot);
  await noAck.refresh();
  assert.equal(noAck.pending['job:1'].note,'Keep me');
  assert.match(noAck.label(),/did not confirm/);

  // The board reconstructs filed jobs from snapshots even after the public feed drops them.
  const source = fs.readFileSync(__dirname+'/../src/jobsweep/web/static/app.js','utf8');
  const ctx = vm.createContext({window:{},localStorage:{getItem:()=>null},Date,URLSearchParams,
    location:{hash:''},setTimeout,clearTimeout});
  vm.runInContext(source.replace('  init();\n})();','  globalThis.boardTest={StaticData,state,defaultFilters,cleanImportedSnapshot};\n})();'),ctx);
  const {StaticData:data,state,defaultFilters,cleanImportedSnapshot} = ctx.boardTest;
  state.mode='static';
  data.triage = {'job:1':{uid:'job:1',status:'applied',note:'Archived note',job:{...snapshot,suitability:'excluded',score:0}}};
  data.applyTriage();
  const archived = data.byUid.get('job:1');
  assert.equal(archived.archived,true);
  assert.equal(archived.active,false);
  assert.equal(archived.note,'Archived note');
  assert.deepEqual(Array.from(data.sorted({...defaultFilters(),tab:'applied'}),item=>item.uid),['job:1']);
  assert.equal(data.snapshot(archived).note,undefined); // private note never becomes a public job field
  const cleaned = cleanImportedSnapshot('import:old',JSON.parse('{"uid":"wrong","title":{"bad":"type"},"lanes":"not an array","groups":["research",5],"qualification_checks":{"bad":"type"},"note":"private","status":"applied","__proto__":{"polluted":true}}'));
  assert.equal(cleaned.uid,'import:old');
  assert.equal(cleaned.title,'import:old');
  assert.equal(cleaned.lanes,undefined);
  assert.deepEqual(Array.from(cleaned.groups),['research']);
  assert.equal(cleaned.qualification_checks,undefined);
  assert.equal(cleaned.note,undefined);
  assert.equal(cleaned.status,undefined);
  assert.equal(cleaned.polluted,undefined);
  assert.equal(cleanImportedSnapshot('import:missing',undefined).title,'import:missing');
  assert.equal(cleanImportedSnapshot('import:long',{description:'x'.repeat(12000)}).description.length,10000);
  const cleanedCheck = cleanImportedSnapshot('import:check',{qualification_checks:[null,'invalid',
    {kind:'language',requirement:'Spanish',level:'required',status:'review',evidence:['bad']}]});
  assert.equal(cleanedCheck.qualification_checks.length,1);
  assert.equal(cleanedCheck.qualification_checks[0].evidence,'');
  console.log('PASS: offline persistence, server acknowledgement, in-flight edits, conflict preservation/resolution, latest conflict evidence, archived job snapshots, safe backup snapshots');
})().catch(error=>{console.error(error);process.exitCode=1;});
