"use strict";

/*
 * This is deliberately framework-free. The backing endpoints are private and
 * may evolve independently, so the normalizers below accept the documented
 * fields and harmlessly omit unavailable audit payloads.
 */
const ENDPOINTS = {
  overview: "/admin/dashboard-data/overview",
  agents: "/admin/dashboard-data/agents",
  cycles: "/admin/dashboard-data/cycles",
  cycle: (id) => `/admin/dashboard-data/cycles/${encodeURIComponent(id)}`,
  systemControl: (paused) => `/admin/control/${paused ? "pause" : "resume"}`,
  agentControl: (id, paused) => `/admin/agents/${encodeURIComponent(id)}/${paused ? "pause" : "resume"}`,
};
const DASHBOARD_OPERATOR_ID = "dashboard";

const state = {
  window: "30d",
  runId: "",
  agentId: "",
  overview: null,
  agents: [],
  cycles: [],
  selectedAgentId: null,
  selectedCycleId: null,
  loading: false,
  dataReady: false,
  controlLoading: null,
};

const dollars = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 2,
});
const compactDollars = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1,
});
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function first(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function list(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") return Object.values(value);
  return [];
}

function text(value, fallback = "—") {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function numeric(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function amount(value, compact = false) {
  const raw = numeric(value);
  if (raw === null) return "—";
  // Dashboard monetary projections are consistently expressed in micro-dollars.
  const dollarsValue = raw / 1000000;
  return (compact ? compactDollars : dollars).format(dollarsValue);
}

function percent(value) {
  const raw = numeric(value);
  if (raw === null) return "—";
  return `${raw > 0 ? "+" : ""}${(Math.abs(raw) <= 1 ? raw * 100 : raw).toFixed(2)}%`;
}

function date(value, withTime = true) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return text(value);
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", year: withTime ? undefined : "numeric",
    hour: withTime ? "2-digit" : undefined, minute: withTime ? "2-digit" : undefined,
  }).format(parsed);
}

function relativeDate(value) {
  if (!value) return "—";
  const ms = Date.now() - new Date(value).valueOf();
  if (!Number.isFinite(ms)) return text(value);
  const minutes = Math.round(Math.abs(ms) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / 1440)}d`;
}

function classForValue(value) {
  const raw = numeric(value);
  if (raw === null || raw === 0) return "";
  return raw > 0 ? "positive" : "negative";
}

function statusClass(value) {
  const normalized = String(value || "").toLowerCase();
  if (/(fail|error|reject|cancel|abort)/.test(normalized)) return "error";
  if (/(warn|stale|partial|pending|skip)/.test(normalized)) return "warning";
  if (/(success|complete|done|ok|active|fresh)/.test(normalized)) return "success";
  return "";
}

function appendText(parent, value) {
  parent.textContent = text(value);
  return parent;
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) appendText(node, value);
  return node;
}

function query() {
  const params = new URLSearchParams({ window: state.window });
  if (state.runId) params.set("run_id", state.runId);
  if (state.agentId) params.set("agent_id", state.agentId);
  return params.toString();
}

async function request(endpoint) {
  const response = await fetch(`${endpoint}?${query()}`, {
    headers: { Accept: "application/json" }, credentials: "same-origin",
  });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).detail || ""; } catch { /* response is not JSON */ }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function idempotencyKey() {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  return `dashboard-${randomUuid || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
}

async function controlRequest(endpoint) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "X-Operator-Id": DASHBOARD_OPERATOR_ID,
      "Idempotency-Key": idempotencyKey(),
    },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).detail || ""; } catch { /* response is not JSON */ }
    throw new Error(detail || `Control request failed (${response.status})`);
  }
  return response.json();
}

function setConnection(kind, label) {
  const dot = $("#connection-dot");
  dot.className = `status-dot${kind ? ` is-${kind}` : ""}`;
  $("#connection-label").textContent = label;
}

function toast(message, kind = "") {
  const node = element("div", `toast${kind ? ` ${kind}` : ""}`, message);
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 5000);
}

function showTab(name) {
  $$(".nav-link").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.classList.toggle("is-active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $$(".view").forEach((view) => {
    const selected = view.dataset.view === name;
    view.classList.toggle("is-active", selected);
    view.hidden = !selected;
  });
  history.replaceState(null, "", `#${name}`);
}

function updateUrlQuery() {
  const url = new URL(window.location.href);
  url.search = query();
  history.replaceState(null, "", url);
}

function setLoading(value) {
  state.loading = value;
  const button = $("#refresh-button");
  button.disabled = value;
  button.classList.toggle("is-spinning", value);
  syncGlobalControl();
  syncAgentControlButtons();
  if (!value && state.dataReady) renderAgents();
  if (value) setConnection("", "Loading data");
}

