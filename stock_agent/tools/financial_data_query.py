"""
财务数据查询工具 (对应 PRD 8.7 工具 1)

供基本面分析师调用, 查询 A 股财务指标 (ROE/PE/PB/营收/净利润等)。
数据源: AkShare (主) → BaoStock/Tushare (备), 通过 dataflows 接口调用。

优化点 (参考 docs/analysis/pe-pb-data-update-analysis.md):
  1. 实时 PE/PB 计算: 基于最新价格 × 总股本 / 净利润(净资产), 而非滞后的 daily_basic 数据
  2. 数据质量校验: PE 范围 -100~1000, PB 范围 0.1~100, 异常值标记
  3. 数据来源标注: 区分 "realtime"(实时计算) 与 "daily_basic"(静态)
  4. 防御性处理: 兼容 dict/list/None 等多种数据格式, 避免崩溃
"""
import json
from datetime import datetime, timedelta
from loguru import logger

from stock_agent.dataflows import interface as data_interface

# PE/PB 合理范围 (参考 pe-pb-data-update-analysis.md 数据质量验证)
_PE_VALID_RANGE = (-100, 1000)
_PB_VALID_RANGE = (0.1, 100)


def _safe_float(val, default=None):
    """安全转换为 float, 失败返回 default"""
    if val is None or val == "":
        return default
    try:
        f = float(val)
        # 排除 NaN/Inf
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _find_field(record: dict, candidates: list) -> float:
    """从记录中按候选字段名模糊匹配提取数值 (兼容中文字段名差异)。

    Args:
        record: 财务数据记录 (dict)
        candidates: 候选字段名列表, 如 ["净利润", "net_profit", "归属净利润"]

    Returns:
        匹配到的字段值 (float), 未匹配返回 None
    """
    if not isinstance(record, dict):
        return None
    for key, val in record.items():
        key_str = str(key)
        for cand in candidates:
            if cand in key_str or cand.lower() in key_str.lower():
                return _safe_float(val)
    return None


def _calc_realtime_pe_pb(ticker: str) -> dict:
    """基于实时价格和财务数据计算实时 PE/PB (参考 pe-pb-data-update-analysis.md)。

    计算公式:
        PE = 最新价格 × 总股本 / 净利润
        PB = 最新价格 × 总股本 / 净资产

    优势: 数据实时性从"每日"提升到"分钟级", 盘中分析使用最新价格计算。

    Args:
        ticker: A 股股票代码

    Returns:
        {
            "pe": 22.5, "pb": 3.2,
            "price": 11.0, "market_cap": 1100000000,
            "source": "realtime_calculated", "is_realtime": True,
            "updated_at": "2026-07-09T10:30:00"
        }
        计算失败返回 {}
    """
    try:
        # 1. 获取最新价格 (近 5 日行情取最新收盘价)
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        stock_data = data_interface.get_stock_data(ticker, start_date, end_date)
        latest_price = None
        if stock_data and stock_data.get("data"):
            data_list = stock_data["data"]
            if isinstance(data_list, list) and data_list:
                latest = data_list[-1]
                if isinstance(latest, dict):
                    # 兼容多种字段名
                    latest_price = (
                        _find_field(latest, ["收盘", "close"])
                        or _find_field(latest, ["最新", "price"])
                    )
        if not latest_price:
            logger.debug(f"[实时PE/PB] {ticker} 未获取到最新价格")
            return {}

        # 2. 获取总股本 (从股票基本信息)
        stock_info = data_interface.get_stock_info(ticker)
        total_shares = None
        if stock_info and stock_info.get("info"):
            info = stock_info["info"]
            if isinstance(info, dict):
                total_shares = _find_field(info, ["总股本", "total_share", "总股本(股)"])
        if not total_shares:
            logger.debug(f"[实时PE/PB] {ticker} 未获取到总股本")
            return {}

        # 3. 获取净利润和净资产 (从财务数据)
        fin_data = data_interface.get_financial_data(ticker)
        net_profit = None
        net_assets = None
        if fin_data and fin_data.get("data"):
            data_list = fin_data["data"]
            if isinstance(data_list, list) and data_list:
                latest_fin = data_list[0]
                if isinstance(latest_fin, dict):
                    net_profit = _find_field(
                        latest_fin,
                        ["净利润", "net_profit", "归属净利润", "归母净利润"],
                    )
                    net_assets = _find_field(
                        latest_fin,
                        ["净资产", "net_assets", "股东权益", "所有者权益", "归属净资产"],
                    )

        # 4. 计算实时市值 (单位: 元)
        # 注意: 总股本可能以"万股"为单位, 需要判断
        # AkShare stock_individual_info_em 返回的总股本单位通常为"股"
        realtime_market_cap = latest_price * total_shares

        # 5. 计算实时 PE
        pe = None
        if net_profit and net_profit > 0:
            pe = realtime_market_cap / net_profit
            # 单位对齐: 若 PE 明显偏大 (如 >10000), 可能是净利润单位为"万元", 需要乘 10000
            if pe > 10000 and net_profit < 1e6:
                pe = realtime_market_cap / (net_profit * 10000)

        # 6. 计算实时 PB
        pb = None
        if net_assets and net_assets > 0:
            pb = realtime_market_cap / net_assets
            # 单位对齐: 若 PB 明显偏大, 可能是净资产单位为"万元"
            if pb > 1000 and net_assets < 1e6:
                pb = realtime_market_cap / (net_assets * 10000)

        # 7. 数据质量校验 (参考 pe-pb-data-update-analysis.md)
        quality_warnings = []
        if pe is not None and not (_PE_VALID_RANGE[0] <= pe <= _PE_VALID_RANGE[1]):
            quality_warnings.append(f"PE={pe:.2f} 超出合理范围{_PE_VALID_RANGE}")
            logger.warning(f"[实时PE/PB] {ticker} {quality_warnings[-1]}")
        if pb is not None and not (_PB_VALID_RANGE[0] <= pb <= _PB_VALID_RANGE[1]):
            quality_warnings.append(f"PB={pb:.2f} 超出合理范围{_PB_VALID_RANGE}")
            logger.warning(f"[实时PE/PB] {ticker} {quality_warnings[-1]}")

        result = {
            "pe": round(pe, 2) if pe is not None else None,
            "pb": round(pb, 2) if pb is not None else None,
            "price": latest_price,
            "market_cap": round(realtime_market_cap, 2),
            "source": "realtime_calculated",
            "is_realtime": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "基于实时价格和最新财报计算",
        }
        if quality_warnings:
            result["quality_warnings"] = quality_warnings
        return result
    except Exception as e:
        logger.warning(f"[实时PE/PB] {ticker} 计算失败: {e}")
        return {}


