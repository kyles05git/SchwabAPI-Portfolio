/** Mirrors the pydantic view models serialized by GET /api/data. */

export type Num = string | number;

export interface SummaryView {
  account: string;
  liquidation_value: Num | null;
  day_pl: Num | null;
  cash_available: Num | null;
  kill_engaged: boolean;
}

/**
 * Which comparability group a sleeve belongs to. Sleeves in different scopes have
 * different starting capital, start dates, and evidence horizons, so they are ranked
 * separately and never share a benchmark.
 */
export type SleeveScope = "official-cohort" | "legacy" | "standalone";

export interface SleeveRow {
  /** Rank *within this row's scope group*, not across the whole leaderboard. */
  rank: number;
  /** Stable database identity. Join rows, curves, and cohort evidence on this. */
  sleeve_id: string;
  /** Human-readable name. Not unique across scopes. */
  name: string;
  strategy: string;
  scope: SleeveScope;
  cohort_id: string | null;
  starting_capital: Num;
  cycles: number;
  trades: number;
  value: Num;
  return_pct: Num;
  excess_pct: Num | null;
  /** Benchmark the excess was measured against, or null when none was resolved. */
  excess_benchmark: string | null;
  max_drawdown_pct: Num;
  sharpe: Num | null;
  is_benchmark: boolean;
  /** True when this row belongs to a superseded cohort: kept for audit, ordered last. */
  historical: boolean;
}

export interface EquitySeries {
  sleeve_id: string;
  name: string;
  points: Num[];
}

export interface SafetyView {
  kill_engaged: boolean;
  kill_since: string | null;
  kill_reason: string | null;
  trades_today: number;
  realized_pnl: Num;
  start_equity: Num | null;
  capital_cap: Num;
  daily_loss_limit: Num;
  max_trades_per_day: number;
  max_order_notional: Num;
}

export interface PositionRow {
  symbol: string;
  quantity: Num;
  settled: Num;
  average_price: Num | null;
  market_value: Num | null;
  day_pl: Num | null;
}

export interface PositionsView {
  account: string;
  available: boolean;
  message: string | null;
  positions: PositionRow[];
  liquidation_value: Num | null;
  cash_available_for_trading: Num | null;
  cash_available_for_withdrawal: Num | null;
}

export interface OrderRow {
  order_id: string;
  status: string;
  side: string | null;
  symbol: string | null;
  quantity: Num | null;
  filled: Num | null;
  limit_price: Num | null;
  working: boolean;
}

export interface OrdersView {
  available: boolean;
  message: string | null;
  hours: number;
  orders: OrderRow[];
}

export interface ValidationRow {
  strategy: string;
  universe: string;
  validated: boolean;
  pass_rate: number;
  min_pass_rate: number;
  mean_excess_pct: Num | null;
  beats_benchmark: boolean;
}

export interface ValidationView {
  rows: ValidationRow[];
}

export interface ApprovalRow {
  token: string;
  describe: string;
  rationale: string | null;
  account_tail: string;
  expires_in_min: number;
}

export interface ApprovalsView {
  rows: ApprovalRow[];
}

export interface RegimeView {
  available: boolean;
  message: string | null;
  score: number;
  gross_exposure_cap: Num;
  spy_above_200dma: boolean;
  trend_50_over_200: boolean;
  breadth_above_50: boolean;
  calm_volatility: boolean;
  breadth_pct: number;
}

export interface TaxLotRow {
  symbol: string;
  quantity: Num;
  cost_per_share: Num;
  acquired: string;
  days_held: number;
  long_term: boolean;
}

export interface TaxLotsView {
  rows: TaxLotRow[];
}

export interface AuditRow {
  ts: string;
  command: string;
  event: string;
  detail: string | null;
}

export interface AuditView {
  rows: AuditRow[];
}

export interface ReconciliationView {
  completed_at: string | null;
  success: boolean | null;
  orders_seen: number;
  transitions: number;
  fills_applied: number;
  discrepancies: number;
  pending_fill_applications: number;
}

/** A cohort kept as evidence rather than collected: readable, never the default. */
export interface HistoricalCohortView {
  cohort_id: string;
  lifecycle: string;
  label: string;
  reason: string;
  superseded_by: string | null;
  reference: string;
}

