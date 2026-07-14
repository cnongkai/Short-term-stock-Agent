"""
研究经理 (对应 PRD Phase 5 / 6.9 裁决辩论)

综合多空辩论历史与四维分析报告, 裁决输出投资计划。
输出 investment_plan (JSON 字符串, 含整体方向 + 逐只标的建议)。

参考架构: TradingAgents-CN agents/researchers/ (research_manager 模式)
"""
import json
from loguru import logger

from stock_agent.agents.utils.prompts import RESEARCH_MANAGER_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.graph.run_metrics import get_metrics


def create_research_manager(llm):
    """创建研究经理图节点

    Args:
        llm: LLM 实例 (deep_thinking_llm, 深度思考)

    Returns:
        research_manager_node(state) -> dict: 更新 investment_plan + investment_debate_state.judge_decision
    """

    def research_manager_node(state) -> dict:
        logger.info("[研究经理] 开始裁决多空辩论")

        metrics = get_metrics()

        # === 读取状态 ===
        debate_state = state.get("investment_debate_state", {})
        history = debate_state.get("history", "")

        analysis_reports = state.get("analysis_reports", {})
        candidate_pool = state.get("candidate_pool", [])

        # === 构建候选池摘要 ===
        pool_summary = _build_pool_summary(analysis_reports, candidate_pool)

        # === 构建 prompt ===
        prompt = f"""{RESEARCH_MANAGER_SYSTEM}

多空辩论完整历史:
{truncate_text(history, 4000)}

候选池四维分析摘要:
{pool_summary}

请综合辩论双方观点与分析报告, 裁决输出投资计划。
输出必须为严格 JSON 格式, 包含整体方向和逐只标的的建议。
对于每只标的, 给出 direction(看多/看空/中性)。"""

        # === 调用 LLM (深度思考) + 节点计时 + Token 估算 ===
        with metrics.node_timer("研究经理"), metrics.llm_timer("研究经理", prompt):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"direction": "中性", "rationale": "LLM调用失败"}',
                node_name="研究经理",
            )
        content = response.content
        metrics.record_llm_result("研究经理", content)

        # === 解析 JSON (容错) ===
        plan = safe_json_parse(content, default={"direction": "中性", "rationale": content})

        # 如果解析出的是 dict, 补充逐只建议
        if isinstance(plan, dict) and "stocks" not in plan:
            plan["stocks"] = _build_stock_directions(candidate_pool, analysis_reports, plan)

        investment_plan = json.dumps(plan, ensure_ascii=False)

        logger.info(f"[研究经理] 裁决完成, 整体方向: {plan.get('direction', '未知')}")

        # === 更新状态 ===
        new_debate_state = dict(debate_state)
        new_debate_state["judge_decision"] = investment_plan

        return {
            "investment_plan": investment_plan,
            "investment_debate_state": new_debate_state,
        }

    return research_manager_node


def _build_pool_summary(analysis_reports: dict, candidate_pool: list) -> str:
    """构建候选池摘要 (评级 + 置信度)"""
    if not candidate_pool:
        return "(候选池为空)"

    lines = []
    for stock in candidate_pool:
        ticker = stock.get("ticker", "?")
        name = stock.get("name", "?")
        source = stock.get("source", "?")
        report = analysis_reports.get(ticker, {})

        ratings = []
        for dim in ["fundamentals", "technical", "china_specific", "stock_development"]:
            dim_report = report.get(dim, {})
            if isinstance(dim_report, dict):
                ratings.append(f"{dim}={dim_report.get('rating', '?')}")

        lines.append(f"  {ticker} {name} [{source}]: {' | '.join(ratings) or '无分析数据'}")

    return "\n".join(lines)


def _build_stock_directions(candidate_pool: list, analysis_reports: dict, plan: dict) -> list:
    """根据整体方向和分析报告构建逐只标的建议"""
    overall_direction = plan.get("direction", "中性")
    stocks = []
    for stock in candidate_pool:
        ticker = stock.get("ticker", "?")
        name = stock.get("name", "?")
        # 简单映射: 整体看多则逐只看多, 实际应由 LLM 决定
        stocks.append({
            "ticker": ticker,
            "name": name,
            "direction": overall_direction,
        })
    return stocks
