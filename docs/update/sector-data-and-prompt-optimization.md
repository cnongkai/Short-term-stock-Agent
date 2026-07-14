# 板块数据真实化 + Agent Prompt 短线化重构

## Context

用户指出三个问题:
1. `sector_inference.py#L166-181` 的 `_get_static_sectors()` 是硬编码假数据(8 个板块带固定涨跌幅/资金流), 数据源失败时用假数据污染五维赋分
2. 18 个 agent prompt 结构不统一, 缺短线趋势判断 SKILL, 未系统化区分短线与长线
3. 需立足短线调优, 在不偏离价值投资基础上规避市盈率陷阱/季节性股票等误区

**预期结果**: 板块数据 fallback 改用历史真实值; 18 个 prompt 重组为【角色/设定/目标/SKILL/输出/限制】六段结构, 注入短线 SKILL 与误区警示; 死常量 `SECTOR_INFERENCE_SYSTEM` 清除。

---

## 改动 1: sector_inference.py — 历史均值替代假数据

**文件**: [stock_agent/agents/discovery/sector_inference.py](../../stock_agent/agents/discovery/sector_inference.py)

### 1.1 新增 `_get_historical_sectors(trade_date)` 替代 `_get_static_sectors()`

删除第 163-182 行的 `_get_static_sectors()`, 新增函数从 `results/sector_scores_history.json` 读取上次真实板块数据:

```python
def _get_historical_sectors(trade_date: str) -> list:
    """历史均值兜底板块数据 (替代假数据 _get_static_sectors)

    数据源失败时, 取上次成功运行的真实板块名 + 原始量化字段。
    无历史(首次运行)返回 [], 下游降级为黑马-only (PRD 10.2)。
    """
    prev = _load_prev_scores(trade_date)
    if not prev:
        logger.warning("[板块推理] 无实时数据且无历史记录, 返回空 (降级黑马-only)")
        return []
    sectors = []
    for name, sc in prev.items():
        if not isinstance(sc, dict) or not name:
            continue
        sectors.append({
            "板块名称": name,
            "涨跌幅": sc.get("change_pct", 0),
            "主力净流入": sc.get("fund_flow", 0),
            "换手率": sc.get("turnover", 0),       # 关键映射
            "上涨家数": sc.get("up_count", 0),     # 关键映射
            "下跌家数": sc.get("down_count", 0),   # 关键映射
            "领涨股": sc.get("components", []),
        })
    logger.info(f"[板块推理] 历史均值兜底: {len(sectors)} 个真实板块")
    return sectors
```

**关键映射原因** (已验证 sector_inference.py:254-258):
- `_score_sector` 第 256 行读 `换手率`/`turnover_rate` (不接受 `turnover`)
- 第 257-258 行只读中文键 `上涨家数`/`下跌家数` (无英文 fallback)
- 历史文件存的是英文键 `turnover`/`up_count`/`down_count`, 不映射会静默退化为 0

### 1.2 `_fetch_sector_data_raw` 签名扩展

第 139 行 `def _fetch_sector_data_raw() -> list:` 改为 `def _fetch_sector_data_raw(trade_date: str) -> list:`, 内部 3 处 `_get_static_sectors()` 调用(第 157/160 行)改为 `_get_historical_sectors(trade_date)`。

### 1.3 节点调用点

- 第 62 行: `sector_list = _fetch_sector_data_raw()` → `_fetch_sector_data_raw(trade_date)`
- 第 65 行: `sector_list = _get_static_sectors()` → `_get_historical_sectors(trade_date)`

### 1.4 fallback 链最终形态

```
_fetch_sector_data_raw(trade_date):
  1. get_sector_data() 成功 → 返回真实数据
  2. 指数格式 → data + _get_historical_sectors(trade_date)
  3. 异常/空 → _get_historical_sectors(trade_date)
     ├─ 有历史 → 真实板块名+历史值 (非空)
     └─ 无历史 → [] → ranked_sectors=[] → stock_selector 空 → 黑马-only (PRD 10.2)
```

---

## 改动 2: prompts.py — SKILL 常量 + 六段重组

**文件**: [stock_agent/agents/utils/prompts.py](../../stock_agent/agents/utils/prompts.py)

### 2.1 新增 4 个共享常量 (第 13 行 docstring 后)

