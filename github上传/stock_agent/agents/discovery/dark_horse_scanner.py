"""
黑马扫描器 - 支线 (对应 PRD Phase 3 / 6.6 / 8.4, V2 新增)

扫描非热门板块中具备暴涨潜力的个股, 是双路选股的"支线"路径。
与主线选股 (stock_selector) 并行执行, 两者写入不同的状态字段
(dark_horse_candidates / hot_sector_candidates), 由 Pool Merger 合并去重。

黑马四维信号评分模型 (PRD 6.6):
  综合评分 = 异动量价*0.30 + 突发资金*0.25 + 技术突破*0.20 + 催化事件*0.25
  评分 ≥ 60 分 (dark_horse_score_threshold) 进入候选池, 最多 3 只 (dark_horse_max)

四维信号:
  1. 异动量价 (30%): 成交量较前5日均量突增≥200%, 当日涨幅≥3%, 非涨停
  2. 突发资金 (25%): 主力资金净流入排名全市场前50, 所属板块不在热门Top N
  3. 技术突破 (20%): 突破60日均线/箱体上沿/前期高点, MACD金叉确认
  4. 催化事件 (25%): 近3日内发布重大合同/并购重组/业绩预增/回购等利好公告

图节点接口: create_dark_horse_scanner(llm) → dark_horse_scanner_node(state) -> dict
返回: {"dark_horse_candidates": [...]}  (单写者, 无需 reducer)
降级 (PRD 10.2): 数据源失败或无合格黑马时返回空列表, 仅输出主线推荐
"""
import json
import time
from loguru import logger

from stock_agent.agents.utils.prompts import DARK_HORSE_SCANNER_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.log_utils import log_stage
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.dataflows import interface as data_interface


def create_dark_horse_scanner(llm):
    """创建黑马扫描图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        dark_horse_scanner_node(state) -> dict: 更新 dark_horse_candidates
    """

    def dark_horse_scanner_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[黑马扫描] 开始扫描非热门板块黑马股")

        trade_date = state.get("trade_date", "")
        ranked_sectors = state.get("ranked_sectors", [])

        # 热门板块名集合 (用于排除, 黑马须来自非热门板块)
        hot_sector_names = {
            s.get("sector", "") for s in ranked_sectors
            if isinstance(s, dict) and s.get("sector")
        }
        logger.info(f"[黑马扫描] 输入: {len(ranked_sectors)} 个热门板块 (将排除), "
                    f"排除集合: {list(hot_sector_names)[:5]}")

        # === 读取黑马配置 (PRD 6.6) ===
        from stock_agent.default_config import DEFAULT_CONFIG
        dark_horse_max = DEFAULT_CONFIG.get("dark_horse_max", 3)
        score_threshold = DEFAULT_CONFIG.get("dark_horse_score_threshold", 60)

        # === 获取市场异动股 (全市场涨幅榜/量比榜) ===
        with log_stage("获取市场异动股", "黑马扫描") as stage:
            market_movers = _fetch_market_movers()
            stage.result = f"{len(market_movers)} 字符"

        if not market_movers or market_movers == "(暂无可用市场异动数据)":
            logger.info("[黑马扫描] 无市场异动数据, 跳过黑马扫描 (PRD 10.2 仅输出主线)")
            return {"dark_horse_candidates": []}

        # === 获取异动股的资金流数据 (限 top 候选, 避免过多调用) ===
        with log_stage("获取异动股资金流", "黑马扫描") as stage:
            fund_flow_data = _fetch_fund_flow_for_movers(market_movers, top_n=15)
            stage.result = f"{len(fund_flow_data)} 字符"

        # === 构建 prompt (PRD 8.4 / 6.6) ===
        prompt = f"""{DARK_HORSE_SCANNER_SYSTEM}

分析日期: {trade_date}
热门板块 (黑马须来自非热门板块): {list(hot_sector_names) or '(无)'}
黑马数量上限: {dark_horse_max} 只
入选评分阈值: {score_threshold} 分

全市场异动股 (涨幅榜/量比榜):
{truncate_text(market_movers, 3000)}

异动股资金流向 (主力净流入):
{truncate_text(fund_flow_data, 2000)}

请按四维信号模型扫描黑马:
综合评分 = 异动量价*0.30 + 突发资金*0.25 + 技术突破*0.20 + 催化事件*0.25
- 异动量价: 成交量较前5日均量突增≥200%, 当日涨幅≥3%, 非涨停
- 突发资金: 主力资金净流入排名前50, 所属板块不在热门Top N
- 技术突破: 突破60日均线/箱体上沿/前期高点, MACD金叉确认
- 催化事件: 近3日内发布重大合同/并购重组/业绩预增/回购等利好

评分 ≥ {score_threshold} 分且来自非热门板块的个股入选, 最多 {dark_horse_max} 只。
无合格黑马时返回空列表 (PRD 10.2: 仅输出主线推荐)。

输出严格 JSON:
{{
  "candidates": [
    {{
      "ticker": "300XXX",
      "name": "某军工股",
      "sector": "国防",
      "source": "dark_horse",
      "trigger_signals": ["volume_surge", "fund_inflow", "technical_breakout"],
      "dark_horse_score": 78.5,
      "reason": "成交量突增280%+主力净流入3.2亿+突破60日均线"
    }}
  ]
}}"""

        # === 调用 LLM 扫描黑马 ===
        with log_stage("LLM 扫描黑马", "黑马扫描"):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"candidates": []}',
                node_name="黑马扫描",
            )
        result = safe_json_parse(response.content, default={"candidates": []})
        candidates = result.get("candidates", []) if isinstance(result, dict) else []

        # 标记来源 + 过滤低分
        filtered = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            c["source"] = "dark_horse"
            score = c.get("dark_horse_score", 0)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0
            if score >= score_threshold:
                filtered.append(c)

        # 按评分降序, 截取上限
        filtered.sort(key=lambda x: float(x.get("dark_horse_score", 0)), reverse=True)
        filtered = filtered[:dark_horse_max]

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[黑马扫描] 完成, 耗时 {node_elapsed:.2f}s, "
                    f"扫描到 {len(filtered)} 只黑马 (阈值 {score_threshold} 分)")
        for i, c in enumerate(filtered, 1):
            logger.info(f"  {i}. {c.get('ticker','?')} {c.get('name','?')} "
                        f"[{c.get('sector','?')}] score={c.get('dark_horse_score','?')}")

        # 仅写 dark_horse_candidates (单写者, 与主线选股并行不冲突)
        return {"dark_horse_candidates": filtered}

    return dark_horse_scanner_node


