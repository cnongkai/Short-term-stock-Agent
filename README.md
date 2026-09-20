# 短线股票推荐 Agent (PRD V2.1)

基于 JTBD 模型与 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) 架构的 A 股短线股票自动化每日推荐系统。

采用 LangGraph 多智能体编排, 通过**发现层 → 分析层 → 决策层**三阶段流水线,
每日输出 2-11 只短线候选标的 (主线热门板块选股 + 支线黑马扫描)。

> **V2.1 新增**: 熔断器 (provider+method 级别) + safe_llm_invoke (90s 硬超时+重试+降级) +
> SKILL 六段提示词注入 (短线趋势/催化/风险/误区) + 板块BK代码动态映射 (替代死列表) +
> Sina fallback 硬超时 + HALF_OPEN 渐进退避 + 技术指标类型安全

## 核心架构

```
START
  │
  ├─→ 新闻分析师 ─────────────┐
  ├─→ 情绪分析师 ─────────────┼─→ fan-in 同步 ─→ 主线选股 ─┐
  ├─→ 政策分析师 ─────────────┤                └─→ 黑马扫描 ─┼─→ 候选池合并(去重+熔断)
  └─→ 板块推理(V2纯量化) ────┘                              │
         │                                                  │
         │  五维赋分: 热度/扩散力/动摇度/回补力/拥挤度       │
         │  输出: Top10 拥挤度地图 (5级颜色梯度)             │
         │                                                  │
                            ┌───────────────────────────────┘
                            ▼
                      分析层 (四维 ReAct)
            ┌──────┬──────┬──────┐
         基本面   技术   A股专属  个股发展   (每只股票 4 维分析, 各调 10 工具)
            └──────┴──────┴──────┘
                            │
                            ▼
                      决策层
         多空辩论 → 交易代理 → 三风格风控辩论 → 组合经理
                            │
                            ▼
                      报告生成 → END
```

> **V2 架构变更**: 板块推理从"三路分析师的下游"改为**第四路并行节点**,
> 采用纯量化五维赋分 (不依赖 LLM), 与新闻/情绪/政策分析师同时从 START 运行。
> 利用 LangGraph fan-in 语义: 双路选股节点等待全部 4 路完成后才启动。

### 三层架构说明

| 层 | 模块 | PRD 章节 | 说明 |
| --- | --- | --- | --- |
| **发现层** | 新闻/情绪/政策分析师 | 8.1 | 三路并行提取热点话题 (hot_topics 用 operator.add 累积) |
| | 板块推理 (V2) | 8.3 | **第四路并行**, 纯量化五维赋分, 输出 Top10 拥挤度地图 |
| | 主线选股 | 8.4 | 从热门板块选 5-8 只 (龙头+跟风) |
| | 黑马扫描 | 6.6/8.4 | 非热门板块扫描 0-3 只黑马 |
| | 候选池合并 | 8.4/10.2 | 去重合并 + 大盘熔断检查 |
| **分析层** | 基本面/技术/A股专属/个股发展 | 8.5 | 四维 ReAct 迭代分析, 各调 10 工具 (含 web_search + realtime_quote) |
| **决策层** | 多空辩论 | Phase 5 | Bull vs Bear 研究员辩论 |
| | 交易代理 | Phase 6 | 综合分析给出交易决策 |
| | 三风格风控辩论 | Phase 7 | 激进/保守/中立辩论 |
| | 组合经理 | Phase 8 | 聚合审批, 输出最终推荐清单 |

## 安装

```bash
# 1. 进入项目目录
cd short-term-stock-agent

# 2. 安装依赖
pip install -r requirements.txt
```

## 配置

```bash
# 1. 复制环境变量模板
cp .env.example .env

# 2. 编辑 .env, 填入 API Key
#    必填: DEEPSEEK_API_KEY (默认 LLM)
#    可选: TUSHARE_TOKEN (备用数据源)
```

支持的 LLM (OpenAI 兼容协议):
- **DeepSeek** (默认): `deepseek-chat`
- **智谱 GLM**: `glm-4-plus` / `glm-4-flash`
- **通义千问**: `qwen-plus`

切换 LLM 只需修改 `.env` 中的 `LLM_PROVIDER` 和模型名。

## 使用

