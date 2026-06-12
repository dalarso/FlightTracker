"""Frontend (web/static/js/app.js) behavioral tests for the 'frontend' fix bucket.

app.js is browser JavaScript, not Python, and the repo ships no JS test runner.  These
tests therefore load the REAL app.js into a sandboxed Node VM (node --experimental nothing;
plain `vm` module) with hand-stubbed browser globals (window/document/fetch/EventSource/L/
setTimeout) and then drive the just-fixed functions, asserting on observable effects.

Each fix in the dossier gets a focused case:
  1. _statsReqSeq          — an out-of-order /api/stats response must NOT render.
  2. _searchGen            — a stale "See more" page must NOT append into a new search table.
  3. visibilitychange      — backgrounding the tab must close the log EventSource.
  4. populateForm-on-save  — a clamped numeric (team-id -> 0) must be written back to its input.
  5. syncMapToConfig       — re-opening the map must re-seed zoneRect from current config.
  6. checkInitialStatus    — exhausted retries must NOT reveal toggles; must schedule a re-poll.

The test is skipped (not failed) if `node` is unavailable so it never breaks a Pi-only run.
Like the other suites it imports nothing from the hardware modules and never touches the
repo DB; FT_DATA_DIR is pointed at a temp dir purely to match the suite-wide convention.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP_JS = _ROOT / "web" / "static" / "js" / "app.js"

# Point any incidental data access at a throwaway dir (mirrors the other web/display tests).
os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-fe-test-"))

_NODE = shutil.which("node")


# A Node harness: builds a minimal browser-ish sandbox, loads the real app.js into it via the
# `vm` module, then runs a per-case scenario function passed on argv.  Prints "OK" on success
# or throws (non-zero exit) on the first failed assertion.  Kept deliberately small: it stubs
# only what app.js touches at load time plus what each scenario drives.
_HARNESS = textwrap.dedent(r"""
    'use strict';
    const fs = require('fs');
    const vm = require('vm');
    const APP = process.argv[2];
    const SCENARIO = process.argv[3];

    function assert(cond, msg) { if (!cond) throw new Error('ASSERT FAILED: ' + msg); }

    // ---- microtask helper: app.js uses async/await; flush pending promise jobs ----
    const flush = () => new Promise(res => realSetImmediate(res));
    const realSetImmediate = setImmediate;

    // ---- fake DOM ----------------------------------------------------------------
    function makeEl(id) {
      const children = [];
      const el = {
        id, value: '', textContent: '', innerHTML: '', className: '',
        checked: false, disabled: false, dataset: {},
        style: { _d: {}, display: '',
                 removeProperty(k){ this[k] = ''; this._removed = this._removed||{}; this._removed[k]=true; },
                 setProperty(k,v){ this[k]=v; } },
        classList: { _s: new Set(),
          add(c){ this._s.add(c); }, remove(c){ this._s.delete(c); },
          toggle(c,on){ if(on===undefined){ this._s.has(c)?this._s.delete(c):this._s.add(c); }
                        else { on?this._s.add(c):this._s.delete(c); } return this._s.has(c); },
          contains(c){ return this._s.has(c); } },
        children, firstChild: null,
        appendChild(c){ children.push(c); el.firstChild = children[0]; return c; },
        removeChild(c){ const i=children.indexOf(c); if(i>=0) children.splice(i,1); el.firstChild = children[0]||null; return c; },
        querySelector(){ return null; }, querySelectorAll(){ return []; },
        addEventListener(){}, remove(){}, setLatLng(){}, bindTooltip(){ return el; },
        getAttribute(){ return ''; }, isConnected: true,
      };
      return el;
    }

    const _els = {};
    function getEl(id){ return (_els[id] = _els[id] || makeEl(id)); }

    const documentStub = {
      hidden: false,
      _listeners: {},
      getElementById: getEl,
      querySelector(sel){ return documentStub._qs ? documentStub._qs(sel) : null; },
      querySelectorAll(){ return []; },
      addEventListener(type, fn){ (documentStub._listeners[type] ||= []).push(fn); },
      createElement(){ return makeEl('_new'); },
      createDocumentFragment(){ const f = makeEl('_frag'); return f; },
    };

    // ---- fetch / EventSource / Leaflet stubs (overridden per-scenario) ------------
    let fetchQueue = [];           // FIFO of {resolve} controllers when we need manual ordering
    function jsonResp(obj){ return Promise.resolve({ ok:true, json:()=>Promise.resolve(obj) }); }

    const evtSources = [];
    function EventSourceStub(url){ this.url=url; this.closed=false; this.onmessage=null;
      this.onerror=null; this.close=function(){ this.closed=true; }; evtSources.push(this); }

    const rects = [];   // every L.rectangle created, so the harness can read module-scope zoneRect
    function LRect(initial){ const r = { _bounds:initial||null, setBounds(b){ this._bounds=b; },
      getBounds(){ return { getNorth:()=>0,getSouth:()=>0,getWest:()=>0,getEast:()=>0 }; },
      addTo(){ return r; } }; rects.push(r); return r; }
    const L = {
      map(){ return { setView(){ return this; }, invalidateSize(){}, }; },
      tileLayer(){ return { addTo(){ return this; } }; },
      rectangle(bounds){ return LRect(bounds); },
      circleMarker(){ return { addTo(){ return { bindTooltip(){ return {}; } }; } }; },
      marker(){ const mk = { on(){}, setLatLng(){}, addTo(){ return mk; } }; return mk; },
      divIcon(){ return {}; },
    };

    // ---- sandbox -----------------------------------------------------------------
    const sandbox = {
      window: {}, document: documentStub, console,
      setTimeout: (fn,ms)=>{ sandbox.__timeouts.push({fn,ms}); return sandbox.__timeouts.length; },
      clearTimeout(){}, setInterval(){ return 1; }, clearInterval(){},
      requestAnimationFrame: (fn)=>{ if(fn) fn(); return 1; },
      fetch: (url, init)=>sandbox.__fetch(url, init),
      EventSource: EventSourceStub, L,
      Number, Date, Math, JSON, encodeURIComponent, isNaN, parseInt, parseFloat,
      __timeouts: [], __fetch: ()=>jsonResp({}),
    };
    sandbox.window.fetch = sandbox.fetch;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });

    // expose the listeners the IIFE/module attached
    function fireVisibility(){ (documentStub._listeners.visibilitychange||[]).forEach(fn=>fn()); }

    // =================  SCENARIOS  ================================================
    const scenarios = {

      async statsReqSeq() {
        // loadStatsTab fires first (slow), applyStatsPeriod second (fast).  The fast/newer
        // response resolves first; the slow/older one must be discarded by the seq guard.
        let renderCalls = [];
        sandbox.renderTodayStats = ()=>{};
        sandbox.renderRecentFlights = d => renderCalls.push(d.__tag);
        sandbox.renderPeriodStats   = d => renderCalls.push(d.__tag);
        sandbox.renderFreeApiAccuracy = ()=>{}; sandbox.renderGaAccuracy = ()=>{};
        sandbox.showToast = ()=>{};
        getEl('period-from').value='2026-06-01'; getEl('period-to').value='2026-06-02';

        // Manual-resolution fetch: hand back a controllable promise per call.
        const pending = [];
        sandbox.__fetch = (url)=>{ let resolve; const p = new Promise(r=>resolve=r);
          pending.push({url, resolve}); return p.then(v=>v); };

        const pA = sandbox.loadStatsTab();     // seq 1 (the older request)
        const pB = sandbox.applyStatsPeriod(); // seq 2 (the newer request)
        await flush();
        // loadStatsTab made 3 fetches (stats + 2 accuracy); applyStatsPeriod made 1.
        // Resolve the NEWER one (applyStatsPeriod) FIRST, then the older one.
        const newer = pending[pending.length-1];   // applyStatsPeriod's single fetch
        newer.resolve({ ok:true, json:()=>Promise.resolve({ __tag:'B' }) });
        await flush(); await pB.catch(()=>{});
        // Now resolve every still-pending (older loadStatsTab) fetch.
        pending.forEach(p=>p.resolve({ ok:true, json:()=>Promise.resolve({ __tag:'A', today:null }) }));
        await flush(); await pA.catch(()=>{});

        assert(renderCalls.includes('B'), 'newer request B should have rendered');
        assert(!renderCalls.includes('A'), 'older request A must NOT render after B won, got=' + JSON.stringify(renderCalls));
      },

      async searchGen() {
        // Start a search (gen 1) that pages; capture its See-more handler; start a NEW search
        // (gen 2) that swaps the table; then let the gen-1 page resolve — it must NOT append.
        sandbox.showToast = ()=>{};
        const tbodyA = makeEl('tbodyA');  // table for search A (gets detached)
        const tbodyB = makeEl('tbodyB');  // table for search B
        let currentTbody = tbodyA;
        sandbox.escHtml = s=>s; sandbox._formatTime = ()=>'';
        getEl('stats-search-input').value = 'AAL';
        getEl('stats-search-results');

        // doStatsSearch(A): seed offset, bump _searchGen to 1
        sandbox.__fetch = ()=>jsonResp({ count: 250, sightings: [ {date:'2026-06-01',callsign:'AAL1',time:'10:00'} ] });
        await sandbox.doStatsSearch(); await flush();

        // Begin a See-more for A but DON'T resolve it yet: install a manual fetch.
        let resolveMore;
        sandbox.__fetch = ()=>new Promise(r=>resolveMore=r);
        // Point the table query at A's tbody.
        documentStub._qs = sel => sel === '#search-table tbody' ? currentTbody : null;
        const morePromise = sandbox.loadMoreSearchResults();   // captures gen=1

        // Now a NEW search B happens (Enter): bumps _searchGen to 2, rebuilds the table.
        getEl('stats-search-input').value = 'UAL';
        sandbox.__fetch = ()=>jsonResp({ count: 5, sightings: [ {date:'2026-06-02',callsign:'UAL9',time:'11:00'} ] });
        currentTbody = tbodyB;
        await sandbox.doStatsSearch(); await flush();
        const bRowsBefore = tbodyB.children.length;

        // Finally the stale A page resolves: it must bail on the gen check (no append to B).
        resolveMore({ ok:true, json:()=>Promise.resolve({ count:250, sightings:[ {date:'2026-06-01',callsign:'AAL2',time:'10:05'} ] }) });
        await flush(); await morePromise.catch(()=>{});

        assert(tbodyB.children.length === bRowsBefore,
               'stale gen-1 page must not append rows into gen-2 table B');
      },

      async visibilityClosesLog() {
        // Open the log (creates an EventSource), then hide the tab; the SSE must close.
        sandbox.classifyLine = ()=>''; sandbox.scrollLog = ()=>{};
        sandbox.__fetch = ()=>jsonResp({ lines: [] });
        sandbox.initLog(); await flush();
        assert(evtSources.length === 1, 'initLog should open one EventSource');
        assert(!evtSources[0].closed, 'EventSource open before hide');

        documentStub.hidden = true;
        fireVisibility();
        assert(evtSources[0].closed === true, 'backgrounding the tab must close the log EventSource');
      },

      async populateFormOnSave() {
        // A cleared NHL team id is coerced to 0 by buildConfigPayload; after a successful
        // save the input must be re-rendered with that normalized 0 (not left blank).
        sandbox.showToast = ()=>{}; sandbox.toggleScoreboardBody = ()=>{};
        sandbox.unitToFt = v=>v; sandbox.unitToKm = v=>v; sandbox.ftToUnit = v=>v;
        sandbox.kmToUnit = v=>v;
        // Seed every input buildConfigPayload/populateForm reads.  Defaults are fine; we only
        // assert on the team-id round-trip.
        const setVal = (id,v)=>{ getEl(id).value = v; };
        // valid required numerics so the save gate passes
        ['zone_tl_y','zone_tl_x','zone_br_y','zone_br_x','loc_lat','loc_lon','loc_alt',
         'min_altitude','max_altitude','brightness','night_brightness','gpio_slowdown']
          .forEach(id=>setVal(id, '1'));
        setVal('sb_nhl_team_id', '');   // user cleared it -> parseInt(...)||0 == 0
        // saveConfig sets the module-scope `config` from the normalized payload (built from the
        // inputs above, all valid) before calling populateForm(), so no config seeding is needed.
        sandbox.__fetch = ()=>jsonResp({ ok:true });
        // querySelectorAll for sb priority rows -> empty is fine
        documentStub.querySelectorAll = ()=>[];
        getEl('save-status');
        await sandbox.saveConfig(false); await flush();

        assert(getEl('sb_nhl_team_id').value === 0 || getEl('sb_nhl_team_id').value === '0',
               'cleared team id should be written back as 0, got=' + JSON.stringify(getEl('sb_nhl_team_id').value));
      },

      async syncMapToConfigOnReentry() {
        // First initMap builds the rectangle from config zone A.  Then config changes to zone
        // B (external save).  Re-entering initMap must re-seed zoneRect to B, not keep A.
        // config is a module-scope `let` we can't poke from outside, so we drive it through the
        // real loadConfig() fetch path; zoneRect (also module-scope) is read via the rects[]
        // capture array in the L.rectangle stub.
        sandbox.showToast = ()=>{};
        const cfgA = { LOCATION_HOME:[36,-115,1], ZONE_HOME:{tl_y:36.2,tl_x:-115.3,br_y:36.0,br_x:-115.0} };
        const cfgB = { LOCATION_HOME:[36,-115,1], ZONE_HOME:{tl_y:37.5,tl_x:-116.5,br_y:37.0,br_x:-116.0} };
        // loadConfig() touches many inputs; stub the unit helpers so it doesn't throw.
        sandbox.unitToFt=v=>v; sandbox.unitToKm=v=>v; sandbox.ftToUnit=v=>v; sandbox.kmToUnit=v=>v;
        sandbox.toggleScoreboardBody=()=>{}; documentStub.querySelectorAll=()=>[];
        // populateForm() (called by loadConfig) reorders the scoreboard priority table.
        documentStub._qs = sel => sel === '.sb-table tbody' ? makeEl('sb-tbody') : null;

        sandbox.__fetch = ()=>jsonResp(cfgA);
        await sandbox.loadConfig(); await flush();
        rects.length = 0;          // ignore any rect from before; track only the map's
        sandbox.initMap();         // builds map + zoneRect from zone A
        assert(rects.length === 1 && rects[0]._bounds, 'zoneRect built on first initMap');
        const zoneRect = rects[0];

        // External Config-tab save changes the zone in memory (via loadConfig path here).
        sandbox.__fetch = ()=>jsonResp(cfgB);
        await sandbox.loadConfig(); await flush();
        sandbox.initMap();         // re-entry: must call syncMapToConfig and re-seed bounds
        const b = zoneRect._bounds;       // [[n,w],[s,e]]
        assert(b && b[0][0] === 37.5 && b[1][1] === -116.0,
               're-entry must re-seed zoneRect to the new zone, got=' + JSON.stringify(b));
      },

      async checkInitialStatusExhausted() {
        // Every /api/status fetch fails.  After retries are exhausted the toggles must stay
        // hidden (no removeProperty('display')) and a slow re-poll must be scheduled.
        sandbox.updateServiceBtn=()=>{}; sandbox.updateDisplayBtn=()=>{};
        sandbox.updateNightBtn=()=>{}; sandbox.updateAPIsBtn=()=>{};
        // Make all status fetches fail, then drain the checkInitialStatus() call app.js fires at
        // module load (its default-fetch success would otherwise reveal the toggles and pollute
        // this assertion) before resetting our observation state.
        sandbox.__fetch = ()=>Promise.reject(new Error('down'));
        await flush(); await flush();
        const toggles = ['display-toggle','night-toggle','service-toggle','apis-toggle'];
        toggles.forEach(id=>{ const e=getEl(id); e.style._removed = {}; });

        sandbox.__timeouts.length = 0;
        await sandbox.checkInitialStatus(0);   // 0 retries -> straight to exhausted branch
        await flush();

        toggles.forEach(id=>{
          const rm = getEl(id).style._removed || {};
          assert(!rm.display, id + ' must NOT be revealed on exhausted retry');
        });
        // a re-poll must be scheduled (~10s)
        const repoll = sandbox.__timeouts.find(t=>t.ms === 10000);
        assert(repoll, 'a slow (10s) re-poll must be scheduled after exhausted retries');
      },
    };

    (async () => {
      const fn = scenarios[SCENARIO];
      if (!fn) throw new Error('unknown scenario ' + SCENARIO);
      await fn();
      console.log('OK');
    })().catch(err => { console.error(err.stack || String(err)); process.exit(1); });
