"""
发现层模块 (对应 PRD Phase 1-3 / 8.1-8.4)

三路并行发现 → 板块推理 → 双路选股 → 候选池合并:
  - news_analyst       : 财经新闻分析师 (提取新闻热点, PRD M1)
  - sentiment_analyst  : 市场情绪分析师 (股吧/社媒情绪, PRD M1)
  - policy_analyst     : 政策新闻分析师 (V2 新增, 6 类权威信源, PRD 6.3)
  - sector_inference   : 板块推理器 (四维评分排名, PRD M2 / 7.2)
  - stock_selector     : 个股选取器主线 (热门板块选股, PRD M3 / 7.3)
  - dark_horse_scanner : 黑马扫描器支线 (V2 新增, 四维信号, PRD 6.6)
  - pool_merger        : 候选池合并器 (主线+黑马去重合并, PRD 7.3)
"""
