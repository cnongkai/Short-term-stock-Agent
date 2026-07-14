# 修复 AkShare 数据源 + 补充财经新闻源 + 分析师工具包重构

## 概述

用户请求 3 项修复:
1. **修复 AkShare 数据源不可用问题** — 上次全流程运行仍有 156 个 RemoteDisconnected 错误
2. **数据源补充新浪财经、东方财富时事新闻等多个财经数据源** — RSS 源失效，需启用 HTTP API
3. **分析师工具包重构** — 不再单独依赖 AkShare，能自行搜索解决问题

## 当前状态分析

### Task 1: AkShare Fallback（已完成 ✅）

经代码审查，[akshare_provider.py](../../stock_agent/dataflows/providers/akshare_provider.py) 已完成全部 fallback 改造：

| 方法 | Fallback 链 | 状态 |
|------|-------------|------|
| `get_stock_data` | stock_zh_a_hist(东财) → stock_zh_a_daily(新浪) → 新浪 HTTP API 直连 | ✅ |
| `get_fund_flow` | stock_individual_fund_flow → rank → 东方财富 HTTP API 直连 | ✅ |
| `get_stock_info` | stock_individual_info_em → stock_info_a_code_name | ✅ |
| `get_sector_data` | stock_board_industry_name_em → stock_sector_spot(新浪) | ✅ |
| `get_sector_stocks` | stock_board_industry_cons_em → stock_sector_detail(新浪) | ✅ |
| `get_dragon_tiger` | 当日 → 近7天范围 | ✅ |
| `get_market_movers` | stock_zh_a_spot_em → stock_zh_a_spot(新浪) | ✅ |

**结论**：Task 1 无需额外代码修改，仅需 smoke test 验证。

### Task 2: Web News Provider HTTP API（部分完成 🔄）

[web_news_provider.py](../../stock_agent/dataflows/providers/web_news_provider.py) 现状：

- ✅ `__init__` 已配置 `requests.Session` + UA + Referer header
- ✅ `_fetch_sina_news_api()` 方法已创建（新浪滚动作闻 API）
- ✅ `_fetch_eastmoney_news_api()` 方法已创建（东方财富个股新闻 API）
- ❌ `get_news()` **未接入 HTTP API fallback** — 当前流程: RSS → 静态新闻（跳过了 HTTP API）
- ❌ `get_stock_news()` **未接入 HTTP API fallback** — 当前流程: RSS → 返回空（跳过了 HTTP API）

### Task 3: 分析师工具包重构（未开始 ❌）

当前分析师工具链 8 个工具全部通过 `data_interface` → `DataSourceManager` → AkShare/BaoStock 获取数据。
当数据源全部失败时，分析师无任何自救能力（不能联网搜索、不能获取实时行情）。

需新增 2 个独立工具（不依赖 provider 链，直接 HTTP 调用）：
- `web_search` — DuckDuckGo HTML 搜索（免费无 API key）
- `realtime_quote` — 新浪实时行情（免费，GBK 编码）

并更新 `tool_registry.py`、`agent_utils.py`、`prompts.py`。

---

## 实施方案

### Step 1: 完成 Task 2 — Web News Provider 接入 HTTP API fallback

**文件**: [web_news_provider.py](../../stock_agent/dataflows/providers/web_news_provider.py)

#### 1.1 改造 `get_news()` (约 118-196 行)

**当前流程**: RSS 聚合 → `if all_news:` 返回 → 否则静态新闻

**改造为 4 级 fallback**: RSS → 新浪 HTTP API → 东方财富 HTTP API → 静态新闻

具体修改：在 `if all_news:` 返回分支之后、`logger.info("[WebNews] RSS 源均无数据...")` 之前，插入 HTTP API fallback：

```python
        if all_news:
            all_news.sort(key=lambda x: x.get("published", ""), reverse=True)
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
```

#### 1.2 改造 `get_stock_news()` (约 274-346 行)

**当前流程**: RSS 按股票名过滤 → 返回结果或空

