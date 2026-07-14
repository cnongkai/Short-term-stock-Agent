"""
新闻搜索工具 (对应 PRD 8.7 工具 3)

供新闻分析师/情绪分析师/技术分析师/个股发展分析师调用, 搜索财经新闻。
数据源: AkShare 财经新闻 + 个股新闻。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_news_search_tool(config: dict = None):
    """创建新闻搜索工具"""
    from langchain_core.tools import tool

    @tool
    def news_search(keyword: str, count: int = 20, source_type: str = "general") -> str:
        """搜索财经新闻或个股新闻。

        Args:
            keyword: 搜索关键词或股票代码。传入股票代码(如"600584")搜索该股新闻;
                     传入关键词(如"半导体")搜索相关财经新闻。
            count: 返回条数上限, 默认 20
            source_type: 新闻类型, "general"(财经新闻) 或 "stock"(个股新闻)

        Returns:
            JSON 字符串, 含新闻列表
        """
        logger.info(f"[工具] news_search | keyword={keyword}, count={count}, type={source_type}")
        try:
            result = data_interface.get_news(keyword=keyword, count=count)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] news_search 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return news_search
