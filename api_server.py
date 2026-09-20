"""
短线股票推荐 Agent — 全栈 API 服务器

基于 Python 标准库 http.server，零外部依赖。
提供 RESTful API 供前端调用，包括：
  - 启动/查询推荐运行
  - 获取推荐结果与验证指标
  - 获取/保存配置
  - 历史运行列表

启动方式:
  python api_server.py --port 5000
  python api_server.py  (默认端口 5000)

前端连接:
  前端通过 fetch('http://localhost:5000/api/...') 调用
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import glob
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 项目根目录
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

RESULTS_DIR = os.path.join(_PROJECT_DIR, "results")
REPORTS_DIR = os.path.join(RESULTS_DIR, "reports")
# 前端静态文件目录 (V5: 后端同时托管前端, 用户只需启动一个服务器)
FRONTEND_DIR = os.path.join(_PROJECT_DIR, "frontend-UI")

# ========== 全局运行状态 ==========
_run_lock = threading.Lock()
_run_state = {
    "status": "idle",        # idle | running | completed | error
    "trade_date": None,
    "started_at": None,
    "finished_at": None,
    "elapsed": None,
    "pid": None,
    "error": None,
    "log_lines": [],
    "heartbeat": None,       # V4: 最后心跳时间, 前端据此判断系统是否还活着
}
_process = None


def _update_state(**kwargs):
    """线程安全更新运行状态"""
    with _run_lock:
        _run_state.update(kwargs)


def _read_state():
    """线程安全读取运行状态"""
    with _run_lock:
        return dict(_run_state)


def _run_agent(trade_date, risk, holding):
    """在子进程中运行 Agent (V4: 独立线程读 stdout + 心跳 + 总超时保护)

    V4 修复要点:
    1. 用独立 daemon 线程读取子进程 stdout, 避免 readline() 阻塞主线程
    2. 主线程用 wait(timeout=5) 轮询, 每 5 秒更新心跳时间戳
    3. 添加 30 分钟总超时保护, 防止子进程无限挂起
    4. 修复 logger 未定义的 NameError
    """
    global _process
    try:
        _update_state(
            status="running",
            trade_date=trade_date,
            started_at=time.time(),
            finished_at=None,
            elapsed=None,
            error=None,
            log_lines=[],
            heartbeat=time.time(),
        )

        cmd = [
            sys.executable,
            "-u",  # 无缓冲模式
            os.path.join(_PROJECT_DIR, "main.py"),
            "--date", trade_date,
            "--risk", risk,
            "--holding", holding,
        ]

        child_env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

        _process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=_PROJECT_DIR,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        _update_state(pid=_process.pid)

        # --- V4: 独立线程读取子进程 stdout ---
        log_lines = []

        def _stdout_reader():
            """daemon 线程: 持续读取子进程 stdout, 更新 log_lines"""
            try:
                while True:
                    line = _process.stdout.readline()
                    if not line:
                        break
                    log_lines.append(line.rstrip())
                    if len(log_lines) > 500:
                        del log_lines[:len(log_lines) - 500]
                    _update_state(log_lines=list(log_lines[-200:]))
            except Exception:
                pass

        reader_thread = threading.Thread(target=_stdout_reader, daemon=True, name="stdout-reader")
        reader_thread.start()

        # --- V4: 主线程带超时轮询 + 心跳 + 总超时保护 ---
        MAX_RUNTIME = 1800  # 30 分钟总超时 (与 total_run_budget 对齐)
        start_time = _run_state["started_at"]

        while True:
            try:
                retcode = _process.wait(timeout=5)
                # 进程已退出
                break
            except subprocess.TimeoutExpired:
                # 每 5 秒更新心跳, 让前端知道系统还在运行
                elapsed_so_far = time.time() - start_time
                _update_state(
                    heartbeat=time.time(),
                    elapsed=round(elapsed_so_far, 2),
                )

                # 总超时检查
                if elapsed_so_far > MAX_RUNTIME:
                    print(f"[API] 子进程运行超 {MAX_RUNTIME}s, 强制终止", flush=True)
                    _process.kill()
                    _process.wait(timeout=10)
                    retcode = _process.returncode
                    _update_state(error=f"运行超时 ({MAX_RUNTIME}s), 已强制终止")
                    break

                # 检查子进程是否还活着 (可能已退出但 wait 没捕获到)
                if _process.poll() is not None:
                    retcode = _process.returncode
                    break

        # 等待 reader 线程结束 (最多 5 秒)
        reader_thread.join(timeout=5)

        elapsed = time.time() - start_time
        if retcode == 0:
            _update_state(status="completed", finished_at=time.time(), elapsed=round(elapsed, 2))
        else:
            existing_error = _read_state().get("error", "")
            _update_state(
                status="error",
                finished_at=time.time(),
                elapsed=round(elapsed, 2),
                error=existing_error or f"进程退出码: {retcode}",
            )
    except Exception as e:
        _update_state(status="error", error=str(e), finished_at=time.time())
    finally:
        _process = None


# ========== 数据读取函数 ==========

def _find_latest_metrics():
    """找到最新的 metrics JSON 文件"""
    pattern = os.path.join(REPORTS_DIR, "metrics_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    return files[-1]


def _find_metrics_for_date(date_str):
    """找到指定日期的 metrics JSON 文件"""
    pattern = os.path.join(REPORTS_DIR, f"metrics_{date_str}_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    return files[-1]


def _find_recommendation(date_str=None):
    """找到推荐 JSON 文件"""
    if date_str:
        path = os.path.join(RESULTS_DIR, f"recommendation_{date_str}.json")
        if os.path.exists(path):
            return path
        return None
    # 找最新的
    pattern = os.path.join(RESULTS_DIR, "recommendation_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    return files[-1]


def _load_json_safe(path):
    """安全加载 JSON 文件"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


