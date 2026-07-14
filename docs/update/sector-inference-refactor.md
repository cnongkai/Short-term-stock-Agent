# 板块推理架构重构: 并行化 + 五维赋分 + 拥挤度地图

## 概述

将板块推理从"下游汇总者"重构为"并行独立节点", 与新闻/情绪/政策分析师同时运行。
基于资金面+注意力构建搜索指数, 用五维指标(热度/扩散力/动摇度/回补力/拥挤度)对板块赋分,
输出 Top 10 拥挤度地图(颜色梯度), 然后与其它并行层一起进入主线选股和黑马扫描。

## 当前架构 vs 目标架构

### 当前架构
```
START → News Analyst ─────────┐
START → Sentiment Analyst ────┼→ Sector Inference → Stock Selector ─┐
START → Policy Analyst ───────┘                    → Dark Horse Scanner ─┬→ Pool Merger → Analysis
```
- Sector Inference **依赖**三路分析师的 `hot_topics` / `policy_events`
- 四维 LLM 评分: 情绪热度*0.25 + 资金流向*0.30 + 涨幅趋势*0.20 + 政策影响*0.25
- 输出 `ranked_sectors` (Top 5)

### 目标架构
```
START → News Analyst ─────────────────┐
START → Sentiment Analyst ────────────┤
START → Policy Analyst ───────────────┼→ Stock Selector ─┐
START → Sector Inference (独立) ──────┘  → Dark Horse Scanner ─┬→ Pool Merger → Analysis
```
- Sector Inference **独立运行**, 不依赖三路分析师产出
- 自行获取资金面(板块资金流) + 注意力(Bing搜索)构建搜索指数
- 五维算法赋分(无LLM, 纯量化): 热度/扩散力/动摇度/回补力/拥挤度
- 输出 `ranked_sectors` + `sector_crowding_map` (Top 10, 颜色梯度)
- 四路并行完成后, 一起进入 Stock Selector + Dark Horse Scanner

---

## 实施方案

### Step 1: 重构图边 — `graph/setup.py`

**文件**: [setup.py](../../stock_agent/graph/setup.py)

#### 1.1 修改 Sector Inference 节点创建 (L81)
传入 config 供新模块读取指标权重:
```python
# 旧:
workflow.add_node("Sector Inference", create_sector_inference(self.quick_thinking_llm))
# 新:
workflow.add_node("Sector Inference", create_sector_inference(self.quick_thinking_llm, self.config))
```

#### 1.2 重构发现层边 (L109-123)
```python
# === Phase 1: 四路并行发现 (V2: Sector Inference 独立并行) ===
workflow.add_edge(START, "News Analyst")
workflow.add_edge(START, "Sentiment Analyst")
workflow.add_edge(START, "Policy Analyst")
workflow.add_edge(START, "Sector Inference")  # V2: 第四路并行

# === Phase 2: 四路汇合 → 双路选股 (LangGraph 自动等待所有前驱完成) ===
# News/Sentiment/Policy 的 hot_topics/policy_events 在此点已完成累积
# Sector Inference 的 ranked_sectors 也已就绪
for analyst in ["News Analyst", "Sentiment Analyst", "Policy Analyst", "Sector Inference"]:
    workflow.add_edge(analyst, "Stock Selector")
    workflow.add_edge(analyst, "Dark Horse Scanner")

# 双路选股 → 合并
workflow.add_edge("Stock Selector", "Pool Merger")
workflow.add_edge("Dark Horse Scanner", "Pool Merger")
```

**删除的旧边**:
```python
# 旧 (删除):
workflow.add_edge("News Analyst", "Sector Inference")
workflow.add_edge("Sentiment Analyst", "Sector Inference")
workflow.add_edge("Policy Analyst", "Sector Inference")
```

---

### Step 2: 新增状态字段 — `agents/utils/agent_states.py`

**文件**: [agent_states.py](../../stock_agent/agents/utils/agent_states.py)

在 `ranked_sectors` 之后添加拥挤度地图字段 (L71 附近):
```python
    # 板块排名: 含 sector/heat_score/trend/... + V2 五维指标
    ranked_sectors: Annotated[list, "板块排名 Top N (PRD 8.3, V2 含五维指标+环比)"]
    # V2 新增: Top 10 板块拥挤度地图 (含颜色梯度)
    sector_crowding_map: Annotated[dict, "板块拥挤度地图 (Top 10, 含颜色梯度, V2 新增)"]
```

---

