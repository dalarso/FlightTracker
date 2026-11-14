/* FlightTracker web UI. Vanilla JS, no build step.
 *
 * This is deliberately a classic script with top-level function declarations: the Node
 * harness in tests/test_frontend_app.py loads it into a VM and stubs / drives these globals
 * by name. Anything that touches a browser-only API (location, history, localStorage,
 * matchMedia, getComputedStyle, ...) is guarded so the file also loads in that sandbox.
 */

// CSRF: tag every same-origin request so the server can reject cross-site drive-by POSTs.
// (The server requires this header on all state-changing requests; see _csrf_guard.)
(function () {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    init.headers = Object.assign({}, init.headers, { 'X-Requested-With': 'FlightTracker' });
    return _origFetch(input, init);
  };
})();

// ── Constants ──────────────────────────────────────────────────────────────────────────
const FT_TO_M  = 0.3048;
const FT_TO_KM = 0.0003048;
const KM_TO_FT = 3280.84;
const KM_TO_M  = 1000;
const TABS = ['stats', 'map', 'apis', 'rules', 'config', 'log'];   // the first is home
const HOME_TAB = TABS[0];
const TAB_TITLES = { stats: 'Stats', map: 'Map', apis: 'APIs', rules: 'Rules', config: 'Settings', log: 'Log' };
const LOG_MAX_LINES = 2000;

// ── State ──────────────────────────────────────────────────────────────────────────────
let config = {};
let altUnit = 'ft';
let displayOn = true;
let serviceRunning = true;
let apisEnabled = true;
let nightActive = false;
let map, zoneRect, homeMarker, cornerMarkers = [];
let zoneBounds = {};
let evtSource = null;
let _activeTab = HOME_TAB;
const _tabScroll = {};

// ── Small helpers ──────────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function icon(name, cls) {
  return `<svg class="icon${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function _num(v, dflt = 0) { const n = Number(v); return Number.isFinite(n) ? n : dflt; }
function _fmtInt(v) { return _num(v).toLocaleString(); }
function _plural(n, one, many) { return `${_fmtInt(n)} ${_num(n) === 1 ? one : (many || one + 's')}`; }

function _storeGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
function _storeSet(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* private mode */ } }

function _media(query) {
  try { return !!(window.matchMedia && window.matchMedia(query).matches); } catch (e) { return false; }
}
function _cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

// JSON fetch: throws on network errors and non-2xx (with the server's error text if any).
async function _json(url, init) {
  const r = await fetch(url, init);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const err = new Error((data && data.error) || `HTTP ${r.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

function _post(url, body, method = 'POST') {
  const init = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  return _json(url, init);
}

function _confirm(msg) {
  return typeof confirm === 'function' ? confirm(msg) : true;
}

// Group all N-number registrations (private/GA) into one combined row.
function _groupNReg(items) {
  const nItems = (items || []).filter(i => /^N\d/.test(i.prefix));
  const rest   = (items || []).filter(i => !/^N\d/.test(i.prefix));
  if (!nItems.length) return rest;
  const nTotal = nItems.reduce((sum, i) => sum + _num(i.count), 0);
  const nEntry = { prefix: 'N-reg', name: 'Private / GA', count: nTotal };
  const idx    = rest.findIndex(i => _num(i.count) < nTotal);
  return idx === -1 ? [...rest, nEntry] : [...rest.slice(0, idx), nEntry, ...rest.slice(idx)];
}

// ── Theme ──────────────────────────────────────────────────────────────────────────────
function _effectiveTheme() {
  const root = document.documentElement;
  const set = root && root.dataset ? root.dataset.theme : '';
  if (set === 'light' || set === 'dark') return set;
  return _media('(prefers-color-scheme: light)') ? 'light' : 'dark';
}

function toggleTheme() {
  const next = _effectiveTheme() === 'dark' ? 'light' : 'dark';
  if (document.documentElement) document.documentElement.dataset.theme = next;
  _storeSet('ft-theme', next);
  _syncThemeUi();
  // Map handles and the zone take their colors from the theme tokens.
  if (zoneRect && zoneRect.setStyle) {
    const accent = _cssVar('--accent', '#4c9bff');
    zoneRect.setStyle({ color: accent, fillColor: accent });
  }
}

function _syncThemeUi() {
  const t = _effectiveTheme();
  const btn = $('theme-btn');
  if (btn) {
    const label = t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    btn.setAttribute('aria-label', label);
    btn.title = label;
  }
  const root = document.documentElement;
  if (root && root.dataset && root.dataset.theme) {
    document.querySelectorAll('meta[name="theme-color"]').forEach(m =>
      m.setAttribute('content', t === 'light' ? '#f5f7fa' : '#0b0f14'));
  }
}

// ── Tabs ───────────────────────────────────────────────────────────────────────────────
function showTab(name) {
  if (!TABS.includes(name)) name = HOME_TAB;
  if (name !== _activeTab && typeof window.scrollY === 'number') _tabScroll[_activeTab] = window.scrollY;
  _activeTab = name;
  toggleControls(false);
  document.querySelectorAll('.panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
  document.querySelectorAll('.tab[data-tab]').forEach(b => {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  document.title = name === HOME_TAB ? 'FlightTracker' : `${TAB_TITLES[name]} \u00b7 FlightTracker`;
  _syncHash(name);
  if (typeof window.scrollTo === 'function') window.scrollTo(0, _tabScroll[name] || 0);

  if (name === 'map')   initMap();
  if (name === 'log')   initLog();     else stopLog();
  if (name === 'apis')  initAPIsTab(); else stopAPIsTab();
  if (name === 'stats') { loadStatsTab({ quiet: _statsLoaded }); _startStatsRefresh(); }
  else _stopStatsRefresh();
  if (name === 'rules') initRulesTab();
}

function goToTab(name) { showTab(name); }

// The active tab lives in the URL hash so a reload (or a bookmark) lands on it.
function _syncHash(name) {
  if (typeof location === 'undefined' || typeof history === 'undefined') return;
  const want = name === HOME_TAB ? '' : '#' + name;
  if ((location.hash || '') === want) return;
  try { history.replaceState(null, '', want || location.pathname + location.search); } catch (e) { /* ignore */ }
}

function _tabFromHash() {
  if (typeof location === 'undefined') return HOME_TAB;
  const h = (location.hash || '').replace('#', '');
  return TABS.includes(h) ? h : HOME_TAB;
}

// ── Display controls (header switches) ─────────────────────────────────────────────────
const _SWITCH_IDS = ['display-toggle', 'night-toggle', 'service-toggle', 'apis-toggle'];
let _controlsReady = false;
let _statusTimer = null;

function toggleControls(force) {
  const box = $('device-controls');
  const btn = $('controls-btn');
  if (!box || !btn) return;
  const open = typeof force === 'boolean' ? force : !box.classList.contains('is-open');
  box.classList.toggle('is-open', open);
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function _setConn(state, detail) {
  const el = $('conn-status');
  if (!el) return;
  const labels = { pending: 'Connecting', ok: 'Connected', warn: 'Check display', stopped: 'Service stopped', offline: 'Offline' };
  el.dataset.state = state;
  const lbl = el.querySelector ? el.querySelector('.conn-label') : null;
  if (lbl) lbl.textContent = labels[state] || state;
  el.title = detail || labels[state] || '';
}

function _scheduleStatus(ms, retries = 3) {
  clearTimeout(_statusTimer);
  _statusTimer = setTimeout(() => { _statusTimer = null; checkInitialStatus(retries); }, ms);
}

// Reads every header-switch state from /api/status.  Until that succeeds the switches stay
// disabled: showing them in their HTML default state would assert something unconfirmed.
async function checkInitialStatus(retries = 3) {
  let d;
  try {
    d = await _json('/api/status');
  } catch (e) {
    if (retries > 0) {
      // The server may be mid-restart; retry a few times before calling it offline.
      _scheduleStatus(1500, retries - 1);
      return;
    }
    _setControlsReady(false);
    _setConn('offline', "Can't reach the Pi. Retrying every 10 seconds.");
    _scheduleStatus(10000, 3);
    return;
  }
  d = d || {};
  updateServiceBtn(d.running);
  updateDisplayBtn(!d.paused);
  updateNightBtn(d.night);
  updateAPIsBtn(!d.apis_disabled);
  _setControlsReady(true);
  _checkHealth(d.running);
  if (!document.hidden) _scheduleStatus(30000, 1);
}

async function _checkHealth(running) {
  if (!running) { _setConn('stopped', 'The FlightTracker service is stopped'); return; }
  try {
    const h = await _json('/api/health');
    if (h && h.ok === false) {
      const problem = h.display && h.display.problem;
      _setConn('warn', problem ? `Display self-check: ${problem}` : (h.error || 'Flight polling has fallen behind'));
    } else {
      _setConn('ok', 'Connected to the Pi');
    }
  } catch (e) {
    _setConn('ok', 'Connected to the Pi');
  }
}

async function checkDisplayStatus() {
  try {
    const d = await _json('/api/display');
    updateDisplayBtn(!d.paused);
  } catch (e) { /* the periodic status poll will catch up */ }
}

function _setSwitch(el, on) { if (el) el.setAttribute('aria-checked', on ? 'true' : 'false'); }

function _setControlsReady(ready) {
  _controlsReady = !!ready;
  const box = $('device-controls');
  if (box && box.dataset) box.dataset.ready = _controlsReady ? 'true' : 'false';
  _syncSwitchDisabled();
  _updateControlsSummary();
}

function _syncSwitchDisabled() {
  _SWITCH_IDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    const busy = el.dataset && el.dataset.busy === '1';
    // Display / Night / APIs only mean something while the service runs.
    el.disabled = !_controlsReady || busy || (id !== 'service-toggle' && !serviceRunning);
  });
}

function _updateControlsSummary() {
  const btn = $('controls-btn');
  if (!btn || !btn.dataset) return;
  let s = 'unknown';
  if (_controlsReady) {
    if (!serviceRunning || !displayOn) s = 'off';
    else if (nightActive) s = 'night';
    else if (!apisEnabled) s = 'warn';
    else s = 'ok';
  }
  btn.dataset.state = s;
}

function updateDisplayBtn(on) {
  displayOn = !!on;
  _setSwitch($('display-toggle'), displayOn);
  const lbl = $('display-label');
  if (lbl) lbl.textContent = displayOn ? 'On' : 'Off';
  _updateControlsSummary();
}

function updateServiceBtn(running) {
  serviceRunning = !!running;
  _setSwitch($('service-toggle'), serviceRunning);
  const lbl = $('service-label');
  if (lbl) lbl.textContent = serviceRunning ? 'Running' : 'Stopped';
  _syncSwitchDisabled();
  _updateControlsSummary();
}

function updateAPIsBtn(enabled) {
  apisEnabled = !!enabled;
  _setSwitch($('apis-toggle'), apisEnabled);
  const lbl = $('apis-label');
  if (lbl) lbl.textContent = apisEnabled ? 'On' : 'Off';
  _updateControlsSummary();
}

function updateNightBtn(active) {
  nightActive = !!active;
  _setSwitch($('night-toggle'), nightActive);
  const lbl = $('night-label');
  if (lbl) lbl.textContent = nightActive ? 'On' : 'Off';
  _updateControlsSummary();
}

// One request per switch at a time: a second tap (or Enter) while the first request is in
// flight is ignored instead of flipping the switch back.
async function _runSwitch(id, fn) {
  const el = $(id);
  if (!el || (el.dataset && el.dataset.busy === '1')) return;
  el.dataset.busy = '1';
  el.classList.add('is-busy');
  _syncSwitchDisabled();
  try {
    await fn();
  } finally {
    el.dataset.busy = '';
    el.classList.remove('is-busy');
    _syncSwitchDisabled();
  }
}

async function toggleDisplay() {
  return _runSwitch('display-toggle', async () => {
    try {
      const d = await _post(displayOn ? '/api/display/off' : '/api/display/on');
      if (d && d.ok) updateDisplayBtn(!displayOn);
      else showToast((d && d.error) || 'Could not change the display', 'err');
    } catch (e) { showToast(e.message || 'Request failed', 'err'); }
  });
}

async function toggleService() {
  const stopping = serviceRunning;
  if (stopping && !_confirm('Stop the FlightTracker service? The display goes dark and all polling stops until you start it again.')) return;
  return _runSwitch('service-toggle', async () => {
    try {
      const d = await _post(stopping ? '/api/service/stop' : '/api/service/start');
      if (d && d.ok) {
        updateServiceBtn(!stopping);
        if (stopping) updateDisplayBtn(false);   // a stopped service means a dark display
        else await checkDisplayStatus();
        // systemctl needs a moment before it reports the new state; re-sync everything.
        _scheduleStatus(1500, 3);
      } else {
        showToast((d && d.error) || 'Service error', 'err');
      }
    } catch (e) { showToast(e.message || 'Request failed', 'err'); }
  });
}

async function toggleAPIs() {
  return _runSwitch('apis-toggle', async () => {
    try {
      const d = await _post('/api/apis/toggle');
      if (d && d.ok) updateAPIsBtn(!d.apis_disabled);
      else showToast((d && d.error) || 'Could not change the API setting', 'err');
    } catch (e) { showToast(e.message || 'Request failed', 'err'); }
  });
}

async function toggleNight() {
  return _runSwitch('night-toggle', async () => {
    try {
      const d = await _post('/api/display/night');
      if (d && d.ok) {
        updateNightBtn(d.night);
        _scheduleStatus(1500, 3);
      } else {
        showToast((d && d.error) || 'Could not change night mode', 'err');
      }
    } catch (e) { showToast(e.message || 'Request failed', 'err'); }
  });
}

// ── Scoreboard: live game + hide control ───────────────────────────────────────────────
let _sbGame = null;         // last-seen active game dict (or null)
let _sbDismissedId = null;  // game_id currently hidden (or null)
let _sbTimer = null;

function _sbStatusLabel(g) {
  const st = g.state;
  if (st === 'FINAL' || st === 'OFF') {
    if ((g.period_label || '').startsWith('F/')) return g.period_label;       // MLB extra innings
    const pt = g.period_type || 'REG';
    if (pt === 'OT' || pt === 'SO') return 'Final ' + (g.period_label || pt); // OT/SO final
    return 'Final';
  }
  // Live: period + game clock, like the LED board ("2nd 14:32"); no clock in an
  // intermission / period break or when it's stopped at zero.
  const label = g.period_label || 'Live';
  let clock = String(g.time_remaining || '').trim();
  if (/^0\d:\d\d$/.test(clock)) clock = clock.slice(1);
  if (!clock || g.in_intermission || clock.replace(/[0:.]/g, '') === '') return label;
  return `${label} ${clock}`;
}

function renderSbLive(d) {
  const field = $('sb-live-field');
  if (!field) return;
  const g = d && d.enabled ? d.game : null;
  // on_board (server-side) applies the board's post-game window, so a final drops off here
  // when it drops off the LED board; older servers don't send it, so fall back to the state.
  const active = !!g && (typeof d.on_board === 'boolean'
    ? d.on_board
    : ['LIVE', 'CRIT', 'FINAL', 'OFF'].includes(g.state));
  if (!active) { field.hidden = true; _sbGame = null; return; }
  _sbGame = g;
  const gid = g.game_id != null ? String(g.game_id) : '';
  const hidden = !!(_sbDismissedId && gid && _sbDismissedId === gid);
  field.hidden = false;
  field.dataset.hidden = hidden ? '1' : '';
  // Standard orientation, matching the LED board: AWAY first, HOME second
  // ("UTA 3–5 VGK" when VGK is at home, "VGK 5–3 UTA" on the road).
  const team = { abbr: d.team_name || '', score: g.team_score ?? 0 };
  const opp  = { abbr: g.opp_abbr || '?',  score: g.opp_score ?? 0 };
  const [away, home] = g.team_home ? [opp, team] : [team, opp];
  $('sb-live-text').textContent =
    `${away.abbr} ${away.score}\u2013${home.score} ${home.abbr} \u00b7 ${_sbStatusLabel(g)}`;
  $('sb-live-dot').classList.toggle('off', hidden);
  const btn = $('sb-hide-btn');
  btn.textContent = hidden ? 'Show this game' : 'Hide this game';
  btn.classList.toggle('btn-primary', hidden);
  btn.classList.toggle('btn-secondary', !hidden);
}

async function pollScoreboard() {
  try {
    const d = await _json('/api/scoreboard');
    _sbDismissedId = d && d.dismissed_game_id != null ? String(d.dismissed_game_id) : null;
    renderSbLive(d);
  } catch (e) { /* keep the last state on a transient error */ }
}

async function toggleHideGame() {
  const field = $('sb-live-field');
  const btn = $('sb-hide-btn');
  const hidden = field.dataset.hidden === '1';
  btn.disabled = true;
  try {
    if (hidden) {
      const dd = await _post('/api/scoreboard/show');
      if (dd && dd.ok) showToast('Scoreboard restored');
    } else {
      const gid = _sbGame && _sbGame.game_id != null ? String(_sbGame.game_id) : '';
      const dd = await _post('/api/scoreboard/dismiss', { game_id: gid });
      if (dd && dd.ok) showToast('Game hidden. The board shows the idle screen.');
      else showToast((dd && dd.error) || 'Could not hide the game', 'err');
    }
  } catch (e) { showToast(e.message || 'Request failed', 'err'); }
  await pollScoreboard();
  btn.disabled = false;
}

function startScoreboardPoll() {
  if (_sbTimer) return;
  pollScoreboard();
  _sbTimer = setInterval(pollScoreboard, 20000);
}

function stopScoreboardPoll() {
  if (_sbTimer) { clearInterval(_sbTimer); _sbTimer = null; }
}

// ── Unit conversion ────────────────────────────────────────────────────────────────────
// Enough decimals that a whole-foot altitude survives ft -> m/km -> ft unchanged (integer
// metres turned 100 ft into 98 ft on the way back).
function ftToUnit(ft, unit) {
  if (unit === 'm')  return +(ft * FT_TO_M).toFixed(1);
  if (unit === 'km') return +(ft * FT_TO_KM).toFixed(4);
  return ft;
}
function unitToFt(val, unit) {
  if (unit === 'm')  return Math.round(val / FT_TO_M);
  if (unit === 'km') return Math.round(val / FT_TO_KM);
  return Math.round(val);
}
function kmToUnit(km, unit) {
  if (unit === 'ft') return +(km * KM_TO_FT).toFixed(1);
  if (unit === 'm')  return +(km * KM_TO_M).toFixed(1);
  return +Number(km).toFixed(5);
}
function unitToKm(val, unit) {
  if (unit === 'ft') return val / KM_TO_FT;
  if (unit === 'm')  return val / KM_TO_M;
  return parseFloat(val);
}

function setAltUnit(unit) {
  if (!['ft', 'm', 'km'].includes(unit) || unit === altUnit) { _applyAltUnitUI(); return; }
  // Convert only fields that hold a number: a blank field used to come back as 0, which
  // would then have been saved as a 0 ft altitude.
  const conv = (id, toBase, fromBase) => {
    const el = $(id);
    const v = parseFloat(el.value);
    if (!isNaN(v)) el.value = fromBase(toBase(v, altUnit), unit);
  };
  const wasClean = !_configDirty;
  conv('min_altitude', unitToFt, ftToUnit);
  conv('max_altitude', unitToFt, ftToUnit);
  conv('loc_alt', unitToKm, kmToUnit);
  altUnit = unit;
  _storeSet('ft-alt-unit', unit);
  _applyAltUnitUI();
  // Switching units isn't an edit: keep a clean form clean.
  if (wasClean) _cleanSnapshot = _payloadKey();
}

function _applyAltUnitUI() {
  const lbl = $('loc-alt-label');
  if (lbl) lbl.textContent = `Antenna altitude (${altUnit} ASL)`;
  document.querySelectorAll('.unit-label').forEach(el => { el.textContent = altUnit; });
  document.querySelectorAll('.seg[data-unit]').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.unit === altUnit ? 'true' : 'false'));
}

function _restoreAltUnit() {
  const u = _storeGet('ft-alt-unit');
  if (u === 'm' || u === 'km') { altUnit = u; _applyAltUnitUI(); }
}

// ── Toast ──────────────────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, type = 'ok') {
  const t = $('toast');
  if (!t) return;
  const ic = type === 'err' ? 'alert' : type === 'info' ? 'info' : 'check';
  t.innerHTML = `${icon(ic)}<span></span>`;
  const span = t.querySelector ? t.querySelector('span') : null;
  if (span) span.textContent = msg; else t.textContent = msg;
  t.className = 'toast ' + type;
  clearTimeout(toastTimer);
  requestAnimationFrame(() => t.classList.add('show'));
  toastTimer = setTimeout(() => t.classList.remove('show'), type === 'err' ? 5000 : 2600);
}

