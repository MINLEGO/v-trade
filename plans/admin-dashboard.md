# V-Trade Admin Dashboard — Full Specification

Separate React SPA that consumes the existing V-Trade private admin API.
No backend changes required.

---

## 1  Tech Stack

| Layer | Choice |
|---|---|
| Framework | React 18 + TypeScript |
| Bundler | Vite |
| Styling | Tailwind CSS 4 |
| Component library | shadcn/ui (Radix primitives + Tailwind) |
| Routing | React Router v6 |
| Data fetching | TanStack Query v5 |
| Tables | TanStack Table v8 |
| Charts | Recharts |
| State management | Zustand (minimal — auth state, selected agent) |
| HTTP client | Fetch API wrapped in a thin `apiClient` module |
| Auth storage | `sessionStorage` (secret never persisted to `localStorage`) |

---

## 2  Authentication

The API accepts two schemes; the dashboard will support both:

- **Bearer token** — user pastes the `VTRADE_ADMIN_AUTH_SECRET` into a login form.
  Dashboard stores it in `sessionStorage` and sends `Authorization: Bearer <secret>`.
- **HTTP Basic** — optional fallback; the login form can accept a username:password pair.

On any `401` response the app redirects to `/login`.
The secret is never logged, never sent to third-party analytics, and is cleared on tab close.

### Control headers

Mutating endpoints (`pause`, `resume`) require two extra headers:

| Header | Source |
|---|---|
| `X-Operator-Id` | Entered once per session in the login form or auto-generated UUID stored in Zustand |
| `Idempotency-Key` | Generated per request as `crypto.randomUUID()` |

---

## 3  Application Shell

```
┌─────────────────────────────────────────────────┐
│  Header: V-Trade Admin  [health badge] [logout] │
├──────────┬──────────────────────────────────────┤
│ Sidebar  │  Main content area                   │
│          │  (route-driven)                      │
│ Overview │                                      │
│ Agents   │                                      │
│ Trades   │                                      │
│ Cycles   │                                      │
│ Alerts   │                                      │
│ Config   │                                      │
│ Health   │                                      │
│ Audit    │                                      │
└──────────┴──────────────────────────────────────┘
```

- Sidebar is collapsible on mobile.
- A global **agent filter** dropdown in the header filters all agent-scoped views.
- A red **GLOBAL PAUSE** toggle button sits in the header with confirmation dialog.

---

## 4  Pages and Features

### 4.1  Overview (`GET /admin/overview`)

KPI cards at the top:

| Card | Field(s) |
|---|---|
| Experiment Runs | `runs.running`, `runs.paused`, `runs.runs` |
| Agents | `agents.agents`, `agents.paused_agents` |
| Open Alerts | `alerts.open_alerts`, `alerts.latest_alert_at` |
| Cycles | `cycles.running_cycles`, `cycles.failed_cycles`, `cycles.last_success_at`, `cycles.last_failure_at` |
| System Pause | `controls.globally_paused` (toggle button) |

Below the cards: a quick-access table of the **leaderboard** (top 10 agents by account value).

---

### 4.2  Leaderboard (`GET /admin/leaderboard`)

Paginated table. Columns:

| Column | API field |
|---|---|
| Rank | computed from order |
| Agent Name | `agent_name` |
| Model | `model_label` |
| Account Value | `account_value_micros` (formatted as `$X.XX`) |
| Realized PnL | `realized_pnl_micros` |
| Unrealized PnL | `unrealized_pnl_micros` |
| Total PnL | `total_pnl_micros` (colored green/red) |
| Drawdown | `drawdown_fraction` (percentage bar) |
| Last Updated | `calculated_at` |
| Paused? | `paused_at` (badge) |
| Actions | Pause / Resume button per agent |

Sort by any column client-side. Pagination via `limit`/`offset` query params.

---

### 4.3  Agents Detail (`GET /admin/leaderboard` + filter)

When a specific agent is selected from the leaderboard or the global filter:

**Agent Summary Card:**
- Name, model, run ID, initial cash, current account value
- PnL breakdown (realized + unrealized = total)
- Drawdown from peak
- Pause/resume button

**Sub-tabs within the agent view:**