def _get_history():
    """获取历史运行日期列表"""
    pattern = os.path.join(RESULTS_DIR, "recommendation_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    dates = []
    for f in files:
        basename = os.path.basename(f)
        # recommendation_2026-07-20.json → 2026-07-20
        date_str = basename.replace("recommendation_", "").replace(".json", "")
        dates.append({
            "date": date_str,
            "has_report": os.path.exists(os.path.join(RESULTS_DIR, f"recommendation_{date_str}.md")),
        })
    return dates


def _get_config():
    """获取当前配置（从 default_config + .env）"""
    try:
        from config.settings import load_config
        config = load_config()
        # 只返回前端需要的配置项
        quick_cfg = config.get("quick_model_config", {})
        return {
            "llm_provider": config.get("llm_provider", "deepseek"),
            "backend_url": config.get("backend_url", ""),
            "deep_think_llm": config.get("deep_think_llm", ""),
            "quick_think_llm": config.get("quick_think_llm", ""),
            "quick_model_config": quick_cfg,
            "deep_model_config": config.get("deep_model_config", {}),
            "max_tokens": quick_cfg.get("max_tokens", 4000),
            "temperature": quick_cfg.get("temperature", 0.7),
            "llm_call_timeout": quick_cfg.get("timeout", 120),
            "react_max_iterations": config.get("react_max_iterations", 5),
            "react_max_tools_per_iter": config.get("react_max_tools_per_iter", 3),
            "react_max_total_tools": config.get("react_max_total_tools", 10),
            "llm_call_hard_timeout": config.get("llm_call_hard_timeout", 90),
            "analysis_parallel_dimensions": config.get("analysis_parallel_dimensions", True),
            "analysis_max_workers": config.get("analysis_max_workers", 4),
            "max_debate_rounds": config.get("max_debate_rounds", 2),
            "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds", 1),
            "total_run_budget": config.get("total_run_budget", 1800),
            "max_recur_limit": config.get("max_recur_limit", 200),
            "circuit_breaker_drop_pct": config.get("circuit_breaker_drop_pct", 4.0),
            "provider_failure_threshold": config.get("provider_failure_threshold", 3),
            "provider_cooldown_seconds": config.get("provider_cooldown_seconds", 120),
            "llm_retry_max": config.get("llm_retry_max", 2),
            # V3
            "enable_outcome_tracking": config.get("enable_outcome_tracking", True),
            "enable_feedback_injection": config.get("enable_feedback_injection", True),
            "enable_attribution_analysis": config.get("enable_attribution_analysis", True),
            "enable_param_optimization": config.get("enable_param_optimization", True),
            "default_holding_period": config.get("default_holding_period", "1-5"),
            "feedback_lookback_days": config.get("feedback_lookback_days", 30),
            "attribution_lookback_days": config.get("attribution_lookback_days", 60),
            "attribution_min_samples": config.get("attribution_min_samples", 5),
            # 数据源
            "use_akshare": config.get("use_akshare", True),
            "use_baostock": config.get("use_baostock", True),
            "use_tencent": config.get("use_tencent", True),
            "use_cninfo": config.get("use_cninfo", True),
            # Tushare Token (从 .env 读取)
            "tushare_token": os.environ.get("TUSHARE_TOKEN", ""),
        }
    except Exception as e:
        return {"error": str(e)}