// ── Secrets (eye toggle) ───────────────────────────────────────────────────────────────
// Password-field id -> the config key it holds (for on-demand secret reveal).
const _SECRET_FIELDS = {
  ow_key: 'OPENWEATHER_API_KEY', airlabs_key: 'AIRLABS_API_KEY',
  airlabs_key2: 'AIRLABS_API_KEY_2', fa_key: 'FLIGHTAWARE_API_KEY',
  opensky_secret: 'OPENSKY_CLIENT_SECRET',
};
const _SECRET_SENTINEL = '********';

async function toggleEye(id, btn) {
  const inp = $(id);
  if (!inp) return;
  const revealing = inp.type === 'password';
  // Secrets are masked by default and not shipped in the page; fetch the real value on
  // demand the first time the eye is opened while the field still holds the mask.
  if (revealing && _SECRET_FIELDS[id] && inp.value === _SECRET_SENTINEL) {
    try {
      const d = await _post('/api/config/reveal', { key: _SECRET_FIELDS[id] });
      if (d && typeof d.value === 'string') {
        const wasClean = !_configDirty;
        inp.value = d.value;
        if (wasClean) _cleanSnapshot = _payloadKey();   // revealing isn't an edit
      }
    } catch (e) {
      showToast('Could not reveal the value', 'err');
      return;
    }
  }
  inp.type = revealing ? 'text' : 'password';
  if (btn) {
    btn.setAttribute('aria-pressed', revealing ? 'true' : 'false');
    btn.setAttribute('aria-label', revealing ? 'Hide value' : 'Show value');
    const use = btn.querySelector ? btn.querySelector('use') : null;
    if (use) use.setAttribute('href', revealing ? '#i-eye-off' : '#i-eye');
  }
}

// ── Settings: load / populate / save ───────────────────────────────────────────────────
let _configDirty = false;
let _cleanSnapshot = null;   // the form as last loaded/saved; "dirty" means it now differs
let _saving = false;
let _saveMsgTimer = null;

function _payloadKey() {
  try { return JSON.stringify(buildConfigPayload()); } catch (e) { return null; }
}

// Dirty means "different from what's saved", so flipping a switch and flipping it back
// (or dragging a league away and back) is clean again.
function _refreshDirty() {
  if (_cleanSnapshot === null) return;
  _setConfigDirty(_payloadKey() !== _cleanSnapshot);
}

async function loadConfig() {
  const banner = $('config-error');
  try {
    const data = await _json('/api/config');
    if (!data || !data.ZONE_HOME || !Array.isArray(data.LOCATION_HOME)) throw new Error('Unexpected config shape');
    config = data;
  } catch (e) {
    if (banner) banner.hidden = false;
    showToast('Failed to load settings: ' + e.message, 'err');
    return;
  }
  if (banner) banner.hidden = true;
  populateForm();
}

// Mirror the in-memory `config` into every Settings input.  Called after the initial load
// AND after a successful save, so the form always shows the values that were actually
// persisted (fields buildConfigPayload clamped or defaulted never silently differ).
function populateForm() {
  $('zone_tl_y').value = config.ZONE_HOME.tl_y;
  $('zone_tl_x').value = config.ZONE_HOME.tl_x;
  $('zone_br_y').value = config.ZONE_HOME.br_y;
  $('zone_br_x').value = config.ZONE_HOME.br_x;
  $('loc_lat').value = config.LOCATION_HOME[0];
  $('loc_lon').value = config.LOCATION_HOME[1];
  $('loc_alt').value = kmToUnit(config.LOCATION_HOME[2], altUnit);
  $('timezone').value = config.TIMEZONE || 'America/Los_Angeles';
  $('min_altitude').value = ftToUnit(config.MIN_ALTITUDE, altUnit);
  $('max_altitude').value = ftToUnit(config.MAX_ALTITUDE, altUnit);
  $('local_airports').value = config.LOCAL_AIRPORTS || config.LOCAL_AIRPORT || '';
  $('brightness').value = config.BRIGHTNESS;
  $('night_brightness').value = config.NIGHT_BRIGHTNESS ?? 20;
  $('gpio_slowdown').value = config.GPIO_SLOWDOWN;
  $('journey_code').value = config.JOURNEY_CODE_SELECTED || '';
  $('journey_blank').value = config.JOURNEY_BLANK_FILLER || '';
  $('hat_pwm').checked = !!config.HAT_PWM_ENABLED;
  $('loading_led_enabled').checked = !!config.LOADING_LED_ENABLED;
  $('loading_led_gpio_pin').value = config.LOADING_LED_GPIO_PIN ?? 25;
  $('weather_location').value = config.WEATHER_LOCATION || '';
  $('temp_units').value = config.TEMPERATURE_UNITS || 'imperial';
  $('ow_key').value = config.OPENWEATHER_API_KEY || '';
  $('rainfall_enabled').checked = !!config.RAINFALL_ENABLED;
  // Scoreboard master switch: off by default for new installs.
  $('scoreboard_enabled').checked = config.SCOREBOARD_ENABLED !== undefined ? !!config.SCOREBOARD_ENABLED : false;
  toggleScoreboardBody();
  // Per-sport settings.  Backward-compat: the old single-team keys map to NHL.
  const sbNhlEnabled = config.SCOREBOARD_NHL_ENABLED !== undefined ? config.SCOREBOARD_NHL_ENABLED
                     : (config.SCOREBOARD_ENABLED !== undefined ? config.SCOREBOARD_ENABLED : true);
  $('sb_nhl_enabled').checked = !!sbNhlEnabled;
  $('sb_nhl_team_id').value   = config.SCOREBOARD_NHL_TEAM_ID ?? config.SCOREBOARD_TEAM_ID ?? '';
  $('sb_nhl_team_name').value = (config.SCOREBOARD_NHL_TEAM_NAME ?? config.SCOREBOARD_TEAM_NAME ?? '').toString().toUpperCase();
  ['nfl', 'mlb', 'nba', 'wnba', 'mls', 'fifa'].forEach(k => {
    const K = k.toUpperCase();
    $(`sb_${k}_enabled`).checked = !!config[`SCOREBOARD_${K}_ENABLED`];
    $(`sb_${k}_team_id`).value   = config[`SCOREBOARD_${K}_TEAM_ID`] ?? '';
    $(`sb_${k}_team_name`).value = String(config[`SCOREBOARD_${K}_TEAM_NAME`] || '').toUpperCase();
  });
  // Reorder the league rows to match the saved priority.
  const priority = Array.isArray(config.SCOREBOARD_PRIORITY)
    ? config.SCOREBOARD_PRIORITY
    : ['NHL', 'NFL', 'MLB', 'NBA', 'WNBA', 'MLS', 'FIFA'];
  const tbody = document.querySelector('.sb-table tbody');
  if (tbody) {
    priority.forEach(league => {
      const row = tbody.querySelector(`tr[data-league="${league}"]`);
      if (row) tbody.appendChild(row);
    });
  }
  _syncSbMoveButtons();
  $('scoreboard_post_game_minutes').value = config.SCOREBOARD_POST_GAME_MINUTES ?? 30;
  $('scoreboard_goal_celebration_seconds').value = config.SCOREBOARD_GOAL_CELEBRATION_SECONDS ?? 30;
  $('scoreboard_win_celebration_seconds').value = config.SCOREBOARD_WIN_CELEBRATION_SECONDS ?? 180;
  $('receiver_host').value = config.RECEIVER_HOST || '';
  $('receiver_type').value = config.RECEIVER_TYPE || 'dump1090';
  $('poll_interval').value = config.POLL_INTERVAL ?? 15;
  $('data_check_interval').value = config.DATA_CHECK_INTERVAL ?? 2;
  $('date_format').value = config.DATE_FORMAT || 'MDY';
  $('airlabs_key').value  = config.AIRLABS_API_KEY   || '';
  $('airlabs_key2').value = config.AIRLABS_API_KEY_2 || '';
  $('fa_key').value = config.FLIGHTAWARE_API_KEY || '';
  $('opensky_id').value = config.OPENSKY_CLIENT_ID || '';
  $('opensky_secret').value = config.OPENSKY_CLIENT_SECRET || '';
  $('feeder_monthly_credit').value = config.FEEDER_MONTHLY_CREDIT ?? 10.00;
  $('airlabs_monthly_limit').value  = config.AIRLABS_MONTHLY_LIMIT  ?? 1000;
  $('airlabs_reset_day').value      = config.AIRLABS_RESET_DAY      ?? 9;
  $('airlabs2_monthly_limit').value = config.AIRLABS2_MONTHLY_LIMIT ?? 1000;
  $('airlabs2_reset_day').value     = config.AIRLABS2_RESET_DAY     ?? 9;
  $('aeroapi_reset_day').value = config.AEROAPI_RESET_DAY ?? 1;
  $('adsbdb_cache_ttl').value = config.ADSBDB_CACHE_TTL ?? 3600;
  $('opensky_cache_ttl').value = config.OPENSKY_CACHE_TTL ?? 3600;
  $('route_ttl_scheduled').value = config.ROUTE_TTL_SCHEDULED ?? 604800;
  $('route_ttl_default').value = config.ROUTE_TTL_DEFAULT ?? 3600;
  $('route_miss_ttl').value = config.ROUTE_MISS_TTL ?? 300;
  $('route_paid_miss_ttl').value = config.ROUTE_PAID_MISS_TTL ?? 7200;
  _cleanSnapshot = _payloadKey();
  _setConfigDirty(false);
}

function buildConfigPayload() {
  const v   = id => $(id).value;
  const int = id => parseInt($(id).value);
  // `|| default` would turn a deliberate 0 into the default; these keep 0 when 0 is valid.
  const intMin = (id, min, dflt) => (n => isNaN(n) || n < min ? dflt : n)(int(id));
  const sport = k => ({
    [`SCOREBOARD_${k.toUpperCase()}_ENABLED`]:   $(`sb_${k}_enabled`).checked,
    [`SCOREBOARD_${k.toUpperCase()}_TEAM_ID`]:   int(`sb_${k}_team_id`) || 0,
    [`SCOREBOARD_${k.toUpperCase()}_TEAM_NAME`]: (v(`sb_${k}_team_name`) || '').toUpperCase().slice(0, 4),
  });
  return {
    ZONE_HOME: {
      tl_y: parseFloat(v('zone_tl_y')),
      tl_x: parseFloat(v('zone_tl_x')),
      br_y: parseFloat(v('zone_br_y')),
      br_x: parseFloat(v('zone_br_x')),
    },
    LOCATION_HOME: [
      parseFloat(v('loc_lat')),
      parseFloat(v('loc_lon')),
      unitToKm(parseFloat(v('loc_alt')), altUnit),
    ],
    MIN_ALTITUDE: unitToFt(parseFloat(v('min_altitude')), altUnit),
    MAX_ALTITUDE: unitToFt(parseFloat(v('max_altitude')), altUnit),
    LOCAL_AIRPORTS: v('local_airports').split(',').map(s => s.trim().toUpperCase()).filter(Boolean).join(','),
    BRIGHTNESS: int('brightness'),
    NIGHT_BRIGHTNESS: int('night_brightness'),
    GPIO_SLOWDOWN: int('gpio_slowdown'),
    JOURNEY_CODE_SELECTED: v('journey_code'),
    JOURNEY_BLANK_FILLER: v('journey_blank'),
    HAT_PWM_ENABLED: $('hat_pwm').checked,
    LOADING_LED_ENABLED: $('loading_led_enabled').checked,
    LOADING_LED_GPIO_PIN: int('loading_led_gpio_pin') || 25,
    WEATHER_LOCATION: v('weather_location'),
    TEMPERATURE_UNITS: v('temp_units'),
    OPENWEATHER_API_KEY: v('ow_key'),
    RAINFALL_ENABLED: $('rainfall_enabled').checked,
    SCOREBOARD_ENABLED: $('scoreboard_enabled').checked,
    // Priority comes from the current row order (drag or arrow buttons).
    SCOREBOARD_PRIORITY: [...document.querySelectorAll('.sb-table tbody tr[data-league]')].map(r => r.dataset.league),
    ...sport('nhl'), ...sport('nfl'), ...sport('mlb'), ...sport('nba'),
    ...sport('wnba'), ...sport('mls'), ...sport('fifa'),
    SCOREBOARD_POST_GAME_MINUTES: intMin('scoreboard_post_game_minutes', 0, 30),
    // 0 is meaningful for both celebrations (= off), so only an empty/invalid field falls back.
    SCOREBOARD_GOAL_CELEBRATION_SECONDS: intMin('scoreboard_goal_celebration_seconds', 0, 30),
    SCOREBOARD_WIN_CELEBRATION_SECONDS:  intMin('scoreboard_win_celebration_seconds', 0, 180),
    TIMEZONE: v('timezone').trim(),
    RECEIVER_HOST: v('receiver_host').trim(),
    RECEIVER_TYPE: v('receiver_type'),
    POLL_INTERVAL: int('poll_interval') || 15,
    DATA_CHECK_INTERVAL: int('data_check_interval') || 2,
    DATE_FORMAT: v('date_format'),
    AIRLABS_API_KEY:   v('airlabs_key'),
    AIRLABS_API_KEY_2: v('airlabs_key2'),
    FLIGHTAWARE_API_KEY: v('fa_key'),
    OPENSKY_CLIENT_ID: v('opensky_id'),
    OPENSKY_CLIENT_SECRET: v('opensky_secret'),
    FEEDER_MONTHLY_CREDIT: (n => isNaN(n) ? 10.00 : n)(parseFloat(v('feeder_monthly_credit'))),
    AIRLABS_MONTHLY_LIMIT:  int('airlabs_monthly_limit')  || 1000,
    AIRLABS_RESET_DAY:      int('airlabs_reset_day')      || 9,
    AIRLABS2_MONTHLY_LIMIT: int('airlabs2_monthly_limit') || 1000,
    AIRLABS2_RESET_DAY:     int('airlabs2_reset_day')     || 9,
    AEROAPI_RESET_DAY: int('aeroapi_reset_day') || 1,
    ADSBDB_CACHE_TTL:    intMin('adsbdb_cache_ttl', 60, 3600),
    OPENSKY_CACHE_TTL:   intMin('opensky_cache_ttl', 60, 3600),
    ROUTE_TTL_SCHEDULED: intMin('route_ttl_scheduled', 60, 604800),
    ROUTE_TTL_DEFAULT:   intMin('route_ttl_default', 60, 3600),
    ROUTE_MISS_TTL:      intMin('route_miss_ttl', 30, 300),
    ROUTE_PAID_MISS_TTL: intMin('route_paid_miss_ttl', 60, 7200),
  };
}

