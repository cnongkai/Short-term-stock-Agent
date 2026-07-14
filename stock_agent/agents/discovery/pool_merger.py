"""
候选池合并器 (对应 PRD Phase 3 / 8.4 / 10.2)

合并主线选股 (hot_sector_candidates) 与黑马扫描 (dark_horse_candidates) 结果,
去重后写入 candidate_pool, 同时执行大盘熔断检查 (PRD 10.2)。

这是双路并行选股的汇合点: Stock Selector 和 Dark Horse Scanner 并行执行,
各自写入独立状态字段, Pool Merger 读取两路结果合并去重, 作为 candidate_pool 的
唯一写者 (单写者模式, 无需 reducer, 避免并行写冲突)。

熔断机制 (PRD 10.2):
  大盘 (上证指数) 单日跌幅 > circuit_breaker_drop_pct (默认 4%) 时触发熔断,
  设 circuit_breaker=True, 下游 should_run_analysis 据此路由到暂停节点。

图节点接口: create_pool_merger() → pool_merger_node(state) -> dict  (无 LLM)
返回: {"candidate_pool": [...], "circuit_breaker": bool}
"""
import json
import time
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_pool_merger():
    """创建候选池合并图节点 (无需 LLM, 纯数据合并)

    Returns:
        pool_merger_node(state) -> dict: 更新 candidate_pool + circuit_breaker
    """

    def pool_merger_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[候选池合并] 开始合并双路选股结果")

        # === 读取双路选股产出 (PRD 8.4) ===
        hot_candidates = state.get("hot_sector_candidates", [])
        dark_candidates = state.get("dark_horse_candidates", [])

        logger.info(f"[候选池合并] 主线 {len(hot_candidates)} 只 + 黑马 {len(dark_candidates)} 只")

        # === 去重合并 (主线优先, PRD 8.4) ===
        merged = _merge_and_dedup(hot_candidates, dark_candidates)

        logger.info(f"[候选池合并] 去重后候选池 {len(merged)} 只")
        for i, c in enumerate(merged, 1):
            logger.info(f"  {i}. {c.get('ticker','?')} {c.get('name','?')} "
                        f"[{c.get('sector','?')}] source={c.get('source','?')}")

        # === 大盘熔断检查 (PRD 10.2) ===
        circuit_breaker = _check_circuit_breaker(state)
        if circuit_breaker:
            logger.warning("[候选池合并] 触发熔断! 大盘跌幅超阈值, 将暂停推荐 (PRD 10.2)")
        else:
            logger.info("[候选池合并] 大盘正常, 未触发熔断")

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[候选池合并] 完成, 耗时 {node_elapsed:.2f}s, "
                    f"最终候选池 {len(merged)} 只, 熔断={circuit_breaker}")

        # candidate_pool 单写者 (本节点), 下游分析/决策层只读
        return {
            "candidate_pool": merged,
            "circuit_breaker": circuit_breaker,
        }

    return pool_merger_node


def _merge_and_dedup(hot_candidates: list, dark_candidates: list) -> list:
    """合并双路候选并按 ticker 去重 (主线优先, PRD 8.4)

    Args:
        hot_candidates: 主线选股结果 (source=hot_sector)
        dark_candidates: 黑马扫描结果 (source=dark_horse)

    Returns:
        去重后的候选池列表 (主线优先, 黑马重复时保留主线)
    """
    seen_tickers = set()
    merged = []

    # 主线优先加入
    for c in hot_candidates:
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker", "")).strip()
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        merged.append(c)

    # 黑马加入 (跳过与主线重复的 ticker)
    for c in dark_candidates:
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker", "")).strip()
        if not ticker or ticker in seen_tickers:
            logger.debug(f"[候选池合并] 黑马 {ticker} 与主线重复, 保留主线")
            continue
        seen_tickers.add(ticker)
        merged.append(c)

    return merged


def _check_circuit_breaker(state) -> bool:
    """大盘熔断检查 (PRD 10.2: 大盘单日跌幅 > 4%)

    读取上证指数 (sh000001) 当日跌幅, 超过阈值则触发熔断。
    数据源失败时不触发熔断 (宁可多推荐, 不误杀)。

    Args:
        state: 全局状态

    Returns:
        True 表示触发熔断, False 表示正常
    """
    from stock_agent.default_config import DEFAULT_CONFIG
    drop_threshold = DEFAULT_CONFIG.get("circuit_breaker_drop_pct", 4.0)

    trade_date = state.get("trade_date", "")

    try:
        # 获取上证指数行情 (默认 sh000001)
        res = data_interface.get_market_index("sh000001")
        if not res or not res.get("data"):
            logger.debug("[熔断检查] 无法获取大盘指数数据, 跳过熔断检查")
            return False

        index_data = res["data"]
        # 兼容多种数据格式: 可能是 dict 或 list
        if isinstance(index_data, list) and index_data:
            index_data = index_data[0] if isinstance(index_data[0], dict) else {}
        if not isinstance(index_data, dict):
            logger.debug("[熔断检查] 大盘指数数据格式异常, 跳过熔断检查")
            return False

        # 提取跌幅 (兼容 pct_change / change_pct / change 等字段名)
        change_pct = (
            index_data.get("pct_change")
            or index_data.get("change_pct")
            or index_data.get("change")
            or 0
        )
        try:
            change_pct = float(change_pct)
        except (TypeError, ValueError):
            change_pct = 0.0

        logger.info(f"[熔断检查] 上证指数当日涨跌幅: {change_pct}% (阈值: -{drop_threshold}%)")

        # 跌幅超过阈值触发熔断 (跌幅为负值, 如 -4.5% < -4.0%)
        if change_pct < -drop_threshold:
            return True

    except Exception as e:
        logger.warning(f"[熔断检查] 检查异常, 跳过熔断: {e}")
        return False

    return False
