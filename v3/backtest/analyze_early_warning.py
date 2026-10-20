#!/usr/bin/env python3
"""
分析任务 2：早期预警信号回溯

分析 PEPE 7 月每一次实际行情启动前的 48 小时：
- 启动前 24 小时 OI 增速 vs baseline
- 启动前 12 小时成交量 vs baseline
- 启动前 6 小时 ATR vs baseline
- 资金费率在启动前的变化方向

看这些指标能不能在启动前 12~24 小时给出预警
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))


def fetch_klines(symbol: str, start_date: str, end_date: str, interval: str = "1h") -> list:
    """获取历史 K 线"""
    print(f"📊 获取 {symbol} 历史 K 线（{start_date} ~ {end_date}）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    days = (end - start).days + 1
    
    params = {"symbol": symbol, "interval": interval, "limit": days * 24}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            klines.append({
                "time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5])
            })
        
        print(f"✅ 获取成功：{len(klines)} 根 K 线")
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def fetch_funding_rate(symbol: str, start_date: str, end_date: str) -> list:
    """获取资金费率历史"""
    print(f"📊 获取 {symbol} 资金费率（{start_date} ~ {end_date}）...")
    
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    
    try:
        # Binance 资金费率 API 只返回当前，历史需要换端点
        # 这里用简化版：返回当前费率作为示例
        params = {"symbol": symbol}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # 模拟 7 月数据（实际需要从其他数据源获取）
        # 这里返回空列表，说明数据限制
        print(f"⚠️ 历史资金费率数据需要从其他数据源获取")
        return []
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def detect_breakout_points(klines: list, lookback: int = 24, threshold: float = 0.05) -> list:
    """
    检测行情启动点
    
    定义：价格在 lookback 小时内涨幅/跌幅超过 threshold
    
    Returns:
        [(启动时间索引，启动方向，启动幅度), ...]
    """
    breakouts = []
    
    for i in range(lookback, len(klines)):
        window = klines[i-lookback:i]
        start_price = window[0]["open"]
        end_price = window[-1]["close"]
        
        change_pct = (end_price - start_price) / start_price
        
        if abs(change_pct) >= threshold:
            direction = "LONG" if change_pct > 0 else "SHORT"
            breakouts.append((i, direction, change_pct))
    
    return breakouts


def analyze_pre_breakout_signals(klines: list, breakout_index: int, direction: str) -> dict:
    """
    分析启动前的预警信号
    
    Args:
        klines: K 线数据
        breakout_index: 启动点索引
        direction: 启动方向
    
    Returns:
        {
            "breakout_time": ...,
            "direction": ...,
            "pre_24h": {...},
            "pre_12h": {...},
            "pre_6h": {...}
        }
    """
    result = {
        "breakout_time": klines[breakout_index]["time"],
        "breakout_price": klines[breakout_index]["close"],
        "direction": direction,
        "pre_24h": {},
        "pre_12h": {},
        "pre_6h": {},
    }
    
    # 计算 baseline（启动前 48-24 小时）
    baseline_start = max(0, breakout_index - 72)
    baseline_end = max(0, breakout_index - 48)
    baseline_window = klines[baseline_start:baseline_end] if baseline_end > baseline_start else []
    
    if len(baseline_window) < 10:
        return result
    
    # Baseline 统计
    baseline_vol = sum(k["volume"] for k in baseline_window) / len(baseline_window)
    baseline_closes = [k["close"] for k in baseline_window]
    baseline_atr = calc_atr(baseline_window[-20:]) if len(baseline_window) >= 20 else 0
    
    # ── 启动前 24 小时（-48h ~ -24h）────────────────────────────────
    pre_24h_start = max(0, breakout_index - 48)
    pre_24h_end = max(0, breakout_index - 24)
    pre_24h_window = klines[pre_24h_start:pre_24h_end]
    
    if len(pre_24h_window) > 0:
        pre_24h_vol = sum(k["volume"] for k in pre_24h_window) / len(pre_24h_window)
        pre_24h_closes = [k["close"] for k in pre_24h_window]
        
        # 成交量变化
        vol_ratio = pre_24h_vol / baseline_vol if baseline_vol > 0 else 1.0
        
        # 价格变化（OI 的代理指标：用成交量和价格变化推断）
        price_change = (pre_24h_closes[-1] - pre_24h_closes[0]) / pre_24h_closes[0] if pre_24h_closes[0] > 0 else 0
        
        result["pre_24h"] = {
            "volume_avg": round(pre_24h_vol, 2),
            "volume_vs_baseline": round(vol_ratio, 2),
            "price_change_pct": round(price_change * 100, 2),
            "signal": "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.8 else "持平",
        }
    
    # ── 启动前 12 小时（-24h ~ -12h）────────────────────────────────
    pre_12h_start = max(0, breakout_index - 24)
    pre_12h_end = max(0, breakout_index - 12)
    pre_12h_window = klines[pre_12h_start:pre_12h_end]
    
    if len(pre_12h_window) > 0:
        pre_12h_vol = sum(k["volume"] for k in pre_12h_window) / len(pre_12h_window)
        pre_12h_closes = [k["close"] for k in pre_12h_window]
        
        vol_ratio = pre_12h_vol / baseline_vol if baseline_vol > 0 else 1.0
        price_change = (pre_12h_closes[-1] - pre_12h_closes[0]) / pre_12h_closes[0] if pre_12h_closes[0] > 0 else 0
        
        # ATR 变化
        pre_12h_atr = calc_atr(pre_12h_window[-20:]) if len(pre_12h_window) >= 20 else 0
        atr_ratio = pre_12h_atr / baseline_atr if baseline_atr > 0 else 1.0
        
        result["pre_12h"] = {
            "volume_avg": round(pre_12h_vol, 2),
            "volume_vs_baseline": round(vol_ratio, 2),
            "price_change_pct": round(price_change * 100, 2),
            "atr_ratio": round(atr_ratio, 2),
            "signal": "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.8 else "持平",
        }
    
    # ── 启动前 6 小时（-12h ~ -6h）────────────────────────────────
    pre_6h_start = max(0, breakout_index - 12)
    pre_6h_end = max(0, breakout_index - 6)
    pre_6h_window = klines[pre_6h_start:pre_6h_end]
    
    if len(pre_6h_window) > 0:
        pre_6h_vol = sum(k["volume"] for k in pre_6h_window) / len(pre_6h_window)
        pre_6h_closes = [k["close"] for k in pre_6h_window]
        
        vol_ratio = pre_6h_vol / baseline_vol if baseline_vol > 0 else 1.0
        price_change = (pre_6h_closes[-1] - pre_6h_closes[0]) / pre_6h_closes[0] if pre_6h_closes[0] > 0 else 0
        
        # ATR 变化
        pre_6h_atr = calc_atr(pre_6h_window[-20:]) if len(pre_6h_window) >= 20 else 0
        atr_ratio = pre_6h_atr / baseline_atr if baseline_atr > 0 else 1.0
        
        result["pre_6h"] = {
            "volume_avg": round(pre_6h_vol, 2),
            "volume_vs_baseline": round(vol_ratio, 2),
            "price_change_pct": round(price_change * 100, 2),
            "atr_ratio": round(atr_ratio, 2),
            "signal": "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.8 else "持平",
        }
    
    return result


def calc_atr(klines: list, period: int = 14) -> float:
    """计算 ATR"""
    if len(klines) < 2:
        return 0.0
    
    trs = []
    for i in range(1, min(len(klines), period + 1)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i-1]["close"]
        
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    
    return sum(trs) / len(trs) if trs else 0.0


def main():
    """主函数"""
    print("="*70)
    print("🧪 分析任务 2：早期预警信号回溯")
    print("="*70)
    
    # PEPE 7 月数据
    pepe_klines = fetch_klines("1000PEPEUSDT", "2026-07-01", "2026-07-31", "1h")
    
    if not pepe_klines:
        print("❌ 无法获取 PEPE 数据，退出分析")
        return
    
    # 检测启动点（涨跌幅≥5%）
    breakouts = detect_breakout_points(pepe_klines, lookback=24, threshold=0.05)
    print(f"\n📍 检测到 {len(breakouts)} 个启动点（24h 涨跌幅≥5%）")
    
    # 分析每个启动点前的预警信号
    pre_breakout_analysis = []
    for i, (breakout_idx, direction, change_pct) in enumerate(breakouts):
        print(f"\n分析启动点{i+1}: {direction} +{change_pct*100:.1f}% @ {datetime.fromtimestamp(pepe_klines[breakout_idx]['time']/1000)}")
        
        analysis = analyze_pre_breakout_signals(pepe_klines, breakout_idx, direction)
        pre_breakout_analysis.append(analysis)
        
        print(f"  前 24h: 成交量={analysis['pre_24h'].get('volume_vs_baseline', 'N/A')}x 信号={analysis['pre_24h'].get('signal', 'N/A')}")
        print(f"  前 12h: 成交量={analysis['pre_12h'].get('volume_vs_baseline', 'N/A')}x ATR={analysis['pre_12h'].get('atr_ratio', 'N/A')}x")
        print(f"  前 6h:  成交量={analysis['pre_6h'].get('volume_vs_baseline', 'N/A')}x ATR={analysis['pre_6h'].get('atr_ratio', 'N/A')}x")
    
    # 汇总统计
    print(f"\n{'='*70}")
    print("📊 预警信号汇总统计")
    print(f"{'='*70}")
    
    if len(pre_breakout_analysis) == 0:
        print("⚠️ 无启动点可分析")
        return
    
    # 统计各时间段出现"放量"信号的次数
    pre_24h_volume_up = sum(1 for a in pre_breakout_analysis if a['pre_24h'].get('signal') == '放量')
    pre_12h_volume_up = sum(1 for a in pre_breakout_analysis if a['pre_12h'].get('signal') == '放量')
    pre_6h_volume_up = sum(1 for a in pre_breakout_analysis if a['pre_6h'].get('signal') == '放量')
    
    total = len(pre_breakout_analysis)
    
    print(f"\n【成交量预警信号统计】")
    print(f"  启动前 24h 放量：{pre_24h_volume_up}/{total} ({pre_24h_volume_up/total*100:.0f}%)")
    print(f"  启动前 12h 放量：{pre_12h_volume_up}/{total} ({pre_12h_volume_up/total*100:.0f}%)")
    print(f"  启动前 6h 放量：  {pre_6h_volume_up}/{total} ({pre_6h_volume_up/total*100:.0f}%)")
    
    # 统计 ATR 变化
    pre_12h_atr_up = sum(1 for a in pre_breakout_analysis if a['pre_12h'].get('atr_ratio', 1.0) > 1.2)
    pre_6h_atr_up = sum(1 for a in pre_breakout_analysis if a['pre_6h'].get('atr_ratio', 1.0) > 1.2)
    
    print(f"\n【ATR 预警信号统计】")
    print(f"  启动前 12h ATR 放大 (>1.2x): {pre_12h_atr_up}/{total} ({pre_12h_atr_up/total*100:.0f}%)")
    print(f"  启动前 6h ATR 放大 (>1.2x):  {pre_6h_atr_up}/{total} ({pre_6h_atr_up/total*100:.0f}%)")
    
    # 导出 JSON
    output_file = Path(__file__).parent / "pepe_early_warning_analysis.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(pre_breakout_analysis, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 详细数据已导出：{output_file}")
    
    # 结论
    print(f"\n{'='*70}")
    print("💡 初步结论")
    print(f"{'='*70}")
    
    if pre_12h_volume_up / total > 0.6:
        print(f"✅ 启动前 12h 放量信号有效（命中率{pre_12h_volume_up/total*100:.0f}%）")
        print(f"   → 建议集成'放量_价格未动'预警逻辑")
    else:
        print(f"⚠️ 启动前 12h 放量信号命中率低（{pre_12h_volume_up/total*100:.0f}%）")
        print(f"   → 需要结合其他指标（OI、资金费率）")
    
    if pre_6h_atr_up / total > 0.6:
        print(f"✅ 启动前 6h ATR 放大信号有效（命中率{pre_6h_atr_up/total*100:.0f}%）")
        print(f"   → 建议集成'ATR 放大'作为启动确认")
    else:
        print(f"⚠️ 启动前 6h ATR 放大信号命中率低（{pre_6h_atr_up/total*100:.0f}%）")


if __name__ == "__main__":
    main()
