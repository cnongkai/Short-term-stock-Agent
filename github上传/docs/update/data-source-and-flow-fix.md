# 数据源稳定性 + 缓存批量写入 + 全流程防挂起 修复计划

## Context（背景）

运行 `main.py` 时进程在第 5 只股票（600893）的分析层卡死：`fundamentals_600893` 维度的 LLM 调用挂起，ThreadPoolExecutor 的 `with` 块永久等待所有 future，导致整个 LangGraph 图阻塞，后续 3 只股票 + 决策层 + 报告生成全部未执行。

同时 AkShare 多个 API（板块/资金流/龙虎榜/股票信息/异动）大面积 `RemoteDisconnected`，BaoStock 作为主数据源又不支持板块/龙虎榜/资金流，导致工具层数据稀疏，LLM 反复重试加剧卡顿。

本次修复目标：
1. **Provider 链重排**：AkShare 回归主数据源（当前误被降为备用），Tushare 有 token 时插入中间
2. **AkShare 稳定性**：为每个易失败方法加 Sina/东方财富 fallback + socket 超时兜底
3. **批量写入**：磁盘缓存持久化（SQLite）+ 多标的批量预取
4. **全流程防挂起**：分析层 future 超时 + ReAct LLM 硬超时

## 实施顺序

按依赖关系分 5 步：Task 4（provider 顺序）→ Task 1（AkShare 稳定性）→ Task 2a（磁盘缓存）→ Task 2b（批量预取）→ Task 3（超时修复）。

---

## Task 4：Provider 链重排

