"""
module 套件 (app/backend/module/)
===================================
存放後端共用的核心工具模組。

目前包含：
  jwt.py — JWT 完整生命週期管理
            ① 全域設定（金鑰、演算法、有效期）
            ② 密碼工具（Argon2 雜湊 / 驗證）
            ③ 簽發 Token（AT / RT / decode）
            ④ 驗收 Token（FastAPI Depends：get_current_user_id / get_current_user）
"""

__all__ = ["jwt"]
