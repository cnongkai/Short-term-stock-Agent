"""
工具模块 (对应 PRD M4c 分析师工具链 / 8.7 工具调用接口规范)

8 类分析师工具 + 注册中心:
  - tool_registry          : 工具注册中心 (懒加载 + 缓存)
  - financial_data_query   : 财务数据查询
  - announcement_search    : 公告检索 (巨潮)
  - news_search            : 新闻搜索
  - technical_indicator_calc: 技术指标计算
  - dragon_tiger_query     : 龙虎榜查询
  - fund_flow_query        : 资金流向查询
  - industry_comparison    : 行业对比
  - research_report_search : 研报检索
"""
