"""
金融分析 skill 註冊表
=====================
使用者在輸入框用 `@dcf`、`@財報解讀` 這類字樣點名 skill，前端把 skill id 帶進請求；
後端依 id 取出 `library/<id>/SKILL.md`，把裡面的方法論注入兩條流程：

| 流程       | `## 資料蒐集`                       | `## 分析與輸出`                  |
|------------|-------------------------------------|----------------------------------|
| 深度研究   | 併入研究指示（決定要查什麼）        | 取代預設的研究簡報格式           |
| 股市 Agent | 併入 Router 指示（決定呼叫哪些工具）| 併入 Analyst 指示（決定怎麼寫）  |

SKILL.md 格式：YAML frontmatter（id、mention、label、description、source…）
＋ Markdown 正文，正文必須含上面兩個 `##` 小節。

檔案在 import 時載入一次並驗證；格式錯誤直接讓服務起不來，
而不是在使用者點名時才炸掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

LIBRARY_DIR = Path(__file__).resolve().parent / "library"

GATHER_HEADING = "資料蒐集"
OUTPUT_HEADING = "分析與輸出"

# id 只允許小寫英數與連字號：它會進 API、DB metadata 與 log
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# mention 是使用者打在 @ 後面的字：不可含空白或 @（前端以空白判斷提及結束）
_MENTION_PATTERN = re.compile(r"^[^\s@]{1,20}$")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class SkillFormatError(ValueError):
    """SKILL.md 缺欄位或缺必要小節。"""


@dataclass(frozen=True)
class Skill:
    id: str
    mention: str
    label: str
    description: str
    source: str
    gather: str        # 「資料蒐集」小節正文
    output: str        # 「分析與輸出」小節正文

    @property
    def token(self) -> str:
        """輸入框裡的字樣，例如 `@財報解讀`。"""
        return f"@{self.mention}"

    def public(self) -> Dict[str, str]:
        """給前端的欄位：不含 prompt 正文（沒必要外流，也省流量）。"""
        return {
            "id": self.id,
            "mention": self.mention,
            "label": self.label,
            "description": self.description,
        }


def _split_sections(body: str) -> Dict[str, str]:
    """以 `## ` 標題切出各小節（`###` 以下屬於所在小節的內容）。"""
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    lines: List[str] = []
    for line in body.splitlines():
        m = re.match(r"^## (?!#)(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current, lines = m.group(1), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def parse_skill(text: str, where: str = "<string>") -> Skill:
    m = _FRONTMATTER.match(text.replace("\r\n", "\n"))
    if not m:
        raise SkillFormatError(f"{where}: 缺少 YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise SkillFormatError(f"{where}: frontmatter 必須是 mapping")

    for key in ("id", "mention", "label", "description", "source"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise SkillFormatError(f"{where}: frontmatter 缺少 `{key}`")
    if not _ID_PATTERN.fullmatch(meta["id"]):
        raise SkillFormatError(f"{where}: id `{meta['id']}` 格式不符")
    if not _MENTION_PATTERN.fullmatch(meta["mention"]):
        raise SkillFormatError(f"{where}: mention `{meta['mention']}` 不可含空白或 @")

    sections = _split_sections(m.group(2))
    for heading in (GATHER_HEADING, OUTPUT_HEADING):
        if not sections.get(heading):
            raise SkillFormatError(f"{where}: 缺少 `## {heading}` 小節")

    return Skill(
        id=meta["id"].strip(),
        mention=meta["mention"].strip(),
        label=meta["label"].strip(),
        description=meta["description"].strip(),
        source=meta["source"].strip(),
        gather=sections[GATHER_HEADING],
        output=sections[OUTPUT_HEADING],
    )


def load_library(directory: Path = LIBRARY_DIR) -> Dict[str, Skill]:
    skills: Dict[str, Skill] = {}
    mentions: Dict[str, str] = {}
    for path in sorted(directory.glob("*/SKILL.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"), str(path))
        if skill.id != path.parent.name:
            raise SkillFormatError(f"{path}: id `{skill.id}` 必須與資料夾名稱相同")
        key = skill.mention.lower()
        if key in mentions:
            raise SkillFormatError(f"{path}: mention `{skill.mention}` 與 `{mentions[key]}` 重複")
        mentions[key] = skill.id
        skills[skill.id] = skill
    return skills


# 前端選單的顯示順序；不在清單內的 skill 排在最後（依 id）
_DISPLAY_ORDER = ["dcf", "comps", "earnings", "chips", "sector"]

SKILLS: Dict[str, Skill] = load_library()


def get_skill(skill_id: Optional[str]) -> Optional[Skill]:
    if not skill_id:
        return None
    return SKILLS.get(skill_id)


def list_skills() -> List[Skill]:
    rank = {sid: i for i, sid in enumerate(_DISPLAY_ORDER)}
    return sorted(SKILLS.values(), key=lambda s: (rank.get(s.id, len(rank)), s.id))


__all__ = [
    "SKILLS",
    "Skill",
    "SkillFormatError",
    "get_skill",
    "list_skills",
    "load_library",
    "parse_skill",
]