/** An active cohort and the persisted start session that ranked it. */
export interface ActiveCohortView {
  cohort_id: string;
  /** Immutable exchange start session; null when the manifest records none. */
  start_session: string | null;
  is_default: boolean;
}

export interface CohortSelectionView {
  requested: string | null;
  selected: string | null;
  /** Every persisted cohort, historical ones included. Audit never loses a cohort. */
  available: string[];
  /** The subset still collecting, newest first. The picker offers these. */
  active: string[];
  /** The same set with start sessions, for the multiple-active warning. */
  active_cohorts: ActiveCohortView[];
  /** Superseded cohorts, for the collapsed Historical Cohorts section. */
  historical: HistoricalCohortView[];
  /** True only when the operator explicitly opened a historical cohort. */
  selected_is_historical: boolean;
  /** True when more than one cohort is collecting: a state needing a human decision. */
  multiple_active: boolean;
  /** Active cohorts the default won against. Still running, still owed a decision. */
  older_active: string[];
  /** True when active cohorts exist but none can be defaulted to without guessing. */
  ambiguous: boolean;
  /** Why nothing could be defaulted to. Set exactly when `ambiguous` is true. */
  ambiguity_reason: string;
}

export interface CohortIdentityView {
  cohort_id: string;
  /** Stable sleeve identities. Render `member_names` instead. */
  member_sleeves: string[];
  member_names: string[];
  benchmark_sleeve: string | null;
  benchmark_sleeve_name: string | null;
  /** Set only when every member started from the same capital. */
  starting_capital_per_sleeve: Num | null;
  created_at: string;
  first_session: string | null;
  latest_session: string | null;
}

/** Life-cycle phase of an official cohort. Presentation only; never authorization. */
export type CohortPhaseName =
  | "scheduled"
  | "collecting"
  | "review-ready"
  | "passed"
  | "attention-needed"
  | "failed";

export interface LatestRunView {
  run_id: string;
  scheduled_for: string;
  status: string;
  completed_members: number;
  expected_members: number;
  error_summary: string | null;
}

/**
 * Where the clock stands relative to the cohort's next owed session.
 *
 * Deliberately independent of `CohortPhaseName`. A session's official decision time is
 * the exchange close, so before that close a pending run is `upcoming` — not late, not
 * missing, and not a reproducibility defect. After the close it is `awaiting-execution`
 * inside normal grace. A persisted provider wait is `awaiting-provider-data` through the
 * actual retry deadline and becomes `overdue` only when that deadline expires.
 */
export type SessionTimingState =
  | "no-session-scheduled"
  | "upcoming"
  | "awaiting-execution"
  | "awaiting-provider-data"
  | "overdue"
  | "settled";

export interface CohortPhaseView {
  cohort_id: string;
  phase: CohortPhaseName;
  headline: string;
  /** Calendar date of the clock the phase was assessed against. */
  as_of: string;
  timing_state: SessionTimingState;
  /** Naive Eastern wall clock the assessment was pinned to, when one was injected. */
  now_et: string | null;
  /**
   * Latest exchange session whose official close has already happened. Sessions after
   * it cannot have produced evidence, so they are never missing observations.
   */
  evidence_cutoff: string | null;
  /** A closed session still inside the scheduler's normal execution grace period. */
  awaiting_execution_session: string | null;
  /** A session whose provider evidence is incomplete but still safely retryable. */
  awaiting_provider_session: string | null;
  /** Sessions whose ordinary grace or provider-specific hard deadline expired. */
  overdue_sessions: string[];
  start_session: string | null;
  started: boolean;
  review_target: number;
  /** Sessions scheduled on or before `as_of`. Future sessions are never counted. */
  due_sessions: number;
  completed_due_sessions: number;
  sessions_remaining: number;
  progress_ratio: number;
  total_scheduled_sessions: number;
  next_scheduled_session: string | null;
  latest_due_run: LatestRunView | null;
  latest_observation_date: string | null;
  official_observations: number;
  completion_reliability: number | null;
  next_action: string;
  integrity_alerts: string[];
}

export interface CohortSleeveView {
  sleeve_id: string;
  sleeve_name: string;
  strategy: string;
  reproducible: boolean;
  configuration_hash: string | null;
  definition: Record<string, unknown> | null;
}

