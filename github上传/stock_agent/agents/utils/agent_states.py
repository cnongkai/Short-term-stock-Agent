"""
Agent 状态定义 (对应 PRD 第8章 AI 任务定义与输入输出规范)

定义 LangGraph 全局状态 AgentState 及辩论子状态。
复用 TradingAgents-CN 的 Annotated[type, desc] 模式, 扩展 PRD V2 新增字段:
  - 发现层产出: hot_topics / policy_events / ranked_sectors / candidate_pool
  - 分析层产出: analysis_reports (4维, 含 ReAct 日志)
  - ReAct 计数器: react_tool_call_counts (防死循环)
  - 熔断标志: circuit_breaker (PRD 10.2)

参考架构: TradingAgents-CN agents/utils/agent_states.py
"""
import operator
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph import MessagesState


# =====================================================================
# 研究辩论子状态 (PRD Phase 5 研究辩论, 看多 vs 看空)
# 复用 TradingAgents-CN InvestDebateState
# =====================================================================
class InvestDebateState(TypedDict):
    """投资辩论状态 (多空研究员辩论)"""
    bull_history: Annotated[str, "看多研究员发言历史"]
    bear_history: Annotated[str, "看空研究员发言历史"]
    history: Annotated[str, "辩论总历史"]
    current_response: Annotated[str, "最新发言 (Bull/Bear 标识+内容)"]
    judge_decision: Annotated[str, "研究经理裁决"]
    count: Annotated[int, "当前辩论发言次数 (达到 2*max_debate_rounds 结束)"]


# =====================================================================
# 风险辩论子状态 (PRD Phase 7 风控审批, 激进/保守/中立三风格)
# 复用 TradingAgents-CN RiskDebateState
# =====================================================================
class RiskDebateState(TypedDict):
    """风险辩论状态 (三风格辩论者)"""
    risky_history: Annotated[str, "激进辩论者发言历史"]
    safe_history: Annotated[str, "保守辩论者发言历史"]
    neutral_history: Annotated[str, "中立辩论者发言历史"]
    history: Annotated[str, "辩论总历史"]
    latest_speaker: Annotated[str, "最后发言者 (Risky/Safe/Neutral)"]
    current_risky_response: Annotated[str, "激进辩论者最新发言"]
    current_safe_response: Annotated[str, "保守辩论者最新发言"]
    current_neutral_response: Annotated[str, "中立辩论者最新发言"]
    judge_decision: Annotated[str, "风险经理裁决"]
    count: Annotated[int, "当前辩论发言次数 (达到 3*max_risk_discuss_rounds 结束)"]


# =====================================================================
# 全局状态 (PRD 全流程状态流转)
# =====================================================================
class AgentState(MessagesState):
    """短线推荐 Agent 全局状态

    包含从发现层到决策层的所有中间产出, 是各 Agent 节点间数据传递的载体。
    """

    # === 基础信息 ===
    trade_date: Annotated[str, "交易日期 (YYYY-MM-DD)"]
    user_config: Annotated[dict, "用户配置 (风险偏好/持仓周期/板块偏好, PRD Phase 0/M9)"]

    # === 发现层产出 (PRD Phase 1-3 / 8.1-8.4) ===
    # 热点话题: 含 source_type(news/social/policy), authority_level, impact_direction, impact_strength
    # 使用 operator.add reducer: 三路并行发现(新闻/情绪/政策)各自返回新话题, 框架自动拼接
    hot_topics: Annotated[list, operator.add]
    # 政策事件: 含 title/source/authority_level/affected_sectors/impact_direction/impact_strength/rationale
    policy_events: Annotated[list, "政策事件清单 (PRD 8.2, V2 新增)"]
    # 板块排名: 含 sector/heat_score/trend/policy_impact/rationale/components
    # V2: 含五维指标 (heat/diffusion/volatility/rebound/crowding) + 环比 (mom_change)
    ranked_sectors: Annotated[list, "板块排名 Top N (PRD 8.3, V2 含五维指标+环比)"]
    # V2 新增: Top 10 板块拥挤度地图 (含颜色梯度, 用于可视化输出)
    sector_crowding_map: Annotated[dict, "板块拥挤度地图 (Top 10, 含颜色梯度, V2 新增)"]

    # === 双路选股产出 (PRD 8.3 / 8.4 双路并行) ===
    # 采用分离状态字段: 主线和黑马各自单写, 避免并行写同一 key 触发 ConcurrentUpdateError。
    # Pool Merger 读取两路结果去重合并后写入 candidate_pool (单写者, 无需 reducer)。
    # 主线选股产出 (仅 Stock Selector 写, source=hot_sector, 5-8 只)
    hot_sector_candidates: Annotated[list, "主线选股候选 (PRD 8.4, source=hot_sector)"]
    # 黑马扫描产出 (仅 Dark Horse Scanner 写, source=dark_horse, 0-3 只)
    dark_horse_candidates: Annotated[list, "黑马扫描候选 (PRD 8.4, source=dark_horse)"]

    # 候选池: 含 ticker/name/sector/source(hot_sector/dark_horse)/role/trigger_signals/dark_horse_score/reason
    # 由 Pool Merger 单写 (合并双路 + 去重 + 熔断检查), 下游分析/决策层只读
    candidate_pool: Annotated[list, "候选个股池 7-11 只 (PRD 8.4)"]

    # === 分析层产出 (PRD Phase 4 / 8.5) ===
    # 结构: {ticker: {fundamentals, technical, china_specific, stock_development}}
    # 每维含: rating / confidence / react_iterations / tool_calls(日志) / key_metrics / risks
    analysis_reports: Annotated[dict, "四维分析报告 (PRD 8.5, V2 新增个股发展维度+工具日志)"]

    # ReAct 工具调用计数器 (防死循环, 复用 TA 模式)
    # 结构: {"ticker__analyst_name": count}
    react_tool_call_counts: Annotated[dict, "ReAct 工具调用计数器"]

    # === 决策层产出 (PRD Phase 5-7) ===
    investment_debate_state: Annotated[InvestDebateState, "投资辩论状态 (PRD Phase 5)"]
    investment_plan: Annotated[str, "研究经理裁决的投资计划 (PRD Phase 5)"]
    trader_investment_plan: Annotated[str, "交易代理决策 (PRD Phase 6)"]
    risk_debate_state: Annotated[RiskDebateState, "风险辩论状态 (PRD Phase 7)"]
    final_trade_decision: Annotated[str, "风险经理最终决策 (PRD Phase 7)"]
    # 最终推荐清单: 含 ticker/name/source/rating/target_price/stop_loss/take_profit/confidence/risk_level/tool_logs
    final_portfolio: Annotated[list, "最终推荐清单 (PRD Phase 8)"]

    # === 熔断标志 (PRD 10.2 极端行情) ===
    circuit_breaker: Annotated[bool, "大盘跌>4% 触发熔断暂停推荐"]
