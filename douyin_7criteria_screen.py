#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_7criteria_screen.py — 独立筛选脚本(不依赖、不修改 v3/v4 任何文件)

对 6 只股票按"漏斗 + 逐条一票否决(early-return)"做硬核校验,绝不用加权打分。
数据全部来自 akshare(东方财富/同花顺等真实数据源),按字段打印数据日期。
缺数据 fail-fast:标 DATA_MISSING,绝不静默 fallback、绝不用替代数据冒充。

判定口径(硬否决 = hard veto):
  C1 资产负债率(2026Q1 资产负债表)         : >=50%  → VETO
  C2 经营活动现金流净额(2026Q1)            : <=0    → VETO
  C3 第一大股东(+一致行动人)持股比例        : <30%   → VETO（见下方一致行动人说明）
  C4 控股股东股权质押                        : 存在任何控股股东质押 → VETO
  C5 机构持股% + 股东户数环比(散户占比代理) : 信息项,无否决阈值 → 仅展示,不否决

漏斗:C1→C2→C3→C4 顺序评估,任一 VETO 立即 early-return(其余记 SKIPPED);
      任一 DATA_MISSING 亦 fail-fast 立即停(其余记 SKIPPED),final=DATA_MISSING;
      C1~C4 全 PASS 才评估 C5 并给 final=PASS。

