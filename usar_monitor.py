# -*- coding: utf-8 -*-
"""
usar_monitor.py — USAR 论文模式 (thesis-mode) 监控逻辑模块。

设计本质(一句话还原):
    "T3 先决,T1/T2/T4 独立。"

    * T3 = 前置条件 / 一票否决 → 早期 return,失败即 EXIT,绝不进入后续判断。
    * T1/T2/T4 = 三个独立布尔触发器 → 各报各的状态,不求和、不加权、不进总分。

显式禁止:把这 4 个触发器压成 sum(score × weight)。本模块用早期 return +
独立触发器实现,不存在任何打分/加权聚合。

数据治理:
    * 基本面触发器 T1/T2/T4/T3 状态从 usar_config.json 读取(人工维护),
      本模块绝不自动改写状态。
    * 实时价/成交量从 usar_live.json 读取(由 web_search 双源交叉验证后人工填入),
      非秒级实时;数据矛盾立即上报,不静默 fallback。

文件隔离:本模块只依赖 usar_config.json / usar_live.json,与 v3/v4 引擎、
portfolio*.json、stock_industry_map.json 完全解耦,不读取它们。
"""

import json
import os

CONFIG_FILE = "usar_config.json"
LIVE_FILE = "usar_live.json"


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _here(filename):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def load_config(path=None):
    with open(path or _here(CONFIG_FILE), "r", encoding="utf-8") as f:
        return json.load(f)


