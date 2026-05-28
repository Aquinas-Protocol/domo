const $ = (sel) => document.querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const AGENT_ORDER = ["main", "comms", "research"];

const AGENT_META = {
  main: {
    callsign: "DAEDALUS",
    display: "Daedalus",
    role: "MAIN AGENT",
    label: "OPERATOR",
    image: "/static/assets/agents/daedalus.png",
    voice: "British Male (authoritative, direct)",
    specialties: ["Strategic Operations", "Pattern Recognition"],
  },
  research: {
    callsign: "INTEL",
    display: "Intel",
    role: "RESEARCH AGENT",
    label: "ANALYST",
    image: "/static/assets/agents/intel.png",
    voice: "British Male (calm, analytical)",
    specialties: ["Analysis", "Intelligence", "Data Synthesis"],
  },
  comms: {
    callsign: "HERMES",
    display: "Hermes",
    role: "COMMS AGENT",
    label: "SPECIALIST",
    image: "/static/assets/agents/hermes.png",
    voice: "American Male (warm, clear)",
    specialties: ["Communications", "Outreach", "Influence"],
  },
};

const VIEW_LABELS = {
  dashboard: ["DASHBOARD", "OVERVIEW"],
  agents: ["AGENTS", "LIVE CHAT"],
  cron: ["CRON / SCHEDULER", "SCHEDULED JOBS"],
  comms: ["COMMS LOG", "LEDGER"],
  system: ["SYSTEM HEALTH", "RUNTIME"],
  memories: ["MEMORIES", "VAULT"],
  settings: ["SETTINGS", "LOCAL"],
};

const state = {
  authed: false,
  agents: [],
  selected: null,
  ledgerRows: [],
  usage: {
    daily_cost_usd: 0,
    daily_runs: 0,
    lifetime_cost_usd: 0,
    lifetime_runs: 0,
    lifetime_tokens: 0,
    lifetime_token_rows: 0,
  },
  credit: null,  // populated by loadCredit() from /api/credit; null until first refresh
  vaultPath: "",
  schedulerRailTab: "scheduled",
  schedulerPageTab: "scheduled",
  clockTimer: null,
  cronJobs: [],
  cronHistory: [],
};

const AGENT_CALLSIGN_INITIAL = { main: "D", research: "I", comms: "H" };

function cronHumanLabel(expr) {
  if (!expr) return "";
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const [m, h, dom, mon, dow] = parts;
  const everyN = /^\*\/(\d+)$/;
  if (dom === "*" && mon === "*" && dow === "*" && /^\d+$/.test(h) && /^\d+$/.test(m)) {
    return `${h.padStart(2, "0")}:${m.padStart(2, "0")} daily`;
  }
  if (dom === "*" && mon === "*" && dow === "*" && h === "*" && everyN.test(m)) {
    return `Every ${m.match(everyN)[1]} minutes`;
  }
  if (dom === "*" && mon === "*" && dow === "*" && everyN.test(h) && m === "0") {
    return `Every ${h.match(everyN)[1]} hours`;
  }
  if (m === "*" && h === "*" && dom === "*" && mon === "*" && dow === "*") {
    return "Every minute";
  }
  if (dom === "*" && mon === "*" && /^\d+$/.test(h) && /^\d+$/.test(m) && /^[0-9,\-]+$/.test(dow)) {
    return `${h.padStart(2, "0")}:${m.padStart(2, "0")} on weekday(s) ${dow}`;
  }
  return expr;
}

function agentMeta(name) {
  const base = AGENT_META[name] || {};
  const agent = state.agents.find((a) => a.name === name);
  const display = agent ? agent.display_name : name;
  return {
    callsign: base.callsign || String(display || name).toUpperCase(),
    display: base.display || display || name,
    role: base.role || "AGENT",
    label: base.label || "ACTIVE",
    image: base.image || "",
    voice: base.voice || "Voice profile pending",
    specialties: base.specialties || ["Operations"],
  };
}

function orderedAgents() {
  const known = AGENT_ORDER
    .map((name) => state.agents.find((a) => a.name === name))
    .filter(Boolean);
  const rest = state.agents.filter((a) => !AGENT_ORDER.includes(a.name));
  return [...known, ...rest];
}

