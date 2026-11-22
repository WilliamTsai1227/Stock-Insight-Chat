import asyncpg
from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException
from uuid import UUID

from app.backend.module.jwt import get_current_user

router = APIRouter(tags=["Files"])

@router.post("/api/files/upload")
async def upload_file(
    project_id: UUID = Form(...),
    chat_id: UUID = Form(None),
    file: UploadFile = File(...),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    舊版檔案上傳接口尚未完成儲存與權限驗證，一律拒絕處理。
    """
    # Storage and ownership checks are not implemented for this legacy route.
    # Fail closed instead of claiming a file was safely accepted or persisted.
    raise HTTPException(status_code=501, detail="Legacy file upload is unavailable. Use chat attachments.")

@router.delete("/api/files/{file_id}")
async def delete_file(
    file_id: UUID,
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    舊版刪檔接口尚未實作，一律拒絕處理。
    """
    raise HTTPException(status_code=501, detail="Legacy file deletion is unavailable.")