export interface ReadinessEvidenceView {
  sleeve_id: string;
  sleeve_name: string;
  session_date: string | null;
  observation_status: string | null;
  evidence_status: string;
  ready: boolean | null;
  reason_codes: string[];
  quote_coverage: Num | null;
  snapshot_ids: Record<string, string>;
}

export interface ComparisonExclusionView {
  session_date: string;
  reason: string;
}

export interface RollingExcessView {
  start_date: string;
  end_date: string;
  sleeve_return: number;
  benchmark_return: number;
  excess_return: number;
}

export interface ComparisonReliabilityView {
  total_observations: number;
  official: number;
  partial: number;
  missing: number;
  used: number;
  excluded: number;
  readiness_ready: number;
  readiness_unready: number;
  readiness_unknown: number;
  reason_codes: string[];
}

export interface SleeveComparisonView {
  sleeve_id: string;
  sleeve_name: string;
  strategy: string;
  maturity: string;
  sample_count: number;
  matched_dates: string[];
  sleeve_return: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  rolling_excess: Record<string, RollingExcessView[]>;
  max_drawdown_pct: Num;
  volatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  beta: number | null;
  correlation: number | null;
  total_turnover: Num | null;
  turnover_ratio: Num | null;
  total_modeled_cost: Num | null;
  modeled_cost_drag: Num | null;
  num_filled: number;
  num_rejected: number;
  reject_rate: number | null;
  coverage: number;
  reliability: ComparisonReliabilityView;
  exclusions: ComparisonExclusionView[];
}

export interface SleeveCorrelationView {
  sleeve_a: string;
  sleeve_b: string;
  sample_count: number;
  correlation: number | null;
}

export interface CohortComparisonView {
  cohort_id: string | null;
  available: boolean;
  message: string | null;
  benchmark_dates: string[];
  common_dates: string[];
  sleeves: SleeveComparisonView[];
  correlations: SleeveCorrelationView[];
}

export interface RunErrorView {
  code: string;
  message: string;
  member_id: string | null;
  capability: string | null;
  retryable: boolean;
  context: Record<string, unknown>;
}

export interface RunMemberView {
  sleeve_id: string;
  sleeve_name: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  error: RunErrorView | null;
}

/** One run's state relative to its own session's official close. */
export type RunTiming =
  | "upcoming"
  | "awaiting-execution"
  | "awaiting-provider-data"
  | "overdue"
  | "executed"
  | "closed-session";

export interface CohortRunView {
  run_id: string;
  run_key: string;
  cohort_id: string;
  session_id: string;
  scheduled_for: string;
  /** Durable run status, e.g. `pending`, `completed`, `partial`. */
  status: string;
  /**
   * Clock-relative timing. Render this beside `status`: a `pending` run reads very
   * differently when it is `upcoming` than when it is `overdue`.
   */
  timing: RunTiming;
  expected_members: string[];
  completed_members: string[];
  started_at: string;
  completed_at: string | null;
  snapshot_id: string | null;
  quote_snapshot_id: string | null;
  data_snapshot_ids: Record<string, string>;
  retry_deadline_et: string | null;
  errors: RunErrorView[];
  members: RunMemberView[];
}

export interface CohortRunHealthView {
  available: boolean;
  message: string | null;
  total_runs: number | null;
  latest_status: string | null;
  executed_runs: number;
  /** Closed sessions still inside the scheduler's grace period. Due, not failed. */
  awaiting_execution_runs: number;
  /** Runs still safely retryable while provider evidence completes. */
  awaiting_provider_data_runs: number;
  upcoming_runs: number;
  overdue_runs: number;
  runs: CohortRunView[];
}

/**
 * Phase-aware display class for a gate rule. Derived from `status` plus the cohort
 * phase; it never replaces `status`, which still fails closed.
 */
export type RulePresentation = "healthy" | "awaiting-evidence" | "needs-attention";

export interface GateRuleView {
  /** Stable rule identifier, e.g. `completion-rate`. */
  rule: string;
  /** Plain-language rule name, e.g. `Completion reliability`. */
  label: string;
  status: string;
  reason: string;
  /** The rule's evidence has not been produced or recorded yet. */
  awaiting_evidence: boolean;
  presentation: RulePresentation;
  evidence: string[];
}