const _REQUIRED_NUMBERS = [
  ['zone_tl_y', p => p.ZONE_HOME.tl_y], ['zone_tl_x', p => p.ZONE_HOME.tl_x],
  ['zone_br_y', p => p.ZONE_HOME.br_y], ['zone_br_x', p => p.ZONE_HOME.br_x],
  ['loc_lat', p => p.LOCATION_HOME[0]], ['loc_lon', p => p.LOCATION_HOME[1]],
  ['loc_alt', p => p.LOCATION_HOME[2]],
  ['min_altitude', p => p.MIN_ALTITUDE], ['max_altitude', p => p.MAX_ALTITUDE],
  ['brightness', p => p.BRIGHTNESS], ['night_brightness', p => p.NIGHT_BRIGHTNESS],
  ['gpio_slowdown', p => p.GPIO_SLOWDOWN],
];

function _markInvalid(el, bad) { if (el) el.setAttribute('aria-invalid', bad ? 'true' : 'false'); }

// Returns [message, [field ids]] for the first problem, or null when the payload is fine.
function _validateConfig(p) {
  const bad = [];
  _REQUIRED_NUMBERS.forEach(([id, get]) => {
    const val = get(p);
    const isBad = typeof val !== 'number' || isNaN(val);
    _markInvalid($(id), isBad);
    if (isBad) bad.push(id);
  });
  if (bad.length) return ['Some required numbers are empty or invalid', bad];
  if (p.ZONE_HOME.tl_y < p.ZONE_HOME.br_y) return ['The zone is upside down: the top-left latitude must be north of the bottom-right latitude', ['zone_tl_y', 'zone_br_y']];
  if (p.MIN_ALTITUDE > p.MAX_ALTITUDE) return ['The minimum altitude is above the maximum altitude', ['min_altitude', 'max_altitude']];
  for (const id of ['brightness', 'night_brightness']) {
    const val = id === 'brightness' ? p.BRIGHTNESS : p.NIGHT_BRIGHTNESS;
    if (val < 0 || val > 100) return ['Brightness must be between 0 and 100', [id]];
  }
  return null;
}

function _saveStatus(msg, cls, ms) {
  const st = $('save-status');
  if (!st) return;
  clearTimeout(_saveMsgTimer);
  st.textContent = msg;
  st.className = 'save-status' + (cls ? ' ' + cls : '');
  if (ms) {
    _saveMsgTimer = setTimeout(() =>
      _saveStatus(_configDirty ? 'Unsaved changes' : '', _configDirty ? 'dirty' : ''), ms);
  }
}

function _setConfigDirty(dirty) {
  _configDirty = !!dirty;
  const bar = $('savebar');
  if (bar && bar.dataset) bar.dataset.state = _configDirty ? 'dirty' : 'clean';
  const st = $('save-status');
  if (_configDirty) _saveStatus('Unsaved changes', 'dirty');
  else if (st && st.classList && st.classList.contains('dirty')) _saveStatus('', '');
}

function _setSaveBusy(busy) {
  document.querySelectorAll('[data-action="save"], [data-action="save-restart"]').forEach(b => { b.disabled = !!busy; });
}

async function saveConfig(restart) {
  if (_saving) return;
  const payload = buildConfigPayload();
  const problem = _validateConfig(payload);
  if (problem) {
    const [msg, ids] = problem;
    ids.forEach(id => _markInvalid($(id), true));
    _saveStatus(msg, 'err', 6000);
    showToast(msg, 'err');
    const first = $(ids[0]);
    if (first && typeof first.focus === 'function' && _activeTab === 'config') first.focus();
    return;
  }
  _saving = true;
  _setSaveBusy(true);
  _saveStatus('Saving\u2026');
  try {
    payload.restart = !!restart;
    const data = await _post('/api/config', payload);
    if (data && data.ok) {
      const { restart: _r, ...saved } = payload;
      config = saved;
      // Re-render from the normalized payload so a field clamped or defaulted in
      // buildConfigPayload (a cleared team ID -> 0, an out-of-range TTL) shows what was saved.
      populateForm();
      const msg = restart ? 'Saved. Restarting the display\u2026' : 'Saved';
      _saveStatus(msg, 'ok', 4000);
      showToast(msg, 'ok');
      if (restart) _scheduleStatus(5000, 3);
    } else {
      const msg = (data && data.error) || 'Save failed';
      _saveStatus(msg, 'err', 6000);
      showToast(msg, 'err');
    }
  } catch (e) {
    const msg = e.message || 'Save failed';
    _saveStatus(msg, 'err', 6000);
    showToast(msg, 'err');
  } finally {
    _saving = false;
    _setSaveBusy(false);
  }
}

function _initConfigForm() {
  const form = $('config-form');
  if (!form || !form.addEventListener) return;
  const onEdit = e => {
    if (e.target && e.target.getAttribute && e.target.getAttribute('aria-invalid') === 'true') _markInvalid(e.target, false);
    _refreshDirty();
  };
  form.addEventListener('input', onEdit);
  form.addEventListener('change', onEdit);
}

function toggleScoreboardBody() {
  $('sb-body').hidden = !$('scoreboard_enabled').checked;
}

// Settings section index: jump to a card, and highlight the card in view while scrolling.
let _spyCurrent = null;
let _spyQueued = false;

function _jumpToSection(id) {
  const el = $(id);
  if (!el || !el.scrollIntoView) return;
  el.scrollIntoView({ behavior: _media('(prefers-reduced-motion: reduce)') ? 'auto' : 'smooth', block: 'start' });
  _markSection(id);
}

function _markSection(id) {
  _spyCurrent = id;
  const nav = $('section-index');
  document.querySelectorAll('#section-index button').forEach(b => {
    const on = b.dataset.target === id;
    b.classList.toggle('is-active', on);
    // On narrow screens the index is a horizontal chip row: keep the active chip in view
    // without scrolling the page itself.
    if (on && nav && nav.scrollWidth > nav.clientWidth) {
      const left = b.offsetLeft - (nav.clientWidth - b.offsetWidth) / 2;
      if (nav.scrollTo) nav.scrollTo({ left, behavior: 'smooth' }); else nav.scrollLeft = left;
    }
  });
}

function _onWindowScroll() {
  if (_activeTab !== 'config' || _spyQueued) return;
  _spyQueued = true;
  requestAnimationFrame(() => {
    _spyQueued = false;
    const cards = [...document.querySelectorAll('.settings-form > .card')];
    if (!cards.length) return;
    const line = (parseInt(_cssVar('--appbar-h', '60'), 10) || 60) + 32;
    let current = cards[0].id;
    cards.forEach(c => { if (c.getBoundingClientRect().top <= line) current = c.id; });
    const doc = document.documentElement;
    if (window.innerHeight + window.scrollY >= doc.scrollHeight - 4) current = cards[cards.length - 1].id;
    if (current !== _spyCurrent) _markSection(current);
  });
}

// ── Map ────────────────────────────────────────────────────────────────────────────────
// Leaflet is loaded the first time the Map tab opens, so the dashboard never waits on the
// CDN.  Pinned with Subresource Integrity: a tampered CDN copy is refused, not run.
const _LEAFLET = {
  css: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  cssSri: 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=',
  js: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  jsSri: 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=',
};
let _leafletPromise = null;

function _loadLeaflet() {
  if (typeof L !== 'undefined') return Promise.resolve();
  if (_leafletPromise) return _leafletPromise;
  _leafletPromise = new Promise((resolve, reject) => {
    const head = document.head || document.querySelector('head');
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = _LEAFLET.css;
    css.integrity = _LEAFLET.cssSri;
    css.crossOrigin = '';
    head.appendChild(css);
    const js = document.createElement('script');
    js.src = _LEAFLET.js;
    js.integrity = _LEAFLET.jsSri;
    js.crossOrigin = '';
    js.onload = () => resolve();
    js.onerror = () => { _leafletPromise = null; js.remove(); reject(new Error('Leaflet failed to load')); };
    head.appendChild(js);
  });
  return _leafletPromise;
}

function _setMapState(msg, isError) {
  const el = $('map-state');
  if (!el) return;
  el.hidden = !msg;
  el.textContent = msg || '';
  el.classList.toggle('is-error', !!isError);
}

function initMap() {
  if (typeof L === 'undefined') {
    _setMapState('Loading the map\u2026');
    _loadLeaflet()
      .then(() => { if (_activeTab === 'map') initMap(); })
      .catch(() => _setMapState('The map library could not load. Check the internet connection, then open the Map tab again.', true));
    return;
  }
  if (map) {
    // The map lives for the page's lifetime, but config.ZONE_HOME may have changed since it
    // was built (e.g. zone numbers edited + saved on the Settings tab).  Re-seed the
    // rectangle/handles/coords from the latest config so the map never shows (or saves
    // back) stale bounds.
    _setMapState(null);
    syncMapToConfig();
    setTimeout(() => map.invalidateSize(), 100);
    return;
  }
  if (!config.LOCATION_HOME || !Array.isArray(config.LOCATION_HOME) || !config.ZONE_HOME) {
    _setMapState('Settings have not loaded yet, so there is no zone to show.', true);
    showToast('Settings not loaded yet', 'err');
    return;
  }
  _setMapState(null);
  const accent = _cssVar('--accent', '#4c9bff');
  map = L.map('map').setView([config.LOCATION_HOME[0], config.LOCATION_HOME[1]], 13);
  // OSM retired the a/b/c "{s}." tile subdomains; tile.openstreetmap.org is the supported
  // host.  The page itself sends no Referer (Referrer-Policy: no-referrer), but the OSM tile
  // policy asks browser clients for one, so tile requests send the dashboard's origin.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
    referrerPolicy: 'strict-origin-when-cross-origin',
  }).addTo(map);

  const z = config.ZONE_HOME;
  zoneBounds = { n: z.tl_y, s: z.br_y, w: z.tl_x, e: z.br_x };
  zoneRect = L.rectangle([[z.tl_y, z.tl_x], [z.br_y, z.br_x]],
    { color: accent, weight: 2, fillColor: accent, fillOpacity: 0.12 }).addTo(map);
  homeMarker = L.circleMarker([config.LOCATION_HOME[0], config.LOCATION_HOME[1]],
    { radius: 7, color: '#ffffff', weight: 2, fillColor: '#3fb950', fillOpacity: 1 })
    .addTo(map).bindTooltip('Home');

  const size = _media('(pointer: coarse)') ? 26 : 18;
  const handleIcon = L.divIcon({ className: 'zone-handle', iconSize: [size, size], iconAnchor: [size / 2, size / 2] });

  // NW, NE, SE, SW: each corner owns two edges.
  const cornerDefs = [
    { latEdge: 'n', lngEdge: 'w' },
    { latEdge: 'n', lngEdge: 'e' },
    { latEdge: 's', lngEdge: 'e' },
    { latEdge: 's', lngEdge: 'w' },
  ];
  const cornerPositions = [[z.tl_y, z.tl_x], [z.tl_y, z.br_x], [z.br_y, z.br_x], [z.br_y, z.tl_x]];
  cornerMarkers = [];
  cornerPositions.forEach((pos, i) => {
    const def = cornerDefs[i];
    const m = L.marker(pos, { icon: handleIcon, draggable: true, zIndexOffset: 1000, keyboard: false }).addTo(map);
    m.on('drag', e => {
      // e.latlng is always correct during a drag; don't call getLatLng().
      zoneBounds[def.latEdge] = e.latlng.lat;
      zoneBounds[def.lngEdge] = e.latlng.lng;
      zoneRect.setBounds([[zoneBounds.n, zoneBounds.w], [zoneBounds.s, zoneBounds.e]]);
      updateMapCoordDisplay();
    });
    m.on('dragend', () => {
      // A corner dragged past the opposite one flips the box; normalise so N stays north
      // and W stays west, then snap every handle to the clean bounds.
      const b = zoneBounds;
      zoneBounds = { n: Math.max(b.n, b.s), s: Math.min(b.n, b.s), w: Math.min(b.w, b.e), e: Math.max(b.w, b.e) };
      zoneRect.setBounds([[zoneBounds.n, zoneBounds.w], [zoneBounds.s, zoneBounds.e]]);
      placeCornerMarkers();
      updateMapCoordDisplay();
    });
    cornerMarkers.push(m);
  });

  updateZoneCoordDisplay();
  _fitZone();
}

function _fitZone() {
  if (!map || !zoneRect) return;
  const phone = typeof window.innerWidth === 'number' && window.innerWidth < 768;
  const sheet = document.querySelector('.map-sheet');
  const sheetH = sheet && sheet.offsetHeight ? sheet.offsetHeight : 0;
  const sheetW = sheet && sheet.offsetWidth ? sheet.offsetWidth : 0;
  map.fitBounds(zoneRect.getBounds(), phone
    ? { paddingTopLeft: [24, 24], paddingBottomRight: [24, sheetH + 24] }
    : { paddingTopLeft: [40, 40], paddingBottomRight: [sheetW + 48, 40] });
}

function placeCornerMarkers() {
  if (cornerMarkers.length < 4) return;
  cornerMarkers[0].setLatLng([zoneBounds.n, zoneBounds.w]);
  cornerMarkers[1].setLatLng([zoneBounds.n, zoneBounds.e]);
  cornerMarkers[2].setLatLng([zoneBounds.s, zoneBounds.e]);
  cornerMarkers[3].setLatLng([zoneBounds.s, zoneBounds.w]);
}

function updateMapCoordDisplay() {
  $('m-tl-y').textContent = Number(zoneBounds.n).toFixed(6);
  $('m-br-y').textContent = Number(zoneBounds.s).toFixed(6);
  $('m-tl-x').textContent = Number(zoneBounds.w).toFixed(6);
  $('m-br-x').textContent = Number(zoneBounds.e).toFixed(6);
}

function updateZoneCoordDisplay() {
  const z = config.ZONE_HOME;
  $('m-tl-y').textContent = Number(z.tl_y).toFixed(6);
  $('m-br-y').textContent = Number(z.br_y).toFixed(6);
  $('m-tl-x').textContent = Number(z.tl_x).toFixed(6);
  $('m-br-x').textContent = Number(z.br_x).toFixed(6);
}

function saveFromMap() {
  if (!zoneRect) { showToast('The map has not loaded yet', 'err'); return; }
  const b = zoneRect.getBounds();
  $('zone_tl_y').value = b.getNorth().toFixed(6);
  $('zone_tl_x').value = b.getWest().toFixed(6);
  $('zone_br_y').value = b.getSouth().toFixed(6);
  $('zone_br_x').value = b.getEast().toFixed(6);
  saveConfig(false);
}

// Re-seed the rectangle, corner handles and coord display from config.ZONE_HOME.  Used both
// to discard in-progress drags and to refresh the map when it re-opens after a Settings
// save.  Returns false when there is nothing to sync.
function syncMapToConfig() {
  if (!config.ZONE_HOME || !zoneRect) return false;
  const z = config.ZONE_HOME;
  zoneBounds = { n: z.tl_y, s: z.br_y, w: z.tl_x, e: z.br_x };
  zoneRect.setBounds([[zoneBounds.n, zoneBounds.w], [zoneBounds.s, zoneBounds.e]]);
  placeCornerMarkers();
  updateMapCoordDisplay();
  return true;
}

function discardMapChanges() {
  if (!syncMapToConfig()) { showToast('Settings not loaded yet', 'err'); return; }
  showToast('Zone changes discarded', 'info');
}

// ── Log ────────────────────────────────────────────────────────────────────────────────
let _logQueue = [];
let _logFlushQueued = false;
let _logFilter = '';
let _logFilterTimer = null;

function _setLogStatus(state, text) {
  const chip = $('log-status');
  if (chip && chip.dataset) chip.dataset.state = state;
  const t = $('log-status-text');
  if (t) t.textContent = text;
}

