from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Iterator

from ..models import SourceRef
from .base import RawDocument

logger = logging.getLogger(__name__)

_EXTENSIONS = (".jpg", ".jpeg", ".png")


class ImageSource:
    """スマホで撮影したプリント写真をOCRで読み込む。"""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def fetch(self) -> Iterator[RawDocument]:
        if not self._directory.exists():
            return
        for path in sorted(self._directory.iterdir()):
            if path.suffix.lower() not in _EXTENSIONS:
                continue
            text = _ocr_image(path)
            if not text.strip():
                logger.warning("No text extracted from %s", path)
                continue
            ref_date = date.fromtimestamp(path.stat().st_mtime)
            yield RawDocument(
                source=SourceRef(
                    source_type="image",
                    source_id=str(path),
                    label=path.name,
                    captured_at=ref_date.isoformat(),
                ),
                text=text,
                reference_date=ref_date,
            )


def _ocr_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("OCR依存パッケージが未インストールのため %s のOCRをスキップします", path)
        return ""
    with Image.open(path) as img:
        return pytesseract.image_to_string(img, lang="jpn")
