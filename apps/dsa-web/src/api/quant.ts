import apiClient from './index';

export interface AccountInfo {
  total_assets: number;
  available_cash: number;
  market_value: number;
  total_pnl: number;
  pnl_pct: number;
  position_count: number;
  max_positions: number;
}

export interface Position {
  stock_code: string;
  stock_name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  pnl: number;
  pnl_pct: number;
  stop_loss_price: number | null;
  open_time: string | null;
}

export interface Trade {
  order_id: string;
  stock_code: string;
  stock_name?: string;
  action: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  amount: number;
  commission: number;
  status: string;
  timestamp: string;
  reason?: string;
}

export interface PortfolioData {
  account: AccountInfo;
  positions: Position[];
  trades: Trade[];
}

export interface OrderRequest {
  stock_code: string;
  action: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  stop_loss_price?: number;
}

export const quantApi = {
  getPortfolio: () =>
    apiClient.get<PortfolioData>('/api/v1/quant/portfolio').then((r) => r.data),

  placeOrder: (req: OrderRequest) =>
    apiClient.post<{ status: string; trade: Trade }>('/api/v1/quant/order', req).then((r) => r.data),
};
