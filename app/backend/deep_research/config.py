"""
深度研究：環境變數、可選模型清單與各種上限。

所有環境變數皆有預設值，一個都不設也能運作。`DEEP_SEARCH_MODEL` /
`DEEP_SEARCH_MODELS` 只能在 `MODEL_CATALOG` 白名單內挑選，不能引入新模型 ——
理由見下方白名單的註解。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────
# 模型
# ─────────────────────────────────────────────────────────────
# 唯一允許的模型。這裡是白名單，不是「建議清單」：
# 深度研究一次會燒掉數十萬 token（web search 每輪都把搜尋結果塞回 prompt），
# 換一個模型就是換一組費率，而費率對照表（module/token_usage.py）與配額扣點
# 都以這裡的 id 為準。開放使用者自選模型 = 開放他們自選單價，
# 因此只留成本最低、且已實測支援 web_search / file_search 的 gpt-5.6-luna。
#
# 要放寬時：先在 TOKEN_COST_TABLE 補上該模型的費率，再把 id 加進這裡；
# 只改其中一邊會讓那個模型的用量以錯誤單價入帳。
MODEL_CATALOG: Dict[str, Dict[str, str]] = {
    "gpt-5.6-luna": {
        "label": "GPT-5.6 Luna",
        "description": "深度研究專用模型；支援 web search 與 file search",
    },
}

# 白名單本身不吃環境變數 —— 見 `_catalog_from_env()` 的說明
ALLOWED_MODEL_IDS = frozenset(MODEL_CATALOG)

_FALLBACK_MODEL = "gpt-5.6-luna"


# 已提醒過的設定錯誤（source, 被擋掉的 id）；每次 /config 都會重讀環境變數，
# 沒有這個集合的話同一行警告會跟著每一個請求重印
_warned_rejections: set = set()


def _reject_unlisted(model_ids: List[str], source: str) -> List[str]:
    """濾掉不在白名單內的 model id，並在 log 留下痕跡（同一組只提醒一次）。"""
    kept, dropped = [], []
    for model_id in model_ids:
        (kept if model_id in ALLOWED_MODEL_IDS else dropped).append(model_id)
    if dropped:
        key = (source, tuple(dropped))
        if key not in _warned_rejections:
            _warned_rejections.add(key)
            print(
                f"[DEEP-SEARCH] 已忽略 {source} 中不在白名單內的模型："
                f"{'、'.join(dropped)}（可用：{'、'.join(sorted(ALLOWED_MODEL_IDS))}）",
                flush=True,
            )
    return kept


def _catalog_from_env() -> Dict[str, Dict[str, str]]:
    """
    `DEEP_SEARCH_MODELS`（逗號分隔）只能**縮減**可選清單，不能擴充：

        DEEP_SEARCH_MODELS=gpt-5.6-luna

    清單外的 id 會被忽略並記在啟動 log。以前這裡接受任意 id（方便試新模型），
    但那條路徑同時繞過了費率對照 —— 設一個沒有費率的模型，它的用量會以
    `cost_usd = 0` 入帳，看起來像免費的。`DEEP_SEARCH_MODEL` 同樣受此限制。
    """
    raw = os.getenv("DEEP_SEARCH_MODELS", "").strip()
    picked: Dict[str, Dict[str, str]] = {}
    if raw:
        requested = [item.strip() for item in raw.split(",") if item.strip()]
        for model_id in _reject_unlisted(requested, "DEEP_SEARCH_MODELS"):
            picked[model_id] = MODEL_CATALOG[model_id]
    else:
        picked = dict(MODEL_CATALOG)

    # 運維指定的預設值一定要在清單裡（同樣要先過白名單）
    default = os.getenv("DEEP_SEARCH_MODEL", "").strip()
    if default and default not in picked:
        for model_id in _reject_unlisted([default], "DEEP_SEARCH_MODEL"):
            picked[model_id] = MODEL_CATALOG[model_id]

    return picked or dict(MODEL_CATALOG)


def available_models() -> List[Dict[str, str]]:
    """前端下拉選單用的模型清單。"""
    return [
        {"id": model_id, "label": meta["label"], "description": meta["description"]}
        for model_id, meta in _catalog_from_env().items()
    ]


def _resolve_default_model() -> str:
    """`DEEP_SEARCH_MODEL` 不在白名單時退回 `_FALLBACK_MODEL`（已於上方記 log）。"""
    configured = os.getenv("DEEP_SEARCH_MODEL", "").strip()
    if configured in ALLOWED_MODEL_IDS:
        return configured
    return _FALLBACK_MODEL


# 預設模型：使用者未在前端選擇時採用
DEEP_SEARCH_DEFAULT_MODEL = _resolve_default_model()


def resolve_model(requested: Optional[str]) -> str:
    """
    將前端送來的 model 收斂成實際要用的 model id。

    不在清單內的一律退回預設模型 —— 這個參數直接來自瀏覽器，
    不能讓使用者任意指定字串去打 OpenAI（也就等於自選單價）。
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
# 圖片會整張轉成 base64 放進請求（體積再 +33%），上限比一般檔案低
MAX_IMAGE_MB = min(_env_int("DEEP_SEARCH_MAX_IMAGE_MB", 10), MAX_FILE_MB)
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
# 單次請求所有檔案加總：檔案會被讀進記憶體，而 backend 容器只有 640m，
# 只限單檔的話 10 × 20MB = 200MB 一次就能把它打到 OOM
MAX_TOTAL_MB = max(MAX_FILE_MB, _env_int("DEEP_SEARCH_MAX_TOTAL_MB", 50))
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024
# 圖片像素上限（解壓縮炸彈：一張幾 KB 的 PNG 可以宣告數十億像素）
MAX_IMAGE_PIXELS = _env_int("DEEP_SEARCH_MAX_IMAGE_PIXELS", 40_000_000)
# Office 檔（docx/pptx/xlsx 本質是 zip）解壓後總量與成員數上限（zip bomb）
MAX_ZIP_UNCOMPRESSED_MB = _env_int("DEEP_SEARCH_MAX_ZIP_UNCOMPRESSED_MB", 200)
MAX_ZIP_MEMBERS = _env_int("DEEP_SEARCH_MAX_ZIP_MEMBERS", 5000)
# 顯示用檔名長度上限（檔名會進 prompt 與畫面）
FILENAME_MAX_CHARS = 100
QUERY_MAX_CHARS = _env_int("DEEP_SEARCH_QUERY_MAX_CHARS", 4000)

