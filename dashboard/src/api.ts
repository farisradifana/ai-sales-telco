// Thin typed client. Session is an httpOnly cookie set by the API; the browser never holds DB or service credentials.
export type Channel = 'sell_in' | 'sell_out';
export type ScopeType = 'outlet' | 'district';

export interface Me {
  principal_id: string;
  role: 'sales' | 'supervisor' | 'area_manager' | 'admin';
  display_name: string;
  sales_id: number | null;
  outlets: { id: number; code: string; name: string; district_id: number; district_name: string }[];
  districts: { id: number; code: string; name: string }[];
  watermarks: Record<string, string>;
}

export interface Period { start: string; end_exclusive: string; days: number; timezone: string }

export interface Driver {
  product_id: number; product: string; stock_unit: 'PCS' | 'IDR';
  q0: string; q1: string; r0: string; r1: string; delta_idr: string;
  volume_effect_idr: string; price_mix_effect_idr: string; entry_exit_effect_idr: string; growth: string | null;
}

export interface Action {
  priority: number; action: string; owner: string; review_in_days: number; success_metric: string;
  confidence: string; evidence: string[]; fact?: string; hypothesis?: string | null; guardrail?: string;
}

export interface TrendPoint {
  day: string; period: 'baseline' | 'current'; expected_outlet_days: number; complete_outlet_days: number;
  revenue_idr: string | null; observed_partial_idr: string | null; status: 'complete' | 'missing_input' | 'no_expected_outlets';
}

export interface RankRow { district_id: number; district_name: string; baseline_idr: string; current_idr: string; delta_idr: string; growth: string | null }

export interface Dashboard {
  evidence_id: string; evidence_digest: string; diagnose_evidence_id: string; correlation_id: string; query_version: string;
  scope: ScopeType; entity_id: number; entity: { name: string; code: string; district_id?: number; district_name?: string };
  channel: Channel; baseline: Period; current: Period; baseline_label: string; current_label: string; period_policy: string;
  revenue_baseline_idr: string; revenue_current_idr: string; delta_idr: string; growth: string | null;
  drivers: Driver[]; outlet_contributions: { outlet_id: number; name: string; baseline_idr: string; current_idr: string; delta_idr: string }[];
  coverage: Record<string, number | null>; baseline_coverage: Record<string, number | null>;
  population: { outlets_in_scope: number; opened_during_comparison: number; closed_during_comparison: number; note: string };
  watermark: string | null; source_batch_key: string | null; data_ready: boolean; status: 'final' | 'provisional'; issues: string[];
  current_complete_ratio: string | null; baseline_complete_ratio: string | null; reconciliation_residual_idr: string;
  recommendation: { data_ready: boolean; actions: Action[]; limitations: string[]; execution_policy: string };
  trend: TrendPoint[]; district_ranking: RankRow[]; generated_at: string; decomposition: string; stockout: string;
}

export interface StockEvidence {
  evidence_id: string; holder: { type: string; id: number }; as_of: string; last_movement_at: string | null;
  items: { product_id: number; sku: string; product: string; stock_unit: 'PCS' | 'IDR'; quantity: string }[];
}

export interface TargetEvidence {
  evidence_id: string; target_date: string; channel: Channel; target_idr: string | null; actual_idr: string; gap_idr: string | null;
  achievement_ratio: string | null; target_available: boolean; intraday: boolean;
  breakdown: { category: string; stock_unit: string; revenue_idr: string; quantity: string }[];
}

export interface PendingAction { action_id: string; requested_by: string; kind: string; summary: string; created_at: string; can_decide: boolean }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public correlationId?: string) { super(message); }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let code = `HTTP_${r.status}`, msg = r.statusText, corr: string | undefined;
    try { const j = await r.json(); code = j.error?.code ?? code; msg = j.error?.message ?? msg; corr = j.error?.correlation_id; } catch { /* non-JSON */ }
    throw new ApiError(r.status, code, msg, corr);
  }
  return r.json() as Promise<T>;
}

export interface PeriodInput { baseline_start: string; baseline_end: string; current_start: string; current_end: string }

export const api = {
  me: () => call<Me>('/v1/me'),
  devLogin: (principal_id: string) => call<{ status: string }>('/v1/auth/dev-login', { method: 'POST', body: JSON.stringify({ principal_id }) }),
  logout: () => call<{ status: string }>('/v1/auth/logout', { method: 'POST' }),
  dashboard: (scope: ScopeType, entity_id: number, channel: Channel, period?: PeriodInput) =>
    call<Dashboard>('/v1/tools/dashboard', { method: 'POST', body: JSON.stringify({ scope, entity_id, channel, ...(period ?? {}) }) }),
  stock: (body: object) => call<StockEvidence>('/v1/tools/stock', { method: 'POST', body: JSON.stringify(body) }),
  target: (channel: Channel) => call<TargetEvidence>('/v1/tools/target', { method: 'POST', body: JSON.stringify({ channel }) }),
  actions: () => call<{ items: PendingAction[] }>('/v1/actions'),
  decide: (id: string, decision: 'approve' | 'reject') =>
    call<object>(`/v1/actions/${id}/decision`, { method: 'POST', body: JSON.stringify({ decision, note: '' }) }),
  exportUrl: (scope: ScopeType, id: number, channel: Channel, start: string, end: string) =>
    `/v1/exports/sales.csv?${new URLSearchParams({ scope, entity_id: String(id), channel, start, end })}`,
};
