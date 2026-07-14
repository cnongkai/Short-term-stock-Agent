"""
研报检索工具 (对应 PRD 8.7 工具 8)

供基本面分析师/个股发展分析师调用, 检索券商研究报告。
数据源: AkShare 研报数据 (如可用) 或新闻降级。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_research_report_search_tool(config: dict = None):
    """创建研报检索工具"""
    from langchain_core.tools import tool

    @tool
    def research_report_search(keyword: str, count: int = 10) -> str:
        """检索券商研究报告 (个股研报/行业研报)。

        Args:
            keyword: 搜索关键词, 如股票代码 "600584" 或行业名 "半导体"
            count: 返回条数上限, 默认 10

        Returns:
            JSON 字符串, 含研报列表 (标题/摘要/评级/目标价)
        """
        logger.info(f"[工具] research_report_search | keyword={keyword}, count={count}")
        try:
            # 尝试获取 AkShare 研报数据
            reports = _fetch_research_reports(keyword, count)
            if reports:
                return json.dumps({"data": reports, "source": "akshare"}, ensure_ascii=False, default=str)

            # 降级: 使用新闻搜索代替
            logger.debug("[工具] 研报数据不可用, 降级为新闻搜索")
            news_result = data_interface.get_news(keyword=keyword, count=count)
            return json.dumps({
                "data": news_result.get("data", []),
                "source": "news_fallback",
                "note": "研报接口不可用, 降级为新闻搜索",
            }, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] research_report_search 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return research_report_search


def _fetch_research_reports(keyword: str, count: int) -> list:
    """从 AkShare 获取研报数据 (逐接口独立降级)"""
    from datetime import datetime, timedelta

    # 尝试个股研报 (仅当 keyword 为纯数字股票代码时)
    pure_code = keyword.strip().split()[0] if keyword.strip() else ""
    if pure_code.isdigit() and len(pure_code) == 6:
        try:
            import akshare as ak
            df = ak.stock_research_report_em(symbol=pure_code)
            if df is not None and not df.empty:
                records = df.head(count).to_dict("records")
                return [_format_report(r) for r in records]
        except Exception as e:
            logger.debug(f"[研报] 个股研报获取失败 ({pure_code}): {e}")

    # 降级: 财经新闻 (使用有效日期, 避免空字符串报错)
    try:
        import akshare as ak
        today = datetime.now().strftime("%Y%m%d")
        df = ak.news_cctv(date=today)
        if df is not None and not df.empty:
            records = df.head(count).to_dict("records")
            return [_format_report(r) for r in records]
    except Exception as e:
        logger.debug(f"[研报] 财经新闻降级失败: {e}")

    return []


def _format_report(record: dict) -> dict:
    """格式化研报记录"""
    return {
        "title": record.get("标题", record.get("title", record.get("research_report_title", ""))),
        "summary": record.get("摘要", record.get("content", record.get("research_report_abstract", ""))),
        "rating": record.get("评级", record.get("em_rating", "")),
        "target_price": record.get("目标价", record.get("em_target_price", "")),
        "date": record.get("日期", record.get("publish_date", "")),
        "source": "akshare",
    }