```python
SHORT_TERM_TREND_SKILL = """【SKILL: 短线趋势判断】
- 龙头接力识别: 龙头滞涨/炸板时警惕退潮, 跟风补涨是末期信号
- 情绪周期: 启动→发酵→高潮→退潮, 高潮期(连板密集)后通常接退潮
- 量价背离: 价涨量缩或价跌量增为反转预警
- 分时盘口: 集合竞价异动/尾盘抢筹砸盘/大单净流入方向
- 连板梯队: 梯队断层(中位股断板)是退潮先兆"""

SHORT_TERM_CATALYST_SKILL = """【SKILL: 短线催化识别】
- 重大合同/并购: 合同金额占年营收>10%为强催化
- 业绩预增: 预增>50%且超预期为强催化, 警惕"利好出尽"
- 政策利好: 央行/国务院>部委>交易所, 首次提及弹性最大
- 技术突破: 首次突破关键位+放量有效性更高
- 催化弹性: 小市值+低流通+新题材 > 大市值+老题材"""

SHORT_TERM_RISK_SKILL = """【SKILL: 短线风控】
- 止损纪律: 入场价下方5-8%严格执行, 到位即离场
- 止盈分批: 催化兑现后分批止盈
- 退潮信号: 涨停家数骤降/连板高度下降/炸板率上升 → 减仓
- 仓位控制: 单票仓位上限, 黑马股更轻 (PRD 11.2)
- 持仓周期: 1-5交易日, 超时未达目标减仓"""

SHORT_TERM_PITFALLS = """【短线误区警示 (在价值投资基础上规避, 非放弃价值判断)】
1. 市盈率陷阱: 周期股(钢铁/煤炭/化工)低PE常是景气顶部, 高成长股高PE看PEG
2. 季节性股票: 春节前消费/双11前电商, 旺季price in时回避, 警惕利好出尽
3. 价值陷阱: 蓝筹低估值但阴跌, 区分"便宜"与"有催化"
4. 除权除息/指数调整: 高位除权填权不确定, 指数调整被动资金扰动
5. 题材炒作末期: 龙头滞涨+跟风补涨+量增价不涨 = 派发信号
6. 业绩窗口期: 财报披露前5日避免重仓, 预亏黑天鹅风险
7. 流动性陷阱: 庄股/低流通看似强势实则出货难, 封单虚高需警惕
8. 消息兑现: 长期利好短期已反映, "靴子落地"常伴随回调"""
```

### 2.2 SKILL 引用矩阵 (按需拼接, 非全量)

| Prompt | TREND | CATALYST | RISK | PITFALLS |
|--------|:-----:|:--------:|:----:|:--------:|
| NEWS_ANALYST | ✓ | ✓ | | |
| SENTIMENT_ANALYST | ✓ | | | |
| POLICY_ANALYST | | ✓ | | |
| STOCK_SELECTOR | ✓ | ✓ | | ✓ |
| DARK_HORSE_SCANNER | ✓ | ✓ | ✓ | |
| FUNDAMENTALS | | ✓ | | ✓ |
| TECHNICAL | ✓ | | ✓ | |
| CHINA_SPECIFIC | ✓ | | ✓ | |
| STOCK_DEVELOPMENT | | ✓ | | |
| BULL_RESEARCHER | ✓ | ✓ | | ✓ |
| BEAR_RESEARCHER | ✓ | | ✓ | ✓ |
| RESEARCH_MANAGER | ✓ | | ✓ | |
| TRADER | ✓ | | ✓ | ✓ |
| AGGRESSIVE_DEBATOR | ✓ | ✓ | | |
| CONSERVATIVE_DEBATOR | ✓ | | ✓ | ✓ |
| NEUTRAL_DEBATOR | ✓ | | ✓ | |
| RISK_MANAGER | | | ✓ | ✓ |
| PORTFOLIO_MANAGER | | | ✓ | ✓ |

### 2.3 六段结构重组规则 (保留现有有效内容)

每个 prompt 重组为: 【角色】【设定】【目标】【SKILL】【输出】【限制】

| 段落 | 现有内容来源 |
|------|-------------|
| 【角色】 | 原 prompt 首句"你是一位..." |
| 【设定】 | 🔴强制要求 + 🚫禁止 + ReAct工作流 + 自救工具 + 语言货币 |
| 【目标】 | "任务:"/"分析维度:"/"论证角度:" |
| 【SKILL】 | 新增 SHORT_TERM_* 常量拼接 (按矩阵) |
| 【输出】 | "输出格式(严格 JSON):" + JSON 模板 (**逐字保留**) |
| 【限制】 | "要求:" + V2短线策略约束 + 人民币/中文约束 |

**重组原则**:
- JSON 模板逐字保留 (字段名/嵌套不变, 下游 `safe_json_parse` 依赖)
- V2 短线策略约束 (止损5-8%/持仓1-5日) 保留
- ReAct 一次调用原则保留
- 货币/中文约束保留
- 多空辩论反方论点结构保留
- 红色警示 emoji 保留

