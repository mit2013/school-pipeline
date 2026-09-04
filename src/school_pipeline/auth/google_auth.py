from __future__ import annotations

import os


def get_credentials(credentials_path: str, token_path: str, scopes: list[str]):
    """OAuth認証を行い(または保存済みトークンを再利用/更新して)Credentialsを返す。

    初回はブラウザでの認可フローが起動し、token_path にリフレッシュトークンを
    含むトークンを保存する。以降はそのトークンを再利用し、期限切れ時は自動更新する。
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    return creds
