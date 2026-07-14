"""
板块推理器 V2 (对应 PRD Phase 2 / M2 板块筛选器 / 8.3)

V2 架构变更:
  - 独立并行: 不再依赖三路分析师(新闻/情绪/政策)的 hot_topics/policy_events,
    与三路分析师同时从 START 运行, 自行获取数据。
  - 纯量化赋分: 从 LLM 四维评分改为算法五维赋分, 更快更稳定。
  - 搜索指数: 基于资金面(板块资金流) + 注意力(Bing搜索)构建。
  - 五维指标: 热度 / 扩散力 / 动摇度 / 回补力 / 拥挤度
  - 环比: 历史分数持久化, 支持日环比计算
  - 拥挤度地图: 输出 Top 10 板块拥挤度地图, 5 级颜色梯度映射

五维评分模型 (V2):
  综合评分 = 热度*0.25 + 扩散力*0.20 + 动摇度*0.15 + 回补力*0.20 + 拥挤度*0.20

图节点接口: create_sector_inference(llm, config) → sector_inference_node(state) -> dict
返回: {"ranked_sectors": [...], "sector_crowding_map": {...}}
"""
import json
import os
import time
from loguru import logger

from stock_agent.dataflows import interface as data_interface
from stock_agent.agents.utils.log_utils import log_stage


# =====================================================================
# 历史分数持久化路径
# =====================================================================
_SCORES_FILE = os.path.join(
    os.getenv("RESULTS_DIR", "./results"), "sector_scores_history.json"
)


def create_sector_inference(llm=None, config=None):
    """创建板块推理图节点 (V2: 独立并行, 五维量化赋分)

    Args:
        llm: LLM 实例 (V2 不再使用, 保留参数向后兼容)
        config: 系统配置 (读取 sector_metric_weights / sector_top_n)

    Returns:
        sector_inference_node(state) -> dict: 更新 ranked_sectors + sector_crowding_map
    """
    _config = config or {}
    _weights = _config.get("sector_metric_weights", {
        "heat": 0.25, "diffusion": 0.20, "volatility": 0.15,
        "rebound": 0.20, "crowding": 0.20,
    })
    _crowding_top_n = _config.get("sector_crowding_top_n", 10)

    def sector_inference_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[板块推理] V2 开始 (独立并行, 五维量化赋分)")

        trade_date = state.get("trade_date", "")
        top_n = _config.get("sector_top_n", 5)

        # === 1. 获取资金面数据 (板块行情+资金流) ===
        with log_stage("获取板块数据", "板块推理") as stage:
            sector_list = _fetch_sector_data_raw(trade_date)
            if not sector_list:
                logger.warning("[板块推理] 无板块数据, 降级到历史均值")
                sector_list = _get_historical_sectors(trade_date)
            stage.result = f"{len(sector_list)} 个板块"

        # === 2. 获取注意力数据 (Bing 搜索各板块提及量) ===
        with log_stage("获取注意力(Bing搜索)", "板块推理") as stage:
            attention_map = _fetch_attention_scores(sector_list)
            stage.result = f"{len(attention_map)}/{len(sector_list)} 个板块有提及"

        # === 3. 五维赋分 ===
        with log_stage("五维赋分", "板块推理") as stage:
            scored = []
            for sector in sector_list:
                try:
                    s = _score_sector(sector, attention_map)
                    if s:
                        scored.append(s)
                except Exception as e:
                    logger.debug(f"[板块推理] 赋分失败 ({sector}): {e}")
            stage.result = f"{len(scored)}/{len(sector_list)} 个板块赋分成功"

        if not scored:
            logger.warning("[板块推理] 全部赋分失败, 返回空")
            return {"ranked_sectors": [], "sector_crowding_map": {}}

        # === 4. 加权总分 ===
        with log_stage("加权总分", "板块推理"):
            for s in scored:
                s["total_score"] = round(
                    _weights["heat"] * s["heat"]
                    + _weights["diffusion"] * s["diffusion"]
                    + _weights["volatility"] * s["volatility"]
                    + _weights["rebound"] * s["rebound"]
                    + _weights["crowding"] * s["crowding"],
                    2,
                )

        # === 5. 环比 (对比上次运行) ===
        with log_stage("环比计算", "板块推理") as stage:
            prev_scores = _load_prev_scores(trade_date)
            for s in scored:
                prev = prev_scores.get(s["sector"], {})
                s["mom_change"] = _calc_mom(s["total_score"], prev.get("total_score"))
            stage.result = f"历史记录 {len(prev_scores)} 个板块"

        # 保存当前分数 (供下次环比)
        _save_scores(scored, trade_date)

        # === 6. 排序 → Top N (拥挤度地图取 Top 10) ===
        with log_stage("排序+拥挤度地图", "板块推理") as stage:
            scored.sort(key=lambda x: x["total_score"], reverse=True)
            top_10 = scored[:_crowding_top_n]
            crowding_map = _build_crowding_map(top_10, trade_date)
            stage.result = f"Top {len(top_10)} 地图已生成"

        # === 7. 输出 ranked_sectors (向后兼容 Stock Selector / Dark Horse Scanner) ===
        ranked = [_to_ranked_sector(s) for s in scored[:top_n]]

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[板块推理] 全部完成, 总耗时 {node_elapsed:.2f}s, Top {min(top_n, len(ranked))}:")
        for i, s in enumerate(ranked, 1):
            logger.info(
                f"  {i}. {s.get('sector','?'):<8s} 总分={s.get('heat_score','?'):<6} "
                f"拥挤={s.get('metrics',{}).get('crowding','?'):<6} "
                f"环比={s.get('mom_change','N/A')}"
            )

        return {"ranked_sectors": ranked, "sector_crowding_map": crowding_map}

    return sector_inference_node