def _fetch_market_movers() -> str:
    """获取全市场异动股 (涨幅榜/量比榜, PRD 6.6)"""
    try:
        res = data_interface.get_market_movers()
        if res and res.get("data"):
            return json.dumps(res["data"][:50], ensure_ascii=False, default=str)
    except Exception as e:
        logger.debug(f"[黑马扫描] 获取市场异动股失败: {e}")
    return "(暂无可用市场异动数据)"


def _fetch_fund_flow_for_movers(movers_str: str, top_n: int = 15) -> str:
    """为异动股补充资金流数据 (限 top_n 只, 避免过多调用)

    Args:
        movers_str: 异动股 JSON 字符串
        top_n: 最多查询的个股数

    Returns:
        资金流数据 JSON 字符串
    """
    # 解析异动股列表, 提取 ticker
    tickers = []
    try:
        movers = json.loads(movers_str) if isinstance(movers_str, str) else movers_str
        if isinstance(movers, list):
            for m in movers[:top_n]:
                if isinstance(m, dict):
                    t = m.get("ticker") or m.get("code") or m.get("symbol")
                    if t:
                        tickers.append(str(t))
    except Exception:
        pass

    if not tickers:
        return "(无法解析异动股代码, 资金流数据为空)"

    # 逐只查询资金流 (限 top_n, 控制调用量)
    results = []
    for ticker in tickers[:top_n]:
        try:
            res = data_interface.get_fund_flow(ticker)
            if res and res.get("data"):
                results.append({"ticker": ticker, "fund_flow": res["data"]})
        except Exception as e:
            logger.debug(f"[黑马扫描] 获取 {ticker} 资金流失败: {e}")

    if not results:
        return "(资金流数据获取失败, 请基于异动量价和技术信号评估)"
    return json.dumps(results, ensure_ascii=False, default=str)
