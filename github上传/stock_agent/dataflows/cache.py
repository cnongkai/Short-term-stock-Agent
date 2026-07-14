"""
TTL 缓存装饰器 (对应 PRD 8.7: 工具结果缓存 1 小时)

V2 升级: 内存层 + SQLite 磁盘持久化双层缓存。
- 内存层: 热路径, RLock 保护并发读写 (分析层四维并行)
- SQLite 层: WAL 模式, 后台 daemon 线程定期批量 flush 脏数据
- 冷启动恢复: 启动时从磁盘加载未过期项到内存
- 向后兼容: @cached(ttl=...) 签名不变, wrapper.cache_clear() 仍可用
"""
import os
import time
import pickle
import sqlite3
import threading
import functools
from typing import Any, Callable, Optional

from loguru import logger


class CacheStore:
    """双层缓存: 内存(热) + SQLite(持久化)

    线程安全: 内存层用 RLock, SQLite 用 WAL 模式 + check_same_thread=False。
    批量写入: 脏队列 + daemon 线程定期 flush, 避免每次写都落盘。
    """

    _instance: Optional["CacheStore"] = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path: str = "", flush_interval: int = 60, enable_disk: bool = True):
        self._enable_disk = enable_disk
        self._flush_interval = flush_interval
        self._mem: dict[str, tuple[float, Any]] = {}  # {key: (expire_at, value)}
        self._dirty: set[str] = set()  # 待落盘 key
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._conn: Optional[sqlite3.Connection] = None
        self._flusher: Optional[threading.Thread] = None

        if self._enable_disk:
            if not db_path:
                # 默认路径: results_dir/cache.sqlite
                results_dir = os.getenv("RESULTS_DIR", "./results")
                db_path = os.path.join(results_dir, "cache.sqlite")
            self._db_path = db_path
            self._init_sqlite()
            self._load_from_disk()
            self._start_flusher()
            logger.info(f"[缓存] SQLite 持久化已启用: {db_path}")
        else:
            self._db_path = ""
            logger.info("[缓存] 磁盘持久化未启用, 仅内存缓存")

    def _init_sqlite(self):
        """初始化 SQLite 连接 (WAL 模式, 支持并发读写)"""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expire_at REAL, value BLOB)"
        )
        self._conn.commit()

    def _load_from_disk(self):
        """冷启动: 从磁盘加载未过期项到内存"""
        try:
            cur = self._conn.execute("SELECT key, expire_at, value FROM cache")
            now = time.time()
            loaded = 0
            for key, expire_at, value_blob in cur.fetchall():
                if expire_at > now:
                    try:
                        value = pickle.loads(value_blob)
                        with self._lock:
                            self._mem[key] = (expire_at, value)
                        loaded += 1
                    except Exception:
                        continue
            if loaded:
                logger.info(f"[缓存] 从磁盘恢复 {loaded} 项")
        except Exception as e:
            logger.warning(f"[缓存] 磁盘加载失败: {e}")

    def _start_flusher(self):
        """启动后台 daemon 线程, 定期批量落盘脏数据"""
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True, name="cache-flusher")
        self._flusher.start()

    def _flush_loop(self):
        """后台 flush 循环"""
        while not self._stop_event.wait(self._flush_interval):
            try:
                self._flush()
            except Exception as e:
                logger.debug(f"[缓存] flush 异常: {e}")

    def _flush(self):
        """将脏队列批量写入 SQLite"""
        with self._lock:
            if not self._dirty:
                return
            dirty_keys = list(self._dirty)
            self._dirty.clear()
            items = []
            now = time.time()
            for key in dirty_keys:
                entry = self._mem.get(key)
                if entry is None:
                    continue
                expire_at, value = entry
                if expire_at <= now:
                    continue  # 已过期, 不落盘
                try:
                    value_blob = pickle.dumps(value)
                    items.append((key, expire_at, value_blob))
                except Exception:
                    continue

        if not items:
            return
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO cache (key, expire_at, value) VALUES (?, ?, ?)", items
            )
            # 顺带清理过期项
            self._conn.execute("DELETE FROM cache WHERE expire_at < ?", (now,))
            self._conn.commit()
            logger.debug(f"[缓存] flush {len(items)} 项到磁盘")
        except Exception as e:
            logger.warning(f"[缓存] flush 写盘失败: {e}")

    def get(self, key: str) -> Optional[Any]:
        """读缓存: 内存优先 → SQLite 回源 → 回填内存"""
        now = time.time()
        with self._lock:
            entry = self._mem.get(key)
            if entry is not None:
                expire_at, value = entry
                if expire_at > now:
                    return value
                else:
                    del self._mem[key]  # 惰性过期

        # 内存未命中, 查 SQLite
        if not self._enable_disk or self._conn is None:
            return None
        try:
            cur = self._conn.execute(
                "SELECT expire_at, value FROM cache WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            expire_at, value_blob = row
            if expire_at <= now:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
            value = pickle.loads(value_blob)
            with self._lock:
                self._mem[key] = (expire_at, value)
            return value
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: int):
        """写缓存: 写内存 + 加入脏队列 (不立即落盘)"""
        expire_at = time.time() + ttl
        with self._lock:
            self._mem[key] = (expire_at, value)
            self._dirty.add(key)

    def cache_clear(self, prefix: Optional[str] = None):
        """清空缓存 (可按函数名前缀过滤)"""
        with self._lock:
            if prefix is None:
                self._mem.clear()
                self._dirty.clear()
            else:
                keys_to_del = [k for k in self._mem if k.startswith(prefix)]
                for k in keys_to_del:
                    del self._mem[k]
                self._dirty.discard(prefix)

        if self._enable_disk and self._conn is not None:
            try:
                if prefix is None:
                    self._conn.execute("DELETE FROM cache")
                else:
                    self._conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
                self._conn.commit()
            except Exception as e:
                logger.debug(f"[缓存] 清理磁盘缓存失败: {e}")

    def shutdown(self):
        """关闭: 最后 flush 一次 + 停止 daemon"""
        self._stop_event.set()
        if self._enable_disk:
            self._flush()
            if self._conn:
                self._conn.close()


