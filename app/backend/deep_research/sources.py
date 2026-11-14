"""
深度研究：使用者上傳檔案的前處理。

依副檔名分三條路，理由是 OpenAI File Search 的支援格式不含試算表，
而圖片本來就不該進向量庫：

  .pdf/.docx/.pptx/.txt/.md/... → 上傳到臨時 vector store，交給 FileSearchTool 檢索
  .xlsx/.xlsm/.csv             → 本地轉成 Markdown 表格，直接放進 prompt
  .png/.jpg/...                → 轉 base64 data URL，以 input_image 傳給多模態模型

vector store 只在這一次研究期間存在：研究結束（含失敗）立刻 `cleanup()` 刪除，
另外再加掛 expires_after 當保險絲，避免後端沒清乾淨時檔案留在 OpenAI 帳號裡。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import io
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status

from .config import (
    ACCEPTED_EXTENSIONS,
    FILENAME_MAX_CHARS,
    IMAGE_EXTENSIONS,
    LEGACY_EXTENSION_HINTS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_MB,
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_MB,
    SPREADSHEET_EXTENSIONS,
    SPREADSHEET_MAX_CHARS,
    SPREADSHEET_TOTAL_MAX_CHARS,
    VECTOR_STORE_EXPIRES_DAYS,
)

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# 試算表轉 Markdown 的取樣上限（避免一張百萬列的表把 prompt 撐爆）
_SHEET_MAX_ROWS = 200
_SHEET_MAX_COLS = 30


def _ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _client():
    """深度研究專用的 AsyncOpenAI client（與 Agents SDK 共用同一把金鑰）。"""
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="伺服器未設定 OPENAI_API_KEY，深度研究無法使用。",
        )
    return AsyncOpenAI(api_key=api_key)


# ─────────────────────────────────────────────────────────────
# 讀檔與驗證
# ─────────────────────────────────────────────────────────────
@dataclass
class RawUpload:
    filename: str
    ext: str
    content: bytes

    @property
    def kind(self) -> str:
        if self.ext in IMAGE_EXTENSIONS:
            return "image"
        if self.ext in SPREADSHEET_EXTENSIONS:
            return "spreadsheet"
        return "document"


def _bad_request(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# 檔名裡不該出現的字元：控制字元、零寬字元、雙向覆寫（RLO 可以把
# 「報告‮fdp.exe」顯示成「報告exe.pdf」）。檔名會進 prompt 與畫面，一律剔除。
_FILENAME_STRIP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}


def sanitize_filename(raw: str) -> str:
    """
    把瀏覽器送來的檔名收斂成可安全顯示、可放進 prompt 的字串。

    - 去掉路徑（`../../etc/passwd`、`C:\\x\\a.pdf` → 只留最後一段）
    - 去掉控制／零寬／雙向覆寫字元
    - 過長時截斷主檔名、保留副檔名
    """
    import unicodedata

    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(
        ch for ch in name if unicodedata.category(ch) not in _FILENAME_STRIP_CATEGORIES
    )
    name = " ".join(name.split()).rstrip(" .")
    # 不用 os.path.splitext：它把 ".pdf" 當成沒有副檔名的隱藏檔
    dot = name.rfind(".")
    tail = name[dot + 1:] if dot >= 0 else ""
    if tail and len(tail) <= 10 and tail.isalnum():
        stem, ext = name[:dot], "." + tail.lower()
    else:
        stem, ext = name, ""
    stem = stem.strip(" .") or "未命名檔案"
    room = max(1, FILENAME_MAX_CHARS - len(ext))
    return f"{stem[:room]}{ext}"


async def _read_limited(upload: UploadFile, limit: int, display: str) -> bytes:
    """分段讀取，一超過上限就停：不必先把整個超大檔案讀進記憶體才發現太大。"""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _bad_request(
                f"檔案 {display} 超過 {limit // (1024 * 1024)} MB 上限。",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


# 各格式的檔頭（magic bytes）。副檔名可以亂改，檔頭才是實際內容 ——
# 把執行檔改名成 .pdf、把 HTML 改名成 .png 都在這裡擋下。
_OFFICE_REQUIRED_MEMBER = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
    ".xlsm": "xl/workbook.xml",
}
_TEXT_EXTENSIONS = {".txt", ".md", ".json", ".html", ".csv"}
_IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}


def _check_office_zip(ext: str, content: bytes, display: str) -> None:
    import zipfile

    if not content.startswith(b"PK\x03\x04"):
        raise _bad_request(f"{display} 不是有效的 {ext} 檔（內容與副檔名不符）。")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise _bad_request(f"{display} 內部結構異常（檔案數過多），已拒絕。")
            uncompressed = sum(m.file_size for m in members)
            if uncompressed > MAX_ZIP_UNCOMPRESSED_MB * 1024 * 1024:
                raise _bad_request(
                    f"{display} 解壓後超過 {MAX_ZIP_UNCOMPRESSED_MB} MB，疑似壓縮炸彈，已拒絕。"
                )
            names = {m.filename for m in members}
    except zipfile.BadZipFile:
        raise _bad_request(f"{display} 檔案已損毀或不是有效的 {ext} 檔。")
    if "[Content_Types].xml" not in names or _OFFICE_REQUIRED_MEMBER[ext] not in names:
        raise _bad_request(f"{display} 不是有效的 {ext} 檔（內容與副檔名不符）。")


def _check_image(ext: str, content: bytes, display: str) -> None:
    import warnings

    from PIL import Image

    try:
        with warnings.catch_warnings():
            # 像素上限由下方自行判斷，不要讓 Pillow 的 DecompressionBombWarning 洗 log
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as img:
                fmt = img.format
                width, height = img.size
                img.verify()      # 只驗結構，不解碼整張圖
    except Image.DecompressionBombError:
        raise _bad_request(f"圖片 {display} 解析度過大，已拒絕。")
    except Exception:  # noqa: BLE001 — 打不開就是壞檔或偽裝檔
        raise _bad_request(f"{display} 不是有效的圖片檔（內容與副檔名不符或已損毀）。")

    if fmt != _IMAGE_FORMATS[ext]:
        raise _bad_request(f"{display} 的實際格式是 {fmt}，與副檔名 {ext} 不符。")
    if width * height > MAX_IMAGE_PIXELS:
        raise _bad_request(
            f"圖片 {display} 解析度過大（{width}×{height}），請縮小後再上傳。"
        )


def validate_content(ext: str, content: bytes, display: str) -> None:
    """依副檔名檢查實際內容；不合格直接拋 400。CPU 工作，呼叫端放到 thread 執行。"""
    if ext == ".pdf":
        # PDF 規格允許 %PDF- 前面有少量垃圾位元組，但必須在前 1KB 內
        if b"%PDF-" not in content[:1024]:
            raise _bad_request(f"{display} 不是有效的 PDF 檔（內容與副檔名不符）。")
    elif ext in _OFFICE_REQUIRED_MEMBER:
        _check_office_zip(ext, content, display)
    elif ext in _IMAGE_FORMATS:
        _check_image(ext, content, display)
    elif ext in _TEXT_EXTENSIONS:
        # 純文字不該有 NUL：有的話幾乎可以確定是改了副檔名的二進位檔
        if b"\x00" in content:
            raise _bad_request(f"{display} 看起來不是純文字檔（含二進位內容）。")


async def read_uploads(files: List[UploadFile]) -> List[RawUpload]:
    """
    把 UploadFile 讀進記憶體並做驗證，任何一個不合格就整批拒絕。

    檢查順序由便宜到昂貴：數量 → 副檔名 → 大小（邊讀邊算）→ 總量 → 實際內容。
    """
    usable = [f for f in files if f and f.filename]
    if len(usable) > MAX_FILES:
        raise _bad_request(f"一次最多上傳 {MAX_FILES} 個檔案（收到 {len(usable)} 個）。")

    result: List[RawUpload] = []
    image_count = 0
    total_bytes = 0
    seen_names: set[str] = set()

    try:
        for upload in usable:
            display = sanitize_filename(upload.filename)
            ext = _ext_of(display)

            if ext in LEGACY_EXTENSION_HINTS:
                raise _bad_request(LEGACY_EXTENSION_HINTS[ext])
            if ext not in ACCEPTED_EXTENSIONS:
                raise _bad_request(
                    f"不支援的檔案格式：{display}。"
                    f"可接受 {'、'.join(sorted(ACCEPTED_EXTENSIONS))}。"
                )

            is_image = ext in IMAGE_EXTENSIONS
            if is_image:
                image_count += 1
                if image_count > MAX_IMAGES:
                    raise _bad_request(f"一次最多上傳 {MAX_IMAGES} 張圖片。")

            limit = MAX_IMAGE_BYTES if is_image else MAX_FILE_BYTES
            content = await _read_limited(upload, limit, display)
            if not content:
                raise _bad_request(f"檔案 {display} 是空的。")

            total_bytes += len(content)
            if total_bytes > MAX_TOTAL_BYTES:
                raise _bad_request(
                    f"所有檔案加總超過 {MAX_TOTAL_MB} MB 上限。",
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

            await asyncio.to_thread(validate_content, ext, content, display)

            # 同名檔案加上序號，避免 prompt 與來源清單裡分不清
            name, n = display, 2
            while name in seen_names:
                stem, e = os.path.splitext(display)
                name = f"{stem} ({n}){e}"
                n += 1
            seen_names.add(name)

            result.append(RawUpload(filename=name, ext=ext, content=content))
    finally:
        for upload in usable:
            with contextlib.suppress(Exception):
                await upload.close()

    return result


# ─────────────────────────────────────────────────────────────
# 試算表 → Markdown
# ─────────────────────────────────────────────────────────────
def _cells_to_markdown(rows: List[List[str]]) -> str:
    """第一列當表頭轉成 Markdown 表格；空表回空字串。"""
    if not rows:
        return ""

    # openpyxl 會把每列補滿到 max_col，尾端整欄皆空的要先砍掉，
    # 否則表格會拖著一串空欄位，白白吃掉 prompt 額度也難讀
    width = max(len(r) for r in rows)
    while width > 1 and all(not (r[width - 1].strip() if width <= len(r) else "") for r in rows):
        width -= 1

    padded = [(r + [""] * width)[:width] for r in rows]
    header, body = padded[0], padded[1:]

    def escape(cell: str) -> str:
        return (cell or "").replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        "| " + " | ".join(escape(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(escape(c) for c in r) + " |" for r in body)
    return "\n".join(lines)


def _xlsx_to_markdown(filename: str, content: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(
        io.BytesIO(content), read_only=True, data_only=True, keep_links=False
    )
    try:
        blocks: List[str] = []
        for sheet in workbook.worksheets:
            rows: List[List[str]] = []
            for row in sheet.iter_rows(
                max_row=_SHEET_MAX_ROWS, max_col=_SHEET_MAX_COLS, values_only=True
            ):
                if row is None or all(c is None for c in row):
                    continue
                rows.append(["" if c is None else str(c) for c in row])
            if not rows:
                continue
            truncated = (
                f"\n\n> （僅擷取前 {_SHEET_MAX_ROWS} 列）"
                if sheet.max_row and sheet.max_row > _SHEET_MAX_ROWS
                else ""
            )
            blocks.append(
                f"### 工作表：{sheet.title}\n\n{_cells_to_markdown(rows)}{truncated}"
            )
        return "\n\n".join(blocks)
    finally:
        workbook.close()


def _csv_to_markdown(content: bytes) -> str:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [
        row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)
    ]
    return _cells_to_markdown(rows[:_SHEET_MAX_ROWS])


def spreadsheet_to_markdown(upload: RawUpload) -> str:
    if upload.ext == ".csv":
        body = _csv_to_markdown(upload.content)
    else:
        body = _xlsx_to_markdown(upload.filename, upload.content)

    if len(body) > SPREADSHEET_MAX_CHARS:
        body = body[:SPREADSHEET_MAX_CHARS] + "\n\n> （內容過長，已截斷）"
    return body


# ─────────────────────────────────────────────────────────────
# 前處理結果
# ─────────────────────────────────────────────────────────────
@dataclass
class PreparedSources:
    names: List[str] = field(default_factory=list)
    vector_store_id: Optional[str] = None
    uploaded_file_ids: List[str] = field(default_factory=list)
    # (檔名, Markdown) —— 直接拼進 prompt
    spreadsheets: List[Tuple[str, str]] = field(default_factory=list)
    # (檔名, data URL) —— 以 input_image 傳給模型
    images: List[Tuple[str, str]] = field(default_factory=list)
    # 個別檔案處理失敗的說明（不中斷整體研究，只回報給前端）
    warnings: List[str] = field(default_factory=list)

    @property
    def has_file_search(self) -> bool:
        return bool(self.vector_store_id)

    def spreadsheet_prompt_block(self) -> str:
        """把試算表內容組成一段可直接附在使用者訊息後面的文字。"""
        if not self.spreadsheets:
            return ""

        chunks: List[str] = []
        budget = SPREADSHEET_TOTAL_MAX_CHARS
        for name, body in self.spreadsheets:
            if budget <= 0:
                chunks.append(f"## 附件試算表：{name}\n\n> （超出可讀取上限，已略過）")
                continue
            clipped = body[:budget]
            budget -= len(clipped)
            chunks.append(f"## 附件試算表：{name}\n\n{clipped}")

        return "以下是使用者上傳的試算表內容（已轉為 Markdown）：\n\n" + "\n\n".join(chunks)

    async def cleanup(self) -> None:
        """刪掉這次研究在 OpenAI 上留下的所有東西；失敗只記錄不拋出。"""
        if not self.vector_store_id and not self.uploaded_file_ids:
            return

        client = _client()
        try:
            if self.vector_store_id:
                try:
                    await client.vector_stores.delete(self.vector_store_id)
                except Exception as exc:  # noqa: BLE001 — 清理失敗不該影響已回傳的結果
                    print(
                        f"[DeepResearch] vector store 刪除失敗 "
                        f"{self.vector_store_id}: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                finally:
                    self.vector_store_id = None

            for file_id in self.uploaded_file_ids:
                try:
                    await client.files.delete(file_id)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[DeepResearch] 檔案刪除失敗 {file_id}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
            self.uploaded_file_ids = []
        finally:
            await client.close()


async def prepare_sources(uploads: List[RawUpload]) -> PreparedSources:
    """
    把已驗證的上傳內容轉成 Agent 可用的三種形態。

    只要有任何一個文件類檔案，就會建立 vector store；失敗時會先把已建立的
    資源清掉再拋出，不會留下孤兒 vector store。
    """
    prepared = PreparedSources(names=[u.filename for u in uploads])

    for upload in uploads:
        if upload.kind == "image":
            media_type = _IMAGE_MEDIA_TYPES.get(upload.ext, "image/png")
            encoded = base64.b64encode(upload.content).decode("ascii")
            prepared.images.append(
                (upload.filename, f"data:{media_type};base64,{encoded}")
            )
        elif upload.kind == "spreadsheet":
            try:
                body = await asyncio.to_thread(spreadsheet_to_markdown, upload)
            except Exception as exc:  # noqa: BLE001 — 單一檔案壞掉不該中斷整場研究
                prepared.warnings.append(
                    f"{upload.filename} 解析失敗（{type(exc).__name__}），已略過。"
                )
                continue
            if body.strip():
                prepared.spreadsheets.append((upload.filename, body))
            else:
                prepared.warnings.append(f"{upload.filename} 沒有可讀取的內容，已略過。")

    documents = [u for u in uploads if u.kind == "document"]
    if not documents:
        return prepared

    client = _client()
    try:
        store = await client.vector_stores.create(
            name="deep-research-session",
            expires_after={
                "anchor": "last_active_at",
                "days": VECTOR_STORE_EXPIRES_DAYS,
            },
        )
        prepared.vector_store_id = store.id

        for doc in documents:
            vs_file = await client.vector_stores.files.upload_and_poll(
                vector_store_id=store.id,
                file=(doc.filename, doc.content),
            )
            prepared.uploaded_file_ids.append(vs_file.id)
            if vs_file.status != "completed":
                reason = getattr(getattr(vs_file, "last_error", None), "message", "")
                prepared.warnings.append(
                    f"{doc.filename} 建立索引失敗"
                    f"{f'（{reason}）' if reason else ''}，研究時無法檢索此檔。"
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        await prepared.cleanup()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"文件索引建立失敗：{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        await client.close()

    # 全部文件都索引失敗時，掛著空的 vector store 只會讓 Agent 空轉
    if prepared.vector_store_id and len(prepared.warnings) >= len(documents):
        await prepared.cleanup()

    return prepared


def source_manifest(prepared: PreparedSources) -> List[Dict[str, Any]]:
    """回給前端顯示「這次讀了哪些檔案、用什麼方式讀」。"""
    manifest: List[Dict[str, Any]] = []
    doc_names = set(prepared.names) - {n for n, _ in prepared.spreadsheets} - {
        n for n, _ in prepared.images
    }
    for name in prepared.names:
        if name in {n for n, _ in prepared.images}:
            channel = "image"
        elif name in {n for n, _ in prepared.spreadsheets}:
            channel = "spreadsheet"
        elif name in doc_names:
            channel = "file_search"
        else:
            channel = "skipped"
        manifest.append({"name": name, "channel": channel})
    return manifest