export interface OperationalGateView {
  cohort_id: string | null;
  available: boolean;
  message: string | null;
  status: string | null;
  summary: string | null;
  operationally_useful: boolean | null;
  investment_alpha_assessed: boolean;
  live_trading_authorized: boolean;
  rules: GateRuleView[];
}

/* --- 30-session accounting review (read-only) ------------------------------
 *
 * The dashboard never writes a review record; `schwab-trader cohort review` is the
 * canonical write path. Everything below mirrors the durable, append-only records in
 * `schwab_trader.cohort_review`.
 */

export type AccountingArea = "cash" | "positions" | "valuation";
export type ReviewFinding = "matched" | "difference";
export type OperatorActionName = "keep" | "modify" | "pause" | "retire";

export interface AccountingCheckView {
  entry_id: string;
  observation_key: string;
  sleeve_id: string;
  sleeve_name: string;
  session_date: string;
  area: AccountingArea;
  finding: ReviewFinding;
  summary: string | null;
  explanation: string | null;
  /** A matched area, or a difference with a usable explanation. */
  explained: boolean;
  recorded_at: string;
  recorded_by: string;
  /** Higher revisions supersede lower ones. The earlier entry is never deleted. */
  revision: number;
  supersedes: string | null;
}

export interface PendingObservationView {
  observation_key: string;
  sleeve_id: string;
  sleeve_name: string;
  session_date: string;
  missing_areas: AccountingArea[];
}

export interface ReviewNoteView {
  note_id: string;
  sleeve_id: string | null;
  sleeve_name: string;
  observation_key: string | null;
  note: string;
  recorded_at: string;
  recorded_by: string;
}

export interface OperatorDecisionView {
  decision_id: string;
  sleeve_id: string;
  sleeve_name: string;
  action: OperatorActionName;
  rationale: string;
  recorded_at: string;
  recorded_by: string;
  revision: number;
  supersedes: string | null;
}

export interface CohortReviewView {
  /** False only when the review store could not be read — never when it is empty. */
  available: boolean;
  message: string | null;
  review_due: boolean;
  review_target: number;
  completed_due_sessions: number;
  official_observations: number;
  reviewed_observations: number;
  /** Current accounting entries recorded. The entries themselves are not served in
   *  bulk; only differences and corrections are. */
  recorded_check_count: number;
  unexplained_difference_count: number;
  decided_sleeve_count: number;
  member_count: number;
  differences: AccountingCheckView[];
  superseded_checks: AccountingCheckView[];
  pending_observations: PendingObservationView[];
  notes: ReviewNoteView[];
  decisions: OperatorDecisionView[];
  superseded_decisions: OperatorDecisionView[];
}

export interface CohortDashboardView {
  contract_version: string;
  available: boolean;
  message: string | null;
  selection: CohortSelectionView;
  identity: CohortIdentityView | null;
  phase: CohortPhaseView | null;
  /** Stable sleeve id to human-readable name, for every id referenced in this view. */
  sleeve_names: Record<string, string>;
  sleeve_definitions: CohortSleeveView[];
  comparison: CohortComparisonView;
  run_health: CohortRunHealthView;
  readiness: ReadinessEvidenceView[];
  operational_gate: OperationalGateView;
  cohort_review: CohortReviewView;
}

/* --- Sleeve detail (GET /api/sleeve) --------------------------------------
 *
 * Mirrors the pydantic models in `schwab_trader.sleeve_detail`. Served by a separate,
 * lazily requested route rather than folded into `DashboardData`: the record carries
 * per-sleeve history, and the dashboard payload is polled every 30 seconds.
 */

/** The window a history section was returned through. `offset` counts back from the
 *  most recent record, so `offset: 0` is always the newest page. */
export interface PageInfo {
  limit: number;
  offset: number;
  returned: number;
  /** Records the source held within its scan ceiling — a floor when `truncated`. */
  available: number;
  has_more: boolean;
  /** True when the scan ceiling was hit, so `available` is not an exact count. */
  truncated: boolean;
}

export interface SleeveLifecycleView {
  /** `active` or `superseded`, from the cohort lifecycle registry. */
  lifecycle: string;
  /** True when the sleeve's cohort was withdrawn. The record stays readable. */
  historical: boolean;
  label: string;
  reason: string;
  superseded_by: string | null;
  reference: string;
  /** Always false. This view offers no action for any sleeve, superseded or not. */
  actionable: boolean;
}