function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = text;
}

function on(sel, eventName, handler) {
  const el = $(sel);
  if (el) el.addEventListener(eventName, handler);
}

function formatTime(value) {
  if (!value) return "--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDate(value) {
  if (!value) return "pending";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusText(agent) {
  if (!agent) return "OFFLINE";
  if (agent.queue_depth > 0) return `${agent.queue_depth} IN FLIGHT`;
  return agent.online ? "ONLINE" : "OFFLINE";
}

function rowSummary(row) {
  return row.summary || row.error_summary || "(pending)";
}

function startClock() {
  if (state.clockTimer) clearInterval(state.clockTimer);
  const tick = () => {
    const now = new Date();
    setText("#clock", now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }));
  };
  tick();
  state.clockTimer = setInterval(tick, 1000);
}

async function boot() {
  startClock();
  const me = await fetch("/api/me", { cache: "no-store" })
    .then((r) => r.json())
    .catch(() => ({ authenticated: false }));

  state.authed = Boolean(me.authenticated);
  if (!state.authed) {
    showLogin();
    return;
  }

  showApp();
  await refreshAll();
  switchView(location.hash ? location.hash.slice(1) : "dashboard");
}

function showLogin() {
  $("#login")?.classList.remove("hidden");
  $("#app")?.classList.add("hidden");
}

function showApp() {
  $("#login")?.classList.add("hidden");
  $("#app")?.classList.remove("hidden");
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, { cache: "no-store", ...options });
  if (res.status === 401) {
    state.authed = false;
    showLogin();
  }
  return res;
}

on("#pin-form", "submit", async (e) => {
  e.preventDefault();
  setText("#pin-error", "");
  const pin = $("#pin-input")?.value || "";
  const res = await fetch("/api/auth/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin }),
  });
  if (!res.ok) {
    setText("#pin-error", "ACCESS DENIED");
    return;
  }
  $("#pin-input").value = "";
  await boot();
});

on("#logout", "click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  state.authed = false;
  showLogin();
});

on("#refresh-all", "click", refreshAll);
on("#system-refresh", "click", refreshAll);
on("#ledger-refresh", "click", refreshLedgerFromControls);

["#ledger-agent", "#ledger-status", "#ledger-window"].forEach((sel) => {
  on(sel, "change", refreshLedgerFromControls);
});

document.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-view]");
  if (nav) switchView(nav.dataset.view);

  const jump = event.target.closest("[data-view-jump]");
  if (jump) switchView(jump.dataset.viewJump);

  const railTab = event.target.closest("[data-scheduler-rail-tab]");
  if (railTab) {
    state.schedulerRailTab = railTab.dataset.schedulerRailTab;
    renderSchedulerRail();
  }

  const pageTab = event.target.closest("[data-scheduler-page-tab]");
  if (pageTab) {
    state.schedulerPageTab = pageTab.dataset.schedulerPageTab;
    renderSchedulerPage();
  }
});

