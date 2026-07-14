"""
技术指标计算工具 (对应 PRD 8.7 工具 4)

供技术分析师调用, 获取股票行情数据并计算技术指标 (MACD/RSI/KDJ/BOLL/均线等)。
数据源: AkShare (主) → BaoStock/Tushare (备), 行情数据 + stockstats 指标计算。

优化点 (参考 docs/analysis/market_analyst_technical_analysis_issue.md):
  1. 指标解读: 不只返回原始数值, 同时返回趋势信号 (金叉/死叉/超买/超卖/多空排列)
  2. 数据充足性: 默认获取 90 天数据 (60日均线计算需要60+天), 展示最近 5 天
  3. 降级计算: stockstats 不可用时, 手动计算 MA/MACD/RSI/BOLL
  4. 防御性处理: 兼容 dict/list 等多种数据格式
"""
import json
from loguru import logger

from stock_agent.dataflows import interface as data_interface


def create_technical_indicator_calc_tool(config: dict = None):
    """创建技术指标计算工具"""
    from langchain_core.tools import tool

    @tool
    def technical_indicator_calc(ticker: str, indicators: str = "MACD,RSI,KDJ,BOLL", period: int = 90) -> str:
        """获取 A 股股票行情并计算技术指标 (MACD/RSI/KDJ/BOLL/均线系统)。

        会返回原始指标数值 + 趋势信号解读 (金叉/死叉/超买/超卖)。
        数据充足性: 默认获取90天数据用于指标计算, 展示最近5天行情。

        Args:
            ticker: A 股股票代码, 如 "600584"
            indicators: 需要的指标, 逗号分隔, 默认 "MACD,RSI,KDJ,BOLL"
            period: 获取行情的天数, 默认 90 (60日均线计算需要足够历史数据)

        Returns:
            JSON 字符串, 含行情数据 + 计算后的技术指标 + 趋势信号解读
        """
        logger.info(f"[工具] technical_indicator_calc | ticker={ticker}, indicators={indicators}, period={period}")
        try:
            from datetime import datetime, timedelta

            # 计算日期范围 (多取30天确保有足够数据计算指标, 参考技术分析issue文档)
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=period + 60)).strftime("%Y-%m-%d")

            # 获取行情数据
            stock_data = data_interface.get_stock_data(ticker, start_date, end_date)
            if not stock_data or not stock_data.get("data"):
                return json.dumps({"error": f"未获取到 {ticker} 的行情数据", "data": None}, ensure_ascii=False)

            data_list = stock_data["data"]
            # 防御: 仅保留 dict 格式记录
            if not isinstance(data_list, list):
                return json.dumps({"error": "行情数据格式异常", "data": None}, ensure_ascii=False)

            # 计算技术指标 (使用 stockstats 或手动计算)
            indicator_result = _calc_indicators(data_list, indicators)

            # 生成趋势信号解读 (参考技术分析issue文档: 提供解读而非让模型猜测)
            signal_interpretation = _interpret_signals(indicator_result)

            result = {
                "ticker": ticker,
                "price_data": data_list[-5:],  # 最近 5 天行情 (参考issue文档: 只展示3-5天)
                "indicators": indicator_result,
                "signals": signal_interpretation,
                "data_count": len(data_list),
                "source": stock_data.get("source", "unknown"),
            }
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] technical_indicator_calc 失败: {e}")
            return json.dumps({"error": str(e), "data": None}, ensure_ascii=False)

    return technical_indicator_calc


