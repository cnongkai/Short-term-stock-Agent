"""
短线股票推荐 Agent - 默认配置 (对应 PRD Phase 0 用户配置 / M9 用户配置面板)

本模块定义系统运行时的基础配置项。运行时配置由 .env 文件覆盖,
未在 .env 中设置的项回退到此处的默认值。

参考架构: TradingAgents-CN default_config.py
"""
import os

# 项目根目录
_PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_CONFIG = {
    # === 路径配置 ===
    "project_dir": _PROJECT_DIR,
    "results_dir": os.getenv("RESULTS_DIR", "./results"),
    "log_dir": os.getenv("LOG_DIR", "./results/logs"),

    # === LLM 配置 (PRD Phase 0) ===
    "llm_provider": os.getenv("LLM_PROVIDER", "deepseek"),
    "deep_think_llm": os.getenv("DEEP_THINK_LLM", "deepseek-chat"),
    "quick_think_llm": os.getenv("QUICK_THINK_LLM", "deepseek-chat"),
    "backend_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    # 模型参数 (V2 优化: 超时 180→120s, 单次卡顿更快失败)
    "quick_model_config": {
        "max_tokens": 4000,
        "temperature": 0.7,
        "timeout": 120,
    },
    "deep_model_config": {
        "max_tokens": 4000,
        "temperature": 0.7,
        "timeout": 120,
    },

    # === ReAct 迭代循环配置 (PRD 6.7, V2 新增) ===
    "react_max_iterations": int(os.getenv("REACT_MAX_ITERATIONS", "5")),
    "react_max_tools_per_iter": int(os.getenv("REACT_MAX_TOOLS_PER_ITER", "3")),
    "react_max_total_tools": int(os.getenv("REACT_MAX_TOTAL_TOOLS", "10")),

    # === 按分析师维度的 ReAct 覆盖配置 (V2 优化, 降低 stock_development 迭代) ===
    # 优先级高于全局 react_max_* 配置, 缺失项回退全局值
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

    # === 分析层并行配置 (V2 优化, 提升效率) ===
    "analysis_parallel_dimensions": os.getenv("ANALYSIS_PARALLEL_DIMENSIONS", "true").lower() == "true",
    "analysis_parallel_stocks": int(os.getenv("ANALYSIS_PARALLEL_STOCKS", "1")),
    "analysis_max_workers": int(os.getenv("ANALYSIS_MAX_WORKERS", "4")),
    # 批量预取配置 (V2: 分析层前预热缓存)
    "prefetch_enabled": os.getenv("PREFETCH_ENABLED", "true").lower() == "true",
    "prefetch_workers": int(os.getenv("PREFETCH_WORKERS", "4")),
    "prefetch_timeout": int(os.getenv("PREFETCH_TIMEOUT", "120")),
    "prefetch_lookback_days": int(os.getenv("PREFETCH_LOOKBACK_DAYS", "60")),
    # 超时防挂起配置 (V2: 单维/标的/LLM 三级超时, 超时填兜底评级继续推进)
    "analysis_dimension_timeout": int(os.getenv("ANALYSIS_DIMENSION_TIMEOUT", "120")),
    "analysis_stock_timeout": int(os.getenv("ANALYSIS_STOCK_TIMEOUT", "300")),
    "llm_call_hard_timeout": int(os.getenv("LLM_CALL_HARD_TIMEOUT", "90")),

    # === 总运行预算 (V2 优化: 验证报告超时判定基准, 替代单次 LLM 超时) ===
    "total_run_budget": int(os.getenv("TOTAL_RUN_BUDGET", "1800")),  # 30 分钟

    # === 工具链配置 (PRD 8.7) ===
    "tool_call_timeout": int(os.getenv("TOOL_CALL_TIMEOUT", "30")),
    "tool_cache_ttl": int(os.getenv("TOOL_CACHE_TTL", "3600")),

    # === 辩论与风控配置 (PRD 6.9) ===
    "max_debate_rounds": int(os.getenv("MAX_DEBATE_ROUNDS", "2")),
    "max_risk_discuss_rounds": int(os.getenv("MAX_RISK_DISCUSS_ROUNDS", "1")),
    "max_recur_limit": int(os.getenv("MAX_RECUR_LIMIT", "200")),

    # === 选股参数 (PRD 7.3 / 6.6) ===
    "hot_sector_pool_min": int(os.getenv("HOT_SECTOR_POOL_MIN", "5")),
    "hot_sector_pool_max": int(os.getenv("HOT_SECTOR_POOL_MAX", "8")),
    "dark_horse_max": int(os.getenv("DARK_HORSE_MAX", "3")),
    "dark_horse_score_threshold": int(os.getenv("DARK_HORSE_SCORE_THRESHOLD", "60")),
    "sector_top_n": int(os.getenv("SECTOR_TOP_N", "5")),

    # === 板块五维指标权重 (V2: 纯量化赋分, 不依赖 LLM) ===
    # 五维归一化到 0-100, 加权得到总分; 权重之和应为 1.0
    "sector_metric_weights": {
        "heat": 0.25,        # 热度: 资金净流入 + 注意力指数
        "diffusion": 0.20,   # 扩散力: 上涨广度 + 注意力广度
        "volatility": 0.15,  # 动摇度: 涨跌幅 + 换手率
        "rebound": 0.20,     # 回补力: 涨幅 + 上涨占比
        "crowding": 0.20,    # 拥挤度: 资金 + 注意力双高=拥挤
    },
    "sector_crowding_top_n": int(os.getenv("SECTOR_CROWDING_TOP_N", "10")),  # 拥挤度地图输出前 N
    "sector_attention_search_enabled": os.getenv("SECTOR_ATTENTION_SEARCH_ENABLED", "true").lower() == "true",
    "sector_scores_history_path": os.getenv("SECTOR_SCORES_HISTORY_PATH", "results/sector_scores_history.json"),

    # === 熔断机制 (PRD 10.2) ===
    "circuit_breaker_drop_pct": float(os.getenv("CIRCUIT_BREAKER_DROP_PCT", "4.0")),

    # === 数据源开关 (PRD 6.10) ===
    "akshare_enabled": os.getenv("AKSHARE_ENABLED", "true").lower() == "true",
    "baostock_enabled": os.getenv("BAOSTOCK_ENABLED", "true").lower() == "true",
    "tushare_enabled": os.getenv("TUSHARE_ENABLED", "false").lower() == "true",
    "tushare_token": os.getenv("TUSHARE_TOKEN", ""),
    "cninfo_enabled": os.getenv("CNINFO_ENABLED", "true").lower() == "true",
    # AkShare 稳定性配置 (V2: socket 超时兜底 + HTTP 重试)
    "akshare_http_timeout": int(os.getenv("AKSHARE_HTTP_TIMEOUT", "30")),
    "akshare_socket_timeout": int(os.getenv("AKSHARE_SOCKET_TIMEOUT", "30")),
    # 缓存持久化配置 (V2: SQLite 双层缓存)
    "cache_enable_disk": os.getenv("CACHE_ENABLE_DISK", "true").lower() == "true",
    "cache_db_path": os.getenv("CACHE_DB_PATH", ""),  # 空则 results_dir/cache.sqlite
    "cache_flush_interval": int(os.getenv("CACHE_FLUSH_INTERVAL", "60")),

    # === 政策信源配置 (PRD 6.3, V2 新增) ===
    "policy_sources_enabled": os.getenv("POLICY_SOURCES_ENABLED", "true").lower() == "true",
    "policy_rate_limit_seconds": int(os.getenv("POLICY_RATE_LIMIT_SECONDS", "300")),
    "policy_fallback_rss": os.getenv("POLICY_FALLBACK_RSS", "true").lower() == "true",

    # === 数据源熔断器配置 (V2: 避免反复重试故障 provider) ===
    "provider_failure_threshold": int(os.getenv("PROVIDER_FAILURE_THRESHOLD", "3")),
    "provider_cooldown_seconds": int(os.getenv("PROVIDER_COOLDOWN_SECONDS", "120")),

    # === LLM 重试配置 (V2: 瞬时错误自动重试) ===
    "llm_retry_max": int(os.getenv("LLM_RETRY_MAX", "2")),
    "llm_retry_base_wait": float(os.getenv("LLM_RETRY_BASE_WAIT", "1.0")),
    "llm_retry_max_wait": float(os.getenv("LLM_RETRY_MAX_WAIT", "3.0")),

    # === 调试 ===
    "debug": os.getenv("DEBUG", "false").lower() == "true",
}