function initLog() {
  if (evtSource) return;
  _setLogStatus('live', 'Live');
  _json('/api/log/history').then(d => {
    // The history is the last N lines, so it replaces whatever is on screen: appending it
    // again on every visit to the tab used to duplicate the log.
    const out = $('log-output');
    const frag = document.createDocumentFragment();
    ((d && d.lines) || []).forEach(line => frag.appendChild(_logLineEl(line)));
    out.textContent = '';
    out.appendChild(frag);
    _trimLog(out);
    _updateLogStats(out);
    scrollLog();
  }).catch(() => {});
  evtSource = new EventSource('/api/log/stream');
  evtSource.onmessage = e => appendLog(e.data);
  evtSource.onerror = () => {
    _setLogStatus('error', 'Reconnecting');
    if (evtSource) evtSource.close();
    evtSource = null;
    setTimeout(() => {
      // Don't reconnect while the page is hidden; visibilitychange re-opens the stream on
      // return.  Without this a backgrounded tab kept reconnecting the SSE log every 5 s.
      if (!document.hidden && _activeTab === 'log') initLog();
    }, 5000);
  };
}

// Lines are batched into one DOM write per animation frame (a busy log no longer forces a
// reflow per line), and auto-scroll only follows when you're already at the bottom.
function appendLog(line) {
  _logQueue.push(line);
  if (_logFlushQueued) return;
  _logFlushQueued = true;
  requestAnimationFrame(_flushLog);
}

function _flushLog() {
  _logFlushQueued = false;
  const out = $('log-output');
  if (!out || !_logQueue.length) { _logQueue = []; return; }
  const follow = !(out.scrollHeight - out.scrollTop - out.clientHeight > 80);
  const frag = document.createDocumentFragment();
  _logQueue.forEach(line => frag.appendChild(_logLineEl(line)));
  _logQueue = [];
  out.appendChild(frag);
  _trimLog(out);
  _updateLogStats(out);
  if ($('autoscroll').checked && follow) scrollLog();
}

function _logLineEl(line) {
  const el = document.createElement('div');
  el.className = 'log-line ' + classifyLine(line);
  el.textContent = line;
  if (_logFilter && !String(line).toLowerCase().includes(_logFilter)) el.classList.add('is-hidden');
  return el;
}

function _trimLog(out) {
  while (out.children.length > LOG_MAX_LINES) out.removeChild(out.firstChild);
}

function _updateLogStats(out) {
  const total = out.children.length;
  let text = `${_fmtInt(total)} ${total === 1 ? 'line' : 'lines'}`;
  if (_logFilter) {
    let shown = 0;
    for (const el of out.children) if (!el.classList.contains('is-hidden')) shown++;
    text = `${_fmtInt(shown)} of ${text}`;
  }
  $('log-stats').textContent = text;
}

function _onLogFilter(value) {
  clearTimeout(_logFilterTimer);
  _logFilterTimer = setTimeout(() => {
    _logFilter = String(value || '').trim().toLowerCase();
    const out = $('log-output');
    for (const el of out.children) {
      el.classList.toggle('is-hidden', !!_logFilter && !el.textContent.toLowerCase().includes(_logFilter));
    }
    _updateLogStats(out);
  }, 120);
}

function classifyLine(line) {
  if (line.includes('[TEST:'))         return 'test';   // must be first; test lines contain other tokens
  if (line.includes('in_zone=True'))   return 'inzone';
  if (line.includes("plane=''"))       return 'nodata';
  if (/error|exception/i.test(line))   return 'error';
  if (line.includes('[override]'))     return 'override';
  if (line.includes(':cached]'))       return 'cached';
  if (line.includes(':miss]'))         return 'miss';
  // Combined "[route:X] [type:X]" line: classify as route
  if (line.includes('[route:'))        return 'route';
  if (line.includes('[web]'))          return 'web';
  return '';
}

function stopLog() {
  if (evtSource) { evtSource.close(); evtSource = null; }
  _setLogStatus('offline', 'Paused');
}

function scrollLog() { const o = $('log-output'); o.scrollTop = o.scrollHeight; }
function clearLog() { $('log-output').textContent = ''; _updateLogStats($('log-output')); }

// ── APIs tab ───────────────────────────────────────────────────────────────────────────
let apisTabTimer = null;
let _usageSnapshot = {};
let _cacheStats = {};

function initAPIsTab() {
  if (apisTabTimer) return;
  loadAPIsTab();
  apisTabTimer = setInterval(loadAPIsTab, 30000);
}

function stopAPIsTab() {
  clearInterval(apisTabTimer);
  apisTabTimer = null;
}

async function loadAPIsTab() {
  try {
    const [usage, stack, stats] = await Promise.all([
      _json('/api/usage'),
      _json('/api/apis'),
      _json('/api/cache/stats'),
    ]);
    _cacheStats = stats || {};
    renderUsage(usage || {});
    renderStack((stack && stack.stack) || []);
  } catch (e) {
    $('apis-month').textContent = 'Could not load API data from the Pi.';
  }
}

function fmtPeriod(start, end) {
  if (!start || !end) return '\u2014';
  // Date.UTC keeps the shown date equal to the server's YYYY-MM-DD in any browser timezone.
  const fmt = s => {
    const [y, m, d] = s.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  };
  return `${fmt(start)} \u2013 ${fmt(end)}`;
}

function _renderMeter(prefix, pct) {
  const bar = $(prefix + '-bar');
  const p = Math.max(0, _num(pct));
  if (!bar) return;
  bar.style.width = Math.min(100, p) + '%';
  bar.className = 'meter-bar' + (p >= 90 ? ' danger' : p >= 70 ? ' warn' : '');
}

function renderUsage(d) {
  _usageSnapshot = d || {};
  const stamp = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  $('apis-month').textContent = `Usage for the current billing periods, updated ${stamp}.`;
  const dash = '\u2014';
  [['al', d.airlabs || {}], ['al2', d.airlabs2 || {}]].forEach(([p, a]) => {
    $(`${p}-calls`).textContent     = a.calls != null ? _fmtInt(a.calls) : dash;
    $(`${p}-remaining`).textContent = a.remaining != null ? _fmtInt(a.remaining) : dash;
    $(`${p}-limit`).textContent     = a.limit != null ? _fmtInt(a.limit) : dash;
    $(`${p}-period`).textContent    = fmtPeriod(a.period_start, a.period_end);
    $(`${p}-pct`).textContent       = `${_num(a.pct_used)}% used`;
    _renderMeter(p, a.pct_used);
  });
  const fa = d.flightaware || {};
  const money = (v, places) => v != null ? `$${_num(v).toFixed(places)}` : dash;
  $('fa-calls').textContent     = fa.calls != null ? _fmtInt(fa.calls) : dash;
  $('fa-spend').textContent     = money(fa.est_spend, 3);
  $('fa-credit').textContent    = money(fa.monthly_credit, 2);
  $('fa-remaining').textContent = money(fa.remaining, 3);
  $('fa-period').textContent    = fmtPeriod(fa.period_start, fa.period_end);
  $('fa-pct').textContent       = `${_num(fa.pct_used)}% of credit`;
  _renderMeter('fa', fa.pct_used);
}

function toggleAdjust(api) {
  const form = $('adjust-' + api);
  if (!form) return;
  const opening = form.hidden;
  form.hidden = !opening;
  if (opening) {
    const inp = $('adjust-' + api + '-val');
    if (api === 'airlabs')       inp.value = _usageSnapshot?.airlabs?.calls ?? 0;
    else if (api === 'airlabs2') inp.value = _usageSnapshot?.airlabs2?.calls ?? 0;
    else                         inp.value = _usageSnapshot?.flightaware?.est_spend ?? 0;
    if (inp.focus) { inp.focus(); inp.select(); }
  }
}

async function saveAdjust(api) {
  const inp = $('adjust-' + api + '-val');
  const value = parseFloat(inp.value);
  if (isNaN(value) || value < 0) { showToast('Enter a number of 0 or more', 'err'); return; }
  try {
    await _post('/api/usage/adjust', { api, value });
    $('adjust-' + api).hidden = true;
    renderUsage(await _json('/api/usage') || {});
    showToast('Usage corrected', 'ok');
  } catch (e) {
    showToast(e.message || 'Request failed', 'err');
  }
}

async function _refreshStack() {
  const [stack, stats] = await Promise.all([_json('/api/apis'), _json('/api/cache/stats')]);
  _cacheStats = stats || {};
  renderStack((stack && stack.stack) || []);
}

async function toggleApi(apiKey) {
  try {
    const j = await _post('/api/apis/toggle', { api: apiKey });
    await _refreshStack();
    showToast(j && j.enabled ? `${apiKey} enabled` : `${apiKey} disabled`, 'ok');
  } catch (e) {
    showToast(e.message || 'Request failed', 'err');
  }
}

async function clearApiCache(apiKey) {
  if (!_confirm(`Clear the ${apiKey} cache? The next lookups will call the API again.`)) return;
  try {
    const j = await _post('/api/cache/clear', { api: apiKey });
    await _refreshStack();
    showToast(`Cache cleared: ${_plural(j && j.deleted, 'entry', 'entries')} removed`, 'ok');
  } catch (e) {
    showToast(e.message || 'Request failed', 'err');
  }
}

function renderStack(stack) {
  const container = $('stack-rows');
  if (!container) return;
  // Priority 0 (type lookup, not in the route chain) goes last; the rest ascending.
  const sorted = [...stack].sort((a, b) => {
    if (a.priority === 0) return 1;
    if (b.priority === 0) return -1;
    return a.priority - b.priority;
  });
  if (!sorted.length) { container.innerHTML = '<p class="empty">No data sources reported.</p>'; return; }
  container.innerHTML = sorted.map(api => {
    const keyBadge = !api.requires_key
      ? '<span class="badge badge-accent">No key needed</span>'
      : api.key_set ? '<span class="badge badge-ok">Key set</span>' : '<span class="badge badge-danger">No key</span>';
    const url = (api.url && /^https:\/\//.test(api.url))
      ? ` \u00b7 <a href="${escHtml(api.url)}" target="_blank" rel="noopener">${escHtml(api.url.replace('https://', ''))}</a>`
      : '';
    // Enable/disable and cache controls only for sources that have a flag.
    const canToggle = api.api_key && api.priority > 0 && api.priority < 7;
    const enabled = !api.disabled;
    const toggle = canToggle
      ? `<button type="button" class="pill-btn${enabled ? ' is-on' : ''}" data-action="api-toggle" data-api="${escHtml(api.api_key)}" aria-pressed="${enabled}" title="${enabled ? 'Click to disable' : 'Click to enable'}"><span class="dot" aria-hidden="true"></span>${enabled ? 'Enabled' : 'Disabled'}</button>`
      : '';
    const cacheCount = api.api_key ? (_cacheStats[api.api_key] ?? '?') : null;
    const cache = (canToggle && cacheCount !== null)
      ? `<button type="button" class="pill-btn" data-action="api-clear-cache" data-api="${escHtml(api.api_key)}" title="Delete cached entries so the next lookup calls the API">${icon('trash')}Clear cache (${escHtml(cacheCount)})</button>`
      : '';
    return `<div class="stack-row${api.disabled ? ' is-disabled' : ''}">
      <div class="stack-prio">${api.priority === 0 ? '\u2014' : escHtml(api.priority)}</div>
      <div>
        <div class="stack-main">
          <div class="stack-name">${escHtml(api.name)}</div>
          <div class="stack-type">${escHtml(api.type)}</div>
          <div class="stack-meta">${escHtml(api.cost)}${url}</div>
          ${api.notes ? `<div class="stack-notes">${escHtml(api.notes)}</div>` : ''}
        </div>
        ${toggle || cache ? `<div class="stack-controls">${toggle}${cache}</div>` : ''}
      </div>
      <div class="stack-badge">${keyBadge}</div>
    </div>`;
  }).join('');
}

async function clearCacheEntry() {
  const input  = $('cache-entry-input');
  const result = $('cache-entry-result');
  const val    = (input.value || '').trim().toUpperCase();
  const say = (text, cls) => { result.textContent = text; result.className = 'form-result' + (cls ? ' ' + cls : ''); };
  if (!val) { say('Enter a tail number or callsign first.'); return; }
  say('Clearing\u2026');
  try {
    const j = await _post('/api/cache/clear/entry', { value: val });
    const n = _num(j && j.deleted);
    say(n > 0 ? `Cleared ${_plural(n, 'entry', 'entries')} for ${val} (${j.found_as}).` : `No cache entries found for ${val}.`, n > 0 ? 'ok' : '');
    input.value = '';
    _refreshStack().catch(() => {});
  } catch (e) {
    say(e.message || 'Request failed', 'err');
  }
}

// ── Stats tab (the home page) ──────────────────────────────────────────────────────────
// One /api/stats call (cached on the Pi until the DB changes) feeds the headline tiles, the
// recent-flights feed, today's hours and, while the 90-day preset is selected, the period
// card.  It refreshes every minute while the tab is open and the page is visible.
let _statsSeq = 0;              // newest "today" refresh wins
let _statsReqSeq = 0;           // newest period request wins: a stale one never repaints it
let _statsPeriodDirty = false;  // true when the date inputs differ from the loaded range
let _serverToday = null;        // the Pi's date (timezone-correct "today")
let _period = { preset: '90d', from: null, to: null };
let _lastDefault = null;        // the last default-mode /api/stats payload
let _statsTimer = null;
let _statsLoaded = false;
let _seenRecent = null;         // recent-flight keys already shown (new ones flash)
const _listShown = {};          // "show more" depth per list, kept across refreshes

function _isoToday() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}
function _isoOffset(days) {
  const d = new Date(); d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
// A date offset from the server's today (falls back to browser time), so a browser in
// another timezone than the Pi still asks for the right days.
function _serverOffset(days) {
  const base = _serverToday || _isoToday();
  const [y, m, d] = base.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, '0')}-${String(dt.getUTCDate()).padStart(2, '0')}`;
}

function _presetRange(preset) {
  const today = _serverToday || _isoToday();
  const ranges = {
    yesterday: [_serverOffset(-1), _serverOffset(-1)],
    '7d':  [_serverOffset(-6), today],
    '30d': [_serverOffset(-29), today],
    '90d': [_serverOffset(-89), today],
  };
  return ranges[preset] || null;
}

function _setPeriodPreset(preset) {
  document.querySelectorAll('#period-presets .seg').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.preset === preset ? 'true' : 'false'));
}

function _setPeriodInputs(from, to) {
  $('period-from').value = from;
  $('period-to').value = to;
  $('period-apply-btn').hidden = true;
  _statsPeriodDirty = false;
}

function _setStatsUpdated(state) {
  const el = $('stats-updated');
  if (!el) return;
  el.dataset.state = state;
  if (state === 'loading') el.textContent = 'Updating…';
  else if (state === 'error') el.textContent = 'Can’t reach the Pi';
  else el.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}

function _startStatsRefresh() {
  if (_statsTimer) return;
  _statsTimer = setInterval(() => {
    if (!document.hidden && _activeTab === 'stats') loadStatsTab({ quiet: true });
  }, 60000);
}

function _stopStatsRefresh() {
  clearInterval(_statsTimer);
  _statsTimer = null;
}

async function loadStatsTab(opts = {}) {
  const quiet = !!opts.quiet;
  if (!_period.from) {
    const [from, to] = _presetRange(_period.preset) || _presetRange('90d');
    _period.from = from; _period.to = to;
    _setPeriodInputs(from, to);
    _setPeriodPreset(_period.preset);
  }
  const seq = ++_statsSeq;
  const pseq = _period.preset === '90d' ? ++_statsReqSeq : null;
  if (!quiet) _setStatsUpdated('loading');
  try {
    const [d, acc, gaAcc] = await Promise.all([
      _json('/api/stats?days=90'),
      _json('/api/free-api-accuracy').catch(() => null),
      _json('/api/ga-accuracy').catch(() => null),
    ]);
    if (seq !== _statsSeq) return;   // a newer refresh superseded this one
    let rolled = false;
    if (d.today) {
      rolled = !!_serverToday && _serverToday !== d.today;
      _serverToday = d.today;
      // Presets are relative to today: keep them anchored when the date rolls over.
      const r = _period.preset ? _presetRange(_period.preset) : null;
      if (r && (rolled || _period.preset === '90d')) {
        _period.from = r[0]; _period.to = r[1];
        if (!_statsPeriodDirty) _setPeriodInputs(r[0], r[1]);
      }
    }
    _lastDefault = d;
    renderTodayStats(d);
    renderRecentFlights(d);
    if (acc && !acc.error)     renderFreeApiAccuracy(acc);
    if (gaAcc && !gaAcc.error) renderGaAccuracy(gaAcc);
    if (pseq !== null && pseq === _statsReqSeq && _period.preset === '90d') {
      renderPeriodStats(d);
    } else if (_period.preset !== '90d' && (rolled || (quiet && _period.to >= (_serverToday || _isoToday())))) {
      // A 7d / 30d / custom view that includes today stays live too.
      _loadPeriod(_period.from, _period.to, { quiet: true });
    }
    _statsLoaded = true;
    _setStatsUpdated('ok');
  } catch (e) {
    if (seq !== _statsSeq) return;
    _setStatsUpdated('error');
    if (!quiet) showToast('Could not load stats from the Pi', 'err');
  }
}

async function _loadPeriod(from, to, opts = {}) {
  const seq = ++_statsReqSeq;
  const btn = opts.button;
  const body = $('period-body');
  if (btn) { btn.classList.add('is-loading'); btn.disabled = true; }
  if (body && !opts.quiet) body.classList.add('is-loading');
  try {
    const d = await _json(`/api/stats?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
    if (seq !== _statsReqSeq) return;   // a newer period request superseded this one
    if (d.error) { if (!opts.quiet) showToast(d.error, 'err'); return; }
    renderPeriodStats(d);
    if (btn) btn.hidden = true;
    _statsPeriodDirty = false;
  } catch (e) {
    if (seq === _statsReqSeq && !opts.quiet) showToast('Could not load stats for that range', 'err');
  } finally {
    if (btn) { btn.classList.remove('is-loading'); btn.disabled = false; }
    if (body && seq === _statsReqSeq) body.classList.remove('is-loading');
  }
}

