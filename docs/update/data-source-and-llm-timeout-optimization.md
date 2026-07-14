# 数据源接口优化 + LLM 超时优化

## Context

2026-07-11 运行 `main.py` 验证四路并行架构时，日志中反复出现 `[数据源] 所有 provider 的 get_stock_info 均无数据` (10+ 次)、`get_sector_stocks 均无数据` (5+ 次)、`get_fund_flow 均无数据` (3+ 次) 等告警。根因是 AkShare 底层调用东方财富网页抓取接口时频繁 `RemoteDisconnected`，而现有的 Sina/HTTP fallback 也走同一批服务器导致连锁失败。同时，13 处 `llm.invoke()` 调用（发现层 + 决策层 + 风控层）均无超时保护和重试机制，仅 `react_loop.py` 有 `_invoke_with_timeout`，任一 LLM 调用卡住即可阻塞整个节点。

本次优化目标：(1) 为数据源添加熔断器和 push2 JSON API 直连 fallback，补全 Tushare 缺失方法；(2) 抽出共享 `safe_llm_invoke` 工具，为所有 13 处裸 `llm.invoke()` 添加超时+重试+兜底。

---

## 阶段一：数据源优化

### 1.1 新建熔断器 `provider_health.py`

**新建** `stock_agent/dataflows/provider_health.py`

`ProviderHealthTracker` 单例（线程安全），按 `provider_class.method_name` 维度跟踪连续失败次数：
- `should_skip(provider, method)` → 连续失败 ≥3 次后返回 True（熔断中），冷却 120s 后进入 HALF_OPEN 允许一次试探
- `record_success()` → 重置计数
- `record_failure()` → 累计，达阈值熔断
- HALF_OPEN 探测失败 → 重新熔断，冷却加倍

### 1.2 修改 `_call_chain` 接入熔断器 + 聚合日志

**修改** `stock_agent/dataflows/data_source_manager.py`

- `_call_chain` 中每个 provider 调用前先检查 `should_skip()`，被熔断则跳过
- 返回空 dict 或抛异常时调用 `record_failure()`，成功时 `record_success()`
- **区分"不支持"与"失败"**：provider 方法返回 `None` 表示"不支持"（跳过，不计失败），返回 `{}` 表示"失败了"（计失败）
- 新增 `_log_chain_failure()` 方法：首次失败打 WARNING，后续每 5 次或 60s 聚合一条 INFO，避免日志刷屏

### 1.3 BaoStock 不支持的方法返回 `None`

**修改** `stock_agent/dataflows/providers/baostock_provider.py`

将 6 个"不支持"方法（`get_sector_data` / `get_sector_stocks` / `get_news` / `get_market_movers` / `get_dragon_tiger` / `get_fund_flow`）的返回值从 `{}` 改为 `None`，并移除 `@cached` 装饰器（不支持的方法无需缓存）。这样 `_call_chain` 能区分"provider 不支持"与"provider 调用失败"，避免熔断器误判。

### 1.4 AkShare 新增 push2 JSON API 直连 fallback

**修改** `stock_agent/dataflows/providers/akshare_provider.py`

push2.eastmoney.com 的 JSON API（与网页抓取 API 不同端口/路径，更稳定）：

- **`get_stock_info`** 新增 `_fallback_stock_info_push2(ticker)`：调用 `http://push2.eastmoney.com/api/qt/stock/get?secid={market}.{ticker}&fields=f57,f58,f84,f85,f116,f117,f162,f167,...`，返回与 `stock_individual_info_em` 相同结构的 `{item, value}` DataFrame。插入 `_try_chain` 第二顺位。
- **`get_sector_stocks`** 新增 `_fallback_sector_stocks_push2(sector)`：调用 `http://push2.eastmoney.com/api/qt/clist/get?fs=b:{bk_code}&fields=f12,f14,f2,f3,...`。维护类级 `_SECTOR_BK_MAP` 字典（板块名→BK代码，覆盖 A 股约 100 个东财行业板块）。插入 `_try_chain` 第二顺位。

### 1.5 补全 Tushare Provider 缺失方法

**修改** `stock_agent/dataflows/providers/tushare_provider.py`

新增 5 个方法（均加 `@cached`）：
| 方法 | Tushare API | 说明 |
|------|-------------|------|
| `get_stock_info` | `pro.stock_basic` | 按 ts_code 查询，返回 {证券代码, 股票简称, 行业, 地区, 上市日期} |
| `get_sector_stocks` | `pro.stock_basic` 按 industry 过滤 | sector 参数为中文行业名 |
| `get_market_movers` | `pro.daily` 按 pct_chg 排序 | 取最近交易日涨幅前 50 |
| `get_market_index` | `pro.index_daily` | 代码转换 sh000001 → 000001.SH |
| `get_news` | 返回 `None` | Tushare 无新闻接口，让 WebNews fallback |

### 1.6 新增配置项

**修改** `stock_agent/default_config.py`

```python
"provider_failure_threshold": int(os.getenv("PROVIDER_FAILURE_THRESHOLD", "3")),
"provider_cooldown_seconds": int(os.getenv("PROVIDER_COOLDOWN_SECONDS", "120")),
"llm_retry_max": int(os.getenv("LLM_RETRY_MAX", "2")),
"llm_retry_base_wait": float(os.getenv("LLM_RETRY_BASE_WAIT", "1.0")),
"llm_retry_max_wait": float(os.getenv("LLM_RETRY_MAX_WAIT", "3.0")),
```

