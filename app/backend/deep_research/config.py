"""
深度研究：環境變數、可選模型清單與各種上限。

唯一必要的環境變數是 `DEEP_SEARCH_MODEL`（前端未指定模型時的預設值）；
其餘皆有合理預設，未設定也能運作。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────
# 模型
# ─────────────────────────────────────────────────────────────
# 僅列出 Responses API 上支援 hosted tools（web_search / file_search）的模型。
# key 為送給 OpenAI 的 model id，順序即前端下拉選單的順序。
MODEL_CATALOG: Dict[str, Dict[str, str]] = {
    "gpt-5.6-luna": {
        "label": "GPT-5.6 Luna",
        "description": "預設模型；已實測支援 web search 與 file search",
    },
    "gpt-5.6-sol": {
        "label": "GPT-5.6 Sol",
        "description": "同代的另一個變體，可與 Luna 對照品質差異",
    },
    "gpt-5.6-terra": {
        "label": "GPT-5.6 Terra",
        "description": "同代的另一個變體，可與 Luna 對照品質差異",
    },
    "gpt-5": {
        "label": "GPT-5",
        "description": "上一代旗艦，長篇論述穩定，速度較慢",
    },
    "gpt-5-mini": {
        "label": "GPT-5 mini",
        "description": "上一代輕量款，適合先跑一次確認流程通不通",
    },
    "gpt-4.1": {
        "label": "GPT-4.1",
        "description": "非推理模型，回覆快、長文寫作穩定",
    },
}

_FALLBACK_MODEL = "gpt-5.6-luna"


def _catalog_from_env() -> Dict[str, Dict[str, str]]:
    """
    `DEEP_SEARCH_MODELS`（逗號分隔）可覆寫可選清單，例如：

        DEEP_SEARCH_MODELS=gpt-5.6-luna,gpt-5.6-sol

    清單外的 id 仍會被接受（沿用 id 當 label），方便試新模型時不必改程式。
    `DEEP_SEARCH_MODEL` 指定的預設值一定會被併進清單，見下方註解。
    """
    raw = os.getenv("DEEP_SEARCH_MODELS", "").strip()
    if raw:
        picked: Dict[str, Dict[str, str]] = {}
        for item in raw.split(","):
            model_id = item.strip()
            if not model_id:
                continue
            picked[model_id] = MODEL_CATALOG.get(
                model_id, {"label": model_id, "description": ""}
            )
    else:
        picked = dict(MODEL_CATALOG)

    # DEEP_SEARCH_MODEL 是運維明確指定的預設值，一定要在清單裡。
    # 少了這段，設一個清單外的模型會被 resolve_model() 悄悄換成清單第一個，
    # 看起來像設定沒生效卻沒有任何錯誤訊息 —— 最難查的那種。
    default = os.getenv("DEEP_SEARCH_MODEL", "").strip()
    if default and default not in picked:
        picked[default] = MODEL_CATALOG.get(
            default, {"label": default, "description": "由 DEEP_SEARCH_MODEL 指定"}
        )

    return picked or dict(MODEL_CATALOG)


def available_models() -> List[Dict[str, str]]:
    """前端下拉選單用的模型清單。"""
    return [
        {"id": model_id, "label": meta["label"], "description": meta["description"]}
        for model_id, meta in _catalog_from_env().items()
    ]


# 預設模型：使用者未在前端選擇時採用（本功能唯一必填的環境變數）
DEEP_SEARCH_DEFAULT_MODEL = (
    os.getenv("DEEP_SEARCH_MODEL", "").strip() or _FALLBACK_MODEL
)


def resolve_model(requested: Optional[str]) -> str:
    """
    將前端送來的 model 收斂成實際要用的 model id。

    不在清單內的一律退回預設模型 —— 這個參數直接來自瀏覽器，
    不能讓使用者任意指定字串去打 OpenAI。
    """
    catalog = _catalog_from_env()
    if requested and requested.strip() in catalog:
        return requested.strip()
    if DEEP_SEARCH_DEFAULT_MODEL in catalog:
        return DEEP_SEARCH_DEFAULT_MODEL
    return next(iter(catalog), _FALLBACK_MODEL)


def supports_reasoning_effort(model: str) -> bool:
    """
    這個模型能不能吃 `reasoning.effort`。

    Responses API 對非推理模型（gpt-4.1 系列）帶 reasoning 會直接回 400，
    因此送出前必須先判斷，否則使用者一選 gpt-4.1 整個研究就掛掉。
    """
    name = (model or "").lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ─────────────────────────────────────────────────────────────
# 上限（皆可由環境變數調整）
# ─────────────────────────────────────────────────────────────
MAX_FILES = _env_int("DEEP_SEARCH_MAX_FILES", 10)
MAX_FILE_MB = _env_int("DEEP_SEARCH_MAX_FILE_MB", 20)
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_IMAGES = _env_int("DEEP_SEARCH_MAX_IMAGES", 4)
QUERY_MAX_CHARS = _env_int("DEEP_SEARCH_QUERY_MAX_CHARS", 4000)

# 試算表轉成 Markdown 後直接塞進 prompt 的字數上限（超過即截斷）
SPREADSHEET_MAX_CHARS = _env_int("DEEP_SEARCH_SPREADSHEET_MAX_CHARS", 12000)
SPREADSHEET_TOTAL_MAX_CHARS = _env_int(
    "DEEP_SEARCH_SPREADSHEET_TOTAL_MAX_CHARS", 30000
)

# Agent 迴圈上限（一輪 = 一次模型呼叫；web search 會用掉數輪）
RESEARCH_MAX_TURNS = _env_int("DEEP_SEARCH_MAX_TURNS", 24)

# session 存活時間；逾時後連同研究結果與已產生的檔案一起清掉
SESSION_TTL_MINUTES = _env_int("DEEP_SEARCH_SESSION_TTL_MINUTES", 120)
# 每位使用者最多同時保留幾個 session（超過時淘汰最舊的）
MAX_SESSIONS_PER_USER = _env_int("DEEP_SEARCH_MAX_SESSIONS_PER_USER", 5)

# OpenAI vector store 的保險絲：即使後端清理失敗，OpenAI 側也會自行過期
VECTOR_STORE_EXPIRES_DAYS = _env_int("DEEP_SEARCH_VECTOR_STORE_EXPIRES_DAYS", 1)


# ─────────────────────────────────────────────────────────────
# 可接受的檔案類型
# ─────────────────────────────────────────────────────────────
# 走 OpenAI File Search（上傳至 vector store 後由 Agent 檢索）
FILE_SEARCH_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".md", ".json", ".html", ".pptx",
}
# 走本地解析 → 轉 Markdown → 直接放進 prompt
SPREADSHEET_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
# 走多模態 input_image
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

ACCEPTED_EXTENSIONS = (
    FILE_SEARCH_EXTENSIONS | SPREADSHEET_EXTENSIONS | IMAGE_EXTENSIONS
)

# 明確不支援、但使用者很可能會丟上來的舊格式 → 給可行動的錯誤訊息
LEGACY_EXTENSION_HINTS = {
    ".doc": "舊版 Word（.doc）無法解析，請另存為 .docx 後再上傳。",
    ".xls": "舊版 Excel（.xls）無法解析，請另存為 .xlsx 後再上傳。",
    ".ppt": "舊版 PowerPoint（.ppt）無法解析，請另存為 .pptx 後再上傳。",
}


def public_config() -> Dict[str, Any]:
    """`GET /api/deep-research/config` 的回應內容。"""
    from .templates.themes import DEFAULT_THEME, public_themes

    return {
        "themes": public_themes(),
        "default_theme": DEFAULT_THEME,
        "default_model": resolve_model(None),
        "models": available_models(),
        "max_files": MAX_FILES,
        "max_file_mb": MAX_FILE_MB,
        "max_images": MAX_IMAGES,
        "query_max_chars": QUERY_MAX_CHARS,
        "accepted_extensions": sorted(ACCEPTED_EXTENSIONS),
    }
