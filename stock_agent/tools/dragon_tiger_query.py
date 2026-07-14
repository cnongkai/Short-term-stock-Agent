"""
龙虎榜查询工具 (对应 PRD 8.7 工具 5)

供 A 股专属分析师调用, 查询龙虎榜数据 (机构席位/游资动向)。
数据源: AkShare (主) → Tushare (备)。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_dragon_tiger_query_tool(config: dict = None):
    """创建龙虎榜查询工具"""
    from langchain_core.tools import tool

    @tool
    def dragon_tiger_query(ticker: str, date: str = "") -> str:
        """查询 A 股龙虎榜数据 (机构席位买卖动向、游资接力情况)。

        Args:
            ticker: A 股股票代码, 如 "600584" (用于过滤该股的龙虎榜记录)
            date: 交易日期 (YYYY-MM-DD), 空则用今天

        Returns:
            JSON 字符串, 含龙虎榜数据 (过滤为指定股票的记录)
        """
        if not date:
            from datetime import datetime
            date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"[工具] dragon_tiger_query | ticker={ticker}, date={date}")
        try:
            result = data_interface.get_dragon_tiger(date)
            if not result or not result.get("data"):
                return json.dumps({"error": f"未获取到 {date} 的龙虎榜数据", "data": []}, ensure_ascii=False)

            # 过滤指定股票的龙虎榜记录
            all_records = result["data"]
            filtered = []
            for record in all_records:
                # 龙虎榜数据中股票代码字段可能叫 "代码" 或 "symbol"
                record_ticker = str(record.get("代码", record.get("symbol", "")))
                if ticker in record_ticker:
                    filtered.append(record)

            return json.dumps({
                "data": filtered if filtered else all_records[:10],  # 无匹配则返回前10条
                "filter_ticker": ticker,
                "source": result.get("source"),
            }, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] dragon_tiger_query 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return dragon_tiger_query