function onPeriodChange() {
  _statsPeriodDirty = true;
  $('period-apply-btn').hidden = false;
  _setPeriodPreset(null);
}

async function applyStatsPeriod() {
  let from = $('period-from').value;
  let to   = $('period-to').value;
  if (!from || !to) { showToast('Pick a start and an end date', 'err'); return; }
  if (from > to) { [from, to] = [to, from]; $('period-from').value = from; $('period-to').value = to; }
  _period = { preset: null, from, to };
  _setPeriodPreset(null);
  await _loadPeriod(from, to, { button: $('period-apply-btn') });
}

async function resetStatsPeriod() { return quickPeriod('90d'); }

async function quickPeriod(preset, opts = {}) {
  const r = _presetRange(preset);
  if (!r) return;
  const [from, to] = r;
  _setPeriodInputs(from, to);
  _period = { preset, from, to };
  _setPeriodPreset(preset);
  // The default payload already holds the 90-day picture: no second request.
  if (preset === '90d' && _lastDefault && _lastDefault.today === (_serverToday || _isoToday())) {
    ++_statsReqSeq;   // and no older period request may paint over it
    renderPeriodStats(_lastDefault);
    return;
  }
  await _loadPeriod(from, to, opts);
}

function fmtDate(dateStr, opts) {
  if (!dateStr || typeof dateStr !== 'string') return '—';
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-US', { timeZone: 'UTC', ...opts });
}

function _fmtAvg(x) {
  return x >= 100 ? Math.round(x).toLocaleString() : (Math.round(x * 10) / 10).toLocaleString();
}

const _hh = i => String(i).padStart(2, '0');

function _peakHour(hours) {
  let best = -1, max = 0;
  (hours || []).forEach((v, i) => { if (_num(v) > max) { max = _num(v); best = i; } });
  return best < 0 ? '' : `${_hh(best)}:00–${_hh((best + 1) % 24)}:00 (${_plural(max, 'flight')})`;
}

// ── Charts: plain HTML bars (crisp at any width), an optional SVG trend line, and one
// tooltip that follows the pointer or a tap. ───────────────────────────────────────────
function _barChart(el, o) {
  if (!el) return;
  const values = o.values || [];
  const n = values.length;
  const max = o.max || Math.max(1, ...values);
  const bars = values.map((v, i) => {
    const h = v > 0 ? Math.max(2, v / max * 100) : 0;
    const cls = (i === o.highlight ? ' is-hi' : '') + (o.barClass ? ' ' + o.barClass(v, i) : '');
    return `<span class="bc-bar${cls}" style="height:${h.toFixed(1)}%"></span>`;
  }).join('');
  let line = '';
  if (o.line && o.line.some(v => v != null)) {
    const pts = o.line.map((v, i) => v == null ? null : `${(i + 0.5).toFixed(2)},${(100 - v / max * 100).toFixed(2)}`).filter(Boolean).join(' ');
    line = `<svg class="bc-line" viewBox="0 0 ${n} 100" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}"/></svg>`;
  }
  const yAxis = o.yAxis
    ? `<div class="bc-y" aria-hidden="true"><span>${_fmtInt(max)}</span><span>${_fmtInt(Math.round(max / 2))}</span><span>0</span></div>`
    : '';
  const ticks = (o.ticks || []).map(t =>
    `<span style="left:${((t.i + 0.5) / Math.max(n, 1) * 100).toFixed(2)}%">${escHtml(t.label)}</span>`).join('');
  el.classList.toggle('has-y', !!o.yAxis);
  el.style.setProperty('--bc-gap', n > 60 ? '1px' : n > 30 ? '2px' : '3px');
  el.innerHTML = `${yAxis}<div class="bc-plot"><div class="bc-bars">${bars}</div>${line}</div>`
    + `<div class="bc-x" aria-hidden="true">${ticks}</div><div class="bc-tip" hidden></div>`;
  el._tips = o.tips || [];
  el._n = n;
  if (!el._bound && el.addEventListener) {
    el._bound = true;
    el.addEventListener('pointermove', _chartHover);
    el.addEventListener('pointerdown', _chartHover);
    el.addEventListener('pointerleave', _chartLeave);
  }
}

function _chartHover(e) {
  const el = e.currentTarget;
  const plot = el.querySelector('.bc-bars');
  const tip = el.querySelector('.bc-tip');
  if (!plot || !tip || !el._n) return;
  const r = plot.getBoundingClientRect();
  const i = Math.floor((e.clientX - r.left) / r.width * el._n);
  plot.querySelectorAll('.is-hover').forEach(b => b.classList.remove('is-hover'));
  if (i < 0 || i >= el._n || !el._tips[i]) { tip.hidden = true; return; }
  plot.children[i].classList.add('is-hover');
  tip.textContent = el._tips[i];
  tip.hidden = false;
  const box = el.getBoundingClientRect();
  const x = r.left - box.left + (i + 0.5) / el._n * r.width;
  const half = tip.offsetWidth / 2;
  tip.style.left = `${Math.max(half, Math.min(box.width - half, x))}px`;
}

function _chartLeave(e) {
  const el = e.currentTarget;
  const tip = el.querySelector('.bc-tip');
  if (tip) tip.hidden = true;
  el.querySelectorAll('.is-hover').forEach(b => b.classList.remove('is-hover'));
}

function _hoursChart(el, hours, highlight) {
  const vals = Array.from({ length: 24 }, (_, i) => _num((hours || [])[i]));
  _barChart(el, {
    values: vals,
    highlight,
    tips: vals.map((v, i) => `${_hh(i)}:00–${_hh((i + 1) % 24)}:00 · ${_plural(v, 'flight')}`),
    ticks: [0, 6, 12, 18].map(i => ({ i, label: `${_hh(i)}:00` })),
  });
}

function _dayChart(el, hist, today) {
  if (!el) return;
  const vals = hist.map(h => _num(h.total));
  const n = vals.length;
  // Trailing 7-day average, drawn once there's enough history for it to mean something.
  const showAvg = n >= 14;
  const avg = showAvg ? vals.map((_, i) => i < 6 ? null : vals.slice(i - 6, i + 1).reduce((a, b) => a + b, 0) / 7) : null;
  const tickIdx = n <= 8 ? vals.map((_, i) => i) : [0, Math.floor((n - 1) / 2), n - 1];
  _barChart(el, {
    values: vals,
    highlight: hist.findIndex(h => h.date === today),
    line: avg,
    yAxis: true,
    tips: hist.map(h => `${fmtDate(h.date, { weekday: 'short', month: 'short', day: 'numeric' })} · ${_plural(h.total, 'flight')}`
      + ((h.airlabs || h.aeroapi) ? ` · AirLabs ${_fmtInt(h.airlabs)} · AeroAPI ${_fmtInt(h.aeroapi)}` : '')),
    ticks: n ? tickIdx.map(i => ({ i, label: fmtDate(hist[i].date, n <= 8 ? { weekday: 'short' } : { month: 'short', day: 'numeric' }) })) : [],
  });
  const body = $('period-body');
  if (body) body.classList.toggle('no-avg', !showAvg);
  const busiest = hist.reduce((b, h) => (_num(h.total) > _num(b && b.total) ? h : b), null);
  el.setAttribute('aria-label', n
    ? `Flights per day, ${fmtDate(hist[0].date, { month: 'short', day: 'numeric' })} to ${fmtDate(hist[n - 1].date, { month: 'short', day: 'numeric' })}`
      + (busiest && busiest.total ? `; busiest ${fmtDate(busiest.date, { month: 'short', day: 'numeric' })} with ${busiest.total}` : '')
    : 'No flights in this period');
}

// ── Lists ──────────────────────────────────────────────────────────────────────────────
function _rankRow(label, name, count, idx, max) {
  const pct = max ? Math.max(2, Math.round(_num(count) / max * 100)) : 0;
  return `<div class="rank-row${pct ? ' has-bar' : ''}"${pct ? ` style="--w:${pct}%"` : ''}>`
    + `<span class="rank-label">${idx != null ? `<span class="rank-num">${idx}</span>` : ''}${escHtml(label)}${name ? `<small>${escHtml(name)}</small>` : ''}</span>`
    + `<span class="rank-count">${_fmtInt(count)}</span></div>`;
}

// Show-more / show-less controls under a list that renders `shown` items.
function _appendMoreLess(el, total, shown, step, init, render, max) {
  const cap = max ? Math.min(total, max) : total;
  const remaining = cap - shown;
  if (remaining <= 0 && shown <= init) return;
  const ctrl = document.createElement('div');
  ctrl.className = 'list-more';
  if (remaining > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'link-btn';
    more.textContent = `Show ${Math.min(step, remaining)} more`;
    more.onclick = () => render(Math.min(shown + step, cap));
    ctrl.appendChild(more);
  }
  if (shown > init) {
    const less = document.createElement('button');
    less.type = 'button';
    less.className = 'link-btn muted';
    less.textContent = 'Show less';
    less.onclick = () => render(init);
    ctrl.appendChild(less);
  }
  el.appendChild(ctrl);
}

function _fillRankList(elId, items, labelKey, nameKey, step = 5) {
  const el = $(elId);
  if (!el) return;
  const all = items || [];
  const max = Math.max(0, ...all.map(i => _num(i.count)));
  const render = shown => {
    _listShown[elId] = shown;
    if (!all.length) { el.innerHTML = '<p class="empty">No data yet</p>'; return; }
    el.innerHTML = all.slice(0, shown)
      .map((item, i) => _rankRow(item[labelKey], nameKey ? item[nameKey] : '', item.count, i + 1, max)).join('');
    _appendMoreLess(el, all.length, shown, step, step, render);
  };
  render(Math.max(step, _listShown[elId] || step));
}

// ── Today ──────────────────────────────────────────────────────────────────────────────
function renderTodayStats(d) {
  $('stats-date').textContent = d.today ? fmtDate(d.today, { weekday: 'long', month: 'long', day: 'numeric' }) : 'Today';
  $('stats-total').textContent = d.total != null ? _fmtInt(d.total) : '—';

  // Today so far vs. yesterday up to the same time of day (a fair comparison all day long).
  const vs = $('kpi-today-vs');
  const now = d.server_now || '';
  if (Array.isArray(d.yesterday_times) && now && d.total != null) {
    const t = now.slice(11, 19);
    const by = d.yesterday_times.filter(x => x <= t).length;
    const diff = _num(d.total) - by;
    vs.innerHTML = diff === 0
      ? `Same as by now yesterday (${_fmtInt(by)})`
      : `<span class="delta ${diff > 0 ? 'up' : 'down'}">${icon(diff > 0 ? 'up' : 'down')}${_fmtInt(Math.abs(diff))}</span> vs ${_fmtInt(by)} by now yesterday`;
  } else {
    vs.textContent = ' ';
  }

  $('kpi-unique').textContent = d.today_unique != null ? _fmtInt(d.today_unique) : '—';

  const done = (d.history || []).filter(h => h.date !== d.today).slice(-7);
  $('kpi-avg7').textContent = done.length ? _fmtAvg(done.reduce((s, h) => s + _num(h.total), 0) / done.length) : '—';
  $('kpi-avg7-sub').textContent = done.length ? `flights per day, last ${done.length} full days` : 'flights per day';

  // 'airlabs' is the combined total of both AirLabs keys (summed server-side).
  const ac = d.api_calls || {};
  $('kpi-calls').textContent = _fmtInt(_num(ac.airlabs) + _num(ac.aeroapi));
  $('stats-api-calls').textContent = `AirLabs ${_fmtInt(ac.airlabs)} · AeroAPI ${_fmtInt(ac.aeroapi)}`;

  const hours = d.today_hourly || [];
  _hoursChart($('today-hours'), hours, now ? parseInt(now.slice(11, 13), 10) : -1);
  const peak = _peakHour(hours);
  $('today-peak').textContent = peak ? `Busiest so far: ${peak}` : 'No flights yet today';

  const top = _groupNReg(d.top_today);
  const max = Math.max(0, ...top.map(i => _num(i.count)));
  $('stats-top').innerHTML = top.length
    ? top.map(item => _rankRow(item.prefix, item.name, item.count, null, max)).join('')
    : '<p class="empty">No flights yet today.</p>';
}

// ── Flight tables ──────────────────────────────────────────────────────────────────────
function _flightHead(withAircraft) {
  return `<thead><tr><th class="c-time">Time</th><th class="c-flight">Flight</th><th class="c-tail">Tail</th><th class="c-route">Route</th>${withAircraft ? '<th class="c-ac">Aircraft</th>' : ''}</tr></thead>`;
}

// "just now" / "12 min ago" for sightings in the last hour (Pi time on both sides).
function _ago(seenAt, now) {
  if (!seenAt || !now || seenAt.slice(0, 10) !== now.slice(0, 10)) return '';
  const toSec = s => parseInt(s.slice(11, 13), 10) * 3600 + parseInt(s.slice(14, 16), 10) * 60 + (parseInt(s.slice(17, 19), 10) || 0);
  const mins = Math.round((toSec(now) - toSec(seenAt)) / 60);
  if (isNaN(mins) || mins < 0 || mins >= 60) return '';
  return mins < 1 ? 'just now' : `${mins} min ago`;
}

function _flightRow(s, opts = {}) {
  const q = opts.q || '';
  const cs = String(s.callsign || '');
  const reg = String(s.registration || '');
  const orig = String(s.origin || '?');
  const dest = String(s.destination || '?');
  const hit = v => q.length >= 2 && v.includes(q);
  const mark = v => hit(v) ? `<mark class="hl">${escHtml(v)}</mark>` : escHtml(v);
  const ago = opts.now ? _ago(s.seen_at, opts.now) : '';
  const isNew = opts.fresh && opts.keyOf && opts.fresh.has(opts.keyOf(s));
  return `<tr${isNew ? ' class="is-new"' : ''}>`
    + `<td class="c-time mono">${escHtml(_formatTime(s.time || ''))}${ago ? `<span class="ago">${ago}</span>` : ''}</td>`
    + `<td class="c-flight"><span class="mono-strong">${mark(cs)}</span>${s.airline ? `<small>${escHtml(s.airline)}</small>` : ''}</td>`
    + `<td class="c-tail mono">${reg ? mark(reg) : '<span class="subtle">—</span>'}</td>`
    + `<td class="c-route route">${mark(orig)}<span class="arrow">→</span>${mark(dest)}</td>`
    + (opts.aircraft ? `<td class="c-ac muted">${escHtml(s.aircraft || '')}</td>` : '')
    + `</tr>`;
}

// Rows with a date separator whenever the day changes; returns the HTML and the last date
// so a later page can continue without repeating a separator.
function _flightRows(items, opts = {}) {
  let last = opts.lastDate || null;
  const cols = opts.aircraft ? 5 : 4;
  const out = [];
  items.forEach(s => {
    if (s.date !== last) {
      last = s.date;
      out.push(`<tr class="date-sep"><td colspan="${cols}">${escHtml(fmtDate(s.date, { weekday: 'short', month: 'short', day: 'numeric' }))}</td></tr>`);
    }
    out.push(_flightRow(s, opts));
  });
  return { html: out.join(''), lastDate: last };
}

function renderRecentFlights(d) {
  const el = $('recent-flights-list');
  if (!el) return;
  const all = d.recent || [];
  const STEP = 10, INIT = 8, MAX = 50;
  const keyOf = s => `${s.seen_at}|${s.callsign}`;
  // After the first paint, flights that weren't on screen last time get a brief highlight.
  const fresh = _seenRecent ? new Set(all.filter(s => !_seenRecent.has(keyOf(s))).map(keyOf)) : null;
  _seenRecent = new Set(all.map(keyOf));
  const render = shown => {
    _listShown['recent-flights-list'] = shown;
    if (!all.length) { el.innerHTML = '<p class="empty">No flights recorded yet.</p>'; return; }
    el.innerHTML = `<table class="data-table flight-table">${_flightHead(false)}<tbody>`
      + `${_flightRows(all.slice(0, shown), { now: d.server_now, fresh, keyOf }).html}</tbody></table>`;
    _appendMoreLess(el, all.length, shown, STEP, INIT, render, MAX);
  };
  render(Math.max(INIT, _listShown['recent-flights-list'] || INIT));
}

// ── Accuracy ───────────────────────────────────────────────────────────────────────────
function _stamp(seenAt) {
  const s = String(seenAt || '');
  if (s.length < 16) return s || '—';
  return `${_formatDate(s.slice(0, 10))} ${_formatTime(s.slice(11, 16))}`;
}