""")


def _run_scenario(scenario):
    """Run one Node scenario against the real app.js; return (rc, stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_HARNESS)
        harness_path = f.name
    try:
        proc = subprocess.run(
            [_NODE, harness_path, str(_APP_JS), scenario],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        os.unlink(harness_path)


@unittest.skipUnless(_NODE, "node not available; frontend JS tests skipped")
class FrontendAppJs(unittest.TestCase):
    def _assert_scenario(self, name):
        rc, out, err = _run_scenario(name)
        self.assertEqual(rc, 0, f"{name} failed:\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        self.assertIn("OK", out, f"{name} did not report OK:\n{out}\n{err}")

    def test_stats_request_sequencing(self):
        # Finding 1: an out-of-order /api/stats response must not render stale data.
        self._assert_scenario("statsReqSeq")

    def test_search_pagination_generation_guard(self):
        # Finding 2: a stale 'See more' page must not append into a newer search's table.
        self._assert_scenario("searchGen")

    def test_visibility_closes_log_stream(self):
        # Finding 3: backgrounding the tab must close the log EventSource.
        self._assert_scenario("visibilityClosesLog")

    def test_populate_form_after_save(self):
        # Finding 4: a clamped numeric (cleared team id -> 0) must be written back to the input.
        self._assert_scenario("populateFormOnSave")

    def test_map_reseeds_on_reentry(self):
        # Finding 5: re-opening the map must re-seed zoneRect from the current config.
        self._assert_scenario("syncMapToConfigOnReentry")

    def test_check_initial_status_exhausted_retry(self):
        # Finding 6: exhausted retries must not reveal default-state toggles; must re-poll.
        self._assert_scenario("checkInitialStatusExhausted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
