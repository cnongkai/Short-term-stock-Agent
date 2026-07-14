"""
看多研究员 (对应 PRD Phase 5 / 6.9 多智能体辩论时序)

基于四维分析报告论证候选池的上涨理由, 与看空研究员交替辩论。
状态更新模式复用 TradingAgents-CN bull_researcher.py:
  读取 investment_debate_state → llm.invoke → 更新 history/bull_history/current_response/count

参考架构: TradingAgents-CN agents/researchers/bull_researcher.py
"""
from loguru import logger

from stock_agent.agents.utils.prompts import BULL_RESEARCHER_SYSTEM
from stock_agent.agents.utils.json_helper import truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke


def create_bull_researcher(llm):
    """创建看多研究员图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        bull_node(state) -> dict: 更新 investment_debate_state
    """

    def bull_node(state) -> dict:
        logger.info("[看多研究员] 开始论证候选池上涨理由")

        # === 读取状态 ===
        debate_state = state.get("investment_debate_state", {})
        history = debate_state.get("history", "")
        current_response = debate_state.get("current_response", "")  # 看空方最新发言

        analysis_reports = state.get("analysis_reports", {})
        candidate_pool = state.get("candidate_pool", [])

        # === 构建候选池分析摘要 (供看多方引用) ===
        reports_summary = _build_reports_summary(analysis_reports, candidate_pool)

        # === 构建 prompt (PRD Phase 5: 看多论证) ===
        prompt = f"""{BULL_RESEARCHER_SYSTEM}

当前候选池 ({len(candidate_pool)} 只标的) 的四维分析报告摘要:
{reports_summary}

辩论对话历史:
{truncate_text(history, 3000)}

看空方最新论点:
{current_response or '(首轮发言, 暂无看空方论点)'}

请基于上述分析报告, 为候选池构建强有力的看多论证。
重点关注: 基本面强劲的标的、技术面向好的标的、有催化事件的标的。
请用中文以对话方式输出你的看多论点。"""

        # === 调用 LLM ===
        response = safe_llm_invoke(
            llm, prompt,
            timeout=90, max_retries=2,
            fallback_content="(看多论证失败)",
            node_name="看多研究员",
        )
        argument = f"Bull Analyst: {response.content}"

        # === 更新辩论状态 (复用 TA 模式) ===
        new_count = debate_state.get("count", 0) + 1
        logger.info(f"[看多研究员] 发言完成, 计数: {debate_state.get('count', 0)} -> {new_count}")

        new_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": debate_state.get("bull_history", "") + "\n" + argument,
            "bear_history": debate_state.get("bear_history", ""),
            "current_response": argument,
            "judge_decision": debate_state.get("judge_decision", ""),
            "count": new_count,
        }

        return {"investment_debate_state": new_debate_state}

    return bull_node


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
                report_text = dim_report.get("report", "")
                lines.append(f"  {dim}: 评级={rating} 置信度={confidence}")
                if report_text:
                    lines.append(f"    摘要: {truncate_text(report_text, 200)}")
            else:
                lines.append(f"  {dim}: {str(dim_report)[:100]}")

    return "\n".join(lines) if lines else "(无可用分析报告)"
