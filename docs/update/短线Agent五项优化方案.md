# 短线 Agent 五项优化方案

## 概述

基于上一次运行验证报告（总耗时 1280s，8 只标的，stock_development 多次触达 10 次迭代上限，AkShare 频繁 RemoteDisconnected，验证报告误报"超时"），对系统进行 5 项优化：

1. **数据源优先级**：BaoStock 提至 AkShare 之前（BaoStock 稳定，AkShare 网络抖动多）
2. **分析层并行化**：4 维 ReAct 分析并行执行（ThreadPoolExecutor）
3. **短线策略**：持仓/风格/估值提示词改为短线策略（允许轻微溢价、紧止损、1-5 日持仓）
4. **stock_development ReAct 优化**：按分析师维度覆盖 ReAct 约束，减少迭代次数
5. **超时问题修复**：验证报告超时判定 Bug + AkShare 重试收敛 + LLM 超时调优

---

## 现状分析

### 1. 数据源链 (`data_source_manager.py`)
- `_init_providers()` L33-72 顺序：AkShare → BaoStock → Tushare → WebNews
- `_call_chain()` L77-92 按 append 顺序遍历，首个非空 `data` 返回
- AkShare `RemoteDisconnected` 频发，导致实时 PE/PB 计算失败（日志显示 15+ 条"未获取到总股本"）
- BaoStock 的 `get_sector_data`/`get_sector_stocks` 返回低质量伪数据（指数模拟 / 硬编码 4 个板块），若排在前会遮挡 AkShare 真实板块数据

### 2. 分析层 (`analysis_layer.py`)
- L60：`for idx, stock in enumerate(candidate_pool, 1)` — 标的串行
- L67-108：4 维（基本面/技术/A股专属/个股发展）串行执行，每维 `metrics.node_timer()` 包裹
- 8 只标的 × 4 维 = 32 个 ReAct 循环串行 → 分析层占整体耗时 ~70%
- `metrics.timeout_count += 1` (run_metrics.py L127) 非原子操作，并行需加锁

### 3. 策略提示词 (`prompts.py`)
- `FUNDAMENTALS_SYSTEM` L281：`"valuation_judgment": "当前估值偏低/合理/偏高"` — 价值投资语言
- `TRADER_SYSTEM` L527：`"position_size": "30%"` — 仓位偏大
- `RISK_MANAGER_SYSTEM` L603：`"adjusted_position": "25%"`
- `PORTFOLIO_MANAGER_SYSTEM` L635：`"position_size": "25%"`
- `RESEARCH_MANAGER_SYSTEM` L482：`"position_size": "30%"`，止盈止损无短线约束

### 4. stock_development ReAct (`react_loop.py` + `stock_development_analyst.py`)
- 全局约束：`react_max_iterations=5`, `react_max_tools_per_iter=3`, `react_max_total_tools=10`
- 验证报告显示 stock_development 多数标的触达 10 次工具调用上限
- `STOCK_DEVELOPMENT_SYSTEM` L385：仅说"每个工具调用一次即可"，未硬性约束 announcement_search 调用次数
- `run_react_loop()` 无 `analyst_name` 参数，无法按维度差异化配置

### 5. 超时问题 (`run_metrics.py` + `akshare_provider.py`)
- **Bug**：run_metrics.py L303 `total_elapsed < deep_timeout`（180s）判定总耗时超时 — 实际总耗时 1280s 必然"超时"
- AkShare `_RETRY_CONFIG` L23-27：`stop_after_attempt(3)` + `retry_if_exception_type(Exception)` 过宽，单个 RemoteDisconnected 阻塞 ~10s（1+2+4s 等待 + 3 次尝试）
- LLM 超时 180s 偏宽松，单次卡顿拖累整体

---

## 实施方案

### 任务 1：BaoStock 接口优先级调整

**文件**：`stock_agent/dataflows/data_source_manager.py`

**改动**：调整 `_init_providers()` L33-72 的 append 顺序，将 BaoStock 移到 AkShare 之前。

