import os
"""
arb/config.py — 套利机器人专用配置
严禁在此文件中 import fx/ 下的任何模块
"""

# ══════════════════════════════════════════
# 安全开关 — 上线前必须检查
# ══════════════════════════════════════════
DRY_RUN = True          # True=只打印预览，不真实下单；改为False需用户CONFIRM

# ══════════════════════════════════════════
# 资金配置
# ══════════════════════════════════════════
ARB_CAPITAL_USDT   = 500.0     # 套利总资金（USDT）
ARB_SINGLE_MAX     = 250.0     # 单标的最大仓位（USDT）
ARB_MIN_RATE_8H    = 0.05      # 最小资金费率门槛（每8h %），低于此不开仓
ARB_MIN_VOLUME_M   = 50.0      # 最小日交易量（百万U），流动性过滤

# ══════════════════════════════════════════
# 交易所配置（代理统一用 PROXY_URL）
# ══════════════════════════════════════════
PROXY_URL = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES   = {"http": PROXY_URL, "https": PROXY_URL}

# 币安（主力，现货 + 合约同所对冲）
BINANCE_API_KEY    = "REDACTED_API_KEY"
BINANCE_API_SECRET = "REDACTED_API_SECRET"

# OKX（需先在App添加提现白名单）
OKX_API_KEY = os.environ.get("ARB_CONFIG_OKX_API_KEY", "")
OKX_API_SECRET = os.environ.get("ARB_CONFIG_OKX_API_SECRET", "")
OKX_PASSPHRASE = "Zxcvzx12."

# Bybit
BYBIT_API_KEY = os.environ.get("ARB_CONFIG_BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("ARB_CONFIG_BYBIT_API_SECRET", "")

# Bitget (UTA)
BITGET_API_KEY = os.environ.get("ARB_CONFIG_BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("ARB_CONFIG_BITGET_API_SECRET", "")
BITGET_PASSPHRASE = "Zxcvzx12"

# ══════════════════════════════════════════
# 禁止调用的接口（agent 检查用）
# ══════════════════════════════════════════
FORBIDDEN_ENDPOINTS = [
    "/fapi/v1/order",      # 币安合约下单 — 套利不走这里
    "/fapi/v1/batchOrders",
]
