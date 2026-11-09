// Runtime config served by nginx from env vars (see nginx/default.conf.template)
const API_BASE_URL = (window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl) || 'http://localhost:8000';

const COLORS = { glider: '#4fc3f7', argo: '#69f0ae', encounter: '#ff5252', search: '#ff9800', fresh: '#ffd740', expired: '#90a4ae' };
let CACHE_TTL_HOURS = 12;  // replaced by /api/config on start-up

const sensorSelect = document.getElementById('sensor-select');
const bboxInput = document.getElementById('bbox-input');
const statusPanel = document.getElementById('status-panel');
const resultsPanel = document.getElementById('results-panel');
const rulePanel = document.getElementById('rule-panel');

// ---------- Map + layers ----------
const map = L.map('map').setView([49.5, -15.0], 4);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
}).addTo(map);

const platformsLayer = L.featureGroup().addTo(map);   // every track in the index
const freshCacheLayer = L.featureGroup().addTo(map);  // cached boxes still within the TTL
const expiredCacheLayer = L.featureGroup();            // older cached boxes: off by default
const resultsLayer = L.featureGroup().addTo(map);     // encounters of the last search
const drawnItems = L.featureGroup().addTo(map);       // the current search box

// Layer toggles live in the right-hand panel
const LAYERS = { platforms: platformsLayer, fresh: freshCacheLayer, expired: expiredCacheLayer, results: resultsLayer };
document.querySelectorAll('#panel input[data-layer]').forEach(box => {
    box.addEventListener('change', () => {
        const layer = LAYERS[box.dataset.layer];
        box.checked ? map.addLayer(layer) : map.removeLayer(layer);
    });
});

map.addControl(new L.Control.Draw({
    draw: { polygon: false, circle: false, marker: false, circlemarker: false, polyline: false, rectangle: true },
    edit: { featureGroup: drawnItems, remove: true }
}));

// ---------- Legend ----------
function renderLegend(rule, config) {
    const line = (color, dash = '') => `<svg width="28" height="10"><line x1="1" y1="5" x2="27" y2="5" stroke="${color}" stroke-width="3" ${dash ? `stroke-dasharray="${dash}"` : ''}/></svg>`;
    const box = color => `<svg width="28" height="14"><rect x="2" y="2" width="24" height="10" fill="${color}" fill-opacity="0.12" stroke="${color}" stroke-dasharray="4,3"/></svg>`;
    const dot = (color, fill) => `<svg width="28" height="14"><circle cx="14" cy="7" r="5" stroke="${color}" stroke-width="2" fill="${fill}"/></svg>`;
    document.getElementById('legend').innerHTML = `
        <div>${line(COLORS.glider)} Glider track</div>
        <div>${line(COLORS.argo)} Argo float track</div>
        <div>${dot('#cfd8dc', '#1a252f')} Track start &nbsp; ${dot('#cfd8dc', '#cfd8dc')} end</div>
        <div>${dot('#fff', COLORS.encounter)} Encounter</div>
        <div>${line(COLORS.search)} Current search box</div>
        <div>${box(COLORS.fresh)} Cached box, fresh</div>
        <div>${box(COLORS.expired)} Cached box, expired</div>
        <div><svg width="28" height="14"><rect x="2" y="2" width="24" height="10" fill="${COLORS.fresh}" fill-opacity="0.15" stroke="${COLORS.fresh}" stroke-width="2.5"/></svg> Newest cached box</div>
        <p class="legend-note">A cached box expires <b>${CACHE_TTL_HOURS} h</b> after it was fetched.
           Searching inside a fresh box uses stored data (cache HIT); otherwise data is fetched again (MISS).</p>
        ${config ? `<p class="legend-note">Data: last <b>${config.lookback_days} days</b>. Argo from ${config.argo_source === 'erddap' ? 'Ifremer ERDDAP' : config.argo_source};
           gliders from ${new URL(config.glider_server).hostname}${config.mock_glider ? ' (plus a mock glider)' : ''}.</p>` : ''}
        ${rule ? `<p class="legend-note">Encounter rule: ≤ ${fmt(rule.max_dist_m / 1000, 0)} km,
           ≤ ${fmt(rule.max_depth_diff_m, 0)} m depth, ≤ ${fmt(rule.max_time_diff_days, 0)} days apart.</p>` : ''}`;
}

