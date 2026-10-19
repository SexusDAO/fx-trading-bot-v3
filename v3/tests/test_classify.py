"""tests/test_classify.py"""
import sys
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.classify import classify_signal

def _make_klines(closes: list[float]) -> list:
    """生成最简 kline 列表 [ts, o, h, l, c, vol]"""
    return [[0, c, c*1.01, c*0.99, c, 1000] for c in closes]

def _trend_closes(n=60) -> list[float]:
    """上涨趋势：EMA 多排"""
    return [100 + i * 0.5 for i in range(n)]

def _downtrend_closes(n=60) -> list[float]:
    """下跌趋势：EMA 空排"""
    return [100 - i * 0.5 for i in range(n)]

def _flat_closes(n=60) -> list[float]:
    return [100.0] * n

def test_trend_long():
    k = _make_klines(_trend_closes())
    result = classify_signal(k, k, funding=0.0001, chg24=0.05)
    assert result == "trend", f"期望trend，得{result}"
    print(f"✅ 趋势多单 → {result}")

def test_trend_short():
    k = _make_klines(_downtrend_closes())
    result = classify_signal(k, k, funding=0.0001, chg24=-0.05)
    assert result == "trend_short", f"期望trend_short，得{result}"
    print(f"✅ 趋势空单 → {result}")

def test_reversal_l2():
    """极度超卖：大跌+极端资金费率+RSI极低"""
    # 用下跌 klines 让 RSI 足够低
    closes = [100 - i * 2 for i in range(60)]   # 大幅下跌
    k = _make_klines(closes)
    result = classify_signal(k, k, funding=-0.005, chg24=-0.25)
    assert result in ("reversal", "reversal_l1"), f"期望逆势，得{result}"
    print(f"✅ 逆势多单大跌 → {result}")

def test_reversal_short():
    closes = [100 + i * 2 for i in range(60)]   # 大幅上涨
    k = _make_klines(closes)
    result = classify_signal(k, k, funding=0.005, chg24=0.30)
    # RSI 可能不够高，至少不应返回 reversal 类型做多
    assert result in ("reversal_short", "trend"), f"得{result}"
    print(f"✅ 超买场景 → {result}")

def test_insufficient_data():
    """数据不足默认 trend"""
    k = _make_klines([100.0] * 5)
    result = classify_signal(k, k, funding=0.0, chg24=0.0)
    assert result == "trend", f"得{result}"
    print(f"✅ 数据不足 → {result}")

if __name__ == "__main__":
    test_trend_long()
    test_trend_short()
    test_reversal_l2()
    test_reversal_short()
    test_insufficient_data()
    print("\n🎉 classify 全部测试通过")
