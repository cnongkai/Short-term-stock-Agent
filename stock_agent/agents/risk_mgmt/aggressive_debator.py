"""
激进辩论者 (对应 PRD Phase 7 / 6.9 三风格风险辩论)

倾向支持高风险高收益决策, 强调上涨潜力与催化因素。
状态更新模式复用 TradingAgents-CN aggresive_debator.py:
  读取 risk_debate_state → llm.invoke → 更新 risky_history/latest_speaker/current_risky_response/count

参考架构: TradingAgents-CN agents/risk_mgmt/aggresive_debator.py
"""
from loguru import logger

from stock_agent.agents.utils.prompts import AGGRESSIVE_DEBATOR_SYSTEM
from stock_agent.agents.utils.json_helper import truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke


def create_aggressive_debator(llm):
    """创建激进辩论者图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        aggressive_node(state) -> dict: 更新 risk_debate_state
    """

    def aggressive_node(state) -> dict:
        logger.info("[激进辩论者] 开始发言")

        # === 读取状态 ===
        risk_state = state.get("risk_debate_state", {})
        history = risk_state.get("history", "")
        risky_history = risk_state.get("risky_history", "")
        current_safe = risk_state.get("current_safe_response", "")
        current_neutral = risk_state.get("current_neutral_response", "")

        trader_plan = state.get("trader_investment_plan", "{}")

        # === 构建 prompt ===
        prompt = f"""{AGGRESSIVE_DEBATOR_SYSTEM}

交易代理的逐只交易决策:
{truncate_text(trader_plan, 3000)}

风险辩论历史:
{truncate_text(history, 2000)}

保守方最新论点: {current_safe or '(暂无)'}
中立方最新论点: {current_neutral or '(暂无)'}

请从激进视角为交易决策辩护, 强调上涨潜力和错过机会的成本。
用中文以对话方式输出, 直接回应保守和中立方的观点。"""

        # === 调用 LLM ===
        response = safe_llm_invoke(
            llm, prompt,
            timeout=90, max_retries=2,
            fallback_content="(激进辩论者发言失败)",
            node_name="激进辩论者",
        )
        argument = f"Risky Analyst: {response.content}"

        # === 更新风险辩论状态 (复用 TA 模式) ===
        new_count = risk_state.get("count", 0) + 1
        logger.info(f"[激进辩论者] 发言完成, 计数: {risk_state.get('count', 0)} -> {new_count}")

        new_risk_state = {
            "history": history + "\n" + argument,
            "risky_history": risky_history + "\n" + argument,
            "safe_history": risk_state.get("safe_history", ""),
            "neutral_history": risk_state.get("neutral_history", ""),
            "latest_speaker": "Risky",
            "current_risky_response": argument,
            "current_safe_response": risk_state.get("current_safe_response", ""),
            "current_neutral_response": risk_state.get("current_neutral_response", ""),
            "judge_decision": risk_state.get("judge_decision", ""),
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_state}

    return aggressive_node
