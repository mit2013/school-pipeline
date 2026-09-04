from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Iterator

from ..models import SourceRef
from .base import RawDocument

logger = logging.getLogger(__name__)


class PdfSource:
    """指定フォルダ内のPDF(配布プリント等)からテキストを抽出する。

    通常のテキスト抽出で本文が取れない(スキャン画像PDFなど)場合は
    OCRにフォールバックする。
    """

    def __init__(self, directory: str | Path, ocr_fallback: bool = True) -> None:
        self._directory = Path(directory)
        self._ocr_fallback = ocr_fallback

    def fetch(self) -> Iterator[RawDocument]:
        if not self._directory.exists():
            return
        for path in sorted(self._directory.glob("*.pdf")):
            text = _extract_pdf_text(path)
            if not text.strip() and self._ocr_fallback:
                text = _ocr_pdf(path)
            if not text.strip():
                logger.warning("No text extracted from %s", path)
                continue
            ref_date = date.fromtimestamp(path.stat().st_mtime)
            yield RawDocument(
                source=SourceRef(
                    source_type="pdf",
                    source_id=str(path),
                    label=path.name,
                    captured_at=ref_date.isoformat(),
                ),
                text=text,
                reference_date=ref_date,
            )


def _extract_pdf_text(path: Path) -> str:
    import pdfplumber

    chunks = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def _ocr_pdf(path: Path) -> str:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning("OCR依存パッケージが未インストールのため %s のOCRをスキップします", path)
        return ""
    chunks = [pytesseract.image_to_string(image, lang="jpn") for image in convert_from_path(str(path))]
    return "\n".join(chunks)
