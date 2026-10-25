// Run from jobsweep with: node tests/test_board_dates.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.env.JOBSWEEP_BOARD_JS || __dirname + '/../src/jobsweep/web/static/app.js', 'utf8');
let now = Date.parse('2026-10-25T21:00:00Z');
class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
const ctx = vm.createContext({window:{},localStorage:{getItem:()=>null},Date:Clock,URLSearchParams,location:{hash:''},setTimeout,clearTimeout});
vm.runInContext(source.replace('  init();\n})();', '  globalThis.boardTest = {StaticData, state, defaultFilters, hashFor, parseHash};\n})();'),ctx);
const {StaticData:data,state,defaultFilters,hashFor,parseHash} = ctx.boardTest;
state.mode='static'; state.generatedAt=now-10*86400000;
const job=(uid,posted_at,score=50,first_seen='2026-10-25T20:59:00Z')=>data.prep({uid,posted_at,score,first_seen,title:uid,status:'new',active:true,suitability:'eligible'});
const fixtures=[job('24h','2026-10-24T21:00:00Z',99),job('1h','2026-10-25T20:00:00Z',90),job('1m','2026-10-25T20:59:00Z',25),job('offset','2026-10-25T16:30:00-04:00'),job('old','2026-09-01'),job('unknown',null),job('invalid','not a date'),job('today','2026-10-25')];
data.items=fixtures;
let f={...defaultFilters(),entry:false,min:0,sort:'posted'};
const ids=()=>Array.from(data.sorted(f),j=>j.uid);
assert.deepEqual(ids().slice(0,3),['1m','offset','1h']);
assert.deepEqual(new Set(ids().slice(-2)),new Set(['unknown','invalid']));
f.postedDays='1';
assert.deepEqual(new Set(ids()),new Set(['1m','offset','1h','24h','today']));
f.postedDays=''; f.days='1'; assert.equal(ids().length,8); // newly imported, not newly posted
fixtures[4].first_seen='2026-10-20T12:00:00Z'; data.prep(fixtures[4]); data.filtered=null;
assert.equal(ids().includes('old'),false); // measured against now, not stale export time
f.tab='saved'; fixtures[0].status='saved'; fixtures[4].status='saved';
assert.deepEqual(ids(),['24h']); // explicit recency also applies to saved jobs
f.tab='all'; f.days=''; f.postedDays='1';
state.f=f; ctx.location.hash=hashFor(); assert.equal(parseHash().f.postedDays,'1');
assert.equal(parseHash().f.sort,'posted');
now+=8*86400000; assert.equal(ids().length,0); // cached results must not freeze the cutoff
console.log('PASS: exact timestamps, time zones, unknown/invalid dates last, posting-vs-import dates, current cutoff, saved tabs, URL round trip, cache expiry');
