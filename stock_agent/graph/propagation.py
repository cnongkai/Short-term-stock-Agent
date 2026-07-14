"""
状态初始化与图参数 (对应 PRD Phase 0 用户配置 + 状态流转)

提供 create_initial_state 创建图的初始状态, get_graph_args 获取图执行参数。
参考架构: TradingAgents-CN graph/propagation.py
"""
from typing import Dict, Any, Tuple

from stock_agent.agents.utils.agent_states import AgentState, InvestDebateState, RiskDebateState


def create_initial_state(
    trade_date: str,
    user_config: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """创建图的初始状态

    Args:
        trade_date: 交易日期 (YYYY-MM-DD)
        user_config: 用户配置 (风险偏好/持仓周期/板块偏好, PRD M9)

    Returns:
        初始状态字典
    """
    if user_config is None:
        user_config = {
            "risk_preference": "moderate",  # conservative/moderate/aggressive
            "holding_period": "1-5",  # 持仓周期(交易日)
            "sector_preference": [],  # 板块偏好(空表示不限制)
        }

    return {
        "trade_date": trade_date,
        "user_config": user_config,
        "messages": [],
        # 发现层产出
        "hot_topics": [],
        "policy_events": [],
        "ranked_sectors": [],
        "sector_crowding_map": {},  # V2 新增: 板块拥挤度地图
        # 双路选股产出 (PRD 8.3/8.4 并行, 分离字段避免写冲突)
        "hot_sector_candidates": [],
        "dark_horse_candidates": [],
        "candidate_pool": [],
        # 分析层产出
        "analysis_reports": {},
        "react_tool_call_counts": {},
        # 决策层产出
        "investment_debate_state": InvestDebateState(
            bull_history="",
            bear_history="",
            history="",
            current_response="",
            judge_decision="",
            count=0,
        ),
        "investment_plan": "",
        "trader_investment_plan": "",
        "risk_debate_state": RiskDebateState(
            risky_history="",
            safe_history="",
            neutral_history="",
            history="",
            latest_speaker="",
            current_risky_response="",
            current_safe_response="",
            current_neutral_response="",
            judge_decision="",
            count=0,
        ),
        "final_trade_decision": "",
        "final_portfolio": [],
        # 熔断标志
        "circuit_breaker": False,
    }


def get_graph_args(config: Dict[str, Any]) -> Dict[str, Any]:
    """从配置提取图执行参数

    Args:
        config: 系统配置字典

    Returns:
        图执行参数 (max_debate_rounds / max_risk_discuss_rounds / max_recur_limit)
    """
    return {
        "max_debate_rounds": config.get("max_debate_rounds", 2),
        "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds", 1),
        "max_recur_limit": config.get("max_recur_limit", 200),
    }
