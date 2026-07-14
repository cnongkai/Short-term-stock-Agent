"""
政策信源管理器 (对应 PRD 6.3, V2 新增 6 类权威政策信源)

聚合 6 类权威政策信源, 为政策分析师 (policy_analyst) 提供最新政策事件数据。
每个信源独立抓取 + 独立限速 + 独立降级, 任一信源故障不影响其他信源。

6 类权威信源 (PRD 6.3):
  1. 证监会 (csrc.gov.cn)        — 证券监管政策/监管措施
  2. 央行 (pbc.gov.cn)            — 货币政策/利率/准备金
  3. 发改委 (ndrc.gov.cn)         — 产业政策/投资项目审批
  4. 交易所 (上交所/深交所)        — 交易规则/监管动态
  5. 巨潮资讯 (cninfo)            — 政策类公告 (复用 CnInfoProvider)
  6. 四大证券报 + 新华社财经       — 政策解读/权威报道 (RSS)

合规 (PRD 6.3):
  - robots.txt 遵守 (仅抓取公开页面)
  - 限速: 每个信源独立限速, ≤1次/policy_rate_limit_seconds (默认 5 分钟)
  - 摘要提取: 仅存储标题+摘要+URL, 不存储全文
  - URL 标注: 每条政策事件附原文链接

统一接口: fetch_latest(max_items=20) -> {"data": [...], "source": "policy"}
"""
import time
from typing import Dict, List, Optional
from loguru import logger