def _save_config(config_updates):
    """保存配置到 .env 文件"""
    env_path = os.path.join(_PROJECT_DIR, ".env")
    
    # 读取现有 .env
    existing = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
    
    # 映射前端配置到 .env 键名
    env_mapping = {
        "llm_provider": "LLM_PROVIDER",
        "backend_url": "DEEPSEEK_BASE_URL",
        "deep_think_llm": "DEEP_THINK_LLM",
        "quick_think_llm": "QUICK_THINK_LLM",
        "llm_call_hard_timeout": "LLM_CALL_HARD_TIMEOUT",
        "react_max_iterations": "REACT_MAX_ITERATIONS",
        "react_max_tools_per_iter": "REACT_MAX_TOOLS_PER_ITER",
        "react_max_total_tools": "REACT_MAX_TOTAL_TOOLS",
        "analysis_max_workers": "ANALYSIS_MAX_WORKERS",
        "max_debate_rounds": "MAX_DEBATE_ROUNDS",
        "max_risk_discuss_rounds": "MAX_RISK_DISCUSS_ROUNDS",
        "total_run_budget": "TOTAL_RUN_BUDGET",
        "provider_failure_threshold": "PROVIDER_FAILURE_THRESHOLD",
        # 扩展字段
        "max_recur_limit": "MAX_RECUR_LIMIT",
        "default_holding_period": "DEFAULT_HOLDING_PERIOD",
        "feedback_lookback_days": "FEEDBACK_LOOKBACK_DAYS",
        "attribution_lookback_days": "ATTRIBUTION_LOOKBACK_DAYS",
        "attribution_min_samples": "ATTRIBUTION_MIN_SAMPLES",
        "temperature": "TEMPERATURE",
        "max_tokens": "MAX_TOKENS",
        "tushare_token": "TUSHARE_TOKEN",
    }
    
    for key, env_key in env_mapping.items():
        if key in config_updates:
            existing[env_key] = str(config_updates[key])

    # 布尔类型配置 (checkbox)
    bool_mapping = {
        "analysis_parallel_dimensions": "ANALYSIS_PARALLEL_DIMENSIONS",
        "enable_outcome_tracking": "ENABLE_OUTCOME_TRACKING",
        "enable_feedback_injection": "ENABLE_FEEDBACK_INJECTION",
        "enable_attribution_analysis": "ENABLE_ATTRIBUTION_ANALYSIS",
        "enable_param_optimization": "ENABLE_PARAM_OPTIMIZATION",
        "use_akshare": "USE_AKSHARE",
        "use_baostock": "USE_BAOSTOCK",
        "use_tencent": "USE_TENCENT",
        "use_cninfo": "USE_CNINFO",
    }
    for key, env_key in bool_mapping.items():
        if key in config_updates:
            existing[env_key] = "true" if config_updates[key] else "false"
    
    # API Key 特殊处理
    if "api_key" in config_updates and config_updates["api_key"]:
        existing["DEEPSEEK_API_KEY"] = config_updates["api_key"]
    
    # 写入 .env
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("# Stock Agent Configuration (由前端自动生成)\n")
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    
    return {"success": True, "message": "配置已保存到 .env 文件"}


