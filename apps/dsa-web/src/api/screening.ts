import apiClient from './index';

export interface SeedEntry {
  code: string;
  name: string;
  model: string;
  phase1_score: number;
  max_score: number;
  passed_dims: string[];
  failed_dims: string[];
  dim_details: Record<string, string>;
  created_at: string;
  phase2_triggered: boolean;
  phase2_trigger_time: string;
  phase2_reason: string;
}

export interface SeedPool {
  date: string;
  created_at: string;
  count: number;
  triggered_count: number;
  entries: SeedEntry[];
}

export const screeningApi = {
  getSeedPool: (date?: string) =>
    apiClient
      .get<SeedPool>('/api/v1/screening/seed-pool', { params: date ? { date } : {} })
      .then((r) => r.data),

  getDates: () =>
    apiClient
      .get<{ dates: string[] }>('/api/v1/screening/dates')
      .then((r) => r.data),
};
