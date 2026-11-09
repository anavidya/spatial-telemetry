// Glider Reference Finder: pick a glider deployment, get Argo profiles that can check its sensors.
const API_BASE_URL = (window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl) || 'http://localhost:8000';
const COLORS = { glider: '#4fc3f7', argo: '#90a4ae', ref: '#ff5252', link: '#ff8a80',
                 deep: '#4fc3f7', shelf: '#ffb74d', unknown: '#78909c', selected: '#ffd740' };

const $ = id => document.getElementById(id);
const sensorSelect = $('sensor-select'), statusPanel = $('status-panel'), resultsPanel = $('results-panel');
let RULE = null, deployments = [], selectedId = null, rectsById = {};

// ---------- Map + layers ----------
const map = L.map('map').setView([38, -70], 5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 19
}).addTo(map);
const LAYERS = {
    deployments: L.featureGroup().addTo(map),
    glider: L.featureGroup().addTo(map),
    argo: L.featureGroup().addTo(map),
    refs: L.featureGroup().addTo(map),
};
// Layer toggles (remembered in this browser, so an unticked layer is still unticked after a reload)
const LAYER_KEY = 'glider-reference-finder.layers';
let layerState = {};
try { layerState = JSON.parse(localStorage.getItem(LAYER_KEY)) || {}; } catch { layerState = {}; }
document.querySelectorAll('#panel input[data-layer]').forEach(box => {
    const name = box.dataset.layer;
    if (name in layerState) box.checked = layerState[name];
    const apply = () => box.checked ? map.addLayer(LAYERS[name]) : map.removeLayer(LAYERS[name]);
    apply();
    box.addEventListener('change', () => {
        apply();
        layerState[name] = box.checked;
        try { localStorage.setItem(LAYER_KEY, JSON.stringify(layerState)); } catch { /* storage unavailable */ }
        if (name === 'deployments') renderDeploymentList();
    });
});
const SERVER_LABEL = url => {
    try { const h = new URL(url).hostname; return h.includes('ioos') ? 'IOOS DAC' : h.includes('dfo-mpo') ? 'DFO Canada' : h; }
    catch { return url || ''; }
};

