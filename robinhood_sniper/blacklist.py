"""
blacklist.py — 黑名单管理

包含两类黑名单：
  1. 创建者地址黑名单（原有）：代币止损/归零时把 deployer 写入，买入前检查
  2. 扫描黑名单（新增）：对任一代币地址记录关闭原因，24h 内不再重复扫描
     触发条件：评分不达标 / 安全风控未通过 / 蜜罐确认 / 暂时流动性不足
     与创建者黑名单独立，不互相影响。
  - 两种黑名单均持久化到文件
"""
import json, time, logging
from pathlib import Path

logger = logging.getLogger("sniper.blacklist")

_BLACKLIST_FILE = Path(__file__).parent / ".creator_blacklist.json"
_SCAN_BL_FILE   = Path(__file__).parent / ".scan_blacklist.json"
_SCAN_BL_TTL    = 86400   # 24h 默认
# 按拒绝原因分级 TTL：短暂特征（刷单/卖压）快速过期，结构性问题保留更长
_SCAN_BL_TTL_BY_REASON = {
    "R2":   1800,    # 30min：刷单特征可能是早期价格发现，很快会变化
    "R2.5": 1800,    # 30min：卖压可能只是短暂出货，30min 后再看
    "R1":   3600,    # 1h：拉盘砸盘，等稳定
    "R3":   7200,    # 2h：成交量异常
    "R4":   86400,   # 24h：安全风险/税率，不会变
    "R5":   86400,   # 24h：持有人极少且DS已索引，数据可信
    "R6":   3600,    # 1h：链上持有人不足，可能还在积累
    "HIGH": 86400,   # 24h：高风险合约
}


# ── 创建者黑名单 ──────────────────────────────────────────────────────────────

def _load() -> dict:
    """加载创建者黑名单：{creator_addr: [{"symbol": ..., "reason": ..., "ts": ...}]}"""
    if _BLACKLIST_FILE.exists():
        try:
            return json.loads(_BLACKLIST_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(bl: dict):
    _BLACKLIST_FILE.write_text(json.dumps(bl, indent=2, ensure_ascii=False))


_blacklist: dict = _load()


def is_blacklisted(creator_addr: str) -> bool:
    """买入前检查：创建者是否在黑名单"""
    return creator_addr.lower() in _blacklist


def add_to_blacklist(creator_addr: str, symbol: str, reason: str):
    """归零/止损后把创建者加入黑名单"""
    addr = creator_addr.lower()
    if addr in _blacklist:
        _blacklist[addr].append({"symbol": symbol, "reason": reason, "ts": int(time.time())})
    else:
        _blacklist[addr] = [{"symbol": symbol, "reason": reason, "ts": int(time.time())}]
        logger.warning(f"[Blacklist] 🚫 新增黑名单创建者 {addr[:16]}... ← {symbol} ({reason})")
    _save(_blacklist)


def blacklist_summary() -> str:
    return f"黑名单: {len(_blacklist)} 个创建者地址"


# ── 扫描黑名单（token 地址级，24h）──────────────────────────────────────────

def _load_scan_bl() -> dict:
    """加载扫描黑名单，自动过滤 24h 过期记录"""
    if _SCAN_BL_FILE.exists():
        try:
            raw = json.loads(_SCAN_BL_FILE.read_text())
            now = time.time()
            return {k: v for k, v in raw.items() if now - v["ts"] < _SCAN_BL_TTL}
        except Exception:
            pass
    return {}


def _save_scan_bl(bl: dict):
    _SCAN_BL_FILE.write_text(json.dumps(bl, indent=2, ensure_ascii=False))


_scan_blacklist: dict = _load_scan_bl()
logger.info(f"[Blacklist] 扫描黑名单加载: {len(_scan_blacklist)} 个地址")


def _get_ttl(reason: str) -> int:
    """根据拒绝原因返回对应 TTL"""
    for key, ttl in _SCAN_BL_TTL_BY_REASON.items():
        if key in reason:
            return ttl
    return _SCAN_BL_TTL  # 默认 24h


def is_scan_blacklisted(token_addr: str) -> bool:
    """扫描前检查：token 地址是否在黑名单且未过期"""
    entry = _scan_blacklist.get(token_addr.lower())
    if not entry:
        return False
    ttl = _get_ttl(entry.get("reason", ""))
    if time.time() - entry["ts"] >= ttl:
        del _scan_blacklist[token_addr.lower()]
        logger.debug(f"[Blacklist] ⏰ {entry.get('symbol','')} 黑名单已过期({ttl//60}min)，重新允许扫描")
        return False
    return True


def add_to_scan_blacklist(token_addr: str, symbol: str, reason: str):
    """把 token 地址加入扫描黑名单，按原因分级 TTL"""
    addr = token_addr.lower()
    ttl = _get_ttl(reason)
    _scan_blacklist[addr] = {"symbol": symbol, "reason": reason, "ts": int(time.time()), "ttl": ttl}
    _save_scan_bl(_scan_blacklist)
    logger.info(f"[Blacklist] 🔕 扫描黑名单 {symbol} {addr[:16]}... ← {reason} (过期:{ttl//60}min)")
