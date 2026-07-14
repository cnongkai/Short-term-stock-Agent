"""
风控审批模块 (对应 PRD Phase 7 / M7 / 6.9)

三风格风险辩论 → 风险经理裁决 → 组合经理聚合:
  - aggressive_debator  : 激进辩论者 (高收益视角)
  - conservative_debator: 保守辩论者 (本金安全视角)
  - neutral_debator     : 中立辩论者 (风险收益平衡)
  - risk_manager        : 风险经理 (裁决审批, PRD 10.2 不收敛默认拒绝)
  - portfolio_manager   : 组合经理 (聚合最终推荐清单, PRD Phase 8)

参考架构: TradingAgents-CN agents/risk_mgmt/
"""
