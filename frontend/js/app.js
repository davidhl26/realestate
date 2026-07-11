// Flip Board — frontend logic
(function () {
  "use strict";

  // ============== UTIL ==============
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ============== AUTH ==============
  function _showLogin() {
    const ov = $("#login-overlay");
    if (ov) { ov.style.display = "flex"; setTimeout(() => $("#login-password")?.focus(), 50); }
  }
  window.addEventListener("fb-auth-required", _showLogin);
  $("#login-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#login-error"); err.textContent = "";
    try {
      await API.login($("#login-password").value);
      location.reload();
    } catch { err.textContent = "Incorrect password."; }
  });
  (async () => {
    try {
      const s = await API.authStatus();
      if (s && s.required && !s.authed) _showLogin();
    } catch {}
  })();
  window.fbLogout = async () => { try { await API.logout(); } catch {} location.reload(); };

  // ============== MOBILE NAV DRAWER ==============
  $("#nav-toggle")?.addEventListener("click", () => document.body.classList.toggle("nav-open"));
  $("#nav-backdrop")?.addEventListener("click", () => document.body.classList.remove("nav-open"));
  $$(".nav-link").forEach(a => a.addEventListener("click", () => document.body.classList.remove("nav-open")));

  function fmtMoney(v, signed) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const sign = signed && v > 0 ? "+" : (v < 0 ? "-" : "");
    return sign + "$" + Math.abs(Math.round(v)).toLocaleString("en-US");
  }
  function fmtPct(v, signed) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const sign = signed && v > 0 ? "+" : "";
    return sign + v.toFixed(1) + "%";
  }
  function moneyClass(v) {
    if (v > 0) return "money positive";
    if (v < 0) return "money negative";
    return "money";
  }
  function scoreColor(s) {
    if (s >= 70) return "#10B981";
    if (s >= 55) return "#F59E0B";
    if (s >= 40) return "#F97316";
    return "#EF4444";
  }
  // Safety risk grade A–F → color + badge
  function riskColor(g) {
    return { A: "#10B981", B: "#84CC16", C: "#F59E0B", D: "#F97316", F: "#EF4444" }[g] || "#9CA3AF";
  }
  function riskBadge(d) {
    const g = d.risk_grade;
    if (!g) return "";
    const brk = (d.deal_breakers && d.deal_breakers.length) ? "⛔" : "";
    return `<span class="risk-badge" style="background:${riskColor(g)}" title="Safety: grade ${g}${brk ? " — deal-breaker" : ""}">${brk}🛡 ${g}</span>`;
  }
  function dealBreakerStrip(d) {
    const n = (d.deal_breakers && d.deal_breakers.length) || 0;
    if (!n) return "";
    return `<div class="deal-card-breaker" title="${escape(d.deal_breakers.join(' · '))}">⛔ ${n} deal-breaker${n > 1 ? "s" : ""} — ${escape(d.deal_breakers[0])}</div>`;
  }
  // Compact Zillow activity line for cards. Days-on-Zillow is editable inline
  // (click → type once → it auto-increments daily). Views shown if set.
  function cardZillowLine(d) {
    const dom = (d.days_on_market != null && d.days_on_market !== "") ? d.days_on_market : "";
    const daysChip = `📅 <span class="card-dom-edit" data-id="${escape(d.id)}" data-value="${dom}" title="Days on Zillow — click to enter, then it auto-increments each day">${dom !== "" ? dom : "—"}</span> days on Zillow`;
    let line = daysChip;
    if (d.page_view_count != null && d.page_view_count !== "")
      line += ` · 👁 ${Number(d.page_view_count).toLocaleString("en-US")} views`;
    return `<div class="deal-card-zillow">${line}</div>`;
  }
  // Inline edit of days-on-Zillow straight from a card (no need to open the deal)
  function _attachCardDomEdit(container) {
    $$(".card-dom-edit", container).forEach(el => el.addEventListener("click", e => {
      e.stopPropagation();
      if (el.querySelector("input")) return;
      const id = el.dataset.id, cur = el.dataset.value;
      const input = document.createElement("input");
      input.type = "number"; input.min = "0"; input.value = cur || "";
      input.style.cssText = "width:56px;font-size:11px;padding:1px 4px;";
      el.textContent = ""; el.appendChild(input); input.focus(); input.select();
      const save = async () => {
        const raw = input.value.trim();
        const val = raw === "" ? null : Number(raw);
        if (raw !== "" && (isNaN(val) || val < 0)) { el.textContent = cur || "—"; return; }
        try {
          await API.patchDeal(id, { days_on_market: val });
          await refreshDeals();  // re-render with the live value + anchor
        } catch (err) { el.textContent = cur || "—"; toast(err.message, "error"); }
      };
      input.addEventListener("keydown", ev => {
        if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
        else if (ev.key === "Escape") { ev.preventDefault(); el.textContent = cur || "—"; }
      });
      input.addEventListener("blur", save);
    }));
  }
  function signalPillClass(sig) {
    sig = (sig || "").toUpperCase();
    if (sig.includes("SLAM") || sig.includes("GOOD")) return "green";
    if (sig.includes("POSSIBLE")) return "yellow";
    if (sig.includes("RISKY") || sig.includes("MARGINAL")) return "orange";
    if (sig.includes("NO DEAL") || sig.includes("PASS") || sig.includes("AVOID")) return "red";
    return "gray";
  }
  function toast(msg, kind = "") {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast " + kind;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 3500);
  }
  function shortAddr(a) {
    if (!a) return "—";
    return (a.split(",")[0] || a).slice(0, 30);
  }
  function escape(s) {
    return (s == null ? "" : String(s))
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ============== STATE ==============
  const state = {
    deals: [],
    aggregates: null,
    currentDealId: null,   // the deal currently being VIEWED
    formDealId: null,      // the deal the Add/Edit form saves to (null = new deal)
    sortKey: "score",
    sortDir: -1,
    charts: {},
    viewMode: localStorage.getItem("flip-view-mode") || "cards",
    theme: localStorage.getItem("flip-theme") || "light",
    searchQ: "",
    cityFilter: "",
    dealsSort: localStorage.getItem("flip-deals-sort") || "score",
    gallery: [],
  };

  // ============== THEME ==============
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("flip-theme", t);
    state.theme = t;
    $("#theme-label").textContent = t === "dark" ? "Light mode" : "Dark mode";
    // Re-render charts with new colors
    if (state.deals.length) refreshDashboard();
  }
  applyTheme(state.theme);
  $("#theme-toggle")?.addEventListener("click", () => {
    applyTheme(state.theme === "dark" ? "light" : "dark");
  });

  // ============== NAVIGATION ==============
  // Grouped nav: several views live under one sidebar entry, shown via a
  // sub-tab strip (keeps the working views intact, just collapses the nav).
  const _VIEW_GROUP = { add: "sourcing", search: "sourcing", batch: "sourcing",
                        watch: "sourcing",
                        auction: "auctions", skiptrace: "auctions" };
  const _GROUP_TABS = {
    sourcing: [
      { v: "add", label: "➕ Add" },
      { v: "search", label: "🔎 Search & watch" },
    ],
    auctions: [
      { v: "auction", label: "🏛 Auctions + max bid" },
      { v: "skiptrace", label: "📇 Skip Trace" },
    ],
  };
  function _renderGroupTabs(name) {
    const host = $("#group-tabs"); if (!host) return;
    const g = _VIEW_GROUP[name];
    if (!g) { host.style.display = "none"; host.innerHTML = ""; return; }
    host.style.display = "flex";
    host.innerHTML = _GROUP_TABS[g].map(t =>
      `<button class="group-tab ${t.v === name ? "active" : ""}" data-gtab="${t.v}">${t.label}</button>`).join("");
    host.querySelectorAll("[data-gtab]").forEach(b =>
      b.addEventListener("click", () => showView(b.dataset.gtab)));
  }

  function showView(name) {
    $$(".view").forEach(v => v.classList.remove("active"));
    const el = $("#view-" + name);
    if (el) el.classList.add("active");
    const grp = _VIEW_GROUP[name];
    $$(".nav-link").forEach(a => a.classList.toggle("active",
      a.dataset.view === name || (a.dataset.group && a.dataset.group === grp)));
    _renderGroupTabs(name);
    if (name === "dashboard") refreshDashboard();
    if (name === "deals") refreshDeals();
    if (name === "settings") { refreshCookies(); refreshAiConfig(); refreshBrowserSessionStatus(); }
    if (name === "crm") refreshCrmView();
    if (name === "leads") {
      // Leads view removed — redirect to CRM (where the leads kanban lives)
      showView("crm");
      return;
    }
    if (name === "batch") refreshBatchView();
    if (name === "skiptrace") refreshSkipTraceView();
    if (name === "usamap") refreshUsaMapView();
    if (name === "auction" && typeof renderWatchlist === "function") setTimeout(renderWatchlist, 50);
    if (name === "watch" && typeof refreshWatchView === "function") setTimeout(refreshWatchView, 50);
    if (name === "search" && typeof window._renderSearchWatches === "function") setTimeout(window._renderSearchWatches, 50);
    if (name === "radar" && typeof refreshRadarView === "function") refreshRadarView();
    if (name === "add") {
      // Entering the form defaults to a NEW deal. The edit flow re-binds
      // formDealId to the edited deal AFTER calling showView("add").
      state.formDealId = null;
      if (typeof loadRecentPdfs === "function") loadRecentPdfs();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
    // FAB visibility — hide on add/detail views
    const fab = $("#fab");
    if (fab) fab.style.display = (name === "add" || name === "detail") ? "none" : "flex";
    // Chat FAB always visible — works from any view via openChatBulletproof
    // (auto-picks a deal if none selected)
    // Close chat panel when leaving detail
    if (name !== "detail") $("#chat-panel").style.display = "none";
  }

  $$(".nav-link").forEach(a => {
    a.addEventListener("click", e => {
      e.preventDefault();
      if (a.dataset.view === "add") resetDealForm();  // start a blank new deal
      showView(a.dataset.view);
    });
  });

  // Clear the Add/Edit form back to a blank NEW deal (no id binding, no stale
  // fields, no leftover scrape/search status).
  function resetDealForm() {
    const f = $("#deal-form");
    if (f) { f.reset(); f._extraFields = {}; }
    state.formDealId = null;
    const ss = $("#scrape-status"); if (ss) { ss.textContent = ""; ss.className = "status-line"; }
    const as = $("#address-search-status"); if (as) { as.textContent = ""; as.className = "status-line"; }
    const su = $("#scrape-url"); if (su) su.value = "";
  }
  window._resetDealForm = resetDealForm;

  // ============== DASHBOARD ==============
  async function refreshDashboard() {
    let deals = [];
    try { deals = await API.listDeals(); state.deals = deals; }
    catch (e) { toast(e.message, "error"); return; }
    // Stats + side panels are best-effort — never let one failure blank the cockpit.
    try { const agg = await API.aggregates(); state.aggregates = agg; renderStats(agg); } catch {}
    let watchlist = [], leads = [];
    try { watchlist = await API.auctionWatchlist(); } catch {}
    try { leads = await API.leadsList(); } catch {}
    try { renderCockpit(deals, watchlist, leads); } catch (e) { console.error("cockpit", e); }
  }

  // ============== DASHBOARD COCKPIT (action-oriented) ==============
  const _RISK_RANK = { F: 5, D: 4, C: 3, B: 2, A: 1 };
  function renderCockpit(deals, watchlist, leads) {
    deals = deals || []; watchlist = watchlist || []; leads = leads || [];
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const todayISO = today.toISOString().slice(0, 10);
    const setCount = (id, n) => { const e = $(id); if (e) e.textContent = n ? `(${n})` : ""; };
    const empty = msg => `<p class="muted" style="font-size:13px; margin:6px 0 0;">${msg}</p>`;

    // 1) To review — deal-breakers or F/D risk
    const alerts = deals
      .filter(d => (d.deal_breakers && d.deal_breakers.length) || ["F", "D"].includes(d.risk_grade))
      .sort((a, b) => ((b.deal_breakers?.length ? 100 : 0) + (_RISK_RANK[b.risk_grade] || 0))
                    - ((a.deal_breakers?.length ? 100 : 0) + (_RISK_RANK[a.risk_grade] || 0)))
      .slice(0, 8);
    setCount("#dash-alerts-count", alerts.length);
    $("#dash-alerts").innerHTML = alerts.length ? alerts.map(d => {
      const reason = (d.deal_breakers && d.deal_breakers[0]) || "High risk";
      return `<div class="cockpit-row" data-open="${escape(d.id)}">
        <span class="risk-badge" style="background:${riskColor(d.risk_grade)};">🛡 ${d.risk_grade || "?"}</span>
        <div class="cockpit-row-main"><div class="cockpit-row-title">${escape(d.address || "?")}</div>
          <div class="cockpit-row-sub">${escape(reason)}</div></div>
      </div>`;
    }).join("") : empty("✅ No risky deals — nothing urgent to check.");

    // 2) Best opportunities — clean risk + high score
    const opps = deals
      .filter(d => !(d.deal_breakers && d.deal_breakers.length) && ["A", "B"].includes(d.risk_grade || "A"))
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, 6);
    setCount("#dash-opps-count", opps.length);
    $("#dash-opps").innerHTML = opps.length ? opps.map(d => `
      <div class="cockpit-row" data-open="${escape(d.id)}">
        <span class="score-badge" style="background:${scoreColor(d.score)};">${d.score ?? "?"}</span>
        <div class="cockpit-row-main"><div class="cockpit-row-title">${escape(d.address || "?")}</div>
          <div class="cockpit-row-sub">${escape(d.signal || "")}${d.net_profit != null ? " · " + fmtMoney(d.net_profit, true) : ""}</div></div>
      </div>`).join("") : empty("Add deals to see your best leads here.");

    // 3) Upcoming auctions (≤21 days)
    const soon = watchlist.map(w => {
      let dleft = null;
      if (w.auction_date) { const d = new Date(w.auction_date + "T00:00:00"); dleft = Math.round((d - today) / 86400000); }
      return { ...w, dleft };
    }).filter(w => w.dleft != null && w.dleft >= 0 && w.dleft <= 21).sort((a, b) => a.dleft - b.dleft);
    setCount("#dash-auctions-count", soon.length);
    $("#dash-auctions").innerHTML = soon.length ? soon.map(w => `
      <div class="cockpit-row" data-auction="1">
        <span class="cockpit-jx ${w.dleft <= 7 ? "urgent" : ""}">${w.dleft}d</span>
        <div class="cockpit-row-main"><div class="cockpit-row-title">${escape(w.address || "?")}</div>
          <div class="cockpit-row-sub">${w.max_bid ? "Max bid " + fmtMoney(w.max_bid) : ""} · ${escape(w.auction_date || "")}</div></div>
      </div>`).join("") : empty("No tracked auctions in the next 21 days.");

    // 4) Relances dues
    const due = leads.filter(l => l.follow_up && l.follow_up <= todayISO)
      .sort((a, b) => String(a.follow_up).localeCompare(String(b.follow_up)));
    setCount("#dash-followups-count", due.length);
    $("#dash-followups").innerHTML = due.length ? due.map(l => `
      <div class="cockpit-row" data-crm="1">
        <span class="cockpit-jx urgent">📅</span>
        <div class="cockpit-row-main"><div class="cockpit-row-title">${escape(l.address || l.name || "Lead")}</div>
          <div class="cockpit-row-sub">Follow-up due ${escape(l.follow_up)} · ${escape(l.status || "")}</div></div>
      </div>`).join("") : empty("No overdue follow-ups. 👍");

    // Wiring
    $$("#view-dashboard .cockpit-row").forEach(row => row.addEventListener("click", () => {
      if (row.dataset.open) openDeal(row.dataset.open);
      else if (row.dataset.auction) showView("auction");
      else if (row.dataset.crm) showView("crm");
    }));
  }

  // ============== DASHBOARD DEAL SELECTOR ==============
  function renderDealSelector(deals) {
    const sel = $("#dash-deal-picker");
    if (!sel) return;
    if (!deals.length) {
      sel.innerHTML = '<option value="">No deals yet</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    const sorted = [...deals].sort((a, b) => (b.score || 0) - (a.score || 0));
    sel.innerHTML = '<option value="">— Select a deal to inspect —</option>' +
      sorted.map(d => {
        const lbl = `${d.address || 'Untitled'} · ${d.score}/100 ${d.grade || ''} · ${d.signal || ''}`;
        return `<option value="${escape(d.id)}">${escape(lbl)}</option>`;
      }).join("");
  }

  function inspectDealOnDashboard(dealId) {
    const d = state.deals.find(x => x.id === dealId);
    const panel = $("#focused-deal-panel");
    const clearBtn = $("#dash-clear-selection");
    if (!d) {
      panel.style.display = "none";
      clearBtn.style.display = "none";
      $("#dash-deal-picker").value = "";
      return;
    }
    // Sync dropdown value
    $("#dash-deal-picker").value = dealId;
    clearBtn.style.display = "inline-flex";

    const sigCls = signalPillClass(d.signal);
    const recStr = (d.recommended_strategy || []).join(' / ');
    panel.style.display = "block";
    panel.innerHTML = `
      <div class="focused-deal">
        <div class="focused-deal-img" style="${d.image ? `background-image:url('${escape(d.image)}')` : ''}">
          ${!d.image ? '<div class="focused-deal-img-placeholder">🏡</div>' : ''}
        </div>
        <div class="focused-deal-body">
          <div class="focused-deal-header">
            <div class="focused-deal-address">${escape(d.address || 'Untitled')}</div>
            <div style="display:flex; gap:6px; align-items:center;">
              <span class="score-badge" style="background:${scoreColor(d.score)}">${d.score}</span>
              <span class="pill ${sigCls}">${escape(d.signal || '')}</span>
            </div>
          </div>
          <div class="focused-deal-meta">
            ${escape(d.city || '')}, ${escape(d.state || '')}
            ${d.beds ? ` · ${d.beds}bd/${d.baths || '?'}ba` : ''}
            ${d.sqft ? ` · ${d.sqft.toLocaleString()}sf` : ''}
            ${d.year_built ? ` · built ${d.year_built}` : ''}
            ${recStr ? ` · <strong style="color:var(--text);">${escape(recStr)}</strong>` : ''}
          </div>
          <div class="focused-deal-stats">
            <div class="fd-stat"><div class="lbl">Purchase</div><div class="val">${fmtMoney(d.purchase_price)}</div></div>
            <div class="fd-stat"><div class="lbl">ARV</div><div class="val">${fmtMoney(d.arv_base)}</div></div>
            <div class="fd-stat"><div class="lbl">Rehab</div><div class="val">${fmtMoney(d.rehab_base)}</div></div>
            <div class="fd-stat"><div class="lbl">Net profit</div><div class="val ${d.net_profit >= 0 ? 'green' : 'red'}">${fmtMoney(d.net_profit, true)}</div></div>
            <div class="fd-stat"><div class="lbl">ROI</div><div class="val">${fmtPct(d.roi)}</div></div>
            <div class="fd-stat"><div class="lbl">Cap rate</div><div class="val">${fmtPct(d.cap_rate)}</div></div>
            <div class="fd-stat"><div class="lbl">70% rule</div><div class="val ${d.rule_70_pass ? 'green' : 'red'}">${d.rule_70_pass ? 'PASS' : 'FAIL'}</div></div>
            <div class="fd-stat"><div class="lbl">Status</div><div class="val" style="font-size:13px; text-transform:uppercase;">${escape(d.status || 'evaluating')}</div></div>
          </div>
          <div class="focused-deal-actions">
            <button class="btn primary" data-act="open">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" width="13" height="13"><path d="M15 3h6v6M10 14L21 3M21 14v7H3V3h7" stroke-linecap="round" stroke-linejoin="round"/></svg>
              Open full detail
            </button>
            <button class="btn" data-act="pdf">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6" stroke-linecap="round"/></svg>
              PDF
            </button>
            <button class="btn" data-act="chat">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z" stroke-linejoin="round"/></svg>
              Ask AI
            </button>
            ${d.source_url ? `<a class="btn" href="${escape(d.source_url)}" target="_blank">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="11" height="11"><path d="M15 3h6v6M10 14L21 3" stroke-linecap="round" stroke-linejoin="round"/></svg>
              Source
            </a>` : ''}
          </div>
        </div>
      </div>
    `;
    $$("[data-act]", panel).forEach(b => {
      b.addEventListener("click", async () => {
        const act = b.dataset.act;
        if (act === "open") openDeal(dealId);
        else if (act === "pdf") {
          await openDeal(dealId);
          setTimeout(() => $("#pdf-btn").click(), 400);
        }
        else if (act === "chat") {
          await openDeal(dealId);
          setTimeout(() => $("#chat-fab").click(), 400);
        }
      });
    });
    // Smooth scroll into view
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  $("#dash-deal-picker")?.addEventListener("change", e => {
    inspectDealOnDashboard(e.target.value);
  });
  $("#dash-clear-selection")?.addEventListener("click", () => {
    inspectDealOnDashboard(null);
  });

  function renderStats(agg) {
    // Nav badge with deal count
    const badge = $("#nav-deal-count");
    if (badge) badge.textContent = (agg && agg.count) ? agg.count : "";
    const grid = $("#stats-grid");
    if (!agg || !agg.count) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1;">
        <div class="empty-ico">🏠</div>
        <h3>No deals yet</h3>
        <p>Click "New deal" or paste a Zillow / ispeedtolead URL to begin.</p>
      </div>`;
      return;
    }
    grid.innerHTML = `
      <div class="stat-card"><div class="label">Total Deals</div><div class="value">${agg.count}</div></div>
      <div class="stat-card ${agg.total_profit >= 0 ? "green" : "red"}">
        <div class="label">Aggregate Profit</div>
        <div class="value">${fmtMoney(agg.total_profit, true)}</div>
        <div class="delta">If all flipped</div>
      </div>
      <div class="stat-card"><div class="label">Avg ROI</div><div class="value">${fmtPct(agg.avg_roi)}</div></div>
      <div class="stat-card"><div class="label">Avg Cap Rate</div><div class="value">${fmtPct(agg.avg_cap_rate)}</div></div>
      <div class="stat-card"><div class="label">Pass 70% Rule</div><div class="value">${agg.passing_70_rule}/${agg.count}</div></div>
      <div class="stat-card"><div class="label">Total Capital</div><div class="value">${fmtMoney(agg.total_capital)}</div></div>
    `;
  }

  function chartTextColor() {
    return state.theme === "dark" ? "#CBD5E1" : "#475569";
  }
  function chartGridColor() {
    return state.theme === "dark" ? "rgba(255,255,255,0.06)" : "rgba(15,23,42,0.06)";
  }

  function destroyChart(key) {
    if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
  }

  function renderProfitChart(deals) {
    destroyChart("profit");
    const ctx = $("#chart-profit");
    if (!ctx || !deals.length) return;
    const top = [...deals].sort((a, b) => (b.net_profit || 0) - (a.net_profit || 0)).slice(0, 10);
    state.charts.profit = new Chart(ctx, {
      type: "bar",
      data: {
        labels: top.map(d => shortAddr(d.address)),
        datasets: [{
          data: top.map(d => d.net_profit || 0),
          backgroundColor: top.map(d => (d.net_profit || 0) >= 0 ? "#00B374" : "#E5484D"),
          borderRadius: 6,
          hoverBackgroundColor: top.map(d => (d.net_profit || 0) >= 0 ? "#00925E" : "#C73037"),
        }],
      },
      options: {
        indexAxis: "y",
        onClick: (_, els) => { if (els.length) inspectDealOnDashboard(top[els[0].index].id); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: chartTextColor(), callback: v => "$" + (v / 1000).toFixed(0) + "K" },
               grid: { color: chartGridColor() } },
          y: { ticks: { color: chartTextColor() }, grid: { display: false } },
        },
      },
    });
  }

  function renderRoiCapChart(deals) {
    destroyChart("roiCap");
    const ctx = $("#chart-roi-cap");
    if (!ctx || !deals.length) return;
    state.charts.roiCap = new Chart(ctx, {
      type: "scatter",
      data: {
        datasets: [{
          data: deals.map(d => ({ x: d.cap_rate || 0, y: d.roi || 0, address: d.address, id: d.id })),
          backgroundColor: deals.map(d => scoreColor(d.score)),
          pointRadius: 9, pointHoverRadius: 13,
        }],
      },
      options: {
        onClick: (_, els) => { if (els.length) openDeal(deals[els[0].index].id); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => {
            const p = ctx.raw;
            return `${shortAddr(p.address)}: ROI ${p.y.toFixed(1)}% | Cap ${p.x.toFixed(1)}%`;
          }}},
        },
        scales: {
          x: { title: { display: true, text: "Cap Rate (%)", color: chartTextColor() },
               ticks: { color: chartTextColor() }, grid: { color: chartGridColor() } },
          y: { title: { display: true, text: "Flip ROI (%)", color: chartTextColor() },
               ticks: { color: chartTextColor() }, grid: { color: chartGridColor() } },
        },
      },
    });
  }

  function renderScoreChart(deals) {
    destroyChart("scores");
    const ctx = $("#chart-scores");
    if (!ctx || !deals.length) return;
    const sorted = [...deals].sort((a, b) => (b.score || 0) - (a.score || 0));
    state.charts.scores = new Chart(ctx, {
      type: "bar",
      data: {
        labels: sorted.map(d => shortAddr(d.address)),
        datasets: [{
          data: sorted.map(d => d.score || 0),
          backgroundColor: sorted.map(d => scoreColor(d.score)),
          borderRadius: 6,
        }],
      },
      options: {
        onClick: (_, els) => { if (els.length) openDeal(sorted[els[0].index].id); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: chartTextColor() }, grid: { display: false } },
          y: { min: 0, max: 100, ticks: { color: chartTextColor() }, grid: { color: chartGridColor() } },
        },
      },
    });
  }

  function renderTopDeals(deals) {
    const sorted = [...deals].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 5);
    const container = $("#top-deals");
    if (!sorted.length) { container.innerHTML = ""; return; }
    container.innerHTML = sorted.map(d => `
      <div class="deal-row" data-id="${d.id}"
           style="display:flex; align-items:center; padding:10px 4px; border-bottom:1px solid var(--border); cursor:pointer;">
        <div style="flex:1;">
          <div style="font-weight:600; color:var(--text);">${escape(d.address)}</div>
          <div class="muted">${escape((d.recommended_strategy || []).join(" / "))}</div>
        </div>
        <div style="display:flex; gap:12px; align-items:center;">
          <span class="score-badge" style="background:${scoreColor(d.score)}">${d.score}</span>
          <span class="pill ${signalPillClass(d.signal)}">${escape(d.signal)}</span>
          <span class="${moneyClass(d.net_profit)}" style="min-width:90px; text-align:right;">
            ${fmtMoney(d.net_profit, true)}
          </span>
        </div>
      </div>
    `).join("");
    $$(".deal-row", container).forEach(row => {
      row.addEventListener("click", () => inspectDealOnDashboard(row.dataset.id));
    });
  }

  // ============== DEALS LIST ==============
  async function refreshDeals() {
    try {
      const deals = await API.listDeals();
      state.deals = deals;
      _populateCityFilter();
      renderDeals();
    } catch (e) { toast(e.message, "error"); }
  }

  function filteredDeals() {
    const q = state.searchQ.toLowerCase().trim();
    let list = state.deals.slice();
    if (q) list = list.filter(d => {
      const hay = ((d.address || "") + " " + (d.city || "") + " " + (d.signal || "") + " " +
                    (d.recommended_strategy || []).join(" ") + " " + (d.status || "")).toLowerCase();
      return hay.includes(q);
    });
    if (state.cityFilter) {
      const cf = state.cityFilter.toLowerCase();
      list = list.filter(d => (d.city || "").toLowerCase() === cf);
    }
    // Sort (applies to the cards view; the table keeps its column sort)
    if (state.dealsSort === "newest") {
      list.sort((a, b) => String(b.added_date || "").localeCompare(String(a.added_date || "")));
    } else if (state.dealsSort === "risk") {
      const rank = { F: 5, D: 4, C: 3, B: 2, A: 1 };
      const rk = x => (x.deal_breakers && x.deal_breakers.length ? 100 : 0) + (rank[x.risk_grade] || 0);
      list.sort((a, b) => rk(b) - rk(a));
    } else {
      list.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    }
    return list;
  }

  // Populate the city dropdown from the deals currently loaded.
  function _populateCityFilter() {
    const sel = $("#deals-city");
    if (!sel) return;
    const cities = [...new Set((state.deals || [])
      .map(d => (d.city || "").trim()).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    const cur = state.cityFilter;
    sel.innerHTML = `<option value="">🏙 All cities</option>` +
      cities.map(c => `<option value="${escape(c)}">${escape(c)}</option>`).join("");
    // Keep the current selection if it still exists, else reset.
    if (cur && cities.some(c => c.toLowerCase() === cur.toLowerCase())) sel.value = cur;
    else { sel.value = ""; state.cityFilter = ""; }
  }

  // ============== BULK SELECTION STATE ==============
  let _dealsSelectMode = false;
  const _dealsSelected = new Set();

  function _toggleSelectMode(on) {
    _dealsSelectMode = (on !== undefined) ? on : !_dealsSelectMode;
    document.body.classList.toggle("deals-select-mode", _dealsSelectMode);
    const btn = $("#deals-toggle-select-mode");
    if (btn) {
      btn.classList.toggle("active", _dealsSelectMode);
      btn.innerHTML = _dealsSelectMode
        ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M18 6L6 18M6 6l12 12" stroke-linecap="round"/></svg> Exit selection`
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" stroke-linecap="round" stroke-linejoin="round"/></svg> Selection mode`;
    }
    if (!_dealsSelectMode) _dealsSelected.clear();
    _updateBulkBar();
    renderDeals();
  }

  function _updateBulkBar() {
    const bar = $("#deals-bulk-bar");
    if (!bar) return;
    const n = _dealsSelected.size;
    bar.style.display = (_dealsSelectMode && n > 0) ? "flex" : "none";
    const cnt = $("#deals-bulk-count");
    if (cnt) cnt.textContent = n;
  }

  async function _bulkDeleteSelectedDeals() {
    const ids = Array.from(_dealsSelected);
    if (!ids.length) return;
    const confirmMsg =
      `Delete ${ids.length} deal${ids.length > 1 ? 's' : ''}?\n\n` +
      `This action cannot be undone.\n\n` +
      ids.slice(0, 8).map(id => {
        const d = (state.deals || []).find(x => x.id === id);
        return `• ${d?.address || id}`;
      }).join("\n") +
      (ids.length > 8 ? `\n• ... and ${ids.length - 8} more` : "");
    if (!confirm(confirmMsg)) return;

    const btn = $("#deals-bulk-delete");
    if (btn) { btn.disabled = true; btn.textContent = "Deleting…"; }
    let okCount = 0, failCount = 0;
    for (const id of ids) {
      try {
        await API.deleteDeal(id);
        okCount++;
      } catch (e) {
        console.error(`Delete ${id} failed:`, e);
        failCount++;
      }
    }
    if (btn) { btn.disabled = false; btn.innerHTML =
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" stroke-linecap="round"/></svg> Delete`;
    }
    if (failCount === 0) {
      toast(`✓ ${okCount} deal${okCount > 1 ? 's' : ''} deleted`, "success");
    } else {
      toast(`${okCount} deleted, ${failCount} failed`, "warn");
    }
    _dealsSelected.clear();
    _toggleSelectMode(false);
    refreshDeals();
  }

  function _toggleDealSelection(id) {
    if (_dealsSelected.has(id)) _dealsSelected.delete(id);
    else _dealsSelected.add(id);
    _updateBulkBar();
    // Update the card visual without full re-render
    const card = document.querySelector(`.deal-card[data-id="${id}"]`);
    if (card) card.classList.toggle("selected", _dealsSelected.has(id));
  }

  function renderDeals() {
    const deals = filteredDeals();
    if (state.viewMode === "cards") {
      $("#deals-cards-container").style.display = "block";
      $("#deals-table-container").style.display = "none";
      renderDealsCards(deals);
    } else {
      $("#deals-cards-container").style.display = "none";
      $("#deals-table-container").style.display = "block";
      renderDealsTable(deals);
    }
  }

  function renderDealsCards(deals) {
    const container = $("#deals-cards-container");
    if (!deals.length) {
      container.innerHTML = `<div class="card empty">
        <img src="/img/empty-state.png" alt="" style="width:150px; height:150px; object-fit:contain; margin:0 auto 6px; display:block; opacity:.92;">
        <h3>No deals here</h3>
        <p>Adjust your search or add a deal via Sourcing.</p>
      </div>`;
      return;
    }
    container.innerHTML = `<div class="deals-grid">${
      deals.map(d => `
        <div class="deal-card" data-id="${d.id}">
          <div class="deal-card-img" style="${d.image ? `background-image:url('${escape(d.image)}')` : ''}">
            ${!d.image ? '<div class="deal-card-img-placeholder">🏡</div>' : ''}
            <div class="deal-card-badges">
              <span class="score-badge" style="background:${scoreColor(d.score)}">${d.score}</span>
              <span class="pill ${signalPillClass(d.signal)}">${escape(d.signal)}</span>
              ${riskBadge(d)}
              ${d.ai_auto === "running" ? '<span class="pill yellow" title="Automatic analysis in progress (ARV/photos)">🤖…</span>' : ""}
            </div>
          </div>
          <div class="deal-card-body">
            ${dealBreakerStrip(d)}
            <div class="deal-card-address">${escape(d.address)}</div>
            <div class="deal-card-city">${escape(d.city || '')}, ${escape(d.state || '')}</div>
            <div class="deal-card-specs">
              ${d.beds ? `<span>🛏️ ${d.beds}bd</span>` : ''}
              ${d.baths ? `<span>🛁 ${d.baths}ba</span>` : ''}
              ${d.sqft ? `<span>📐 ${d.sqft.toLocaleString()}sf</span>` : ''}
              ${d.year_built ? `<span>📅 ${d.year_built}</span>` : ''}
            </div>
            ${cardZillowLine(d)}
            <div class="deal-card-row">
              <span class="label">Purchase</span>
              <span class="val">${fmtMoney(d.purchase_price)}</span>
            </div>
            <div class="deal-card-row">
              <span class="label">ARV</span>
              <span class="val">${fmtMoney(d.arv_base)}</span>
            </div>
            <div class="deal-card-row">
              <span class="label">🎯 Max offer</span>
              <span class="val ${d.max_offer_blocked ? "price-blocked" : ""}" style="font-weight:800; color:${d.max_offer_blocked ? "var(--red)" : "var(--green)"};" title="${d.max_offer_blocked ? "Blocked — deal-breaker to clear" : (d.max_offer_gap != null ? (d.max_offer_gap >= 0 ? fmtMoney(d.max_offer_gap) + " of margin below asking price" : "Asking " + fmtMoney(-d.max_offer_gap) + " above your max") : "")}">${d.max_offer != null ? fmtMoney(d.max_offer) : "—"}</span>
            </div>
            <div class="deal-card-row">
              <span class="label">Net profit (flip)</span>
              <span class="val ${moneyClass(d.net_profit)}">${fmtMoney(d.net_profit, true)}</span>
            </div>
            <div class="deal-card-row">
              <span class="label">ROI / Cap</span>
              <span class="val">${fmtPct(d.roi)} / ${fmtPct(d.cap_rate)}</span>
            </div>
            <div class="deal-card-footer">
              <span class="status-pill ${(d.status || 'evaluating').replace('_', '-')}" data-id="${d.id}" data-status="${escape(d.status || 'evaluating')}">${escape(d.status || 'evaluating')}</span>
              ${cardSourceBadge(d.source_url)}
            </div>
            <div class="deal-card-actions">
              <button class="deal-card-delete" data-id="${d.id}" title="Delete this deal">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" stroke-linecap="round"/></svg>
                Delete
              </button>
            </div>
          </div>
        </div>
      `).join('')
    }</div>`;
    $$(".deal-card", container).forEach(card => {
      const id = card.dataset.id;
      // Restore selection state after re-render
      if (_dealsSelected.has(id)) card.classList.add("selected");
      card.addEventListener("click", e => {
        if (e.target.closest(".status-pill")) return;
        if (e.target.closest(".deal-card-delete")) return;  // delete handles its own
        if (e.target.closest(".card-dom-edit")) return;     // inline days editor
        if (_dealsSelectMode) {
          _toggleDealSelection(id);
        } else {
          openDeal(id);
        }
      });
    });
    _attachCardDomEdit(container);
    // While an auto-analysis runs, poll once every 10 s so the 🤖 badge clears
    // and ARV/max-offer appear without a manual refresh.
    clearTimeout(window._aiAutoTimer);
    if (deals.some(d => d.ai_auto === "running")) {
      window._aiAutoTimer = setTimeout(() => {
        if ($("#view-deals")?.classList.contains("active")) refreshDeals();
      }, 10000);
    }
    // Per-card delete buttons
    $$(".deal-card-delete", container).forEach(btn => {
      btn.addEventListener("click", async e => {
        e.stopPropagation();
        const id = btn.dataset.id;
        const deal = (state.deals || []).find(d => d.id === id);
        const label = deal?.address || id;
        if (!confirm(`Delete "${label}"?\n\nThis action cannot be undone.`)) return;
        btn.disabled = true;
        try {
          await API.deleteDeal(id);
          toast(`✓ Deal deleted: ${label}`, "success");
          await refreshDeals();
        } catch (err) {
          toast("Failed: " + err.message, "error");
          btn.disabled = false;
        }
      });
    });
    $$(".status-pill", container).forEach(p => {
      p.addEventListener("click", e => {
        e.stopPropagation();
        if (_dealsSelectMode) return;
        cycleStatus(p.dataset.id, p.dataset.status);
      });
    });
  }

  function renderDealsTable(deals) {
    const tbody = $("#deals-tbody");
    if (!deals.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">
        <div class="empty-ico">🔍</div>
        <h3>No deals match</h3>
        <p>Try a different search.</p>
      </td></tr>`;
      return;
    }
    const sorted = [...deals].sort((a, b) => {
      const av = a[state.sortKey], bv = b[state.sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number") return (av - bv) * state.sortDir;
      return String(av).localeCompare(String(bv)) * state.sortDir;
    });
    tbody.innerHTML = sorted.map(d => `
      <tr data-id="${d.id}" ${_dealsSelected.has(d.id) ? 'class="selected"' : ''}>
        ${_dealsSelectMode
          ? `<td style="text-align:center;"><input type="checkbox" class="row-select" data-id="${d.id}" ${_dealsSelected.has(d.id) ? 'checked' : ''}></td>`
          : ''}
        <td><strong>${escape(d.address)}</strong>
            <div class="muted">${escape(d.city || "")} ${escape(d.state || "")}</div></td>
        <td><span class="score-badge" style="background:${scoreColor(d.score)}">${d.score}</span></td>
        <td><span class="pill ${signalPillClass(d.signal)}">${escape(d.signal)}</span></td>
        <td>${fmtMoney(d.purchase_price)}</td>
        <td>${fmtMoney(d.arv_base)}</td>
        <td class="${moneyClass(d.net_profit)}">${fmtMoney(d.net_profit, true)}</td>
        <td>${fmtPct(d.roi)}</td>
        <td>${fmtPct(d.cap_rate)}</td>
        <td><span class="status-pill ${(d.status || 'evaluating').replace('_', '-')}"
                 data-id="${d.id}" data-status="${escape(d.status || 'evaluating')}">${escape(d.status || 'evaluating')}</span></td>
        <td></td>
      </tr>
    `).join("");
    $$("#deals-tbody tr").forEach(row => {
      const id = row.dataset.id;
      if (!id) return;
      row.addEventListener("click", e => {
        if (e.target.closest(".status-pill")) return;
        if (e.target.type === "checkbox") return;  // checkbox handles its own event
        if (_dealsSelectMode) {
          _toggleDealSelection(id);
          row.classList.toggle("selected", _dealsSelected.has(id));
          const cb = row.querySelector(".row-select");
          if (cb) cb.checked = _dealsSelected.has(id);
        } else {
          openDeal(id);
        }
      });
    });
    $$(".row-select", tbody).forEach(cb => {
      cb.addEventListener("change", e => {
        e.stopPropagation();
        const id = cb.dataset.id;
        if (cb.checked) _dealsSelected.add(id);
        else _dealsSelected.delete(id);
        cb.closest("tr").classList.toggle("selected", cb.checked);
        _updateBulkBar();
      });
    });
    $$(".status-pill", tbody).forEach(p => {
      p.addEventListener("click", e => {
        e.stopPropagation();
        if (_dealsSelectMode) return;
        cycleStatus(p.dataset.id, p.dataset.status);
      });
    });
  }

  const STATUS_CYCLE = ["evaluating", "under_contract", "closed", "sold", "passed"];
  async function cycleStatus(id, current) {
    const idx = STATUS_CYCLE.indexOf(current);
    const next = STATUS_CYCLE[(idx + 1) % STATUS_CYCLE.length];
    try {
      await API.patchDeal(id, { status: next });
      toast(`Status → ${next}`, "success");
      refreshDeals();
    } catch (e) { toast(e.message, "error"); }
  }

  // Search
  $("#deals-search")?.addEventListener("input", e => {
    state.searchQ = e.target.value;
    renderDeals();
  });
  // City filter + sort
  $("#deals-city")?.addEventListener("change", e => {
    state.cityFilter = e.target.value;
    renderDeals();
  });
  $("#deals-sort")?.addEventListener("change", e => {
    state.dealsSort = e.target.value;
    localStorage.setItem("flip-deals-sort", state.dealsSort);
    renderDeals();
  });
  { const ds = $("#deals-sort"); if (ds) ds.value = state.dealsSort; }
  // Duplicate detector
  $("#deals-dup-btn")?.addEventListener("click", async () => {
    const banner = $("#deals-dup-banner"); if (!banner) return;
    banner.style.display = "block";
    banner.innerHTML = `<div class="card"><span class="spinner"></span> Searching for duplicates…</div>`;
    try {
      const groups = await API.dealsDuplicates();
      if (!groups.length) {
        banner.innerHTML = `<div class="card" style="border-left:3px solid var(--green);">✅ No duplicates found. <button class="btn ghost" id="dup-close" style="font-size:12px; margin-left:8px;">Close</button></div>`;
      } else {
        banner.innerHTML = `<div class="card" style="border-left:3px solid #e8a93b;">
          <strong>👯 ${groups.length} possible duplicate group(s)</strong>
          ${groups.map(g => `<div style="margin-top:8px; font-size:13px;">${g.map(x =>
            `<div style="display:flex; gap:8px; align-items:center; padding:2px 0;">
               <a href="#" data-dup-open="${escape(x.id)}" style="font-weight:600;">${escape(x.address)}</a>
             </div>`).join("")}</div>`).join("")}
          <p class="muted" style="font-size:12px; margin:8px 0 0;">Open each card and delete the extra one (Delete button on the card). <button class="btn ghost" id="dup-close" style="font-size:12px;">Close</button></p>
        </div>`;
      }
      banner.querySelectorAll("[data-dup-open]").forEach(a => a.addEventListener("click", e => {
        e.preventDefault(); openDeal(a.dataset.dupOpen);
      }));
      banner.querySelector("#dup-close")?.addEventListener("click", () => { banner.style.display = "none"; });
    } catch (e) { banner.innerHTML = `<div class="card">${escape(e.message)}</div>`; }
  });

  // ----- Bulk selection wiring -----
  $("#deals-toggle-select-mode")?.addEventListener("click", () => _toggleSelectMode());
  $("#deals-bulk-clear")?.addEventListener("click", () => {
    _dealsSelected.clear();
    _updateBulkBar();
    renderDeals();
  });
  $("#deals-bulk-select-all")?.addEventListener("click", () => {
    filteredDeals().forEach(d => _dealsSelected.add(d.id));
    _updateBulkBar();
    renderDeals();
  });
  $("#deals-bulk-delete")?.addEventListener("click", _bulkDeleteSelectedDeals);

  // View toggle
  $$("#view-toggle button").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === state.viewMode);
    b.addEventListener("click", () => {
      state.viewMode = b.dataset.mode;
      localStorage.setItem("flip-view-mode", state.viewMode);
      $$("#view-toggle button").forEach(x => x.classList.toggle("active", x.dataset.mode === state.viewMode));
      renderDeals();
    });
  });

  $$("#deals-table thead th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir = -state.sortDir;
      else { state.sortKey = k; state.sortDir = -1; }
      renderDeals();
    });
  });

  // ============== DEAL DETAIL ==============
  async function openDeal(id) {
    try {
      const data = await API.getDeal(id);
      state.currentDealId = id;
      renderDealDetail(data);
      showView("detail");
      updateGlobalDealBar(data);
    } catch (e) { toast(e.message, "error"); }
  }

  // ============== GLOBAL DEAL SELECTOR (top bar) ==============
  let _gdbIdx = 0;
  let _gdbFiltered = [];

  function updateGlobalDealBar(deal) {
    const cur = $("#gdb-current");
    const picker = $("#gdb-picker");
    if (!cur || !picker) return;
    if (deal) {
      cur.textContent = deal.address || deal.id;
      picker.classList.add("has-deal");
      ["#gdb-open", "#gdb-chat", "#gdb-clear"].forEach(s => {
        const el = $(s); if (el) el.disabled = false;
      });
    } else {
      cur.textContent = "Select a deal…";
      picker.classList.remove("has-deal");
      ["#gdb-open", "#gdb-chat", "#gdb-clear"].forEach(s => {
        const el = $(s); if (el) el.disabled = true;
      });
    }
  }

  function _gdbOpenDropdown() {
    const dd = $("#gdb-dropdown");
    if (!dd) return;
    dd.style.display = "block";
    const inp = $("#gdb-search");
    if (inp) { inp.value = ""; setTimeout(() => inp.focus(), 10); }
    _gdbRenderList("");
  }

  function _gdbCloseDropdown() {
    const dd = $("#gdb-dropdown");
    if (dd) dd.style.display = "none";
  }

  function _gdbScoreClass(score) {
    if (score >= 85) return "A";
    if (score >= 70) return "A";
    if (score >= 55) return "B";
    if (score >= 40) return "C";
    if (score >= 25) return "D";
    return "F";
  }

  function _gdbRenderList(query) {
    const list = $("#gdb-list");
    const countEl = $("#gdb-count");
    if (!list) return;
    const q = (query || "").toLowerCase().trim();
    let deals = state.deals || [];
    if (q) {
      deals = deals.filter(d => {
        const hay = `${d.address || ""} ${d.city || ""} ${d.state || ""} ${d.zip || ""} ${d.grade || ""} ${d.signal || ""} ${d.score || ""}`.toLowerCase();
        return hay.includes(q);
      });
    }
    // Sort by score desc when no query
    if (!q) deals = [...deals].sort((a, b) => (b.score || 0) - (a.score || 0));
    _gdbFiltered = deals;
    _gdbIdx = 0;
    if (countEl) countEl.textContent = `${deals.length}/${(state.deals||[]).length}`;

    if (!deals.length) {
      list.innerHTML = `<div class="gdb-empty">No deals match "${escape(q)}"</div>`;
      return;
    }
    list.innerHTML = deals.map((d, i) => {
      const grade = d.grade || (d.score >= 70 ? "A" : d.score >= 55 ? "B" : d.score >= 40 ? "C" : d.score >= 25 ? "D" : "F");
      const cls = _gdbScoreClass(d.score || 0);
      const sig = (d.signal || "").toLowerCase();
      const sigCls = sig.includes("good") || sig.includes("excellent") ? "good"
                   : sig.includes("avoid") || sig.includes("loss") || sig.includes("risky") ? "bad" : "";
      const roi = d.roi_pct != null
                  ? `${d.roi_pct > 0 ? '+' : ''}${d.roi_pct.toFixed(0)}% ROI` : "";
      return `
        <div class="gdb-item ${i === _gdbIdx ? 'active' : ''}" data-id="${escape(d.id)}" data-idx="${i}">
          <div class="gdb-item-score ${cls}">${d.score ?? "?"}</div>
          <div class="gdb-item-main">
            <div class="gdb-item-addr">${escape(d.address || d.id)}</div>
            <div class="gdb-item-meta">
              ${d.city ? `<span>${escape(d.city)}${d.state ? ', ' + escape(d.state) : ''}</span>` : ''}
              ${d.purchase_price ? `<span>$${Math.round(d.purchase_price/1000)}K</span>` : ''}
              ${d.arv_base ? `<span>→ $${Math.round(d.arv_base/1000)}K ARV</span>` : ''}
            </div>
          </div>
          ${d.signal ? `<span class="gdb-item-signal ${sigCls}">${escape(d.signal)}</span>` : ''}
          ${roi ? `<span class="gdb-item-roi">${roi}</span>` : ''}
        </div>
      `;
    }).join("");

    list.querySelectorAll(".gdb-item").forEach(el => {
      el.addEventListener("click", () => {
        const id = el.dataset.id;
        _gdbSelect(id);
      });
    });
  }

  async function _gdbSelect(id) {
    _gdbCloseDropdown();
    state.currentDealId = id;
    const deal = (state.deals || []).find(d => d.id === id);
    if (deal) updateGlobalDealBar(deal);
    // If user clicked from main view, also navigate to detail
    await openDeal(id);
  }

  // Wire it up
  $("#gdb-picker")?.addEventListener("click", e => {
    e.stopPropagation();
    const dd = $("#gdb-dropdown");
    if (dd && dd.style.display === "block") _gdbCloseDropdown();
    else _gdbOpenDropdown();
  });
  $("#gdb-search")?.addEventListener("input", e => _gdbRenderList(e.target.value));
  $("#gdb-search")?.addEventListener("keydown", e => {
    if (e.key === "Escape") { _gdbCloseDropdown(); return; }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      _gdbIdx = Math.min(_gdbIdx + 1, _gdbFiltered.length - 1);
      _gdbRenderList($("#gdb-search").value);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _gdbIdx = Math.max(_gdbIdx - 1, 0);
      _gdbRenderList($("#gdb-search").value);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const d = _gdbFiltered[_gdbIdx];
      if (d) _gdbSelect(d.id);
    }
  });
  document.addEventListener("click", e => {
    const bar = e.target.closest("#global-deal-bar");
    if (!bar) _gdbCloseDropdown();
  });
  $("#gdb-open")?.addEventListener("click", () => {
    if (state.currentDealId) openDeal(state.currentDealId);
  });
  $("#gdb-chat")?.addEventListener("click", () => {
    if (state.currentDealId && typeof openChat === "function") {
      const panel = $("#chat-panel");
      if (panel) openChat();
    }
  });
  $("#gdb-clear")?.addEventListener("click", () => {
    state.currentDealId = null;
    updateGlobalDealBar(null);
  });

  // Tiny badge for deal cards showing the source platform
  function cardSourceBadge(url) {
    if (!url) return '<span class="muted" style="font-size:11px;">no source</span>';
    const lower = (url || "").toLowerCase();
    let label = "Listing", cls = "src-link-gray";
    if (lower.includes("zillow.com"))         { label = "Zillow";       cls = "src-link-zillow"; }
    else if (lower.includes("redfin.com"))    { label = "Redfin";       cls = "src-link-redfin"; }
    else if (lower.includes("ispeedtolead") || lower.includes("dealspeed")) {
                                                label = "ispeedtolead"; cls = "src-link-ispeed"; }
    else if (lower.includes("realtor.com"))   { label = "Realtor";      cls = "src-link-realtor"; }
    return `<a href="${escape(url)}" target="_blank" class="src-link ${cls}"
              style="padding:3px 8px; font-size:10.5px;"
              onclick="event.stopPropagation()" title="Open ${escape(label)} listing">
      ${escape(label)}
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="9" height="9"><path d="M15 3h6v6M10 14L21 3" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </a>`;
  }

  // Build a row of source / external links shown under the address
  function renderSourceLinks(d) {
    const links = [];
    const url = d.source_url || "";
    const lower = url.toLowerCase();

    // Primary source — the URL the deal was scraped from
    if (url) {
      let label = "Source listing", icon = "🔗", cls = "src-link-gray";
      if (lower.includes("zillow.com"))         { label = "Zillow";        icon = "Z"; cls = "src-link-zillow"; }
      else if (lower.includes("redfin.com"))    { label = "Redfin";        icon = "R"; cls = "src-link-redfin"; }
      else if (lower.includes("ispeedtolead") || lower.includes("dealspeed")) {
                                                  label = "ispeedtolead"; icon = "✦"; cls = "src-link-ispeed"; }
      else if (lower.includes("realtor.com"))   { label = "Realtor.com";   icon = "R"; cls = "src-link-realtor"; }
      else if (lower.includes("trulia.com"))    { label = "Trulia";        icon = "T"; cls = "src-link-gray"; }
      links.push(`<a href="${escape(url)}" target="_blank" class="src-link ${cls}" title="Open source listing">
        <span class="src-link-mark">${escape(icon)}</span>
        <span>${escape(label)}</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="11" height="11"><path d="M15 3h6v6M10 14L21 3M21 14v7H3V3h7" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </a>`);
    }

    // Always offer quick search links so users can find the listing on other sites
    const q = encodeURIComponent(d.address || "");
    if (q) {
      // Don't duplicate primary source as a search link
      if (!lower.includes("zillow.com")) {
        links.push(`<a href="https://www.zillow.com/homes/${q}_rb/" target="_blank" class="src-link src-link-search" title="Search on Zillow">
          <span class="src-link-mark">Z</span><span>Zillow</span></a>`);
      }
      if (!lower.includes("redfin.com")) {
        links.push(`<a href="https://www.redfin.com/stingray/do/location-autocomplete?location=${q}" target="_blank" class="src-link src-link-search" title="Search on Redfin">
          <span class="src-link-mark">R</span><span>Redfin</span></a>`);
      }
      links.push(`<a href="https://www.google.com/maps/search/${q}" target="_blank" class="src-link src-link-search" title="Open in Google Maps">
        <span class="src-link-mark">M</span><span>Maps</span></a>`);
      links.push(`<a href="https://www.google.com/search?q=${q}+property+history" target="_blank" class="src-link src-link-search" title="Google search">
        <span class="src-link-mark">G</span><span>Search</span></a>`);
    }

    if (!links.length) return "";
    return `<div class="source-links">${links.join("")}</div>`;
  }

  const _DOC_VERDICT = {
    good: ["var(--green)", "✅ GOOD"],
    caution: ["#e8a93b", "⚠️ CAUTION"],
    bad: ["var(--red)", "⛔ BAD"],
  };
  function renderDealDocuments(data) {
    const d = data.deal || {};
    const list = $("#detail-documents-list");
    if (!list) return;
    const docs = d.documents || [];
    const D = v => (v == null || v === "") ? "—" : "$" + Math.round(v).toLocaleString("en-US");
    const sevLbl = { minor: "minor", moderate: "moderate", major: "major", safety: "safety" };
    list.innerHTML = docs.length ? docs.map(doc => {
      const a = doc.analysis || {};
      const vd = _DOC_VERDICT[a.verdict] || ["var(--muted)", "—"];
      const findings = (a.findings || []).map(f =>
        `<div class="risk-flag"><span class="risk-sev ${f.severity === 'safety' ? 'deal_breaker' : f.severity === 'major' ? 'high' : f.severity === 'moderate' ? 'medium' : 'low'}">${sevLbl[f.severity] || f.severity}</span>
          <span style="flex:1;">${escape(f.system ? f.system + " — " : "")}${escape(f.issue || "")}</span>
          <span style="font-weight:600; white-space:nowrap;">${D(f.est_cost)}</span></div>`).join("");
      const breakers = (a.deal_breakers || []).map(b => `<li>${escape(b)}</li>`).join("");
      return `<div class="card" style="margin-bottom:10px; border-left:3px solid ${vd[0]};">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
          <div>
            <div style="font-weight:700;">📄 ${escape(doc.filename || "document.pdf")}</div>
            <div class="muted" style="font-size:12px;">${escape(a.doc_type || "Document")} · ${doc.pages || "?"} p. · ${escape((doc.uploaded_at || "").slice(0, 10))}</div>
          </div>
          <span class="pill" style="background:${vd[0]}1a; color:${vd[0]}; font-weight:700; font-size:13px; padding:5px 12px;">${vd[1]}</span>
        </div>
        ${a.summary ? `<p style="margin:8px 0; font-size:13px;">${escape(a.summary)}</p>` : ""}
        ${a.verdict_reason ? `<p style="margin:4px 0; font-size:13px; color:${vd[0]};">${escape(a.verdict_reason)}</p>` : ""}
        <div style="display:flex; gap:16px; flex-wrap:wrap; margin:8px 0; font-size:13px;">
          ${a.total_repair_estimate ? `<span><strong>Estimated repairs:</strong> ${D(a.total_repair_estimate)}</span>` : ""}
          ${a.suggested_rehab ? `<span><strong>Suggested rehab:</strong> ${D(a.suggested_rehab)} <button class="btn ghost doc-apply" data-rehab="${a.suggested_rehab}" style="font-size:11px; padding:2px 8px;">apply</button></span>` : ""}
          ${(a.key_numbers && a.key_numbers.appraised_value) ? `<span><strong>Appraised value:</strong> ${D(a.key_numbers.appraised_value)}</span>` : ""}
        </div>
        ${breakers ? `<div style="margin:6px 0;"><strong style="color:var(--red); font-size:13px;">⛔ Deal-breakers</strong><ul style="margin:4px 0; padding-left:18px; font-size:13px;">${breakers}</ul></div>` : ""}
        ${findings ? `<details style="margin-top:6px;"><summary style="cursor:pointer; font-size:13px; font-weight:600;">${(a.findings || []).length} finding(s)</summary><div style="margin-top:6px;">${findings}</div></details>` : ""}
        <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
          <button class="btn doc-reapply" data-id="${escape(doc.id)}" style="font-size:12px;">🔄 Refresh deal data</button>
          <a class="btn ghost" href="${API.dealDocumentUrl(d.id, doc.id)}" target="_blank" rel="noopener" style="font-size:12px;">Open PDF ↗</a>
          <button class="btn ghost doc-delete" data-id="${escape(doc.id)}" style="font-size:12px;">🗑 Remove</button>
        </div>
      </div>`;
    }).join("") : `<p class="muted" style="font-size:13px; margin:0;">No documents. Add an inspection for automatic analysis.</p>`;

    // Wire delete + apply-rehab
    list.querySelectorAll(".doc-delete").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Remove this document?")) return;
      try { await API.deleteDealDocument(d.id, b.dataset.id); openDeal(d.id); }
      catch (e) { toast(e.message, "error"); }
    }));
    list.querySelectorAll(".doc-reapply").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "…";
      try {
        const r = await API.reapplyDealDocument(d.id, b.dataset.id);
        const ap = r.applied || {};
        toast(Object.keys(ap).length ? `Data refreshed (${Object.keys(ap).join(", ")})` : "Data already up to date", "success");
        openDeal(d.id);
      } catch (e) { b.disabled = false; b.textContent = "🔄 Refresh deal data"; toast(e.message, "error"); }
    }));
    list.querySelectorAll(".doc-apply").forEach(b => b.addEventListener("click", async () => {
      try { await API.patchDeal(d.id, { rehab_base: Number(b.dataset.rehab) }); toast("Rehab applied", "success"); openDeal(d.id); }
      catch (e) { toast(e.message, "error"); }
    }));
    // Wire the upload input (re-attached each render)
    const inp = $("#doc-upload-input");
    if (inp) inp.onchange = () => { if (inp.files && inp.files[0]) _uploadDealDoc(d.id, inp.files[0]); };
  }
  async function _uploadDealDoc(dealId, file) {
    const st = $("#doc-upload-status");
    if (st) st.innerHTML = '<span class="spinner"></span> Reading + AI-analyzing the document… (~30-60s)';
    try {
      const r = await API.uploadDealDocument(dealId, file);
      if (!r.ok) { if (st) st.innerHTML = `<span style="color:var(--red)">${escape(r.error || "Failed")}</span>`; return; }
      const v = (r.document.analysis || {}).verdict;
      if (st) st.innerHTML = `<span style="color:var(--green)">✓ Analyzed — verdict: ${escape(v || "?")}</span>`;
      openDeal(dealId);  // re-render with the new doc + updated risk/rehab
    } catch (e) { if (st) st.innerHTML = `<span style="color:var(--red)">${escape(e.message)}</span>`; }
  }

  function _fmtCommentTs(ts) {
    try {
      return new Date(ts).toLocaleString("en-US", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch { return ts || ""; }
  }
  function renderDealComments(data) {
    const d = data.deal || {};
    const list = $("#deal-comments-list"); if (!list) return;
    const comments = (d.comments || []).slice().sort((a, b) =>
      String(b.created_at || "").localeCompare(String(a.created_at || "")));  // newest first
    list.innerHTML = comments.length ? comments.map(c => `
      <div class="deal-comment">
        <div class="deal-comment-meta">
          <span>🕒 ${escape(_fmtCommentTs(c.created_at))}</span>
          <button class="deal-comment-del" data-id="${escape(c.id)}" title="Delete">✕</button>
        </div>
        <div class="deal-comment-text">${escape(c.text).replace(/\n/g, "<br>")}</div>
      </div>`).join("") : `<p class="muted" style="font-size:13px; margin:0;">No comments yet.</p>`;
    list.querySelectorAll(".deal-comment-del").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Delete this comment?")) return;
      try { const r = await API.deleteDealComment(d.id, b.dataset.id); d.comments = r.comments; renderDealComments(data); }
      catch (e) { toast(e.message, "error"); }
    }));
    const inp = $("#deal-comment-input"), btn = $("#deal-comment-add");
    const submit = async () => {
      const text = (inp.value || "").trim();
      if (!text) return;
      btn.disabled = true;
      try {
        const r = await API.addDealComment(d.id, text);
        d.comments = r.comments; inp.value = ""; renderDealComments(data);
      } catch (e) { toast(e.message, "error"); }
      finally { btn.disabled = false; }
    };
    if (btn) btn.onclick = submit;
    if (inp) inp.onkeydown = (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit(); } };
  }

  // ============== NEIGHBORHOOD MAP (Leaflet: subject + comp price pins) ==============
  let _dealMap = null;
  // Readable list of the nearby homes with their Zillow prices (below the map).
  function renderSalesList(sales) {
    const box = $("#detail-sales-list"); if (!box) return;
    const arr = (sales || []).filter(s => s && s.price);
    if (!arr.length) { box.style.display = "none"; box.innerHTML = ""; return; }
    arr.sort((a, b) => (b.price || 0) - (a.price || 0));
    const rows = arr.map(s => {
      const url = (s.url && /^https?:/.test(s.url)) ? s.url
        : "https://www.zillow.com/homes/" + encodeURIComponent(s.address || "") + "_rb/";
      const specs = [s.beds ? s.beds + "bd" : "", s.sqft ? Number(s.sqft).toLocaleString("en-US") + " sf" : "", s.date || ""].filter(Boolean).join(" · ");
      return `<div class="sale-row">
        <span class="sale-price">$${Number(s.price).toLocaleString("en-US")}</span>
        <span class="sale-addr">${escape(s.address || "?")}<span class="sale-meta">${escape(specs)}</span></span>
        <a class="sale-z" href="${url}" target="_blank" rel="noopener">Zillow ↗</a>
      </div>`;
    }).join("");
    box.style.display = "block";
    box.innerHTML = `<div class="sale-list-head">🏘 ${arr.length} nearby home${arr.length > 1 ? "s" : ""} · Zillow prices</div>
      <div class="sale-list-scroll">${rows}</div>`;
  }

  async function renderDealMap(data) {
    const card = $("#detail-map-card"); if (!card) return;
    const d = data.deal || {};
    card.style.display = "block";
    renderSalesList(d.area_sales);
    if (typeof L === "undefined") {
      // Leaflet couldn't load (blocker/offline) — degrade to a plain comps list
      // instead of silently hiding the whole section.
      const mapEl = $("#detail-map");
      try {
        const r = await API.dealCompsMap(d.id);
        const list = (r.comps || []).map(c =>
          `<div class="risk-flag"><span style="font-weight:700;">$${Math.round((c.price || 0) / 1000)}K</span><span style="flex:1;">${escape(c.address || "?")}</span><span class="muted">${escape(String(c.date || ""))}</span></div>`).join("");
        if (mapEl) mapEl.innerHTML = list || '<p class="muted" style="padding:12px;">No comparables yet.</p>';
        if (mapEl) mapEl.style.height = "auto";
        const note = $("#detail-map-note");
        if (note) note.textContent = "Map unavailable (library blocked) — comparables shown as a list.";
      } catch {}
      return;
    }
    const note = $("#detail-map-note");
    if (note) note.textContent = "Loading map + comparables…";
    if (_dealMap) { try { _dealMap.remove(); } catch {} _dealMap = null; }
    const map = L.map("detail-map", { scrollWheelZoom: false });
    _dealMap = map;
    map.on("click", () => map.scrollWheelZoom.enable());
    const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                            { maxZoom: 19, attribution: "Esri" });
    const road = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                             { maxZoom: 19, attribution: "© OpenStreetMap" });
    sat.addTo(map);
    const bSat = $("#map-layer-sat"), bRoad = $("#map-layer-road");
    const setLayer = (which) => {
      if (which === "sat") { map.removeLayer(road); sat.addTo(map); }
      else { map.removeLayer(sat); road.addTo(map); }
      bSat?.classList.toggle("active", which === "sat");
      bRoad?.classList.toggle("active", which === "road");
    };
    if (bSat) bSat.onclick = () => setLayer("sat");
    if (bRoad) bRoad.onclick = () => setLayer("road");
    setTimeout(() => { try { map.invalidateSize(); } catch {} }, 250);

    const K = v => (v == null || v === "" || isNaN(v)) ? "?" : "$" + Math.round(Number(v) / 1000) + "K";
    const pinLayer = L.layerGroup().addTo(map);
    let viewSet = false;

    // (Re)load all pins. Geocoding is capped server-side (12/request), so we
    // poll until `ungeocoded` reaches 0 — pins pop in as addresses resolve.
    const loadPins = async (attempt = 0) => {
      try {
        const r = await API.dealCompsMap(d.id);
        if (!r.ok) {
          if (note) note.textContent = r.error || "Map unavailable for this property.";
          if (!viewSet) map.setView([39.5, -98.35], 4);
          return;
        }
        const s = r.subject;
        if (!viewSet) { map.setView([s.lat, s.lng], 16); viewSet = true; }
        pinLayer.clearLayers();
        const pts = [[s.lat, s.lng]];
        L.marker([s.lat, s.lng], { zIndexOffset: 1000, icon: L.divIcon({
            className: "", iconAnchor: [46, 16],
            html: `<div class="map-subject-pin">🏠 THIS PROPERTY ${s.price ? "· " + K(s.price) : ""}</div>` }) })
          .addTo(pinLayer)
          .bindPopup(`<strong>${escape(s.address)}</strong><br>Purchase: ${s.price ? "$" + Number(s.price).toLocaleString("en-US") : "?"}${s.arv ? "<br>ARV: $" + Number(s.arv).toLocaleString("en-US") : ""}`);
        (r.comps || []).forEach(c => {
          pts.push([c.lat, c.lng]);
          const isComp = c.source !== "sold";
          // Zillow link: real listing URL when we have one, else Zillow's
          // address-search URL which lands on the property page.
          const zUrl = c.url && /^https?:/.test(c.url) ? c.url
            : "https://www.zillow.com/homes/" + encodeURIComponent(c.address || "") + "_rb/";
          const ppsf = (c.price && c.sqft) ? Math.round(Number(c.price) / Number(c.sqft)) : null;
          const deltaArv = (c.price && s.arv) ? Number(c.price) - Number(s.arv) : null;
          const explain = isComp
            ? "💡 Comparable used to estimate your ARV (similar sale nearby)."
            : "💡 Recent neighborhood sale — shows the local market price level.";
          L.marker([c.lat, c.lng], { icon: L.divIcon({
              className: "", iconAnchor: [22, 13],
              html: `<div class="comp-pill${isComp ? " comp-hl" : ""}">${K(c.price)}</div>` }) })
            .addTo(pinLayer)
            .bindPopup(`<strong>${escape(c.address || "?")}</strong><br>` +
              `${c.price ? (c.source === "sold" ? "Sold: <strong>$" : "Comparable: <strong>$") + Number(c.price).toLocaleString("en-US") + "</strong>" : ""}` +
              `${c.date ? ` · ${escape(String(c.date))}` : ""}` +
              `${c.beds ? `<br>${c.beds}bd${c.baths ? "/" + c.baths + "ba" : ""}${c.sqft ? " · " + Number(c.sqft).toLocaleString("en-US") + " sf" : ""}` : ""}` +
              `${ppsf ? `<br>≈ <strong>$${ppsf}/sqft</strong>` : ""}` +
              `${deltaArv != null ? `<br>vs your ARV ($${Math.round(s.arv / 1000)}K): <strong style="color:${deltaArv >= 0 ? "#0fae6f" : "#e05353"};">${deltaArv >= 0 ? "+" : "−"}$${Math.abs(Math.round(deltaArv / 1000))}K</strong>` : ""}` +
              `${c.distance_mi ? `<br>${Number(c.distance_mi).toFixed(1)} mi from property` : ""}` +
              `<br><span style="font-size:11px; color:#667;">${explain}</span>` +
              `<br><a href="${zUrl}" target="_blank" rel="noopener" style="display:inline-block; margin-top:6px; font-weight:700;">View on Zillow ↗</a>`,
              { maxWidth: 280 });
        });
        // Initial framing: the immediate neighborhood (like Zillow), not the
        // whole town — farther pins stay on the map when zooming out.
        if (pts.length > 1) {
          const kmTo = (p) => {
            const dy = (p[0] - s.lat) * 111.32;
            const dx = (p[1] - s.lng) * 111.32 * Math.cos(s.lat * Math.PI / 180);
            return Math.sqrt(dx * dx + dy * dy);
          };
          const near = pts.filter(p => kmTo(p) <= 1.2);
          map.fitBounds(near.length >= 4 ? near : pts, { padding: [40, 40], maxZoom: 17 });
        }
        const n = (r.comps || []).length;
        const nSold = (r.comps || []).filter(c => c.source === "sold").length;
        const droppedTxt = r.dropped_far ? ` · ${r.dropped_far} dropped (address not found near the property)` : "";
        const geoTxt = r.ungeocoded ? ` · geocoding in progress (${r.ungeocoded} remaining)…` : "";
        if (note) note.textContent = n
          ? `${n} pin(s) — ${nSold} neighborhood sale(s), ${n - nSold} comparable(s)${droppedTxt}${geoTxt} Click a price for details.`
          : "No pins yet — “📍 Neighborhood sales” pulls 20+ nearby homes with their Zillow prices (~60-90 s, ~$0.80); “🤖 Find comparables” targets the best ARV comps.";
        const findBtn = $("#map-find-comps");
        if (findBtn) {
          findBtn.style.display = n ? "none" : "inline-flex";
          findBtn.disabled = false;
          findBtn.textContent = "🤖 Find comparables (AI)";
          findBtn.onclick = async () => {
            findBtn.disabled = true;
            findBtn.innerHTML = '<span class="spinner"></span> Searching…';
            try {
              const rr = await API.aiRun("arv", d.id);
              if (!rr.ok) { toast(rr.error || "Search failed", "error"); findBtn.disabled = false; findBtn.textContent = "🤖 Find comparables (AI)"; return; }
              toast(`✓ ARV + ${((rr.result || {}).comparables || []).length} comparable(s) found`, "success");
              openDeal(d.id);
            } catch (e) {
              toast(e.message, "error");
              findBtn.disabled = false; findBtn.textContent = "🤖 Find comparables (AI)";
            }
          };
        }
        if (r.ungeocoded > 0 && attempt < 12) setTimeout(() => loadPins(attempt + 1), 1500);
      } catch (e) {
        if (note) note.textContent = "Comparables unavailable: " + e.message;
      }
    };
    loadPins();

    // 📍 Neighborhood sales — the Zillow-style sold layer (AI web search)
    const salesBtn = $("#map-load-sales");
    if (salesBtn) {
      salesBtn.disabled = false;
      salesBtn.textContent = (d.area_sales && d.area_sales.length)
        ? "📍 Refresh sales" : "📍 Neighborhood sales";
      salesBtn.onclick = async () => {
        salesBtn.disabled = true;
        salesBtn.innerHTML = '<span class="spinner"></span> Finding 20+ homes… (~60-90 s)';
        if (note) note.textContent = "AI is compiling at least 20 nearby homes with their Zillow prices (Zillow sold, Redfin, records)…";
        try {
          const rr = await API.dealAreaSales(d.id);
          if (!rr.ok) { toast(rr.error || "Failed", "error"); }
          else {
            toast(`✓ ${rr.count} nearby home(s) with Zillow prices — mapping…`, "success");
            renderSalesList(rr.sales);
            d.area_sales = rr.sales;   // keep the in-memory deal in sync
          }
          salesBtn.disabled = false;
          salesBtn.textContent = "📍 Refresh sales";
          loadPins();
        } catch (e) {
          toast(e.message, "error");
          salesBtn.disabled = false; salesBtn.textContent = "📍 Neighborhood sales";
        }
      };
    }
  }

  function renderDetailRisk(data) {
    const host = $("#detail-risk"); if (!host) return;
    const risk = data.risk || {};
    const d = data.deal || {};
    const breakers = risk.deal_breakers || [];
    const flags = risk.risk_flags || [];
    // AI verdict's must-verify checklists (computed by the verdict task)
    const v = (d.ai_insights && d.ai_insights.verdict && d.ai_insights.verdict.result) || {};
    const mvOffer = v.must_verify_before_offer || [];
    const mvClose = v.must_verify_before_closing || [];
    if (!breakers.length && !flags.length && !mvOffer.length && !mvClose.length) {
      host.innerHTML = `<div class="card risk-block"><div style="display:flex;align-items:center;gap:8px;">
        <span class="risk-badge" style="background:${riskColor(risk.risk_grade || 'A')}">🛡 ${risk.risk_grade || 'A'}</span>
        <span style="font-weight:600;">${escape(risk.risk_summary || 'No obvious risk signals')}</span></div>
        <p class="muted" style="margin:6px 0 0;font-size:12.5px;">Run “Full analysis” for a deep AI check (title, liens, history).</p></div>`;
      return;
    }
    const danger = breakers.length > 0;
    const flagRows = flags.map(f =>
      `<div class="risk-flag"><span class="risk-sev ${f.severity}">${f.severity === 'high' ? 'high' : f.severity === 'medium' ? 'medium' : 'low'}</span><span>${escape(f.label)}</span></div>`).join("");
    const breakerRows = breakers.map(b =>
      `<div class="risk-flag"><span class="risk-sev deal_breaker">deal-breaker</span><span><strong>${escape(b)}</strong></span></div>`).join("");
    const checklist = (title, items) => items.length
      ? `<div style="margin-top:12px;"><div style="font-weight:700;font-size:13px;margin-bottom:4px;">${title}</div>${items.map(i => `<label style="display:flex;gap:8px;align-items:flex-start;font-size:13px;padding:3px 0;"><input type="checkbox" style="margin-top:3px;">${escape(i)}</label>`).join("")}</div>`
      : "";
    host.innerHTML = `<div class="card risk-block ${danger ? 'danger' : ''}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <span class="risk-badge" style="background:${riskColor(risk.risk_grade)};font-size:13px;padding:4px 10px;">🛡 Safety ${risk.risk_grade}</span>
        <strong style="font-size:14px;">${escape(risk.risk_summary || '')}</strong>
      </div>
      ${danger ? `<p style="margin:8px 0 4px;color:#b91c1c;font-weight:600;">⛔ Deal-breaker(s) — avoid unless thoroughly verified. Max price stays blocked until this is cleared.</p>` : ""}
      <div style="margin-top:8px;">${breakerRows}${flagRows}</div>
      ${checklist("✅ Verify BEFORE making an offer", mvOffer)}
      ${checklist("✅ Verify BEFORE closing", mvClose)}
      ${(!mvOffer.length && !mvClose.length) ? `<p class="muted" style="margin:10px 0 0;font-size:12px;">Automatic pre-screen (free). Run “Full analysis” for the AI title/liens/history check + the detailed checklist.</p>` : ""}
    </div>`;
  }

  function renderDealDetail(data) {
    const d = data.deal;
    const m = data.metrics;

    // Breadcrumb name in sticky header
    const bn = $("#detail-breadcrumb-name");
    if (bn) bn.textContent = d.address || "Deal detail";

    // === Status pill in header ===
    $("#detail-status-container").innerHTML = `
      <span class="status-pill ${(d.status || 'evaluating').replace('_', '-')}"
            data-id="${d.id}" data-status="${escape(d.status || 'evaluating')}">${escape(d.status || 'evaluating')}</span>`;
    $(".status-pill", $("#detail-status-container"))?.addEventListener("click", e => {
      cycleStatus(d.id, e.target.dataset.status);
      setTimeout(() => openDeal(d.id), 100);
    });

    // === Hero ===
    const hero = $("#detail-hero");
    const sigHex = signalPillClass(data.signal);
    // Zillow market-activity line: time on Zillow (auto) · views · saves (editable —
    // Zillow no longer exposes view/save counts to scrapers, so the user pastes them).
    const _cnt = v => (v != null && v !== "" && !isNaN(v)) ? Number(v).toLocaleString() : "—";
    // Days on Zillow — editable: enter it once, then it auto-increments daily
    // (the backend anchors the listing date and recomputes the count each day).
    const _domVal = (d.days_on_market != null && d.days_on_market !== "") ? d.days_on_market : "";
    const sinceChip = `<span class="zchip" title="Days on Zillow — click to enter, then it auto-increments each day">📅 <span class="editable-count" data-count-field="days_on_market" data-value="${_domVal}">${_domVal !== "" ? _domVal : "—"}</span> days on Zillow</span>`;
    const viewsChip = `<span class="zchip" title="Zillow views (click to enter)">👁 <span class="editable-count" data-count-field="page_view_count" data-value="${d.page_view_count ?? ""}">${_cnt(d.page_view_count)}</span> views</span>`;
    const savesChip = `<span class="zchip" title="Zillow saves (click to enter)">♥ <span class="editable-count" data-count-field="favorite_count" data-value="${d.favorite_count ?? ""}">${_cnt(d.favorite_count)}</span> saves</span>`;
    const zLine = `<div class="detail-hero-zillow">${sinceChip}${viewsChip}${savesChip}</div>`;
    hero.innerHTML = `
      <div class="detail-hero-img" id="hero-img"
           style="${d.image ? `background-image:url('${escape(d.image)}')` : ''}">
        ${!d.image ? '<div class="detail-hero-img-placeholder">🏡</div>' : ''}
        ${(d.image_gallery && d.image_gallery.length > 1) ? `
          <span class="img-count">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="11" height="11"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/></svg>
            ${d.image_gallery.length} photos
          </span>` : ''}
      </div>
      <div class="detail-hero-body">
        <h1>${escape(d.address)}</h1>
        ${renderSourceLinks(d)}
        <div class="detail-hero-meta">
          ${escape(d.city || '')}, ${escape(d.state || '')} ${escape(d.zip || '')}
          ${d.beds ? `• ${d.beds} bd / ${d.baths || '?'} ba` : ''}
          ${d.sqft ? `• ${d.sqft.toLocaleString()} sqft` : ''}
          ${d.year_built ? `• built ${d.year_built}` : ''}
          ${d.lot_size ? `• ${escape(d.lot_size)}` : ''}
        </div>
        ${zLine}
        <div class="detail-hero-stats">
          <div class="detail-hero-stat">
            <span class="lbl">Purchase <span style="font-weight:500; color:var(--accent); text-transform:none; letter-spacing:0;">(editable)</span></span>
            <span class="val">
              <span class="editable" data-field="purchase_price" data-value="${d.purchase_price || 0}">${fmtMoney(d.purchase_price)}</span>
            </span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">ARV (base) <span style="font-weight:500; color:var(--accent); text-transform:none; letter-spacing:0;">(editable)</span></span>
            <span class="val">
              <span class="editable" data-field="arv_base" data-value="${d.arv_base || 0}">${fmtMoney(d.arv_base)}</span>
            </span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">Rehab <span style="font-weight:500; color:var(--accent); text-transform:none; letter-spacing:0;">🛠 estimate</span></span>
            <span class="val">
              <button class="rehab-open-btn" data-rehab-open="${escape(d.id)}" title="Open the rehab estimator">${fmtMoney(d.rehab_base)}</button>
            </span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">🎯 Max offer ${data.max_offer?.basis === "auction" ? "(auction)" : ""}</span>
            <span class="val ${data.max_offer?.blocked ? "price-blocked" : ""}" style="color:${data.max_offer?.blocked ? "var(--red)" : "var(--green)"}; font-weight:800;">
              ${data.max_offer?.max_offer != null ? fmtMoney(data.max_offer.max_offer) : "—"}
            </span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">Net profit (flip)</span>
            <span class="val ${m.net_profit >= 0 ? 'green' : 'red'}">${fmtMoney(m.net_profit, true)}</span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">ROI (annualized)</span>
            <span class="val">${fmtPct(m.roi)} <span class="muted" style="font-size:12px">(${m.annualized_roi.toFixed(0)}%/yr)</span></span>
          </div>
          <div class="detail-hero-stat">
            <span class="lbl">Cap rate</span>
            <span class="val">${fmtPct(m.rent.cap_rate)}</span>
          </div>
        </div>
        <div class="detail-hero-score">
          <span class="score-badge" style="background:${scoreColor(data.score)}; min-width:50px; height:34px; font-size:14px;">
            ${data.score}/100
          </span>
          <div>
            <div style="font-weight:700; color:var(--text);">Grade ${data.grade}</div>
            <div><span class="pill ${sigHex}">${escape(data.signal)}</span></div>
          </div>
          <div style="margin-left:auto; font-size:11px; color:var(--muted); text-align:right;">
            Recommended<br><strong style="color:var(--text); font-size:13px;">${escape(m.recommended_strategy.join(' / '))}</strong>
          </div>
        </div>
      </div>
    `;
    // Hero image click → open lightbox (full gallery, swipeable)
    if (d.image) {
      const heroImgs = (d.image_gallery && d.image_gallery.length) ? d.image_gallery : [d.image];
      $("#hero-img")?.addEventListener("click", () => openLightbox(heroImgs, 0));
    }

    // Wire inline editables
    $$(".editable", hero).forEach(el => attachInlineEdit(el));
    $$(".editable-count", hero).forEach(el => attachCountEdit(el));
    // Rehab value → open the work-items estimator
    $$("[data-rehab-open]", hero).forEach(el =>
      el.addEventListener("click", () => openRehabModal(el.dataset.rehabOpen)));

    // === Safety / risk block ===
    renderDetailRisk(data);
    // === Documents (inspection etc.) ===
    renderDealDocuments(data);
    // === Neighborhood map + comps ===
    renderDealMap(data);
    // === Timestamped comments ===
    renderDealComments(data);

    // === Flip-to-rent alert ===
    const alert = $("#flip-alert");
    if (m.flip_to_rent_alert) {
      alert.style.display = "block";
      $("#flip-alert-text").textContent =
        ` Cap rate ${m.rent.cap_rate.toFixed(1)}% vs flip ROI ${m.roi.toFixed(1)}%. ` +
        "Long-term rental conversion may produce better risk-adjusted returns.";
    } else alert.style.display = "none";

    // === Strategy table ===
    $("#strategy-table").innerHTML = `
      <thead><tr><th>Metric</th><th>Flip</th><th>Rent</th><th>BRRRR</th></tr></thead>
      <tbody>
        <tr><td>One-time profit</td>
          <td class="${moneyClass(m.net_profit)}">${fmtMoney(m.net_profit, true)}</td>
          <td>—</td><td>—</td></tr>
        <tr><td>Monthly cash flow</td>
          <td>—</td>
          <td class="${moneyClass(m.rent.monthly_net)}">${fmtMoney(m.rent.monthly_net, true)}</td>
          <td class="${moneyClass(m.brrrr.monthly_cash_flow)}">${fmtMoney(m.brrrr.monthly_cash_flow, true)}</td></tr>
        <tr><td>Cap rate</td><td>—</td><td>${fmtPct(m.rent.cap_rate)}</td><td>—</td></tr>
        <tr><td>Capital recovered</td><td>—</td><td>—</td><td>${fmtMoney(m.brrrr.refi_value)}</td></tr>
        <tr><td>Horizon</td>
          <td>${d.holding_months || 5} mo</td><td>5+ yrs</td><td>5+ yrs</td></tr>
      </tbody>
    `;

    // === Scenarios ===
    $("#scenario-table").innerHTML = `
      <thead><tr><th>Scenario</th><th>ARV</th><th>Rehab</th><th>Profit</th><th>ROI</th></tr></thead>
      <tbody>
        ${m.scenarios.map(s => `
          <tr><td>${s.name}</td>
            <td>${fmtMoney(s.arv)}</td>
            <td>${fmtMoney(s.rehab)}</td>
            <td class="${moneyClass(s.net)}">${fmtMoney(s.net, true)}</td>
            <td>${fmtPct(s.roi)}</td></tr>
        `).join("")}
      </tbody>
    `;

    // === Back-solver ===
    $("#backsolve-table").innerHTML = `
      <thead><tr><th>Target margin</th><th>Max purchase</th></tr></thead>
      <tbody>${m.backsolve.map(b => `
        <tr><td>${b.target_margin}% of ARV</td>
            <td><strong>${fmtMoney(b.max_purchase)}</strong></td></tr>
      `).join("")}</tbody>
    `;

    // === 70% rule ===
    const ruleClass = m.rule_70_pass ? "green" : "red";
    $("#rule70-table").innerHTML = `
      <tr><td>Status</td><td><span class="pill ${ruleClass}">${m.rule_70_pass ? "PASS" : "FAIL"}</span></td></tr>
      <tr><td>Purchase + Rehab</td><td>${fmtMoney(d.purchase_price + d.rehab_base)}</td></tr>
      <tr><td>70% of ARV</td><td>${fmtMoney(d.arv_base * 0.70)}</td></tr>
      <tr><td>Max Purchase</td><td>${fmtMoney(m.max_purchase_70)}</td></tr>
      <tr><td>Position vs Max</td><td class="${moneyClass(-m.rule_70_overage)}">${fmtMoney(-m.rule_70_overage, true)}</td></tr>
    `;

    // === Financing ===
    $("#financing-table").innerHTML = `
      <thead><tr><th>Option</th><th>Rate</th><th>~6mo Cost</th><th>Cash Needed</th></tr></thead>
      <tbody>${m.financing.map(f => `
        <tr><td><strong>${escape(f.option)}</strong></td>
            <td>${escape(f.rate)}</td>
            <td>${fmtMoney(f.cost_6mo)}</td>
            <td>${fmtMoney(f.total_capital_needed)}</td></tr>
      `).join("")}</tbody>
    `;

    // === Risks ===
    const risks = d.risks || [];
    $("#risks-list").innerHTML = risks.length
      ? risks.map(r => `<li style="padding:8px 0; border-bottom:1px solid var(--border);">
          <span class="pill ${riskPill(r.severity)}" style="margin-right:8px;">${escape(r.severity)}</span>
          ${escape(r.text)}
        </li>`).join("")
      : '<li class="muted">No risks recorded yet.</li>';

    // === ARV anchors (visual bars) ===
    renderArvAnchors(d, m);

    // === Visuals: gallery + map ===
    renderVisuals(d);

    // === Comparables ===
    renderComps(d);

    // === Description ===
    if (d.notes || d.description) {
      $("#description-card").style.display = "block";
      $("#description-text").textContent = d.description || d.notes || "";
    } else $("#description-card").style.display = "none";

    // === Financing scenario card (right after hero) ===
    renderFinancingCard(d, m);

    // === AI Assistant ===
    renderAiPanel(d);

    // === Photo carousel (above hero) ===
    renderHeroCarousel(d.image_gallery || (d.image ? [d.image] : []));

    // === CRM per-deal ===
    renderDealCrm(d);

    // === Chat FAB visible ===
    $("#chat-fab").style.display = "flex";

    $("#pdf-preview").style.display = "none";

    // === Make every block collapsible (click its header to shrink) ===
    initDetailCardCollapse();
  }

  // Click any card's header to collapse/expand it in the deal detail. Uses the
  // card's first child as the clickable header; ignores clicks on controls.
  function initDetailCardCollapse() {
    const root = $("#view-detail"); if (!root) return;
    root.querySelectorAll(".card").forEach(card => {
      if (card.dataset.collapsibleInit) return;
      const header = card.firstElementChild;
      if (!header) return;
      const title = header.matches("h3") ? header : (header.querySelector("h3") || header);
      card.dataset.collapsibleInit = "1";
      card.classList.add("card-collapsible");
      header.classList.add("card-collapse-header");
      title.classList.add("card-collapse-title");
      header.addEventListener("click", (e) => {
        if (e.target.closest("button, a, input, select, textarea, label, .method-chip, .editable")) return;
        const collapsed = card.classList.toggle("card-collapsed");
        // Leaflet needs a resize nudge when its card is re-opened.
        if (!collapsed && card.querySelector("#detail-map") && _dealMap) {
          setTimeout(() => { try { _dealMap.invalidateSize(); } catch {} }, 60);
        }
      });
    });
  }

  // ============== PHOTO CAROUSEL ==============
  let carouselState = { images: [], idx: 0 };

  function renderHeroCarousel(images) {
    const wrap = $("#hero-carousel");
    if (!images || !images.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "block";
    carouselState.images = images;
    carouselState.idx = 0;
    const track = $("#carousel-track");
    track.innerHTML = images.map((url, i) => `
      <div class="carousel-slide" data-idx="${i}" style="background-image:url('${escape(url)}')"></div>
    `).join("");
    track.style.transform = "translateX(0)";

    const dots = $("#carousel-dots");
    dots.innerHTML = images.map((_, i) =>
      `<div class="carousel-dot ${i===0?'active':''}" data-idx="${i}"></div>`).join("");
    $$(".carousel-dot", dots).forEach(d => {
      d.addEventListener("click", e => { e.stopPropagation(); goCarousel(Number(d.dataset.idx)); });
    });
    $$(".carousel-slide", track).forEach(s => {
      s.addEventListener("click", () => openLightbox(images, Number(s.dataset.idx)));
    });
    updateCarousel();
  }

  function updateCarousel() {
    const n = carouselState.images.length;
    $("#carousel-track").style.transform = `translateX(-${carouselState.idx * 100}%)`;
    $$(".carousel-dot", $("#carousel-dots")).forEach((d, i) =>
      d.classList.toggle("active", i === carouselState.idx));
    $("#carousel-counter").textContent = `${carouselState.idx + 1} / ${n}`;
    $("#carousel-prev").disabled = carouselState.idx === 0;
    $("#carousel-next").disabled = carouselState.idx >= n - 1;
  }

  function goCarousel(idx) {
    const n = carouselState.images.length;
    if (n < 2) return;
    carouselState.idx = Math.max(0, Math.min(n - 1, idx));
    updateCarousel();
  }
  function nextCarousel() { goCarousel(carouselState.idx + 1); }
  function prevCarousel() { goCarousel(carouselState.idx - 1); }

  $("#carousel-prev")?.addEventListener("click", e => { e.stopPropagation(); prevCarousel(); });
  $("#carousel-next")?.addEventListener("click", e => { e.stopPropagation(); nextCarousel(); });

  // Touch swipe
  let touchStartX = 0;
  $("#hero-carousel")?.addEventListener("touchstart", e => {
    touchStartX = e.touches[0].clientX;
  }, { passive: true });
  $("#hero-carousel")?.addEventListener("touchend", e => {
    const dx = e.changedTouches[0].clientX - touchStartX;
    if (Math.abs(dx) > 40) dx > 0 ? prevCarousel() : nextCarousel();
  }, { passive: true });

  // Keyboard nav (only when detail view is active and no modal open)
  document.addEventListener("keydown", e => {
    if ($("#view-detail").classList.contains("active") &&
        !document.activeElement.matches("input, textarea") &&
        $("#chat-panel").style.display === "none") {
      if (e.key === "ArrowLeft") { e.preventDefault(); prevCarousel(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); nextCarousel(); }
    }
  });

  // ============== FINANCING CARD ==============
  const FINANCING_METHODS = [
    { id: "cash",         label: "Cash" },
    { id: "hard_money",   label: "Hard Money" },
    { id: "private",      label: "Private Lender" },
    { id: "conventional", label: "Conventional" },
    { id: "heloc",        label: "HELOC" },
  ];
  const METHOD_DEFAULTS = {
    cash:         { ltv_pct: 0,  interest_rate_pct: 0,    origination_pct: 0,   lender_fees_pct: 0,    term_months: 0,  rehab_financed: false },
    // Hard money defaults from a typical fix-and-flip quote: 90% LTV, 11% rate,
    // 2% origination (points) + 3% other lender fees (processing/admin/junk).
    hard_money:   { ltv_pct: 90, interest_rate_pct: 11.0, origination_pct: 2.0, lender_fees_pct: 3.0,  term_months: 6,  rehab_financed: true  },
    private:      { ltv_pct: 80, interest_rate_pct: 9.0,  origination_pct: 1.0, lender_fees_pct: 0,    term_months: 12, rehab_financed: true  },
    conventional: { ltv_pct: 80, interest_rate_pct: 7.5,  origination_pct: 1.0, lender_fees_pct: 0,    term_months: 360, rehab_financed: false },
    heloc:        { ltv_pct: 100, interest_rate_pct: 9.5, origination_pct: 0.0, lender_fees_pct: 0,    term_months: 12, rehab_financed: true  },
  };

  function renderFinancingCard(d, m) {
    const fin = d.financing || { method: "cash", ...METHOD_DEFAULTS.cash };
    const method = fin.method || "cash";
    const sel = m.selected_financing || {};

    // Method chips
    $("#method-chips").innerHTML = FINANCING_METHODS.map(meth => `
      <button class="method-chip ${meth.id === method ? 'active' : ''}" data-method="${meth.id}">${escape(meth.label)}</button>
    `).join("");
    $$("#method-chips button").forEach(b => {
      b.addEventListener("click", async () => {
        const newMethod = b.dataset.method;
        const newFin = { method: newMethod, ...METHOD_DEFAULTS[newMethod] };
        try {
          const updated = await API.patchDeal(state.currentDealId, { financing: newFin });
          toast(`Method: ${FINANCING_METHODS.find(x => x.id === newMethod).label}`, "success");
          renderDealDetail(updated);
        } catch (e) { toast(e.message, "error"); }
      });
    });

    const isCash = method === "cash";
    const dim = isCash ? "disabled" : "";

    $("#financing-inputs").innerHTML = `
      <div class="fin-input">
        <span class="lbl">Loan-to-value</span>
        <span class="val ${dim}">
          <span class="editable" data-fin-field="ltv_pct" data-value="${fin.ltv_pct || 0}"
                ${isCash ? 'style="pointer-events:none;"' : ''}>${fin.ltv_pct || 0}%</span>
        </span>
      </div>
      <div class="fin-input">
        <span class="lbl">Interest rate</span>
        <span class="val ${dim}">
          <span class="editable" data-fin-field="interest_rate_pct" data-value="${fin.interest_rate_pct || 0}"
                ${isCash ? 'style="pointer-events:none;"' : ''}>${(fin.interest_rate_pct || 0).toFixed(2)}%</span>
        </span>
      </div>
      <div class="fin-input">
        <span class="lbl">Origination (points)</span>
        <span class="val ${dim}">
          <span class="editable" data-fin-field="origination_pct" data-value="${fin.origination_pct || 0}"
                ${isCash ? 'style="pointer-events:none;"' : ''}>${(fin.origination_pct || 0).toFixed(2)}%</span>
        </span>
      </div>
      <div class="fin-input">
        <span class="lbl" title="Miscellaneous lender fees — processing, admin & junk fees — as % of the loan amount">Other lender fees</span>
        <span class="val ${dim}">
          <span class="editable" data-fin-field="lender_fees_pct" data-value="${fin.lender_fees_pct || 0}"
                ${isCash ? 'style="pointer-events:none;"' : ''}>${(fin.lender_fees_pct || 0).toFixed(2)}%</span>
        </span>
      </div>
      <div class="fin-input">
        <span class="lbl">Term</span>
        <span class="val ${dim}">
          <span class="editable" data-fin-field="term_months" data-value="${fin.term_months || 0}"
                ${isCash ? 'style="pointer-events:none;"' : ''}>${fin.term_months || 0} mo</span>
        </span>
      </div>
      <div class="fin-input">
        <span class="lbl">Rehab financed?</span>
        <div class="toggle-yes-no">
          <button data-rehab="true" class="yes ${fin.rehab_financed ? 'active' : ''}" ${isCash ? 'disabled' : ''}>Yes</button>
          <button data-rehab="false" class="no ${!fin.rehab_financed ? 'active' : ''}" ${isCash ? 'disabled' : ''}>No</button>
        </div>
      </div>
    `;

    // Wire inline edits for financing fields
    $$(".editable[data-fin-field]", $("#financing-inputs")).forEach(el => attachFinEdit(el));

    // Toggle rehab financed
    $$("[data-rehab]", $("#financing-inputs")).forEach(b => {
      b.addEventListener("click", async () => {
        if (b.disabled) return;
        const val = b.dataset.rehab === "true";
        try {
          const newFin = { ...fin, rehab_financed: val };
          const updated = await API.patchDeal(state.currentDealId, { financing: newFin });
          renderDealDetail(updated);
        } catch (e) { toast(e.message, "error"); }
      });
    });

    // Outputs
    if (isCash) {
      const cashNeeded = (d.purchase_price || 0) + (d.rehab_base || 0);
      $("#financing-outputs").innerHTML = `
        <div class="fin-out highlight">
          <div class="lbl">Cash needed up-front</div>
          <div class="val">${fmtMoney(cashNeeded)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Financing cost</div>
          <div class="val">$0</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Net profit</div>
          <div class="val ${m.net_profit >= 0 ? 'green' : 'red'}">${fmtMoney(m.net_profit, true)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">ROI on cash</div>
          <div class="val">${fmtPct(m.roi)} <span class="muted" style="font-size:11px">(${m.annualized_roi.toFixed(0)}%/yr)</span></div>
        </div>
      `;
    } else {
      $("#financing-outputs").innerHTML = `
        <div class="fin-out">
          <div class="lbl">Loan amount</div>
          <div class="val">${fmtMoney(sel.loan_amount)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Interest cost (${sel.term_months}mo)</div>
          <div class="val">${fmtMoney(sel.interest_cost)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Points paid</div>
          <div class="val">${fmtMoney(sel.points_paid)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Other lender fees (${(sel.lender_fees_pct || 0).toFixed(2)}%)</div>
          <div class="val">${fmtMoney(sel.lender_fees_paid)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Total financing cost</div>
          <div class="val red">${fmtMoney(sel.total_financing_cost)}</div>
        </div>
        <div class="fin-out highlight">
          <div class="lbl">Cash needed up-front</div>
          <div class="val accent">${fmtMoney(sel.cash_needed_up_front)}</div>
        </div>
        <div class="fin-out">
          <div class="lbl">Net profit after financing</div>
          <div class="val ${sel.net_profit_after_financing >= 0 ? 'green' : 'red'}">${fmtMoney(sel.net_profit_after_financing, true)}</div>
        </div>
        <div class="fin-out highlight">
          <div class="lbl">ROI on cash</div>
          <div class="val ${sel.roi_on_cash >= 0 ? 'green' : 'red'}">${fmtPct(sel.roi_on_cash)}
            <span class="muted" style="font-size:11px">(${sel.roi_on_cash_annualized.toFixed(0)}%/yr)</span></div>
        </div>
      `;
    }
  }

  function attachFinEdit(el) {
    el.addEventListener("click", e => {
      e.stopPropagation();
      if (el.classList.contains("editing")) return;
      const field = el.dataset.finField;
      const current = Number(el.dataset.value || 0);
      const input = document.createElement("input");
      input.type = "number";
      input.value = current || "";
      input.className = "inline-input";
      input.step = field === "term_months" ? "1" : "0.1";
      input.min = "0";
      el.classList.add("editing");
      el.textContent = "";
      el.appendChild(input);
      input.focus();
      input.select();

      const cancel = () => {
        el.classList.remove("editing");
        const isPct = field !== "term_months";
        el.textContent = isPct ? `${current.toFixed(2)}%` : `${current} mo`;
      };
      const save = async () => {
        const newVal = Number(input.value);
        if (isNaN(newVal) || newVal === current) { cancel(); return; }
        const deal = state.deals.find(x => x.id === state.currentDealId);
        const fin = { ...(deal?.financing || { method: "hard_money", ...METHOD_DEFAULTS.hard_money }), [field]: newVal };
        el.classList.remove("editing");
        try {
          const updated = await API.patchDeal(state.currentDealId, { financing: fin });
          toast(`${prettyField(field)} updated → ${field === 'term_months' ? newVal + ' mo' : newVal + '%'}`, "success");
          renderDealDetail(updated);
        } catch (e) {
          toast("Update failed: " + e.message, "error");
          cancel();
        }
      };
      input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      });
      input.addEventListener("blur", save);
    });
  }

  // ============== INLINE EDITING ==============
  function attachInlineEdit(el) {
    el.addEventListener("click", e => {
      e.stopPropagation();
      if (el.classList.contains("editing")) return;
      const field = el.dataset.field;
      const current = Number(el.dataset.value || 0);
      const input = document.createElement("input");
      input.type = "number";
      input.value = current || "";
      input.className = "inline-input large";
      input.step = "1000";
      input.min = "0";
      el.classList.add("editing");
      el.textContent = "";
      el.appendChild(input);
      input.focus();
      input.select();

      const cleanup = () => { el.classList.remove("editing"); };
      const cancel = () => {
        cleanup();
        el.textContent = fmtMoney(current);
      };
      const save = async () => {
        const newVal = Number(input.value);
        if (isNaN(newVal) || newVal === current) { cancel(); return; }
        cleanup();
        el.textContent = fmtMoney(newVal);
        el.classList.add("saving");
        el.dataset.value = newVal;
        try {
          const updated = await API.patchDeal(state.currentDealId, { [field]: newVal });
          el.classList.remove("saving");
          el.classList.add("saved");
          setTimeout(() => el.classList.remove("saved"), 700);
          toast(`${prettyField(field)} updated → ${fmtMoney(newVal)}`, "success");
          // Re-render the whole detail so all metrics update
          renderDealDetail(updated);
        } catch (e) {
          el.classList.remove("saving");
          el.textContent = fmtMoney(current);
          el.dataset.value = current;
          toast("Update failed: " + e.message, "error");
        }
      };

      input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      });
      input.addEventListener("blur", save);
    });
  }

  // Inline editor for plain integer counts (Zillow views / saves).
  function attachCountEdit(el) {
    const fmt = v => (v != null && v !== "" && !isNaN(v)) ? Number(v).toLocaleString() : "—";
    el.addEventListener("click", e => {
      e.stopPropagation();
      if (el.classList.contains("editing")) return;
      const field = el.dataset.countField;
      const current = el.dataset.value === "" ? null : Number(el.dataset.value);
      const input = document.createElement("input");
      input.type = "number"; input.min = "0"; input.step = "1";
      input.value = current ?? ""; input.className = "inline-input";
      input.style.width = "80px";
      el.classList.add("editing"); el.textContent = ""; el.appendChild(input);
      input.focus(); input.select();
      const cleanup = () => el.classList.remove("editing");
      const cancel = () => { cleanup(); el.textContent = fmt(current); };
      const save = async () => {
        const raw = input.value.trim();
        const newVal = raw === "" ? null : Number(raw);
        if (raw !== "" && isNaN(newVal)) { cancel(); return; }
        if (newVal === current) { cancel(); return; }
        cleanup(); el.textContent = fmt(newVal);
        el.dataset.value = newVal == null ? "" : newVal;
        el.classList.add("saving");
        try {
          await API.patchDeal(state.currentDealId, { [field]: newVal });
          el.classList.remove("saving"); el.classList.add("saved");
          setTimeout(() => el.classList.remove("saved"), 700);
        } catch (err) {
          el.classList.remove("saving");
          el.textContent = fmt(current); el.dataset.value = current == null ? "" : current;
          toast("Update failed: " + err.message, "error");
        }
      };
      input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      });
      input.addEventListener("blur", save);
    });
  }

  function prettyField(f) {
    return ({
      purchase_price: "Purchase price",
      arv_base: "ARV (base)",
      arv_low: "ARV (low)",
      arv_high: "ARV (high)",
      rehab_base: "Rehab budget",
      holding_months: "Holding period",
      holding_cost_monthly: "Holding cost/mo",
      selling_cost_pct: "Selling cost %",
      estimated_rent: "Estimated rent",
      ltv_pct: "Loan-to-value",
      interest_rate_pct: "Interest rate",
      origination_pct: "Origination",
      lender_fees_pct: "Other lender fees",
      term_months: "Loan term",
    })[f] || f;
  }

  function riskPill(sev) {
    const s = (sev || "").toUpperCase();
    if (s.includes("CRIT")) return "red";
    if (s.includes("HIGH") && !s.includes("LOW")) return "red";
    if (s.includes("MED-HIGH")) return "orange";
    if (s.includes("LOW-MED")) return "yellow";
    if (s.includes("MED")) return "yellow";
    if (s.includes("LOW")) return "green";
    return "gray";
  }

  function renderArvAnchors(d, m) {
    const anchors = [];
    if (d.arv_base) anchors.push({ label: "Your ARV (base)", value: d.arv_base, color: "#10B981" });
    if (d.zillow_estimate) anchors.push({ label: "Zillow", value: d.zillow_estimate, color: "#3B82F6" });
    if (d.realtor_estimate) anchors.push({ label: "Realtor", value: d.realtor_estimate, color: "#8B5CF6" });
    if (d.redfin_estimate) anchors.push({ label: "Redfin", value: d.redfin_estimate, color: "#EF4444" });
    if (d.comp_value_estimate) anchors.push({ label: "Sale comps avg", value: d.comp_value_estimate, color: "#F59E0B" });
    if (m.scenarios && m.scenarios[2]) anchors.push({ label: "Worst case", value: m.scenarios[2].arv, color: "#94A3B8" });

    const card = $("#arv-anchors-card");
    if (anchors.length < 2) { card.style.display = "none"; return; }
    card.style.display = "block";
    const max = Math.max(...anchors.map(a => a.value));
    $("#arv-anchors-bars").innerHTML = anchors.map(a => `
      <div class="arv-anchor">
        <div class="arv-anchor-label">${escape(a.label)}</div>
        <div class="arv-anchor-bar">
          <div class="arv-anchor-bar-fill" style="width:${(a.value / max * 100).toFixed(1)}%; background:${a.color}"></div>
        </div>
        <div class="arv-anchor-value">${fmtMoney(a.value)}</div>
      </div>
    `).join("");

    // Spread warning
    const min = Math.min(...anchors.map(a => a.value));
    const spread = (max - min) / min * 100;
    if (spread > 15) {
      $("#arv-anchors-bars").insertAdjacentHTML("beforeend",
        `<div style="margin-top:12px; padding:10px 12px; background:var(--orange-light); color:var(--orange);
                font-size:12.5px; border-radius:8px; font-weight:500;">
          ⚠ Spread of ${spread.toFixed(0)}% between sources — verify ARV with independent comps before committing.
        </div>`);
    }
  }

  function renderVisuals(d) {
    const gallery = d.image_gallery || (d.image ? [d.image] : []);
    state.gallery = gallery;
    const visuals = $("#visuals-section");

    if (gallery.length || d.lat) {
      visuals.style.display = "grid";
    } else {
      visuals.style.display = "none";
      return;
    }

    // Gallery
    if (gallery.length) {
      $("#gallery-card").style.display = "block";
      $("#gallery-count").textContent = `(${gallery.length})`;
      $("#gallery").innerHTML = gallery.slice(0, 12).map((url, i) => `
        <div class="gallery-img" data-idx="${i}" style="background-image:url('${escape(url)}')"></div>
      `).join("");
      $$(".gallery-img", $("#gallery")).forEach(el => {
        el.addEventListener("click", () => openLightbox(gallery, Number(el.dataset.idx)));
      });
    } else $("#gallery-card").style.display = "none";

    // Map
    if (d.lat && d.lng) {
      $("#map-card").style.display = "block";
      const q = encodeURIComponent(d.address || `${d.lat},${d.lng}`);
      $("#map-iframe").src =
        `https://www.google.com/maps?q=${q}&output=embed&z=15`;
    } else $("#map-card").style.display = "none";
  }

  function renderComps(d) {
    const sale = d.sale_comparables || [];
    const rent = d.rent_comparables || [];
    const section = $("#comps-section");
    if (!sale.length && !rent.length) { section.style.display = "none"; return; }
    section.style.display = "grid";
    $("#sale-comps-count").textContent = sale.length;
    $("#rent-comps-count").textContent = rent.length;

    if (sale.length) {
      $("#sale-comps-table").innerHTML = `
        <thead><tr><th>Address</th><th>Bd/Ba</th><th>SqFt</th><th>Price</th><th>$/sf</th><th>Date</th></tr></thead>
        <tbody>${sale.map(c => `
          <tr>
            <td>${escape((c.address || '').split(',')[0])}</td>
            <td>${c.beds || '?'}/${c.baths || '?'}</td>
            <td>${c.sqft ? c.sqft.toLocaleString() : '—'}</td>
            <td><strong>${fmtMoney(c.price)}</strong></td>
            <td>${c.sqft && c.price ? '$' + Math.round(c.price / c.sqft) : '—'}</td>
            <td class="muted">${escape(c.date || '')}</td>
          </tr>`).join('')}</tbody>
      `;
    } else $("#sale-comps-table").innerHTML = '<tbody><tr><td class="muted" style="padding:14px;">No sale comps available.</td></tr></tbody>';

    if (rent.length) {
      $("#rent-comps-table").innerHTML = `
        <thead><tr><th>Address</th><th>Bd/Ba</th><th>SqFt</th><th>Rent</th><th>$/sf/yr</th><th>Date</th></tr></thead>
        <tbody>${rent.map(c => `
          <tr>
            <td>${escape((c.address || '').split(',')[0])}</td>
            <td>${c.beds || '?'}/${c.baths || '?'}</td>
            <td>${c.sqft ? c.sqft.toLocaleString() : '—'}</td>
            <td><strong>${fmtMoney(c.rent)}/mo</strong></td>
            <td>${c.sqft && c.rent ? '$' + (c.rent * 12 / c.sqft).toFixed(1) : '—'}</td>
            <td class="muted">${escape(c.date || '')}</td>
          </tr>`).join('')}</tbody>
      `;
    } else $("#rent-comps-table").innerHTML = '<tbody><tr><td class="muted" style="padding:14px;">No rent comps available.</td></tr></tbody>';
  }

  // ============== AI ASSISTANT ==============
  let aiTaskRegistry = null;

  async function renderAiPanel(deal) {
    if (!aiTaskRegistry) {
      try { aiTaskRegistry = await API.aiTasks(); }
      catch { aiTaskRegistry = []; }
    }
    const insights = deal.ai_insights || {};
    const groups = {};
    aiTaskRegistry.forEach(t => {
      if (!groups[t.group]) groups[t.group] = [];
      groups[t.group].push(t);
    });
    const GROUP_LABELS = {
      research: "🔬 Research",
      analysis: "🧠 Analysis",
      action: "⚡ Action",
      content: "✍️ Content",
    };
    let html = "";
    Object.entries(groups).forEach(([gk, tasks]) => {
      html += `<div class="ai-group">
        <div class="ai-group-label">${GROUP_LABELS[gk] || gk}</div>
        <div class="ai-task-grid">
          ${tasks.map(t => renderAiChip(t, insights[t.name])).join("")}
        </div>
      </div>`;
    });
    $("#ai-task-groups").innerHTML = html;

    $$(".ai-task-chip", $("#ai-panel")).forEach(chip => {
      chip.addEventListener("click", () => runAiTask(chip.dataset.task));
    });

    // Render existing insights below
    const resultsHolder = $("#ai-results-holder") || (() => {
      const div = document.createElement("div");
      div.id = "ai-results-holder";
      $("#ai-panel").appendChild(div);
      return div;
    })();
    resultsHolder.innerHTML = "";
    aiTaskRegistry.forEach(t => {
      if (insights[t.name]) {
        resultsHolder.appendChild(renderAiResult(t, insights[t.name]));
      }
    });

    // Verdict hero
    if (insights.verdict && insights.verdict.result) {
      renderVerdictHero(insights.verdict);
    } else $("#verdict-hero").style.display = "none";

    // Model pill
    try {
      const cfg = await API.aiConfig();
      $("#ai-model-pill").textContent = (cfg.model || "claude-opus-4-8").replace("claude-", "");
    } catch {}
  }

  function renderAiChip(t, insight) {
    const cls = insight ? "ai-task-chip has-result" : "ai-task-chip";
    const status = insight
      ? `<span class="ai-task-status done">✓</span>`
      : "";
    const vision = t.uses_vision ? '<span class="pill purple" style="font-size:9px; padding:1px 5px;">vision</span>' : '';
    const web = t.uses_web ? '<span class="pill blue" style="font-size:9px; padding:1px 5px;">web</span>' : '';
    return `
      <div class="${cls}" data-task="${escape(t.name)}" title="${escape(t.desc)}">
        ${status}
        <div class="ai-task-icon">${t.icon}</div>
        <div class="ai-task-info">
          <div class="ai-task-label">${escape(t.label)} ${vision}${web}</div>
          <div class="ai-task-desc">${escape(t.desc)}</div>
        </div>
      </div>`;
  }

  async function runAiTask(taskName) {
    if (!state.currentDealId) return;
    const chip = $(`.ai-task-chip[data-task="${taskName}"]`);
    if (chip) {
      chip.classList.add("running");
      chip.querySelector(".ai-task-status")?.remove();
      chip.insertAdjacentHTML("afterbegin",
        '<span class="ai-task-status running"><span class="spinner" style="width:9px; height:9px; border-width:1.5px;"></span>RUNNING</span>');
    }
    try {
      const result = await API.aiRun(taskName, state.currentDealId);
      toast(`✓ ${aiTaskRegistry.find(t => t.name === taskName)?.label || taskName} done`, "success");
      const fresh = await API.getDeal(state.currentDealId);
      renderAiPanel(fresh.deal);
    } catch (e) {
      showAiError(e.message, taskName);
      if (chip) {
        chip.classList.remove("running");
        chip.querySelector(".ai-task-status")?.remove();
      }
    }
  }

  function showAiError(msg, taskName) {
    // Detect billing error → rich modal with billing link
    const lower = (msg || "").toLowerCase();
    if (lower.includes("credit") || lower.includes("balance") || lower.includes("billing")) {
      showAiErrorModal({
        icon: "💳",
        title: "Out of Anthropic credits",
        message: msg,
        action: {
          label: "Open billing page",
          href: "https://console.anthropic.com/settings/billing",
          variant: "primary",
        },
        secondary: {
          label: "Switch to Sonnet 4.5 (cheaper)",
          onclick: () => { showView("settings"); setTimeout(() => $("#ai-model-input")?.focus(), 300); },
        },
        hint: "Add $10-20 for 30-100 AI runs. Or switch to a cheaper model (Sonnet 4.5 / Haiku 4.5).",
      });
    } else if (lower.includes("api key") || lower.includes("auth") || lower.includes("invalid")) {
      showAiErrorModal({
        icon: "🔑",
        title: "API key issue",
        message: msg,
        secondary: {
          label: "Open Settings",
          onclick: () => { showView("settings"); setTimeout(() => $("#ai-key-input")?.focus(), 300); },
        },
      });
    } else if (lower.includes("rate") || lower.includes("429")) {
      showAiErrorModal({
        icon: "⏳",
        title: "Rate limited",
        message: msg,
        hint: "Wait 60 seconds, then retry.",
      });
    } else if (lower.includes("overloaded") || lower.includes("503")) {
      showAiErrorModal({
        icon: "🌐",
        title: "API overloaded",
        message: msg,
        hint: "Anthropic's API is busy. Retry in a moment.",
      });
    } else {
      toast("Task failed: " + msg, "error");
    }
  }

  function showAiErrorModal(opts) {
    // Build modal on the fly
    let modal = $("#ai-error-modal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "ai-error-modal";
      modal.className = "modal";
      document.body.appendChild(modal);
    }
    modal.style.display = "flex";
    modal.innerHTML = `
      <div class="modal-backdrop"></div>
      <div class="modal-content" style="max-width:480px">
        <div class="modal-body" style="padding:32px 28px; text-align:center;">
          <div style="font-size:48px; margin-bottom:14px;">${opts.icon}</div>
          <h2 style="font-size:18px; margin-bottom:10px; color:var(--text); letter-spacing:-0.3px;">${escape(opts.title)}</h2>
          <p style="color:var(--text-2); font-size:13.5px; line-height:1.6; margin-bottom:${opts.hint ? '10px' : '20px'};">${escape(opts.message)}</p>
          ${opts.hint ? `<p style="color:var(--muted); font-size:12px; margin-bottom:20px; padding:10px 14px; background:var(--bg-2); border-radius:8px;">${escape(opts.hint)}</p>` : ''}
          <div style="display:flex; gap:10px; justify-content:center; flex-wrap:wrap;">
            ${opts.action ? `<a href="${escape(opts.action.href)}" target="_blank" class="btn ${opts.action.variant || ''}">${escape(opts.action.label)} ↗</a>` : ''}
            ${opts.secondary ? `<button class="btn" id="ai-err-secondary">${escape(opts.secondary.label)}</button>` : ''}
            <button class="btn ghost" id="ai-err-close">Close</button>
          </div>
        </div>
      </div>
    `;
    modal.querySelector(".modal-backdrop").addEventListener("click", () => modal.style.display = "none");
    $("#ai-err-close")?.addEventListener("click", () => modal.style.display = "none");
    if (opts.secondary?.onclick) {
      $("#ai-err-secondary")?.addEventListener("click", () => {
        modal.style.display = "none";
        opts.secondary.onclick();
      });
    }
  }

  function renderAiResult(t, insight) {
    const r = insight.result || {};
    const wrap = document.createElement("div");
    wrap.className = "ai-result-panel";
    let body = "";
    try {
      // Each task type has a specific renderer
      const rfn = AI_RENDERERS[t.name] || AI_RENDERERS._fallback;
      body = rfn(r);
    } catch (e) { body = `<pre style="font-size:11px; color:var(--red);">${escape(e.message)}</pre>`; }

    const ranAt = insight.ran_at ? new Date(insight.ran_at).toLocaleString() : "";
    const usage = insight.usage || {};
    wrap.innerHTML = `
      <div class="ai-result-header">
        <h3>${t.icon} ${escape(t.label)}</h3>
        <div class="ai-result-meta">
          ${insight.web_searches_used ? `🌐 ${insight.web_searches_used} searches • ` : ''}
          ${usage.input_tokens || 0}→${usage.output_tokens || 0} tok • ${escape(insight.model || '')}
          <button class="btn ghost" data-clear-task="${escape(t.name)}" style="padding:2px 6px; font-size:11px; margin-left:8px;">×</button>
        </div>
      </div>
      <div class="ai-result-content">${body}</div>
    `;
    wrap.querySelector("[data-clear-task]")?.addEventListener("click", async () => {
      try {
        await API.aiClearInsight(state.currentDealId, t.name);
        const fresh = await API.getDeal(state.currentDealId);
        renderAiPanel(fresh.deal);
      } catch (e) { toast(e.message, "error"); }
    });
    return wrap;
  }

  // ============== AI RENDERERS ==============
  const AI_RENDERERS = {
    _fallback: (r) => `<pre style="font-size:11px; white-space:pre-wrap;">${escape(JSON.stringify(r, null, 2))}</pre>`,

    arv: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">ARV Low</div><div class="val">${fmtMoney(r.arv_low)}</div></div>
        <div class="ai-mini-stat" style="background:var(--accent-light)"><div class="lbl">ARV Base</div><div class="val" style="color:var(--accent-dark)">${fmtMoney(r.arv_base)}</div></div>
        <div class="ai-mini-stat"><div class="lbl">ARV High</div><div class="val">${fmtMoney(r.arv_high)}</div></div>
      </div>
      <span class="pill ${r.confidence === 'High' ? 'green' : r.confidence === 'Low' ? 'red' : 'yellow'}">${escape(r.confidence || '')} confidence</span>
      <div class="ai-section"><h4>Reasoning</h4><p>${escape(r.reasoning || '')}</p></div>
      ${r.comparables && r.comparables.length ? `<div class="ai-section"><h4>Comparables (${r.comparables.length})</h4>
        <table class="ai-line-items"><thead><tr><th>Address</th><th>Bd/Ba</th><th>SqFt</th><th>Price</th><th>Date</th></tr></thead>
        <tbody>${r.comparables.map(c => `<tr><td>${escape((c.address||'').split(',')[0])}</td><td>${c.beds||'?'}/${c.baths||'?'}</td><td>${c.sqft?c.sqft.toLocaleString():''}</td><td class="num">${fmtMoney(c.price)}</td><td>${escape(c.date||'')}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${r.market_notes ? `<div class="ai-section"><h4>Market notes</h4><p>${escape(r.market_notes)}</p></div>` : ''}
      ${r.warnings && r.warnings.length ? `<div class="ai-section"><h4>⚠ Warnings</h4><ul>${r.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul></div>` : ''}
    `,

    rehab: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">Total Low</div><div class="val">${fmtMoney(r.total_low)}</div></div>
        <div class="ai-mini-stat" style="background:var(--accent-light)"><div class="lbl">Base</div><div class="val" style="color:var(--accent-dark)">${fmtMoney(r.total_base)}</div></div>
        <div class="ai-mini-stat"><div class="lbl">High</div><div class="val">${fmtMoney(r.total_high)}</div></div>
      </div>
      <p style="font-size:12px; color:var(--muted);">Regional ×${r.regional_multiplier} (${escape(r.regional_basis || '')}) — Scope: <strong>${escape(r.scope_recommended || '')}</strong> — Timeline: ${r.timeline_weeks?.base || '?'} weeks</p>
      <div class="ai-section"><h4>Line items</h4>
        <table class="ai-line-items"><thead><tr><th>Category</th><th>Low</th><th>Base</th><th>High</th><th>Notes</th></tr></thead>
        <tbody>${(r.line_items || []).map(li => `<tr><td><strong>${escape(li.category)}${li.count?' ×'+li.count:''}</strong></td><td class="num">${fmtMoney(li.low)}</td><td class="num">${fmtMoney(li.base)}</td><td class="num">${fmtMoney(li.high)}</td><td class="muted" style="font-size:11px;">${escape(li.notes||'')}</td></tr>`).join('')}
        <tr><td><em>Subtotal</em></td><td class="num">${fmtMoney(r.subtotal_low)}</td><td class="num">${fmtMoney(r.subtotal_base)}</td><td class="num">${fmtMoney(r.subtotal_high)}</td><td></td></tr>
        <tr><td><em>Contingency ${r.contingency_pct}%</em></td><td class="num" colspan="3" style="text-align:right;">${fmtMoney(r.contingency_amount)}</td><td></td></tr>
        <tr class="total"><td>TOTAL</td><td class="num">${fmtMoney(r.total_low)}</td><td class="num">${fmtMoney(r.total_base)}</td><td class="num">${fmtMoney(r.total_high)}</td><td></td></tr>
        </tbody></table></div>
      ${r.scope_notes ? `<div class="ai-section"><h4>Scope notes</h4><p>${escape(r.scope_notes)}</p></div>` : ''}
      ${r.risk_flags && r.risk_flags.length ? `<div class="ai-section"><h4>⚠ Risk flags</h4><ul>${r.risk_flags.map(x => `<li>${escape(x)}</li>`).join('')}</ul></div>` : ''}
    `,

    rent_comps: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">Rent Low</div><div class="val">${fmtMoney(r.rent_low)}/mo</div></div>
        <div class="ai-mini-stat" style="background:var(--accent-light)"><div class="lbl">Base</div><div class="val" style="color:var(--accent-dark)">${fmtMoney(r.rent_base)}/mo</div></div>
        <div class="ai-mini-stat"><div class="lbl">High</div><div class="val">${fmtMoney(r.rent_high)}/mo</div></div>
      </div>
      <span class="pill ${r.confidence === 'High' ? 'green' : r.confidence === 'Low' ? 'red' : 'yellow'}">${escape(r.confidence || '')} confidence</span>
      ${r.best_rent_strategy ? `<span class="pill blue" style="margin-left:6px;">${escape(r.best_rent_strategy)}</span>` : ''}
      ${r.occupancy_estimate_pct ? `<span class="muted" style="margin-left:8px;">Occupancy ${r.occupancy_estimate_pct}%</span>` : ''}
      ${r.comparables ? `<div class="ai-section"><h4>Rental comps (${r.comparables.length})</h4>
        <table class="ai-line-items"><thead><tr><th>Address</th><th>Bd/Ba</th><th>SqFt</th><th>Rent</th><th>Date</th></tr></thead>
        <tbody>${r.comparables.map(c => `<tr><td>${escape((c.address||'').split(',')[0])}</td><td>${c.beds||'?'}/${c.baths||'?'}</td><td>${c.sqft?c.sqft.toLocaleString():''}</td><td class="num">${fmtMoney(c.rent_per_mo)}/mo</td><td>${escape(c.date||'')}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${r.market_notes ? `<div class="ai-section"><h4>Market</h4><p>${escape(r.market_notes)}</p></div>` : ''}
    `,

    neighborhood: (r) => {
      const s = r.school_ratings || {};
      const c = r.crime || {};
      const d = r.demographics || {};
      const g = r.growth_outlook || {};
      const h = r.hazards || {};
      return `
        <div class="ai-stat-row">
          <div class="ai-mini-stat"><div class="lbl">Walk Score</div><div class="val">${r.walk_score_estimate || '?'}</div></div>
          <div class="ai-mini-stat"><div class="lbl">Crime Grade</div><div class="val" style="color:${c.overall_grade==='A'?'var(--green)':c.overall_grade==='F'?'var(--red)':'var(--text)'}">${escape(c.overall_grade || '?')}</div></div>
          <div class="ai-mini-stat"><div class="lbl">Growth</div><div class="val" style="font-size:14px;">${escape(g.rating || '?')}</div></div>
        </div>
        <div class="ai-section"><h4>Schools</h4>
          <table class="ai-line-items"><tbody>
            <tr><td>Elementary</td><td>${escape(s.elementary?.name || '?')}</td><td class="num">${s.elementary?.rating_out_of_10 || '?'}/10</td></tr>
            <tr><td>Middle</td><td>${escape(s.middle?.name || '?')}</td><td class="num">${s.middle?.rating_out_of_10 || '?'}/10</td></tr>
            <tr><td>High</td><td>${escape(s.high?.name || '?')}</td><td class="num">${s.high?.rating_out_of_10 || '?'}/10</td></tr>
          </tbody></table></div>
        <div class="ai-section"><h4>Crime</h4><p>${escape(c.summary || '')}</p></div>
        <div class="ai-section"><h4>Demographics</h4>
          <p>Pop: ${escape(d.population || '?')} • Median income: ${escape(d.median_income || '?')} • Owner-occupied: ${d.owner_occupied_pct || '?'}%</p></div>
        ${g.factors && g.factors.length ? `<div class="ai-section"><h4>Growth factors</h4><ul>${g.factors.map(f => `<li>${escape(f)}</li>`).join('')}</ul></div>` : ''}
        ${r.developments && r.developments.length ? `<div class="ai-section"><h4>Recent developments</h4><ul>${r.developments.map(d => `<li>${escape(d)}</li>`).join('')}</ul></div>` : ''}
        ${(h.flood_zone || h.fire_risk) ? `<div class="ai-section"><h4>Hazards</h4><p>Flood zone: <strong>${escape(h.flood_zone || '?')}</strong> • Fire risk: <strong>${escape(h.fire_risk || '?')}</strong></p></div>` : ''}
        ${r.investor_summary ? `<div class="ai-section"><h4>Investor summary</h4><p>${escape(r.investor_summary)}</p></div>` : ''}
      `;
    },

    taxes_insurance: (r) => {
      const t = r.property_tax || {}, i = r.insurance || {}, h = r.hoa || {};
      return `
        <div class="ai-stat-row">
          <div class="ai-mini-stat"><div class="lbl">Property tax / mo</div><div class="val">${fmtMoney(t.monthly_estimate)}</div></div>
          <div class="ai-mini-stat"><div class="lbl">Insurance / mo</div><div class="val">${fmtMoney(i.monthly_estimate)}</div></div>
          <div class="ai-mini-stat"><div class="lbl">HOA / mo</div><div class="val">${h.applies ? fmtMoney(h.monthly_estimate) : "—"}</div></div>
        </div>
        <p class="muted">Annual: taxes ${fmtMoney(t.annual_estimate)} (${t.effective_rate_pct}%) • insurance ${fmtMoney(i.annual_estimate)} (${escape(i.type || '')})</p>
        ${t.source_note ? `<div class="ai-section"><h4>Tax source</h4><p>${escape(t.source_note)}</p></div>` : ''}
        ${i.notes ? `<div class="ai-section"><h4>Insurance notes</h4><p>${escape(i.notes)}</p></div>` : ''}
        ${h.notes ? `<div class="ai-section"><h4>HOA</h4><p>${escape(h.notes)}</p></div>` : ''}
      `;
    },

    history: (r) => `
      ${r.last_sale ? `<p>Last sale: <strong>${fmtMoney(r.last_sale.price)}</strong> on ${escape(r.last_sale.date)} (${r.last_sale.appreciation_since_pct}% appreciation since)</p>` : ''}
      ${r.sales_history && r.sales_history.length ? `<div class="ai-section"><h4>Sales history</h4>
        <table class="ai-line-items"><thead><tr><th>Date</th><th>Price</th><th>Notes</th></tr></thead>
        <tbody>${r.sales_history.map(s => `<tr><td>${escape(s.date)}</td><td class="num">${fmtMoney(s.price)}</td><td>${escape(s.notes||'')}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${r.permits && r.permits.length ? `<div class="ai-section"><h4>Permits</h4><ul>${r.permits.map(p => `<li>${escape(p.date)}: ${escape(p.type)} — ${fmtMoney(p.value)}</li>`).join('')}</ul></div>` : ''}
      ${r.violations_or_liens && r.violations_or_liens.length ? `<div class="ai-section"><h4>⚠ Violations / liens</h4><ul>${r.violations_or_liens.map(v => `<li><strong>${escape(v.type)}</strong> ${escape(v.date||'')} — ${fmtMoney(v.amount)} — ${escape(v.status||'')}</li>`).join('')}</ul></div>` : ''}
      ${r.foreclosure_status && r.foreclosure_status !== "None" ? `<p><span class="pill red">${escape(r.foreclosure_status)}</span></p>` : ''}
      ${r.title_concerns && r.title_concerns.length ? `<div class="ai-section"><h4>Title concerns</h4><ul>${r.title_concerns.map(c => `<li>${escape(c)}</li>`).join('')}</ul></div>` : ''}
      ${r.summary ? `<div class="ai-section"><h4>Summary</h4><p>${escape(r.summary)}</p></div>` : ''}
    `,

    risks: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">Flood</div><div class="val" style="font-size:13px;">${escape(r.flood?.zone || '?')}</div></div>
        <div class="ai-mini-stat"><div class="lbl">Fire risk</div><div class="val" style="font-size:13px;">${escape(r.fire_risk?.level || '?')}</div></div>
        <div class="ai-mini-stat"><div class="lbl">Termite</div><div class="val" style="font-size:13px;">${escape(r.termite_zone || '?')}</div></div>
      </div>
      ${r.hurricane?.applies ? `<p><strong>Hurricane:</strong> ${escape(r.hurricane.level)} — ${escape(r.hurricane.notes || '')}</p>` : ''}
      ${r.earthquake?.applies ? `<p><strong>Earthquake:</strong> ${escape(r.earthquake.level)} — ${escape(r.earthquake.notes || '')}</p>` : ''}
      ${r.environmental?.superfund_within_1mi ? `<p><span class="pill red">⚠ Superfund site within 1 mi</span> ${escape(r.environmental.notes || '')}</p>` : ''}
      ${r.structural_age_concerns && r.structural_age_concerns.length ? `<div class="ai-section"><h4>Structural concerns</h4><ul>${r.structural_age_concerns.map(c => `<li>${escape(c)}</li>`).join('')}</ul></div>` : ''}
      ${r.summary ? `<div class="ai-section"><h4>Summary</h4><p>${escape(r.summary)}</p></div>` : ''}
      ${r.deal_breakers && r.deal_breakers.length ? `<div class="ai-section"><h4>🚨 Deal breakers</h4><ul style="color:var(--red);">${r.deal_breakers.map(d => `<li>${escape(d)}</li>`).join('')}</ul></div>` : ''}
    `,

    photos: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">Overall condition</div><div class="val">${r.overall_condition_score || '?'}/10</div></div>
        <div class="ai-mini-stat"><div class="lbl">Rehab scope</div><div class="val" style="font-size:14px;">${escape(r.rehab_complexity || '?')}</div></div>
        <div class="ai-mini-stat"><div class="lbl">Est. rehab</div><div class="val">${fmtMoney(r.estimated_rehab_range?.low)}-${fmtMoney(r.estimated_rehab_range?.high)}</div></div>
      </div>
      ${r.rooms_observed && r.rooms_observed.length ? `<div class="ai-section"><h4>Rooms observed</h4>
        <table class="ai-line-items"><thead><tr><th>Room</th><th>Condition</th><th>Rehab needed</th></tr></thead>
        <tbody>${r.rooms_observed.map(rm => `<tr><td><strong>${escape(rm.room)}</strong></td><td>${rm.condition}/10</td><td class="muted" style="font-size:11.5px;">${(rm.rehab_needed||[]).map(escape).join('; ')}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${r.exterior ? `<div class="ai-section"><h4>Exterior (${r.exterior.condition}/10)</h4><p>${(r.exterior.rehab_needed||[]).map(escape).join('; ')}</p></div>` : ''}
      ${r.hidden_concerns && r.hidden_concerns.length ? `<div class="ai-section"><h4>⚠ Hidden concerns</h4><ul>${r.hidden_concerns.map(c => `<li>${escape(c)}</li>`).join('')}</ul></div>` : ''}
      ${r.scope_summary ? `<div class="ai-section"><h4>Scope summary</h4><p>${escape(r.scope_summary)}</p></div>` : ''}
    `,

    verdict: (r) => `
      <p style="text-align:center; font-size:24px; font-weight:700; color:${r.verdict?.includes('BUY')?'var(--green)':r.verdict==='PASS'||r.verdict==='AVOID'?'var(--red)':'var(--orange)'}">${escape(r.verdict || '?')}</p>
      <p class="muted" style="text-align:center;">(Detailed verdict in the hero card above)</p>
    `,

    red_flags: (r) => `
      <p>Overall risk grade: <strong style="color:${r.overall_risk_grade==='A'?'var(--green)':r.overall_risk_grade==='F'?'var(--red)':'var(--text)'}; font-size:18px;">${escape(r.overall_risk_grade || '?')}</strong></p>
      ${r.red_flags && r.red_flags.length ? `<div class="ai-section"><h4>Red flags (${r.red_flags.length})</h4>
        ${r.red_flags.map(f => `<div style="padding:8px 10px; margin:6px 0; background:${f.severity==='Critical'||f.severity==='High'?'var(--red-light)':f.severity==='Medium'?'var(--yellow-light)':'var(--bg-2)'}; border-radius:8px;">
          <strong>${escape(f.flag)}</strong> <span class="pill ${f.severity==='Critical'?'red':f.severity==='High'?'orange':f.severity==='Medium'?'yellow':'gray'}">${escape(f.severity)}</span>
          <div style="font-size:11.5px; margin-top:4px;"><strong>Evidence:</strong> ${escape(f.evidence)}</div>
          <div style="font-size:11.5px; margin-top:2px;"><strong>Verify:</strong> ${escape(f.mitigation)}</div>
        </div>`).join('')}</div>` : '<p>No red flags detected.</p>'}
      ${r.deal_breakers && r.deal_breakers.length ? `<div class="ai-section"><h4>🚨 Deal breakers</h4><ul style="color:var(--red);">${r.deal_breakers.map(d => `<li>${escape(d)}</li>`).join('')}</ul></div>` : ''}
      ${r.summary ? `<p>${escape(r.summary)}</p>` : ''}
    `,

    offer: (r) => `
      <div class="ai-stat-row">
        <div class="ai-mini-stat" style="background:var(--accent-light)"><div class="lbl">Suggested offer</div><div class="val" style="color:var(--accent-dark)">${fmtMoney(r.suggested_initial_offer)}</div></div>
        <div class="ai-mini-stat"><div class="lbl">Max walk-away</div><div class="val">${fmtMoney(r.max_walk_away_price)}</div></div>
        <div class="ai-mini-stat"><div class="lbl">Acceptance odds</div><div class="val">${r.estimated_seller_acceptance_pct || '?'}%</div></div>
      </div>
      <div class="ai-section"><h4>Strategy</h4><p>${escape(r.negotiation_strategy || '')}</p></div>
      ${r.terms_to_request ? `<div class="ai-section"><h4>Terms to request</h4><ul>${r.terms_to_request.map(t => `<li>${escape(t)}</li>`).join('')}</ul></div>` : ''}
      ${r.concessions_to_offer ? `<div class="ai-section"><h4>Concessions you can offer</h4><ul>${r.concessions_to_offer.map(c => `<li>${escape(c)}</li>`).join('')}</ul></div>` : ''}
      ${r.rationale ? `<div class="ai-section"><h4>Rationale</h4><p>${escape(r.rationale)}</p></div>` : ''}
    `,

    timing: (r) => `
      <p><span class="pill ${r.current_market_phase?.includes('Hot')||r.current_market_phase?.includes('Seller')?'green':r.current_market_phase?.includes('Buyer')?'red':'yellow'}">${escape(r.current_market_phase || '?')}</span>
      ${r.seasonal_premium_pct ? ` <span class="muted">+${r.seasonal_premium_pct}% seasonal premium</span>` : ''}</p>
      <div class="ai-stat-row">
        <div class="ai-mini-stat"><div class="lbl">DOM now</div><div class="val">${r.expected_dom_now || '?'} days</div></div>
        <div class="ai-mini-stat" style="background:var(--accent-light)"><div class="lbl">DOM best season</div><div class="val" style="color:var(--accent-dark)">${r.expected_dom_best_season || '?'} days</div></div>
      </div>
      <div class="ai-section"><h4>Best months</h4><p>${(r.best_listing_months||[]).map(escape).join(', ')}</p></div>
      <div class="ai-section"><h4>Worst months</h4><p>${(r.worst_listing_months||[]).map(escape).join(', ')}</p></div>
      ${r.recommendation ? `<div class="ai-section"><h4>Recommendation</h4><p>${escape(r.recommendation)}</p></div>` : ''}
    `,

    mls_listing: (r) => {
      const personas = Object.keys(r.descriptions || {});
      const id = "mls-" + Math.random().toString(36).slice(2);
      return `
        ${r.headline_options ? `<div class="ai-section"><h4>Headline options</h4><ul>${r.headline_options.map(h => `<li>${escape(h)}</li>`).join('')}</ul></div>` : ''}
        <div class="persona-tab-bar" id="${id}-tabs">
          ${personas.map((p, i) => `<div class="persona-tab ${i===0?'active':''}" data-tab="${p}">${escape(p.replace('_', ' '))}</div>`).join('')}
        </div>
        <div id="${id}-content" style="font-size:13.5px; line-height:1.7; white-space:pre-wrap;">${escape(r.descriptions[personas[0]] || '')}</div>
        <script>(function(){
          const tabs = document.querySelectorAll('#${id}-tabs .persona-tab');
          tabs.forEach(t => t.addEventListener('click', e => {
            tabs.forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            document.getElementById('${id}-content').textContent = ${JSON.stringify(r.descriptions)}[t.dataset.tab];
          }));
        })();</script>
      `;
    },

    offer_letter: (r) => `
      ${r.tone_used ? `<span class="pill blue">Tone: ${escape(r.tone_used)}</span>` : ''}
      <div class="ai-section"><h4>Letter</h4><p style="white-space:pre-wrap; font-family:Georgia, serif; padding:12px; background:var(--bg-2); border-radius:8px;">${escape(r.letter || '')}</p></div>
      ${r.terms_summary ? `<div class="ai-section"><h4>Terms summary</h4><ul>${r.terms_summary.map(t => `<li>${escape(t)}</li>`).join('')}</ul></div>` : ''}
    `,

    marketing: (r) => `
      <div class="ai-section"><h4>📱 Instagram</h4><p style="white-space:pre-wrap;">${escape(r.instagram_post || '')}</p></div>
      <div class="ai-section"><h4>👍 Facebook</h4><p style="white-space:pre-wrap;">${escape(r.facebook_post || '')}</p></div>
      <div class="ai-section"><h4>🐦 Twitter/X</h4><p>${escape(r.twitter_post || '')}</p></div>
      ${r.flyer ? `<div class="ai-section"><h4>📄 Flyer</h4>
        <p><strong style="font-size:16px;">${escape(r.flyer.headline)}</strong></p>
        <p><em>${escape(r.flyer.tagline)}</em></p>
        <ul>${(r.flyer.bullets||[]).map(b => `<li>${escape(b)}</li>`).join('')}</ul>
        <p><strong>CTA:</strong> ${escape(r.flyer.cta)}</p></div>` : ''}
      ${r.email_subject_lines ? `<div class="ai-section"><h4>✉️ Email subjects</h4><ul>${r.email_subject_lines.map(s => `<li>${escape(s)}</li>`).join('')}</ul></div>` : ''}
    `,
  };

  function renderVerdictHero(insight) {
    const r = insight.result;
    if (!r || !r.verdict) { $("#verdict-hero").style.display = "none"; return; }
    const v = r.verdict.toUpperCase();
    let cls = "verdict-buy";
    if (v.includes("PASS") || v.includes("AVOID")) cls = "verdict-pass";
    else if (v.includes("NEGOTIATE") || v.includes("AFTER")) cls = "verdict-negotiate";

    const el = $("#verdict-hero");
    el.style.display = "block";
    el.innerHTML = `<div class="verdict-hero ${cls}">
      <div class="verdict-hero-top">
        <div>
          <div class="verdict-label">AI Verdict <span class="muted" style="margin-left:8px;">${escape(insight.model || '')}</span></div>
          <div class="verdict-decision">${escape(r.verdict)}</div>
        </div>
        <span class="pill ${r.confidence === 'High' ? 'green' : r.confidence === 'Low' ? 'red' : 'yellow'}">${escape(r.confidence || '')} confidence</span>
      </div>
      <div class="verdict-stats">
        <div class="verdict-stat"><span class="lbl">Target offer</span><span class="val">${fmtMoney(r.target_offer_price)}</span></div>
        <div class="verdict-stat"><span class="lbl">Max offer</span><span class="val">${fmtMoney(r.max_offer_price)}</span></div>
        <div class="verdict-stat"><span class="lbl">Expected profit</span><span class="val" style="color:${r.expected_profit > 0 ? 'var(--green)' : 'var(--red)'}">${fmtMoney(r.expected_profit, true)}</span></div>
        <div class="verdict-stat"><span class="lbl">Expected ROI</span><span class="val">${r.expected_roi_pct || '?'}%</span></div>
      </div>
      <div class="verdict-reasons">
        <div class="reasons-buy">
          <h4>✓ Reasons to buy</h4>
          <ul>${(r.top_3_reasons_buy||[]).map(x => `<li>${escape(x)}</li>`).join('')}</ul>
        </div>
        <div class="reasons-pass">
          <h4>✗ Concerns</h4>
          <ul>${(r.top_3_reasons_pass||[]).map(x => `<li>${escape(x)}</li>`).join('')}</ul>
        </div>
      </div>
      ${r.deal_summary ? `<div class="verdict-summary">${escape(r.deal_summary)}</div>` : ''}
    </div>`;
  }

  // Run full analysis = parallel research tier + verdict (one backend call)
  $("#ai-run-research")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    if (!confirm(`Full AI analysis — the flip essentials:\nARV, Rehab, History (title/liens), Risks + Photos, Red flags, Verdict.\n~10-20 s · cost ~$0.80-$1.30 (½ vs before).\n\nRent comps / Neighborhood / Taxes are no longer included by default — run them from their own tab if needed.`)) return;
    const btn = $("#ai-run-research");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Parallel analysis in progress…`;
    try {
      const r = await API.aiRunAll(state.currentDealId);
      const ok = Object.values(r.results || {}).filter(x => x.ok).length;
      const tot = Object.keys(r.results || {}).length;
      toast(`✓ Full analysis: ${ok}/${tot} research tasks + verdict · ${r.total_web_searches || 0} web searches`, "success");
      // Refresh the deal detail so all new insights + verdict show.
      if (typeof openDeal === "function") await openDeal(state.currentDealId);
    } catch (e) {
      toast("Analysis failed: " + e.message, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke-linejoin="round"/></svg> Run full analysis`;
    }
  });

  $("#ai-clear-all")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    if (!confirm("Clear all AI insights on this deal?")) return;
    try {
      const fresh = await API.getDeal(state.currentDealId);
      const insights = fresh.deal.ai_insights || {};
      for (const name of Object.keys(insights)) {
        await API.aiClearInsight(state.currentDealId, name);
      }
      toast("AI insights cleared", "success");
      const d2 = await API.getDeal(state.currentDealId);
      renderAiPanel(d2.deal);
    } catch (e) { toast(e.message, "error"); }
  });

  // ============== LIGHTBOX (swipeable gallery) ==============
  let lightboxState = { images: [], idx: 0 };
  // Accepts either a single URL (back-compat) or an array of URLs + a start index.
  function openLightbox(images, startIdx = 0) {
    const arr = (Array.isArray(images) ? images : [images]).filter(Boolean);
    if (!arr.length) return;
    lightboxState.images = arr;
    lightboxState.idx = Math.max(0, Math.min(arr.length - 1, startIdx | 0));
    lightboxShow();
    $("#lightbox").style.display = "flex";
  }
  function lightboxShow() {
    const { images, idx } = lightboxState;
    $("#lightbox-img").src = images[idx] || "";
    const multi = images.length > 1;
    const prev = $("#lightbox-prev"), next = $("#lightbox-next"), c = $("#lightbox-counter");
    if (prev) prev.style.display = multi ? "flex" : "none";
    if (next) next.style.display = multi ? "flex" : "none";
    if (c) { c.style.display = multi ? "block" : "none"; c.textContent = `${idx + 1} / ${images.length}`; }
  }
  function lightboxGo(delta) {
    const n = lightboxState.images.length; if (n < 2) return;
    lightboxState.idx = (lightboxState.idx + delta + n) % n;   // wrap-around
    lightboxShow();
  }
  function closeLightbox() { $("#lightbox").style.display = "none"; }

  $("#lightbox-close")?.addEventListener("click", (e) => { e.stopPropagation(); closeLightbox(); });
  $("#lightbox-prev")?.addEventListener("click", (e) => { e.stopPropagation(); lightboxGo(-1); });
  $("#lightbox-next")?.addEventListener("click", (e) => { e.stopPropagation(); lightboxGo(1); });
  // Backdrop click closes; clicks on the image/arrows do not (unless it was a drag-swipe).
  (() => {
    const lb = $("#lightbox"); if (!lb) return;
    let x0 = null, y0 = null, dragged = false;
    const down = (x, y) => { x0 = x; y0 = y; dragged = false; };
    const up = (x, y) => {
      if (x0 == null) return;
      const dx = x - x0, dy = y - y0;
      if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy)) { lightboxGo(dx < 0 ? 1 : -1); dragged = true; }
      x0 = y0 = null;
    };
    lb.addEventListener("touchstart", (e) => { const t = e.changedTouches[0]; down(t.clientX, t.clientY); }, { passive: true });
    lb.addEventListener("touchend", (e) => { const t = e.changedTouches[0]; up(t.clientX, t.clientY); }, { passive: true });
    // Desktop mouse drag-to-swipe
    lb.addEventListener("mousedown", (e) => down(e.clientX, e.clientY));
    lb.addEventListener("mouseup", (e) => up(e.clientX, e.clientY));
    lb.addEventListener("click", (e) => {
      if (dragged) { dragged = false; return; }        // a swipe, not a tap
      if (e.target === lb) closeLightbox();             // only the backdrop closes
    });
  })();

  $("#back-btn")?.addEventListener("click", () => showView("deals"));

  $("#pdf-btn")?.addEventListener("click", () => {
    if (!state.currentDealId) return;
    openPdfModal();
  });

  // Pre-Qualification Letter → generate the PDF (today's date + this deal's address)
  $("#prequal-btn")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    const btn = $("#prequal-btn");
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Generating…';
    try {
      const resp = await fetch(API.prequalLetterUrl(state.currentDealId));
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const addr = (state.deals.find(d => d.id === state.currentDealId)?.address || "property")
        .split(",")[0].replace(/[^\w]+/g, "-");
      await downloadPdfBulletproof(blobUrl, `PreQualification-Letter-${addr}.pdf`);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 4000);
    } catch (e) {
      toast("Letter generation failed: " + e.message, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  });

  $("#edit-btn")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    try {
      const data = await API.getDeal(state.currentDealId);
      fillForm(data.deal);
      showView("add");                         // sets formDealId = null
      state.formDealId = state.currentDealId;  // re-bind: we're EDITING this deal
    } catch (e) { toast(e.message, "error"); }
  });

  $("#refresh-btn")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    const btn = $("#refresh-btn");
    let url = null;
    // If deal has no source_url, prompt user for one
    try {
      const d = await API.getDeal(state.currentDealId);
      if (!d.deal.source_url) {
        url = prompt("Paste the source URL (Zillow / Redfin / ispeedtolead):", "");
        if (!url) return;
      }
    } catch (e) { toast(e.message, "error"); return; }
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Refreshing...';
    try {
      const r = await API.refreshDeal(state.currentDealId, url);
      if (!r.ok) { toast(r.error || "Refresh failed", "error"); return; }
      toast(`Refreshed: ${r.photos} photos, ${r.sale_comps} sale comps, ${r.rent_comps} rent comps`, "success");
      openDeal(state.currentDealId);
    } catch (e) { toast(e.message, "error"); }
    finally {
      btn.disabled = false;
      btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" stroke-linecap="round" stroke-linejoin="round"/></svg> Refresh`;
    }
  });

  $("#delete-btn")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    if (!confirm("Delete this deal? This cannot be undone.")) return;
    try {
      await API.deleteDeal(state.currentDealId);
      toast("Deal deleted", "success");
      state.currentDealId = null;
      showView("deals");
    } catch (e) { toast(e.message, "error"); }
  });

  // ============== SEARCH (sourcing) ==============
  let _searchListings = [];
  const _SEARCH_SEEN = "fb-seen-listings", _SEARCH_SAVED = "fb-saved-searches";
  const _ls = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
  const _lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };
  const _seenSet = () => new Set(_ls(_SEARCH_SEEN, []));
  const _seenKey = l => ((l.address || "") + "|" + (l.city || "")).toLowerCase().trim();
  const _onBoard = a => (state.deals || []).some(d =>
    (d.address || "").toLowerCase().split(",")[0].trim() === (a || "").toLowerCase().split(",")[0].trim());

  function _searchFin(l) {
    const num = v => (v === 0 || v) ? Number(v) : null;
    const price = num(l.price), arv = num(l.arv_estimate), rehab = num(l.rehab_estimate) ?? 0;
    let profit = null, margin = null;
    if (arv != null && price != null) { profit = arv - price - rehab - arv * 0.08; margin = arv > 0 ? profit / arv * 100 : null; }
    return { price, arv, rehab, profit, margin };
  }
  function _gatherSearch() {
    const input = ($("#search-input").value || "").trim();
    const p = { max_listings: Number($("#search-count").value) || 10 };
    if (/zillow\.com/i.test(input)) p.url = input; else p.location = input;
    const n = id => { const v = Number($(id)?.value); return v > 0 ? v : null; };
    if (n("#search-pricemin")) p.price_min = n("#search-pricemin");
    if (n("#search-pricemax")) p.price_max = n("#search-pricemax");
    if (n("#search-bedsmin")) p.beds_min = n("#search-bedsmin");
    if (n("#search-bathsmin")) p.baths_min = n("#search-bathsmin");
    if (n("#search-sqftmin")) p.sqft_min = n("#search-sqftmin");
    const t = $("#search-type")?.value; if (t) p.property_type = t;
    return { input, payload: p };
  }

  async function runSearch() {
    const { input, payload } = _gatherSearch();
    if (!input) { toast("Enter a Zillow search URL or a city", "warn"); return; }
    const st = $("#search-status"), btn = $("#search-run");
    btn.disabled = true;
    st.innerHTML = '<span class="spinner"></span> AI + web search in progress… (~30-60s)';
    $("#search-results").innerHTML = ""; $("#search-controls").style.display = "none";
    try {
      if (!state.deals || !state.deals.length) { try { state.deals = await API.listDeals(); } catch {} }
      const res = await API.searchListings(payload);
      if (!res.ok) { st.innerHTML = `<span style="color:var(--red)">${escape(res.error || "Search failed")}</span>`; return; }
      _searchListings = res.listings || [];
      _searchAreaLabel = res.area_label || "";
      st.textContent = `✓ ${_searchListings.length} listing(s)${res.area_label ? " — " + res.area_label : ""}`;
      $("#search-controls").style.display = _searchListings.length ? "flex" : "none";
      renderSearchResults();
    } catch (e) { st.innerHTML = `<span style="color:var(--red)">${escape(e.message)}</span>`; }
    finally { btn.disabled = false; }
  }
  let _searchAreaLabel = "";

  function renderSearchResults() {
    const box = $("#search-results");
    if (!_searchListings.length) { box.innerHTML = `<div class="card"><p class="muted">No listings found.</p></div>`; return; }
    const seen = _seenSet();
    const newOnly = $("#search-newonly")?.checked, hideBoard = $("#search-hideboard")?.checked;
    let list = _searchListings.map((l, i) => ({ l, i, fin: _searchFin(l), isNew: !seen.has(_seenKey(l)), ob: _onBoard(l.address) }));
    if (newOnly) list = list.filter(x => x.isNew);
    if (hideBoard) list = list.filter(x => !x.ob);
    const sort = $("#search-sort")?.value || "profit";
    const cmp = {
      profit: (a, b) => (b.fin.profit ?? -1e12) - (a.fin.profit ?? -1e12),
      price: (a, b) => (a.fin.price ?? 1e12) - (b.fin.price ?? 1e12),
      recent: (a, b) => (a.l.days_on_market ?? 999) - (b.l.days_on_market ?? 999),
      reno: (a, b) => (b.l.last_renovated || 0) - (a.l.last_renovated || 0),
    }[sort];
    if (cmp) list.sort(cmp);
    $("#search-count-label").textContent = `${list.length} shown${_searchAreaLabel ? " · " + _searchAreaLabel : ""}`;
    const yr = new Date().getFullYear();
    box.innerHTML = `<div class="deals-grid">${list.map(({ l, i, fin, isNew, ob }) => {
      const price = fin.price != null ? `$${Math.round(fin.price / 1000)}K` : "—";
      const age = l.year_built ? `${l.year_built} (${yr - l.year_built} yrs)` : "year ?";
      const reno = l.last_renovated ? `reno ${l.last_renovated}` : "reno ?";
      let profitTag = "";
      if (fin.profit != null) {
        const col = fin.margin >= 20 ? "var(--green)" : fin.margin >= 10 ? "#e8a93b" : "var(--red)";
        profitTag = `<div style="margin-top:4px; font-weight:700; color:${col};">▲ $${Math.round(fin.profit / 1000)}K profit${fin.margin != null ? ` · ${Math.round(fin.margin)}%` : ""}</div>`;
      }
      const arvLine = fin.arv != null ? `<span class="muted">ARV ~$${Math.round(fin.arv / 1000)}K · Rehab ~$${Math.round((fin.rehab || 0) / 1000)}K</span>` : "";
      return `<div class="card search-result">
        <div style="display:flex; justify-content:space-between; gap:6px;">
          <div style="font-weight:600;">${escape(l.address || "?")}</div>
          ${isNew ? '<span class="pill green" style="height:fit-content;">NEW</span>' : ""}
        </div>
        <div class="muted" style="font-size:12px;">${escape([l.city, l.state, l.zip].filter(Boolean).join(" "))}</div>
        <div style="margin:7px 0 2px; font-size:13px;"><span class="money">${price}</span> · ${l.beds || "?"}bd / ${l.baths || "?"}ba${l.sqft ? ` · ${l.sqft} sf` : ""}</div>
        <div style="font-size:11.5px;" class="muted">🏗 ${age} · 🔧 ${reno}</div>
        <div style="font-size:12px; margin-top:3px;">${arvLine}</div>
        ${profitTag}
        <div style="display:flex; gap:8px; margin-top:10px;">
          <button class="btn ${ob ? "" : "primary"} search-add" data-i="${i}" ${ob ? "disabled" : ""}>${ob ? "✓ On board" : "+ Add"}</button>
          ${l.url ? `<a class="btn ghost" href="${escape(l.url)}" target="_blank" rel="noopener">Zillow ↗</a>` : ""}
        </div>
      </div>`;
    }).join("")}</div>`;
    box.querySelectorAll(".search-add").forEach(b => b.addEventListener("click", () => {
      if (!b.disabled) _insertSearchDeal(_searchListings[+b.dataset.i], b);
    }));
  }

  async function _insertSearchDeal(l, btn) {
    const full = [l.address, l.city, `${l.state || ""} ${l.zip || ""}`.trim()].filter(s => s && s.trim()).join(", ") || l.address || "";
    const zl = l.url || ("https://www.zillow.com/homes/" + encodeURIComponent(full) + "_rb/");
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await API.createDeal({
        address: full, city: l.city || "", state: l.state || "", zip: String(l.zip || ""),
        property_type: l.property_type || "Single Family Residence",
        beds: l.beds || null, baths: l.baths || null, sqft: l.sqft || null,
        year_built: l.year_built || null,
        purchase_price: l.price || 0,
        arv_base: l.arv_estimate || 0, rehab_base: l.rehab_estimate || 0,
        arv_confidence: "Low",
        source: "search", source_url: zl, status: "evaluating",
        notes: `[Imported via Search]${l.last_renovated ? "\nLast renovation: " + l.last_renovated : ""}\n${zl}`,
      });
      const deal = r.deal || r;
      (state.deals = state.deals || []).push(deal);
      btn.textContent = "✓ Added"; btn.classList.add("added"); btn.classList.remove("primary");
      if ($("#search-analyze")?.checked) {
        btn.textContent = "🤖 analyzing…";
        try {
          const rr = await API.rehabEstimate(deal.id);
          if (rr.ok && rr.items?.length) {
            const total = Math.round(rr.items.reduce((s, it) => s + (Number(it.cost) || 0), 0) * 1.15);
            await API.patchDeal(deal.id, { rehab_base: total, rehab_items: rr.items, rehab_contingency_pct: 15 });
          }
        } catch {}
        btn.textContent = "✓ Added + analyzed";
      }
    } catch (e) { btn.disabled = false; btn.textContent = "+ Add"; toast(e.message, "error"); }
  }

  // Saved searches (localStorage)
  function _renderSavedSearches() {
    const box = $("#search-saved"); if (!box) return;
    const saved = _ls(_SEARCH_SAVED, []);
    box.innerHTML = saved.length
      ? `<span class="muted" style="font-size:11px; align-self:center;">★ Saved:</span>` + saved.map((s, i) =>
          `<span class="pill gray" style="cursor:pointer;"><span data-saved="${i}">${escape(s.label || s.input)}</span> <span data-saved-del="${i}" style="cursor:pointer; opacity:.6;">×</span></span>`).join("")
      : "";
    box.querySelectorAll("[data-saved]").forEach(el => el.addEventListener("click", () => {
      const s = _ls(_SEARCH_SAVED, [])[+el.dataset.saved]; if (!s) return;
      $("#search-input").value = s.input || ""; $("#search-pricemin").value = s.price_min || "";
      $("#search-pricemax").value = s.price_max || ""; $("#search-bedsmin").value = s.beds_min || "";
      $("#search-bathsmin").value = s.baths_min || ""; $("#search-sqftmin").value = s.sqft_min || "";
      $("#search-type").value = s.property_type || ""; $("#search-count").value = s.max_listings || 10;
      runSearch();
    }));
    box.querySelectorAll("[data-saved-del]").forEach(el => el.addEventListener("click", e => {
      e.stopPropagation();
      const saved = _ls(_SEARCH_SAVED, []); saved.splice(+el.dataset.savedDel, 1); _lsSet(_SEARCH_SAVED, saved); _renderSavedSearches();
    }));
  }

  $("#search-run")?.addEventListener("click", runSearch);
  $("#search-input")?.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); runSearch(); } });
  $("#search-sort")?.addEventListener("change", renderSearchResults);
  $("#search-newonly")?.addEventListener("change", renderSearchResults);
  $("#search-hideboard")?.addEventListener("change", renderSearchResults);
  $("#search-mark-seen")?.addEventListener("click", () => {
    const seen = _seenSet(); _searchListings.forEach(l => seen.add(_seenKey(l)));
    _lsSet(_SEARCH_SEEN, [...seen].slice(-3000)); renderSearchResults(); toast("Marked as seen", "success");
  });
  $("#search-add-all")?.addEventListener("click", async () => {
    for (const b of [...$("#search-results").querySelectorAll(".search-add")].filter(x => !x.disabled)) {
      await _insertSearchDeal(_searchListings[+b.dataset.i], b);
    }
    toast("Listings added to board", "success");
  });
  $("#search-save")?.addEventListener("click", () => {
    const { input, payload } = _gatherSearch();
    if (!input) { toast("Nothing to save", "warn"); return; }
    const label = prompt("Search name:", input.length > 30 ? input.slice(0, 30) + "…" : input);
    if (!label) return;
    const saved = _ls(_SEARCH_SAVED, []); saved.push({ label, input, ...payload }); _lsSet(_SEARCH_SAVED, saved);
    _renderSavedSearches(); toast("Search saved", "success");
  });
  _renderSavedSearches();

  // --- Search → Watch fusion: turns the current criteria into a permanent watch ---
  $("#search-to-watch")?.addEventListener("click", async () => {
    const { input, payload } = _gatherSearch();
    if (!input) { toast("First enter a city or a search URL", "warn"); return; }
    const p = { max_listings: payload.max_listings || 15,
                interval_min: Number($("#search-watch-interval")?.value ?? 1440) };
    if (payload.url) p.url = payload.url; else p.location = payload.location;
    if (payload.price_max) p.price_max = payload.price_max;
    if (payload.price_min) p.price_min = payload.price_min;
    if (payload.beds_min) p.beds_min = payload.beds_min;
    if (payload.property_type) p.property_type = payload.property_type;
    const btn = $("#search-to-watch"); btn.disabled = true;
    const st = $("#search-status");
    if (st) st.innerHTML = '<span class="spinner"></span> Creating watch…';
    try {
      const w = await API.watchCreate(p);
      if (st) st.textContent = "✓ Watch created — first run in progress…";
      renderSearchWatches();
      try { await API.watchRun(w.id); } catch {}
      if (st) st.textContent = "✓ Watch active. New listings will arrive analyzed in the feed.";
      renderSearchWatches();
      toast("🔭 Watch created", "success");
    } catch (e) { toast(e.message, "error"); if (st) st.textContent = ""; }
    finally { btn.disabled = false; }
  });

  // Compact active-watches panel shown at the top of the Search view.
  async function renderSearchWatches() {
    const box = $("#search-watches"); if (!box) return;
    let watches = [];
    try { watches = await API.watchesList(); } catch { box.style.display = "none"; return; }
    if (!watches.length) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "block";
    const fmtAgo = ts => {
      if (!ts) return "never run";
      const mins = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 60000));
      if (mins < 60) return `${mins} min ago`;
      const h = Math.round(mins / 60); return h < 24 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
    };
    const fmtFreq = m => m >= 1440 ? "1×/day" : (m >= 60 ? "every " + Math.round(m / 60) + " h" : "every " + m + " min");
    box.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px;">
        <strong style="font-size:14px;">🔭 Active watches (${watches.length})</strong>
        <button class="btn ghost" id="search-manage-watches" style="font-size:12px;">⚙️ Manage</button>
      </div>
      <div style="display:flex; flex-direction:column; gap:6px;">
        ${watches.map(w => {
          const crit = w.label || w.location || "Watch";
          const nb = w.tracked || 0;
          return `<div style="display:flex; align-items:center; gap:10px; font-size:13px; flex-wrap:wrap;">
            <span style="font-weight:600;">${escape(crit.length > 40 ? crit.slice(0, 40) + "…" : crit)}</span>
            <span class="muted" style="font-size:12px;">· ${fmtFreq(w.interval_min || 60)} · ${fmtAgo(w.last_run)}</span>
            ${nb ? `<span class="pill green" style="font-size:11px;">${nb} tracked</span>` : ""}
            <button class="btn ghost sw-run" data-id="${escape(w.id)}" style="font-size:11px; margin-left:auto;">↻ Run</button>
            <button class="btn ghost sw-del" data-id="${escape(w.id)}" style="font-size:11px;">🗑</button>
          </div>`;
        }).join("")}
      </div>`;
    box.querySelector("#search-manage-watches")?.addEventListener("click", () => showView("watch"));
    box.querySelectorAll(".sw-run").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "…";
      try { await API.watchRun(b.dataset.id); toast("Watch re-run", "success"); renderSearchWatches(); }
      catch (e) { toast(e.message, "error"); b.disabled = false; b.textContent = "↻ Run"; }
    }));
    box.querySelectorAll(".sw-del").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Delete this watch?")) return;
      try { await API.watchDelete(b.dataset.id); renderSearchWatches(); } catch (e) { toast(e.message, "error"); }
    }));
  }
  window._renderSearchWatches = renderSearchWatches;

  // Batch importer reachable from the Add view (declutters the sub-tabs).
  $("#add-open-batch")?.addEventListener("click", (e) => { e.preventDefault(); showView("batch"); });

  // ============== ZILLOW WATCH ==============
  async function refreshWatchView() {
    const box = $("#watch-list"); if (!box) return;
    let watches = [];
    try { watches = await API.watchesList(); }
    catch (e) { box.innerHTML = `<div class="card">${escape(e.message)}</div>`; return; }
    if (!state.deals || !state.deals.length) { try { state.deals = await API.listDeals(); } catch {} }
    if (!watches.length) {
      box.innerHTML = `<div class="card"><p class="muted" style="margin:0;">No watches. Create your first one above — e.g. “Cleveland, OH” under $120K.</p></div>`;
      return;
    }
    const D = v => (v == null || v === "") ? "—" : "$" + Number(v).toLocaleString("en-US");
    const ago = ts => {
      if (!ts) return "never";
      const h = Math.round((Date.now() - new Date(ts).getTime()) / 3600000);
      return h < 1 ? "<1 h ago" : h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
    };
    const evBadge = {
      new: '<span class="pill green">🆕 NEW</span>',
      price_drop: '<span class="pill yellow">📉 PRICE DROP</span>',
      gone: '<span class="pill gray">❌ GONE</span>',
    };
    box.innerHTML = watches.map(w => {
      const crit = [w.price_max ? "< " + D(w.price_max) : "", w.beds_min ? w.beds_min + "bd+" : "",
                    w.property_type || ""].filter(Boolean).join(" · ");
      const events = (w.events || []).slice(0, 25).map((e, i) => {
        const price = e.type === "price_drop"
          ? `<s class="muted">${D(e.old_price)}</s> → <strong>${D(e.price)}</strong> <span style="color:var(--green); font-weight:700;">(−${D(e.drop)})</span>`
          : D(e.price);
        const ob = _onBoard(e.address);
        return `<div class="watch-event">
          ${evBadge[e.type] || ""}
          <div class="watch-event-main">
            <div class="watch-event-title">${escape(e.address || "?")}</div>
            <div class="watch-event-sub">${price}${e.beds ? ` · ${e.beds}bd${e.sqft ? " · " + e.sqft + " sf" : ""}` : ""} · ${escape(String(e.ts || "").slice(0, 10))}</div>
          </div>
          ${e.url ? `<a class="btn ghost" href="${escape(e.url)}" target="_blank" rel="noopener" style="font-size:11px;">Zillow ↗</a>` : ""}
          ${e.type !== "gone" ? `<button class="btn ${ob ? "" : "ghost"} watch-add" data-w="${escape(w.id)}" data-i="${i}" ${ob ? "disabled" : ""} style="font-size:11px;">${ob ? "✓ Board" : "+ Board"}</button>` : ""}
        </div>`;
      }).join("");
      const iv = Number(w.interval_min ?? 60);
      const ivLabel = { 0: "manual", 60: "⏱ every hour", 180: "⏱ every 3 h", 360: "⏱ every 6 h", 1440: "⏱ 1×/day" }[iv] || `⏱ every ${iv} min`;
      let nextTxt = "";
      if (iv > 0 && w.last_run) {
        const next = new Date(new Date(w.last_run).getTime() + iv * 60000);
        nextTxt = next > new Date() ? ` · next ~${next.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}` : " · imminent";
      }
      return `<div class="card" style="margin-bottom:14px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
          <div>
            <div style="font-weight:700; font-size:15px;">🔭 ${escape(w.label || w.location)}</div>
            <div class="muted" style="font-size:12px;">${escape(crit)}${crit ? " · " : ""}${w.tracked} property(ies) tracked · updated ${ago(w.last_run)} · ${w.run_count} run(s)</div>
            <div style="font-size:12px; font-weight:600; color:${iv > 0 ? "var(--green)" : "var(--muted)"};">${ivLabel}${nextTxt}</div>
          </div>
          <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
            <select class="watch-interval-sel" data-id="${escape(w.id)}" title="Automatic frequency" style="font-size:12px; padding:5px 6px;">
              <option value="60" ${iv === 60 ? "selected" : ""}>1 h</option>
              <option value="180" ${iv === 180 ? "selected" : ""}>3 h</option>
              <option value="360" ${iv === 360 ? "selected" : ""}>6 h</option>
              <option value="1440" ${iv === 1440 ? "selected" : ""}>24 h</option>
              <option value="0" ${iv === 0 ? "selected" : ""}>Manual</option>
            </select>
            <button class="btn primary watch-run" data-id="${escape(w.id)}" style="font-size:12px;">🔄 Refresh</button>
            <button class="btn ghost watch-del" data-id="${escape(w.id)}" style="font-size:12px;">🗑</button>
          </div>
        </div>
        <div style="margin-top:10px;">${events || '<p class="muted" style="font-size:13px;">No events yet — click “Refresh” for the first run.</p>'}</div>
      </div>`;
    }).join("");

    box.querySelectorAll(".watch-run").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.innerHTML = '<span class="spinner"></span> AI…';
      try {
        const r = await API.watchRun(b.dataset.id);
        if (!r.ok) { toast(r.error || "Failed", "error"); }
        else toast(`✓ ${r.found} listing(s) — ${r.new} new, ${r.price_drops} price drop(s)${r.gone ? ", " + r.gone + " gone" : ""}`, "success");
        refreshWatchView();
      } catch (e) { toast(e.message, "error"); b.disabled = false; b.textContent = "🔄 Refresh"; }
    }));
    box.querySelectorAll(".watch-del").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Delete this watch (and its history)?")) return;
      try { await API.watchDelete(b.dataset.id); refreshWatchView(); } catch (e) { toast(e.message, "error"); }
    }));
    box.querySelectorAll(".watch-interval-sel").forEach(sel => sel.addEventListener("change", async () => {
      const iv = Number(sel.value);
      try {
        await API.watchPatch(sel.dataset.id, { interval_min: iv });
        toast(iv === 0 ? "Watch set to manual" : `Automatic watch: every ${iv >= 60 ? (iv / 60) + " h" : iv + " min"}`, "success");
        refreshWatchView();
      } catch (e) { toast(e.message, "error"); }
    }));
    box.querySelectorAll(".watch-add").forEach(b => b.addEventListener("click", async () => {
      const w = watches.find(x => x.id === b.dataset.w);
      const e = (w?.events || [])[+b.dataset.i];
      if (!e) return;
      b.disabled = true; b.textContent = "…";
      try {
        const full = [e.address, e.city, `${e.state || ""} ${e.zip || ""}`.trim()].filter(s => s && s.trim()).join(", ") || e.address;
        const r = await API.createDeal({
          address: full, city: e.city || "", state: e.state || "", zip: String(e.zip || ""),
          beds: e.beds || null, baths: e.baths || null, sqft: e.sqft || null, year_built: e.year_built || null,
          purchase_price: e.price || 0, arv_base: e.arv_estimate || 0, rehab_base: e.rehab_estimate || 0,
          arv_confidence: "Low", source: "watch", source_url: e.url || "", status: "evaluating",
          notes: `[Zillow watch — ${w.label || w.location}]` + (e.type === "price_drop" ? `\nPrice drop: −$${Number(e.drop).toLocaleString("en-US")}` : ""),
        });
        (state.deals = state.deals || []).push(r.deal || r);
        b.textContent = "✓ Board"; toast("Deal added", "success");
      } catch (err) {
        b.disabled = false; b.textContent = "+ Board";
        toast(err.message, "error");
      }
    }));
  }
  $("#watch-create")?.addEventListener("click", async () => {
    const loc = ($("#watch-location")?.value || "").trim();
    if (!loc) { toast("Enter a city, zip, or state", "warn"); return; }
    const p = { location: loc, max_listings: Number($("#watch-count")?.value) || 15,
                interval_min: Number($("#watch-interval")?.value ?? 60) };
    const n = id => { const v = Number($(id)?.value); return v > 0 ? v : null; };
    if (n("#watch-pricemax")) p.price_max = n("#watch-pricemax");
    if (n("#watch-bedsmin")) p.beds_min = n("#watch-bedsmin");
    if ($("#watch-type")?.value) p.property_type = $("#watch-type").value;
    const st = $("#watch-status");
    try {
      const w = await API.watchCreate(p);
      $("#watch-location").value = "";
      if (st) st.textContent = "✓ Watch created — first run in progress…";
      refreshWatchView();
      // First run right away so the watch starts with a baseline.
      try { await API.watchRun(w.id); } catch {}
      if (st) st.textContent = "✓ Watch created and initialized.";
      refreshWatchView();
    } catch (e) { toast(e.message, "error"); }
  });
  // Kick stale watches (>20 h) in the background at app open — the watch feature.
  setTimeout(() => { API.watchesRunStale?.().catch(() => {}); }, 4000);

  // ============== DEAL RADAR (interesting finds feed) ==============
  async function refreshRadarBadge() {
    try {
      const r = await API.radarList();
      const badge = $("#nav-radar-count");
      if (badge) {
        const n = r.unseen || 0;
        badge.textContent = n ? String(n) : "";
        badge.style.display = n ? "" : "none";
      }
      return r;
    } catch { return null; }
  }

  async function refreshRadarView() {
    const feed = $("#radar-feed"), st = $("#radar-status");
    if (!feed) return;
    renderRadarZones();
    // Paused state — Radar is off in Settings.
    let paused = false;
    try {
      const cfg = await API.aiConfig();
      paused = !!(cfg && cfg.radar_enabled === false);
    } catch {}
    const scanBtn = $("#radar-scan-btn");
    if (scanBtn) scanBtn.disabled = paused;
    if (paused && st) {
      st.innerHTML = '⏸ <strong>Radar is paused.</strong> It won\'t add deals or scan. Turn it back on in <a href="#" id="radar-goto-settings">Settings → Deal Radar</a>.';
      st.querySelector("#radar-goto-settings")?.addEventListener("click", e => { e.preventDefault(); showView("settings"); });
    } else if (st) {
      st.innerHTML = '<span class="spinner"></span> Loading finds…';
    }
    let data;
    try { data = await API.radarList(); }
    catch (e) { if (st && !paused) st.innerHTML = `<span style="color:var(--red)">${escape(e.message)}</span>`; return; }
    let finds = data.finds || [];
    // Interesting (passed all criteria) first, then the rest.
    finds = finds.slice().sort((a, b) => (b.interesting ? 1 : 0) - (a.interesting ? 1 : 0));
    const nInt = finds.filter(f => f.interesting).length;
    if (st && !paused) st.textContent = finds.length
      ? `${finds.length} listing(s) from the last 24h — ${nInt} pass all your criteria.`
      : "";
    if (!finds.length) {
      feed.innerHTML = `<div class="card"><p class="muted" style="margin:0;">No finds yet. <strong>Add a zone above</strong> (e.g. “Cleveland, OH”), then hit <strong>⚡ Scan now</strong> — it lists the homes posted in the last 24 hours and flags the ones that pass your criteria.</p></div>`;
    } else {
      const K = v => (v == null ? "—" : "$" + Math.round(Number(v) / 1000) + "K");
      feed.innerHTML = `<div class="deals-grid">${finds.map(f => {
        const via = escape([f.city, f.state].filter(Boolean).join(", ")) + " · via " + escape(f.watch_label || "watch");
        const zBtn = f.url ? `<a class="btn ghost" href="${escape(f.url)}" target="_blank" rel="noopener" style="font-size:12px;">Zillow ↗</a>` : "";
        const delBtn = `<button class="btn ghost radar-del" data-id="${escape(f.id)}" title="Dismiss" style="margin-left:auto; font-size:12px;">🗑</button>`;
        if (f.interesting) {
          const marginCol = f.margin_pct >= 20 ? "var(--green)" : f.margin_pct >= 12 ? "#e8a93b" : "var(--red)";
          const reasons = (f.reasons || []).map(r => `<span class="radar-reason">${escape(r)}</span>`).join("");
          const dealBtn = f.deal_id ? `<button class="btn primary radar-open" data-deal="${escape(f.deal_id)}" style="font-size:12px;">Open deal →</button>` : "";
          return `<div class="card radar-card${f.seen ? "" : " radar-new"}">
            <div style="display:flex; justify-content:space-between; gap:8px; align-items:flex-start;">
              <div style="font-weight:700;">${escape(f.address || "?")}</div>
              <span class="pill green" style="height:fit-content;">✓ INTERESTING</span>
            </div>
            <div class="muted" style="font-size:12px;">${via}</div>
            <div style="display:flex; gap:14px; margin:9px 0 2px; font-size:13px; flex-wrap:wrap;">
              <span><span class="muted">Price</span> <strong>${K(f.price)}</strong></span>
              <span><span class="muted">ARV</span> <strong>${K(f.arv)}</strong></span>
              <span><span class="muted">Rehab</span> ${K(f.rehab)}</span>
            </div>
            <div style="font-weight:800; color:${marginCol}; margin-top:2px;">▲ ${K(f.profit)} profit · ${Math.round(f.margin_pct)}% margin${f.roi ? ` · ${Math.round(f.roi)}% ROI` : ""} · Risk ${escape(f.risk_grade || "?")}</div>
            <div class="radar-reasons">${reasons}</div>
            <div style="display:flex; gap:8px; margin-top:10px;">${dealBtn}${zBtn}${delBtn}</div>
          </div>`;
        }
        // Non-scored fresh listing — surfaced for review
        const specs = [f.beds ? f.beds + "bd" : "", f.sqft ? Number(f.sqft).toLocaleString("en-US") + " sf" : ""].filter(Boolean).join(" · ");
        const addBtn = f.deal_id
          ? `<button class="btn radar-open" data-deal="${escape(f.deal_id)}" style="font-size:12px;">Open deal →</button>`
          : `<button class="btn primary radar-add" data-id="${escape(f.id)}" style="font-size:12px;">➕ Add to board</button>`;
        return `<div class="card radar-card${f.seen ? "" : " radar-new"}" style="opacity:.94;">
          <div style="display:flex; justify-content:space-between; gap:8px; align-items:flex-start;">
            <div style="font-weight:700;">${escape(f.address || "?")}</div>
            <span class="pill" style="height:fit-content; background:rgba(127,127,127,0.14);">🆕 last 24h</span>
          </div>
          <div class="muted" style="font-size:12px;">${via}</div>
          <div style="margin:8px 0 2px; font-size:13px;"><span class="muted">Price</span> <strong>${K(f.price)}</strong>${specs ? ` · ${escape(specs)}` : ""}</div>
          <div class="muted" style="font-size:12px;">New listing — add it to analyze ARV, profit & risk.</div>
          <div style="display:flex; gap:8px; margin-top:10px;">${addBtn}${zBtn}${delBtn}</div>
        </div>`;
      }).join("")}</div>`;
      feed.querySelectorAll(".radar-open").forEach(b => b.addEventListener("click", () => openDeal(b.dataset.deal)));
      feed.querySelectorAll(".radar-del").forEach(b => b.addEventListener("click", async () => {
        try { await API.radarDelete(b.dataset.id); refreshRadarView(); refreshRadarBadge(); }
        catch (e) { toast(e.message, "error"); }
      }));
      feed.querySelectorAll(".radar-add").forEach(b => b.addEventListener("click", async () => {
        const f = finds.find(x => x.id === b.dataset.id); if (!f) return;
        b.disabled = true; b.textContent = "…";
        try {
          const r = await API.createDeal({
            address: f.address, city: f.city || "", state: f.state || "",
            purchase_price: f.price || 0, source: "radar", source_url: f.url || "",
            status: "evaluating", force_duplicate: true,
            notes: `[Radar — ${f.watch_label || "watch"}]\n${f.url || ""}`,
          });
          const deal = r.deal || r;
          toast("Added to board — analyzing…", "success");
          openDeal(deal.id);
        } catch (e) { toast(e.message, "error"); b.disabled = false; b.textContent = "➕ Add to board"; }
      }));
    }
    // Mark all seen (clears the badge) once viewed.
    try { await API.radarSeen(); } catch {}
    const badge = $("#nav-radar-count");
    if (badge) { badge.textContent = ""; badge.style.display = "none"; }
  }
  $("#radar-refresh-btn")?.addEventListener("click", () => refreshRadarView());
  $("#radar-settings-btn")?.addEventListener("click", () => showView("settings"));

  // ⚡ Scan now — run every watch immediately (real-time import) and stream
  // new finds into the feed as they land.
  let _radarScanning = false;
  async function startRadarScan() {
    if (_radarScanning) return;
    const btn = $("#radar-scan-btn"), st = $("#radar-status");
    let res;
    try { res = await API.radarScan(); }
    catch (e) { toast(e.message, "error"); return; }
    if (!res.ok) { toast(res.error || "Scan failed", "warn"); return; }
    _radarScanning = true;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Scanning…'; }
    const total = res.total || 1;
    if (st) st.innerHTML = `<span class="spinner"></span> Scanning ${total} zone(s) in real time — new deals will appear as they're found…`;
    const t0 = Date.now();
    const poll = async () => {
      let s;
      try { s = await API.radarScanStatus(); } catch { s = null; }
      await refreshRadarView();   // show finds arriving live
      refreshRadarBadge();
      const running = s && s.running;
      const timedOut = Date.now() - t0 > 8 * 60 * 1000;   // safety cap 8 min
      if (running && !timedOut) {
        if (st && s) st.innerHTML = `<span class="spinner"></span> Scanning… ${s.done}/${s.total} zone(s) · ${s.found || 0} listings found · ${s.fresh || 0} in last 24h · ${s.surfaced || 0} shown`;
        setTimeout(poll, 5000);
      } else {
        _radarScanning = false;
        if (btn) { btn.disabled = false; btn.innerHTML = "⚡ Scan now"; }
        const surfaced = s ? (s.surfaced || 0) : 0, added = s ? (s.added || 0) : 0, found = s ? (s.found || 0) : 0;
        const excl = s ? ((s.sold || 0) + (s.old || 0)) : 0;
        const exclTxt = excl ? ` (${s.sold || 0} sold/off-market + ${s.old || 0} older than 24h excluded)` : "";
        if (st) {
          if (s && s.error && !surfaced) {
            st.innerHTML = `<span style="color:var(--red)">Scan issue: ${escape(s.error)}</span>`;
          } else if (surfaced) {
            st.textContent = `✓ Scan done — ${surfaced} active listing(s) from the last 24h${added ? `, ${added} added to your board` : ""}${exclTxt}.`;
          } else if (found) {
            st.textContent = `✓ Scan done — ${found} found, but none were both active and posted in the last 24h${exclTxt}. Try again later or widen the zone.`;
          } else {
            st.textContent = "✓ Scan done — no new listings found in your zone(s) right now.";
          }
        }
        await refreshRadarView();
        if (surfaced) toast(`🎯 ${surfaced} new listing(s) on the radar`, "success");
      }
    };
    setTimeout(poll, 4000);
  }
  $("#radar-scan-btn")?.addEventListener("click", startRadarScan);

  // ----- Watched zones (each zone = a Zillow watch the Radar scans) -----
  async function renderRadarZones() {
    const box = $("#radar-zones"); if (!box) return;
    let zones = [];
    try { zones = await API.watchesList(); } catch { box.innerHTML = ""; return; }
    if (!zones.length) {
      box.innerHTML = `<p class="muted" style="font-size:12px; margin:6px 0 0;">No zones yet — add a city or ZIP above to start.</p>`;
      return;
    }
    const fmtAgo = ts => {
      if (!ts) return "never scanned";
      const mins = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 60000));
      if (mins < 60) return `scanned ${mins} min ago`;
      const h = Math.round(mins / 60); return h < 24 ? `scanned ${h} h ago` : `scanned ${Math.round(h / 24)} d ago`;
    };
    box.innerHTML = zones.map(z => {
      const filters = [z.price_max ? "≤ $" + Number(z.price_max).toLocaleString("en-US") : "",
                       z.beds_min ? z.beds_min + "+ bd" : "",
                       z.property_type || ""].filter(Boolean).join(" · ");
      return `<div style="display:flex; align-items:center; gap:10px; font-size:13px; flex-wrap:wrap; padding:6px 0; border-top:1px solid var(--border);">
        <span style="font-weight:600;">📍 ${escape(z.label || z.location || "?")}</span>
        ${filters ? `<span class="muted" style="font-size:12px;">${escape(filters)}</span>` : ""}
        <span class="muted" style="font-size:12px;">· ${fmtAgo(z.last_run)}${z.tracked ? " · " + z.tracked + " tracked" : ""}</span>
        <button class="btn ghost rz-del" data-id="${escape(z.id)}" title="Remove zone" style="margin-left:auto; font-size:11px;">🗑</button>
      </div>`;
    }).join("");
    box.querySelectorAll(".rz-del").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Remove this zone from the Radar?")) return;
      try { await API.watchDelete(b.dataset.id); renderRadarZones(); } catch (e) { toast(e.message, "error"); }
    }));
  }
  window._renderRadarZones = renderRadarZones;

  $("#radar-zone-add")?.addEventListener("click", async () => {
    const loc = ($("#radar-zone-location")?.value || "").trim();
    if (!loc) { toast("Enter a city, ZIP or state", "warn"); return; }
    const num = id => { const v = Number($(id)?.value); return v > 0 ? v : null; };
    const p = { location: loc, interval_min: 60, max_listings: 15 };
    if (num("#radar-zone-pricemax")) p.price_max = num("#radar-zone-pricemax");
    if (num("#radar-zone-bedsmin")) p.beds_min = num("#radar-zone-bedsmin");
    if ($("#radar-zone-type")?.value) p.property_type = $("#radar-zone-type").value;
    const btn = $("#radar-zone-add"); btn.disabled = true;
    try {
      await API.watchCreate(p);
      $("#radar-zone-location").value = "";
      $("#radar-zone-pricemax").value = ""; $("#radar-zone-bedsmin").value = "";
      toast(`📍 Zone added — scanning ${escape(loc)}…`, "success");
      await renderRadarZones();
      startRadarScan();   // kick a real-time scan right away so finds appear
    } catch (e) { toast(e.message, "error"); }
    finally { btn.disabled = false; }
  });

  // Badge: check on open, then every 5 min.
  setTimeout(refreshRadarBadge, 2500);
  setInterval(refreshRadarBadge, 300000);

  // ===== Auto-update: reload when a new version is deployed (stale-SPA killer) =====
  (() => {
    let bootVersion = null;
    const check = async () => {
      try {
        const r = await fetch("/api/version", { credentials: "include" });
        if (!r.ok) return;
        const v = (await r.json()).version;
        if (!bootVersion) { bootVersion = v; return; }
        if (v && v !== bootVersion) {
          const last = Number(localStorage.getItem("fb-autoreload-ts") || 0);
          if (Date.now() - last > 120000) {   // guard against reload loops
            localStorage.setItem("fb-autoreload-ts", String(Date.now()));
            toast("New version — reloading…", "success");
            setTimeout(() => location.reload(), 800);
          }
        }
      } catch {}
    };
    check();
    window.addEventListener("focus", check);
    setInterval(check, 5 * 60 * 1000);
  })();

  // ============== AUCTION ASSISTANT ==============
  let _aucLast = null;  // last analysis result
  function _gatherAuction() {
    const v = id => ($(id)?.value || "").trim();
    const n = id => { const x = Number(v(id)); return x > 0 ? x : null; };
    const p = { address: v("#auc-address") };
    if (n("#auc-opening")) p.opening_bid = n("#auc-opening");
    if (n("#auc-beds")) p.beds = n("#auc-beds");
    if (n("#auc-baths")) p.baths = n("#auc-baths");
    if (n("#auc-sqft")) p.sqft = n("#auc-sqft");
    if (n("#auc-year")) p.year_built = n("#auc-year");
    if (v("#auc-date")) p.auction_date = v("#auc-date");
    if (Number(v("#auc-margin")) > 0) p.target_margin_pct = Number(v("#auc-margin"));
    if (Number(v("#auc-holding")) >= 0 && v("#auc-holding")) p.holding = Number(v("#auc-holding"));
    if (v("#auc-comments")) p.comments = v("#auc-comments");
    if (n("#auc-arv-override")) p.arv_override = n("#auc-arv-override");
    if (n("#auc-rehab-override")) p.rehab_override = n("#auc-rehab-override");
    if (v("#auc-url")) p.url = v("#auc-url");
    return p;
  }
  // Distinguish a real property address from a bare city ("cleveland ohio").
  // A real address has a street-type word (St/Ave/Rd…) or a leading house
  // number — NOT just digits (a zip would wrongly pass). auction.com often
  // hides the house number, so we must accept "Hood Ave, Cleveland, OH" too.
  function _looksLikeAddress(a) {
    a = (a || "").trim();
    if (a.length < 5) return false;
    const streetWord = /\b(st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|ct|court|pl|place|ter|terrace|cir|circle|hwy|highway|pkwy|parkway|trl|trail|loop|run|path|sq|square|pike|cove|cv|row|walk|xing|crossing|aly|alley|pt|point|ridge|rdg|bend|bnd|hl|hill|hts|heights|plz|plaza)\b/i;
    const leadingNumber = /^\s*\d+\s+\S/;
    const unit = /#\s*\w|\b(unit|apt|ste|suite|lot)\b/i;
    return streetWord.test(a) || leadingNumber.test(a) || unit.test(a);
  }
  async function runAuction(force = false) {
    const payload = _gatherAuction();
    if (force) payload.force = true;
    const st = $("#auc-status"), btn = $("#auc-run");
    if (!payload.address) { toast("First paste the property address", "warn"); return; }
    const hasOverride = payload.arv_override && payload.rehab_override;
    if (!force && !hasOverride && !_looksLikeAddress(payload.address)) {
      st.innerHTML = `<span style="color:#e8a93b">⚠️ “${escape(payload.address)}” looks like a city, not a specific address.</span>`;
      $("#auc-result").innerHTML = `<div class="card" style="border-left:3px solid #e8a93b;">
        <p style="margin:0 0 6px;"><strong>The Auction tool analyzes ONE specific property</strong> — ideally a full address, e.g. <code>3744 W 135th St, Cleveland, OH 44111</code>.</p>
        <p style="margin:0 0 10px;" class="muted">To explore <strong>a whole city</strong>, use the <a href="#" id="auc-goto-search" style="font-weight:600;">🔎 Search</a> module instead.</p>
        <button class="btn" id="auc-force">Analyze this address anyway →</button>
      </div>`;
      $("#auc-goto-search")?.addEventListener("click", (e) => { e.preventDefault(); showView("search"); const si = $("#search-input"); if (si) si.value = payload.address; });
      $("#auc-force")?.addEventListener("click", () => runAuction(true));
      return;
    }
    btn.disabled = true;
    const ai = !(payload.arv_override && payload.rehab_override);
    st.innerHTML = ai ? '<span class="spinner"></span> AI + comps analysis in progress… (~30-60s)' : '<span class="spinner"></span> Calculating…';
    $("#auc-result").innerHTML = "";
    try {
      if (!state.deals || !state.deals.length) { try { state.deals = await API.listDeals(); } catch {} }
      const r = await API.auctionAnalyze(payload);
      if (!r.ok) { st.innerHTML = `<span style="color:var(--red)">${escape(r.error || "Analysis failed")}</span>`; return; }
      _aucLast = r; st.textContent = "✓ Analysis complete";
      renderAuctionResult(r);
    } catch (e) { st.innerHTML = `<span style="color:var(--red)">${escape(e.message)}</span>`; }
    finally { btn.disabled = false; }
  }
  function renderAuctionResult(r) {
    const K = v => v == null ? "—" : "$" + Math.round(v / 1000) + "K";
    const D = v => v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US");
    const verdict = {
      go: ["var(--green)", "✅ GO", "Margin under your max bid."],
      tight: ["#e8a93b", "⚠️ TIGHT", "Thin margin — strict discipline required."],
      pass: ["var(--red)", "⛔ PASS", "The opening bid already exceeds your max."],
      caution: ["#e8a93b", "⚠️ CAUTION", "Heavy rehab vs ARV — verify condition."],
    }[r.verdict] || ["var(--muted)", "—", ""];
    const margin = r.arv ? Math.round(r.profit_at_max / r.arv * 100) : null;
    const openLine = r.opening_bid != null
      ? `<div style="font-size:13px;">Opening bid: <strong>${D(r.opening_bid)}</strong> · ${r.opening_bid <= r.max_bid ? `<span style="color:var(--green)">${D(r.max_bid - r.opening_bid)} of margin under your max</span>` : `<span style="color:var(--red)">already ${D(r.opening_bid - r.max_bid)} above your max</span>`}</div>`
      : "";
    const risks = (r.risks || []).length ? `<div style="margin-top:10px;"><div style="font-weight:600; font-size:13px; margin-bottom:4px;">⚠️ Auction risks</div><ul style="margin:0; padding-left:18px; font-size:13px; line-height:1.5;">${r.risks.map(x => `<li>${escape(x)}</li>`).join("")}</ul></div>` : "";
    $("#auc-result").innerHTML = `
      <div class="card" style="display:grid; gap:16px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:10px;">
          <div>
            <div style="font-weight:700; font-size:16px;">${escape(r.address)}</div>
            <div class="muted" style="font-size:12px;">${r.ai_used ? `AI-estimated · ARV confidence: ${escape(r.arv_confidence || "—")}` : "Manual values"}</div>
          </div>
          <span class="pill" style="background:${verdict[0]}1a; color:${verdict[0]}; font-weight:700; font-size:14px; padding:6px 14px;">${verdict[1]}</span>
        </div>

        <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px;">
          <div class="auc-stat"><div class="auc-stat-l">Estimated ARV</div><div class="auc-stat-v">${D(r.arv)}</div></div>
          <div class="auc-stat"><div class="auc-stat-l">Estimated rehab</div><div class="auc-stat-v">${D(r.rehab)}</div></div>
          <div class="auc-stat" style="background:var(--green)15; border:1px solid var(--green);">
            <div class="auc-stat-l" style="color:var(--green); font-weight:700;">🎯 MAX BID</div>
            <div class="auc-stat-v" style="color:var(--green); font-size:26px;">${D(r.max_bid)}</div>
          </div>
          <div class="auc-stat"><div class="auc-stat-l">Profit at this max</div><div class="auc-stat-v">${D(r.profit_at_max)}${margin != null ? ` <span class="muted" style="font-size:12px;">(${margin}%)</span>` : ""}</div></div>
        </div>

        ${openLine}
        ${verdict[2] ? `<div style="font-size:13px; color:${verdict[0]};">${verdict[2]}${r.verdict_note && r.verdict_note !== verdict[2] ? " " + escape(r.verdict_note) : ""}</div>` : ""}

        <details style="font-size:13px;">
          <summary style="cursor:pointer; font-weight:600;">Calculation breakdown</summary>
          <div style="margin-top:8px; display:grid; grid-template-columns:1fr 1fr; gap:4px 18px; max-width:480px;">
            <span>ARV</span><span style="text-align:right;">${D(r.arv)}</span>
            <span>− Rehab</span><span style="text-align:right;">−${D(r.rehab)}</span>
            <span>− Selling costs (8%)</span><span style="text-align:right;">−${D(r.selling_costs)}</span>
            <span>− Holding</span><span style="text-align:right;">−${D(r.holding)}</span>
            <span>− Closing</span><span style="text-align:right;">−${D(r.closing)}</span>
            <span>− Target margin (${r.target_margin_pct}%)</span><span style="text-align:right;">−${D(r.target_profit)}</span>
            <span style="font-weight:600; border-top:1px solid var(--border); padding-top:4px;">= Max bid (÷ ${1 + r.premium_pct / 100} ${r.premium_pct}% premium)</span><span style="text-align:right; font-weight:600; border-top:1px solid var(--border); padding-top:4px;">${D(r.max_bid)}</span>
            <span class="muted">Ref. 70% rule (ARV×0.70 − rehab)</span><span class="muted" style="text-align:right;">${D(r.mao70)}</span>
            <span class="muted">All-in at purchase</span><span class="muted" style="text-align:right;">${D(r.all_in_at_max)}</span>
          </div>
          <p class="muted" style="margin-top:8px; font-size:12px;">auction.com buyer premium ~${r.premium_pct}% applied to the bid. ${r.summary ? escape(r.summary) : ""}</p>
        </details>

        ${r.condition_summary ? `<div style="font-size:13px;"><strong>Condition:</strong> ${escape(r.condition_summary)}</div>` : ""}
        ${risks}

        <div style="display:flex; gap:10px; flex-wrap:wrap;">
          <button class="btn primary" id="auc-add-board">+ Add to board</button>
          <button class="btn" id="auc-watch">⭐ Track this auction</button>
          ${($("#auc-url")?.value || "").trim() ? `<a class="btn ghost" href="${escape($("#auc-url").value.trim())}" target="_blank" rel="noopener">View listing ↗</a>` : ""}
        </div>
      </div>`;
    $("#auc-add-board")?.addEventListener("click", _auctionAddDeal);
    $("#auc-watch")?.addEventListener("click", _auctionWatch);
  }
  async function _auctionWatch(e) {
    if (!_aucLast) return;
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "…";
    try {
      await API.auctionWatch({ ..._gatherAuction(), ..._aucLast });
      btn.textContent = "⭐ Tracked"; btn.disabled = true;
      toast("Added to tracked auctions", "success");
      renderWatchlist();
    } catch (err) { btn.disabled = false; btn.textContent = "⭐ Track this auction"; toast(err.message, "error"); }
  }
  async function _auctionAddDeal(e) {
    const r = _aucLast; if (!r) return;
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "…";
    const p = _gatherAuction();
    try {
      const res = await API.createDeal({
        address: r.address,
        property_type: "Single Family Residence",
        beds: p.beds || null, baths: p.baths || null, sqft: p.sqft || null, year_built: p.year_built || null,
        purchase_price: r.max_bid || 0,
        arv_base: r.arv || 0, rehab_base: r.rehab || 0,
        arv_confidence: r.arv_confidence || "Low",
        source: "auction", source_url: p.url || "", status: "evaluating",
        notes: `[Auction] Recommended max bid: $${(r.max_bid || 0).toLocaleString("en-US")}\n`
          + (r.opening_bid ? `Opening bid: $${r.opening_bid.toLocaleString("en-US")}\n` : "")
          + `Estimated profit at max: $${(r.profit_at_max || 0).toLocaleString("en-US")}\n`
          + (r.condition_summary ? `Condition: ${r.condition_summary}\n` : "")
          + ((r.risks || []).length ? `Risks: ${r.risks.join("; ")}\n` : "")
          + (p.comments ? `\nListing comments:\n${p.comments}` : ""),
      });
      const deal = res.deal || res;
      (state.deals = state.deals || []).push(deal);
      btn.textContent = "✓ Added to board"; btn.classList.remove("primary");
      toast("Deal added", "success");
    } catch (err) { btn.disabled = false; btn.textContent = "+ Add to board"; toast(err.message, "error"); }
  }
  $("#auc-run")?.addEventListener("click", () => runAuction());

  // ===== Auction discovery by city/state =====
  let _aucFindResults = [];
  async function runAuctionFind() {
    const v = id => ($(id)?.value || "").trim();
    const n = id => { const x = Number(v(id)); return x > 0 ? x : null; };
    const location = v("#aucf-location");
    if (!location) { toast("Enter a city or state", "warn"); return; }
    const payload = { location, max_listings: Number(v("#aucf-count")) || 10 };
    if (n("#aucf-pricemax")) payload.price_max = n("#aucf-pricemax");
    if (n("#aucf-bedsmin")) payload.beds_min = n("#aucf-bedsmin");
    if (Number(v("#aucf-margin")) > 0) payload.target_margin_pct = Number(v("#aucf-margin"));
    if (Number(v("#aucf-holding")) >= 0 && v("#aucf-holding")) payload.holding = Number(v("#aucf-holding"));
    const st = $("#aucf-status"), btn = $("#aucf-run");
    btn.disabled = true;
    st.innerHTML = '<span class="spinner"></span> AI + web auction search… (~30-60s)';
    $("#aucf-results").innerHTML = "";
    try {
      if (!state.deals || !state.deals.length) { try { state.deals = await API.listDeals(); } catch {} }
      const r = await API.auctionFind(payload);
      if (!r.ok) { st.innerHTML = `<span style="color:var(--red)">${escape(r.error || "Search failed")}</span>`; return; }
      _aucFindResults = r.listings || [];
      st.innerHTML = `✓ ${_aucFindResults.length} auction(s)${r.area_label ? " — " + escape(r.area_label) : ""}${r.notes ? `<br><span class="muted" style="font-size:11.5px;">${escape(r.notes)}</span>` : ""}`;
      renderAuctionFindResults();
    } catch (e) { st.innerHTML = `<span style="color:var(--red)">${escape(e.message)}</span>`; }
    finally { btn.disabled = false; }
  }
  function renderAuctionFindResults() {
    const box = $("#aucf-results");
    if (!_aucFindResults.length) { box.innerHTML = `<div class="card"><p class="muted" style="margin:0;">No auctions found for this area.</p></div>`; return; }
    const D = v => (v == null || v === "") ? "—" : "$" + Math.round(v).toLocaleString("en-US");
    const vcol = { go: "var(--green)", tight: "#e8a93b", pass: "var(--red)", unknown: "var(--muted)" };
    const vlbl = { go: "GO", tight: "TIGHT", pass: "PASS", unknown: "?" };
    const today = new Date(); today.setHours(0, 0, 0, 0);
    box.innerHTML = `<div class="deals-grid">${_aucFindResults.map((l, i) => {
      let dateTag = "";
      if (l.auction_date) {
        const d = new Date(l.auction_date + "T00:00:00");
        const days = Math.round((d - today) / 86400000);
        const col = isNaN(days) ? "var(--muted)" : days < 0 ? "var(--muted)" : days <= 7 ? "var(--red)" : days <= 14 ? "#e8a93b" : "var(--muted)";
        dateTag = `<span style="color:${col}; font-weight:600;">📅 ${escape(l.auction_date)}${!isNaN(days) && days >= 0 ? ` (${days}d left)` : ""}</span>`;
      }
      const col = vcol[l.verdict] || "var(--muted)";
      const onBoard = _onBoard ? _onBoard(l.address) : false;
      return `<div class="card" style="display:flex; flex-direction:column; gap:6px;">
        <div style="display:flex; justify-content:space-between; gap:6px; align-items:flex-start;">
          <div style="font-weight:600;">${escape(l.address || "?")}</div>
          <span class="pill" style="background:${col}1a; color:${col}; font-weight:700; white-space:nowrap;">${vlbl[l.verdict] || "?"}</span>
        </div>
        <div class="muted" style="font-size:12px;">${escape([l.city, l.state, l.zip].filter(Boolean).join(" "))}${l.source ? " · " + escape(l.source) : ""}</div>
        <div style="font-size:12.5px; display:flex; gap:12px; flex-wrap:wrap;">${dateTag}${l.opening_bid ? `<span class="muted">opening ${D(l.opening_bid)}</span>` : ""}</div>
        <div style="font-size:12px;" class="muted">ARV ${D(l.arv_estimate)} · Rehab ${D(l.rehab_estimate)}${l.beds ? ` · ${l.beds}bd/${l.baths || "?"}ba` : ""}</div>
        <div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:2px;">
          <span style="font-size:11px; color:var(--muted); text-transform:uppercase;">Max bid</span>
          <span style="font-weight:700; font-size:18px; color:${col};">${D(l.max_bid)}</span>
        </div>
        <div style="display:flex; gap:6px; margin-top:6px; flex-wrap:wrap;">
          <button class="btn ghost aucf-detail" data-i="${i}" style="font-size:12px;" title="Detailed AI analysis">🔬 Details</button>
          <button class="btn ghost aucf-watch" data-i="${i}" style="font-size:12px;">⭐ Track</button>
          <button class="btn ${onBoard ? "" : "ghost"} aucf-add" data-i="${i}" style="font-size:12px;" ${onBoard ? "disabled" : ""}>${onBoard ? "✓ Board" : "+ Board"}</button>
        </div>
      </div>`;
    }).join("")}</div>`;
    box.querySelectorAll(".aucf-detail").forEach(b => b.addEventListener("click", () => _aucfDetail(_aucFindResults[+b.dataset.i])));
    box.querySelectorAll(".aucf-watch").forEach(b => b.addEventListener("click", () => _aucfWatch(_aucFindResults[+b.dataset.i], b)));
    box.querySelectorAll(".aucf-add").forEach(b => b.addEventListener("click", () => _aucfAdd(_aucFindResults[+b.dataset.i], b)));
  }
  // "Details" → prefill the single-address form (section ②) and run the deep analysis
  function _aucfDetail(l) {
    if (!l) return;
    const full = [l.address, l.city, `${l.state || ""} ${l.zip || ""}`.trim()].filter(s => s && s.trim()).join(", ");
    $("#auc-address").value = full || l.address || "";
    $("#auc-opening").value = l.opening_bid || "";
    $("#auc-beds").value = l.beds || ""; $("#auc-baths").value = l.baths || ""; $("#auc-sqft").value = l.sqft || "";
    $("#auc-year").value = l.year_built || ""; $("#auc-date").value = l.auction_date || "";
    document.getElementById("auc-address").scrollIntoView({ behavior: "smooth", block: "center" });
    runAuction();
  }
  async function _aucfWatch(l, btn) {
    if (!l) return;
    btn.disabled = true; btn.textContent = "…";
    try {
      await API.auctionWatch({
        address: [l.address, l.city, `${l.state || ""} ${l.zip || ""}`.trim()].filter(s => s && s.trim()).join(", ") || l.address,
        url: l.url || "", opening_bid: l.opening_bid, auction_date: l.auction_date,
        beds: l.beds, baths: l.baths, sqft: l.sqft, year_built: l.year_built,
        arv: l.arv_estimate, rehab: l.rehab_estimate, max_bid: l.max_bid,
        mao70: l.mao70, profit_at_max: l.profit_at_max, verdict: l.verdict,
        target_margin_pct: l.target_margin_pct,
      });
      btn.textContent = "⭐ Tracked";
      toast("Added to tracked auctions", "success");
      renderWatchlist();
    } catch (e) { btn.disabled = false; btn.textContent = "⭐ Track"; toast(e.message, "error"); }
  }
  async function _aucfAdd(l, btn) {
    if (!l) return;
    btn.disabled = true; btn.textContent = "…";
    const full = [l.address, l.city, `${l.state || ""} ${l.zip || ""}`.trim()].filter(s => s && s.trim()).join(", ") || l.address;
    try {
      const res = await API.createDeal({
        address: full, city: l.city || "", state: l.state || "", zip: String(l.zip || ""),
        property_type: l.property_type || "Single Family Residence",
        beds: l.beds || null, baths: l.baths || null, sqft: l.sqft || null, year_built: l.year_built || null,
        purchase_price: l.max_bid || l.opening_bid || 0,
        arv_base: l.arv_estimate || 0, rehab_base: l.rehab_estimate || 0, arv_confidence: "Low",
        source: "auction", source_url: l.url || "", status: "evaluating",
        notes: `[Auction — found by area]\nMax bid: $${(l.max_bid || 0).toLocaleString("en-US")}`
          + (l.opening_bid ? `\nOpening: $${Number(l.opening_bid).toLocaleString("en-US")}` : "")
          + (l.auction_date ? `\nAuction date: ${l.auction_date}` : "")
          + (l.source ? `\nSource: ${l.source}` : ""),
      });
      const deal = res.deal || res;
      (state.deals = state.deals || []).push(deal);
      btn.textContent = "✓ Board"; btn.disabled = true; btn.classList.remove("primary");
      toast("Deal added", "success");
    } catch (e) { btn.disabled = false; btn.textContent = "+ Board"; toast(e.message, "error"); }
  }
  $("#aucf-run")?.addEventListener("click", runAuctionFind);
  $("#aucf-location")?.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); runAuctionFind(); } });

  async function renderWatchlist() {
    const box = $("#auc-watchlist"); if (!box) return;
    let items = [];
    try { items = await API.auctionWatchlist(); } catch { box.innerHTML = ""; return; }
    if (!items.length) { box.innerHTML = `<div class="card"><p class="muted" style="margin:0;">No tracked auctions. Analyze a property, then click “⭐ Track”.</p></div>`; return; }
    const D = v => v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US");
    const vcol = { go: "var(--green)", tight: "#e8a93b", caution: "#e8a93b", pass: "var(--red)" };
    const vlbl = { go: "GO", tight: "TIGHT", caution: "CAUTION", pass: "PASS" };
    const today = new Date(); today.setHours(0, 0, 0, 0);
    box.innerHTML = `<div style="display:grid; gap:10px;">${items.map(it => {
      let dateTag = "";
      if (it.auction_date) {
        const d = new Date(it.auction_date + "T00:00:00");
        const days = Math.round((d - today) / 86400000);
        const col = days < 0 ? "var(--muted)" : days <= 7 ? "var(--red)" : days <= 14 ? "#e8a93b" : "var(--muted)";
        dateTag = `<span style="color:${col}; font-weight:600;">📅 ${it.auction_date}${days >= 0 ? ` (${days}d left)` : " (past)"}</span>`;
      } else dateTag = `<span class="muted">📅 date ?</span>`;
      const col = vcol[it.verdict] || "var(--muted)";
      return `<div class="card" style="padding:12px 14px; display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
        <div style="flex:1; min-width:180px;">
          <div style="font-weight:600;">${escape(it.address || "?")}</div>
          <div style="font-size:12px; margin-top:2px; display:flex; gap:12px; flex-wrap:wrap;">
            ${dateTag}
            <span class="muted">ARV ${D(it.arv)} · Rehab ${D(it.rehab)}</span>
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:11px; color:var(--muted); text-transform:uppercase;">Max bid</div>
          <div style="font-weight:700; font-size:18px; color:var(--green);">${D(it.max_bid)}</div>
        </div>
        <span class="pill" style="background:${col}1a; color:${col}; font-weight:700;">${vlbl[it.verdict] || "—"}</span>
        <div style="display:flex; gap:6px;">
          <button class="btn ghost wl-recheck" data-id="${escape(it.id)}" title="Re-analyze" style="font-size:12px;">🔄</button>
          <button class="btn ghost wl-remove" data-id="${escape(it.id)}" title="Remove" style="font-size:12px;">🗑</button>
        </div>
      </div>`;
    }).join("")}</div>`;
    box.querySelectorAll(".wl-recheck").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "…";
      try { await API.auctionRecheck(b.dataset.id); toast("Re-analyzed", "success"); renderWatchlist(); }
      catch (e) { b.disabled = false; b.textContent = "🔄"; toast(e.message, "error"); }
    }));
    box.querySelectorAll(".wl-remove").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("Remove this auction from tracking?")) return;
      try { await API.auctionUnwatch(b.dataset.id); renderWatchlist(); } catch (e) { toast(e.message, "error"); }
    }));
  }
  $("#auc-recheck-all")?.addEventListener("click", async () => {
    const btn = $("#auc-recheck-all"); btn.disabled = true; btn.textContent = "🔄 Analyzing…";
    try {
      const res = await API.auctionRecheckAll();
      const opp = (res.opportunities || []).length, up = (res.upcoming || []).length;
      toast(`${res.checked || 0} re-analyzed · ${opp} opportunity(ies) · ${up} soon`, "success");
      renderWatchlist();
    } catch (e) { toast(e.message, "error"); }
    finally { btn.disabled = false; btn.textContent = "🔄 Re-analyze all"; }
  });
  document.querySelector('[data-view="auction"]')?.addEventListener("click", () => { setTimeout(renderWatchlist, 50); });

  // ============== REHAB ESTIMATOR ==============
  const REHAB_CATALOG = [
    ["Kitchen", 12000], ["Bathroom", 6000], ["Flooring", 4000], ["Paint", 3500],
    ["Roof", 8000], ["HVAC", 6000], ["Electrical", 4000],
    ["Plumbing", 3500], ["Windows", 5000], ["Exterior", 3000],
    ["Drywall / plaster", 3000], ["Water heater", 1500],
  ];
  let _rehabItems = [];   // [{label, cost}]
  let _rehabDealId = null;

  function openRehabModal(dealId) {
    _rehabDealId = dealId;
    const deal = (state.deals || []).find(d => d.id === dealId);
    _rehabItems = Array.isArray(deal?.rehab_items) && deal.rehab_items.length
      ? deal.rehab_items.map(i => ({ label: i.label, cost: Number(i.cost) || 0 }))
      : [];
    $("#rehab-ai-status").textContent = "";
    $("#rehab-contingency-on").checked = deal?.rehab_contingency_pct != null ? true : true;
    $("#rehab-contingency-pct").value = deal?.rehab_contingency_pct ?? 15;
    // catalog chips
    $("#rehab-catalog").innerHTML = REHAB_CATALOG.map(([l, c]) =>
      `<button class="btn ghost rehab-chip" data-label="${escape(l)}" data-cost="${c}" style="font-size:12px; padding:5px 9px;">+ ${escape(l)}</button>`).join("");
    $$("#rehab-catalog .rehab-chip").forEach(b => b.addEventListener("click", () => {
      _rehabItems.push({ label: b.dataset.label, cost: Number(b.dataset.cost) || 0 });
      _renderRehabItems();
    }));
    _renderRehabItems();
    $("#rehab-modal").style.display = "flex";
  }
  function closeRehabModal() { $("#rehab-modal").style.display = "none"; _rehabDealId = null; }

  function _rehabSubtotal() { return _rehabItems.reduce((s, i) => s + (Number(i.cost) || 0), 0); }
  function _rehabTotal() {
    const sub = _rehabSubtotal();
    const on = $("#rehab-contingency-on")?.checked;
    const pct = Number($("#rehab-contingency-pct")?.value) || 0;
    return on ? Math.round(sub * (1 + pct / 100)) : sub;
  }
  function _renderRehabItems() {
    const box = $("#rehab-items");
    box.innerHTML = _rehabItems.length ? _rehabItems.map((it, i) => `
      <div class="rehab-row" style="display:flex; gap:8px; align-items:center; margin-bottom:6px;">
        <input class="rehab-label" data-i="${i}" value="${escape(it.label)}" placeholder="Work item" style="flex:1;">
        <span style="color:var(--muted);">$</span>
        <input class="rehab-cost" data-i="${i}" type="number" value="${it.cost}" style="width:100px;">
        <button class="btn ghost rehab-del" data-i="${i}" title="Remove" style="padding:4px 8px;">×</button>
      </div>`).join("")
      : `<div class="muted" style="font-size:12.5px; padding:6px 0;">No items. Add via the buttons above or with AI.</div>`;
    box.querySelectorAll(".rehab-label").forEach(inp => inp.addEventListener("input", e => { _rehabItems[+e.target.dataset.i].label = e.target.value; }));
    box.querySelectorAll(".rehab-cost").forEach(inp => inp.addEventListener("input", e => { _rehabItems[+e.target.dataset.i].cost = Number(e.target.value) || 0; _updateRehabTotal(); }));
    box.querySelectorAll(".rehab-del").forEach(b => b.addEventListener("click", e => { _rehabItems.splice(+e.target.dataset.i, 1); _renderRehabItems(); }));
    _updateRehabTotal();
  }
  function _updateRehabTotal() { const t = $("#rehab-total"); if (t) t.textContent = fmtMoney(_rehabTotal()); }

  $("#rehab-add-line")?.addEventListener("click", () => { _rehabItems.push({ label: "", cost: 0 }); _renderRehabItems(); });
  $("#rehab-contingency-on")?.addEventListener("change", _updateRehabTotal);
  $("#rehab-contingency-pct")?.addEventListener("input", _updateRehabTotal);
  $("#rehab-modal-close")?.addEventListener("click", closeRehabModal);
  $("#rehab-modal-backdrop")?.addEventListener("click", closeRehabModal);
  $("#rehab-cancel")?.addEventListener("click", closeRehabModal);

  $("#rehab-ai-btn")?.addEventListener("click", async () => {
    if (!_rehabDealId) return;
    const btn = $("#rehab-ai-btn"), st = $("#rehab-ai-status");
    btn.disabled = true; st.textContent = "Analyzing…";
    try {
      const r = await API.rehabEstimate(_rehabDealId);
      if (!r.ok) { st.textContent = r.error || "Failed"; return; }
      _rehabItems = (r.items || []).map(i => ({ label: i.label, cost: Number(i.cost) || 0 }));
      _renderRehabItems();
      st.textContent = r.summary ? `✓ ${r.summary}` : "✓ AI estimate applied";
    } catch (e) { st.textContent = e.message; }
    finally { btn.disabled = false; }
  });

  $("#rehab-apply")?.addEventListener("click", async () => {
    if (!_rehabDealId) return;
    const total = _rehabTotal();
    const items = _rehabItems.filter(i => i.label || i.cost);
    try {
      const updated = await API.patchDeal(_rehabDealId, {
        rehab_base: total,
        rehab_items: items,
        rehab_contingency_pct: $("#rehab-contingency-on").checked ? (Number($("#rehab-contingency-pct").value) || 0) : 0,
      });
      const i = (state.deals || []).findIndex(d => d.id === _rehabDealId);
      if (i >= 0) state.deals[i] = updated.deal || updated;
      toast(`Rehab updated: ${fmtMoney(total)}`, "success");
      closeRehabModal();
      if (state.currentDealId === _rehabDealId || true) openDeal(state.currentDealId);
    } catch (e) { toast(e.message, "error"); }
  });

  $("#pdf-close")?.addEventListener("click", () => {
    $("#pdf-preview").style.display = "none";
    $("#pdf-iframe").src = "about:blank";
  });

  // ============== ADD DEAL ==============
  async function tryScrape(url) {
    if (!url) return;
    // Auto-detect LEAD URLs and route to Leads view instead
    if (url.includes("/ld/") || url.includes("open_order=")) {
      // Leads view removed — fall through to treat it like any other URL
    }
    $("#scrape-status").innerHTML = '<span class="spinner"></span> Fetching...';
    $("#scrape-status").className = "status-line";
    $("#scrape-btn").disabled = true;
    try {
      const data = await API.scrape(url);
      const seed = seedToFormData(data);
      fillForm(seed);
      if (data.scrape_error || data.error) {
        const msg = data.scrape_error || data.error;
        let html = `<strong>${escape(msg)}</strong>`;
        if (data.external_link) {
          html += ` &middot; <a href="${escape(data.external_link)}" target="_blank" style="color:var(--accent)">Open listing ↗</a>`;
        }
        $("#scrape-status").innerHTML = html;
        $("#scrape-status").className = "status-line error";
      } else {
        $("#scrape-status").textContent = `✓ Auto-filled from ${data.source || "listing"}`;
        $("#scrape-status").className = "status-line success";
      }
      // ARV missing check
      const arvField = $("#deal-form").elements["arv_base"];
      if (!arvField.value || arvField.value === "0") {
        // Wait a beat so user sees the success message first
        setTimeout(() => openArvMissingModal(), 600);
      }
    } catch (e) {
      $("#scrape-status").textContent = "Failed: " + e.message;
      $("#scrape-status").className = "status-line error";
    } finally {
      $("#scrape-btn").disabled = false;
    }
  }

  // ============== ARV MISSING / AI RESEARCH ==============
  async function openArvMissingModal() {
    $("#arv-modal").style.display = "flex";
    try {
      const cfg = await API.aiConfig();
      $("#arv-no-key-hint").style.display = cfg.configured ? "none" : "block";
    } catch {
      $("#arv-no-key-hint").style.display = "block";
    }
  }
  function closeArvMissingModal() { $("#arv-modal").style.display = "none"; }
  $("#arv-modal-close")?.addEventListener("click", closeArvMissingModal);
  $(".modal-backdrop", $("#arv-modal"))?.addEventListener("click", closeArvMissingModal);
  $("#arv-opt-manual")?.addEventListener("click", () => {
    closeArvMissingModal();
    $("#deal-form").elements["arv_base"].focus();
    toast("Enter ARV in the form", "success");
  });
  $("#arv-opt-ai")?.addEventListener("click", () => {
    closeArvMissingModal();
    researchArvForForm();
  });
  $("#goto-settings-from-arv")?.addEventListener("click", e => {
    e.preventDefault();
    closeArvMissingModal();
    showView("settings");
    setTimeout(() => $("#ai-key-input")?.focus(), 200);
  });

  function collectFormForResearch() {
    const f = $("#deal-form");
    const get = (n) => f.elements[n]?.value || "";
    const num = (n) => Number(get(n)) || null;
    return {
      address: get("address"), city: get("city"), state: get("state"), zip: get("zip"),
      property_type: get("property_type"),
      beds: num("beds"), baths: num("baths"), sqft: num("sqft"),
      year_built: num("year_built"), lot_size: get("lot_size"),
      purchase_price: num("purchase_price"), rehab_base: num("rehab_base"),
      rehab_scope: get("rehab_scope"),
      zillow_estimate: f._extraFields?.zillow_estimate,
      realtor_estimate: f._extraFields?.realtor_estimate,
      comp_value_estimate: f._extraFields?.comp_value_estimate,
    };
  }

  async function researchArvForForm() {
    const deal = collectFormForResearch();
    if (!deal.address) {
      toast("Add an address first", "error");
      return;
    }
    $("#arv-results-modal").style.display = "flex";
    $("#arv-results-body").innerHTML = `<div class="empty">
      <div class="empty-ico">🔍</div>
      <h3>Researching ARV...</h3>
      <p>Claude is searching the web for recent comparable sales.<br>This usually takes 20-40 seconds.</p>
    </div>`;
    $("#arv-results-apply").disabled = true;
    try {
      const r = await API.researchArv({ deal });
      renderArvResults(r);
    } catch (e) {
      $("#arv-results-body").innerHTML = `<div style="padding:20px; color:var(--red);">
        <strong>Error:</strong> ${escape(e.message)}
      </div>`;
    }
  }

  function renderArvResults(r) {
    const confColor = r.confidence === "High" ? "green" : r.confidence === "Low" ? "red" : "yellow";
    let html = `
      <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:12px; margin-bottom:18px;">
        <div class="stat-card"><div class="label">ARV Low</div><div class="value">${fmtMoney(r.arv_low)}</div></div>
        <div class="stat-card green"><div class="label">ARV Base</div><div class="value">${fmtMoney(r.arv_base)}</div></div>
        <div class="stat-card"><div class="label">ARV High</div><div class="value">${fmtMoney(r.arv_high)}</div></div>
      </div>
      <p><span class="pill ${confColor}">${escape(r.confidence)} confidence</span>
         <span class="muted" style="margin-left:10px;">${r.web_searches_used || 0} web searches • ${r.model}</span></p>
      <h3 class="modal-section">Reasoning</h3>
      <p style="font-size:13px; line-height:1.6;">${escape(r.reasoning || '—')}</p>
    `;
    if (r.comparables && r.comparables.length) {
      html += `<h3 class="modal-section">Comparables found (${r.comparables.length})</h3>
        <div class="table-wrap"><table class="data-table">
          <thead><tr><th>Address</th><th>Bd/Ba</th><th>SqFt</th><th>Price</th><th>Date</th></tr></thead>
          <tbody>${r.comparables.map(c => `
            <tr>
              <td>${escape(c.address || '—')}<div class="muted">${escape(c.notes || '')}</div></td>
              <td>${c.beds || '?'}/${c.baths || '?'}</td>
              <td>${c.sqft ? c.sqft.toLocaleString() : '—'}</td>
              <td><strong>${fmtMoney(c.price)}</strong></td>
              <td class="muted">${escape(c.date || '')}</td>
            </tr>`).join('')}</tbody>
        </table></div>`;
    }
    if (r.market_notes) {
      html += `<h3 class="modal-section">Market notes</h3>
        <p style="font-size:13px;">${escape(r.market_notes)}</p>`;
    }
    if (r.warnings && r.warnings.length) {
      html += `<div style="margin-top:14px; padding:10px 12px; background:var(--orange-light); color:var(--orange); border-radius:8px;">
        <strong>⚠ Warnings:</strong><br>${r.warnings.map(w => '• ' + escape(w)).join('<br>')}</div>`;
    }
    $("#arv-results-body").innerHTML = html;
    $("#arv-results-apply").disabled = false;
    $("#arv-results-apply")._payload = r;
  }

  function closeArvResultsModal() { $("#arv-results-modal").style.display = "none"; }
  $("#arv-results-close")?.addEventListener("click", closeArvResultsModal);
  $("#arv-results-cancel")?.addEventListener("click", closeArvResultsModal);
  $(".modal-backdrop", $("#arv-results-modal"))?.addEventListener("click", closeArvResultsModal);

  $("#arv-results-apply")?.addEventListener("click", () => {
    const r = $("#arv-results-apply")._payload;
    if (!r) return;
    const f = $("#deal-form");
    if (r.arv_low) f.elements["arv_low"].value = r.arv_low;
    if (r.arv_base) f.elements["arv_base"].value = r.arv_base;
    if (r.arv_high) f.elements["arv_high"].value = r.arv_high;
    if (r.confidence) f.elements["arv_confidence"].value = r.confidence;
    // Append AI reasoning to notes
    if (r.reasoning || (r.comparables && r.comparables.length)) {
      const block = [
        "[AI ARV research]",
        r.reasoning,
        r.comparables && r.comparables.length
          ? "Comps: " + r.comparables.map(c =>
              `${(c.address || '').split(',')[0]} (${c.beds}/${c.baths}, ${c.sqft}sf) — $${(c.price || 0).toLocaleString()}`
            ).join("; ")
          : "",
        r.market_notes,
      ].filter(Boolean).join("\n");
      const cur = f.elements["notes"].value;
      f.elements["notes"].value = (cur ? cur + "\n\n" : "") + block;
    }
    closeArvResultsModal();
    toast("ARV applied to form", "success");
  });

  // Inline AI button next to ARV field
  $("#arv-research-btn")?.addEventListener("click", async () => {
    try {
      const cfg = await API.aiConfig();
      if (!cfg.configured) {
        showView("settings");
        toast("Add an Anthropic API key first", "error");
        setTimeout(() => $("#ai-key-input")?.focus(), 200);
        return;
      }
    } catch {}
    researchArvForForm();
  });
  // One bar for both: detect whether the input is a URL/link or an address,
  // then route to the scraper or the address search accordingly.
  function _looksLikeUrl(v) {
    return /^https?:\/\//i.test(v)
        || /^www\./i.test(v)
        || /\b(zillow|redfin|realtor|trulia|homes|ispeedtolead|auction|movoto|har|compass|xome|hubzu)\.[a-z]{2,}/i.test(v)  // portal.tld
        || /^[\w-]+(\.[\w-]+)+\/\S/.test(v);   // bare domain.tld/path
  }
  function handleUnifiedSearch(value) {
    const v = (value || "").trim();
    if (!v) return;
    if (_looksLikeUrl(v)) tryScrape(v);
    else tryFindByAddress(v);
  }
  $("#scrape-btn")?.addEventListener("click", () => handleUnifiedSearch($("#scrape-url").value.trim()));
  $("#scrape-url")?.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); handleUnifiedSearch(e.target.value.trim()); }
  });

  // ============== ADDRESS SEARCH ==============
  async function tryFindByAddress(address) {
    if (!address || address.length < 5) return;
    const status = $("#scrape-status");
    const btn = $("#scrape-btn");
    btn.disabled = true;
    status.innerHTML = '<span class="spinner"></span> Searching Zillow…';
    status.className = "status-line";
    try {
      const r = await API.findByAddress(address);
      if (!r.found) {
        status.innerHTML = `<strong>Not found.</strong> ${escape(r.error || '')}<br/>
          You can still <a href="https://www.zillow.com/homes/${encodeURIComponent(address)}_rb/"
                              target="_blank" style="color:var(--accent)">search Zillow manually ↗</a>
          or fill the form below.`;
        status.className = "status-line error";
        // Pre-fill just the address field anyway
        const form = $("#deal-form");
        form.elements.address.value = address;
        return;
      }
      const data = r.data || {};
      const sourceLabel = r.source === "zillow" ? "Zillow" :
                           r.source === "redfin" ? "Redfin" :
                           r.source === "ai" ? "AI search" : r.source;
      const fmtCount = (data.image_gallery || []).length;
      status.innerHTML = `✓ Found via <strong>${escape(sourceLabel)}</strong>
        ${data.address ? '— ' + escape(data.address) : ''}
        ${fmtCount ? `· ${fmtCount} photos` : ''}
        <a href="${escape(r.url)}" target="_blank" style="color:var(--accent); margin-left:8px;">View source ↗</a>`;
      status.className = "status-line success";
      // Pre-fill the form
      fillForm(seedToFormData(data));
    } catch (e) {
      const msg = e.message || "Search failed";
      const isAuth = /credit|balance/i.test(msg);
      status.innerHTML = `<strong>Search failed:</strong> ${escape(msg)}` +
        (isAuth ? `<br/><a href="https://console.anthropic.com/settings/billing"
                              target="_blank" style="color:var(--accent)">Top up Anthropic credits ↗</a>` : '');
      status.className = "status-line error";
    } finally {
      btn.disabled = false;
    }
  }
  // (address search is now reached through the unified #scrape-url bar above)

  // Translate Zillow / Redfin enum property types → form select options
  const _PROPERTY_TYPE_MAP = {
    "SINGLE_FAMILY":       "Single Family Residence",
    "SINGLE_FAMILY_HOME":  "Single Family Residence",
    "Single Family":       "Single Family Residence",
    "CONDO":               "Condominium",
    "CONDOMINIUM":         "Condominium",
    "TOWNHOUSE":           "Townhouse",
    "MULTI_FAMILY":        "Multi-family (2-4)",
    "MULTIFAMILY":         "Multi-family (2-4)",
    "DUPLEX":              "Multi-family (2-4)",
    "TRIPLEX":             "Multi-family (2-4)",
    "QUADPLEX":            "Multi-family (2-4)",
  };
  function _normalizePropertyType(t) {
    if (!t) return undefined;
    return _PROPERTY_TYPE_MAP[t] || _PROPERTY_TYPE_MAP[t.toUpperCase()] || t;
  }

  function seedToFormData(seed) {
    const out = {
      address: seed.address || "",
      city: seed.city, state: seed.state, zip: seed.zip,
      property_type: _normalizePropertyType(seed.property_type),
      beds: seed.beds, baths: seed.baths, sqft: seed.sqft,
      year_built: seed.year_built, lot_size: seed.lot_size,
      purchase_price: seed.listing_price,
      arv_base: seed.zestimate || seed.comp_value_estimate || seed.arv_estimate,
      arv_low: seed.comp_value_low,
      arv_high: seed.comp_value_high,
      rehab_base: seed.rehab_estimate,
      estimated_rent: seed.rent_zestimate,
      // If backend gave us monthly_taxes, use it. Otherwise estimate from
      // property_tax_rate_pct × listing_price (annual / 12).
      monthly_taxes: seed.monthly_taxes
        || (seed.property_tax_rate_pct && seed.listing_price
              ? Math.round(seed.listing_price * seed.property_tax_rate_pct / 100 / 12)
              : null),
      monthly_hoa: seed.monthly_hoa,
      median_dom: seed.median_dom,
      days_on_market: seed.days_on_market,
      time_on_zillow: seed.time_on_zillow,
      page_view_count: seed.page_view_count,
      favorite_count: seed.favorite_count,
      source_url: seed.source_url,
      image: seed.image,
      image_gallery: seed.image_gallery,
      lat: seed.lat, lng: seed.lng,
      sale_comparables: seed.sale_comparables,
      rent_comparables: seed.rent_comparables,
      zillow_estimate: seed.zillow_estimate,
      realtor_estimate: seed.realtor_estimate,
      redfin_estimate: seed.redfin_estimate,
      comp_value_estimate: seed.comp_value_estimate,
      comp_value_low: seed.comp_value_low,
      comp_value_high: seed.comp_value_high,
      foundation: seed.foundation, basement: seed.basement,
      roof_notes: seed.roof_notes, hvac_notes: seed.hvac_notes,
      water_heater_notes: seed.water_heater_notes, flood_risk: seed.flood_risk,
      showing_date: seed.showing_date, strategy_hint: seed.strategy_hint,
      listing_name: seed.listing_name,
      description: seed.description,
    };

    const factParts = [];
    if (seed.listing_name) factParts.push("**" + seed.listing_name + "**");
    if (seed.showing_date) factParts.push("Showing: " + seed.showing_date);
    if (seed.strategy_hint) factParts.push("Strategy hint: " + seed.strategy_hint);
    const externals = [];
    if (seed.zillow_estimate) externals.push("Zillow: $" + seed.zillow_estimate.toLocaleString());
    if (seed.realtor_estimate) externals.push("Realtor: $" + seed.realtor_estimate.toLocaleString());
    if (seed.comp_value_estimate)
      externals.push("ispeedtolead comping: $" + seed.comp_value_estimate.toLocaleString());
    if (externals.length) factParts.push("ARV anchors → " + externals.join(" | "));
    const physical = [];
    if (seed.foundation) physical.push("Foundation: " + seed.foundation);
    if (seed.basement) physical.push("Basement: " + seed.basement);
    if (seed.roof_notes) physical.push("Roof: " + seed.roof_notes);
    if (seed.hvac_notes) physical.push("HVAC: " + seed.hvac_notes);
    if (seed.water_heater_notes) physical.push("Water heater: " + seed.water_heater_notes);
    if (seed.flood_risk) physical.push("Flood risk: " + seed.flood_risk);
    if (physical.length) factParts.push(physical.join(" • "));
    if (seed.school_rating) { factParts.push("School rating: " + seed.school_rating); out.school_rating = seed.school_rating; }
    const factsBlock = factParts.length ? factParts.join("\n") + "\n\n" : "";
    out.notes = (factsBlock + (seed.description || "")).slice(0, 6000);
    return out;
  }

  function fillForm(d) {
    const form = $("#deal-form");
    form.reset();
    Object.entries(d).forEach(([k, v]) => {
      const el = form.elements[k];
      if (el && v !== null && v !== undefined && !Array.isArray(v) && typeof v !== "object") {
        el.value = v;
      }
    });
    // Stash complex fields so we can submit them
    form._extraFields = {};
    ["image_gallery","sale_comparables","rent_comparables","image","lat","lng",
     "zillow_estimate","realtor_estimate","redfin_estimate","comp_value_estimate",
     "comp_value_low","comp_value_high","foundation","basement","roof_notes",
     "hvac_notes","water_heater_notes","flood_risk","showing_date","strategy_hint",
     "listing_name","description"].forEach(k => {
      if (d[k] !== undefined && d[k] !== null) form._extraFields[k] = d[k];
    });
  }

  $("#deal-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const fd = new FormData(form);
    const obj = {};
    fd.forEach((v, k) => {
      if (v === "" || v == null) return;
      if (["beds","baths","sqft","year_built","purchase_price","arv_base","arv_low","arv_high",
            "rehab_base","rehab_low","rehab_high","holding_months","holding_cost_monthly",
            "selling_cost_pct","estimated_rent","monthly_taxes","monthly_insurance","monthly_hoa",
            "monthly_maintenance","monthly_mgmt","vacancy_pct","market_trend_yoy_pct","median_dom"]
            .includes(k)) {
        obj[k] = Number(v);
      } else obj[k] = v;
    });
    if (form._extraFields) Object.assign(obj, form._extraFields);
    // Only attach an id when the form is bound to an existing deal (edit, or a
    // re-submit of a deal we just created). A fresh add has formDealId == null,
    // so the backend mints a new id instead of overwriting the last-viewed deal.
    if (state.formDealId) obj.id = state.formDealId;
    try {
      const data = await API.createDeal(obj);
      toast(state.formDealId ? "Deal updated" : "Deal saved", "success");
      state.currentDealId = data.deal.id;
      state.formDealId = data.deal.id;  // a re-submit now updates this same deal
      openDeal(state.currentDealId);
    } catch (e) {
      // Duplicate address → offer to open the existing deal or force-create.
      if (/duplicate/i.test(e.message)) {
        const m = e.message.match(/id:\s*([a-z0-9-]+)/i);
        if (confirm(e.message + "\n\nOK = open the existing deal\nCancel = create a duplicate anyway")) {
          if (m) { openDeal(m[1]); return; }
        } else {
          try {
            const data = await API.createDeal({ ...obj, force_duplicate: true });
            toast("Deal created (duplicate accepted)", "success");
            state.currentDealId = data.deal.id; state.formDealId = data.deal.id;
            openDeal(state.currentDealId); return;
          } catch (e2) { toast(e2.message, "error"); return; }
        }
      }
      toast(e.message, "error");
    }
  });

  // ============== COMPARE ==============
  $("#compare-pdf-btn")?.addEventListener("click", async () => {
    $("#compare-status").innerHTML = '<span class="spinner"></span> Generating...';
    $("#compare-status").className = "status-line";
    try {
      const url = API.comparePdfUrl();
      $("#compare-iframe").src = url;
      $("#compare-pdf-preview").style.display = "block";
      $("#compare-status").textContent = "Done.";
      $("#compare-status").className = "status-line success";
      // Stash the URL so the download button can use it
      const dl = $("#compare-pdf-download");
      if (dl) { dl.dataset.url = url; dl.dataset.filename = "flip-board-comparison.pdf"; }
      $("#compare-pdf-preview").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      $("#compare-status").textContent = e.message;
      $("#compare-status").className = "status-line error";
    }
  });

  // Comparison PDF download (uses native dialog if pywebview is available)
  $("#compare-pdf-download")?.addEventListener("click", async () => {
    const btn = $("#compare-pdf-download");
    const url = btn?.dataset.url || API.comparePdfUrl();
    const filename = btn?.dataset.filename || "flip-board-comparison.pdf";
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Saving…';
    try {
      // Native save (most reliable in WebKit)
      if (window.pywebview && window.pywebview.api && window.pywebview.api.save_pdf) {
        const r = await window.pywebview.api.save_pdf(url, filename);
        if (r.ok) toast(`✓ Saved: ${r.path}`, "success");
        else if (r.cancelled) toast("Cancelled", "info");
        else toast("Failed: " + (r.error || "?"), "error");
      } else {
        // Fallback: open URL in new tab so user can save manually
        const a = document.createElement("a");
        a.href = url; a.download = filename; a.target = "_blank";
        document.body.appendChild(a); a.click(); a.remove();
      }
    } finally {
      btn.disabled = false; btn.innerHTML = orig;
    }
  });
  $("#compare-pdf-close")?.addEventListener("click", () => {
    $("#compare-pdf-preview").style.display = "none";
  });

  // ============== PDF OPTIONS MODAL ==============
  let currentDealCache = null;
  async function openPdfModal() {
    try { currentDealCache = await API.getDeal(state.currentDealId); }
    catch (e) { toast(e.message, "error"); return; }
    const d = currentDealCache.deal;
    const M = $("#pdf-modal");
    $("[name=holding_months]", M).value = d.holding_months || 5;
    $("[name=holding_cost_monthly]", M).value = d.holding_cost_monthly || 500;
    $("[name=selling_cost_pct]", M).value = d.selling_cost_pct || 8;
    $("[name=strategy][value=flip]", M).checked = true;
    // Pre-fill the financing block from the deal's saved scenario (hard money,
    // etc.) so the report reflects exactly what was entered on the deal.
    const fin = d.financing || {};
    const setV = (name, val) => {
      const el = $(`[name=${name}]`, M);
      if (el && val != null && val !== "") el.value = val;
    };
    if (fin.method) { const sel = $("[name=financing_method]", M); if (sel) sel.value = fin.method; }
    setV("loan_ltv_pct", fin.ltv_pct);
    setV("interest_rate_pct", fin.interest_rate_pct);
    setV("origination_pct", fin.origination_pct);
    setV("lender_fees_pct", fin.lender_fees_pct);
    setV("loan_term_months", fin.term_months);
    const rf = $("[name=rehab_financed]", M);
    if (rf && fin.rehab_financed != null) rf.value = fin.rehab_financed ? "yes" : "no";
    M.style.display = "flex";
    updateLivePreview();
  }
  function closePdfModal() { $("#pdf-modal").style.display = "none"; }
  $("#modal-close")?.addEventListener("click", closePdfModal);
  $("#modal-cancel")?.addEventListener("click", closePdfModal);
  $(".modal-backdrop", $("#pdf-modal"))?.addEventListener("click", closePdfModal);

  function collectModalOptions() {
    const opts = {};
    $$("input, select", $("#pdf-modal")).forEach(el => {
      if (el.type === "radio") { if (el.checked) opts[el.name] = el.value; }
      else if (el.name) {
        const v = el.value;
        if (el.type === "number") opts[el.name] = v === "" ? null : Number(v);
        else opts[el.name] = v;
      }
    });
    return opts;
  }

  function updateLivePreview() {
    if (!currentDealCache) return;
    const opts = collectModalOptions();
    const d = currentDealCache.deal;
    const arv = d.arv_base || 0;
    const purchase = d.purchase_price || 0;
    const rehab = d.rehab_base || 0;
    const closing = purchase * (opts.purchase_closing_pct || 2) / 100;
    const holding_total = (opts.holding_months || 5) * (opts.holding_cost_monthly || 0);
    const selling = arv * (opts.selling_cost_pct || 8) / 100;
    const dd = opts.due_diligence_fees || 0;
    const other = opts.other_fees || 0;
    let financing_cost = 0;
    let cash_needed = purchase + rehab + closing + holding_total;
    let strat = opts.strategy || "flip";
    if (opts.financing_method && opts.financing_method !== "cash") {
      const loanBase = purchase * ((opts.loan_ltv_pct || 0) / 100);
      const rehabFinanced = opts.rehab_financed === "yes" ? rehab : 0;
      const loan = loanBase + rehabFinanced;
      const months = opts.loan_term_months || (opts.holding_months || 5);
      const interest = loan * ((opts.interest_rate_pct || 0) / 100) * (months / 12);
      const points = loan * ((opts.origination_pct || 0) / 100);
      const lenderFees = loan * ((opts.lender_fees_pct || 0) / 100);
      financing_cost = interest + points + lenderFees;
      cash_needed = (purchase - loanBase) + (rehab - rehabFinanced) + closing + holding_total + points + lenderFees;
    }
    const all_in = purchase + closing + rehab + holding_total + selling + dd + other + financing_cost;
    const net_profit = arv - all_in;
    const roi = cash_needed > 0 ? (net_profit / cash_needed * 100) : 0;
    const annualized = (opts.holding_months || 5) > 0 ? roi * 12 / opts.holding_months : 0;
    let html = "";
    html += row("Strategy", strat.toUpperCase());
    html += row("Total all-in cost", fmtMoney(all_in));
    html += row("Financing cost", fmtMoney(financing_cost));
    html += row("Cash needed up-front", `<strong>${fmtMoney(cash_needed)}</strong>`);
    if (strat === "flip" || strat === "brrrr") {
      html += row("Sale price (ARV)", fmtMoney(arv));
      html += row("Net profit", `<span class="${moneyClass(net_profit)}"><strong>${fmtMoney(net_profit, true)}</strong></span>`);
      html += row("ROI on cash", `<strong>${roi.toFixed(1)}% (ann. ${annualized.toFixed(0)}%)</strong>`);
    }
    if (strat === "hold" || strat === "brrrr") {
      const m = currentDealCache.metrics;
      html += row("Monthly net cash flow", `<span class="${moneyClass(m.rent.monthly_net)}">${fmtMoney(m.rent.monthly_net, true)}</span>`);
      html += row("Cap rate", `${m.rent.cap_rate.toFixed(1)}%`);
    }
    $("#live-preview-table").innerHTML = html;
    function row(k, v) { return `<tr><td>${k}</td><td>${v}</td></tr>`; }
  }

  $$("input, select", $("#pdf-modal")).forEach(el => {
    el.addEventListener("input", updateLivePreview);
    el.addEventListener("change", updateLivePreview);
  });

  // Bulletproof PDF download — tries pywebview native save dialog first,
  // falls back to browser download via temporary <a> click.
  async function downloadPdfBulletproof(blobUrl, filename) {
    // Path A: pywebview native save dialog (most reliable in WKWebView)
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_pdf_blob) {
      try {
        // Read the blob as base64
        const resp = await fetch(blobUrl);
        const blob = await resp.blob();
        const b64 = await new Promise((resolve, reject) => {
          const r = new FileReader();
          r.onloadend = () => resolve(r.result.split(",", 2)[1]);
          r.onerror = reject;
          r.readAsDataURL(blob);
        });
        const result = await window.pywebview.api.save_pdf_blob(b64, filename);
        if (result.ok) {
          toast(`✓ Saved: ${result.path}`, "success");
        } else if (result.cancelled) {
          toast("Cancelled", "info");
        } else {
          toast("Save failed: " + (result.error || "?"), "error");
        }
        return;
      } catch (e) {
        console.warn("[pdf] native save failed, falling back:", e);
      }
    }
    // Path B: fallback — temporary <a> click (works in browsers, sometimes in WebKit)
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    a.target = "_blank";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 100);
    toast("Download started (check your Downloads folder)", "info");
  }

  $("#pdf-download")?.addEventListener("click", async (e) => {
    e.preventDefault();
    const btn = $("#pdf-download");
    const blobUrl = btn?.dataset.blobUrl;
    const filename = btn?.dataset.filename || "report.pdf";
    if (!blobUrl) {
      toast("Generate the PDF first (the 'Generate PDF' button)", "warn");
      return;
    }
    btn.disabled = true;
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Saving…';
    try {
      await downloadPdfBulletproof(blobUrl, filename);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalHTML;
    }
  });

  $("#modal-generate")?.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    const opts = collectModalOptions();
    $("#modal-generate").disabled = true;
    $("#modal-generate").innerHTML = '<span class="spinner"></span> Generating...';
    try {
      const blobUrl = await API.generatePdf(state.currentDealId, opts);
      $("#pdf-iframe").src = blobUrl;
      // Stash blob URL on the button for the download handler
      const dlBtn = $("#pdf-download");
      if (dlBtn) {
        dlBtn.dataset.blobUrl = blobUrl;
        dlBtn.dataset.filename = `flip-report-${state.currentDealId}.pdf`;
      }
      $("#pdf-preview").style.display = "block";
      closePdfModal();
      $("#pdf-preview").scrollIntoView({ behavior: "smooth", block: "start" });
      toast("PDF generated", "success");
    } catch (e) { toast("PDF failed: " + e.message, "error"); }
    finally {
      $("#modal-generate").disabled = false;
      $("#modal-generate").innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6" stroke-linecap="round"/></svg> Generate PDF`;
    }
  });

  // ============== BROWSER SESSION (ispeedtolead auth) ==============
  async function refreshBrowserSessionStatus() {
    const el = $("#browser-session-status");
    if (!el) return;
    try {
      const s = await API.browserSessionStatus();
      if (s.session_established) {
        el.innerHTML = `<span class="pill green">✓ Connected</span>
          <span class="muted">Profile saved (${s.profile_size_mb} MB) — scrapes will run silently.</span>`;
      } else {
        el.innerHTML = `<span class="pill gray">Not connected</span>
          <span class="muted">Click "Sign in" below to authenticate once.</span>`;
      }
    } catch (e) {
      el.innerHTML = `<span class="pill red">Error</span> ${escape(e.message)}`;
    }
  }

  const authBtn = $("#auth-ispeed-btn");
  if (authBtn) authBtn.addEventListener("click", async () => {
    authBtn.disabled = true;
    authBtn.innerHTML = '<span class="spinner"></span> Opening Chromium…';
    toast("A Chromium window will open. Log in to ispeedtolead, then it'll close automatically.", "success");
    try {
      const r = await API.browserSessionConnect({
        login_url: "https://app.ispeedtolead.com/auth/login",
        success_url_contains: "/my-leads",
      });
      if (r.ok) {
        toast("✓ Signed in — session saved.", "success");
      } else {
        toast(r.error || "Login not completed", "error");
      }
    } catch (e) {
      toast("Auth failed: " + e.message, "error");
    } finally {
      authBtn.disabled = false;
      authBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 012 2v14a2 2 0 01-2 2h-4M10 17l5-5-5-5M15 12H3" stroke-linecap="round" stroke-linejoin="round"/></svg> Sign in to ispeedtolead`;
      refreshBrowserSessionStatus();
    }
  });

  const resetBtn = $("#auth-reset-btn");
  if (resetBtn) resetBtn.addEventListener("click", async () => {
    if (!confirm("Clear the saved browser session? You'll need to sign in again.")) return;
    try {
      await API.browserSessionReset();
      toast("Session cleared", "success");
      refreshBrowserSessionStatus();
    } catch (e) { toast(e.message, "error"); }
  });

  // ============== SETTINGS (cookies) ==============
  async function refreshCookies() {
    try {
      const cookies = await API.listCookies();
      const tbody = $("#cookies-tbody");
      if (!cookies.length) {
        tbody.innerHTML = `<tr><td colspan="3" class="muted" style="padding:14px;">No cookies saved.</td></tr>`;
        return;
      }
      tbody.innerHTML = cookies.map(c => `
        <tr><td><code>${escape(c.domain)}</code></td>
            <td style="font-family:monospace; font-size:11px; color:var(--muted);">${escape(c.preview)}</td>
            <td><button class="btn danger" data-domain="${escape(c.domain)}" style="padding:4px 10px; font-size:11px;">Delete</button></td>
        </tr>`).join("");
      $$("#cookies-tbody button[data-domain]").forEach(b => {
        b.addEventListener("click", async () => {
          if (!confirm("Delete cookie for " + b.dataset.domain + "?")) return;
          try { await API.deleteCookie(b.dataset.domain); toast("Deleted", "success"); refreshCookies(); }
          catch (e) { toast(e.message, "error"); }
        });
      });
    } catch (e) { toast(e.message, "error"); }
  }
  $("#cookie-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await API.saveCookie(fd.get("domain").trim(), fd.get("cookie").trim());
      toast("Cookie saved", "success"); e.target.reset(); refreshCookies();
    } catch (e) { toast(e.message, "error"); }
  });

  async function refreshAiConfig() {
    try {
      const cfg = await API.aiConfig();
      $("#ai-key-input").placeholder = cfg.configured
        ? `Configured (${cfg.key_preview}) — re-enter to update`
        : "sk-ant-...";
      $("#ai-model-input").value = cfg.model || "claude-opus-4-8";
      const ar = $("#ai-auto-research");
      if (ar) {
        ar.checked = cfg.auto_research !== false;
        ar.onchange = async () => {
          try { await API.saveAiConfig({ auto_research: ar.checked }); toast(ar.checked ? "Auto-analysis enabled" : "Auto-analysis disabled", "success"); }
          catch (e) { toast(e.message, "error"); }
        };
      }
      const tm = $("#ai-target-margin");
      if (tm) {
        tm.value = String(cfg.target_margin_pct || 15);
        tm.onchange = async () => {
          try { await API.saveAiConfig({ target_margin_pct: Number(tm.value) }); toast(`Target margin → ${tm.value}% (max offer recomputed)`, "success"); }
          catch (e) { toast(e.message, "error"); }
        };
      }
      // Deal Radar settings
      const re = $("#radar-enabled");
      if (re) {
        re.checked = cfg.radar_enabled !== false;
        re.onchange = async () => {
          try { await API.saveAiConfig({ radar_enabled: re.checked }); toast(re.checked ? "Radar enabled" : "Radar disabled", "success"); }
          catch (e) { toast(e.message, "error"); }
        };
      }
      const ra = $("#radar-auto-add");
      if (ra) {
        ra.checked = cfg.radar_auto_add !== false;
        ra.onchange = async () => {
          try { await API.saveAiConfig({ radar_auto_add: ra.checked }); toast(ra.checked ? "Radar auto-add on" : "Radar auto-add off", "success"); }
          catch (e) { toast(e.message, "error"); }
        };
      }
      const rp = $("#radar-min-profit");
      if (rp) {
        rp.value = cfg.radar_min_profit != null ? cfg.radar_min_profit : 25000;
        rp.onchange = async () => {
          try { await API.saveAiConfig({ radar_min_profit: Number(rp.value) }); toast(`Radar min profit → $${Number(rp.value).toLocaleString("en-US")}`, "success"); }
          catch (e) { toast(e.message, "error"); }
        };
      }
      $("#ai-status").textContent = cfg.configured
        ? `✓ Configured with ${cfg.model || 'claude-opus-4-8'}`
        : "Not configured — AI ARV research is disabled.";
      $("#ai-status").className = cfg.configured ? "status-line success" : "status-line";
      // ScraperAPI proxy status (Zillow photos)
      const pk = $("#proxy-key-input");
      const ps = $("#proxy-status");
      if (pk) pk.placeholder = cfg.proxy_configured
        ? `Configured (${cfg.proxy_key_preview}) — re-enter to update`
        : "ScraperAPI key…";
      if (ps) {
        ps.textContent = cfg.proxy_configured
          ? "✓ Automatic Zillow photos enabled — paste a URL and all photos come in."
          : "Without a key: Zillow blocks scraping, only the address is retrieved (+ Street View if a Maps key is set).";
        ps.className = cfg.proxy_configured ? "status-line success" : "status-line";
      }
      // Maps key status
      const mk = $("#maps-key-input");
      const ms = $("#maps-status");
      if (mk) mk.placeholder = cfg.maps_configured
        ? `Configured (${cfg.maps_key_preview}) — re-enter to update`
        : "AIzaSy…";
      if (ms) {
        ms.textContent = cfg.maps_configured
          ? "✓ Street View enabled — exterior photo when a listing is blocked."
          : "Optional — without a key, no fallback photo.";
        ms.className = cfg.maps_configured ? "status-line success" : "status-line";
      }
    } catch {}
  }
  $("#proxy-key-save")?.addEventListener("click", async () => {
    const key = ($("#proxy-key-input")?.value || "").trim();
    if (!key) { toast("Paste your ScraperAPI key", "warn"); return; }
    try {
      await API.saveAiConfig({ scraper_api_key: key });
      toast("ScraperAPI key saved — automatic Zillow photos enabled", "success");
      $("#proxy-key-input").value = "";
      refreshAiConfig();
    } catch (err) { toast(err.message, "error"); }
  });
  $("#maps-key-save")?.addEventListener("click", async () => {
    const key = ($("#maps-key-input")?.value || "").trim();
    if (!key) { toast("Paste a Google Maps key", "warn"); return; }
    try {
      await API.saveAiConfig({ google_maps_key: key });
      toast("Google Maps key saved — Street View enabled", "success");
      $("#maps-key-input").value = "";
      refreshAiConfig();
    } catch (err) { toast(err.message, "error"); }
  });
  $("#ai-config-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const key = (fd.get("anthropic_api_key") || "").trim();
    const model = fd.get("model") || "claude-opus-4-8";
    try {
      const body = { model };
      if (key) body.anthropic_api_key = key;
      await API.saveAiConfig(body);
      toast("AI config saved", "success");
      $("#ai-key-input").value = "";
      refreshAiConfig();
    } catch (err) { toast(err.message, "error"); }
  });

  // ============== COMMAND PALETTE (Cmd+K) ==============
  let cmdkIdx = 0, cmdkItems = [];
  function openCmdk() {
    $("#cmdk").style.display = "flex";
    $("#cmdk-input").value = "";
    $("#cmdk-input").focus();
    refreshCmdk("");
  }
  function closeCmdk() { $("#cmdk").style.display = "none"; }
  $(".cmdk-backdrop", $("#cmdk"))?.addEventListener("click", closeCmdk);

  const CMDK_COMMANDS = [
    { id: "go-dashboard", label: "Go to Dashboard", sub: "G D", action: () => showView("dashboard") },
    { id: "go-deals", label: "Go to Deals", sub: "G L", action: () => showView("deals") },
    { id: "go-add", label: "Add new deal", sub: "⌘N", action: () => { resetDealForm(); showView("add"); } },
    { id: "go-settings", label: "Settings", sub: "", action: () => showView("settings") },
    { id: "theme", label: "Toggle dark mode", sub: "⌘⇧L", action: () => applyTheme(state.theme === "dark" ? "light" : "dark") },
  ];

  async function refreshCmdk(q) {
    const ql = q.toLowerCase().trim();
    const items = [];
    // If query looks like a URL, offer scrape
    if (ql.startsWith("http")) {
      items.push({
        label: "Fetch URL → " + q.slice(0, 50) + (q.length > 50 ? "…" : ""),
        sub: "Enter",
        action: () => { showView("add"); $("#scrape-url").value = q; tryScrape(q); closeCmdk(); }
      });
    }
    CMDK_COMMANDS.forEach(c => {
      if (!ql || c.label.toLowerCase().includes(ql)) items.push(c);
    });
    // Match deals
    if (state.deals.length) {
      state.deals.forEach(d => {
        if (!ql || d.address.toLowerCase().includes(ql) || (d.city || "").toLowerCase().includes(ql)) {
          items.push({
            label: d.address, sub: `${d.score}/100 ${d.signal}`,
            action: () => { closeCmdk(); openDeal(d.id); },
          });
        }
      });
    }
    cmdkItems = items.slice(0, 12);
    cmdkIdx = 0;
    renderCmdk();
  }

  function renderCmdk() {
    $("#cmdk-list").innerHTML = cmdkItems.map((it, i) => `
      <div class="cmdk-item ${i === cmdkIdx ? 'active' : ''}" data-idx="${i}">
        <svg class="item-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7" stroke-linecap="round"/></svg>
        <span>${escape(it.label)}</span>
        <span class="item-sub">${escape(it.sub || '')}</span>
      </div>
    `).join("");
    $$(".cmdk-item").forEach(el => {
      el.addEventListener("click", () => cmdkItems[Number(el.dataset.idx)]?.action());
    });
  }

  $("#cmdk-input")?.addEventListener("input", e => refreshCmdk(e.target.value));
  $("#cmdk-input")?.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); cmdkIdx = Math.min(cmdkIdx + 1, cmdkItems.length - 1); renderCmdk(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cmdkIdx = Math.max(0, cmdkIdx - 1); renderCmdk(); }
    else if (e.key === "Enter") { e.preventDefault(); cmdkItems[cmdkIdx]?.action(); }
    else if (e.key === "Escape") closeCmdk();
  });

  // ============== KEYBOARD SHORTCUTS ==============
  let gKeyTime = 0;
  document.addEventListener("keydown", e => {
    // Skip if typing in input
    const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);

    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault(); openCmdk(); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "n" && !inField) {
      e.preventDefault(); resetDealForm(); showView("add"); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === "L" || e.key === "l")) {
      e.preventDefault(); applyTheme(state.theme === "dark" ? "light" : "dark"); return;
    }
    if (e.key === "Escape") {
      if ($("#pdf-modal").style.display === "flex") closePdfModal();
      else if ($("#cmdk").style.display === "flex") closeCmdk();
      else if ($("#lightbox").style.display === "flex") closeLightbox();
    }
    // Arrow keys navigate the lightbox gallery
    if ($("#lightbox")?.style.display === "flex") {
      if (e.key === "ArrowLeft") { e.preventDefault(); lightboxGo(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); lightboxGo(1); }
    }
    // G then [D|L|A|C|S]
    if (!inField && e.key === "g") {
      gKeyTime = Date.now();
    } else if (!inField && Date.now() - gKeyTime < 1000) {
      const map = { d: "dashboard", l: "deals", a: "add", s: "settings" };
      const target = map[e.key.toLowerCase()];
      if (target) {
        e.preventDefault();
        if (target === "add") resetDealForm();
        showView(target); gKeyTime = 0;
      }
    }
  });

  // ============== DRAG & DROP URL + FILE ==============
  let dragCount = 0;

  // Detect if a drag carries Files (vs a URL/text). When dragging Files we
  // do NOT show the URL overlay — that overlay would cover the PDF drop zone
  // and steal the drop event.
  function _dragHasFiles(e) {
    const types = e.dataTransfer?.types || [];
    return Array.from(types).some(t => t === "Files" || t === "application/x-moz-file");
  }

  document.addEventListener("dragenter", e => {
    e.preventDefault();
    if (_dragHasFiles(e)) return;  // let file drops reach the targeted dropzone
    if (++dragCount === 1) $("#dropzone").style.display = "flex";
  });
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("dragleave", e => {
    if (_dragHasFiles(e)) return;
    if (--dragCount <= 0) { dragCount = 0; $("#dropzone").style.display = "none"; }
  });
  document.addEventListener("drop", async e => {
    // If a child dropzone (PDF importer) already handled this drop, do nothing.
    // The child calls stopPropagation OR sets a flag — easier: check if files
    // landed on a known dropzone target.
    const isPdfZoneTarget = e.target && (
      e.target.id === "pdf-drop-zone" ||
      e.target.closest?.("#pdf-drop-zone")
    );
    if (isPdfZoneTarget) {
      // Local handler already runs. Just clear overlay state and bail.
      e.preventDefault();
      dragCount = 0;
      $("#dropzone").style.display = "none";
      return;
    }

    e.preventDefault();
    dragCount = 0;
    $("#dropzone").style.display = "none";

    // 1) File drop anywhere else on the page → if it's a PDF, route to importer
    const file = e.dataTransfer?.files?.[0];
    if (file) {
      if (file.name.toLowerCase().endsWith(".pdf") ||
          file.type === "application/pdf") {
        showView("add");
        // Wait for the view switch, then scroll to PDF importer + start
        setTimeout(() => {
          const zone = $("#pdf-drop-zone");
          if (zone) zone.scrollIntoView({behavior: "smooth", block: "center"});
          handlePdfFile(file);
        }, 200);
      } else {
        toast(`Unsupported file: "${file.name}". Only PDFs are accepted.`, "warn");
      }
      return;
    }

    // 2) URL drop → route everything to Add Deal (Leads view is gone)
    const url = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
    if (url && url.startsWith("http")) {
      showView("add");
      $("#scrape-url").value = url;
      tryScrape(url);
    }
  });

  // ============== SKIP-TRACE QUEUE (Auctions) ==============
  const STAGE_META = {
    queued:    { icon: "📥", label: "Queued",     desc: "Just imported" },
    tracing:   { icon: "🔍", label: "Tracing",    desc: "Being skip-traced" },
    traced:    { icon: "✓",  label: "Traced",     desc: "Owner info added" },
    contacted: { icon: "📞", label: "Contacted",  desc: "Reached out" },
    won:       { icon: "🏆", label: "Won",        desc: "Under contract" },
    lost:      { icon: "❌", label: "Lost",       desc: "Outbid / fell through" },
    passed:    { icon: "⏭",  label: "Passed",     desc: "Skipped" },
  };
  let skipTraceCurrentStage = null;  // null = all

  // ============== USA MAP VIEW ==============
  let _usamapData = null;
  let _usamapMetric = "market_score";

  async function refreshUsaMapView() {
    try {
      _usamapData = await API.statesMap();
      renderUsaMapStats(_usamapData);
      renderUsaMapMain();
      renderUsaMapRanking();
    } catch (e) { toast("USA Map error: " + e.message, "error"); }
  }

  function renderUsaMapStats(data) {
    const grid = $("#usamap-stats");
    if (!grid) return;
    const states = data.states || [];
    const topState = states[0];
    const stateWithDeals = states.filter(s => s.my_deals_count > 0);
    grid.innerHTML = `
      <div class="stat-card">
        <div class="stat-label">Total deals (board)</div>
        <div class="stat-value">${data.total_deals || 0}</div>
        <div class="stat-sub">${data.total_states_with_deals} states covered</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Top market potential</div>
        <div class="stat-value">${topState?.code || '?'}</div>
        <div class="stat-sub">${topState?.name || ''} · ${topState?.market_grade || ''}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total profit potential</div>
        <div class="stat-value ${(data.total_profit_potential >= 0) ? 'good' : 'bad'}">
          ${data.total_profit_potential >= 0 ? '+' : '-'}$${Math.abs(Math.round(data.total_profit_potential / 1000))}K
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label">#1 state (your deals)</div>
        <div class="stat-value">${
          stateWithDeals.sort((a,b) => (b.my_avg_score||0) - (a.my_avg_score||0))[0]?.code || '—'
        }</div>
        <div class="stat-sub">by average score</div>
      </div>
    `;
  }

  function renderUsaMapMain() {
    const container = $("#usamap-svg-container");
    if (!container || !_usamapData) return;
    window.renderUsaMap(container, _usamapData.states, _usamapMetric, (state) => {
      renderUsaMapStateDetail(state);
    });
    renderUsaMapLegend();
  }

  function renderUsaMapLegend() {
    const el = $("#usamap-legend");
    if (!el) return;
    const labels = {
      market_score:    ["Low (15)", "Excellent (95)"],
      yoy_pct:         ["-3%", "+6%"],
      median_price:    ["$160K", "$820K"],
      my_deals_count:  ["0", `${Math.max(...(_usamapData.states.map(s=>s.my_deals_count)))} deals`],
      my_total_profit: ["Loss", "Max profit"],
    };
    const [lo, hi] = labels[_usamapMetric] || ["Low", "High"];
    el.innerHTML = `
      <span class="usamap-legend-label">${escape(lo)}</span>
      <div class="usamap-legend-bar"></div>
      <span class="usamap-legend-label">${escape(hi)}</span>
      <span style="margin-left:auto; color:var(--muted); font-size:11px;">
        ⚪ dot = number of your deals in the state
      </span>
    `;
  }

  function renderUsaMapStateDetail(s) {
    const wrap = $("#usamap-state-info");
    if (!wrap) return;
    const gradeClass = "grade-" + (s.market_grade || "?").replace("+", "plus").replace("-", "minus");
    const yoyClass = (s.yoy_pct >= 0) ? "good" : "bad";
    let html = `
      <div class="usamap-state-card">
        <h4>
          <span class="state-code">${escape(s.code)}</span>
          ${escape(s.name)}
        </h4>
        <div class="usamap-state-row">
          <span class="label">Region</span>
          <span class="val">${escape(s.region || '?')}</span>
        </div>
        <div class="usamap-state-row">
          <span class="label">Market grade</span>
          <span class="val"><span class="grade-pill ${gradeClass}">${escape(s.market_grade)}</span> (${s.market_score}/100)</span>
        </div>
        <div class="usamap-state-row">
          <span class="label">Median price (2026)</span>
          <span class="val">$${(s.median_price/1000).toFixed(0)}K</span>
        </div>
        <div class="usamap-state-row">
          <span class="label">YoY growth</span>
          <span class="val ${yoyClass}">${s.yoy_pct >= 0 ? '+' : ''}${s.yoy_pct.toFixed(1)}%</span>
        </div>
        <div class="usamap-state-row">
          <span class="label">National rank</span>
          <span class="val">#${s.rank} of 51</span>
        </div>
      </div>
    `;
    if (s.my_deals_count > 0) {
      html += `
        <div class="usamap-state-card">
          <h4>📍 Your deals in this state (${s.my_deals_count})</h4>
          <div class="usamap-state-row">
            <span class="label">Average score</span>
            <span class="val">${s.my_avg_score || '?'}/100</span>
          </div>
          <div class="usamap-state-row">
            <span class="label">Total profit potential</span>
            <span class="val ${(s.my_total_profit >= 0) ? 'good' : 'bad'}">
              ${s.my_total_profit >= 0 ? '+$' : '-$'}${Math.abs(Math.round(s.my_total_profit/1000))}K
            </span>
          </div>
          <div class="usamap-state-deals">
            <h5>Your deals</h5>
            ${(s.my_deals || []).map(d => `
              <div class="usamap-mini-deal" data-id="${escape(d.id)}">
                <div class="mini-score" style="background:${scoreColor(d.score)}; color:white;">${d.score ?? '?'}</div>
                <div style="flex:1; min-width:0;">
                  <div style="font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escape(d.address || d.id)}</div>
                  <div class="muted" style="font-size:10.5px;">${escape(d.city || '')} · ${d.net_profit >= 0 ? '+$' : '-$'}${Math.abs(Math.round((d.net_profit||0)/1000))}K</div>
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    } else {
      html += `
        <div class="usamap-state-card" style="background: rgba(245,158,11,0.08); border-left: 3px solid #f59e0b;">
          <strong style="font-size:13px;">🎯 No deals in this state</strong>
          <p class="muted" style="font-size:12px; margin-top:4px;">
            ${s.market_grade && s.market_grade[0] === 'A'
              ? `Excellent market — explore it! Median ${'$'+(s.median_price/1000).toFixed(0)}K, YoY ${s.yoy_pct >= 0 ? '+' : ''}${s.yoy_pct.toFixed(1)}%`
              : `Average market — other states are more promising.`}
          </p>
        </div>
      `;
    }
    wrap.innerHTML = html;
    // Wire deal clicks
    wrap.querySelectorAll(".usamap-mini-deal").forEach(el => {
      el.addEventListener("click", () => openDeal(el.dataset.id));
    });
  }

  function renderUsaMapRanking() {
    const tbody = $("#usamap-ranking-tbody");
    if (!tbody || !_usamapData) return;
    const top = _usamapData.states.slice(0, 20);
    tbody.innerHTML = top.map(s => {
      const rankClass = s.rank <= 3 ? `rank-${s.rank}` : "";
      const gradeClass = "grade-" + (s.market_grade || "?").replace("+", "plus").replace("-", "minus");
      const yoyClass = (s.yoy_pct >= 0) ? "good" : "bad";
      const profitClass = (s.my_total_profit >= 0) ? "good" : "bad";
      return `
        <tr data-code="${escape(s.code)}">
          <td><span class="rank-badge ${rankClass}">${s.rank}</span></td>
          <td><strong>${escape(s.name)}</strong> <span class="muted" style="font-family:monospace;">${escape(s.code)}</span></td>
          <td><span class="muted">${escape(s.region || '?')}</span></td>
          <td><span class="grade-pill ${gradeClass}">${escape(s.market_grade)}</span></td>
          <td>$${(s.median_price/1000).toFixed(0)}K</td>
          <td class="${yoyClass}">${s.yoy_pct >= 0 ? '+' : ''}${s.yoy_pct.toFixed(1)}%</td>
          <td>${s.my_deals_count || '—'}</td>
          <td class="${profitClass}">${s.my_total_profit ? (s.my_total_profit >= 0 ? '+$' : '-$') + Math.abs(Math.round(s.my_total_profit/1000)) + 'K' : '—'}</td>
        </tr>
      `;
    }).join("");
    tbody.querySelectorAll("tr").forEach(row => {
      row.addEventListener("click", () => {
        const code = row.dataset.code;
        const state = (_usamapData.states || []).find(x => x.code === code);
        if (state) {
          renderUsaMapStateDetail(state);
          // Highlight on map
          const tile = document.querySelector(`#usamap-svg-container [data-code="${code}"]`);
          if (tile) {
            document.querySelectorAll("#usamap-svg-container .usamap-state.active").forEach(e => e.classList.remove("active"));
            tile.classList.add("active");
            tile.scrollIntoView({behavior: "smooth", block: "center"});
          }
        }
      });
    });
  }

  // Wire metric selector
  $("#usamap-metric")?.addEventListener("change", e => {
    _usamapMetric = e.target.value;
    renderUsaMapMain();
  });

  async function refreshSkipTraceView() {
    try {
      const [stages, items] = await Promise.all([
        API.auctionsStages(),
        API.auctionsList(skipTraceCurrentStage),
      ]);
      renderSkipTraceStats(stages);
      renderStageFilter(stages);
      renderSkipTraceList(items);
      renderCredentialsList();
      const badge = $("#nav-skiptrace-count");
      if (badge) badge.textContent = stages.aggregates.total || "";
    } catch (e) { toast(e.message, "error"); }
  }

  async function renderCredentialsList() {
    const wrap = $("#creds-list");
    if (!wrap) return;
    try {
      const creds = await API.auctionCredsList();
      if (!creds.length) {
        wrap.innerHTML = `<div class="muted" style="font-size:11px; padding:6px 0;">
          No credentials saved. The scraper will use the public preview (slower, fewer items).
        </div>`;
        return;
      }
      wrap.innerHTML = creds.map(c => `
        <div style="display:flex; align-items:center; gap:8px; padding:6px 10px; background:var(--surface-2); border-radius:6px; margin-bottom:4px;">
          <span style="flex:1; font-size:12px;">
            🔓 <strong>${escape(c.domain)}</strong>
            <span class="muted" style="margin-left:8px;">${escape(c.username)}</span>
          </span>
          <button class="btn ghost" data-act="del" data-domain="${escape(c.domain)}" style="font-size:11px; padding:3px 8px;">Remove</button>
        </div>
      `).join("");
      wrap.querySelectorAll('[data-act="del"]').forEach(btn => {
        btn.addEventListener("click", async () => {
          if (!confirm("Remove these credentials?")) return;
          try {
            await API.auctionCredsDelete(btn.dataset.domain);
            toast("Removed", "success");
            renderCredentialsList();
          } catch (e) { toast(e.message, "error"); }
        });
      });
    } catch (e) {
      wrap.innerHTML = `<div class="muted" style="font-size:11px; color:var(--red);">Failed to load credentials.</div>`;
    }
  }

  // Wire up the save-credentials button
  {
    const saveBtn = $("#cred-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", async () => {
      const domain = $("#cred-domain")?.value?.trim();
      const username = $("#cred-username")?.value?.trim();
      const password = $("#cred-password")?.value;
      if (!domain || !username || !password) {
        toast("Fill domain, username, and password", "warn");
        return;
      }
      saveBtn.disabled = true;
      try {
        await API.auctionCredsSave(domain, username, password);
        toast(`✓ Saved login for ${domain}`, "success");
        $("#cred-domain").value = "";
        $("#cred-username").value = "";
        $("#cred-password").value = "";
        renderCredentialsList();
      } catch (e) {
        toast(e.message, "error");
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  function renderSkipTraceStats(stages) {
    const grid = $("#skiptrace-stats");
    const agg = stages.aggregates;
    if (!agg.total) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1;">
        <div class="empty-ico">🔍</div>
        <h3>No auctions imported yet</h3>
        <p>Paste a county auction URL above to import the daily list.</p>
      </div>`;
      return;
    }
    const cards = [
      { lbl: "Total", val: agg.total },
      { lbl: "Queued", val: agg.by_status.queued || 0, cls: "" },
      { lbl: "Tracing", val: agg.by_status.tracing || 0, cls: "" },
      { lbl: "Traced", val: agg.by_status.traced || 0, cls: "green" },
      { lbl: "Contacted", val: agg.by_status.contacted || 0, cls: "" },
      { lbl: "Won", val: agg.by_status.won || 0, cls: "green" },
    ];
    grid.innerHTML = cards.map(c => `
      <div class="stat-card ${c.cls || ''}">
        <div class="label">${escape(c.lbl)}</div>
        <div class="value">${c.val}</div>
      </div>
    `).join("");
  }

  function renderStageFilter(stages) {
    const wrap = $("#stage-filter");
    const agg = stages.aggregates;
    const allCount = agg.total;
    let html = `<button class="stage-chip ${!skipTraceCurrentStage ? 'active' : ''}" data-stage="">All <span class="count">${allCount}</span></button>`;
    for (const s of stages.stages) {
      const meta = STAGE_META[s];
      if (!meta) continue;
      const count = agg.by_status[s] || 0;
      html += `<button class="stage-chip ${skipTraceCurrentStage === s ? 'active' : ''}" data-stage="${escape(s)}">
        ${meta.icon} ${escape(meta.label)} <span class="count">${count}</span>
      </button>`;
    }
    wrap.innerHTML = html;
    $$("button[data-stage]", wrap).forEach(b => {
      b.addEventListener("click", () => {
        skipTraceCurrentStage = b.dataset.stage || null;
        refreshSkipTraceView();
      });
    });
  }

  function renderSkipTraceList(items) {
    const wrap = $("#skiptrace-list");
    if (!items.length) {
      wrap.innerHTML = `<div class="card empty">
        <div class="empty-ico">📭</div>
        <h3>No items in this stage</h3>
        <p>${skipTraceCurrentStage ? 'Try a different stage filter' : 'Import an auction list to get started'}</p>
      </div>`;
      return;
    }
    wrap.innerHTML = items.map((it, idx) => {
      const stage = it.status || "queued";
      const meta = STAGE_META[stage] || STAGE_META.queued;
      const typeLabel = ({
        tax_deed: "Tax deed", mortgage_foreclosure: "Foreclosure",
        hoa_foreclosure: "HOA foreclosure",
      })[it.auction_type] || it.auction_type || "";
      return `
        <div class="auction-card" data-id="${escape(it.id)}" style="animation-delay:${Math.min(idx * 30, 600)}ms;">
          <div>
            <div class="auction-header">
              <div class="auction-title">${escape(it.address || it.case_number || 'Untitled auction')}</div>
              <span class="auction-stage-pill ${escape(stage)}">${meta.icon} ${escape(meta.label)}</span>
              ${typeLabel ? `<span class="pill gray">${escape(typeLabel)}</span>` : ''}
              ${it.auction_status && it.auction_status !== 'Active' ? `<span class="pill yellow">${escape(it.auction_status)}</span>` : ''}
            </div>
            <div class="auction-meta">
              ${it.case_number ? `<span>Case: <strong>${escape(it.case_number)}</strong></span>` : ''}
              ${it.parcel_id ? `<span>Parcel: <strong>${escape(it.parcel_id)}</strong></span>` : ''}
              ${it.opening_bid ? `<span>Opening bid: <strong>${fmtMoney(it.opening_bid)}</strong></span>` : ''}
              ${it.auction_date ? `<span>📅 ${escape(it.auction_date)} ${escape(it.auction_time || '')}</span>` : ''}
              ${it.owner_name ? `<span>Owner: <strong>${escape(it.owner_name)}</strong></span>` : ''}
              ${it.owner_phone ? `<span>📞 ${escape(it.owner_phone)}</span>` : ''}
            </div>
          </div>
          <div class="auction-actions">
            <button class="btn primary" data-act="skip-trace" title="Use Claude + web search to find owner contact info">
              ${stage === "tracing" ? '⏳ Tracing…' : '🔎 Skip-trace'}
            </button>
            ${(it.owner_phone || it.owner_email || it.skip_trace) ?
              `<button class="btn" data-act="view-trace">📇 View trace</button>` : ''}
            <button class="btn ghost" data-act="edit" style="font-size:11px;">Edit</button>
            ${(stage === "traced" || stage === "contacted") ?
              `<button class="btn primary" data-act="to-lead">→ Lead</button>` : ''}
          </div>
        </div>
      `;
    }).join("");

    $$(".auction-card", wrap).forEach(card => {
      const id = card.dataset.id;
      $$("[data-act]", card).forEach(b => {
        b.addEventListener("click", e => {
          e.stopPropagation();
          handleSkipTraceAction(b.dataset.act, id);
        });
      });
    });
  }

  async function handleSkipTraceAction(action, id) {
    if (action === "edit") {
      try {
        const item = await API.auctionsGet(id);
        openSkipTraceModal(item);
      } catch (e) { toast(e.message, "error"); }
    } else if (action === "to-lead") {
      if (!confirm("Promote this auction item to a lead? Owner info from skip-trace will be carried over.")) return;
      try {
        const r = await API.auctionsToLead(id);
        toast("Promoted to lead", "success");
        showView("leads");
      } catch (e) { toast(e.message, "error"); }
    } else if (action === "skip-trace") {
      // One-shot AI skip-trace via Claude + web search
      const card = document.querySelector(`.auction-card[data-id="${id}"]`);
      const btn = card?.querySelector('[data-act="skip-trace"]');
      if (btn) { btn.disabled = true; btn.textContent = "⏳ Tracing…"; }
      toast("🔎 Asking Claude to find owner contact…", "info");
      try {
        const r = await API.auctionsSkipTrace(id);
        if (r.trace?.owner_phone || r.trace?.owner_email) {
          toast(`✓ Found ${r.trace.owner_phone ? "phone" : "email"} for ${r.item.address || 'this owner'}`, "success");
        } else {
          toast("Trace done — no contact found. See details.", "warn");
        }
        await refreshSkipTraceView();
        showTraceResultModal(r.item, r.trace);
      } catch (e) {
        toast("Trace failed: " + e.message, "error");
        if (btn) { btn.disabled = false; btn.textContent = "🔎 Skip-trace"; }
      }
    } else if (action === "view-trace") {
      try {
        const item = await API.auctionsGet(id);
        showTraceResultModal(item, item.skip_trace || null);
      } catch (e) { toast(e.message, "error"); }
    }
  }

  // ---- Display the full skip-trace result in a modal ----
  function showTraceResultModal(item, trace) {
    const modal = $("#trace-result-modal");
    if (!modal) return;
    $("#trace-result-title").textContent =
      `Skip-trace: ${item.address || item.case_number || 'item'}`;
    const phones = trace?.phones || [];
    const emails = trace?.emails || [];
    const humans = trace?.owner_humans || [];
    const warnings = trace?.warnings || [];
    const conf = trace?.confidence_overall || trace?.confidence || "Unknown";
    const confColor = ({HIGH:"green", MEDIUM:"yellow", LOW:"red"})[conf] || "gray";

    $("#trace-result-body").innerHTML = `
      <div style="display:flex; flex-direction:column; gap:14px; padding-top:6px;">
        <div style="background:var(--surface-2); padding:12px; border-radius:8px;">
          <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <div><strong style="font-size:14px;">${escape(item.owner_name || trace?.owner_name || 'Unknown owner')}</strong></div>
            <span class="pill ${confColor}">${escape(conf)} confidence</span>
            ${trace?.owner_type ? `<span class="pill gray">${escape(trace.owner_type)}</span>` : ''}
          </div>
          ${trace?.mailing_address ? `<div style="font-size:12px; color:var(--muted); margin-top:6px;">📬 Mailing: ${escape(trace.mailing_address)}</div>` : ''}
        </div>

        ${phones.length ? `
          <div>
            <div style="font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:6px;">📞 Phones</div>
            ${phones.map(p => `
              <div style="display:flex; gap:10px; align-items:center; padding:8px; background:var(--surface-2); border-radius:6px; margin-bottom:4px;">
                <a href="tel:${escape(p.number)}" style="font-weight:600; color:var(--text);">${escape(p.number)}</a>
                <span class="pill gray" style="font-size:10px;">${escape(p.type || 'phone')}</span>
                <span class="pill ${({HIGH:'green',MEDIUM:'yellow',LOW:'red'})[p.confidence]||'gray'}" style="font-size:10px;">${escape(p.confidence || '?')}</span>
                ${p.source ? `<a href="${escape(p.source)}" target="_blank" style="font-size:11px; margin-left:auto; color:var(--muted);">source ↗</a>` : ''}
              </div>
            `).join('')}
          </div>` : '<div class="muted" style="font-size:12px;">No phone numbers found.</div>'}

        ${emails.length ? `
          <div>
            <div style="font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:6px;">✉ Emails</div>
            ${emails.map(e => `
              <div style="display:flex; gap:10px; align-items:center; padding:8px; background:var(--surface-2); border-radius:6px; margin-bottom:4px;">
                <a href="mailto:${escape(e.email)}" style="font-weight:600;">${escape(e.email)}</a>
                <span class="pill ${({HIGH:'green',MEDIUM:'yellow',LOW:'red'})[e.confidence]||'gray'}" style="font-size:10px;">${escape(e.confidence || '?')}</span>
                ${e.source ? `<a href="${escape(e.source)}" target="_blank" style="font-size:11px; margin-left:auto; color:var(--muted);">source ↗</a>` : ''}
              </div>
            `).join('')}
          </div>` : ''}

        ${humans.length ? `
          <div>
            <div style="font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:6px;">👥 People associated with this property/LLC</div>
            ${humans.map(h => `
              <div style="padding:6px 8px; background:var(--surface-2); border-radius:6px; margin-bottom:4px; font-size:12px;">
                <strong>${escape(h.name)}</strong>
                ${h.role ? `<span class="pill gray" style="font-size:10px; margin-left:6px;">${escape(h.role)}</span>` : ''}
              </div>
            `).join('')}
          </div>` : ''}

        ${trace?.notes ? `
          <div>
            <div style="font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:6px;">Notes</div>
            <div style="font-size:13px; line-height:1.5;">${escape(trace.notes)}</div>
          </div>` : ''}

        ${warnings.length ? `
          <div style="background:#fef3c7; color:#78350f; padding:10px; border-radius:6px; border:1px solid #fbbf24;">
            <strong style="font-size:11px; text-transform:uppercase;">⚠ Warnings</strong>
            <ul style="margin:4px 0 0 16px; padding:0; font-size:12px;">
              ${warnings.map(w => `<li>${escape(w)}</li>`).join('')}
            </ul>
          </div>` : ''}

        ${trace?.web_searches_used ? `<div style="font-size:11px; color:var(--muted); text-align:right;">${trace.web_searches_used} web searches · ${escape(trace.model || '')}</div>` : ''}
      </div>
    `;
    modal.style.display = "flex";
  }

  // Modal close handlers (script loads at end of body — DOM is ready)
  {
    const closeBtn = $("#trace-result-close");
    if (closeBtn) closeBtn.addEventListener("click", () => $("#trace-result-modal").style.display = "none");
    const traceBackdrop = document.querySelector("#trace-result-modal .modal-backdrop");
    if (traceBackdrop) traceBackdrop.addEventListener("click", () => $("#trace-result-modal").style.display = "none");
  }

  // ---- Bulk skip-trace controls ----
  let bulkTraceJob = null;
  let bulkTracePoll = null;

  async function startBulkTrace() {
    const status = $("#bulk-trace-status")?.value || "queued";
    const btn = $("#bulk-trace-btn");
    if (btn) btn.disabled = true;
    try {
      const r = await API.auctionsSkipTraceBulk(status);
      if (!r.job_id) {
        toast(r.message || "No items to trace", "warn");
        if (btn) btn.disabled = false;
        return;
      }
      bulkTraceJob = r.job_id;
      $("#bulk-trace-progress").style.display = "block";
      $("#bulk-trace-total").textContent = r.total;
      $("#bulk-trace-done").textContent = "0";
      $("#bulk-trace-found").textContent = "0";
      $("#bulk-trace-failed").textContent = "0";
      $("#bulk-trace-bar").style.width = "0%";
      $("#bulk-trace-current").textContent = "Starting…";
      toast(`🔎 Tracing ${r.total} items…`, "info");
      pollBulkTrace();
    } catch (e) {
      toast(e.message, "error");
      if (btn) btn.disabled = false;
    }
  }

  async function pollBulkTrace() {
    if (!bulkTraceJob) return;
    try {
      const j = await API.auctionsSkipTraceStatus(bulkTraceJob);
      $("#bulk-trace-done").textContent = j.done;
      $("#bulk-trace-found").textContent = j.found;
      $("#bulk-trace-failed").textContent = j.failed;
      $("#bulk-trace-bar").style.width = ((j.done / Math.max(1, j.total)) * 100) + "%";
      $("#bulk-trace-current").textContent = j.current ?
        `🔎 ${j.current.slice(0, 60)}` : "Working…";

      if (j.status === "done" || j.status === "cancelled") {
        toast(`✓ Bulk trace finished: ${j.found} contacts found from ${j.total} items`,
              j.status === "done" ? "success" : "warn");
        bulkTraceJob = null;
        if (bulkTracePoll) { clearTimeout(bulkTracePoll); bulkTracePoll = null; }
        const btn = $("#bulk-trace-btn");
        if (btn) btn.disabled = false;
        refreshSkipTraceView();
        return;
      }
      // Refresh the list every 5 polls
      if (j.done > 0 && j.done % 5 === 0) refreshSkipTraceView();
      bulkTracePoll = setTimeout(pollBulkTrace, 2000);
    } catch (e) {
      console.error("poll err", e);
      bulkTracePoll = setTimeout(pollBulkTrace, 5000);
    }
  }

  async function cancelBulkTrace() {
    if (!bulkTraceJob) return;
    try {
      await API.auctionsSkipTraceCancel(bulkTraceJob);
      toast("Cancelling…", "info");
    } catch (e) { toast(e.message, "error"); }
  }

  // Wire up bulk trace buttons
  {
    const startBtn = $("#bulk-trace-btn");
    if (startBtn) startBtn.addEventListener("click", startBulkTrace);
    const cancelBtn = $("#bulk-trace-cancel");
    if (cancelBtn) cancelBtn.addEventListener("click", cancelBulkTrace);
  }

  // ---- Skip-trace modal ----
  function openSkipTraceModal(item) {
    const m = $("#skiptrace-modal");
    const form = $("#skiptrace-form");
    form.reset();
    $("#skiptrace-modal-title").textContent = item ? "Edit auction item" : "New item";
    if (item) {
      Object.entries(item).forEach(([k, v]) => {
        const el = form.elements[k];
        if (el && !Array.isArray(v) && typeof v !== "object") el.value = v;
      });
      form.dataset.editing = item.id;
    } else { delete form.dataset.editing; }
    m.style.display = "flex";
  }
  $("#skiptrace-modal-close")?.addEventListener("click", () => $("#skiptrace-modal").style.display = "none");
  $("#skiptrace-modal-cancel")?.addEventListener("click", () => $("#skiptrace-modal").style.display = "none");
  $(".modal-backdrop", $("#skiptrace-modal"))?.addEventListener("click", () => $("#skiptrace-modal").style.display = "none");

  $("#skiptrace-modal-delete")?.addEventListener("click", async () => {
    const id = $("#skiptrace-form").dataset.editing;
    if (!id) return;
    if (!confirm("Delete this auction item?")) return;
    try {
      await API.auctionsDelete(id);
      toast("Deleted", "success");
      $("#skiptrace-modal").style.display = "none";
      refreshSkipTraceView();
    } catch (e) { toast(e.message, "error"); }
  });

  $("#skiptrace-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const obj = {};
    fd.forEach((v, k) => { if (v) obj[k] = v; });
    if (obj.opening_bid) obj.opening_bid = Number(obj.opening_bid);
    const id = e.target.dataset.editing;
    try {
      if (id) await API.auctionsPatch(id, obj);
      else await API.auctionsCreate(obj);
      toast("Saved", "success");
      $("#skiptrace-modal").style.display = "none";
      refreshSkipTraceView();
    } catch (e) { toast(e.message, "error"); }
  });

  // ---- Tab switching ----
  $$(".tab-btn[data-tab]").forEach(b => {
    b.addEventListener("click", () => {
      const tab = b.dataset.tab;
      $$(".tab-btn[data-tab]").forEach(x => x.classList.toggle("active", x.dataset.tab === tab));
      $$(".auction-tab").forEach(t => t.style.display = (t.dataset.tab === tab) ? "block" : "none");
    });
  });

  // ---- Manual add ----
  $("#auction-add-manual-btn")?.addEventListener("click", () => openSkipTraceModal(null));

  // ---- Single URL scrape ----
  $("#auction-single-btn")?.addEventListener("click", async () => {
    const url = $("#auction-single-url").value.trim();
    if (!url) return;
    const status = $("#auction-single-status");
    const btn = $("#auction-single-btn");
    btn.disabled = true;
    status.innerHTML = '<span class="spinner"></span> Scraping detail page (~5-10 sec)…';
    status.className = "status-line";
    try {
      const r = await API.auctionsImportSingle(url);
      if (!r.ok) {
        status.innerHTML = `<strong>Failed:</strong> ${escape(r.error || 'unknown')}` +
          (r.raw_text_excerpt ? `<details style="margin-top:6px;"><summary>Raw page excerpt</summary><pre style="font-size:10px; white-space:pre-wrap; max-height:200px; overflow:auto; background:var(--bg-2); padding:8px; border-radius:6px;">${escape(r.raw_text_excerpt)}</pre></details>` : '');
        status.className = "status-line error";
      } else {
        const it = r.item || {};
        status.innerHTML = `✓ Added: <strong>${escape(it.address || it.case_number || 'item')}</strong> ` +
          (it.opening_bid ? `(${fmtMoney(it.opening_bid)})` : '') +
          (r.skipped ? ` <em>(${r.skipped} already in queue)</em>` : '');
        status.className = "status-line success";
        $("#auction-single-url").value = "";
        refreshSkipTraceView();
      }
    } catch (e) {
      status.innerHTML = `<strong>Failed:</strong> ${escape(e.message)}`;
      status.className = "status-line error";
    } finally { btn.disabled = false; }
  });

  // ---- Import an auction URL ----
  $("#auction-import-btn")?.addEventListener("click", async () => {
    const url = $("#auction-import-url").value.trim();
    if (!url) return;
    const status = $("#auction-import-status");
    const btn = $("#auction-import-btn");
    btn.disabled = true;
    status.innerHTML = '<span class="spinner"></span> Scraping auction list (~10-30 sec)…';
    status.className = "status-line";
    try {
      const r = await API.auctionsImport(url);
      if (!r.ok) {
        status.innerHTML = `<strong>Import failed:</strong> ${escape(r.error || 'unknown')}` +
          (r.raw_text_excerpt ? `<details style="margin-top:6px;"><summary>Raw page excerpt</summary><pre style="font-size:10px; white-space:pre-wrap; max-height:200px; overflow:auto; background:var(--bg-2); padding:8px; border-radius:6px;">${escape(r.raw_text_excerpt)}</pre></details>` : '');
        status.className = "status-line error";
      } else {
        status.innerHTML = `✓ Imported <strong>${r.added}</strong> new items (${r.skipped} duplicates skipped) from ${escape(r.site)}. Total in queue: ${r.total_now}.`;
        status.className = "status-line success";
        $("#auction-import-url").value = "";
        refreshSkipTraceView();
      }
    } catch (e) {
      status.innerHTML = `<strong>Failed:</strong> ${escape(e.message)}`;
      status.className = "status-line error";
    } finally { btn.disabled = false; }
  });

  // ============== BATCH IMPORT ==============
  let batchPollTimer = null;
  let batchCurrentJobId = null;

  async function refreshBatchView() {
    // Show job history
    try {
      const jobs = await API.batchJobs();
      renderBatchHistory(jobs);
    } catch (e) {}
    // Resume polling if there's a running job
    if (batchCurrentJobId) {
      pollBatchJob(batchCurrentJobId);
    }
  }

  function renderBatchHistory(jobs) {
    const wrap = $("#batch-history");
    if (!jobs.length) {
      wrap.innerHTML = '<div class="muted" style="padding:12px; text-align:center;">No jobs run yet.</div>';
      return;
    }
    wrap.innerHTML = jobs.slice(0, 10).map(j => `
      <div class="batch-history-row" data-id="${escape(j.id)}">
        <span class="job-id">${escape(j.id)}</span>
        <span style="color:var(--text);">${j.total} items · ${j.succeeded || 0} ok · ${j.failed || 0} failed · ${j.skipped || 0} skipped</span>
        <span class="muted" style="font-size:11px;">${new Date(j.created_at).toLocaleString()}</span>
        <span class="job-status ${escape(j.status)}">${escape(j.status)}</span>
        <span><button class="btn ghost" data-act="view" data-id="${escape(j.id)}" style="padding:2px 8px; font-size:11px;">View</button>
          <button class="btn ghost" data-act="del" data-id="${escape(j.id)}" style="padding:2px 8px; font-size:11px;">×</button></span>
      </div>
    `).join("");
    $$("button[data-act]", wrap).forEach(b => {
      b.addEventListener("click", async () => {
        const id = b.dataset.id;
        if (b.dataset.act === "view") {
          try {
            const j = await API.batchGet(id);
            batchCurrentJobId = j.id;
            $("#batch-progress-card").style.display = "block";
            renderBatchJob(j);
            if (j.status === "running" || j.status === "queued") {
              pollBatchJob(j.id);
            }
          } catch (e) { toast(e.message, "error"); }
        } else if (b.dataset.act === "del") {
          if (!confirm("Delete this job's history?")) return;
          try { await API.batchDelete(id); refreshBatchView(); }
          catch (e) { toast(e.message, "error"); }
        }
      });
    });
  }

  function renderBatchJob(j) {
    const pct = j.progress_pct != null
      ? j.progress_pct
      : (j.total > 0 ? Math.round(((j.succeeded + j.failed + j.skipped) / j.total) * 100) : 0);
    $("#batch-bar").style.width = pct + "%";
    $("#batch-bar-pct").textContent = `${Math.round(pct)}%`;
    $("#batch-job-id").textContent = `Job ${j.id}`;

    // Status pill
    const statusPill = $("#batch-status-pill");
    const statusClass = {
      queued: "gray", running: "blue", paused: "yellow",
      completed: "green", cancelled: "red",
    }[j.status] || "gray";
    statusPill.className = `pill ${statusClass}`;
    statusPill.textContent = j.status;

    // Bar visual state
    const bar = $("#batch-bar");
    bar.classList.remove("paused", "completed", "cancelled");
    if (j.status === "paused") bar.classList.add("paused");
    else if (j.status === "completed") bar.classList.add("completed");
    else if (j.status === "cancelled") bar.classList.add("cancelled");

    // Live "current item" message
    const currentItem = j.items[j.current_index];
    const msgEl = $("#batch-current-message");
    if (j.status === "running" && currentItem && currentItem.status === "running") {
      msgEl.textContent = `▸ [${j.current_index + 1}/${j.total}] ${(currentItem.progress_message || "processing")} — ${currentItem.input.slice(0, 70)}`;
    } else if (j.status === "paused") {
      msgEl.textContent = `⏸ Paused at ${j.current_index + 1}/${j.total} — click Resume to continue`;
    } else if (j.status === "completed") {
      msgEl.textContent = `✓ Done — ${j.succeeded} succeeded, ${j.failed} failed, ${j.skipped} skipped`;
    } else if (j.status === "cancelled") {
      msgEl.textContent = `✗ Cancelled at ${j.current_index + 1}/${j.total}`;
    } else {
      msgEl.textContent = "";
    }

    // Render control buttons based on status
    renderBatchControls(j);

    $("#batch-stats").innerHTML = `
      <div class="stat-card"><div class="label">Total</div><div class="value">${j.total}</div></div>
      <div class="stat-card green"><div class="label">Succeeded</div><div class="value">${j.succeeded}</div></div>
      <div class="stat-card red"><div class="label">Failed</div><div class="value">${j.failed}</div></div>
      <div class="stat-card"><div class="label">Skipped</div><div class="value">${j.skipped}</div></div>
      <div class="stat-card"><div class="label">Remaining</div><div class="value">${j.remaining}</div></div>
      <div class="stat-card"><div class="label">Status</div><div class="value" style="font-size:14px; line-height:1.6;">${escape(j.status.toUpperCase())}</div></div>
    `;

    const tbody = $("#batch-tbody");
    tbody.innerHTML = j.items.map((it, idx) => {
      const result = it.result || {};
      let resultCell = "";
      if (it.status === "succeeded") {
        const addr = result.address || "";
        const imgs = result.image_count ? ` · ${result.image_count} photos` : "";
        const dealId = it.deal_id ? ` <a href="#" data-open-deal="${escape(it.deal_id)}" style="color:var(--accent); font-weight:600;">open deal →</a>` : "";
        resultCell = `<div class="batch-result-cell">✓ ${escape(addr)}${imgs} via ${escape(result.source || '?')}${dealId}</div>`;
      } else if (it.status === "failed") {
        resultCell = `<div class="batch-error">✗ ${escape(it.error || 'failed')}</div>`;
      } else if (it.status === "skipped") {
        resultCell = `<div class="muted" style="font-size:11.5px;">— ${escape(it.error || 'skipped')}</div>`;
      } else if (it.status === "running") {
        resultCell = `<div style="font-size:11.5px; color:var(--blue); font-weight:500;">⏳ ${escape(it.progress_message || 'processing...')}</div>`;
      } else {
        resultCell = `<div class="muted" style="font-size:11.5px;">queued</div>`;
      }
      return `
        <tr>
          <td>${idx + 1}</td>
          <td class="batch-input-cell" title="${escape(it.input)}">${escape(it.input)}</td>
          <td>${(() => {
              const meta = {
                url:           { label: "listing",  cls: "blue"   },
                address:       { label: "address",  cls: "green"  },
                zillow_search: { label: "🔍 search", cls: "purple" },
                prefetched:    { label: "AI find",  cls: "orange" },
              }[it.type] || { label: it.type, cls: "gray" };
              return `<span class="pill ${meta.cls}">${escape(meta.label)}</span>`;
            })()}</td>
          <td><span class="batch-row-status ${escape(it.status)}">${escape(it.status)}</span></td>
          <td>${resultCell}</td>
          <td>${it.deal_id ? `<button class="btn ghost" data-open-deal="${escape(it.deal_id)}" style="padding:3px 8px; font-size:11px;">open</button>` : ''}</td>
        </tr>
      `;
    }).join("");

    $$("[data-open-deal]", tbody).forEach(el => {
      el.addEventListener("click", e => {
        e.preventDefault();
        openDeal(el.dataset.openDeal);
      });
    });
  }

  function renderBatchControls(j) {
    const wrap = $("#batch-controls");
    if (!wrap) return;
    let buttons = "";
    if (j.status === "running") {
      buttons += `<button class="btn" data-act="pause">
        <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>
        Pause</button>`;
      buttons += `<button class="btn danger" data-act="cancel">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6" stroke-linecap="round"/></svg>
        Cancel</button>`;
    } else if (j.status === "paused") {
      buttons += `<button class="btn primary" data-act="resume">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 3l14 9-14 9V3z"/></svg>
        Resume</button>`;
      buttons += `<button class="btn danger" data-act="cancel">Cancel</button>`;
    } else if (j.status === "completed" || j.status === "cancelled") {
      const failedCount = (j.failed || 0) + (j.skipped || 0);
      if (failedCount > 0) {
        buttons += `<button class="btn primary" data-act="retry-failed">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" stroke-linecap="round" stroke-linejoin="round"/></svg>
          Retry ${failedCount} failed</button>`;
      }
      buttons += `<button class="btn" data-act="restart">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M3 12a9 9 0 109-9M12 3v6h6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Restart all</button>`;
    }
    wrap.innerHTML = buttons;
    $$("button[data-act]", wrap).forEach(b => {
      b.addEventListener("click", async () => {
        const act = b.dataset.act;
        if (!batchCurrentJobId) return;
        try {
          if (act === "pause") {
            await API.batchPause(batchCurrentJobId);
            toast("Pausing…", "success");
          } else if (act === "resume") {
            await API.batchResume(batchCurrentJobId);
            toast("Resuming", "success");
            pollBatchJob(batchCurrentJobId);
          } else if (act === "cancel") {
            if (!confirm("Cancel this batch?")) return;
            await API.batchCancel(batchCurrentJobId);
            toast("Cancel requested", "success");
          } else if (act === "restart") {
            if (!confirm(`Restart this batch from scratch (${$("#batch-tbody tr").length} items)?`)) return;
            const newJob = await API.batchRestart(batchCurrentJobId);
            batchCurrentJobId = newJob.id;
            renderBatchJob(newJob);
            pollBatchJob(newJob.id);
            toast(`Restarted as job ${newJob.id}`, "success");
            refreshBatchView();
          } else if (act === "retry-failed") {
            const newJob = await API.batchRetryFailed(batchCurrentJobId);
            batchCurrentJobId = newJob.id;
            renderBatchJob(newJob);
            pollBatchJob(newJob.id);
            toast(`Retrying ${newJob.total} failed items as job ${newJob.id}`, "success");
            refreshBatchView();
          }
        } catch (e) { toast(e.message, "error"); }
      });
    });
  }

  async function pollBatchJob(jobId) {
    if (batchPollTimer) clearInterval(batchPollTimer);
    // Poll every 800ms for snappier real-time feel
    batchPollTimer = setInterval(async () => {
      try {
        const j = await API.batchGet(jobId);
        renderBatchJob(j);
        if (j.status === "completed" || j.status === "cancelled") {
          clearInterval(batchPollTimer);
          batchPollTimer = null;
          toast(`Batch ${j.status}: ${j.succeeded} ok / ${j.failed} failed / ${j.skipped} skipped`,
                  j.succeeded > 0 ? "success" : "error");
          refreshBatchView();
          refreshDashboard();  // refresh stats — new deals
        }
        // Note: don't stop polling on "paused" — user might resume
      } catch (e) {
        clearInterval(batchPollTimer);
        batchPollTimer = null;
        toast("Lost connection to job: " + e.message, "error");
      }
    }, 800);
  }

  $("#batch-start")?.addEventListener("click", async () => {
    const raw = $("#batch-inputs").value;
    const lines = raw.split("\n").map(l => l.trim()).filter(l => l && !l.startsWith("#"));
    if (!lines.length) { toast("Paste at least one URL or address", "error"); return; }
    if (lines.length > 200 && !confirm(`Start batch with ${lines.length} items? Will take ~${Math.round(lines.length * 4 / 60)} min.`)) return;
    const delay = Number($("#batch-delay").value || 2.5);
    const skipDups = $("#batch-skip-dups").value === "yes";
    try {
      const j = await API.batchStart(lines, { delay_sec: delay, skip_duplicates: skipDups });
      batchCurrentJobId = j.id;
      $("#batch-progress-card").style.display = "block";
      $("#batch-info").innerHTML = `<span class="pill green">Started</span> Job ${escape(j.id)} — ${j.total} items, ~${Math.round(j.total * (delay + 2.5) / 60)} min estimated.`;
      $("#batch-info").className = "status-line success";
      renderBatchJob(j);
      pollBatchJob(j.id);
      refreshBatchView();
    } catch (e) {
      $("#batch-info").innerHTML = `<strong>Failed to start:</strong> ${escape(e.message)}`;
      $("#batch-info").className = "status-line error";
    }
  });

  // ----- Zillow search → import newest N (scrapes each listing for photos) -----
  let zsearchCount = 10;
  $("#zsearch-counts")?.addEventListener("click", e => {
    const chip = e.target.closest(".method-chip");
    if (!chip) return;
    zsearchCount = Number(chip.dataset.count) || 10;
    $$("#zsearch-counts .method-chip").forEach(c => c.classList.toggle("active", c === chip));
    const lbl = $("#zsearch-count-label");
    if (lbl) lbl.textContent = zsearchCount;
  });

  $("#zsearch-import")?.addEventListener("click", async () => {
    const url = ($("#zsearch-url").value || "").trim();
    const status = $("#zsearch-status");
    const isZsearch = /zillow\.com/i.test(url) &&
      (/searchquerystate=/i.test(url) || /\/homes\//i.test(url));
    if (!isZsearch) {
      status.innerHTML = "Paste a Zillow <strong>search</strong> URL (the one with <code>searchQueryState=…</code>).";
      status.className = "status-line error";
      return;
    }
    const estMin = Math.max(1, Math.ceil(zsearchCount * 0.7));  // ~40s per listing
    if (zsearchCount >= 30 &&
        !confirm(`Import the ${zsearchCount} newest listings? Each is scraped individually for photos — this takes roughly ${estMin} minutes.`)) {
      return;
    }
    const btn = $("#zsearch-import");
    btn.disabled = true;
    status.innerHTML = `<span class="spinner"></span> Finding the ${zsearchCount} newest listings…`;
    status.className = "status-line";
    try {
      const j = await API.batchStart([url], {
        search_max: zsearchCount,
        scrape_each: true,
        skip_duplicates: true,
        delay_sec: 2.5,
      });
      batchCurrentJobId = j.id;
      $("#batch-progress-card").style.display = "block";
      $("#batch-info").innerHTML = `<span class="pill green">Started</span> Finding + scraping the ${zsearchCount} newest listings (with photos) — about ${estMin} min.`;
      $("#batch-info").className = "status-line success";
      status.textContent = "";
      renderBatchJob(j);
      pollBatchJob(j.id);
      refreshBatchView();
      $("#batch-progress-card").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      status.innerHTML = `<strong>Failed to start:</strong> ${escape(e.message)}`;
      status.className = "status-line error";
    } finally {
      btn.disabled = false;
    }
  });

  $("#batch-cancel")?.addEventListener("click", async () => {
    if (!batchCurrentJobId) return;
    if (!confirm("Cancel this batch? Items already processed will be kept.")) return;
    try {
      await API.batchCancel(batchCurrentJobId);
      toast("Cancel requested — will stop after current item.", "success");
    } catch (e) { toast(e.message, "error"); }
  });

  $("#batch-load-leads")?.addEventListener("click", async () => {
    try {
      const leads = await API.leadsList();
      const urls = leads.map(l => l.source_url).filter(Boolean);
      const addresses = leads.filter(l => !l.source_url && l.address).map(l => l.address);
      const combined = [...urls, ...addresses];
      if (!combined.length) { toast("No leads with URLs or addresses to import", "error"); return; }
      $("#batch-inputs").value = combined.join("\n");
      toast(`Loaded ${combined.length} items from your leads`, "success");
    } catch (e) { toast(e.message, "error"); }
  });

  // ============== LEADS ==============
  async function refreshLeadsView() {
    try {
      const [leads, agg] = await Promise.all([
        API.leadsList(), API.leadsAggregates(),
      ]);
      // Sidebar badge
      const badge = $("#nav-lead-count");
      if (badge) badge.textContent = agg.total || "";
      renderLeadsStats(agg);
      renderLeadsList(leads);
    } catch (e) { toast(e.message, "error"); }
  }

  function renderLeadsStats(agg) {
    const grid = $("#leads-stats");
    if (!agg.total) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1;">
        <div class="empty-ico">⚡</div>
        <h3>No leads yet</h3>
        <p>Paste an ispeedtolead URL above, or click "New lead" to evaluate one.</p>
      </div>`;
      return;
    }
    grid.innerHTML = `
      <div class="stat-card"><div class="label">Total leads</div><div class="value">${agg.total}</div></div>
      <div class="stat-card green"><div class="label">Worth buying</div><div class="value">${agg.worth_buying}</div></div>
      <div class="stat-card"><div class="label">New</div><div class="value">${agg.by_status?.new || 0}</div></div>
      <div class="stat-card"><div class="label">Contacted</div><div class="value">${agg.by_status?.contacted || 0}</div></div>
      <div class="stat-card"><div class="label">Closed (→ deal)</div><div class="value">${agg.by_status?.closed || 0}</div></div>
      <div class="stat-card"><div class="label">Spent on leads</div><div class="value">${fmtMoney(agg.total_spent_on_leads)}</div></div>
    `;
  }

  function leadVerdictPill(analysis) {
    if (!analysis || !analysis.result) return '<span class="lead-verdict-pill none">Not analyzed</span>';
    const v = (analysis.result.recommendation || "").toUpperCase();
    if (v.startsWith("BUY")) return `<span class="lead-verdict-pill buy">✓ ${escape(analysis.result.recommendation)}</span>`;
    if (v.startsWith("MAYBE")) return `<span class="lead-verdict-pill maybe">⚖ ${escape(analysis.result.recommendation)}</span>`;
    if (v.startsWith("PASS")) return `<span class="lead-verdict-pill pass">✗ ${escape(analysis.result.recommendation)}</span>`;
    return `<span class="lead-verdict-pill none">${escape(analysis.result.recommendation || '?')}</span>`;
  }

  function renderLeadsList(leads) {
    const wrap = $("#leads-list-container");
    if (!leads.length) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = leads.map(l => {
      const a = l.ai_analysis;
      const r = a?.result || {};
      const ev = r.ev_estimate;
      return `
        <div class="lead-card" data-id="${l.id}">
          <div class="lead-card-main">
            <div class="lead-card-header">
              <div class="lead-card-title">${escape(l.address || l.source_url || 'Untitled lead')}</div>
              ${leadVerdictPill(a)}
              <span class="status-pill ${escape(l.status || 'new')}">${escape(l.status || 'new')}</span>
            </div>
            <div class="lead-card-meta">
              ${l.city ? `${escape(l.city)}, ${escape(l.state || '')}` : ''}
              ${l.beds ? `• ${l.beds}bd/${l.baths || '?'}ba` : ''}
              ${l.sqft ? `• ${l.sqft.toLocaleString()}sf` : ''}
              ${l.source ? `• <span class="pill gray">${escape(l.source)}</span>` : ''}
            </div>
            ${l.motivation ? `<div class="lead-motivation">💡 ${escape(l.motivation)}</div>` : ''}
            <div class="lead-card-financials">
              <div class="lead-card-financial">
                <div class="lbl">Lead price</div>
                <div class="val red">${fmtMoney(l.lead_price)}</div>
              </div>
              <div class="lead-card-financial">
                <div class="lbl">Asking</div>
                <div class="val">${fmtMoney(l.asking_price)}</div>
              </div>
              <div class="lead-card-financial">
                <div class="lbl">Est. ARV</div>
                <div class="val">${fmtMoney(l.estimated_arv || r.estimated_arv)}</div>
              </div>
              <div class="lead-card-financial">
                <div class="lbl">${ev !== undefined ? 'Expected value' : 'Est. rehab'}</div>
                <div class="val ${ev > 0 ? 'accent' : ev < 0 ? 'red' : ''}">${
                  ev !== undefined ? fmtMoney(ev, true) : fmtMoney(l.estimated_rehab || r.estimated_rehab)
                }</div>
              </div>
            </div>
            ${a ? `<div style="font-size:12px; color:var(--muted); margin-top:8px;">
              <span class="pill blue">conv. ${r.conversion_likelihood_pct || '?'}%</span>
              <span class="pill purple">fair lead $ ≤ ${fmtMoney(r.fair_lead_price_max)}</span>
              <span style="margin-left:8px;">${escape(r.verdict_summary || '')}</span>
            </div>` : ''}
          </div>
          <div class="lead-card-actions">
            ${a ? `<button class="btn" data-action="view-analysis">View analysis</button>`
                : `<button class="btn primary" data-action="analyze">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" width="13" height="13"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" stroke-linejoin="round"/></svg>
                    Analyze
                  </button>`}
            <button class="btn" data-action="edit">Edit</button>
            ${a && (r.recommendation||'').toUpperCase().startsWith('BUY') ?
              `<button class="btn" data-action="promote">→ Deal</button>` : ''}
            <button class="btn danger" data-action="delete">Delete</button>
            ${l.source_url ? `<a href="${escape(l.source_url)}" target="_blank" class="btn ghost" style="font-size:11px;">Source ↗</a>` : ''}
          </div>
        </div>
      `;
    }).join("");

    $$(".lead-card", wrap).forEach(card => {
      const id = card.dataset.id;
      $$("[data-action]", card).forEach(b => {
        b.addEventListener("click", e => {
          e.stopPropagation();
          handleLeadAction(b.dataset.action, id);
        });
      });
    });
  }

  async function handleLeadAction(action, id) {
    if (action === "analyze") {
      try {
        const lead = await API.leadGet(id);
        if (!lead.lead_price) {
          toast("Set a lead_price first (Edit the lead).", "error");
          return;
        }
        $("#lead-analysis-modal").style.display = "flex";
        $("#lead-analysis-body").innerHTML = `<div class="empty"><div class="empty-ico">🔍</div>
          <h3>Analyzing…</h3><p>Claude is searching the web and evaluating expected value. ~30-60 sec.</p></div>`;
        const out = await API.leadAnalyze(id);
        renderLeadAnalysisResult(out);
        refreshLeadsView();
      } catch (e) {
        $("#lead-analysis-body").innerHTML = `<div class="empty">
          <div class="empty-ico">⚠</div>
          <h3>Analysis failed</h3>
          <p>${escape(e.message)}</p>
        </div>`;
      }
    } else if (action === "view-analysis") {
      const lead = await API.leadGet(id);
      if (lead.ai_analysis) {
        $("#lead-analysis-modal").style.display = "flex";
        renderLeadAnalysisResult(lead.ai_analysis);
      }
    } else if (action === "edit") {
      const lead = await API.leadGet(id);
      openLeadModal(lead);
    } else if (action === "promote") {
      if (!confirm("Convert this lead into a deal? It will appear in your Deals board.")) return;
      try {
        const r = await API.leadPromote(id);
        toast("Promoted to deal", "success");
        showView("deals");
        setTimeout(() => openDeal(r.deal_id), 300);
      } catch (e) { toast(e.message, "error"); }
    } else if (action === "delete") {
      if (!confirm("Delete this lead?")) return;
      try {
        await API.leadDelete(id);
        toast("Deleted", "success");
        refreshLeadsView();
      } catch (e) { toast(e.message, "error"); }
    }
  }

  function renderLeadAnalysisResult(out) {
    const r = out.result || out;
    const v = (r.recommendation || "").toUpperCase();
    let cls = "pass";
    if (v.startsWith("BUY")) cls = "buy";
    else if (v.startsWith("MAYBE")) cls = "maybe";

    let html = `
      <div class="lead-analysis-verdict ${cls}">
        <div class="lbl">Verdict</div>
        <div class="val">${escape(r.recommendation || '?')}</div>
        <div style="margin-top:6px;"><span class="pill ${r.confidence === 'High' ? 'green' : r.confidence === 'Low' ? 'red' : 'yellow'}">${escape(r.confidence || 'Medium')} confidence</span></div>
      </div>

      <div class="lead-analysis-grid">
        <div class="lead-analysis-stat">
          <div class="lbl">Fair max lead price</div>
          <div class="val">${fmtMoney(r.fair_lead_price_max)}</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Expected value</div>
          <div class="val" style="color:${r.ev_estimate > 0 ? 'var(--green)' : 'var(--red)'};">${fmtMoney(r.ev_estimate, true)}</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Conversion likelihood</div>
          <div class="val">${r.conversion_likelihood_pct || '?'}%</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Est. ARV</div>
          <div class="val">${fmtMoney(r.estimated_arv)}</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Est. rehab</div>
          <div class="val">${fmtMoney(r.estimated_rehab)}</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Spread</div>
          <div class="val">${fmtMoney(r.estimated_spread)}</div>
        </div>
        <div class="lead-analysis-stat">
          <div class="lbl">Expected assignment fee</div>
          <div class="val">${fmtMoney(r.expected_assignment_fee)}</div>
        </div>
        <div class="lead-analysis-stat" style="grid-column: span 2;">
          <div class="lbl">Motivation signal</div>
          <div class="val">${escape(r.motivation_signal || '?')}</div>
        </div>
      </div>

      <div class="ai-section"><h4>✓ Positives</h4><ul>${(r.top_3_positive||[]).map(x => `<li>${escape(x)}</li>`).join('')}</ul></div>
      <div class="ai-section"><h4>⚠ Concerns</h4><ul>${(r.top_3_concerns||[]).map(x => `<li>${escape(x)}</li>`).join('')}</ul></div>
      ${r.next_steps_if_buy?.length ? `<div class="ai-section"><h4>Next steps if you buy this lead</h4><ul>${r.next_steps_if_buy.map(x => `<li>${escape(x)}</li>`).join('')}</ul></div>` : ''}
      ${r.verdict_summary ? `<div class="verdict-summary" style="margin-top:18px;">${escape(r.verdict_summary)}</div>` : ''}
    `;
    $("#lead-analysis-body").innerHTML = html;
  }

  // Lead-analysis modal removed with the Leads view
  {
    const analModal = $("#lead-analysis-modal");
    if (analModal) {
      $("#lead-analysis-close")?.addEventListener("click", () => analModal.style.display = "none");
      $(".modal-backdrop", analModal)?.addEventListener("click", () => analModal.style.display = "none");
    }
  }

  // ----- Lead modal (form) -----
  function openLeadModal(lead) {
    const m = $("#lead-modal");
    const form = $("#lead-form");
    form.reset();
    $("#lead-modal-title").textContent = lead ? "Edit lead" : "New lead";
    if (lead) {
      Object.entries(lead).forEach(([k, v]) => {
        const el = form.elements[k];
        if (el && !Array.isArray(v) && typeof v !== "object") el.value = v;
      });
      form.dataset.editing = lead.id;
    } else { delete form.dataset.editing; }
    m.style.display = "flex";
  }
  // Lead form modal removed with the Leads view
  {
    const lm = $("#lead-modal");
    if (lm) {
      $("#lead-modal-close")?.addEventListener("click", () => lm.style.display = "none");
      $("#lead-modal-cancel")?.addEventListener("click", () => lm.style.display = "none");
      $(".modal-backdrop", lm)?.addEventListener("click", () => lm.style.display = "none");
    }
  }

  $("#lead-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const obj = {};
    fd.forEach((v, k) => {
      if (v === "" || v == null) return;
      if (["lead_price", "asking_price", "estimated_arv", "estimated_rehab",
            "beds", "baths", "sqft", "year_built"].includes(k)) {
        obj[k] = Number(v);
      } else obj[k] = v;
    });
    if (e.target.dataset.editing) obj.id = e.target.dataset.editing;
    try {
      await API.leadCreate(obj);
      toast("Lead saved", "success");
      $("#lead-modal").style.display = "none";
      refreshLeadsView();
    } catch (e) { toast(e.message, "error"); }
  });

  $("#leads-add-btn")?.addEventListener("click", () => openLeadModal(null));

  // Quick paste URL → scrape → create
  $("#lead-scrape-btn")?.addEventListener("click", async () => {
    const url = $("#lead-scrape-url").value.trim();
    if (!url) return;
    const status = $("#lead-scrape-status");
    status.innerHTML = '<span class="spinner"></span> Fetching lead (may open Chromium if not signed in)…';
    status.className = "status-line";
    $("#lead-scrape-btn").disabled = true;
    try {
      const data = await API.leadScrape(url);
      // Build a lead from whatever data we got + open the modal to confirm
      const seed = {
        source: "ispeedtolead",
        source_url: url,
        external_id: data.external_id,
        address: data.address || "",
        city: data.city || "", state: data.state || "", zip: data.zip || "",
        beds: data.beds, baths: data.baths, sqft: data.sqft, year_built: data.year_built,
        property_type: data.property_type,
        asking_price: data.asking_price,
        estimated_arv: data.estimated_arv,
        estimated_rehab: data.estimated_rehab,
        motivation: data.motivation,
        description: data.description || data._page_text_excerpt || "",
        image: data.image,
        image_gallery: data.image_gallery,
        status: "new",
      };
      status.textContent = data.address
        ? `✓ Got data — fill in the lead price below and save.`
        : `Partial data. Fill in the missing fields.`;
      status.className = "status-line " + (data.address ? "success" : "");
      openLeadModal(seed);
      $("#lead-scrape-url").value = "";
    } catch (e) {
      status.innerHTML = `<strong>Scrape failed:</strong> ${escape(e.message)}<br/>
        <button class="btn primary" id="lead-auth-now" style="margin-top:8px;">Sign in to ispeedtolead now</button>
        <a href="#" id="lead-manual-open" style="color:var(--accent); margin-left:8px;">Enter manually instead</a>`;
      status.className = "status-line error";
      $("#lead-manual-open")?.addEventListener("click", e => {
        e.preventDefault();
        openLeadModal({ source: "ispeedtolead", source_url: url, status: "new" });
      });
      $("#lead-auth-now")?.addEventListener("click", async () => {
        const btn = $("#lead-auth-now");
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Opening Chromium…';
        toast("Log in in the Chromium window that opens.", "success");
        try {
          const r = await API.browserSessionConnect({
            login_url: "https://app.ispeedtolead.com/auth/login",
            success_url_contains: "/my-leads",
          });
          if (r.ok) {
            toast("✓ Signed in. Retrying scrape…", "success");
            status.innerHTML = '<span class="spinner"></span> Retrying scrape…';
            // Retry
            const data = await API.leadScrape(url);
            const seed = {
              source: "ispeedtolead", source_url: url,
              external_id: data.external_id,
              address: data.address || "", city: data.city || "",
              state: data.state || "", zip: data.zip || "",
              beds: data.beds, baths: data.baths, sqft: data.sqft,
              year_built: data.year_built, property_type: data.property_type,
              asking_price: data.asking_price, estimated_arv: data.estimated_arv,
              estimated_rehab: data.estimated_rehab, motivation: data.motivation,
              description: data.description || data._page_text_excerpt || "",
              image: data.image, image_gallery: data.image_gallery,
              status: "new",
            };
            status.textContent = data.address
              ? `✓ Got data — fill in the lead price and save.`
              : `Partial data — fill in what's missing.`;
            status.className = "status-line success";
            openLeadModal(seed);
          } else {
            toast(r.error || "Login not completed", "error");
            btn.disabled = false;
            btn.innerHTML = "Sign in to ispeedtolead now";
          }
        } catch (e) { toast(e.message, "error"); }
      });
    } finally {
      $("#lead-scrape-btn").disabled = false;
    }
  });

  // ============== CRM ==============
  const ROLE_META = {
    seller:     { icon: "🏠", label: "Seller" },
    agent:      { icon: "👤", label: "Agent" },
    contractor: { icon: "🔧", label: "Contractor" },
    lender:     { icon: "💰", label: "Lender" },
    title:      { icon: "📜", label: "Title/Escrow" },
    inspector:  { icon: "🔍", label: "Inspector" },
    appraiser:  { icon: "📋", label: "Appraiser" },
    other:      { icon: "•",  label: "Other" },
  };
  const TYPE_ICON = {
    call: "📞", email: "✉️", meeting: "🤝", sms: "💬",
    offer: "💰", note: "📝", other: "•",
  };

  function initials(name) {
    return (name || "?").trim().split(/\s+/).slice(0, 2).map(w => w[0].toUpperCase()).join("");
  }

  async function refreshCrmView() {
    try {
      await renderLeadsKanban();
      const badge = $("#nav-crm-count");
      if (badge) badge.textContent = _kanbanLeadsCache.length || "";
    } catch (e) { toast(e.message, "error"); }
    try { await renderDealsPipeline(); } catch (e) { console.error("deals pipeline", e); }
  }

  // ============== DEALS PIPELINE (track deals to closing) ==============
  const _DP_COLS = [
    ["evaluating", "🔎 Evaluating", "#3b82f6"],
    ["under_contract", "📝 Under contract", "#f59e0b"],
    ["closed", "✅ Closed", "#10b981"],
    ["sold", "💰 Sold", "#8b5cf6"],
    ["passed", "⛔ Passed", "#9ca3af"],
  ];
  async function renderDealsPipeline() {
    const host = $("#deals-pipeline"); if (!host) return;
    let deals = [];
    try { deals = await API.listDeals(); state.deals = deals; }
    catch (e) { host.innerHTML = `<p class="muted">${escape(e.message)}</p>`; return; }
    if (!deals.length) {
      host.innerHTML = `<p class="muted" style="font-size:13px; margin:0;">No deals — add some from Sourcing.</p>`;
      return;
    }
    const D = v => (v == null || v === "") ? "—" : "$" + Math.round(Number(v) / 1000) + "K";
    host.innerHTML = `<div class="dp-cols">${_DP_COLS.map(([key, label, color]) => {
      const items = deals.filter(d => (d.status || "evaluating") === key);
      const cards = items.map(d => `
        <div class="dp-card" data-id="${escape(d.id)}">
          <div class="dp-card-top">
            <span class="dp-addr" data-open="${escape(d.id)}" title="Open the deal">${escape(d.address || "?")}</span>
            ${riskBadge(d)}
          </div>
          <div class="dp-meta">
            🎯 ${D(d.max_offer)}${d.max_offer_blocked ? " ⛔" : ""} · profit ${D(d.net_profit_financed ?? d.net_profit)}
          </div>
          <select class="dp-status" data-id="${escape(d.id)}" title="Change status">
            ${_DP_COLS.map(([k, l]) => `<option value="${k}" ${k === key ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          ${key === "under_contract" ? `
            <label class="dp-close-label">Expected closing
              <input type="date" class="dp-closing" data-id="${escape(d.id)}" value="${escape(d.closing_date || "")}">
            </label>
            ${(d.deal_breakers && d.deal_breakers.length) ? `<div class="dp-warn">⛔ ${d.deal_breakers.length} unresolved deal-breaker(s)</div>` : ""}` : ""}
        </div>`).join("");
      return `<div class="dp-col">
        <div class="dp-col-head" style="border-top:3px solid ${color};">${label} <span class="dp-count">${items.length}</span></div>
        ${cards || '<div class="dp-empty">—</div>'}
      </div>`;
    }).join("")}</div>`;

    // Open deal on address click
    host.querySelectorAll("[data-open]").forEach(el =>
      el.addEventListener("click", () => openDeal(el.dataset.open)));
    // Direct status change (with a risk gate before committing)
    host.querySelectorAll(".dp-status").forEach(sel => sel.addEventListener("change", async () => {
      const id = sel.dataset.id;
      const target = sel.value;
      const deal = deals.find(x => x.id === id);
      const engaging = ["under_contract", "closed"].includes(target);
      if (engaging && deal && deal.deal_breakers && deal.deal_breakers.length) {
        const ok = confirm(`⛔ “${deal.address}” has ${deal.deal_breakers.length} UNRESOLVED deal-breaker(s):\n\n- ${deal.deal_breakers.join("\n- ")}\n\nCommit anyway to “${target === "under_contract" ? "under contract" : "closed"}”?`);
        if (!ok) { renderDealsPipeline(); return; }
      }
      try {
        await API.patchDeal(id, { status: target });
        toast(`Status → ${target}`, "success");
        renderDealsPipeline();
      } catch (e) { toast(e.message, "error"); renderDealsPipeline(); }
    }));
    // Closing date persist
    host.querySelectorAll(".dp-closing").forEach(inp => inp.addEventListener("change", async () => {
      try {
        await API.patchDeal(inp.dataset.id, { closing_date: inp.value || null });
        toast(inp.value ? `Closing set for ${inp.value}` : "Closing date removed", "success");
      } catch (e) { toast(e.message, "error"); }
    }));
  }

  // ============== LEADS KANBAN ==============
  // Columns are customizable and persisted server-side (/api/kanban/columns).
  let _kanbanColumns = [
    { key: "new",         label: "🆕 New",         color: "#6b7280" },
    { key: "contacted",   label: "📞 Contacted",   color: "#3b82f6" },
    { key: "appointment", label: "📅 Appointment", color: "#f59e0b" },
    { key: "offer",       label: "💰 Offer",       color: "#8b5cf6" },
    { key: "closed",      label: "✅ Closed",      color: "#22c55e" },
    { key: "passed",      label: "⏭ Passed",      color: "#ef4444" },
  ];
  let _kanbanLeadsCache = [];
  let _kanbanSearchFilter = "";
  let _kanbanSort = "recent";          // recent | score | price | profit
  let _kanbanFilters = { grade: "", minScore: 0, worth: "" };  // worth: "" | "yes" | "no"
  const _kanbanSelected = new Set();   // selected lead ids (multi-select)
  const _COLOR_PALETTE = ["#6b7280", "#3b82f6", "#f59e0b", "#8b5cf6", "#22c55e",
                          "#ef4444", "#ec4899", "#14b8a6", "#eab308", "#0ea5e9"];

  // Financials for a lead (uses the linked deal as fallback). When a deal is
  // linked, the gain reflects its selected financing (cash → net profit;
  // hard money / other → net profit after financing). Otherwise falls back to
  // the simple estimate: ARV − price − rehab − ~8% selling costs.
  function _leadFin(lead) {
    const deal = lead.external_id ? (state.deals || []).find(d => d.id === lead.external_id) : null;
    const num = v => (v === 0 || v) ? Number(v) : null;
    const price = num(lead.asking_price) ?? num(deal?.purchase_price);
    const arv = num(lead.estimated_arv) ?? num(deal?.arv_base);
    const rehab = num(lead.estimated_rehab) ?? num(deal?.rehab_base) ?? 0;
    const score = num(lead.score) ?? num(deal?.score);
    let profit = null, roi = null, financed = false;
    const method = deal?.financing_method || "cash";
    if (deal && num(deal.net_profit_financed) != null) {
      // Server-computed gain that already accounts for the chosen financing.
      profit = num(deal.net_profit_financed);
      roi = num(deal.roi_financed);
      financed = method !== "cash";
    } else if (arv != null && price != null) {
      profit = arv - price - (rehab || 0) - arv * 0.08;
      const cashIn = price + (rehab || 0);
      roi = cashIn > 0 ? (profit / cashIn) * 100 : null;
    }
    return { deal, price, arv, rehab, score, profit, roi, method, financed };
  }
  function _daysSince(iso) {
    if (!iso) return null;
    const ms = Date.now() - new Date(iso).getTime();
    return ms > 0 ? Math.floor(ms / 86400000) : 0;
  }

  async function renderLeadsKanban() {
    const board = $("#leads-kanban");
    if (!board) return;
    try {
      const [cols, leads] = await Promise.all([API.kanbanColumns(), API.leadsList()]);
      if (Array.isArray(cols) && cols.length) _kanbanColumns = cols;
      _kanbanLeadsCache = leads;
      // Need deals loaded for card thumbnails + click-through to the deal.
      if (!state.deals || !state.deals.length) {
        try { state.deals = await API.listDeals(); } catch {}
      }
    } catch (e) {
      board.innerHTML = `<div class="muted" style="padding:20px;">Error loading kanban: ${escape(e.message)}</div>`;
      return;
    }
    _renderKanbanColumns();
  }

  async function _saveKanbanColumns() {
    try {
      _kanbanColumns = await API.kanbanSetColumns(_kanbanColumns);
      renderLeadsKanban();
    } catch (e) { toast("Failed to save columns: " + e.message, "error"); }
  }

  function _addKanbanColumn() {
    const label = prompt("New column name:", "");
    if (!label || !label.trim()) return;
    _kanbanColumns = [..._kanbanColumns, { label: label.trim() }];
    _saveKanbanColumns();
  }
  function _renameKanbanColumn(key) {
    const col = _kanbanColumns.find(c => c.key === key);
    if (!col) return;
    const label = prompt("Rename column:", col.label);
    if (!label || !label.trim()) return;
    col.label = label.trim();
    _saveKanbanColumns();
  }
  function _deleteKanbanColumn(key) {
    if (_kanbanColumns.length <= 1) { toast("Keep at least one column.", "warn"); return; }
    const col = _kanbanColumns.find(c => c.key === key);
    const n = _kanbanLeadsCache.filter(l => (l.status || "new") === key).length;
    const msg = n > 0
      ? `Delete "${col.label}"? Its ${n} lead(s) will move to the first column.`
      : `Delete the "${col.label}" column?`;
    if (!confirm(msg)) return;
    _kanbanColumns = _kanbanColumns.filter(c => c.key !== key);
    _saveKanbanColumns();
  }

  function _kanbanVisible() {
    const f = (_kanbanSearchFilter || "").toLowerCase();
    const { grade, minScore, worth } = _kanbanFilters;
    return _kanbanLeadsCache.filter(l => {
      if (f && !((l.address || "") + " " + (l.city || "") + " " + (l.owner_name || ""))
            .toLowerCase().includes(f)) return false;
      const fin = _leadFin(l);
      if (grade) {
        const g = (l.ai_analysis?.result?.flip_grade || l.grade || fin.deal?.grade || "").toUpperCase();
        if (!g.startsWith(grade)) return false;
      }
      if (minScore && (fin.score == null || fin.score < minScore)) return false;
      if (worth) {
        const wb = l.ai_analysis?.result?.worth_buying;
        if (worth === "yes" && wb !== true) return false;
        if (worth === "no" && wb !== false) return false;
      }
      return true;
    });
  }

  function _sortLeads(leads) {
    const fin = l => _leadFin(l);
    const by = {
      recent: (a, b) => (b.status_changed_at || b.added_at || "").localeCompare(a.status_changed_at || a.added_at || ""),
      score:  (a, b) => (fin(b).score || 0) - (fin(a).score || 0),
      price:  (a, b) => (fin(a).price || 0) - (fin(b).price || 0),
      profit: (a, b) => (fin(b).profit ?? -1e12) - (fin(a).profit ?? -1e12),
    }[_kanbanSort] || null;
    return by ? [...leads].sort(by) : leads;
  }

  function _renderKanbanColumns() {
    const board = $("#leads-kanban");
    if (!board) return;
    const filtered = _kanbanVisible();
    const cols = _kanbanColumns;
    const firstKey = cols[0]?.key || "new";
    const kfmt = v => (v == null) ? "" : (Math.abs(v) >= 1000 ? `$${Math.round(v / 1000)}K` : `$${Math.round(v)}`);

    board.innerHTML = cols.map(s => {
      // Leads whose status doesn't match any column fall into the first column.
      let leads = filtered.filter(l => {
        const st = l.status || "new";
        return st === s.key || (s.key === firstKey && !cols.some(c => c.key === st));
      });
      leads = _sortLeads(leads);
      let sumPrice = 0, sumProfit = 0;
      leads.forEach(l => { const f = _leadFin(l); sumPrice += f.price || 0; sumProfit += f.profit || 0; });
      const totals = leads.length
        ? `<div class="kanban-col-totals">${kfmt(sumPrice)}${sumProfit ? ` · <span class="${sumProfit >= 0 ? 'money' : 'risk'}">▲${kfmt(sumProfit)}</span>` : ''}</div>`
        : "";
      return `
        <div class="kanban-col" data-status="${escape(s.key)}" style="border-top-color:${escape(s.color || '#6b7280')};">
          <div class="kanban-col-header">
            <span class="kanban-col-grip" draggable="true" data-col-grip="${escape(s.key)}" title="Drag to reorder">⠿</span>
            <button class="kanban-col-swatch" data-col-color="${escape(s.key)}" style="background:${escape(s.color || '#6b7280')}" title="Change color"></button>
            <span class="kanban-col-title" title="${escape(s.label)}">${escape(s.label)}</span>
            <span class="col-count">${leads.length}</span>
            <span class="kanban-col-tools">
              <button class="kanban-col-btn" data-col-rename="${escape(s.key)}" title="Rename">✎</button>
              <button class="kanban-col-btn" data-col-delete="${escape(s.key)}" title="Delete column">×</button>
            </span>
          </div>
          ${totals}
          <div class="kanban-col-body">
            ${leads.length === 0
              ? `<div class="kanban-empty">Drag a lead here</div>`
              : leads.map(l => _renderKanbanCard(l)).join("")}
          </div>
          <button class="kanban-add-btn" data-add-status="${escape(s.key)}">+ Add lead here</button>
        </div>
      `;
    }).join("")
    + `<button class="kanban-add-col" id="kanban-add-col" title="Add a column">
         <span>＋</span><span class="kanban-add-col-label">Column</span>
       </button>`;
    _wireKanbanEvents();
  }

  function _renderKanbanCard(lead) {
    const addr = lead.address || lead.owner_name || "(no address)";
    const fin = _leadFin(lead);
    const deal = fin.deal;
    const img = lead.image || (lead.images && lead.images[0]) || deal?.image || "";
    const price = fin.price != null ? `$${Math.round(fin.price / 1000)}K` : "";
    const grade = lead.ai_analysis?.result?.flip_grade || lead.grade || deal?.grade;
    const gradeCls = grade ? (/^[AB]/.test(grade) ? "money" : /^[DF]/.test(grade) ? "risk" : "") : "";
    const worthBuying = lead.ai_analysis?.result?.worth_buying;
    const nComments = (lead.comments || []).length;
    const thumb = img
      ? `<div class="kanban-card-thumb" style="background-image:url('${escape(img)}')"></div>`
      : `<div class="kanban-card-thumb empty">🏡</div>`;

    // D — profit potential (+ROI) on the card
    let profitTag = "";
    if (fin.profit != null) {
      const k = Math.round(fin.profit / 1000);
      const roi = fin.roi != null ? ` · ${Math.round(fin.roi)}%` : "";
      const methodLabel = { cash: "cash", hard_money: "hard money", private: "private lender",
                            conventional: "conventional", heloc: "HELOC" }[fin.method] || fin.method;
      const tip = fin.financed
        ? `Net profit after financing (${methodLabel})`
        : (fin.deal ? `Net profit — cash (ARV − price − rehab − 8% selling)`
                    : `Profit potential (ARV − price − rehab − 8% selling costs)`);
      profitTag = `<span class="kanban-card-profit ${fin.profit >= 0 ? 'money' : 'risk'}" title="${tip}">▲ ${k >= 0 ? '$' + k : '-$' + Math.abs(k)}K${roi}</span>`;
    }

    // B — days in stage + follow-up
    const days = _daysSince(lead.status_changed_at || lead.added_at);
    let ageCls = "", ageTitle = "Days in this stage";
    if (days != null && days >= 30) ageCls = "stale-hot";
    else if (days != null && days >= 14) ageCls = "stale";
    const ageTag = days != null ? `<span class="kanban-card-age ${ageCls}" title="${ageTitle}">${days}d</span>` : "";
    let followTag = "";
    if (lead.follow_up) {
      const fd = _daysSince(lead.follow_up);
      const overdue = fd != null && fd >= 0;  // date in the past (or today)
      followTag = `<span class="kanban-card-follow ${overdue ? 'overdue' : ''}" title="Follow-up scheduled">📅 ${escape(lead.follow_up)}</span>`;
    }

    const selected = _kanbanSelected.has(lead.id);
    return `
      <div class="kanban-card${selected ? ' selected' : ''}" draggable="true" data-id="${escape(lead.id)}"${deal ? ` data-deal="${escape(deal.id)}"` : ""}>
        <input type="checkbox" class="kanban-card-check" data-check="${escape(lead.id)}" ${selected ? "checked" : ""} title="Select">
        ${thumb}
        <div class="kanban-card-main">
          <div class="kanban-card-title">${escape(addr)}</div>
          <div class="kanban-card-meta">
            ${price ? `<span class="money">${price}</span>` : ''}
            ${profitTag}
            ${grade ? `<span class="${gradeCls}">${escape(grade)}</span>` : ''}
            ${worthBuying === false ? '<span class="risk">⚠</span>' : ''}
            ${worthBuying === true ? '<span class="money">✓</span>' : ''}
            ${nComments ? `<span class="kanban-card-comments" title="${nComments} comment(s)">💬 ${nComments}</span>` : ''}
            ${ageTag}
            ${followTag}
            ${deal ? '<span class="kanban-card-link" title="View the deal">↗</span>' : ''}
          </div>
        </div>
        <div class="kanban-card-actions">
          <button class="kanban-card-menu" data-menu="${escape(lead.id)}" title="Actions">⋮</button>
          <button class="kanban-card-edit" data-edit="${escape(lead.id)}" title="Notes &amp; status">✎</button>
        </div>
      </div>
    `;
  }

  let _draggedLeadId = null;
  function _wireKanbanEvents() {
    const board = $("#leads-kanban");
    if (!board) return;

    // Column tools: rename / delete / add
    board.querySelectorAll("[data-col-rename]").forEach(b =>
      b.addEventListener("click", e => { e.stopPropagation(); _renameKanbanColumn(b.dataset.colRename); }));
    board.querySelectorAll("[data-col-delete]").forEach(b =>
      b.addEventListener("click", e => { e.stopPropagation(); _deleteKanbanColumn(b.dataset.colDelete); }));
    const addColBtn = board.querySelector("#kanban-add-col");
    if (addColBtn) addColBtn.addEventListener("click", _addKanbanColumn);

    // Column reorder (grip drag) + color swatch
    board.querySelectorAll("[data-col-grip]").forEach(g => {
      g.addEventListener("dragstart", e => {
        _draggedColKey = g.dataset.colGrip;
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/col", _draggedColKey); } catch {}
      });
      g.addEventListener("dragend", () => { _draggedColKey = null; });
    });
    board.querySelectorAll("[data-col-color]").forEach(sw =>
      sw.addEventListener("click", e => { e.stopPropagation(); _openColorPopover(sw.dataset.colColor, sw); }));

    // Card click → open the linked DEAL (if any), else the lead modal.
    // The ✎ button always opens the lead modal (status / notes / comments).
    board.querySelectorAll(".kanban-card").forEach(card => {
      card.querySelector("[data-edit]")?.addEventListener("click", e => {
        e.stopPropagation();
        openLeadModal(card.dataset.id);
      });
      card.querySelector("[data-menu]")?.addEventListener("click", e => {
        e.stopPropagation();
        _openCardMenu(card.dataset.id, e.currentTarget);
      });
      const chk = card.querySelector(".kanban-card-check");
      chk?.addEventListener("click", e => e.stopPropagation());
      chk?.addEventListener("change", e => {
        if (e.target.checked) _kanbanSelected.add(card.dataset.id);
        else _kanbanSelected.delete(card.dataset.id);
        card.classList.toggle("selected", e.target.checked);
        _updateBulkBar();
      });
      card.addEventListener("click", e => {
        if (e.target.closest("[data-edit]") || e.target.closest("[data-menu]")
            || e.target.closest(".kanban-card-check")) return;
        const dealId = card.dataset.deal;
        if (dealId) openDeal(dealId);
        else openLeadModal(card.dataset.id);
      });
      // Drag start
      card.addEventListener("dragstart", e => {
        _draggedLeadId = card.dataset.id;
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", card.dataset.id); } catch {}
      });
      card.addEventListener("dragend", () => {
        card.classList.remove("dragging");
        _draggedLeadId = null;
        board.querySelectorAll(".kanban-col.drag-over")
              .forEach(c => c.classList.remove("drag-over"));
      });
    });

    // Columns as drop targets
    board.querySelectorAll(".kanban-col").forEach(col => {
      col.addEventListener("dragover", e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        col.classList.add("drag-over");
      });
      col.addEventListener("dragleave", e => {
        // Only remove if we're actually leaving (not entering a child)
        if (!col.contains(e.relatedTarget)) col.classList.remove("drag-over");
      });
      col.addEventListener("drop", async e => {
        e.preventDefault();
        col.classList.remove("drag-over");
        // Column reorder takes priority over card move.
        if (_draggedColKey) {
          const fromKey = _draggedColKey; _draggedColKey = null;
          const toKey = col.dataset.status;
          if (fromKey && toKey && fromKey !== toKey) _reorderColumns(fromKey, toKey);
          return;
        }
        const newStatus = col.dataset.status;
        const leadId = _draggedLeadId
                        || e.dataTransfer.getData("text/plain");
        if (!leadId) return;
        const lead = _kanbanLeadsCache.find(l => l.id === leadId);
        if (!lead || lead.status === newStatus) return;
        // Optimistic update
        lead.status = newStatus;
        _renderKanbanColumns();
        try {
          await API.leadPatch(leadId, { status: newStatus });
          toast(`Lead moved to "${newStatus}"`, "success");
        } catch (err) {
          toast("Failed to update: " + err.message, "error");
          renderLeadsKanban();
        }
      });
    });

    // "+ Add lead here" buttons → open the deal picker (pre-filtered to this status)
    board.querySelectorAll("[data-add-status]").forEach(btn => {
      btn.addEventListener("click", () => {
        _newLeadInitialStatus = btn.dataset.addStatus || "new";
        openKanbanDealPicker();
      });
    });
  }
  let _newLeadInitialStatus = "new";
  let _draggedColKey = null;

  function _reorderColumns(fromKey, toKey) {
    const cols = [..._kanbanColumns];
    const fi = cols.findIndex(c => c.key === fromKey);
    const ti = cols.findIndex(c => c.key === toKey);
    if (fi < 0 || ti < 0) return;
    const [moved] = cols.splice(fi, 1);
    cols.splice(ti, 0, moved);
    _kanbanColumns = cols;
    _saveKanbanColumns();
  }

  function _closePopovers() { $$(".kanban-popover").forEach(p => p.remove()); }
  document.addEventListener("click", e => {
    if (!e.target.closest(".kanban-popover") && !e.target.closest("[data-col-color]")
        && !e.target.closest("[data-menu]")) _closePopovers();
  });

  function _openColorPopover(key, anchor) {
    _closePopovers();
    const col = _kanbanColumns.find(c => c.key === key); if (!col) return;
    const pop = document.createElement("div");
    pop.className = "kanban-popover kanban-colors";
    pop.innerHTML = _COLOR_PALETTE.map(c =>
      `<button class="kanban-color-dot" style="background:${c}" data-c="${c}" title="${c}"></button>`).join("");
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.left = r.left + "px";
    pop.style.top = (r.bottom + 5) + "px";
    pop.querySelectorAll("[data-c]").forEach(b => b.addEventListener("click", () => {
      col.color = b.dataset.c; _closePopovers(); _saveKanbanColumns();
    }));
  }

  function _openCardMenu(id, anchor) {
    _closePopovers();
    const pop = document.createElement("div");
    pop.className = "kanban-popover kanban-menu";
    pop.innerHTML = `<div class="kanban-menu-head">Move to</div>`
      + _kanbanColumns.map(c => `<button class="kanban-menu-item" data-move="${escape(c.key)}">→ ${escape(c.label)}</button>`).join("")
      + `<button class="kanban-menu-item danger" data-del="1">🗑 Delete</button>`;
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.left = Math.min(r.left, window.innerWidth - 210) + "px";
    pop.style.top = (r.bottom + 4) + "px";
    pop.querySelectorAll("[data-move]").forEach(b => b.addEventListener("click", async () => {
      _closePopovers();
      const st = b.dataset.move;
      const lead = _kanbanLeadsCache.find(l => l.id === id);
      if (!lead || lead.status === st) return;
      lead.status = st; _renderKanbanColumns();
      try { await API.leadPatch(id, { status: st }); }
      catch (e) { toast(e.message, "error"); renderLeadsKanban(); }
    }));
    pop.querySelector("[data-del]")?.addEventListener("click", async () => {
      _closePopovers();
      if (!confirm("Delete this lead?")) return;
      try { await API.leadDelete(id); _kanbanSelected.delete(id); renderLeadsKanban(); toast("Lead deleted", "success"); }
      catch (e) { toast(e.message, "error"); }
    });
  }

  // Multi-select bulk action bar
  function _clearSelection() { _kanbanSelected.clear(); _updateBulkBar(); _renderKanbanColumns(); }
  function _updateBulkBar() {
    let bar = $("#kanban-bulk-bar");
    const n = _kanbanSelected.size;
    if (!n) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "kanban-bulk-bar"; bar.className = "kanban-bulk-bar";
      document.body.appendChild(bar);
    }
    const opts = _kanbanColumns.map(c => `<option value="${escape(c.key)}">${escape(c.label)}</option>`).join("");
    bar.innerHTML = `<span><b>${n}</b> selected</span>
      <select id="bulk-move-sel">${opts}</select>
      <button class="btn primary" id="bulk-move-btn">Move</button>
      <button class="btn danger" id="bulk-del-btn">Delete</button>
      <button class="btn ghost" id="bulk-clear-btn">Cancel</button>`;
    $("#bulk-move-btn").onclick = async () => {
      const st = $("#bulk-move-sel").value, ids = [..._kanbanSelected];
      ids.forEach(id => { const l = _kanbanLeadsCache.find(x => x.id === id); if (l) l.status = st; });
      _kanbanSelected.clear(); _updateBulkBar(); _renderKanbanColumns();
      for (const id of ids) { try { await API.leadPatch(id, { status: st }); } catch {} }
      toast(`${ids.length} lead(s) moved`, "success");
    };
    $("#bulk-del-btn").onclick = async () => {
      const ids = [..._kanbanSelected];
      if (!confirm(`Delete ${ids.length} lead(s)?`)) return;
      for (const id of ids) { try { await API.leadDelete(id); } catch {} }
      _kanbanSelected.clear(); _updateBulkBar(); renderLeadsKanban();
      toast(`${ids.length} lead(s) deleted`, "success");
    };
    $("#bulk-clear-btn").onclick = () => _clearSelection();
  }

  // Wire the kanban search + "New lead" button (once)
  $("#kanban-search")?.addEventListener("input", e => {
    _kanbanSearchFilter = e.target.value;
    _renderKanbanColumns();
  });
  $("#kanban-sort")?.addEventListener("change", e => { _kanbanSort = e.target.value; _renderKanbanColumns(); });
  $("#kanban-filter-grade")?.addEventListener("change", e => { _kanbanFilters.grade = e.target.value; _renderKanbanColumns(); });
  $("#kanban-filter-worth")?.addEventListener("change", e => { _kanbanFilters.worth = e.target.value; _renderKanbanColumns(); });
  // "New lead" toolbar button → opens the deal picker (any deal can become a lead)
  $("#kanban-add-lead")?.addEventListener("click", () => {
    _newLeadInitialStatus = "new";
    openKanbanDealPicker();
  });
  // "+ Colonne" toolbar button
  $("#kanban-add-col-btn")?.addEventListener("click", _addKanbanColumn);

  // ---- Lead detail modal (status, notes, comments) ----
  let _leadModalId = null;
  function _patchLeadCache(updated) {
    const i = _kanbanLeadsCache.findIndex(l => l.id === updated.id);
    if (i >= 0) _kanbanLeadsCache[i] = updated;
  }
  function _fmtCommentDate(iso) {
    try {
      return new Date(iso).toLocaleString("en-US",
        { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    } catch { return ""; }
  }
  function openLeadModal(id) {
    const lead = _kanbanLeadsCache.find(l => l.id === id);
    const modal = $("#lead-modal");
    if (!lead || !modal) return;
    _leadModalId = id;
    $("#lead-modal-title").textContent = lead.address || lead.owner_name || "Lead";
    $("#lead-modal-sub").textContent = [lead.city, lead.state,
      lead.asking_price ? `$${Math.round(lead.asking_price / 1000)}K` : ""].filter(Boolean).join(" · ");
    const sel = $("#lead-modal-status");
    sel.innerHTML = _kanbanColumns.map(c =>
      `<option value="${escape(c.key)}">${escape(c.label)}</option>`).join("");
    sel.value = lead.status || (_kanbanColumns[0]?.key || "new");
    $("#lead-modal-notes").value = lead.notes || "";
    const fu = $("#lead-modal-follow"); if (fu) fu.value = lead.follow_up || "";
    const od = $("#lead-modal-opendeal");
    if (lead.external_id && (state.deals || []).some(d => d.id === lead.external_id)) {
      od.style.display = "";
      od.onclick = e => { e.preventDefault(); closeLeadModal(); openDeal(lead.external_id); };
    } else { od.style.display = "none"; od.onclick = null; }
    _renderLeadComments(lead);
    $("#lead-comment-input").value = "";
    modal.style.display = "flex";
    setTimeout(() => $("#lead-comment-input")?.focus(), 50);
  }
  function closeLeadModal() { const m = $("#lead-modal"); if (m) m.style.display = "none"; _leadModalId = null; }
  function _renderLeadComments(lead) {
    const list = $("#lead-comments-list");
    if (!list) return;
    const cs = (lead.comments || []).slice().reverse();
    list.innerHTML = cs.length
      ? cs.map(c => `
          <div class="lead-comment">
            <div class="lead-comment-text">${escape(c.text)}</div>
            <div class="lead-comment-foot">
              <span>${_fmtCommentDate(c.created_at)}</span>
              <button class="lead-comment-del" data-cid="${escape(c.id)}" title="Delete">×</button>
            </div>
          </div>`).join("")
      : `<div class="muted" style="font-size:12px;">No comments yet.</div>`;
    list.querySelectorAll(".lead-comment-del").forEach(b =>
      b.addEventListener("click", async () => {
        try {
          const updated = await API.leadDelComment(_leadModalId, b.dataset.cid);
          _patchLeadCache(updated); _renderLeadComments(updated); _renderKanbanColumns();
        } catch (e) { toast(e.message, "error"); }
      }));
  }
  $("#lead-modal-close")?.addEventListener("click", closeLeadModal);
  $("#lead-modal-backdrop")?.addEventListener("click", closeLeadModal);
  $("#lead-comment-add-btn")?.addEventListener("click", async () => {
    const inp = $("#lead-comment-input");
    const text = (inp?.value || "").trim();
    if (!text || !_leadModalId) return;
    try {
      const updated = await API.leadAddComment(_leadModalId, text);
      _patchLeadCache(updated); _renderLeadComments(updated);
      if (inp) inp.value = "";
      _renderKanbanColumns();  // refresh the 💬 badge on the card
    } catch (e) { toast(e.message, "error"); }
  });
  $("#lead-comment-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); $("#lead-comment-add-btn")?.click(); }
  });
  $("#lead-modal-save")?.addEventListener("click", async () => {
    if (!_leadModalId) return;
    try {
      const updated = await API.leadPatch(_leadModalId, {
        status: $("#lead-modal-status").value,
        notes: $("#lead-modal-notes").value,
        follow_up: $("#lead-modal-follow")?.value || "",
      });
      _patchLeadCache(updated);
      toast("Lead saved", "success");
      closeLeadModal(); renderLeadsKanban();
    } catch (e) { toast(e.message, "error"); }
  });
  $("#lead-modal-delete")?.addEventListener("click", async () => {
    if (!_leadModalId) return;
    if (!confirm("Delete this lead?")) return;
    try {
      await API.leadDelete(_leadModalId);
      toast("Lead deleted", "success");
      closeLeadModal(); renderLeadsKanban();
    } catch (e) { toast(e.message, "error"); }
  });

  // ---- "+ From a deal" → modal picker to convert a deal into a lead ----
  function openKanbanDealPicker() {
    const modal = $("#kanban-deal-picker-modal");
    if (!modal) return;
    modal.style.display = "flex";
    const inp = $("#kanban-picker-search");
    if (inp) { inp.value = ""; setTimeout(() => inp.focus(), 50); }
    renderKanbanPickerList("");
  }
  function closeKanbanDealPicker() {
    const modal = $("#kanban-deal-picker-modal");
    if (modal) modal.style.display = "none";
  }

  function renderKanbanPickerList(query) {
    const list = $("#kanban-picker-list");
    if (!list) return;
    const q = (query || "").toLowerCase().trim();
    // Exclude deals that are already in the kanban (matched by address)
    const existing = new Set(
      (_kanbanLeadsCache || []).map(l => (l.address || "").toLowerCase().trim())
    );
    let deals = (state.deals || []).filter(d => {
      if (existing.has((d.address || "").toLowerCase().trim())) return false;
      if (!q) return true;
      const hay = `${d.address || ""} ${d.city || ""} ${d.state || ""} ${d.zip || ""} ${d.signal || ""} ${d.score || ""}`.toLowerCase();
      return hay.includes(q);
    });
    // Sort by score desc
    deals.sort((a, b) => (b.score || 0) - (a.score || 0));

    if (!deals.length) {
      list.innerHTML = `<div class="gdb-empty">
        ${(state.deals || []).length === existing.size
          ? "All your deals are already in the kanban."
          : `No deals match "${escape(q)}".`}
      </div>`;
      return;
    }

    list.innerHTML = deals.map(d => {
      const grade = d.grade || (d.score >= 70 ? "A" : d.score >= 55 ? "B" : d.score >= 40 ? "C" : d.score >= 25 ? "D" : "F");
      const cls = _gdbScoreClass(d.score || 0);
      const sig = (d.signal || "").toLowerCase();
      const sigCls = sig.includes("good") || sig.includes("excellent") ? "good"
                   : sig.includes("avoid") || sig.includes("loss") || sig.includes("risky") ? "bad" : "";
      return `
        <div class="gdb-item" data-id="${escape(d.id)}" style="border-bottom:1px solid var(--divider);">
          <div class="gdb-item-score ${cls}">${d.score ?? "?"}</div>
          <div class="gdb-item-main">
            <div class="gdb-item-addr">${escape(d.address || d.id)}</div>
            <div class="gdb-item-meta">
              ${d.city ? `<span>${escape(d.city)}${d.state ? ', ' + escape(d.state) : ''}</span>` : ''}
              ${d.purchase_price ? `<span>$${Math.round(d.purchase_price/1000)}K</span>` : ''}
              ${d.arv_base ? `<span>→ $${Math.round(d.arv_base/1000)}K ARV</span>` : ''}
            </div>
          </div>
          ${d.signal ? `<span class="gdb-item-signal ${sigCls}">${escape(d.signal)}</span>` : ''}
          <button class="btn primary" style="font-size:11px; padding:5px 10px;">+ Add</button>
        </div>
      `;
    }).join("");

    list.querySelectorAll(".gdb-item").forEach(el => {
      el.addEventListener("click", async () => {
        const id = el.dataset.id;
        await addDealToKanban(id);
      });
    });
  }

  async function addDealToKanban(dealId) {
    const d = (state.deals || []).find(x => x.id === dealId);
    if (!d) { toast("Deal not found", "error"); return; }
    // Build a lead payload from the deal
    const leadPayload = {
      source: "deal_promotion",
      source_url: d.source_url || `internal:deal/${d.id}`,
      lead_price: 0,
      address: d.address || "",
      city: d.city || "",
      state: d.state || "",
      zip: d.zip || "",
      property_type: d.property_type || "Single Family Residence",
      beds: d.beds,
      baths: d.baths,
      sqft: d.sqft,
      year_built: d.year_built,
      asking_price: d.purchase_price,
      estimated_arv: d.arv_base,
      estimated_rehab: d.rehab_base,
      grade: d.grade,
      score: d.score,
      signal: d.signal,
      image: d.image || (d.image_gallery && d.image_gallery[0]) || "",
      description: (
        `[Imported from the deals board]\n\n` +
        `Deal ID: ${d.id}\n` +
        `Score: ${d.score ?? '?'}/100 (${d.grade ?? '?'}, ${d.signal ?? '?'})\n` +
        (d.purchase_price ? `Price: $${d.purchase_price.toLocaleString()}\n` : '') +
        (d.arv_base ? `ARV: $${d.arv_base.toLocaleString()}\n` : '') +
        (d.notes ? `\nNotes:\n${d.notes}` : '')
      ),
      status: _newLeadInitialStatus || "new",
      external_id: d.id,
      ai_analysis: d.ai_insights ? { result: d.ai_insights } : undefined,
    };
    try {
      const saved = await API.leadCreate(leadPayload);
      const col = _kanbanColumns.find(s => s.key === leadPayload.status);
      toast(`✓ "${d.address}" added to the "${col?.label || leadPayload.status}" column`, "success");
      closeKanbanDealPicker();
      _newLeadInitialStatus = "new";  // reset
      await renderLeadsKanban();
    } catch (e) {
      toast("Failed to add: " + e.message, "error");
    }
  }

  // Wire the new buttons
  $("#kanban-add-from-deal")?.addEventListener("click", openKanbanDealPicker);
  $("#kanban-picker-close")?.addEventListener("click", closeKanbanDealPicker);
  $(".modal-backdrop", $("#kanban-deal-picker-modal"))?.addEventListener("click", closeKanbanDealPicker);
  $("#kanban-picker-search")?.addEventListener("input", e => renderKanbanPickerList(e.target.value));
  $("#kanban-picker-search")?.addEventListener("keydown", e => {
    if (e.key === "Escape") closeKanbanDealPicker();
  });

  function renderCrmStats(agg) {
    const grid = $("#crm-stats");
    if (!agg.contacts_count && !agg.interactions_count) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1;">
        <div class="empty-ico">📇</div>
        <h3>No contacts yet</h3>
        <p>Add sellers, agents, contractors, lenders to track every relationship around your deals.</p>
      </div>`;
      return;
    }
    grid.innerHTML = `
      <div class="stat-card"><div class="label">Total Contacts</div><div class="value">${agg.contacts_count}</div></div>
      <div class="stat-card"><div class="label">Activities Logged</div><div class="value">${agg.interactions_count}</div></div>
      <div class="stat-card"><div class="label">Sellers</div><div class="value">${agg.by_role?.seller || 0}</div></div>
      <div class="stat-card"><div class="label">Contractors</div><div class="value">${agg.by_role?.contractor || 0}</div></div>
      <div class="stat-card"><div class="label">Agents</div><div class="value">${agg.by_role?.agent || 0}</div></div>
      <div class="stat-card"><div class="label">Lenders</div><div class="value">${agg.by_role?.lender || 0}</div></div>
    `;
  }

  function renderCrmContacts(contacts) {
    const q = ($("#crm-contact-search")?.value || "").toLowerCase().trim();
    const filtered = q
      ? contacts.filter(c =>
          (c.name + " " + c.role + " " + (c.company||"") + " " + (c.phone||"") + " " + (c.email||""))
            .toLowerCase().includes(q))
      : contacts;
    const list = $("#crm-contacts-list");
    if (!filtered.length) {
      list.innerHTML = '<div class="muted" style="padding:20px; text-align:center;">No contacts.</div>';
      return;
    }
    list.innerHTML = filtered.map(c => `
      <div class="contact-row" data-id="${c.id}">
        <div class="contact-avatar role-${escape(c.role || 'other')}">${escape(initials(c.name))}</div>
        <div class="contact-info">
          <div class="contact-name">${escape(c.name)}
            <span class="pill gray" style="margin-left:6px;">${escape(ROLE_META[c.role]?.label || c.role || 'Other')}</span>
          </div>
          <div class="contact-meta">
            ${c.company ? `<span>${escape(c.company)}</span>` : ''}
            ${c.phone ? `<a href="tel:${escape(c.phone)}" onclick="event.stopPropagation()">${escape(c.phone)}</a>` : ''}
            ${c.email ? `<a href="mailto:${escape(c.email)}" onclick="event.stopPropagation()">${escape(c.email)}</a>` : ''}
            ${c.deal_ids?.length ? `<span>${c.deal_ids.length} deal${c.deal_ids.length>1?'s':''}</span>` : ''}
          </div>
        </div>
        <div class="contact-actions">
          <button data-action="edit" data-id="${c.id}" title="Edit">Edit</button>
          <button data-action="delete" data-id="${c.id}" title="Delete">×</button>
        </div>
      </div>
    `).join("");
    $$(".contact-row", list).forEach(row => {
      row.addEventListener("click", e => {
        if (e.target.closest("[data-action]")) return;
        openContactModal(filtered.find(c => c.id === row.dataset.id));
      });
    });
    $$("[data-action]", list).forEach(b => {
      b.addEventListener("click", async e => {
        e.stopPropagation();
        const id = b.dataset.id;
        if (b.dataset.action === "edit") {
          const c = filtered.find(x => x.id === id);
          openContactModal(c);
        } else if (b.dataset.action === "delete") {
          if (!confirm("Delete this contact and all their logged activities?")) return;
          try { await API.crmDeleteContact(id); toast("Deleted", "success"); refreshCrmView(); }
          catch (e) { toast(e.message, "error"); }
        }
      });
    });
  }

  function renderCrmActivity(interactions, contacts) {
    const list = $("#crm-activity-list");
    if (!interactions.length) {
      list.innerHTML = '<div class="muted" style="padding:20px; text-align:center;">No activity logged yet.</div>';
      return;
    }
    const byId = Object.fromEntries(contacts.map(c => [c.id, c]));
    list.innerHTML = interactions.slice(0, 30).map(i => {
      const c = i.contact_id ? byId[i.contact_id] : null;
      const dealLabel = i.deal_id ? state.deals.find(d => d.id === i.deal_id)?.address : null;
      return `
        <div class="activity-row">
          <div class="activity-icon">${TYPE_ICON[i.type] || '•'}</div>
          <div class="activity-content">
            <div class="activity-subject">${escape(i.subject || (i.type || 'Activity'))}</div>
            ${i.body ? `<div class="activity-body">${escape(i.body)}</div>` : ''}
            <div class="activity-meta">
              <time>${new Date(i.date).toLocaleString()}</time>
              ${c ? `<span class="pill gray">${escape(c.name)}</span>` : ''}
              ${dealLabel ? `<a href="#" data-deal="${escape(i.deal_id)}" style="color:var(--accent);">${escape(dealLabel.split(',')[0])}</a>` : ''}
            </div>
          </div>
        </div>
      `;
    }).join("");
    $$("[data-deal]", list).forEach(a => {
      a.addEventListener("click", e => { e.preventDefault(); openDeal(a.dataset.deal); });
    });
  }

  $("#crm-contact-search")?.addEventListener("input", () => refreshCrmView());

  // ----- Contact modal -----
  let contactModalDealId = null;
  function openContactModal(contact) {
    const m = $("#contact-modal");
    const form = $("#contact-form");
    form.reset();
    $("#contact-modal-title").textContent = contact ? "Edit contact" : "New contact";
    if (contact) {
      Object.entries(contact).forEach(([k, v]) => {
        const el = form.elements[k];
        if (el && !Array.isArray(v)) el.value = v;
      });
      form.dataset.editing = contact.id;
    } else { delete form.dataset.editing; }
    m.style.display = "flex";
  }
  function closeContactModal() { $("#contact-modal").style.display = "none"; }

  $("#contact-modal-close")?.addEventListener("click", closeContactModal);
  $("#contact-modal-cancel")?.addEventListener("click", closeContactModal);
  $(".modal-backdrop", $("#contact-modal"))?.addEventListener("click", closeContactModal);

  $("#contact-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const obj = {};
    fd.forEach((v, k) => obj[k] = v);
    if (e.target.dataset.editing) obj.id = e.target.dataset.editing;
    if (contactModalDealId) obj.deal_ids = [contactModalDealId];
    try {
      await API.crmCreateContact(obj);
      toast("Contact saved", "success");
      closeContactModal();
      if ($("#view-crm").classList.contains("active")) refreshCrmView();
      if (state.currentDealId) {
        const fresh = await API.getDeal(state.currentDealId);
        renderDealCrm(fresh.deal);
      }
      contactModalDealId = null;
    } catch (e) { toast(e.message, "error"); }
  });

  $("#crm-add-contact-btn")?.addEventListener("click", () => openContactModal(null));

  // ----- Interaction modal -----
  let interactionModalDealId = null;
  async function openInteractionModal(dealId) {
    interactionModalDealId = dealId;
    const m = $("#interaction-modal");
    const form = $("#interaction-form");
    form.reset();
    // Populate contact select
    try {
      const contacts = await API.crmContacts();
      const sel = $("#interaction-contact-select");
      sel.innerHTML = `<option value="">— (none) —</option>` +
        contacts.map(c => `<option value="${escape(c.id)}">${escape(c.name)} (${escape(c.role || 'other')})</option>`).join("");
    } catch (e) {}
    m.style.display = "flex";
  }
  function closeInteractionModal() { $("#interaction-modal").style.display = "none"; }
  $("#interaction-modal-close")?.addEventListener("click", closeInteractionModal);
  $("#interaction-modal-cancel")?.addEventListener("click", closeInteractionModal);
  $(".modal-backdrop", $("#interaction-modal"))?.addEventListener("click", closeInteractionModal);

  $("#interaction-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const obj = { deal_id: interactionModalDealId };
    fd.forEach((v, k) => { if (v) obj[k] = v; });
    try {
      await API.crmCreateInteraction(obj);
      toast("Activity logged", "success");
      closeInteractionModal();
      if (state.currentDealId) {
        const fresh = await API.getDeal(state.currentDealId);
        renderDealCrm(fresh.deal);
      }
      if ($("#view-crm").classList.contains("active")) refreshCrmView();
    } catch (e) { toast(e.message, "error"); }
  });

  // ----- Per-deal CRM card -----
  async function renderDealCrm(d) {
    try {
      const [contacts, interactions] = await Promise.all([
        API.crmContacts(d.id), API.crmInteractions({ deal_id: d.id }),
      ]);
      // Contacts list
      const cl = $("#deal-contacts-list");
      cl.innerHTML = contacts.length ? contacts.map(c => `
        <div class="contact-row" data-id="${c.id}">
          <div class="contact-avatar role-${escape(c.role || 'other')}">${escape(initials(c.name))}</div>
          <div class="contact-info">
            <div class="contact-name">${escape(c.name)}</div>
            <div class="contact-meta">
              <span class="pill gray">${escape(ROLE_META[c.role]?.label || c.role || 'Other')}</span>
              ${c.phone ? `<a href="tel:${escape(c.phone)}" onclick="event.stopPropagation()">${escape(c.phone)}</a>` : ''}
              ${c.email ? `<a href="mailto:${escape(c.email)}" onclick="event.stopPropagation()">${escape(c.email)}</a>` : ''}
            </div>
          </div>
        </div>
      `).join("") : '<div class="empty-mini">No contacts linked to this deal yet.</div>';

      $$(".contact-row", cl).forEach(row => {
        row.addEventListener("click", () =>
          openContactModal(contacts.find(c => c.id === row.dataset.id)));
      });

      // Activity timeline
      const al = $("#deal-activity-list");
      al.innerHTML = interactions.length ? interactions.slice(0, 10).map(i => {
        const c = i.contact_id ? contacts.find(x => x.id === i.contact_id) : null;
        return `
          <div class="activity-row">
            <div class="activity-icon">${TYPE_ICON[i.type] || '•'}</div>
            <div class="activity-content">
              <div class="activity-subject">${escape(i.subject || i.type || 'Activity')}</div>
              ${i.body ? `<div class="activity-body">${escape(i.body)}</div>` : ''}
              <div class="activity-meta">
                <time>${new Date(i.date).toLocaleString()}</time>
                ${c ? `<span class="pill gray">${escape(c.name)}</span>` : ''}
              </div>
            </div>
          </div>
        `;
      }).join("") : '<div class="empty-mini">No activity yet — log a call, email, or meeting to start tracking.</div>';
    } catch (e) {
      $("#deal-contacts-list").innerHTML = '<div class="empty-mini muted">Error loading contacts.</div>';
    }
  }

  $("#add-contact-deal")?.addEventListener("click", () => {
    contactModalDealId = state.currentDealId;
    openContactModal(null);
  });
  $("#add-interaction-deal")?.addEventListener("click", () => {
    if (state.currentDealId) openInteractionModal(state.currentDealId);
  });

  // ============== AI CHAT ==============
  // Defensive bindings — if any element is missing, log + continue instead
  // of crashing the IIFE (which would kill all later handlers).
  const _chatFab    = $("#chat-fab");
  const _chatClose  = $("#chat-close");
  const _chatClear  = $("#chat-clear");
  const _chatPanel  = $("#chat-panel");
  console.log("[chat] init — fab:", !!_chatFab, "close:", !!_chatClose,
                "clear:", !!_chatClear, "panel:", !!_chatPanel);

  if (_chatFab) {
    _chatFab.addEventListener("click", e => {
      console.log("[chat] FAB clicked! currentDealId:", state.currentDealId);
      e.stopPropagation();
      openChatBulletproof();
    });
  }
  // Backup: event delegation on document — catches clicks even if FAB
  // wasn't in the DOM at IIFE-init time (e.g., re-rendered)
  document.addEventListener("click", e => {
    const target = e.target.closest("#chat-fab");
    if (target) {
      console.log("[chat] FAB clicked (via delegation)! currentDealId:", state.currentDealId);
      openChatBulletproof();
    }
  });

  // Wrapper around openChat that ALWAYS shows feedback
  function openChatBulletproof() {
    const panel = $("#chat-panel");
    if (!panel) {
      toast("❌ Bug: chat panel not found in the DOM", "error");
      return;
    }
    if (!state.currentDealId) {
      // Try to recover — find a deal to chat about
      const deals = state.deals || [];
      if (deals.length) {
        const top = [...deals].sort((a,b) => (b.score||0) - (a.score||0))[0];
        state.currentDealId = top.id;
        console.log("[chat] auto-set currentDealId to:", top.id);
        openDeal(top.id).then(() => openChat());
        return;
      } else {
        toast("No deal selected — open a deal first", "warn");
        return;
      }
    }
    openChat();
  }

  // Sidebar "Chat with AI" — works even without being on a deal detail.
  // If no deal is currently selected, pick the top-scoring one or prompt.
  const _navOpenChat = $("#nav-open-chat");
  if (_navOpenChat) _navOpenChat.addEventListener("click", async e => {
    e.preventDefault();
    console.log("[chat] sidebar 'Chat with AI' clicked, currentDealId:", state.currentDealId);
    if (!state.currentDealId) {
      // Find a sensible default — top-scoring deal
      try {
        const deals = await API.listDeals();
        if (!deals.length) {
          toast("Create a deal first so you can chat with the AI about it", "warn");
          return;
        }
        deals.sort((a, b) => (b.score || 0) - (a.score || 0));
        const top = deals[0];
        const ok = confirm(
          `No deal selected. Open the chat on the top-scoring deal:\n\n"${top.address || top.id}" (score ${top.score || '?'})?`
        );
        if (!ok) return;
        state.currentDealId = top.id;
        await openDeal(top.id);  // shows detail view + sets currentDealId
        setTimeout(() => openChat(), 300);
      } catch (e) { toast(e.message, "error"); }
    } else {
      openChat();
    }
  });
  if (_chatClose) _chatClose.addEventListener("click", () => {
    if (_chatPanel) _chatPanel.style.display = "none";
  });
  if (_chatClear) _chatClear.addEventListener("click", async () => {
    if (!state.currentDealId) return;
    if (!confirm("Clear chat history for this deal?")) return;
    try {
      await API.aiChatClear(state.currentDealId);
      renderChatMessages([]);
      toast("Chat cleared", "success");
    } catch (e) { toast(e.message, "error"); }
  });

  async function openChat() {
    if (!state.currentDealId) return;
    const panel = $("#chat-panel");
    panel.style.display = "flex";
    const deal = state.deals.find(d => d.id === state.currentDealId);
    $("#chat-deal-address").textContent = deal?.address || "";
    try {
      const history = await API.aiChatHistory(state.currentDealId);
      renderChatMessages(history);
    } catch (e) {
      renderChatMessages([]);
    }
    $("#chat-input").focus();
  }

  function renderChatMessages(history) {
    const wrap = $("#chat-messages");
    if (!history.length) {
      wrap.innerHTML = `<div class="chat-empty">
        <div class="empty-ico">💭</div>
        <strong style="color:var(--text)">Ask anything about this deal.</strong>
        <p style="margin-top:6px;">Claude has full context — ARV, rehab, comps, photos, AI insights. Try a question below.</p>
      </div>`;
      return;
    }
    wrap.innerHTML = history.map(m => {
      if (m.role === "user") {
        return `<div class="chat-msg user">${escape(m.content)}</div>`;
      } else {
        return `<div class="chat-msg assistant">${renderMarkdown(m.content)}
          ${m.model ? `<div class="chat-msg-meta">${escape(m.model)} · ${m.usage?.input_tokens || 0}→${m.usage?.output_tokens || 0} tok</div>` : ''}
        </div>`;
      }
    }).join("");
    wrap.scrollTop = wrap.scrollHeight;
  }

  // Minimal markdown renderer (bold, italic, code, lists, headings, paragraphs)
  function renderMarkdown(text) {
    if (!text) return "";
    let s = escape(text);
    // Code blocks
    s = s.replace(/```([\s\S]*?)```/g, (_, c) => `<pre style="background:var(--bg-2); padding:8px; border-radius:6px; overflow-x:auto; font-size:11px;">${c.trim()}</pre>`);
    // Inline code
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Bold
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // Italic
    s = s.replace(/(?<![*])\*([^*]+)\*(?![*])/g, "<em>$1</em>");
    // Headings
    s = s.replace(/^### (.+)$/gm, "<h4 style='font-size:13px; font-weight:700; margin:8px 0 4px;'>$1</h4>");
    s = s.replace(/^## (.+)$/gm, "<h4 style='font-size:14px; font-weight:700; margin:10px 0 4px;'>$1</h4>");
    // Lists (split into blocks first)
    const blocks = s.split(/\n\n+/).map(b => {
      const trimmed = b.trim();
      // Bullet list
      if (/^[-*•]\s/.test(trimmed)) {
        const items = trimmed.split(/\n/).map(line => line.replace(/^[-*•]\s*/, "")).filter(Boolean);
        return "<ul>" + items.map(it => `<li>${it}</li>`).join("") + "</ul>";
      }
      // Numbered list
      if (/^\d+\.\s/.test(trimmed)) {
        const items = trimmed.split(/\n/).map(line => line.replace(/^\d+\.\s*/, "")).filter(Boolean);
        return "<ol>" + items.map(it => `<li>${it}</li>`).join("") + "</ol>";
      }
      // Otherwise paragraph
      return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
    });
    return blocks.join("");
  }

  const _chatForm  = $("#chat-input-form");
  const _chatInput = $("#chat-input");
  const _chatSugg  = $("#chat-suggestions");
  if (_chatForm) _chatForm.addEventListener("submit", async e => {
    e.preventDefault();
    sendChat();
  });
  if (_chatInput) _chatInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  if (_chatSugg) {
    $$(".chip-btn", _chatSugg).forEach(b => {
      b.addEventListener("click", () => {
        if (_chatInput) _chatInput.value = b.dataset.q;
        sendChat();
      });
    });
  }

  async function sendChat() {
    const input = $("#chat-input");
    const msg = input.value.trim();
    if (!msg || !state.currentDealId) return;
    input.value = "";
    const wrap = $("#chat-messages");
    // Hide empty state if there
    if ($(".chat-empty", wrap)) wrap.innerHTML = "";
    wrap.insertAdjacentHTML("beforeend", `<div class="chat-msg user">${escape(msg)}</div>`);
    const thinking = document.createElement("div");
    thinking.className = "chat-msg thinking";
    thinking.innerHTML = "<span></span><span></span><span></span>";
    wrap.appendChild(thinking);
    wrap.scrollTop = wrap.scrollHeight;
    $("#chat-send").disabled = true;
    try {
      const out = await API.aiChat(state.currentDealId, msg);
      thinking.remove();
      wrap.insertAdjacentHTML("beforeend", `<div class="chat-msg assistant">${renderMarkdown(out.reply)}
        <div class="chat-msg-meta">${escape(out.model || '')} · ${out.usage?.input_tokens || 0}→${out.usage?.output_tokens || 0} tok</div>
      </div>`);
      wrap.scrollTop = wrap.scrollHeight;
    } catch (e) {
      thinking.remove();
      // Detect billing
      const lower = (e.message || "").toLowerCase();
      if (lower.includes("credit") || lower.includes("balance")) {
        wrap.insertAdjacentHTML("beforeend",
          `<div class="chat-msg error">💳 Out of Anthropic credits —
            <a href="https://console.anthropic.com/settings/billing" target="_blank" style="color:white; text-decoration:underline;">top up here ↗</a></div>`);
      } else {
        wrap.insertAdjacentHTML("beforeend", `<div class="chat-msg error">${escape(e.message)}</div>`);
      }
      wrap.scrollTop = wrap.scrollHeight;
    } finally {
      $("#chat-send").disabled = false;
      input.focus();
    }
  }

  // Auto-resize chat textarea (defensive)
  if (_chatInput) _chatInput.addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(120, e.target.scrollHeight) + "px";
  });

  // ============== FAB + extras ==============
  const fabBtn = $("#fab");
  if (fabBtn) fabBtn.addEventListener("click", () => { resetDealForm(); showView("add"); });
  const openCmdkBtn = $("#open-cmdk");
  if (openCmdkBtn) openCmdkBtn.addEventListener("click", () => openCmdk());

  // ============== PDF IMPORT ==============
  let _pdfProperties = [];
  let _pdfSelected = new Set();
  let _pdfDocType = null;
  let _pdfFilename = null;

  function fmtMoneyOrDash(v) {
    return v ? `$${Math.round(v).toLocaleString()}` : `<span class="muted">—</span>`;
  }

  let _pdfXhr = null;
  let _pdfTimer = null;
  let _pdfStartTime = 0;
  let _pdfPendingFile = null;  // File waiting for user to click "Analyze"

  function pdfDbg(msg) {
    console.log("[pdf-debug]", msg);
  }

  // Format file size like "337 KB" or "2.1 MB"
  function _fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  // Format relative time like "il y a 2h"
  function _fmtAge(mtimeSec) {
    const ageSec = Date.now() / 1000 - mtimeSec;
    if (ageSec < 60)          return "just now";
    if (ageSec < 3600)        return `${Math.floor(ageSec / 60)}min ago`;
    if (ageSec < 86400)       return `${Math.floor(ageSec / 3600)}h ago`;
    if (ageSec < 7 * 86400)   return `${Math.floor(ageSec / 86400)}d ago`;
    const d = new Date(mtimeSec * 1000);
    return d.toLocaleDateString("en-US");
  }

  // ---- Persistent "Analysis in progress" notification ----
  // Floats at the top of the screen, stays visible even when navigating views
  let _pdfNotifTimer = null;

  function showPdfNotification(filename) {
    let banner = document.getElementById("pdf-notif-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "pdf-notif-banner";
      banner.style.cssText = `
        position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
        background: linear-gradient(135deg, #8b5cf6, #6366f1);
        color: white; padding: 12px 20px; border-radius: 12px;
        font-size: 13px; font-weight: 600;
        box-shadow: 0 10px 40px rgba(139,92,246,0.4);
        display: flex; align-items: center; gap: 12px;
        z-index: 9999; min-width: 360px; max-width: 600px;
        animation: pdfNotifSlide 0.3s ease;
      `;
      document.body.appendChild(banner);
      // Add the spinner keyframes if not yet present
      if (!document.getElementById("pdf-notif-style")) {
        const st = document.createElement("style");
        st.id = "pdf-notif-style";
        st.textContent = `
          @keyframes pdfNotifSlide {
            from { transform: translate(-50%, -40px); opacity: 0; }
            to   { transform: translate(-50%, 0);     opacity: 1; }
          }
          @keyframes pdfNotifSpin { to { transform: rotate(360deg); } }
          #pdf-notif-banner .spinner-circ {
            width: 18px; height: 18px;
            border: 3px solid rgba(255,255,255,0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: pdfNotifSpin 0.9s linear infinite;
          }
        `;
        document.head.appendChild(st);
      }
    }
    banner.innerHTML = `
      <div class="spinner-circ"></div>
      <div style="flex:1;">
        <div style="font-size:12px; opacity:0.85; text-transform:uppercase; letter-spacing:0.05em;">Analysis in progress</div>
        <div id="pdf-notif-file" style="font-size:14px;">${escape(filename)}</div>
        <div id="pdf-notif-stage" style="font-size:11.5px; opacity:0.85; margin-top:2px;">Starting…</div>
      </div>
      <div id="pdf-notif-elapsed" style="font-size:12px; opacity:0.85; font-variant-numeric:tabular-nums;">0s</div>
    `;
    banner.style.display = "flex";
  }

  function updatePdfNotification(stage, elapsedSec) {
    const stageEl = document.getElementById("pdf-notif-stage");
    if (stageEl && stage) stageEl.textContent = stage;
    const elEl = document.getElementById("pdf-notif-elapsed");
    if (elEl && elapsedSec != null) elEl.textContent = elapsedSec + "s";
  }

  function hidePdfNotification(success, summary) {
    const banner = document.getElementById("pdf-notif-banner");
    if (!banner) return;
    if (success) {
      banner.style.background = "linear-gradient(135deg, #22c55e, #16a34a)";
      banner.innerHTML = `
        <div style="font-size:22px;">✅</div>
        <div style="flex:1;">
          <div style="font-size:12px; opacity:0.9; text-transform:uppercase; letter-spacing:0.05em;">Done</div>
          <div style="font-size:14px;">${escape(summary || "Analysis succeeded")}</div>
        </div>
      `;
      setTimeout(() => { banner.style.display = "none"; }, 3500);
    } else {
      banner.style.background = "linear-gradient(135deg, #ef4444, #dc2626)";
      banner.innerHTML = `
        <div style="font-size:22px;">❌</div>
        <div style="flex:1;">
          <div style="font-size:12px; opacity:0.9; text-transform:uppercase; letter-spacing:0.05em;">Failed</div>
          <div style="font-size:14px;">${escape(summary || "Analysis failed")}</div>
        </div>
      `;
      setTimeout(() => { banner.style.display = "none"; }, 5000);
    }
  }

  // ---- Click "Analyze →" on a PDF → start analysis immediately ----
  async function analyzePdfDirectly(path, filename) {
    const name = filename || path.split("/").pop() || path;
    pdfDbg(`analyzePdfDirectly("${path}")`);
    const target = "deal";  // default — could be made configurable later

    // Persistent notification
    showPdfNotification(name);
    const startTime = Date.now();
    if (_pdfNotifTimer) clearInterval(_pdfNotifTimer);
    _pdfNotifTimer = setInterval(() => {
      const sec = Math.floor((Date.now() - startTime) / 1000);
      updatePdfNotification(null, sec);
    }, 1000);

    toast(`🔄 Analysis of ${name} started…`, "info");
    updatePdfNotification("📤 Sending the path to the backend…", 0);

    try {
      updatePdfNotification("📖 Reading the PDF (text extraction)…");
      // Tiny delay so user sees the stage change
      await new Promise(r => setTimeout(r, 200));

      updatePdfNotification("🧠 Claude Opus 4.8 is analyzing the document (20-60s)…");
      const r = await API.importPdfAnalyzeFromPath(path);

      if (!r.ok) {
        if (_pdfNotifTimer) { clearInterval(_pdfNotifTimer); _pdfNotifTimer = null; }
        hidePdfNotification(false, r.error || "Analysis failed");
        toast("Failed: " + (r.error || "?"), "error");
        return;
      }

      const props = r.properties || [];
      if (props.length === 0) {
        if (_pdfNotifTimer) { clearInterval(_pdfNotifTimer); _pdfNotifTimer = null; }
        hidePdfNotification(false, "No properties found in this PDF");
        toast("No properties found", "warn");
        return;
      }

      updatePdfNotification(`💾 Creating ${props.length} deals…`);
      const commit = await API.importPdfCommit(props, target, r.doc_type, name);
      const s = commit.summary;
      const totalCreated = s.deals + s.leads + s.auctions;

      if (_pdfNotifTimer) { clearInterval(_pdfNotifTimer); _pdfNotifTimer = null; }
      hidePdfNotification(true,
        `${totalCreated} ${target}${totalCreated > 1 ? 's' : ''} created from ${name}`);
      toast(`✓ ${totalCreated} ${target}${totalCreated > 1 ? 's' : ''} created`, "success");

      // Redirect to the relevant view
      setTimeout(() => {
        if (s.deals)         showView("deals");
        else if (s.leads)    showView("leads");
        else if (s.auctions) showView("skiptrace");
      }, 1200);
    } catch (e) {
      if (_pdfNotifTimer) { clearInterval(_pdfNotifTimer); _pdfNotifTimer = null; }
      hidePdfNotification(false, e.message);
      toast("Error: " + e.message, "error");
    }
  }

  async function loadRecentPdfs() {
    const list = $("#pdf-recent-list");
    if (!list) return;
    list.innerHTML = `<div class="muted" style="text-align:center; padding:20px; font-size:12px;">Loading…</div>`;
    try {
      const r = await API.importPdfRecent();
      const pdfs = r.pdfs || [];
      if (!pdfs.length) {
        list.innerHTML = `<div class="empty" style="padding:30px;">
          <div class="empty-ico">📭</div>
          <h4>No PDFs found</h4>
          <p class="muted">Put a PDF in Downloads / Desktop / Documents, then click Refresh.</p>
        </div>`;
        return;
      }
      list.innerHTML = pdfs.map(p => `
        <div class="cmp-deal-card" data-path="${escape(p.path)}" data-name="${escape(p.name)}"
              style="display:flex; align-items:center; gap:10px; padding:10px 12px; margin-bottom:4px; cursor:pointer;"
              role="button" tabindex="0">
          <div style="font-size:24px; flex-shrink:0;">📑</div>
          <div style="flex:1; min-width:0;">
            <div style="font-size:13px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escape(p.name)}</div>
            <div class="muted" style="font-size:11px; margin-top:2px;">
              ${escape(p.folder)} · ${_fmtSize(p.size)} · ${_fmtAge(p.mtime)}
            </div>
          </div>
          <button type="button" class="btn primary" data-act="analyze" style="font-size:12px; padding:6px 12px; flex-shrink:0;">
            Analyze →
          </button>
        </div>
      `).join("");

      // Wire up clicks — direct analysis, skip the "File received" panel
      list.querySelectorAll(".cmp-deal-card").forEach(card => {
        const path = card.dataset.path;
        const name = card.dataset.name;
        card.addEventListener("click", () => analyzePdfDirectly(path, name));
        card.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault(); analyzePdfDirectly(path, name);
          }
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="muted" style="padding:20px; color:var(--red); font-size:12px;">Error: ${escape(e.message)}</div>`;
    }
  }

  function setPdfStep(stepName, status) {
    // status: 'pending' | 'active' | 'done' | 'failed'
    const li = document.querySelector(`#pdf-step-list li[data-step="${stepName}"]`);
    if (!li) return;
    const icons = { pending: "⬜", active: "🔄", done: "✅", failed: "❌" };
    const txt = li.textContent.replace(/^[⬜🔄✅❌]\s*/, "");
    li.textContent = (icons[status] || "⬜") + " " + txt;
    li.style.opacity = (status === "pending") ? "0.4" : "1";
    li.style.fontWeight = (status === "active") ? "600" : "400";
  }

  function setPdfProgress(pct, stageLabel, detail) {
    const bar = $("#pdf-progress-bar");
    if (bar) bar.style.width = Math.min(100, pct) + "%";
    const p = $("#pdf-progress-pct");
    if (p) p.textContent = Math.round(pct) + "%";
    if (stageLabel) {
      const lbl = $("#pdf-stage-label");
      if (lbl) lbl.textContent = stageLabel;
    }
    if (detail != null) {
      const d = $("#pdf-stage-detail");
      if (d) d.textContent = detail;
    }
  }

  function tickPdfTimer() {
    const s = Math.floor((Date.now() - _pdfStartTime) / 1000);
    const el = $("#pdf-elapsed");
    if (el) el.textContent = `${s}s`;
  }

  // ---- STAGE 1: file dropped/picked → show "File received" + Analyze button ----
  function handlePdfFile(file) {
    console.log("[pdf-importer] handlePdfFile called with:", file);
    if (!file) { toast("handlePdfFile called with no file", "error"); return; }
    // Immediate visible feedback BEFORE any further processing
    toast(`📥 ${file.name} selected (${(file.size/1024).toFixed(0)} KB)`, "info");
    // Make sure we're on the Add Deal view (so the "File received" panel is visible)
    if (typeof showView === "function") showView("add");
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      toast(`Unsupported file: "${file.name}" must be a .pdf (type: ${file.type || "unknown"})`, "error");
      return;
    }
    if (file.size === 0) {
      toast("Empty file", "error"); return;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast(`PDF too large: ${(file.size/1024/1024).toFixed(1)} MB (max 25 MB)`, "error");
      return;
    }
    _pdfPendingFile = file;
    _pdfFilename = file.name;

    // Show "File received" panel with Analyze button
    $("#pdf-drop-zone").style.display = "none";
    $("#pdf-analyzing").style.display = "none";
    $("#pdf-result").style.display = "none";
    $("#pdf-received").style.display = "block";
    $("#pdf-received-name").textContent = file.name;
    $("#pdf-received-meta").textContent =
      `${(file.size/1024).toFixed(0)} KB · ${file.type || "application/pdf"}`;
    toast(`✓ File received: ${file.name}`, "success");
  }

  // ---- STAGE 2: user clicked "Analyze" → start XHR upload + Claude ----
  function startPdfAnalysis() {
    const file = _pdfPendingFile;
    if (!file) { toast("No file to analyze", "warn"); return; }
    const target = $("#pdf-received-target")?.value || "deal";

    // Switch from "received" → "analyzing"
    $("#pdf-received").style.display = "none";
    $("#pdf-result").style.display = "none";
    $("#pdf-analyzing").style.display = "block";
    ["upload", "extract", "claude", "render"].forEach(s => setPdfStep(s, "pending"));
    setPdfStep("upload", "active");
    const sizeStr = file.size ? `${(file.size/1024).toFixed(0)} KB` : `path`;
    setPdfProgress(0, "Preparing…", `${file.name} · ${sizeStr} · target: ${target}s`);

    _pdfStartTime = Date.now();
    if (_pdfTimer) clearInterval(_pdfTimer);
    _pdfTimer = setInterval(tickPdfTimer, 1000);

    // PATH MODE — native picker gave us a local path → no upload needed,
    // backend reads the file directly from disk.
    if (file._path) {
      setPdfStep("upload", "done");
      setPdfStep("extract", "active");
      setPdfProgress(30, "Reading the PDF (native mode, no upload)…",
        `Backend reads ${file._path}`);
      const creep = setInterval(() => {
        const bar = $("#pdf-progress-bar");
        if (!bar) return;
        const cur = parseFloat(bar.style.width) || 0;
        if (cur < 90) setPdfProgress(cur + 1, null, null);
      }, 1500);

      setTimeout(() => {
        setPdfStep("extract", "done");
        setPdfStep("claude", "active");
        setPdfProgress(55, "Claude Opus 4.8 is analyzing the PDF…",
          "Claude reads each page. 20-60 seconds.");
      }, 2000);

      (async () => {
        try {
          const r = await API.importPdfAnalyzeFromPath(file._path);
          clearInterval(creep);
          if (!r.ok) {
            setPdfStep("claude", "failed");
            toast("Analysis failed: " + (r.error || "?"), "error");
            if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
            setTimeout(resetPdfImporter, 3000);
            return;
          }
          setPdfStep("claude", "done");
          const props = r.properties || [];
          if (props.length === 0) {
            setPdfStep("render", "failed");
            toast("No properties found in this PDF", "warn");
            if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
            setTimeout(resetPdfImporter, 3500);
            return;
          }
          setPdfStep("render", "active");
          setPdfProgress(85,
            `Creating ${props.length} ${target}${props.length > 1 ? 's' : ''}…`,
            "Inserting into the database…");
          const commit = await API.importPdfCommit(props, target, r.doc_type, _pdfFilename);
          const s = commit.summary;
          setPdfStep("render", "done");
          setPdfProgress(100, "Done!",
            `${s.deals} deals · ${s.leads} leads · ${s.auctions} auctions · ${s.skipped} skipped`);
          if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
          const totalCreated = s.deals + s.leads + s.auctions;
          toast(`✓ ${totalCreated} ${target}${totalCreated > 1 ? 's' : ''} created from the PDF`, "success");
          setTimeout(() => {
            resetPdfImporter();
            if (s.deals)         showView("deals");
            else if (s.leads)    showView("leads");
            else if (s.auctions) showView("skiptrace");
          }, 1500);
        } catch (e) {
          clearInterval(creep);
          setPdfStep("claude", "failed");
          toast("Failed: " + e.message, "error");
          if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
          setTimeout(resetPdfImporter, 3000);
        }
      })();
      return;
    }
    // ELSE: file-based mode (HTML File object) → continue with XHR upload below

    // ---- Upload via XHR (gives us real upload progress) ----
    const xhr = new XMLHttpRequest();
    _pdfXhr = xhr;
    const fd = new FormData();
    fd.append("file", file);

    xhr.open("POST", "/api/import-pdf/analyze");
    xhr.timeout = 5 * 60 * 1000;  // 5 min

    // Upload progress (0% → 30%)
    xhr.upload.addEventListener("progress", e => {
      if (e.lengthComputable) {
        const pct = (e.loaded / e.total) * 30;
        setPdfProgress(pct, "Uploading the file…",
          `${(e.loaded/1024).toFixed(0)} / ${(e.total/1024).toFixed(0)} KB`);
      }
    });

    // Upload complete → server is now processing
    xhr.upload.addEventListener("load", () => {
      setPdfStep("upload", "done");
      setPdfStep("extract", "active");
      setPdfProgress(35, "Extracting text from the PDF…",
        "The backend reads the PDF with pdfplumber. ~1-3 seconds.");
      // After ~2 sec assume we're in Claude territory
      setTimeout(() => {
        if (_pdfXhr !== xhr) return;  // cancelled
        setPdfStep("extract", "done");
        setPdfStep("claude", "active");
        setPdfProgress(55, "Claude Opus 4.8 is analyzing the PDF…",
          "Claude reads each page and identifies every property. This step takes 20 to 60 seconds depending on the PDF size.");
      }, 2500);
    });

    // While Claude is working, slowly creep the bar 55 → 90
    const creepInterval = setInterval(() => {
      const bar = $("#pdf-progress-bar");
      if (!bar) return;
      const cur = parseFloat(bar.style.width) || 0;
      if (cur < 90) setPdfProgress(cur + 0.7, null, null);
    }, 1500);

    xhr.onload = () => {
      clearInterval(creepInterval);
      if (xhr.status < 200 || xhr.status >= 300) {
        let msg = `HTTP ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
        setPdfStep("claude", "failed");
        toast("Analysis failed: " + msg, "error");
        if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
        setTimeout(resetPdfImporter, 3500);
        return;
      }
      let r = null;
      try { r = JSON.parse(xhr.responseText); }
      catch (e) {
        toast("Invalid response from server", "error");
        resetPdfImporter(); return;
      }
      if (!r.ok) {
        setPdfStep("claude", "failed");
        toast("Analysis failed: " + (r.error || "unknown"), "error");
        if (r.raw_text_excerpt) {
          $("#pdf-analyzing").style.display = "none";
          $("#pdf-result").style.display = "block";
          $("#pdf-result-doctype").textContent = "RAW TEXT (could not be parsed)";
          $("#pdf-result-summary").textContent = r.error || "Parse failed";
          $("#pdf-properties-list").innerHTML =
            `<pre style="white-space:pre-wrap; font-size:11px; padding:12px; background:var(--surface-2); border-radius:6px; max-height:400px; overflow-y:auto;">${escape(r.raw_text_excerpt)}</pre>`;
        } else {
          setTimeout(resetPdfImporter, 3000);
        }
        if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
        return;
      }
      // Success! Auto-commit all extracted properties as deals/leads/auctions
      setPdfStep("claude", "done");
      const props = r.properties || [];
      const nFound = props.length;
      const targetType = $("#pdf-received-target")?.value || "deal";

      if (nFound === 0) {
        setPdfStep("render", "failed");
        setPdfProgress(100, "No properties found",
          "Claude couldn't extract any property from this PDF.");
        toast("No properties found in this PDF", "warn");
        if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
        setTimeout(resetPdfImporter, 3500);
        return;
      }

      // Add a new "commit" step so the user sees we're creating deals
      setPdfStep("render", "active");
      setPdfProgress(85, `Automatically creating ${nFound} ${targetType}s…`,
        `Inserting into the database…`);

      // Auto-commit all properties
      (async () => {
        try {
          const commit = await API.importPdfCommit(
            props, targetType, r.doc_type, _pdfFilename
          );
          const s = commit.summary;
          setPdfStep("render", "done");
          setPdfProgress(100, "Done!",
            `${s.deals} deals · ${s.leads} leads · ${s.auctions} auctions · ${s.skipped} skipped`);
          if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
          const totalCreated = s.deals + s.leads + s.auctions;
          toast(
            `✓ ${totalCreated} ${targetType}${totalCreated > 1 ? 's' : ''} created from the PDF`,
            "success"
          );
          // Navigate to the right view after a short pause
          setTimeout(() => {
            resetPdfImporter();
            if (s.deals)         showView("deals");
            else if (s.leads)    showView("leads");
            else if (s.auctions) showView("skiptrace");
          }, 1500);
        } catch (e) {
          setPdfStep("render", "failed");
          toast("Creation failed: " + e.message, "error");
          if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
          // Fallback: show the review list so user can retry manually
          setTimeout(() => renderPdfResult(r), 800);
        }
      })();
    };

    xhr.onerror = () => {
      clearInterval(creepInterval);
      setPdfStep("upload", "failed");
      toast("Network error during upload", "error");
      if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
      setTimeout(resetPdfImporter, 3000);
    };

    xhr.ontimeout = () => {
      clearInterval(creepInterval);
      setPdfStep("claude", "failed");
      toast("Timeout: analysis exceeded 5 minutes", "error");
      if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
      setTimeout(resetPdfImporter, 3000);
    };

    xhr.send(fd);
  }

  function cancelPdfUpload() {
    if (_pdfXhr) { _pdfXhr.abort(); _pdfXhr = null; }
    if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
    toast("Cancelled", "info");
    resetPdfImporter();
  }

  function resetPdfImporter() {
    if (_pdfXhr) { try { _pdfXhr.abort(); } catch {} _pdfXhr = null; }
    if (_pdfTimer) { clearInterval(_pdfTimer); _pdfTimer = null; }
    $("#pdf-drop-zone").style.display = "block";
    const received = $("#pdf-received");
    if (received) received.style.display = "none";
    $("#pdf-analyzing").style.display = "none";
    $("#pdf-result").style.display = "none";
    _pdfPendingFile = null;
    _pdfProperties = [];
    _pdfSelected = new Set();
    const inp = $("#pdf-file-input");
    if (inp) inp.value = "";
  }

  function renderPdfResult(r) {
    _pdfProperties = r.properties || [];
    _pdfDocType = r.doc_type;
    _pdfSelected = new Set(_pdfProperties.map((_, i) => i));  // select all by default

    $("#pdf-analyzing").style.display = "none";
    $("#pdf-result").style.display = "block";

    $("#pdf-result-doctype").textContent = (r.doc_type || "DOCUMENT").toUpperCase().replace(/_/g, " ");
    $("#pdf-result-summary").textContent = r.doc_summary || `${r.properties.length} properties extracted`;
    $("#pdf-result-meta").innerHTML = `
      ${r.page_count || 0} pages · ${r.char_count?.toLocaleString() || 0} chars ·
      ${r.properties.length} properties found
      ${r.truncated ? '<span class="pill yellow" style="margin-left:6px;">⚠ Truncated — PDF too long, some properties may be missing</span>' : ''}
    `;

    // Warnings
    if (r.warnings?.length) {
      $("#pdf-warnings").innerHTML = `
        <div style="background:#fef3c7; color:#78350f; padding:10px; border-radius:6px; border:1px solid #fbbf24; font-size:12px;">
          <strong>⚠ Warnings:</strong>
          <ul style="margin:4px 0 0 16px; padding:0;">
            ${r.warnings.map(w => `<li>${escape(w)}</li>`).join("")}
          </ul>
        </div>`;
    } else {
      $("#pdf-warnings").innerHTML = "";
    }

    renderPdfPropertiesList();
  }

  function renderPdfPropertiesList() {
    const list = $("#pdf-properties-list");
    if (!_pdfProperties.length) {
      list.innerHTML = `<div class="empty" style="padding:30px;">
        <div class="empty-ico">🤷</div>
        <h4>No properties found</h4>
        <p>Claude couldn't extract any property listings from this PDF.</p>
      </div>`;
      return;
    }

    list.innerHTML = _pdfProperties.map((p, idx) => {
      const sel = _pdfSelected.has(idx);
      return `
        <div class="cmp-deal-card ${sel ? 'selected' : ''}" data-idx="${idx}"
              style="margin-bottom:6px; display:flex; align-items:flex-start; padding:10px 12px;">
          <input type="checkbox" data-idx="${idx}" ${sel ? 'checked' : ''}
                  style="margin-top:3px;">
          <div style="flex:1; min-width:0;">
            <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:4px;">
              <strong style="font-size:13px;">${escape(p.address || 'Unknown address')}</strong>
              ${p.case_number ? `<span class="pill gray" style="font-size:10px;">Case ${escape(p.case_number)}</span>` : ''}
              ${p.property_type ? `<span class="pill gray" style="font-size:10px;">${escape(p.property_type)}</span>` : ''}
              ${p.price_type ? `<span class="pill yellow" style="font-size:10px;">${escape(p.price_type)}</span>` : ''}
            </div>
            <div style="display:flex; gap:14px; flex-wrap:wrap; font-size:12px;">
              <span><strong>Price:</strong> ${fmtMoneyOrDash(p.purchase_price)}</span>
              ${p.arv_base ? `<span><strong>ARV:</strong> ${fmtMoneyOrDash(p.arv_base)}</span>` : ''}
              ${p.rehab_base ? `<span><strong>Rehab:</strong> ${fmtMoneyOrDash(p.rehab_base)}</span>` : ''}
              ${p.beds != null ? `<span>${p.beds}b/${p.baths || 0}ba</span>` : ''}
              ${p.sqft ? `<span>${p.sqft.toLocaleString()} sqft</span>` : ''}
              ${p.year_built ? `<span>Built ${p.year_built}</span>` : ''}
              ${p.lot_size ? `<span>${escape(p.lot_size)}</span>` : ''}
              ${p.auction_date ? `<span>📅 ${escape(p.auction_date)}</span>` : ''}
              ${p.parcel_id ? `<span class="muted">Parcel ${escape(p.parcel_id)}</span>` : ''}
            </div>
            ${p.notes ? `<div class="muted" style="font-size:11.5px; margin-top:4px; line-height:1.4;">${escape(p.notes)}</div>` : ''}
          </div>
        </div>
      `;
    }).join("");

    list.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener("change", () => {
        const i = parseInt(cb.dataset.idx, 10);
        if (cb.checked) _pdfSelected.add(i); else _pdfSelected.delete(i);
        cb.closest('.cmp-deal-card').classList.toggle('selected', cb.checked);
        updatePdfCommitCount();
      });
    });
    updatePdfCommitCount();
  }

  function updatePdfCommitCount() {
    const n = _pdfSelected.size;
    const span = $("#pdf-commit-count");
    if (span) span.textContent = n;
    const btn = $("#pdf-commit-btn");
    if (btn) btn.disabled = n === 0;
  }

  async function commitPdfImport() {
    const target = $("#pdf-target")?.value || "deal";
    const selected = Array.from(_pdfSelected).map(i => _pdfProperties[i]);
    if (!selected.length) { toast("Select at least one property", "warn"); return; }

    const btn = $("#pdf-commit-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }

    try {
      const r = await API.importPdfCommit(selected, target, _pdfDocType, _pdfFilename);
      const s = r.summary;
      const parts = [];
      if (s.deals)    parts.push(`${s.deals} deals`);
      if (s.leads)    parts.push(`${s.leads} leads`);
      if (s.auctions) parts.push(`${s.auctions} auctions`);
      if (s.skipped)  parts.push(`${s.skipped} skipped`);
      if (s.errors)   parts.push(`${s.errors} errors`);
      toast(`✓ Imported: ${parts.join(', ') || 'nothing'}`, s.errors ? "warn" : "success");
      resetPdfImporter();
      // Navigate to the appropriate view
      if (s.deals)         showView("deals");
      else if (s.leads)    showView("leads");
      else if (s.auctions) showView("skiptrace");
    } catch (e) {
      toast("Import failed: " + e.message, "error");
      if (btn) btn.disabled = false;
    } finally {
      if (btn) { btn.innerHTML = `Create <span id="pdf-commit-count">${_pdfSelected.size}</span>`; }
    }
  }

  // ============= PIE: native picker via HTTP backend (most reliable) =============
  async function pickPdfNative() {
    console.log("[pdf-importer] pickPdfNative → calling backend native picker…");
    toast("📂 Opening the native picker…", "info");
    try {
      const r = await API.importPdfNativePick();
      console.log("[pdf-importer] backend returned:", r);
      if (r.cancelled) { toast("Cancelled", "info"); return; }
      if (!r.path) { toast("No path received from the picker", "warn"); return; }
      handlePdfPath(r.path);
    } catch (e) {
      console.error("[pdf-importer] backend native picker failed:", e);
      toast("Native picker unavailable: " + e.message + ". Use drag-and-drop or paste the path.", "error");
    }
  }

  function handlePathInput() {
    pdfDbg("handlePathInput called");
    const inp = $("#pdf-path-input");
    if (!inp) { pdfDbg("  ❌ no input element"); return; }
    let path = (inp.value || "").trim();
    pdfDbg(`  raw input: "${path}"`);
    // Strip file:// prefix if user dragged a file in
    if (path.startsWith("file://")) {
      path = decodeURI(path.replace(/^file:\/\//, ""));
      pdfDbg(`  stripped file://: "${path}"`);
    }
    // Strip surrounding quotes
    path = path.replace(/^["']|["']$/g, "");
    if (!path) { toast("Paste a file path", "warn"); pdfDbg("  ❌ empty"); return; }
    if (!path.toLowerCase().endsWith(".pdf")) {
      toast("The file must be a .pdf", "error");
      pdfDbg("  ❌ not .pdf");
      return;
    }
    pdfDbg(`  ✓ calling handlePdfPath("${path}")`);
    handlePdfPath(path);
  }

  // STAGE 1b: file picked via NATIVE dialog (path-based) → show "File received"
  function handlePdfPath(path) {
    pdfDbg(`handlePdfPath("${path}")`);
    const filename = path.split("/").pop() || path;
    if (!filename.toLowerCase().endsWith(".pdf")) {
      toast(`Unsupported file: ${filename}`, "error");
      pdfDbg(`  ❌ rejected — not .pdf`);
      return;
    }
    _pdfPendingFile = { _path: path, name: filename };
    _pdfFilename = filename;
    if (typeof showView === "function") showView("add");
    $("#pdf-drop-zone").style.display = "none";
    $("#pdf-analyzing").style.display = "none";
    $("#pdf-result").style.display = "none";
    const recv = $("#pdf-received");
    if (!recv) { pdfDbg("  ❌ #pdf-received element missing"); return; }
    recv.style.display = "block";
    $("#pdf-received-name").textContent = filename;
    $("#pdf-received-meta").textContent = `📁 ${path}`;
    toast(`✓ File received: ${filename}`, "success");
    pdfDbg(`  ✓ shown "File received" panel — click Analyze to continue`);
  }

  // Wire up the PDF importer once
  {
    const drop = $("#pdf-drop-zone");
    const input = $("#pdf-file-input");
    const pickBtn = $("#pdf-pick-btn");
    console.log("[pdf-importer] init — drop:", !!drop, "input:", !!input, "pickBtn:", !!pickBtn);

    // Click on the "Native macOS picker" button
    if (pickBtn) {
      pickBtn.addEventListener("click", e => {
        e.preventDefault(); e.stopPropagation();
        pickPdfNative();
      });
    }

    // Refresh button for the recent PDFs list
    const refreshBtn = $("#pdf-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", e => {
      e.preventDefault();
      loadRecentPdfs();
    });
    // Initial load (in case we're already on the Add Deal view)
    if ($("#pdf-recent-list")) loadRecentPdfs();
    // Optional path input fallback (only if it exists in the DOM)
    const pathInput = $("#pdf-path-input");
    if (pathInput) {
      pathInput.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); handlePathInput(); }
      });
      pathInput.addEventListener("drop", e => {
        e.preventDefault(); e.stopPropagation();
        const f = e.dataTransfer?.files?.[0];
        if (f && f.path) {
          pathInput.value = f.path;
          handlePathInput();
        } else {
          // Get the text representation (usually the file path)
          const txt = e.dataTransfer.getData("text/plain") || e.dataTransfer.getData("text/uri-list");
          if (txt) {
            pathInput.value = txt;
            handlePathInput();
          }
        }
      });
    }

    // Fallback: also wire up the hidden HTML input in case someone uses it
    if (input) {
      input.addEventListener("change", e => {
        const f = e.target.files?.[0];
        if (f) handlePdfFile(f);
      });
    }
    document.addEventListener("change", e => {
      if (e.target && e.target.id === "pdf-file-input") {
        const f = e.target.files?.[0];
        if (f) handlePdfFile(f);
      }
    });

    if (drop && input) {
      drop.addEventListener("dragenter", e => {
        e.preventDefault();
        e.stopPropagation();
        drop.style.background = "var(--surface-2)";
        drop.style.borderColor = "var(--accent)";
      });
      drop.addEventListener("dragover", e => {
        e.preventDefault();
        e.stopPropagation();
        drop.style.background = "var(--surface-2)";
        drop.style.borderColor = "var(--accent)";
      });
      drop.addEventListener("dragleave", e => {
        e.stopPropagation();
        drop.style.background = "";
        drop.style.borderColor = "";
      });
      drop.addEventListener("drop", e => {
        e.preventDefault();
        e.stopPropagation();
        drop.style.background = "";
        drop.style.borderColor = "";
        // Hide the URL overlay if it somehow appeared
        const ov = $("#dropzone");
        if (ov) ov.style.display = "none";
        const f = e.dataTransfer?.files?.[0];
        if (f) handlePdfFile(f);
        else toast("No file detected in the drop", "warn");
      });
    }
    const allBtn = $("#pdf-select-all");
    if (allBtn) allBtn.addEventListener("click", () => {
      _pdfSelected = new Set(_pdfProperties.map((_, i) => i));
      renderPdfPropertiesList();
    });
    const noneBtn = $("#pdf-select-none");
    if (noneBtn) noneBtn.addEventListener("click", () => {
      _pdfSelected = new Set();
      renderPdfPropertiesList();
    });
    const commit = $("#pdf-commit-btn");
    if (commit) commit.addEventListener("click", commitPdfImport);
    const cancel = $("#pdf-cancel-btn");
    if (cancel) cancel.addEventListener("click", cancelPdfUpload);

    // NEW: "File received" stage → [Analyze] + [Cancel]
    const analyzeBtn = $("#pdf-analyze-btn");
    if (analyzeBtn) analyzeBtn.addEventListener("click", startPdfAnalysis);
    const receivedCancel = $("#pdf-received-cancel");
    if (receivedCancel) receivedCancel.addEventListener("click", resetPdfImporter);
  }

  // ============== COMPARE VIEW ==============
  let _cmpAllDeals = [];
  let _cmpSelected = new Set();

  async function refreshCompareView() {
    try {
      const deals = await API.listDeals();
      _cmpAllDeals = deals;
      renderComparePicker();
    } catch (e) { toast(e.message, "error"); }
  }

  function renderComparePicker() {
    const grid = $("#cmp-deal-grid");
    if (!grid) return;
    if (!_cmpAllDeals.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1;">
        <div class="empty-ico">📋</div>
        <h3>No deals yet</h3>
        <p>Add a deal first, then come back to compare.</p>
      </div>`;
      return;
    }
    grid.innerHTML = _cmpAllDeals.map(d => {
      const isSel = _cmpSelected.has(d.id);
      const score = d.score || "—";
      const grade = d.grade || "—";
      return `
        <label class="cmp-deal-card ${isSel ? 'selected' : ''}" data-id="${escape(d.id)}">
          <input type="checkbox" ${isSel ? 'checked' : ''} data-id="${escape(d.id)}">
          <div class="cmp-deal-info">
            <div class="cmp-deal-addr">${escape(d.address || 'Untitled')}</div>
            <div class="cmp-deal-meta">
              ${d.purchase_price ? `${fmtMoney(d.purchase_price)} → ${fmtMoney(d.arv_base || 0)}` : 'No price set'}
              ${d.beds ? ` · ${d.beds}b/${d.baths || 0}ba` : ''}
            </div>
          </div>
          <span class="cmp-deal-score" data-grade="${escape(grade)}">${score} ${grade !== '—' ? grade : ''}</span>
        </label>
      `;
    }).join("");
    grid.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener("change", () => {
        const id = cb.dataset.id;
        if (cb.checked) _cmpSelected.add(id); else _cmpSelected.delete(id);
        cb.closest('.cmp-deal-card').classList.toggle('selected', cb.checked);
      });
    });
  }

  const cmpAllBtn = $("#cmp-select-all");
  if (cmpAllBtn) cmpAllBtn.addEventListener("click", () => {
    _cmpSelected = new Set(_cmpAllDeals.map(d => d.id));
    renderComparePicker();
  });
  const cmpNoneBtn = $("#cmp-select-none");
  if (cmpNoneBtn) cmpNoneBtn.addEventListener("click", () => {
    _cmpSelected = new Set();
    renderComparePicker();
  });
  const cmpTop5Btn = $("#cmp-select-top5");
  if (cmpTop5Btn) cmpTop5Btn.addEventListener("click", () => {
    const top = [..._cmpAllDeals].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 5);
    _cmpSelected = new Set(top.map(d => d.id));
    renderComparePicker();
  });

  const cmpRunBtn = $("#cmp-run-btn");
  if (cmpRunBtn) cmpRunBtn.addEventListener("click", runCompare);

  async function runCompare() {
    const ids = Array.from(_cmpSelected);
    if (ids.length < 1) { toast("Pick at least 1 deal", "warn"); return; }
    if (ids.length === 1) { toast("Pick 2+ deals to actually compare", "warn"); return; }

    const includeVerdict = $("#cmp-verdict")?.checked;
    const focus = $("#cmp-focus")?.value || null;

    // Hide previous results
    ["cmp-verdict-card", "cmp-winners-grid", "cmp-aggregates",
     "cmp-table-card", "cmp-breakdown-card"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = "none";
    });
    $("#cmp-loading").style.display = "block";
    $("#cmp-loading-msg").textContent = includeVerdict
      ? `Computing metrics for ${ids.length} deals + asking Claude for verdict (~20s)…`
      : `Computing metrics for ${ids.length} deals…`;

    cmpRunBtn.disabled = true;
    try {
      const r = await API.compareDeals(ids, includeVerdict, focus);
      renderCompareResult(r);
      toast(`✓ Compared ${r.deals.length} deals — winner: ${r.best_deal?.address?.split(',')[0]}`,
            "success");
    } catch (e) {
      toast("Compare failed: " + e.message, "error");
    } finally {
      $("#cmp-loading").style.display = "none";
      cmpRunBtn.disabled = false;
    }
  }

  function renderCompareResult(r) {
    if (!r.ok) { toast(r.error || "Compare failed", "error"); return; }

    // 1) AI Verdict
    if (r.ai_verdict && r.ai_verdict.ok) {
      const v = r.ai_verdict;
      const winner = r.deals.find(d => d.id === v.winner_id) || r.best_deal;
      $("#cmp-winner-title").textContent = `Winner: ${winner.address}`;
      $("#cmp-verdict-text").textContent = v.verdict || "";
      $("#cmp-why-winner").innerHTML = (v.why_winner || []).map(w =>
        `<div style="display:flex; gap:8px; align-items:start; margin:4px 0; font-size:13px;">
          <span style="color:var(--green); flex-shrink:0;">✓</span>
          <span>${escape(w)}</span>
        </div>`
      ).join("");
      if (v.portfolio_play) {
        $("#cmp-portfolio-play").innerHTML = `<strong>📊 Portfolio play:</strong> ${escape(v.portfolio_play)}`;
      } else {
        $("#cmp-portfolio-play").innerHTML = "";
      }
      if (v.next_actions?.length) {
        $("#cmp-next-actions").innerHTML = `
          <div class="page-eyebrow" style="margin-bottom:4px;">Next actions</div>
          ${v.next_actions.map(a => `<div style="font-size:13px; margin:3px 0;">→ ${escape(a)}</div>`).join("")}
        `;
      } else {
        $("#cmp-next-actions").innerHTML = "";
      }
      const honorables = v.honorable_mentions || [];
      const avoids = v.avoid || [];
      $("#cmp-honorable-avoid").innerHTML = `
        ${honorables.length ? `<div class="cmp-winner-callout" style="background:rgba(46,125,50,0.07); border:1px solid rgba(46,125,50,0.2);">
          <div class="cmp-winner-label" style="color:var(--green);">🥈 HONORABLE MENTIONS</div>
          ${honorables.map(h => {
            const dd = r.deals.find(d => d.id === h.id);
            return `<div style="margin-top:6px; font-size:12.5px;">
              <strong>${escape(dd?.address?.split(',')[0] || h.id)}:</strong> ${escape(h.reason)}
            </div>`;
          }).join("")}
        </div>` : ''}
        ${avoids.length ? `<div class="cmp-winner-callout" style="background:rgba(198,40,40,0.06); border:1px solid rgba(198,40,40,0.2);">
          <div class="cmp-winner-label" style="color:var(--red);">⚠ AVOID</div>
          ${avoids.map(h => {
            const dd = r.deals.find(d => d.id === h.id);
            return `<div style="margin-top:6px; font-size:12.5px;">
              <strong>${escape(dd?.address?.split(',')[0] || h.id)}:</strong> ${escape(h.reason)}
            </div>`;
          }).join("")}
        </div>` : ''}
      `;
      $("#cmp-verdict-card").style.display = "block";
    } else if (r.verdict_error) {
      toast(r.verdict_error, "warn");
    }

    // 2) Per-category winners
    const wins = r.winners;
    const winRows = [
      ["🏆 Highest Score",         wins.highest_score,    v => `${v} pts`],
      ["💰 Highest profit $",      wins.highest_profit,   v => `$${Math.round(v).toLocaleString()}`],
      ["📈 Highest ROI",           wins.highest_roi,      v => `${(v*100).toFixed(1)}%`],
      ["🏘 Best cap rate (rental)",wins.highest_cap_rate, v => `${(v*100).toFixed(2)}%`],
      ["💵 Best BRRRR cash-flow",  wins.best_brrrr_cf,    v => `$${Math.round(v)}/mo`],
      ["📊 Biggest 70% headroom",  wins.biggest_headroom, v => `$${Math.round(v).toLocaleString()}`],
      ["💼 Lowest cash needed",    wins.lowest_cash_in,   v => `$${Math.round(v).toLocaleString()}`],
      ["🌎 Best market trend",     wins.best_market,      v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}% YoY`],
    ];
    $("#cmp-winners-grid").innerHTML = winRows.filter(([_, w]) => w).map(([label, w, fmt]) => `
      <div class="cmp-winner-callout">
        <div class="cmp-winner-label">${label}</div>
        <div class="cmp-winner-value">${fmt(w.value)}</div>
        <div class="cmp-winner-addr">${escape(w.address)}</div>
      </div>
    `).join("");
    $("#cmp-winners-grid").style.display = "grid";

    // 3) Aggregates
    const agg = r.aggregates;
    const stratPills = Object.entries(agg.by_strategy || {})
      .map(([s, n]) => `<span class="pill ${({FLIP:'green',BRRRR:'gray',RENT:'yellow',WHOLESALE:'orange',PASS:'red'})[s] || 'gray'}">${escape(s)}: ${n}</span>`)
      .join(" ");
    $("#cmp-aggregates").innerHTML = `
      <h3>📊 Aggregate stats</h3>
      <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:14px; margin-top:10px;">
        <div><div class="cmp-winner-label">Deals compared</div><div class="cmp-winner-value">${agg.count}</div></div>
        <div><div class="cmp-winner-label">Avg score</div><div class="cmp-winner-value">${agg.avg_score}</div></div>
        <div><div class="cmp-winner-label">Avg ROI</div><div class="cmp-winner-value">${(agg.avg_roi*100).toFixed(1)}%</div></div>
        <div><div class="cmp-winner-label">Total profit potential</div><div class="cmp-winner-value" style="color:var(--green);">$${Math.round(agg.total_profit_potential).toLocaleString()}</div></div>
        <div><div class="cmp-winner-label">Total cash needed</div><div class="cmp-winner-value">$${Math.round(agg.total_cash_needed).toLocaleString()}</div></div>
      </div>
      <div style="margin-top:14px; display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
        <span style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; margin-right:6px;">Recommended strategies:</span>
        ${stratPills}
      </div>
    `;
    $("#cmp-aggregates").style.display = "block";

    // 4) Side-by-side ranking table
    const cols = [
      { key: "rank",         label: "#", fmt: (d, i) => `<strong>${i+1}</strong>` },
      { key: "address",      label: "Deal", fmt: d => `<strong>${escape((d.address || '').split(',')[0] || d.id)}</strong><br><span class="muted" style="font-size:10.5px;">${escape((d.address || '').split(',').slice(1).join(',').trim())}</span>` },
      { key: "score",        label: "Score", fmt: d => `<span class="pill ${d.grade.startsWith('A') ? 'green' : d.grade === 'B' ? 'yellow' : 'red'}">${d.score} ${d.grade}</span>` },
      { key: "signal",       label: "Signal", fmt: d => d.signal, w: "signal" },
      { key: "purchase_price",label:"Purchase", fmt: d => `$${Math.round(d.purchase_price).toLocaleString()}`, w: "lowest_cash_in" },
      { key: "arv_base",     label:"ARV", fmt: d => `$${Math.round(d.arv_base).toLocaleString()}` },
      { key: "rehab_base",   label:"Rehab", fmt: d => `$${Math.round(d.rehab_base).toLocaleString()}` },
      { key: "net_profit",   label:"Profit $", fmt: d => `<strong style="color:${d.net_profit > 0 ? 'var(--green)' : 'var(--red)'};">$${Math.round(d.net_profit).toLocaleString()}</strong>`, w: "highest_profit" },
      { key: "roi",          label:"ROI %", fmt: d => `<strong>${(d.roi*100).toFixed(1)}%</strong>`, w: "highest_roi" },
      { key: "annualized_roi",label:"Ann. ROI", fmt: d => `${(d.annualized_roi*100).toFixed(1)}%` },
      { key: "cash_in",      label:"Cash in", fmt: d => `$${Math.round(d.cash_in).toLocaleString()}` },
      { key: "rule_status",  label:"70% rule", fmt: d => `<span class="pill ${d.rule_status === 'PASS' ? 'green' : 'red'}">${d.rule_status}</span>` },
      { key: "purchase_headroom",label:"Headroom (15%)", fmt: d => `$${Math.round(d.purchase_headroom).toLocaleString()}` },
      { key: "cap_rate",     label:"Cap rate", fmt: d => `${(d.cap_rate*100).toFixed(2)}%`, w: "highest_cap_rate" },
      { key: "monthly_cf_brrrr", label:"BRRRR CF/mo", fmt: d => `$${Math.round(d.monthly_cf_brrrr)}`, w: "best_brrrr_cf" },
      { key: "recommended_strategy", label:"Strategy", fmt: d => `<span class="pill ${({FLIP:'green',BRRRR:'gray',RENT:'yellow',WHOLESALE:'orange'})[d.recommended_strategy] || 'red'}">${d.recommended_strategy}</span>` },
    ];
    const winnerMap = {};
    Object.entries(wins).forEach(([k, w]) => { if (w) winnerMap[w.id + "::" + k] = true; });

    $("#cmp-thead").innerHTML = cols.map(c => `<th>${c.label}</th>`).join("");
    $("#cmp-tbody").innerHTML = r.deals.map((d, i) => {
      const isWinner = d.id === r.best_deal?.id;
      return `<tr class="${isWinner ? 'winner-row' : ''}">${cols.map(c => {
        const isW = c.w && winnerMap[d.id + "::" + c.w];
        return `<td class="${isW ? 'winner' : ''}">${c.fmt(d, i)}</td>`;
      }).join("")}</tr>`;
    }).join("");
    $("#cmp-table-card").style.display = "block";

    // 5) Score breakdown
    const breakdownDims = [
      ["margin_roi", "Margin & ROI (30)"],
      ["arv_confidence", "ARV Confidence (15)"],
      ["seventy_rule", "70% Rule (15)"],
      ["market", "Market (15)"],
      ["rehab_complexity", "Rehab (10)"],
      ["neighborhood", "Neighborhood (10)"],
      ["exit_optionality", "Exits (5)"],
    ];
    const breakdownEl = $("#cmp-breakdown-grid");
    breakdownEl.innerHTML = `
      <div class="cmp-breakdown-row header">
        <div>Deal</div>
        ${breakdownDims.map(([_, l]) => `<div style="text-align:center;">${l}</div>`).join("")}
      </div>
      ${r.deals.map(d => `
        <div class="cmp-breakdown-row">
          <div><strong>${escape((d.address || '').split(',')[0])}</strong><br><span class="muted" style="font-size:10.5px;">${d.score} pts · ${d.grade}</span></div>
          ${breakdownDims.map(([k]) => {
            const b = d.breakdown[k];
            const pct = b.pts / b.max;
            const cls = pct >= 0.7 ? "high" : pct >= 0.4 ? "med" : "low";
            return `<div style="text-align:center;">
              <span class="cmp-breakdown-pill ${cls}" title="${escape(b.label || '')}">${b.pts}/${b.max}</span>
            </div>`;
          }).join("")}
        </div>
      `).join("")}
    `;
    $("#cmp-breakdown-card").style.display = "block";

    // Scroll into view
    document.getElementById("cmp-verdict-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ============== BOOT ==============
  refreshDashboard();
})();
