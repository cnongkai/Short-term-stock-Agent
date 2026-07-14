"""
AkShare 数据源 (对应 PRD 6.10, 主数据源)

AkShare 是免费开源的 Python 财经数据接口库, 覆盖:
  - A 股行情 (日线/实时)
  - 财务数据 (财务分析指标)
  - 龙虎榜数据
  - 资金流向
  - 板块行情
  - 新闻资讯
  - 大盘指数

所有方法 try/except 包裹 + tenacity 重试, 失败返回空 dict/list, 不抛异常 (PRD: 工具优雅降级)。

AkShare 文档: https://akshare.akfamily.xyz/
"""
import concurrent.futures
import socket

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from stock_agent.dataflows.cache import cached

# 重试配置 (V2 优化): 最多 2 次, 间隔 1-2 秒, 仅对网络异常重试
# 原配置对 Exception 重试 3 次, 单个 RemoteDisconnected 阻塞 ~10s;
# 新配置仅对网络异常重试 2 次, 阻塞降至 ~3s, 非网络异常立即失败。
import httpx
_RETRY_CONFIG = {
    "stop": stop_after_attempt(2),
    "wait": wait_exponential(multiplier=1, min=1, max=2),
    "retry": retry_if_exception_type((ConnectionError, TimeoutError, httpx.RemoteProtocolError)),
}