def _calc_indicators(price_data: list, indicators: str) -> dict:
    """计算技术指标 (优先用 stockstats, 失败则手动计算简单指标)"""
    requested = [i.strip().upper() for i in indicators.split(",")] if indicators else []
    result = {}

    # 尝试用 stockstats 计算完整指标
    try:
        import pandas as pd
        from stockstats import StockDataFrame

        df = pd.DataFrame(price_data)
        # stockstats 需要标准列名
        col_map = {}
        for col in df.columns:
            col_str = str(col)
            col_lower = col_str.lower()
            if "开盘" in col_str or col_lower in ("open",):
                col_map[col] = "open"
            elif "最高" in col_str or col_lower in ("high",):
                col_map[col] = "high"
            elif "最低" in col_str or col_lower in ("low",):
                col_map[col] = "low"
            elif "收盘" in col_str or col_lower in ("close",):
                col_map[col] = "close"
            elif "成交量" in col_str or col_lower in ("volume", "vol"):
                col_map[col] = "volume"
        df = df.rename(columns=col_map)

        # 强制将 OHLCV 列转为数值类型 (数据源可能返回字符串, 导致 stockstats
        # RSI/KDJ 计算时 'str' - 'str' 异常; errors='coerce' 将非数值转为 NaN)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 丢弃含 NaN 的行 (保证指标计算正确, 至少需要 close 非空)
        if "close" in df.columns:
            df = df.dropna(subset=["close"]).reset_index(drop=True)

        required = {"open", "high", "low", "close", "volume"}
        if required.issubset(set(df.columns)) and len(df) > 0:
            sdf = StockDataFrame.retype(df)
            for ind in requested:
                try:
                    if ind == "MACD":
                        result["macd"] = _safe_round(sdf["macd"].iloc[-1])
                        result["macds"] = _safe_round(sdf["macds"].iloc[-1])
                        # MACD 柱状图变化 (金叉/死叉判断)
                        if len(sdf) >= 2:
                            result["macd_prev"] = _safe_round(sdf["macd"].iloc[-2])
                    elif ind == "RSI":
                        result["rsi_14"] = _safe_round(sdf["rsi_14"].iloc[-1])
                    elif ind == "KDJ":
                        result["kdj_k"] = _safe_round(sdf["kdjk"].iloc[-1])
                        result["kdj_d"] = _safe_round(sdf["kdjd"].iloc[-1])
                        result["kdj_j"] = _safe_round(sdf["kdjj"].iloc[-1])
                    elif ind == "BOLL":
                        result["boll_ub"] = _safe_round(sdf["boll_ub"].iloc[-1])
                        result["boll_lb"] = _safe_round(sdf["boll_lb"].iloc[-1])
                        result["boll_mid"] = _safe_round(sdf["boll"].iloc[-1]) if "boll" in sdf else None
                except Exception as e:
                    logger.debug(f"[技术指标] {ind} 计算异常: {e}")

            # 均线 (始终计算, MA60 需要60+天数据)
            if "close" in df.columns:
                close = df["close"]
                result["ma5"] = _safe_round(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
                result["ma10"] = _safe_round(close.rolling(10).mean().iloc[-1]) if len(close) >= 10 else None
                result["ma20"] = _safe_round(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
                result["ma60"] = _safe_round(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else None

            return result
    except ImportError:
        logger.debug("[技术指标] stockstats 未安装, 使用手动计算")
    except Exception as e:
        logger.debug(f"[技术指标] stockstats 计算失败: {e}")

    # 降级: 手动计算均线 + MACD/RSI/BOLL (参考技术分析issue文档代码示例)
    return _calc_indicators_manual(price_data, requested)


def _calc_indicators_manual(price_data: list, requested: list) -> dict:
    """手动计算技术指标 (stockstats 不可用时的降级方案)"""
    result = {}
    try:
        closes = []
        highs = []
        lows = []
        for record in price_data:
            # 兼容 dict 和 list 两种格式
            if not isinstance(record, dict):
                continue
            for key in ("收盘", "close", "Close"):
                if key in record:
                    closes.append(_safe_float(record[key]))
                    break
            for key in ("最高", "high", "High"):
                if key in record:
                    highs.append(_safe_float(record[key]))
                    break
            for key in ("最低", "low", "Low"):
                if key in record:
                    lows.append(_safe_float(record[key]))
                    break

        if not closes:
            return result

        # 均线
        result["ma5"] = round(sum(closes[-5:]) / 5, 4) if len(closes) >= 5 else None
        result["ma10"] = round(sum(closes[-10:]) / 10, 4) if len(closes) >= 10 else None
        result["ma20"] = round(sum(closes[-20:]) / 20, 4) if len(closes) >= 20 else None
        result["ma60"] = round(sum(closes[-60:]) / 60, 4) if len(closes) >= 60 else None
        result["latest_close"] = closes[-1]

        # MACD (12/26/9)
        if "MACD" in requested and len(closes) >= 35:
            ema12 = _ema(closes, 12)
            ema26 = _ema(closes, 26)
            dif = [a - b for a, b in zip(ema12, ema26)]
            dea = _ema(dif, 9)
            macd = [(d - e) * 2 for d, e in zip(dif, dea)]
            result["macd"] = round(macd[-1], 4)
            result["macds"] = round(dea[-1], 4)
            if len(macd) >= 2:
                result["macd_prev"] = round(macd[-2], 4)

        # RSI (14)
        if "RSI" in requested and len(closes) >= 15:
            rsi = _calc_rsi(closes, 14)
            result["rsi_14"] = round(rsi, 4) if rsi else None

        # BOLL (20, 2)
        if "BOLL" in requested and len(closes) >= 20:
            mid = sum(closes[-20:]) / 20
            variance = sum((c - mid) ** 2 for c in closes[-20:]) / 20
            std = variance ** 0.5
            result["boll_mid"] = round(mid, 4)
            result["boll_ub"] = round(mid + 2 * std, 4)
            result["boll_lb"] = round(mid - 2 * std, 4)

    except Exception as e:
        logger.debug(f"[技术指标] 手动计算失败: {e}")

    return result


def _interpret_signals(indicators: dict) -> dict:
    """解读技术指标信号 (参考技术分析issue文档: 提供解读而非让模型猜测)

    Args:
        indicators: 计算出的技术指标字典

    Returns:
        趋势信号解读, 包含各指标的状态和综合判断
    """
    signals = {"details": [], "overall_trend": "未知"}

    try:
        ma5 = indicators.get("ma5")
        ma10 = indicators.get("ma10")
        ma20 = indicators.get("ma20")
        ma60 = indicators.get("ma60")
        latest = indicators.get("latest_close")

        # 均线多空排列判断
        if all(v is not None for v in [ma5, ma10, ma20]):
            if ma5 > ma10 > ma20:
                signals["details"].append("均线多头排列 (MA5>MA10>MA20), 短期趋势向上")
            elif ma5 < ma10 < ma20:
                signals["details"].append("均线空头排列 (MA5<MA10<MA20), 短期趋势向下")
            else:
                signals["details"].append("均线交织, 趋势不明朗")

            # 60日均线支撑/压力
            if ma60 and latest:
                if latest > ma60:
                    signals["details"].append(f"股价({latest})站上60日均线({ma60}), 中期趋势偏多")
                else:
                    signals["details"].append(f"股价({latest})跌破60日均线({ma60}), 中期趋势偏空")

        # MACD 金叉/死叉
        macd = indicators.get("macd")
        macds = indicators.get("macds")
        macd_prev = indicators.get("macd_prev")
        if macd is not None and macds is not None:
            if macd > macds:
                signals["details"].append(f"MACD柱状图为正 (DIF={macd} > DEA={macds}), 多头动能")
                # 判断是否刚金叉
                if macd_prev is not None and macd_prev <= macds:
                    signals["details"].append("🔔 MACD 刚形成金叉, 买入信号")
            else:
                signals["details"].append(f"MACD柱状图为负 (DIF={macd} < DEA={macds}), 空头动能")
                if macd_prev is not None and macd_prev >= macds:
                    signals["details"].append("⚠️ MACD 刚形成死叉, 卖出信号")

        # RSI 超买/超卖
        rsi = indicators.get("rsi_14")
        if rsi is not None:
            if rsi > 70:
                signals["details"].append(f"RSI={rsi} 超买 (>70), 注意回调风险")
            elif rsi < 30:
                signals["details"].append(f"RSI={rsi} 超卖 (<30), 关注反弹机会")
            else:
                signals["details"].append(f"RSI={rsi} 中性区间 (30-70)")

        # KDJ 信号
        kdj_k = indicators.get("kdj_k")
        kdj_j = indicators.get("kdj_j")
        if kdj_k is not None and kdj_j is not None:
            if kdj_j > 100:
                signals["details"].append(f"KDJ-J={kdj_j} 超买 (>100), 短期见顶风险")
            elif kdj_j < 0:
                signals["details"].append(f"KDJ-J={kdj_j} 超卖 (<0), 短期反弹机会")

        # BOLL 位置
        boll_ub = indicators.get("boll_ub")
        boll_lb = indicators.get("boll_lb")
        if boll_ub and boll_lb and latest:
            boll_pct = (latest - boll_lb) / (boll_ub - boll_lb) * 100 if boll_ub != boll_lb else 50
            if boll_pct > 80:
                signals["details"].append(f"股价接近布林带上轨 (位置{boll_pct:.0f}%), 短期偏强")
            elif boll_pct < 20:
                signals["details"].append(f"股价接近布林带下轨 (位置{boll_pct:.0f}%), 短期偏弱")
            else:
                signals["details"].append(f"股价处于布林带中段 (位置{boll_pct:.0f}%)")

        # 综合趋势判断
        bullish_count = sum(1 for d in signals["details"] if any(k in d for k in ["多头", "金叉", "偏多", "站上", "反弹"]))
        bearish_count = sum(1 for d in signals["details"] if any(k in d for k in ["空头", "死叉", "偏空", "跌破", "超买", "见顶"]))
        if bullish_count > bearish_count:
            signals["overall_trend"] = "偏多"
        elif bearish_count > bullish_count:
            signals["overall_trend"] = "偏空"
        else:
            signals["overall_trend"] = "中性"

    except Exception as e:
        logger.debug(f"[技术指标] 信号解读失败: {e}")

    return signals


# =================================================================
# 辅助计算函数
# =================================================================
def _safe_float(val):
    """安全转 float"""
    try:
        if val is None or val == "":
            return None
        f = float(val)
        if f != f:  # NaN
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_round(val, digits=4):
    """安全四舍五入"""
    f = _safe_float(val)
    return round(f, digits) if f is not None else None


def _ema(data: list, period: int) -> list:
    """计算指数移动平均 (EMA)"""
    if not data or period <= 0:
        return []
    k = 2 / (period + 1)
    ema = [data[0]]
    for i in range(1, len(data)):
        ema.append(data[i] * k + ema[-1] * (1 - k))
    return ema


def _calc_rsi(closes: list, period: int = 14) -> float:
    """计算 RSI 指标"""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # 取最近 period 期
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
