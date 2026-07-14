"""
中立辩论者 (对应 PRD Phase 7 / 6.9 三风格风险辩论)

平衡收益与风险, 客观权衡多空双方观点。
状态更新模式复用 TradingAgents-CN neutral_debator.py:
  读取 risk_debate_state → llm.invoke → 更新 neutral_history/latest_speaker/current_neutral_response/count

参考架构: TradingAgents-CN agents/risk_mgmt/neutral_debator.py
"""
from loguru import logger

from stock_agent.agents.utils.prompts import NEUTRAL_DEBATOR_SYSTEM
from stock_agent.agents.utils.json_helper import truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke


def create_neutral_debator(llm):
    """创建中立辩论者图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        neutral_node(state) -> dict: 更新 risk_debate_state
    """

    def neutral_node(state) -> dict:
        logger.info("[中立辩论者] 开始发言")

        # === 读取状态 ===
        risk_state = state.get("risk_debate_state", {})
        history = risk_state.get("history", "")
        neutral_history = risk_state.get("neutral_history", "")
        current_risky = risk_state.get("current_risky_response", "")
        current_safe = risk_state.get("current_safe_response", "")

        trader_plan = state.get("trader_investment_plan", "{}")

        # === 构建 prompt ===
        prompt = f"""{NEUTRAL_DEBATOR_SYSTEM}

交易代理的逐只交易决策:
{truncate_text(trader_plan, 3000)}

风险辩论历史:
{truncate_text(history, 2000)}

激进方最新论点: {current_risky or '(暂无)'}
保守方最新论点: {current_safe or '(暂无)'}

请从中立视角综合评估风险收益比, 给出平衡建议。
用中文以对话方式输出, 调和激进与保守方观点。"""

        # === 调用 LLM ===
        response = safe_llm_invoke(
            llm, prompt,
            timeout=90, max_retries=2,
            fallback_content="(中立辩论者发言失败)",
            node_name="中立辩论者",
        )
        argument = f"Neutral Analyst: {response.content}"

        # === 更新风险辩论状态 ===
        new_count = risk_state.get("count", 0) + 1
        logger.info(f"[中立辩论者] 发言完成, 计数: {risk_state.get('count', 0)} -> {new_count}")

        new_risk_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_state.get("risky_history", ""),
            "safe_history": risk_state.get("safe_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_risky_response": risk_state.get("current_risky_response", ""),
            "current_safe_response": risk_state.get("current_safe_response", ""),
            "current_neutral_response": argument,
            "judge_decision": risk_state.get("judge_decision", ""),
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_state}

    return neutral_node
