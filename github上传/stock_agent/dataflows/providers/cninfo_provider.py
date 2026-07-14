"""
巨潮资讯网数据源 (对应 PRD 8.6 个股发展信息分析数据源)

巨潮资讯网 (cninfo.com.cn) 是证监会指定的法定信息披露平台,
提供 A 股上市公司公告检索。

用于 PRD M4b 个股发展信息分析 (重大合同/并购重组/业绩预告等公告)。

合规: 仅检索公开公告, 摘要提取 + URL 标注。
"""
import requests
from loguru import logger

from stock_agent.dataflows.cache import cached


class CnInfoProvider:
    """巨潮资讯网公告检索 Provider"""

    # 巨潮公告查询 API (公开接口)
    _SEARCH_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    _DETAIL_URL = "http://www.cninfo.com.cn/new/disclosure"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        })

    @cached(ttl=1800)
    def search_announcements(self, ticker: str, keyword: str = "", count: int = 10) -> dict:
        """检索上市公司公告 (PRD 8.6)

        Args:
            ticker: 股票代码 (如 "600584")
            keyword: 搜索关键词 (如 "重大合同", 空则检索最新公告)
            count: 返回条数上限

        Returns:
            {"data": [{title, pub_time, url, ...}]} 或 {}
        """
        try:
            # 转换代码为巨潮格式
            stock_code = ticker.replace(".SH", "").replace(".SZ", "")

            params = {
                "stock": stock_code,
                "tabName": "fulltext",
                "pageSize": str(count),
                "pageNum": "1",
                "category": "",  # 全部类别
                "plate": "",
                "searchkey": keyword,
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }

            resp = self._session.post(self._SEARCH_URL, data=params, timeout=15)
            resp.raise_for_status()
            result = resp.json()

            announcements = result.get("announcements", [])
            if not announcements:
                return {"data": [], "source": "cninfo"}

            # 提取关键字段 (摘要 + URL)
            records = []
            for ann in announcements[:count]:
                title = ann.get("announcementTitle", "")
                pub_time = ann.get("announcementTime", "")
                # 构造公告详情 URL
                ann_id = ann.get("announcementId", "")
                adjunct_url = ann.get("adjunctUrl", "")
                url = f"http://static.cninfo.com.cn/{adjunct_url}" if adjunct_url else ""

                records.append({
                    "title": title,
                    "pub_time": str(pub_time),
                    "url": url,
                    "type": ann.get("announcementTypeName", ""),
                    "source": "cninfo",
                })

            logger.info(f"[巨潮] 检索 {stock_code} 公告: {len(records)} 条")
            return {"data": records, "source": "cninfo"}

        except Exception as e:
            logger.warning(f"[巨潮] search_announcements 失败 ({ticker}): {e}")
            return {"data": [], "error": str(e), "source": "cninfo"}
