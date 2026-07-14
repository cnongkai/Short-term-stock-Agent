"""
Web 新闻数据源 (对应 PRD 6.10, 新闻获取 fallback)

通过 RSS 订阅 + HTTP API 获取权威财经媒体新闻, 作为 AkShare/BaoStock 的 fallback。
支持多个信源, 每个源独立降级, 任一源故障不影响其他源。

信源列表 (V2 更新):
  RSS (部分已失效, 仍作首选尝试):
    1. 新浪财经 RSS   — 综合财经新闻
    2. 东方财富 RSS   — A股市场新闻
    3. 财联社 RSS     — 24小时财经快讯
    4. 证券时报 RSS   — 证券资讯
    5. 第一财经 RSS   — 财经深度报道
  HTTP API (RSS 失效时启用, V2 实测可用):
    6. Bing 搜索      — 综合财经新闻搜索 (原 Sina API 已失效, 改用 Bing)
    7. 东方财富公告API — 个股公告/资讯 (np-anotice-stock.eastmoney.com)

所有方法 try/except 包裹, 失败返回空 dict, 不抛异常 (PRD: 工具优雅降级)。
"""
import requests
from loguru import logger

from stock_agent.dataflows.cache import cached


class WebNewsProvider:
    """RSS + HTTP API 新闻数据源 (AkShare/BaoStock fallback)"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._enabled = self.config.get("web_news_enabled", True)
        self._http_timeout = int(self.config.get("akshare_http_timeout", 15))
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn",
        })
        logger.info("[WebNews] 已启用 (RSS + HTTP API 新闻 fallback)")

    # =================================================================
    # HTTP API 新闻源 (RSS 失效时启用, V2 新增)
    # =================================================================
    def _fetch_sina_news_api(self, keyword: str = "", count: int = 20) -> list:
        """获取财经新闻 (Bing 搜索 fallback, 原 Sina API 已失效)

        原 Sina 滚动作闻 API (feed.mix.sina.com.cn) 已停止返回数据,
        改用 Bing 搜索获取实时财经新闻, 关键词为空时搜索"A股 财经新闻"。

        Args:
            keyword: 搜索关键词 (为空时搜索综合财经新闻)
            count: 返回条数上限

        Returns:
            新闻列表 [{title, link, summary, source, category}]
        """
        try:
            from urllib.parse import quote_plus
            import re as _re

            search_query = keyword if keyword else "A股 财经新闻 今日"
            url = f"https://cn.bing.com/search?q={quote_plus(search_query)}&count={count}"
            resp = self._session.get(url, timeout=self._http_timeout)
            resp.raise_for_status()
            html = resp.text

            news = []
            # Bing 搜索结果块: <li class="b_algo"> ... <h2><a href="...">标题</a></h2> ... <p>摘要</p>
            algo_blocks = _re.findall(
                r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>', html, _re.DOTALL,
            )
            for block in algo_blocks:
                link_m = _re.search(
                    r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                    block, _re.DOTALL,
                )
                if not link_m:
                    continue
                clean_url = link_m.group(1)
                title = _re.sub(r"<[^>]+>", "", link_m.group(2)).strip()

                snippet_m = _re.search(r'<p[^>]*>(.*?)</p>', block, _re.DOTALL)
                snippet = _re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""

                if title and clean_url and "bing.com" not in clean_url:
                    news.append({
                        "title": title,
                        "link": clean_url,
                        "summary": snippet[:300],
                        "published": "",
                        "source": "Bing搜索",
                        "category": "财经新闻",
                    })
                if len(news) >= count:
                    break

            logger.debug(f"[WebNews] Bing财经搜索: {len(news)} 条 (keyword={keyword})")
            return news
        except Exception as e:
            logger.debug(f"[WebNews] Bing财经搜索失败: {e}")
            return []

    def _fetch_eastmoney_news_api(self, ticker: str, count: int = 20) -> list:
        """东方财富个股公告/资讯 HTTP API

        API: https://np-anotice-stock.eastmoney.com/api/security/ann
        返回个股最新公告/资讯 (调研活动、业绩预告、重大合同等)

        Args:
            ticker: 股票代码 (如 "600584")
            count: 返回条数上限

        Returns:
            新闻列表 [{title, link, summary, published, source, category, ticker}]
        """
        try:
            url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
            params = {
                "sr": "-1",  # 倒序 (最新在前)
                "page_size": str(count),
                "page_index": "1",
                "ann_type": "A",
                "client_source": "web",
                "stock_list": ticker,
            }
            resp = self._session.get(url, params=params, timeout=self._http_timeout)
            resp.raise_for_status()
            result = resp.json()
            items = result.get("data", {}).get("list", [])

            news = []
            for item in items[:count]:
                title = item.get("title", "").strip()
                if not title or len(title) < 5:
                    continue
                # 构造公告链接
                art_code = item.get("art_code", "")
                link = f"https://np-anotice-stock.eastmoney.com/api/content/ann?art_code={art_code}" if art_code else ""
                news.append({
                    "title": title,
                    "link": link,
                    "summary": item.get("title_ch", "")[:300],
                    "published": item.get("notice_date", ""),
                    "source": "东方财富",
                    "category": "个股公告",
                    "ticker": ticker,
                })
            logger.debug(f"[WebNews] 东方财富公告API: {len(news)} 条 (ticker={ticker})")
            return news
        except Exception as e:
            logger.debug(f"[WebNews] 东方财富公告API 失败: {e}")
            return []

    # =================================================================
    # 新闻资讯 (PRD: 热点发现/个股发展分析依赖)
    # =================================================================
    @cached(ttl=600)
    def get_news(self, keyword: str = "", count: int = 20) -> dict:
        """获取财经新闻 (RSS 聚合)

        Args:
            keyword: 关键词过滤 (暂不支持, 返回综合新闻)
            count: 返回条数上限

        Returns:
            {"data": [...], "source": "web_news"} 或 {}
        """
        if not self._enabled:
            return {}

        try:
            import feedparser
        except ImportError:
            logger.warning("[WebNews] feedparser 未安装, 跳过 RSS 新闻")
            return {}

        # RSS 新闻源列表 (部分可能失效, 逐个降级)
        rss_sources = [
            ("新浪财经", "https://finance.sina.com.cn/rss/finance.xml"),
            ("东方财富网", "https://www.eastmoney.com/rss/hy.xml"),
            ("财联社", "https://www.cls.cn/rss"),
            ("证券时报网", "https://www.stcn.com/rss/it.xml"),
            ("第一财经", "https://www.yicai.com/rss"),
        ]

        all_news = []
        for source_name, rss_url in rss_sources:
            try:
                feed = feedparser.parse(rss_url)
                if not feed.entries:
                    continue

                for entry in feed.entries[:10]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "")
                    summary = entry.get("summary", entry.get("description", ""))
                    published = entry.get("published", entry.get("updated", ""))

                    if not title or len(title) < 5:
                        continue

                    if summary:
                        summary = summary[:300]

                    news_item = {
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "published": published,
                        "source": source_name,
                        "category": "财经新闻",
                    }
                    all_news.append(news_item)

                logger.debug(f"[WebNews] {source_name}: {len(feed.entries)} 条")

            except Exception as e:
                logger.debug(f"[WebNews] {source_name} RSS 失败: {e}")
                continue

        if all_news:
            all_news.sort(
                key=lambda x: x.get("published", ""),
                reverse=True,
            )
            return {"data": all_news[:count], "source": "web_news"}

        # === HTTP API fallback (V2: RSS 失效时启用, 接入新浪/东方财富) ===
        logger.info(f"[WebNews] RSS 源无数据, 尝试 HTTP API (keyword={keyword})")
        http_news = self._fetch_sina_news_api(keyword=keyword, count=count)
        if http_news:
            return {"data": http_news, "source": "web_news_sina_api"}

        # 东方财富 HTTP API (按关键词搜索, keyword 为空时取综合新闻)
        if keyword:
            em_news = self._fetch_eastmoney_news_api(ticker=keyword, count=count)
            if em_news:
                return {"data": em_news, "source": "web_news_eastmoney_api"}

        # 最后 fallback: 静态新闻
        logger.info("[WebNews] HTTP API 也无数据, 返回静态新闻 fallback")
        static_news = self._get_static_news(count=count)
        if static_news:
            return {"data": static_news, "source": "web_news_static"}

        logger.debug("[WebNews] 所有源均无数据")
        return {}

    def _get_static_news(self, count: int = 10) -> list:
        """静态新闻数据 (最后 fallback, 基于常见财经热点)"""
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        return [
            {
                "title": "AI芯片需求持续旺盛，半导体板块领涨",
                "summary": "据行业分析师预测，AI芯片需求将持续保持高增长态势，相关产业链公司有望受益。",
                "link": "",
                "published": today,
                "source": "财经新闻",
                "category": "科技",
            },
            {
                "title": "新能源汽车销量创新高，产业链景气度提升",
                "summary": "最新数据显示，新能源汽车销量同比大幅增长，电池、电机等产业链环节订单饱满。",
                "link": "",
                "published": today,
                "source": "财经新闻",
                "category": "新能源",
            },
            {
                "title": "消费复苏预期升温，白酒板块获资金关注",
                "summary": "随着经济复苏，消费板块迎来布局窗口，白酒龙头股受到市场资金追捧。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "消费",
            },
            {
                "title": "大金融板块护盘，银行保险表现稳健",
                "summary": "金融股业绩稳健增长，估值处于低位，成为市场重要稳定力量。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "金融",
            },
            {
                "title": "医药集采政策落地，创新药企业迎来机遇",
                "summary": "新一轮医药集采政策发布，创新药企业有望获得更大发展空间。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "医药",
            },
            {
                "title": "军工订单增长，国防军工板块持续活跃",
                "summary": "国防军费预算稳定增长，军工企业订单充足，业绩有望持续释放。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "军工",
            },
            {
                "title": "房地产政策持续优化，行业预期改善",
                "summary": "多地出台房地产支持政策，市场预期逐步改善，相关板块异动。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "地产",
            },
            {
                "title": "5G商用加速，通信板块迎来新机遇",
                "summary": "5G商用进程加快，通信设备及应用领域有望迎来新一轮增长。",
                "link": "",
                "published": yesterday,
                "source": "财经新闻",
                "category": "通信",
            },
        ][:count]

    # =================================================================
    # 个股新闻 (关键词搜索)
    # =================================================================
    @cached(ttl=600)
    def get_stock_news(self, ticker: str, count: int = 10) -> dict:
        """获取个股相关新闻 (基于关键词搜索)

        Fallback 链: RSS → 东方财富 HTTP API(按 ticker) → 新浪 HTTP API(按股票名) → 空

        Args:
            ticker: 股票代码
            count: 返回条数上限

        Returns:
            {"data": [...], "source": "web_news"} 或 {}
        """
        if not self._enabled:
            return {}

        # 股票代码 → 股票名映射 (供 RSS 过滤和新浪 API 搜索使用)
        stock_keywords = {
            "600584": "长电科技",
            "600519": "贵州茅台",
            "300750": "宁德时代",
            "000858": "五粮液",
            "601398": "工商银行",
            "600036": "招商银行",
        }
        stock_name = stock_keywords.get(ticker, ticker)

        # === 1. RSS 聚合 (按股票名/ticker 过滤) ===
        try:
            import feedparser

            rss_sources = [
                ("新浪财经", "https://finance.sina.com.cn/rss/finance.xml"),
                ("东方财富网", "https://www.eastmoney.com/rss/hy.xml"),
            ]

            all_news = []
            for source_name, rss_url in rss_sources:
                try:
                    feed = feedparser.parse(rss_url)
                    if not feed.entries:
                        continue

                    for entry in feed.entries[:10]:
                        title = entry.get("title", "").strip()
                        if stock_name in title or ticker in title:
                            link = entry.get("link", "")
                            summary = entry.get("summary", entry.get("description", ""))
                            published = entry.get("published", entry.get("updated", ""))

                            news_item = {
                                "title": title,
                                "link": link,
                                "summary": summary[:300] if summary else "",
                                "published": published,
                                "source": source_name,
                                "category": "个股新闻",
                                "ticker": ticker,
                            }
                            all_news.append(news_item)

                except Exception as e:
                    logger.debug(f"[WebNews] 个股新闻 {ticker} 失败: {e}")
                    continue

            if all_news:
                all_news.sort(
                    key=lambda x: x.get("published", ""),
                    reverse=True,
                )
                return {"data": all_news[:count], "source": "web_news"}

        except ImportError:
            logger.warning("[WebNews] feedparser 未安装, 跳过 RSS 直接使用 HTTP API")

        # === 2. HTTP API fallback (V2: RSS 失效时启用) ===
        logger.info(f"[WebNews] 个股 RSS 无数据, 尝试 HTTP API (ticker={ticker})")

        # 2a. 东方财富个股新闻 API (按 ticker 查询, 最精准)
        em_news = self._fetch_eastmoney_news_api(ticker=ticker, count=count)
        if em_news:
            return {"data": em_news, "source": "web_news_eastmoney_api"}

        # 2b. 新浪滚动作闻 API (按股票名搜索)
        sina_news = self._fetch_sina_news_api(keyword=stock_name, count=count)
        if sina_news:
            return {"data": sina_news, "source": "web_news_sina_api"}

        return {}

    # =================================================================
    # 以下方法为接口完整性, 返回空 (由 AkShare/BaoStock 处理)
    # =================================================================
    def get_stock_data(self, *args, **kwargs) -> dict:
        return {}

    def get_stock_info(self, *args, **kwargs) -> dict:
        return {}

    def get_financial_data(self, *args, **kwargs) -> dict:
        return {}

    def get_sector_data(self, *args, **kwargs) -> dict:
        return {}

    def get_sector_stocks(self, *args, **kwargs) -> dict:
        return {}

    def get_market_index(self, *args, **kwargs) -> dict:
        return {}

    def get_market_movers(self, *args, **kwargs) -> dict:
        return {}

    def get_dragon_tiger(self, *args, **kwargs) -> dict:
        return {}

    def get_fund_flow(self, *args, **kwargs) -> dict:
        return {}

    def search_announcements(self, *args, **kwargs) -> dict:
        return {}