```python
def _init_providers(self):
    """按配置初始化 provider 链"""
    # BaoStock 主数据源 (稳定, 无网络抖动)
    if self.config.get("baostock_enabled", True):
        try:
            self._providers.append(BaoStockProvider(self.config))
            logger.info("[数据源] BaoStock 已启用 (主数据源)")
        except Exception as e:
            logger.warning(f"[数据源] BaoStock 初始化失败: {e}")

    # AkShare 备用 (板块/龙虎榜/资金流/新闻等 BaoStock 不支持的方法 fallback)
    if self.config.get("akshare_enabled", True):
        try:
            self._providers.append(AkShareProvider(self.config))
            logger.info("[数据源] AkShare 已启用 (备用, 板块/龙虎榜/资金流/新闻主供)")
        except Exception as e:
            logger.warning(f"[数据源] AkShare 初始化失败: {e}")

    # Tushare / WebNews 顺序不变 ...
```

**文件**：`stock_agent/dataflows/providers/baostock_provider.py`

**改动**：让 `get_sector_data`（L117-155）和 `get_sector_stocks`（L157-181）返回 `{}`，确保 fallback 到 AkShare 获取真实板块数据（BaoStock 的指数模拟/硬编码板块质量过低）。

```python
@cached(ttl=1800)
def get_sector_data(self) -> dict:
    """获取行业板块行情 (BaoStock 不支持真实板块数据, 返回空 fallback 到 AkShare)"""
    logger.debug("[BaoStock] get_sector_data 不支持真实板块数据, 跳过 (fallback AkShare)")
    return {}

@cached(ttl=1800)
def get_sector_stocks(self, sector: str) -> dict:
    """获取板块成分股 (BaoStock 不支持, 返回空 fallback 到 AkShare)"""
    logger.debug(f"[BaoStock] get_sector_stocks 不支持, 跳过 (fallback AkShare)")
    return {}
```

**原理**：BaoStock 在 `get_stock_data`/`get_financial_data`/`get_stock_info`/`get_market_index` 上稳定且无网络抖动，排第一；但其板块数据是伪数据，返回 `{}` 让 `_call_chain` 自动 fallback 到 AkShare。新闻/龙虎榜/资金流/异动本就返回 `{}`，fallback 链不变。

---

### 任务 2：分析层并行化

**文件**：`stock_agent/default_config.py`

**新增配置**（在 ReAct 配置块后）：
```python
# === 分析层并行配置 (V2 优化) ===
"analysis_parallel_dimensions": os.getenv("ANALYSIS_PARALLEL_DIMENSIONS", "true").lower() == "true",
"analysis_parallel_stocks": int(os.getenv("ANALYSIS_PARALLEL_STOCKS", "1")),
"analysis_max_workers": int(os.getenv("ANALYSIS_MAX_WORKERS", "4")),
"total_run_budget": int(os.getenv("TOTAL_RUN_BUDGET", "1800")),  # 总运行预算(秒), 用于验证报告超时判定
```

**文件**：`stock_agent/graph/run_metrics.py`

**改动 1**（线程安全）：`_init()` L53-63 增加 `self._lock = threading.Lock()`；`llm_timer` L127 的 `self.timeout_count += 1` 改为 `with self._lock: self.timeout_count += 1`。`node_timings`/`llm_calls` 的 dict 赋值和 list.append 在 CPython GIL 下原子，无需加锁，但 `timeout_count` 是 read-modify-write 必须加锁。

**改动 2**（顶部 import）：`import threading`

**文件**：`stock_agent/agents/analysis/analysis_layer.py`

**改动**：重构 `analysis_layer_node`，4 维分析改为 `ThreadPoolExecutor` 并行。标的层默认串行（`analysis_parallel_stocks=1`），可配置为 2。