function switchView(name) {
  const next = VIEW_LABELS[name] ? name : "dashboard";
  if (location.hash !== `#${next}`) {
    history.replaceState(null, "", `#${next}`);
  }
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${next}`));
  $$(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === next);
  });

  const [section, mode] = VIEW_LABELS[next];
  setText("#current-section", section);
  setText("#current-mode", mode);

  if (next === "memories" && !state.vaultPath) loadTree("");
  if (next === "comms") refreshLedgerFromControls();
  if (next === "cron") {
    refreshCron();
  }
  if (next === "system") renderSystemDetail();
}

async function refreshCron() {
  await Promise.all([loadCronJobs(), loadCronHistory()]);
  renderSchedulerRail();
  renderSchedulerPage();
}

async function loadCronJobs() {
  const res = await apiFetch("/api/cron/jobs");
  state.cronJobs = res.ok ? await res.json() : [];
}

async function loadCronHistory() {
  const res = await apiFetch("/api/cron/history?since_minutes=10080&limit=100");
  state.cronHistory = res.ok ? await res.json() : [];
}

async function refreshAll() {
  const start = performance.now();
  await loadAgents();
  populateLedgerAgentFilter();
  await Promise.all([loadLedgerSnapshot(), loadUsage(), loadCredit()]);
  renderAll();
  setText("#net-latency", `${Math.max(1, Math.round(performance.now() - start))}MS`);
}

async function loadAgents() {
  const res = await apiFetch("/api/agents");
  if (!res.ok) return;
  state.agents = await res.json();
  if (!state.selected && state.agents.length) {
    state.selected = orderedAgents()[0]?.name || state.agents[0].name;
  }
}

async function loadLedgerSnapshot() {
  state.ledgerRows = await fetchLedger({ sinceMinutes: 10080, limit: 100 });
}

async function loadUsage() {
  const res = await apiFetch("/api/usage");
  if (!res.ok) return;
  state.usage = await res.json();
}

async function loadCredit() {
  // Soft-fail: if /api/credit is unreachable (older bot build, ledger
  // mid-migration, etc.) leave state.credit null so renderMetrics shows
  // a UNAVAILABLE chip instead of crashing the dashboard.
  try {
    const res = await apiFetch("/api/credit");
    if (!res.ok) {
      state.credit = null;
      return;
    }
    state.credit = await res.json();
  } catch {
    state.credit = null;
  }
}

async function fetchLedger({ agent = "", status = "", sinceMinutes = 240, limit = 100 } = {}) {
  const params = new URLSearchParams();
  if (agent) params.set("agent", agent);
  if (status) params.set("status", status);
  params.set("since_minutes", String(sinceMinutes));
  params.set("limit", String(limit));
  const res = await apiFetch(`/api/ledger?${params}`);
  if (!res.ok) return [];
  return res.json();
}

function renderAll() {
  renderWaveform();
  renderMetrics();
  renderAgentCards("#dashboard-agent-cards");
  renderVoiceConfig();
  renderCommsLog("#dashboard-comms-log", state.ledgerRows.slice(0, 7));
  renderHealth();
  renderAgentList();
  renderSystemDetail();
  renderSchedulerRail();
  renderSchedulerPage();
}

function renderWaveform() {
  const target = $("#voice-wave");
  if (!target || target.childElementCount) return;
  const heights = [34, 52, 22, 62, 44, 70, 30, 56, 36, 78, 42, 58, 24, 66, 32, 48, 80, 26, 55, 38, 60, 45, 76, 28, 64, 35, 50, 72, 31, 53, 40, 68, 22, 58, 36, 74, 44, 62, 30, 50, 82, 25, 57, 39, 61, 46, 70, 33, 54, 41, 65, 29, 49, 75, 36, 59, 27, 63, 44, 78];
  for (const h of heights) {
    const bar = document.createElement("span");
    bar.style.setProperty("--bar-height", `${h}%`);
    target.appendChild(bar);
  }
}

function formatMoney(value) {
  const amount = Number(value || 0);
  return `$${amount.toFixed(2)}`;
}

function formatTokenCount(value) {
  const tokens = Number(value || 0);
  if (!Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1_000_000_000) return `${(tokens / 1_000_000_000).toFixed(1)}B`;
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens >= 10_000_000 ? 1 : 2)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(tokens >= 10_000 ? 0 : 1)}K`;
  return String(Math.round(tokens));
}

function renderMetrics() {
  const totalAgents = state.agents.length;
  const onlineAgents = state.agents.filter((a) => a.online).length;
  const activeRows = state.ledgerRows.filter((r) => ["queued", "running"].includes(r.status));
  const failedRows = state.ledgerRows.filter((r) => r.status === "failed");
  const usage = state.usage || {};
  const dailyRuns = Number(usage.daily_runs || 0);
  const lifetimeRuns = Number(usage.lifetime_runs || 0);
  const lifetimeTokenRows = Number(usage.lifetime_token_rows || 0);
  const lifetimeCaption = lifetimeTokenRows === 0
    ? "CAPTURING FROM NEW RUNS"
    : lifetimeTokenRows < lifetimeRuns
      ? "SINCE TOKEN CAPTURE"
      : "ALL-TIME TOTAL";

  setText("#metric-unread", String(activeRows.length));
  setText("#metric-agents-active", `${onlineAgents}/${totalAgents || 0}`);
  setText("#metric-daily-cost", formatMoney(usage.daily_cost_usd));
  setText("#metric-usage-runs", `${dailyRuns} ${dailyRuns === 1 ? "RUN" : "RUNS"} TODAY`);
  setText("#metric-lifetime-tokens", formatTokenCount(usage.lifetime_tokens));
  setText("#metric-lifetime-caption", lifetimeCaption);
  renderCreditTile();
  setText("#health-turns", String(state.ledgerRows.length));
  setText("#health-failures", String(failedRows.length));
}

