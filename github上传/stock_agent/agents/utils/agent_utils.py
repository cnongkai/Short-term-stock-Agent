"""
Agent 工具包 (对应 PRD M4c 分析师工具链)

Toolkit 类聚合 8 类工具方法, 供分析师 bind_tools 使用。
参考架构: TradingAgents-CN agents/utils/agent_utils.py (Toolkit)

注: 工具的实际实现位于 stock_agent/tools/, 此处仅做聚合与懒加载。
"""
from typing import Any, List


class Toolkit:
    """分析师工具包, 聚合 PRD 8.7 定义的 8 类工具

    工具列表 (PRD M4c):
      1. financial_data_query      - 财务数据查询
      2. announcement_search       - 公告检索 (巨潮)
      3. news_search               - 新闻搜索
      4. technical_indicator_calc  - 技术指标计算
      5. dragon_tiger_query        - 龙虎榜查询
      6. fund_flow_query           - 资金流向查询
      7. industry_comparison       - 行业对比
      8. research_report_search    - 研报检索
    """

    def __init__(self, config: dict):
        self.config = config
        self._tool_registry = None  # 懒加载 ToolRegistry

    @property
    def tool_registry(self):
        """懒加载工具注册中心"""
        if self._tool_registry is None:
            from stock_agent.tools.tool_registry import ToolRegistry

            self._tool_registry = ToolRegistry(config=self.config)
        return self._tool_registry

    def get_tools_for_analyst(self, analyst_name: str) -> List[Any]:
        """根据分析师类型返回其可调用的工具子集 (PRD 8.7 调用方列)

        Args:
            analyst_name: 分析师名称 (fundamentals/technical/china_specific/stock_development)

        Returns:
            langchain Tool 实例列表
        """
        registry = self.tool_registry
        # PRD 8.7 规定的各分析师可用工具子集
        # V2 新增: web_search (全量自救搜索) / realtime_quote (实时行情, 需价格的维度)
        tool_mapping = {
            "fundamentals": [
                "financial_data_query",
                "industry_comparison",
                "research_report_search",
                "realtime_quote",   # V2: 实时价格计算 PE/PB
                "web_search",       # V2: 数据源失败时自救
            ],
            "technical": [
                "technical_indicator_calc",
                "news_search",
                "realtime_quote",   # V2: 获取最新价/支撑压力位
                "web_search",       # V2: 数据源失败时自救
            ],
            "china_specific": [
                "dragon_tiger_query",
                "fund_flow_query",
                "realtime_quote",   # V2: 实时涨跌停判断
                "web_search",       # V2: 数据源失败时自救
            ],
            "stock_development": [
                "announcement_search",
                "news_search",
                "research_report_search",
                "web_search",       # V2: 公告/新闻失败时自救搜索
            ],
            "news": ["news_search", "web_search"],
            "sentiment": ["news_search", "web_search"],
            "policy": ["news_search", "web_search"],
        }
        tool_names = tool_mapping.get(analyst_name, [])
        return [registry.get_tool(name) for name in tool_names]

    # === 便捷方法: 暴露各工具供直接调用 ===
    @property
    def financial_data_query(self):
        return self.tool_registry.get_tool("financial_data_query")

    @property
    def announcement_search(self):
        return self.tool_registry.get_tool("announcement_search")

    @property
    def news_search(self):
        return self.tool_registry.get_tool("news_search")

    @property
    def technical_indicator_calc(self):
        return self.tool_registry.get_tool("technical_indicator_calc")

    @property
    def dragon_tiger_query(self):
        return self.tool_registry.get_tool("dragon_tiger_query")

    @property
    def fund_flow_query(self):
        return self.tool_registry.get_tool("fund_flow_query")

    @property
    def industry_comparison(self):
        return self.tool_registry.get_tool("industry_comparison")

    @property
    def research_report_search(self):
        return self.tool_registry.get_tool("research_report_search")

    @property
    def web_search(self):
        """V2 新增: 网络搜索工具 (DuckDuckGo, 独立于 provider 链)"""
        return self.tool_registry.get_tool("web_search")

    @property
    def realtime_quote(self):
        """V2 新增: 实时行情工具 (新浪, 独立于 provider 链)"""
        return self.tool_registry.get_tool("realtime_quote")


def create_msg_delete():
    """创建消息清理节点 (复用 TA 模式)

    在每个分析师节点结束后清理 messages, 避免上下文过长。
    """

    def msg_delete_node(state):
        # 仅保留最近必要的消息, 清理工具调用历史
        return {"messages": []}

    return msg_delete_node
