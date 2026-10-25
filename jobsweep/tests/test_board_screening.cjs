// Run from jobsweep with: node tests/test_board_screening.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.env.JOBSWEEP_BOARD_JS || __dirname + '/../src/jobsweep/web/static/app.js', 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.style = {setProperty() {}}; }
  append(...kids) { this.children.push(...kids); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener() {}
  set textContent(value) { this.children = [String(value)]; }
  get textContent() { return this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
}
const ctx = vm.createContext({window:{},localStorage:{getItem:()=>null},Date,URLSearchParams,location:{hash:''},setTimeout,clearTimeout,
  Node:Element,document:{createElement:tag=>new Element(tag),createTextNode:text=>String(text)}});
vm.runInContext(source.replace('  init();\n})();', '  globalThis.boardTest = {StaticData, state, defaultFilters, hashFor, parseHash, apiParams, entryBadge, screeningBadge, screeningDetails};\n})();'),ctx);
const {StaticData:data,state,defaultFilters,hashFor,parseHash,apiParams,entryBadge,screeningBadge,screeningDetails} = ctx.boardTest;
state.mode = 'static';
const job = (uid, suitability, extra={}) => data.prep({uid,title:uid,score:80,status:'new',active:true,entry_level:true,suitability,...extra});
data.items = [job('nyc','eligible'),job('generic-remote','review'),job('requires-cams','review'),job('foreign','excluded'),job('engineer','excluded'),job('old-feed',undefined)];
let f = defaultFilters();
const ids = () => Array.from(data.sorted(f), item => item.uid);
assert.equal(f.screening,'eligible');
assert.deepEqual(ids(),['nyc']); // Default cannot silently include unverified remote/qualification or unassessed jobs.
f = {...f,screening:'review'};
assert.deepEqual(new Set(ids()),new Set(['generic-remote','requires-cams','old-feed']));
f = {...f,screening:'all'};
assert.equal(ids().length,6);

data.items.push(job('saved-foreign','excluded',{status:'saved',score:0,entry_level:false,active:false}));
f = {...defaultFilters(),tab:'saved'};
assert.deepEqual(ids(),['saved-foreign']); // Reclassification must not hide a filed application.
f = {...defaultFilters(),screening:'review'};
state.f = f;
ctx.location.hash = hashFor();
assert.equal(parseHash().f.screening,'review');
ctx.location.hash = '#/jobs?screening=bad';
assert.equal(parseHash().f.screening,'eligible');
assert.equal(apiParams(f).get('suitability'),'review,not_assessed');
assert.equal(apiParams({...f,tab:'saved'}).has('suitability'),false);

assert.equal(entryBadge({entry_level:true}).textContent,'Early career');
assert.match(entryBadge({entry_level:true}).attributes.title,/Seniority and experience fit only/);
assert.equal(screeningBadge({suitability:'review'}).textContent,'Needs review');
const detail = screeningDetails({qualification_status:'review',location_reasons:['Remote geography unspecified'],role_reasons:[],
  qualification_checks:[{kind:'credential',requirement:'CAMS',level:'required',status:'review',evidence:'CAMS certification is required.'},
    {kind:'clearance',requirement:'Secret',level:'obtainable',status:'info',evidence:'Ability to obtain Secret clearance.'}]}).textContent;
assert.match(detail,/Required: CAMS — Not confirmed in your profile/);
assert.match(detail,/CAMS certification is required\./);
assert.match(detail,/Can obtain after hiring: Secret/);
assert.match(detail,/Location: Remote geography unspecified/);
assert.match(screeningDetails({qualification_status:'no_flags'}).textContent,/does not establish that you meet every requirement/);
console.log('PASS: screening defaults, review queue, unassessed listings, preserved filed jobs, URL/API filters, seniority wording, requirement evidence');
