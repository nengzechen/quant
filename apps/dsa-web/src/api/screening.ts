import apiClient from './index';
import { IS_DEMO, fetchDemoJson } from '../utils/demo';

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
  getSeedPool: async (date?: string): Promise<SeedPool> => {
    if (IS_DEMO) {
      const d = date || (await screeningApi.getDates()).dates[0];
      if (!d) {
        return { date: '—', created_at: '', count: 0, triggered_count: 0, entries: [] };
      }
      return fetchDemoJson<SeedPool>(`seed_pool_${d}.json`);
    }
    const r = await apiClient.get<SeedPool>('/api/v1/screening/seed-pool', {
      params: date ? { date } : {},
    });
    return r.data;
  },

  getDates: async (): Promise<{ dates: string[] }> => {
    if (IS_DEMO) {
      return fetchDemoJson<{ dates: string[] }>('dates.json');
    }
    const r = await apiClient.get<{ dates: string[] }>('/api/v1/screening/dates');
    return r.data;
  },
};
