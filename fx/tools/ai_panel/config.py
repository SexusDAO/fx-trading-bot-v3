"""多模型专家面板：配置。

所有“固定不变”的内容集中在这里：API 地址、模型列表、系统提示、目录。改这里即全局生效。
密钥只从环境变量 PANEL_API_KEY 读取，不写进代码、不写进结果文件。
"""
import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent

PANEL_BASE_URL = os.environ.get("PANEL_BASE_URL", "https://api.novaapi.ai/v1")


def get_api_key() -> str:
    """每次调用时读取环境变量，缺失时报错（不提供默认值）。"""
    key = os.environ.get("PANEL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置环境变量 PANEL_API_KEY")
    return key


# 每个模型：name / max_tokens / token_param（max_tokens 或 max_completion_tokens；缺省则按名字自动判断）。
# 推理型模型（o 系列、glm-5.x）会先消耗大量思考 token，max_tokens 要给足，否则会返回空回答且照常计费。
PANEL_MODELS = [
    {"name": "claude-opus-5",   "max_tokens": 3000},
    {"name": "gpt-6.1-sol",     "max_tokens": 3000, "token_param": "max_completion_tokens"},
    {"name": "o4-mini",         "max_tokens": 4000, "token_param": "max_completion_tokens"},
    {"name": "glm-5.2",         "max_tokens": 4000},
    {"name": "claude-sonnet-5", "max_tokens": 3000},
]

# 汇总分析使用的模型（analyzer 会调用一次）
PANEL_ANALYZER_MODEL = {"name": "claude-sonnet-5", "max_tokens": 3000}

PANEL_SYSTEM_PROMPT = (
    "你是一名资深量化策略顾问，专注于加密永续合约衍生品市场。\n"
    "核心约束：交易框架以「清算图」为核心，入场价、止损、止盈的锚定逻辑不能被替换。"
    "你的任务是回答参数设计问题：\n"
    "1. 给推理逻辑，不是单纯给数字；\n"
    "2. 明确说明最不确定的地方；\n"
    "3. 特别考虑加密市场特殊性（24H 不间断、清算级联、资金费率）；\n"
    "4. 保持独立判断，不要迎合预期答案。"
)

PANEL_RESULTS_DIR = Path(os.environ.get("PANEL_RESULTS_DIR", _HERE / "results"))
PANEL_QUESTIONS_DIR = Path(os.environ.get("PANEL_QUESTIONS_DIR", _HERE / "questions"))

PANEL_REQUEST_TIMEOUT = 120   # 单次请求秒数
PANEL_MAX_RETRIES = 2         # 失败最多重试次数
PANEL_RETRY_DELAY = 3         # 重试间隔秒数