### Step 3: 重写板块推理器 — `agents/discovery/sector_inference.py`

**文件**: [sector_inference.py](../../stock_agent/agents/discovery/sector_inference.py)

**完全重写**, 从 LLM 驱动改为纯量化算法驱动。

#### 3.1 核心结构
```python
def create_sector_inference(llm, config=None):
    """创建板块推理图节点 (V2: 独立并行, 五维量化赋分)

    不再依赖三路分析师的 hot_topics/policy_events,
    自行获取资金面+注意力数据, 算法计算五维指标。
    """
    _config = config or {}
    _weights = _config.get("sector_metric_weights", {
        "heat": 0.25, "diffusion": 0.20, "volatility": 0.15,
        "rebound": 0.20, "crowding": 0.20,
    })

    def sector_inference_node(state):
        trade_date = state.get("trade_date", "")
        top_n = _config.get("sector_top_n", 5)

        # 1. 获取资金面数据 (板块行情+资金流)
        sector_list = _fetch_sector_data_raw()  # list[dict]

        # 2. 获取注意力数据 (Bing 搜索各板块提及量)
        attention_map = _fetch_attention_scores(sector_list)

        # 3. 五维赋分
        scored = [_score_sector(s, attention_map) for s in sector_list]

        # 4. 加权总分
        for s in scored:
            s["total_score"] = (
                _weights["heat"] * s["heat"]
                + _weights["diffusion"] * s["diffusion"]
                + _weights["volatility"] * s["volatility"]
                + _weights["rebound"] * s["rebound"]
                + _weights["crowding"] * s["crowding"]
            )

        # 5. 环比 (对比上次运行)
        prev = _load_prev_scores(trade_date)
        for s in scored:
            p = prev.get(s["sector"], {})
            s["mom_change"] = _calc_mom(s["total_score"], p.get("total_score"))
        _save_scores(scored, trade_date)

        # 6. 排序 → Top 10
        scored.sort(key=lambda x: x["total_score"], reverse=True)
        top_10 = scored[:10]

        # 7. 拥挤度地图
        crowding_map = _build_crowding_map(top_10)

        # 8. 输出 ranked_sectors (向后兼容 Stock Selector / Dark Horse Scanner)
        ranked = [_to_ranked_sector(s) for s in top_10[:top_n]]

        logger.info(f"[板块推理] 赋分完成, Top 3: ...")
        return {"ranked_sectors": ranked, "sector_crowding_map": crowding_map}

    return sector_inference_node
```

#### 3.2 五维指标计算函数
```python
def _score_sector(sector: dict, attention: dict) -> dict:
    """计算单板块五维指标 (0-100)

    输入 sector 字段 (AkShare 兼容):
      板块名称 / 涨跌幅 / 主力净流入 / 换手率 / 上涨家数 / 下跌家数
    """
    name = sector.get("板块名称") or sector.get("sector") or ""
    change_pct = _safe_float(sector.get("涨跌幅", 0))
    fund_flow = _safe_float(sector.get("主力净流入", 0))
    turnover = _safe_float(sector.get("换手率", 0))
    up_count = _safe_float(sector.get("上涨家数", 0))
    down_count = _safe_float(sector.get("下跌家数", 0))
    attn = attention.get(name, 0)  # 0-100

    # 1. 热度: 资金规模 + 注意度
    heat = _normalize(fund_flow, 0, 50e8) * 60 + attn * 0.4  # 50亿为参考上限

    # 2. 扩散力: 上涨家数占比 + 注意度广度
    total_stocks = up_count + down_count
    up_ratio = up_count / total_stocks if total_stocks > 0 else 0.5
    diffusion = up_ratio * 50 + attn * 0.5

    # 3. 动摇度: 涨跌幅绝对值 + 换手率
    volatility = min(abs(change_pct) * 15, 60) + min(turnover * 5, 40)

    # 4. 回补力: 涨幅正向 + 上涨家数占比
    rebound = max(0, change_pct) * 10 + up_ratio * 40
    rebound = min(rebound, 100)

    # 5. 拥挤度: 资金流入 + 注意度 (双高=拥挤)
    crowding = _normalize(fund_flow, 0, 30e8) * 50 + attn * 0.5

    return {
        "sector": name, "heat": round(heat, 1), "diffusion": round(diffusion, 1),
        "volatility": round(volatility, 1), "rebound": round(rebound, 1),
        "crowding": round(crowding, 1),
        "fund_flow": fund_flow, "change_pct": change_pct,
        "components": sector.get("领涨股", []),
    }
```

