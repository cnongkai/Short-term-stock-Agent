"""
短线股票推荐 Agent - 核心包

基于 LangGraph 多智能体的 A 股短线选股系统, 对应 PRD V2。
架构参考 TradingAgents-CN, 在其基础上扩展为多标的推荐系统。

三层架构:
  - 发现层 (Phase 1-3): 新闻/情绪/政策三路并行发现 → 板块推理 → 双路选股
  - 分析层 (Phase 4):   四维 ReAct 深度分析 (基本面/技术/A股专属/个股发展)
  - 决策层 (Phase 5-7): 多空辩论 → 交易决策 → 三风格风控 → 组合审批

合规声明: 本系统定位为"信息聚合与分析工具", 不构成投资建议、不构成荐股。
"""
__version__ = "2.0.0"
__author__ = "Short-Term Stock Agent Team"
__description__ = "多智能体 A 股短线选股系统 (PRD V2)"
