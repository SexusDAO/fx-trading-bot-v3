"""
shared/hot_config.py — 配置热重载

· 后台线程每 30 秒检查 fx/config_tunable.py 的 mtime，变化时在独立命名空间执行该文件；
· 只接受大写、且 config.py 里已有的参数，写回 config 模块属性
  （judges / trigger_detector 调用时现读 config，因此直接生效）；
· 执行失败保留旧值并记 WARNING；从文件删掉的参数恢复为 config.py 原值。

接口：get(key, default)、start()、stop()、reload_now()
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger("fx.hot_config")

CHECK_INTERVAL = 30.0
_PATH = Path(__file__).resolve().parent.parent / "config_tunable.py"

_lock = threading.Lock()
_orig: dict = {}        # 参数 → config.py 原值（首次覆盖前记录）
_applied: set = set()   # 当前由 config_tunable 覆盖的参数
_last_mtime = None
_stop = threading.Event()
_thread = None


def _cfg():
    import config
    return config


def get(key: str, default=None):
    """读当前生效值（热更新后的 config 属性）。"""
    return getattr(_cfg(), key, default)


def reload_now(path=None) -> bool:
    """立即加载一次。成功返回 True；失败保留旧值返回 False。"""
    global _applied
    p = Path(path) if path else _PATH
    cfg = _cfg()
    if not p.exists():
        with _lock:
            for k in list(_applied):
                setattr(cfg, k, _orig[k])
            _applied = set()
        return False
    ns: dict = {}
    try:
        code = p.read_text(encoding="utf-8")
        exec(compile(code, str(p), "exec"), ns)
    except Exception as e:
        logger.warning(f"[hot_config] {p.name} 加载失败，保留旧值: {type(e).__name__}: {e}")
        return False
    new = {k: v for k, v in ns.items() if k.isupper() and not k.startswith("_")}
    for k in [k for k in new if k not in _orig and not hasattr(cfg, k)]:
        logger.warning(f"[hot_config] 忽略未知参数 {k}（config.py 中不存在）")
        new.pop(k)
    changed, restored = [], []
    with _lock:
        for k, v in new.items():
            if k not in _orig:
                _orig[k] = getattr(cfg, k)
            if getattr(cfg, k) != v:
                changed.append(f"{k}: {getattr(cfg, k)!r}→{v!r}")
            setattr(cfg, k, v)
        for k in list(_applied - set(new)):
            setattr(cfg, k, _orig[k])
            restored.append(k)
        _applied = set(new)
    if changed:
        logger.info(f"[hot_config] 已更新 {len(changed)} 项: " + "; ".join(changed[:20]))
    if restored:
        logger.info(f"[hot_config] 已恢复 config.py 原值: {', '.join(restored)}")
    return True


def check_once(path=None) -> bool:
    """mtime 变化（含文件出现/消失）才重载；返回是否执行了重载。"""
    global _last_mtime
    p = Path(path) if path else _PATH
    try:
        m = p.stat().st_mtime_ns if p.exists() else None
    except OSError:
        m = None
    if m == _last_mtime:
        return False
    _last_mtime = m
    reload_now(p)
    return True


def _loop(interval: float, path) -> None:
    while not _stop.wait(interval):
        try:
            check_once(path)
        except Exception as e:
            logger.warning(f"[hot_config] 检查异常: {e}")


def start(interval: float = CHECK_INTERVAL, path=None) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    check_once(path)
    _thread = threading.Thread(target=_loop, args=(interval, path), name="hot-config", daemon=True)
    _thread.start()
    logger.info(f"[hot_config] 已启动，每 {interval:.0f}s 检查 {(Path(path) if path else _PATH).name}")


def stop(timeout: float = 2.0) -> None:
    global _thread
    _stop.set()
    t = _thread
    if t is not None and t is not threading.current_thread():
        t.join(timeout=timeout)
    _thread = None


def _reset_for_tests() -> None:
    """测试用：恢复全部被覆盖参数并清空状态。"""
    global _applied, _last_mtime
    stop()
    cfg = _cfg()
    with _lock:
        for k in list(_applied):
            setattr(cfg, k, _orig[k])
        _applied = set()
        _orig.clear()
    _last_mtime = None
