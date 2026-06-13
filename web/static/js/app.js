// CSRF: tag every same-origin request so the server can reject cross-site drive-by POSTs.
// (The server requires this header on all state-changing requests — see _csrf_guard.)
(function () {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    init.headers = Object.assign({}, init.headers, { 'X-Requested-With': 'FlightTracker' });
    return _origFetch(input, init);
  };
})();

// ── Constants ──────────────────────────────────────────────────
const FT_TO_M  = 0.3048;
const FT_TO_KM = 0.0003048;
const KM_TO_FT = 3280.84;
const KM_TO_M  = 1000;

// ── State ──────────────────────────────────────────────────────
let config = {};
let altUnit = 'ft';
let displayOn = true;
let map, zoneRect, homeMarker, cornerMarkers = [];
let evtSource = null;

// ── Tabs ───────────────────────────────────────────────────────
// Group all N-number registrations (private/GA) into one combined row
function _groupNReg(items) {
  const nItems = (items || []).filter(i => /^N\d/.test(i.prefix));
  const rest   = (items || []).filter(i => !/^N\d/.test(i.prefix));
  if (!nItems.length) return rest;
  const nTotal = nItems.reduce((sum, i) => sum + i.count, 0);
  const nEntry = { prefix: 'N-reg', name: 'Private / GA', count: nTotal };
  const idx    = rest.findIndex(i => i.count < nTotal);
  return idx === -1 ? [...rest, nEntry] : [...rest.slice(0, idx), nEntry, ...rest.slice(idx)];
}

function toggleHamburger() {
  const nav = document.querySelector('nav');
  const btn = document.getElementById('hamburger');
  nav.classList.toggle('open');
  btn.classList.toggle('open', nav.classList.contains('open'));
}

function closeHamburger() {
  document.querySelector('nav').classList.remove('open');
  document.getElementById('hamburger').classList.remove('open');
}

function showTab(name, btn) {
  closeHamburger();
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(name + '-tab').classList.add('active');
  btn.classList.add('active');
  if (name === 'map')     initMap();
  if (name === 'log')     initLog();     else stopLog();
  if (name === 'apis')    initAPIsTab(); else stopAPIsTab();
  if (name === 'stats')   loadStatsTab();
  if (name === 'rules')   initRulesTab();
}

// Pause the heavy polling timers when the tab/screen is hidden (saves phone battery and
// Pi load for a 24/7-open dashboard); resume the active tab's poller when it returns.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopAPIsTab(); stopLog(); }
  else {
    const active = document.querySelector('.tab-content.active');
    const tab = active ? active.id.replace('-tab', '') : '';
    if (tab === 'apis') initAPIsTab();
    else if (tab === 'log') initLog();
  }
});

function goToTab(name) {
  const btn = [...document.querySelectorAll('nav button')].find(b => (b.getAttribute('onclick') || '').includes("'" + name + "'"));
  if (btn) btn.click();
}

// Close hamburger menu when clicking outside of it
document.addEventListener('click', e => {
  const nav = document.querySelector('nav');
  const hbtn = document.getElementById('hamburger');
  if (nav.classList.contains('open') && !nav.contains(e.target) && e.target !== hbtn) {
    closeHamburger();
  }
});

// ── Combined status fetch on load ──────────────────────────────
async function checkInitialStatus(retries = 3) {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateServiceBtn(d.running);
    updateDisplayBtn(!d.paused);
    updateNightBtn(d.night);
    updateAPIsBtn(!d.apis_disabled);
  } catch(e) {
    if (retries > 0) {
      // Server may be mid-restart — retry a few times before giving up
      setTimeout(() => checkInitialStatus(retries - 1), 1500);
      return;  // don't show toggles yet; show them after a successful retry
    }
    // Exhausted retries — the Pi is still unreachable. Don't reveal toggles in their HTML
    // default state (which would assert a control state we never confirmed); keep waiting and
    // slowly re-poll so the UI converges once the Pi comes back.
    setTimeout(() => checkInitialStatus(3), 10000);
    return;
  }
  // Only reach here on a successful status read — every toggle now reflects real state.
  document.getElementById('display-toggle').style.removeProperty('display');
  document.getElementById('night-toggle').style.removeProperty('display');
  document.getElementById('service-toggle').style.removeProperty('display');
  document.getElementById('apis-toggle').style.removeProperty('display');
}

async function checkDisplayStatus() {
  try {
    const r = await fetch('/api/display');
    const d = await r.json();
    updateDisplayBtn(!d.paused);
  } catch(e) {}
}

function updateDisplayBtn(on) {
  displayOn = on;
  const el = document.getElementById('display-toggle');
  const lbl = document.getElementById('display-label');
  el.classList.toggle('off', !on);
  lbl.textContent = on ? 'Display On' : 'Display Off';
}

async function toggleDisplay() {
  const el = document.getElementById('display-toggle');
  el.style.pointerEvents = 'none';
  const endpoint = displayOn ? '/api/display/off' : '/api/display/on';
  try {
    const r = await fetch(endpoint, { method: 'POST' });
    const d = await r.json();
    if (d.ok) updateDisplayBtn(!displayOn);
  } catch(e) {}
  el.style.pointerEvents = '';
}

// ── Service Toggle ─────────────────────────────────────────────
let serviceRunning = true;

function updateServiceBtn(running) {
  serviceRunning = running;
  const el = document.getElementById('service-toggle');
  const lbl = document.getElementById('service-label');
  el.classList.toggle('off', !running);
  lbl.textContent = running ? 'Service On' : 'Service Off';
  // Grey out display/night/FA toggles when service is not running
  document.getElementById('display-toggle').classList.toggle('disabled', !running);
  document.getElementById('night-toggle').classList.toggle('disabled', !running);
  document.getElementById('apis-toggle').classList.toggle('disabled', !running);
}

async function toggleService() {
  const el = document.getElementById('service-toggle');
  el.style.pointerEvents = 'none';
  const wasStopping = serviceRunning;
  const endpoint = serviceRunning ? '/api/service/stop' : '/api/service/start';
  try {
    const r = await fetch(endpoint, { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      updateServiceBtn(!wasStopping);
      if (wasStopping) {
        // Service just stopped — display is implicitly off
        updateDisplayBtn(false);
      } else {
        // Service just started — re-fetch actual display state
        await checkDisplayStatus();
      }
      // Re-sync all toggle states after a short delay — the service needs a
      // moment to fully start/stop before systemctl reports the right status.
      setTimeout(checkInitialStatus, 1500);
    } else {
      showToast('✗ ' + (d.error || 'Service error'), 'err');
    }
  } catch(e) { showToast('✗ Request failed', 'err'); }
  el.style.pointerEvents = '';
}

// ── Limited APIs Toggle (AirLabs + FlightAware) ────────────────
let apisEnabled = true;

function updateAPIsBtn(enabled) {
  apisEnabled = enabled;
  const el = document.getElementById('apis-toggle');
  const lbl = document.getElementById('apis-label');
  el.classList.toggle('off', !enabled);
  lbl.textContent = enabled ? 'APIs On' : 'APIs Off';
}

async function toggleAPIs() {
  const el = document.getElementById('apis-toggle');
  el.style.pointerEvents = 'none';
  try {
    const r = await fetch('/api/apis/toggle', { method: 'POST' });
    const d = await r.json();
    if (d.ok) updateAPIsBtn(!d.apis_disabled);
    else showToast('✗ ' + (d.error || 'API toggle failed'), 'err');
  } catch(e) { showToast('✗ Request failed', 'err'); }
  el.style.pointerEvents = '';
}

// ── Night Mode ─────────────────────────────────────────────────
let nightActive = false;

function updateNightBtn(active) {
  nightActive = active;
  const el = document.getElementById('night-toggle');
  const lbl = document.getElementById('night-label');
  el.classList.toggle('off', !active);
  lbl.textContent = active ? 'Night On' : 'Night Off';
}

async function toggleNight() {
  const el = document.getElementById('night-toggle');
  el.style.pointerEvents = 'none';
  try {
    const r = await fetch('/api/display/night', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      updateNightBtn(d.night);
      setTimeout(checkInitialStatus, 1500);  // re-sync all toggle states
    }
  } catch(e) {}
  el.style.pointerEvents = '';
}

// ── Unit Conversion ────────────────────────────────────────────
function ftToUnit(ft, unit) {
  if (unit === 'm')  return Math.round(ft * FT_TO_M);
  if (unit === 'km') return +(ft * FT_TO_KM).toFixed(3);
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
  return +km.toFixed(5);
}
function unitToKm(val, unit) {
  if (unit === 'ft') return val / KM_TO_FT;
  if (unit === 'm')  return val / KM_TO_M;
  return parseFloat(val);
}
function setAltUnit(unit, btn) {
  const minFt  = unitToFt(parseFloat(document.getElementById('min_altitude').value) || 0, altUnit);
  const maxFt  = unitToFt(parseFloat(document.getElementById('max_altitude').value) || 0, altUnit);
  const altKm  = unitToKm(parseFloat(document.getElementById('loc_alt').value) || 0, altUnit);
  altUnit = unit;
  document.getElementById('min_altitude').value = ftToUnit(minFt, unit);
  document.getElementById('max_altitude').value = ftToUnit(maxFt, unit);
  document.getElementById('loc_alt').value = kmToUnit(altKm, unit);
  document.getElementById('loc-alt-label').textContent = `Antenna Altitude (${unit} ASL)`;
  document.querySelectorAll('.unit-label').forEach(el => el.textContent = unit);
  document.querySelectorAll('.unit-btn').forEach(b => {
    b.classList.toggle('active', b.textContent.trim() === unit);
  });
}

// ── Toast ──────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, type = 'ok') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  clearTimeout(toastTimer);
  requestAnimationFrame(() => { t.classList.add('show'); });
  toastTimer = setTimeout(() => { t.classList.remove('show'); }, 2500);
}

// ── Eye Toggle ─────────────────────────────────────────────────
// Password-field id -> the config key it holds (for on-demand secret reveal).
const _SECRET_FIELDS = {
  ow_key: 'OPENWEATHER_API_KEY', airlabs_key: 'AIRLABS_API_KEY',
  airlabs_key2: 'AIRLABS_API_KEY_2', fa_key: 'FLIGHTAWARE_API_KEY',
  opensky_secret: 'OPENSKY_CLIENT_SECRET',
};
const _SECRET_SENTINEL = '********';

async function toggleEye(id, btn) {
  const inp = document.getElementById(id);
  const revealing = inp.type === 'password';
  // Secrets are masked by default and not shipped in the page; fetch the real value on
  // demand the first time the eye is opened while the field still holds the mask.
  if (revealing && _SECRET_FIELDS[id] && inp.value === _SECRET_SENTINEL) {
    try {
      const r = await fetch('/api/config/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: _SECRET_FIELDS[id] }),
      });
      const d = await r.json();
      if (d && typeof d.value === 'string') inp.value = d.value;
    } catch (e) { /* leave the mask in place if the reveal fetch fails */ }
  }
  inp.type = revealing ? 'text' : 'password';
  btn.style.opacity = inp.type === 'text' ? '1' : '0.5';
}

// ── Config ─────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    config = await r.json();
    if (!config.ZONE_HOME || !Array.isArray(config.LOCATION_HOME)) throw new Error('Unexpected config shape');
  } catch(e) {
    showToast('Failed to load config: ' + e.message, 'err');
    return;
  }
  populateForm();
}

