"""
BaoStock 数据源 (对应 PRD 6.10, 备用数据源)

BaoStock 是免费开源的证券数据引擎, 提供 A 股行情与财务数据。
作为 AkShare 的备用数据源, 在 AkShare 不可用时降级使用。

所有方法 try/except 包裹, 失败返回空 dict (PRD: 工具优雅降级)。

BaoStock 文档: http://baostock.com/
"""
from loguru import logger
import pandas as pd
from typing import Optional

from stock_agent.dataflows.cache import cached


class BaoStockProvider:
    """BaoStock 数据源 Provider (备用, 免费, 无需 token)"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._bs = None
        self._logged_in = False

    def _ensure_login(self):
        """确保已登录 BaoStock (每次使用前需 login)"""
        if self._logged_in:
            return
        try:
            import baostock as bs
            self._bs = bs
            lg = bs.login()
            if lg.error_code == "0":
                self._logged_in = True
                logger.info("[BaoStock] 登录成功")
            else:
                logger.warning(f"[BaoStock] 登录失败: {lg.error_msg}")
        except ImportError:
            logger.warning("[BaoStock] 模块未安装, 请 pip install baostock")
            raise
        except Exception as e:
            logger.warning(f"[BaoStock] 登录异常: {e}")

    def logout(self):
        """登出 BaoStock"""
        if self._logged_in and self._bs:
            self._bs.logout()
            self._logged_in = False

    @cached(ttl=1800)
    def get_stock_data(self, ticker: str, start_date: str, end_date: str) -> dict:
        """获取 A 股日线行情 (BaoStock)

        BaoStock 股票代码格式: sh.600584 / sz.000001
        """
        try:
            self._ensure_login()
            # 转换代码格式
            code = self._convert_ticker(ticker)
            rs = self._bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2",
            )
            if rs.error_code != "0":
                return {}
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return {}
            # 归一化为 list-of-dicts (下游工具统一按 dict 处理, 避免 'list' object has no attribute 'items')
            fields = rs.fields
            data = [dict(zip(fields, row)) for row in rows]
            return {"data": data, "columns": fields, "source": "baostock"}
        except Exception as e:
            logger.warning(f"[BaoStock] get_stock_data 失败 ({ticker}): {e}")
            return {}

    @cached(ttl=3600)
    def get_financial_data(self, ticker: str) -> dict:
        """获取财务数据 (盈利能力/营运能力/成长能力等)"""
        try:
            self._ensure_login()
            code = self._convert_ticker(ticker)
            rs = self._bs.query_profit_data(code=code, year=self._current_year(), quarter=4)
            if rs.error_code != "0":
                return {}
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return {}
            # 归一化为 list-of-dicts
            fields = rs.fields
            data = [dict(zip(fields, row)) for row in rows]
            return {"data": data, "columns": fields, "source": "baostock"}
        except Exception as e:
            logger.warning(f"[BaoStock] get_financial_data 失败 ({ticker}): {e}")
            return {}

    def _convert_ticker(self, ticker: str) -> str:
        """转换股票代码为 BaoStock 格式 (sh.600584 / sz.000001)"""
        ticker = ticker.replace(".SH", "").replace(".SZ", "")
        if ticker.startswith("6"):
            return f"sh.{ticker}"
        return f"sz.{ticker}"

    def _current_year(self) -> int:
        from datetime import datetime
        return datetime.now().year - 1  # 用上一年的完整年报

    # =================================================================
    # 板块数据 (V2 优化: 返回 None, 让 _call_chain fallback 到 AkShare)
    # =================================================================
    def get_sector_data(self) -> Optional[dict]:
        """获取行业板块行情 (BaoStock 不支持真实板块数据)

        V2 优化: 返回 None 表示"不支持", _call_chain 会跳过且不计入熔断失败。
        (原返回 {} 会被熔断器误判为"调用失败", 导致 BaoStock 被误熔断)
        """
        return None

    def get_sector_stocks(self, sector: str) -> Optional[dict]:
        """获取板块成分股 (BaoStock 不支持)

        V2 优化: 返回 None 表示"不支持", 让 _call_chain fallback 到 AkShare。
        """
        return None

    # =================================================================
    # 新闻资讯 (AkShare fallback, BaoStock 不支持)
    # =================================================================
    def get_news(self, keyword: str = "", count: int = 20) -> Optional[dict]:
        """获取新闻 (BaoStock 不支持, 返回 None 让 WebNews fallback)"""
        return None

    # =================================================================
    # 大盘指数 (熔断检查)
    # =================================================================
    @cached(ttl=1800)
    def get_market_index(self, symbol: str = "sh000001") -> dict:
        """获取大盘指数数据 (BaoStock 格式: sh.000001)"""
        try:
            self._ensure_login()
            if not symbol.startswith("sh.") and not symbol.startswith("sz."):
                symbol = f"sh.{symbol}" if symbol.startswith("sh") else f"sz.{symbol}"
            rs = self._bs.query_history_k_data_plus(
                symbol, "date,open,high,low,close,volume,pctChg",
                start_date=self._today(5), end_date=self._today(),
                frequency="d", adjustflag="3",
            )
            if rs.error_code != "0":
                return {}
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                fields = rs.fields
                data = [dict(zip(fields, row)) for row in rows]
                return {"data": data, "columns": fields, "source": "baostock"}
        except Exception as e:
            logger.warning(f"[BaoStock] get_market_index 失败 ({symbol}): {e}")
        return {}

    # =================================================================
    # 市场异动 (黑马扫描)
    # =================================================================
    def get_market_movers(self) -> Optional[dict]:
        """获取市场异动股 (BaoStock 不支持, 返回 None)"""
        return None

    # =================================================================
    # 龙虎榜 (AkShare fallback, BaoStock 不支持)
    # =================================================================
    def get_dragon_tiger(self, date: str) -> Optional[dict]:
        """获取龙虎榜数据 (BaoStock 不支持, 返回 None)"""
        return None

    # =================================================================
    # 资金流向 (AkShare fallback, BaoStock 不支持)
    # =================================================================
    def get_fund_flow(self, ticker: str) -> Optional[dict]:
        """获取资金流向 (BaoStock 不支持, 返回 None)"""
        return None

    # =================================================================
    # 股票基本信息
    # =================================================================
    @cached(ttl=3600)
    def get_stock_info(self, ticker: str) -> dict:
        """获取股票基本信息"""
        try:
            self._ensure_login()
            code = self._convert_ticker(ticker)
            rs = self._bs.query_stock_basic(code=code)
            if rs.error_code != "0":
                return {}
            info = {}
            while rs.next():
                row = rs.get_row_data()
                info = {
                    "证券代码": row[0],
                    "证券简称": row[1],
                    "上市日期": row[2],
                    "退市日期": row[3],
                    "行业": row[4],
                    "地区": row[5],
                }
            return {"info": info, "source": "baostock"}
        except Exception as e:
            logger.warning(f"[BaoStock] get_stock_info 失败 ({ticker}): {e}")
        return {}

    def _today(self, days_before: int = 0) -> str:
        """获取日期字符串 (YYYY-MM-DD, BaoStock 格式)

        Args:
            days_before: 多少天前 (正数, 0=今天, 5=5天前)
        """
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(days=days_before)).strftime("%Y-%m-%d")
