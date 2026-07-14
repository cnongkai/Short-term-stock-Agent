"""
看空研究员 (对应 PRD Phase 5 / 6.9 多智能体辩论时序)

基于四维分析报告论证候选池的下跌风险, 与看多研究员交替辩论。
状态更新模式复用 TradingAgents-CN bear_researcher.py:
  读取 investment_debate_state → llm.invoke → 更新 history/bear_history/current_response/count

参考架构: TradingAgents-CN agents/researchers/bear_researcher.py
"""
from loguru import logger

from stock_agent.agents.utils.prompts import BEAR_RESEARCHER_SYSTEM
from stock_agent.agents.utils.json_helper import truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke


def create_bear_researcher(llm):
    """创建看空研究员图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        bear_node(state) -> dict: 更新 investment_debate_state
    """

    def bear_node(state) -> dict:
        logger.info("[看空研究员] 开始论证候选池下跌风险")

        # === 读取状态 ===
        debate_state = state.get("investment_debate_state", {})
        history = debate_state.get("history", "")
        current_response = debate_state.get("current_response", "")  # 看多方最新发言

        analysis_reports = state.get("analysis_reports", {})
        candidate_pool = state.get("candidate_pool", [])

        # === 构建候选池分析摘要 ===
        reports_summary = _build_reports_summary(analysis_reports, candidate_pool)

        # === 构建 prompt (PRD Phase 5: 看空论证) ===
        prompt = f"""{BEAR_RESEARCHER_SYSTEM}

当前候选池 ({len(candidate_pool)} 只标的) 的四维分析报告摘要:
{reports_summary}

辩论对话历史:
{truncate_text(history, 3000)}

看多方最新论点:
{current_response or '(首轮发言, 暂无看多方论点)'}

请基于上述分析报告, 为候选池构建看空论证。
重点关注: 估值偏高的标的、技术面疲软的标的、有股东减持/风险事件的标的。
请用中文以对话方式输出你的看空论点。"""

        # === 调用 LLM ===
        response = safe_llm_invoke(
            llm, prompt,
            timeout=90, max_retries=2,
            fallback_content="(看空论证失败)",
            node_name="看空研究员",
        )
        argument = f"Bear Analyst: {response.content}"

        # === 更新辩论状态 ===
        new_count = debate_state.get("count", 0) + 1
        logger.info(f"[看空研究员] 发言完成, 计数: {debate_state.get('count', 0)} -> {new_count}")

        new_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": debate_state.get("bull_history", ""),
            "bear_history": debate_state.get("bear_history", "") + "\n" + argument,
            "current_response": argument,
            "judge_decision": debate_state.get("judge_decision", ""),
            "count": new_count,
        }

        return {"investment_debate_state": new_debate_state}

    return bear_node


def _build_reports_summary(analysis_reports: dict, candidate_pool: list) -> str:
    """构建候选池四维分析报告摘要 (供辩论引用)"""
    if not analysis_reports:
        return "(分析报告为空, 可能数据源不可用)"

    lines = []
    for stock in candidate_pool:
        ticker = stock.get("ticker", "?")
        name = stock.get("name", "?")
        source = stock.get("source", "?")
        report = analysis_reports.get(ticker, {})

        lines.append(f"\n--- {ticker} {name} [{source}] ---")
        for dim in ["fundamentals", "technical", "china_specific", "stock_development"]:
            dim_report = report.get(dim, {})
            if isinstance(dim_report, dict):
                rating = dim_report.get("rating", "?")
                confidence = dim_report.get("confidence", "?")
                risks = dim_report.get("risks", [])
                lines.append(f"  {dim}: 评级={rating} 置信度={confidence}")
                if risks:
                    lines.append(f"    风险: {', '.join(str(r) for r in risks[:3])}")
            else:
                lines.append(f"  {dim}: {str(dim_report)[:100]}")

    return "\n".join(lines) if lines else "(无可用分析报告)"