#### 4.3.1  Positions (`GET /admin/positions?agent_id=...`)
Table columns: Market question, Outcome, Shares, Average Cost, Cost Basis, Realized PnL, Best Bid, Liquidation Value, Valuation Status (fresh/stale/missing badge), Quote Age, Updated At.

#### 4.3.2  Trades (`GET /admin/trades?agent_id=...`)
Table columns: Fill ID, Time, Market, Outcome, Side (BUY/SELL badge), Shares, Price, Gross, Fee, Policy, Liquidity TTL, Cycle ID, Data Cutoff.

#### 4.3.3  Settlements (`GET /admin/settlements?agent_id=...`)
Table columns: Settled At, Market, Outcome, Shares, Payout, Realized PnL, Resolution Result, Source Created At, Observed At, As-of Cutoff.

#### 4.3.4  Rejections (`GET /admin/rejections?agent_id=...`)
Table columns: Intent ID, Order ID, Created At, Market, Outcome, Side, Validation Status, Rejection Code (highlighted), Order Status, Cycle ID.

#### 4.3.5  Cycles (`GET /admin/cycles?agent_id=...`)
Table columns: Cycle ID, Scheduled At, Data Cutoff, Status (badge), Started At, Completed At, Model Termination Status, Failure Reason, Prompt Version, Config Version, Code Version.

#### 4.3.6  Usage & Cost (`GET /admin/usage?agent_id=...`)
Table columns: Provider, Route, Usage Kind, Request Count, Credit Count, Prompt Tokens, Completion Tokens, Reasoning Tokens, Cached Tokens, Billed Cost, Nominal Cost, Avg Latency, Last Used At.

Summary row at top: total billed cost, total tokens.

---

### 4.4  Global Trades View (`GET /admin/trades`)

Same table as 4.3.2 but across all agents. Additional "Agent Name" column. Filterable by agent via the global dropdown.

---

### 4.5  Global Positions View (`GET /admin/positions`)

Same table as 4.3.1 but across all agents. Additional "Agent Name" column. Filterable by agent.

---

### 4.6  Global Settlements (`GET /admin/settlements`)

Same as 4.3.3, all agents. "Agent Name" column included.

---

### 4.7  Global Rejections (`GET /admin/rejections`)

Same as 4.3.4, all agents. "Agent Name" column included.

---

### 4.8  Cycles Overview (`GET /admin/cycles`)

Global cycles view. Columns same as 4.3.5 plus Agent Name. Filterable by agent.

Additional: status distribution chart (pie or bar chart showing running/completed/failed/skipped counts).

---

### 4.9  Usage & Cost (`GET /admin/usage`)

Global usage view. Same columns as 4.3.6 plus agent context. Filterable by agent.

Charts:
- **Cost breakdown** by provider (pie chart, using `billed_cost_micros`).
- **Token distribution** (prompt vs completion vs reasoning, bar chart).
- **Latency** by provider/route (bar chart).

Summary KPI cards: Total billed cost, total tokens, average latency.

---

### 4.10  Data Freshness (`GET /admin/freshness`)

Table columns: Source, Last Observed At, Age (seconds), Record Count.

Visual indicator:
- Green badge: age < 5 minutes
- Yellow badge: age < 1 hour
- Red badge: age > 1 hour or null

Sources: `market`, `order_book`, `resolution`, `venue_sync`.

---

### 4.11  Alerts (`GET /admin/alerts`)

Table columns: ID, Severity (badge with color), Code, Details (expandable), Run ID, Agent ID, Opened At, Acknowledged At, Resolved At.

Filter: All / Open only / Resolved only (toggle).

Open alerts are highlighted with a left border accent.

---

### 4.12  Configuration & Versions (`GET /admin/config-versions`)

Accordion or expandable cards for each experiment definition:

- Experiment version, status, config SHA256, code version, created at, supersedes
- **Models section** (nested): label, slug, provider policy, parameters, config SHA256
- **Prompts section** (nested): name, body (collapsible code block), body SHA256, classification

---

### 4.13  Audit Log (`GET /admin/operator-actions`)

Table columns: ID, Actor ID, Action (pause/resume badge), Target Type, Target ID, Before State (JSON expandable), After State (JSON expandable), Occurred At, Idempotency Key.

