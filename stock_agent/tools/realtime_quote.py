"""
实时行情查询工具 (V2 新增, 对应 PRD 8.7 工具扩展)

供所有分析师调用, 获取 A 股实时行情 (当前价/涨跌/成交量等)。
独立于 provider 链, 直接调用新浪行情 API, 不受 AkShare 故障影响。

数据源: 新浪财经实时行情 API (hq.sinajs.cn)
  - 免费, 无需 API key
  - 需 Referer: https://finance.sina.com.cn
  - 返回 GBK 编码文本: var hq_str_sh600584="名称,开盘,昨收,最新价,..."
"""
import json
import re
from loguru import logger


def create_realtime_quote_tool(config: dict = None):
    """创建实时行情查询工具 (新浪)

    Args:
        config: 系统配置 (读取 akshare_http_timeout 作为超时)

    Returns:
        langchain Tool 实例
    """
    from langchain_core.tools import tool

    _config = config or {}
    _timeout = int(_config.get("akshare_http_timeout", 15))

    @tool
    def realtime_quote(ticker: str) -> str:
        """获取 A 股股票实时行情 (新浪财经, 盘中数据)。

        当 technical_indicator_calc 返回的行情数据延迟或为空时,
        可调用本工具获取最新实时价格。适用于:
        - 获取当前最新价/涨跌幅/涨跌额
        - 获取今日开盘价/最高价/最低价
        - 获取实时成交量/成交额

        Args:
            ticker: A 股股票代码, 如 "600584" (不带前缀)

        Returns:
            JSON 字符串, 含实时行情数据
        """
        logger.info(f"[工具] realtime_quote | ticker={ticker}")
        try:
            quote = _fetch_sina_realtime(ticker, _timeout)
            if not quote:
                return json.dumps(
                    {"error": f"未获取到 {ticker} 的实时行情", "data": None},
                    ensure_ascii=False,
                )
            return json.dumps(quote, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"[工具] realtime_quote 失败: {e}")
            return json.dumps({"error": str(e), "data": None}, ensure_ascii=False)

    return realtime_quote


def _fetch_sina_realtime(ticker: str, timeout: int = 15) -> dict:
    """调用新浪实时行情 API

    API: https://hq.sinajs.cn/list=sh600584
    返回: var hq_str_sh600584="长电科技,11.50,11.42,11.58,...";
    字段顺序: 名称,今开,昨收,最新价,最高,最低,买1,卖1,成交量,成交额,...

    Args:
        ticker: 股票代码 (不带前缀)
        timeout: HTTP 超时秒数

    Returns:
        实时行情 dict, 失败返回 {}
    """
    import requests

    symbol = ("sh" if ticker.startswith("6") else "sz") + ticker
    url = f"https://hq.sinajs.cn/list={symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn",
    }

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.encoding = "gbk"
    text = resp.text.strip()

    # 解析: var hq_str_sh600584="...";
    m = re.search(r'"([^"]*)"', text)
    if not m:
        return {}
    fields = m.group(1).split(",")
    if len(fields) < 10:
        return {}

    name = fields[0]
    open_price = _safe_float(fields[1])
    pre_close = _safe_float(fields[2])
    current = _safe_float(fields[3])
    high = _safe_float(fields[4])
    low = _safe_float(fields[5])
    volume = _safe_float(fields[8])  # 股
    amount = _safe_float(fields[9])  # 元
    date = fields[30] if len(fields) > 30 else ""
    time_str = fields[31] if len(fields) > 31 else ""

    change = current - pre_close if (current and pre_close) else None
    change_pct = (change / pre_close * 100) if (change is not None and pre_close) else None

    return {
        "ticker": ticker,
        "name": name,
        "current_price": current,
        "open_price": open_price,
        "pre_close": pre_close,
        "high": high,
        "low": low,
        "change": round(change, 3) if change is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "volume": volume,
        "amount": amount,
        "date": date,
        "time": time_str,
        "source": "sina_realtime",
    }


def _safe_float(val):
    """安全转换为 float, 失败或 NaN 返回 None"""
    try:
        if not val or val == "":
            return None
        f = float(val)
        return f if f == f else None  # 排除 NaN
    except (ValueError, TypeError):
        return None