// Tier -> inline CSS color (uses existing CSS vars; no new stylesheet rules).
// WARN borrows --amber so SOFT_ROUTE can escalate to --red. NORMAL stays
// muted because a healthy tile shouldn't grab attention.
const _CREDIT_TIER_COLOR = {
  normal: "",
  warn: "var(--amber)",
  soft_route: "var(--amber)",
  reject: "var(--red)",
  hard_pause: "var(--red)",
};

function renderCreditTile() {
  const remainingEl = document.querySelector("#metric-credit-remaining");
  const statusEl = document.querySelector("#metric-credit-status");
  const cardEl = document.querySelector("#metric-credit-card");
  if (!remainingEl || !statusEl) return;

  const credit = state.credit;
  if (!credit) {
    remainingEl.textContent = "$--";
    remainingEl.style.color = "";
    statusEl.textContent = "UNAVAILABLE";
    statusEl.style.color = "";
    if (cardEl) cardEl.title = "Credit governor unreachable";
    return;
  }

  const remaining = Number(credit.remaining_credit_usd || 0);
  const tier = String(credit.tier || "normal").toLowerCase();
  const color = _CREDIT_TIER_COLOR[tier] || "";

  remainingEl.textContent = formatMoney(remaining);
  remainingEl.style.color = color;
  statusEl.textContent = tier.toUpperCase().replace("_", " ");
  statusEl.style.color = color;
  if (cardEl) {
    // Full message + cycle window shows on hover for the operator who wants
    // the long form without taking up tile real estate.
    const cycle = credit.cycle_start ? ` (cycle ${credit.cycle_start})` : "";
    cardEl.title = `${credit.message || tier}${cycle}`;
  }
}