# ─────────────────────────────────────────────────────────────
# 在對話中使用深度研究時，帶入的上下文預算
# ─────────────────────────────────────────────────────────────
# 研究本身會把每輪 web search 結果塞回 prompt，前面再疊一長串歷史很容易爆 context，
# 也會讓單次成本失控。因此歷史另有一份比一般對話更緊的預算：
#   - 每則 assistant 回覆只取開頭（舊的研究報告動輒上萬字，主題與結論多在前段）
#   - 總字數超過預算時，從最舊的訊息開始捨棄
CHAT_HISTORY_MAX_CHARS = _env_int("DEEP_SEARCH_CHAT_HISTORY_MAX_CHARS", 12000)
CHAT_HISTORY_ASSISTANT_MAX_CHARS = _env_int(
    "DEEP_SEARCH_CHAT_HISTORY_ASSISTANT_MAX_CHARS", 3000
)
CHAT_HISTORY_USER_MAX_CHARS = _env_int("DEEP_SEARCH_CHAT_HISTORY_USER_MAX_CHARS", 2000)

# 試算表轉成 Markdown 後直接塞進 prompt 的字數上限（超過即截斷）
SPREADSHEET_MAX_CHARS = _env_int("DEEP_SEARCH_SPREADSHEET_MAX_CHARS", 12000)
SPREADSHEET_TOTAL_MAX_CHARS = _env_int(
    "DEEP_SEARCH_SPREADSHEET_TOTAL_MAX_CHARS", 30000
)

# Agent 迴圈上限（一輪 = 一次模型呼叫；web search 會用掉數輪）
RESEARCH_MAX_TURNS = _env_int("DEEP_SEARCH_MAX_TURNS", 24)