**文件**：[data_source_manager.py](../../stock_agent/dataflows/data_source_manager.py#L33-L78)

当前顺序（磁盘实际状态）：BaoStock → AkShare → Tushare → WebNews。
改为目标顺序：**AkShare → Tushare(若 token) → BaoStock → WebNews**。

改动 `_init_providers`（第 33-78 行）：
1. **AkShare 首位**：移到第一个 append，日志改回 `"[数据源] AkShare 已启用 (主数据源)"`
2. **Tushare 自动启用**：`tushare_token = self.config.get("tushare_token", "")`；`tushare_enabled = self.config.get("tushare_enabled", False) or bool(tushare_token)`。有 token 即启用，插在 AkShare 之后。初始化失败（token 无效）时 try/except 跳过降级。
3. **BaoStock 末位兜底**：移到 Tushare 之后
4. WebNews / 巨潮公告位置不变

更新文件顶部 docstring 的 provider 链描述。

---

## Task 1：AkShare 稳定性修复

**文件**：[akshare_provider.py](../../stock_agent/dataflows/providers/akshare_provider.py)、[default_config.py](../../stock_agent/default_config.py)

### 1.1 HTTP 稳定性兜底（新增 `_setup_http_session`）
在 `AkShareProvider.__init__` 调用一次：
- `socket.setdefaulttimeout(akshare_socket_timeout)`（默认 30s）——根治连接挂起不返回（akshare 内部用 requests，无法直接注入 session，socket 超时是最底层兜底）
- 创建带 `HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=3, backoff_factor=0.5))` 的 `requests.Session` 供 fallback 函数用

### 1.2 通用 fallback 链辅助（新增 `_try_chain`）
```python
def _try_chain(self, funcs: list, normalize=None):
    """funcs: [(callable, label), ...], 依次 try, 返回首个非空 df 的 (df, label)"""
    for fn, label in funcs:
        try:
            df = fn()
            if df is not None and not df.empty:
                return (df, label) if normalize is None else (normalize(df), label)
        except Exception as e:
            logger.debug(f"[AkShare] fallback {label} 失败: {e}")
    return None, None
```

### 1.3 各方法改造（主函数 → fallback，字段归一化）

| 方法 | 主 API | Fallback API | 归一化要点 |
|------|--------|-------------|-----------|
| `get_sector_data` | `stock_board_industry_name_em()` | `stock_sector_spot(indicator="新浪行业")` | `板块`→`板块名称` |
| `get_sector_stocks` | `stock_board_industry_cons_em(symbol=sector)` | `stock_sector_spot` 全量按行业名过滤 | 统一 `代码`/`名称` 字段 |
| `get_market_movers` | `stock_zh_a_spot_em()` | `stock_zh_a_spot()`(Sina) | 统一 `涨跌幅`/`最新价` 字段 |
| `get_fund_flow` | `stock_individual_fund_flow(stock, market)` | `stock_individual_fund_flow_rank(indicator="今日")` 按 ticker 过滤 | 标注 `source: akshare_rank_fallback` |
| `get_stock_info` | `stock_individual_info_em(symbol, timeout)` | `stock_info_a_code_name()` 按 code 过滤 | `code`→`证券代码`, `name`→`股票简称` |
| `get_dragon_tiger` | `stock_lhb_detail_em(start=sd, end=sd)` | 日期范围回退：`start=today-7天, end=today` | 修复 NoneType（当日无数据时回退取近 5 日） |

- 给 `get_fund_flow`、`get_stock_info`、`get_dragon_tiger`、`get_sector_stocks` 补 `@retry` 装饰器（当前缺失）
- `get_dragon_tiger`：主调用捕获 `TypeError`(NoneType)，自动回退到 7 日范围

### 1.4 配置项（default_config.py 数据源区块新增）
```python
"akshare_http_timeout": int(os.getenv("AKSHARE_HTTP_TIMEOUT", "30")),
"akshare_socket_timeout": int(os.getenv("AKSHARE_SOCKET_TIMEOUT", "30")),
```

---

## Task 2a：磁盘缓存持久化

**文件**：[cache.py](../../stock_agent/dataflows/cache.py)、[default_config.py](../../stock_agent/default_config.py)

### 设计
引入模块级单例 `CacheStore`，承载内存层（热路径）+ SQLite 层（持久化）。`@cached` 装饰器委托给 `CacheStore`，签名不变（向后兼容）。

### CacheStore 实现
- **内存层**：`_mem: dict[str, tuple[float, Any]]`，`threading.RLock` 保护（四维并行并发访问）
- **SQLite 层**：`sqlite3.connect(db_path, check_same_thread=False)`，开启 WAL 模式。表 `cache(key TEXT PK, expire_at REAL, value BLOB)`，value 用 pickle 序列化
- **脏队列 + 后台 flush**：`_dirty: set[str]`，daemon 线程每 `flush_interval`(默认 60s) 批量 `INSERT OR REPLACE`。启动时 `_load_from_disk()` 恢复未过期项
- **读**：内存未命中→查 SQLite→回填内存
- **写**：写内存 + 加入 dirty（不立即写盘）
- **`cache_clear(prefix=None)`**：清内存 + 删 SQLite 对应行

### @cached 装饰器改造
```python
def cached(ttl: int = 3600):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            store = get_cache_store()
            cache_key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"
            hit = store.get(cache_key)
            if hit is not None:
                return hit
            result = func(*args, **kwargs)
            if result:
                store.set(cache_key, result, ttl)
            return result
        wrapper.cache_clear = lambda: get_cache_store().cache_clear(func.__name__)
        return wrapper
    return decorator
```
所有现有 `@cached` 调用零改动。

### 配置项（default_config.py）
```python
"cache_enable_disk": os.getenv("CACHE_ENABLE_DISK", "true").lower() == "true",
"cache_db_path": os.getenv("CACHE_DB_PATH", ""),  # 空则 results_dir/cache.sqlite
"cache_flush_interval": int(os.getenv("CACHE_FLUSH_INTERVAL", "60")),
```

---

## Task 2b：多标的批量预取

**文件**：[data_source_manager.py](../../stock_agent/dataflows/data_source_manager.py)、[analysis_layer.py](../../stock_agent/agents/analysis/analysis_layer.py#L47-L60)、[default_config.py](../../stock_agent/default_config.py)

### DataSourceManager.prefetch_ticker_data（新增方法）
```python
def prefetch_ticker_data(self, candidate_pool: list, trade_date: str) -> dict:
    """批量预取候选池所有 ticker 数据 (ThreadPoolExecutor 并发), 写入缓存。"""
```
- `ThreadPoolExecutor(max_workers=prefetch_workers)`（默认 4）
- 每个 ticker 一个任务，串行调 `get_stock_data`/`get_stock_info`/`get_financial_data`/`get_fund_flow`（`@cached` 自动缓存）
- 额外 1 个任务调 `get_dragon_tiger(trade_date)`
- 日期范围：`end=trade_date, start=trade_date-60天`
- `as_completed(timeout=prefetch_timeout)` 兜底，单 ticker 失败不影响其他
- 返回 `{"prefetched": int, "failed": [...], "elapsed": float}`

### analysis_layer_node 开头调用预取
在第 57 行（候选池空检查之后）插入：
```python
if config.get("prefetch_enabled", True) and candidate_pool:
    try:
        from stock_agent.dataflows.interface import get_manager
        with metrics.node_timer("prefetch"):
            pf = get_manager().prefetch_ticker_data(candidate_pool, trade_date)
            logger.info(f"[分析层] 预取完成: {pf['prefetched']}只, 失败{len(pf['failed'])}项, 耗时{pf['elapsed']:.1f}s")
    except Exception as e:
        logger.warning(f"[分析层] 预取失败(不影响后续): {e}")
```
预取失败不阻断（工具仍按需拉取）。

### 配置项
```python
"prefetch_enabled": os.getenv("PREFETCH_ENABLED", "true").lower() == "true",
"prefetch_workers": int(os.getenv("PREFETCH_WORKERS", "4")),
"prefetch_timeout": int(os.getenv("PREFETCH_TIMEOUT", "120")),
"prefetch_lookback_days": int(os.getenv("PREFETCH_LOOKBACK_DAYS", "60")),
```

---

## Task 3：全流程防挂起

**文件**：[analysis_layer.py](../../stock_agent/agents/analysis/analysis_layer.py#L105-L158)、[react_loop.py](../../stock_agent/agents/utils/react_loop.py)、[default_config.py](../../stock_agent/default_config.py)

### 3a. 分析层 future 超时（analysis_layer.py 第 105-158 行）
将 `with ThreadPoolExecutor` 改为手动管理 + 超时：

```python
dim_timeout = config.get("analysis_dimension_timeout", 120)
stock_timeout = config.get("analysis_stock_timeout", 300)

if parallel_dims and max_workers > 1:
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(dim_specs)))
    futures = {ex.submit(analyze_dimension(dim, fn, fb), ticker, name): dim
               for dim, fn, fb in dim_specs}
    try:
        for fut in as_completed(futures, timeout=stock_timeout):
            dim_name = futures[fut]
            try:
                _, result = fut.result(timeout=dim_timeout)
                ticker_report[dim_name] = result
            except TimeoutError:
                fut.cancel()
                ticker_report[dim_name] = {"rating": "中性", "confidence": 0.1, "report": f"单维超时({dim_timeout}s)"}
            except Exception as e:
                ticker_report[dim_name] = {"rating": "中性", "confidence": 0.1, "report": f"并行异常: {e}"}
    except TimeoutError:
        for fut, dim_name in futures.items():
            if not fut.done():
                fut.cancel()
                ticker_report.setdefault(dim_name, {"rating": "中性", "confidence": 0.1, "report": f"标的超时({stock_timeout}s)"})
    finally:
        ex.shutdown(wait=False)  # 关键: 不阻塞等待挂起线程
```
标的层并行块（第 140-151 行）同样改造。

### 3b. ReAct LLM 硬超时（react_loop.py）
新增 `_invoke_with_timeout(llm, messages, timeout)`：用 `ThreadPoolExecutor(1) + future.result(timeout)` 包裹 `llm.invoke()`（`signal.alarm` 仅主线程可用，ReAct 在工作线程运行）。

替换 3 处 LLM 调用：
- 第 103 行 `llm_with_tools.invoke(messages)`
- 第 200 行 `llm.invoke(messages)`（达工具上限后）
- 第 212 行 `llm.invoke(messages)`（迭代上限后）

在 `except Exception` 块（第 104 行）优先判断 `TimeoutError`，直接返回兜底 JSON `{"rating":"中性","confidence":0.1,"report":"LLM调用超时"}` + tool_log，不走 400 恢复逻辑。

### 3c. 配置项（default_config.py 分析层区块新增）
```python
"analysis_dimension_timeout": int(os.getenv("ANALYSIS_DIMENSION_TIMEOUT", "120")),
"analysis_stock_timeout": int(os.getenv("ANALYSIS_STOCK_TIMEOUT", "300")),
"llm_call_hard_timeout": int(os.getenv("LLM_CALL_HARD_TIMEOUT", "90")),
```
- `llm_call_hard_timeout` 默认 90s（小于 ChatOpenAI 的 120s，作为最硬兜底）

---

## 验证步骤

1. **冒烟测试**（每步后运行）：`python -m pytest tests/test_smoke.py -v`——确认包导入/图编译/路由/工具注册未破坏
2. **数据源测试**：`python test_data_sources.py`——确认 AkShare 各方法有 fallback 后能返回数据
3. **磁盘缓存验证**：跑一次图后检查 `results/cache.sqlite` 存在且有数据；重启后首次调用应日志显示 `[缓存命中]`
4. **批量预取验证**：日志出现 `[分析层] 预取完成: N只`，后续工具调用显示 `[缓存命中]`
5. **全流程验证**：`python main.py --date 2026-07-11`——确认 8 只股票全部完成分析层，进入决策层，最终输出推荐报告（即使某维度超时也能用兜底评级继续）
6. **超时验证**：设 `ANALYSIS_DIMENSION_TIMEOUT=5` 跑图，确认单维超时 5s 内填兜底评级，不阻塞后续

## 风险与对策
- **SQLite 并发锁**：WAL 模式 + RLock + 内存优先；若仍 locked 则改为单写线程队列
- **线程泄漏（硬超时）**：`shutdown(wait=False)` 后挂起线程仍存活，但 LLM I/O 最终会因 socket 超时退出；`llm_call_hard_timeout` 设小于 httpx 超时确保先触发
- **socket.setdefaulttimeout 全局副作用**：仅设一次，30s 兜底，对其他网络库影响可控
- **龙虎榜日期回退**：返回近 5 日数据，工具层日志标注 `date_range`，下游以"近期龙虎榜"语义解读