function renderAgentCards(selector) {
  const target = $(selector);
  if (!target) return;
  target.innerHTML = "";

  for (const agent of orderedAgents()) {
    // Skip single-task scheduled agents flagged dashboard_visible=false in
    // agents.yaml. They stay in state.agents so ledger/metrics/online-counts
    // still see them — only the visual surfaces filter.
    if (agent.dashboard_visible === false) continue;
    const meta = agentMeta(agent.name);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-card ${agent.online ? "online" : "offline"} ${state.selected === agent.name ? "selected" : ""}`;
    button.dataset.agent = agent.name;
    button.setAttribute("aria-label", `Open ${meta.callsign}`);
    button.innerHTML = `
      <div class="agent-portrait">
        ${meta.image ? `<img src="${meta.image}" alt="${meta.callsign} portrait">` : ""}
      </div>
      <div class="agent-info">
        <span class="agent-symbol" aria-hidden="true"></span>
        <h3>${meta.callsign}</h3>
        <p class="agent-role">${meta.role}</p>
        <p class="agent-status">${statusText(agent)}</p>
        <span class="agent-label">${meta.label}</span>
        <span class="agent-divider"></span>
        <p class="agent-turns">${agent.queue_depth || 0} TURNS<br>ACTIVE IN WAR ROOM</p>
        <div class="agent-specialty">
          <span>SPECIALTY:</span>
          ${meta.specialties.map((item) => `<span>${item}</span>`).join("")}
        </div>
      </div>
    `;
    button.addEventListener("click", () => {
      selectAgent(agent.name);
      switchView("agents");
    });
    target.appendChild(button);
  }
}

function renderVoiceConfig() {
  const target = $("#voice-config-list");
  if (!target) return;
  target.innerHTML = "";

  for (const agent of orderedAgents()) {
    if (agent.dashboard_visible === false) continue;
    const meta = agentMeta(agent.name);
    const row = document.createElement("div");
    row.className = "voice-row";
    row.innerHTML = `
      <button class="play-button disabled" type="button" aria-label="Voice preview unavailable" disabled></button>
      <select aria-label="${meta.callsign} voice profile" disabled>
        <option>${meta.display} (${meta.role.replace(" AGENT", "")})</option>
      </select>
      <span class="voice-desc">${meta.voice}</span>
    `;
    target.appendChild(row);
  }
}

function renderCommsLog(selector, rows) {
  const target = $(selector);
  if (!target) return;
  target.innerHTML = "";

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "NO RECENT COMMUNICATIONS";
    target.appendChild(empty);
    return;
  }

  for (const row of rows) {
    const meta = agentMeta(row.agent_name);
    const line = document.createElement("div");
    line.className = "comms-line";
    line.innerHTML = `
      <span>${formatTime(row.created_at)}</span>
      <span class="agent-name">${meta.callsign.toLowerCase()}</span>
      <span>${row.status}</span>
      <span class="summary-text"></span>
    `;
    line.querySelector(".summary-text").textContent = rowSummary(row);
    target.appendChild(line);
  }
}

function renderHealth() {
  const total = Math.max(state.agents.length, 1);
  const activeCapacity = state.agents.reduce((sum, agent) => sum + (Number(agent.queue_depth) || 0), 0);
  const percent = Math.round((activeCapacity / Math.max(total * 5, 1)) * 100);
  setText("#health-percent", `${percent}%`);
  setText("#health-age", state.authed ? "LIVE" : "LOCAL");
  const ring = $(".health-ring");
  if (ring) ring.style.setProperty("--health", `${percent}%`);
}

function renderAgentList() {
  const target = $("#agent-list");
  if (!target) return;
  target.innerHTML = "";

  for (const agent of orderedAgents()) {
    if (agent.dashboard_visible === false) continue;
    const meta = agentMeta(agent.name);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-list-item ${agent.online ? "online" : "offline"} ${state.selected === agent.name ? "selected" : ""}`;
    button.innerHTML = `
      <span class="agent-list-thumb">${meta.image ? `<img src="${meta.image}" alt="">` : ""}</span>
      <span>
        <span class="agent-list-name">${meta.callsign}</span>
        <span class="agent-list-status">${statusText(agent)}</span>
      </span>
    `;
    button.addEventListener("click", () => selectAgent(agent.name));
    target.appendChild(button);
  }

  if (state.selected) updateChatHeader();
}

function selectAgent(name) {
  state.selected = name;
  updateChatHeader();
  renderAgentList();
  renderAgentCards("#dashboard-agent-cards");
}

function updateChatHeader() {
  const agent = state.agents.find((a) => a.name === state.selected);
  if (!agent) return;
  const meta = agentMeta(agent.name);
  setText("#chat-title", meta.callsign);
  setText("#chat-subtitle", `${meta.role} - ${meta.label}`);
  setText("#chat-status", statusText(agent));
  $("#chat-status")?.classList.toggle("online", agent.online);
  const input = $("#chat-input");
  const submit = $("#chat-submit");
  if (input) input.disabled = false;
  if (submit) submit.disabled = false;
}

