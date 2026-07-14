"""
行业对比工具 (对应 PRD 8.7 工具 7)

供基本面分析师调用, 对比目标股票与同行业公司的财务指标。
数据源: AkShare (财务数据) + 行业板块成分股。
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def _extract_industry_from_info(info) -> str:
    """从股票 info 字段中安全提取行业名称。

    防御性处理: info 可能是 dict / list / None / 其它类型, 统一安全提取。
    修复日志中反复出现的 'list' object has no attribute 'items' 错误。

    Args:
        info: get_stock_info() 返回的 info 字段 (dict / list / None)

    Returns:
        行业名称字符串, 提取失败返回 ""
    """
    if not info:
        return ""

    # 情况1: info 是 dict, 直接遍历键值
    if isinstance(info, dict):
        for key, val in info.items():
            key_str = str(key)
            if "行业" in key_str or "industry" in key_str.lower():
                val_str = str(val).strip()
                if val_str and val_str.lower() not in ("none", "nan", "null", ""):
                    return val_str
        return ""

    # 情况2: info 是 list, 遍历每个元素 (元素可能是 dict)
    if isinstance(info, list):
        for item in info:
            if isinstance(item, dict):
                industry = _extract_industry_from_info(item)
                if industry:
                    return industry
        return ""

    # 情况3: 其它类型, 直接转字符串尝试匹配
    info_str = str(info)
    if "行业" in info_str:
        # 简单提取, 不保证准确, 但避免崩溃
        return info_str
    return ""


def create_industry_comparison_tool(config: dict = None):
    """创建行业对比工具"""
    from langchain_core.tools import tool

    @tool
    def industry_comparison(ticker: str, peers: str = "", metric: str = "PE,PB,ROE") -> str:
        """对比目标股票与同行业公司的财务指标 (PE/PB/ROE 等)。

        Args:
            ticker: 目标 A 股股票代码, 如 "600584"
            peers: 对比的同行业股票代码, 逗号分隔, 如 "600585,002156" (空则自动获取同板块)
            metric: 对比指标, 逗号分隔, 默认 "PE,PB,ROE"

        Returns:
            JSON 字符串, 含目标股与对比股的指标数据
        """
        logger.info(f"[工具] industry_comparison | ticker={ticker}, peers={peers}, metric={metric}")
        try:
            # 确定对比股列表
            peer_list = [p.strip() for p in peers.split(",") if p.strip()] if peers else []

            # 如果未指定对比股, 尝试获取同板块成分股 (取前 5 只)
            if not peer_list:
                stock_info = data_interface.get_stock_info(ticker)
                # 从股票信息中提取行业 (如果可用)
                # 防御: info 可能是 dict / list / None, 统一安全提取
                industry = _extract_industry_from_info(
                    stock_info.get("info") if stock_info else None
                )
                if industry:
                    sector_stocks = data_interface.get_sector_stocks(industry)
                    if sector_stocks and sector_stocks.get("data"):
                        for stock in sector_stocks["data"][:6]:
                            # 防御: stock 可能不是 dict
                            if not isinstance(stock, dict):
                                continue
                            code = str(stock.get("代码", stock.get("symbol", stock.get("股票代码", ""))))
                            if code and code != ticker:
                                peer_list.append(code)
                            if len(peer_list) >= 5:
                                break

            # 如果仍未获取到对比股, 仅返回目标股数据
            all_tickers = [ticker] + peer_list[:5]

            # 批量获取财务数据
            comparisons = []
            for t in all_tickers:
                fin_data = data_interface.get_financial_data(t)
                if fin_data and fin_data.get("data"):
                    # 防御: data 可能不是 list 或为空
                    data_list = fin_data["data"]
                    if not isinstance(data_list, list) or not data_list:
                        comparisons.append({"ticker": t, "metrics": {}, "error": "无有效数据"})
                        continue
                    latest = data_list[0] if data_list else {}
                    # 防御: 仅处理 dict 格式
                    if not isinstance(latest, dict):
                        comparisons.append({"ticker": t, "metrics": {}, "error": "数据格式不支持"})
                        continue
                    # 过滤请求的指标
                    requested = [m.strip() for m in metric.split(",")] if metric else []
                    if requested:
                        filtered = {
                            k: v for k, v in latest.items()
                            if any(req.lower() in k.lower() for req in requested)
                        }
                        comparisons.append({"ticker": t, "metrics": filtered or latest})
                    else:
                        comparisons.append({"ticker": t, "metrics": latest})
                else:
                    comparisons.append({"ticker": t, "metrics": {}, "error": "无数据"})

            return json.dumps({
                "target": ticker,
                "metric": metric,
                "comparisons": comparisons,
                "source": "akshare",
            }, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] industry_comparison 失败: {e}")
            return json.dumps({"error": str(e), "data": None}, ensure_ascii=False)

    return industry_comparison
