"""
数据源管理器 (对应 PRD 6.10 数据流图)

按 provider 链 (AkShare → Tushare → BaoStock → WebNews) 依次尝试获取数据,
首个成功返回即用, 全部失败返回空 dict (PRD: 工具优雅降级)。

- AkShare 主数据源 (行情/板块/龙虎榜/资金流/新闻/异动, 内含 Sina/东方财富 fallback)
- Tushare 二级数据源 (需 token, 有 token 时自动启用)
- BaoStock 末位兜底 (行情/财务/股票信息/大盘指数, 稳定无网络抖动)
- WebNews RSS 新闻 fallback

同时管理巨潮公告检索 (cninfo) 和政策信源 (policy)。

设计模式: Provider Chain + 故障转移
"""
import time as _time
from typing import Any, Dict, List, Optional
from loguru import logger

from stock_agent.dataflows.providers.akshare_provider import AkShareProvider
from stock_agent.dataflows.providers.baostock_provider import BaoStockProvider
from stock_agent.dataflows.providers.tushare_provider import TushareProvider
from stock_agent.dataflows.providers.cninfo_provider import CnInfoProvider
from stock_agent.dataflows.providers.web_news_provider import WebNewsProvider
from stock_agent.dataflows.provider_health import get_health_tracker


class _FailureAgg:
    """失败聚合计数器 (按 method_name 维度, 减少日志噪声)"""

    def __init__(self):
        self.count = 0
        self.first_ts = 0.0
        self.last_ts = 0.0
        self.skipped_providers: set = set()


