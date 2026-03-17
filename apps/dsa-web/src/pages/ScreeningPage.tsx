import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import { screeningApi } from '../api/screening';
import type { SeedPool, SeedEntry } from '../api/screening';
import { getParsedApiError } from '../api/error';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert } from '../components/common';

// ─── 工具函数 ─────────────────────────────────────────────

const MODEL_COLOR: Record<string, string> = {
  BottomSwing: 'text-cyan border-cyan/30 bg-cyan/5',
  StrongTrend: 'text-red-400 border-red-400/30 bg-red-400/5',
  LimitUpHunter: 'text-yellow-400 border-yellow-400/30 bg-yellow-400/5',
};
const MODEL_LABEL: Record<string, string> = {
  BottomSwing: '抄底波段',
  StrongTrend: '强势趋势',
  LimitUpHunter: '涨停猎手',
};

const fmtTime = (iso: string) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch { return iso; }
};

const fmtDate = (d: string) => {
  if (!d || d.length !== 8) return d;
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
};

// ─── 模型 Badge ────────────────────────────────────────────

const ModelBadge: React.FC<{ model: string }> = ({ model }) => (
  <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${MODEL_COLOR[model] ?? 'text-secondary border-default'}`}>
    {MODEL_LABEL[model] ?? model}
  </span>
);

// ─── 概览数字卡片 ───────────────────────────────────────────

const StatCard: React.FC<{ label: string; value: string | number; sub?: string; highlight?: boolean }> = ({
  label, value, sub, highlight,
}) => (
  <div className={`terminal-card p-4 flex flex-col gap-1 ${highlight ? 'border-cyan/30' : ''}`}>
    <span className="text-xs text-muted uppercase tracking-wider">{label}</span>
    <span className={`text-2xl font-mono font-semibold ${highlight ? 'text-cyan' : 'text-primary'}`}>{value}</span>
    {sub && <span className="text-xs text-muted">{sub}</span>}
  </div>
);

// ─── 单行展开详情 ───────────────────────────────────────────

const EntryRow: React.FC<{ entry: SeedEntry }> = ({ entry }) => {
  const [open, setOpen] = useState(false);
  const scorePct = entry.max_score ? Math.round((entry.phase1_score / entry.max_score) * 100) : 0;

  return (
    <>
      <tr
        className={`border-b border-dim cursor-pointer transition-colors ${
          entry.phase2_triggered
            ? 'bg-yellow-400/5 hover:bg-yellow-400/10'
            : 'hover:bg-elevated'
        }`}
        onClick={() => setOpen((v) => !v)}
      >
        <td className="py-3 px-3">
          <div className="flex items-center gap-1.5">
            {entry.phase2_triggered && (
              <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse flex-shrink-0" />
            )}
            <span className="font-mono text-cyan font-medium">{entry.code}</span>
          </div>
        </td>
        <td className="py-3 px-3 text-secondary text-sm">{entry.name || '—'}</td>
        <td className="py-3 px-3"><ModelBadge model={entry.model} /></td>
        <td className="py-3 px-3">
          <div className="flex items-center gap-2">
            <div className="w-20 h-1.5 rounded-full bg-elevated overflow-hidden">
              <div
                className="h-full rounded-full bg-cyan transition-all"
                style={{ width: `${scorePct}%` }}
              />
            </div>
            <span className="font-mono text-sm text-primary">
              {entry.phase1_score}<span className="text-muted">/{entry.max_score}</span>
            </span>
          </div>
        </td>
        <td className="py-3 px-3 max-w-xs">
          <div className="flex flex-wrap gap-1">
            {entry.passed_dims.slice(0, 4).map((d) => (
              <span key={d} className="text-xs px-1.5 py-0.5 rounded bg-green-500/10 text-green-400 border border-green-500/20">
                {d}
              </span>
            ))}
            {entry.passed_dims.length > 4 && (
              <span className="text-xs text-muted">+{entry.passed_dims.length - 4}</span>
            )}
          </div>
        </td>
        <td className="py-3 px-3">
          {entry.phase2_triggered ? (
            <div>
              <span className="text-xs px-2 py-0.5 rounded-full bg-yellow-400/15 text-yellow-400 border border-yellow-400/30 font-medium">
                已触发
              </span>
              <p className="text-xs text-muted mt-0.5">{fmtTime(entry.phase2_trigger_time)}</p>
            </div>
          ) : (
            <span className="text-xs text-muted">等待盘中</span>
          )}
        </td>
        <td className="py-3 px-3 text-xs text-muted whitespace-nowrap">{fmtTime(entry.created_at)}</td>
        <td className="py-3 px-3 text-muted text-xs">{open ? '▲' : '▼'}</td>
      </tr>

      {/* 展开详情 */}
      {open && (
        <tr className="bg-elevated border-b border-dim">
          <td colSpan={8} className="px-6 py-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* 通过的维度 */}
              <div>
                <p className="text-xs text-green-400 font-semibold mb-2 uppercase tracking-wider">通过指标</p>
                <div className="space-y-1">
                  {entry.passed_dims.map((d) => (
                    <div key={d} className="flex gap-2 text-xs">
                      <span className="text-green-400 flex-shrink-0">✓</span>
                      <span className="text-secondary font-medium w-32 flex-shrink-0">{d}</span>
                      <span className="text-muted">{entry.dim_details?.[d] ?? ''}</span>
                    </div>
                  ))}
                </div>
              </div>
              {/* 未通过 + Phase2 原因 */}
              <div>
                {entry.failed_dims.length > 0 && (
                  <>
                    <p className="text-xs text-red-400 font-semibold mb-2 uppercase tracking-wider">未通过指标</p>
                    <div className="space-y-1 mb-3">
                      {entry.failed_dims.map((d) => (
                        <div key={d} className="flex gap-2 text-xs">
                          <span className="text-red-400 flex-shrink-0">✗</span>
                          <span className="text-muted">{d}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {entry.phase2_triggered && entry.phase2_reason && (
                  <>
                    <p className="text-xs text-yellow-400 font-semibold mb-2 uppercase tracking-wider">Phase2 触发原因</p>
                    <p className="text-xs text-secondary">{entry.phase2_reason}</p>
                  </>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
};

// ─── 主页面 ───────────────────────────────────────────────

type Tab = 'all' | 'triggered';

const ScreeningPage: React.FC = () => {
  const [pool, setPool] = useState<SeedPool | null>(null);
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>('');
  const [tab, setTab] = useState<Tab>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [lastUpdated, setLastUpdated] = useState('');

  const load = useCallback(async (date?: string) => {
    try {
      const data = await screeningApi.getSeedPool(date || undefined);
      setPool(data);
      setError(null);
      setLastUpdated(new Date().toLocaleTimeString('zh-CN'));
    } catch (e) {
      setError(getParsedApiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // 加载可用日期
  useEffect(() => {
    screeningApi.getDates()
      .then((d) => {
        setDates(d.dates);
        if (d.dates.length > 0 && !selectedDate) {
          setSelectedDate(d.dates[0]);
        }
      })
      .catch(() => {});
  }, [selectedDate]);

  // 首次 + 每 15 秒刷新
  useEffect(() => {
    void load(selectedDate || undefined);
    const t = setInterval(() => void load(selectedDate || undefined), 15000);
    return () => clearInterval(t);
  }, [load, selectedDate]);

  const entries = pool?.entries ?? [];
  const triggered = entries.filter((e) => e.phase2_triggered);
  const displayed = tab === 'triggered' ? triggered : entries;

  // 按模型分组统计
  const modelCounts = entries.reduce<Record<string, number>>((acc, e) => {
    acc[e.model] = (acc[e.model] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="p-6 space-y-6 max-w-[1400px]">
      {/* 页头 */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-semibold text-primary">选股监控</h1>
          <p className="text-xs text-muted mt-0.5">每 15 秒自动刷新 · 最后更新 {lastUpdated}</p>
        </div>
        <div className="flex items-center gap-3">
          {/* 日期选择 */}
          <select
            className="input-terminal text-sm py-1.5 px-3 h-9"
            value={selectedDate}
            onChange={(e) => setSelectedDate(e.target.value)}
          >
            {dates.map((d) => (
              <option key={d} value={d}>{fmtDate(d)}</option>
            ))}
            {dates.length === 0 && <option value="">暂无数据</option>}
          </select>
          <button
            type="button"
            className="btn-secondary text-xs px-3 py-1.5"
            onClick={() => void load(selectedDate || undefined)}
          >
            刷新
          </button>
        </div>
      </div>

      {error && <ApiErrorAlert error={error} />}

      {/* 概览卡片 */}
      {pool && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard label="种子池" value={pool.count} sub={`${pool.date} 生成`} highlight />
          <StatCard label="Phase2 已触发" value={pool.triggered_count} sub="盘中买入信号" />
          {Object.entries(modelCounts).map(([model, cnt]) => (
            <StatCard key={model} label={MODEL_LABEL[model] ?? model} value={cnt} sub="只候选股" />
          ))}
        </div>
      )}

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b border-default pb-0">
        {([['all', `全部候选 (${entries.length})`], ['triggered', `已触发 (${triggered.length})`]] as [Tab, string][]).map(
          ([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors -mb-px ${
                tab === key
                  ? 'border-cyan text-cyan'
                  : 'border-transparent text-muted hover:text-secondary'
              }`}
            >
              {label}
            </button>
          )
        )}
      </div>

      {/* 表格 */}
      <div className="terminal-card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="w-8 h-8 border-2 border-cyan/20 border-t-cyan rounded-full animate-spin" />
          </div>
        ) : displayed.length === 0 ? (
          <p className="text-center text-muted text-sm py-16">
            {tab === 'triggered' ? '今日暂无 Phase2 触发信号' : '暂无数据 — 等待今日 Phase1 运行'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-default text-muted text-xs uppercase tracking-wider">
                  {['代码', '名称', '模型', 'Phase1 得分', '通过指标', 'Phase2 状态', '筛选时间', ''].map((h) => (
                    <th key={h} className="text-left py-2 px-3 font-normal whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {displayed.map((entry) => (
                  <EntryRow key={entry.code} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 定时任务说明 */}
      <div className="terminal-card p-4 text-xs text-muted space-y-1">
        <p className="text-secondary font-medium mb-2">定时任务</p>
        <p>🕘 <span className="font-mono text-cyan">09:00</span> 北京时间 · Phase1 全市场扫描 → 生成种子池</p>
        <p>🕙 <span className="font-mono text-cyan">09:30</span> 北京时间 · Phase2 盘中监控启动 → 实时更新触发状态</p>
        <p>🔄 本页面每 15 秒自动拉取最新数据，种子池有更新会自动显示</p>
      </div>
    </div>
  );
};

export default ScreeningPage;