#### 3.3 注意力获取 (Bing 搜索)
```python
def _fetch_attention_scores(sector_list: list) -> dict:
    """通过 Bing 搜索获取各板块注意力分数

    策略: 批量搜索"A股 热门板块 今日", 统计各板块名在搜索结果中的出现次数。
    避免逐板块搜索 (太慢), 一次搜索 + 关键词匹配。
    """
    from stock_agent.tools.web_search import _bing_search
    try:
        results = _bing_search("A股 热门板块 今日 资金流入", max_results=20, timeout=15)
        # 统计各板块名在标题+摘要中的出现次数
        attention = {}
        for sector in sector_list:
            name = sector.get("板块名称") or sector.get("sector") or ""
            if not name:
                continue
            count = sum(
                1 for r in results
                if name in r.get("title", "") or name in r.get("snippet", "")
            )
            # 归一化到 0-100 (最多出现 20 次 = 100 分)
            attention[name] = min(count * 5, 100)
        return attention
    except Exception as e:
        logger.debug(f"[板块推理] 注意力获取失败: {e}")
        return {}
```

#### 3.4 拥挤度地图 (颜色梯度)
```python
def _build_crowding_map(top_10: list) -> dict:
    """构建 Top 10 板块拥挤度地图 (颜色梯度映射)

    颜色梯度:
      >= 80: 🔴 极度拥挤
      60-79: 🟠 拥挤
      40-59: 🟡 适中
      20-39: 🟢 宽松
      < 20:  🔵 极度宽松
    """
    def _color(score):
        if score >= 80: return {"color": "red", "icon": "🔴", "label": "极度拥挤"}
        if score >= 60: return {"color": "orange", "icon": "🟠", "label": "拥挤"}
        if score >= 40: return {"color": "yellow", "icon": "🟡", "label": "适中"}
        if score >= 20: return {"color": "green", "icon": "🟢", "label": "宽松"}
        return {"color": "blue", "icon": "🔵", "label": "极度宽松"}

    sectors = []
    for i, s in enumerate(top_10, 1):
        c = _color(s["crowding"])
        sectors.append({
            "rank": i,
            "sector": s["sector"],
            "crowding": s["crowding"],
            "total_score": s["total_score"],
            "mom_change": s.get("mom_change"),
            "color": c["color"],
            "icon": c["icon"],
            "label": c["label"],
            "metrics": {
                "heat": s["heat"], "diffusion": s["diffusion"],
                "volatility": s["volatility"], "rebound": s["rebound"],
            },
        })

    # ASCII 地图 (日志输出)
    map_text = "\n".join(
        f"  {s['icon']} #{s['rank']:2d} {s['sector']:<8s} "
        f"拥挤={s['crowding']:5.1f} 总分={s['total_score']:5.1f} "
        f"环比={s.get('mom_change','N/A')}"
        for s in sectors
    )
    logger.info(f"[板块推理] 拥挤度地图 (Top 10):\n{map_text}")

    return {"top_10": sectors, "map_text": map_text, "trade_date": ...}
```

#### 3.5 环比 (历史分数持久化)
```python
_SCORES_FILE = os.path.join(
    os.getenv("RESULTS_DIR", "./results"), "sector_scores_history.json"
)

def _load_prev_scores(trade_date: str) -> dict:
    """加载上一次运行的板块分数 (用于环比)"""
    try:
        if os.path.exists(_SCORES_FILE):
            with open(_SCORES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 返回最近一次 (非当天) 的分数
            for entry in reversed(data.get("history", [])):
                if entry.get("trade_date") != trade_date:
                    return {s["sector"]: s for s in entry.get("scores", [])}
    except Exception:
        pass
    return {}

def _save_scores(scored: list, trade_date: str):
    """保存当前板块分数 (供下次环比)"""
    try:
        os.makedirs(os.path.dirname(_SCORES_FILE), exist_ok=True)
        data = {"history": []}
        if os.path.exists(_SCORES_FILE):
            with open(_SCORES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["history"].append({"trade_date": trade_date, "scores": scored})
        # 保留最近 30 天
        data["history"] = data["history"][-30:]
        with open(_SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str, indent=2)
    except Exception as e:
        logger.debug(f"[板块推理] 保存历史分数失败: {e}")

def _calc_mom(current: float, prev: float or None) -> str:
    """计算环比变化率"""
    if prev is None or prev == 0:
        return "N/A(首次)"
    change = (current - prev) / prev * 100
    return f"{change:+.1f}%"
```

