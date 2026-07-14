"""
分析层模块 (对应 PRD Phase 4 / 8.5, V2 核心创新)

四维 ReAct 分析师对候选池逐只深度分析:
  - react_loop              : 可复用 ReAct 迭代循环 (思考-行动-观察, PRD 6.7)
  - fundamentals_analyst    : 基本面分析师 (财务/估值, PRD M4)
  - technical_analyst       : 技术分析师 (趋势/指标/量价, PRD M4)
  - china_specific_analyst  : A 股专属分析师 (龙虎榜/北向/融资融券, PRD M4)
  - stock_development_analyst: 个股发展信息分析师 (V2 新增, 公告/研发/股东, PRD M4b / 7.5)
  - analysis_layer          : 分析层图节点 (遍历候选池聚合四维报告)
"""
