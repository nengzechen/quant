#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史大盘复盘补录脚本（yfinance 版）
用途：为指定日期范围补生成 market_review_YYYYMMDD.md 报告
使用：python patch/gen_historical_reviews.py --start 20260302 --end 20260306
"""

import argparse
import logging
import os
import sys
from typing import Optional, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# Yahoo Finance ticker → (代码, 名称)
TICKERS = {
    '000001.SS': ('000001', '上证指数'),
    '399001.SZ': ('399001', '深证成指'),
    '000300.SS': ('000300', '沪深300'),
}


def fetch_all_indices(start_date: str, end_date: str) -> Dict[str, List[dict]]:
    """
    用 yfinance 拉历史日线数据
    start_date / end_date: YYYYMMDD
    返回: {date_str(YYYY-MM-DD): [{name, code, open, close, high, low, vol, chg_pct, chg}, ...]}
    """
    import yfinance as yf
    from datetime import datetime, timedelta

    sd = datetime.strptime(start_date, '%Y%m%d')
    ed = datetime.strptime(end_date,   '%Y%m%d') + timedelta(days=1)
    # 多拉一天前的数据用于计算涨跌幅
    fetch_start = (sd - timedelta(days=5)).strftime('%Y-%m-%d')
    fetch_end   = ed.strftime('%Y-%m-%d')

    tickers_list = list(TICKERS.keys())
    data = yf.download(tickers_list, start=fetch_start, end=fetch_end, progress=False)

    all_data: Dict[str, List[dict]] = {}

    for ticker, (code, name) in TICKERS.items():
        try:
            close_series  = data['Close'][ticker].dropna()
            open_series   = data['Open'][ticker].dropna()
            high_series   = data['High'][ticker].dropna()
            low_series    = data['Low'][ticker].dropna()
            vol_series    = data['Volume'][ticker].dropna()
            pct_series    = close_series.pct_change() * 100

            for date_idx in close_series.index:
                date_str = date_idx.strftime('%Y-%m-%d')
                # 只保留目标范围内的日期
                if not (sd.strftime('%Y-%m-%d') <= date_str <= ed.strftime('%Y-%m-%d')):
                    continue
                close_val = float(close_series[date_idx])
                open_val  = float(open_series.get(date_idx, close_val))
                high_val  = float(high_series.get(date_idx, close_val))
                low_val   = float(low_series.get(date_idx, close_val))
                vol_val   = float(vol_series.get(date_idx, 0))
                pct_val   = float(pct_series.get(date_idx, 0))
                prev_close = close_val / (1 + pct_val / 100) if pct_val != 0 else close_val
                chg_val   = close_val - prev_close

                amplitude = (high_val - low_val) / prev_close * 100 if prev_close > 0 else 0

                entry = {
                    'code': code, 'name': name,
                    'open': open_val, 'close': close_val,
                    'high': high_val, 'low': low_val,
                    'vol': vol_val, 'chg_pct': pct_val,
                    'chg': chg_val, 'amplitude': amplitude,
                }
                all_data.setdefault(date_str, []).append(entry)
        except Exception as e:
            logger.warning(f"[{name}] 处理数据失败: {e}")

    return all_data


def build_prompt(date_str: str, indices: List[dict]) -> str:
    date_fmt = f"{date_str[:4]}年{date_str[5:7]}月{date_str[8:10]}日"
    lines = []
    for idx in indices:
        icon = '🟢' if idx['chg_pct'] > 0 else ('🔴' if idx['chg_pct'] < 0 else '⚪')
        lines.append(
            f"- {idx['name']}({idx['code']}): 收盘 {idx['close']:.2f}，"
            f"涨跌幅 {icon} {idx['chg_pct']:+.2f}%（涨跌额 {idx['chg']:+.2f}），"
            f"开盘 {idx['open']:.2f}，最高 {idx['high']:.2f}，最低 {idx['low']:.2f}，"
            f"振幅 {idx['amplitude']:.2f}%"
        )
    index_block = '\n'.join(lines)

    return f"""你是一名专业的A股量化分析师，请根据以下{date_fmt}的真实行情数据，生成一份结构化的大盘复盘报告。

## {date_fmt} 指数行情
{index_block}

## 输出要求（严格遵守 Markdown 格式）

### 一、市场总结
（2-3句：今日整体涨跌方向、市场情绪）

### 二、指数点评
（逐一分析各指数强弱、涨跌原因推测）

| 指数 | 最新 | 涨跌幅 | 开盘 | 最高 | 最低 | 振幅 |
|------|------|--------|------|------|------|------|
（根据数据填入，保留2位小数）

### 三、资金动向
（根据成交量判断当日资金活跃度和情绪）

### 四、热点解读
（基于指数强弱推测热点方向，需注明"推测"）

### 五、后市展望
（结合当日走势，给出下一交易日展望，1-2句）

---
⚠️ 注意：仅基于提供数据分析，未提供的数据（北向资金、个股涨跌家数等）请勿编造。
"""


def generate_with_llm(date_str: str, indices: List[dict]) -> Optional[str]:
    from src.analyzer import GeminiAnalyzer
    analyzer = GeminiAnalyzer()  # 自动从 config 读取 API Key
    if not analyzer.is_available():
        logger.warning("LLM 不可用")
        return None
    prompt = build_prompt(date_str, indices)
    gen_cfg = {'temperature': 0.7, 'max_output_tokens': 2048}
    try:
        if analyzer._use_openai:
            return analyzer._call_openai_api(prompt, gen_cfg)
        else:
            resp = analyzer._model.generate_content(prompt, generation_config=gen_cfg)
            return resp.text.strip() if resp and resp.text else None
    except Exception as e:
        logger.error(f"[{date_str}] LLM 生成失败: {e}")
        return None


def save_report(date_str: str, content: str, reports_dir: str) -> str:
    date_nodash = date_str.replace('-', '')
    filepath = os.path.join(reports_dir, f"market_review_{date_nodash}.md")
    header = f"# 🎯 大盘复盘\n\n## {date_str} 大盘复盘\n\n"
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(header + content)
    logger.info(f"报告已保存: {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description='历史大盘复盘补录')
    parser.add_argument('--start', default='20260302', help='开始日期 YYYYMMDD')
    parser.add_argument('--end',   default='20260306', help='结束日期 YYYYMMDD')
    parser.add_argument('--reports-dir', default=os.path.join(ROOT, 'reports'))
    parser.add_argument('--overwrite', action='store_true', help='覆盖已有报告')
    args = parser.parse_args()

    os.makedirs(args.reports_dir, exist_ok=True)
    logger.info(f"补录 {args.start} ~ {args.end}")

    all_data = fetch_all_indices(args.start, args.end)
    if not all_data:
        logger.error("未获取到数据")
        return

    dates = sorted(all_data.keys())
    logger.info(f"共 {len(dates)} 个交易日: {dates}")

    generated = 0
    for date_str in dates:
        date_nodash = date_str.replace('-', '')
        report_path = os.path.join(args.reports_dir, f"market_review_{date_nodash}.md")
        if not args.overwrite and os.path.exists(report_path):
            logger.info(f"[{date_str}] 已存在，跳过")
            continue

        indices = all_data[date_str]
        logger.info(f"[{date_str}] 生成报告（{len(indices)} 个指数）...")
        review = generate_with_llm(date_str, indices)
        if not review:
            continue
        save_report(date_str, review, args.reports_dir)
        generated += 1

    logger.info(f"完成！共生成 {generated} 份报告")


if __name__ == '__main__':
    main()
