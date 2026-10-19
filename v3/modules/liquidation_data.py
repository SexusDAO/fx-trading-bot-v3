"""
清算数据获取模块 v1.0

从 Coinglass API 获取实时清算热图数据
支持：
- 多头/空头清算位
- 清算量级
- 多币种批量获取
"""

import logging
import requests
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LiquidationZone:
    """清算区域"""
    price: float
    liquidation_amount: float  # 清算量（USD）
    side: str  # "long" 或 "short"
    symbol: str


@dataclass
class LiquidationData:
    """币种清算数据聚合"""
    symbol: str
    long_liquidations: List[LiquidationZone]  # 多头清算位（价格下方）
    short_liquidations: List[LiquidationZone]  # 空头清算位（价格上方）
    total_long_amount: float
    total_short_amount: float
    timestamp: float


class LiquidationFetcher:
    """清算数据获取器"""

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化清算数据获取器

        Args:
            api_key: Coinglass API Key（可选，部分接口需要）
        """
        self.api_key = api_key
        self.base_url = "https://open-api.coinglass.com/api"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BinanceAI-Pro/3.2",
            "Accept": "application/json"
        })
        if api_key:
            self.session.headers.update({"coinglassSecret": api_key})

    def fetch_liquidation_heatmap(
        self,
        symbol: str,
        price_range_pct: float = 0.10
    ) -> Optional[LiquidationData]:
        """
        获取单个币种的清算热图数据

        Args:
            symbol: 币种符号（如 "BTCUSDT"）
            price_range_pct: 价格范围（默认当前价±10%）

        Returns:
            LiquidationData 或 None（获取失败）
        """
        try:
            # 方案 1：Coinglass 清算热图 API
            # 注意：实际 API 路径需要根据 Coinglass 文档调整
            # 这里使用模拟结构，实际部署时需要替换为真实 API

            # 真实 API 示例（需要根据实际文档调整）：
            # GET /api/pro/liquidation/symbol?symbol=BTCUSDT
            url = f"{self.base_url}/pro/liquidation/symbol"
            params = {
                "symbol": symbol.replace("USDT", ""),  # Coinglass 使用 BTC 而非 BTCUSDT
                "limit": 50  # 返回最近的 50 个清算位
            }

            response = self.session.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                logger.warning(f"Coinglass API 返回错误：{data.get('msg', 'unknown')}")
                return None

            return self._parse_coinglass_response(data, symbol)

        except requests.exceptions.RequestException as e:
            logger.warning(f"获取清算数据失败（Coinglass）: {e}")
            return self._fallback_liquidation_data(symbol)

        except Exception as e:
            logger.error(f"清算数据获取异常：{e}")
            return self._fallback_liquidation_data(symbol)

    def _parse_coinglass_response(
        self,
        data: Dict[str, Any],
        symbol: str
    ) -> Optional[LiquidationData]:
        """解析 Coinglass API 响应"""
        try:
            # 注意：以下是示例结构，实际需要根据 Coinglass API 文档调整
            liquidation_data = data.get("data", {})
            long_liq = liquidation_data.get("long_liquidation", [])
            short_liq = liquidation_data.get("short_liquidation", [])

            long_zones = [
                LiquidationZone(
                    price=float(item["price"]),
                    liquidation_amount=float(item["amount"]),
                    side="long",
                    symbol=symbol
                )
                for item in long_liq
                if item.get("price") and item.get("amount")
            ]

            short_zones = [
                LiquidationZone(
                    price=float(item["price"]),
                    liquidation_amount=float(item["amount"]),
                    side="short",
                    symbol=symbol
                )
                for item in short_liq
                if item.get("price") and item.get("amount")
            ]

            return LiquidationData(
                symbol=symbol,
                long_liquidations=long_zones,
                short_liquidations=short_zones,
                total_long_amount=sum(z.liquidation_amount for z in long_zones),
                total_short_amount=sum(z.liquidation_amount for z in short_zones),
                timestamp=data.get("timestamp", 0)
            )

        except Exception as e:
            logger.error(f"解析 Coinglass 响应失败：{e}")
            return None

    def _fallback_liquidation_data(self, symbol: str) -> Optional[LiquidationData]:
        """
        降级方案：当 API 失败时返回空数据
        调用方会优雅降级到结构位逻辑
        """
        logger.debug(f"使用降级方案：返回空清算数据 for {symbol}")
        return LiquidationData(
            symbol=symbol,
            long_liquidations=[],
            short_liquidations=[],
            total_long_amount=0,
            total_short_amount=0,
            timestamp=0
        )

    def fetch_batch(
        self,
        symbols: List[str],
        price_range_pct: float = 0.10
    ) -> Dict[str, LiquidationData]:
        """
        批量获取多个币种的清算数据

        Args:
            symbols: 币种列表
            price_range_pct: 价格范围

        Returns:
            {symbol: LiquidationData} 字典
        """
        results = {}
        for symbol in symbols:
            liq_data = self.fetch_liquidation_heatmap(symbol, price_range_pct)
            if liq_data:
                results[symbol] = liq_data
            else:
                # 降级：返回空数据
                results[symbol] = self._fallback_liquidation_data(symbol)
        return results


# 全局单例（可选）
_default_fetcher: Optional[LiquidationFetcher] = None


def get_liquidation_fetcher(api_key: Optional[str] = None) -> LiquidationFetcher:
    """获取全局清算数据获取器单例"""
    global _default_fetcher
    if _default_fetcher is None:
        _default_fetcher = LiquidationFetcher(api_key)
    return _default_fetcher


def fetch_liquidation_for_symbol(
    symbol: str,
    api_key: Optional[str] = None
) -> Optional[LiquidationData]:
    """便捷函数：获取单个币种清算数据"""
    fetcher = get_liquidation_fetcher(api_key)
    return fetcher.fetch_liquidation_heatmap(symbol)