export interface SleeveIdentityView {
  /** Stable storage/API identity. The only thing that selects this sleeve. */
  sleeve_id: string;
  /** Display name. Not unique across cohorts; never an identity. */
  name: string;
  original_name: string;
  strategy: string;
  scope: SleeveScope;
  cohort_id: string | null;
  namespace_id: string;
  created_at: string;
  lifecycle: SleeveLifecycleView;
}

export interface SleeveStrategyView {
  available: boolean;
  message: string | null;
  strategy: string;
  strategy_id: string | null;
  strategy_version: string | null;
  implementation_name: string | null;
  parameters: Record<string, unknown>;
  universe_definition: unknown;
  universe: string[];
  factor: string;
  configuration_hash: string | null;
  /** False for a legacy sleeve whose parameters were never captured. Labelled, not faked. */
  reproducible: boolean;
  decision_frequency: string;
  decision_time: string;
  benchmark_symbol_or_sleeve: string | null;
  data_requirements: string[];
  long_only: boolean | null;
  leverage_allowed: boolean | null;
}

export interface SleeveCapitalView {
  starting_capital: Num;
  /** `t+1` when settled-cash modeling is on, otherwise `instant`. */
  settlement_model: string;
  leverage: Num;
  max_positions: number;
  max_position_fraction: Num;
}

export interface SleevePositionRow {
  symbol: string;
  quantity: number;
  average_cost: Num;
  cost_basis: Num;
  /** Share of the sleeve's total position *cost basis*, not of market value. */
  cost_basis_weight: Num | null;
  /** Always null: no current mark is available and this view never requests a quote. */
  market_value: Num | null;
  /** Always null, for the same reason as `market_value`. */
  unrealized_pnl: Num | null;
  mark_status: string;
}

export interface SleevePositionsView {
  available: boolean;
  message: string | null;
  /** When the read happened. Positions carry no recorded timestamp of their own. */
  as_of: string | null;
  rows: SleevePositionRow[];
  total_cost_basis: Num | null;
  mark_note: string;
}

export interface SleeveCashView {
  available: boolean;
  message: string | null;
  as_of: string | null;
  settled_cash: Num | null;
  unsettled_cash: Num | null;
  total_cash: Num | null;
  realized_pnl: Num | null;
  starting_capital: Num | null;
  buying_power: Num | null;
  opened_at: string | null;
}

export interface RecordedValuationView {
  available: boolean;
  message: string | null;
  /** `official-observation` or `evaluation-cycle`: which record this came from. */
  source: string | null;
  as_of: string | null;
  session_date: string | null;
  total_equity: Num | null;
  positions_value: Num | null;
  cash: Num | null;
  unrealized_pnl: Num | null;
  realized_pnl: Num | null;
  return_pct: Num | null;
  /**
   * Whether `return_pct` is a `ratio` (0.0154) or already a `percent` (1.54). The two
   * recorded sources genuinely disagree — an observation stores a ratio, an evaluation
   * cycle stores a percentage — so format from this field rather than guessing and being
   * wrong by a factor of a hundred.
   */
  return_pct_basis: string | null;
  benchmark_value: Num | null;
  /** Observation status when the source is an observation. A partial record is labelled. */
  status: string | null;
}

export interface SleevePerformanceView {
  available: boolean;
  message: string | null;
  as_of: string | null;
  first_recorded_at: string | null;
  cycles: number;
  trades_filled: number;
  starting_capital: Num | null;
  latest_value: Num | null;
  total_return_pct: Num | null;
  realized_pnl: Num | null;
  max_drawdown_pct: Num | null;
  sharpe: Num | null;
}

export interface EquityPointView {
  as_of: string;
  session_date: string | null;
  total_value: Num;
  source: string;
}

export interface EquityHistoryView {
  available: boolean;
  message: string | null;
  source: string | null;
  page: PageInfo | null;
  /** Oldest-first inside the returned window, so it plots directly. */
  points: EquityPointView[];
}

export interface CycleRowView {
  cycle_id: number;
  as_of: string;
  strategy: string;
  num_proposals: number;
  num_filled: number;
  num_rejected: number;
  cash: Num;
  positions_value: Num;
  total_value: Num;
  realized_pnl: Num;
  unrealized_pnl: Num;
  /** Already a percentage (`1.54` means 1.54%), as the paper valuation records it. */
  return_pct: Num;
}

