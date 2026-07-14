# 数据源与 LLM 超时优化 — 单元测试与回归验证 (Phase 3 收尾)

## 摘要

本计划是先前已批准并实施的「数据源与 LLM 超时优化」工作的收尾阶段。
经过代码验证, **所有实现工作 (Tasks #25-#33) 已全部完成并落盘**:

- ✅ `stock_agent/dataflows/provider_health.py` — Provider+method 级熔断器 (CLOSED/OPEN/HALF_OPEN 三态)
- ✅ `stock_agent/agents/utils/llm_utils.py` — `safe_llm_invoke` (超时+重试+兜底) + `FallbackResponse` + `_invoke_with_timeout`
- ✅ `stock_agent/default_config.py` — 5 个新配置项 (`provider_failure_threshold` 等)
- ✅ `stock_agent/dataflows/data_source_manager.py` — `_FailureAgg` + 熔断器集成 + 聚合日志
- ✅ 3 个 provider 文件 (akshare/baostock/tushare) — None vs {} 区分 + push2 兜底 + Tushare 补全
- ✅ `stock_agent/graph/run_metrics.py` — `record_timeout()` 方法
- ✅ `stock_agent/agents/utils/react_loop.py` — 改用共享 `_invoke_with_timeout`
- ✅ 14 个 LLM 调用文件 — 全部替换为 `safe_llm_invoke(...)`

**唯一未完成的工作 (Task #34):** 创建单元测试文件 + 运行回归测试。

本计划聚焦于这两项收尾工作, 不重新实现已完成的代码。

---

## 当前状态分析

### 已验证存在的测试文件 (3 个)
- `tests/test_smoke.py` — 冒烟测试 (7 个测试: 包导入/状态/图编译/路由/工具注册/合并去重/政策信源)
- `tests/test_parallel_architecture.py` — 四路并行架构测试
- `tests/test_sector_inference.py` — 板块推理 V2 测试 (30 个单测)

### 缺失的测试文件 (2 个, 本计划需创建)
- `tests/test_provider_health.py` — 熔断器单元测试
- `tests/test_llm_utils.py` — `safe_llm_invoke` 单元测试

### 测试约定 (来自 test_smoke.py)
- 纯 pytest 函数 (无类), 每个测试有中文 docstring
- 文件头部注入项目根目录到 `sys.path`
- 使用 Mock LLM 类 (`_MockLLM` / `_MockResponse`), 不连网
- 用 `# =====` 分节注释
- 测试隔离, 无真实网络依赖

---

## 待实现内容

### 文件 1: `tests/test_provider_health.py` (新建)

**目标:** 验证 `ProviderHealthTracker` 的三态熔断逻辑、单例、线程安全、统计、重置。

**测试用例 (12 个):**

| # | 测试函数 | 验证点 |
|---|---------|--------|
| 1 | `test_singleton` | `get_health_tracker()` 多次调用返回同一实例 |
| 2 | `test_initial_state_no_skip` | 新 key `should_skip` 返回 False (CLOSED) |
| 3 | `test_below_threshold_no_open` | 阈值=3 时, 2 次失败后仍 CLOSED (`should_skip=False`) |
| 4 | `test_threshold_triggers_open` | 第 3 次失败触发 OPEN, `should_skip=True` |
| 5 | `test_record_success_resets` | OPEN/CLOSED 状态下 `record_success` 重置 failure_count=0, state=CLOSED |
| 6 | `test_cooldown_expiry_to_half_open` | OPEN 状态冷却到期后 → HALF_OPEN (`should_skip=False`), 用 `monkeypatch` 替换 `time.time` |
| 7 | `test_half_open_success_closes` | HALF_OPEN 状态 `record_success` → CLOSED |
| 8 | `test_half_open_failure_reopens_doubled` | HALF_OPEN 状态 `record_failure` → OPEN, 冷却时间加倍 (`open_until` = now + cooldown*2) |
| 9 | `test_reset_clears_all` | `reset()` 后所有记录清空, `get_stats()` 返回 {} |
| 10 | `test_get_stats_format` | `get_stats()` 返回正确结构 (state/failure_count/total_failures/open_until) |
| 11 | `test_independent_keys` | 不同 provider+method 互不影响 (AkShare.get_stock_info 熔断不影响 BaoStock.get_stock_info) |
| 12 | `test_total_failures_accumulates` | `total_failures` 累计 (即使 record_success 重置 failure_count, total_failures 不归零) |

**实现要点:**
- 用 `get_health_tracker(failure_threshold=3, cooldown_seconds=120)` 获取实例 (注意单例, 每个测试开头调用 `tracker.reset()`)
- 时间相关测试用 `monkeypatch.setattr("stock_agent.dataflows.provider_health.time.time", lambda: fake_now)` 控制时钟
- 不连真实网络, 仅测试熔断器状态机

### 文件 2: `tests/test_llm_utils.py` (新建)

**目标:** 验证 `safe_llm_invoke` 的超时、重试、兜底逻辑, 以及 `_is_transient_error` 判定、`FallbackResponse` 结构。

**测试用例 (12 个):**

| # | 测试函数 | 验证点 |
|---|---------|--------|
| 1 | `test_is_transient_error_rate_limit` | 429/rate_limit → True |
| 2 | `test_is_transient_error_server_error` | 500/502/503/504 → True |
| 3 | `test_is_transient_error_timeout_connection` | TimeoutError/ConnectionError/RemoteDisconnected → True |
| 4 | `test_is_transient_error_client_error` | 400/401/403 → False (不重试) |
| 5 | `test_fallback_response_attributes` | `FallbackResponse` 有 `.content`/`.tool_calls`/`.response_metadata`/`.additional_kwargs` |
| 6 | `test_safe_invoke_normal` | 正常调用返回 LLM 响应 (MockLLM 返回 `_MockResponse("ok")`) |
| 7 | `test_safe_invoke_transient_retry_success` | 第 1 次抛 ConnectionError (瞬时), 第 2 次成功 → 返回成功响应, 调用 2 次 |
| 8 | `test_safe_invoke_non_transient_no_retry` | 抛 ValueError("400 bad request") (非瞬时) → 不重试, 直接兜底, 仅调用 1 次 |
| 9 | `test_safe_invoke_timeout_retry` | `llm.invoke` 抛阻塞超时 (用 `time.sleep` 模拟) → 触发 TimeoutError 重试 |
| 10 | `test_safe_invoke_all_retries_exhausted_fallback` | 持续抛 ConnectionError, 重试耗尽 → 返回 `FallbackResponse`, `.content` == fallback_content |
| 11 | `test_safe_invoke_no_fallback_raises` | `fallback_content=None` + 持续失败 → 抛出最后异常 |
| 12 | `test_safe_invoke_on_fallback_callback` | 兜底时 `on_fallback` 回调被调用一次 |

**实现要点:**
- 用 `_MockLLM` 类 (可配置 `invoke` 行为: 成功/抛异常/第 N 次成功)
- 用 `monkeypatch.setattr("stock_agent.agents.utils.llm_utils.time.sleep", lambda x: None)` 跳过重试等待, 加速测试
- 超时测试用 `timeout=1` + MockLLM 内 `time.sleep(2)` 模拟阻塞 (或直接让 invoke 抛 TimeoutError)
- 验证调用次数: 在 MockLLM 上加 `call_count` 属性

**Mock LLM 类设计 (放在 test_llm_utils.py 顶部):**
```python
class _MockResponse:
    def __init__(self, content="ok"):
        self.content = content

class _MockLLM:
    """可配置 invoke 行为的 Mock LLM"""
    def __init__(self, behavior="success", exc=None, fail_then_succeed=0):
        self.behavior = behavior      # "success" / "raise" / "sleep"
        self.exc = exc                # 抛出的异常
        self.fail_then_succeed = fail_then_succeed  # 前 N 次失败, 之后成功
        self.call_count = 0
    def invoke(self, prompt):
        self.call_count += 1
        if self.fail_then_succeed > 0 and self.call_count <= self.fail_then_succeed:
            raise self.exc
        if self.behavior == "raise":
            raise self.exc
        if self.behavior == "sleep":
            time.sleep(2)
        return _MockResponse("ok")
```

---

## 实现顺序

### Step 1: 创建 `tests/test_provider_health.py`
按上表 12 个测试用例实现, 遵循 test_smoke.py 的约定。

### Step 2: 创建 `tests/test_llm_utils.py`
按上表 12 个测试用例实现, 顶部定义 Mock LLM 类, monkeypatch 跳过 sleep。

### Step 3: 运行新单元测试
```bash
python -m pytest tests/test_provider_health.py tests/test_llm_utils.py -v
```
预期: 24 个测试全部 PASS。如有失败, 修复测试或实现 (优先修测试, 实现 bug 需同步修复并记录)。

### Step 4: 运行回归测试
```bash
python -m pytest tests/test_smoke.py tests/test_parallel_architecture.py tests/test_sector_inference.py -v
```
预期: 原有 39 个测试全部 PASS (确认新代码未破坏既有功能)。

### Step 5: 全量测试 (合并运行)
```bash
python -m pytest tests/ -v
```
预期: 63 个测试 (39 原有 + 24 新增) 全部 PASS。

### Step 6 (可选): 端到端验证
```bash
python main.py --date 2026-07-11 --debug
```
- 验证日志中 `[数据源] 所有 provider 的 get_stock_info 均无数据` 警告显著减少 (熔断器生效)
- 验证日志中 `[熔断器] xxx 连续失败 3 次 → OPEN` 出现
- 验证 LLM 超时后出现 `[xxx] 返回兜底响应` 而非永久阻塞
- 验证 `metrics.json` 中 `timeout_count` 字段有值
- 此步骤耗时 ~13 分钟, 需网络访问, **仅在用户要求时执行**

---

## 假设与决策

1. **不重新实现已完成代码**: 实现层 (Tasks #25-#33) 已验证完成, 本计划仅补充测试与验证。
2. **测试不连真实网络**: 所有测试用 Mock, 与 test_smoke.py 约定一致。
3. **单例测试需 reset**: `ProviderHealthTracker` 是全局单例, 每个测试开头调用 `tracker.reset()` 确保隔离。
4. **monkeypatch 时间**: 熔断器冷却测试用 `monkeypatch.setattr` 替换 `time.time`, 避免真实等待 120s。
5. **monkeypatch sleep**: LLM 重试测试用 `monkeypatch.setattr` 替换 `time.sleep`, 避免真实等待退避。
6. **E2E 验证为可选**: 耗时长且依赖网络, 默认不执行, 除非用户明确要求。
7. **实现 bug 处理**: 若测试发现实现 bug, 同步修复实现文件并在最终报告中说明。

---

## 验证步骤

完成后的验收标准:

1. ✅ `tests/test_provider_health.py` 存在, 含 12 个测试函数
2. ✅ `tests/test_llm_utils.py` 存在, 含 12 个测试函数 + Mock LLM 类
3. ✅ `python -m pytest tests/test_provider_health.py tests/test_llm_utils.py -v` 全绿
4. ✅ `python -m pytest tests/ -v` 全绿 (63 个测试)
5. ✅ 原有 39 个测试无回归
6. (可选) E2E 运行日志显示熔断器与兜底机制生效

最终向用户报告: 新增测试数、通过数、回归测试结果、(若执行) E2E 验证关键日志摘要。