#### 3.6 向后兼容输出
```python
def _to_ranked_sector(scored: dict) -> dict:
    """转换为 ranked_sectors 格式 (向后兼容 Stock Selector / Dark Horse Scanner)"""
    return {
        "sector": scored["sector"],
        "heat_score": scored["total_score"],  # 兼容: 用总分作为 heat_score
        "trend": "up" if scored["change_pct"] > 0 else ("down" if scored["change_pct"] < 0 else "flat"),
        "fund_flow": scored["fund_flow"],
        "change_pct": scored["change_pct"],
        "rationale": f"热度={scored['heat']}/扩散={scored['diffusion']}/动摇={scored['volatility']}/回补={scored['rebound']}/拥挤={scored['crowding']}",
        "components": scored.get("components", []),
        # V2 新增字段 (下游可选使用)
        "metrics": {
            "heat": scored["heat"], "diffusion": scored["diffusion"],
            "volatility": scored["volatility"], "rebound": scored["rebound"],
            "crowding": scored["crowding"],
        },
        "mom_change": scored.get("mom_change"),
    }
```

---

### Step 4: 更新配置 — `default_config.py`

**文件**: [default_config.py](../../stock_agent/default_config.py)

在 sector 相关配置附近添加:
```python
    # V2: 板块五维指标权重
    "sector_metric_weights": {
        "heat": 0.25,        # 热度: 资金+注意力
        "diffusion": 0.20,   # 扩散力: 上涨广度+注意力广度
        "volatility": 0.15,  # 动摇度: 涨跌幅+换手率
        "rebound": 0.20,     # 回补力: 涨幅+上涨占比
        "crowding": 0.20,    # 拥挤度: 资金+注意力(双高=拥挤)
    },
    "sector_crowding_top_n": 10,  # 拥挤度地图输出前 N
```

---

### Step 5: 更新 Smoke Test — `tests/test_smoke.py`

**文件**: [test_smoke.py](../../tests/test_smoke.py)

#### 5.1 更新初始状态断言 (L83 附近)
```python
    assert state["ranked_sectors"] == []
    assert state["sector_crowding_map"] == {}  # V2 新增
```

#### 5.2 更新 MockLLM 返回 (L34)
不需要改动 — Sector Inference 不再使用 LLM。

---

## 假设与决策

1. **纯量化 vs LLM**: 选纯量化 — 五维指标均可从数据直接计算, 无需 LLM 推理, 更快更稳定。
2. **注意力数据源**: 选 Bing 搜索 — 已验证可用, 无需额外 API key。一次批量搜索 + 关键词匹配, 避免逐板块搜索。
3. **历史分数持久化**: 选 JSON 文件 (`results/sector_scores_history.json`) — 轻量, 无需数据库, 保留 30 天。
4. **向后兼容**: `ranked_sectors` 输出格式保持兼容 (sector/heat_score/trend/components), 下游 Stock Selector / Dark Horse Scanner 无需改动。
5. **四路并行同步**: LangGraph 自动处理 — 当 4 个前驱节点 (News/Sentiment/Policy/Sector Inference) 都有边指向 Stock Selector 时, Stock Selector 等待全部完成才执行。
6. **拥挤度颜色**: 5 级梯度 (红/橙/黄/绿/蓝) — 直观映射拥挤程度, 同时输出 emoji + 文字标签 + ASCII 地图。

## 验证步骤

1. **Smoke test**: `python -m pytest tests/test_smoke.py -v` — 9/9 通过
2. **板块推理单测**: 
   ```python
   from stock_agent.agents.discovery.sector_inference import create_sector_inference
   node = create_sector_inference(llm=None, config={})
   result = node({"trade_date": "2026-07-11"})
   print(result["sector_crowding_map"]["map_text"])
   ```
3. **图编译验证**: `python -c "from stock_agent.graph.trading_graph import StockRecommendationGraph; g = StockRecommendationGraph(); print('OK')"` — 确认新图结构可编译
4. **全流程验证** (可选): `python main.py --date 2026-07-11`

## 实施顺序

1. Step 2: 新增 `sector_crowding_map` 状态字段
2. Step 3: 重写 `sector_inference.py` (核心工作)
3. Step 4: 添加配置项
4. Step 1: 重构 `setup.py` 图边
5. Step 5: 更新 smoke test
6. 运行验证
