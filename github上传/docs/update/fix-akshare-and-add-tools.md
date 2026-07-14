# 修复 AkShare 数据源 + 增加财经新闻源 + 分析师自服务工具

## Context（背景）

上次全流程运行中，日志出现 **156 次 RemoteDisconnected** 和 **195 次"均无数据"** 警告。核心问题：
1. `get_stock_data`（stock_zh_a_hist）**无 fallback**，East Money 断连时直接返回空，BaoStock 兜底但字段名不同
2. `get_fund_flow` 主备两条路径都走东方财富，同时失败
3. RSS 新闻源全部失效，退化为静态假新闻
4. 分析师工具全部依赖 `data_interface` → provider 链 → AkShare，无自主搜索能力

本次修复目标：
1. **修复 AkShare**：`get_stock_data` 加三级 fallback（东财→新浪 akshare→新浪 HTTP 直连），`get_fund_flow` 加东财 HTTP 直连
2. **增加新闻源**：WebNewsProvider 加新浪/东方财富 HTTP API 新闻源（RSS 失效时启用）
3. **分析师自服务**：新增 `web_search`（DuckDuckGo）+ `realtime_quote`（新浪实时行情）两个工具，注册到所有分析师，让其能在数据源失败时自行搜索

## 实施顺序

Task 1（修复 AkShare）→ Task 2（增加新闻源）→ Task 3（新增自服务工具）

---

## Task 1：修复 AkShare 数据源

**文件**：[akshare_provider.py](../../stock_agent/dataflows/providers/akshare_provider.py)

### 1.1 改造 `get_stock_data`（第 118-144 行）— 三级 fallback

将直接调用 `stock_zh_a_hist` 改为 `_try_chain` 三级候选：

1. `stock_zh_a_hist`（东方财富，当前主源）
2. `stock_zh_a_daily`（新浪，akshare 封装，需 `sh`/`sz` 前缀符号）
3. `_fallback_sina_kline_http`（直接新浪 HTTP API，绕过 akshare）

**新增 3 个辅助方法**：
- `_sina_symbol(ticker)` → 生成 `sh600584` / `sz000001` 格式
- `_fallback_sina_kline_http(ticker, start, end)` → 调用 `https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&datalen=1023`，解析 jsonp 返回 DataFrame
- `_normalize_kline_df(df)` → 新浪英文字段（day/open/high/low/close/volume）归一化为 AkShare 中文字段（日期/开盘/最高/最低/收盘/成交量），保证下游 `technical_indicator_calc` 的字段兼容

### 1.2 改造 `get_fund_flow`（第 243-273 行）— 三级 fallback

1. `stock_individual_fund_flow`（当前主源）
2. `_fallback_fund_flow_rank`（当前 rank fallback）
3. `_fallback_fund_flow_http`（直接东财 HTTP API，绕过 akshare）

**新增 `_fallback_fund_flow_http(ticker)`**：
- API: `http://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?secid={market}.{ticker}&lmt=10&klt=101`
- market: 1=沪市(6开头), 0=深市(0/3开头)
- 解析 klines 数组，每行逗号分隔为 日期/主力净流入/小单/中单/大单/超大单/占比

---

## Task 2：增加新浪/东方财富 HTTP 新闻源

**文件**：[web_news_provider.py](../../stock_agent/dataflows/providers/web_news_provider.py)

### 2.1 改造 `__init__` — 添加 requests.Session
```python
self._session = requests.Session()
self._session.headers.update({"User-Agent": "Mozilla/5.0 ...", "Referer": "https://finance.sina.com.cn"})
```

### 2.2 新增 `_fetch_sina_news_api(keyword, count)` — 新浪滚动作闻 API
- API: `https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&k={keyword}&num={count}`
- 返回: `data.list[].title/.url/.ctime/.intro`

### 2.3 新增 `_fetch_eastmoney_news_api(ticker, count)` — 东方财富个股新闻 API
- API: `https://np-listapi.eastmoney.com/comm/web/getNewsByStock?code={ticker}&type=1&pageSize={count}`
- 兼容 `data.list` 和 `data` 直接为列表两种返回格式

### 2.4 改造 `get_news` — fallback 链改为：RSS → 新浪 HTTP API → 东方财富 HTTP API → 静态新闻
在 RSS 全部失败后（第 96 行 `if all_news:` 之前），先尝试 HTTP API，再退到静态新闻。

