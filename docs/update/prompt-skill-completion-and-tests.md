# 计划: 完成 Prompt SKILL 重构 + 修复 sector_inference 测试

> 本计划承接上一轮已批准计划 `sector-data-and-prompt-optimization.md` 的剩余步骤。
> **Step 1 (sector_inference.py 假数据→历史兜底) 已完成并验证**, 本计划只覆盖剩余 3 步。

## 摘要

用户三项要求: (1) 移除 sector_inference 死列表假数据 ✅已完成; (2) 给所有 agent prompt 加【角色/设定/目标/SKILL/输出/限制】六段结构 + SKILL 内容; (3) 立足短线、规避市盈率陷阱/季节性股票等误区。本计划完成 prompt 重构收尾(剩余 10 个)、修复因删除 `_get_static_sectors` 而损坏的测试, 并运行全量验证。

## 当前状态分析

### 已完成 (不在本计划范围)
- `stock_agent/agents/discovery/sector_inference.py`: `_get_static_sectors()` 已删除, 新增 `_get_historical_sectors(trade_date)` (line 166-209), 3 处调用点已更新 (line 65/160/163), 关键键映射 `turnover→换手率`/`up_count→上涨家数`/`down_count→下跌家数` 已对齐 `_score_sector` 读取逻辑 (line 283-285)。
- `prompts.py` 顶部 4 个共享 SKILL 常量已定义 (line 15-47): `SHORT_TERM_TREND_SKILL` / `SHORT_TERM_CATALYST_SKILL` / `SHORT_TERM_RISK_SKILL` / `SHORT_TERM_PITFALLS`。
- `prompts.py` 已重构 8 个 prompt (NEWS_ANALYST / SENTIMENT_ANALYST / POLICY_ANALYST / STOCK_SELECTOR / DARK_HORSE_SCANNER / FUNDAMENTALS / TECHNICAL / CHINA_SPECIFIC), 废弃常量 `SECTOR_INFERENCE_SYSTEM` 已删除。

### 待完成 (本计划范围)
1. **prompts.py 剩余 10 个 prompt** (line 369-666) 仍是旧格式, 无 SKILL 注入。
2. **test_sector_inference.py 损坏**:
   - line 76: `si._get_static_sectors()` 调用已删除函数 → `AttributeError`
   - `test_node_returns_correct_keys` (214-238): mock `_load_prev_scores→{}` → 历史 fallback 返回 [] → 断言 `len(ranked_sectors)==5` 与 `len(top_10)==8` 失败
   - `test_node_total_score_weighted` (240-268): 同上 → 断言 `len(ranked_sectors)>0` 失败
   - `test_node_sorted_descending` (270-280): 同上 → 退化为空列表断言 (虽不报错但无测试价值)
   - 缺少: 无历史数据时节点返回空的测试 (PRD 10.2 降级路径)

### 关键事实 (来自 Phase 1 探索)
- `_load_prev_scores(trade_date)` (sector_inference.py:402-422) 返回 `{板块名: 分数dict}`, 分数dict 含英文键 `change_pct`/`fund_flow`/`turnover`/`up_count`/`down_count`/`components`/`total_score` 等, 排除当天记录。
- `_get_historical_sectors` 消费该 dict 并映射为中文键, 供 `_score_sector` 读取。
- SKILL 注入模式 (已重构 prompt 的统一模式): 在定义处用字符串拼接 `+ SHORT_TERM_XXX_SKILL + "\n" + ...`, 零 agent 文件改动。

## 拟定变更

### 变更 1: 重构 prompts.py 剩余 10 个 prompt
**文件**: `stock_agent/agents/utils/prompts.py` (line 369-666)
**为什么**: 完成用户要求 #2/#3 — 所有 agent prompt 统一六段结构 + 按角色注入 SKILL, 区分短线趋势判断并规避误区。
**怎么做**: 对每个 prompt 保留原有业务内容, 重组为【角色】【设定】【目标】【SKILL】【输出】【限制】六段, 在【SKILL】段位置拼接对应常量。保留 JSON 模板原样。SKILL 引用矩阵 (来自上一轮已批准计划):

| Prompt | 行号 | SKILL 引用 |
|---|---|---|
| STOCK_DEVELOPMENT_SYSTEM | 369 | CATALYST |
| BULL_RESEARCHER_SYSTEM | 422 | TREND + CATALYST + PITFALLS |
| BEAR_RESEARCHER_SYSTEM | 444 | TREND + RISK + PITFALLS |
| RESEARCH_MANAGER_SYSTEM | 465 | TREND + RISK |
| TRADER_SYSTEM | 508 | TREND + RISK + PITFALLS |
| AGGRESSIVE_DEBATOR_SYSTEM | 558 | TREND + CATALYST |
| CONSERVATIVE_DEBATOR_SYSTEM | 574 | TREND + RISK + PITFALLS |
| NEUTRAL_DEBATOR_SYSTEM | 590 | TREND + RISK |
| RISK_MANAGER_SYSTEM | 606 | RISK + PITFALLS |
| PORTFOLIO_MANAGER_SYSTEM | 633 | RISK + PITFALLS |

**注入模式示例** (与已完成的 FUNDAMENTALS_SYSTEM 一致):
```python
XXX_SYSTEM = """【角色】...

【设定】
🔴 强制要求: ...
...

【目标】
1. ...
...

""" + SHORT_TERM_TREND_SKILL + "\n" + SHORT_TERM_CATALYST_SKILL + """

【输出】(严格 JSON)
{ ... }

【限制】
- ...
"""
```
**短线调优要点** (融入各 prompt 的【设定】/【限制】):
- 研究经理/交易代理/风控经理/组合经理: 持仓周期 1-5 交易日, 止损 5-8%, 止盈 3-10% (已在现有文本中, 保留)
- 看多/激进辩论者: 强调催化弹性与龙头接力, 但引用 PITFALLS 规避市盈率陷阱/利好出尽
- 看空/保守辩论者: 强调退潮信号/量价背离/季节性股票旺季 price in 风险
- 基本面/发展信息: 区分"便宜"与"有催化", 周期股低 PE 警示景气顶部