---

### 4.14  System Health (`GET /health/live` + `/health/ready`)

- **Liveness**: simple green/red dot (polls every 30 seconds).
- **Readiness** card with sub-checks:
  - Database: status, database name, time, latest migration version
  - Supabase Storage: ok/failed
  - Configuration: status, experiment version, SHA256
- Overall readiness status badge.

---

## 5  Shared Components

| Component | Purpose |
|---|---|
| `DataTable` | Reusable TanStack Table wrapper with sorting, pagination, column visibility toggle |
| `KPICard` | Card showing a metric with label, value, optional trend arrow |
| `StatusBadge` | Colored badge for statuses (running, failed, paused, fresh, stale, missing) |
| `JsonExpand` | Collapsible JSON viewer for nested objects (before/after state, details) |
| `AgentFilter` | Global dropdown to select an agent, stored in Zustand, propagated via URL params or context |
| `PauseButton` | Confirmation dialog + POST to pause/resume endpoints with operator headers |
| `PageHeader` | Title + description + action buttons |
| `LoadingSkeleton` | TanStack Query loading states |
| `ErrorBoundary` | Catches render errors, shows fallback UI |
| `RelativeTime` | Displays timestamps as relative (e.g., "2 min ago") with tooltip for absolute |

---

## 6  Project Structure

```
admin-dashboard/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
├── tailwind.config.ts
├── postcss.config.js
├── components.json              # shadcn/ui config
├── .env.example                 # VITE_API_BASE_URL
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── index.css                # Tailwind directives
│   ├── api/
│   │   ├── client.ts            # fetch wrapper with auth headers
│   │   ├── types.ts             # TypeScript interfaces matching API responses
│   │   └── hooks/
│   │       ├── useOverview.ts
│   │       ├── useLeaderboard.ts
│   │       ├── usePositions.ts
│   │       ├── useTrades.ts
│   │       ├── useSettlements.ts
│   │       ├── useRejections.ts
│   │       ├── useCycles.ts
│   │       ├── useUsage.ts
│   │       ├── useFreshness.ts
│   │       ├── useAlerts.ts
│   │       ├── useConfigVersions.ts
│   │       ├── useOperatorActions.ts
│   │       ├── useHealth.ts
│   │       └── useControlActions.ts
│   ├── store/
│   │   ├── auth.ts              # Zustand: secret, operator ID
│   │   └── filters.ts           # Zustand: selected agent, view filters
│   ├── components/
│   │   ├── ui/                  # shadcn/ui generated components
│   │   ├── layout/
│   │   │   ├── AppShell.tsx
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Header.tsx
│   │   │   └── PageHeader.tsx
│   │   ├── DataTable.tsx
│   │   ├── KPICard.tsx
│   │   ├── StatusBadge.tsx
│   │   ├── JsonExpand.tsx
│   │   ├── AgentFilter.tsx
│   │   ├── PauseButton.tsx
│   │   ├── RelativeTime.tsx
│   │   └── LoadingSkeleton.tsx
│   └── pages/
│       ├── LoginPage.tsx
│       ├── OverviewPage.tsx
│       ├── LeaderboardPage.tsx
│       ├── AgentDetailPage.tsx
│       ├── PositionsPage.tsx
│       ├── TradesPage.tsx
│       ├── SettlementsPage.tsx
│       ├── RejectionsPage.tsx
│       ├── CyclesPage.tsx
│       ├── UsagePage.tsx
│       ├── FreshnessPage.tsx
│       ├── AlertsPage.tsx
│       ├── ConfigVersionsPage.tsx
│       ├── AuditLogPage.tsx
│       └── HealthPage.tsx
```

---

## 7  API Consumption Summary

All data comes from these existing endpoints. **No backend changes.**

