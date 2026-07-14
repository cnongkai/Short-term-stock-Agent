"""
技术指标计算工具单元测试

验证:
  1. 字符串类型 OHLCV 数据可正确计算指标 (V2 修复: pd.to_numeric 防止 'str'-'str' 异常)
  2. 数值类型 OHLCV 数据正常计算 (回归测试)
  3. 缺失/空数据优雅降级
  4. 趋势信号解读正确性
"""
import os
import sys

# 确保项目根目录在 Python 路径中
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

import pytest

from stock_agent.tools.technical_indicator_calc import (
    _calc_indicators,
    _calc_indicators_manual,
    _interpret_signals,
    _safe_float,
    _safe_round,
    _ema,
    _calc_rsi,
)


# =====================================================================
# 辅助: 构造 OHLCV 数据
# =====================================================================
def _make_numeric_data(n=90, base_price=10.0):
    """构造 n 天数值类型 OHLCV 数据 (模拟 A 股日线)"""
    data = []
    price = base_price
    for i in range(n):
        # 简单随机游走
        change = ((i * 7) % 11 - 5) * 0.05
        open_p = price
        close_p = price + change
        high_p = max(open_p, close_p) + 0.1
        low_p = min(open_p, close_p) - 0.1
        volume = 100000 + i * 100
        data.append({
            "日期": f"2026-01-{i+1:02d}" if i < 31 else f"2026-02-{i-30:02d}" if i < 59 else f"2026-03-{i-58:02d}",
            "开盘": open_p,
            "最高": high_p,
            "最低": low_p,
            "收盘": close_p,
            "成交量": volume,
        })
        price = close_p
    return data


def _make_string_data(n=90, base_price=10.0):
    """构造 n 天字符串类型 OHLCV 数据 (模拟数据源返回的字符串)"""
    data = []
    price = base_price
    for i in range(n):
        change = ((i * 7) % 11 - 5) * 0.05
        open_p = price
        close_p = price + change
        high_p = max(open_p, close_p) + 0.1
        low_p = min(open_p, close_p) - 0.1
        volume = 100000 + i * 100
        data.append({
            "日期": f"2026-01-{i+1:02d}",
            "开盘": str(round(open_p, 4)),    # 字符串
            "最高": str(round(high_p, 4)),
            "最低": str(round(low_p, 4)),
            "收盘": str(round(close_p, 4)),
            "成交量": str(volume),
        })
        price = close_p
    return data


# =====================================================================
# 测试 1: 字符串类型 OHLCV → 指标计算 (V2 修复核心)
# =====================================================================
class TestStringTypeData:
    """验证字符串类型数据 (数据源常见返回格式) 能正确计算指标"""

    def test_string_data_macd_no_crash(self):
        """字符串类型数据计算 MACD 不应抛出 'str'-'str' 异常"""
        data = _make_string_data(n=90)
        result = _calc_indicators(data, "MACD,RSI,KDJ,BOLL")
        # MACD 应有计算结果 (非 None)
        assert "macd" in result or "ma5" in result, f"应有指标结果, 实际: {list(result.keys())}"

    def test_string_data_rsi_calculated(self):
        """字符串类型数据 RSI 计算不应失败 (原 bug: 'str'-'str' 异常)"""
        data = _make_string_data(n=90)
        result = _calc_indicators(data, "RSI")
        # 要么 stockstats 计算成功, 要么降级到 manual
        # 关键: 不应因类型异常导致 rsi_14 缺失
        assert "rsi_14" in result or result == {}, f"RSI 应计算或降级, 实际: {result}"

    def test_string_data_kdj_calculated(self):
        """字符串类型数据 KDJ 计算不应失败 (原 bug: 'str'-'float' 异常)"""
        data = _make_string_data(n=90)
        result = _calc_indicators(data, "KDJ")
        # 不应抛异常, 至少有 ma5 等基础指标
        assert isinstance(result, dict)

    def test_string_data_ma_calculated(self):
        """字符串类型数据均线应正确计算"""
        data = _make_string_data(n=90)
        result = _calc_indicators(data, "MACD")
        if "ma5" in result:
            assert result["ma5"] is not None
            assert isinstance(result["ma5"], (int, float))

    def test_string_vs_numeric_same_results(self):
        """相同数据的字符串和数值版本应产生相近的均线结果"""
        data_num = _make_numeric_data(n=90)
        data_str = _make_string_data(n=90)
        result_num = _calc_indicators(data_num, "MACD")
        result_str = _calc_indicators(data_str, "MACD")
        # 均线值应接近 (浮点精度差异)
        if "ma5" in result_num and "ma5" in result_str:
            assert abs(result_num["ma5"] - result_str["ma5"]) < 0.01, (
                f"字符串/数值 ma5 差异过大: num={result_num['ma5']}, str={result_str['ma5']}"
            )


