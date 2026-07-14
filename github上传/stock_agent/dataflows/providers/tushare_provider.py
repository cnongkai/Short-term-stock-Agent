"""
Tushare 数据源 (对应 PRD 6.10, 备用数据源)

Tushare Pro 是专业的金融数据接口, 需要注册获取 token。
作为 AkShare/BaoStock 之后的备用数据源。

所有方法 try/except 包裹, 失败返回空 dict (PRD: 工具优雅降级)。

Tushare 文档: https://tushare.pro/
"""
from loguru import logger
from typing import Optional

from stock_agent.dataflows.cache import cached


class TushareProvider:
    """Tushare 数据源 Provider (备用, 需 token)"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._token = self.config.get("tushare_token", "")
        self._pro = None

    @property
    def pro(self):
        """懒加载 tushare pro 接口"""
        if self._pro is None:
            if not self._token:
                logger.warning("[Tushare] 未配置 token, 跳过")
                raise ValueError("Tushare token 未配置")
            try:
                import tushare as ts
                ts.set_token(self._token)
                self._pro = ts.pro_api()
                logger.info("[Tushare] 接口初始化成功")
            except ImportError:
                logger.warning("[Tushare] 模块未安装, 请 pip install tushare")
                raise
        return self._pro

    @cached(ttl=1800)
    def get_stock_data(self, ticker: str, start_date: str, end_date: str) -> dict:
        """获取 A 股日线行情 (Tushare)"""
        try:
            code = self._convert_ticker(ticker)
            sd = start_date.replace("-", "")
            ed = end_date.replace("-", "")
            df = self.pro.daily(ts_code=code, start_date=sd, end_date=ed)
            if df is None or df.empty:
                return {}
            records = df.tail(30).to_dict("records")
            return {"data": records, "columns": list(df.columns), "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_stock_data 失败 ({ticker}): {e}")
            return {}

    @cached(ttl=3600)
    def get_stock_info(self, ticker: str) -> dict:
        """获取股票基本信息 (Tushare stock_basic)

        V2 新增: 补全 AkShare get_stock_info 故障时的 fallback。
        """
        try:
            code = self._convert_ticker(ticker)
            df = self.pro.stock_basic(
                ts_code=code,
                fields="ts_code,symbol,name,area,industry,list_date",
            )
            if df is None or df.empty:
                return {}
            row = df.iloc[0].to_dict()
            info = {
                "证券代码": str(row.get("symbol", ticker)),
                "股票简称": str(row.get("name", "")),
                "行业": str(row.get("industry", "")),
                "地区": str(row.get("area", "")),
                "上市日期": str(row.get("list_date", "")),
            }
            return {"info": info, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_stock_info 失败 ({ticker}): {e}")
            return {}

    @cached(ttl=3600)
    def get_financial_data(self, ticker: str) -> dict:
        """获取财务指标 (Tushare fina_indicator)"""
        try:
            code = self._convert_ticker(ticker)
            df = self.pro.fina_indicator(ts_code=code, period=str(self._current_year()) + "1231")
            if df is None or df.empty:
                return {}
            records = df.head(4).to_dict("records")
            return {"data": records, "columns": list(df.columns), "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_financial_data 失败 ({ticker}): {e}")
            return {}

    @cached(ttl=3600)
    def get_dragon_tiger(self, date: str) -> dict:
        """获取龙虎榜 (Tushare top_list)"""
        try:
            sd = date.replace("-", "")
            df = self.pro.top_list(trade_date=sd)
            if df is None or df.empty:
                return {}
            records = df.to_dict("records")
            return {"data": records, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_dragon_tiger 失败 ({date}): {e}")
            return {}

    @cached(ttl=1800)
    def get_fund_flow(self, ticker: str) -> dict:
        """获取资金流向 (Tushare moneyflow)"""
        try:
            code = self._convert_ticker(ticker)
            df = self.pro.moneyflow(ts_code=code)
            if df is None or df.empty:
                return {}
            records = df.tail(5).to_dict("records")
            return {"data": records, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_fund_flow 失败 ({ticker}): {e}")
            return {}

    @cached(ttl=1800)
    def get_sector_stocks(self, sector: str) -> dict:
        """获取板块成分股 (Tushare stock_basic 按 industry 过滤)

        V2 新增: sector 参数为中文行业名 (如 "半导体"), Tushare 的 industry 字段匹配。
        注意: Tushare 的行业分类与东方财富不完全一致, 匹配率约 70%。
        """
        try:
            df = self.pro.stock_basic(
                list_status="L",
                fields="ts_code,symbol,name,industry,list_date",
            )
            if df is None or df.empty:
                return {}
            df = df[df["industry"] == sector]
            if df.empty:
                return {}
            records = df.to_dict("records")
            return {"data": records, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_sector_stocks 失败 ({sector}): {e}")
            return {}

    @cached(ttl=600)
    def get_market_movers(self) -> dict:
        """获取市场异动股 (Tushare daily 按 pct_chg 排序)

        V2 新增: 取最近交易日涨幅前 50。
        """
        try:
            from datetime import datetime, timedelta
            # Tushare daily 需指定 trade_date, 尝试最近 3 天
            for days_back in range(1, 4):
                trade_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
                df = self.pro.daily(
                    trade_date=trade_date,
                    fields="ts_code,symbol,name,pct_chg,vol,amount",
                )
                if df is not None and not df.empty:
                    break
            if df is None or df.empty:
                return {}
            # 取涨幅前 50
            df = df.nlargest(50, "pct_chg")
            records = df.to_dict("records")
            return {"data": records, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_market_movers 失败: {e}")
            return {}

    @cached(ttl=1800)
    def get_market_index(self, symbol: str = "sh000001") -> dict:
        """获取大盘指数 (Tushare index_daily)

        V2 新增: 代码转换 sh000001 → 000001.SH。
        """
        try:
            # 转换: sh000001 → 000001.SH, sz399001 → 399001.SZ
            ts_code = symbol.replace("sh", "").replace("sz", "")
            if symbol.startswith("sh"):
                ts_code = f"{ts_code}.SH"
            elif symbol.startswith("sz"):
                ts_code = f"{ts_code}.SZ"
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
            df = self.pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                return {}
            latest = df.tail(2).to_dict("records")
            return {"data": latest, "source": "tushare"}
        except Exception as e:
            logger.warning(f"[Tushare] get_market_index 失败 ({symbol}): {e}")
            return {}

    def get_news(self, keyword: str = "", count: int = 20) -> Optional[dict]:
        """获取新闻 (Tushare 无新闻接口, 返回 None 让 WebNews fallback)

        V2 新增: 显式返回 None 表示"不支持"。
        """
        return None

    def _convert_ticker(self, ticker: str) -> str:
        """转换股票代码为 Tushare 格式 (600584.SH / 000001.SZ)"""
        ticker = ticker.replace(".SH", "").replace(".SZ", "")
        if ticker.startswith("6"):
            return f"{ticker}.SH"
        return f"{ticker}.SZ"

    def _current_year(self) -> int:
        from datetime import datetime
        return datetime.now().year - 1