async function loadDashboard() {
  state.dataReady = false;
  setLoading(true);
  updateUrlQuery();
  try {
    const [overviewPayload, agentPayload, cyclePayload] = await Promise.all([
      request(ENDPOINTS.overview), request(ENDPOINTS.agents), request(ENDPOINTS.cycles),
    ]);
    state.overview = overviewPayload || {};
    state.agents = list(first(agentPayload.agents, agentPayload.items, agentPayload));
    state.cycles = list(first(cyclePayload.cycles, cyclePayload.items, cyclePayload));
    state.dataReady = true;
    renderAll();
    setConnection("healthy", "Live data");
    $("#updated-at").textContent = `Updated ${new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  } catch (error) {
    state.dataReady = false;
    const message = `Could not load dashboard data: ${error.message}`;
    setConnection("error", "Data unavailable");
    $("#updated-at").textContent = "Last refresh failed";
    toast(message, "error");
    renderFailure(message);
  } finally {
    setLoading(false);
  }
}

function renderFailure(message) {
  const ids = ["activity-list", "agent-list", "cycle-list", "alert-list", "freshness-list", "usage-list"];
  ids.forEach((id) => { $("#" + id).replaceChildren(element("p", "empty-state", message)); });
  $("#leaderboard-body").replaceChildren(tableMessage(7, message));
}

function tableMessage(columns, message) {
  const row = document.createElement("tr");
  const cell = element("td", "table-message", message);
  cell.colSpan = columns;
  row.append(cell);
  return row;
}

function overviewData() {
  const data = state.overview || {};
  if (data.overview || data.summary) return first(data.overview, data.summary, data);
  // The repository deliberately groups aggregates by their concern. Flatten
  // those groups only for the presentation layer.
  return {
    ...data.performance,
    ...data.cycles,
    ...data.usage,
    ...data.alerts,
    filters: data.filters,
    performance_history: data.performance_history,
    open_alert_items: data.open_alerts,
    freshness: data.freshness,
    provider_usage: data.usage_by_provider,
    controls: data.controls,
  };
}

function renderAll() {
  renderAgentOptions();
  renderOverview();
  renderAgents();
  renderCycles();
  renderOperations();
}

function renderAgentOptions() {
  const select = $("#agent-filter");
  const previous = select.value;
  select.replaceChildren(new Option("All agents", ""));
  state.agents.forEach((agent) => {
    const id = first(agent.id, agent.agent_id);
    if (!id) return;
    select.add(new Option(text(first(agent.name, agent.agent_name, id)), id));
  });
  select.value = state.agentId || previous;
}

function metric(label, value, note, valueClass = "") {
  const card = element("article", "metric-card");
  card.append(element("span", "metric-label", label));
  card.append(element("strong", `metric-value ${valueClass}`, value));
  if (note) card.append(element("span", "metric-note", note));
  return card;
}

function renderOverview() {
  const data = overviewData();
  const controls = first(data.controls, {});
  const value = first(data.account_value_micros, data.account_value, data.total_account_value_micros);
  const pnl = first(data.total_pnl_micros, data.pnl_micros, data.total_pnl);
  const returnValue = first(data.return_fraction, data.return_percent, data.return);
  const drawdown = first(data.drawdown_fraction, data.drawdown, data.max_drawdown_fraction);
  const successfulCycles = first(data.successful_cycles, data.completed_cycles, data.cycles_completed);
  const cycleCount = first(data.cycles, data.total_cycles, data.cycle_count);
  const cost = first(
    data.billed_cost_micros,
    data.cost_micros,
    data.total_cost_micros,
    data.provider_cost_micros,
  );
  const alertCount = first(data.open_alerts, data.alert_count, list(data.open_alert_items).length);
  const cards = [
    metric("Account value", amount(value), "Latest calculated value"),
    metric("Total PnL", amount(pnl), "Versus initial cash", classForValue(pnl)),
    metric("Return", percent(returnValue), "Selected period", classForValue(returnValue)),
    metric("Drawdown", percent(drawdown), "Peak to latest", numeric(drawdown) > 0 ? "negative" : ""),
    metric("Cycles", successfulCycles !== undefined ? `${text(successfulCycles)}/${text(cycleCount)}` : text(cycleCount), "Completed / total"),
    metric("Provider cost", amount(cost), alertCount ? `${alertCount} open alert${Number(alertCount) === 1 ? "" : "s"}` : "No open alerts", alertCount ? "warning" : ""),
  ];
  const grid = $("#overview-metrics");
  grid.setAttribute("aria-busy", "false");
  grid.replaceChildren(...cards);

  const paused = Boolean(first(controls.globally_paused, data.globally_paused));
  const status = $("#run-status");
  status.replaceChildren();
  const dot = element("span", `status-dot is-${paused ? "warning" : "healthy"}`);
  dot.setAttribute("aria-hidden", "true");
  status.append(dot, document.createTextNode(paused ? "Run is paused" : "Run is active"));
  syncGlobalControl();

  const leaderboard = list(first(data.leaderboard, data.agent_performance, state.agents));
  renderLeaderboard(leaderboard);
  renderActivity(list(first(data.activity, data.recent_activity, data.events, state.cycles)));
  drawPerformance(first(data.performance_history, data.chart, []));
}

function renderLeaderboard(rows) {
  const body = $("#leaderboard-body");
  if (!rows.length) { body.replaceChildren(tableMessage(7, "No agent performance is available for this period.")); return; }
  const fragment = document.createDocumentFragment();
  rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    const agentCell = element("span", "agent-cell");
    agentCell.append(element("i", `agent-pip agent-${index % 4}`), document.createTextNode(text(first(row.agent_name, row.name, row.agent_id))));
    nameCell.append(agentCell);
    const pnl = first(row.total_pnl_micros, row.pnl_micros, row.total_pnl);
    const ret = first(row.return_fraction, row.return_percent, row.return);
    const dd = first(row.drawdown_fraction, row.drawdown);
    const cells = [nameCell, element("td", "", text(first(row.model_label, row.model, "—"))), element("td", "", amount(first(row.account_value_micros, row.account_value))), element("td", classForValue(pnl), amount(pnl)), element("td", classForValue(ret), percent(ret)), element("td", numeric(dd) > 0 ? "negative" : "", percent(dd)), element("td", "", relativeDate(first(row.performance_calculated_at, row.calculated_at, row.last_cycle_at, row.updated_at)))];
    tr.append(...cells); fragment.append(tr);
  });
  body.replaceChildren(fragment);
}

function renderActivity(events) {
  const root = $("#activity-list");
  if (!events.length) { root.replaceChildren(element("li", "empty-state", "No audit activity is available for this period.")); return; }
  const fragment = document.createDocumentFragment();
  events.slice(0, 8).forEach((event) => {
    const kind = String(first(event.kind, event.type, event.event_type, event.status, "activity")).toLowerCase();
    const item = element("li");
    item.append(element("i", `activity-icon ${statusClass(kind) || (kind.includes("trade") ? "trade" : "")}`));
    const copy = element("div", "activity-copy");
    copy.append(document.createTextNode(text(first(
      event.message,
      event.summary,
      event.title,
      event.action,
      event.failure_reason,
      event.status ? `Cycle ${event.status}` : undefined,
    ))));
    const meta = first(event.agent_name, event.agent, event.cycle_id, event.model);
    if (meta) copy.append(element("small", "", meta));
    item.append(copy, element("time", "activity-time", relativeDate(first(event.occurred_at, event.created_at, event.at, event.started_at))));
    fragment.append(item);
  });
  root.replaceChildren(fragment);
}

function seriesFromPerformance(raw) {
  const items = list(raw);
  if (!items.length) return [];
  if (items[0] && Array.isArray(items[0].points)) return items;
  const byAgent = new Map();
  items.forEach((point) => {
    const name = text(first(point.agent_name, point.agent, "Run"));
    if (!byAgent.has(name)) byAgent.set(name, []);
    byAgent.get(name).push(point);
  });
  return [...byAgent].map(([name, points]) => ({ name, points }));
}

function drawPerformance(raw) {
  const canvas = $("#performance-chart");
  const empty = $(".chart-empty");
  const series = seriesFromPerformance(raw).slice(0, 4);
  const valid = series.filter((item) => item.points.some((point) => numeric(first(point.account_value_micros, point.value_micros, point.value)) !== null));
  const legend = $("#performance-legend");
  legend.replaceChildren(...valid.map((item) => {
    const label = element("span", "legend-item"); label.append(element("i", "legend-swatch"), document.createTextNode(item.name)); return label;
  }));
  empty.hidden = valid.length > 0;
  if (!valid.length) return;
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(box.width * ratio)); canvas.height = Math.max(1, Math.floor(box.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio); context.clearRect(0, 0, box.width, box.height);
  const all = valid.flatMap((item) => item.points.map((point) => ({ x: new Date(first(point.at, point.calculated_at, point.timestamp)).valueOf(), y: numeric(first(point.account_value_micros, point.value_micros, point.value)) })).filter((point) => Number.isFinite(point.x) && point.y !== null));
  if (!all.length) return;
  const padding = { top: 22, right: 20, bottom: 28, left: 54 };
  const width = box.width - padding.left - padding.right; const height = box.height - padding.top - padding.bottom;
  const xMin = Math.min(...all.map((point) => point.x)); const xMax = Math.max(...all.map((point) => point.x));
  const yMin = Math.min(...all.map((point) => point.y)); const yMax = Math.max(...all.map((point) => point.y));
  const yPad = (yMax - yMin || 1) * .08; const scaleYMin = yMin - yPad; const scaleYMax = yMax + yPad;
  const x = (value) => padding.left + ((value - xMin) / (xMax - xMin || 1)) * width;
  const y = (value) => padding.top + (1 - (value - scaleYMin) / (scaleYMax - scaleYMin)) * height;
  context.strokeStyle = "#e5eaf1"; context.lineWidth = 1; context.fillStyle = "#8791a1"; context.font = "10px Inter, sans-serif";
  for (let i = 0; i < 4; i += 1) { const value = scaleYMin + (scaleYMax - scaleYMin) * i / 3; const yPos = y(value); context.beginPath(); context.moveTo(padding.left, yPos); context.lineTo(box.width - padding.right, yPos); context.stroke(); context.fillText(amount(value, true), 4, yPos + 3); }
  ["#2863ff", "#7b5ce5", "#e37552", "#009c69"].forEach((colour, index) => {
    const item = valid[index]; if (!item) return;
    const points = item.points.map((point) => ({ x: new Date(first(point.at, point.calculated_at, point.timestamp)).valueOf(), y: numeric(first(point.account_value_micros, point.value_micros, point.value)) })).filter((point) => Number.isFinite(point.x) && point.y !== null).sort((a, b) => a.x - b.x);
    context.beginPath(); points.forEach((point, pointIndex) => { if (pointIndex) context.lineTo(x(point.x), y(point.y)); else context.moveTo(x(point.x), y(point.y)); }); context.strokeStyle = colour; context.lineWidth = 2; context.stroke();
  });
}

function renderAgents() {
  const root = $("#agent-list");
  $("#agent-count").textContent = String(state.agents.length);
  if (!state.agents.length) { root.replaceChildren(element("p", "empty-state", "No agents are available for this selection.")); return; }
  if (!state.selectedAgentId || !state.agents.some((agent) => String(first(agent.id, agent.agent_id)) === String(state.selectedAgentId))) state.selectedAgentId = first(state.agents[0].id, state.agents[0].agent_id);
  const fragment = document.createDocumentFragment();
  state.agents.forEach((agent) => {
    const id = String(first(agent.id, agent.agent_id)); const selected = id === String(state.selectedAgentId);
    const button = element("button", `agent-row${selected ? " is-active" : ""}`); button.type = "button"; button.dataset.agentId = id; button.setAttribute("aria-pressed", String(selected));
    const title = element("span", "agent-row-title"); title.append(document.createTextNode(text(first(agent.name, agent.agent_name, id))), badge(first(agent.status, agent.paused_at ? "paused" : "active")));
    button.append(title, element("small", "", `${text(first(agent.model_label, agent.model, "No model"))} · ${amount(first(agent.account_value_micros, agent.account_value))}`)); fragment.append(button);
  });
  root.replaceChildren(fragment); renderAgentDetail(state.agents.find((agent) => String(first(agent.id, agent.agent_id)) === String(state.selectedAgentId)));
}

function badge(value) { return element("span", `badge ${statusClass(value)}`, text(value)); }

function agentIsPaused(agent) {
  return Boolean(agent.paused_at) || String(first(agent.status, "")).toLowerCase() === "paused";
}

function agentControlButton(agent) {
  const id = String(first(agent.id, agent.agent_id));
  const paused = agentIsPaused(agent);
  const action = paused ? "Resume" : "Pause";
  const button = element("button", `control-button ${paused ? "resume-control" : "pause-control"}`, `${action} agent`);
  button.type = "button";
  button.dataset.agentControl = id;
  button.dataset.paused = String(paused);
  button.disabled = state.loading || !state.dataReady || Boolean(state.controlLoading);
  button.setAttribute("aria-label", `${action} ${text(first(agent.name, agent.agent_name, id))}`);
  return button;
}

const RETENTION_UNAVAILABLE = "Payload unavailable after retention cleanup.";

function auditText(value, unavailable = "Not recorded for this cycle.", retentionPurged = false) {
  if (retentionPurged) return { content: RETENTION_UNAVAILABLE, unavailable: true };
  if (value === undefined || value === null || value === "") return { content: unavailable, unavailable: true };
  if (typeof value === "object") return { content: JSON.stringify(value, null, 2), unavailable: false };
  return { content: String(value), unavailable: false };
}

function renderAgentDetail(agent) {
  const root = $("#agent-detail");
  if (!agent) { root.replaceChildren(element("div", "empty-state large-empty", "Select an agent to inspect its audit context.")); return; }
  const hero = element("section", "agent-hero"); const title = document.createElement("div"); title.append(element("h2", "", first(agent.name, agent.agent_name, "Unnamed agent")), element("p", "", `${text(first(agent.model_label, agent.model, "Model unavailable"))} · ${text(first(agent.run_id, "Current run"))}`));
  const kpis = element("div", "hero-kpis");
  [["Account value", amount(first(agent.account_value_micros, agent.account_value))], ["Total PnL", amount(first(agent.total_pnl_micros, agent.pnl_micros, agent.total_pnl))], ["Return", percent(first(agent.return_fraction, agent.return_percent, agent.return))]].forEach(([label, value]) => { const kpi = element("div", "hero-kpi"); kpi.append(element("span", "", label), element("strong", "", value)); kpis.append(kpi); });
  const actions = element("div", "agent-actions"); actions.append(agentControlButton(agent)); hero.append(title, kpis, actions);
  const plans = list(first(agent.active_plans, agent.plans));
  const longTermPlan = plans.find((plan) => plan.plan_type === "long_term");
  const nextCyclePlan = plans.find((plan) => plan.plan_type === "next_cycle");
  const grid = element("div", "detail-grid");
  grid.append(contextPanel("Long-term plan", first(agent.long_term_plan, agent.long_term_plan_text, longTermPlan?.content), first(agent.long_term_plan_updated_at, longTermPlan?.created_at, agent.long_term_plan_cycle_id)), contextPanel("Next-cycle plan", first(agent.next_cycle_plan, agent.next_cycle_plan_text, nextCyclePlan?.content), first(agent.next_cycle_plan_updated_at, nextCyclePlan?.created_at, agent.next_cycle_plan_cycle_id)), beliefsPanel(list(first(agent.beliefs, agent.current_beliefs))), positionsPanel(list(first(agent.positions, agent.open_positions))));
  root.replaceChildren(hero, grid);
}

function contextPanel(title, value, meta) {
  const card = element("article", "panel context-card"); const audit = auditText(value, "No retained plan is available."); card.append(element("h3", "", title), element("p", audit.unavailable ? "retention-note" : "", audit.content)); if (meta) card.append(element("span", "context-meta", `Last updated ${date(meta)}`)); return card;
}

function beliefsPanel(beliefs) {
  const card = element("article", "panel context-card"); card.append(element("h3", "", "Beliefs")); const root = element("div", "belief-list");
  if (!beliefs.length) root.append(element("p", "retention-note", "No retained beliefs are available."));
  beliefs.slice(0, 12).forEach((belief) => { const row = element("div", "belief-row"); const copy = document.createElement("div"); copy.append(element("strong", "", first(belief.market_question, belief.market, belief.subject, belief.title, belief.category, "Belief")), element("span", "", first(belief.content, belief.rationale, belief.summary, belief.updated_at, "No rationale recorded"))); row.append(copy, element("span", "confidence", percent(first(belief.confidence, belief.probability, belief.estimated_probability)))); root.append(row); }); card.append(root); return card;
}

function positionsPanel(positions) {
  const card = element("article", "panel context-card");
  card.append(element("h3", "", "Open positions"));
  const root = element("div", "position-list");
  if (!positions.length) root.append(element("p", "retention-note", "No open positions are recorded."));
  positions.slice(0, 8).forEach((position) => {
    const row = element("div", "position-row");
    const copy = document.createElement("div");
    const age = numeric(position.valuation_max_age_seconds);
    const valuation = text(first(position.valuation_status, "value unavailable"));
    const fee = amount(position.entry_fees_micros);
    copy.append(
      element("strong", "", first(position.question, position.market_question, position.market, position.outcome)),
      element(
        "span",
        "",
        `${text(first(position.contract_units, "—"))} contract units · ${valuation}`
          + (age === null ? "" : ` · last bid ≤ ${age}s`)
          + ` · entry fees ${fee}`,
      ),
    );
    row.append(
      copy,
      element(
        "strong",
        classForValue(first(position.unrealized_pnl_micros, position.pnl_micros)),
        `Net P&L ${amount(first(position.unrealized_pnl_micros, position.pnl_micros, position.liquidation_value_micros))}`,
      ),
    );
    root.append(row);
  });
  card.append(root);
  return card;
}

function renderCycles() {
  const root = $("#cycle-list"); $("#cycle-count").textContent = String(state.cycles.length);
  if (!state.cycles.length) { root.replaceChildren(element("p", "empty-state", "No cycles are available for this selection.")); return; }
  const fragment = document.createDocumentFragment();
  state.cycles.forEach((cycle) => {
    const id = String(first(cycle.id, cycle.cycle_id)); const selected = id === String(state.selectedCycleId); const button = element("button", `cycle-row${selected ? " is-active" : ""}`); button.type = "button"; button.dataset.cycleId = id; button.setAttribute("aria-pressed", String(selected));
    const copy = document.createElement("span"); copy.append(element("strong", "", text(first(cycle.agent_name, cycle.agent, "Agent cycle"))), element("small", "", `${text(first(cycle.status, "unknown"))} · ${text(first(cycle.model_label, cycle.model, "Model unavailable"))}`)); button.append(copy, element("time", "", relativeDate(first(cycle.started_at, cycle.created_at, cycle.completed_at)))); fragment.append(button);
  }); root.replaceChildren(fragment);
}

async function selectCycle(id) {
  state.selectedCycleId = id; renderCycles(); showTab("cycles");
  const root = $("#cycle-detail"); root.replaceChildren(element("div", "empty-state large-empty", "Loading complete audit trail…"));
  try { renderCycleDetail(await request(ENDPOINTS.cycle(id))); } catch (error) { root.replaceChildren(element("div", "empty-state large-empty", `Could not load this cycle: ${error.message}`)); }
}

function renderCycleDetail(raw) {
  const cycle = first(raw.metadata, raw.cycle, raw);
  const root = $("#cycle-detail"); const hero = element("section", "cycle-hero"); const title = document.createElement("div"); title.append(element("h2", "", text(first(cycle.agent_name, cycle.agent, "Agent cycle"))), element("p", "", `${text(first(cycle.model_label, cycle.model, "Model unavailable"))} · started ${date(first(cycle.started_at, cycle.created_at))}`)); hero.append(title, badge(first(cycle.status, "unknown")));
  const summary = element("section", "cycle-summary");
  const usage = list(raw.provider_usage);
  const cost = usage.reduce((total, item) => total + (numeric(item.billed_cost_micros) || 0), 0);
  const tokens = usage.reduce((total, item) => total + (numeric(item.prompt_tokens) || 0) + (numeric(item.completion_tokens) || 0) + (numeric(item.reasoning_tokens) || 0), 0);
  const start = new Date(cycle.started_at).valueOf(); const end = new Date(cycle.completed_at).valueOf();
  const duration = Number.isFinite(start) && Number.isFinite(end) ? `${((end - start) / 1000).toFixed(1)}s` : "—";
  [["Duration", duration], ["Cost", amount(cost)], ["Tokens", number.format(tokens)], ["Termination", text(first(cycle.model_termination_status, cycle.harness_termination_status, cycle.status))]].forEach(([label, value]) => { const item = document.createElement("div"); item.append(element("span", "", label), element("strong", "", value)); summary.append(item); });
  const inspection = element("div", "cycle-inspection-grid"); const timelinePanel = element("section", "panel timeline-panel"); const heading = element("div", "panel-heading"); heading.append(element("div", "", undefined)); heading.firstChild.append(element("p", "eyebrow", "Chronological record"), element("h2", "", "Reasoning and actions")); timelinePanel.append(heading, timeline(raw));
  const side = element("aside", "side-stack"); side.append(cycleContext(raw), diagnosticsPanel(list(raw.diagnostics)), retentionPanel(raw)); inspection.append(timelinePanel, side); root.replaceChildren(hero, summary, inspection);
}

function cycleContext(detail) {
  const panel = element("section", "panel context-card"); panel.append(element("h3", "", "Cycle context"));
  const metadata = first(detail.metadata, {});
  const prompt = auditText(metadata.rendered_cycle_prompt, "The rendered prompt is unavailable.", metadata.prompt_retention_purged);
  const promptDetails = document.createElement("details");
  promptDetails.append(
    element("summary", "", "Rendered prompt"),
    element("pre", prompt.unavailable ? "code-block retention-note" : "code-block", prompt.content),
  );
  const context = auditText(metadata.prompt_context, "The initial prompt context is unavailable.", metadata.prompt_retention_purged);
  const contextDetails = document.createElement("details");
  contextDetails.append(
    element("summary", "", "Initial prompt context"),
    element("pre", context.unavailable ? "code-block retention-note" : "code-block", context.content),
  );
  panel.append(promptDetails, contextDetails);
  const plans = list(detail.plan_revisions); const longPlan = plans.find((plan) => plan.plan_type === "long_term"); const nextPlan = plans.find((plan) => plan.plan_type === "next_cycle");
  const longTerm = auditText(longPlan?.content, "Long-term plan was not revised in this cycle."); const next = auditText(nextPlan?.content, "Next-cycle plan was not revised in this cycle."); const beliefs = list(detail.belief_revisions);
  [["Long-term plan", longTerm], ["Next-cycle plan", next]].forEach(([title, content]) => { const details = document.createElement("details"); details.open = false; details.append(element("summary", "", title), element("p", content.unavailable ? "retention-note" : "", content.content)); panel.append(details); });
  const beliefDetails = document.createElement("details"); beliefDetails.append(element("summary", "", `Beliefs (${beliefs.length})`)); if (beliefs.length) beliefDetails.append(beliefsPanel(beliefs).querySelector(".belief-list")); else beliefDetails.append(element("p", "retention-note", "No retained belief snapshot is available.")); panel.append(beliefDetails); return panel;
}

function timeline(detail) {
  const events = auditEvents(detail); const root = element("ol", "timeline");
  if (!events.length) { root.append(element("li", "empty-state", "No retained timeline events are available for this cycle.")); return root; }
  events.forEach((event) => root.append(timelineEntry(event))); return root;
}

function auditEvents(detail) {
  const events = [];
  list(detail.runtime_steps).forEach((item) => events.push({ kind: item.status === "failed" ? "error" : "runtime", title: `${text(item.stage, "Runtime")} step`, summary: first(item.error, item.status), at: first(item.completed_at, item.started_at), output: item.output, sequence: -1000 }));
  const calls = list(detail.tool_calls);
  const research = list(detail.research);
  const usage = list(detail.provider_usage);
  list(detail.model_turns).forEach((item) => {
    const turnIndex = numeric(item.turn_index) || 0;
    const turnUsage = usage.filter((row) => String(row.model_turn_id) === String(item.id));
    const tokens = turnUsage.reduce((total, row) => total + (numeric(row.prompt_tokens) || 0) + (numeric(row.completion_tokens) || 0) + (numeric(row.reasoning_tokens) || 0), 0);
    const cost = turnUsage.reduce((total, row) => total + (numeric(row.billed_cost_micros) || 0), 0);
    events.push({ kind: "reasoning", title: `Model turn ${text(item.turn_index)}`, reasoning: item.reasoning, at: first(item.completed_at, item.started_at), input: item.request, response: item.response, retention_purged: item.retention_purged, tokens, cost_micros: cost, sequence: turnIndex * 1000 });
    calls.filter((call) => String(call.model_turn_id) === String(item.id)).forEach((call) => {
      const callIndex = numeric(call.call_index) || 0;
      events.push({ kind: call.success === false ? "error" : "tool", title: first(call.display_name, call.tool_name, "Tool call"), summary: call.error || first(call.validation_status, call.success === true ? "Completed" : "Unknown result"), at: first(call.completed_at, call.called_at), arguments: call.arguments, output: call.output, retention_purged: call.retention_purged, sequence: turnIndex * 1000 + callIndex * 10 + 1 });
      research.filter((row) => String(row.tool_call_id) === String(call.id)).forEach((row, researchIndex) => events.push({ kind: "research", title: first(row.title, "Research result"), summary: row.query ? `Query: ${row.query}` : row.canonical_url, at: first(row.created_at, row.fetched_at), query: row.query, sources: row.canonical_url ? [{ title: row.title, url: row.canonical_url }] : undefined, sequence: turnIndex * 1000 + callIndex * 10 + 2 + researchIndex / 100 }));
    });
  });
  list(detail.belief_revisions).forEach((item) => events.push({ kind: "belief", title: "Belief revised", summary: item.content, at: item.created_at, result: item }));
  list(detail.plan_revisions).forEach((item) => events.push({ kind: "plan", title: `${text(item.plan_type)} plan revised`, summary: item.content, at: item.created_at, result: item }));
  list(detail.operations).forEach((item) => events.push({ kind: item.lifecycle_state === "REJECTED" ? "error" : "trade", title: `${text(item.order_side, "Order")} ${text(item.outcome_side, "outcome")}`, summary: first(item.lifecycle_reason, item.market_question, item.lifecycle_state), at: first(item.filled_at, item.created_at), trade: item }));
  return events.sort((left, right) => {
    const timeDifference = new Date(first(left.at, 0)).valueOf() - new Date(first(right.at, 0)).valueOf();
    if (timeDifference) return timeDifference;
    return (numeric(left.sequence) || 0) - (numeric(right.sequence) || 0);
  });
}

function timelineEntry(event) {
  const kind = String(first(event.kind, event.type, event.event_type, "reasoning")).toLowerCase(); const classifications = `${kind} ${statusClass(first(event.status, kind))}`; const entry = element("li", `timeline-entry ${classifications}`);
  const header = document.createElement("header"); const title = first(event.title, event.tool_name, event.name, kind.replace(/[_-]/g, " ")); header.append(element("h3", "", title), element("time", "", date(first(event.occurred_at, event.at, event.created_at, event.started_at)))); entry.append(header);
  const body = first(event.reasoning, event.content, event.summary, event.message, event.result_summary, event.decision, event.trade_summary);
  if (body !== undefined) {
    const audit = auditText(body, RETENTION_UNAVAILABLE, event.retention_purged);
    entry.append(element("p", audit.unavailable ? "retention-note" : "", audit.content));
  }
  const details = [];
  if (event.arguments || event.input) details.push(["Tool input", first(event.arguments, event.input)]);
  if (event.result || event.output || event.response) details.push(["Tool result", first(event.result, event.output, event.response)]);
  if (event.query || event.sources || event.results) details.push(["Research detail", { query: event.query, sources: first(event.sources, event.results) }]);
  if (event.trade || event.order || event.fill) details.push(["Execution detail", first(event.trade, event.order, event.fill)]);
  if (event.cost_micros || event.tokens) details.push(["Model usage", { tokens: event.tokens, input_tokens: event.input_tokens, output_tokens: event.output_tokens, cost_micros: event.cost_micros }]);
  details.forEach(([label, payload]) => {
    const detail = document.createElement("details");
    const audit = auditText(payload, RETENTION_UNAVAILABLE, event.retention_purged);
    detail.append(element("summary", "", label), element("pre", audit.unavailable ? "code-block retention-note" : "code-block", audit.content));
    entry.append(detail);
  });
  if (event.retention_purged && body === undefined && !details.length) entry.append(element("p", "retention-note", RETENTION_UNAVAILABLE));
  return entry;
}

function diagnosticsPanel(diagnostics) {
  const panel = element("section", "panel context-card"); panel.append(element("h3", "", "Diagnostics")); const root = element("div", "diagnostic-list");
  if (!diagnostics.length) root.append(element("p", "retention-note", "No deterministic diagnostic signals were raised."));
  diagnostics.forEach((diagnostic) => { const item = element("div", "diagnostic"); item.append(element("strong", statusClass(diagnostic.severity), first(diagnostic.title, diagnostic.code, "Diagnostic")), element("p", "", first(diagnostic.message, diagnostic.detail, diagnostic.description))); root.append(item); }); panel.append(root); return panel;
}

function retentionPanel(detail) {
  const metadata = first(detail.metadata, detail);
  const purged = Boolean(metadata.prompt_retention_purged) || list(detail.model_turns).some((item) => item.retention_purged) || list(detail.tool_calls).some((item) => item.retention_purged);
  const unavailable = first(detail.retention, detail.payload_retention, purged ? "Some detailed payloads have expired" : null);
  if (!unavailable) return document.createDocumentFragment();
  const panel = element("section", "retention-note"); panel.append(element("strong", "", "Audit payload availability"), document.createTextNode(` ${text(unavailable)}. Sensitive payloads may expire while audit metadata remains available.`)); return panel;
}

function renderOperations() {
  const data = overviewData(); renderAlerts(list(first(data.open_alert_items, data.open_alerts_list)), first(data.open_alerts, data.alert_count)); renderFreshness(list(first(data.freshness, data.data_freshness))); renderUsage(list(first(data.provider_usage, data.costs)));
}

function systemIsPaused() {
  const data = overviewData();
  const controls = first(data.controls, {});
  return Boolean(first(controls.globally_paused, data.globally_paused));
}

function syncAgentControlButtons() {
  const disabled = state.loading || !state.dataReady || Boolean(state.controlLoading);
  $$('[data-agent-control]').forEach((button) => { button.disabled = disabled; });
}

function syncGlobalControl() {
  const button = $("#global-control-button");
  if (!button) return;
  const available = state.dataReady;
  const paused = systemIsPaused();
  const pending = state.controlLoading === "system";
  button.disabled = !available || state.loading || Boolean(state.controlLoading);
  button.textContent = pending ? "Updating…" : available ? `${paused ? "Resume" : "Pause"} run` : "Run state unavailable";
  button.dataset.paused = String(paused);
  button.setAttribute("aria-label", `${paused ? "Resume" : "Pause"} complete run`);
}

async function changeSystemState(paused) {
  if (state.controlLoading || paused === systemIsPaused()) return;
  const action = paused ? "Pause" : "Resume";
  const message = paused
    ? "Pause the complete run? Future scheduled cycles will be skipped; an active cycle will finish."
    : "Resume the complete run? Future scheduled cycles may run again.";
  if (!window.confirm(message)) return;
  state.controlLoading = "system";
  syncGlobalControl();
  try {
    await controlRequest(ENDPOINTS.systemControl(paused));
    toast(`${action} run submitted.`);
    await loadDashboard();
  } catch (error) {
    toast(`Could not ${action.toLowerCase()} run: ${error.message}`, "error");
  } finally {
    state.controlLoading = null;
    syncGlobalControl();
    renderAgents();
  }
}

async function changeAgentState(agent, paused) {
  const id = String(first(agent.id, agent.agent_id));
  if (!id || state.controlLoading || paused === agentIsPaused(agent)) return;
  const name = text(first(agent.name, agent.agent_name, id));
  const action = paused ? "Pause" : "Resume";
  const message = paused
    ? `Pause ${name}? Future scheduled cycles will be skipped; an active cycle will finish.`
    : `Resume ${name}? Future scheduled cycles may run again.`;
  if (!window.confirm(message)) return;
  state.controlLoading = `agent:${id}`;
  renderAgents();
  try {
    await controlRequest(ENDPOINTS.agentControl(id, paused));
    toast(`${action} submitted for ${name}.`);
    await loadDashboard();
  } catch (error) {
    toast(`Could not ${action.toLowerCase()} ${name}: ${error.message}`, "error");
  } finally {
    state.controlLoading = null;
    renderAgents();
    syncGlobalControl();
  }
}

function renderAlerts(alerts, count) {
  $("#alert-count").textContent = count === undefined ? String(alerts.length) : String(count); const root = $("#alert-list");
  if (!alerts.length) { root.replaceChildren(element("p", "empty-state", "No open alerts.")); return; }
  const fragment = document.createDocumentFragment(); alerts.forEach((alert) => { const row = element("div", "alert-row"); row.append(badge(first(alert.severity, "info"))); const copy = document.createElement("div"); copy.append(element("p", "", first(alert.message, alert.title, alert.code)), element("small", "", first(alert.created_at ? date(alert.created_at) : undefined, alert.source, ""))); row.append(copy); fragment.append(row); }); root.replaceChildren(fragment);
}

function renderFreshness(items) {
  const root = $("#freshness-list"); if (!items.length) { root.replaceChildren(element("p", "empty-state", "No freshness checks are available.")); return; }
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const status = String(first(item.status, item.freshness, "fresh")).toLowerCase();
    const row = element("div", "freshness-row");
    row.append(element("i", `freshness-dot ${status}`));
    const copy = document.createElement("div");
    const maxAge = numeric(item.freshness_max_age_seconds);
    const age = item.age_seconds === undefined ? undefined : `${item.age_seconds}s old`;
    const policy = maxAge === null ? undefined : `fresh ≤ ${maxAge}s`;
    copy.append(element("p", "", first(item.source, item.name, item.dataset)), element("small", "", first(item.cutoff ? `Latest cutoff ${date(item.cutoff)}` : undefined, age, policy, status)));
    row.append(copy, element("span", `badge ${statusClass(status)}`, status));
    fragment.append(row);
  });
  root.replaceChildren(fragment);
}

function providerUsageLabel(item) {
  const totalTokens = (numeric(item.prompt_tokens) || 0)
    + (numeric(item.completion_tokens) || 0)
    + (numeric(item.reasoning_tokens) || 0);
  const requestCount = numeric(item.request_count) || 0;
  return `${number.format(totalTokens)} tokens / ${number.format(requestCount)} requests`;
}

function renderUsage(items) {
  const root = $("#usage-list"); if (!items.length) { root.replaceChildren(element("p", "empty-state", "No provider usage is available for this period.")); return; }
  const fragment = document.createDocumentFragment(); items.forEach((item) => { const row = element("div", "usage-row"); row.append(element("span", "badge", first(item.provider, item.route, "provider"))); const copy = document.createElement("div"); copy.append(element("p", "", first(item.model, item.name, item.provider)), element("small", "", providerUsageLabel(item))); row.append(copy, element("span", "usage-value", amount(first(item.cost_micros, item.cost, item.billed_cost_micros)))); fragment.append(row); }); root.replaceChildren(fragment);
}

function bindEvents() {
  $$(".nav-link").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.tab)));
  $$("[data-switch-tab]").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.switchTab)));
  $$(".range-button").forEach((button) => button.addEventListener("click", () => { state.window = button.dataset.window; $$(".range-button").forEach((item) => item.classList.toggle("is-active", item === button)); loadDashboard(); }));
  $("#agent-filter").addEventListener("change", (event) => { state.agentId = event.target.value; state.selectedAgentId = null; state.selectedCycleId = null; loadDashboard(); });
  $("#run-filter").addEventListener("change", (event) => { state.runId = event.target.value.trim(); state.selectedAgentId = null; state.selectedCycleId = null; loadDashboard(); });
  $("#refresh-button").addEventListener("click", loadDashboard);
  $("#global-control-button").addEventListener("click", () => {
    changeSystemState($("#global-control-button").dataset.paused !== "true");
  });
  $("#agent-list").addEventListener("click", (event) => { const row = event.target.closest("[data-agent-id]"); if (!row) return; state.selectedAgentId = row.dataset.agentId; renderAgents(); });
  $("#agent-detail").addEventListener("click", (event) => {
    const button = event.target.closest("[data-agent-control]");
    if (!button || button.disabled) return;
    const agent = state.agents.find((item) => String(first(item.id, item.agent_id)) === button.dataset.agentControl);
    if (agent) changeAgentState(agent, button.dataset.paused !== "true");
  });
  $("#cycle-list").addEventListener("click", (event) => { const row = event.target.closest("[data-cycle-id]"); if (row) selectCycle(row.dataset.cycleId); });
  window.addEventListener("resize", () => { if (state.overview) drawPerformance(first(overviewData().performance_history, overviewData().chart, [])); });
}

function initialiseFromLocation() {
  const params = new URLSearchParams(window.location.search); state.window = ["24h", "7d", "30d", "all"].includes(params.get("window")) ? params.get("window") : "30d"; state.runId = params.get("run_id") || ""; state.agentId = params.get("agent_id") || "";
  $("#run-filter").value = state.runId; $$(".range-button").forEach((button) => button.classList.toggle("is-active", button.dataset.window === state.window)); const hash = window.location.hash.slice(1); if (["overview", "agents", "cycles", "operations"].includes(hash)) showTab(hash);
}

initialiseFromLocation(); bindEvents(); loadDashboard();