```python
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def create_analysis_layer(llm, toolkit, config: dict):
    def analysis_layer_node(state) -> dict:
        logger.info(f"[分析层] 开始四维 ReAct 分析")
        metrics = get_metrics()

        candidate_pool = state.get("candidate_pool", [])
        trade_date = state.get("trade_date", "")
        existing_reports = state.get("analysis_reports", {})
        if not candidate_pool:
            logger.warning("[分析层] 候选池为空, 跳过分析")
            return {"analysis_reports": {}}

        analysis_reports = dict(existing_reports)
        react_counts = dict(state.get("react_tool_call_counts", {}))

        parallel_dims = config.get("analysis_parallel_dimensions", True)
        max_workers = config.get("analysis_max_workers", 4)
        parallel_stocks = config.get("analysis_parallel_stocks", 1)

        # 维度分析函数 (可并行)
        def analyze_dimension(dim_name, analyze_fn, fallback_rating):
            def _wrap(ticker, name):
                with metrics.node_timer(f"{dim_name}_{ticker}"):
                    try:
                        return dim_name, analyze_fn(llm, toolkit, ticker, name, trade_date, config)
                    except Exception as e:
                        logger.error(f"[分析层] {ticker} {dim_name} 异常: {e}")
                        return dim_name, {"rating": fallback_rating, "confidence": 0.1, "report": f"分析异常: {e}"}
            return _wrap

        dim_specs = [
            ("fundamentals", analyze_fundamentals, "中性"),
            ("technical", analyze_technical, "中性"),
            ("china_specific", analyze_china_specific, "中性"),
            ("stock_development", analyze_stock_development, "无催化"),
        ]

        def process_stock(stock):
            ticker = stock.get("ticker", "")
            name = stock.get("name", "")
            source = stock.get("source", "")
            logger.info(f"[分析层] 分析 {ticker} {name} [{source}]")
            ticker_report = {}

            if parallel_dims and max_workers > 1:
                # 4 维并行
                with ThreadPoolExecutor(max_workers=min(max_workers, len(dim_specs))) as ex:
                    futures = {
                        ex.submit(analyze_dimension(dim, fn, fb)(ticker, name)): dim
                        for dim, fn, fb in dim_specs
                    }
                    for fut in as_completed(futures):
                        dim_name, result = fut.result()
                        ticker_report[dim_name] = result
            else:
                # 串行回退
                for dim, fn, fb in dim_specs:
                    _, ticker_report[dim] = analyze_dimension(dim, fn, fb)(ticker, name)

            total_tools = sum(ticker_report.get(d, {}).get("react_iterations", 0) for d, _, _ in dim_specs)
            logger.info(f"[分析层] {ticker} 四维分析完成, 总工具调用: {total_tools} 次")
            return ticker, ticker_report, total_tools

        # 标的层并行/串行
        if parallel_stocks > 1:
            with ThreadPoolExecutor(max_workers=parallel_stocks) as ex:
                futures = {ex.submit(process_stock, stock): stock for stock in candidate_pool}
                for fut in as_completed(futures):
                    ticker, ticker_report, total_tools = fut.result()
                    analysis_reports[ticker] = ticker_report
                    react_counts[ticker] = total_tools
        else:
            for idx, stock in enumerate(candidate_pool, 1):
                ticker, ticker_report, total_tools = process_stock(stock)
                analysis_reports[ticker] = ticker_report
                react_counts[ticker] = total_tools

        logger.info(f"[分析层] 全部 {len(candidate_pool)} 只标的四维分析完成")
        return {"analysis_reports": analysis_reports, "react_tool_call_counts": react_counts}

    return analysis_layer_node
```

**线程安全分析**：
- `metrics.node_timings[node_name] = {...}`：单条 dict 赋值，GIL 原子 ✅
- `metrics.llm_calls.append(call_info)`：list.append，GIL 原子 ✅
- `metrics.timeout_count += 1`：read-modify-write，**需 Lock**（任务 5 已处理）✅
- `@cached` 装饰器：基于 dict，GIL 原子 ✅
- LangChain LLM 客户端（ChatDeepSeek）：底层 httpx 线程安全 ✅
- 默认 `parallel_stocks=1`（4 维并行，标的串行），规避 LLM 并发过高触发限流；可调为 2

---

### 任务 3：短线策略提示词

**文件**：`stock_agent/agents/utils/prompts.py`

**改动 1** — `FUNDAMENTALS_SYSTEM`（L249-292）：增加短线策略框架，允许轻微溢价。