# ─────────────────────────────────────────────────────────────
# 產出篇幅（簡報頁數 / 報告小節數）
# ─────────────────────────────────────────────────────────────
# 由使用者在前端指定，後端 clamp 進範圍後寫進 skill 的 instructions。
# 範圍不是排版限制（樣板本身不限頁數），而是成本與品質的煞車：
# 開太大時模型會開始灌水，一次產出也會從數十秒拉到數分鐘。
#
# 這裡有兩道限制，職責不同：
#   LENGTH_HARD_MAX  絕對上限，任何來源（前端、API、環境變數）都不得超過
#   spec["min/max"]  各 skill 的合理區間，超出會被靜靜 clamp 進來
LENGTH_HARD_MAX = 20

_LENGTH_SPECS: Dict[str, Dict[str, Any]] = {
    "report": {
        "label": "小節數",
        "unit": "節",
        "hint": "不含摘要、尚待釐清與參考來源等固定區塊",
        "min": 3,
        "max": 10,
        "default": _env_int("DEEP_SEARCH_REPORT_SECTIONS", 6),
    },
    "deck": {
        "label": "頁數",
        "unit": "頁",
        "hint": "含封面與結語頁",
        "min": 5,
        "max": 20,
        "default": _env_int("DEEP_SEARCH_DECK_SLIDES", 12),
    },
}


def _sanitized_length_specs() -> Dict[str, Dict[str, Any]]:
    """
    把 spec 自身校正到可信狀態，程式啟動時做一次。

    `default` 來自環境變數，是唯一能繞過 clamp 的路徑 —— `resolve_length(kind, None)`
    直接回傳它。少了這裡的校正，`DEEP_SEARCH_DECK_SLIDES=999` 會讓每一次沒指定頁數的
    產出都跑 999 頁，而且從 API 完全看不出異常。
    """
    for spec in _LENGTH_SPECS.values():
        spec["max"] = min(spec["max"], LENGTH_HARD_MAX)
        spec["min"] = min(spec["min"], spec["max"])
        spec["default"] = max(spec["min"], min(spec["max"], spec["default"]))
    return _LENGTH_SPECS


LENGTH_SPECS: Dict[str, Dict[str, Any]] = _sanitized_length_specs()


def exceeds_hard_max(requested: Optional[int]) -> bool:
    """
    這個值是否越過絕對上限，該直接回 400 而非默默 clamp。

    區間內的偏差（例如簡報填 25）clamp 掉就好，使用者拿到 20 頁不會有損失；
    但 `length: 500` 這種只會來自繞過前端的直打，回一個明確的錯誤比悄悄改成
    20 頁誠實，也讓濫用在 log 裡看得見。
    """
    if requested is None:
        return False
    try:
        return not (1 <= int(requested) <= LENGTH_HARD_MAX)
    except (TypeError, ValueError):
        return True


def resolve_length(kind: str, requested: Optional[int]) -> int:
    """
    將前端送來的篇幅收斂成實際要寫進 prompt 的數字。

    這個值直接來自瀏覽器，且會變成模型的輸出量 —— 沒 clamp 的話
    一個 `length: 500` 就能讓單次產出燒掉整包 token 額度。
    超過 `LENGTH_HARD_MAX` 的請求在 API 層就被擋掉了，這裡是最後一道保險。
    """
    spec = LENGTH_SPECS.get(kind)
    if spec is None:
        raise KeyError(kind)
    try:
        value = spec["default"] if requested is None else int(requested)
    except (TypeError, ValueError):
        value = spec["default"]
    return max(spec["min"], min(spec["max"], value))


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
        "max_image_mb": MAX_IMAGE_MB,
        "max_total_mb": MAX_TOTAL_MB,
        "max_images": MAX_IMAGES,
        "query_max_chars": QUERY_MAX_CHARS,
        "accepted_extensions": sorted(ACCEPTED_EXTENSIONS),
        # 前端「迴紋針」說明面板用：每一類檔案怎麼被讀、接受哪些副檔名
        "file_groups": [
            {
                "kind": "document",
                "label": "文件",
                "how": "建立臨時索引，研究時逐段檢索",
                "extensions": sorted(FILE_SEARCH_EXTENSIONS),
            },
            {
                "kind": "spreadsheet",
                "label": "試算表",
                "how": "讀成表格（每張表最多前 200 列）",
                "extensions": sorted(SPREADSHEET_EXTENSIONS),
            },
            {
                "kind": "image",
                "label": "圖片",
                "how": "直接交給模型看圖",
                "extensions": sorted(IMAGE_EXTENSIONS),
            },
        ],
        "length_specs": LENGTH_SPECS,
    }