// ---------- Helpers ----------
const fmt = (n, d = 0) => Number(n).toFixed(d);
const fmtDate = iso => iso ? new Date(iso).toISOString().slice(0, 10) : '–';
const fmtTime = iso => iso ? new Date(iso).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '–';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const MODE = { D: 'delayed-mode', A: 'adjusted', R: 'real-time' };
async function getJson(path) {
    const res = await fetch(`${API_BASE_URL}${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
    return res.json();
}
function depthClass(d) {
    if (d.max_depth_m == null || !RULE) return 'unknown';
    return d.max_depth_m >= RULE.layer_top_m + RULE.min_overlap_m ? 'deep' : 'shelf';
}

// ---------- Deployments (catalogue) ----------
async function loadDeployments() {
    const fc = await getJson('/api/deployments');
    deployments = fc.features.map(f => ({ ...f.properties, geometry: f.geometry }));
    const st = fc.catalogue || {};
    $('catalogue-status').textContent = st.last_refresh
        ? `Catalogue: ${st.count} deployments, refreshed ${fmtTime(st.last_refresh)}`
        : st.last_error ? `Catalogue refresh failed: ${st.last_error}` : 'Catalogue is loading from ERDDAP (about a minute)…';
    drawDeployments();
    renderDeploymentList();
    return st;
}

function drawDeployments() {
    LAYERS.deployments.clearLayers();
    rectsById = {};
    // Server sends big areas first, so small boxes end up on top and stay clickable.
    // Unreliably wide areas (junk positions, multi-glider datasets) are listed but not drawn.
    deployments.filter(d => !d.unreliable_extent || d.dataset_id === selectedId).forEach(d => {
        const cls = depthClass(d), selected = d.dataset_id === selectedId;
        const layer = L.geoJSON(d.geometry, { style: {
            color: selected ? COLORS.selected : COLORS[cls], weight: selected ? 2.5 : 1,
            dashArray: cls === 'unknown' ? '4,4' : null, fillOpacity: selected ? 0.08 : 0.03 } });
        layer.bindTooltip(`<b>${esc(d.dataset_id)}</b><br>${esc(d.institution)}<br>${fmtDate(d.start_time)} → ${fmtDate(d.end_time)}`
            + (d.max_depth_m != null ? `<br>max depth ${fmt(d.max_depth_m)} m` : '') + '<br><i>click to find references</i>');
        layer.on('click', () => selectDeployment(d.dataset_id));
        layer.addTo(LAYERS.deployments);
        rectsById[d.dataset_id] = layer;
    });
}

function renderDeploymentList() {
    const q = $('deploy-filter').value.trim().toLowerCase();
    const inView = $('in-view').checked, view = map.getBounds();
    const items = deployments.filter(d => {
        if (q && !(`${d.dataset_id} ${d.title} ${d.institution}`.toLowerCase().includes(q))) return false;
        if (inView) {
            if (d.unreliable_extent) return false;   // would match every view
            const b = L.geoJSON(d.geometry).getBounds();
            if (!view.intersects(b)) return false;
        }
        return true;
    });
    $('deploy-count').textContent = items.length;
    $('deploy-list').innerHTML = items.length ? items.map(d => {
        const cls = depthClass(d);
        const badge = (cls === 'deep' ? '<span class="tag deep">deep</span>' : cls === 'shelf' ? '<span class="tag shelf">shallow</span>' : '')
            + (d.unreliable_extent ? `<span class="tag wide" title="Reported area spans ${fmt(d.span_deg)}°: junk positions or a multi-glider dataset. Open it to measure the real track.">area unreliable</span>` : '');
        return `<button type="button" class="deploy-item${d.dataset_id === selectedId ? ' active' : ''}" data-id="${esc(d.dataset_id)}">
            <span>${esc(d.dataset_id)} ${badge}</span>
            <small>${esc(d.institution || '')} · ${esc(SERVER_LABEL(d.server))}<br>${fmtDate(d.start_time)} → ${fmtDate(d.end_time)}${d.max_depth_m != null ? ` · ${fmt(d.max_depth_m)} m` : ''}</small></button>`;
    }).join('') : '<p class="hint">No deployments here. Zoom out or untick "Only those in map view".</p>';
    const nWide = deployments.filter(d => d.unreliable_extent).length;
    if (inView && nWide) $('deploy-list').insertAdjacentHTML('beforeend', `<p class="hint">${nWide} deployment(s) with an unreliable area are hidden here; untick "Only those in map view" to see them.</p>`);
    const hidden = !$('panel').querySelector('input[data-layer="deployments"]').checked;
    if (hidden) $('deploy-list').insertAdjacentHTML('afterbegin', '<p class="warn">Deployment boxes are hidden: tick “Deployments” under Layers.</p>');
    $('deploy-list').querySelectorAll('.deploy-item').forEach(btn => {
        const id = btn.dataset.id;
        btn.addEventListener('mouseenter', () => rectsById[id] && rectsById[id].setStyle({ color: COLORS.selected, weight: 2.5 }));
        btn.addEventListener('mouseleave', () => drawDeployments());
        btn.addEventListener('click', () => selectDeployment(id));
    });
}
$('deploy-filter').addEventListener('input', renderDeploymentList);
$('in-view').addEventListener('change', renderDeploymentList);
map.on('moveend', renderDeploymentList);

// ---------- Recent gliders (this browser only) ----------
const RECENT_KEY = 'glider-reference-finder.recent';
const loadRecent = () => { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; } };
function saveRecent(id, param, n, verdict) {
    const list = loadRecent().filter(r => !(r.id === id && r.param === param));
    list.unshift({ id, param, n, verdict, at: new Date().toISOString() });
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 12))); } catch { /* storage unavailable */ }
    renderRecent();
}
function renderRecent() {
    const list = loadRecent();
    $('recent-list').innerHTML = list.length ? list.map((r, i) => `
        <button type="button" class="recent" data-i="${i}">
            <span>${esc(r.id)}</span>
            <small>${esc(r.param)} · ${r.n} reference(s)<br>${fmtTime(r.at)}</small></button>`).join('')
        : '<p class="legend-note">No gliders checked yet.</p>';
    $('recent-list').querySelectorAll('.recent').forEach(btn => btn.addEventListener('click', () => {
        const r = list[+btn.dataset.i];
        sensorSelect.value = r.param;
        selectDeployment(r.id);
    }));
}
$('clear-recent').addEventListener('click', () => { try { localStorage.removeItem(RECENT_KEY); } catch { } renderRecent(); });

// ---------- Find references for one deployment ----------
let requestSeq = 0;
async function selectDeployment(id) {
    selectedId = id;
    const seq = ++requestSeq, param = sensorSelect.value;
    const url = new URL(window.location);
    url.searchParams.set('glider', id); url.searchParams.set('param', param);
    history.replaceState(null, '', url);
    drawDeployments(); renderDeploymentList();
    const d = deployments.find(x => x.dataset_id === id);
    if (d && !d.unreliable_extent) map.fitBounds(L.geoJSON(d.geometry).getBounds(), { padding: [40, 40], maxZoom: 9 });
    ['glider', 'argo', 'refs'].forEach(k => LAYERS[k].clearLayers());
    resultsPanel.innerHTML = '';
    const started = Date.now();
    const tick = () => { if (seq === requestSeq && statusPanel.dataset.busy) statusPanel.querySelector('.elapsed').textContent = `${Math.round((Date.now() - started) / 1000)} s`; };
    statusPanel.dataset.busy = '1';
    statusPanel.innerHTML = `<h3>${esc(id)}</h3><p>Fetching glider dives and nearby Argo profiles… <span class="elapsed">0 s</span></p>
        <p class="hint">The first look at a deployment can take a minute or two; later ones are cached.</p>`;
    const timer = setInterval(tick, 1000);
    try {
        const r = await getJson(`/api/deployments/${encodeURIComponent(id)}/references?param=${encodeURIComponent(param)}`);
        if (seq !== requestSeq) return;
        if (r.error) throw new Error(r.error);
        showResult(r);
        saveRecent(id, param, r.references.length, r.verdict);
        loadDeployments().catch(() => {});   // max depth is now known: recolour
    } catch (err) {
        if (seq === requestSeq) statusPanel.innerHTML = `<h3>${esc(id)}</h3><p class="warn">Request failed: ${esc(err.message)}</p>`;
    } finally {
        clearInterval(timer); delete statusPanel.dataset.busy;
    }
}
sensorSelect.addEventListener('change', () => { if (selectedId) selectDeployment(selectedId); });

function showResult(r) {
    const g = r.glider, f = r.fetch || {}, s = f.seconds || {};
    const timing = Object.entries(s).map(([k, v]) => `${k} ${v} s`).join(' · ');
    const nCand = (r.argo_candidates || []).length;
    const argo = f.argo_error ? `<span class="warn">Argo fetch failed: ${esc(f.argo_error)}</span>`
        : f.argo && f.argo.cached ? `${nCand} Argo profile(s) nearby · <span class="hint">from cache (fetched ${fmtTime(f.argo.fetched_at)})</span>`
        : f.argo ? `${nCand} Argo profile(s) nearby · <span class="hint">fetched now: ${f.argo.floats} float(s), ${f.argo.profiles} profile(s) in the search area</span>` : '';
    statusPanel.innerHTML = `<h3>${esc(r.dataset_id)} <span class="tag">${esc(SERVER_LABEL(r.server))}</span></h3>
        <p class="verdict ${r.references.length ? 'ok' : ''}">${esc(r.verdict)}</p>
        <p class="fetch">${g.n_dives} dives · ${fmtDate(g.start_time)} → ${fmtDate(g.end_time)} · max ${fmt(g.max_depth_m)} m
           ${f.glider && f.glider.depth_source ? `(${esc(f.glider.depth_source)})` : ''}<br>
           ${f.glider && f.glider.layout ? `Layout: ${esc(f.glider.layout)}<br>` : ''}
           ${f.glider && f.glider.thinned ? `<span class="hint">${esc(f.glider.thinned)}</span><br>` : ''}
           Sensors: ${esc(r.glider_sensors.join(', ') || 'none listed')}<br>${argo}<br>
           <span class="hint">${timing}${f.glider && f.glider.cached ? ' · glider dives from cache' : ''}</span></p>`;

    // Glider dives
    if (r.glider_track) {
        const line = L.geoJSON(r.glider_track, { style: { color: COLORS.glider, weight: 2.5, opacity: 0.9 } })
            .bindPopup(`<b>${esc(r.dataset_id)}</b><br>${g.n_dives} dives, max ${fmt(g.max_depth_m)} m`).addTo(LAYERS.glider);
        const c = r.glider_track.coordinates;
        if (c && c.length) {
            L.circleMarker([c[0][1], c[0][0]], { radius: 5, color: COLORS.glider, fillColor: '#1a252f', fillOpacity: 1, weight: 2 }).bindTooltip('Glider start').addTo(LAYERS.glider);
            L.circleMarker([c[c.length - 1][1], c[c.length - 1][0]], { radius: 5, color: COLORS.glider, fillColor: COLORS.glider, fillOpacity: 1, weight: 2 }).bindTooltip('Glider end').addTo(LAYERS.glider);
        }
        map.fitBounds(line.getBounds(), { padding: [60, 60], maxZoom: 10 });
    }
    // All Argo profiles fetched nearby
    (r.argo_candidates || []).forEach(a => L.circleMarker([a.lat, a.lon], { radius: 4, color: COLORS.argo, weight: 1, fillColor: COLORS.argo, fillOpacity: 0.6 })
        .bindPopup(`Argo ${esc(a.profile_id)}<br>${fmtTime(a.t)}<br>to ${fmt(a.pmax)} dbar · ${MODE[a.data_mode] || a.data_mode || ''}`).addTo(LAYERS.argo));

    // Reference profiles, numbered by rank
    r.references.forEach((x, i) => {
        const n = i + 1;
        L.polyline([[x.glider_lat, x.glider_lon], [x.argo_lat, x.argo_lon]], { color: COLORS.link, weight: 1.5, dashArray: '3,4' }).addTo(LAYERS.refs);
        L.circleMarker([x.glider_lat, x.glider_lon], { radius: 4, color: '#fff', weight: 1, fillColor: COLORS.glider, fillOpacity: 1 })
            .bindTooltip(`Glider dive ${esc(x.glider_profile)} (match #${n})`).addTo(LAYERS.refs);
        L.marker([x.argo_lat, x.argo_lon], { icon: L.divIcon({ className: 'ref-icon', html: `<span>${n}</span>`, iconSize: [22, 22] }) })
            .bindPopup(refPopup(x, n)).addTo(LAYERS.refs);
    });
    resultsPanel.innerHTML = r.references.map((x, i) => `
        <div class="card" data-lat="${x.argo_lat}" data-lon="${x.argo_lon}">
            <h4>#${i + 1} · Float ${esc(x.argo_id.replace('ARGO_', ''))} · cycle ${esc(x.argo_profile.split('_')[1])}
                <span class="tag mode-${esc(x.data_mode)}">${esc(MODE[x.data_mode] || x.data_mode || '?')}</span></h4>
            <p>${fmt(x.dist_m / 1000, 1)} km · ${fmt(x.dt_s / 86400, 1)} days from glider dive ${esc(x.glider_profile)}<br>
               Shared layer <b>${fmt(x.shared_top_m)}–${fmt(x.shared_bottom_m)} m</b> (${fmt(x.overlap_m)} m)<br>
               Argo ${fmtDate(x.argo_time)} · glider ${fmtDate(x.glider_time)} · ${x.n_glider_dives} dive(s) qualify</p>
            <div class="button-row">
                <button type="button" class="compare-btn" data-i="${i}">Check sensors</button>
                <a class="float-link" href="${esc(x.source_url)}" target="_blank" rel="noopener">Float page ↗</a>
            </div>
        </div>`).join('');
    lastResult = r;
}
function refPopup(x, n) {
    return `<b>#${n} Argo ${esc(x.argo_profile)}</b> (${esc(MODE[x.data_mode] || x.data_mode || '?')})<br>
        ${fmt(x.dist_m / 1000, 1)} km, ${fmt(x.dt_s / 86400, 1)} days from dive ${esc(x.glider_profile)}<br>
        Shared ${fmt(x.shared_top_m)}–${fmt(x.shared_bottom_m)} m<br><a href="${esc(x.source_url)}" target="_blank" rel="noopener">Float page ↗</a>`;
}
let lastResult = null;
resultsPanel.addEventListener('click', e => {
    const btn = e.target.closest('.compare-btn');
    if (btn && lastResult) {
        const x = lastResult.references[+btn.dataset.i];
        window.openCompare({ glider: lastResult.dataset_id, glider_profile: x.glider_profile, argo_profile: x.argo_profile,
                             rank: +btn.dataset.i + 1, data_mode: x.data_mode, dist_m: x.dist_m, dt_s: x.dt_s });
        return;
    }
    const card = e.target.closest('.card');
    if (card && !e.target.closest('a')) map.setView([+card.dataset.lat, +card.dataset.lon], 10);
});