在分析维度后新增短线策略说明：
```
📈 短线策略框架 (V2 优化):
- 本分析面向 1-5 个交易日的短线交易, 非中长线价值投资
- 估值判断允许轻微溢价: PE/PB 高于行业均值 10-20% 仍可接受 (短线看催化与动量, 非绝对估值)
- 优先关注: 业绩拐点、题材催化、资金面改善等短线驱动因素
- 估值偏高但有明确短线催化(如重大合同/政策利好/技术突破)时, 仍可给"看多"评级
```
并将 `"valuation_judgment": "当前估值偏低/合理/偏高"` 改为 `"valuation_judgment": "短线估值评估(允许轻微溢价): 偏低/合理/轻微溢价/明显高估"`。

**改动 2** — `RESEARCH_MANAGER_SYSTEM`（L460-491）：短线仓位/止盈止损。
```json
"position_size": "15%",
"take_profit": "...",
"stop_loss": "...",
```
并新增短线约束说明：
```
📈 短线策略约束:
- 持仓周期 1-5 个交易日, 仓位 10-20%
- 止损位设在入场价下方 5-8% (短线严格执行止损)
- 止盈位基于短线催化目标, 通常 3-10% 空间
- 三个场景目标价围绕短线波动区间设定
```

**改动 3** — `TRADER_SYSTEM`（L497-537）：`"position_size": "30%"` → `"position_size": "15%"`，新增短线持仓周期与止损约束（5-8% 止损，1-5 日持仓）。

**改动 4** — `RISK_MANAGER_SYSTEM`（L591-613）：`"adjusted_position": "25%"` → `"adjusted_position": "15%"`，新增短线风控约束（严格 5-8% 止损，仓位 10-20%）。

**改动 5** — `PORTFOLIO_MANAGER_SYSTEM`（L615-647）：`"position_size": "25%"` → `"position_size": "15%"`，新增短线组合约束（单一行业仓位不超过 30%，总仓位不超过 60%，1-5 日持仓）。

---

### 任务 4：stock_development ReAct 优化

**文件**：`stock_agent/default_config.py`

**新增配置**：
```python
# === 按分析师维度的 ReAct 覆盖配置 (V2 优化, 降低 stock_development 迭代) ===
"react_per_analyst": {
    "stock_development": {
        "max_iterations": 3,
        "max_tools_per_iter": 2,
        "max_total_tools": 5,
    },
    "fundamentals": {
        "max_iterations": 4,
        "max_tools_per_iter": 2,
        "max_total_tools": 7,
    },
},
```

**文件**：`stock_agent/agents/utils/react_loop.py`

**改动 1**（L35-59）：`run_react_loop` 增加 `analyst_name: str = None` 参数，读取 per-analyst 覆盖配置：
```python
def run_react_loop(
    llm,
    tools: list,
    system_prompt: str,
    user_query: str,
    config: dict = None,
    analyst_name: str = None,
) -> Tuple[str, List[dict]]:
    config = config or {}
    # 按分析师维度覆盖 ReAct 约束 (V2 优化: stock_development 等高迭代维度降配)
    per_analyst = config.get("react_per_analyst", {})
    overrides = per_analyst.get(analyst_name, {}) if analyst_name else {}
    max_iterations = overrides.get("max_iterations", config.get("react_max_iterations", 5))
    max_tools_per_iter = overrides.get("max_tools_per_iter", config.get("react_max_tools_per_iter", 3))
    max_total_tools = overrides.get("max_total_tools", config.get("react_max_total_tools", 10))
    if analyst_name:
        logger.info(f"[ReAct] 分析师={analyst_name}, 约束: iter={max_iterations}/{max_tools_per_iter}, total={max_total_tools}")
```

**文件**：`stock_agent/agents/analysis/stock_development_analyst.py`

**改动 1**（L61-64）：调用 `run_react_loop` 时传入 `analyst_name="stock_development"`：
```python
analysis_text, tool_log = run_react_loop(
    llm=llm, tools=tools, system_prompt=STOCK_DEVELOPMENT_SYSTEM,
    user_query=user_query, config=config, analyst_name="stock_development",
)
```

**改动 2**（L33-58 user_query）：强化 announcement_search 调用约束：
```
2. 行动: 调用工具获取数据:
   - announcement_search 检索公告 (⚠️ 仅调用 1 次, keyword 留空检索全部近 30 日公告)
   - news_search 搜索相关新闻 (仅 1 次)
   - research_report_search 检索研报 (可选)
   🔧 严格约束: 每个工具最多调用 1 次, 不要重复调用!
```

