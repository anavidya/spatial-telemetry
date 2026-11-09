// Sensor check view: one glider dive vs one Argo reference profile.
(() => {
    const API = (window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl) || 'http://localhost:8000';
    // Validated against the #1a252f panel surface (dataviz validator: all checks pass)
    const SERIES = { glider: { name: 'Glider', color: '#2f98d8' }, argo: { name: 'Argo', color: '#e8484a', dash: '6,4' } };
    const STATUS = { ok: ['✓', 'Agrees', 'ok'], watch: ['!', 'Watch', 'watch'], drift: ['✕', 'Possible drift', 'drift'], unknown: ['?', 'No verdict', 'unknown'] };
    const MODE = { D: 'delayed-mode', A: 'adjusted', R: 'real-time' };
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const f = (n, d) => (n == null || isNaN(n)) ? '–' : (n > 0 && d >= 3 ? '+' : '') + Number(n).toFixed(d);
    const fs = (n, d = 4) => (n == null || isNaN(n)) ? '–' : (n >= 0 ? '+' : '') + Number(n).toFixed(d);

    const overlay = $('compare-overlay');
    const close = () => { overlay.hidden = true; };
    $('compare-close').addEventListener('click', close);
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !overlay.hidden) close(); });

    // ---------- tiny SVG line chart with nearest-point hover ----------
    function niceTicks(lo, hi, n = 5) {
        const span = hi - lo || 1, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
        const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
        const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
        return out;
    }
    function lineChart(el, { series, xLabel, yLabel, invertY = false, band = null, fmtX, fmtY, tip }) {
        const W = 420, H = 320, m = { l: 52, r: 14, t: 12, b: 40 };
        const all = series.flatMap(s => s.points);
        if (!all.length) { el.innerHTML = '<p class="hint">No data in the shared layer.</p>'; return; }
        let [x0, x1] = [Math.min(...all.map(p => p[0])), Math.max(...all.map(p => p[0]))];
        let [y0, y1] = [Math.min(...all.map(p => p[1])), Math.max(...all.map(p => p[1]))];
        const px = (x1 - x0) * 0.04 || 0.01, py = (y1 - y0) * 0.04 || 1; x0 -= px; x1 += px; y0 -= py; y1 += py;
        const X = v => m.l + (v - x0) / (x1 - x0) * (W - m.l - m.r);
        const Y = v => invertY ? m.t + (v - y0) / (y1 - y0) * (H - m.t - m.b) : H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b);
        const xt = niceTicks(x0, x1), yt = niceTicks(y0, y1);
        let svg = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(yLabel)} against ${esc(xLabel)}">`;
        if (band) {
            const a = Y(Math.max(band[0], y0)), b = Y(Math.min(band[1], y1));
            svg += `<rect x="${m.l}" y="${Math.min(a, b)}" width="${W - m.l - m.r}" height="${Math.abs(b - a)}" class="band"/>`;
        }
        yt.forEach(v => svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" class="grid"/><text x="${m.l - 6}" y="${Y(v) + 4}" class="tick" text-anchor="end">${fmtY(v)}</text>`);
        xt.forEach(v => svg += `<line y1="${m.t}" y2="${H - m.b}" x1="${X(v)}" x2="${X(v)}" class="grid"/><text x="${X(v)}" y="${H - m.b + 16}" class="tick" text-anchor="middle">${fmtX(v)}</text>`);
        svg += `<text x="${(W + m.l) / 2}" y="${H - 6}" class="axis-label" text-anchor="middle">${esc(xLabel)}</text>`;
        svg += `<text transform="translate(13 ${(H - m.b + m.t) / 2}) rotate(-90)" class="axis-label" text-anchor="middle">${esc(yLabel)}</text>`;
        series.forEach(s => {
            const d = s.points.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
            svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''}/>`;
        });
        svg += `<circle class="hover-dot" r="5" cx="-10" cy="-10"/></svg><div class="chart-tip" hidden></div>`;
        el.innerHTML = svg;
        const svgEl = el.querySelector('svg'), dot = el.querySelector('.hover-dot'), tipEl = el.querySelector('.chart-tip');
        svgEl.addEventListener('mousemove', e => {
            const r = svgEl.getBoundingClientRect(), sx = (e.clientX - r.left) * W / r.width, sy = (e.clientY - r.top) * H / r.height;
            let best = null;
            series.forEach(s => s.points.forEach(p => {
                const d = (X(p[0]) - sx) ** 2 + (Y(p[1]) - sy) ** 2;
                if (!best || d < best.d) best = { d, s, p };
            }));
            if (!best || best.d > 900) { tipEl.hidden = true; dot.setAttribute('cx', -10); return; }
            dot.setAttribute('cx', X(best.p[0])); dot.setAttribute('cy', Y(best.p[1])); dot.setAttribute('fill', best.s.color);
            tipEl.innerHTML = `<span class="key" style="background:${best.s.color}"></span>${esc(best.s.name)}<br>${tip(best.p)}`;
            tipEl.hidden = false;
            tipEl.style.left = `${Math.min(e.clientX - r.left + 12, r.width - 150)}px`;
            tipEl.style.top = `${e.clientY - r.top + 12}px`;
        });
        svgEl.addEventListener('mouseleave', () => { tipEl.hidden = true; dot.setAttribute('cx', -10); });
    }

    function seriesFrom(plot, xKey, yKey, layer) {
        return ['glider', 'argo'].map(k => {
            const d = plot[k], pts = [];
            for (let i = 0; i < d.pres.length; i++) {
                if (layer && (d.pres[i] < layer[0] || d.pres[i] > layer[1])) continue;
                pts.push([d[xKey][i], d[yKey][i], d.pres[i]]);
            }
            if (yKey === 'pres') pts.sort((a, b) => a[1] - b[1]);
            return { ...SERIES[k], points: pts };
        });
    }

    function statRow(label, s, d = 4) {
        if (!s) return `<tr><th>${label}</th><td colspan="3" class="muted">not enough shared levels</td></tr>`;
        return `<tr><th>${label}</th><td class="num">${fs(s.median, d)}</td><td class="num">${fs(s.iqr[0], d)} … ${fs(s.iqr[1], d)}</td><td class="num">${s.n_levels}</td></tr>`;
    }

    // One plain sentence from the main result (salinity at equal potential temperature).
    function plainSummary(sal, layer) {
        if (!sal) return 'Not enough shared water to compare salinity for this pair.';
        const m = sal.median, size = Math.abs(m).toFixed(3);
        const how = Math.abs(m) < 0.0005 ? 'reads <b>the same as</b> the float' : `reads <b>${size} ${m < 0 ? 'lower' : 'higher'}</b> than the float`;
        return `At the same water temperature, the glider's salinity ${how}, compared over ${sal.n_levels} levels between ${layer[0].toFixed(0)} and ${layer[1].toFixed(0)} m.`;
    }

    function render(r, ctx) {
        const v = r.verdict, st = STATUS[v.level] || STATUS.unknown, res = r.result, layer = res.layer;
        const legend = Object.values(SERIES).map(s => `<span class="legend-item"><svg width="26" height="10"><line x1="1" y1="5" x2="25" y2="5" stroke="${s.color}" stroke-width="2.5" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''}/></svg>${s.name}</span>`).join('');
        $('compare-body').innerHTML = `
            <h3 id="compare-title">Sensor check · reference #${ctx.rank}</h3>
            <p class="hint">Glider <b>${esc(r.dataset_id)}</b> dive ${esc(r.glider_profile)} (${esc((r.glider_time || '').slice(0, 16).replace('T', ' '))} UTC)
               vs Argo <b>${esc(r.argo_profile)}</b> (${esc(MODE[r.argo_data_mode] || r.argo_data_mode || '?')}, ${esc((r.argo_time || '').slice(0, 16).replace('T', ' '))} UTC)
               · ${(ctx.dist_m / 1000).toFixed(1)} km, ${(ctx.dt_s / 86400).toFixed(1)} days apart</p>
            <div class="verdict-banner ${st[2]}"><span class="icon" aria-hidden="true">${st[0]}</span><b>${st[1]}.</b> ${esc(v.text)}</div>
            <p class="cmp-summary">${plainSummary(res.theta_levels.salinity, layer)}</p>
            <details class="cmp-details"><summary>Show the numbers</summary>
            <table class="stats">
                <thead><tr><th>Offset, glider − Argo, ${layer[0].toFixed(0)}–${layer[1].toFixed(0)} dbar</th><th>Median</th><th>Middle 50%</th><th>Levels</th></tr></thead>
                <tbody>
                    ${statRow('Salinity at equal potential temperature (main result)', res.theta_levels.salinity)}
                    ${statRow('Salinity at equal depth (cross-check)', res.depth.salinity)}
                    ${statRow('Temperature at equal depth, °C (cross-check)', res.depth.temperature, 3)}
                </tbody>
            </table>
            <p class="hint">Median = the typical difference; middle 50% = where half of the compared levels fall; levels = how many were compared.
               Equal-depth numbers are thrown off when eddies or internal waves move the water, so they are only a cross-check.</p>
            </details>
            <div class="legend-row">${legend}<span class="muted">Shaded: shared layer compared</span></div>
            <div class="charts">
                <figure><figcaption>Potential temperature vs salinity (shared layer)</figcaption><div id="chart-ts" class="chart-box"></div></figure>
                <figure><figcaption>Salinity vs pressure (whole profiles)</figcaption><div id="chart-sp" class="chart-box"></div></figure>
            </div>
            <p class="hint">${r.notes.map(esc).join(' ')} Equal-depth offsets are affected when eddies or internal waves lift the water; the equal-temperature offset is not.
               Thresholds: agrees ≤ ${r.thresholds.ok}, watch ≤ ${r.thresholds.watch}. Took ${r.seconds.total} s.</p>`;
        lineChart($('chart-ts'), {
            series: seriesFrom(r.plot, 'psal', 'theta', layer), xLabel: 'Practical salinity', yLabel: 'Potential temperature (°C)',
            fmtX: v => v.toFixed(2), fmtY: v => v.toFixed(1),
            tip: p => `S ${p[0].toFixed(4)} · θ ${p[1].toFixed(3)} °C<br>${p[2].toFixed(0)} dbar`,
        });
        lineChart($('chart-sp'), {
            series: seriesFrom(r.plot, 'psal', 'pres', null), xLabel: 'Practical salinity', yLabel: 'Pressure (dbar)',
            invertY: true, band: layer, fmtX: v => v.toFixed(2), fmtY: v => v.toFixed(0),
            tip: p => `S ${p[0].toFixed(4)} at ${p[1].toFixed(0)} dbar`,
        });
    }

    window.openCompare = async ctx => {
        overlay.hidden = false;
        $('compare-body').innerHTML = `<h3 id="compare-title">Sensor check · reference #${ctx.rank}</h3>
            <p>Fetching the full glider dive and Argo profile… <span class="elapsed">0 s</span></p>`;
        const t0 = Date.now(), timer = setInterval(() => {
            const el = $('compare-body').querySelector('.elapsed'); if (el) el.textContent = `${Math.round((Date.now() - t0) / 1000)} s`;
        }, 1000);
        try {
            const q = new URLSearchParams({ glider: ctx.glider, glider_profile: ctx.glider_profile, argo_profile: ctx.argo_profile });
            const res = await fetch(`${API}/api/compare?${q}`);
            const r = await res.json();
            if (r.error) throw new Error(r.error);
            render(r, ctx);
        } catch (err) {
            $('compare-body').innerHTML = `<h3 id="compare-title">Sensor check · reference #${ctx.rank}</h3><p class="warn">Could not compare: ${esc(err.message)}</p>`;
        } finally { clearInterval(timer); }
    };
})();