def _build_results_response(date_str=None):
    """构建完整的结果响应（推荐数据 + 指标）"""
    # 找推荐文件
    rec_path = _find_recommendation(date_str)
    if not rec_path:
        return {"error": "未找到推荐结果文件", "date": date_str}
    
    rec_data = _load_json_safe(rec_path)
    if "error" in rec_data:
        return rec_data
    
    # 提取 full_state 中的数据
    full_state = rec_data.get("full_state", rec_data)
    
    # 找指标文件
    actual_date = rec_data.get("trade_date", date_str or "")
    metrics_path = _find_metrics_for_date(actual_date)
    metrics_data = _load_json_safe(metrics_path) if metrics_path else {}
    
    # 构建前端需要的 APP_DATA 格式
    response = {
        "trade_date": actual_date,
        "final_portfolio": rec_data.get("final_portfolio", full_state.get("final_portfolio", [])),
        "ranked_sectors": full_state.get("ranked_sectors", rec_data.get("sector_heatmap", {}).get("top_10", [])),
        "hot_topics": full_state.get("hot_topics", []),
        "policy_events": full_state.get("policy_events", []),
        "candidate_pool": full_state.get("candidate_pool", []),
        "analysis_reports": full_state.get("analysis_reports", {}),
        "circuit_breaker": full_state.get("circuit_breaker", False),
        "final_trade_decision": full_state.get("final_trade_decision", ""),
        # 辩论状态数据 (供前端动态渲染)
        "investment_debate_state": full_state.get("investment_debate_state", []),
        "risk_debate_state": full_state.get("risk_debate_state", []),
        # 指标数据
        "metrics": {
            "total_elapsed": metrics_data.get("total_elapsed", 0),
            "timeout_count": metrics_data.get("timeout_count", 0),
            "node_timings": metrics_data.get("node_timings", {}),
            "llm_calls": metrics_data.get("llm_calls", []),
            "debate_info": metrics_data.get("debate_info", {}),
            "react_stats": metrics_data.get("react_stats", {}),
            "portfolio_count": metrics_data.get("portfolio_count", 0),
        },
    }

    return response


# ========== HTTP 请求处理器 ==========

