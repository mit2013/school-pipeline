from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..models import SchoolEvent, SourceRef
from ..sources.base import Source
from .cache import ExtractionCache
from .dedup import dedup_events, find_possible_duplicates

logger = logging.getLogger(__name__)


class Extractor(Protocol):
    def extract(
        self, text: str, *, reference_date: date, source: SourceRef | None = None
    ) -> list[SchoolEvent]: ...


@dataclass
class PipelineResult:
    events: list[SchoolEvent]
    possible_duplicates: list[tuple[SchoolEvent, SchoolEvent]]
    low_confidence: list[SchoolEvent]


def run_pipeline(
    sources: list[Source],
    extractor: Extractor,
    *,
    confidence_threshold: float = 0.5,
    cache: ExtractionCache | None = None,
    force: bool = False,
) -> PipelineResult:
    # 抽出条件(モデル・プロンプト・ツール定義)の指紋。これが変わったキャッシュは使わない。
    fingerprint = getattr(extractor, "fingerprint", "")

    all_events: list[SchoolEvent] = []
    for source in sources:
        for doc in source.fetch():
            source_key = f"{doc.source.source_type}:{doc.source.source_id}"
            content_hash = ExtractionCache.content_hash(doc.text)

            if cache is not None and not force:
                cached_events = cache.get(source_key, content_hash, fingerprint)
                if cached_events is not None:
                    logger.info("Using cached extraction for %s (unchanged since last run)", doc.source.label)
                    for ev in cached_events:
                        ev.source = doc.source
                    all_events.extend(cached_events)
                    continue

            try:
                events = extractor.extract(doc.text, reference_date=doc.reference_date, source=doc.source)
            except Exception:
                logger.exception("Extraction failed for %s", doc.source.label)
                continue

            if cache is not None:
                cache.put(source_key, content_hash, events, fingerprint)
            all_events.extend(events)

    deduped = dedup_events(all_events)
    kept = [e for e in deduped if e.confidence >= confidence_threshold]
    low_confidence = [e for e in deduped if e.confidence < confidence_threshold]
    duplicates = find_possible_duplicates(kept)
    return PipelineResult(events=kept, possible_duplicates=duplicates, low_confidence=low_confidence)
