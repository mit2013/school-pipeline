from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# 100万トークンあたりの単価(USD)。https://platform.claude.com/docs/en/about-claude/pricing
# キャッシュ書き込みは入力の1.25倍、キャッシュ読み出しは0.1倍で課金される。
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


@dataclass
class UsageTotals:
    """1回の実行で発生したAPI使用量の合計。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def add_response(self, response: dict) -> None:
        """messages.create() のレスポンス(dict化済み)から使用量を積み上げる。"""
        usage = response.get("usage") or {}
        self.calls += 1
        self.input_tokens += usage.get("input_tokens") or 0
        self.output_tokens += usage.get("output_tokens") or 0
        self.cache_creation_tokens += usage.get("cache_creation_input_tokens") or 0
        self.cache_read_tokens += usage.get("cache_read_input_tokens") or 0

    def cost_usd(self, model: str) -> float | None:
        """このモデルでの概算費用(USD)。単価が未知のモデルなら None。"""
        price = _PRICE_PER_MTOK.get(model)
        if price is None:
            logger.warning("Unknown pricing for model %r; cost cannot be estimated.", model)
            return None
        price_in, price_out = price
        return (
            self.input_tokens * price_in
            + self.cache_creation_tokens * price_in * _CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * price_in * _CACHE_READ_MULTIPLIER
            + self.output_tokens * price_out
        ) / 1_000_000


@dataclass
class CreditLedger:
    """Consoleで確認した残高を起点に、それ以降の使用額を差し引いて残高を推定する。

    Anthropic は残高を返すAPIを公開していないため、正確な残高は Console
    (https://platform.claude.com/settings/billing) でしか確認できない。
    ここでは「最後に人が確認した残高」を基準点として保存し、そこからの
    支出を積み上げることで目安を出す。ズレてきたら再度同期すればよい。
    """

    anchor_usd: float | None = None
    anchor_date: str | None = None
    spent_since_anchor_usd: float = 0.0
    history: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> CreditLedger:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load usage ledger at %s: %s", p, exc)
            return cls()
        return cls(
            anchor_usd=data.get("anchor_usd"),
            anchor_date=data.get("anchor_date"),
            spent_since_anchor_usd=data.get("spent_since_anchor_usd", 0.0),
            history=data.get("history", []),
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "anchor_usd": self.anchor_usd,
                    "anchor_date": self.anchor_date,
                    "spent_since_anchor_usd": self.spent_since_anchor_usd,
                    # 直近の実行履歴。古いものから捨てて上限を設ける。
                    "history": self.history[-100:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def set_anchor(self, balance_usd: float, on: date | None = None) -> None:
        """Consoleで確認した残高で基準点を貼り直し、支出の積算をリセットする。"""
        self.anchor_usd = balance_usd
        self.anchor_date = (on or date.today()).isoformat()
        self.spent_since_anchor_usd = 0.0

    def record(self, cost_usd: float, totals: UsageTotals, model: str) -> None:
        if cost_usd <= 0:
            return
        self.spent_since_anchor_usd += cost_usd
        self.history.append(
            {
                "date": date.today().isoformat(),
                "model": model,
                "calls": totals.calls,
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "cost_usd": round(cost_usd, 6),
            }
        )

    @property
    def estimated_remaining_usd(self) -> float | None:
        if self.anchor_usd is None:
            return None
        return self.anchor_usd - self.spent_since_anchor_usd


def format_money(usd: float, jpy_per_usd: float) -> str:
    return f"${usd:.4f} (約{usd * jpy_per_usd:,.0f}円)"
