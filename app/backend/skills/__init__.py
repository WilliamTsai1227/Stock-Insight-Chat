"""
金融分析 skill（使用者以 @ 點名，例如 `@dcf`、`@財報解讀`）。

skill 本身是 `library/<id>/SKILL.md`；`registry` 負責載入與驗證，
`prompts` 負責把 skill 套進深度研究與股市 Agent 的系統提示。
"""

from .registry import SKILLS, Skill, get_skill, list_skills

__all__ = ["SKILLS", "Skill", "get_skill", "list_skills"]
