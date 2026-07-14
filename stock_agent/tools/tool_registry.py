"""
工具注册中心 (对应 PRD M4c 分析师工具链 / 8.7 工具调用接口规范)

懒加载 + 缓存 8 类 langchain Tool 实例, 供 Toolkit.get_tool(name) 调用。
每个工具用 @tool 装饰器定义为 langchain Tool, 内部调用 dataflows 接口。

工具清单 (PRD 8.7):
  1. financial_data_query      - 财务数据查询
  2. announcement_search       - 公告检索 (巨潮)
  3. news_search               - 新闻搜索
  4. technical_indicator_calc  - 技术指标计算
  5. dragon_tiger_query        - 龙虎榜查询
  6. fund_flow_query           - 资金流向查询
  7. industry_comparison       - 行业对比
  8. research_report_search    - 研报检索
"""
from typing import Any, Dict
from loguru import logger


class ToolRegistry:
    """工具注册中心 (懒加载 + 缓存)"""

    # 工具名 → 创建函数的映射 (懒加载, 避免循环导入)
    _TOOL_FACTORIES = None

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._cache: Dict[str, Any] = {}  # 工具名 → Tool 实例缓存

    @classmethod
    def _get_factories(cls):
        """懒加载工具工厂映射 (首次调用时导入各工具模块)"""
        if cls._TOOL_FACTORIES is None:
            from stock_agent.tools.financial_data_query import create_financial_data_query_tool
            from stock_agent.tools.announcement_search import create_announcement_search_tool
            from stock_agent.tools.news_search import create_news_search_tool
            from stock_agent.tools.technical_indicator_calc import create_technical_indicator_calc_tool
            from stock_agent.tools.dragon_tiger_query import create_dragon_tiger_query_tool
            from stock_agent.tools.fund_flow_query import create_fund_flow_query_tool
            from stock_agent.tools.industry_comparison import create_industry_comparison_tool
            from stock_agent.tools.research_report_search import create_research_report_search_tool
            # V2 新增: 独立于 provider 链的自救工具 (联网搜索 + 实时行情)
            from stock_agent.tools.web_search import create_web_search_tool
            from stock_agent.tools.realtime_quote import create_realtime_quote_tool

            cls._TOOL_FACTORIES = {
                "financial_data_query": create_financial_data_query_tool,
                "announcement_search": create_announcement_search_tool,
                "news_search": create_news_search_tool,
                "technical_indicator_calc": create_technical_indicator_calc_tool,
                "dragon_tiger_query": create_dragon_tiger_query_tool,
                "fund_flow_query": create_fund_flow_query_tool,
                "industry_comparison": create_industry_comparison_tool,
                "research_report_search": create_research_report_search_tool,
                # V2 新增: 自救工具 (不依赖 AkShare, 直接 HTTP 调用)
                "web_search": create_web_search_tool,
                "realtime_quote": create_realtime_quote_tool,
            }
        return cls._TOOL_FACTORIES

    def get_tool(self, name: str) -> Any:
        """获取工具实例 (懒加载 + 缓存)

        Args:
            name: 工具名称 (如 "financial_data_query")

        Returns:
            langchain Tool 实例
        """
        # 缓存命中
        if name in self._cache:
            return self._cache[name]

        # 懒加载创建
        factories = self._get_factories()
        factory = factories.get(name)
        if factory is None:
            logger.error(f"[工具注册] 未知工具名: {name}")
            raise ValueError(f"未知工具: {name}")

        tool = factory(self.config)
        self._cache[name] = tool
        logger.debug(f"[工具注册] 创建工具: {name}")
        return tool

    def get_all_tools(self) -> list:
        """获取全部 8 个工具实例"""
        return [self.get_tool(name) for name in self._get_factories().keys()]
