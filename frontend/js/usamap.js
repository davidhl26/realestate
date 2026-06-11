// USA choropleth — real geographic state shapes (from usa-geo.js), colored by
// a metric, clickable to select a state. Keeps the same renderUsaMap(container,
// states, metric, onClick) signature the app calls.

(function () {
  function formatMetric(metric, v) {
    if (v == null || isNaN(v)) return "n/a";
    if (metric === "median_price")    return "$" + Math.round(v / 1000) + "K";
    if (metric === "yoy_pct")         return (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
    if (metric === "my_total_profit") return (v >= 0 ? "+$" : "-$") + Math.abs(Math.round(v / 1000)) + "K";
    if (metric === "my_deals_count")  return v + " deal" + (v === 1 ? "" : "s");
    if (metric === "market_score")    return v + "/100";
    return String(v);
  }
  window.usamapFormatMetric = formatMetric;

  // red → orange → amber → lime → green
  function colorFor(t) {
    if (t == null || isNaN(t)) return "#e2e8f0";
    t = Math.max(0, Math.min(1, t));
    const stops = [
      [0.00, [239, 68, 68]],
      [0.25, [245, 158, 11]],
      [0.50, [251, 191, 36]],
      [0.75, [132, 204, 22]],
      [1.00, [34, 197, 94]],
    ];
    for (let i = 0; i < stops.length - 1; i++) {
      const [t0, c0] = stops[i], [t1, c1] = stops[i + 1];
      if (t <= t1) {
        const r = (t - t0) / (t1 - t0 || 1);
        const c = c0.map((ch, j) => Math.round(ch + (c1[j] - ch) * r));
        return `rgb(${c[0]},${c[1]},${c[2]})`;
      }
    }
    const last = stops[stops.length - 1][1];
    return `rgb(${last[0]},${last[1]},${last[2]})`;
  }

  function isLight(rgb) {
    const m = (rgb || "").match(/\d+/g);
    if (!m) return true;
    const [r, g, b] = m.map(Number);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 165;
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  window.renderUsaMap = function renderUsaMap(container, states, metric, onClick) {
    if (!container) return;
    const PATHS = window.USA_MAP_PATHS;
    const VIEWBOX = window.USA_MAP_VIEWBOX || "0 0 1000 600";
    if (!PATHS) {
      container.innerHTML = '<div class="muted" style="padding:30px;text-align:center;">Carte indisponible (usa-geo.js manquant)</div>';
      return;
    }

    const byCode = {};
    (states || []).forEach(s => { byCode[s.code] = s; });

    // min/max for the chosen metric → normalized color
    const vals = (states || []).map(s => Number(s[metric])).filter(v => !isNaN(v));
    let vMin = Math.min(...vals), vMax = Math.max(...vals);
    if (!isFinite(vMin) || vMin === vMax) { vMin = 0; vMax = vMax || 1; }

    let svg = `<svg viewBox="${VIEWBOX}" xmlns="http://www.w3.org/2000/svg" class="usa-svg" preserveAspectRatio="xMidYMid meet">`;
    for (const [code, d] of Object.entries(PATHS)) {
      const st = byCode[code];
      const v = st ? Number(st[metric]) : NaN;
      const t = isNaN(v) ? null : (v - vMin) / (vMax - vMin);
      const fill = colorFor(t);
      const name = st ? st.name : code;
      const tip = `${name} — ${formatMetric(metric, v)}` +
        (st && st.my_deals_count ? ` · ${st.my_deals_count} deal${st.my_deals_count === 1 ? "" : "s"}` : "");
      svg += `<path d="${d}" class="usamap-state" data-code="${code}" fill="${fill}">`
           + `<title>${esc(tip)}</title></path>`;
    }
    svg += `</svg>`;
    container.innerHTML = svg;

    const svgEl = container.querySelector("svg");
    const NS = "http://www.w3.org/2000/svg";

    // Labels + deal-count badges placed at each path's bbox center. Labels only
    // where the state is big enough to fit text (avoids clutter on tiny NE states).
    svgEl.querySelectorAll("path.usamap-state").forEach(p => {
      const code = p.getAttribute("data-code");
      const st = byCode[code];
      let bb;
      try { bb = p.getBBox(); } catch { return; }
      const cx = bb.x + bb.width / 2, cy = bb.y + bb.height / 2;
      const big = bb.width > 26 && bb.height > 16;
      if (big) {
        const t = document.createElementNS(NS, "text");
        t.setAttribute("x", cx); t.setAttribute("y", cy + 4);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("class", "usamap-label");
        t.setAttribute("fill", isLight(p.getAttribute("fill")) ? "#1f2937" : "#ffffff");
        t.textContent = code;
        t.style.pointerEvents = "none";
        svgEl.appendChild(t);
      }
      if (st && st.my_deals_count > 0) {
        const bx = cx + (big ? 11 : 0), by = big ? cy - 13 : cy;
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("cx", bx); c.setAttribute("cy", by); c.setAttribute("r", 7);
        c.setAttribute("fill", "#111827"); c.setAttribute("stroke", "#fff"); c.setAttribute("stroke-width", "1.3");
        c.style.pointerEvents = "none";
        const tt = document.createElementNS(NS, "text");
        tt.setAttribute("x", bx); tt.setAttribute("y", by + 3);
        tt.setAttribute("text-anchor", "middle"); tt.setAttribute("class", "usamap-badge-text");
        tt.setAttribute("fill", "#fff"); tt.textContent = st.my_deals_count;
        tt.style.pointerEvents = "none";
        svgEl.appendChild(c); svgEl.appendChild(tt);
      }
    });

    // Click selection
    svgEl.querySelectorAll("path.usamap-state").forEach(p => {
      p.addEventListener("click", () => {
        svgEl.querySelectorAll("path.usamap-state.active").forEach(e => e.classList.remove("active"));
        p.classList.add("active");
        const st = byCode[p.getAttribute("data-code")];
        if (st && typeof onClick === "function") onClick(st);
      });
    });
  };
})();
