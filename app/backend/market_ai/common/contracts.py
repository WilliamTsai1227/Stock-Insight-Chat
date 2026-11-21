from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class Evidence:
    """跨新聞、基本面、技術面與持倉模組共用的可追溯資料契約。"""

    source: str
    source_type: str
    source_id: Optional[str]
    facts: Iterable[Dict[str, Any]] = field(default_factory=list)
    as_of: Optional[str] = None
    confidence: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "facts": list(self.facts),
            "as_of": self.as_of,
            "confidence": self.confidence,
        }
