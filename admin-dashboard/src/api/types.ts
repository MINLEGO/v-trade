// Pagination params
export interface PageParams {
  limit?: number; // 1-500, default 100
  offset?: number; // >= 0, default 0
}

export interface AgentFilterParams extends PageParams {
  agent_id?: string; // UUID
}

// Overview response (from /admin/overview)
export interface OverviewRuns {
  running_runs: number;
  paused_runs: number;
  runs: number;
}

export interface OverviewAgents {
  agents: number;
  paused_agents: number;
}

export interface OverviewAlerts {
  open_alerts: number;
  latest_alert_at: string | null;
}

export interface OverviewCycles {
  last_success_at: string | null;
  last_failure_at: string | null;
  running_cycles: number;
  failed_cycles: number;
}

export interface OverviewControls {
  globally_paused: boolean;
  version: number;
  updated_at: string;
  updated_by: string;
}

export interface Overview {
  runs: OverviewRuns;
  agents: OverviewAgents;
  alerts: OverviewAlerts;
  cycles: OverviewCycles;
  controls: OverviewControls;
}

// Leaderboard row (from /admin/leaderboard)
export interface LeaderboardRow {
  agent_id: string;
  agent_name: string;
  run_id: string;
  model_label: string;
  account_value_micros: number | null;
  realized_pnl_micros: number | null;
  unrealized_pnl_micros: number | null;
  total_pnl_micros: number | null;
  drawdown_fraction: number | null;
  calculated_at: string | null;
  paused_at: string | null;
}

// Position row (from /admin/positions)
export interface PositionRow {
  id: string;
  agent_id: string;
  agent_name: string;
  market_id: string;
  question: string;
  outcome_id: string;
  outcome: string;
  shares: number;
  average_cost: number;
  cost_basis_micros: number;
  realized_pnl_micros: number;
  best_bid: number | null;
  quote_cutoff: string | null;
  liquidation_value_micros: number | null;
  valuation_status: "fresh" | "stale" | "missing";
  quote_age_seconds: number | null;
  updated_at: string;
}

// Trade/Fill row (from /admin/trades)
export interface TradeRow {
  fill_id: string;
  filled_at: string;
  agent_id: string;
  agent_name: string;
  market_id: string;
  question: string;
  outcome_id: string;
  outcome: string;
  side: "BUY" | "SELL";
  shares: number;
  price: number;
  gross_micros: number;
  fee_micros: number;
  policy: string;
  liquidity_time_in_force: number | null;
  agent_cycle_id: string;
  data_cutoff: string;
}

// Settlement row (from /admin/settlements)
export interface SettlementRow {
  id: string;
  settled_at: string;
  agent_id: string;
  agent_name: string;
  market_id: string;
  question: string;
  outcome_id: string;
  outcome: string;
  shares: number;
  payout_micros: number;
  realized_pnl_micros: number;
  result: string;
  source_created_at: string;
  observed_at: string;
  as_of_cutoff: string;
}

// Rejection row (from /admin/rejections)
export interface RejectionRow {
  intent_id: string;
  order_id: string | null;
  created_at: string;
  agent_id: string;
  agent_name: string;
  market_id: string;
  question: string;
  outcome_id: string;
  outcome: string;
  side: "BUY" | "SELL";
  validation_status: string;
  rejection_code: string;
  order_status: string | null;
  agent_cycle_id: string;
}

// Cycle row (from /admin/cycles)
export interface CycleRow {
  id: string;
  agent_id: string;
  agent_name: string;
  scheduled_at: string;
  data_cutoff: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  model_termination_status: string | null;
  failure_reason: string | null;
  rendered_prompt_sha256: string | null;
  artifact_sha256: string | null;
  prompt_version: string | null;
  prompt_sha256: string | null;
  experiment_version: string | null;
  config_version: number | null;
  config_sha256: string | null;
  code_version: string | null;
}

// Usage row (from /admin/usage)
export interface UsageRow {
  provider: string;
  route: string;
  usage_kind: string;
  usage_records: number;
  request_count: number;
  credit_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  billed_cost_micros: number;
  nominal_cost_micros: number;
  average_latency_ms: number | null;
  last_used_at: string;
}

// Freshness row (from /admin/freshness)
export interface FreshnessRow {
  source: string;
  last_observed_at: string | null;
  age_seconds: number | null;
  record_count: number;
}

// Config version row (from /admin/config-versions)
export interface ModelConfig {
  model_config_id: string;
  label: string;
  model_slug: string;
  provider_policy: string;
  parameters: Record<string, unknown>;
  config_sha256: string;
}

export interface PromptVersion {
  prompt_version_id: string;
  name: string;
  body: string;
  body_sha256: string;
  classification: string;
}

export interface ConfigVersionRow {
  id: string;
  experiment_version: string;
  version_number: number;
  status: string;
  definition: Record<string, unknown>;
  config_sha256: string;
  code_version: string;
  created_at: string;
  supersedes_id: string | null;
  models: ModelConfig[] | null;
  prompts: PromptVersion[] | null;
}

// Alert row (from /admin/alerts)
export interface AlertRow {
  id: string;
  run_id: string;
  agent_id: string;
  severity: string;
  code: string;
  details: Record<string, unknown>;
  opened_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

// Operator action row (from /admin/operator-actions)
export interface OperatorActionRow {
  id: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string | null;
  before_state: Record<string, unknown>;
  after_state: Record<string, unknown>;
  occurred_at: string;
  idempotency_key: string;
}

// Health responses
export interface HealthLive {
  status: string;
  checked_at: string;
}

export interface HealthReadyCheck {
  status: string;
  [key: string]: unknown;
}

export interface HealthReady {
  status: "ready" | "not_ready";
  checked_at: string;
  checks: {
    database?: HealthReadyCheck;
    supabase_storage?: HealthReadyCheck;
    configuration?: HealthReadyCheck;
  };
}