export interface CyclesView {
  available: boolean;
  message: string | null;
  page: PageInfo | null;
  rows: CycleRowView[];
}

export interface SimulatedOrderRowView {
  paper_order_id: number;
  as_of: string;
  side: string;
  symbol: string;
  quantity: number;
  limit_price: Num;
  /** `filled` or `rejected`, as recorded by the paper engine. Never a broker order. */
  status: string;
  /** The recorded rejection reason, when the order was rejected. */
  reason: string | null;
  fill_price: Num | null;
  filled_at: string | null;
}

export interface SimulatedOrdersView {
  available: boolean;
  message: string | null;
  page: PageInfo | null;
  rows: SimulatedOrderRowView[];
}

export interface ObservationRowView {
  session_date: string;
  /** The recorded valuation time for the session. */
  as_of: string;
  decision_time: string;
  status: string;
  run_id: string;
  strategy_hash: string;
  total_value: Num | null;
  /** A *ratio* (`0.0154` means 1.54%) — a different unit from `CycleRowView.return_pct`. */
  return_pct: Num | null;
  benchmark_value: Num | null;
  exposure: Num | null;
  num_positions: number | null;
  turnover: Num | null;
  modeled_cost: Num | null;
  num_filled: number;
  num_rejected: number;
  quote_coverage: Num | null;
  readiness_ready: boolean | null;
  readiness_reasons: string[];
  snapshot_ids: Record<string, string>;
}

export interface ObservationsView {
  available: boolean;
  message: string | null;
  page: PageInfo | null;
  rows: ObservationRowView[];
}

export interface RunRowView {
  run_id: string;
  run_key: string;
  session_id: string;
  scheduled_for: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  member_status: string | null;
  member_started_at: string | null;
  member_completed_at: string | null;
  member_error_code: string | null;
  member_error_message: string | null;
  snapshot_id: string | null;
  quote_snapshot_id: string | null;
  data_snapshot_ids: Record<string, string>;
}

export interface RunsView {
  available: boolean;
  message: string | null;
  page: PageInfo | null;
  rows: RunRowView[];
}

export interface SleeveLineageView {
  namespace_id: string;
  cohort_id: string | null;
  configuration_hash: string | null;
  /** More than one means the recorded definition changed mid-collection. */
  strategy_hashes: string[];
  run_ids: string[];
  snapshot_ids: Record<string, string>;
  cohort_start_session: string | null;
  /** Honest gaps: fields an operator might expect that storage does not record. */
  notes: string[];
}

export interface SleeveDetail {
  contract_version: string;
  generated_at: string;
  /** Always true. There is no action path in this contract. */
  read_only: boolean;
  identity: SleeveIdentityView;
  strategy: SleeveStrategyView;
  capital: SleeveCapitalView;
  positions: SleevePositionsView;
  cash: SleeveCashView;
  recorded_valuation: RecordedValuationView;
  performance: SleevePerformanceView;
  equity_history: EquityHistoryView;
  cycles: CyclesView;
  simulated_orders: SimulatedOrdersView;
  observations: ObservationsView;
  runs: RunsView;
  lineage: SleeveLineageView;
  warnings: string[];
}

/** The error body `/api/sleeve` returns for a refused request. Carries a stable code
 *  and a pre-written sentence — never an exception, path, or connection string. */
export interface SleeveDetailErrorBody {
  error: {
    code: string;
    message: string;
  };
}

export interface DashboardData {
  api_version: string;
  generated_at: string;
  /**
   * Whether the server was started with broker access. When false, broker-derived
   * sections are unavailable rather than zero, and the UI must not lead with them.
   */
  live_enabled: boolean;
  benchmark: string;
  sleeves: SleeveRow[];
  curves: EquitySeries[];
  safety: SafetyView;
  positions: PositionsView;
  summary: SummaryView;
  orders: OrdersView;
  validation: ValidationView;
  approvals: ApprovalsView;
  regime: RegimeView;
  tax_lots: TaxLotsView;
  audit: AuditView;
  reconciliation: ReconciliationView;
  cohort: CohortDashboardView;
}