# =====================================================================
# 资金面数据获取
# =====================================================================
def _fetch_sector_data_raw(trade_date: str) -> list:
    """获取板块行情数据 (资金面, 三级 fallback)

    Args:
        trade_date: 当前交易日 (历史兜底时用于排除当日记录)

    Returns:
        板块数据列表 [{板块名称, 涨跌幅, 主力净流入, 换手率, 上涨家数, 下跌家数, 领涨股}]
    """
    try:
        result = data_interface.get_sector_data()
        if result and result.get("data"):
            data = result["data"]
            if isinstance(data, list) and data and isinstance(data[0], dict):
                keys = set(data[0].keys())
                # 真实板块数据特征 (含行业名称)
                if "板块名称" in keys or "industry" in keys or "sector" in keys:
                    logger.debug(f"[板块推理] 获取到真实板块数据: {len(data)} 个")
                    return data
                # 指数数据 → 补充历史板块
                logger.info("[板块推理] 数据为指数格式, 补充历史板块列表")
                return data + _get_historical_sectors(trade_date)
    except Exception as e:
        logger.warning(f"[板块推理] 获取板块数据失败: {e}")
    return _get_historical_sectors(trade_date)


def _get_historical_sectors(trade_date: str) -> list:
    """历史均值兜底板块数据 (替代假数据 _get_static_sectors)

    数据源失败时, 从 results/sector_scores_history.json 取上一次成功
    运行的真实板块名 + 原始量化字段, 避免注入编造的涨跌幅/资金流。

    无历史(首次运行)返回 [], 下游降级为黑马-only (PRD 10.2)。

    Args:
        trade_date: 当前交易日 (排除当日记录, 取最近一次历史)

    Returns:
        板块数据列表 (中文键, 兼容 _score_sector); 无历史时返回 []
    """
    prev = _load_prev_scores(trade_date)
    if not prev:
        logger.warning(
            "[板块推理] 无实时板块数据且无历史记录, 板块推理返回空 "
            "(下游降级为黑马-only, PRD 10.2)"
        )
        return []

    # 英文键(历史) → 中文键(_score_sector 读取)
    # 关键: turnover→换手率, up_count→上涨家数, down_count→下跌家数
    # _score_sector 第256行只接受 换手率/turnover_rate(非turnover)
    # _score_sector 第257-258行只接受中文键 上涨家数/下跌家数(无英文fallback)
    sectors = []
    for name, sc in prev.items():
        if not isinstance(sc, dict) or not name:
            continue
        sectors.append({
            "板块名称": name,
            "涨跌幅": sc.get("change_pct", 0),
            "主力净流入": sc.get("fund_flow", 0),
            "换手率": sc.get("turnover", 0),
            "上涨家数": sc.get("up_count", 0),
            "下跌家数": sc.get("down_count", 0),
            "领涨股": sc.get("components", []),
        })
    logger.info(
        f"[板块推理] 实时数据源失败, 使用历史均值兜底: {len(sectors)} 个真实板块 "
        f"(来源 sector_scores_history.json)"
    )
    return sectors