```bash
# 推荐当日 (默认今天)
python main.py

# 指定日期
python main.py --date 2026-07-09

# 调试模式 (DEBUG 日志)
python main.py --date 2026-07-09 --debug

# 指定风险偏好和持仓周期
python main.py --date 2026-07-09 --risk aggressive --holding 1-3
```

运行后:
- 推荐清单打印到终端
- 完整报告保存至 `results/recommendation_YYYY-MM-DD.md`
- 全链路状态日志保存至 `results/logs/`

## 目录结构

```
short-term-stock-agent/
├── main.py                          # 入口脚本
├── .env.example                     # 环境变量模板
├── requirements.txt                 # 依赖
├── config/
│   └── settings.py                  # 配置加载器 (.env → DEFAULT_CONFIG)
├── stock_agent/
│   ├── default_config.py            # 默认配置
│   ├── llm_clients/                 # LLM 客户端 (OpenAI 兼容)
│   ├── agents/
│   │   ├── discovery/               # 发现层 (新闻/情绪/政策/板块/选股/黑马/合并)
│   │   ├── analysis/                # 分析层 (4 维 ReAct 分析师)
│   │   ├── researchers/             # 多空研究员 (Bull/Bear/Manager)
│   │   ├── trader/                  # 交易代理
│   │   ├── risk_mgmt/               # 风控 (激进/保守/中立/风险经理/组合经理)
│   │   ├── output/                  # 报告生成
│   │   └── utils/                   # 状态/提示词/ReAct/工具日志/llm_utils(safe_invoke)
│   ├── dataflows/                   # 数据源 (AkShare/BaoStock/Tushare/CnInfo/政策) + 熔断器
│   ├── tools/                       # 10 个分析师工具 (langchain @tool, 含 web_search + realtime_quote)
│   └── graph/                       # LangGraph 图编排 (setup/conditional/trading_graph/run_metrics)
└── tests/
    ├── test_smoke.py                # 冒烟测试 (9 项: 导入/状态/图编译/路由/工具/合并)
    ├── test_sector_inference.py     # 板块推理单测 (31 项: 五维赋分/颜色梯度/环比/降级/工具函数)
    ├── test_parallel_architecture.py # 四路并行集成测试 (18 项: 图结构/fan-in/状态隔离/独立性)
    ├── test_provider_health.py      # 熔断器单测 (13 项: CLOSED/OPEN/HALF_OPEN/渐进退避)
    ├── test_llm_utils.py            # safe_llm_invoke 单测 (12 项: 超时/重试/降级)
    └── test_technical_indicator_calc.py # 技术指标单测 (24 项: 字符串/数值类型/边界/信号)
```

## PRD V2/V2.1 增强

### V2 五大增强

1. **6 类权威政策信源** (PRD 6.3): 证监会/央行/发改委/交易所/巨潮/四大证券报, 独立限速 + 合规降级
2. **个股发展信息分析** (PRD 7.5): 新增第 4 维 ReAct 分析师, 分析重大合同/并购/业绩预告等催化事件
3. **双路选股** (PRD 8.3): 主线热门板块选股 (5-8 只) + 支线黑马扫描 (0-3 只) 并行执行
4. **ReAct 迭代循环** (PRD 6.7): 分析师采用 Reasoning-Acting-Observation 迭代, 配 10 个可调用工具
5. **板块五维量化赋分** (V2 新增): 板块推理改为第四路并行, 纯量化五维赋分 (热度/扩散力/动摇度/回补力/拥挤度), 输出 Top10 拥挤度地图, 支持日环比追踪

### V2.1 稳定性与短线调优

6. **数据源熔断器** (V2.1): provider+method 级别熔断 (3 次失败 → 120s 冷却 → HALF_OPEN 探测 → 渐进退避 2x/4x), 避免对不可用 API 反复重试
7. **LLM 安全调用** (V2.1): safe_llm_invoke 包装 14 处 llm.invoke(), 90s 硬超时 + 瞬时错误重试 (2 次/指数退避) + FallbackResponse 降级, 消除分析层卡死
8. **SKILL 六段提示词** (V2.1): 18 个 agent prompt 统一为【角色/设定/目标/SKILL/输出/限制】结构, 注入 4 个共享 SKILL 常量 (短线趋势/催化/风险/短线误区), 规避市盈率陷阱/季节性股票/价值陷阱等 8 类短线误区
9. **板块BK代码动态映射** (V2.1): 删除含重复 BK 代码+缺失板块的 `_SECTOR_BK_MAP` 死列表, 改为从 `stock_board_industry_name_em` 实时获取并缓存, push2 fallback 覆盖率从 ~30% 提升至 ~100%
10. **Sina fallback 硬超时** (V2.1): 用 ThreadPoolExecutor 为 akshare 内部无 timeout 的 requests 调用强制 30s 硬超时, 消除 SSL 握手挂死 (单次 fallback 从 60s+ 降至 30s)
11. **技术指标类型安全** (V2.1): OHLCV 列强制 `pd.to_numeric(errors='coerce')`, 修复数据源返回字符串导致 RSI/KDJ 计算异常