class DataSourceManager:
    """统一数据源管理器, 管理 provider 链与故障转移"""

    _instance = None  # 单例

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._providers: List[Any] = []  # 行情/财务 provider 链
        self._cninfo: Optional[CnInfoProvider] = None
        self._policy_manager = None  # 懒加载政策信源
        self._failure_aggs: Dict[str, _FailureAgg] = {}  # 失败聚合 (按 method)
        self._health = get_health_tracker(
            failure_threshold=self.config.get("provider_failure_threshold", 3),
            cooldown_seconds=self.config.get("provider_cooldown_seconds", 120),
        )
        self._init_providers()

    def _init_providers(self):
        """按配置初始化 provider 链

        顺序: AkShare(主) → Tushare(若有 token) → BaoStock(兜底) → WebNews(新闻)
        - AkShare 主供: 行情/板块/龙虎榜/资金流/新闻/异动 (内含 Sina/东方财富 fallback)
        - Tushare 二级: 有 token 时自动启用 (行情/财务/龙虎榜/资金流)
        - BaoStock 末位: 行情/财务/股票信息/大盘指数 (稳定无网络抖动)
        """
        # AkShare 主数据源 (板块/龙虎榜/资金流/新闻/异动主供)
        if self.config.get("akshare_enabled", True):
            try:
                self._providers.append(AkShareProvider(self.config))
                logger.info("[数据源] AkShare 已启用 (主数据源)")
            except Exception as e:
                logger.warning(f"[数据源] AkShare 初始化失败: {e}")

        # Tushare 二级数据源 (有 token 时自动启用, 即使 TUSHARE_ENABLED=false)
        tushare_token = self.config.get("tushare_token", "")
        tushare_enabled = self.config.get("tushare_enabled", False) or bool(tushare_token)
        if tushare_enabled:
            try:
                self._providers.append(TushareProvider(self.config))
                logger.info("[数据源] Tushare 已启用 (二级, 检测到 token)")
            except Exception as e:
                logger.warning(f"[数据源] Tushare 初始化失败 (token 可能无效): {e}")

        # BaoStock 末位兜底 (稳定, 无网络抖动)
        if self.config.get("baostock_enabled", True):
            try:
                self._providers.append(BaoStockProvider(self.config))
                logger.info("[数据源] BaoStock 已启用 (末位兜底)")
            except Exception as e:
                logger.warning(f"[数据源] BaoStock 初始化失败: {e}")

        # WebNews RSS 新闻 fallback (AkShare/BaoStock/Tushare 新闻失败时使用)
        if self.config.get("web_news_enabled", True):
            try:
                self._providers.append(WebNewsProvider(self.config))
                logger.info("[数据源] WebNews (RSS) 已启用 (新闻 fallback)")
            except Exception as e:
                logger.warning(f"[数据源] WebNews 初始化失败: {e}")

        # 巨潮公告
        if self.config.get("cninfo_enabled", True):
            self._cninfo = CnInfoProvider(self.config)
            logger.info("[数据源] 巨潮资讯网已启用")

        logger.info(f"[数据源] Provider 链: {len(self._providers)} 个行情源 + 巨潮公告")

    # =================================================================
    # Provider 链调用 (行情/财务/龙虎榜/资金流/板块/新闻/指数/异动)
    # =================================================================
    def _call_chain(self, method_name: str, *args, **kwargs) -> dict:
        """按 provider 链依次尝试, 首个非空结果返回 (故障转移)

        V2 优化:
        - 熔断器: 连续失败的 provider+method 会被跳过 (冷却期内)
        - 区分"不支持"(返回 None) 与"失败"(返回 {}), None 不计熔断
        - 日志聚合: 首次失败 WARNING, 后续每 5 次或 60s 聚合一条 INFO
        """
        skipped = []  # 被熔断跳过的 provider 列表
        for provider in self._providers:
            provider_name = provider.__class__.__name__
            method = getattr(provider, method_name, None)
            if method is None:
                continue

            # 熔断检查: 被熔断的 provider+method 直接跳过
            if self._health.should_skip(provider_name, method_name):
                skipped.append(provider_name)
                continue

            try:
                result = method(*args, **kwargs)
                # None 表示 provider 显式声明不支持此方法 (不计熔断)
                if result is None:
                    continue
                if result and result.get("data"):
                    self._health.record_success(provider_name, method_name)
                    return result
                # 空 dict 表示调用了但无数据 (计为失败, 可能触发熔断)
                self._health.record_failure(provider_name, method_name)
            except Exception as e:
                self._health.record_failure(provider_name, method_name)
                logger.debug(f"[数据源] {provider_name}.{method_name} 失败: {e}")
                continue

        # 全链失败: 聚合日志
        self._log_chain_failure(method_name, skipped)
        return {}

    def _log_chain_failure(self, method_name: str, skipped: list):
        """聚合日志: 首次失败 WARNING, 后续每 5 次或 60s 汇总一条 INFO

        避免一次运行中同一 method 的 "均无数据" WARNING 刷屏 10+ 次。
        """
        agg = self._failure_aggs.setdefault(method_name, _FailureAgg())
        now = _time.time()
        if agg.count == 0:
            agg.first_ts = now
            agg.count = 1
            agg.skipped_providers.update(skipped)
            logger.warning(
                f"[数据源] {method_name} 全链失败 (跳过: {skipped or '无'})"
            )
            return
        agg.count += 1
        agg.last_ts = now
        agg.skipped_providers.update(skipped)
        # 每 5 次或距首次超 60s 打一条汇总
        if agg.count % 5 == 0 or (now - agg.first_ts) > 60:
            logger.info(
                f"[数据源] {method_name} 累计失败 {agg.count} 次 "
                f"(跨 {now - agg.first_ts:.0f}s, 跳过: {sorted(agg.skipped_providers)})"
            )
            agg.first_ts = now
            agg.skipped_providers.clear()

    # === 对外接口方法 ===
    def get_stock_data(self, ticker: str, start_date: str, end_date: str) -> dict:
        """获取股票行情 (provider 链)"""
        return self._call_chain("get_stock_data", ticker, start_date, end_date)

    def get_financial_data(self, ticker: str) -> dict:
        """获取财务数据 (provider 链)"""
        return self._call_chain("get_financial_data", ticker)

    def get_dragon_tiger(self, date: str) -> dict:
        """获取龙虎榜 (provider 链)"""
        return self._call_chain("get_dragon_tiger", date)

    def get_fund_flow(self, ticker: str) -> dict:
        """获取资金流向 (provider 链)"""
        return self._call_chain("get_fund_flow", ticker)

    def get_sector_data(self) -> dict:
        """获取板块行情 (仅 AkShare 支持)"""
        return self._call_chain("get_sector_data")

    def get_sector_stocks(self, sector: str) -> dict:
        """获取板块成分股 (仅 AkShare 支持)"""
        return self._call_chain("get_sector_stocks", sector)

    def get_news(self, keyword: str = "", count: int = 20) -> dict:
        """获取新闻 (provider 链)"""
        return self._call_chain("get_news", keyword, count)

    def get_market_index(self, symbol: str = "sh000001") -> dict:
        """获取大盘指数 (熔断检查)"""
        return self._call_chain("get_market_index", symbol)

    def get_market_movers(self) -> dict:
        """获取市场异动股 (黑马扫描)"""
        return self._call_chain("get_market_movers")

    def get_stock_info(self, ticker: str) -> dict:
        """获取股票基本信息"""
        return self._call_chain("get_stock_info", ticker)

    # =================================================================
    # 批量预取 (V2: 分析层前预热缓存, 工具调用全部命中缓存)
    # =================================================================
    def prefetch_ticker_data(self, candidate_pool: list, trade_date: str) -> dict:
        """批量预取候选池所有 ticker 数据 (ThreadPoolExecutor 并发), 结果写入缓存

        预取项 (per ticker): get_stock_data / get_stock_info / get_financial_data / get_fund_flow
        另预取一次 get_dragon_tiger(trade_date) (按日期, 全局共享)

        Args:
            candidate_pool: 候选池列表, 元素含 ticker 字段
            trade_date: 交易日期 (YYYY-MM-DD)

        Returns:
            {"prefetched": int, "failed": [...], "elapsed": float}
        """
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime, timedelta

        start = _time.time()
        workers = int(self.config.get("prefetch_workers", 4))
        timeout = int(self.config.get("prefetch_timeout", 120))
        lookback = int(self.config.get("prefetch_lookback_days", 60))

        end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=lookback)
        start_date = start_dt.strftime("%Y-%m-%d")

        tickers = []
        for item in candidate_pool:
            t = item.get("ticker", "") if isinstance(item, dict) else str(item)
            if t:
                tickers.append(t)

        prefetched = 0
        failed = []

        def _fetch_one(ticker: str):
            """预取单只 ticker 的 4 类数据 (每类 try/except 独立容错)"""
            errs = []
            for method_name, args in [
                ("get_stock_data", (ticker, start_date, trade_date)),
                ("get_stock_info", (ticker,)),
                ("get_financial_data", (ticker,)),
                ("get_fund_flow", (ticker,)),
            ]:
                try:
                    # 调用 provider 链 (@cached 自动缓存结果)
                    getattr(self, method_name)(*args)
                except Exception as e:
                    errs.append(f"{method_name}: {e}")
            return ticker, errs

        tasks = [(t, None) for t in tickers]
        # 额外: 龙虎榜按日期取一次 (全局共享)
        tasks.append(("__dragon_tiger__", trade_date))

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                future_map = {}
                for ticker, dt in tasks:
                    if ticker == "__dragon_tiger__":
                        future_map[ex.submit(self.get_dragon_tiger, dt)] = ticker
                    else:
                        future_map[ex.submit(_fetch_one, ticker)] = ticker

                try:
                    for fut in as_completed(future_map, timeout=timeout):
                        ticker = future_map[fut]
                        try:
                            result = fut.result()
                            if ticker == "__dragon_tiger__":
                                prefetched += 1
                            else:
                                _, errs = result
                                if errs:
                                    failed.extend(errs)
                                else:
                                    prefetched += 1
                        except Exception as e:
                            failed.append(f"{ticker}: {e}")
                except TimeoutError:
                    logger.warning(f"[预取] 超时({timeout}s), 部分未完成")
                    for fut, ticker in future_map.items():
                        if not fut.done():
                            fut.cancel()
                            failed.append(f"{ticker}: 预取超时")
        except Exception as e:
            logger.warning(f"[预取] 批量预取异常: {e}")

        elapsed = _time.time() - start
        logger.info(f"[预取] 完成 {prefetched}/{len(tickers)+1}, 失败 {len(failed)} 项, 耗时 {elapsed:.1f}s")
        return {"prefetched": prefetched, "failed": failed, "elapsed": elapsed}

    # === 巨潮公告 (独立 provider) ===
    def search_announcements(self, ticker: str, keyword: str = "", count: int = 10) -> dict:
        """检索巨潮公告 (PRD 8.6)"""
        if self._cninfo is None:
            return {"data": [], "error": "巨潮数据源未启用"}
        return self._cninfo.search_announcements(ticker, keyword, count)

    # === 政策信源 (懒加载) ===
    def get_policy_manager(self):
        """获取政策信源管理器 (懒加载, PRD 6.3 V2 新增)"""
        if self._policy_manager is None:
            try:
                from stock_agent.dataflows.providers.policy.manager import PolicySourceManager
                self._policy_manager = PolicySourceManager(self.config)
                logger.info("[数据源] 政策信源管理器已加载")
            except Exception as e:
                logger.warning(f"[数据源] 政策信源管理器加载失败: {e}")
        return self._policy_manager


# =================================================================
# 单例管理 (供 interface.py 使用)
# =================================================================
_global_manager: Optional[DataSourceManager] = None
_global_config: dict = None


def set_config(config: dict):
    """设置全局配置 (初始化时调用)"""
    global _global_config, _global_manager
    _global_config = config
    _global_manager = None  # 重置单例, 下次 get_manager 时重建


def get_manager() -> DataSourceManager:
    """获取全局 DataSourceManager 单例"""
    global _global_manager
    if _global_manager is None:
        _global_manager = DataSourceManager(_global_config)
    return _global_manager
