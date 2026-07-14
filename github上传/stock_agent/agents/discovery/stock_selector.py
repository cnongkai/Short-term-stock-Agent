"""
个股选取器 - 主线 (对应 PRD Phase 3 / M3 双路并行 / 8.4)

从 Top N 热门板块中选取短线候选股, 是双路选股的"主线"路径。
与黑马扫描器 (dark_horse_scanner) 并行执行, 两者写入不同的状态字段
(hot_sector_candidates / dark_horse_candidates), 由 Pool Merger 合并去重。

主线选股策略 (PRD 7.3):
  1. 初筛: 从 Top N 板块成分股中筛除 ST/流动性不足/市值过小的标的
  2. 复筛: 识别龙头股 (板块内涨幅 Top1-3) 和跟风股 (资金流入但涨幅次之)
  3. 每板块 1-2 只, 合计 5-8 只

图节点接口: create_stock_selector(llm) → stock_selector_node(state) -> dict
返回: {"hot_sector_candidates": [...]}  (单写者, 无需 reducer)
"""
import json
import time
from loguru import logger

from stock_agent.agents.utils.prompts import STOCK_SELECTOR_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.log_utils import log_stage
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.dataflows import interface as data_interface


def create_stock_selector(llm):
    """创建主线个股选取图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        stock_selector_node(state) -> dict: 更新 hot_sector_candidates
    """

    def stock_selector_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[主线选股] 开始从热门板块选取候选股")

        trade_date = state.get("trade_date", "")
        ranked_sectors = state.get("ranked_sectors", [])

        if not ranked_sectors:
            logger.warning("[主线选股] 板块排名为空, 无法选股")
            return {"hot_sector_candidates": []}

        logger.info(f"[主线选股] 输入: {len(ranked_sectors)} 个热门板块")

        # === 读取选股数量配置 (PRD 7.3: 5-8 只) ===
        from stock_agent.default_config import DEFAULT_CONFIG
        pool_min = DEFAULT_CONFIG.get("hot_sector_pool_min", 5)
        pool_max = DEFAULT_CONFIG.get("hot_sector_pool_max", 8)

        # === 获取各板块成分股 (PRD M2 板块筛选器输出) ===
        with log_stage("获取板块成分股", "主线选股") as stage:
            sector_stocks = _fetch_sector_stocks(ranked_sectors)
            stage.result = f"{len(sector_stocks)} 字符"

        # === 获取市场异动榜 (辅助识别龙头/涨幅领先股) ===
        with log_stage("获取市场异动榜", "主线选股"):
            market_movers = _fetch_market_movers()

        # === 构建 prompt (PRD 8.4) ===
        prompt = f"""{STOCK_SELECTOR_SYSTEM}

分析日期: {trade_date}
选股数量: {pool_min}-{pool_max} 只

热门板块排名 (Top {len(ranked_sectors)}):
{truncate_text(json.dumps(ranked_sectors, ensure_ascii=False, default=str), 2500)}

各板块成分股 (含行情摘要):
{truncate_text(sector_stocks, 3000)}

市场涨幅榜/异动榜 (辅助识别龙头):
{truncate_text(market_movers, 1500)}

请从上述热门板块中选取 {pool_min}-{pool_max} 只短线候选股。
每板块 1-2 只, 优先选取板块龙头 (涨幅 Top1-3) 和资金流入的跟风股。
输出严格 JSON:
{{
  "candidates": [
    {{
      "ticker": "600584",
      "name": "长电科技",
      "sector": "半导体",
      "source": "hot_sector",
      "role": "leader",
      "reason": "板块内涨幅Top1+资金净流入Top2"
    }}
  ]
}}"""

        # === 调用 LLM 选股 ===
        with log_stage("LLM 选股", "主线选股"):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"candidates": []}',
                node_name="主线选股",
            )
        result = safe_json_parse(response.content, default={"candidates": []})
        candidates = result.get("candidates", []) if isinstance(result, dict) else []

        # 标记来源 (确保 source 固定为 hot_sector)
        for c in candidates:
            if isinstance(c, dict):
                c["source"] = "hot_sector"
                c.setdefault("role", "follower")

        # 截取上限
        candidates = candidates[:pool_max]

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[主线选股] 完成, 耗时 {node_elapsed:.2f}s, 选取 {len(candidates)} 只主线候选")
        for i, c in enumerate(candidates, 1):
            logger.info(f"  {i}. {c.get('ticker','?')} {c.get('name','?')} "
                        f"[{c.get('sector','?')}] role={c.get('role','?')}")

        # 仅写 hot_sector_candidates (单写者, 与黑马扫描并行不冲突)
        return {"hot_sector_candidates": candidates}

    return stock_selector_node


def _fetch_sector_stocks(ranked_sectors: list) -> str:
    """获取各热门板块的成分股列表 (PRD M2)"""
    results = []
    for sector_info in ranked_sectors:
        if not isinstance(sector_info, dict):
            continue
        sector_name = sector_info.get("sector", "")
        if not sector_name:
            continue
        try:
            res = data_interface.get_sector_stocks(sector_name)
            if res and res.get("data"):
                # 仅取成分股摘要 (代码+名称+涨幅), 避免数据过大
                stocks = res["data"][:20]  # 每板块最多 20 只
                results.append({
                    "sector": sector_name,
                    "heat_score": sector_info.get("heat_score"),
                    "trend": sector_info.get("trend"),
                    "stocks": stocks,
                })
        except Exception as e:
            logger.debug(f"[主线选股] 获取板块 {sector_name} 成分股失败: {e}")
            # 降级: 用板块推理时给出的 components
            results.append({
                "sector": sector_name,
                "stocks": sector_info.get("components", []),
                "note": "成分股数据获取失败, 使用推理时的成分股",
            })

    if not results:
        return "(暂无可用板块成分股数据, 请基于板块排名中的 components 字段选股)"
    return json.dumps(results, ensure_ascii=False, default=str)


def _fetch_market_movers() -> str:
    """获取市场异动股 (涨幅榜, 辅助识别龙头)"""
    try:
        res = data_interface.get_market_movers()
        if res and res.get("data"):
            return json.dumps(res["data"][:30], ensure_ascii=False, default=str)
    except Exception as e:
        logger.debug(f"[主线选股] 获取市场异动榜失败: {e}")
    return "(暂无可用市场异动数据)"
