# 短线股票推荐 Agent

基于 LangGraph 多智能体编排的 A 股短线自动化每日推荐系统。

系统通过 **发现层 → 分析层 → 辩论层** 三阶段流水线，每日产出 2-11 只短线候选标的（主线热门板块选股 + 支线黑马扫描），并配备全栈 Web 界面实现一键启动、实时监控、结果展示和配置管理。

## 快速开始

### 环境要求

- Python 3.10+
- DeepSeek API Key（或其他 OpenAI 兼容 LLM）

### 安装

```bash
cd short-term-stock-agent
pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

支持的 LLM（均走 OpenAI 兼容协议）：

| LLM | Provider | 模型名 | 说明 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek` | `deepseek-chat` | 默认，性价比高 |
| 智谱 GLM | `glm` | `glm-4-plus` / `glm-4-flash` | 国产备选 |
| 通义千问 | `qwen` | `qwen-plus` | 阿里云生态 |

切换 LLM 只需修改 `.env` 中的 `LLM_PROVIDER` 和模型名。

### 启动

**方式一：Web 界面（推荐）**

```bash
# 双击 run.bat 或命令行启动
python api_server.py --port 5000
# 浏览器自动打开 http://localhost:5000
```

Web 界面提供：
- 一键启动推荐流程，实时查看运行进度（候选池构建、分析进程、辩论轮次）
- 推荐结果展示（评级、目标价、止损位、置信度、风险等级）
- 运行指标仪表盘（总耗时、Token 消耗、各节点计时、工具调用统计）
- 在线配置面板（LLM / ReAct / 选股 / 风控参数，修改后直接生效）
- 历史推荐与验证报告查阅
- 产品介绍页（架构图、流程说明、设计理念）

**方式二：命令行**

```bash
python main.py                              # 推荐当日
python main.py --date 2026-09-07            # 指定日期
python main.py --debug                      # 调试模式
python main.py --risk aggressive --holding 1-3  # 指定风险偏好和持仓周期
```

运行后推荐清单打印到终端，完整报告保存至 `results/recommendation_YYYY-MM-DD.md`。

## 核心架构

```
START
  │
  ├─→ 新闻分析师 ─────────────┐
  ├─→ 情绪分析师 ─────────────┼─→ fan-in 同步 ─→ 主线选股 ─┐
  ├─→ 政策分析师 ─────────────┤                └─→ 黑马扫描 ─┼─→ 候选池合并(去重+熔断)
  └─→ 板块推理(纯量化) ───────┘                              │
         │  五维赋分: 热度/扩散力/动摇度/回补力/拥挤度       │
         │  输出: Top10 拥挤度地图 (5级颜色梯度)             │
                            ┌───────────────────────────────┘
                            ▼
                      分析层 (四维 ReAct)
            ┌──────┬──────┬──────┐
         基本面   技术   A股专属  个股发展   (每只股票 4 维分析, 各调 10 工具)
            └──────┴──────┴──────┘
                            │
                            ▼
                      辩论层
         多空辩论 → 交易代理 → 三风格风控辩论 → 组合经理
                            │
                            ▼
                      报告生成 → END
```

### 发现层（7 个 Agent）

四路并行信号采集 → 双路选股 → 合并熔断。

| Agent | 职责 | 特点 |
| --- | --- | --- |
| 新闻分析师 | 从新闻标题提取热点话题 | LLM 提取，输出 hot_topics |
| 情绪分析师 | 采集散户极端情绪信号 | 股吧数据，反向指标 |
| 政策分析师 | 捕捉自上而下的产业方向 | 6 类权威信源，1.2 倍权重加成 |
| 板块推理器 | 纯量化五维赋分排名板块 | 不依赖 LLM，零幻觉，零 token 消耗 |
| 主线选股器 | 从 Top N 热门板块选龙头+跟风股 | 共识性机会 |
| 黑马扫描器 | 从非热门板块扫描暴涨潜力股 | 非共识性机会，四维信号模型，≥60 分入选 |
| 候选池合并器 | 去重合并 + 大盘熔断检查 | 上证跌幅 >4% 触发熔断跳过后续全部环节 |

### 分析层（4 个 ReAct 分析师）

四个维度正交设计，减少信息冗余，每个维度提供增量信息。

| 分析师 | 回答的问题 | 工具数 | ReAct 迭代 |
| --- | --- | --- | --- |
| 基本面分析师 | 公司好不好（估值/财务/行业地位） | 10 | 4 轮 |
| 技术面分析师 | 资金认不认（K线/指标/量价） | 10 | 5 轮 |
| 中国特性分析师 | A 股特色逻辑（龙虎榜/资金流/游资） | 10 | 5 轮 |
| 个股发展分析师 | 有没有催化剂（公告/研报/事件驱动） | 10 | 3 轮 |

共享 `run_react_loop()` 实现，含工具冷却（连续失败 2 次自动跳过）和超时兜底（120s 超时填中性评级继续推进）。

