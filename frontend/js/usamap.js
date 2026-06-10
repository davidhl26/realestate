// USA states tile map — each state rendered as a colored hexagon arranged in
// a geographic grid. Compact, clear, no external dependencies.

// Grid coordinates (row, col) for each state. Approximates US geography.
// 0,0 is top-left. Larger row = more south, larger col = more east.
window.USA_STATE_GRID = {
  // West coast
  "WA":[1,1],  "OR":[2,1],  "CA":[3,1],
  // Mountain
  "ID":[2,2],  "NV":[3,2],
  "MT":[1,3],  "WY":[2,3],  "UT":[3,3],  "AZ":[4,3],
  // Plains
  "ND":[1,4],  "SD":[2,4],  "CO":[3,4],  "NM":[4,4],
  "MN":[1,5],  "NE":[2,5],  "KS":[3,5],  "OK":[4,5],  "TX":[5,5],
  // Midwest
  "WI":[1,6],  "IA":[2,6],  "MO":[3,6],  "AR":[4,6],  "LA":[5,6],
  "MI":[1,7],  "IL":[2,7],  "KY":[3,7],  "TN":[4,7],  "MS":[5,7],
  // East
  "IN":[2,8],  "OH":[2,9],  "WV":[3,9],  "VA":[3,10],
  "AL":[5,8],  "GA":[5,9],  "SC":[4,10], "NC":[4,9],  "FL":[6,9],
  // Northeast
  "PA":[2,10], "NY":[1,10], "VT":[1,11], "NH":[1,12], "ME":[0,12],
  "MA":[2,12], "RI":[2,13], "CT":[3,11], "NJ":[3,12], "DE":[3,13], "MD":[4,11], "DC":[4,12],
  // Outliers (placed bottom-left)
  "AK":[5,0],  "HI":[6,1],
};

(function () {
  const HEX_W = 50;     // horizontal width of each hex slot
  const HEX_H = 56;     // vertical height (taller for staggered look)
  const PAD   = 6;

  /** Render the SVG tile map into the container.
   *  states: array of state objects from /api/board/states-map
   *  metric: which field to color by ("market_score" / "yoy_pct" / etc)
   *  onClick: callback(state) when a tile is clicked
   */
  window.renderUsaMap = function renderUsaMap(container, states, metric, onClick) {
    if (!container) return;
    const grid = window.USA_STATE_GRID;
    // Build lookup
    const dataByCode = {};
    states.forEach(s => { dataByCode[s.code] = s; });

    // Compute min/max for the metric → color scale
    let values = states.map(s => Number(s[metric] || 0)).filter(v => !isNaN(v));
    let vMin = Math.min(...values), vMax = Math.max(...values);
    if (vMin === vMax) { vMin = 0; vMax = vMax || 1; }

    // Calculate SVG bounds
    let maxRow = 0, maxCol = 0;
    Object.values(grid).forEach(([r, c]) => {
      if (r > maxRow) maxRow = r;
      if (c > maxCol) maxCol = c;
    });
    const totalW = (maxCol + 1) * (HEX_W + PAD) + HEX_W;
    const totalH = (maxRow + 1) * HEX_H + HEX_W;

    const colorFor = (v) => {
      if (v == null || isNaN(v)) return "#cbd5e1";
      const t = (v - vMin) / (vMax - vMin || 1);
      // gradient: red → orange → yellow → light-green → green
      // For "lower is better" metrics, invert? — handled by caller via metric choice
      const stops = [
        [0.00, [239, 68, 68]],   // red
        [0.25, [245, 158, 11]],  // orange
        [0.50, [251, 191, 36]],  // amber
        [0.75, [132, 204, 22]],  // lime
        [1.00, [34, 197, 94]],   // green
      ];
      for (let i = 0; i < stops.length - 1; i++) {
        const [t0, c0] = stops[i], [t1, c1] = stops[i + 1];
        if (t <= t1) {
          const r = (t - t0) / (t1 - t0);
          const c = c0.map((ch, j) => Math.round(ch + (c1[j] - ch) * r));
          return `rgb(${c[0]},${c[1]},${c[2]})`;
        }
      }
      const last = stops[stops.length - 1][1];
      return `rgb(${last[0]},${last[1]},${last[2]})`;
    };

    let svg = `<svg viewBox="0 0 ${totalW} ${totalH}" xmlns="http://www.w3.org/2000/svg" style="font-family: inherit;">`;
    for (const [code, [row, col]] of Object.entries(grid)) {
      // Offset every other row for hex-staggered look
      const x = col * (HEX_W + PAD) + (row % 2 === 0 ? 0 : (HEX_W + PAD) / 2);
      const y = row * HEX_H;
      const data = dataByCode[code] || {code, name: code};
      const v = Number(data[metric] || 0);
      const fill = colorFor(v);
      const tooltip = `${data.name || code} — ${formatMetric(metric, v)}` +
        (data.my_deals_count ? ` · ${data.my_deals_count} deals` : "");
      svg += `<g class="usamap-state" data-code="${code}" transform="translate(${x},${y})">
        <title>${escapeXml(tooltip)}</title>
        <rect x="0" y="0" width="${HEX_W}" height="${HEX_W}" rx="6" fill="${fill}"/>
        <text x="${HEX_W/2}" y="${HEX_W/2 - 2}" text-anchor="middle" font-size="14" font-weight="700"
              fill="${isLight(fill) ? '#1f2937' : 'white'}" pointer-events="none">${code}</text>`;
      // Badge for deals count
      if (data.my_deals_count > 0) {
        svg += `<circle cx="${HEX_W-9}" cy="9" r="9" fill="white" stroke="#1f2937" stroke-width="1.5"/>
                <text x="${HEX_W-9}" y="13" text-anchor="middle" font-size="11" font-weight="700" fill="#1f2937" pointer-events="none">${data.my_deals_count}</text>`;
      }
      svg += `</g>`;
    }
    svg += `</svg>`;

    container.innerHTML = svg;
    // Attach click handlers
    container.querySelectorAll(".usamap-state").forEach(el => {
      el.addEventListener("click", () => {
        const code = el.getAttribute("data-code");
        container.querySelectorAll(".usamap-state.active").forEach(e => e.classList.remove("active"));
        el.classList.add("active");
        if (typeof onClick === "function") onClick(dataByCode[code]);
      });
    });
  };

  function formatMetric(metric, v) {
    if (metric === "median_price")    return "$" + Math.round(v/1000) + "K";
    if (metric === "yoy_pct")         return (v > 0 ? "+" : "") + v.toFixed(1) + "%";
    if (metric === "my_total_profit") return (v >= 0 ? "+$" : "-$") + Math.abs(Math.round(v/1000)) + "K";
    if (metric === "my_deals_count")  return v + " deals";
    if (metric === "market_score")    return v + "/100";
    return String(v);
  }
  window.usamapFormatMetric = formatMetric;

  function isLight(rgb) {
    const m = rgb.match(/\d+/g);
    if (!m) return false;
    const [r, g, b] = m.map(Number);
    const lum = (0.299 * r + 0.587 * g + 0.114 * b);
    return lum > 165;
  }

  function escapeXml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
})();
