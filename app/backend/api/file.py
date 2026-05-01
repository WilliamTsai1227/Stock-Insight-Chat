from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from uuid import UUID
from ..models import FileModel
from typing import List

# 這裡不再需要寫 prefix="/api/files"，因為 __init__.py 已經幫你處理好了
router = APIRouter(tags=["Files"])

@router.post("/api/files/upload")
async def upload_file(
    project_id: UUID = Form(...),
    chat_id: UUID = Form(None),
    file: UploadFile = File(...)
):
    """
    實作規格書 ## 1. 檔案上傳接口
    1. 接收 Binary 檔案
    2. 檢查檔案格式
    3. 上傳至 S3 儲存
    4. 建立資料庫紀錄並回傳 ID
    """
    # 這裡定義允許的上傳格式
    ALLOWED_CONTENT_TYPES = [
        "image/jpeg", "image/png", "image/webp", # 圖片
        "application/pdf",                        # PDF
        "text/csv", "text/plain",                 # CSV / 文字檔
        "application/vnd.ms-excel",               # 舊版 Excel
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" # 新版 Excel
    ]

    # 檢查 Content-Type
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400, 
            detail=f"不支援的檔案格式: {file.content_type}。目前僅支援：圖片、PDF、Excel 或 CSV 檔案。"
        )

    try:
        # TODO: 這裡未來會實作 S3 上傳與資料庫儲存邏輯
        return {
            "status": "success",
            "data": {
                "file_id": "file_uuid_example",
                "file_type": file.content_type,
                "file_name": file.filename,
                "status": "processing"
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/files/{file_id}")
async def delete_file(file_id: UUID):
    """
    根據 file_id 刪除指定檔案
    """
    return {"status": "success", "message": f"File {file_id} has been deleted."}
