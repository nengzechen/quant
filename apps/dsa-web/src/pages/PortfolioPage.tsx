import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import { quantApi } from '../api/quant';
import type { PortfolioData, Position, Trade, OrderRequest } from '../api/quant';
import { getParsedApiError } from '../api/error';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert } from '../components/common';

// ─── 工具函数 ───────────────────────────────────────────────

const fmt = (n: number, digits = 2) =>
  n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtPct = (n: number) => `${n >= 0 ? '+' : ''}${fmt(n)}%`;

const pnlClass = (n: number) =>
  n > 0 ? 'text-red-400' : n < 0 ? 'text-green-400' : 'text-secondary';

const actionLabel = (a: string) => (a === 'BUY' ? '买入' : '卖出');
const actionClass = (a: string) =>
  a === 'BUY' ? 'text-red-400' : 'text-green-400';

const fmtTime = (iso: string | null) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
};

// ─── 账户概览卡片 ─────────────────────────────────────────

const StatCard: React.FC<{
  label: string;
  value: string;
  sub?: string;
  subClass?: string;
}> = ({ label, value, sub, subClass }) => (
  <div className="terminal-card p-4 flex flex-col gap-1">
    <span className="text-xs text-muted uppercase tracking-wider">{label}</span>
    <span className="text-xl font-mono font-semibold text-primary">{value}</span>
    {sub && <span className={`text-xs font-mono ${subClass ?? 'text-secondary'}`}>{sub}</span>}
  </div>
);

// ─── 持仓表格 ─────────────────────────────────────────────