def get_cache_store(config: dict = None) -> CacheStore:
    """获取 CacheStore 单例 (首次调用时从 config 初始化)"""
    if CacheStore._instance is None:
        with CacheStore._instance_lock:
            if CacheStore._instance is None:
                # 延迟导入避免循环依赖
                try:
                    from stock_agent.default_config import DEFAULT_CONFIG
                    cfg = config or DEFAULT_CONFIG
                except ImportError:
                    cfg = config or {}

                enable_disk = cfg.get("cache_enable_disk", True)
                db_path = cfg.get("cache_db_path", "")
                flush_interval = int(cfg.get("cache_flush_interval", 60))

                CacheStore._instance = CacheStore(
                    db_path=db_path,
                    flush_interval=flush_interval,
                    enable_disk=enable_disk,
                )
    return CacheStore._instance


def cached(ttl: int = 3600) -> Callable:
    """TTL 缓存装饰器 (双层: 内存 + SQLite 持久化)

    Args:
        ttl: 缓存存活时间(秒), 默认 3600 (1 小时, PRD 8.7)

    Returns:
        装饰器函数
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            store = get_cache_store()
            # 构建缓存键 (函数名 + 位置参数 + 关键字参数)
            cache_key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"

            # 检查缓存命中
            hit = store.get(cache_key)
            if hit is not None:
                logger.debug(f"[缓存命中] {func.__name__} (key={cache_key[:80]})")
                return hit

            # 缓存未命中, 执行函数
            result = func(*args, **kwargs)

            # 写入缓存 (仅缓存非空结果)
            if result:
                store.set(cache_key, result, ttl)

            return result

        # 暴露缓存清理方法 (向后兼容: 按函数名清理)
        wrapper.cache_clear = lambda: get_cache_store().cache_clear(func.__name__)
        return wrapper

    return decorator