用法: python douyin_7criteria_screen.py
"""

import sys
import datetime as dt

try:
    import akshare as ak
    import pandas as pd
except Exception as e:  # 依赖缺失 = fail-fast,不继续
    print(f"FATAL: 依赖导入失败({type(e).__name__}: {e})。请先 pip install akshare pandas。")
    sys.exit(2)

STOCKS = ["300476", "300308", "001309", "301308", "688012", "300502"]
REPORT_Q = "2026Q1"
REPORT_DATE = "20260331"          # 2026 一季报报告期
LIAB_VETO = 50.0                  # 资产负债率否决线(%)
TOP1_FLOOR = 30.0                 # 第一大股东持股下限(%)

PASS, VETO, MISS, SKIP, INFO = "PASS", "VETO", "DATA_MISSING", "SKIPPED", "INFO"


# ---------- 工具 ----------
def ex_prefix(code: str) -> str:
    """交易所前缀。68x/60x/689→SH,其余(0/3/001/301)→SZ。"""
    if code.startswith(("60", "68", "689", "900")):
        return "SH"
    return "SZ"


def pick_col(df: "pd.DataFrame", *cands) -> str:
    """按候选子串定位列名,找不到则抛错(→ 上层转 DATA_MISSING),绝不猜错列。"""
    for sub in cands:
        for c in df.columns:
            if sub in str(c):
                return c
    raise KeyError(f"未找到含 {cands} 的列;实际列={list(df.columns)}")


class Result:
    __slots__ = ("status", "value", "data_date", "note")

    def __init__(self, status, value="", data_date="", note=""):
        self.status = status
        self.value = value          # 展示用的关键数值
        self.data_date = data_date  # 该字段的数据日期
        self.note = note            # 说明/根因


def miss(reason, data_date=""):
    return Result(MISS, value="", data_date=data_date, note=reason)


# ---------- C1 资产负债率 ----------
def c1_liability(code: str) -> Result:
    sym = ex_prefix(code) + code
    try:
        df = ak.stock_balance_sheet_by_report_em(symbol=sym)
    except Exception as e:
        return miss(f"资产负债表拉取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return miss("资产负债表为空")
    dcol = pick_col(df, "REPORT_DATE", "报告期")
    acol = pick_col(df, "TOTAL_ASSETS", "资产总计")
    lcol = pick_col(df, "TOTAL_LIABILITIES", "负债合计")
    row = df[df[dcol].astype(str).str.startswith("2026-03-31")]
    if row.empty:
        return miss(f"无 2026-03-31 报告期记录(现有最新={str(df[dcol].iloc[0])})")
    r = row.iloc[0]
    try:
        ta, tl = float(r[acol]), float(r[lcol])
    except Exception:
        return miss("资产/负债字段非数值")
    if ta <= 0:
        return miss("资产总计<=0,无法计算")
    ratio = tl / ta * 100.0
    ddate = str(r[dcol])[:10]
    val = f"资产负债率={ratio:.2f}%"
    if ratio >= LIAB_VETO:
        return Result(VETO, val, ddate, f">= {LIAB_VETO}% 否决")
    return Result(PASS, val, ddate, f"< {LIAB_VETO}%")


# ---------- C2 经营现金流 ----------
def c2_op_cashflow(code: str) -> Result:
    sym = ex_prefix(code) + code
    try:
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=sym)
    except Exception as e:
        return miss(f"现金流量表拉取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return miss("现金流量表为空")
    dcol = pick_col(df, "REPORT_DATE", "报告期")
    ocol = pick_col(df, "NETCASH_OPERATE", "经营活动产生的现金流量净额", "经营活动")
    row = df[df[dcol].astype(str).str.startswith("2026-03-31")]
    if row.empty:
        return miss(f"无 2026-03-31 报告期记录(现有最新={str(df[dcol].iloc[0])})")
    r = row.iloc[0]
    try:
        ocf = float(r[ocol])
    except Exception:
        return miss("经营现金流字段非数值")
    ddate = str(r[dcol])[:10]
    val = f"经营现金流净额={ocf/1e8:.2f}亿"
    if ocf <= 0:
        return Result(VETO, val, ddate, "<=0 否决")
    return Result(PASS, val, ddate, ">0")


# ---------- C3 第一大股东(+一致行动人) ----------
def c3_top_holder(code: str) -> Result:
    sym = ex_prefix(code).lower() + code
    try:
        df = ak.stock_gdfx_top_10_em(symbol=sym, date=REPORT_DATE)
    except Exception as e:
        return miss(f"十大股东拉取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return miss(f"十大股东为空(date={REPORT_DATE})")
    try:
        pcol = pick_col(df, "占总股本持股比例", "持股比例")
    except KeyError as e:
        return miss(str(e))
    try:
        top1 = float(str(df[pcol].iloc[0]).replace("%", ""))
    except Exception:
        return miss("第一大股东持股比例非数值")
    ddate = REPORT_DATE
    val = f"第一大股东持股={top1:.2f}%"
    # 一致行动人(concert parties)无法从十大股东表程序化聚合:
    #   - top1 >= 30%   → 已满足下限,加一致行动人只增不减 → PASS
    #   - top1 <  30%   → 需一致行动人数据才能判定,缺数据 → DATA_MISSING(不误否决、不编造)
    if top1 >= TOP1_FLOOR:
        return Result(PASS, val, ddate, f"第一大股东已 >= {TOP1_FLOOR}%(一致行动人只增不减)")
    return miss(
        f"第一大股东 {top1:.2f}% < {TOP1_FLOOR}%,且 akshare 无一致行动人聚合字段,"
        f"无法确认合计是否达标(拒绝误否决/编造)",
        ddate,
    )


# ---------- C4 控股股东质押 ----------
def _latest_gpzy_date() -> str:
    """取最近一期股权质押统计交易日。"""
    prof = ak.stock_gpzy_profile_em()
    dcol = pick_col(prof, "交易日期", "日期")
    return str(pd.to_datetime(prof[dcol]).max().date()).replace("-", "")


def c4_pledge(code: str) -> Result:
    try:
        gdate = _latest_gpzy_date()
    except Exception as e:
        return miss(f"质押统计日期获取失败: {type(e).__name__}: {e}")
    # 全市场质押比例快照,定位本股票
    try:
        df = ak.stock_gpzy_pledge_ratio_em(date=gdate)
    except Exception as e:
        return miss(f"质押比例拉取失败: {type(e).__name__}: {e}", gdate)
    if df is None or len(df) == 0:
        return miss(f"质押比例数据为空(date={gdate})", gdate)
    try:
        ccol = pick_col(df, "股票代码", "代码")
        rcol = pick_col(df, "质押比例")
    except KeyError as e:
        return miss(str(e), gdate)
    row = df[df[ccol].astype(str).str.zfill(6) == code]
    if row.empty:
        # 未出现在质押名单 → 无质押
        return Result(PASS, "质押比例=0%(未在质押名单)", gdate, "无质押")
    try:
        pr = float(str(row.iloc[0][rcol]).replace("%", ""))
    except Exception:
        return miss("质押比例非数值", gdate)
    if pr <= 0:
        return Result(PASS, f"质押比例={pr:.2f}%", gdate, "无质押")
    # 有质押 → 需确认是否为控股股东质押
    try:
        det = ak.stock_gpzy_pledge_ratio_detail_em(date=gdate)
        dccol = pick_col(det, "股票代码", "代码")
        sub = det[det[dccol].astype(str).str.zfill(6) == code]
        # 明细含"是否控股股东"则据此判定;否则无法区分 → DATA_MISSING
        flag_col = None
        for c in det.columns:
            if "控股" in str(c):
                flag_col = c
                break
        if flag_col is not None and not sub.empty:
            if sub[flag_col].astype(str).str.contains("是").any():
                return Result(VETO, f"质押比例={pr:.2f}%(含控股股东)", gdate, "控股股东质押→否决")
            return Result(PASS, f"质押比例={pr:.2f}%(非控股股东)", gdate, "无控股股东质押")
    except Exception:
        pass
    return miss(
        f"存在质押(整体{pr:.2f}%)但无法区分是否控股股东(明细缺'控股股东'标识),"
        f"拒绝臆断→请人工核查",
        gdate,
    )


# ---------- C5 机构持股% + 股东户数环比(信息项,不否决) ----------
def c5_info(code: str) -> Result:
    parts, ddate = [], ""
    # 股东户数环比
    try:
        df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if df is not None and len(df) >= 1:
            dcol = pick_col(df, "股东户数统计截止日", "截止日", "日期")
            ncol = pick_col(df, "股东户数-本次", "股东户数")
            df = df.sort_values(dcol, ascending=False)
            cur = float(df.iloc[0][ncol])
            ddate = str(df.iloc[0][dcol])[:10]
            if len(df) >= 2:
                prev = float(df.iloc[1][ncol])
                chg = (cur - prev) / prev * 100.0 if prev else float("nan")
                # 户数下降→筹码集中(散户占比下降),为正面信号
                parts.append(f"股东户数={int(cur)}({ddate}),环比={chg:+.2f}%")
            else:
                parts.append(f"股东户数={int(cur)}({ddate}),环比=未获取(无上期)")
        else:
            parts.append("股东户数=未获取")
    except Exception as e:
        parts.append(f"股东户数=未获取({type(e).__name__})")
    # 机构持股%(基金持股近似)
    try:
        q = f"{REPORT_DATE[:4]}{((int(REPORT_DATE[4:6]) - 1)//3)+1}"  # 2026Q1→20261
        fdf = ak.stock_report_fund_hold(symbol="基金持仓", date=REPORT_DATE)
        ccol = pick_col(fdf, "股票代码", "代码")
        sub = fdf[fdf[ccol].astype(str).str.zfill(6) == code]
        if not sub.empty:
            rcol = pick_col(fdf, "占总股本比例", "占流通股本比例", "持股比例")
            parts.append(f"基金持股比例={sub.iloc[0][rcol]}({REPORT_DATE})")
        else:
            parts.append(f"基金持股=未获取(名单无此股, {REPORT_DATE})")
    except Exception as e:
        parts.append(f"机构持股=未获取({type(e).__name__})")
    return Result(INFO, " | ".join(parts), ddate, "信息项,不参与否决")


# ---------- 漏斗 ----------
VETO_CHAIN = [
    ("C1_资产负债率", c1_liability),
    ("C2_经营现金流", c2_op_cashflow),
    ("C3_第一大股东", c3_top_holder),
    ("C4_控股股东质押", c4_pledge),
]
C5_NAME = "C5_机构&户数"


def screen(code: str):
    results = {}
    final = PASS
    stopped = False
    for name, fn in VETO_CHAIN:
        if stopped:
            results[name] = Result(SKIP)
            continue
        r = fn(code)
        results[name] = r
        if r.status == VETO:
            final, stopped = VETO, True
        elif r.status == MISS:
            final, stopped = MISS, True   # fail-fast:缺数据即停,不静默继续
    # 仅在 C1~C4 全 PASS 才跑 C5
    if not stopped:
        results[C5_NAME] = c5_info(code)
    else:
        results[C5_NAME] = Result(SKIP)
    return final, results


def main():
    print("=" * 100)
    print(f"抖音7条硬核筛选 | 报告期 {REPORT_Q}({REPORT_DATE}) | 运行时间 {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"否决线: 资产负债率>={LIAB_VETO}% | 经营现金流<=0 | 第一大股东<{TOP1_FLOOR}% | 控股股东任何质押")
    print("=" * 100)

    all_cols = [n for n, _ in VETO_CHAIN] + [C5_NAME]
    summary = []
    for code in STOCKS:
        final, res = screen(code)
        summary.append((code, final, res))
        print(f"\n【{code}】 最终: {final}")
        for name in all_cols:
            r = res[name]
            dd = f" | 数据日期: {r.data_date}" if r.data_date else ""
            extra = f" | {r.value}" if r.value else ""
            note = f" | {r.note}" if r.note else ""
            print(f"  {name:<16} {r.status:<12}{extra}{dd}{note}")

    # 判定表
    print("\n" + "=" * 100)
    print("判定表")
    print("=" * 100)
    header = f"{'股票':<8}" + "".join(f"{n.split('_')[0]:<14}" for n in all_cols) + f"{'FINAL':<12}"
    print(header)
    print("-" * len(header))
    for code, final, res in summary:
        line = f"{code:<8}" + "".join(f"{res[n].status:<14}" for n in all_cols) + f"{final:<12}"
        print(line)

    # 退出码:任何 DATA_MISSING → 2(fail-fast 显式信号);全部判定完成 → 0
    if any(f == MISS for _, f, _ in summary):
        print("\n注意: 存在 DATA_MISSING(数据缺口),已 fail-fast 标注,未做任何替代填充。")
        sys.exit(2)


if __name__ == "__main__":
    main()
