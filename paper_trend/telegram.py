"""Text-only Telegram delivery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Settings


class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = 3_900) -> list[str]:
    """Split on paragraph boundaries so one paper block usually stays together."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
        while len(current) > limit:
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class TelegramClient:
    token: str
    chat_id: str
    message_thread_id: str = ""
    timeout: int = 45

    @classmethod
    def from_settings(cls, settings: Settings) -> "TelegramClient":
        return cls(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout=settings.request_timeout,
        )

    def send_text(self, text: str) -> int:
        return self._send(text)

    def send_html(self, text: str) -> int:
        return self._send(text, parse_mode="HTML")

    def send_text_to(self, chat_id: str, text: str) -> int:
        return self._send(text, chat_id=chat_id)

    def send_html_to(self, chat_id: str, text: str) -> int:
        return self._send(text, parse_mode="HTML", chat_id=chat_id)

    def send_document_to(
        self,
        chat_id: str,
        path: str | Path,
        filename: str = "",
        caption: str = "",
    ) -> int:
        """Upload one private CSV/document to the specified subscriber."""
        document = Path(path)
        if not self.token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN과 대상 Chat ID가 필요합니다.")
        if not document.is_file():
            raise TelegramError(f"첨부할 파일이 없습니다: {document}")
        payload: dict[str, str] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
        if self.message_thread_id and chat_id == self.chat_id:
            payload["message_thread_id"] = self.message_thread_id
        try:
            with document.open("rb") as handle:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data=payload,
                    files={
                        "document": (
                            filename or document.name,
                            handle,
                            "text/csv" if document.suffix.lower() == ".csv" else "application/octet-stream",
                        )
                    },
                    timeout=self.timeout,
                )
            body = response.json()
        except (OSError, requests.RequestException, ValueError) as exc:
            raise TelegramError(
                f"Telegram 문서 전송 실패: {type(exc).__name__}"
            ) from exc
        if not response.ok or body.get("ok") is not True:
            raise TelegramError(
                f"Telegram API 오류: {body.get('description', response.status_code)}"
            )
        return 1

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN이 필요합니다.")
        params: dict[str, str | int] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params=params,
                timeout=max(self.timeout, timeout + 10),
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(
                f"Telegram 업데이트 수신 실패: {type(exc).__name__}"
            ) from exc
        if not response.ok or body.get("ok") is not True:
            raise TelegramError(
                f"Telegram API 오류: {body.get('description', response.status_code)}"
            )
        return list(body.get("result") or [])

    def set_commands(
        self, commands: list[tuple[str, str]], chat_id: str = ""
    ) -> None:
        payload: dict[str, str] = {
            "commands": json.dumps(
                [
                    {"command": command, "description": description}
                    for command, description in commands
                ],
                ensure_ascii=False,
            )
        }
        if chat_id:
            payload["scope"] = json.dumps({"type": "chat", "chat_id": chat_id})
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/setMyCommands",
                data=payload,
                timeout=self.timeout,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(
                f"Telegram 명령 목록 등록 실패: {type(exc).__name__}"
            ) from exc
        if not response.ok or body.get("ok") is not True:
            raise TelegramError(
                f"Telegram API 오류: {body.get('description', response.status_code)}"
            )

    def _send(self, text: str, parse_mode: str = "", chat_id: str = "") -> int:
        destination = chat_id or self.chat_id
        if not self.token or not destination:
            raise TelegramError("TELEGRAM_BOT_TOKEN과 대상 Chat ID가 필요합니다.")
        sent = 0
        for chunk in split_message(text):
            payload = {
                "chat_id": destination,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if self.message_thread_id:
                payload["message_thread_id"] = self.message_thread_id
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    data=payload,
                    timeout=self.timeout,
                )
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise TelegramError(
                    f"Telegram 전송 실패: {type(exc).__name__}"
                ) from exc
            if not response.ok or body.get("ok") is not True:
                raise TelegramError(
                    f"Telegram API 오류: {body.get('description', response.status_code)}"
                )
            sent += 1
        return sent