### 辩论层（9 个 Agent）

双层对抗式辩论，强制消除确认偏误。

**第一层：多空辩论（解决"买不买"）**

| Agent | 职责 | LLM |
| --- | --- | --- |
| 看多研究员 | 找上涨理由 | quick_thinking |
| 看空研究员 | 找下跌风险 | quick_thinking |
| 研究经理 | 综合裁决，输出投资计划 | deep_thinking |

交替发言 2 轮（实际 4 次），互相看到对方论点后反驳。

**第二层：风险辩论（解决"怎么买"）**

| Agent | 职责 | LLM |
| --- | --- | --- |
| 激进辩论者 | 追求收益的视角 | quick_thinking |
| 保守辩论者 | 保护本金的视角 | quick_thinking |
| 中立辩论者 | 居中调停 | quick_thinking |
| 风险裁决官 | 综合审批 | deep_thinking |
| 组合经理 | 聚合输出最终推荐清单 | deep_thinking |

轮转顺序 Risky → Safe → Neutral，确保激进方先发言提出机会。辩论不收敛时风险裁决官默认"拒绝"，无标的通过则输出空推荐清单。

**LLM 成本差异化分配**：辩论者用 quick_thinking（快速产出论点），裁决者用 deep_thinking（综合判断质量最高），一个流程约调用 LLM 15-20 次，成本控制在合理范围。

## V3 评估与反馈闭环

系统通过 SQLite 性能数据库实现自学习闭环：

```
推荐产出 → recommendations 表
    ↓ (等待 T+N 持仓周期结束)
结果追踪 → outcomes 表 (收益率/最大回刀/超额收益)
    ↓
归因分析 → 六维归因 (分析师/板块/置信度/评级组合/市场环境/失败模式)
    ↓
参数调优 → 五维调优建议 (选股阈值/板块权重/ReAct迭代/置信度校准/分析师权重)
    ↓
反馈注入 → 历史表现摘要注入 Agent 提示词
    ↓
下一次推荐（系统"记住"了历史经验）
```

### 评估模块

| 模块 | 文件 | 功能 |
| --- | --- | --- |
| PerformanceDB | `stock_agent/eval/performance_db.py` | SQLite 持久化，3 张核心表 + 索引 |
| OutcomeTracker | `stock_agent/eval/outcome_tracker.py` | T+N 收益率计算，止盈/止损/持有到期判定 |
| AttributionEngine | `stock_agent/eval/attribution_engine.py` | 六维归因分析，生成优化建议 |
| ParamOptimizer | `stock_agent/eval/param_optimizer.py` | 保守参数调优，单次最大 20% 调整 |
| FeedbackInjector | `stock_agent/eval/feedback_injector.py` | 历史表现注入 Agent system prompt |
| BacktestRunner | `stock_agent/eval/backtest_runner.py` | 批量历史回测 |

### 触发机制

| 环节 | 触发方式 | 说明 |
| --- | --- | --- |
| 反馈注入 | 自动 | 每次 `propagate()` 启动时自动注入 |
| 归因分析 | 自动 | 每次运行结束生成验证报告时自动执行 |
| 参数调优 | 自动（dry_run） | 验证报告生成时自动执行，只生成建议不自动应用 |
| 结果追踪 | 手动 | 需持仓周期结束后运行脚本 |

### 安全机制

- 样本门槛：归因需 ≥5 条追踪记录，调优需 ≥10 条
- 幅度限制：单次参数调整不超过 20%
- 只读模式：调优仅生成建议写入验证报告，不自动修改配置

## 稳定性设计

### 数据源熔断器

provider + method 级别熔断，避免对不可用 API 反复重试：

- 3 次失败 → 120s 冷却 → HALF_OPEN 探测 → 渐进退避（2x → 4x）
- 成功后自动重置，统计可观测

### LLM 安全调用

`safe_llm_invoke` 包装所有 LLM 调用：

- 90s 硬超时（消除卡死）
- 瞬时错误自动重试（2 次，指数退避）
- 超时/失败后降级为 FallbackResponse（填入中性评级继续推进）

### ReAct 循环约束

- 每轮最大工具调用数（默认 3）
- 单个分析师总工具调用上限（默认 10）
- 工具冷却机制（连续失败 2 次自动跳过）
- LLM 超时后基于现有信息生成最终分析

### 板块五维量化赋分

板块推理从"依赖 LLM 评分"改为纯量化计算，不花 token、零幻觉、确定性高：

| 指标 | 权重 | 含义 |
| --- | --- | --- |
| 热度 heat | 0.25 | 资金净流入 + 注意力 |
| 扩散力 diffusion | 0.20 | 板块内上涨广度 |
| 动摇度 volatility | 0.15 | 涨跌幅 + 换手率 |
| 回补力 rebound | 0.20 | 上行动能 |
| 拥挤度 crowding | 0.20 | 资金 + 注意力双高 = 拥挤 |

