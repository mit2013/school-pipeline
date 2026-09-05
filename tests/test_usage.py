from __future__ import annotations

from datetime import date

from school_pipeline.extraction.llm_extractor import AnthropicExtractor
from school_pipeline.pipeline.usage import CreditLedger, UsageTotals, format_money


def _response(input_tokens: int, output_tokens: int, events: list | None = None) -> dict:
    """messages.create() のレスポンスを model_dump() した形を模したもの。"""
    return {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "record_events",
                "input": {"events": events if events is not None else []},
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


class FakeClient:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls = 0

    def create_message(self, *, system, user, tool_schema) -> dict:
        self.calls += 1
        return self._responses.pop(0)


def test_totals_accumulate_across_calls():
    totals = UsageTotals()
    totals.add_response(_response(1000, 200))
    totals.add_response(_response(1500, 300))

    assert totals.calls == 2
    assert totals.input_tokens == 2500
    assert totals.output_tokens == 500


def test_cost_uses_per_model_pricing():
    totals = UsageTotals(calls=1, input_tokens=1_000_000, output_tokens=1_000_000)

    # Sonnet 5 は $2 / $10 per 1M tokens。
    assert totals.cost_usd("claude-sonnet-5") == 12.0
    # Opus 5 は $5 / $25。
    assert totals.cost_usd("claude-opus-5") == 30.0


def test_cost_is_none_for_unknown_model():
    totals = UsageTotals(calls=1, input_tokens=1000, output_tokens=100)
    assert totals.cost_usd("some-future-model") is None


def test_cached_tokens_are_discounted():
    """キャッシュ読み出しは入力単価の0.1倍、書き込みは1.25倍で課金される。"""
    totals = UsageTotals(calls=1, cache_read_tokens=1_000_000)
    assert totals.cost_usd("claude-sonnet-5") == 0.20

    totals = UsageTotals(calls=1, cache_creation_tokens=1_000_000)
    assert totals.cost_usd("claude-sonnet-5") == 2.50


def test_extractor_records_usage_even_when_parsing_yields_nothing():
    """解析結果が空でも課金は発生しているので、使用量は必ず記録される。"""
    client = FakeClient([_response(2000, 400, events=[])])
    extractor = AnthropicExtractor(client)

    extractor.extract("なにかの本文", reference_date=date(2026, 9, 5))

    assert extractor.usage.calls == 1
    assert extractor.usage.input_tokens == 2000
    assert extractor.usage.output_tokens == 400


def test_extractor_starts_with_zero_usage():
    extractor = AnthropicExtractor(FakeClient([]))
    assert extractor.usage.calls == 0
    assert extractor.usage.cost_usd("claude-sonnet-5") == 0.0


def test_ledger_subtracts_spend_from_the_anchor(tmp_path):
    path = tmp_path / "usage.json"
    ledger = CreditLedger()
    ledger.set_anchor(3.49, on=date(2026, 9, 5))

    totals = UsageTotals(calls=7, input_tokens=16583, output_tokens=2880)
    ledger.record(0.0620, totals, "claude-sonnet-5")
    ledger.save(path)

    reloaded = CreditLedger.load(path)
    assert reloaded.anchor_usd == 3.49
    assert reloaded.anchor_date == "2026-09-05"
    assert abs(reloaded.estimated_remaining_usd - 3.428) < 1e-9
    assert len(reloaded.history) == 1


def test_ledger_reanchoring_resets_accumulated_spend():
    """Consoleの実残高で貼り直したら、それ以前の推定支出は捨てる。"""
    ledger = CreditLedger()
    ledger.set_anchor(5.00, on=date(2026, 9, 1))
    ledger.record(1.51, UsageTotals(calls=25), "claude-sonnet-5")
    assert abs(ledger.estimated_remaining_usd - 3.49) < 1e-9

    ledger.set_anchor(3.40, on=date(2026, 9, 5))
    assert ledger.spent_since_anchor_usd == 0.0
    assert ledger.estimated_remaining_usd == 3.40


def test_ledger_without_anchor_has_no_estimate():
    assert CreditLedger().estimated_remaining_usd is None


def test_ledger_load_of_missing_file_is_empty(tmp_path):
    ledger = CreditLedger.load(tmp_path / "does-not-exist.json")
    assert ledger.anchor_usd is None
    assert ledger.spent_since_anchor_usd == 0.0


def test_format_money_shows_both_currencies():
    assert format_money(0.062, 155.0) == "$0.0620 (約10円)"
