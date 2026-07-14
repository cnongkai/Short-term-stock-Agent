"""
网络搜索工具 (V2 新增, 对应 PRD 8.7 工具扩展)

供所有分析师调用, 当数据源工具(依赖 AkShare/BaoStock)失败时,
分析师可通过 web_search 自行联网搜索补充信息, 实现"自救"能力。

数据源 (双 fallback):
  1. Bing HTML 搜索 (cn.bing.com, 国内可直连, 免费, 无需 API key)
  2. DuckDuckGo HTML 搜索 (海外, 作为备用)

设计: 独立于 provider 链, 直接 HTTP 调用, 不受 AkShare 故障影响。
"""
import json
import re
from urllib.parse import unquote, quote_plus

from loguru import logger


def create_web_search_tool(config: dict = None):
    """创建网络搜索工具 (Bing + DuckDuckGo fallback)

    Args:
        config: 系统配置 (读取 web_search_timeout)

    Returns:
        langchain Tool 实例
    """
    from langchain_core.tools import tool

    _config = config or {}
    _timeout = int(_config.get("web_search_timeout", 15))

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """联网搜索财经信息、公司新闻、行业动态等 (Bing/DuckDuckGo)。

        当 financial_data_query / news_search 等数据源工具返回空或失败时,
        可调用本工具自行搜索补充信息。适用于:
        - 查询公司最新公告/业绩/研报
        - 搜索行业政策/市场动态
        - 获取分析师无法从数据源获取的任意信息

        Args:
            query: 搜索关键词 (建议中文, 如 "长电科技 2026年 业绩预告")
            max_results: 返回结果数上限, 默认 5

        Returns:
            JSON 字符串, 含搜索结果列表 [{title, url, snippet}]
        """
        logger.info(f"[工具] web_search | query={query}, max={max_results}")
        try:
            # 主搜索: Bing (国内可直连)
            results = _bing_search(query, max_results, _timeout)
            source = "bing"
            if not results:
                # 备用: DuckDuckGo (海外, 可能超时)
                logger.debug("[web_search] Bing 无结果, 尝试 DuckDuckGo")
                results = _duckduckgo_search(query, max_results, _timeout)
                source = "duckduckgo"
            return json.dumps(
                {"data": results, "count": len(results), "source": source},
                ensure_ascii=False, default=str,
            )
        except Exception as e:
            logger.error(f"[工具] web_search 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return web_search


def _bing_search(query: str, max_results: int = 5, timeout: int = 15) -> list:
    """Bing HTML 搜索 (国内可直连, 无需 API key)

    解析 https://cn.bing.com/search?q=... 返回的 HTML,
    提取结果链接和摘要。

    Args:
        query: 搜索关键词
        max_results: 返回结果数上限
        timeout: HTTP 超时秒数

    Returns:
        [{title, url, snippet}, ...]
    """
    import requests

    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    results = []
    # Bing 搜索结果块: <li class="b_algo"> ... <h2><a href="...">标题</a></h2> ... <p>摘要</p>
    # 按结果块分割
    algo_blocks = re.findall(
        r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>',
        html, re.DOTALL,
    )
    for block in algo_blocks:
        # 提取标题和链接 (Bing 标题在 <h2> 内的 <a> 标签, 非首个 siteicon <a>)
        link_m = re.search(r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link_m:
            continue
        clean_url = link_m.group(1)
        title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()

        # 提取摘要 (Bing 摘要通常在 <p> 或 b_lineclamp 标签内)
        snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""

        # 过滤 Bing 内部链接
        if title and clean_url and "bing.com" not in clean_url:
            results.append({
                "title": title,
                "url": clean_url,
                "snippet": snippet[:300],
            })
        if len(results) >= max_results:
            break

    logger.debug(f"[web_search] Bing: {len(results)} 条 (query={query})")
    return results


def _duckduckgo_search(query: str, max_results: int = 5, timeout: int = 15) -> list:
    """DuckDuckGo HTML 搜索 (备用, 海外)

    解析 https://html.duckduckgo.com/html/ 返回的 HTML,
    提取结果链接和摘要。

    Args:
        query: 搜索关键词
        max_results: 返回结果数上限
        timeout: HTTP 超时秒数

    Returns:
        [{title, url, snippet}, ...]
    """
    import requests

    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    data = {"q": query, "b": ""}  # POST 表单

    resp = requests.post(url, headers=headers, data=data, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    results = []
    # DuckDuckGo HTML 结果块: <a class="result__a" href="...">标题</a>
    # 摘要: <a class="result__snippet" ...>摘要</a>
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    )
    for raw_url, title_html, snippet_html in blocks:
        # DuckDuckGo 链接格式: //duckduckgo.com/l/?uddg=<encoded_url>&...
        if "uddg=" in raw_url:
            m = re.search(r"uddg=([^&]+)", raw_url)
            if m:
                clean_url = unquote(m.group(1))
            else:
                continue
        else:
            clean_url = raw_url

        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()
        if title and clean_url:
            results.append({
                "title": title,
                "url": clean_url,
                "snippet": snippet[:300],
            })
        if len(results) >= max_results:
            break

    logger.debug(f"[web_search] DuckDuckGo: {len(results)} 条 (query={query})")
    return results
