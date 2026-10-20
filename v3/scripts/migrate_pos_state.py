"""
scripts/migrate_pos_state.py — 旧 pos_state.json → v3 格式迁移

支持：
  1. 读取旧 workspace/config/pos_state.json
  2. 转换为 v3 pos_state 字段格式
  3. 写入 v3/config/pos_state.json（通过 pos_state_store）
  4. 打印迁移报告，不破坏旧文件

运行：python3 scripts/migrate_pos_state.py [--dry-run]
"""

import sys, json, argparse
from pathlib import Path

V3_ROOT  = Path(__file__).parent.parent
OLD_FILE = V3_ROOT.parent / "config" / "pos_state.json"  # workspace/config/pos_state.json
sys.path.insert(0, str(V3_ROOT))

from modules.pos_state_store import write as ps_write, clear_all, get_all


def _load_old() -> dict:
    try:
        with open(OLD_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 无法读取旧 pos_state: {e}")
        return {}


def _convert(symbol: str, old: dict) -> dict | None:
    """
    旧格式 → v3 格式。
    旧格式字段名可能不统一，做宽松映射。
    """
    # 取 positionAmt 判断方向
    amt = float(old.get("positionAmt", old.get("position_amt", 0)))
    if amt == 0:
        return None  # 空仓，跳过

    direction = "LONG" if amt > 0 else "SHORT"

    entry = float(old.get("entryPrice", old.get("entry_price",
                   old.get("avg_price", 0))))
    notional = abs(amt) * entry

    return {
        "entry_price":     entry,
        "avg_price":       float(old.get("avg_price", entry)),
        "notional":        round(notional, 2),
        "total_notional":  round(notional, 2),
        "first_notional":  round(notional, 2),
        "sl_price":        float(old.get("sl_price", old.get("stop_price", 0))),
        "sl_order_id":     str(old.get("sl_order_id", old.get("stop_order_id", ""))),
        "tp1_price":       float(old.get("tp1_price", old.get("tp_price", 0))),
        "tp1_order_id":    str(old.get("tp1_order_id", "")),
        "tp2_price":       float(old.get("tp2_price", 0)),
        "tp2_order_id":    str(old.get("tp2_order_id", "")),
        "signal_type":     old.get("signal_type", "trend"),
        "score_at_open":   int(old.get("score", old.get("score_at_open", 0))),
        "score_details":   old.get("score_details", {}),
        "entry_mode":      old.get("entry_mode", "LIMIT"),
        "flatten_count":   int(old.get("add_count", old.get("flatten_count", 0))),
        "entry_time":      old.get("entry_time", old.get("open_time", "")),
        "_migrated_from":  "v2",
    }


def run(dry_run: bool = False) -> dict:
    old_state = _load_old()
    if not old_state:
        print("旧 pos_state.json 为空或不存在，无需迁移。")
        return {"migrated": 0, "skipped": 0}

    print(f"\n旧 pos_state.json 共 {len(old_state)} 条记录\n")

    migrated = 0
    skipped  = 0
    report   = []

    for key, old in old_state.items():
        # key 可能是 "BTCUSDT_LONG" 或 "BTCUSDT"
        if "_" in key and key.endswith(("_LONG", "_SHORT")):
            parts = key.rsplit("_", 1)
            symbol, direction_hint = parts[0], parts[1]
        else:
            symbol = key
            direction_hint = None

        new = _convert(symbol, old)
        if new is None:
            print(f"  ⏭  {key}: 空仓，跳过")
            skipped += 1
            continue

        direction = direction_hint or new.get("direction", "LONG")
        if not dry_run:
            ps_write(symbol, direction, new)

        migrated += 1
        report.append({
            "key":       f"{symbol}_{direction}",
            "entry":     new["entry_price"],
            "notional":  new["notional"],
            "sl":        new["sl_price"],
        })
        mark = "[dry-run] " if dry_run else ""
        print(f"  {mark}✅ {symbol} {direction}: entry={new['entry_price']} "
              f"notional={new['notional']:.0f}U sl={new['sl_price']}")

    print(f"\n迁移完成: {migrated} 条成功，{skipped} 条跳过")
    if dry_run:
        print("（dry-run 模式，未实际写入）")
    else:
        print(f"v3 pos_state 现有 {len(get_all())} 条持仓")

    return {"migrated": migrated, "skipped": skipped, "report": report}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