class AkShareProvider:
    """AkShare 数据源 Provider (主数据源, 免费, 无需 token)

    稳定性设计:
    - socket.setdefaulttimeout 兜底: 根治 akshare 底层连接挂起不返回
    - requests.Session + 重试适配器: 供 fallback 函数复用连接池
    - _try_chain: 主 API 失败时依次尝试 Sina/东方财富 fallback API
    - V2 新增: push2 JSON API 直连 fallback (比网页抓取接口更稳定)
    """

    _socket_timeout_set = False  # 类级标记, socket 超时只设一次

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._ak = None  # 懒加载 akshare
        self._http_timeout = int(self.config.get("akshare_http_timeout", 30))
        self._socket_timeout = int(self.config.get("akshare_socket_timeout", 30))
        self._session = self._setup_http_session()
        self._setup_socket_timeout()
        # 持久线程池: 为 _call_with_hard_timeout 提供跨平台硬超时能力
        # (akshare 部分接口如 stock_zh_a_daily 内部 requests 无 timeout, socket 兜底对 SSL 握手无效)
        self._timeout_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # 板块名→BK代码 动态映射缓存 (替代已删除的 _SECTOR_BK_MAP 死列表)
        # 由 _build_sector_bk_map() 从 stock_board_industry_name_em 实时获取, 避免硬编码过期/重复
        self._sector_bk_cache: dict = {}

    def _setup_socket_timeout(self):
        """设置全局 socket 超时 (兜底 akshare 连接挂起, 仅设一次)"""
        if not AkShareProvider._socket_timeout_set:
            socket.setdefaulttimeout(self._socket_timeout)
            AkShareProvider._socket_timeout_set = True
            logger.debug(f"[AkShare] socket 全局超时已设为 {self._socket_timeout}s")

    def _setup_http_session(self) -> requests.Session:
        """创建带连接池和重试的 requests.Session (供 fallback 函数使用)"""
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(
            pool_connections=10, pool_maxsize=20, max_retries=retry_strategy
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    @property
    def ak(self):
        """懒加载 akshare 模块"""
        if self._ak is None:
            try:
                import akshare as ak
                self._ak = ak
                logger.info("[AkShare] 模块加载成功")
            except ImportError:
                logger.warning("[AkShare] 模块未安装, 请 pip install akshare")
                raise
        return self._ak

    def _call_with_hard_timeout(self, fn, label: str, timeout: float = None):
        """用线程池强制硬超时 (跨平台, 解决 akshare 内部 requests 无 timeout 问题)

        akshare 部分接口 (如 stock_zh_a_daily 走 finance.sina.com.cn) 内部 requests 调用
        未设 connect/read timeout, socket.setdefaulttimeout 对 SSL 握手阶段无效,
        导致单次 fallback 可挂死 60s+. 本方法用 ThreadPoolExecutor.future.result(timeout=)
        强制在指定时间内返回, 超时则放弃该 fallback (线程仍在后台运行, 但不阻塞主流程)。

        Args:
            fn: 无参可调用对象 (内部闭包绑定参数)
            label: fallback 标签 (用于日志)
            timeout: 超时秒数, 默认 self._http_timeout

        Returns:
            fn() 的返回值, 或 None (超时/异常)
        """
        if timeout is None:
            timeout = self._http_timeout
        future = self._timeout_executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.debug(f"[AkShare] fallback [{label}] 硬超时 ({timeout}s), 放弃")
            future.cancel()  # 尝试取消 (若尚未开始)
            return None
        except Exception as e:
            logger.debug(f"[AkShare] fallback [{label}] 失败: {e}")
            return None

    def _try_chain(self, funcs: list, normalize=None, timeout: float = None):
        """通用 fallback 链: 依次尝试候选函数, 返回首个非空 df

        Args:
            funcs: [(callable, label), ...] 候选函数列表 (callable 无参, 内部闭包绑定参数)
            normalize: 可选的字段归一化函数 (df -> df)
            timeout: 每个 fallback 的硬超时秒数 (默认 self._http_timeout),
                     解决 akshare 内部 requests 无 timeout 导致的挂死

        Returns:
            (df, label) 或 (None, None)
        """
        for fn, label in funcs:
            df = self._call_with_hard_timeout(fn, label, timeout=timeout)
            if df is not None and not df.empty:
                try:
                    if normalize is not None:
                        df = normalize(df)
                    return df, label
                except Exception as e:
                    logger.debug(f"[AkShare] fallback [{label}] 归一化失败: {e}")
        return None, None

    # =================================================================
    # 行情数据 (PRD: 技术分析/选股依赖)
    # =================================================================
    @cached(ttl=1800)
    @retry(**_RETRY_CONFIG)
    def get_stock_data(self, ticker: str, start_date: str, end_date: str) -> dict:
        """获取 A 股日线行情数据 (OHLCV)

        三级 Fallback: stock_zh_a_hist(东财) → stock_zh_a_daily(新浪) → 新浪 HTTP API 直连

        Args:
            ticker: 股票代码 (如 "600584", 不带前缀)
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)

        Returns:
            {"data": [...], "columns": [...]} 或 {} (失败时)
        """
        try:
            sd = start_date.replace("-", "")
            ed = end_date.replace("-", "")
            sina_symbol = self._sina_symbol(ticker)

            df, label = self._try_chain([
                (lambda: self.ak.stock_zh_a_hist(
                    symbol=ticker, period="daily", start_date=sd, end_date=ed, adjust="qfq"),
                 "stock_zh_a_hist"),
                (lambda: self.ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq"),
                 "stock_zh_a_daily_sina"),
                (lambda: self._fallback_sina_kline_http(ticker, start_date, end_date),
                 "sina_http_kline"),
            ], normalize=self._normalize_kline_df)

            if df is None:
                return {}
            records = df.tail(30).to_dict("records")  # 最近 30 条
            return {"data": records, "columns": list(df.columns), "source": f"akshare_{label}"}
        except Exception as e:
            logger.warning(f"[AkShare] get_stock_data 失败 ({ticker}): {e}")
            return {}

    def _sina_symbol(self, ticker: str) -> str:
        """生成新浪符号格式: sh600584 / sz000001"""
        return ("sh" if ticker.startswith("6") else "sz") + ticker

    def _fallback_sina_kline_http(self, ticker: str, start_date: str, end_date: str):
        """直接调用新浪 K线 HTTP API (绕过 akshare, 最末位兜底)

        API: https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData
        返回字段: day, open, high, low, close, volume
        """
        import json
        import re
        import pandas as pd
        symbol = self._sina_symbol(ticker)
        url = (
            "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/"
            f"CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=1023"
        )
        headers = {"Referer": "https://finance.sina.com.cn"}
        resp = self._session.get(url, headers=headers, timeout=self._http_timeout)
        resp.raise_for_status()
        text = resp.text
        # 解析 jsonp: var KLineData=[...] 或 =(...)
        m = re.search(r"=\s*(\[.*\])", text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        if not data:
            return None
        df = pd.DataFrame(data)
        # 按日期过滤
        if "day" in df.columns:
            df = df[(df["day"] >= start_date) & (df["day"] <= end_date)]
        return df

    def _normalize_kline_df(self, df):
        """归一化 K线字段: 新浪英文 → AkShare 中文 (统一下游字段)"""
        if df is None or df.empty:
            return df
        rename_map = {
            "day": "日期", "open": "开盘", "high": "最高",
            "low": "最低", "close": "收盘", "volume": "成交量",
        }
        existing = {k: v for k, v in rename_map.items() if k in df.columns and v not in df.columns}
        if existing:
            df = df.rename(columns=existing)
        return df

    @cached(ttl=3600)
    @retry(**_RETRY_CONFIG)
    def get_stock_info(self, ticker: str) -> dict:
        """获取股票基本信息 (名称/总市值/流通市值等)

        三级 Fallback: stock_individual_info_em(东方财富网页) → push2 JSON API → stock_info_a_code_name(全量列表)
        """
        try:
            df, label = self._try_chain([
                (lambda: self.ak.stock_individual_info_em(symbol=ticker), "stock_individual_info_em"),
                (lambda: self._fallback_stock_info_push2(ticker), "eastmoney_push2_info"),
                (lambda: self._fallback_stock_info_by_code(ticker), "stock_info_a_code_name"),
            ])
            if df is None:
                return {}
            if label == "stock_individual_info_em":
                info = dict(zip(df["item"], df["value"]))
            else:
                # push2 / fallback: 从全量列表过滤, 字段较少 (证券代码/股票简称)
                row = df.iloc[0]
                info = {"证券代码": str(row.get("code", ticker)), "股票简称": str(row.get("name", ""))}
            return {"info": info, "source": f"akshare_{label}"}
        except Exception as e:
            logger.warning(f"[AkShare] get_stock_info 失败 ({ticker}): {e}")
            return {}

    def _fallback_stock_info_push2(self, ticker: str):
        """East Money push2 个股信息 JSON API (绕过网页抓取, 更稳定)

        API: http://push2.eastmoney.com/api/qt/stock/get
        返回字段: f57=代码, f58=名称, f84=总市值, f85=流通市值, f116=营收, f117=净利润,
                 f162=PE, f167=PB, f168=换手率, f169=涨跌幅
        """
        import pandas as pd
        market = "1" if ticker.startswith("6") else "0"
        secid = f"{market}.{ticker}"
        url = (
            f"http://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid}&fields=f57,f58,f84,f85,f116,f117,f162,f167,f168,f169"
        )
        resp = self._session.get(url, timeout=self._http_timeout)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if not data:
            return None
        # 转为与 stock_individual_info_em 相同的 {item, value} 两列 df 结构
        field_map = {
            "f57": "代码", "f58": "股票简称", "f84": "总市值",
            "f85": "流通市值", "f116": "营业收入", "f117": "净利润",
            "f162": "市盈率", "f167": "市净率", "f168": "换手率",
            "f169": "涨跌幅",
        }
        items = list(field_map.values())
        values = [data.get(k, "") for k in field_map.keys()]
        return pd.DataFrame({"item": items, "value": values})

    def _fallback_stock_info_by_code(self, ticker: str):
        """从全量股票列表按代码过滤 (stock_info_a_code_name)"""
        df = self.ak.stock_info_a_code_name()
        if df is None or df.empty:
            return None
        mask = df["code"].astype(str) == str(ticker)
        return df[mask]

    # =================================================================
    # 财务数据 (PRD: 基本面分析依赖)
    # =================================================================
    @cached(ttl=3600)
    def get_financial_data(self, ticker: str) -> dict:
        """获取财务分析指标 (ROE/PE/PB/营收/净利润等)"""
        try:
            df = self.ak.stock_financial_analysis_indicator(symbol=ticker)
            if df is None or df.empty:
                return {}
            records = df.head(4).to_dict("records")  # 最近 4 期
            return {"data": records, "columns": list(df.columns), "source": "akshare"}
        except Exception as e:
            logger.warning(f"[AkShare] get_financial_data 失败 ({ticker}): {e}")
            return {}

    # =================================================================
    # 龙虎榜数据 (PRD: A 股专属分析依赖)
    # =================================================================
    @cached(ttl=3600)
    @retry(**_RETRY_CONFIG)
    def get_dragon_tiger(self, date: str) -> dict:
        """获取龙虎榜数据

        修复: 当日无 LHB 数据时 stock_lhb_detail_em 抛 NoneType,
        自动回退到最近 7 天范围取最近记录。

        Args:
            date: 交易日期 (YYYY-MM-DD)

        Returns:
            {"data": [...]} 或 {}
        """
        from datetime import datetime, timedelta
        try:
            sd = date.replace("-", "")
            # 主调用: 当日精确查询 (可能因无数据抛 TypeError)
            try:
                df = self.ak.stock_lhb_detail_em(start_date=sd, end_date=sd)
                if df is not None and not df.empty:
                    records = df.to_dict("records")
                    return {"data": records, "source": "akshare"}
            except TypeError:
                logger.debug(f"[AkShare] get_dragon_tiger 当日({date})无数据, 回退到近7天范围")
            except Exception as e:
                logger.debug(f"[AkShare] get_dragon_tiger 当日查询异常({date}): {e}")

            # Fallback: 回退到最近 7 天范围
            end_dt = datetime.strptime(date, "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=7)
            sd_range = start_dt.strftime("%Y%m%d")
            ed_range = end_dt.strftime("%Y%m%d")
            df = self.ak.stock_lhb_detail_em(start_date=sd_range, end_date=ed_range)
            if df is None or df.empty:
                return {}
            records = df.to_dict("records")
            return {"data": records, "source": "akshare_lhb_7d_fallback"}
        except Exception as e:
            logger.warning(f"[AkShare] get_dragon_tiger 失败 ({date}): {e}")
            return {}

    # =================================================================
    # 资金流向 (PRD: A 股专属分析/黑马扫描依赖)
    # =================================================================
    @cached(ttl=1800)
    @retry(**_RETRY_CONFIG)
    def get_fund_flow(self, ticker: str) -> dict:
        """获取个股资金流向 (主力/超大单/大单/中单/小单)

        三级 Fallback: stock_individual_fund_flow(东财akshare) → rank(东财akshare) → 东财 HTTP API 直连
        """
        try:
            market = "sh" if ticker.startswith("6") else "sz"
            df, label = self._try_chain([
                (lambda: self.ak.stock_individual_fund_flow(stock=ticker, market=market), "individual_fund_flow"),
                (lambda: self._fallback_fund_flow_rank(ticker), "fund_flow_rank"),
                (lambda: self._fallback_fund_flow_http(ticker), "eastmoney_http_fund_flow"),
            ])
            if df is None:
                return {}
            records = df.tail(5).to_dict("records")  # 最近 5 天
            source_map = {
                "individual_fund_flow": "akshare",
                "fund_flow_rank": "akshare_rank_fallback",
                "eastmoney_http_fund_flow": "eastmoney_http",
            }
            return {"data": records, "source": source_map.get(label, label)}
        except Exception as e:
            logger.warning(f"[AkShare] get_fund_flow 失败 ({ticker}): {e}")
            return {}

    def _fallback_fund_flow_rank(self, ticker: str):
        """资金流排名 fallback: 从全量排名按代码过滤 (东方财富)"""
        df = self.ak.stock_individual_fund_flow_rank(indicator="今日")
        if df is None or df.empty:
            return None
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        mask = df[code_col].astype(str) == str(ticker)
        return df[mask]

    def _fallback_fund_flow_http(self, ticker: str):
        """直接调用东方财富资金流向 HTTP API (绕过 akshare, 最末位兜底)

        API: http://push2.eastmoney.com/api/qt/stock/fflow/daykline/get
        market: 1=沪市(6开头), 0=深市(0/3开头)
        """
        import pandas as pd
        market = "1" if ticker.startswith("6") else "0"
        secid = f"{market}.{ticker}"
        url = (
            f"http://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
            f"?secid={secid}&lmt=10&fields1=f1,f2,f3,f7"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63&klt=101"
        )
        resp = self._session.get(url, timeout=self._http_timeout)
        resp.raise_for_status()
        result = resp.json()
        klines = result.get("data", {}).get("klines", [])
        if not klines:
            return None
        field_names = ["日期", "主力净流入", "小单净流入", "中单净流入",
                       "大单净流入", "超大单净流入", "主力净流入占比",
                       "小单净流入占比", "中单净流入占比", "大单净流入占比",
                       "超大单净流入占比", "f62", "f63"]
        rows = []
        for line in klines:
            parts = line.split(",")
            rows.append(dict(zip(field_names, parts)))
        return pd.DataFrame(rows)

    # =================================================================
    # 板块数据 (PRD: 板块推理/选股依赖)
    # =================================================================
    @cached(ttl=1800)
    @retry(**_RETRY_CONFIG)
    def get_sector_data(self) -> dict:
        """获取行业板块行情 (涨幅/成交额/领涨股)

        Fallback: stock_board_industry_name_em(东方财富) → stock_sector_spot(新浪行业)
        """
        try:
            df, label = self._try_chain([
                (lambda: self.ak.stock_board_industry_name_em(), "board_industry_name_em"),
                (lambda: self.ak.stock_sector_spot(indicator="新浪行业"), "sector_spot_sina"),
            ], normalize=self._normalize_sector_df)
            if df is None:
                return {}
            # 顺带构建板块名→BK代码动态映射 (供 push2 fallback 使用)
            self._build_sector_bk_map(df)
            records = df.to_dict("records")
            source = "akshare" if label == "board_industry_name_em" else "akshare_sina_fallback"
            return {"data": records, "source": source}
        except Exception as e:
            logger.warning(f"[AkShare] get_sector_data 失败: {e}")
            return {}

    def _build_sector_bk_map(self, df) -> None:
        """从 stock_board_industry_name_em 结果构建板块名→BK代码动态映射

        替代已删除的 _SECTOR_BK_MAP 死列表 (含重复BK代码+缺失板块).
        东财 board_industry_name_em 返回 '板块名称' 和 '板块代码' 列,
        板块代码即 BK 编号, 长期稳定但偶有新增/更名, 动态获取最可靠。
        """
        if df is None or df.empty:
            return
        # 东财返回列: 板块名称, 板块代码 (BK####)
        if "板块名称" in df.columns and "板块代码" in df.columns:
            mapping = dict(zip(df["板块名称"], df["板块代码"]))
            if mapping:
                self._sector_bk_cache.update(mapping)
                logger.debug(f"[AkShare] 板块BK映射已更新: {len(mapping)} 个板块")

    def _normalize_sector_df(self, df) -> "pd.DataFrame":
        """归一化板块字段: 新浪版 '板块' → '板块名称' (统一下游字段)"""
        if df is None or df.empty:
            return df
        if "板块" in df.columns and "板块名称" not in df.columns:
            df = df.rename(columns={"板块": "板块名称"})
        return df

    @cached(ttl=1800)
    @retry(**_RETRY_CONFIG)
    def get_sector_stocks(self, sector: str) -> dict:
        """获取板块成分股

        三级 Fallback: stock_board_industry_cons_em(东方财富网页) → push2 JSON API → stock_sector_detail(新浪)
        """
        try:
            df, label = self._try_chain([
                (lambda: self.ak.stock_board_industry_cons_em(symbol=sector), "board_industry_cons_em"),
                (lambda: self._fallback_sector_stocks_push2(sector), "eastmoney_push2_cons"),
                (lambda: self._fallback_sector_stocks_sina(sector), "sector_detail_sina"),
            ])
            if df is None:
                return {}
            records = df.to_dict("records")
            source_map = {
                "board_industry_cons_em": "akshare",
                "eastmoney_push2_cons": "eastmoney_push2",
                "sector_detail_sina": "akshare_sina_fallback",
            }
            return {"data": records, "source": source_map.get(label, label)}
        except Exception as e:
            logger.warning(f"[AkShare] get_sector_stocks 失败 ({sector}): {e}")
            return {}

    def _fallback_sector_stocks_push2(self, sector: str):
        """East Money push2 板块成分股 JSON API (绕过网页抓取, 更稳定)

        API: http://push2.eastmoney.com/api/qt/clist/get
        需要板块 BK 代码 (从动态缓存查找), 缓存为空时尝试实时获取, 未知板块返回 None。
        """
        import pandas as pd
        bk_code = self._sector_bk_cache.get(sector)
        if not bk_code:
            # 缓存为空: 尝试实时获取板块列表构建映射 (仅一次, 避免循环调用)
            if not self._sector_bk_cache:
                logger.debug(f"[AkShare] push2 fallback: BK缓存为空, 尝试实时获取板块列表")
                try:
                    df = self.ak.stock_board_industry_name_em()
                    self._build_sector_bk_map(df)
                except Exception as e:
                    logger.debug(f"[AkShare] push2 fallback: 实时获取板块列表失败: {e}")
            bk_code = self._sector_bk_cache.get(sector)
        if not bk_code:
            logger.debug(f"[AkShare] push2 fallback: 未知板块 '{sector}', 跳过")
            return None
        url = (
            f"http://push2.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz=200&po=1&np=1&fltt=2&invt=2"
            f"&fs=b:{bk_code}&fields=f12,f14,f2,f3,f4,f5,f6,f7,f15,f18"
        )
        resp = self._session.get(url, timeout=self._http_timeout)
        resp.raise_for_status()
        diff = resp.json().get("data", {}).get("diff", [])
        if not diff:
            return None
        # 字段: f12=代码, f14=名称, f2=最新价, f3=涨跌幅, f4=涨跌额, f5=成交量, f6=成交额, f7=振幅, f15=最高, f18=昨收
        rename = {
            "f12": "代码", "f14": "名称", "f2": "最新价", "f3": "涨跌幅",
            "f4": "涨跌额", "f5": "成交量", "f6": "成交额", "f7": "振幅",
            "f15": "最高", "f18": "昨收",
        }
        df = pd.DataFrame(diff).rename(columns=rename)
        # 仅保留已重命名的列 (过滤掉其他 f 字段)
        cols = [c for c in rename.values() if c in df.columns]
        return df[cols]

    def _fallback_sector_stocks_sina(self, sector: str):
        """新浪板块成分股 fallback: stock_sector_detail(按 sector 标签)"""
        # 新浪财经板块成分股接口需要 sector 标签 (如 "new_energy")
        # 此处尝试直接用中文名, 失败则返回 None (质量较低, 仅兜底)
        try:
            df = self.ak.stock_sector_detail(sector=sector)
            return df
        except Exception:
            return None

    # =================================================================
    # 新闻资讯 (PRD: 热点发现/个股发展分析依赖)
    # =================================================================
    @cached(ttl=600)
    @retry(**_RETRY_CONFIG)
    def get_news(self, keyword: str = "", count: int = 20) -> dict:
        """获取财经新闻 (关键词搜索或个股新闻)"""
        try:
            if keyword:
                # 个股新闻
                df = self.ak.stock_news_em(symbol=keyword)
                if df is not None and not df.empty:
                    records = df.head(count).to_dict("records")
                    return {"data": records, "source": "akshare"}
            # 财经快讯
            df = self.ak.news_cctv(date="".join(str(self._today()).split("-")))
            if df is None or df.empty:
                return {}
            records = df.head(count).to_dict("records")
            return {"data": records, "source": "akshare"}
        except Exception as e:
            logger.warning(f"[AkShare] get_news 失败 ({keyword}): {e}")
            return {}

    # =================================================================
    # 大盘指数 (PRD 10.2: 熔断检查依赖)
    # =================================================================
    @cached(ttl=1800)
    @retry(**_RETRY_CONFIG)
    def get_market_index(self, symbol: str = "sh000001") -> dict:
        """获取大盘指数数据 (默认上证指数, 用于熔断检查)

        Args:
            symbol: 指数代码 (sh000001=上证, sz399001=深成指, sz399006=创业板)
        """
        try:
            df = self.ak.stock_zh_index_daily(symbol=symbol)
            if df is None or df.empty:
                return {}
            latest = df.tail(2).to_dict("records")  # 最近 2 天 (算跌幅)
            return {"data": latest, "source": "akshare"}
        except Exception as e:
            logger.warning(f"[AkShare] get_market_index 失败 ({symbol}): {e}")
            return {}

    # =================================================================
    # 市场异动 (PRD 6.6: 黑马扫描依赖)
    # =================================================================
    @cached(ttl=600)
    @retry(**_RETRY_CONFIG)
    def get_market_movers(self) -> dict:
        """获取市场异动股 (涨幅榜/量比榜)

        Fallback: stock_zh_a_spot_em(东方财富) → stock_zh_a_spot(新浪)
        """
        try:
            df, label = self._try_chain([
                (lambda: self.ak.stock_zh_a_spot_em(), "zh_a_spot_em"),
                (lambda: self.ak.stock_zh_a_spot(), "zh_a_spot_sina"),
            ], normalize=self._normalize_spot_df)
            if df is None:
                return {}
            # 取涨幅前 50
            if "涨跌幅" in df.columns:
                df = df.nlargest(50, "涨跌幅")
            records = df.to_dict("records")
            source = "akshare" if label == "zh_a_spot_em" else "akshare_sina_fallback"
            return {"data": records, "source": source}
        except Exception as e:
            logger.warning(f"[AkShare] get_market_movers 失败: {e}")
            return {}

    def _normalize_spot_df(self, df) -> "pd.DataFrame":
        """归一化实时行情字段 (新浪版字段名 → 东方财富版)"""
        if df is None or df.empty:
            return df
        # 新浪版 trade(成交价) → 最新价, 涨跌幅 字段名可能不同
        rename_map = {"trade": "最新价", "changepercent": "涨跌幅", "name": "名称", "code": "代码"}
        for old, new in rename_map.items():
            if old in df.columns and new not in df.columns:
                df = df.rename(columns={old: new})
        return df

    def _today(self):
        """获取今天日期"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d")