# =====================================================================
# 注意力数据获取 (Bing 搜索)
# =====================================================================
def _fetch_attention_scores(sector_list: list) -> dict:
    """通过 Bing 搜索获取各板块注意力分数

    策略: 批量搜索"A股 热门板块 今日", 统计各板块名在搜索结果中的出现次数。
    一次搜索 + 关键词匹配, 避免逐板块搜索 (太慢)。

    Args:
        sector_list: 板块数据列表

    Returns:
        {板块名: 注意力分数(0-100)}
    """
    try:
        from stock_agent.tools.web_search import _bing_search

        results = _bing_search("A股 热门板块 今日 资金流入", max_results=20, timeout=15)
        if not results:
            logger.debug("[板块推理] Bing 搜索无结果, 注意力分数全为 0")
            return {}

        # 统计各板块名在标题+摘要中的出现次数
        attention = {}
        for sector in sector_list:
            name = sector.get("板块名称") or sector.get("sector") or ""
            if not name or len(name) < 2:
                continue
            count = sum(
                1 for r in results
                if name in r.get("title", "") or name in r.get("snippet", "")
            )
            # 归一化到 0-100 (每出现 1 次 = 5 分, 上限 100)
            attention[name] = min(count * 5, 100)

        return attention
    except Exception as e:
        logger.debug(f"[板块推理] 注意力获取失败: {e}")
        return {}


# =====================================================================
# 五维指标计算
# =====================================================================
def _score_sector(sector: dict, attention: dict) -> dict:
    """计算单板块五维指标 (0-100)

    输入 sector 字段 (AkShare 兼容):
      板块名称 / 涨跌幅 / 主力净流入 / 换手率 / 上涨家数 / 下跌家数 / 领涨股

    五维指标:
      1. 热度 (heat): 资金规模 + 注意度
      2. 扩散力 (diffusion): 上涨广度 + 注意度广度
      3. 动摇度 (volatility): 涨跌幅绝对值 + 换手率
      4. 回补力 (rebound): 涨幅正向 + 上涨占比
      5. 拥挤度 (crowding): 资金流入 + 注意度 (双高=拥挤)

    Args:
        sector: 板块数据 dict
        attention: 注意力分数 dict {板块名: 分数}

    Returns:
        含五维指标 + 原始数据的 dict
    """
    name = sector.get("板块名称") or sector.get("sector") or sector.get("name") or ""
    if not name:
        return None

    change_pct = _safe_float(sector.get("涨跌幅", sector.get("change_pct", 0)))
    fund_flow = _safe_float(sector.get("主力净流入", sector.get("fund_flow", 0)))
    turnover = _safe_float(sector.get("换手率", sector.get("turnover_rate", 0)))
    up_count = _safe_float(sector.get("上涨家数", 0))
    down_count = _safe_float(sector.get("下跌家数", 0))
    attn = attention.get(name, 0)  # 0-100

    # 1. 热度: 资金规模(60%) + 注意度(40%)
    # 资金流入归一化: 50亿为参考上限 (正流入越高热度越高)
    fund_norm = _normalize_positive(fund_flow, 50e8)
    heat = fund_norm * 60 + attn * 0.4

    # 2. 扩散力: 上涨家数占比(50%) + 注意度广度(50%)
    total_stocks = up_count + down_count
    up_ratio = up_count / total_stocks if total_stocks > 0 else 0.5
    diffusion = up_ratio * 50 + attn * 0.5

    # 3. 动摇度: 涨跌幅绝对值(60%上限) + 换手率(40%上限)
    volatility = min(abs(change_pct) * 15, 60) + min(turnover * 5, 40)

    # 4. 回补力: 涨幅正向(60%) + 上涨占比(40%)
    # 仅正涨幅贡献回补力, 负涨幅不贡献
    rebound = max(0, change_pct) * 10 + up_ratio * 40
    rebound = min(rebound, 100)

    # 5. 拥挤度: 资金流入(50%) + 注意度(50%)
    # 双高 = 拥挤 (资金涌入 + 关注度高 = 交易拥挤)
    fund_crowd = _normalize_positive(fund_flow, 30e8)
    crowding = fund_crowd * 50 + attn * 0.5

    # 提取成分股 (领涨股)
    components = sector.get("领涨股", sector.get("components", []))

    return {
        "sector": name,
        "heat": round(heat, 1),
        "diffusion": round(diffusion, 1),
        "volatility": round(volatility, 1),
        "rebound": round(rebound, 1),
        "crowding": round(crowding, 1),
        "fund_flow": fund_flow,
        "change_pct": change_pct,
        "turnover": turnover,
        "up_count": up_count,
        "down_count": down_count,
        "attention": attn,
        "components": components,
    }