// ---------- Helpers ----------
const fmt = (n, d = 2) => Number(n).toFixed(d);
const fmtTime = iso => iso ? new Date(iso).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '–';
const fmtDuration = s => s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${fmt(s / 3600, 1)} h` : `${fmt(s / 86400, 1)} days`;

function boundsToBbox(b) {
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => fmt(v, 4)).join(',');
}

function parseBbox(str) {
    const parts = str.split(',').map(s => parseFloat(s.trim()));
    if (parts.length !== 4 || parts.some(Number.isNaN)) return null;
    const [w, s, e, n] = parts;
    if (w >= e || s >= n) return null;
    return parts;
}

function showBox(bbox) {
    const [w, s, e, n] = bbox;
    drawnItems.clearLayers();
    drawnItems.addLayer(L.rectangle([[s, w], [n, e]], { color: COLORS.search, weight: 2, fill: false }));
}

function updateUrl(bboxStr, sensor) {
    const url = new URL(window.location);
    url.searchParams.set('bbox', bboxStr);
    url.searchParams.set('param', sensor);
    history.replaceState(null, '', url);
}

async function getJson(path) {
    const res = await fetch(`${API_BASE_URL}${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
    return res.json();
}

// ---------- Context layers: platforms + cache boxes ----------
async function loadPlatforms() {
    const fc = await getJson('/api/platforms');
    platformsLayer.clearLayers();
    fc.features.forEach(f => {
        const p = f.properties;
        const color = p.platform_type === 'GLIDER' ? COLORS.glider : COLORS.argo;
        const popup = `<b>${p.platform_id}</b> (${p.platform_type})<br>
            ${p.n_points} points, deepest ${fmt(p.deepest_m, 0)} m<br>
            ${fmtTime(p.start_time)} → ${fmtTime(p.end_time)}<br>
            Sensors: ${(p.available_parameters || []).join(', ')}`;
        L.geoJSON(f, { style: { color, weight: 3, opacity: 0.9 } }).bindPopup(popup).addTo(platformsLayer);
        // Fixed-size markers keep short tracks visible when zoomed out
        const coords = f.geometry.coordinates;
        const [slon, slat] = coords[0], [elon, elat] = coords[coords.length - 1];
        L.circleMarker([slat, slon], { radius: 5, color, weight: 2, fillColor: '#1a252f', fillOpacity: 1 })
            .bindPopup(`${popup}<br><i>Start</i>`).addTo(platformsLayer);
        L.circleMarker([elat, elon], { radius: 5, color, weight: 2, fillColor: color, fillOpacity: 1 })
            .bindPopup(`${popup}<br><i>End</i>`).addTo(platformsLayer);
    });
    return fc.features.length;
}

async function loadCacheWindows() {
    const windows = await getJson('/api/cache-windows');   // newest first
    freshCacheLayer.clearLayers();
    expiredCacheLayer.clearLayers();
    const newestId = windows.length ? windows[0].id : null;
    // Add oldest first so the newest box ends up on top and is easiest to click
    [...windows].reverse().forEach(w => {
        const isNewest = w.id === newestId;
        const rect = L.rectangle([[w.min_lat, w.min_lon], [w.max_lat, w.max_lon]], {
            color: w.fresh ? COLORS.fresh : COLORS.expired,
            weight: isNewest ? 2.5 : 1, dashArray: isNewest ? null : '4,4', fillOpacity: isNewest ? 0.1 : 0.04
        }).addTo(w.fresh ? freshCacheLayer : expiredCacheLayer);
        rect.bindTooltip(`Cached box #${w.id}${isNewest ? ' (newest)' : ''} — <b>${w.fresh ? 'fresh' : 'expired'}</b><br>
            Fetched ${fmtTime(w.last_fetched_at)}<br>
            ${w.fresh ? 'Expires' : 'Expired'} ${fmtTime(w.expires_at)}<br><i>click to search it again</i>`);
        rect.on('click', () => {
            bboxInput.value = [w.min_lon, w.min_lat, w.max_lon, w.max_lat].map(v => fmt(v, 4)).join(',');
            runSearch();
        });
    });
    const nExpired = windows.filter(w => !w.fresh).length;
    document.getElementById('count-fresh').textContent = windows.length - nExpired;
    document.getElementById('count-expired').textContent = nExpired;
    return windows.length;
}

function zoomToPlatforms() {
    const target = platformsLayer.getLayers().length ? platformsLayer : freshCacheLayer;
    if (target.getBounds().isValid()) map.fitBounds(target.getBounds(), { padding: [60, 60], maxZoom: 9 });
}

async function refreshContext({ fit = false } = {}) {
    const [nPlatforms, nWindows] = await Promise.all([loadPlatforms(), loadCacheWindows()]);
    if (fit) zoomToPlatforms();
    return { nPlatforms, nWindows };
}

// ---------- Recent searches (this browser only) ----------
const RECENT_KEY = 'spatial-telemetry.recent-searches';
const RECENT_MAX = 8;

function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; }
}

function saveRecent(bboxStr, sensor, cacheStatus, encounterCount) {
    const list = loadRecent().filter(r => !(r.bbox === bboxStr && r.sensor === sensor));
    list.unshift({ bbox: bboxStr, sensor, cacheStatus, encounterCount, at: new Date().toISOString() });
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))); } catch { /* storage unavailable */ }
    renderRecent();
}