**文件**：`stock_agent/agents/analysis/fundamentals_analyst.py` / `technical_analyst.py` / `china_specific_analyst.py`

**改动**：各自调用 `run_react_loop` 时传入对应 `analyst_name`（`"fundamentals"`/`"technical"`/`"china_specific"`），使 per-analyst 覆盖生效。

**文件**：`stock_agent/agents/utils/prompts.py` — `STOCK_DEVELOPMENT_SYSTEM`（L371-411）

**改动**：强化工具调用约束，明确 announcement_search 单次调用：
```
ReAct 循环工作流程:
- 行动: 调用工具 (announcement_search / news_search / research_report_search)
  🔧 严格约束: 每个工具最多调用 1 次!
  - announcement_search: keyword 留空检索全部近 30 日公告 (一次获取所有类型, 不要按类型多次调用)
  - news_search: 搜索个股相关新闻 (1 次)
  - research_report_search: 可选, 仅在前两者无明确催化时调用
- 观察: 分析工具返回结果
- 收到数据后立即生成分析报告, 不要再调用工具
```

---

### 任务 5：超时问题修复

**文件**：`stock_agent/graph/run_metrics.py`

**改动 1**（L303 超时判定 Bug）：用 `total_run_budget` 替代 `deep_timeout` 判定总耗时。
```python
total_run_budget = config.get("total_run_budget", 1800)
# ...
lines.append(f"| **总运行预算** | <{total_run_budget}秒 | {total_elapsed:.2f}秒 | {'✅ **正常**' if total_elapsed < total_run_budget else '⚠️ **超时**'} |")
```
同时 L308-317 配置验证区新增 `- 总运行预算: {total_run_budget}秒`。

**文件**：`stock_agent/default_config.py`

**改动**：LLM 超时从 180s 降至 120s（单次 LLM 卡顿更快失败，配合重试收敛）：
```python
"quick_model_config": {"max_tokens": 4000, "temperature": 0.7, "timeout": 120},
"deep_model_config": {"max_tokens": 4000, "temperature": 0.7, "timeout": 120},
```

**文件**：`stock_agent/dataflows/providers/akshare_provider.py`

