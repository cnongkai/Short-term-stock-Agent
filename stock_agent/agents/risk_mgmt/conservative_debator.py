"""
保守辩论者 (对应 PRD Phase 7 / 6.9 三风格风险辩论)

倾向控制风险、保护本金, 强调下行风险与不确定性。
状态更新模式复用 TradingAgents-CN conservative_debator.py:
  读取 risk_debate_state → llm.invoke → 更新 safe_history/latest_speaker/current_safe_response/count

参考架构: TradingAgents-CN agents/risk_mgmt/conservative_debator.py
"""
from loguru import logger

from stock_agent.agents.utils.prompts import CONSERVATIVE_DEBATOR_SYSTEM
from stock_agent.agents.utils.json_helper import truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke


def create_conservative_debator(llm):
    """创建保守辩论者图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        conservative_node(state) -> dict: 更新 risk_debate_state
    """

    def conservative_node(state) -> dict:
        logger.info("[保守辩论者] 开始发言")

        # === 读取状态 ===
        risk_state = state.get("risk_debate_state", {})
        history = risk_state.get("history", "")
        safe_history = risk_state.get("safe_history", "")
        current_risky = risk_state.get("current_risky_response", "")
        current_neutral = risk_state.get("current_neutral_response", "")

        trader_plan = state.get("trader_investment_plan", "{}")

        # === 构建 prompt ===
        prompt = f"""{CONSERVATIVE_DEBATOR_SYSTEM}

交易代理的逐只交易决策:
{truncate_text(trader_plan, 3000)}

风险辩论历史:
{truncate_text(history, 2000)}

激进方最新论点: {current_risky or '(暂无)'}
中立方最新论点: {current_neutral or '(暂无)'}

请从保守视角质疑交易决策, 强调下行风险和止损纪律。
用中文以对话方式输出, 直接回应激进和中立方的观点。"""

        # === 调用 LLM ===
        response = safe_llm_invoke(
            llm, prompt,
            timeout=90, max_retries=2,
            fallback_content="(保守辩论者发言失败)",
            node_name="保守辩论者",
        )
        argument = f"Safe Analyst: {response.content}"

        # === 更新风险辩论状态 ===
        new_count = risk_state.get("count", 0) + 1
        logger.info(f"[保守辩论者] 发言完成, 计数: {risk_state.get('count', 0)} -> {new_count}")

        new_risk_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_state.get("risky_history", ""),
            "safe_history": safe_history + "\n" + argument,
            "neutral_history": risk_state.get("neutral_history", ""),
            "latest_speaker": "Safe",
            "current_risky_response": risk_state.get("current_risky_response", ""),
            "current_safe_response": argument,
            "current_neutral_response": risk_state.get("current_neutral_response", ""),
            "judge_decision": risk_state.get("judge_decision", ""),
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_state}

    return conservative_node
