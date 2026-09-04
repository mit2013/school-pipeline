from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

from ..models import SourceRef
from .base import RawDocument


class ManualTextSource:
    """Classroomの投稿など、APIで取得できない情報を手動でコピペした
    .txt ファイル置き場から読み込む。

    運用イメージ: 子ども(または保護者)がClassroomの投稿を見つけたら、
    本文をテキストファイルとしてこのフォルダに保存するだけでよい。
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def fetch(self) -> Iterator[RawDocument]:
        if not self._directory.exists():
            return
        for path in sorted(self._directory.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            ref_date = date.fromtimestamp(path.stat().st_mtime)
            yield RawDocument(
                source=SourceRef(
                    source_type="manual",
                    source_id=str(path),
                    label=path.stem,
                    captured_at=ref_date.isoformat(),
                ),
                text=text,
                reference_date=ref_date,
            )