function renderRecent() {
    const list = loadRecent();
    const el = document.getElementById('recent-list');
    if (!list.length) { el.innerHTML = '<p class="legend-note">No searches yet.</p>'; return; }
    el.innerHTML = list.map((r, i) => `
        <button type="button" class="recent" data-i="${i}" title="${r.bbox}">
            <span>${r.sensor} · ${r.encounterCount} hit${r.encounterCount === 1 ? '' : 's'}</span>
            <small>${r.bbox}<br>${fmtTime(r.at)}</small>
        </button>`).join('');
    el.querySelectorAll('.recent').forEach(btn => {
        const r = list[+btn.dataset.i];
        const b = parseBbox(r.bbox);
        let preview = null;
        btn.addEventListener('mouseenter', () => {
            preview = L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#fff', weight: 2, fill: false, dashArray: '6,4' }).addTo(map);
        });
        btn.addEventListener('mouseleave', () => { if (preview) map.removeLayer(preview); });
        btn.addEventListener('click', () => {
            if (preview) map.removeLayer(preview);
            bboxInput.value = r.bbox;
            sensorSelect.value = r.sensor;
            map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [40, 40] });
            runSearch();
        });
    });
}

function fetchSummary(f) {
    if (!f) return '';
    const secs = f.seconds || {};
    const took = k => secs[k] !== undefined ? ` <span class="hint">(${secs[k]} s)</span>` : '';
    const lines = [`<b>Fetched ${f.window.start} → ${f.window.end}</b>${took('total')}`];
    if (f.argo) {
        const a = f.argo;
        lines.push(`Argo${took('argo')}: <b>${a.floats}</b> float(s), ${a.profiles} profile(s); ${a.raw_points.toLocaleString()} measurements → ${a.stored_points} points`);
    }
    if (f.glider) {
        const g = f.glider;
        const capped = g.datasets_used < g.datasets_found ? ` (first ${g.datasets_used} of ${g.datasets_found})` : '';
        lines.push(`Gliders${took('glider')}: <b>${g.gliders}</b> deployment(s)${capped}, ${g.profiles} dive/climb profile(s); ${g.rows_transferred.toLocaleString()} turning-point rows → ${g.stored_points} points`);
        if (g.skipped && g.skipped.length) lines.push(`<span class="warn">Skipped (slow or failing): ${g.skipped.join(', ')}</span>`);
    }
    const errors = ['argo', 'glider'].filter(k => f[`${k}_error`]).map(k => `${k}: ${f[`${k}_error`]}`);
    let html = `<p class="fetch">${lines.join('<br>')}</p>`;
    if (errors.length) html += `<p class="warn">Fetch failed — ${errors.join('<br>')}<br>Box not cached; searching again retries.</p>`;
    return html;
}