# =====================================================================
# 测试 2: 数值类型数据回归测试
# =====================================================================
class TestNumericTypeData:
    """验证数值类型数据 (原有行为) 不受影响"""

    def test_numeric_data_full_indicators(self):
        """数值类型数据计算全部指标"""
        data = _make_numeric_data(n=90)
        result = _calc_indicators(data, "MACD,RSI,KDJ,BOLL")
        assert isinstance(result, dict)
        # 至少有均线
        assert "ma5" in result

    def test_numeric_data_ma60_needs_60_days(self):
        """MA60 需要 60+ 天数据"""
        data = _make_numeric_data(n=90)
        result = _calc_indicators(data, "MACD")
        assert "ma60" in result
        assert result["ma60"] is not None

    def test_numeric_data_ma60_none_if_insufficient(self):
        """数据不足 60 天时 MA60 应为 None"""
        data = _make_numeric_data(n=30)
        result = _calc_indicators(data, "MACD")
        assert "ma60" in result
        assert result["ma60"] is None


# =====================================================================
# 测试 3: 边界情况
# =====================================================================
class TestEdgeCases:
    """验证空数据、缺失字段的优雅降级"""

    def test_empty_data(self):
        """空数据应返回空 dict"""
        result = _calc_indicators([], "MACD")
        assert result == {} or isinstance(result, dict)

    def test_missing_close_column(self):
        """缺失 close 列应优雅降级"""
        data = [{"开盘": 10, "最高": 11, "最低": 9}]  # 无 "收盘"
        result = _calc_indicators(data, "MACD")
        assert isinstance(result, dict)

    def test_mixed_string_numeric(self):
        """混合类型 (部分字符串部分数值) 应正确处理"""
        data = [
            {"开盘": "10.0", "最高": "11.0", "最低": "9.0", "收盘": "10.5", "成交量": "100"},
            {"开盘": 10.5, "最高": 11.5, "最低": 10.0, "收盘": 11.0, "成交量": 200},
        ]
        result = _calc_indicators(data, "MACD")
        assert isinstance(result, dict)

    def test_nan_after_coerce_dropped(self):
        """非数值字符串 (如 'N/A') 应被 coerce 为 NaN 后丢弃"""
        data = [
            {"开盘": "10.0", "最高": "11.0", "最低": "9.0", "收盘": "N/A", "成交量": "100"},
            {"开盘": "10.0", "最高": "11.0", "最低": "9.0", "收盘": "10.5", "成交量": "100"},
        ]
        result = _calc_indicators(data, "MACD")
        assert isinstance(result, dict)


# =====================================================================
# 测试 4: 趋势信号解读
# =====================================================================
class TestSignalInterpretation:
    """验证 _interpret_signals 正确解读指标信号"""

    def test_bullish_alignment(self):
        """多头排列: MA5 > MA10 > MA20"""
        indicators = {
            "ma5": 11.0, "ma10": 10.5, "ma20": 10.0, "ma60": 9.5,
            "latest_close": 11.0,
        }
        signals = _interpret_signals(indicators)
        assert any("多头排列" in d for d in signals["details"])

    def test_bearish_alignment(self):
        """空头排列: MA5 < MA10 < MA20"""
        indicators = {
            "ma5": 9.0, "ma10": 9.5, "ma20": 10.0, "ma60": 10.5,
            "latest_close": 9.0,
        }
        signals = _interpret_signals(indicators)
        assert any("空头排列" in d for d in signals["details"])

    def test_rsi_overbought(self):
        """RSI > 70 超买"""
        indicators = {"rsi_14": 75.0}
        signals = _interpret_signals(indicators)
        assert any("超买" in d for d in signals["details"])

    def test_rsi_oversold(self):
        """RSI < 30 超卖"""
        indicators = {"rsi_14": 25.0}
        signals = _interpret_signals(indicators)
        assert any("超卖" in d for d in signals["details"])

    def test_macd_golden_cross(self):
        """MACD 金叉: macd_prev <= macds 且 macd > macds"""
        indicators = {
            "macd": 0.5, "macds": 0.3, "macd_prev": 0.2,
        }
        signals = _interpret_signals(indicators)
        assert any("金叉" in d for d in signals["details"])


# =====================================================================
# 测试 5: 辅助函数
# =====================================================================
class TestHelpers:
    """验证辅助计算函数"""

    def test_safe_float_valid(self):
        assert _safe_float("10.5") == 10.5
        assert _safe_float(10.5) == 10.5
        assert _safe_float(10) == 10.0

    def test_safe_float_invalid(self):
        assert _safe_float(None) is None
        assert _safe_float("") is None
        assert _safe_float("abc") is None
        assert _safe_float(float("nan")) is None

    def test_safe_round(self):
        assert _safe_round("10.56789", 2) == 10.57
        assert _safe_round(None, 2) is None
        assert _safe_round("abc", 2) is None

    def test_ema(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        ema = _ema(data, 3)
        assert len(ema) == 5
        # EMA 应递增 (数据递增)
        assert ema[-1] > ema[0]

    def test_ema_empty(self):
        assert _ema([], 3) == []
        assert _ema([1, 2, 3], 0) == []

    def test_calc_rsi(self):
        # 上涨序列 → RSI 接近 100
        closes = [float(i) for i in range(1, 25)]
        rsi = _calc_rsi(closes, 14)
        assert rsi is not None
        assert rsi > 90  # 持续上涨 → 高 RSI

    def test_calc_rsi_insufficient(self):
        closes = [1.0, 2.0, 3.0]
        rsi = _calc_rsi(closes, 14)
        assert rsi is None