| Dashboard Feature | API Endpoint | Method | Auth | Extra Headers |
|---|---|---|---|---|
| Overview | `/admin/overview` | GET | Bearer/Basic | — |
| Leaderboard | `/admin/leaderboard` | GET | Bearer/Basic | — |
| Positions | `/admin/positions` | GET | Bearer/Basic | — |
| Trades | `/admin/trades` | GET | Bearer/Basic | — |
| Settlements | `/admin/settlements` | GET | Bearer/Basic | — |
| Rejections | `/admin/rejections` | GET | Bearer/Basic | — |
| Cycles | `/admin/cycles` | GET | Bearer/Basic | — |
| Usage | `/admin/usage` | GET | Bearer/Basic | — |
| Freshness | `/admin/freshness` | GET | Bearer/Basic | — |
| Config Versions | `/admin/config-versions` | GET | Bearer/Basic | — |
| Alerts | `/admin/alerts` | GET | Bearer/Basic | — |
| Audit Log | `/admin/operator-actions` | GET | Bearer/Basic | — |
| Liveness | `/health/live` | GET | Bearer/Basic | — |
| Readiness | `/health/ready` | GET | Bearer/Basic | — |
| Global Pause | `/admin/control/pause` | POST | Bearer/Basic | `X-Operator-Id`, `Idempotency-Key` |
| Global Resume | `/admin/control/resume` | POST | Bearer/Basic | `X-Operator-Id`, `Idempotency-Key` |
| Agent Pause | `/admin/agents/{id}/pause` | POST | Bearer/Basic | `X-Operator-Id`, `Idempotency-Key` |
| Agent Resume | `/admin/agents/{id}/resume` | POST | Bearer/Basic | `X-Operator-Id`, `Idempotency-Key` |

---

## 8  Key Design Decisions

1. **No backend changes** — the dashboard is a pure API consumer.
2. **Micro-dollars formatting** — all monetary values arrive as micro-dollars (integer). The dashboard divides by 1,000,000 and formats as `$X.XX`.
3. **Pagination** — all list endpoints accept `limit` (1-500) and `offset` (≥0). TanStack Table drives pagination state.
4. **Agent filtering** — six endpoints (`positions`, `trades`, `settlements`, `rejections`, `cycles`, `usage`) accept optional `agent_id` UUID query param. The global `AgentFilter` dropdown propagates this.
5. **Polling** — overview and health pages auto-refresh every 30 seconds via TanStack Query `refetchInterval`. Other pages refresh on focus or manual trigger.
6. **CORS** — the FastAPI server will need a CORS middleware to allow the dashboard origin. This is a config-level concern (or the dashboard is served via a reverse proxy on the same origin).
7. **Dark mode** — default dark theme matching the existing server-rendered dashboard (`#10141b` background). Light mode toggle optional.

---

## 9  Mermaid — Page Navigation Flow

```mermaid
graph TD
    Login[/login] --> Overview[/overview]
    Overview --> Leaderboard[/leaderboard]
    Overview --> Health[/health]
    Leaderboard --> AgentDetail[/agents/:id]
    AgentDetail --> AgentPositions[Positions tab]
    AgentDetail --> AgentTrades[Trades tab]
    AgentDetail --> AgentSettlements[Settlements tab]
    AgentDetail --> AgentRejections[Rejections tab]
    AgentDetail --> AgentCycles[Cycles tab]
    AgentDetail --> AgentUsage[Usage tab]
    Leaderboard --> Positions[/positions]
    Leaderboard --> Trades[/trades]
    Leaderboard --> Settlements[/settlements]
    Leaderboard --> Rejections[/rejections]
    Overview --> Cycles[/cycles]
    Overview --> Usage[/usage]
    Overview --> Freshness[/freshness]
    Overview --> Alerts[/alerts]
    Overview --> ConfigVersions[/config-versions]
    Overview --> AuditLog[/audit-log]
```

---

## 10  Mermaid — Data Flow

```mermaid
sequenceDiagram
    participant D as Dashboard SPA
    participant A as V-Trade Admin API
    participant DB as PostgreSQL

    D->>A: POST /admin/control/pause
    Note right of D: X-Operator-Id + Idempotency-Key
    A->>DB: UPDATE system_controls
    A->>DB: INSERT operator_actions
    A-->>D: 200 after_state JSON

    D->>A: GET /admin/overview
    A->>DB: SELECT runs, agents, alerts, cycles, controls
    A-->>D: 200 overview JSON

    D->>A: GET /admin/leaderboard?limit=50&offset=0
    A->>DB: SELECT leaderboard view
    A-->>D: 200 array of agent rows