# =====================================================================
# 拥挤度地图 (颜色梯度)
# =====================================================================
def _build_crowding_map(top_10: list, trade_date: str) -> dict:
    """构建 Top 10 板块拥挤度地图 (颜色梯度映射)

    颜色梯度:
      >= 80: 🔴 极度拥挤
      60-79: 🟠 拥扰
      40-59: 🟡 适中
      20-39: 🟢 宽松
      < 20:  🔵 极度宽松

    Args:
        top_10: Top 10 板块评分列表
        trade_date: 交易日期

    Returns:
        拥挤度地图 dict {top_10, map_text, trade_date}
    """
    def _color(score):
        if score >= 80:
            return {"color": "red", "icon": "🔴", "label": "极度拥挤"}
        if score >= 60:
            return {"color": "orange", "icon": "🟠", "label": "拥挤"}
        if score >= 40:
            return {"color": "yellow", "icon": "🟡", "label": "适中"}
        if score >= 20:
            return {"color": "green", "icon": "🟢", "label": "宽松"}
        return {"color": "blue", "icon": "🔵", "label": "极度宽松"}

    sectors = []
    for i, s in enumerate(top_10, 1):
        c = _color(s["crowding"])
        sectors.append({
            "rank": i,
            "sector": s["sector"],
            "crowding": s["crowding"],
            "total_score": s["total_score"],
            "mom_change": s.get("mom_change", "N/A"),
            "color": c["color"],
            "icon": c["icon"],
            "label": c["label"],
            "metrics": {
                "heat": s["heat"],
                "diffusion": s["diffusion"],
                "volatility": s["volatility"],
                "rebound": s["rebound"],
            },
        })

    # ASCII 地图 (日志输出)
    map_text = "\n".join(
        f"  {s['icon']} #{s['rank']:2d} {s['sector']:<8s} "
        f"拥挤={s['crowding']:5.1f} 总分={s['total_score']:5.1f} "
        f"环比={s.get('mom_change', 'N/A')}"
        for s in sectors
    )

    logger.info(f"[板块推理] 拥挤度地图 (Top {len(top_10)}):\n{map_text}")

    return {
        "top_10": sectors,
        "map_text": map_text,
        "trade_date": trade_date,
    }