**改造为 4 级 fallback**: RSS → 东方财富 HTTP API(按 ticker) → 新浪 HTTP API(按股票名) → 返回空

具体修改：在 `if all_news:` 返回分支之后、`except ImportError:` 之前，插入 HTTP API fallback：

```python
            if all_news:
                all_news.sort(key=lambda x: x.get("published", ""), reverse=True)
                return {"data": all_news[:count], "source": "web_news"}

        except ImportError:
            logger.warning("[WebNews] feedparser 未安装, 跳过 RSS 直接使用 HTTP API")

        # === HTTP API fallback (V2: RSS 失效时启用) ===
        logger.info(f"[WebNews] 个股 RSS 无数据, 尝试 HTTP API (ticker={ticker})")

        # 1. 东方财富个股新闻 API (按 ticker 查询, 最精准)
        em_news = self._fetch_eastmoney_news_api(ticker=ticker, count=count)
        if em_news:
            return {"data": em_news, "source": "web_news_eastmoney_api"}

        # 2. 新浪滚动作闻 API (按股票名搜索)
        stock_keywords = {
            "600584": "长电科技", "600519": "贵州茅台", "300750": "宁德时代",
            "000858": "五粮液", "601398": "工商银行", "600036": "招商银行",
        }
        stock_name = stock_keywords.get(ticker, ticker)
        sina_news = self._fetch_sina_news_api(keyword=stock_name, count=count)
        if sina_news:
            return {"data": sina_news, "source": "web_news_sina_api"}

        return {}
```

注意：需将 `stock_keywords` dict 的定义从 `try` 块内提前到方法开头（供 HTTP API fallback 复用）。

---

### Step 2: Task 3 — 创建 `web_search` 工具（DuckDuckGo）

**新建文件**: `stock_agent/tools/web_search.py`

DuckDuckGo HTML 搜索（https://html.duckduckgo.com/html/）免费、无需 API key。
解析搜索结果页 HTML，提取标题/链接/摘要。

```python
"""
网络搜索工具 (V2 新增, 对应 PRD 8.7 工具扩展)

供所有分析师调用, 当数据源工具(依赖 AkShare/BaoStock)失败时,
分析师可通过 web_search 自行联网搜索补充信息, 实现"自救"能力。

数据源: DuckDuckGo HTML 搜索 (免费, 无需 API key)
设计: 独立于 provider 链, 直接 HTTP 调用, 不受 AkShare 故障影响。
"""
import json
import re
from loguru import logger


def create_web_search_tool(config: dict = None):
    """创建网络搜索工具 (DuckDuckGo)"""
    from langchain_core.tools import tool

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """联网搜索财经信息、公司新闻、行业动态等 (DuckDuckGo)。

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
            results = _duckduckgo_search(query, max_results)
            return json.dumps(
                {"data": results, "count": len(results), "source": "duckduckgo"},
                ensure_ascii=False, default=str,
            )
        except Exception as e:
            logger.error(f"[工具] web_search 失败: {e}")
            return json.dumps({"error": str(e), "data": []}, ensure_ascii=False)

    return web_search


def _duckduckgo_search(query: str, max_results: int = 5) -> list:
    """DuckDuckGo HTML 搜索 (无需 API key)

    解析 https://html.duckduckgo.com/html/ 返回的 HTML,
    提取结果链接和摘要。
    """
    import requests
    from urllib.parse import unquote

    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    data = {"q": query, "b": ""}  # POST 表单
    timeout = int((config or {}).get("web_search_timeout", 15))

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
```

**注意**: `_duckduckgo_search` 内引用 `config` 需通过闭包传入。实际实现时需将 `config` 作为模块级变量或调整函数签名（在 `create_web_search_tool` 内定义 `_duckduckgo_search` 为闭包，或传入 config 参数）。计划在实现时将 `_duckduckgo_search` 改为接收 `config` 参数。

---

### Step 3: Task 3 — 创建 `realtime_quote` 工具（新浪实时行情）

**新建文件**: `stock_agent/tools/realtime_quote.py`

新浪实时行情 API (`hq.sinajs.cn`)，免费、无需 key，需 Referer header + GBK 解码。

