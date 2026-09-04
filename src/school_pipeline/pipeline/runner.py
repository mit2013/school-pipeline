from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..models import SchoolEvent, SourceRef
from ..sources.base import Source
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
) -> PipelineResult:
    all_events: list[SchoolEvent] = []
    for source in sources:
        for doc in source.fetch():
            try:
                events = extractor.extract(doc.text, reference_date=doc.reference_date, source=doc.source)
            except Exception:
                logger.exception("Extraction failed for %s", doc.source.label)
                continue
            all_events.extend(events)

    deduped = dedup_events(all_events)
    kept = [e for e in deduped if e.confidence >= confidence_threshold]
    low_confidence = [e for e in deduped if e.confidence < confidence_threshold]
    duplicates = find_possible_duplicates(kept)
    return PipelineResult(events=kept, possible_duplicates=duplicates, low_confidence=low_confidence)