# =====================================================================
# 环比 (历史分数持久化)
# =====================================================================
def _load_prev_scores(trade_date: str) -> dict:
    """加载上一次运行的板块分数 (用于环比)

    Args:
        trade_date: 当前交易日期

    Returns:
        {板块名: 分数dict} 或 {} (无历史数据时)
    """
    try:
        if os.path.exists(_SCORES_FILE):
            with open(_SCORES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 返回最近一次 (非当天) 的分数
            for entry in reversed(data.get("history", [])):
                if entry.get("trade_date") != trade_date:
                    scores = entry.get("scores", [])
                    return {s["sector"]: s for s in scores if "sector" in s}
    except Exception as e:
        logger.debug(f"[板块推理] 加载历史分数失败: {e}")
    return {}


def _save_scores(scored: list, trade_date: str):
    """保存当前板块分数 (供下次环比)

    保留最近 30 天的历史记录。

    Args:
        scored: 当前板块评分列表
        trade_date: 交易日期
    """
    try:
        os.makedirs(os.path.dirname(_SCORES_FILE), exist_ok=True)

        data = {"history": []}
        if os.path.exists(_SCORES_FILE):
            with open(_SCORES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

        # 避免同日重复写入 (覆盖当天记录)
        data["history"] = [
            e for e in data.get("history", [])
            if e.get("trade_date") != trade_date
        ]
        data["history"].append({"trade_date": trade_date, "scores": scored})

        # 保留最近 30 天
        data["history"] = data["history"][-30:]

        with open(_SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str, indent=2)

        logger.debug(f"[板块推理] 历史分数已保存: {_SCORES_FILE}")
    except Exception as e:
        logger.debug(f"[板块推理] 保存历史分数失败: {e}")


def _calc_mom(current: float, prev) -> str:
    """计算环比变化率

    Args:
        current: 当前总分
        prev: 上次总分 (float 或 None)

    Returns:
        环比变化率字符串 (如 "+5.2%" / "-3.1%" / "N/A(首次)")
    """
    if prev is None:
        return "N/A(首次)"
    try:
        prev = float(prev)
    except (TypeError, ValueError):
        return "N/A(首次)"
    if prev == 0:
        return "N/A(上次为0)"
    change = (current - prev) / prev * 100
    return f"{change:+.1f}%"


# =====================================================================
# 向后兼容输出
# =====================================================================
def _to_ranked_sector(scored: dict) -> dict:
    """转换为 ranked_sectors 格式 (向后兼容 Stock Selector / Dark Horse Scanner)

    保留下游需要的字段: sector / heat_score / trend / components
    新增 V2 字段: metrics / mom_change

    Args:
        scored: 五维评分 dict

    Returns:
        ranked_sector dict (兼容旧格式)
    """
    change_pct = scored.get("change_pct", 0)
    trend = "up" if change_pct > 0 else ("down" if change_pct < 0 else "flat")

    return {
        "sector": scored["sector"],
        "heat_score": scored["total_score"],  # 兼容: 用总分作为 heat_score
        "trend": trend,
        "fund_flow": scored.get("fund_flow", 0),
        "change_pct": change_pct,
        "rationale": (
            f"热度={scored['heat']}/扩散={scored['diffusion']}/"
            f"动摇={scored['volatility']}/回补={scored['rebound']}/"
            f"拥挤={scored['crowding']}"
        ),
        "components": scored.get("components", []),
        # V2 新增字段 (下游可选使用)
        "metrics": {
            "heat": scored["heat"],
            "diffusion": scored["diffusion"],
            "volatility": scored["volatility"],
            "rebound": scored["rebound"],
            "crowding": scored["crowding"],
        },
        "mom_change": scored.get("mom_change", "N/A"),
    }


# =====================================================================
# 工具函数
# =====================================================================
def _safe_float(val, default=0.0) -> float:
    """安全转换为 float"""
    try:
        if val is None or val == "":
            return default
        f = float(val)
        return f if f == f else default  # 排除 NaN
    except (ValueError, TypeError):
        return default


def _normalize_positive(val: float, max_val: float) -> float:
    """将正数归一化到 0-1 (负数归 0)

    用于资金流入: 正流入越大→分数越高, 负流入(流出)→0

    Args:
        val: 原始值 (如资金净流入 15.2e8)
        max_val: 参考上限 (如 50e8)

    Returns:
        0.0 到 1.0 之间的归一化值
    """
    if val <= 0 or max_val <= 0:
        return 0.0
    return min(val / max_val, 1.0)
