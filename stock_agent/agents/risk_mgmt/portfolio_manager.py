"""
投资组合经理 (对应 PRD Phase 8 / 6.9 最终推荐清单聚合)

聚合通过风控审批的标的, 控制组合整体风险, 输出最终推荐清单。
输出 final_portfolio (列表, 每只含 ticker/name/source/rating/target_price/
  take_profit/stop_loss/confidence/risk_level)。

PRD 约束:
  - 组合行业分散

参考架构: TradingAgents-CN (portfolio_manager 为 V2 新增, TA 无此角色)
"""
import json
from loguru import logger

from stock_agent.agents.utils.prompts import PORTFOLIO_MANAGER_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.graph.run_metrics import get_metrics


def create_portfolio_manager(llm):
    """创建投资组合经理图节点

    Args:
        llm: LLM 实例 (deep_thinking_llm)

    Returns:
        portfolio_manager_node(state) -> dict: 更新 final_portfolio
    """

    def portfolio_manager_node(state) -> dict:
        logger.info("[组合经理] 开始聚合最终推荐清单")

        metrics = get_metrics()

        # === 读取状态 ===
        final_decision = state.get("final_trade_decision", "{}")
        trader_plan = state.get("trader_investment_plan", "{}")
        analysis_reports = state.get("analysis_reports", {})
        candidate_pool = state.get("candidate_pool", [])

        # === 解析审批结果, 筛选通过审批的标的 ===
        risk_result = safe_json_parse(final_decision, default={"decisions": []})
        risk_decisions = risk_result.get("decisions", []) if isinstance(risk_result, dict) else []

        trader_result = safe_json_parse(trader_plan, default={"decisions": []})
        trader_decisions = trader_result.get("decisions", []) if isinstance(trader_result, dict) else []

        # 构建标的索引 (ticker → 交易决策 + 风控决策)
        approved_stocks = _build_approved_list(risk_decisions, trader_decisions, candidate_pool)

        if not approved_stocks:
            logger.warning("[组合经理] 无标的通过风控审批, 输出空推荐清单")
            return {"final_portfolio": []}

        # === 构建 prompt (LLM 优化组合配置) ===
        approved_summary = json.dumps(approved_stocks, ensure_ascii=False, indent=2)

        prompt = f"""{PORTFOLIO_MANAGER_SYSTEM}

通过风控审批的标的 ({len(approved_stocks)} 只):
{truncate_text(approved_summary, 3000)}

请输出最终推荐清单, 确保组合分散。
输出严格 JSON:
{{
  "portfolio": [
    {{
      "ticker": "代码",
      "name": "名称",
      "source": "hot_sector 或 dark_horse",
      "rating": "买入/持有/卖出",
      "target_price": "目标价",
      "take_profit": "止盈价",
      "stop_loss": "止损价",
      "confidence": 0.0到1.0,
      "risk_level": 0.0到1.0
    }}
  ],
  "summary": "组合整体分析"
}}

要求:
- 组合行业分散"""

        # === 调用 LLM + 节点计时 + Token 估算 ===
        with metrics.node_timer("组合经理"), metrics.llm_timer("组合经理", prompt):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{}',
                node_name="组合经理",
                on_fallback=metrics.record_timeout,
            )
        content = response.content
        metrics.record_llm_result("组合经理", content)

        result = safe_json_parse(content, default={"portfolio": approved_stocks})
        portfolio = result.get("portfolio", approved_stocks) if isinstance(result, dict) else approved_stocks

        logger.info(f"[组合经理] 最终推荐 {len(portfolio)} 只标的")
        return {"final_portfolio": portfolio}

    return portfolio_manager_node


def _build_approved_list(risk_decisions, trader_decisions, candidate_pool) -> list:
    """构建通过审批的标的清单 (合并交易决策 + 风控决策)"""
    # 索引: ticker → 风控决策
    risk_map = {d.get("ticker"): d for d in risk_decisions if isinstance(d, dict)}
    # 索引: ticker → 交易决策
    trader_map = {d.get("ticker"): d for d in trader_decisions if isinstance(d, dict)}
    # 索引: ticker → 候选池信息 (含 sector)
    pool_map = {s.get("ticker"): s for s in candidate_pool if isinstance(s, dict)}

    approved = []
    for ticker, risk_dec in risk_map.items():
        decision = risk_dec.get("decision", "拒绝")
        if decision not in ("批准", "调整"):
            continue  # 仅保留批准和调整的标的

        trader_dec = trader_map.get(ticker, {})
        pool_info = pool_map.get(ticker, {})

        approved.append({
            "ticker": ticker,
            "name": risk_dec.get("name", trader_dec.get("name", pool_info.get("name", ""))),
            "source": pool_info.get("source", trader_dec.get("source", "")),
            "sector": pool_info.get("sector", ""),
            "rating": trader_dec.get("action", "持有"),
            "target_price": str(trader_dec.get("target_price", "0")),
            "take_profit": str(trader_dec.get("take_profit", "0")),
            "stop_loss": str(trader_dec.get("stop_loss", "0")),
            "confidence": trader_dec.get("confidence", 0.5),
            "risk_level": trader_dec.get("risk_level", 0.5),
        })

    return approved
