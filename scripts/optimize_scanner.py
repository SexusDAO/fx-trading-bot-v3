#!/usr/bin/env python3
"""scanner.py 优化脚本"""
import re

path = "/root/.openclaw/workspace/scanner.py"
content = open(path, "r", encoding="utf-8").read()

# 优化 1: Coinglass 缓存 TTL 从 15 分钟改为 30 分钟
content = content.replace(
    "_CG_CACHE_TTL = 15 * 60           # 15 分钟缓存有效期",
    "_CG_CACHE_TTL = 30 * 60           # 30 分钟缓存有效期（优化：从 15 分钟延长）"
)

# 优化 2: 注释更新
old_comment = """# 目标：10 次/min 限速下，每次扫描只对"观察区 + score>=70"标的调用，结果缓存 15 分钟
# 对同一标的 15 分钟内重复扫描直接返回缓存"""
new_comment = """# 目标：10 次/min 限速下，每次扫描只对"观察区 + score>=70"标的调用，结果缓存 30 分钟
# 对同一标的 30 分钟内重复扫描直接返回缓存"""
content = content.replace(old_comment, new_comment)

# 优化 3: 添加 K 线缓存代码（在_CG_LOCK 行之后插入）
kline_cache_code = '''

# ── K 线缓存（优化二）─────────────────────────────────────────
# 避免重复拉取 K 线，5 分钟 TTL
_KLINE_CACHE: dict[str, dict] = {}  # {sym_interval_limit: {ts, klines}}
_KLINE_TTL = 5 * 60                 # 5 分钟缓存有效期
_KLINE_LOCK = __import__("threading").Lock()


def _get_klines_cached(sym: str, interval: str, limit: int) -> list:
    """
    带缓存的 K 线拉取。
    同一标的 + 时间周期 + 数量，5 分钟内直接返回缓存，不重复调用 API。
    """
    key = f"{sym}_{interval}_{limit}"
    now = time.time()
    with _KLINE_LOCK:
        cached = _KLINE_CACHE.get(key)
        if cached and (now - cached["ts"]) < _KLINE_TTL:
            log(f"[kline_cache_hit] {sym} {interval}x{limit} TTL 剩余{int(_KLINE_TTL-(now-cached['ts']))}s")
            return cached["klines"]
    
    # 未命中：真实调用 API
    klines = api.get_klines(sym, interval, limit)
    with _KLINE_LOCK:
        _KLINE_CACHE[key] = {"ts": now, "klines": klines}
    return klines
'''

content = content.replace(
    '_CG_LOCK = __import__("threading").Lock()',
    '_CG_LOCK = __import__("threading").Lock()' + kline_cache_code
)

# 优化 4: 修改扫描范围限制
content = re.sub(
    r'SCAN_TOP_N\s*=\s*100',
    'SCAN_TOP_N = 60   # 优化：从 100 降至 60（减少 40% API 调用）',
    content
)

# 优化 5: 提高最小成交量门槛
content = re.sub(
    r'MIN_VOL_USD\s*=\s*3_000_000',
    'MIN_VOL_USD = 5_000_000   # 优化：从 300 万 U 提至 500 万 U（过滤低流动性）',
    content
)

# 优化 6: 观察区容量限制（添加新常量）
if 'WL_MAX_CANDIDATES' not in content:
    content = content.replace(
        'MAX_SHORT      = 1     # 空单上限',
        'MAX_SHORT      = 1     # 空单上限（优化：控制在 1 个，降低做空风险）\nWL_MAX_CANDIDATES = 30  # 观察区候选上限（优化：从 60 降至 30）'
    )

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("scanner.py 优化完成：")
print("  1. Coinglass 缓存 TTL: 15 分钟 → 30 分钟")
print("  2. 添加 K 线缓存（5 分钟 TTL）")
print("  3. SCAN_TOP_N: 100 → 60")
print("  4. MIN_VOL_USD: 300 万 → 500 万 U")
print("  5. WL_MAX_CANDIDATES: 30")