// ---------- Search ----------
async function runSearch() {
    const bbox = parseBbox(bboxInput.value);
    if (!bbox) {
        statusPanel.innerHTML = `<h3>Invalid box</h3><p>Use min_lon,min_lat,max_lon,max_lat, e.g. -45,51,-43,53</p>`;
        return;
    }
    const bboxStr = bbox.join(',');
    const sensor = sensorSelect.value;
    showBox(bbox);
    updateUrl(bboxStr, sensor);
    resultsLayer.clearLayers();
    resultsPanel.innerHTML = '';
    statusPanel.innerHTML = `<h3>Querying…</h3><p>${bboxStr} · ${sensor}<br><span class="hint">A new area fetches live Argo and glider data and can take a minute or two.</span></p>`;

    try {
        const params = new URLSearchParams({ bbox: bboxStr, target_param: sensor });
        const data = await getJson(`/api/overlaps?${params}`);
        if (data.error) throw new Error(data.error);

        const r = data.rule;
        rulePanel.textContent = `Rule: ≤ ${fmt(r.max_dist_m / 1000, 0)} km · ≤ ${fmt(r.max_depth_diff_m, 0)} m depth · ≤ ${fmt(r.max_time_diff_days, 0)} days`;
        statusPanel.innerHTML = `<h3>Query complete</h3>
            <p>Cache: <b>${data.cache_status}</b><br>Encounters: <b>${data.encounter_count}</b></p>${fetchSummary(data.fetch)}`;

        data.encounters.forEach(enc => {
            L.geoJSON(enc.glider_track, { style: { color: COLORS.glider, weight: 4, dashArray: '5,5' } })
                .bindPopup(`Glider: ${enc.glider_id}`).addTo(resultsLayer);
            L.geoJSON(enc.float_track, { style: { color: COLORS.argo, weight: 4 } })
                .bindPopup(`Float: ${enc.argo_id}`).addTo(resultsLayer);

            const [lon, lat] = enc.glider_point.coordinates;
            const [flon, flat] = enc.argo_point.coordinates;
            L.polyline([[lat, lon], [flat, flon]], { color: COLORS.encounter, weight: 2, dashArray: '2,4' }).addTo(resultsLayer);
            L.circleMarker([lat, lon], { radius: 8, fillColor: COLORS.encounter, color: '#fff', weight: 2, fillOpacity: 1 })
                .bindPopup(`<b>Encounter</b> ${enc.glider_id} × ${enc.argo_id}<br>
                    Distance ${fmt(enc.dist_m / 1000)} km · Δdepth ${fmt(enc.dz_m, 0)} m · Δt ${fmtDuration(enc.dt_s)}<br>
                    Glider at ${fmt(enc.glider_depth_m, 0)} m, ${fmtTime(enc.glider_time)}<br>
                    Float at ${fmt(enc.argo_depth_m, 0)} m, ${fmtTime(enc.argo_time)}`)
                .addTo(resultsLayer);

            resultsPanel.insertAdjacentHTML('beforeend', `
                <div class="card" data-lat="${lat}" data-lon="${lon}">
                    <h4>${enc.glider_id} × ${enc.argo_id}</h4>
                    <p>${fmt(enc.dist_m / 1000)} km apart · Δdepth ${fmt(enc.dz_m, 0)} m · Δt ${fmtDuration(enc.dt_s)}<br>
                       at ~${fmt(Math.abs(enc.glider_depth_m), 0)} m depth, ${fmtTime(enc.glider_time)}<br>
                       ${enc.n_matching_points} matching point pair(s)</p>
                    <p><span class="badge">Shared sensors</span> ${(enc.shared_sensors || []).join(', ')}</p>
                </div>`);
        });

        saveRecent(bboxStr, sensor, data.cache_status, data.encounter_count);

        if (data.encounter_count === 0) {
            resultsPanel.innerHTML = `<p class="hint">No glider/float pair inside the rule for <b>${sensor}</b> in this box.</p>`;
        }

        // A cache miss adds a box and may move mock tracks, so redraw the context layers
        if (data.cache_status === 'MISS') await refreshContext();
    } catch (err) {
        statusPanel.innerHTML = `<h3>Request failed</h3><p>${err.message}</p>`;
    }
}

resultsPanel.addEventListener('click', e => {
    const card = e.target.closest('.card');
    if (card) map.setView([+card.dataset.lat, +card.dataset.lon], 10);
});

// ---------- Wire up controls ----------
map.on(L.Draw.Event.CREATED, e => {
    bboxInput.value = boundsToBbox(e.layer.getBounds());
    runSearch();
});
map.on(L.Draw.Event.EDITED, e => {
    e.layers.eachLayer(layer => { bboxInput.value = boundsToBbox(layer.getBounds()); });
    runSearch();
});
map.on(L.Draw.Event.DELETED, () => {
    resultsLayer.clearLayers();
    resultsPanel.innerHTML = '';
});

document.getElementById('search-btn').addEventListener('click', runSearch);
document.getElementById('clear-recent').addEventListener('click', () => {
    try { localStorage.removeItem(RECENT_KEY); } catch { /* storage unavailable */ }
    renderRecent();
});
document.getElementById('zoom-btn').addEventListener('click', zoomToPlatforms);
bboxInput.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
document.getElementById('view-btn').addEventListener('click', () => {
    bboxInput.value = boundsToBbox(map.getBounds());
    runSearch();
});
document.getElementById('copy-link-btn').addEventListener('click', async e => {
    try { await navigator.clipboard.writeText(window.location.href); e.target.textContent = 'Link copied'; }
    catch { e.target.textContent = 'Copy failed — use the address bar'; }
    setTimeout(() => { e.target.textContent = 'Copy search link'; }, 1500);
});

// ---------- Start-up ----------
(async () => {
    const url = new URL(window.location);
    if (url.searchParams.get('param')) sensorSelect.value = url.searchParams.get('param');
    const urlBbox = url.searchParams.get('bbox');
    renderLegend(null);
    renderRecent();
    try {
        const config = await getJson('/api/config');
        CACHE_TTL_HOURS = config.cache_ttl_hours;
        renderLegend(config.rule, config);
        const { nPlatforms, nWindows } = await refreshContext({ fit: !urlBbox });
        statusPanel.innerHTML = `<h3>System Readout</h3>
            <p>${nPlatforms} platform(s) and ${nWindows} cached box(es) loaded.<br>Draw, type or click a box to search.</p>`;
    } catch (err) {
        statusPanel.innerHTML = `<h3>Backend unreachable</h3><p>${err.message}<br>API: ${API_BASE_URL}</p>`;
    }
    if (urlBbox) {
        bboxInput.value = urlBbox;
        const b = parseBbox(urlBbox);
        if (b) map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [40, 40] });
        runSearch();
    }
})();