**改动**（L23-27）：收敛重试配置，缩窄异常类型，减少尝试次数。
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 重试配置: 最多 2 次, 间隔 1-2 秒, 仅对网络异常重试 (V2 优化: 快速失败避免阻塞)
import httpx
_RETRY_CONFIG = {
    "stop": stop_after_attempt(2),
    "wait": wait_exponential(multiplier=1, min=1, max=2),
    "retry": retry_if_exception_type((ConnectionError, TimeoutError, httpx.RemoteProtocolError)),
}
```
**原理**：原配置对 `Exception` 重试 3 次，单个 RemoteDisconnected 阻塞 ~10s；新配置仅对网络异常重试 2 次，阻塞降至 ~3s。非网络异常（如数据格式错误）立即失败。注意：`@retry` 装饰的方法在非重试异常时会直接抛出，但各方法内部已有 `try/except` 返回 `{}`，所以不影响 fallback 链。

---

## 假设与决策

1. **BaoStock 板块方法返回 `{}`**：BaoStock 的 `get_sector_data`/`get_sector_stocks` 是伪数据（指数模拟/硬编码），排第一会遮挡 AkShare 真实数据。返回 `{}` 让 `_call_chain` 自动 fallback。其他 BaoStock 不支持的方法（新闻/龙虎榜/资金流/异动）本就返回 `{}`，链路不变。

2. **并行化默认 4 维并行、标的串行**：4 维并行（max_workers=4）已能将分析层耗时降至 ~1/4。标的层默认串行（`parallel_stocks=1`）规避 DeepSeek 并发限流风险；用户可通过 env `ANALYSIS_PARALLEL_STOCKS=2` 开启 2 标的并发。

3. **短线溢价幅度 10-20%**：用户说"允许轻微溢价"，解读为 PE/PB 高于行业均值 10-20% 仍可接受。短线看催化与动量，非绝对估值。

4. **stock_development 降配到 iter=3/tools_per_iter=2/total=5**：验证报告显示该维度多数触达 10 次上限且多为重复 announcement_search 调用。降配 + prompt 约束单次调用，预期工具调用从 10 降至 3-5。

5. **LLM 超时 180→120s**：单次 LLM 调用 120s 足够（DeepSeek 响应通常 5-30s），更快失败避免长尾卡顿。配合 AkShare 重试收敛，整体预算 1800s（30 分钟）。

6. **线程安全**：`timeout_count` 加 Lock，其余 dict/list 操作 GIL 原子。LLM 客户端与缓存线程安全。

---

## 验证步骤

### 1. 语法与导入验证
```bash
cd "short-term-stock-agent"
python -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in [
  'stock_agent/dataflows/data_source_manager.py',
  'stock_agent/dataflows/providers/baostock_provider.py',
  'stock_agent/dataflows/providers/akshare_provider.py',
  'stock_agent/default_config.py',
  'stock_agent/graph/run_metrics.py',
  'stock_agent/agents/analysis/analysis_layer.py',
  'stock_agent/agents/utils/react_loop.py',
  'stock_agent/agents/utils/prompts.py',
  'stock_agent/agents/analysis/stock_development_analyst.py',
  'stock_agent/agents/analysis/fundamentals_analyst.py',
  'stock_agent/agents/analysis/technical_analyst.py',
  'stock_agent/agents/analysis/china_specific_analyst.py',
]] and print('语法检查通过')"
```

### 2. 导入链验证
```bash
python -c "from stock_agent.dataflows.data_source_manager import DataSourceManager; from stock_agent.agents.analysis.analysis_layer import create_analysis_layer; from stock_agent.agents.utils.react_loop import run_react_loop; from stock_agent.graph.run_metrics import get_metrics; print('导入成功')"
```

### 3. 数据源优先级验证
```bash
python -c "
from stock_agent.dataflows.data_source_manager import DataSourceManager
m = DataSourceManager({})
print('Provider 链顺序:', [p.__class__.__name__ for p in m._providers])
assert m._providers[0].__class__.__name__ == 'BaoStockProvider', 'BaoStock 应排第一'
print('✅ BaoStock 优先级验证通过')
"
```

### 4. Per-analyst ReAct 配置验证
```bash
python -c "
from stock_agent.default_config import DEFAULT_CONFIG
pa = DEFAULT_CONFIG['react_per_analyst']
assert pa['stock_development']['max_total_tools'] == 5
print('✅ stock_development 降配:', pa['stock_development'])
"
```

### 5. 超时判定 Bug 验证
检查 run_metrics.py L303 区域已用 `total_run_budget` 而非 `deep_timeout`。

### 6. 端到端运行验证
```bash
python main.py
```
查看新生成的验证报告：
- 总耗时是否显著下降（目标 < 600s，原 1280s）
- stock_development 各标的 ReAct 迭代是否降至 3-5（原 10）
- "实际耗时"行不再误报"超时"
- AkShare RemoteDisconnected 阻塞是否降低
- 推荐标的仓位是否为 10-20%（短线策略）

### 7. 日志检查
```bash
# 检查 BaoStock 排第一
grep "BaoStock 已启用 (主数据源)" results/logs/agent_*.log
# 检查 per-analyst ReAct 约束生效
grep "分析师=stock_development" results/logs/agent_*.log
# 检查并行执行
grep "ThreadPoolExecutor\|分析层.*分析" results/logs/agent_*.log
```

---

## 实施顺序

1. **Phase 1**（数据源 + 超时修复基础）：任务 1 + 任务 5（run_metrics Bug + akshare 重试 + LLM 超时 + total_run_budget 配置）
2. **Phase 2**（策略 + ReAct 优化）：任务 3（prompts 短线策略）+ 任务 4（per-analyst ReAct + stock_development prompt + analyst_name 传参）
3. **Phase 3**（并行化）：任务 2（analysis_layer 重构 + run_metrics Lock + default_config 并行配置）
4. **Phase 4**（验证）：语法/导入/配置/端到端运行