---

## 阶段二：LLM 超时与重试优化

### 2.1 新建共享工具 `llm_utils.py`

**新建** `stock_agent/agents/utils/llm_utils.py`

核心函数 `safe_llm_invoke(llm, prompt_or_messages, timeout=90, max_retries=2, fallback_content=None, node_name="", on_fallback=None)`：
- **超时**：ThreadPoolExecutor(1) + `future.result(timeout)` 模式（与 `react_loop._invoke_with_timeout` 一致，兼容子线程）
- **重试**：仅对瞬时错误（429/500/502/503/TimeoutError/ConnectionError/RemoteDisconnected）重试，指数退避 1-3s，最多 2 次。非瞬时错误（400/401）立即失败。
- **兜底**：全部失败后返回 `FallbackResponse(fallback_content)`（模拟 LangChain AIMessage 的 `.content` 属性），调用 `on_fallback` 回调
- 同时导出 `_invoke_with_timeout` 供 `react_loop.py` 委托

### 2.2 `react_loop.py` 委托

**修改** `stock_agent/agents/utils/react_loop.py`

`_invoke_with_timeout` 内部委托给 `llm_utils._invoke_with_timeout`（保持签名不变，向后兼容）。**不**引入 `safe_llm_invoke` 的重试逻辑——ReAct 循环本身有迭代重试语义，且 `llm_with_tools` 重试可能引发 tool_calls 配对 400 错误。

### 2.3 `run_metrics.py` 新增 `record_timeout()`

**修改** `stock_agent/graph/run_metrics.py`

新增线程安全的 `record_timeout()` 方法（复用已有 `_lock`），供 `safe_llm_invoke` 的 `on_fallback` 回调使用。

### 2.4 批量替换 13 处裸 `llm.invoke()`

统一模式：`llm.invoke(prompt)` → `safe_llm_invoke(llm, prompt, timeout=config["llm_call_hard_timeout"], max_retries=config["llm_retry_max"], fallback_content=..., node_name=..., on_fallback=metrics.record_timeout)`

**发现层 5 处**（用 `log_stage` 计时，fallback_content 与 `safe_json_parse` 的 default 对齐）：

| 文件 | 行 | fallback_content |
|------|-----|------------------|
| `discovery/policy_analyst.py` | 88 | `'{"policy_events": [], "hot_topics": []}'` |
| `discovery/news_analyst.py` | 66 | `'{"hot_topics": []}'` |
| `discovery/sentiment_analyst.py` | 60 | `'{"hot_topics": []}'` |
| `discovery/stock_selector.py` | 96 | `'{"candidates": []}'` |
| `discovery/dark_horse_scanner.py` | 117 | `'{"candidates": []}'` |

**决策/风控层 8 处**（用 `metrics.llm_timer` 计时）：

| 文件 | 行 | fallback_content |
|------|-----|------------------|
| `researchers/research_manager.py` | 57 | `'{"direction": "中性", "rationale": "LLM调用失败"}'` |
| `researchers/bull_researcher.py` | 57 | `"(看多论证失败)"` |
| `researchers/bear_researcher.py` | 57 | `"(看空论证失败)"` |
| `trader/trader.py` | 80 | `'{"decisions": []}'` |
| `risk_mgmt/risk_manager.py` | 75 | `'{"decisions": [], "overall_risk_level": 0.8}'` |
| `risk_mgmt/portfolio_manager.py` | 88 | `'{}'`（让 `result.get("portfolio", approved_stocks)` 回退到 approved_stocks）|
| `risk_mgmt/aggressive_debator.py` | 54 | `"(激进辩论者发言失败)"` |
| `risk_mgmt/conservative_debator.py` | 54 | `"(保守辩论者发言失败)"` |
| `risk_mgmt/neutral_debator.py` | 54 | `"(中立辩论者发言失败)"` |

---

## 实施顺序

1. `default_config.py` — 新增 5 个配置项
2. `provider_health.py` — 新建熔断器
3. `data_source_manager.py` — `_call_chain` 接入熔断 + 日志聚合
4. `baostock_provider.py` — 不支持方法返回 None
5. `akshare_provider.py` — push2 fallback + 板块映射表
6. `tushare_provider.py` — 补全 5 方法
7. `llm_utils.py` — 新建共享工具
8. `run_metrics.py` — 新增 `record_timeout()`
9. `react_loop.py` — 委托给共享工具
10. 13 处批量替换（discovery 5 处 → decision/risk 8 处）
11. 新增 `tests/test_provider_health.py` + `tests/test_llm_utils.py`
12. 回归测试 + 端到端验证

---

## 验证方案

### 单元测试
- `tests/test_provider_health.py`：熔断阈值触发、成功重置、冷却到期 HALF_OPEN、探测失败重新熔断
- `tests/test_llm_utils.py`：正常调用、瞬时错误重试、非瞬时错误不重试、超时返回兜底、全部重试耗尽

### 回归测试
```
python -m pytest tests/test_smoke.py tests/test_parallel_architecture.py tests/test_sector_inference.py -v
```

### 端到端验证
```
python main.py --date 2026-07-11 --debug
```
对比优化前后 `results/reports/metrics_*.json`：
- `[数据源] ... 均无数据` WARNING 次数：10+ → 1-2（熔断后跳过）
- `timeout_count`：应降至 0
- discovery 层最慢节点耗时：46s → ~30s（重试快速失败）
- `prefetch` 耗时：66s → ~25s（熔断跳过故障 provider）