class APIHandler(BaseHTTPRequestHandler):
    """RESTful API 请求处理器"""

    def log_message(self, format, *args):
        """静默日志（或可改为写入文件）"""
        pass

    def _send_json(self, data, code=200):
        """发送 JSON 响应"""
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """读取请求体"""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ========== V5: 前端静态文件服务 ==========

    # MIME 类型映射
    _MIME_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".eot": "application/vnd.ms-fontobject",
        ".map": "application/json; charset=utf-8",
    }

    def _serve_static_file(self, path):
        """托管前端静态文件 (V5: 后端同时充当 Web 服务器)

        访问 http://localhost:5000/ → 返回 frontend-UI/index.html
        访问 http://localhost:5000/style.css → 返回 frontend-UI/style.css
        """
        # 根路径 → index.html
        if path == "/" or path == "":
            path = "/index.html"

        # 安全: 防止路径穿越
        if ".." in path:
            self.send_error(403, "Forbidden")
            return

        # 映射到前端目录
        file_path = os.path.join(FRONTEND_DIR, path.lstrip("/"))

        if not os.path.isfile(file_path):
            # 如果文件不存在, 回退到 index.html (SPA 风格)
            fallback = os.path.join(FRONTEND_DIR, "index.html")
            if os.path.isfile(fallback):
                self._send_file(fallback, "text/html; charset=utf-8")
            else:
                self.send_error(404, "File not found")
            return

        # 根据扩展名设置 Content-Type
        ext = os.path.splitext(path)[1].lower()
        content_type = self._MIME_TYPES.get(ext, "application/octet-stream")

        self._send_file(file_path, content_type)

    def _send_file(self, file_path, content_type):
        """发送静态文件响应"""
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Internal error: {e}")

    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """处理 GET 请求"""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # === API 路由 ===
        if path == "/api/status":
            state = _read_state()
            # 运行中实时计算 elapsed（避免前端收到 null 显示为 -s）
            if state["status"] == "running" and state.get("started_at"):
                state["elapsed"] = round(time.time() - state["started_at"], 2)
            self._send_json(state)

        elif path == "/api/results":
            date_str = params.get("date", [None])[0]
            data = _build_results_response(date_str)
            self._send_json(data)

        elif path == "/api/results/latest":
            data = _build_results_response(None)
            self._send_json(data)

        elif path == "/api/history":
            self._send_json({"dates": _get_history()})

        elif path == "/api/config":
            self._send_json(_get_config())

        elif path == "/api/metrics":
            date_str = params.get("date", [None])[0]
            metrics_path = _find_metrics_for_date(date_str) if date_str else _find_latest_metrics()
            if metrics_path:
                self._send_json(_load_json_safe(metrics_path))
            else:
                self._send_json({"error": "未找到指标文件"}, 404)

        elif path == "/api/health":
            self._send_json({"status": "ok", "time": datetime.now().isoformat()})

        # === V5: 前端静态文件服务 (非 API 路由) ===
        elif not path.startswith("/api/"):
            self._serve_static_file(path)

        else:
            self._send_json({"error": "未找到", "path": path}, 404)

    def do_POST(self):
        """处理 POST 请求"""
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/api/run":
            state = _read_state()
            if state["status"] == "running":
                self._send_json({"error": "已有运行中的任务", "status": state}, 409)
                return

            trade_date = body.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
            risk = body.get("risk", "moderate")
            holding = body.get("holding", "1-5")

            # 启动后台线程
            t = threading.Thread(target=_run_agent, args=(trade_date, risk, holding), daemon=True)
            t.start()

            self._send_json({
                "message": "推荐运行已启动",
                "trade_date": trade_date,
                "risk": risk,
                "holding": holding,
            })

        elif path == "/api/stop":
            global _process
            if _process and _process.poll() is None:
                _process.terminate()
                _update_state(status="error", error="用户手动停止")
                self._send_json({"message": "已发送停止信号"})
            else:
                self._send_json({"message": "没有运行中的任务"})

        elif path == "/api/config":
            result = _save_config(body)
            self._send_json(result)

        else:
            self._send_json({"error": "未找到", "path": path}, 404)


def main():
    parser = argparse.ArgumentParser(description="Stock Agent API Server")
    parser.add_argument("--port", type=int, default=5000, help="监听端口")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), APIHandler)
    print(f"{'=' * 60}")
    print(f"  短线推荐 Agent — 全栈服务器 (API + 前端)")
    print(f"{'=' * 60}")
    print(f"  访问地址: http://localhost:{args.port}")
    print(f"  (直接在浏览器打开即可使用全部功能)")
    print(f"{'-' * 60}")
    print(f"  API 端点:")
    print(f"    GET  /api/status       - 运行状态")
    print(f"    POST /api/run          - 启动推荐")
    print(f"    POST /api/stop         - 停止运行")
    print(f"    GET  /api/results      - 推荐结果")
    print(f"    GET  /api/history      - 历史记录")
    print(f"    GET  /api/config       - 获取配置")
    print(f"    POST /api/config       - 保存配置")
    print(f"    GET  /api/metrics      - 验证指标")
    print(f"    GET  /api/health       - 健康检查")
    print(f"{'=' * 60}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[服务器] 已停止")
        server.server_close()


if __name__ == "__main__":
    main()
