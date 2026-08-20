# -*- coding: utf-8 -*-
"""
数据源可达性探针：分别测 akshare(东财/新浪)、tushare、baostock 能否取到
「全量股票代码」和「单只日线」，输出耗时与错误。

主要用途：诊断 GitHub Actions 海外 runner 上哪些数据源可用。

用法：python scripts/probe_datasources.py
"""

import os
import time
import traceback

SAMPLE = "600519"


def timed(name, fn):
    t0 = time.time()
    try:
        result = fn()
        print(f"[OK]   {name:<34} {time.time() - t0:6.1f}s  {result}")
        return True
    except Exception as e:
        print(f"[FAIL] {name:<34} {time.time() - t0:6.1f}s  {type(e).__name__}: {e}")
        if os.environ.get("PROBE_VERBOSE"):
            traceback.print_exc()
        return False


def probe_akshare_codes():
    import akshare as ak
    df = ak.stock_info_a_code_name()
    return f"{len(df)} 只"


def probe_akshare_daily():
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=f"sh{SAMPLE}", adjust="qfq")
    return f"{len(df)} 根 K 线"


def probe_akshare_snapshot():
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    return f"{len(df)} 只快照"


def probe_tushare_codes():
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN 未设置")
    pro = ts.pro_api(token)
    df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,market")
    return f"{len(df)} 只"


def probe_tushare_daily():
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN 未设置")
    pro = ts.pro_api(token)
    df = pro.daily(ts_code=f"{SAMPLE}.SH", limit=100)
    return f"{len(df)} 根 K 线"


def probe_baostock_codes():
    import baostock as bs
    from datetime import date, timedelta
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"login {lg.error_code} {lg.error_msg}")
    try:
        # 往前找最多 10 天，跳过非交易日
        for back in range(10):
            day = (date.today() - timedelta(days=back)).strftime("%Y-%m-%d")
            rs = bs.query_all_stock(day=day)
            if rs.error_code != "0":
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                return f"{len(rows)} 只（{day}）"
        raise RuntimeError("最近 10 天都取不到全量列表")
    finally:
        bs.logout()


def probe_baostock_daily():
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"login {lg.error_code} {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            f"sh.{SAMPLE}", "date,open,high,low,close,volume",
            frequency="d", adjustflag="2",
        )
        if rs.error_code != "0":
            raise RuntimeError(f"query {rs.error_code} {rs.error_msg}")
        n = 0
        while rs.next():
            rs.get_row_data()
            n += 1
        return f"{n} 根 K 线"
    finally:
        bs.logout()


PROBES = [
    ("akshare  全量代码 (东财)", probe_akshare_codes),
    ("akshare  日线 (新浪)", probe_akshare_daily),
    ("akshare  全市场快照 (东财)", probe_akshare_snapshot),
    ("tushare  全量代码", probe_tushare_codes),
    ("tushare  日线", probe_tushare_daily),
    ("baostock 全量代码", probe_baostock_codes),
    ("baostock 日线", probe_baostock_daily),
]


def main():
    print(f"探针开始（样本股 {SAMPLE}）\n" + "-" * 72)
    results = {name: timed(name, fn) for name, fn in PROBES}
    print("-" * 72)
    ok = [n for n, v in results.items() if v]
    print(f"可用 {len(ok)}/{len(results)}：{', '.join(ok) or '无'}")


if __name__ == "__main__":
    main()
