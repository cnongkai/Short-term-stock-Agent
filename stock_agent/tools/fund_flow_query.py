"""
资金流向查询工具 (对应 PRD 8.7 工具 6)

供 A 股专属分析师/黑马扫描器调用, 查询个股资金流向 (主力/超大单/大单/中单/小单)。
数据源: AkShare (主) → Tushare (备)。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_fund_flow_query_tool(config: dict = None):
    """创建资金流向查询工具"""
    from langchain_core.tools import tool

    @tool
    def fund_flow_query(ticker: str, days: int = 5) -> str:
        """查询 A 股个股资金流向 (主力净流入/超大单/大单/中单/小单)。

        Args:
            ticker: A 股股票代码, 如 "600584"
            days: 返回最近几天的资金流向, 默认 5

        Returns:
            JSON 字符串, 含资金流向数据
        """
        logger.info(f"[工具] fund_flow_query | ticker={ticker}, days={days}")
        try:
            result = data_interface.get_fund_flow(ticker)
            if not result or not result.get("data"):
                return json.dumps({"error": f"未获取到 {ticker} 的资金流向数据", "data": None}, ensure_ascii=False)

            # 截取最近 N 天
            data = result["data"]
            if isinstance(data, list) and len(data) > days:
                data = data[-days:]

            return json.dumps({
                "ticker": ticker,
                "data": data,
                "source": result.get("source"),
            }, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] fund_flow_query 失败: {e}")
            return json.dumps({"error": str(e), "data": None}, ensure_ascii=False)

    return fund_flow_query