on("#chat-form", "submit", async (e) => {
  e.preventDefault();
  if (!state.selected) return;

  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";

  appendMsg("OPERATOR", text, "operator");
  const meta = agentMeta(state.selected);
  const assistantMsg = appendMsg(meta.callsign, "", "agent");
  const pre = assistantMsg.querySelector("pre");
  setText("#chat-status", "TRANSMITTING");

  const res = await apiFetch(`/api/agents/${state.selected}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });

  if (res.status === 401) return;
  if (!res.ok || !res.body) {
    assistantMsg.classList.add("error");
    pre.textContent = `[error: HTTP ${res.status}]`;
    updateChatHeader();
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop();
    for (const eventText of events) {
      const line = eventText.split("\n").find((part) => part.startsWith("data: "));
      if (!line) continue;
      try {
        const data = JSON.parse(line.slice(6));
        handleStreamEvent(data, pre, assistantMsg);
      } catch {
        // Ignore malformed stream fragments; the next chunk may complete them.
      }
    }
  }

  updateChatHeader();
});

function handleStreamEvent(data, pre, msgEl) {
  if (data.kind === "text") {
    pre.textContent += data.text || "";
    msgEl.scrollIntoView({ block: "end" });
    return;
  }

  if (data.kind === "tool_use") {
    const tool = document.createElement("div");
    tool.className = "msg tool";
    tool.textContent = `TOOL ${data.tool_name || "UNKNOWN"}`;
    msgEl.parentElement.insertBefore(tool, msgEl);
    return;
  }

  if (data.kind === "error") {
    msgEl.classList.add("error");
    pre.textContent = data.text || "Unknown error";
    return;
  }

  if (data.kind === "final") {
    const meta = document.createElement("div");
    meta.className = "msg tool";
    const cost = data.cost_usd != null ? ` - $${Number(data.cost_usd || 0).toFixed(4)}` : "";
    meta.textContent = `LEDGER #${data.ledger_id || "?"}${cost}`;
    msgEl.parentElement.appendChild(meta);
    loadAgents().then(() => {
      renderAgentList();
      renderAgentCards("#dashboard-agent-cards");
    });
    Promise.all([loadLedgerSnapshot(), loadUsage()]).then(renderAll);
  }
}

function appendMsg(who, text, kind = "") {
  const log = $("#chat-log");
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.innerHTML = `<div class="who"></div><pre></pre>`;
  div.querySelector(".who").textContent = who;
  div.querySelector("pre").textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function populateLedgerAgentFilter() {
  const select = $("#ledger-agent");
  if (!select) return;
  const current = select.value;
  select.innerHTML = "";

  const all = document.createElement("option");
  all.value = "";
  all.textContent = "ALL";
  select.appendChild(all);

  for (const agent of orderedAgents()) {
    const option = document.createElement("option");
    option.value = agent.name;
    option.textContent = agentMeta(agent.name).callsign;
    select.appendChild(option);
  }

  select.value = current;
}

async function refreshLedgerFromControls() {
  const rows = await fetchLedger({
    agent: $("#ledger-agent")?.value || "",
    status: $("#ledger-status")?.value || "",
    sinceMinutes: Number($("#ledger-window")?.value || 240),
    limit: 100,
  });
  renderLedger(rows);
}

function renderLedger(rows) {
  const target = $("#ledger-rows");
  if (!target) return;
  target.innerHTML = "";

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "ledger-row";
    empty.innerHTML = `<div class="empty-state">NO LEDGER ENTRIES MATCH</div><span></span><span></span><span></span>`;
    target.appendChild(empty);
    return;
  }

  for (const row of rows) {
    const meta = agentMeta(row.agent_name);
    const div = document.createElement("div");
    div.className = "ledger-row";
    div.innerHTML = `
      <div>
        <div class="summary"></div>
        <div class="meta">#${row.id} - ${meta.callsign} - by ${row.triggered_by || "unknown"} - ${formatDate(row.created_at)}</div>
      </div>
      <span class="status ${row.status}">${row.status}</span>
      <span class="meta">${row.cost_usd != null ? `$${Number(row.cost_usd || 0).toFixed(4)}` : ""}</span>
      ${row.discord_msg_url ? `<a href="${row.discord_msg_url}" target="_blank" rel="noopener">OPEN</a>` : `<span></span>`}
    `;
    div.querySelector(".summary").textContent = rowSummary(row);
    target.appendChild(div);
  }
}

function renderSystemDetail() {
  const target = $("#system-detail");
  if (!target) return;
  target.innerHTML = "";
  const online = state.agents.filter((a) => a.online).length;
  const active = state.ledgerRows.filter((r) => ["queued", "running"].includes(r.status)).length;
  const completed = state.ledgerRows.filter((r) => r.status === "completed").length;
  const failed = state.ledgerRows.filter((r) => r.status === "failed").length;

  const cards = [
    ["AGENTS ONLINE", `${online}/${state.agents.length || 0}`],
    ["ACTIVE RUNS", String(active)],
    ["COMPLETED TURNS", String(completed)],
    ["FAILED TURNS", String(failed)],
    ["DASHBOARD BIND", "127.0.0.1"],
    ["AUTH WINDOW", "8 HOURS"],
  ];

  for (const [label, value] of cards) {
    const card = document.createElement("article");
    card.className = "system-card";
    card.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    target.appendChild(card);
  }
}

function renderSchedulerRail() {
  renderSchedulerTabs("[data-scheduler-rail-tab]", state.schedulerRailTab);
  const target = $("#scheduler-rail-jobs");
  if (!target) return;
  target.innerHTML = "";

  if (state.schedulerRailTab === "history") {
    renderSchedulerHistory(target, true);
    return;
  }

  if (!state.cronJobs.length) {
    target.appendChild(emptySchedulerState("NO CRON JOBS"));
    return;
  }
  for (const job of state.cronJobs) {
    target.appendChild(createSchedulerJob(job, true));
  }
}

function renderSchedulerPage() {
  renderSchedulerTabs("[data-scheduler-page-tab]", state.schedulerPageTab);
  const target = $("#scheduler-page-jobs");
  if (!target) return;
  target.innerHTML = "";

  if (state.schedulerPageTab === "history") {
    renderSchedulerHistory(target, false);
    return;
  }

  if (!state.cronJobs.length) {
    target.appendChild(emptySchedulerState("NO CRON JOBS - CLICK + NEW JOB TO CREATE ONE"));
    return;
  }
  for (const job of state.cronJobs) {
    target.appendChild(createSchedulerJob(job, false));
  }
}

function renderSchedulerTabs(selector, active) {
  $$(selector).forEach((button) => {
    const value = button.dataset.schedulerRailTab || button.dataset.schedulerPageTab;
    button.classList.toggle("active", value === active);
  });
}

function emptySchedulerState(text) {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = text;
  return empty;
}

function createSchedulerJob(job, compact) {
  const item = document.createElement("article");
  item.className = `scheduler-job ${compact ? "compact" : ""}`;
  const icon = AGENT_CALLSIGN_INITIAL[job.target_agent] || "?";
  const human = cronHumanLabel(job.cron_expr);
  const enabled = !!job.enabled;
  const desc = job.description || `${job.target_agent} - ${job.prompt}`;
  item.innerHTML = `
    <span class="scheduler-job-icon">${icon}</span>
    <div>
      <h3></h3>
      <p></p>
      <span class="cron-expression">${escapeHtml(job.cron_expr)}</span>
      <span class="cron-human">${escapeHtml(human)}</span>
    </div>
    <button class="ghost-button" type="button" data-cron-run aria-label="Run ${escapeHtml(job.name)} now">RUN</button>
    <button class="schedule-toggle ${enabled ? "on" : ""}" type="button" aria-label="${escapeHtml(job.name)} ${enabled ? "enabled" : "disabled"}"></button>
  `;
  item.querySelector("h3").textContent = job.name;
  item.querySelector("p").textContent = desc;
  item.querySelector(".schedule-toggle").addEventListener("click", () => toggleCronJob(job));
  item.querySelector("[data-cron-run]").addEventListener("click", (e) => runCronJobNow(job, e.currentTarget));
  return item;
}

async function runCronJobNow(job, button) {
  if (button) {
    button.disabled = true;
    button.textContent = "FIRING…";
  }
  const res = await apiFetch(`/api/cron/jobs/${job.id}/run`, { method: "POST" });
  if (button) {
    button.disabled = false;
    button.textContent = res.ok ? "FIRED" : "FAILED";
    setTimeout(() => { if (button.isConnected) button.textContent = "RUN"; }, 2500);
  }
  if (res.ok) await refreshCron();
}

function renderSchedulerHistory(target, compact) {
  const rows = compact ? state.cronHistory.slice(0, 4) : state.cronHistory;
  if (!rows.length) {
    target.appendChild(emptySchedulerState("NO CRON RUNS YET"));
    return;
  }
  // Build a quick id->name lookup for cron jobs (so history shows readable name).
  const nameById = {};
  for (const job of state.cronJobs) {
    nameById[String(job.id)] = job.name;
  }
  for (const row of rows) {
    const triggerId = (row.triggered_by || "").split(":")[1] || "";
    const jobName = nameById[triggerId] || `cron:${triggerId || "?"}`;
    const item = document.createElement("article");
    item.className = "scheduler-history-item";
    item.innerHTML = `
      <strong></strong>
      <span></span>
      <span></span>
    `;
    item.querySelector("strong").textContent = jobName;
    item.querySelector("span:nth-of-type(1)").textContent = `${row.status} - ${formatDate(row.created_at)}`;
    item.querySelector("span:nth-of-type(2)").textContent = rowSummary(row);
    target.appendChild(item);
  }
}

async function toggleCronJob(job) {
  const next = !job.enabled;
  const res = await apiFetch(`/api/cron/jobs/${job.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: next }),
  });
  if (!res.ok) return;
  await loadCronJobs();
  renderSchedulerRail();
  renderSchedulerPage();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---- New-job form panel ----

function showCronForm() {
  const form = $("#cron-new-job-form");
  if (!form) return;
  form.classList.remove("hidden");
  setText("#cron-form-error", "");
  $("#cron-form")?.querySelector("input[name='name']")?.focus();
}

function hideCronForm() {
  $("#cron-new-job-form")?.classList.add("hidden");
  $("#cron-form")?.reset();
  setText("#cron-form-error", "");
}

on("#cron-new-job-rail", "click", showCronForm);
on("#cron-new-job-page", "click", showCronForm);
on("#cron-form-cancel", "click", hideCronForm);

on("#cron-form", "submit", async (e) => {
  e.preventDefault();
  setText("#cron-form-error", "");
  const form = e.target;
  const data = new FormData(form);
  const payload = {
    name: String(data.get("name") || "").trim(),
    description: String(data.get("description") || "").trim() || null,
    cron_expr: String(data.get("cron_expr") || "").trim(),
    target_agent: String(data.get("target_agent") || ""),
    prompt: String(data.get("prompt") || "").trim(),
    fresh_session: data.get("fresh_session") === "on",
    agent_task: data.get("agent_task") === "on",
  };
  const res = await apiFetch("/api/cron/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 401) return;
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    setText("#cron-form-error", detail.detail || `HTTP ${res.status}`);
    return;
  }
  hideCronForm();
  await refreshCron();
});

async function loadTree(path) {
  state.vaultPath = path;
  const res = await apiFetch(`/api/vault/tree?path=${encodeURIComponent(path)}`);
  if (res.status === 401) return;
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: "error" }));
    $("#entries").innerHTML = `<li class="file">${detail.detail || "error"}</li>`;
    return;
  }
  const data = await res.json();
  renderBreadcrumbs(data.path);
  renderEntries(data.entries);
}

function renderBreadcrumbs(path) {
  const target = $("#breadcrumbs");
  if (!target) return;
  target.innerHTML = "";

  const root = document.createElement("a");
  root.textContent = "vault";
  root.dataset.path = "";
  root.addEventListener("click", () => loadTree(""));
  target.appendChild(root);

  let current = "";
  for (const part of path.split("/").filter(Boolean)) {
    target.append(" / ");
    current = current ? `${current}/${part}` : part;
    const link = document.createElement("a");
    link.textContent = part;
    link.dataset.path = current;
    link.addEventListener("click", () => loadTree(link.dataset.path));
    target.appendChild(link);
  }
}

function renderEntries(entries) {
  const target = $("#entries");
  if (!target) return;
  target.innerHTML = "";

  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "file";
    empty.textContent = "empty";
    target.appendChild(empty);
    return;
  }

  for (const entry of entries) {
    const li = document.createElement("li");
    li.className = entry.is_dir ? "dir" : "file";
    li.textContent = entry.name;
    li.addEventListener("click", () => {
      if (entry.is_dir) loadTree(entry.rel);
      else loadFile(entry.rel);
    });
    target.appendChild(li);
  }
}

async function loadFile(path) {
  setText("#vault-file-name", path);
  const res = await apiFetch(`/api/vault/file?path=${encodeURIComponent(path)}`);
  if (res.status === 401) return;
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: "error" }));
    setText("#vault-content", detail.detail || "error");
    return;
  }
  const data = await res.json();
  setText("#vault-content", data.content);
}

boot();