```python
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
from loguru import logger


def create_realtime_quote_tool(config: dict = None):
    """创建实时行情查询工具 (新浪)"""
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
    import re
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
    try:
        if not val or val == "":
            return None
        f = float(val)
        return f if f == f else None  # 排除 NaN
    except (ValueError, TypeError):
        return None
```

---

### Step 4: Task 3 — 注册新工具到 ToolRegistry

**修改文件**: [tool_registry.py](../../stock_agent/tools/tool_registry.py)

在 `_get_factories()` 方法中添加 2 个新工具的导入和注册：

```python
    @classmethod
    def _get_factories(cls):
        if cls._TOOL_FACTORIES is None:
            from stock_agent.tools.financial_data_query import create_financial_data_query_tool
            from stock_agent.tools.announcement_search import create_announcement_search_tool
            from stock_agent.tools.news_search import create_news_search_tool
            from stock_agent.tools.technical_indicator_calc import create_technical_indicator_calc_tool
            from stock_agent.tools.dragon_tiger_query import create_dragon_tiger_query_tool
            from stock_agent.tools.fund_flow_query import create_fund_flow_query_tool
            from stock_agent.tools.industry_comparison import create_industry_comparison_tool
            from stock_agent.tools.research_report_search import create_research_report_search_tool
            # V2 新增: 独立于 provider 链的自救工具
            from stock_agent.tools.web_search import create_web_search_tool
            from stock_agent.tools.realtime_quote import create_realtime_quote_tool

            cls._TOOL_FACTORIES = {
                "financial_data_query": create_financial_data_query_tool,
                "announcement_search": create_announcement_search_tool,
                "news_search": create_news_search_tool,
                "technical_indicator_calc": create_technical_indicator_calc_tool,
                "dragon_tiger_query": create_dragon_tiger_query_tool,
                "fund_flow_query": create_fund_flow_query_tool,
                "industry_comparison": create_industry_comparison_tool,
                "research_report_search": create_research_report_search_tool,
                # V2 新增
                "web_search": create_web_search_tool,
                "realtime_quote": create_realtime_quote_tool,
            }
        return cls._TOOL_FACTORIES
```

---

### Step 5: Task 3 — 更新分析师工具映射

**修改文件**: [agent_utils.py](../../stock_agent/agents/utils/agent_utils.py)

为**所有分析师**添加 `web_search`（自救搜索能力），为技术/A股专属/基本面分析师添加 `realtime_quote`（实时行情能力）。

```python
        tool_mapping = {
            "fundamentals": [
                "financial_data_query",
                "industry_comparison",
                "research_report_search",
                "realtime_quote",   # V2: 实时价格计算 PE/PB
                "web_search",       # V2: 数据源失败时自救
            ],
            "technical": [
                "technical_indicator_calc",
                "news_search",
                "realtime_quote",   # V2: 获取最新价/支撑压力位
                "web_search",       # V2: 数据源失败时自救
            ],
            "china_specific": [
                "dragon_tiger_query",
                "fund_flow_query",
                "realtime_quote",   # V2: 实时涨跌停判断
                "web_search",       # V2: 数据源失败时自救
            ],
            "stock_development": [
                "announcement_search",
                "news_search",
                "research_report_search",
                "web_search",       # V2: 公告/新闻失败时自救搜索
            ],
            "news": ["news_search", "web_search"],
            "sentiment": ["news_search", "web_search"],
            "policy": ["news_search", "web_search"],
        }
```

并在 Toolkit 类底部添加 2 个便捷属性：

```python
    @property
    def web_search(self):
        return self.tool_registry.get_tool("web_search")

    @property
    def realtime_quote(self):
        return self.tool_registry.get_tool("realtime_quote")
```

---

### Step 6: Task 3 — 更新分析师 Prompt

**修改文件**: [prompts.py](../../stock_agent/agents/utils/prompts.py)

在 4 个 ReAct 分析师 prompt 中添加新工具使用指引（最小改动，追加段落）：