**引用方式** (定义期拼接, 零 agent 文件改动):
```python
FUNDAMENTALS_SYSTEM = """【角色】...
【设定】...
【目标】...
""" + SHORT_TERM_CATALYST_SKILL + "\n" + SHORT_TERM_PITFALLS + """
【输出】...
【限制】..."""
```

### 2.4 删除死常量

删除第 147-178 行 `SECTOR_INFERENCE_SYSTEM` (V2 改纯量化, 无任何 import, 已 grep 确认)。

---

## 改动 3: 测试更新

**文件**: [tests/test_sector_inference.py](../../tests/test_sector_inference.py)

### 3.1 第 76 行 `test_all_metrics_in_range` (直接调用旧函数)

改为调用 `_get_historical_sectors(trade_date)` 或用 monkeypatch 临时历史文件验证返回结构。新增断言: 返回的板块含中文键 `板块名称`/`换手率`/`上涨家数`。

### 3.2 受影响用例 (依赖 `_get_static_sectors` 返回 8 条)

`test_node_returns_correct_keys` / `test_node_total_score_weighted` / `test_node_sorted_descending` 等: mock `_load_prev_scores` 返回含 2-3 个板块的历史 dict, 使 sector_list 非空。新增 `test_node_empty_when_no_history` 验证无历史时 `ranked_sectors==[]`。

### 3.3 test_parallel_architecture.py (无需改)

第 389/487/557 行 `patch.object(si_module(), "_fetch_sector_data_raw", return_value=None)` — patch 替换整个函数, 不关心签名变更, 节点层面 fallback 正常工作。

---

## 实施顺序

1. **sector_inference.py**: 新增 `_get_historical_sectors`, 删旧函数, 改签名+3处调用点
2. **prompts.py**: 新增 4 个 SKILL 常量 → 逐个重组 18 个 prompt (发现层→分析层→研究→交易→风控) → 删死常量
3. **test_sector_inference.py**: 修复 4 个失败用例 + 新增空历史降级用例
4. **验证**: `python -m pytest tests/ -v` 全绿 + 手动检查日志

---

## 验证步骤

| 验证项 | 方法 | 预期 |
|--------|------|------|
| 假数据已移除 | `grep -n "2.5\|15.2e8\|AI算力" sector_inference.py` | 0 匹配 |
| 新函数可导入 | `python -c "from stock_agent.agents.discovery.sector_inference import _get_historical_sectors"` | 无报错 |
| 历史兜底正确 | 构造临时 history json + monkeypatch `_SCORES_FILE` | 返回板块含中文键, turnover/up_count 非零 |
| 无历史返回空 | 空 history 调用新函数 | 返回 [] + warning |
| 死常量已删 | `grep -n "SECTOR_INFERENCE_SYSTEM" prompts.py` | 0 匹配 |
| SKILL 注入 | `python -c "from stock_agent.agents.utils.prompts import FUNDAMENTALS_SYSTEM; assert '市盈率陷阱' in FUNDAMENTALS_SYSTEM"` | True |
| JSON 模板未丢 | 逐个 prompt 比对重组前后 JSON 块字段 | 完全一致 |
| 全量测试 | `python -m pytest tests/ -v` | 全绿 (81+ 测试) |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 测试硬编码 8/5 计数破裂 | 同步更新 test_sector_inference.py, mock _load_prev_scores |
| turnover 键未映射导致动摇度低估 | 1.1 节映射 turnover→换手率, 新增单测验证 volatility>0 |
| 首次运行无历史 → 无板块推荐 | PRD 10.2 已设计降级, 日志明确提示 |
| SKILL 拼接致 prompt 过长 | 4 个常量各 <300 字, 按需引用不全量拼接 |
| 重组丢失 JSON 字段 | 每个 prompt 重组后人工 diff JSON 块, 冒烟测试覆盖 |
| 短线误区与价值投资冲突 | PITFALLS 标题明确"在价值投资基础上规避", FUNDAMENTALS 保留估值分析 |

---

## 关键文件

- `stock_agent/agents/discovery/sector_inference.py` — 需求 1: 替换假数据为历史均值
- `stock_agent/agents/utils/prompts.py` — 需求 2+3: SKILL 常量 + 六段重组 + 删死常量
- `tests/test_sector_inference.py` — 修复 4 个失败用例 + 新增降级用例
- `results/sector_scores_history.json` — 只读参考: 确认英文键结构驱动映射逻辑
- `stock_agent/agents/discovery/stock_selector.py` — 只读参考: 第 44-46 行验证空 ranked_sectors 降级链
