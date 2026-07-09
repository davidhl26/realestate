// Lightweight API client
const API = (() => {
  // Same-origin by default. For Netlify-hosted frontend + Render-hosted
  // backend, override with: <meta name="api-base" content="https://flip-board.onrender.com">
  // (or set window.FLIPBOARD_API_BASE before this script loads).
  const meta = document.querySelector('meta[name="api-base"]');
  const base = (window.FLIPBOARD_API_BASE
                || (meta && meta.content)
                || "").replace(/\/$/, "");
  async function req(path, opts = {}) {
    const r = await fetch(base + path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      credentials: "include",
      ...opts,
    });
    if (r.status === 401) {
      window.dispatchEvent(new CustomEvent("fb-auth-required"));
      throw new Error("Authentication required");
    }
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        msg = j.detail || j.error || msg;
      } catch {}
      throw new Error(msg);
    }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.blob();
  }
  return {
    authStatus: () => req("/api/auth-status"),
    login: (password) => req("/api/login", { method: "POST", body: JSON.stringify({ password }) }),
    logout: () => req("/api/logout", { method: "POST", body: "{}" }),
    listDeals: () => req("/api/deals"),
    getDeal: (id) => req("/api/deals/" + encodeURIComponent(id)),
    createDeal: (d) => req("/api/deals", { method: "POST", body: JSON.stringify(d) }),
    patchDeal: (id, u) => req("/api/deals/" + encodeURIComponent(id),
      { method: "PATCH", body: JSON.stringify(u) }),
    deleteDeal: (id) => req("/api/deals/" + encodeURIComponent(id), { method: "DELETE" }),
    aggregates: () => req("/api/board/aggregates"),
    dealsDuplicates: () => req("/api/deals-duplicates"),
    dealCompsMap: (id) => req("/api/deals/" + encodeURIComponent(id) + "/comps-map"),
    dealAreaSales: (id) => req("/api/deals/" + encodeURIComponent(id) + "/area-sales", { method: "POST", body: "{}" }),
    watchesList: () => req("/api/watches"),
    watchCreate: (payload) => req("/api/watches", { method: "POST", body: JSON.stringify(payload) }),
    watchDelete: (id) => req("/api/watches/" + encodeURIComponent(id), { method: "DELETE" }),
    watchPatch: (id, u) => req("/api/watches/" + encodeURIComponent(id), { method: "PATCH", body: JSON.stringify(u) }),
    watchRun: (id) => req("/api/watches/" + encodeURIComponent(id) + "/run", { method: "POST", body: "{}" }),
    watchesRunStale: () => req("/api/watches/run-stale", { method: "POST", body: "{}" }),
    radarList: () => req("/api/radar"),
    radarSeen: () => req("/api/radar/seen", { method: "POST", body: "{}" }),
    radarDelete: (id) => req("/api/radar/" + encodeURIComponent(id), { method: "DELETE" }),
    compareDeals: (dealIds, includeVerdict, focus) => req("/api/board/compare",
      { method: "POST", body: JSON.stringify({
        deal_ids: dealIds || [],
        include_verdict: !!includeVerdict,
        focus: focus || null,
      }) }),
    compareAll: () => req("/api/board/compare/all"),
    statesMap: () => req("/api/board/states-map"),

    // ---- PDF Import (extract properties via Claude, bulk-create) ----
    importPdfAnalyze: async (file) => {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/api/import-pdf/analyze", { method: "POST", body: fd });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); msg = j.detail || j.error || msg; } catch {}
        throw new Error(msg);
      }
      return r.json();
    },
    uploadDealDocument: async (dealId, file, applyRehab = true) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("apply_rehab", applyRehab ? "1" : "0");
      const r = await fetch("/api/deals/" + encodeURIComponent(dealId) + "/documents",
        { method: "POST", body: fd, credentials: "include" });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); msg = j.detail || j.error || msg; } catch {}
        throw new Error(msg);
      }
      return r.json();
    },
    deleteDealDocument: (dealId, docId) => req("/api/deals/" + encodeURIComponent(dealId) +
      "/documents/" + encodeURIComponent(docId), { method: "DELETE" }),
    reapplyDealDocument: (dealId, docId) => req("/api/deals/" + encodeURIComponent(dealId) +
      "/documents/" + encodeURIComponent(docId) + "/reapply", { method: "POST", body: "{}" }),
    dealDocumentUrl: (dealId, docId) => "/api/deals/" + encodeURIComponent(dealId) +
      "/documents/" + encodeURIComponent(docId) + "/file",
    addDealComment: (dealId, text) => req("/api/deals/" + encodeURIComponent(dealId) + "/comments",
      { method: "POST", body: JSON.stringify({ text }) }),
    deleteDealComment: (dealId, commentId) => req("/api/deals/" + encodeURIComponent(dealId) +
      "/comments/" + encodeURIComponent(commentId), { method: "DELETE" }),
    importPdfCommit: (properties, target, docType, filename) => req("/api/import-pdf/commit",
      { method: "POST", body: JSON.stringify({
        properties, target: target || "deal",
        doc_type: docType, filename,
      }) }),
    importPdfAnalyzeFromPath: (path) => req("/api/import-pdf/analyze-from-path",
      { method: "POST", body: JSON.stringify({ path }) }),
    importPdfNativePick: () => req("/api/import-pdf/native-pick",
      { method: "POST", body: "{}" }),
    importPdfRecent: () => req("/api/import-pdf/recent"),
    scrape: (url) => req("/api/scrape", { method: "POST", body: JSON.stringify({ url }) }),
    findByAddress: (address) => req("/api/find-by-address",
      { method: "POST", body: JSON.stringify({ address }) }),

    // ---- Batch ----
    batchStart: (inputs, options) => req("/api/batch/start",
      { method: "POST", body: JSON.stringify({ inputs, options: options || {} }) }),
    batchGet: (jobId) => req("/api/batch/" + encodeURIComponent(jobId)),
    batchCancel: (jobId) => req("/api/batch/" + encodeURIComponent(jobId) + "/cancel",
      { method: "POST", body: "{}" }),
    batchPause: (jobId) => req("/api/batch/" + encodeURIComponent(jobId) + "/pause",
      { method: "POST", body: "{}" }),
    batchResume: (jobId) => req("/api/batch/" + encodeURIComponent(jobId) + "/resume",
      { method: "POST", body: "{}" }),
    batchRestart: (jobId) => req("/api/batch/" + encodeURIComponent(jobId) + "/restart",
      { method: "POST", body: "{}" }),
    batchRetryFailed: (jobId) => req("/api/batch/" + encodeURIComponent(jobId) + "/retry-failed",
      { method: "POST", body: "{}" }),
    batchDelete: (jobId) => req("/api/batch/" + encodeURIComponent(jobId), { method: "DELETE" }),
    batchJobs: () => req("/api/batch/jobs"),
    refreshDeal: (id, url) => req("/api/deals/" + encodeURIComponent(id) + "/refresh",
      { method: "POST", body: JSON.stringify(url ? { url } : {}) }),
    aiConfig: () => req("/api/ai/config"),
    saveAiConfig: (cfg) => req("/api/ai/config",
      { method: "POST", body: JSON.stringify(cfg) }),
    researchArv: (body) => req("/api/research-arv",
      { method: "POST", body: JSON.stringify(body) }),
    aiTasks: () => req("/api/ai/tasks"),
    aiRun: (task, dealId, deal) => req("/api/ai/run",
      { method: "POST", body: JSON.stringify(deal ? { task, deal } : { task, deal_id: dealId }) }),
    aiRunAll: (dealId) => req("/api/ai/run-all",
      { method: "POST", body: JSON.stringify({ deal_id: dealId }) }),
    aiClearInsight: (dealId, task) => req(
      `/api/ai/insight/${encodeURIComponent(dealId)}/${encodeURIComponent(task)}`,
      { method: "DELETE" }),

    // ---- AI Chat ----
    aiChat: (dealId, message) => req("/api/ai/chat",
      { method: "POST", body: JSON.stringify({ deal_id: dealId, message }) }),
    aiChatHistory: (dealId) => req(`/api/ai/chat/${encodeURIComponent(dealId)}`),
    aiChatClear: (dealId) => req(`/api/ai/chat/${encodeURIComponent(dealId)}`,
      { method: "DELETE" }),

    // ---- CRM ----
    crmContacts: (dealId) => req(`/api/crm/contacts${dealId ? `?deal_id=${encodeURIComponent(dealId)}` : ''}`),
    crmCreateContact: (c) => req("/api/crm/contacts",
      { method: "POST", body: JSON.stringify(c) }),
    crmUpdateContact: (id, u) => req(`/api/crm/contacts/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(u) }),
    crmDeleteContact: (id) => req(`/api/crm/contacts/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
    crmInteractions: (params) => {
      const qs = new URLSearchParams(params || {}).toString();
      return req(`/api/crm/interactions${qs ? "?" + qs : ""}`);
    },
    crmCreateInteraction: (i) => req("/api/crm/interactions",
      { method: "POST", body: JSON.stringify(i) }),
    crmDeleteInteraction: (id) => req(`/api/crm/interactions/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
    crmAggregates: () => req("/api/crm/aggregates"),

    // ---- Leads ----
    leadsList: () => req("/api/leads"),
    leadsAggregates: () => req("/api/leads/aggregates"),
    leadGet: (id) => req("/api/leads/" + encodeURIComponent(id)),
    leadCreate: (l) => req("/api/leads", { method: "POST", body: JSON.stringify(l) }),
    leadPatch: (id, u) => req("/api/leads/" + encodeURIComponent(id),
      { method: "PATCH", body: JSON.stringify(u) }),
    leadDelete: (id) => req("/api/leads/" + encodeURIComponent(id), { method: "DELETE" }),
    // ---- Kanban columns + comments ----
    kanbanColumns: () => req("/api/kanban/columns"),
    kanbanSetColumns: (columns) => req("/api/kanban/columns",
      { method: "PUT", body: JSON.stringify({ columns }) }),
    leadAddComment: (id, text) => req("/api/leads/" + encodeURIComponent(id) + "/comments",
      { method: "POST", body: JSON.stringify({ text }) }),
    leadDelComment: (id, cid) => req("/api/leads/" + encodeURIComponent(id) +
      "/comments/" + encodeURIComponent(cid), { method: "DELETE" }),
    leadScrape: (url) => req("/api/leads/scrape",
      { method: "POST", body: JSON.stringify({ url }) }),
    leadAnalyze: (id) => req("/api/leads/" + encodeURIComponent(id) + "/analyze",
      { method: "POST", body: "{}" }),
    leadPromote: (id) => req("/api/leads/" + encodeURIComponent(id) + "/promote-to-deal",
      { method: "POST", body: "{}" }),

    // ---- Auctions / Skip-Trace Queue ----
    auctionsList: (status) => req("/api/auctions" + (status ? `?status=${encodeURIComponent(status)}` : "")),
    auctionsStages: () => req("/api/auctions/stages"),
    auctionsGet: (id) => req("/api/auctions/" + encodeURIComponent(id)),
    auctionsCreate: (item) => req("/api/auctions",
      { method: "POST", body: JSON.stringify(item) }),
    auctionsPatch: (id, u) => req("/api/auctions/" + encodeURIComponent(id),
      { method: "PATCH", body: JSON.stringify(u) }),
    auctionsDelete: (id) => req("/api/auctions/" + encodeURIComponent(id),
      { method: "DELETE" }),
    auctionsBulkDelete: (status) => req("/api/auctions/bulk-delete",
      { method: "POST", body: JSON.stringify({ status }) }),
    auctionsImport: (url) => req("/api/auctions/import",
      { method: "POST", body: JSON.stringify({ url }) }),
    auctionsImportSingle: (url) => req("/api/auctions/import-single",
      { method: "POST", body: JSON.stringify({ url }) }),
    auctionsToLead: (id) => req("/api/auctions/" + encodeURIComponent(id) + "/to-lead",
      { method: "POST", body: "{}" }),
    auctionsSkipTrace: (id) => req("/api/auctions/" + encodeURIComponent(id) + "/skip-trace",
      { method: "POST", body: "{}" }),
    auctionsSkipTraceBulk: (status) => req("/api/auctions/skip-trace-bulk",
      { method: "POST", body: JSON.stringify({ status: status || "queued" }) }),
    auctionsSkipTraceStatus: (jobId) => req("/api/auctions/skip-trace-bulk/" + encodeURIComponent(jobId)),
    auctionsSkipTraceCancel: (jobId) => req("/api/auctions/skip-trace-bulk/" + encodeURIComponent(jobId) + "/cancel",
      { method: "POST", body: "{}" }),
    auctionCredsList: () => req("/api/auctions/credentials"),
    auctionCredsSave: (domain, username, password) => req("/api/auctions/credentials",
      { method: "POST", body: JSON.stringify({ domain, username, password }) }),
    auctionCredsDelete: (domain) => req("/api/auctions/credentials/" + encodeURIComponent(domain),
      { method: "DELETE" }),

    // ---- Browser session ----
    browserSessionStatus: () => req("/api/browser-session/status"),
    browserSessionConnect: (opts) => req("/api/browser-session/connect",
      { method: "POST", body: JSON.stringify(opts || {}) }),
    browserSessionReset: () => req("/api/browser-session/reset",
      { method: "POST", body: "{}" }),
    listCookies: () => req("/api/auth-cookies"),
    saveCookie: (domain, cookie) => req("/api/auth-cookies",
      { method: "POST", body: JSON.stringify({ domain, cookie }) }),
    deleteCookie: (domain) => req("/api/auth-cookies/" + encodeURIComponent(domain),
      { method: "DELETE" }),
    dealPdfUrl: (id) => "/api/deals/" + encodeURIComponent(id) + "/pdf?t=" + Date.now(),
    prequalLetterUrl: (id) => "/api/deals/" + encodeURIComponent(id) + "/prequal-letter?t=" + Date.now(),
    rehabEstimate: (id) => req("/api/deals/" + encodeURIComponent(id) + "/rehab-estimate", { method: "POST", body: "{}" }),
    searchListings: (payload) => req("/api/search/listings", { method: "POST", body: JSON.stringify(payload) }),
    auctionAnalyze: (payload) => req("/api/auction/analyze", { method: "POST", body: JSON.stringify(payload) }),
    auctionFind: (payload) => req("/api/auction/find", { method: "POST", body: JSON.stringify(payload) }),
    auctionWatchlist: () => req("/api/auction/watchlist"),
    auctionWatch: (payload) => req("/api/auction/watch", { method: "POST", body: JSON.stringify(payload) }),
    auctionUnwatch: (id) => req("/api/auction/watch/" + encodeURIComponent(id), { method: "DELETE" }),
    auctionRecheck: (id) => req("/api/auction/watch/" + encodeURIComponent(id) + "/recheck", { method: "POST", body: "{}" }),
    auctionRecheckAll: () => req("/api/auction/watchlist/recheck-all", { method: "POST", body: "{}" }),
    comparePdfUrl: () => "/api/board/comparison-pdf?t=" + Date.now(),
    generatePdf: async (id, overrides) => {
      const r = await fetch("/api/deals/" + encodeURIComponent(id) + "/pdf-with-options", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(overrides),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); msg = j.detail || msg; } catch {}
        throw new Error(msg);
      }
      const blob = await r.blob();
      return URL.createObjectURL(blob);
    },
  };
})();