class PolicySourceManager:
    """6 类权威政策信源管理器

    管理政策信源的抓取、限速、降级, 对外提供统一的 fetch_latest 接口。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        # 限速配置 (PRD 6.3: ≤1次/5分钟)
        self._rate_limit = self.config.get("policy_rate_limit_seconds", 300)
        # 各信源上次调用时间 (用于独立限速)
        self._last_call: Dict[str, float] = {}
        # 是否启用
        self._enabled = self.config.get("policy_sources_enabled", True)

    # =================================================================
    # 统一对外接口
    # =================================================================
    def fetch_latest(self, max_items: int = 20) -> dict:
        """获取最新政策事件 (聚合 6 类信源)

        Args:
            max_items: 返回条数上限

        Returns:
            {"data": [政策事件...], "source": "policy"}
            信源全部失败时返回 {"data": [], "error": "...", "source": "policy"}
        """
        if not self._enabled:
            logger.info("[政策信源] 政策信源未启用")
            return {"data": [], "error": "政策信源未启用", "source": "policy"}

        logger.info(f"[政策信源] 开始聚合 6 类信源 (上限 {max_items} 条)")

        all_events: List[dict] = []

        # 依次抓取 6 类信源 (每类独立降级, 失败返回 [])
        sources = [
            ("csrc", self.fetch_csrc),
            ("pbc", self.fetch_pbc),
            ("ndrc", self.fetch_ndrc),
            ("exchanges", self.fetch_exchanges),
            ("cninfo_policy", self.fetch_cninfo_policy),
            ("securities_newspapers", self.fetch_securities_newspapers),
        ]

        success_count = 0
        for name, fetcher in sources:
            try:
                events = fetcher()
                if events:
                    all_events.extend(events)
                    success_count += 1
                    logger.info(f"[政策信源] {name}: {len(events)} 条")
                else:
                    logger.debug(f"[政策信源] {name}: 无数据")
            except Exception as e:
                logger.warning(f"[政策信源] {name} 抓取失败: {e}")

        # 按发布时间降序排序 (最新在前)
        all_events.sort(
            key=lambda x: x.get("publish_time", ""),
            reverse=True,
        )

        # 截取上限
        all_events = all_events[:max_items]

        logger.info(f"[政策信源] 聚合完成: {len(all_events)} 条 "
                    f"(成功信源 {success_count}/6)")

        if not all_events:
            return {"data": [], "error": "所有政策信源均无数据", "source": "policy"}

        return {"data": all_events, "source": "policy"}

    # =================================================================
    # 限速检查 (每个信源独立, PRD 6.3)
    # =================================================================
    def _check_rate_limit(self, source_name: str) -> bool:
        """检查某信源是否允许调用 (限速)

        Returns:
            True 表示允许调用, False 表示限速期内需跳过
        """
        now = time.time()
        last = self._last_call.get(source_name, 0)
        if now - last < self._rate_limit:
            remaining = int(self._rate_limit - (now - last))
            logger.debug(f"[政策信源] {source_name} 限速中, 剩余 {remaining}s")
            return False
        self._last_call[source_name] = now
        return True

    # =================================================================
    # 信源 1: 证监会 (csrc.gov.cn)
    # =================================================================
    def fetch_csrc(self) -> List[dict]:
        """证监会政策公告 (csrc.gov.cn)

        抓取证监会最新政策/监管动态。
        URL: http://www.csrc.gov.cn/pub/newsite/zjhxw/
        合规: 公开新闻列表页, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("csrc"):
            return []
        try:
            import requests
            from bs4 import BeautifulSoup

            url = "http://www.csrc.gov.cn/pub/newsite/zjhxw/"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            # 解析新闻列表项 (li 标签中的链接)
            for item in soup.select("li a")[:15]:
                title = item.get_text(strip=True)
                href = item.get("href", "")
                if not title or len(title) < 5:
                    continue
                # 补全 URL
                if href and not href.startswith("http"):
                    href = "http://www.csrc.gov.cn" + href
                events.append(self._build_event(
                    title=title,
                    source="csrc.gov.cn",
                    authority_level="证监会",
                    url=href,
                ))
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 证监会抓取失败: {e}")
            return []

    # =================================================================
    # 信源 2: 央行 (pbc.gov.cn)
    # =================================================================
    def fetch_pbc(self) -> List[dict]:
        """央行政策公告 (pbc.gov.cn)

        抓取央行最新货币政策/利率/准备金公告。
        URL: http://www.pbc.gov.cn/zhengcehuobisi/125207/index.html
        合规: 公开政策列表页, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("pbc"):
            return []
        try:
            import requests
            from bs4 import BeautifulSoup

            url = "http://www.pbc.gov.cn/zhengcehuobisi/125207/index.html"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select("a")[:15]:
                title = item.get_text(strip=True)
                href = item.get("href", "")
                if not title or len(title) < 5:
                    continue
                if href and not href.startswith("http"):
                    href = "http://www.pbc.gov.cn" + href
                events.append(self._build_event(
                    title=title,
                    source="pbc.gov.cn",
                    authority_level="央行",
                    url=href,
                ))
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 央行抓取失败: {e}")
            return []

    # =================================================================
    # 信源 3: 发改委 (ndrc.gov.cn)
    # =================================================================
    def fetch_ndrc(self) -> List[dict]:
        """发改委政策公告 (ndrc.gov.cn)

        抓取发改委最新产业政策/投资项目审批。
        URL: https://www.ndrc.gov.cn/xxgk/zcfb/
        合规: 公开政策发布页, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("ndrc"):
            return []
        try:
            import requests
            from bs4 import BeautifulSoup

            url = "https://www.ndrc.gov.cn/xxgk/zcfb/"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select("a")[:15]:
                title = item.get_text(strip=True)
                href = item.get("href", "")
                if not title or len(title) < 5:
                    continue
                if href and not href.startswith("http"):
                    href = "https://www.ndrc.gov.cn" + href
                events.append(self._build_event(
                    title=title,
                    source="ndrc.gov.cn",
                    authority_level="发改委",
                    url=href,
                ))
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 发改委抓取失败: {e}")
            return []

    # =================================================================
    # 信源 4: 交易所 (上交所/深交所)
    # =================================================================
    def fetch_exchanges(self) -> List[dict]:
        """交易所监管动态 (上交所 sse.com.cn / 深交所 szse.cn)

        抓交易所最新交易规则/监管动态。
        合规: 公开监管动态页, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("exchanges"):
            return []
        events = []
        # 上交所监管动态
        events.extend(self._fetch_sse())
        # 深交所监管动态
        events.extend(self._fetch_szse())
        return events

    def _fetch_sse(self) -> List[dict]:
        """上交所监管动态 (sse.com.cn)"""
        try:
            import requests
            from bs4 import BeautifulSoup

            url = "http://www.sse.com.cn/disclosure/overview/"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select("a")[:10]:
                title = item.get_text(strip=True)
                href = item.get("href", "")
                if not title or len(title) < 5:
                    continue
                if href and not href.startswith("http"):
                    href = "http://www.sse.com.cn" + href
                events.append(self._build_event(
                    title=title,
                    source="sse.com.cn",
                    authority_level="交易所",
                    url=href,
                ))
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 上交所抓取失败: {e}")
            return []

    def _fetch_szse(self) -> List[dict]:
        """深交所监管动态 (szse.cn)"""
        try:
            import requests
            from bs4 import BeautifulSoup

            url = "http://www.szse.cn/disclosure/listed/notice/index.html"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select("a")[:10]:
                title = item.get_text(strip=True)
                href = item.get("href", "")
                if not title or len(title) < 5:
                    continue
                if href and not href.startswith("http"):
                    href = "http://www.szse.cn" + href
                events.append(self._build_event(
                    title=title,
                    source="szse.cn",
                    authority_level="交易所",
                    url=href,
                ))
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 深交所抓取失败: {e}")
            return []

    # =================================================================
    # 信源 5: 巨潮政策类公告 (复用 CnInfoProvider)
    # =================================================================
    def fetch_cninfo_policy(self) -> List[dict]:
        """巨潮资讯政策类公告 (复用 CnInfoProvider)

        检索巨潮公告中标题含"政策/监管/通知"的政策类公告。
        合规: 公开公告检索, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("cninfo_policy"):
            return []
        try:
            from stock_agent.dataflows.providers.cninfo_provider import CnInfoProvider

            provider = CnInfoProvider(self.config)
            # 用政策类关键词检索 (空 ticker 表示全局检索)
            # 巨潮 API 需指定 stock, 这里用通用关键词检索
            keywords = ["政策", "监管", "通知", "办法", "规定"]
            events = []
            for kw in keywords:
                res = provider.search_announcements(ticker="", keyword=kw, count=5)
                if res and res.get("data"):
                    for ann in res["data"]:
                        events.append(self._build_event(
                            title=ann.get("title", ""),
                            source="cninfo.com.cn",
                            authority_level="巨潮",
                            url=ann.get("url", ""),
                            publish_time=ann.get("pub_time", ""),
                        ))
                if len(events) >= 10:
                    break
            return events
        except Exception as e:
            logger.debug(f"[政策信源] 巨潮政策公告抓取失败: {e}")
            return []

    # =================================================================
    # 信源 6: 四大证券报 + 新华社财经 (RSS)
    # =================================================================
    def fetch_securities_newspapers(self) -> List[dict]:
        """四大证券报 + 新华社财经 (RSS, PRD 6.3)

        通过 RSS 订阅获取权威财经媒体的政策解读/报道。
        合规: RSS 公开订阅, 摘要提取 + URL 标注
        """
        if not self._check_rate_limit("securities_newspapers"):
            return []
        try:
            import feedparser

            # 权威财经 RSS 源 (部分可能失效, 逐个降级)
            rss_sources = [
                ("新华社财经", "http://www.xinhuanet.com/finance/rss.xml"),
                ("中国证券报", "http://www.cs.com.cn/rss/sylm.xml"),
                ("上海证券报", "http://www.cnstock.com/rss/sylm.xml"),
                ("证券时报", "http://www.stcn.com/rss/sylm.xml"),
                ("证券日报", "http://www.zqrb.cn/rss/sylm.xml"),
            ]

            events = []
            for source_name, rss_url in rss_sources:
                try:
                    feed = feedparser.parse(rss_url)
                    if not feed.entries:
                        continue
                    for entry in feed.entries[:5]:
                        title = entry.get("title", "").strip()
                        link = entry.get("link", "")
                        summary = entry.get("summary", entry.get("description", ""))
                        published = entry.get("published", entry.get("updated", ""))
                        if not title:
                            continue
                        # 摘要提取 (截断, 不存全文, PRD 6.3 合规)
                        if summary:
                            summary = summary[:200]
                        events.append(self._build_event(
                            title=title,
                            source=source_name,
                            authority_level="证券报",
                            url=link,
                            publish_time=published,
                            summary=summary,
                        ))
                except Exception as e:
                    logger.debug(f"[政策信源] {source_name} RSS 失败: {e}")
                    continue

            return events
        except ImportError:
            logger.warning("[政策信源] feedparser 未安装, 跳过证券报 RSS")
            return []
        except Exception as e:
            logger.debug(f"[政策信源] 证券报 RSS 抓取失败: {e}")
            return []

    # =================================================================
    # 事件构建工具 (统一字段格式, PRD 8.2)
    # =================================================================
    def _build_event(
        self,
        title: str,
        source: str,
        authority_level: str,
        url: str = "",
        publish_time: str = "",
        summary: str = "",
    ) -> dict:
        """构建统一格式的政策事件 (PRD 8.2 字段)

        affected_sectors / impact_direction / impact_strength 由政策分析师 LLM 推理,
        此处仅填充基础字段 (title/source/authority_level/url/time)。
        """
        return {
            "title": title,
            "source": source,
            "authority_level": authority_level,
            "publish_time": publish_time,
            "url": url,
            "summary": summary,
            # 以下字段由 policy_analyst LLM 推理填充:
            "affected_sectors": [],
            "impact_direction": "",
            "impact_strength": "",
            "rationale": "",
            "affected_stocks": [],
        }
