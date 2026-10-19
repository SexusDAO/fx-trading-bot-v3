#!/usr/bin/env python3
"""
获取 ETH 历史清算数据

数据源：Coinglass API 或 Binance 公共数据
时间范围：过去 30 天
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))


def fetch_liquidation_history(days: int = 30) -> list:
    """
    获取 ETHUSDT 历史清算数据
    
    使用 Coinglass API 获取清算热图历史数据
    返回格式：[{time, price, amount, side, symbol}, ...]
    """
    print(f"📊 获取 ETHUSDT 历史清算数据（过去 {days} 天）...")
    
    # 方案 1：Coinglass API（需要 API Key）
    # 方案 2：Binance 公共 API（有限）
    # 方案 3：使用模拟数据（降级）
    
    # 尝试 Coinglass API
    api_key = None  # 从配置读取
    if api_key:
        try:
            url = "https://open-api.coinglass.com/api/v3/risk/liquidation"
            params = {
                "symbol": "ETH",
                "startTime": int((datetime.now() - timedelta(days=days)).timestamp() * 1000),
                "endTime": int(datetime.now().timestamp() * 1000),
            }
            headers = {"coinglassSecret": api_key}
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 0:
                    print(f"✅ Coinglass API 获取成功")
                    return parse_coinglass_data(data.get("data", []))
        except Exception as e:
            print(f"⚠️ Coinglass API 失败：{e}")
    
    # 降级：生成模拟清算数据（基于价格波动）
    print("⚠️ 使用模拟清算数据（基于历史价格波动）")
    return generate_mock_liquidation_data(days)


def parse_coinglass_data(data: list) -> list:
    """解析 Coinglass API 响应"""
    liquidations = []
    for item in data:
        liquidations.append({
            "time": item.get("time", 0),
            "price": float(item.get("price", 0)),
            "amount": float(item.get("amount", 0)),
            "side": "long" if item.get("type") == "long" else "short",
            "symbol": "ETHUSDT",
        })
    return liquidations


def generate_mock_liquidation_data(days: int = 30) -> list:
    """
    生成模拟清算数据
    
    基于 ETH 历史价格波动，在关键支撑/阻力位生成清算集群
    """
    print("📈 生成模拟清算数据...")
    
    # 先获取历史 K 线
    klines = fetch_eth_klines(days)
    
    liquidations = []
    
    # 基于 K 线高低点生成清算数据
    for i, k in enumerate(klines):
        time = k[0]
        high = k[2]
        low = k[3]
        close = k[4]
        
        # 在关键位置生成清算集群
        # 多头清算（价格下方）
        if i > 20:
            recent_low = min(k[3] for k in klines[i-20:i])
            if low < recent_low * 0.99:  # 跌破近期低点
                # 生成多头清算
                liq_price = recent_low * 0.98
                liq_amount = 50000000 + (recent_low - low) * 10000000
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 2),
                    "amount": round(liq_amount, 0),
                    "side": "long",
                    "symbol": "ETHUSDT",
                })
        
        # 空头清算（价格上方）
        if i > 20:
            recent_high = max(k[2] for k in klines[i-20:i])
            if high > recent_high * 1.01:  # 突破近期高点
                # 生成空头清算
                liq_price = recent_high * 1.02
                liq_amount = 50000000 + (high - recent_high) * 10000000
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 2),
                    "amount": round(liq_amount, 0),
                    "side": "short",
                    "symbol": "ETHUSDT",
                })
    
    print(f"✅ 生成 {len(liquidations)} 条清算记录")
    return liquidations


def fetch_eth_klines(days: int = 30, interval: str = "1h") -> list:
    """获取 ETHUSDT 历史 K 线"""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": "ETHUSDT", "interval": interval, "limit": days * 24}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            klines.append([
                k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            ])
        return klines
    except Exception as e:
        print(f"❌ 获取 K 线失败：{e}")
        return []


def aggregate_liquidation_data(liquidations: list, interval_hours: int = 4) -> dict:
    """
    聚合清算数据为热图格式
    
    按价格区间和时间聚合，生成清算热图
    """
    print(f"📊 聚合清算数据（{interval_hours} 小时间隔）...")
    
    # 按时间分桶
    if not liquidations:
        return {}
    
    heatmap = {}
    
    for liq in liquidations:
        time_bucket = (liq["time"] // (interval_hours * 3600 * 1000)) * (interval_hours * 3600 * 1000)
        price_bucket = round(liq["price"] / 10) * 10  # 每$10 一个区间
        
        key = f"{time_bucket}_{price_bucket}"
        
        if key not in heatmap:
            heatmap[key] = {
                "time": time_bucket,
                "price": price_bucket,
                "long_amount": 0,
                "short_amount": 0,
            }
        
        if liq["side"] == "long":
            heatmap[key]["long_amount"] += liq["amount"]
        else:
            heatmap[key]["short_amount"] += liq["amount"]
    
    return heatmap


def save_liquidation_data(liquidations: list, output_file: str):
    """保存清算数据"""
    output_path = Path(__file__).parent / output_file
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(liquidations, f, indent=2, ensure_ascii=False)
    
    print(f"💾 清算数据已保存：{output_path}")
    return output_path


def main():
    print("=" * 70)
    print("📊 ETH 历史清算数据获取")
    print("=" * 70)
    
    # 获取清算数据
    liquidations = fetch_liquidation_history(days=30)
    
    if not liquidations:
        print("❌ 获取清算数据失败")
        return
    
    # 统计
    long_liq = [l for l in liquidations if l["side"] == "long"]
    short_liq = [l for l in liquidations if l["side"] == "short"]
    
    print(f"\n【清算数据统计】")
    print(f"  总记录数：{len(liquidations)}")
    print(f"  多头清算：{len(long_liq)} 条，总量 ${sum(l['amount'] for l in long_liq):,.0f}")
    print(f"  空头清算：{len(short_liq)} 条，总量 ${sum(l['amount'] for l in short_liq):,.0f}")
    
    if liquidations:
        min_price = min(l["price"] for l in liquidations)
        max_price = max(l["price"] for l in liquidations)
        print(f"  价格范围：${min_price:.2f} ~ ${max_price:.2f}")
    
    # 保存数据
    output_file = save_liquidation_data(liquidations, "eth_liquidation_30d.json")
    
    # 生成聚合热图
    heatmap = aggregate_liquidation_data(liquidations, interval_hours=4)
    heatmap_file = Path(__file__).parent / "eth_liquidation_heatmap.json"
    with open(heatmap_file, 'w', encoding='utf-8') as f:
        json.dump(heatmap, f, indent=2, ensure_ascii=False)
    print(f"💾 热图数据已保存：{heatmap_file}")
    
    print("\n✅ 清算数据获取完成！")


if __name__ == "__main__":
    main()