function _accTone(pct) { return pct >= 90 ? 'good' : pct >= 70 ? 'mid' : 'bad'; }

function _accBlock(stats, title, charts) {
  const head = title ? `<h3 class="subhead">${escHtml(title)}</h3>` : '';
  const total = _num(stats && stats.thirty_day && stats.thirty_day.total);
  if (!total) return `<div class="acc">${head}<p class="empty">No checks recorded yet.</p></div>`;
  const pct = _num(stats.thirty_day.pct);
  const mm = _num(stats.thirty_day.mismatches);
  const todayN = _num(stats.today && stats.today.total);
  const todayPct = stats.today && stats.today.pct != null ? `${stats.today.pct}%` : '—';
  const tone = _accTone(pct);
  const daily = (stats.daily || []).filter(x => _num(x.total) > 0);
  let trend = '';
  if (daily.length >= 2 && charts) {
    charts.push(daily);
    trend = `<div class="bar-chart bar-chart-mini acc-trend" data-chart="${charts.length - 1}" role="img" aria-label="Daily match rate over the last 30 days"></div>`;
  }
  return `<div class="acc">${head}
    <div class="acc-head"><span class="acc-pct ${tone}">${escHtml(pct)}%</span><span class="acc-meta">match rate, last 30 days · ${_plural(total, 'check')} · ${_plural(mm, 'override')}</span></div>
    <div class="meter ${tone}" role="presentation"><div class="meter-bar" style="width:${Math.min(100, Math.round(pct))}%"></div></div>
    ${trend}
    <p class="acc-meta">Today: ${_plural(todayN, 'check')} · ${escHtml(todayPct)} match</p>
  </div>`;
}

function _drawAccCharts(el, charts) {
  el.querySelectorAll('.acc-trend').forEach(c => {
    const daily = charts[Number(c.dataset.chart)] || [];
    const pcts = daily.map(x => Math.round(_num(x.matches) / _num(x.total, 1) * 1000) / 10);
    _barChart(c, {
      values: pcts,
      max: 100,
      barClass: v => 't-' + _accTone(v),
      tips: daily.map((x, i) => `${fmtDate(x.date, { month: 'short', day: 'numeric' })} · ${pcts[i]}% matched (${_num(x.matches)} of ${_plural(x.total, 'check')})`),
    });
  });
}

function renderFreeApiAccuracy(d) {
  const el = $('free-api-content');
  if (!el) return;
  if (!_num(d && d.thirty_day && d.thirty_day.total)) {
    el.innerHTML = '<p class="empty">No cross-checks yet. They build up as commercial flights pass overhead.</p>';
    return;
  }
  const charts = [];
  let html = _accBlock(d, '', charts);
  const mm = d.last_mismatches || [];
  if (mm.length) {
    html += `<h3 class="subhead">Recent overrides <span class="label-note">what adsbdb had, and the paid API's correction</span></h3>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Time</th><th>Flight</th><th>adsbdb had</th><th>Corrected to</th></tr></thead>
        <tbody>${mm.map(r => `<tr><td class="muted c-time">${escHtml(_stamp(r.seen_at))}</td><td class="mono-strong">${escHtml(r.callsign)}</td><td class="mono muted">${escHtml(r.free_route)}</td><td class="mono tone-accent">${escHtml(r.paid_route)}</td></tr>`).join('')}</tbody>
      </table></div>`;
  }
  el.innerHTML = html;
  _drawAccCharts(el, charts);
}

function renderGaAccuracy(d) {
  const el = $('ga-accuracy-content');
  if (!el) return;
  const has = k => _num(d && d[k] && d[k].thirty_day && d[k].thirty_day.total) > 0;
  if (!has('adsbdb') && !has('opensky')) {
    el.innerHTML = '<p class="empty">No GA cross-checks yet. They build up as N-number aircraft pass overhead with FR24 route data.</p>';
    return;
  }
  const charts = [];
  let html = _accBlock(d.adsbdb, 'adsbdb', charts) + _accBlock(d.opensky, 'OpenSky', charts);
  // Merge both APIs' mismatches, newest first, max 10.
  const all = [];
  ((d.adsbdb && d.adsbdb.last_mismatches) || []).forEach(r => all.push({ ...r, _api: 'adsbdb' }));
  ((d.opensky && d.opensky.last_mismatches) || []).forEach(r => all.push({ ...r, _api: 'OpenSky' }));
  all.sort((a, b) => String(b.seen_at || '').localeCompare(String(a.seen_at || '')));
  const mm = all.slice(0, 10);
  if (mm.length) {
    html += `<h3 class="subhead">Recent overrides <span class="label-note">what the free API had, and FR24's correction</span></h3>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Time</th><th>Aircraft</th><th>API</th><th>Free API had</th><th>FR24 says</th></tr></thead>
        <tbody>${mm.map(r => `<tr><td class="muted c-time">${escHtml(_stamp(r.seen_at))}</td><td class="mono-strong">${escHtml(r.registration || r.callsign || '')}</td><td class="muted">${escHtml(r._api)}</td><td class="mono muted">${escHtml(r.free_route)}</td><td class="mono tone-accent">${escHtml(r.fr24_route)}</td></tr>`).join('')}</tbody>
      </table></div>`;
  }
  el.innerHTML = html;
  _drawAccCharts(el, charts);
}

// ── Period card ────────────────────────────────────────────────────────────────────────
const _PRESET_TITLES = { yesterday: 'Yesterday', '7d': 'Last 7 days', '30d': 'Last 30 days', '90d': 'Last 90 days' };

const _SOURCE_BARS = [
  { key: 'free',     label: 'Free APIs',      color: 'var(--ok)' },
  { key: 'mixed',    label: 'Free + paid',    color: 'var(--accent)' },
  { key: 'paid',     label: 'Paid APIs only', color: 'var(--warn)' },
  { key: 'override', label: 'Override rules', color: 'var(--purple)' },
  { key: 'none',     label: 'No route found', color: 'var(--text-3)' },
];