### 2.5 改造 `get_stock_news` — 同样在 RSS 失败后插入 HTTP API
按 ticker 调东方财富个股新闻 API，按股票名称调新浪 API。

---

## Task 3：新增 web_search + realtime_quote 工具

### 3.1 新建 `stock_agent/tools/web_search.py` — DuckDuckGo 搜索工具

**设计要点**：
- 直接 HTTP 调用 DuckDuckGo HTML 搜索（`https://html.duckduckgo.com/html/?q={query}`），不走 provider 链
- `@cached(ttl=1800)` 缓存搜索结果 30 分钟
- 正则解析 HTML 结果块（`result__a` 标题 + `result__snippet` 摘要）
- 解析 `uddg=` 重定向参数获取真实 URL
- 结果截断：title 200 字符、snippet 300 字符，最多 10 条
- `create_web_search_tool(config)` 返回 `@tool` 装饰的 langchain Tool

### 3.2 新建 `stock_agent/tools/realtime_quote.py` — 新浪实时行情工具

**设计要点**：
- 调用 `https://hq.sinajs.cn/list={market}{ticker}`（新浪实时报价 API）
- **必须设 Referer: `https://finance.sina.com.cn`**，否则 403
- **GBK 编码**：`resp.encoding = "gbk"`
- `@cached(ttl=60)` 缓存 60 秒
- 解析字段：名称/今开/昨收/最新价/最高/最低/成交量/成交额
- 自动计算涨跌额和涨跌幅
- 返回 `summary` 字段（LLM 可读摘要）

### 3.3 修改 [tool_registry.py](../../stock_agent/tools/tool_registry.py) — 注册 2 个新工具

在 `_get_factories` 的 `_TOOL_FACTORIES` 字典添加：
```python
"web_search": create_web_search_tool,
"realtime_quote": create_realtime_quote_tool,
```

### 3.4 修改 [agent_utils.py](../../stock_agent/agents/utils/agent_utils.py#L50-L72) — 所有分析师添加新工具

在 `tool_mapping` 中为**每个分析师**添加 `"web_search"` 和 `"realtime_quote"`：
```python
"fundamentals": [..., "web_search", "realtime_quote"],
"technical": [..., "web_search", "realtime_quote"],
"china_specific": [..., "web_search", "realtime_quote"],
"stock_development": [..., "web_search", "realtime_quote"],
```

### 3.5 更新 [prompts.py](../../stock_agent/agents/utils/prompts.py) — 提示词引导

在各分析师 system prompt 的工具说明段落补充：
- `💡 若行情/财务数据为空, 可用 realtime_quote 获取实时价格, 或用 web_search 搜索补充信息`

涉及 4 个 prompt：FUNDAMENTALS_SYSTEM、TECHNICAL_SYSTEM、CHINA_SPECIFIC_SYSTEM、STOCK_DEVELOPMENT_SYSTEM

---

## 验证步骤

1. **冒烟测试**（每步后）：`python -m pytest tests/test_smoke.py -v`
2. **Task 1 验证**：`python -c "from config.settings import load_config; ...; print(m.get_stock_data('600584', '2026-05-01', '2026-07-11'))"` — 确认 source 字段出现 `sina_http_kline`
3. **Task 2 验证**：`python -c "...; print(m.get_news('600584'))"` — 确认返回真实新闻（非静态 fallback）
4. **Task 3 验证**：`python -c "from stock_agent.tools.web_search import _duckduckgo_search; print(_duckduckgo_search('长电科技 600584'))"` — 确认 DuckDuckGo 返回结果
5. **realtime_quote 验证**：`python -c "from stock_agent.tools.realtime_quote import _fetch_sina_quote; print(_fetch_sina_quote('600584'))"` — 确认返回实时行情
6. **全流程验证**：`python main.py --date 2026-07-11` — 确认 RemoteDisconnected 大幅减少，分析师在数据缺失时调用 web_search/realtime_quote

## 风险与对策
- **DuckDuckGo 反爬**：`@cached` 减少请求；被限流时返回空列表，不影响 ReAct 流程
- **新浪 jsonp 解析**：正则提取 JSON 数组，多种包裹格式兼容
- **GBK 编码**：新浪 hq API 必须 `resp.encoding = "gbk"`
- **字段归一化**：`_normalize_kline_df` 仅在列不存在时重命名，不覆盖已有中文列
- **停牌处理**：`current=0` 表示停牌，下游判断