// ---------- Legend ----------
function renderLegend(config) {
    const line = (c, dash = '') => `<svg width="28" height="10"><line x1="1" y1="5" x2="27" y2="5" stroke="${c}" stroke-width="3" ${dash ? `stroke-dasharray="${dash}"` : ''}/></svg>`;
    const box = (c, dash = '') => `<svg width="28" height="14"><rect x="2" y="2" width="24" height="10" fill="${c}" fill-opacity="0.12" stroke="${c}" ${dash ? `stroke-dasharray="${dash}"` : ''}/></svg>`;
    const dot = (c, f) => `<svg width="28" height="14"><circle cx="14" cy="7" r="5" stroke="${c}" stroke-width="1.5" fill="${f}"/></svg>`;
    const r = config.reference_rule;
    $('legend').innerHTML = `
        <div>${box(COLORS.deep)} Deployment, deep enough</div>
        <div>${box(COLORS.shelf)} Deployment, too shallow</div>
        <div>${box(COLORS.unknown, '4,3')} Deployment, depth not checked yet</div>
        <div>${line(COLORS.glider)} Glider dives (track)</div>
        <div>${dot(COLORS.argo, COLORS.argo)} Argo profile nearby</div>
        <div><span class="ref-icon legend-ref"><span>1</span></span> Reference profile (rank)</div>
        <div>${line(COLORS.link, '3,4')} Link to matching glider dive</div>
        <p class="legend-note">Reference rule: within <b>${fmt(r.max_dist_m / 1000)} km</b> and <b>${fmt(r.max_time_days)} days</b> of a dive,
           depth ranges sharing at least <b>${fmt(r.min_overlap_m)} m</b> below <b>${fmt(r.layer_top_m)} m</b>. Best ${r.top_n} shown,
           ranked by distance/${fmt(r.max_dist_m / 1000)} km + time/${fmt(r.max_time_days)} days.</p>
        <p class="legend-note">Gliders: ${esc((config.glider_servers || [config.glider_server]).map(SERVER_LABEL).join(', '))} (lists refreshed hourly).
           Argo: ${config.argo_source === 'erddap' ? 'Ifremer ERDDAP' : esc(config.argo_source)}, re-used for ${fmt(config.argo_fetch_ttl_days)} days once fetched.</p>`;
}

// ---------- Start-up ----------
(async () => {
    renderRecent();
    const url = new URL(window.location);
    if (url.searchParams.get('param')) sensorSelect.value = url.searchParams.get('param');
    try {
        const config = await getJson('/api/config');
        RULE = config.reference_rule;
        renderLegend(config);
        const st = await loadDeployments();
        if (!deployments.length && !st.last_refresh) {
            // catalogue still loading on the server: poll until it arrives
            const poll = setInterval(async () => {
                const s = await loadDeployments().catch(() => ({}));
                if (s.last_refresh || s.last_error) clearInterval(poll);
            }, 10000);
        }
        statusPanel.innerHTML = deployments.length ? '<p class="hint">Click a deployment on the map or in the list.</p>' : '';
        if (url.searchParams.get('glider')) selectDeployment(url.searchParams.get('glider'));
    } catch (err) {
        statusPanel.innerHTML = `<h3>Backend unreachable</h3><p>${esc(err.message)}<br>API: ${esc(API_BASE_URL)}</p>`;
    }
})();