const PositionsTable: React.FC<{ positions: Position[]; onSell: (p: Position) => void }> = ({
  positions,
  onSell,
}) => {
  if (positions.length === 0)
    return <p className="text-muted text-sm py-6 text-center">暂无持仓</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-default text-muted text-xs uppercase tracking-wider">
            {['代码', '名称', '持仓量', '成本价', '现价', '市值', '盈亏', '盈亏%', '止损价', '建仓时间', '操作'].map(
              (h) => (
                <th key={h} className="text-left py-2 px-3 font-normal whitespace-nowrap">
                  {h}
                </th>
              )
            )}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr
              key={p.stock_code}
              className="border-b border-dim hover:bg-elevated transition-colors"
            >
              <td className="py-3 px-3 font-mono text-cyan">{p.stock_code}</td>
              <td className="py-3 px-3 text-primary">{p.stock_name}</td>
              <td className="py-3 px-3 font-mono">{p.quantity.toLocaleString()}</td>
              <td className="py-3 px-3 font-mono">{fmt(p.avg_cost, 3)}</td>
              <td className="py-3 px-3 font-mono">{fmt(p.current_price, 3)}</td>
              <td className="py-3 px-3 font-mono">{fmt(p.market_value)}</td>
              <td className={`py-3 px-3 font-mono ${pnlClass(p.pnl)}`}>{fmt(p.pnl)}</td>
              <td className={`py-3 px-3 font-mono ${pnlClass(p.pnl_pct)}`}>{fmtPct(p.pnl_pct)}</td>
              <td className="py-3 px-3 font-mono text-warning">
                {p.stop_loss_price ? fmt(p.stop_loss_price, 3) : '—'}
              </td>
              <td className="py-3 px-3 text-secondary text-xs whitespace-nowrap">
                {fmtTime(p.open_time)}
              </td>
              <td className="py-3 px-3">
                <button
                  type="button"
                  className="text-xs px-2 py-1 rounded border border-green-500/40 text-green-400 hover:bg-green-500/10 transition-colors"
                  onClick={() => onSell(p)}
                >
                  卖出
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

// ─── 交易记录表格 ─────────────────────────────────────────

const TradesTable: React.FC<{ trades: Trade[] }> = ({ trades }) => {
  if (trades.length === 0)
    return <p className="text-muted text-sm py-6 text-center">暂无交易记录</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-default text-muted text-xs uppercase tracking-wider">
            {['时间', '代码', '方向', '股数', '成交价', '金额', '手续费', '状态'].map((h) => (
              <th key={h} className="text-left py-2 px-3 font-normal whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.record_id} className="border-b border-dim hover:bg-elevated transition-colors">
              <td className="py-2 px-3 text-secondary text-xs whitespace-nowrap">
                {fmtTime(t.timestamp)}
              </td>
              <td className="py-2 px-3 font-mono text-cyan">{t.stock_code}</td>
              <td className={`py-2 px-3 font-semibold ${actionClass(t.action)}`}>
                {actionLabel(t.action)}
              </td>
              <td className="py-2 px-3 font-mono">{t.quantity.toLocaleString()}</td>
              <td className="py-2 px-3 font-mono">{fmt(t.price, 3)}</td>
              <td className="py-2 px-3 font-mono">{fmt(t.total_amount)}</td>
              <td className="py-2 px-3 font-mono text-muted">{fmt(t.commission)}</td>
              <td className="py-2 px-3">
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    t.status === 'FILLED'
                      ? 'bg-green-500/15 text-green-400'
                      : 'bg-red-500/15 text-red-400'
                  }`}
                >
                  {t.status === 'FILLED' ? '成交' : t.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

// ─── 主页面 ───────────────────────────────────────────────

const PortfolioPage: React.FC = () => {
  const [data, setData] = useState<PortfolioData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [lastUpdated, setLastUpdated] = useState('');
  // 快速卖出：点击持仓行的「卖出」按钮后，自动填入下单表单（暂用 alert 简化）
  const [sellTarget, setSellTarget] = useState<Position | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await quantApi.getPortfolio();
      setData(d);
      setError(null);
      setLastUpdated(new Date().toLocaleTimeString('zh-CN'));
    } catch (e) {
      setError(getParsedApiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // 首次加载 + 每 5 秒刷新
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  // 处理持仓行的「卖出」快捷操作
  const handleSell = useCallback((p: Position) => {
    setSellTarget(p);
    // 滚动到下单表单
    document.getElementById('order-form')?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  if (loading)
    return (
      <div className="flex items-center justify-center h-full min-h-[60vh]">
        <div className="w-8 h-8 border-2 border-cyan/20 border-t-cyan rounded-full animate-spin" />
      </div>
    );

  return (
    <div className="p-6 space-y-6 max-w-[1400px]">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-primary">模拟持仓</h1>
          <p className="text-xs text-muted mt-0.5">每 5 秒自动刷新 · 最后更新 {lastUpdated}</p>
        </div>
        <button
          type="button"
          className="btn-secondary text-xs px-3 py-1.5"
          onClick={() => void load()}
        >
          刷新
        </button>
      </div>

      {error && <ApiErrorAlert error={error} />}

      {data && (
        <>
          {/* 账户概览 */}
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            <StatCard label="总资产" value={`¥${fmt(data.account.total_assets)}`} />
            <StatCard label="可用现金" value={`¥${fmt(data.account.available_cash)}`} />
            <StatCard label="持仓市值" value={`¥${fmt(data.account.market_value)}`} />
            <StatCard
              label="总盈亏"
              value={`¥${fmt(data.account.total_pnl)}`}
              sub={fmtPct(data.account.pnl_pct ?? 0)}
              subClass={pnlClass(data.account.total_pnl)}
            />
            <StatCard
              label="持仓数量"
              value={`${data.account.position_count} / ${data.account.max_positions}`}
            />
          </div>

          {/* 下单表单 */}
          <div id="order-form">
            <OrderFormWithTarget
              sellTarget={sellTarget}
              onClearTarget={() => setSellTarget(null)}
              onSuccess={() => void load()}
            />
          </div>

          {/* 持仓列表 */}
          <div className="terminal-card p-5">
            <h3 className="text-sm font-semibold text-cyan mb-4 uppercase tracking-wider">
              当前持仓 ({data.positions.length})
            </h3>
            <PositionsTable positions={data.positions} onSell={handleSell} />
          </div>

          {/* 交易记录 */}
          <div className="terminal-card p-5">
            <h3 className="text-sm font-semibold text-purple mb-4 uppercase tracking-wider">
              最近交易记录
            </h3>
            <TradesTable trades={data.trades} />
          </div>
        </>
      )}
    </div>
  );
};

// 带快速卖出预填的下单表单
const OrderFormWithTarget: React.FC<{
  sellTarget: Position | null;
  onClearTarget: () => void;
  onSuccess: () => void;
}> = ({ sellTarget, onClearTarget, onSuccess }) => {
  const [form, setForm] = useState<OrderRequest>({
    stock_code: '',
    action: 'BUY',
    quantity: 100,
    price: 0,
    stop_loss_price: 0,
  });
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // 当 sellTarget 变化时，预填表单
  useEffect(() => {
    if (sellTarget) {
      setForm({
        stock_code: sellTarget.stock_code,
        action: 'SELL',
        quantity: sellTarget.quantity,
        price: sellTarget.current_price,
        stop_loss_price: 0,
      });
      setMsg(null);
    }
  }, [sellTarget]);

  const set = (k: keyof OrderRequest, v: string | number) => {
    setForm((f) => ({ ...f, [k]: v }));
    if (sellTarget) onClearTarget();
  };

  const submit = async () => {
    if (!form.stock_code.trim()) { setMsg({ type: 'err', text: '请填写股票代码' }); return; }
    if (form.price <= 0) { setMsg({ type: 'err', text: '价格须大于 0' }); return; }
    if (form.quantity <= 0 || form.quantity % 100 !== 0) {
      setMsg({ type: 'err', text: '股数须为 100 的整数倍' }); return;
    }
    setSubmitting(true);
    setMsg(null);
    try {
      const res = await quantApi.placeOrder(form);
      setMsg({ type: 'ok', text: `${actionLabel(form.action)}成功，状态: ${res.status}` });
      onSuccess();
      onClearTarget();
    } catch (e) {
      const err = getParsedApiError(e);
      setMsg({ type: 'err', text: err.message || '下单失败' });
    } finally {
      setSubmitting(false);
    }
  };

  const isSell = form.action === 'SELL';

  return (
    <div className="terminal-card p-5">
      <h3 className="text-sm font-semibold text-cyan mb-4 uppercase tracking-wider">手动下单</h3>
      {sellTarget && (
        <div className="mb-3 text-xs text-warning bg-warning/10 border border-warning/20 rounded px-3 py-2">
          已预填「{sellTarget.stock_name}（{sellTarget.stock_code}）」卖出表单，请确认后提交
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted">股票代码</label>
          <input
            className="input-terminal font-mono"
            placeholder="600519"
            value={form.stock_code}
            onChange={(e) => set('stock_code', e.target.value.trim())}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted">方向</label>
          <select
            className="input-terminal"
            value={form.action}
            onChange={(e) => set('action', e.target.value as 'BUY' | 'SELL')}
          >
            <option value="BUY">买入</option>
            <option value="SELL">卖出</option>
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted">价格</label>
          <input
            type="number"
            className="input-terminal font-mono"
            placeholder="0.00"
            min={0}
            step={0.01}
            value={form.price || ''}
            onChange={(e) => set('price', parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted">股数</label>
          <input
            type="number"
            className="input-terminal font-mono"
            placeholder="100"
            min={100}
            step={100}
            value={form.quantity}
            onChange={(e) => set('quantity', parseInt(e.target.value) || 100)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted">止损价（可选）</label>
          <input
            type="number"
            className="input-terminal font-mono"
            placeholder="0.00"
            min={0}
            step={0.01}
            value={form.stop_loss_price || ''}
            onChange={(e) => set('stop_loss_price', parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted opacity-0">操作</label>
          <button
            type="button"
            className={`h-9 text-sm font-medium rounded-lg border transition-colors px-4 ${
              isSell
                ? 'bg-green-600/20 border-green-500/50 text-green-400 hover:bg-green-600/30'
                : 'btn-primary'
            }`}
            disabled={submitting}
            onClick={() => void submit()}
          >
            {submitting ? '提交中…' : `${actionLabel(form.action)}`}
          </button>
        </div>
      </div>
      {msg && (
        <p className={`mt-3 text-xs font-mono ${msg.type === 'ok' ? 'text-green-400' : 'text-red-400'}`}>
          {msg.text}
        </p>
      )}
    </div>
  );
};

export default PortfolioPage;