#### 6.1 `FUNDAMENTALS_SYSTEM` — 追加自救工具指引

在 "ReAct 循环工作流程" 段落中追加：

```
🛠️ 自救工具 (V2 新增, 数据源失败时使用):
- realtime_quote: 当 financial_data_query 无价格数据时, 调用获取实时价格
- web_search: 当数据源全部失败时, 联网搜索公司业绩/公告/研报等补充信息
```

#### 6.2 `TECHNICAL_SYSTEM` — 追加实时行情指引

```
🛠️ 自救工具 (V2 新增):
- realtime_quote: 获取最新实时价格/今日开盘/最高最低, 用于判断盘中支撑压力位
- web_search: 当 technical_indicator_calc 返回空时, 联网搜索该股技术面分析
```

#### 6.3 `CHINA_SPECIFIC_SYSTEM` — 追加实时行情指引

```
🛠️ 自救工具 (V2 新增):
- realtime_quote: 获取实时涨跌幅, 判断涨停封板/跌停情况
- web_search: 当龙虎榜/资金流数据缺失时, 联网搜索机构动向/资金流入新闻
```

#### 6.4 `STOCK_DEVELOPMENT_SYSTEM` — 追加搜索指引

```
🛠️ 自救工具 (V2 新增):
- web_search: 当 announcement_search/news_search 均无数据时, 联网搜索个股最新公告/新闻/研报
  示例: web_search(query="长电科技 2026年 重大合同")
```

---

## 假设与决策

1. **DuckDuckGo vs Google/Bing**: 选 DuckDuckGo — 免费、无需 API key、HTML 页面可直接爬取。Google 有反爬验证，Bing 需 key。
2. **新浪实时行情 vs 腾讯行情**: 选新浪 — 项目中已使用新浪 API（K线 fallback），保持一致性。
3. **web_search 结果数**: 默认 5 条 — 避免 LLM 上下文过长，ReAct 单轮工具上限 3 个。
4. **所有分析师都获得 web_search**: 用户明确要求"能自行搜索解决问题"，故全量赋予。
5. **realtime_quote 仅给需要价格的分析师**: news/sentiment/policy 分析师不涉及个股价格，不分配。
6. **不修改 default_config.py**: 新工具的 timeout 复用已有 `akshare_http_timeout` / 新增 `web_search_timeout`（可在 default_config 中可选添加，不添加则用默认值 15s）。

## 验证步骤

1. **Smoke test**: `python -m pytest tests/test_smoke.py -v` — 确认 9/9 通过，无导入错误
2. **Web News 验证**: 运行 `python -c "from stock_agent.dataflows.providers.web_news_provider import WebNewsProvider; p = WebNewsProvider({}); print(p.get_news(count=3)); print(p.get_stock_news('600584', count=3))"` — 确认 HTTP API fallback 返回真实数据
3. **新工具验证**: 
   - `python -c "from stock_agent.tools.web_search import create_web_search_tool; t = create_web_search_tool({}); print(t.invoke({'query': '长电科技 业绩', 'max_results': 3}))"`
   - `python -c "from stock_agent.tools.realtime_quote import create_realtime_quote_tool; t = create_realtime_quote_tool({}); print(t.invoke({'ticker': '600584'}))"`
4. **工具注册验证**: `python -c "from stock_agent.tools.tool_registry import ToolRegistry; r = ToolRegistry({}); print([type(r.get_tool(n)).__name__ for n in ['web_search','realtime_quote']])"`
5. **全流程验证** (可选): `python main.py --date 2026-07-11` — 确认端到端无错误

## 实施顺序

1. Step 1: 改造 web_news_provider.py 的 `get_news()` 和 `get_stock_news()`（Task 2 收尾）
2. Step 2-3: 创建 web_search.py + realtime_quote.py（Task 3 新工具）
3. Step 4: 更新 tool_registry.py（注册新工具）
4. Step 5: 更新 agent_utils.py（工具映射）
5. Step 6: 更新 prompts.py（工具指引）
6. 运行 smoke test 验证
7. 逐个验证新工具功能