## V2 板块推理架构 (核心变更)

### 旧架构 vs 新架构

| 维度 | 旧架构 (V1) | 新架构 (V2) |
| --- | --- | --- |
| 执行位置 | 三路分析师的**下游** (串行) | **第四路并行** (与三路分析师同时) |
| 评分方式 | LLM 四维评分 (依赖大模型) | **纯量化五维赋分** (无 LLM 依赖) |
| 数据来源 | 三路分析师的 hot_topics | **资金面 + Bing注意力** (自行获取) |
| 输出 | ranked_sectors | ranked_sectors + **Top10 拥挤度地图** |
| 环比 | 无 | **日环比** (历史分数持久化 30 天) |

### 五维评分模型

| 指标 | 权重 | 计算方式 | 含义 |
| --- | --- | --- | --- |
| 热度 heat | 0.25 | 资金净流入×60% + 注意力×40% | 资金与关注度双高 |
| 扩散力 diffusion | 0.20 | 上涨广度×50% + 注意力广度×50% | 板块内上涨股票占比 |
| 动摇度 volatility | 0.15 | 涨跌幅×60% + 换手率×40% | 波动剧烈程度 |
| 回补力 rebound | 0.20 | 正涨幅×60% + 上涨占比×40% | 上行动能 |
| 拥挤度 crowding | 0.20 | 资金×50% + 注意力×50% | 交易拥挤程度 (双高=拥挤) |

### 拥挤度地图颜色梯度

| 分数 | 颜色 | 标签 |
| --- | --- | --- |
| ≥ 80 | 🔴 | 极度拥挤 |
| 60-79 | 🟠 | 拥挤 |
| 40-59 | 🟡 | 适中 |
| 20-39 | 🟢 | 宽松 |
| < 20 | 🔵 | 极度宽松 |

## 数据源

| 数据源 | 用途 | 是否免费 |
| --- | --- | --- |
| AkShare | 主数据源 (行情/财务/龙虎榜/资金流/板块/新闻) | 免费 |
| BaoStock | 备用 (行情/财务) | 免费 |
| Tushare | 备用 (需 token) | 需注册 |
| 巨潮资讯 | 公告检索 | 免费 |
| 政策信源 | 6 类权威政策事件 | 免费 |

## 测试

```bash
# 运行全部测试 (107 项)
python -m pytest tests/ -v

# 仅冒烟测试 (9 项: 不连真实网络)
python -m pytest tests/test_smoke.py -v

# 板块推理单测 (31 项: 五维赋分/颜色梯度/环比/降级/工具函数)
python -m pytest tests/test_sector_inference.py -v

# 四路并行集成测试 (18 项: 图结构/fan-in同步/状态隔离/独立性)
python -m pytest tests/test_parallel_architecture.py -v

# 熔断器单测 (13 项: CLOSED/OPEN/HALF_OPEN/渐进退避)
python -m pytest tests/test_provider_health.py -v

# safe_llm_invoke 单测 (12 项: 超时/重试/降级)
python -m pytest tests/test_llm_utils.py -v

# 技术指标单测 (24 项: 字符串/数值类型/边界/信号)
python -m pytest tests/test_technical_indicator_calc.py -v
```

