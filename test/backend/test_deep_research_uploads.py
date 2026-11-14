"""深度研究：上傳檔案驗證、檔名清洗、對話上下文預算、上傳大小 middleware。"""

import asyncio
import io
import zipfile

import pytest
from fastapi import HTTPException

from app.backend.deep_research import sources
from app.backend.deep_research.researcher import history_to_input
from app.backend.deep_research.sources import read_uploads, sanitize_filename, validate_content


# ── 測試用檔案 ────────────────────────────────────────────────────────────

def _png(width: int = 4, height: int = 4) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="JPEG")
    return buf.getvalue()


def _office(required: str, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(required, "<x/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


class _Upload:
    """最小的 UploadFile 替身：read(size) / close()。"""

    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._buf = io.BytesIO(content)
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# ── 檔名清洗 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("../../etc/passwd.txt", "passwd.txt"),
    ("C:\\Users\\x\\報告.pdf", "報告.pdf"),
    ("報告\u202efdp.exe", "報告fdp.exe"),          # RLO 雙向覆寫被拿掉
    ("a\u200bb\x00c.md", "abc.md"),               # 零寬、NUL
    ("   .pdf", "未命名檔案.pdf"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension():
    name = sanitize_filename("x" * 500 + ".pdf")
    assert name.endswith(".pdf") and len(name) <= 100


# ── 內容與副檔名一致性 ────────────────────────────────────────────────────

@pytest.mark.parametrize("ext, content", [
    (".pdf", b"%PDF-1.7\n..."),
    (".png", _png()),
    (".jpg", _jpeg()),
    (".docx", _office("word/document.xml")),
    (".pptx", _office("ppt/presentation.xml")),
    (".xlsx", _office("xl/workbook.xml")),
    (".txt", "純文字內容".encode()),
    (".csv", "a,b\n1,2\n".encode("big5")),        # 台灣常見的 Big5 CSV 也要能過
])
def test_valid_content_passes(ext, content):
    validate_content(ext, content, "f" + ext)


@pytest.mark.parametrize("ext, content, why", [
    (".pdf", b"MZ\x90\x00 windows exe", "執行檔改名成 pdf"),
    (".png", b"<html><script>alert(1)</script></html>", "HTML 改名成 png"),
    (".png", _jpeg(), "JPEG 改名成 png"),
    (".docx", b"PK\x03\x04garbage", "壞掉的 zip"),
    (".docx", _office("xl/workbook.xml"), "xlsx 改名成 docx"),
    (".txt", b"abc\x00\x01\x02", "二進位檔改名成 txt"),
])
def test_mismatched_content_rejected(ext, content, why):
    with pytest.raises(HTTPException) as exc:
        validate_content(ext, content, "f" + ext)
    assert exc.value.status_code == 400, why


def test_zip_bomb_rejected(monkeypatch):
    monkeypatch.setattr(sources, "MAX_ZIP_UNCOMPRESSED_MB", 1)
    bomb = _office("word/document.xml", {"word/media/big.bin": b"\x00" * (2 * 1024 * 1024)})
    assert len(bomb) < 100 * 1024                 # 壓縮後很小，解開卻超過上限
    with pytest.raises(HTTPException) as exc:
        validate_content(".docx", bomb, "bomb.docx")
    assert "壓縮炸彈" in exc.value.detail


def test_image_pixel_bomb_rejected(monkeypatch):
    monkeypatch.setattr(sources, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(HTTPException) as exc:
        validate_content(".png", _png(20, 20), "big.png")
    assert "解析度過大" in exc.value.detail


# ── read_uploads：數量、大小、總量 ────────────────────────────────────────

def test_read_uploads_accepts_and_dedupes_names():
    files = [_Upload("a.txt", b"one"), _Upload("a.txt", b"two"), _Upload("p.png", _png())]
    out = _run(read_uploads(files))
    assert [u.filename for u in out] == ["a.txt", "a (2).txt", "p.png"]
    assert all(f.closed for f in files)


def test_read_uploads_rejects_unknown_extension():
    with pytest.raises(HTTPException) as exc:
        _run(read_uploads([_Upload("evil.svg", b"<svg onload=alert(1)>")]))
    assert exc.value.status_code == 400


def test_read_uploads_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(sources, "MAX_FILE_BYTES", 1024)
    with pytest.raises(HTTPException) as exc:
        _run(read_uploads([_Upload("big.txt", b"x" * 5000)]))
    assert exc.value.status_code == 413


def test_read_uploads_rejects_oversized_image(monkeypatch):
    monkeypatch.setattr(sources, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(HTTPException) as exc:
        _run(read_uploads([_Upload("p.png", _png())]))
    assert exc.value.status_code == 413


def test_read_uploads_rejects_total_over_limit(monkeypatch):
    monkeypatch.setattr(sources, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(HTTPException) as exc:
        _run(read_uploads([_Upload("a.txt", b"123456"), _Upload("b.txt", b"123456")]))
    assert exc.value.status_code == 413


def test_read_uploads_rejects_too_many_images(monkeypatch):
    monkeypatch.setattr(sources, "MAX_IMAGES", 1)
    with pytest.raises(HTTPException):
        _run(read_uploads([_Upload("a.png", _png()), _Upload("b.png", _png())]))


def test_read_uploads_rejects_empty_file():
    with pytest.raises(HTTPException):
        _run(read_uploads([_Upload("a.txt", b"")]))


# ── 對話上下文預算 ────────────────────────────────────────────────────────

def test_history_clips_each_message_and_drops_oldest_first():
    turns = [
        ("user", "Q1"), ("assistant", "A" * 5000),
        ("user", "Q2"), ("assistant", "B" * 100),
    ]
    # A 截成 3000 字 + 省略標記後，三則加總超過 3100：從最舊的開始丟
    items = history_to_input(turns, total_max_chars=3100, assistant_max_chars=3000, user_max_chars=100)
    texts = [i["content"][0]["text"] for i in items]
    # 放不下 A 就停；Q1 也因此不在視窗內
    assert texts == ["Q2", "B" * 100]


def test_history_respects_total_budget_and_roles():
    turns = [("user", "q" * 50), ("assistant", "a" * 50)] * 10
    items = history_to_input(turns, total_max_chars=220, assistant_max_chars=50, user_max_chars=50)
    total = sum(len(i["content"][0]["text"]) for i in items)
    assert total <= 220
    assert items[0]["role"] == "user"
    assert items[-1]["role"] == "assistant"
    assert {i["content"][0]["type"] for i in items if i["role"] == "assistant"} == {"output_text"}


def test_history_long_message_is_truncated_with_marker():
    items = history_to_input([("user", "u" * 10)] + [("assistant", "x" * 9000)],
                             total_max_chars=10000, assistant_max_chars=3000, user_max_chars=100)
    assert items[-1]["content"][0]["text"].endswith("（以下省略）")


# ── 上傳大小 middleware ───────────────────────────────────────────────────

def _client(max_bytes: int):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from app.backend.module.upload_limit import UploadSizeLimitMiddleware

    app = FastAPI()

    @app.post("/up")
    async def up(request: Request):
        return {"n": len(await request.body())}

    @app.post("/other")
    async def other(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(UploadSizeLimitMiddleware, paths=["/up"], max_bytes=max_bytes)
    return TestClient(app)


def test_upload_limit_rejects_by_content_length():
    r = _client(100).post("/up", content=b"x" * 500)
    assert r.status_code == 413


def test_upload_limit_rejects_chunked_body_without_content_length():
    def gen():
        for _ in range(10):
            yield b"x" * 50

    r = _client(100).post("/up", content=gen())
    assert r.status_code == 413


def test_upload_limit_allows_small_and_other_paths():
    c = _client(100)
    assert c.post("/up", content=b"x" * 50).json() == {"n": 50}
    assert c.post("/other", content=b"x" * 500).json() == {"n": 500}


def test_upload_limit_returns_413_even_when_fastapi_parses_multipart():
    """FastAPI 會把解析 multipart 時的例外包成 400；middleware 要把它改回 413。"""
    from typing import List

    from fastapi import FastAPI, File, Form, UploadFile
    from fastapi.testclient import TestClient

    from app.backend.module.upload_limit import UploadSizeLimitMiddleware

    app = FastAPI()

    @app.post("/up")
    async def up(q: str = Form(...), files: List[UploadFile] = File(default=[])):
        return {"ok": True}

    app.add_middleware(UploadSizeLimitMiddleware, paths=["/up"], max_bytes=1000)

    def gen():
        yield b'--zz\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\n\r\n'
        for _ in range(20):
            yield b"a" * 200

    r = TestClient(app).post("/up", content=gen(), headers={"Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413
