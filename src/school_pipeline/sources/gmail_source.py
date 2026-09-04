from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from ..models import SourceRef
from .base import RawDocument

logger = logging.getLogger(__name__)

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class GmailAccountConfig:
    name: str
    credentials_path: str
    token_path: str
    query: str = "newer_than:14d"
    max_results: int = 50


class GmailSource:
    """1つのGmailアカウントから検索条件に合致するメールを取得する。

    学校共通アドレス・専門コース用アドレスのように複数アカウントがある場合は、
    アカウントごとに GmailSource を作成してパイプラインに渡す。
    """

    def __init__(self, account: GmailAccountConfig, service=None) -> None:
        self._account = account
        self._service = service or _build_service(account)

    def fetch(self) -> Iterator[RawDocument]:
        service = self._service
        result = (
            service.users()
            .messages()
            .list(userId="me", q=self._account.query, maxResults=self._account.max_results)
            .execute()
        )
        for meta in result.get("messages", []):
            msg = service.users().messages().get(userId="me", id=meta["id"], format="full").execute()
            text = _extract_text(msg.get("payload", {}))
            if not text.strip():
                continue
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc).date()
            subject = headers.get("Subject", "(件名なし)")
            yield RawDocument(
                source=SourceRef(
                    source_type="gmail",
                    source_id=msg["id"],
                    label=subject,
                    account=self._account.name,
                    captured_at=received.isoformat(),
                ),
                text=f"件名: {subject}\n\n{text}",
                reference_date=received,
            )


def _build_service(account: GmailAccountConfig):
    from googleapiclient.discovery import build

    from ..auth.google_auth import get_credentials

    creds = get_credentials(account.credentials_path, account.token_path, _GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds)


def _extract_text(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime_type == "text/plain" and body.get("data"):
        return _b64decode(body["data"])
    if mime_type.startswith("multipart/"):
        parts = [_extract_text(part) for part in payload.get("parts", [])]
        return "\n".join(t for t in parts if t)
    if mime_type == "text/html" and body.get("data"):
        return _strip_html(_b64decode(body["data"]))
    return ""


def _b64decode(data: str) -> str:
    padded = data.replace("-", "+").replace("_", "/")
    return base64.b64decode(padded + "=" * (-len(padded) % 4)).decode("utf-8", errors="ignore")


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text