def load_live(path=None):
    with open(path or _here(LIVE_FILE), "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 漏斗逻辑 1:论文裁决 —— T3 先决 + T1/T2/T4 独立触发器
# --------------------------------------------------------------------------- #
def evaluate_thesis(cfg, live=None):
    """
    返回论文裁决:HOLD / REVIEW / EXIT。

    结构(死命令第0条):
        1. 第0道闸:T3 前置条件。failed → 早期 return EXIT,绝不继续。
        2. T1/T2/T4 独立触发器:逐个独立报状态,不求和不加权。
           任一 failed → REVIEW(论文破裂复核)。
        3. 全部通过 → HOLD。

    `live` 仅为签名保留(裁决只依赖人工维护的基本面状态,不依赖盘面价格)。
    """
    ft = cfg["fundamental_triggers"]

    # ---- 第0道闸:前置条件 / 一票否决,早期 return ----
    if ft["T3_precondition"]["status"] == "failed":
        return {
            "verdict": "EXIT",
            "color": "red",
            "reason": "T3 前置失败 — 商务部资金",
            "exit": True,
            "precondition": "failed",
            "triggers": None,  # 前置失败,独立触发器不再评估
        }

    # ---- T1/T2/T4 独立触发器:独立布尔,不进总分 ----
    indep = {k: ft[k]["status"] for k in ("T1", "T2", "T4")}
    failed = [k for k, v in indep.items() if v == "failed"]
    if failed:
        return {
            "verdict": "REVIEW",
            "color": "amber",
            "reason": f"{failed} 长期失败 — 论文破裂复核",
            "exit": False,
            "precondition": "passed",
            "triggers": indep,
        }

    return {
        "verdict": "HOLD",
        "color": "green",
        "reason": "T3 前置通过;T1/T2/T4 独立触发器全部正常。",
        "exit": False,
        "precondition": "passed",
        "triggers": indep,
    }


# --------------------------------------------------------------------------- #
# 漏斗逻辑 2:进场建议 —— 离散触发位(不是打分)
# --------------------------------------------------------------------------- #
def evaluate_entry(cfg, price, volume_surge):
    """
    返回当前应执行的动作(离散触发位,非评分聚合):
        STOP     — 放量跌破否决位,不接飞刀(优先级最高)
        BUY      — 命中某一批次,给出限价与配比
        BREAKOUT — 放量突破,考虑追储备
        WAIT     — 价在空中,R/R 差,不接

    批次按 trigger_max 升序判定:价格越低命中越深的批次(B2 深于 B1),
    保证两个独立批次都可达。
    """
    # 1) 放量跌破否决位 → STOP(优先于一切批次触发)
    if volume_surge and price < cfg["add_veto"]["price"]:
        return {
            "action": "STOP",
            "color": "red",
            "reason": f"放量跌破 {cfg['add_veto']['price']} — 不接飞刀",
        }

    # 2) 命中批次触发位(升序:先判最深的 B2,再判 B1)
    for tier in sorted(cfg["entry_tiers"], key=lambda t: t["trigger_max"]):
        if price <= tier["trigger_max"]:
            return {
                "action": "BUY",
                "color": "green",
                "tier": tier["id"],
                "tier_label": tier.get("label", tier["id"]),
                "limit": tier["limit"],
                "alloc": tier["alloc_pct"],
                "reason": f"命中 {tier.get('label', tier['id'])} (≤{tier['trigger_max']}) — 限价 {tier['limit']},配比 {tier['alloc_pct']}%",
            }

    # 3) 放量突破阻力 → 考虑追储备
    if price >= cfg["breakout"]["price"] and volume_surge:
        return {
            "action": "BREAKOUT",
            "color": "blue",
            "note": "考虑储备 20% 追突破",
            "reason": f"放量突破 {cfg['breakout']['price']} — 考虑动用储备追突破",
        }

    # 4) 价在空中
    return {
        "action": "WAIT",
        "color": "amber",
        "reason": "价在空中 (18.6 ~ 24.2),R/R 差,不接",
    }


# --------------------------------------------------------------------------- #
# 数据矛盾检测(不静默 fallback)
# --------------------------------------------------------------------------- #
def detect_data_conflict(cfg, live):
    """
    比对 live.sources 的价格,差异超过容差则报红(数据矛盾)。
    返回 {"conflict": bool, "detail": str}。
    """
    tol_pct = cfg.get("data_policy", {}).get("conflict_tolerance_pct", 1.0)
    prices = [s["price"] for s in live.get("sources", []) if s.get("price") is not None]
    if len(prices) < 2:
        return {
            "conflict": True,
            "detail": f"仅 {len(prices)} 个有效价源,无法双源交叉验证 — 数据可信度不足。",
        }
    lo, hi = min(prices), max(prices)
    spread_pct = (hi - lo) / lo * 100 if lo else 0.0
    if spread_pct > tol_pct:
        return {
            "conflict": True,
            "detail": f"双源价差 {hi - lo:.2f} ({spread_pct:.2f}%) > 容差 {tol_pct}% — 数据矛盾,请人工核对。",
        }
    return {
        "conflict": False,
        "detail": f"双源价差 {hi - lo:.2f} ({spread_pct:.2f}%) ≤ 容差 {tol_pct}%。",
    }


# --------------------------------------------------------------------------- #
# 卡片状态装配
# --------------------------------------------------------------------------- #
def build_card_state(cfg=None, live=None):
    """组合论文裁决 + 进场建议 + 数据状态,供模板渲染。"""
    cfg = cfg if cfg is not None else load_config()
    live = live if live is not None else load_live()

    price = live.get("last_price")
    volume_surge = bool(live.get("volume_surge", False))

    thesis = evaluate_thesis(cfg, live)
    entry = evaluate_entry(cfg, price, volume_surge)
    data = detect_data_conflict(cfg, live)

    return {
        "meta": cfg["meta"],
        "thesis": thesis,
        "entry": entry,
        "price": price,
        "volume": live.get("volume"),
        "volume_surge": volume_surge,
        "day_high": live.get("day_high"),
        "day_low": live.get("day_low"),
        "key_levels": cfg["key_levels"],
        "position_cap": cfg["position_cap"],
        "fundamental_triggers": cfg["fundamental_triggers"],
        "data": {
            "date": live.get("data_date"),
            "captured_at_utc": live.get("captured_at_utc"),
            "market_status": live.get("market_status"),
            "market_note": live.get("market_note"),
            "realtime": cfg.get("data_policy", {}).get("realtime", False),
            "disclaimer": cfg.get("data_policy", {}).get("disclaimer", ""),
            "sources": live.get("sources", []),
            "conflict": data["conflict"],
            "conflict_detail": data["detail"],
        },
    }


# --------------------------------------------------------------------------- #
# 自检 / CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import pprint

    state = build_card_state()
    print("=== USAR thesis-mode self-check ===")
    print("一句话还原:T3 先决,T1/T2/T4 独立。")
    pprint.pprint(state["thesis"])
    pprint.pprint(state["entry"])
    pprint.pprint(state["data"]["conflict_detail"])