function renderPeriodStats(d) {
  const isRange = d.mode === 'range';
  const ru = d.rollup || {};
  const hist = d.history || [];
  const from = isRange ? d.range_from : ((hist[0] && hist[0].date) || d.range_from);
  const to = isRange ? d.range_to : d.today;
  $('period-title').textContent = _PRESET_TITLES[_period.preset] || 'Custom range';
  $('period-range').textContent = !from || !to ? ' '
    : from === to ? fmtDate(from, { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric' })
    : `${fmtDate(from, { month: 'short', day: 'numeric' })} – ${fmtDate(to, { month: 'short', day: 'numeric', year: 'numeric' })}`;

  // In the default mode d.total is today's count and the period total lives in
  // rollup.flights; in range mode d.total is already the range total.  Likewise
  // d.api_calls is today's by default and range_api_calls holds the period sums.
  const total = isRange ? _num(d.total) : _num(ru.flights);
  $('period-total').textContent = _fmtInt(total);
  $('period-avg').textContent = hist.length ? _fmtAvg(total / hist.length) : '—';
  const busiest = hist.reduce((b, h) => (_num(h.total) > _num(b && b.total) ? h : b), null);
  $('period-busiest').textContent = busiest && busiest.total ? _fmtInt(busiest.total) : '—';
  $('period-busiest-label').textContent = busiest && busiest.total
    ? `busiest day (${fmtDate(busiest.date, { month: 'short', day: 'numeric' })})` : 'busiest day';
  $('period-unique').textContent = d.range_unique != null ? _fmtInt(d.range_unique) : '—';
  const ac = isRange ? (d.api_calls || {}) : (d.range_api_calls || {});
  $('period-calls').textContent = _fmtInt(_num(ac.airlabs) + _num(ac.aeroapi));
  $('period-api-calls').textContent = `paid lookups (AirLabs ${_fmtInt(ac.airlabs)}, AeroAPI ${_fmtInt(ac.aeroapi)})`;

  _dayChart($('day-chart'), hist, d.today);

  _fillRankList('rollup-airlines', _groupNReg(ru.airlines), 'prefix', 'name');
  _fillRankList('rollup-tails',    ru.tails,  'reg',   'name');
  _fillRankList('rollup-routes',   ru.routes, 'route', null);
  _fillRankList('rollup-types',    ru.types,  'type',  null);

  const hours = d.hourly || [];
  _hoursChart($('period-hours'), hours, -1);
  const peak = _peakHour(hours);
  $('period-peak').textContent = peak ? `peak ${peak}` : '';

  const sp = ru.source_pct || {};
  const sources = _SOURCE_BARS.filter(s => _num(sp[s.key]) > 0);
  $('rollup-sources').innerHTML = sources.length
    ? sources.map(s => {
        const pct = _num(sp[s.key]);
        return `<div class="source-row"><div class="source-head"><span>${s.label}</span><span>${escHtml(pct)}%</span></div><div class="source-bar"><div class="source-fill" style="width:${Math.min(100, pct)}%;background:${s.color}"></div></div></div>`;
      }).join('')
    : '<p class="empty">No data yet</p>';

  // Daily table, newest first, skipping all-zero days.
  const histEl = $('stats-history');
  const rows = [...hist].reverse().filter(r => r.total > 0 || r.airlabs > 0 || r.aeroapi > 0);
  if (!rows.length) { histEl.innerHTML = '<p class="empty">No flights in this period.</p>'; return; }
  const api = (v, r) => (v != null && v !== 0) ? _fmtInt(v) : (r.total > 0 ? '0' : '—');
  histEl.innerHTML = `<table class="data-table">
    <thead><tr><th>Date</th><th class="num">Flights</th><th class="num">AirLabs</th><th class="num">AeroAPI</th></tr></thead>
    <tbody>${rows.map(r => {
      const isToday = r.date === d.today;
      return `<tr${isToday ? ' class="is-today"' : ''}><td>${escHtml(fmtDate(r.date, { weekday: 'short', month: 'short', day: 'numeric' }))}${isToday ? ' <span class="badge badge-accent">Today</span>' : ''}</td><td class="num">${_fmtInt(r.total)}</td><td class="num">${api(r.airlabs, r)}</td><td class="num">${api(r.aeroapi, r)}</td></tr>`;
    }).join('')}</tbody></table>`;
}

function renderStats(d) {
  renderTodayStats(d);
  renderRecentFlights(d);
  renderPeriodStats(d);
}

// ── Search ─────────────────────────────────────────────────────────────────────────────
let _searchQ        = '';
let _searchOffset   = 0;
let _searchTotal    = 0;
let _searchLastDate = null;
let _searchGen      = 0;     // bumped on each new search; stale responses and pages bail

function _appendSearchRows(items, tbody, QU) {
  const res = _flightRows(items, { aircraft: true, q: QU, lastDate: _searchLastDate });
  _searchLastDate = res.lastDate;
  tbody.insertAdjacentHTML('beforeend', res.html);
}

function _updateSeeMoreBtn(resEl) {
  const existing = $('search-more-btn');
  if (existing && existing.remove) existing.remove();
  if (_searchOffset >= _searchTotal) return;
  const left = _searchTotal - _searchOffset;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'search-more-btn';
  btn.className = 'btn btn-secondary more-btn';
  btn.textContent = `Load ${Math.min(100, left).toLocaleString()} more (${left.toLocaleString()} left)`;
  btn.onclick = loadMoreSearchResults;
  resEl.appendChild(btn);
}

async function doStatsSearch() {
  const input = $('stats-search-input');
  const resEl = $('stats-search-results');
  const card = $('search-card');
  const summary = $('search-summary');
  const q = (input.value || '').trim();
  card.hidden = false;
  if (q.length < 2) {
    summary.textContent = '';
    resEl.innerHTML = '<p class="empty">Enter at least 2 characters.</p>';
    return;
  }
  _searchQ = q; _searchOffset = 0; _searchTotal = 0; _searchLastDate = null;
  const gen = ++_searchGen;   // invalidates any in-flight search or "Load more" before this one
  summary.textContent = 'Searching…';
  resEl.innerHTML = '';
  try {
    const d = await _json(`/api/stats/search?q=${encodeURIComponent(q)}&limit=100&offset=0`);
    if (gen !== _searchGen) return;
    if (d.error) { summary.textContent = ''; resEl.innerHTML = `<p class="empty tone-danger">${escHtml(d.error)}</p>`; return; }
    const items = d.sightings || [];
    if (!items.length) { summary.textContent = `No sightings match “${q}”.`; return; }
    _searchTotal  = _num(d.count, items.length);
    _searchOffset = items.length;
    summary.textContent = `${_plural(_searchTotal, 'sighting')} for “${q}”, newest first`;
    resEl.innerHTML = `<table class="data-table flight-table" id="search-table">${_flightHead(true)}<tbody></tbody></table>`;
    _appendSearchRows(items, resEl.querySelector('#search-table tbody'), q.toUpperCase());
    _updateSeeMoreBtn(resEl);
  } catch (e) {
    if (gen !== _searchGen) return;
    summary.textContent = '';
    resEl.innerHTML = '<p class="empty tone-danger">Search failed.</p>';
  }
}

async function loadMoreSearchResults() {
  const btn = $('search-more-btn');
  if (btn) { btn.textContent = 'Loading…'; btn.disabled = true; btn.classList.add('is-loading'); }
  const gen = _searchGen;   // tie this page to the search that spawned the button
  const q = _searchQ;
  try {
    const d = await _json(`/api/stats/search?q=${encodeURIComponent(q)}&limit=100&offset=${_searchOffset}`);
    if (gen !== _searchGen) return;   // a new search started; don't append into its table
    if (d.error || !(d.sightings && d.sightings.length)) { if (btn) btn.remove(); return; }
    _searchOffset += d.sightings.length;
    const tbody = document.querySelector('#search-table tbody');
    if (tbody) _appendSearchRows(d.sightings, tbody, q.toUpperCase());
    _updateSeeMoreBtn($('stats-search-results'));
  } catch (e) {
    if (gen !== _searchGen) return;
    if (btn) { btn.textContent = 'Load failed. Tap to retry'; btn.disabled = false; btn.classList.remove('is-loading'); }
  }
}

function clearStatsSearch() {
  _searchGen++;   // drop any search still in flight
  $('stats-search-input').value = '';
  $('search-card').hidden = true;
  $('stats-search-results').innerHTML = '';
  $('search-summary').textContent = '';
}

function _formatTime(hhmm) {
  // "HH:MM" in 24-hour time (from the server / SQLite).
  return hhmm;
}

function _formatDate(isoDate) {
  // isoDate is "YYYY-MM-DD"
  if (!isoDate || isoDate.length < 10) return isoDate;
  const [y, m, d] = isoDate.slice(0, 10).split('-');
  const fmt = config.DATE_FORMAT || 'MDY';
  if (fmt === 'DMY') return `${parseInt(d)}/${parseInt(m)}/${y}`;
  if (fmt === 'YMD') return `${y}-${m}-${d}`;
  return `${parseInt(m)}/${parseInt(d)}/${y}`;  // MDY default
}

// ── Override rules ─────────────────────────────────────────────────────────────────────
let _rules = [];
let _rulesLoaded = false;
let _editingIdx = null;
let _dragIdx    = null;

async function initRulesTab() {
  // Fetch on the first visit only; later visits re-render the in-memory list (kept in sync
  // after every add / edit / delete / reorder).
  if (_rulesLoaded) { renderRules(); return; }
  try {
    const data = await _json('/api/overrides');
    if (!Array.isArray(data)) throw new Error('Unexpected response format');
    _rules = data;
    _rulesLoaded = true;
    renderRules();
  } catch (e) {
    $('rules-table-wrap').innerHTML =
      `<p class="empty tone-danger">Failed to load rules: ${escHtml(e.message)}</p>`;
  }
}

function _ruleRoute(r) {
  if (!r.origin && !r.destination) return '<span class="rule-blank">No route</span>';
  const ap = v => v ? `<span class="rule-airport">${escHtml(v)}</span>` : '<span class="rule-blank">?</span>';
  return `<span class="rule-route">${ap(r.origin)}<span class="arrow">\u2192</span>${ap(r.destination)}</span>`;
}

function renderRules() {
  const wrap = $('rules-table-wrap');
  if (!wrap) return;
  const countEl = $('rules-count');
  if (countEl) countEl.textContent = _rules.length ? String(_rules.length) : '';
  if (!_rules.length) {
    wrap.innerHTML = '<p class="empty">No rules yet. Add one below.</p>';
    return;
  }
  const editing = _editingIdx !== null;
  const n = _rules.length;
  const rows = _rules.map((r, i) => {
    if (i === _editingIdx) {
      const inp = (id, val, extra = '') =>
        `<input id="${id}" class="rule-edit-input${extra.includes('mono') ? ' mono upper' : ''}" data-idx="${i}" value="${escHtml(val || '')}" ${extra.replace('mono', '')}>`;
      return `<tr class="editing" data-idx="${i}">`
        + `<td class="r-order"></td>`
        + `<td class="r-pattern" data-label="Pattern">${inp('edit-pattern', r.pattern, 'mono aria-label="Pattern" autocapitalize="characters" spellcheck="false"')}</td>`
        + `<td class="r-route" data-label="Route"><div class="rule-edit-route">${inp('edit-origin', r.origin, 'mono maxlength="4" aria-label="Origin" placeholder="From" autocapitalize="characters" spellcheck="false"')}<span class="arrow">\u2192</span>${inp('edit-dest', r.destination, 'mono maxlength="4" aria-label="Destination" placeholder="To" autocapitalize="characters" spellcheck="false"')}</div></td>`
        + `<td class="r-display" data-label="Display name">${inp('edit-display', r.display, 'aria-label="Display name"')}</td>`
        + `<td class="r-plane" data-label="Type override">${inp('edit-plane', r.plane, 'aria-label="Type override"')}</td>`
        + `<td class="r-note" data-label="Note">${inp('edit-note', r.note, 'aria-label="Note"')}</td>`
        + `<td class="r-actions"><button type="button" class="btn btn-primary btn-sm" data-action="rule-save" data-idx="${i}">${icon('check')}Save</button><button type="button" class="btn btn-ghost btn-sm" data-action="rule-cancel">Cancel</button></td>`
        + `</tr>`;
    }
    const pat = escHtml(r.pattern);
    return `<tr data-idx="${i}"${editing ? '' : ' draggable="true"'}>`
      + `<td class="r-order"><span class="drag-handle" title="Drag to reorder">${icon('grip')}</span><span class="move-btns">`
      + `<button type="button" class="icon-btn icon-btn-sm" data-action="rule-move" data-idx="${i}" data-dir="-1" aria-label="Move ${pat} up"${editing || i === 0 ? ' disabled' : ''}>${icon('up')}</button>`
      + `<button type="button" class="icon-btn icon-btn-sm" data-action="rule-move" data-idx="${i}" data-dir="1" aria-label="Move ${pat} down"${editing || i === n - 1 ? ' disabled' : ''}>${icon('down')}</button>`
      + `</span></td>`
      + `<td class="r-pattern"><span class="rule-pattern">${pat}</span></td>`
      + `<td class="r-route">${_ruleRoute(r)}</td>`
      + `<td class="r-display" data-label="Display name">${r.display ? `<span class="rule-note">${escHtml(r.display)}</span>` : ''}</td>`
      + `<td class="r-plane" data-label="Type override">${r.plane ? `<span class="rule-note">${escHtml(r.plane)}</span>` : ''}</td>`
      + `<td class="r-note" data-label="Note">${r.note ? `<span class="rule-note">${escHtml(r.note)}</span>` : ''}</td>`
      + `<td class="r-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="rule-edit" data-idx="${i}">${icon('edit')}Edit</button>`
      + `<button type="button" class="btn btn-danger btn-sm" data-action="rule-delete" data-idx="${i}" aria-label="Delete ${pat}" title="Delete">${icon('trash')}</button></td>`
      + `</tr>`;
  }).join('');

  wrap.innerHTML = `<table class="rules-table">
    <thead><tr><th class="r-order"><span class="sr-only">Order</span></th><th>Pattern</th><th>Route</th><th>Display name</th><th>Type override</th><th>Note</th><th class="r-actions"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  // Focus the first field when a row has just entered edit mode.
  if (editing) setTimeout(() => { const el = $('edit-pattern'); if (el && el.focus) el.focus(); }, 0);
}

function editRule(i) {
  if (_editingIdx !== null && _editingIdx !== i) showToast('Unsaved edits to the other rule were discarded', 'info');
  _editingIdx = i;
  renderRules();
}

function cancelEdit() {
  _editingIdx = null;
  renderRules();
}

// Drag-to-reorder (desktop).  Touch screens use the arrow buttons: HTML5 drag-and-drop
// doesn't work reliably with fingers.
function dragStart(e, i) {
  _dragIdx = i;
  if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(i)); }
  // A tick later, so the drag ghost renders before the row dims.
  setTimeout(() => {
    const rows = document.querySelectorAll('.rules-table tbody tr');
    if (rows[i]) rows[i].classList.add('dragging');
  }, 0);
}

function dragOver(e, i) {
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.rules-table tbody tr').forEach((r, idx) => {
    r.classList.toggle('drag-over', idx === i && idx !== _dragIdx);
  });
}

function dragLeave(e, tr) {
  // Only clear when the pointer has truly left the row, not just moved onto a child.
  if (tr && !tr.contains(e.relatedTarget)) tr.classList.remove('drag-over');
}

async function dragDrop(e, i) {
  e.preventDefault();
  dragEnd();
  // Capture the source now: the browser's dragend fires while the save below is still in
  // flight and resets _dragIdx, which used to break the revert on a failed save.
  const from = _dragIdx;
  _dragIdx = null;
  if (from === null || from === i) return;
  const moved = _rules.splice(from, 1)[0];
  _rules.splice(i, 0, moved);
  const ok = await saveRules();
  if (!ok) {
    const back = _rules.splice(i, 1)[0];
    _rules.splice(from, 0, back);
  }
  renderRules();
  if (ok) showToast('Rules reordered', 'ok');
}

function dragEnd() {
  document.querySelectorAll('.rules-table tbody tr').forEach(r => r.classList.remove('dragging', 'drag-over'));
}

function _initRulesDnD() {
  const wrap = $('rules-table-wrap');
  if (!wrap || !wrap.addEventListener) return;
  const rowOf = e => (e.target && e.target.closest ? e.target.closest('tr[data-idx]') : null);
  wrap.addEventListener('dragstart', e => {
    const tr = rowOf(e);
    if (tr && _editingIdx === null) dragStart(e, Number(tr.dataset.idx));
  });
  wrap.addEventListener('dragover', e => {
    const tr = rowOf(e);
    if (tr && _dragIdx !== null) dragOver(e, Number(tr.dataset.idx));
  });
  wrap.addEventListener('dragleave', e => dragLeave(e, rowOf(e)));
  wrap.addEventListener('drop', e => {
    const tr = rowOf(e);
    if (tr && _dragIdx !== null) dragDrop(e, Number(tr.dataset.idx));
  });
  wrap.addEventListener('dragend', () => { dragEnd(); _dragIdx = null; });
}

async function moveRule(i, dir) {
  const j = i + dir;
  if (_editingIdx !== null || j < 0 || j >= _rules.length) return;
  [_rules[i], _rules[j]] = [_rules[j], _rules[i]];
  const ok = await saveRules();
  if (!ok) [_rules[i], _rules[j]] = [_rules[j], _rules[i]];
  renderRules();
  // Keep keyboard focus on the moved rule's arrow so repeated presses keep moving it.
  const next = document.querySelector(`.rules-table [data-action="rule-move"][data-idx="${ok ? j : i}"][data-dir="${dir}"]`);
  if (next && !next.disabled && next.focus) next.focus();
}

async function saveEditRule(i) {
  const pattern = ($('edit-pattern').value || '').trim().toUpperCase();
  if (!pattern) { showToast('A pattern is required', 'err'); return; }
  if (_rules.some((r, idx) => idx !== i && r.pattern === pattern)) {
    showToast(`A rule for "${pattern}" already exists`, 'err');
    return;
  }
  const original = { ..._rules[i] };
  _rules[i] = {
    pattern,
    origin:      ($('edit-origin').value  || '').trim().toUpperCase(),
    destination: ($('edit-dest').value    || '').trim().toUpperCase(),
    display:     ($('edit-display').value || '').trim(),
    plane:       ($('edit-plane').value   || '').trim(),
    note:        ($('edit-note').value    || '').trim(),
  };
  const ok = await saveRules();
  if (ok) {
    _editingIdx = null;
    renderRules();
    showToast(`Rule updated: ${pattern}`, 'ok');
  } else {
    _rules[i] = original;
    renderRules();
  }
}

async function addRule() {
  const pattern = $('rule-pattern').value.trim().toUpperCase();
  if (!pattern) {
    showToast('A pattern is required', 'err');
    _markInvalid($('rule-pattern'), true);
    return;
  }
  _markInvalid($('rule-pattern'), false);
  if (_rules.some(r => r.pattern === pattern)) {
    showToast(`A rule for "${pattern}" already exists`, 'err');
    return;
  }
  const rule = {
    pattern,
    origin:      $('rule-origin').value.trim().toUpperCase(),
    destination: $('rule-dest').value.trim().toUpperCase(),
    display:     $('rule-display').value.trim(),
    plane:       $('rule-plane').value.trim(),
    note:        $('rule-note').value.trim(),
  };
  _rules.push(rule);
  const ok = await saveRules();
  if (ok) {
    ['rule-pattern', 'rule-origin', 'rule-dest', 'rule-display', 'rule-plane', 'rule-note'].forEach(id => { $(id).value = ''; });
    renderRules();
    showToast(`Rule added: ${rule.pattern}`, 'ok');
  } else {
    _rules.pop(); // revert on failure
  }
}

async function deleteRule(i) {
  const rule = _rules[i];
  if (!rule) return;
  if (!_confirm(`Delete the override rule for ${rule.pattern}?`)) return;
  _editingIdx = null;
  _rules.splice(i, 1);
  const ok = await saveRules();
  if (!ok) _rules.splice(i, 0, rule); // revert on failure
  renderRules();
  if (ok) showToast(`Rule removed: ${rule.pattern}`, 'ok');
}

async function saveRules() {
  const status = $('rules-save-status');
  const set = (text, cls) => { status.textContent = text; status.className = 'save-status' + (cls ? ' ' + cls : ''); };
  set('Saving\u2026');
  try {
    const d = await _post('/api/overrides', _rules);
    if (d && d.ok) {
      set('Saved', 'ok');
      setTimeout(() => { if (status.textContent === 'Saved') set(''); }, 3000);
      return true;
    }
    const msg = (d && d.error) || 'Save failed';
    set(msg, 'err');
    showToast(msg, 'err');
    return false;
  } catch (e) {
    const msg = e.message || 'Save failed';
    set(msg, 'err');
    showToast(msg, 'err');
    return false;
  }
}

// ── Airline name lookup ────────────────────────────────────────────────────────────────
const AIRLINE_NAMES = {
  // US major
  'AAL':'American Airlines',      'DAL':'Delta Air Lines',
  'UAL':'United Airlines',        'SWA':'Southwest Airlines',
  'ASA':'Alaska Airlines',        'JBU':'JetBlue Airways',
  'NKS':'Spirit Airlines',        'FFT':'Frontier Airlines',
  'HAL':'Hawaiian Airlines',      'SCX':'Sun Country Airlines',
  // US regional
  'SKW':'SkyWest Airlines',       'RPA':'Republic Airways',
  'ENY':'Envoy Air',              'JIA':'PSA Airlines',
  'ASH':'Mesa Airlines',          'AWI':'Air Wisconsin',
  'PDT':'Piedmont Airlines',      'GJS':'GoJet Airlines',
  'QXE':'Horizon Air',            'SIL':'Silver Airways',
  'TSQ':'Trans States Airlines',  'CHQ':'Chautauqua Airlines',
  'CPZ':'Comair',                 'JZA':'Jazz Aviation',
  'UCA':'CommutAir',              'MTN':'Mountain Air Cargo',
  'FLG':'Frontier (charter)',
  // US cargo
  'FDX':'FedEx Express',          'UPS':'UPS Airlines',
  'GTI':'Atlas Air',              'ABX':'ABX Air',
  'PAC':'Polar Air Cargo',        'SOU':'Southern Air',
  'CKS':'Kalitta Air',            'ATN':'Air Transport Intl',
  'OAE':'Omni Air International', 'ASN':'Amazon Air',
  'WGN':'Western Global Airlines','NCR':'Northern Air Cargo',
  'DHK':'DHL Aviation',           'AGX':'Amerijet International',
  // US charter / business
  'EJA':'NetJets',                'LXJ':'Flexjet',
  'JSX':'JSX',                    'OCN':'Discover Airlines',
  // Mexico
  'AMX':'Aeromexico',             'VOI':'Volaris',
  'VIV':'VivaAerobus',            'AIJ':'Interjet',
  'MXY':'Breeze Airways',         'VXP':'Avelo Airlines',
  'VRD':'Virgin America',
  // Canada
  'ACA':'Air Canada',             'WJA':'WestJet',
  'TRS':'Air Transat',            'SWG':'Sunwing Airlines',
  'CJT':'Cargojet',               'ROU':'Air Canada Rouge',
  'POE':'Porter Airlines',        'FLE':'Flair Airlines',
  // UK
  'BAW':'British Airways',        'VIR':'Virgin Atlantic',
  'EZY':'easyJet',                'TOM':'TUI Airways',
  'EXS':'Jet2',
  // Germany
  'DLH':'Lufthansa',              'EWG':'Eurowings',
  'CFG':'Condor',                 'GEC':'Lufthansa Cargo',
  // France / Netherlands
  'AFR':'Air France',             'KLM':'KLM',
  // Spain / Italy
  'IBE':'Iberia',                 'VLG':'Vueling',
  'IBS':'Iberia Express',         'AZA':'ITA Airways',
  // Ireland
  'EIN':'Aer Lingus',             'RYR':'Ryanair',
  // Scandinavia / Nordic
  'SAS':'Scandinavian Airlines',  'FIN':'Finnair',
  'NAX':'Norwegian',              'NOZ':'Norwegian Air Sweden',
  'ICE':'Icelandair',
  // Switzerland / Austria / Portugal
  'SWR':'SWISS',                  'AUA':'Austrian Airlines',
  'TAP':'TAP Air Portugal',       'EDW':'Edelweiss Air',
  'TWY':'Solarius Aviation',
  // Belgium / Poland / Hungary / Czech / Romania / Greece
  'BEL':'Brussels Airlines',      'LOT':'LOT Polish Airlines',
  'WZZ':'Wizz Air',               'CSA':'Czech Airlines',
  'ROT':'TAROM',                  'AEE':'Aegean Airlines',
  'OAL':'Olympic Air',
  // Turkey / Russia
  'THY':'Turkish Airlines',       'PGT':'Pegasus Airlines',
  'AFL':'Aeroflot',               'SBI':'S7 Airlines',
  // Middle East
  'UAE':'Emirates',               'QTR':'Qatar Airways',
  'ETD':'Etihad Airways',         'SVA':'Saudia',
  'GFA':'Gulf Air',               'MSR':'EgyptAir',
  'ISR':'El Al',                  'MEA':'Middle East Airlines',
  'RJA':'Royal Jordanian',        'OMA':'Oman Air',
  'FDB':'flydubai',               'ABY':'Air Arabia',
  'KAC':'Kuwait Airways',
  // Asia-Pacific
  'SIA':'Singapore Airlines',     'CPA':'Cathay Pacific',
  'JAL':'Japan Airlines',         'ANA':'All Nippon Airways',
  'KAL':'Korean Air',             'AAR':'Asiana Airlines',
  'CSN':'China Southern',         'CCA':'Air China',
  'CES':'China Eastern',          'CAL':'China Airlines',
  'MAS':'Malaysia Airlines',      'THA':'Thai Airways',
  'GIA':'Garuda Indonesia',       'PAL':'Philippine Airlines',
  'EVA':'EVA Air',                'QFA':'Qantas',
  'ANZ':'Air New Zealand',        'AIC':'Air India',
  'IGO':'IndiGo',                 'JST':'Jetstar',
  'TGW':'Scoot',                  'APJ':'Peach Aviation',
  'LNI':'Lion Air',               'CEB':'Cebu Pacific',
  'HVN':'Vietnam Airlines',       'VJC':'VietJet Air',
  'AXM':'AirAsia',                'AIQ':'Thai AirAsia',
  // Latin America
  'LAN':'LATAM Airlines',         'TAM':'LATAM Brasil',
  'AVA':'Avianca',                'GOL':'GOL Airlines',
  'AZU':'Azul Airlines',          'CMP':'Copa Airlines',
  'ARG':'Aerolíneas Argentinas',  'AEA':'Air Europa',
  'SKU':'Sky Airline',
  // Africa
  'ETH':'Ethiopian Airlines',     'SAA':'South African Airways',
  'KQA':'Kenya Airways',          'RAM':'Royal Air Maroc',
  'DAH':'Air Algérie',            'TAR':'Tunisair',
};

function airlineName(callsign) {
  if (!callsign) return null;
  // The 2-3 letter ICAO prefix before the digits: UAL2175 -> UAL
  const m = callsign.match(/^([A-Z]{2,3})(?=\d)/);
  return m ? (AIRLINE_NAMES[m[1]] || null) : null;
}

// ── Test flight ────────────────────────────────────────────────────────────────────────
let _testCountdownTimer = null;

function _setBtnLabel(btn, text) {
  const span = btn.querySelector ? btn.querySelector('span') : null;
  if (span) span.textContent = text; else btn.textContent = text;
}

async function runTestFlight() {
  const input = $('test-callsign');
  const cs = (input.value || '').trim().toUpperCase();
  if (!cs) { if (input.focus) input.focus(); return; }
  const useCache = $('test-use-cache').checked;
  const btn      = $('test-run-btn');
  const clearBtn = $('test-clear-btn');
  const resetBtn = $('test-reset-btn');
  const resultEl = $('test-result');
  btn.disabled = true;
  btn.classList.add('is-loading');
  _setBtnLabel(btn, 'Testing\u2026');
  clearBtn.hidden = true;
  resultEl.hidden = true;
  clearInterval(_testCountdownTimer);
  try {
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 90000) : null;
    let resp;
    try {
      resp = await fetch('/api/test_flight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callsign: cs, use_cache: useCache }),
        signal: ctrl ? ctrl.signal : undefined,
      });
    } finally {
      if (timer) clearTimeout(timer);
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `Server error ${resp.status}`);
    renderTestResult(data);
    clearBtn.hidden = !data.display_injected;
    resetBtn.hidden = false;
  } catch (e) {
    const msg = e && e.name === 'AbortError'
      ? 'The lookup took longer than 90 seconds and was cancelled.'
      : (e.message || 'Request failed');
    resultEl.innerHTML = `<div class="banner banner-error">${icon('alert')}<span>${escHtml(msg)}</span></div>`;
    resultEl.hidden = false;
    resetBtn.hidden = false;
  } finally {
    btn.disabled = false;
    btn.classList.remove('is-loading');
    _setBtnLabel(btn, 'Run test');
  }
}

function _livePosBody(s) {
  if (!s) return '<span class="tone-muted">\u2014</span>';
  if (s.error) return `<span class="tone-danger">${escHtml(s.error)}</span>`;
  if (s.airborne === false) return '<span class="tone-muted">Not airborne</span>';
  const alt = s.alt_ft != null ? `${_fmtInt(s.alt_ft)} ft` : '\u2014';
  const vs  = s.vs != null ? ` \u00b7 vs ${s.vs > 0 ? '+' : ''}${escHtml(s.vs)}` : '';
  return `<span class="tone-ok">Found</span> ${escHtml(s.hex || '\u2014')}`
    + `<span class="sub">alt ${alt}${vs}${s.found_by ? ` \u00b7 ${escHtml(s.found_by)}` : ''}</span>`;
}

function _step(name, desc, body) {
  return `<div class="step"><div class="step-name">${escHtml(name)}</div><div class="step-desc">${escHtml(desc)}</div><div class="step-body">${body}</div></div>`;
}

function renderTestResult(d) {
  const el = $('test-result');
  const steps = d.steps || {};
  const isCacheMode = steps.mode === 'cache';

  let ovHtml;
  if (d.override_matched && d.override) {
    const ov = d.override;
    const parts = [`Pattern <code>${escHtml(ov.pattern)}</code>`];
    if (ov.origin || ov.destination) parts.push(`route <code>${escHtml(ov.origin || '?')} \u2192 ${escHtml(ov.destination || '?')}</code>`);
    if (ov.display) parts.push(`display <code>${escHtml(ov.display)}</code>`);
    if (ov.plane)   parts.push(`type <code>${escHtml(ov.plane)}</code>`);
    ovHtml = `<div class="override-box"><strong>Override matched</strong>${parts.join(' \u00b7 ')}${ov.note ? `<div class="muted small">${escHtml(ov.note)}</div>` : ''}</div>`;
  } else {
    ovHtml = '<p class="muted small">No override rule matched.</p>';
  }

  let stepsHtml;
  if (isCacheMode) {
    const rr = steps.route_result || {};
    const tr = steps.type_result  || {};
    const routeBody = (rr.origin || rr.destination)
      ? `<span class="tone-accent">${escHtml(rr.origin || '?')} \u2192 ${escHtml(rr.destination || '?')}</span><span class="sub">source: ${escHtml(rr.source || '?')}</span>`
      : '<span class="tone-muted">No route data</span>';
    const typeBody = tr.plane
      ? `<span class="tone-purple">${escHtml(tr.plane)}</span><span class="sub">source: ${escHtml(tr.source || '?')}</span>`
      : '<span class="tone-muted">No type data</span>';
    stepsHtml = `<div><p class="steps-title">Cache pipeline <small>get_route() + get_aircraft_type(), the live code path</small></p><div class="steps">`
      + _step('airplanes.live', 'Live position', _livePosBody(steps.live_position))
      + _step('Route', 'get_route(): full cache waterfall', routeBody)
      + _step('Aircraft type', 'get_aircraft_type(): cache, then APIs', typeBody)
      + `</div></div>`;
  } else {
    const defs = [
      { key: 'live_position', label: 'airplanes.live', desc: 'Live position + type' },
      { key: 'adsbdb_route',  label: 'adsbdb route',   desc: 'Static historical DB' },
      { key: 'opensky',       label: 'OpenSky',        desc: 'Real-time by hex' },
      { key: 'airlabs',       label: 'AirLabs',        desc: 'Real-time by callsign' },
      { key: 'aeroapi',       label: 'AeroAPI',        desc: 'FlightAware, paid' },
      { key: 'adsbdb_type',   label: 'adsbdb type',    desc: 'Aircraft type fallback' },
    ];
    const body = def => {
      const s = steps[def.key];
      if (!s) {
        if (d.override_matched && ['adsbdb_route', 'opensky', 'airlabs', 'aeroapi'].includes(def.key))
          return '<span class="tone-muted">Skipped: override</span>';
        if (!d.hex_code && def.key === 'adsbdb_type') return '<span class="tone-muted">Skipped: no hex</span>';
        return '<span class="tone-muted">\u2014</span>';
      }
      if (s.skipped) return `<span class="tone-warn">Skipped: ${escHtml(s.skipped)}</span>`;
      if (s.error)   return `<span class="tone-danger">${escHtml(s.error)}</span>`;
      if (def.key === 'live_position') return _livePosBody(s);
      if (def.key === 'adsbdb_type') {
        return s.type ? `<span class="tone-purple">${escHtml(s.type)}</span>` : '<span class="tone-muted">No type data</span>';
      }
      if (s.origin !== undefined || s.destination !== undefined) {
        const plaus = s.plausible === false ? '<span class="sub tone-danger">Implausible for this position</span>'
                    : s.plausible === true  ? '<span class="sub tone-ok">Plausible</span>' : '';
        const code = s.status ? ` <span class="tone-muted">(${escHtml(s.status)})</span>` : '';
        return `<span class="tone-accent">${escHtml(s.origin || '?')} \u2192 ${escHtml(s.destination || '?')}</span>${code}${plaus}`;
      }
      if (s.status) return `<span class="tone-muted">HTTP ${escHtml(s.status)}, no data</span>`;
      return '<span class="tone-muted">\u2014</span>';
    };
    stepsHtml = `<div><p class="steps-title">API steps</p><div class="steps">${defs.map(def => _step(def.label, def.desc, body(def))).join('')}</div></div>`;
  }

  const airborne = d.airborne
    ? `<span class="badge badge-ok">Airborne${d.altitude ? ` \u00b7 ${_fmtInt(d.altitude)} ft` : ''}</span>`
    : '<span class="badge badge-muted">Not airborne</span>';
  const mode = isCacheMode ? '<span class="badge badge-accent">Cached</span>' : '<span class="badge badge-danger">Live only</span>';
  const airline = airlineName(d.callsign);

  // display_expires (epoch) gives the true remaining time after API latency; fall back to
  // display_seconds when it's absent.
  const secs = d.display_expires
    ? Math.max(1, d.display_expires - Math.floor(Date.now() / 1000))
    : (d.display_seconds || 30);
  const displayLine = d.display_injected
    ? `<div class="countdown" id="test-countdown">${icon('clock')}<span>On the display for <b id="test-countdown-num">${secs}</b>s</span></div>`
    : '<div class="led-meta">Not sent to the display</div>';

  el.innerHTML = `
    <div class="tr-head">
      <span class="tr-callsign">${escHtml(d.callsign)}</span>
      ${d.tail ? `<span class="mono muted">${escHtml(d.tail)}</span>` : ''}
      ${d.hex_code ? `<code>${escHtml(d.hex_code)}</code>` : ''}
      ${airborne}${mode}
    </div>
    ${airline ? `<p class="tr-sub">${escHtml(airline)}</p>` : ''}
    ${ovHtml}
    ${stepsHtml}
    <div class="led-panel">
      <div class="led-label">On the display</div>
      <div class="led-route">${escHtml(d.final_origin || '?')} \u2192 ${escHtml(d.final_destination || '?')}</div>
      <div class="led-plane">${escHtml(d.final_plane || 'no type data')}</div>
      <div class="led-meta">Route from <code>${escHtml(d.route_source)}</code> \u00b7 type from <code>${escHtml(d.type_source)}</code></div>
      ${displayLine}
    </div>`;
  el.hidden = false;
  if (d.display_injected) startTestCountdown(secs);
}

// Counts down from an absolute end time, so a throttled background tab can't drift.
function startTestCountdown(secs) {
  clearInterval(_testCountdownTimer);
  const end = Date.now() + secs * 1000;
  _testCountdownTimer = setInterval(() => {
    const rem = Math.max(0, Math.ceil((end - Date.now()) / 1000));
    const numEl = $('test-countdown-num');
    if (numEl) numEl.textContent = rem;
    if (rem <= 0) {
      clearInterval(_testCountdownTimer);
      const cd = $('test-countdown');
      if (cd) { cd.innerHTML = `${icon('clock')}<span>Display time elapsed</span>`; cd.classList.add('is-done'); }
      $('test-clear-btn').hidden = true;
    }
  }, 1000);
}

async function clearTestDisplay() {
  clearInterval(_testCountdownTimer);
  try { await fetch('/api/test_flight', { method: 'DELETE' }); } catch (e) { /* best effort */ }
  $('test-clear-btn').hidden = true;
  const cd = $('test-countdown');
  if (cd) { cd.innerHTML = `${icon('check')}<span>Cleared from the display</span>`; cd.classList.add('is-done'); }
  showToast('Test flight cleared from the display', 'ok');
}

async function resetTestFlight() {
  clearInterval(_testCountdownTimer);
  try { await fetch('/api/test_flight', { method: 'DELETE' }); } catch (e) { /* best effort */ }
  $('test-callsign').value = '';
  $('test-result').hidden = true;
  $('test-result').innerHTML = '';
  $('test-clear-btn').hidden = true;
  $('test-reset-btn').hidden = true;
}

// ── Scoreboard league priority: drag (desktop) or arrow buttons (everywhere) ───────────
function _syncSbMoveButtons() {
  const rows = [...document.querySelectorAll('.sb-table tbody tr')];
  rows.forEach((r, i) => {
    const up = r.querySelector('[data-dir="-1"]');
    const down = r.querySelector('[data-dir="1"]');
    if (up) up.disabled = i === 0;
    if (down) down.disabled = i === rows.length - 1;
  });
}

function _moveSbRow(btn, dir) {
  const tr = btn.closest('tr');
  if (!tr) return;
  const sib = dir < 0 ? tr.previousElementSibling : tr.nextElementSibling;
  if (!sib) return;
  if (dir < 0) tr.parentNode.insertBefore(tr, sib); else sib.after(tr);
  _syncSbMoveButtons();
  _refreshDirty();
  if (!btn.disabled) btn.focus();
}

function initSbDragDrop() {
  const tbody = document.querySelector('.sb-table tbody');
  if (!tbody) return;
  let dragSrc = null;
  tbody.addEventListener('dragstart', e => {
    dragSrc = e.target.closest('tr');
    if (!dragSrc) return;
    dragSrc.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragSrc.dataset.league);
  });
  tbody.addEventListener('dragend', () => {
    tbody.querySelectorAll('tr').forEach(r => r.classList.remove('dragging', 'drag-over'));
    dragSrc = null;
  });
  tbody.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.target.closest('tr');
    if (target && target !== dragSrc) {
      tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-over'));
      target.classList.add('drag-over');
    }
  });
  tbody.addEventListener('dragleave', e => {
    // Clear only when leaving the tbody entirely, not when moving between cells.
    if (!tbody.contains(e.relatedTarget)) tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-over'));
  });
  tbody.addEventListener('drop', e => {
    e.preventDefault();
    const target = e.target.closest('tr');
    if (!target || !dragSrc || target === dragSrc) return;
    // Insert before or after the target based on its vertical midpoint.
    const rect = target.getBoundingClientRect();
    if (e.clientY < rect.top + rect.height / 2) tbody.insertBefore(dragSrc, target);
    else target.after(dragSrc);
    tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-over'));
    _syncSbMoveButtons();
    _refreshDirty();
  });
}

// ── Event wiring ───────────────────────────────────────────────────────────────────────
// Controls declare data-action / data-submit attributes; one delegated listener per event
// type maps them to the functions above (no inline JS in the markup).
const _ACTIONS = {
  'tab':             el => showTab(el.dataset.tab),
  'theme':           () => toggleTheme(),
  'controls':        () => toggleControls(),
  'toggle-display':  () => toggleDisplay(),
  'toggle-night':    () => toggleNight(),
  'toggle-service':  () => toggleService(),
  'toggle-apis':     () => toggleAPIs(),
  'reload-config':   () => loadConfig(),
  'jump':            el => _jumpToSection(el.dataset.target),
  'unit':            el => setAltUnit(el.dataset.unit),
  'eye':             el => toggleEye(el.dataset.target, el),
  'hide-game':       () => toggleHideGame(),
  'sb-move':         el => _moveSbRow(el, Number(el.dataset.dir)),
  'save':            () => saveConfig(false),
  'save-restart':    () => saveConfig(true),
  'map-fit':         () => _fitZone(),
  'map-save':        () => saveFromMap(),
  'map-discard':     () => discardMapChanges(),
  'adjust':          el => toggleAdjust(el.dataset.api),
  'api-toggle':      el => toggleApi(el.dataset.api),
  'api-clear-cache': el => clearApiCache(el.dataset.api),
  'period':          el => quickPeriod(el.dataset.preset),
  'search-clear':    () => clearStatsSearch(),
  'test-clear':      () => clearTestDisplay(),
  'test-reset':      () => resetTestFlight(),
  'rule-edit':       el => editRule(Number(el.dataset.idx)),
  'rule-delete':     el => deleteRule(Number(el.dataset.idx)),
  'rule-save':       el => saveEditRule(Number(el.dataset.idx)),
  'rule-cancel':     () => cancelEdit(),
  'rule-move':       el => moveRule(Number(el.dataset.idx), Number(el.dataset.dir)),
  'log-clear':       () => clearLog(),
};

const _SUBMITS = {
  'adjust':            f => saveAdjust(f.dataset.api),
  'clear-cache-entry': () => clearCacheEntry(),
  'period-apply':      () => applyStatsPeriod(),
  'stats-search':      () => doStatsSearch(),
  'test-flight':       () => runTestFlight(),
  'add-rule':          () => addRule(),
};

document.addEventListener('click', e => {
  const t = e.target;
  // Clicking anywhere outside the open display-controls popover closes it.
  const box = $('device-controls');
  if (box && box.classList.contains('is-open') && t && t.closest
      && !t.closest('#device-controls') && !t.closest('#controls-btn')) {
    toggleControls(false);
  }
  const el = t && t.closest ? t.closest('[data-action]') : null;
  if (!el || el.disabled) return;
  const fn = _ACTIONS[el.dataset.action];
  if (fn) { e.preventDefault(); fn(el, e); }
});

document.addEventListener('submit', e => {
  const form = e.target;
  if (!form || !form.dataset) return;
  e.preventDefault();   // nothing on this page does a full-page form post
  const fn = _SUBMITS[form.dataset.submit];
  if (fn) fn(form, e);
});

document.addEventListener('change', e => {
  const t = e.target;
  if (!t || !t.dataset) return;
  if (t.dataset.change === 'period-dirty') onPeriodChange();
  if (t.id === 'scoreboard_enabled') toggleScoreboardBody();
});

document.addEventListener('input', e => {
  const t = e.target;
  if (t && t.dataset && t.dataset.input === 'log-filter') _onLogFilter(t.value);
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') toggleControls(false);
  const t = e.target;
  if (t && t.classList && t.classList.contains('rule-edit-input')) {
    if (e.key === 'Enter') { e.preventDefault(); saveEditRule(Number(t.dataset.idx)); }
    else if (e.key === 'Escape') cancelEdit();
  }
});

// Pause the polling while the page is hidden (saves phone battery and Pi load for a
// dashboard left open 24/7); resume, and re-sync the header, when it comes back.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopLog();
    stopAPIsTab();
    stopScoreboardPoll();
    _stopStatsRefresh();
    clearTimeout(_statusTimer);
    _statusTimer = null;
  } else {
    startScoreboardPoll();
    checkInitialStatus(1);
    if (_activeTab === 'apis') initAPIsTab();
    else if (_activeTab === 'log') initLog();
    else if (_activeTab === 'stats') { loadStatsTab({ quiet: true }); _startStatsRefresh(); }
  }
});

// ── Init ───────────────────────────────────────────────────────────────────────────────
function _init() {
  _restoreAltUnit();
  _syncThemeUi();
  _initConfigForm();
  initSbDragDrop();
  _initRulesDnD();
  loadConfig();
  checkInitialStatus();
  startScoreboardPoll();
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('scroll', _onWindowScroll, { passive: true });
    window.addEventListener('hashchange', () => showTab(_tabFromHash()));
    window.addEventListener('beforeunload', e => {
      if (!_configDirty) return;
      e.preventDefault();
      e.returnValue = '';
    });
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      if (mq && mq.addEventListener) mq.addEventListener('change', _syncThemeUi);
    }
  }
  showTab(_tabFromHash());   // also loads the home tab's data
}

_init();
