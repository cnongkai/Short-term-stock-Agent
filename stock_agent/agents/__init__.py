"""
Agent 模块 (对应 PRD 第6章核心流程设计)

按三层架构组织:
  - discovery/  : 发现层 (热点发现 → 板块推理 → 双路选股, PRD Phase 1-3)
  - analysis/   : 分析层 (四维 ReAct 分析, PRD Phase 4)
  - researchers/: 研究辩论 (多空辩论, PRD Phase 5)
  - trader/     : 交易代理 (PRD Phase 6)
  - risk_mgmt/  : 风控审批 (三风格辩论 + 风险经理 + 组合经理, PRD Phase 7)
  - output/     : 输出层 (报告生成, PRD Phase 8)
  - utils/      : 公共组件 (状态定义 / 工具包 / 提示词 / 日志 / ReAct循环)

参考架构: TradingAgents-CN agents/
"""

from stock_agent.agents.utils.agent_utils import Toolkit, create_msg_delete

__all__ = ["Toolkit", "create_msg_delete"]