测试覆盖:
- **冒烟测试** (9 项): 包导入 / 状态构造 / 图编译 / 条件路由 / 工具注册(10个) / 候选池去重
- **板块推理单测** (31 项): 五维赋分边界 / 颜色梯度映射(5级) / 环比计算 / 历史降级 / 向后兼容输出 / 节点集成 / 工具函数
- **四路并行集成测试** (18 项): 图结构验证(4路并行+fan-in) / 同步语义验证 / 状态字段隔离(无写冲突) / Sector Inference 独立性 / 端到端发现层数据流
- **熔断器单测** (13 项): 单例模式 / 阈值触发 / 冷却到期 / HALF_OPEN 探测 / 渐进退避(2x→4x→4x) / 成功重置 / 统计输出
- **safe_llm_invoke 单测** (12 项): 90s 硬超时 / 瞬时错误重试 / 指数退避 / FallbackResponse 降级 / on_fallback 回调
- **技术指标单测** (24 项): 字符串类型数据(核心修复) / 数值类型回归 / 边界(空/缺列/混合) / 信号解读(多空/超买超卖/金叉) / 辅助函数

## V3 评估与反馈闭环

### 自学习架构

系统通过 SQLite 性能数据库实现"推荐 → 追踪 → 归因 → 反馈注入 → 再推荐"的自学习闭环:

```
推荐产出 → 存入 recommendations 表
    ↓ (等待 T+N 持仓周期结束)
结果追踪 → 写入 outcomes 表 (收益率/最大回撤/超额收益)
    ↓
归因分析 → 六维归因 (分析师/板块/置信度/评级组合/市场环境/失败模式)
    ↓
参数调优 → 五维调优建议 (选股阈值/板块权重/ReAct迭代/置信度校准/分析师权重)
    ↓
反馈注入 → 历史表现摘要注入 Agent 提示词
    ↓
下一次推荐 (系统"记住"了历史经验)
```

### 评估模块

| 模块 | 文件 | 功能 |
| --- | --- | --- |
| PerformanceDB | `stock_agent/eval/performance_db.py` | SQLite 持久化推荐记录与追踪结果 |
| OutcomeTracker | `stock_agent/eval/outcome_tracker.py` | T+N 收益率计算与基准对比 |
| AttributionEngine | `stock_agent/eval/attribution_engine.py` | 六维归因分析与优化建议 |
| ParamOptimizer | `stock_agent/eval/param_optimizer.py` | 保守参数调优 (dry_run, 最大20%调整) |
| FeedbackInjector | `stock_agent/eval/feedback_injector.py` | 历史表现注入 Agent 提示词 |
| BacktestRunner | `stock_agent/eval/backtest_runner.py` | 批量回测 |

### 安全机制

- 样本门槛: 归因需 >=5 条, 调优需 >=10 条追踪记录
- 幅度限制: 单次参数调整不超过 20%
- 只读模式: 调优仅生成建议, 不自动修改配置

## 前端界面

系统提供全栈 Web 界面, 后端同时托管前端静态文件:

```bash
# 一键启动 (双击 run.bat 或命令行)
python api_server.py --port 5000
# 浏览器自动打开 http://localhost:5000
```

前端功能:
- 实时运行状态监控 (候选池/分析进度/辩论轮次)
- 推荐结果展示 (评级/目标价/止损/置信度/风险等级)
- 运行指标仪表盘 (耗时/Token消耗/节点计时/工具调用)
- 配置面板 (LLM/ReAct/选股/风控参数在线调整)
- 历史推荐与验证报告查阅
- 产品介绍页 (架构图/流程说明/设计理念)

## 致谢

本项目在以下方面参考了 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) 的设计与实现:

- **多空辩论决策架构**: Bull/Bear 研究员辩论 → 研究经理汇总 → 交易代理决策 → 三风格风控辩论 (激进/保守/中立) → 风险经理审批 → 组合经理聚合的决策链路设计
- **LangGraph 多智能体编排模式**: 基于 LangGraph StateGraph 的节点注册、条件路由、状态管理范式
- **ReAct 迭代分析框架**: Reasoning-Acting-Observation 循环迭代 + 工具调用日志的设计模式

在 TradingAgents-CN 架构基础上, 本项目针对 A 股短线交易场景进行了大量原创改进:
- 板块推理重构为第四路并行纯量化节点 (五维赋分 + 拥挤度地图 + 日环比)
- 数据源 provider+method 级熔断器 + 渐进退避
- safe_llm_invoke (90s 硬超时 + 重试 + 降级)
- SKILL 六段提示词注入 (短线趋势/催化/风险/误区)
- 双路选股 (主线热门 + 支线黑马) + 四维 ReAct 分析 (含个股发展维度)
- 6 类权威政策信源 + 板块BK代码动态映射

感谢 TradingAgents-CN 开源社区的贡献。