### 变更 2: 修复 test_sector_inference.py
**文件**: `tests/test_sector_inference.py`
**为什么**: 删除 `_get_static_sectors` 后 4 处测试损坏, 必须修复以恢复绿色测试套件。
**怎么做**:

**(2a) line 74-81 `test_all_metrics_in_range`**: 替换 `si._get_static_sectors()` 调用。用 `patch.object(si, "_load_prev_scores", return_value=_FAKE_HISTORICAL)` mock 历史数据, 调用 `si._get_historical_sectors("2026-07-11")` 取板块列表, 再跑 `_score_sector` 断言五维 0-100。这同时验证历史兜底路径的真实行为。

**(2b) 新增模块级 fixture `_FAKE_HISTORICAL`**: 8 个板块的合成历史分数 dict (英文键), 供 (2a) 与 (2c) 共用:
```python
_FAKE_HISTORICAL = {
    "半导体": {"change_pct": 3.0, "fund_flow": 20e8, "turnover": 4.0,
              "up_count": 90, "down_count": 10, "components": ["600584"], "total_score": 60},
    "医药": {"change_pct": -2.0, "fund_flow": -5e8, "turnover": 2.0,
            "up_count": 20, "down_count": 80, "components": [], "total_score": 20},
    # ... 共 8 个, 覆盖正/负流入与涨跌
}
```

**(2c) `test_node_returns_correct_keys` (214-238)**: 把 `patch.object(si, "_load_prev_scores", return_value={})` 改为 `return_value=_FAKE_HISTORICAL`。这样 `_get_historical_sectors` 返回 8 个板块, 保留原断言 `len(ranked_sectors)==5` (top_n=5) 与 `len(top_10)==8`。

**(2d) `test_node_total_score_weighted` (240-268)**: 同 (2c), mock 改为 `_FAKE_HISTORICAL`, 保留 `len(ranked_sectors)>0` 与加权总分断言。

**(2e) `test_node_sorted_descending` (270-280)**: 同 (2c), mock 改为 `_FAKE_HISTORICAL`, 保留降序断言 (现在列表非空, 断言真正生效)。

**(2f) 新增 `test_node_empty_when_no_history`**: 验证 PRD 10.2 降级路径 — `get_sector_data→{"data":[]}` + `_load_prev_scores→{}` → 节点返回 `{"ranked_sectors": [], "sector_crowding_map": {}}`, 且 `_save_scores` 不被调用 (无可保存数据)。放在 `TestSectorInferenceNode` 类内。

### 变更 3: 全量验证
**为什么**: 确认无回归 + 假数据彻底清除 + SKILL 全覆盖。
**怎么做**:
1. `python -m pytest tests/test_sector_inference.py -v` — sector_inference 测试全绿
2. `python -m pytest tests/ -v` — 全量回归 (含上轮 test_provider_health / test_llm_utils)
3. `grep -rn "_get_static_sectors"` 全仓库 → 0 命中 (确认死代码清除)
4. `grep -c "【SKILL" stock_agent/agents/utils/prompts.py` → 18 (每个 prompt 都有 SKILL 段; 4 个共享常量本身带【SKILL: 前缀, 但它们是定义不是引用, 统计 18 个 prompt 常量的引用)
5. `grep -n "SECTOR_INFERENCE_SYSTEM" stock_agent/agents/utils/prompts.py` → 仅注释行命中 (已废弃删除确认)
6. `python -c "from stock_agent.agents.utils import prompts"` — 导入无语法错误

## 假设与决策
- **假设**: 上一轮已重构的 8 个 prompt 与 4 个 SKILL 常量无需再改 (已验证存在于 prompts.py line 15-366)。
- **决策**: 测试修复采用 mock `_load_prev_scores→_FAKE_HISTORICAL` 而非改断言为空, 以保留对历史兜底路径的真实覆盖。
- **决策**: 不改 `_load_prev_scores`/`_save_scores`/`_score_sector` 等生产代码 — 它们已正确, 仅测试需对齐。
- **决策**: SKILL 注入沿用已建立的字符串拼接模式 (定义期拼接), 不引入运行期注入, 保持零 agent 文件改动。
- **范围限定**: 不动 agent 调用层 (discovery/researchers/risk_mgmt/trader 节点文件), 仅改 prompts.py 常量文本。

## 验证步骤
1. 运行 `python -m pytest tests/test_sector_inference.py -v` — 预期全绿 (含新增 `test_node_empty_when_no_history`)
2. 运行 `python -m pytest tests/ -v` — 预期全绿, 无回归
3. Grep `_get_static_sectors` → 0 命中
4. Grep `SECTOR_INFERENCE_SYSTEM` 在 prompts.py → 仅注释命中
5. 导入检查 `python -c "from stock_agent.agents.utils import prompts; print(len([k for k in dir(prompts) if k.endswith('_SYSTEM')]))"` → 18

## 实施顺序
1. 变更 1 (prompts.py 10 个 prompt) — 逐个 Edit, 从 STOCK_DEVELOPMENT_SYSTEM 开始到 PORTFOLIO_MANAGER_SYSTEM
2. 变更 2 (test_sector_inference.py) — 加 fixture + 改 4 处 + 加 1 新测试
3. 变更 3 (验证) — 按 6 项检查执行