拥挤度地图颜色梯度：≥80 极度拥挤 / 60-79 拥挤 / 40-59 适中 / 20-39 宽松 / <20 极度宽松

## 数据源

| 数据源 | 用途 | 免费 |
| --- | --- | --- |
| AkShare | 主数据源（行情/财务/龙虎榜/资金流/板块/新闻） | 是 |
| 腾讯财经 | 独立 fallback（qt.gtimg.cn） | 是 |
| efinance | 独立 fallback（封装东财/腾讯） | 是 |
| BaoStock | 备用（行情/财务） | 是 |
| Tushare | 备用（需注册 token） | 否 |
| 巨潮资讯 | 公告检索 | 是 |
| 政策信源 | 6 类权威政策事件 | 是 |

6 类政策信源：证监会 / 央行 / 发改委 / 交易所 / 巨潮 / 四大证券报，独立限速 + 合规降级。

## API 接口

后端同时托管前端静态文件，单端口部署：

| 接口 | 方法 | 功能 |
| --- | --- | --- |
| `/api/health` | GET | 健康检查 |
| `/api/status` | GET | 运行状态（进度/日志/耗时） |
| `/api/run` | POST | 启动推荐流程 |
| `/api/stop` | POST | 停止运行 |
| `/api/results` | GET | 获取推荐结果 |
| `/api/results/latest` | GET | 获取最新推荐 |
| `/api/history` | GET | 历史运行列表 |
| `/api/config` | GET / POST | 获取/保存配置 |
| `/api/metrics` | GET | 运行指标 |

前端通过相对路径 `/api/...` 调用，部署时不依赖固定端口。

## 目录结构

```
short-term-stock-agent/
├── main.py                          # 命令行入口
├── api_server.py                    # 全栈 API 服务器 + 前端托管
├── run.bat                          # Windows 一键启动脚本
├── .env.example                     # 环境变量模板（脱敏）
├── .gitignore
├── requirements.txt
├── README.md
├── config/
│   └── settings.py                  # 配置加载器 (.env → DEFAULT_CONFIG)
├── docs/
│   └── 产品项目方案.md
├── frontend-UI/
│   └── index.html                   # 前端单页应用
├── stock_agent/
│   ├── default_config.py            # 默认配置
│   ├── llm_clients/                 # LLM 客户端 (OpenAI 兼容)
│   ├── agents/
│   │   ├── discovery/               # 发现层 (7 个 Agent)
│   │   ├── analysis/                # 分析层 (4 维 ReAct 分析师)
│   │   ├── researchers/             # 多空研究员 (Bull/Bear/Manager)
│   │   ├── trader/                   # 交易代理
│   │   ├── risk_mgmt/               # 风控 (激进/保守/中立/风险经理/组合经理)
│   │   ├── output/                   # 报告生成
│   │   └── utils/                    # 状态/提示词/ReAct/工具日志/llm_utils
│   ├── dataflows/                   # 数据源 (AkShare/腾讯/BaoStock/Tushare/CnInfo/政策) + 熔断器
│   ├── tools/                       # 10 个分析师工具 (@tool)
│   ├── eval/                        # V3 评估闭环 (归因/调优/追踪/反馈/回测)
│   └── graph/                       # LangGraph 图编排
├── tests/                           # 测试套件 (107 项)
└── results/                         # 运行产出 (gitignore, 仅保留目录结构)
```

## 测试

```bash
python -m pytest tests/ -v                      # 全部测试 (107 项)
python -m pytest tests/test_smoke.py -v          # 冒烟测试 (9 项, 不连真实网络)
python -m pytest tests/test_sector_inference.py  # 板块推理 (31 项)
python -m pytest tests/test_parallel_architecture.py  # 四路并行集成 (18 项)
python -m pytest tests/test_provider_health.py   # 熔断器 (13 项)
python -m pytest tests/test_llm_utils.py         # safe_llm_invoke (12 项)
python -m pytest tests/test_technical_indicator_calc.py  # 技术指标 (24 项)
```

## 致谢

本项目参考了 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) 的多空辩论决策架构、LangGraph 多智能体编排模式和 ReAct 迭代分析框架。

在 TradingAgents-CN 架构基础上，本项目针对 A 股短线交易场景进行了大量原创改进：

- 板块推理重构为第四路并行纯量化节点（五维赋分 + 拥挤度地图 + 日环比）
- 双路选股（主线热门 + 支线黑马）+ 四维 ReAct 分析（含个股发展维度）
- 数据源 provider+method 级熔断器 + 渐进退避
- safe_llm_invoke（90s 硬超时 + 重试 + 降级）
- SKILL 六段提示词注入（短线趋势/催化/风险/误区）
- V3 评估闭环（归因/调优/追踪/反馈注入自学习）
- 全栈 Web 界面（单端口部署 + 实时监控 + 在线配置）

感谢 TradingAgents-CN 开源社区的贡献。

## License

MIT