// Mirror the in-memory `config` into every Config-tab input.  Called after the initial
// load AND after a successful save, so the form always reflects the values that were
// actually persisted (numeric fields that buildConfigPayload clamped/defaulted no longer
// silently differ from what's on screen).
function populateForm() {
  document.getElementById('zone_tl_y').value = config.ZONE_HOME.tl_y;
  document.getElementById('zone_tl_x').value = config.ZONE_HOME.tl_x;
  document.getElementById('zone_br_y').value = config.ZONE_HOME.br_y;
  document.getElementById('zone_br_x').value = config.ZONE_HOME.br_x;
  document.getElementById('loc_lat').value = config.LOCATION_HOME[0];
  document.getElementById('loc_lon').value = config.LOCATION_HOME[1];
  document.getElementById('loc_alt').value = kmToUnit(config.LOCATION_HOME[2], altUnit);
  document.getElementById('timezone').value = config.TIMEZONE || 'America/Los_Angeles';
  document.getElementById('min_altitude').value = ftToUnit(config.MIN_ALTITUDE, altUnit);
  document.getElementById('max_altitude').value = ftToUnit(config.MAX_ALTITUDE, altUnit);
  document.getElementById('local_airports').value = config.LOCAL_AIRPORTS || config.LOCAL_AIRPORT || '';
  document.getElementById('brightness').value = config.BRIGHTNESS;
  document.getElementById('night_brightness').value = config.NIGHT_BRIGHTNESS ?? 20;
  document.getElementById('gpio_slowdown').value = config.GPIO_SLOWDOWN;
  document.getElementById('journey_code').value = config.JOURNEY_CODE_SELECTED || '';
  document.getElementById('journey_blank').value = config.JOURNEY_BLANK_FILLER || '';
  document.getElementById('hat_pwm').checked = config.HAT_PWM_ENABLED;
  document.getElementById('loading_led_enabled').checked = config.LOADING_LED_ENABLED || false;
  document.getElementById('loading_led_gpio_pin').value = config.LOADING_LED_GPIO_PIN ?? 25;
  document.getElementById('weather_location').value = config.WEATHER_LOCATION || '';
  document.getElementById('temp_units').value = config.TEMPERATURE_UNITS || 'imperial';
  document.getElementById('ow_key').value = config.OPENWEATHER_API_KEY || '';
  document.getElementById('rainfall_enabled').checked = config.RAINFALL_ENABLED || false;
  // Scoreboard master switch — default false for new installs
  const _sbMasterEnabled = config.SCOREBOARD_ENABLED !== undefined
    ? config.SCOREBOARD_ENABLED
    : false;
  document.getElementById('scoreboard_enabled').checked = _sbMasterEnabled;
  toggleScoreboardBody();
  // Per-sport scoreboard settings
  // Backward-compat: old SCOREBOARD_ENABLED / SCOREBOARD_TEAM_ID / SCOREBOARD_TEAM_NAME map to NHL
  const _sbNhlEnabled = config.SCOREBOARD_NHL_ENABLED !== undefined ? config.SCOREBOARD_NHL_ENABLED
                      : (config.SCOREBOARD_ENABLED !== undefined ? config.SCOREBOARD_ENABLED : true);
  const _sbNhlId   = config.SCOREBOARD_NHL_TEAM_ID   ?? config.SCOREBOARD_TEAM_ID   ?? '';
  const _sbNhlName = (config.SCOREBOARD_NHL_TEAM_NAME ?? config.SCOREBOARD_TEAM_NAME ?? '').toString().toUpperCase();
  document.getElementById('sb_nhl_enabled').checked    = _sbNhlEnabled;
  document.getElementById('sb_nhl_team_id').value      = _sbNhlId;
  document.getElementById('sb_nhl_team_name').value    = _sbNhlName;
  document.getElementById('sb_nfl_enabled').checked    = config.SCOREBOARD_NFL_ENABLED || false;
  document.getElementById('sb_nfl_team_id').value      = config.SCOREBOARD_NFL_TEAM_ID ?? '';
  document.getElementById('sb_nfl_team_name').value    = (config.SCOREBOARD_NFL_TEAM_NAME || '').toUpperCase();
  document.getElementById('sb_mlb_enabled').checked    = config.SCOREBOARD_MLB_ENABLED || false;
  document.getElementById('sb_mlb_team_id').value      = config.SCOREBOARD_MLB_TEAM_ID ?? '';
  document.getElementById('sb_mlb_team_name').value    = (config.SCOREBOARD_MLB_TEAM_NAME || '').toUpperCase();
  document.getElementById('sb_nba_enabled').checked    = config.SCOREBOARD_NBA_ENABLED || false;
  document.getElementById('sb_nba_team_id').value      = config.SCOREBOARD_NBA_TEAM_ID ?? '';
  document.getElementById('sb_nba_team_name').value    = (config.SCOREBOARD_NBA_TEAM_NAME || '').toUpperCase();
  document.getElementById('sb_wnba_enabled').checked   = config.SCOREBOARD_WNBA_ENABLED || false;
  document.getElementById('sb_wnba_team_id').value     = config.SCOREBOARD_WNBA_TEAM_ID ?? '';
  document.getElementById('sb_wnba_team_name').value   = (config.SCOREBOARD_WNBA_TEAM_NAME || '').toUpperCase();
  document.getElementById('sb_mls_enabled').checked    = config.SCOREBOARD_MLS_ENABLED || false;
  document.getElementById('sb_mls_team_id').value      = config.SCOREBOARD_MLS_TEAM_ID ?? '';
  document.getElementById('sb_mls_team_name').value    = (config.SCOREBOARD_MLS_TEAM_NAME || '').toUpperCase();
  document.getElementById('sb_fifa_enabled').checked   = config.SCOREBOARD_FIFA_ENABLED || false;
  document.getElementById('sb_fifa_team_id').value     = config.SCOREBOARD_FIFA_TEAM_ID ?? '';
  document.getElementById('sb_fifa_team_name').value   = (config.SCOREBOARD_FIFA_TEAM_NAME || '').toUpperCase();
  // Reorder table rows to match saved priority
  const _sbPriority = Array.isArray(config.SCOREBOARD_PRIORITY)
    ? config.SCOREBOARD_PRIORITY
    : ['NHL', 'NFL', 'MLB', 'NBA', 'WNBA', 'MLS', 'FIFA'];
  const _sbTbody = document.querySelector('.sb-table tbody');
  _sbPriority.forEach(league => {
    const row = _sbTbody.querySelector(`tr[data-league="${league}"]`);
    if (row) _sbTbody.appendChild(row);
  });
  document.getElementById('scoreboard_post_game_minutes').value = config.SCOREBOARD_POST_GAME_MINUTES ?? 30;
  document.getElementById('scoreboard_goal_celebration_seconds').value = config.SCOREBOARD_GOAL_CELEBRATION_SECONDS ?? 30;
  document.getElementById('receiver_host').value = config.RECEIVER_HOST || '';
  document.getElementById('receiver_type').value = config.RECEIVER_TYPE || 'dump1090';
  document.getElementById('poll_interval').value = config.POLL_INTERVAL ?? 15;
  document.getElementById('data_check_interval').value = config.DATA_CHECK_INTERVAL ?? 2;
  document.getElementById('date_format').value = config.DATE_FORMAT || 'MDY';
  document.getElementById('airlabs_key').value  = config.AIRLABS_API_KEY   || '';
  document.getElementById('airlabs_key2').value = config.AIRLABS_API_KEY_2 || '';
  document.getElementById('fa_key').value = config.FLIGHTAWARE_API_KEY || '';
  document.getElementById('opensky_id').value = config.OPENSKY_CLIENT_ID || '';
  document.getElementById('opensky_secret').value = config.OPENSKY_CLIENT_SECRET || '';
  document.getElementById('feeder_monthly_credit').value = config.FEEDER_MONTHLY_CREDIT ?? 10.00;
  document.getElementById('airlabs_monthly_limit').value  = config.AIRLABS_MONTHLY_LIMIT  ?? 1000;
  document.getElementById('airlabs_reset_day').value      = config.AIRLABS_RESET_DAY      ?? 9;
  document.getElementById('airlabs2_monthly_limit').value = config.AIRLABS2_MONTHLY_LIMIT ?? 1000;
  document.getElementById('airlabs2_reset_day').value     = config.AIRLABS2_RESET_DAY     ?? 9;
  document.getElementById('aeroapi_reset_day').value = config.AEROAPI_RESET_DAY ?? 1;
  document.getElementById('adsbdb_cache_ttl').value = config.ADSBDB_CACHE_TTL ?? 3600;
  document.getElementById('opensky_cache_ttl').value = config.OPENSKY_CACHE_TTL ?? 3600;
  document.getElementById('route_ttl_scheduled').value = config.ROUTE_TTL_SCHEDULED ?? 604800;
  document.getElementById('route_ttl_default').value = config.ROUTE_TTL_DEFAULT ?? 3600;
  document.getElementById('route_miss_ttl').value = config.ROUTE_MISS_TTL ?? 300;
  document.getElementById('route_paid_miss_ttl').value = config.ROUTE_PAID_MISS_TTL ?? 7200;
}