def create_financial_data_query_tool(config: dict = None):
    """创建财务数据查询工具 (langchain Tool)

    Args:
        config: 系统配置

    Returns:
        langchain Tool 实例
    """
    from langchain_core.tools import tool

    @tool
    def financial_data_query(ticker: str, indicators: str = "") -> str:
        """查询 A 股上市公司财务数据 (ROE/PE/PB/营收/净利润/毛利率等)。

        会自动计算实时 PE/PB (基于最新价格), 数据时效性优于静态 daily_basic。

        Args:
            ticker: A 股股票代码, 如 "600584" (不带前缀)
            indicators: 需要的指标, 逗号分隔, 如 "ROE,PE,PB" (空则返回全部)

        Returns:
            JSON 字符串, 含财务指标数据 + 实时 PE/PB
        """
        logger.info(f"[工具] financial_data_query | ticker={ticker}, indicators={indicators}")
        try:
            result = data_interface.get_financial_data(ticker)
            if not result or not result.get("data"):
                return json.dumps(
                    {"error": f"未获取到 {ticker} 的财务数据", "data": None},
                    ensure_ascii=False,
                )

            # 如果指定了指标, 过滤 (仅处理 dict 格式记录)
            if indicators:
                requested = [i.strip() for i in indicators.split(",")]
                filtered = []
                for record in result["data"]:
                    # 防御: 仅处理 dict 格式
                    if not isinstance(record, dict):
                        continue
                    filtered_record = {
                        k: v
                        for k, v in record.items()
                        if any(req.lower() in k.lower() for req in requested)
                    }
                    if filtered_record:
                        filtered.append(filtered_record)
                result["data"] = filtered if filtered else result["data"]

            # 实时 PE/PB 计算 (参考 pe-pb-data-update-analysis.md)
            realtime_metrics = _calc_realtime_pe_pb(ticker)
            if realtime_metrics:
                result["realtime_pe_pb"] = realtime_metrics
                logger.info(
                    f"[工具] {ticker} 实时PE={realtime_metrics.get('pe')}, "
                    f"PB={realtime_metrics.get('pb')}, "
                    f"价格={realtime_metrics.get('price')}"
                )
            else:
                # 降级: 实时计算失败, 标注使用静态数据
                result["realtime_pe_pb"] = {
                    "source": "daily_basic",
                    "is_realtime": False,
                    "note": "实时计算失败, 使用财务数据中的静态 PE/PB",
                }

            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] financial_data_query 失败: {e}")
            return json.dumps({"error": str(e), "data": None}, ensure_ascii=False)

    return financial_data_query
