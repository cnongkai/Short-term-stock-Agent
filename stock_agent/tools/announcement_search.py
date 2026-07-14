"""
公告检索工具 (对应 PRD 8.7 工具 2 / 8.6 个股发展信息分析数据源)

供个股发展信息分析师调用, 检索巨潮资讯网公告 (重大合同/并购重组/业绩预告等)。
数据源: 巨潮资讯网 (cninfo.com.cn, 证监会指定披露平台)。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_announcement_search_tool(config: dict = None):
    """创建公告检索工具"""
    from langchain_core.tools import tool

    @tool
    def announcement_search(ticker: str, keyword: str = "", count: int = 10) -> str:
        """检索 A 股上市公司公告 (巨潮资讯网, 证监会指定披露平台)。

        可检索: 重大合同、并购重组、业绩预告、回购、股东增减持等公告。

        Args:
            ticker: A 股股票代码, 如 "600584"
            keyword: 搜索关键词, 如 "重大合同" (空则检索最新公告)
            count: 返回条数上限, 默认 10

        Returns:
            JSON 字符串, 含公告列表 (title/pub_time/url/type)
        """
        logger.info(f"[工具] announcement_search | ticker={ticker}, keyword={keyword}")
        try:
            result = data_interface.search_announcements(ticker, keyword, count)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] announcement_search 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return announcement_search
