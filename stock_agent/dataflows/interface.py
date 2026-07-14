"""
统一数据接口 (对应 PRD 6.10 数据流图 / 8.7 工具调用接口)

提供函数式 API, 供工具层 (tools/) 和发现层 (discovery/) 调用。
内部委托 DataSourceManager 单例, 自动处理 provider 链与故障转移。

使用方式:
    from stock_agent.dataflows.interface import get_stock_data
    data = get_stock_data("600584", "2026-07-01", "2026-07-09")
"""
from stock_agent.dataflows.data_source_manager import get_manager, set_config

# 导出 set_config 供 trading_graph 初始化时调用
__all__ = [
    "set_config",
    "get_stock_data",
    "get_financial_data",
    "get_dragon_tiger",
    "get_fund_flow",
    "get_sector_data",
    "get_sector_stocks",
    "get_news",
    "get_market_index",
    "get_market_movers",
    "get_stock_info",
    "search_announcements",
    "fetch_policy_events",
]


# =================================================================
# 行情数据 (PRD: 技术分析/选股依赖)
# =================================================================
def get_stock_data(ticker: str, start_date: str, end_date: str) -> dict:
    """获取股票日线行情 (OHLCV)

    Args:
        ticker: 股票代码 (如 "600584")
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        {"data": [...], "columns": [...], "source": "..."} 或 {}
    """
    return get_manager().get_stock_data(ticker, start_date, end_date)


def get_stock_info(ticker: str) -> dict:
    """获取股票基本信息 (名称/市值/行业等)"""
    return get_manager().get_stock_info(ticker)


# =================================================================
# 财务数据 (PRD: 基本面分析依赖)
# =================================================================
def get_financial_data(ticker: str) -> dict:
    """获取财务分析指标 (ROE/PE/PB/营收/净利润等)"""
    return get_manager().get_financial_data(ticker)


# =================================================================
# 龙虎榜 (PRD: A 股专属分析依赖)
# =================================================================
def get_dragon_tiger(date: str) -> dict:
    """获取龙虎榜数据"""
    return get_manager().get_dragon_tiger(date)


# =================================================================
# 资金流向 (PRD: A 股专属分析/黑马扫描依赖)
# =================================================================
def get_fund_flow(ticker: str) -> dict:
    """获取个股资金流向"""
    return get_manager().get_fund_flow(ticker)


# =================================================================
# 板块数据 (PRD: 板块推理/选股依赖)
# =================================================================
def get_sector_data() -> dict:
    """获取行业板块行情"""
    return get_manager().get_sector_data()


def get_sector_stocks(sector: str) -> dict:
    """获取板块成分股"""
    return get_manager().get_sector_stocks(sector)


# =================================================================
# 新闻资讯 (PRD: 热点发现依赖)
# =================================================================
def get_news(keyword: str = "", count: int = 20) -> dict:
    """获取财经新闻"""
    return get_manager().get_news(keyword, count)


# =================================================================
# 大盘指数 (PRD 10.2: 熔断检查依赖)
# =================================================================
def get_market_index(symbol: str = "sh000001") -> dict:
    """获取大盘指数 (默认上证指数)"""
    return get_manager().get_market_index(symbol)


# =================================================================
# 市场异动 (PRD 6.6: 黑马扫描依赖)
# =================================================================
def get_market_movers() -> dict:
    """获取市场异动股 (涨幅榜/量比榜)"""
    return get_manager().get_market_movers()


# =================================================================
# 公告检索 (PRD 8.6: 个股发展信息分析依赖)
# =================================================================
def search_announcements(ticker: str, keyword: str = "", count: int = 10) -> dict:
    """检索巨潮公告"""
    return get_manager().search_announcements(ticker, keyword, count)


# =================================================================
# 政策事件 (PRD 6.3: V2 新增政策信源)
# =================================================================
def fetch_policy_events(max_items: int = 20) -> dict:
    """获取最新政策事件 (6 类权威信源)"""
    manager = get_manager()
    policy_mgr = manager.get_policy_manager()
    if policy_mgr is None:
        return {"data": [], "error": "政策信源未启用"}
    return policy_mgr.fetch_latest(max_items)
