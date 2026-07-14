"""
条件路由逻辑 (对应 PRD 第6章流程设计)

定义 LangGraph 条件边的路由判断函数, 控制图流转:
  - should_continue_debate: 多空辩论循环 (PRD Phase 5)
  - should_continue_risk_analysis: 三风格风险辩论循环 (PRD Phase 7)
  - check_circuit_breaker: 熔断检查 (PRD 10.2)

。
"""
from loguru import logger

from stock_agent.agents.utils.agent_states import AgentState


class ConditionalLogic:
    """图条件路由逻辑"""

    def __init__(self, max_debate_rounds: int = 2, max_risk_discuss_rounds: int = 1):
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds

    # =================================================================
    # 多空辩论循环控制 (PRD Phase 5, 复用 TA should_continue_debate)
    # 实际发言次数 = 2 * max_debate_rounds (Bull 和 Bear 各发言 max_debate_rounds 次)
    # =================================================================
    def should_continue_debate(self, state: AgentState) -> str:
        """判断多空辩论是否继续"""
        debate_state = state.get("investment_debate_state", {})
        current_count = debate_state.get("count", 0)
        max_count = 2 * self.max_debate_rounds
        current_response = debate_state.get("current_response", "")

        logger.info(
            f"[投资辩论控制] 当前发言次数: {current_count}/{max_count} "
            f"(配置轮次: {self.max_debate_rounds})"
        )

        if current_count >= max_count:
            logger.info("[投资辩论控制] 达到最大次数, 结束辩论 -> Research Manager")
            return "Research Manager"

        # 交替发言: Bull -> Bear -> Bull -> Bear ...
        next_speaker = "Bear Researcher" if current_response.startswith("Bull") else "Bull Researcher"
        logger.info(f"[投资辩论控制] 继续辩论 -> {next_speaker}")
        return next_speaker

    # =================================================================
    # 三风格风险辩论循环控制 (PRD Phase 7, 复用 TA should_continue_risk_analysis)
    # 实际发言次数 = 3 * max_risk_discuss_rounds (Risky/Safe/Neutral 各发言 max_risk_discuss_rounds 次)
    # =================================================================
    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """判断风险辩论是否继续"""
        risk_state = state.get("risk_debate_state", {})
        current_count = risk_state.get("count", 0)
        max_count = 3 * self.max_risk_discuss_rounds
        latest_speaker = risk_state.get("latest_speaker", "")

        logger.info(
            f"[风险讨论控制] 当前发言次数: {current_count}/{max_count} "
            f"(配置轮次: {self.max_risk_discuss_rounds})"
        )

        if current_count >= max_count:
            logger.info("[风险讨论控制] 达到最大次数, 结束讨论 -> Risk Judge")
            return "Risk Judge"

        # 轮转发言: Risky -> Safe -> Neutral -> Risky -> ...
        # 注意: 返回值必须与 setup.py 中 add_node 注册的节点名一致
        # (Aggressive Analyst / Conservative Analyst / Neutral Analyst)
        # debator 设置的 latest_speaker 为 "Risky"/"Safe"/"Neutral"
        if latest_speaker.startswith("Risky"):
            next_speaker = "Conservative Analyst"
        elif latest_speaker.startswith("Safe"):
            next_speaker = "Neutral Analyst"
        else:
            next_speaker = "Aggressive Analyst"

        logger.info(f"[风险讨论控制] 继续讨论 -> {next_speaker}")
        return next_speaker

    # =================================================================
    # 熔断检查 (PRD 10.2 极端行情: 大盘单日跌幅 > 4%)
    # =================================================================
    def check_circuit_breaker(self, state: AgentState) -> str:
        """检查是否触发熔断, 触发则跳转暂停节点"""
        if state.get("circuit_breaker", False):
            logger.warning("[熔断检查] 大盘跌幅超阈值, 触发熔断暂停推荐 (PRD 10.2)")
            return "Circuit Breaker Pause"
        return "Continue"

    # =================================================================
    # 候选池分析分发判断 (PRD Phase 4 分析层入口)
    # =================================================================
    def should_run_analysis(self, state: AgentState) -> str:
        """判断是否进入分析层 (候选池非空且未熔断)"""
        if state.get("circuit_breaker", False):
            return "Circuit Breaker Pause"
        candidate_pool = state.get("candidate_pool", [])
        if not candidate_pool:
            logger.warning("[分析层] 候选池为空, 跳过分析直接输出空报告 (PRD 10.2 发现无结果)")
            return "Report Generator"
        return "Analysis Layer"