function buildConfigPayload() {
  return {
    ZONE_HOME: {
      tl_y: parseFloat(document.getElementById('zone_tl_y').value),
      tl_x: parseFloat(document.getElementById('zone_tl_x').value),
      br_y: parseFloat(document.getElementById('zone_br_y').value),
      br_x: parseFloat(document.getElementById('zone_br_x').value),
    },
    LOCATION_HOME: [
      parseFloat(document.getElementById('loc_lat').value),
      parseFloat(document.getElementById('loc_lon').value),
      unitToKm(parseFloat(document.getElementById('loc_alt').value), altUnit),
    ],
    MIN_ALTITUDE: unitToFt(parseFloat(document.getElementById('min_altitude').value), altUnit),
    MAX_ALTITUDE: unitToFt(parseFloat(document.getElementById('max_altitude').value), altUnit),
    LOCAL_AIRPORTS: document.getElementById('local_airports').value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean).join(','),
    BRIGHTNESS: parseInt(document.getElementById('brightness').value),
    NIGHT_BRIGHTNESS: parseInt(document.getElementById('night_brightness').value),
    GPIO_SLOWDOWN: parseInt(document.getElementById('gpio_slowdown').value),
    JOURNEY_CODE_SELECTED: document.getElementById('journey_code').value,
    JOURNEY_BLANK_FILLER: document.getElementById('journey_blank').value,
    HAT_PWM_ENABLED: document.getElementById('hat_pwm').checked,
    LOADING_LED_ENABLED: document.getElementById('loading_led_enabled').checked,
    LOADING_LED_GPIO_PIN: parseInt(document.getElementById('loading_led_gpio_pin').value) || 25,
    WEATHER_LOCATION: document.getElementById('weather_location').value,
    TEMPERATURE_UNITS: document.getElementById('temp_units').value,
    OPENWEATHER_API_KEY: document.getElementById('ow_key').value,
    RAINFALL_ENABLED: document.getElementById('rainfall_enabled').checked,
    SCOREBOARD_ENABLED: document.getElementById('scoreboard_enabled').checked,
    // Priority derived from current DOM row order (drag-to-reorder)
    SCOREBOARD_PRIORITY: [...document.querySelectorAll('.sb-table tbody tr[data-league]')].map(r => r.dataset.league),
    SCOREBOARD_NHL_ENABLED:  document.getElementById('sb_nhl_enabled').checked,
    SCOREBOARD_NHL_TEAM_ID:  parseInt(document.getElementById('sb_nhl_team_id').value)  || 0,
    SCOREBOARD_NHL_TEAM_NAME: (document.getElementById('sb_nhl_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_NFL_ENABLED:  document.getElementById('sb_nfl_enabled').checked,
    SCOREBOARD_NFL_TEAM_ID:  parseInt(document.getElementById('sb_nfl_team_id').value)  || 0,
    SCOREBOARD_NFL_TEAM_NAME: (document.getElementById('sb_nfl_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_MLB_ENABLED:  document.getElementById('sb_mlb_enabled').checked,
    SCOREBOARD_MLB_TEAM_ID:  parseInt(document.getElementById('sb_mlb_team_id').value)  || 0,
    SCOREBOARD_MLB_TEAM_NAME: (document.getElementById('sb_mlb_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_NBA_ENABLED:  document.getElementById('sb_nba_enabled').checked,
    SCOREBOARD_NBA_TEAM_ID:  parseInt(document.getElementById('sb_nba_team_id').value)  || 0,
    SCOREBOARD_NBA_TEAM_NAME: (document.getElementById('sb_nba_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_WNBA_ENABLED:  document.getElementById('sb_wnba_enabled').checked,
    SCOREBOARD_WNBA_TEAM_ID:  parseInt(document.getElementById('sb_wnba_team_id').value)  || 0,
    SCOREBOARD_WNBA_TEAM_NAME: (document.getElementById('sb_wnba_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_MLS_ENABLED:  document.getElementById('sb_mls_enabled').checked,
    SCOREBOARD_MLS_TEAM_ID:  parseInt(document.getElementById('sb_mls_team_id').value)  || 0,
    SCOREBOARD_MLS_TEAM_NAME: (document.getElementById('sb_mls_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_FIFA_ENABLED:  document.getElementById('sb_fifa_enabled').checked,
    SCOREBOARD_FIFA_TEAM_ID:  parseInt(document.getElementById('sb_fifa_team_id').value)  || 0,
    SCOREBOARD_FIFA_TEAM_NAME: (document.getElementById('sb_fifa_team_name').value || '').toUpperCase().slice(0, 4),
    SCOREBOARD_POST_GAME_MINUTES: parseInt(document.getElementById('scoreboard_post_game_minutes').value) || 30,
    SCOREBOARD_GOAL_CELEBRATION_SECONDS: parseInt(document.getElementById('scoreboard_goal_celebration_seconds').value) || 30,
    TIMEZONE: document.getElementById('timezone').value.trim(),
    RECEIVER_HOST: document.getElementById('receiver_host').value.trim(),
    RECEIVER_TYPE: document.getElementById('receiver_type').value,
    POLL_INTERVAL: parseInt(document.getElementById('poll_interval').value) || 15,
    DATA_CHECK_INTERVAL: parseInt(document.getElementById('data_check_interval').value) || 2,
    DATE_FORMAT: document.getElementById('date_format').value,
    AIRLABS_API_KEY:   document.getElementById('airlabs_key').value,
    AIRLABS_API_KEY_2: document.getElementById('airlabs_key2').value,
    FLIGHTAWARE_API_KEY: document.getElementById('fa_key').value,
    OPENSKY_CLIENT_ID: document.getElementById('opensky_id').value,
    OPENSKY_CLIENT_SECRET: document.getElementById('opensky_secret').value,
    FEEDER_MONTHLY_CREDIT: (v => isNaN(v) ? 10.00 : v)(parseFloat(document.getElementById('feeder_monthly_credit').value)),
    AIRLABS_MONTHLY_LIMIT:  parseInt(document.getElementById('airlabs_monthly_limit').value)  || 1000,
    AIRLABS_RESET_DAY:      parseInt(document.getElementById('airlabs_reset_day').value)      || 9,
    AIRLABS2_MONTHLY_LIMIT: parseInt(document.getElementById('airlabs2_monthly_limit').value) || 1000,
    AIRLABS2_RESET_DAY:     parseInt(document.getElementById('airlabs2_reset_day').value)     || 9,
    AEROAPI_RESET_DAY: parseInt(document.getElementById('aeroapi_reset_day').value) || 1,
    ADSBDB_CACHE_TTL:    (v=>isNaN(v)||v<60?3600:v)(parseInt(document.getElementById('adsbdb_cache_ttl').value)),
    OPENSKY_CACHE_TTL:   (v=>isNaN(v)||v<60?3600:v)(parseInt(document.getElementById('opensky_cache_ttl').value)),
    ROUTE_TTL_SCHEDULED: (v=>isNaN(v)||v<60?604800:v)(parseInt(document.getElementById('route_ttl_scheduled').value)),
    ROUTE_TTL_DEFAULT:   (v=>isNaN(v)||v<60?3600:v)(parseInt(document.getElementById('route_ttl_default').value)),
    ROUTE_MISS_TTL:      (v=>isNaN(v)||v<30?300:v)(parseInt(document.getElementById('route_miss_ttl').value)),
    ROUTE_PAID_MISS_TTL: (v=>isNaN(v)||v<60?7200:v)(parseInt(document.getElementById('route_paid_miss_ttl').value)),
  };
}

async function saveConfig(restart) {
  const status = document.getElementById('save-status');
  status.textContent = 'Saving…'; status.className = 'save-status';
  try {
    const payload = buildConfigPayload();
    const numericKeys = ['MIN_ALTITUDE','MAX_ALTITUDE','BRIGHTNESS','NIGHT_BRIGHTNESS','GPIO_SLOWDOWN'];
    const locationKeys = payload.LOCATION_HOME;
    const zoneKeys = Object.values(payload.ZONE_HOME);
    if (numericKeys.some(k => isNaN(payload[k])) ||
        locationKeys.some(v => isNaN(v)) ||
        zoneKeys.some(v => isNaN(v))) {
      const msg = '✗ Invalid numeric value — check all fields';
      status.textContent = msg; status.className = 'save-status err';
      showToast(msg, 'err');
      return;
    }
    payload.restart = restart;
    const r = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await r.json();
    if (data.ok) {
      const { restart: _, ...savedConfig } = payload;
      config = savedConfig;
      // Re-render the form from the normalized payload so any field that was clamped or
      // defaulted in buildConfigPayload (e.g. a cleared team ID -> 0, an out-of-range TTL)
      // now shows the value that was actually saved instead of the user's discarded input.
      populateForm();
      const msg = restart ? '✓ Saved — restarting display…' : '✓ Saved';
      status.textContent = msg; status.className = 'save-status ok';
      showToast(msg, 'ok');
    } else {
      const msg = '✗ ' + (data.error || 'Error');
      status.textContent = msg; status.className = 'save-status err';
      showToast(msg, 'err');
    }
  } catch(e) {
    const msg = '✗ ' + e.message;
    status.textContent = msg; status.className = 'save-status err';
    showToast(msg, 'err');
  }
  setTimeout(() => { status.textContent = ''; }, 4000);
}

// ── Map ────────────────────────────────────────────────────────
let zoneBounds = {};

function initMap() {
  if (map) {
    // Map persists for the page's lifetime, but config.ZONE_HOME may have changed since it
    // was built (e.g. the user edited + saved zone numbers on the Config tab). Re-seed the
    // rectangle/handles/coord display from the latest config so the map never shows — or
    // saves back — stale bounds.
    syncMapToConfig();
    setTimeout(() => map.invalidateSize(), 100);
    return;
  }
  if (!config.LOCATION_HOME || !Array.isArray(config.LOCATION_HOME) || !config.ZONE_HOME) {
    showToast('Config not loaded — save your settings on the Config tab first', 'err');
    return;
  }
  map = L.map('map').setView([config.LOCATION_HOME[0], config.LOCATION_HOME[1]], 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap', maxZoom: 18
  }).addTo(map);

  const z = config.ZONE_HOME;
  zoneBounds = { n: z.tl_y, s: z.br_y, w: z.tl_x, e: z.br_x };

  zoneRect = L.rectangle([[z.tl_y, z.tl_x], [z.br_y, z.br_x]],
    { color: '#58a6ff', weight: 2, fillColor: '#58a6ff', fillOpacity: 0.1 }).addTo(map);

  homeMarker = L.circleMarker([config.LOCATION_HOME[0], config.LOCATION_HOME[1]],
    { radius: 8, color: '#3fb950', fillColor: '#3fb950', fillOpacity: 0.8, weight: 2 })
    .addTo(map).bindTooltip('Home');

  const handleIcon = L.divIcon({
    className: '',
    html: '<div style="width:16px;height:16px;background:#58a6ff;border:2px solid #fff;border-radius:4px;cursor:grab;box-sizing:border-box;"></div>',
    iconSize: [16, 16],
    iconAnchor: [8, 8]
  });

  // NW, NE, SE, SW — each corner owns two edges
  const cornerDefs = [
    { latEdge: 'n', lngEdge: 'w' },
    { latEdge: 'n', lngEdge: 'e' },
    { latEdge: 's', lngEdge: 'e' },
    { latEdge: 's', lngEdge: 'w' },
  ];
  const cornerPositions = [[z.tl_y, z.tl_x],[z.tl_y, z.br_x],[z.br_y, z.br_x],[z.br_y, z.tl_x]];

  cornerPositions.forEach((pos, i) => {
    const def = cornerDefs[i];
    const m = L.marker(pos, { icon: handleIcon, draggable: true, zIndexOffset: 1000 }).addTo(map);

    m.on('drag', function(e) {
      // e.latlng is always correct during drag — don't call getLatLng()
      zoneBounds[def.latEdge] = e.latlng.lat;
      zoneBounds[def.lngEdge] = e.latlng.lng;
      zoneRect.setBounds([[zoneBounds.n, zoneBounds.w], [zoneBounds.s, zoneBounds.e]]);
      updateMapCoordDisplay();
    });

    m.on('dragend', function() {
      placeCornerMarkers(); // snap all corners to final clean bounds
    });

    cornerMarkers.push(m);
  });

  updateZoneCoordDisplay();
}

function placeCornerMarkers() {
  cornerMarkers[0].setLatLng([zoneBounds.n, zoneBounds.w]);
  cornerMarkers[1].setLatLng([zoneBounds.n, zoneBounds.e]);
  cornerMarkers[2].setLatLng([zoneBounds.s, zoneBounds.e]);
  cornerMarkers[3].setLatLng([zoneBounds.s, zoneBounds.w]);
}

function updateMapCoordDisplay() {
  document.getElementById('m-tl-y').textContent = zoneBounds.n.toFixed(6);
  document.getElementById('m-br-y').textContent = zoneBounds.s.toFixed(6);
  document.getElementById('m-tl-x').textContent = zoneBounds.w.toFixed(6);
  document.getElementById('m-br-x').textContent = zoneBounds.e.toFixed(6);
}

function updateZoneCoordDisplay() {
  const z = config.ZONE_HOME;
  document.getElementById('m-tl-y').textContent = Number(z.tl_y).toFixed(6);
  document.getElementById('m-br-y').textContent = Number(z.br_y).toFixed(6);
  document.getElementById('m-tl-x').textContent = Number(z.tl_x).toFixed(6);
  document.getElementById('m-br-x').textContent = Number(z.br_x).toFixed(6);
}

function saveFromMap() {
  const b = zoneRect.getBounds();
  document.getElementById('zone_tl_y').value = b.getNorth().toFixed(6);
  document.getElementById('zone_tl_x').value = b.getWest().toFixed(6);
  document.getElementById('zone_br_y').value = b.getSouth().toFixed(6);
  document.getElementById('zone_br_x').value = b.getEast().toFixed(6);
  saveConfig(false);
}

// Re-seed the rectangle, corner handles, and coord display from the current
// config.ZONE_HOME.  Used both to discard in-progress drags and to refresh the map when it
// re-opens after an external (Config-tab) save.  Returns false if there's nothing to sync.
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
  if (!syncMapToConfig()) { showToast('Config not loaded yet', 'err'); return; }
  showToast('Zone changes discarded', 'ok');
}

// ── Log ────────────────────────────────────────────────────────
function initLog() {
  if (evtSource) return;
  document.getElementById('log-status').textContent = '● LIVE';
  document.getElementById('log-status').className = 'chip green';
  fetch('/api/log/history').then(r => r.json()).then(d => {
    // Batch-insert history lines in a single DOM operation via DocumentFragment
    // to avoid 500 individual reflows on a slow Pi browser.
    const out = document.getElementById('log-output');
    const frag = document.createDocumentFragment();
    d.lines.forEach(line => {
      const el = document.createElement('div');
      el.className = 'log-line ' + classifyLine(line);
      el.textContent = line;
      frag.appendChild(el);
    });
    out.appendChild(frag);
    while (out.children.length > 2000) out.removeChild(out.firstChild);
    document.getElementById('log-stats').textContent = out.children.length + ' lines';
    scrollLog();
  }).catch(() => {});
  evtSource = new EventSource('/api/log/stream');
  evtSource.onmessage = e => {
    appendLog(e.data);
    if (document.getElementById('autoscroll').checked) scrollLog();
  };
  evtSource.onerror = () => {
    document.getElementById('log-status').textContent = '● DISCONNECTED';
    document.getElementById('log-status').className = 'chip red';
    evtSource.close();
    evtSource = null;
    setTimeout(() => {
      // Don't reconnect while the tab is hidden — visibilitychange pauses the stream to
      // save Pi load and re-opens it on return; without this, a backgrounded tab keeps
      // reconnecting the SSE log every 5s.
      if (!document.hidden && document.getElementById('log-tab').classList.contains('active')) initLog();
    }, 5000);
  };
}

function appendLog(line) {
  const el = document.createElement('div');
  el.className = 'log-line ' + classifyLine(line);
  el.textContent = line;
  const out = document.getElementById('log-output');
  out.appendChild(el);
  while (out.children.length > 2000) out.removeChild(out.firstChild);
  document.getElementById('log-stats').textContent = out.children.length + ' lines';
}

function classifyLine(line) {
  if (line.includes('[TEST:'))         return 'test';   // must be first — test lines contain other tokens
  if (line.includes('in_zone=True'))   return 'inzone';
  if (line.includes("plane=''"))       return 'nodata';
  if (/error|exception/i.test(line))   return 'error';
  if (line.includes('[override]'))     return 'override';
  if (line.includes(':cached]'))       return 'cached';
  if (line.includes(':miss]'))         return 'miss';
  // Combined "[route:X] [type:X]" line — classify as route (purple)
  if (line.includes('[route:'))        return 'route';
  if (line.includes('[web]'))          return 'web';
  return '';
}

function stopLog() {
  if (evtSource) { evtSource.close(); evtSource = null; }
  document.getElementById('log-status').textContent = '● OFFLINE';
  document.getElementById('log-status').className = 'chip grey';
}

function scrollLog() { const o = document.getElementById('log-output'); o.scrollTop = o.scrollHeight; }
function clearLog() { document.getElementById('log-output').innerHTML = ''; document.getElementById('log-stats').textContent = '0 lines'; }

// ── APIs Tab ───────────────────────────────────────────────────
let apisTabTimer = null;

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
    const [usageRes, stackRes, statsRes] = await Promise.all([
      fetch('/api/usage').then(r => r.json()),
      fetch('/api/apis').then(r => r.json()),
      fetch('/api/cache/stats').then(r => r.json()),
    ]);
    _cacheStats = statsRes;
    renderUsage(usageRes);
    renderStack(stackRes.stack || []);
  } catch(e) {
    document.getElementById('apis-month').textContent = 'Failed to load API data';
  }
}

// ── Stats tab ────────────────────────────────────────────────────────────────
let _statsPeriodDirty = false;   // true when date inputs differ from current loaded range
let _statsReqSeq = 0;            // monotonic token: only the newest /api/stats fetch renders

function _isoToday() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
}
function _isoOffset(days) {
  const d = new Date(); d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function loadStatsTab() {
  // Set date inputs to 90-day default (browser time until server responds)
  document.getElementById('period-from').value = _isoOffset(-89);
  document.getElementById('period-to').value   = _serverToday || _isoToday();
  document.getElementById('period-apply-btn').style.display = 'none';
  _statsPeriodDirty = false;
  const seq = ++_statsReqSeq;
  try {
    const [d, acc, gaAcc] = await Promise.all([
      fetch('/api/stats?days=90').then(r => r.json()),
      fetch('/api/free-api-accuracy').then(r => r.json()).catch(() => null),
      fetch('/api/ga-accuracy').then(r => r.json()).catch(() => null),
    ]);
    if (seq !== _statsReqSeq) return;  // a newer stats request superseded this one
    if (d.today) {
      _serverToday = d.today;
      document.getElementById('period-to').value = d.today;
    }
    renderTodayStats(d);
    renderRecentFlights(d);
    if (acc   && !acc.error)   renderFreeApiAccuracy(acc);
    if (gaAcc && !gaAcc.error) renderGaAccuracy(gaAcc);
    renderPeriodStats(d);
  } catch(e) {
    document.getElementById('stats-total').textContent = '—';
  }
}

function onPeriodChange() {
  _statsPeriodDirty = true;
  document.getElementById('period-apply-btn').style.display = '';
}

async function applyStatsPeriod() {
  const from = document.getElementById('period-from').value;
  const to   = document.getElementById('period-to').value;
  if (!from || !to) return;
  document.getElementById('period-apply-btn').textContent = '…';
  const seq = ++_statsReqSeq;
  try {
    const d = await fetch(`/api/stats?from=${from}&to=${to}`).then(r => r.json());
    if (seq !== _statsReqSeq) return;  // a newer stats request superseded this one
    if (d.error) { showToast(d.error, 'err'); return; }
    renderRecentFlights(d);
    renderPeriodStats(d);
    document.getElementById('period-apply-btn').style.display = 'none';
    _statsPeriodDirty = false;
  } catch(e) {
    showToast('Failed to load stats for that range.', 'err');
  } finally {
    document.getElementById('period-apply-btn').textContent = 'Apply';
  }
}

async function resetStatsPeriod() {
  document.getElementById('period-from').value = _isoOffset(-89);
  document.getElementById('period-to').value   = _serverToday || _isoToday();
  document.getElementById('period-apply-btn').style.display = 'none';
  _statsPeriodDirty = false;
  const seq = ++_statsReqSeq;
  try {
    const d = await fetch('/api/stats?days=90').then(r => r.json());
    if (seq !== _statsReqSeq) return;  // a newer stats request superseded this one
    renderRecentFlights(d);
    renderPeriodStats(d);
  } catch(e) {}
}

// Compute a date string offset from the server's today (falls back to browser time).
// Using server today as base avoids timezone drift between browser and Pi.
function _serverOffset(days) {
  const base = _serverToday || _isoToday();
  const [y, m, d] = base.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth()+1).padStart(2,'0')}-${String(dt.getUTCDate()).padStart(2,'0')}`;
}

async function quickPeriod(preset) {
  const today = _serverToday || _isoToday();
  let from, to;
  if (preset === 'yesterday') {
    from = to = _serverOffset(-1);
  } else if (preset === '7d') {
    from = _serverOffset(-6);
    to   = today;
  } else if (preset === '30d') {
    from = _serverOffset(-29);
    to   = today;
  } else if (preset === '90d') {
    from = _serverOffset(-89);
    to   = today;
  } else {
    return;
  }
  document.getElementById('period-from').value = from;
  document.getElementById('period-to').value   = to;
  document.getElementById('period-apply-btn').style.display = 'none';
  _statsPeriodDirty = false;
  const seq = ++_statsReqSeq;
  try {
    const d = await fetch(`/api/stats?from=${from}&to=${to}`).then(r => r.json());
    if (seq !== _statsReqSeq) return;  // a newer stats request superseded this one
    if (d.error) { showToast('Failed to load stats', 'err'); return; }
    renderRecentFlights(d);
    renderPeriodStats(d);
  } catch(e) { showToast('Failed to load stats', 'err'); }
}

function fmtDate(dateStr, opts) {
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-US', { timeZone: 'UTC', ...opts });
}

function renderTodayStats(d) {
  // Big count + date label (changes wording in range mode)
  const isRange = d.mode === 'range';
  document.getElementById('stats-today-heading').textContent = isRange ? 'Flights in Period' : 'Flights Seen Today';
  document.getElementById('stats-total').textContent = d.total ?? '—';
  document.getElementById('stats-date').textContent  = isRange
    ? (d.range_from && d.range_to ? `${fmtDate(d.range_from,{month:'short',day:'numeric'})} – ${fmtDate(d.range_to,{month:'short',day:'numeric',year:'numeric'})}` : '—')
    : (d.today ? fmtDate(d.today, { weekday:'short', month:'short', day:'numeric' }) : '—');

  const ac = d.api_calls || {};
  // 'airlabs' is the combined total of both AirLabs keys (summed server-side).
  document.getElementById('stats-api-calls').textContent =
    `AirLabs: ${ac.airlabs ?? 0} · AeroAPI: ${ac.aeroapi ?? 0}`;

  const topEl = document.getElementById('stats-top');
  topEl.innerHTML = '';
  _groupNReg(d.top_today).forEach(item => {
    const row = document.createElement('div');
    row.className = 'usage-stat';
    const label = item.name
      ? `${escHtml(item.prefix)} <span style="color:var(--muted);font-size:11px">(${escHtml(item.name)})</span>`
      : escHtml(item.prefix);
    row.innerHTML = `<span class="label" style="font-family:monospace">${label}</span><span class="value">${item.count}</span>`;
    topEl.appendChild(row);
  });

  // Sparkline — last 14 entries (or all if range mode)
  const spark  = document.getElementById('stats-sparkline');
  spark.innerHTML = '';
  const bars   = isRange ? (d.history || []).filter(h => h.total > 0) : (d.history || []).slice(-14);
  const maxVal = Math.max(...bars.map(h => h.total), 1);
  const BAR_H  = 36;
  bars.forEach(h => {
    const px      = Math.max(2, Math.round((h.total / maxVal) * BAR_H));
    const isToday = h.date === d.today;
    const label   = fmtDate(h.date, { month: 'short', day: 'numeric' });
    const bar     = document.createElement('div');
    bar.title     = `${label}: ${h.total} flights`;
    bar.style.cssText = `flex:1;min-width:3px;background:${isToday && !isRange ? 'var(--accent)' : 'var(--border)'};` +
                        `height:${px}px;border-radius:2px 2px 0 0;cursor:default`;
    spark.appendChild(bar);
  });
}

function renderRecentFlights(d) {
  const el = document.getElementById('recent-flights-list');
  const all = d.recent || [];
  const STEP = 10;
  const INIT = 5;
  const MAX  = 50;

  function render(shown) {
    el.innerHTML = '';
    if (!all.length) {
      el.innerHTML = '<span style="font-size:11px;color:var(--muted)">No flights recorded yet.</span>';
      return;
    }

    const tbl = document.createElement('table');
    tbl.style.cssText = 'width:100%;font-size:11px;border-collapse:collapse';
    tbl.innerHTML = `<thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">
      <th style="padding:4px 6px 4px 0;font-weight:500;white-space:nowrap">Date / Time</th>
      <th style="padding:4px 6px;font-weight:500">Flight #</th>
      <th style="padding:4px 6px;font-weight:500">Tail #</th>
      <th style="padding:4px 0;font-weight:500">Route</th>
    </tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector('tbody');

    let lastDate = null;
    all.slice(0, shown).forEach(s => {
      if (s.date !== lastDate) {
        lastDate = s.date;
        const sep = document.createElement('tr');
        sep.innerHTML = `<td colspan="4" style="padding:8px 0 3px;font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;border-top:1px solid var(--border)">${fmtDate(s.date, {weekday:'short',month:'short',day:'numeric'})}</td>`;
        tbody.appendChild(sep);
      }
      const tr = document.createElement('tr');
      const csHtml = `<span style="font-family:monospace;font-weight:600;color:var(--text)">${escHtml(s.callsign)}</span>`
                   + (s.airline ? ` <span style="font-size:10px;color:var(--muted)">(${escHtml(s.airline)})</span>` : '');
      const regHtml = s.registration
        ? `<span style="font-family:monospace;color:var(--muted)">${escHtml(s.registration)}</span>`
        : `<span style="color:var(--border)">—</span>`;
      const orig = s.origin      || '?';
      const dest = s.destination || '?';
      const rtHtml = `<span style="font-family:monospace;color:var(--muted)">${escHtml(orig)}→${escHtml(dest)}</span>`;
      tr.innerHTML = `
        <td style="padding:4px 6px 4px 0;font-family:monospace;white-space:nowrap;color:var(--muted)">${escHtml(_formatTime(s.time))}</td>
        <td style="padding:4px 6px">${csHtml}</td>
        <td style="padding:4px 6px">${regHtml}</td>
        <td style="padding:4px 0">${rtHtml}</td>`;
      tbody.appendChild(tr);
    });
    el.appendChild(tbl);

    // Show-more / show-less controls
    const remaining = Math.min(all.length, MAX) - shown;
    const ctrl = document.createElement('div');
    ctrl.style.cssText = 'margin-top:8px;display:flex;gap:10px;justify-content:center';
    if (remaining > 0) {
      const more = document.createElement('a');
      more.href = '#';
      more.style.cssText = 'font-size:11px;color:var(--accent);text-decoration:none';
      more.textContent = `+${Math.min(STEP, remaining)} more`;
      more.onclick = e => { e.preventDefault(); render(Math.min(shown + STEP, MAX)); };
      ctrl.appendChild(more);
    }
    if (shown > INIT) {
      const less = document.createElement('a');
      less.href = '#';
      less.style.cssText = 'font-size:11px;color:var(--muted);text-decoration:none';
      less.textContent = 'show less';
      less.onclick = e => { e.preventDefault(); render(INIT); };
      ctrl.appendChild(less);
    }
    if (ctrl.children.length) el.appendChild(ctrl);
  }

  render(INIT);
}

function renderFreeApiAccuracy(d) {
  const el = document.getElementById('free-api-content');
  if (!el) return;
  const total30 = d.thirty_day?.total ?? 0;
  if (!total30) {
    el.innerHTML = '<span style="font-size:11px;color:var(--muted)">No cross-checks recorded yet — data accumulates as commercial flights are seen overhead.</span>';
    return;
  }
  const pct30    = d.thirty_day.pct ?? 0;
  const mm30     = d.thirty_day.mismatches ?? 0;
  const todayN   = d.today?.total ?? 0;
  const todayPct = d.today?.pct != null ? d.today.pct + '%' : '—';
  const barColor = pct30 >= 90 ? 'var(--accent)' : pct30 >= 70 ? '#f59e0b' : '#ef4444';

  let html = `
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
      <span style="font-size:28px;font-weight:700;font-family:monospace;color:${barColor}">${pct30}%</span>
      <span style="font-size:12px;color:var(--muted)">match rate · last 30 days · ${total30.toLocaleString()} check${total30 !== 1 ? 's' : ''} · ${mm30.toLocaleString()} override${mm30 !== 1 ? 's' : ''}</span>
    </div>
    <div style="background:var(--bg);border-radius:4px;height:6px;margin-bottom:10px;overflow:hidden">
      <div style="height:100%;width:${Math.round(pct30)}%;background:${barColor};border-radius:4px;transition:width .4s"></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:14px">
      Today: ${todayN} check${todayN !== 1 ? 's' : ''} · ${todayPct} match
    </div>`;

  const mm = d.last_mismatches || [];
  if (mm.length) {
    html += `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">Recent Overrides (adsbdb route → paid API correction)</div>`;
    html += `<table style="width:100%;font-size:11px;border-collapse:collapse">
      <thead><tr style="color:var(--muted)">
        <th style="text-align:left;padding:3px 6px;font-weight:500">Time</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">Flight</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">adsbdb had</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">Paid corrected to</th>
      </tr></thead><tbody>`;
    mm.forEach(r => {
      html += `<tr style="border-top:1px solid var(--border)">
        <td style="padding:4px 6px;color:var(--muted);white-space:nowrap">${escHtml(_formatDate(r.seen_at.slice(0,10)) + ' ' + _formatTime(r.seen_at.slice(11,16)))}</td>
        <td style="padding:4px 6px;font-family:monospace;font-weight:600">${escHtml(r.callsign)}</td>
        <td style="padding:4px 6px;font-family:monospace;color:var(--muted)">${escHtml(r.free_route)}</td>
        <td style="padding:4px 6px;font-family:monospace;color:var(--accent)">${escHtml(r.paid_route)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  el.innerHTML = html;
}

function renderGaAccuracy(d) {
  const el = document.getElementById('ga-accuracy-content');
  if (!el) return;

  function _apiBlock(apiData, apiName) {
    const total30 = apiData?.thirty_day?.total ?? 0;
    if (!total30) {
      return `<div style="margin-bottom:12px">
        <div style="font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px">${escHtml(apiName)}</div>
        <span style="font-size:11px;color:var(--muted)">No checks recorded yet.</span>
      </div>`;
    }
    const pct30    = apiData.thirty_day.pct ?? 0;
    const mm30     = apiData.thirty_day.mismatches ?? 0;
    const todayN   = apiData.today?.total ?? 0;
    const todayPct = apiData.today?.pct != null ? apiData.today.pct + '%' : '—';
    const barColor = pct30 >= 90 ? 'var(--accent)' : pct30 >= 70 ? '#f59e0b' : '#ef4444';
    return `
      <div style="margin-bottom:14px">
        <div style="font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">${escHtml(apiName)}</div>
        <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
          <span style="font-size:24px;font-weight:700;font-family:monospace;color:${barColor}">${pct30}%</span>
          <span style="font-size:12px;color:var(--muted)">match rate · last 30 days · ${total30.toLocaleString()} check${total30 !== 1 ? 's' : ''} · ${mm30.toLocaleString()} override${mm30 !== 1 ? 's' : ''}</span>
        </div>
        <div style="background:var(--bg);border-radius:4px;height:6px;margin-bottom:6px;overflow:hidden">
          <div style="height:100%;width:${Math.round(pct30)}%;background:${barColor};border-radius:4px;transition:width .4s"></div>
        </div>
        <div style="font-size:11px;color:var(--muted)">Today: ${todayN} check${todayN !== 1 ? 's' : ''} · ${todayPct} match</div>
      </div>`;
  }

  const hasAdsbdb  = (d.adsbdb?.thirty_day?.total  ?? 0) > 0;
  const hasOpensky = (d.opensky?.thirty_day?.total ?? 0) > 0;

  if (!hasAdsbdb && !hasOpensky) {
    el.innerHTML = '<span style="font-size:11px;color:var(--muted)">No GA cross-checks recorded yet — data accumulates as N-number aircraft are seen overhead with FR24 route data available.</span>';
    return;
  }

  let html = _apiBlock(d.adsbdb,  'adsbdb');
  html    += _apiBlock(d.opensky, 'OpenSky');

  // Merge and sort mismatches from both APIs (newest first, max 10)
  const allMm = [];
  (d.adsbdb?.last_mismatches  || []).forEach(r => allMm.push({...r, _api: 'adsbdb'}));
  (d.opensky?.last_mismatches || []).forEach(r => allMm.push({...r, _api: 'OpenSky'}));
  allMm.sort((a, b) => b.seen_at.localeCompare(a.seen_at));
  const mm = allMm.slice(0, 10);

  if (mm.length) {
    html += `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">Recent Overrides (free API → FR24 corrected to)</div>`;
    html += `<table style="width:100%;font-size:11px;border-collapse:collapse">
      <thead><tr style="color:var(--muted)">
        <th style="text-align:left;padding:3px 6px;font-weight:500">Time</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">Aircraft</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">API</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">Free API had</th>
        <th style="text-align:left;padding:3px 6px;font-weight:500">FR24 corrected to</th>
      </tr></thead><tbody>`;
    mm.forEach(r => {
      const aircraft = r.registration || r.callsign || '';
      html += `<tr style="border-top:1px solid var(--border)">
        <td style="padding:4px 6px;color:var(--muted);white-space:nowrap">${escHtml(_formatDate(r.seen_at.slice(0,10)) + ' ' + _formatTime(r.seen_at.slice(11,16)))}</td>
        <td style="padding:4px 6px;font-family:monospace;font-weight:600">${escHtml(aircraft)}</td>
        <td style="padding:4px 6px;color:var(--muted)">${escHtml(r._api)}</td>
        <td style="padding:4px 6px;font-family:monospace;color:var(--muted)">${escHtml(r.free_route)}</td>
        <td style="padding:4px 6px;font-family:monospace;color:var(--accent)">${escHtml(r.fr24_route)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  el.innerHTML = html;
}

function renderPeriodStats(d) {
  const isRange = d.mode === 'range';
  const ru = d.rollup || {};

  // Period title
  const title = isRange
    ? `${fmtDate(d.range_from,{month:'short',day:'numeric'})} – ${fmtDate(d.range_to,{month:'short',day:'numeric',year:'numeric'})}`
    : '90-Day Summary';
  document.getElementById('period-title').textContent = title;

  // Period total + api calls.
  // In default mode d.total is today's count; the 90-day total lives in
  // rollup.flights.  In range mode d.total is already the range total.
  document.getElementById('period-total').textContent =
    (isRange ? (d.total ?? 0) : (ru.flights ?? 0)).toLocaleString();
  // Similarly, d.api_calls is today's calls in default mode; the server
  // sends range_api_calls with the full-period sums for the period card.
  const periodAc = isRange ? (d.api_calls || {}) : (d.range_api_calls || {});
  document.getElementById('period-api-calls').textContent =
    `AirLabs: ${periodAc.airlabs ?? 0} · AeroAPI: ${periodAc.aeroapi ?? 0}`;

  // Rollup lists with incremental "show more" (step=5)
  function fillList(elId, items, labelKey, nameKey, step) {
    step = step || 5;
    const all = items || [];
    const el  = document.getElementById(elId);

    function render(shown) {
      el.innerHTML = '';
      if (!all.length) {
        el.innerHTML = '<span style="font-size:11px;color:var(--muted)">No data yet</span>';
        return;
      }
      all.slice(0, shown).forEach((item, i) => {
        const row = document.createElement('div');
        row.className = 'usage-stat';
        const raw = item[labelKey];
        const nm  = nameKey ? item[nameKey] : '';
        const lbl = nm
          ? `${escHtml(raw)} <span style="color:var(--muted);font-size:11px">(${escHtml(nm)})</span>`
          : escHtml(raw);
        row.innerHTML =
          `<span class="label" style="font-family:monospace">` +
          `<span style="color:var(--border);font-size:10px;user-select:none">${i+1}.&thinsp;</span>` +
          `${lbl}</span><span class="value">${item.count.toLocaleString()}</span>`;
        el.appendChild(row);
      });

      const remaining = all.length - shown;
      const ctrl = document.createElement('div');
      ctrl.style.cssText = 'margin-top:5px;display:flex;gap:10px;justify-content:center';

      if (remaining > 0) {
        const more = document.createElement('a');
        more.href = '#';
        more.style.cssText = 'font-size:11px;color:var(--accent);text-decoration:none';
        more.textContent = `+${Math.min(step, remaining)} more`;
        more.onclick = e => { e.preventDefault(); render(shown + step); };
        ctrl.appendChild(more);
      }
      if (shown > step) {
        const less = document.createElement('a');
        less.href = '#';
        less.style.cssText = 'font-size:11px;color:var(--muted);text-decoration:none';
        less.textContent = 'show less';
        less.onclick = e => { e.preventDefault(); render(step); };
        ctrl.appendChild(less);
      }
      if (ctrl.children.length) el.appendChild(ctrl);
    }

    render(step);
  }
  fillList('rollup-airlines', _groupNReg(ru.airlines), 'prefix', 'name', 5);
  fillList('rollup-tails',    ru.tails,    'reg',    'name', 5);
  fillList('rollup-routes',   ru.routes,   'route',  null,   5);
  fillList('rollup-types',    ru.types,    'type',   null,   5);

  // Source efficiency
  const srcEl = document.getElementById('rollup-sources');
  srcEl.innerHTML = '';
  const sp = ru.source_pct || {};
  if (Object.keys(sp).length) {
    [
      { key:'free',     label:'Free APIs',      color:'var(--green)' },
      { key:'mixed',    label:'Free + Paid',     color:'var(--accent)' },
      { key:'paid',     label:'Paid APIs only',  color:'#f59e0b' },
      { key:'override', label:'Override rules',  color:'#fbbf24' },
      { key:'none',     label:'No route found',  color:'var(--muted)' },
    ].forEach(b => {
      const pct = sp[b.key] ?? 0;
      if (!pct) return;
      const row = document.createElement('div');
      row.style.marginBottom = '5px';
      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:2px">
          <span style="color:var(--muted)">${b.label}</span>
          <span style="font-family:monospace;font-weight:600">${pct}%</span>
        </div>
        <div style="height:4px;background:var(--border);border-radius:2px">
          <div style="height:100%;width:${pct}%;min-width:${pct > 0 ? '3px' : '0'};background:${b.color};border-radius:2px"></div>
        </div>`;
      srcEl.appendChild(row);
    });
  } else {
    srcEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">No data yet</span>';
  }

  // History table — newest-first, skip all-zero rows
  const histEl = document.getElementById('stats-history');
  histEl.innerHTML = '';
  const rows = [...(d.history || [])].reverse().filter(r => r.total > 0 || r.airlabs > 0 || r.aeroapi > 0);
  if (!rows.length) { histEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">No flights in this period.</span>'; return; }

  const tbl = document.createElement('table');
  tbl.style.cssText = 'width:100%;font-size:11px;border-collapse:collapse';
  tbl.innerHTML = `<thead><tr style="color:var(--muted);text-align:left">
    <th style="padding:4px 0;font-weight:500">Date</th>
    <th style="padding:4px;font-weight:500;text-align:right">Flights</th>
    <th style="padding:4px;font-weight:500;text-align:right">AirLabs</th>
    <th style="padding:4px 0;font-weight:500;text-align:right">AeroAPI</th>
  </tr></thead><tbody></tbody>`;
  const tbody = tbl.querySelector('tbody');
  rows.forEach(r => {
    const isToday = r.date === d.today;
    const tr = document.createElement('tr');
    tr.style.cssText = isToday ? 'font-weight:600;color:var(--text)' : 'color:var(--muted)';
    const fmtApi = v => (v != null && v !== 0) ? v : (r.total > 0 ? '0' : '—');
    tr.innerHTML = `
      <td style="padding:3px 0">${fmtDate(r.date,{weekday:'short',month:'short',day:'numeric'})}${isToday?' ●':''}</td>
      <td style="padding:3px 4px;text-align:right;font-family:monospace">${r.total}</td>
      <td style="padding:3px 4px;text-align:right;font-family:monospace">${fmtApi(r.airlabs)}</td>
      <td style="padding:3px 0;text-align:right;font-family:monospace">${fmtApi(r.aeroapi)}</td>`;
    tbody.appendChild(tr);
  });
  histEl.appendChild(tbl);
}

function renderStats(d) {
  renderTodayStats(d);
  renderRecentFlights(d);
  renderPeriodStats(d);
}

// ── Search pagination state ────────────────────────────────────────────────
let _searchQ        = '';
let _searchOffset   = 0;
let _searchTotal    = 0;
let _searchLastDate = null;
let _searchGen      = 0;     // bumped on each new search; an in-flight "See more" bails if stale

function _buildSearchRow(s, QU) {
  const hlCs  = QU.length >= 2 && s.callsign.includes(QU);
  const hlReg = QU.length >= 2 && s.registration && s.registration.includes(QU);
  const hlRt  = QU.length >= 2 && ((s.origin || '').includes(QU) || (s.destination || '').includes(QU));
  const csHtml  = `<span style="font-family:monospace;font-weight:${hlCs?'700':'400'};color:${hlCs?'var(--text)':'var(--muted)'}">${escHtml(s.callsign)}</span>`
                + (s.airline && !hlCs ? ` <span style="font-size:10px;color:var(--muted)">(${escHtml(s.airline)})</span>` : '');
  const regHtml = s.registration
    ? `<span style="font-family:monospace;font-weight:${hlReg?'700':'400'};color:${hlReg?'var(--text)':'var(--muted)'}">${escHtml(s.registration)}</span>`
    : `<span style="color:var(--border)">—</span>`;
  const rtHtml = `<span style="font-family:monospace;color:${hlRt?'var(--text)':'var(--muted)'}">${escHtml(s.origin||'?')}→${escHtml(s.destination||'?')}</span>`;
  const acHtml = s.aircraft ? `<span style="color:var(--muted);font-size:10px">${escHtml(s.aircraft)}</span>` : '';
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td style="padding:4px 6px 4px 0;font-family:monospace;white-space:nowrap;color:var(--muted)">${escHtml(_formatTime(s.time))}</td>
    <td style="padding:4px 6px">${csHtml}</td>
    <td style="padding:4px 6px">${regHtml}</td>
    <td style="padding:4px 6px">${rtHtml}</td>
    <td style="padding:4px 0">${acHtml}</td>`;
  return tr;
}

function _appendSearchRows(items, tbody, QU) {
  items.forEach(s => {
    if (s.date !== _searchLastDate) {
      _searchLastDate = s.date;
      const sep = document.createElement('tr');
      sep.innerHTML = `<td colspan="5" style="padding:8px 0 3px;font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;border-top:1px solid var(--border)">${fmtDate(s.date, {weekday:'short',month:'short',day:'numeric'})}</td>`;
      tbody.appendChild(sep);
    }
    tbody.appendChild(_buildSearchRow(s, QU));
  });
}

function _updateSeeMoreBtn(resEl) {
  const existing = document.getElementById('search-more-btn');
  if (existing) existing.remove();
  if (_searchOffset >= _searchTotal) return;
  const remaining = (_searchTotal - _searchOffset).toLocaleString();
  const btn = document.createElement('button');
  btn.id = 'search-more-btn';
  btn.style.cssText = 'margin-top:10px;background:none;border:1px solid var(--border);border-radius:6px;padding:6px 14px;font-size:12px;color:var(--muted);cursor:pointer;width:100%';
  btn.textContent = `See ${remaining} more`;
  btn.onclick = loadMoreSearchResults;
  resEl.appendChild(btn);
}

async function doStatsSearch() {
  const input = document.getElementById('stats-search-input');
  const resEl = document.getElementById('stats-search-results');
  const q = (input.value || '').trim();
  if (q.length < 2) {
    resEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">Enter at least 2 characters.</span>';
    return;
  }
  _searchQ = q; _searchOffset = 0; _searchTotal = 0; _searchLastDate = null;
  _searchGen++;   // invalidate any in-flight "See more" from a previous search
  resEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">Searching...</span>';
  try {
    const r = await fetch(`/api/stats/search?q=${encodeURIComponent(q)}&limit=100&offset=0`);
    const d = await r.json();
    if (d.error) { resEl.innerHTML = `<span style="font-size:11px;color:var(--red)">${escHtml(d.error)}</span>`; return; }
    const items = d.sightings || [];
    if (!items.length) { resEl.innerHTML = '<span style="font-size:11px;color:var(--muted)">No matches found.</span>'; return; }

    _searchTotal  = d.count;
    _searchOffset = items.length;

    const header = document.createElement('div');
    header.style.cssText = 'font-size:10px;color:var(--muted);margin-bottom:8px';
    header.textContent = `${d.count.toLocaleString()} sighting${d.count !== 1 ? 's' : ''} for "${q}"`;

    const tbl = document.createElement('table');
    tbl.id = 'search-table';
    tbl.style.cssText = 'width:100%;font-size:11px;border-collapse:collapse';
    tbl.innerHTML = `<thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">
      <th style="padding:4px 6px 4px 0;font-weight:500;white-space:nowrap">Date / Time</th>
      <th style="padding:4px 6px;font-weight:500">Flight #</th>
      <th style="padding:4px 6px;font-weight:500">Tail #</th>
      <th style="padding:4px 6px;font-weight:500">Route</th>
      <th style="padding:4px 0;font-weight:500;color:var(--muted)">Aircraft</th>
    </tr></thead><tbody></tbody>`;

    resEl.innerHTML = '';
    resEl.appendChild(header);
    resEl.appendChild(tbl);
    _appendSearchRows(items, tbl.querySelector('tbody'), q.toUpperCase());
    _updateSeeMoreBtn(resEl);
  } catch(e) {
    resEl.innerHTML = '<span style="font-size:11px;color:var(--red)">Search failed.</span>';
  }
}

async function loadMoreSearchResults() {
  const btn = document.getElementById('search-more-btn');
  if (btn) { btn.textContent = 'Loading...'; btn.disabled = true; }
  const gen = _searchGen;   // tie this page to the search that spawned the button
  const q = _searchQ;
  try {
    const r = await fetch(`/api/stats/search?q=${encodeURIComponent(q)}&limit=100&offset=${_searchOffset}`);
    const d = await r.json();
    if (gen !== _searchGen) return;   // a new search started — don't append into its table
    if (d.error || !d.sightings?.length) { if (btn) btn.remove(); return; }
    _searchOffset += d.sightings.length;
    const tbody = document.querySelector('#search-table tbody');
    if (tbody) _appendSearchRows(d.sightings, tbody, q.toUpperCase());
    _updateSeeMoreBtn(document.getElementById('stats-search-results'));
  } catch(e) {
    if (gen !== _searchGen) return;
    if (btn) { btn.textContent = 'Load failed — tap to retry'; btn.disabled = false; }
  }
}

function fmtPeriod(start, end) {
  if (!start || !end) return '—';
  // Use Date.UTC so the display date matches the server's YYYY-MM-DD string
  // regardless of the browser's local timezone.
  const fmt = s => {
    const [y, m, d] = s.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d))
      .toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  };
  return `${fmt(start)} – ${fmt(end)}`;
}

let _usageSnapshot = {};
let _serverToday   = null;   // set on first stats load; timezone-correct alternative to _isoToday()
function renderUsage(d) {
  _usageSnapshot = d;
  document.getElementById('apis-month').textContent = '';

  const al = d.airlabs || {};
  document.getElementById('al-calls').textContent     = al.calls ?? '—';
  document.getElementById('al-remaining').textContent = al.remaining ?? '—';
  document.getElementById('al-limit').textContent     = al.limit ?? '—';
  document.getElementById('al-period').textContent    = fmtPeriod(al.period_start, al.period_end);
  const alPct = al.pct_used ?? 0;
  document.getElementById('al-pct').textContent = `${alPct}% used`;
  const alBar = document.getElementById('al-bar');
  alBar.style.width = Math.min(100, alPct) + '%';
  alBar.className = 'usage-bar' + (alPct >= 90 ? ' danger' : alPct >= 70 ? ' warn' : '');

  const al2 = d.airlabs2 || {};
  document.getElementById('al2-calls').textContent     = al2.calls ?? '—';
  document.getElementById('al2-remaining').textContent = al2.remaining ?? '—';
  document.getElementById('al2-limit').textContent     = al2.limit ?? '—';
  document.getElementById('al2-period').textContent    = fmtPeriod(al2.period_start, al2.period_end);
  const al2Pct = al2.pct_used ?? 0;
  document.getElementById('al2-pct').textContent = `${al2Pct}% used`;
  const al2Bar = document.getElementById('al2-bar');
  al2Bar.style.width = Math.min(100, al2Pct) + '%';
  al2Bar.className = 'usage-bar' + (al2Pct >= 90 ? ' danger' : al2Pct >= 70 ? ' warn' : '');

  const fa = d.flightaware || {};
  document.getElementById('fa-calls').textContent     = fa.calls ?? '—';
  document.getElementById('fa-spend').textContent     = fa.est_spend != null ? `$${fa.est_spend.toFixed(3)}` : '—';
  document.getElementById('fa-credit').textContent    = fa.monthly_credit != null ? `$${fa.monthly_credit.toFixed(2)}` : '—';
  document.getElementById('fa-remaining').textContent = fa.remaining != null ? `$${fa.remaining.toFixed(3)}` : '—';
  document.getElementById('fa-period').textContent    = fmtPeriod(fa.period_start, fa.period_end);
  const faPct = fa.pct_used ?? 0;
  document.getElementById('fa-pct').textContent = `${faPct}% of credit used`;
  const faBar = document.getElementById('fa-bar');
  faBar.style.width = Math.min(100, faPct) + '%';
  faBar.className = 'usage-bar' + (faPct >= 90 ? ' danger' : faPct >= 70 ? ' warn' : '');
}

function toggleAdjust(api) {
  const row = document.getElementById('adjust-' + api);
  if (!row) return;
  const showing = row.style.display !== 'none';
  row.style.display = showing ? 'none' : 'block';
  if (!showing) {
    const inp = document.getElementById('adjust-' + api + '-val');
    if (api === 'airlabs') {
      inp.value = _usageSnapshot?.airlabs?.calls ?? 0;
    } else if (api === 'airlabs2') {
      inp.value = _usageSnapshot?.airlabs2?.calls ?? 0;
    } else {
      inp.value = _usageSnapshot?.flightaware?.est_spend ?? 0;
    }
    inp.focus();
    inp.select();
  }
}

async function saveAdjust(api) {
  const inp = document.getElementById('adjust-' + api + '-val');
  const value = parseFloat(inp.value);
  if (isNaN(value) || value < 0) { showToast('Enter a valid number', 'err'); return; }
  try {
    const r = await fetch('/api/usage/adjust', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ api, value })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Request failed');
    document.getElementById('adjust-' + api).style.display = 'none';
    const usageRes = await fetch('/api/usage').then(r => r.json());
    renderUsage(usageRes);
    showToast('Usage corrected', 'ok');
  } catch(e) {
    showToast('Error: ' + e.message, 'err');
  }
}

let _cacheStats = {};

async function toggleApi(apiKey) {
  try {
    const r = await fetch('/api/apis/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ api: apiKey })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Failed');
    const [stackRes, statsRes] = await Promise.all([
      fetch('/api/apis').then(r => r.json()),
      fetch('/api/cache/stats').then(r => r.json()),
    ]);
    _cacheStats = statsRes;
    renderStack(stackRes.stack || []);
    showToast(j.enabled ? `${apiKey} enabled` : `${apiKey} disabled`, 'ok');
  } catch(e) {
    showToast('Error: ' + e.message, 'err');
  }
}

async function clearApiCache(apiKey) {
  try {
    const r = await fetch('/api/cache/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ api: apiKey })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Failed');
    const [stackRes, statsRes] = await Promise.all([
      fetch('/api/apis').then(r => r.json()),
      fetch('/api/cache/stats').then(r => r.json()),
    ]);
    _cacheStats = statsRes;
    renderStack(stackRes.stack || []);
    showToast(`Cache cleared — ${j.deleted} entries removed`, 'ok');
  } catch(e) {
    showToast('Error: ' + e.message, 'err');
  }
}

function renderStack(stack) {
  const container = document.getElementById('stack-rows');
  // Sort: priority 0 (type lookup, not in route chain) goes last; others ascending
  const sorted = [...stack].sort((a, b) => {
    if (a.priority === 0) return 1;
    if (b.priority === 0) return -1;
    return a.priority - b.priority;
  });

  container.innerHTML = sorted.map(api => {
    const prioLabel = api.priority === 0 ? '—' : `#${api.priority}`;
    const prioClass = api.priority === 0 ? 'stack-priority p0' : 'stack-priority';
    const keyBadge  = !api.requires_key
      ? `<span class="badge free">no key needed</span>`
      : api.key_set
        ? `<span class="badge key-set">key set</span>`
        : `<span class="badge no-key">no key</span>`;
    const urlLink = (api.url && /^https:\/\//.test(api.url))
      ? `<a href="${escHtml(api.url)}" target="_blank" style="color:var(--accent);font-size:11px;text-decoration:none">${escHtml(api.url.replace('https://',''))}</a>`
      : '';

    // Enable/disable toggle — only for APIs that have a flag (api_key !== null)
    const canToggle = api.api_key && api.priority > 0 && api.priority < 7;
    const toggleHtml = canToggle ? (() => {
      const enabled = !api.disabled;
      const col  = enabled ? 'var(--accent)' : 'var(--muted)';
      const lbl  = enabled ? 'Enabled' : 'Disabled';
      return `<button onclick="toggleApi('${api.api_key}')"
        title="${enabled ? 'Click to disable' : 'Click to enable'}"
        style="display:inline-flex;align-items:center;gap:5px;background:none;border:1px solid ${col};
               color:${col};border-radius:12px;padding:2px 10px;font-size:10px;cursor:pointer;margin-right:6px">
        <span style="width:8px;height:8px;border-radius:50%;background:${col};display:inline-block"></span>
        ${lbl}
      </button>`;
    })() : '';

    // Cache clear button — for apis with a cache_key
    const cacheCount = api.api_key ? (_cacheStats[api.api_key] ?? '?') : null;
    const cacheHtml = (canToggle && cacheCount !== null) ? `<button onclick="clearApiCache('${api.api_key}')"
      title="Delete cached entries — forces a live API call next time"
      style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:12px;
             padding:2px 10px;font-size:10px;cursor:pointer">
      🗑 Clear cache (${cacheCount})
    </button>` : '';

    const controlsHtml = (toggleHtml || cacheHtml)
      ? `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">${toggleHtml}${cacheHtml}</div>` : '';

    return `
      <div class="stack-row" style="${api.disabled ? 'opacity:.55' : ''}">
        <div class="${prioClass}">${prioLabel}</div>
        <div>
          <div class="stack-name">${escHtml(api.name)}</div>
          <div class="stack-type">${escHtml(api.type)}</div>
          ${urlLink ? `<div style="margin-bottom:4px">${urlLink}</div>` : ''}
          <div class="stack-cost">${escHtml(api.cost)}</div>
          <div class="stack-notes">${escHtml(api.notes)}</div>
          ${controlsHtml}
        </div>
        <div class="stack-badge">${keyBadge}</div>
      </div>`;
  }).join('');
}

function _formatTime(hhmm) {
  // hhmm is "HH:MM" in 24-hour format (from server/SQLite)
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

async function clearCacheEntry() {
  const input  = document.getElementById('cache-entry-input');
  const result = document.getElementById('cache-entry-result');
  const val    = (input.value || '').trim().toUpperCase();
  if (!val) {
    result.style.color = 'var(--muted)';
    result.textContent = 'Enter a tail number or callsign first.';
    return;
  }
  result.style.color = 'var(--muted)';
  result.textContent = 'Clearing…';
  try {
    const r = await fetch('/api/cache/clear/entry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value: val})
    });
    const j = await r.json();
    if (!r.ok) {
      result.style.color = 'var(--red)';
      result.textContent = j.error || 'Error clearing entry.';
      return;
    }
    result.style.color = j.deleted > 0 ? 'var(--green)' : 'var(--muted)';
    result.textContent = j.deleted > 0
      ? `Cleared ${j.deleted} entr${j.deleted === 1 ? 'y' : 'ies'} for ${val} (${j.found_as}).`
      : `No cache entries found for ${val}.`;
    input.value = '';
    // Refresh cache counts on each stack row
    const [stackRes, statsRes] = await Promise.all([
      fetch('/api/apis').then(r => r.json()),
      fetch('/api/cache/stats').then(r => r.json()),
    ]);
    _cacheStats = statsRes;
    renderStack(stackRes.stack || []);
  } catch(e) {
    result.style.color = 'var(--red)';
    result.textContent = 'Request failed: ' + e.message;
  }
}

// ── Override Rules ─────────────────────────────────────────────
let _rules = [];
let _rulesLoaded = false;
let _editingIdx = null;
let _dragIdx    = null;

async function initRulesTab() {
  // Only fetch from server on first visit — subsequent visits re-render
  // from the in-memory array (which stays in sync after add/delete).
  if (_rulesLoaded) { renderRules(); return; }
  try {
    const r = await fetch('/api/overrides');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (!Array.isArray(data)) throw new Error('Unexpected response format');
    _rules = data;
    _rulesLoaded = true;
    renderRules();
  } catch(e) {
    document.getElementById('rules-table-wrap').innerHTML =
      `<div class="rules-empty" style="color:var(--red)">Failed to load rules: ${escHtml(e.message)}</div>`;
  }
}

function _editKeydown(e, i) {
  if (e.key === 'Enter')  saveEditRule(i);
  if (e.key === 'Escape') cancelEdit();
}

function renderRules() {
  const wrap = document.getElementById('rules-table-wrap');
  const countEl = document.getElementById('rules-count');
  countEl.textContent = _rules.length ? `${_rules.length} rule${_rules.length > 1 ? 's' : ''}` : '';

  if (_rules.length === 0) {
    wrap.innerHTML = '<div class="rules-empty">No rules defined — add one below.</div>';
    return;
  }

  const canDrag = _editingIdx === null;

  const rows = _rules.map((r, i) => {
    if (i === _editingIdx) {
      return `
        <tr class="editing">
          <td><span class="drag-handle" style="opacity:.2;cursor:default">⠿</span></td>
          <td><input id="edit-pattern" class="rule-edit-input mono" value="${escHtml(r.pattern)}"     onkeydown="_editKeydown(event,${i})"></td>
          <td><input id="edit-origin"  class="rule-edit-input mono" value="${escHtml(r.origin)}"      onkeydown="_editKeydown(event,${i})" maxlength="4" style="max-width:70px"></td>
          <td><input id="edit-dest"    class="rule-edit-input mono" value="${escHtml(r.destination)}" onkeydown="_editKeydown(event,${i})" maxlength="4" style="max-width:70px"></td>
          <td><input id="edit-display" class="rule-edit-input"       value="${escHtml(r.display||'')}" onkeydown="_editKeydown(event,${i})"></td>
          <td><input id="edit-plane"   class="rule-edit-input"      value="${escHtml(r.plane||'')}"   onkeydown="_editKeydown(event,${i})"></td>
          <td><input id="edit-note"    class="rule-edit-input"      value="${escHtml(r.note||'')}"    onkeydown="_editKeydown(event,${i})"></td>
          <td style="white-space:nowrap">
            <button class="btn btn-primary"   style="padding:3px 10px;font-size:11px;margin-right:4px" onclick="saveEditRule(${i})">✓ Save</button>
            <button class="btn btn-secondary" style="padding:3px 10px;font-size:11px"                  onclick="cancelEdit()">Cancel</button>
          </td>
        </tr>`;
    }
    const dragAttrs = canDrag
      ? `draggable="true" ondragstart="dragStart(event,${i})" ondragover="dragOver(event,${i})" ondrop="dragDrop(event,${i})" ondragend="dragEnd()" ondragleave="dragLeave(event)"`
      : '';
    return `
      <tr ${dragAttrs}>
        <td><span class="drag-handle" title="Drag to reorder">⠿</span></td>
        <td><span class="rule-pattern">${escHtml(r.pattern)}</span></td>
        <td>${r.origin      ? `<span class="rule-airport">${escHtml(r.origin)}</span>`      : '<span class="rule-blank">—</span>'}</td>
        <td>${r.destination ? `<span class="rule-airport">${escHtml(r.destination)}</span>` : '<span class="rule-blank">—</span>'}</td>
        <td><span class="rule-note">${escHtml(r.display || '')}</span></td>
        <td><span class="rule-note" style="color:var(--muted)">${escHtml(r.plane || '')}</span></td>
        <td><span class="rule-note">${escHtml(r.note  || '')}</span></td>
        <td style="white-space:nowrap">
          <button class="btn btn-secondary" style="padding:3px 10px;font-size:11px;margin-right:4px" onclick="editRule(${i})">✎ Edit</button>
          <button class="btn btn-danger"    style="padding:3px 10px;font-size:11px"                  onclick="deleteRule(${i})">✕</button>
        </td>
      </tr>`;
  }).join('');

  wrap.innerHTML = `
    <table class="rules-table">
      <thead><tr>
        <th style="width:24px"></th>
        <th>Pattern</th><th>Origin</th><th>Dest</th><th>Display Name</th><th>Type Override</th><th>Note</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;


  // Focus first field if a row just entered edit mode
  if (_editingIdx !== null) {
    setTimeout(() => { const el = document.getElementById('edit-pattern'); if (el) el.focus(); }, 0);
  }
}

function editRule(i) {
  if (_editingIdx !== null && _editingIdx !== i) {
    showToast('Changes discarded — editing another rule', 'err');
  }
  _editingIdx = i;
  renderRules();
}

function cancelEdit() {
  _editingIdx = null;
  renderRules();
}

// ── Drag-to-reorder ────────────────────────────────────────────
function dragStart(e, i) {
  _dragIdx = i;
  e.dataTransfer.effectAllowed = 'move';
  // Small delay so the drag ghost renders before we dim the row
  setTimeout(() => {
    const rows = document.querySelectorAll('.rules-table tbody tr');
    if (rows[i]) rows[i].classList.add('dragging');
  }, 0);
}

function dragOver(e, i) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  // Clear previous highlight and set new one
  document.querySelectorAll('.rules-table tbody tr').forEach((r, idx) => {
    r.classList.toggle('drag-over', idx === i && idx !== _dragIdx);
  });
}

function dragLeave(e) {
  // Only clear if we've truly left the row (not just moved to a child element)
  if (!e.currentTarget.contains(e.relatedTarget)) {
    e.currentTarget.classList.remove('drag-over');
  }
}

async function dragDrop(e, i) {
  e.preventDefault();
  dragEnd();
  if (_dragIdx === null || _dragIdx === i) { _dragIdx = null; return; }

  const moved = _rules.splice(_dragIdx, 1)[0];
  _rules.splice(i, 0, moved);

  const ok = await saveRules();
  if (ok) {
    renderRules();
    showToast('✓ Rules reordered', 'ok');
  } else {
    // Revert
    const reverted = _rules.splice(i, 1)[0];
    _rules.splice(_dragIdx, 0, reverted);
    renderRules();
  }
  _dragIdx = null;
}

function dragEnd() {
  document.querySelectorAll('.rules-table tbody tr').forEach(r => {
    r.classList.remove('dragging', 'drag-over');
  });
}

async function saveEditRule(i) {
  const pattern = (document.getElementById('edit-pattern').value || '').trim().toUpperCase();
  if (!pattern) { showToast('✗ Pattern is required', 'err'); return; }
  if (_rules.some((r, idx) => idx !== i && r.pattern === pattern)) {
    showToast(`✗ Pattern "${pattern}" already exists`, 'err'); return;
  }

  const original = { ..._rules[i] };
  _rules[i] = {
    pattern,
    origin:      (document.getElementById('edit-origin').value   || '').trim().toUpperCase(),
    destination: (document.getElementById('edit-dest').value     || '').trim().toUpperCase(),
    display:     (document.getElementById('edit-display').value  || '').trim(),
    plane:       (document.getElementById('edit-plane').value    || '').trim(),
    note:        (document.getElementById('edit-note').value     || '').trim(),
  };

  const ok = await saveRules();
  if (ok) {
    _editingIdx = null;
    renderRules();
    showToast(`✓ Rule updated: ${pattern}`, 'ok');
  } else {
    _rules[i] = original;
    renderRules();
  }
}

async function addRule() {
  const pattern = document.getElementById('rule-pattern').value.trim().toUpperCase();
  if (!pattern) { showToast('✗ Pattern is required', 'err'); return; }
  if (_rules.some(r => r.pattern === pattern)) {
    showToast(`✗ Pattern "${pattern}" already exists`, 'err'); return;
  }

  const rule = {
    pattern,
    origin:      document.getElementById('rule-origin').value.trim().toUpperCase(),
    destination: document.getElementById('rule-dest').value.trim().toUpperCase(),
    display:     document.getElementById('rule-display').value.trim(),
    plane:       document.getElementById('rule-plane').value.trim(),
    note:        document.getElementById('rule-note').value.trim(),
  };

  _rules.push(rule);
  const ok = await saveRules();
  if (ok) {
    document.getElementById('rule-pattern').value = '';
    document.getElementById('rule-origin').value  = '';
    document.getElementById('rule-dest').value    = '';
    document.getElementById('rule-display').value = '';
    document.getElementById('rule-plane').value   = '';
    document.getElementById('rule-note').value    = '';
    renderRules();
    showToast(`✓ Rule added: ${rule.pattern}`, 'ok');
  } else {
    _rules.pop(); // revert on failure
  }
}

async function deleteRule(i) {
  _editingIdx = null;
  const removed = _rules.splice(i, 1)[0];
  const ok = await saveRules();
  if (ok) {
    renderRules();
    showToast(`✓ Rule removed: ${removed.pattern}`, 'ok');
  } else {
    _rules.splice(i, 0, removed); // revert on failure
    renderRules();
  }
}

async function saveRules() {
  const status = document.getElementById('rules-save-status');
  status.textContent = 'Saving…'; status.className = 'save-status';
  try {
    const r = await fetch('/api/overrides', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_rules),
    });
    const d = await r.json();
    if (d.ok) {
      status.textContent = '✓ Saved'; status.className = 'save-status ok';
      setTimeout(() => { status.textContent = ''; }, 3000);
      return true;
    } else {
      const msg = '✗ ' + (d.error || 'Save failed');
      status.textContent = msg; status.className = 'save-status err';
      showToast(msg, 'err');
      return false;
    }
  } catch(e) {
    const msg = '✗ ' + e.message;
    status.textContent = msg; status.className = 'save-status err';
    showToast(msg, 'err');
    return false;
  }
}

// ── Airline name lookup ────────────────────────────────────────
const AIRLINE_NAMES = {
  // ── US Major ──────────────────────────────────────────────────
  'AAL':'American Airlines',      'DAL':'Delta Air Lines',
  'UAL':'United Airlines',        'SWA':'Southwest Airlines',
  'ASA':'Alaska Airlines',        'JBU':'JetBlue Airways',
  'NKS':'Spirit Airlines',        'FFT':'Frontier Airlines',
  'HAL':'Hawaiian Airlines',      'SCX':'Sun Country Airlines',
  // ── US Regional ───────────────────────────────────────────────
  'SKW':'SkyWest Airlines',       'RPA':'Republic Airways',
  'ENY':'Envoy Air',              'JIA':'PSA Airlines',
  'ASH':'Mesa Airlines',          'AWI':'Air Wisconsin',
  'PDT':'Piedmont Airlines',      'GJS':'GoJet Airlines',
  'QXE':'Horizon Air',            'SIL':'Silver Airways',
  'TSQ':'Trans States Airlines',  'CHQ':'Chautauqua Airlines',
  'CPZ':'Comair',                 'JZA':'Jazz Aviation',
  'UCA':'CommutAir',              'MTN':'Mountain Air Cargo',
  'FLG':'Frontier (charter)',
  // ── US Cargo ──────────────────────────────────────────────────
  'FDX':'FedEx Express',          'UPS':'UPS Airlines',
  'GTI':'Atlas Air',              'ABX':'ABX Air',
  'PAC':'Polar Air Cargo',        'SOU':'Southern Air',
  'CKS':'Kalitta Air',            'ATN':'Air Transport Intl',
  'OAE':'Omni Air International', 'ASN':'Amazon Air',
  'WGN':'Western Global Airlines','NCR':'Northern Air Cargo',
  'DHK':'DHL Aviation',           'AGX':'Amerijet International',
  // ── US Charter / Business ─────────────────────────────────────
  'EJA':'NetJets',                'LXJ':'Flexjet',
  'JSX':'JSX',                    'OCN':'Discover Airlines',
  // ── Mexico ────────────────────────────────────────────────────
  'AMX':'Aeromexico',             'VOI':'Volaris',
  'VIV':'VivaAerobus',            'AIJ':'Interjet',
  'MXY':'Breeze Airways',         'VXP':'Avelo Airlines',
  'VRD':'Virgin America',
  // ── Canada ────────────────────────────────────────────────────
  'ACA':'Air Canada',             'WJA':'WestJet',
  'TRS':'Air Transat',            'SWG':'Sunwing Airlines',
  'CJT':'Cargojet',               'ROU':'Air Canada Rouge',
  'POE':'Porter Airlines',        'FLE':'Flair Airlines',
  // ── UK ────────────────────────────────────────────────────────
  'BAW':'British Airways',        'VIR':'Virgin Atlantic',
  'EZY':'easyJet',                'TOM':'TUI Airways',
  'EXS':'Jet2',
  // ── Germany ───────────────────────────────────────────────────
  'DLH':'Lufthansa',              'EWG':'Eurowings',
  'CFG':'Condor',                 'GEC':'Lufthansa Cargo',
  // ── France ────────────────────────────────────────────────────
  'AFR':'Air France',
  // ── Netherlands ───────────────────────────────────────────────
  'KLM':'KLM',
  // ── Spain ─────────────────────────────────────────────────────
  'IBE':'Iberia',                 'VLG':'Vueling',
  'IBS':'Iberia Express',
  // ── Italy ─────────────────────────────────────────────────────
  'AZA':'ITA Airways',
  // ── Ireland ───────────────────────────────────────────────────
  'EIN':'Aer Lingus',             'RYR':'Ryanair',
  // ── Scandinavia / Nordic ──────────────────────────────────────
  'SAS':'Scandinavian Airlines',  'FIN':'Finnair',
  'NAX':'Norwegian',              'NOZ':'Norwegian Air Sweden',
  'ICE':'Icelandair',
  // ── Switzerland / Austria / Portugal ─────────────────────────
  'SWR':'SWISS',                  'AUA':'Austrian Airlines',
  'TAP':'TAP Air Portugal',       'EDW':'Edelweiss Air',
  'TWY':'Solarius Aviation',
  // ── Belgium / Poland / Hungary / Czech / Romania / Greece ─────
  'BEL':'Brussels Airlines',      'LOT':'LOT Polish Airlines',
  'WZZ':'Wizz Air',               'CSA':'Czech Airlines',
  'ROT':'TAROM',                  'AEE':'Aegean Airlines',
  'OAL':'Olympic Air',
  // ── Turkey / Russia ───────────────────────────────────────────
  'THY':'Turkish Airlines',       'PGT':'Pegasus Airlines',
  'AFL':'Aeroflot',               'SBI':'S7 Airlines',
  // ── Middle East ───────────────────────────────────────────────
  'UAE':'Emirates',               'QTR':'Qatar Airways',
  'ETD':'Etihad Airways',         'SVA':'Saudia',
  'GFA':'Gulf Air',               'MSR':'EgyptAir',
  'ISR':'El Al',                  'MEA':'Middle East Airlines',
  'RJA':'Royal Jordanian',        'OMA':'Oman Air',
  'FDB':'flydubai',               'ABY':'Air Arabia',
  'KAC':'Kuwait Airways',
  // ── Asia-Pacific ──────────────────────────────────────────────
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
  // ── Latin America ─────────────────────────────────────────────
  'LAN':'LATAM Airlines',         'TAM':'LATAM Brasil',
  'AVA':'Avianca',                'GOL':'GOL Airlines',
  'AZU':'Azul Airlines',          'CMP':'Copa Airlines',
  'ARG':'Aerolíneas Argentinas',  'AEA':'Air Europa',
  'SKU':'Sky Airline',
  // ── Africa ────────────────────────────────────────────────────
  'ETH':'Ethiopian Airlines',     'SAA':'South African Airways',
  'KQA':'Kenya Airways',          'RAM':'Royal Air Maroc',
  'DAH':'Air Algérie',            'TAR':'Tunisair',
};

function airlineName(callsign) {
  if (!callsign) return null;
  // Extract 2–3 letter ICAO prefix followed by digits: UAL2175 → UAL
  const m = callsign.match(/^([A-Z]{2,3})(?=\d)/);
  return m ? (AIRLINE_NAMES[m[1]] || null) : null;
}

// ── Test Flight ────────────────────────────────────────────────
let _testCountdownTimer = null;

async function runTestFlight() {
  const input = document.getElementById('test-callsign');
  const cs = input.value.trim().toUpperCase();
  if (!cs) { input.focus(); return; }

  const useCache = document.getElementById('test-use-cache').checked;
  const btn     = document.getElementById('test-run-btn');
  const clearBtn = document.getElementById('test-clear-btn');
  const resultEl = document.getElementById('test-result');
  btn.disabled = true;
  btn.textContent = '⏳ Testing…';
  clearBtn.style.display = 'none';
  resultEl.style.display = 'none';
  clearInterval(_testCountdownTimer);

  const resetBtn = document.getElementById('test-reset-btn');
  try {
    const ctrl = new AbortController();
    const tmId = setTimeout(() => ctrl.abort(), 90000);
    const resp = await fetch('/api/test_flight', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({callsign: cs, use_cache: useCache}),
      signal: ctrl.signal,
    });
    clearTimeout(tmId);
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.error || `Server error ${resp.status}`);
    }
    const data = await resp.json();
    renderTestResult(data);
    if (data.display_injected) clearBtn.style.display = 'inline-block';
    resetBtn.style.display = 'inline-block';
  } catch(e) {
    resultEl.innerHTML =
      `<div style="padding:14px 18px;color:var(--red)">✗ ${escHtml(e.message || 'Request failed')}</div>`;
    resultEl.style.display = 'block';
    resetBtn.style.display = 'inline-block';
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Test';
  }
}

function renderTestResult(d) {
  const el = document.getElementById('test-result');

  // ── override box ──────────────────────────────────────────────
  let ovHtml = '';
  if (d.override_matched && d.override) {
    const ov = d.override;
    ovHtml = `
      <div class="test-override-box">
        <div class="ov-label">✓ Override Matched</div>
        <div style="font-size:12px;margin-top:3px">
          Pattern: <code style="color:var(--accent)">${escHtml(ov.pattern)}</code>
          ${(ov.origin || ov.destination)
            ? `&nbsp;· Route: <code style="color:var(--accent)">${escHtml(ov.origin||'?')} → ${escHtml(ov.destination||'?')}</code>`
            : ''}
          ${ov.display ? `&nbsp;· Display: <code style="color:#c800c8">${escHtml(ov.display)}</code>` : ''}
          ${ov.plane   ? `&nbsp;· Type Override: <code style="color:#c800c8">${escHtml(ov.plane)}</code>` : ''}
          ${ov.note  ? `<br><span style="font-size:11px;color:var(--muted)">${escHtml(ov.note)}</span>` : ''}
        </div>
      </div>`;
  } else {
    ovHtml = `<div style="font-size:12px;color:var(--muted);margin:6px 0">No override rule matched.</div>`;
  }

  // ── decide render mode ────────────────────────────────────────
  const isCacheMode = (d.steps || {}).mode === 'cache';

  // ── live position row (shared by both modes) ──────────────────
  function livePosBody(s) {
    if (!s) return `<span style="color:var(--muted)">—</span>`;
    if (s.error) return `<span style="color:var(--red)">✗ ${escHtml(s.error)}</span>`;
    if (s.airborne === false) return `<span style="color:var(--muted)">Not airborne</span>`;
    const alt = s.alt_ft != null ? `${Number(s.alt_ft).toLocaleString()} ft` : '—';
    const vs  = s.vs    != null ? ` vs ${s.vs > 0 ? '+' : ''}${s.vs}` : '';
    return `<span style="color:var(--green)">● Found</span> <code style="font-size:11px">${escHtml(s.hex||'—')}</code><br>`
         + `<span style="color:var(--muted);font-size:11px">alt ${alt}${vs} · ${s.found_by||''}</span>`;
  }

  // ── cache mode: simplified two-card result ────────────────────
  let stepsSection = '';
  if (isCacheMode) {
    const lp = (d.steps || {}).live_position;
    const rr = (d.steps || {}).route_result || {};
    const tr = (d.steps || {}).type_result  || {};
    const routeBody = (rr.origin || rr.destination)
      ? `<span style="color:var(--accent);font-weight:700">${escHtml(rr.origin||'?')} → ${escHtml(rr.destination||'?')}</span><br>`
        + `<span style="font-size:11px;color:var(--muted)">source: <code>${escHtml(rr.source||'?')}</code></span>`
      : `<span style="color:var(--muted)">No route data</span>`;
    const typeBody = tr.plane
      ? `<span style="color:#c800c8">${escHtml(tr.plane)}</span><br>`
        + `<span style="font-size:11px;color:var(--muted)">source: <code>${escHtml(tr.source||'?')}</code></span>`
      : `<span style="color:var(--muted)">No type data</span>`;
    stepsSection = `
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:10px 0 0">
        Cache Pipeline
        <span style="font-size:9px;font-weight:400;color:var(--muted);text-transform:none;letter-spacing:0;margin-left:6px">— get_route() + get_aircraft_type() (live code path)</span>
      </div>
      <div class="test-step-grid" style="margin-top:8px">
        <div class="test-step">
          <div class="test-step-name">airplanes.live</div>
          <div class="test-step-desc">Live position</div>
          <div class="test-step-body">${livePosBody(lp)}</div>
        </div>
        <div class="test-step">
          <div class="test-step-name">Route</div>
          <div class="test-step-desc">get_route() — full cache waterfall</div>
          <div class="test-step-body">${routeBody}</div>
        </div>
        <div class="test-step">
          <div class="test-step-name">Aircraft Type</div>
          <div class="test-step-desc">get_aircraft_type() — cache then APIs</div>
          <div class="test-step-body">${typeBody}</div>
        </div>
      </div>`;
  } else {
    // ── no-cache mode: full per-API step grid ─────────────────────
    const stepDefs = [
      {key:'live_position', label:'airplanes.live',  desc:'Live position + type'},
      {key:'adsbdb_route',  label:'adsbdb route',    desc:'Static historical DB'},
      {key:'opensky',       label:'OpenSky',          desc:'Real-time by hex'},
      {key:'airlabs',       label:'AirLabs',           desc:'Real-time by callsign'},
      {key:'aeroapi',       label:'AeroAPI',            desc:'FlightAware paid'},
      {key:'adsbdb_type',   label:'adsbdb type',       desc:'Aircraft type fallback'},
      {key:'opensky_meta',  label:'OpenSky metadata',  desc:'Aircraft registry (public)'},
    ];

    function stepBody(def) {
      const s = (d.steps || {})[def.key];
      if (!s) {
        if (d.override_matched && ['adsbdb_route','opensky','airlabs','aeroapi'].includes(def.key))
          return `<span style="color:var(--muted)">Skipped — override</span>`;
        if (!d.hex_code && ['adsbdb_type','opensky_meta'].includes(def.key))
          return `<span style="color:var(--muted)">Skipped — no hex</span>`;
        return `<span style="color:var(--muted)">—</span>`;
      }
      if (s.skipped)
        return `<span style="color:var(--yellow)">Skipped: ${escHtml(s.skipped)}</span>`;
      if (s.error)
        return `<span style="color:var(--red)">✗ ${escHtml(s.error)}</span>`;
      if (def.key === 'live_position') return livePosBody(s);
      if (def.key === 'adsbdb_type' || def.key === 'opensky_meta') {
        if (s.type) return `<span style="color:#c800c8">${escHtml(s.type)}</span>`;
        return `<span style="color:var(--muted)">No type data</span>`;
      }
      // route step
      if (s.origin !== undefined || s.destination !== undefined) {
        const orig = s.origin || '?';
        const dest = s.destination || '?';
        const plaus = s.plausible === false
          ? ` <span style="color:var(--red)">implausible</span>`
          : s.plausible === true ? ` <span style="color:var(--green)">✓</span>` : '';
        const code  = s.status ? ` <span style="color:var(--muted)">(${s.status})</span>` : '';
        return `<span style="color:var(--accent);font-weight:700">${escHtml(orig)} → ${escHtml(dest)}</span>${plaus}${code}`;
      }
      if (s.status)
        return `<span style="color:var(--muted)">HTTP ${s.status} — no data</span>`;
      return `<span style="color:var(--muted)">—</span>`;
    }

    const stepsHtml = stepDefs.map(def => `
      <div class="test-step">
        <div class="test-step-name">${escHtml(def.label)}</div>
        <div class="test-step-desc">${escHtml(def.desc)}</div>
        <div class="test-step-body">${stepBody(def)}</div>
      </div>`).join('');

    stepsSection = `
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:10px 0 0">API Steps</div>
      <div class="test-step-grid">${stepsHtml}</div>`;
  }

  // ── flight info bar ───────────────────────────────────────────
  const hexStr     = d.hex_code ? `<code style="color:var(--muted);font-size:11px">${escHtml(d.hex_code)}</code>` : '';
  const airborneEl = d.airborne
    ? `<span style="color:var(--green)">● Airborne</span>${d.altitude ? ` <span style="font-size:11px;color:var(--muted)">${Number(d.altitude).toLocaleString()} ft</span>` : ''}`
    : `<span style="color:var(--muted)">○ Not currently airborne</span>`;

  // ── final result + countdown ──────────────────────────────────
  // Use display_expires (epoch) to compute true remaining time after API latency.
  // Fall back to display_seconds if expires not present.
  const SECS = d.display_expires
    ? Math.max(1, d.display_expires - Math.floor(Date.now() / 1000))
    : (d.display_seconds || 30);
  let displayLine = '';
  if (d.display_injected) {
    displayLine = `<div class="test-countdown" id="test-countdown">⏱ On display: <span id="test-countdown-num">${SECS}</span>s remaining</div>`;
    startTestCountdown(SECS);
  } else {
    displayLine = `<div style="font-size:11px;color:var(--muted);margin-top:6px">Display: not injected</div>`;
  }

  const modeLabel = isCacheMode
    ? `<span style="background:rgba(88,166,255,.12);color:var(--accent);border-radius:4px;padding:1px 7px;font-size:10px;font-weight:600;letter-spacing:.3px">CACHED</span>`
    : `<span style="background:rgba(248,81,73,.10);color:var(--red);border-radius:4px;padding:1px 7px;font-size:10px;font-weight:600;letter-spacing:.3px">LIVE ONLY</span>`;

  el.innerHTML = `
    <div style="padding:14px 18px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap">
        <span style="font-size:16px;font-weight:700;font-family:'SFMono-Regular',monospace">${escHtml(d.callsign)}</span>
        ${d.tail ? `<span style="font-size:13px;color:var(--muted);font-family:'SFMono-Regular',monospace">${escHtml(d.tail)}</span>` : ''}
        ${hexStr}
        <span style="font-size:12px">${airborneEl}</span>
        ${modeLabel}
      </div>
      ${(n => n ? `<div style="font-size:13px;color:var(--muted);margin-bottom:8px">${escHtml(n)}</div>` : '')(airlineName(d.callsign))}
      ${ovHtml}
      ${stepsSection}
      <div class="test-final">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:8px">Display Result</div>
        <div class="test-final-route">${escHtml(d.final_origin||'?')} → ${escHtml(d.final_destination||'?')}</div>
        <div class="test-final-plane">${escHtml(d.final_plane||'— (no type data)')}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:5px">
          Route: <code>${escHtml(d.route_source)}</code> &nbsp;·&nbsp; Type: <code>${escHtml(d.type_source)}</code>
        </div>
        ${displayLine}
      </div>
    </div>`;
  el.style.display = 'block';
}

function startTestCountdown(secs) {
  clearInterval(_testCountdownTimer);
  let rem = secs;
  _testCountdownTimer = setInterval(() => {
    rem--;
    const numEl = document.getElementById('test-countdown-num');
    if (numEl) numEl.textContent = rem;
    if (rem <= 0) {
      clearInterval(_testCountdownTimer);
      const cdEl = document.getElementById('test-countdown');
      if (cdEl) cdEl.innerHTML = '<span style="color:var(--muted)">⏹ Display time elapsed</span>';
      document.getElementById('test-clear-btn').style.display = 'none';
    }
  }, 1000);
}

async function clearTestDisplay() {
  clearInterval(_testCountdownTimer);
  try { await fetch('/api/test_flight', {method: 'DELETE'}); } catch(e) {}
  document.getElementById('test-clear-btn').style.display = 'none';
  const cdEl = document.getElementById('test-countdown');
  if (cdEl) cdEl.innerHTML = '<span style="color:var(--muted)">⏹ Cleared</span>';
  showToast('✓ Test flight cleared from display', 'ok');
}

async function resetTestFlight() {
  clearInterval(_testCountdownTimer);
  try { await fetch('/api/test_flight', {method: 'DELETE'}); } catch(e) {}
  document.getElementById('test-callsign').value = '';
  document.getElementById('test-result').style.display = 'none';
  document.getElementById('test-result').innerHTML = '';
  document.getElementById('test-clear-btn').style.display = 'none';
  document.getElementById('test-reset-btn').style.display = 'none';
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Scoreboard master toggle ────────────────────────────────────
function toggleScoreboardBody() {
  const on = document.getElementById('scoreboard_enabled').checked;
  document.getElementById('sb-body').style.display = on ? '' : 'none';
}

// ── Scoreboard drag-to-reorder priority ────────────────────────
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
    // Only clear if leaving the tbody entirely, not just moving between cells
    if (!tbody.contains(e.relatedTarget)) {
      tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-over'));
    }
  });

  tbody.addEventListener('drop', e => {
    e.preventDefault();
    const target = e.target.closest('tr');
    if (!target || !dragSrc || target === dragSrc) return;
    // Insert before or after based on vertical midpoint
    const rect = target.getBoundingClientRect();
    const mid  = rect.top + rect.height / 2;
    if (e.clientY < mid) {
      tbody.insertBefore(dragSrc, target);
    } else {
      target.after(dragSrc);
    }
    tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-over'));
  });
}

// ── Init ───────────────────────────────────────────────────────
loadConfig();
checkInitialStatus();
initSbDragDrop();
