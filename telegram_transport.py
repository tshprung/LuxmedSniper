"""Telegram Bot API transport for the owner’s configured private chat."""

import requests


class TelegramError(Exception):
    def __init__(self, *, retry_after: int = 5, stop: bool = False):
        self.retry_after = retry_after
        self.stop = stop


class Telegram:
    def __init__(self, token: str, owner: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.owner = owner
        self.session = requests.Session()

    def call(self, method: str, data: dict):
        try:
            response = self.session.post(
                f"{self.base}/{method}", json=data, timeout=(5, 20), allow_redirects=False,
            )
            if response.status_code in (401, 403, 409):
                raise TelegramError(stop=True)
            body = response.json()
            if not isinstance(body, dict):
                raise TelegramError()
            if response.status_code == 429:
                parameters = body.get("parameters", {})
                delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                raise TelegramError(retry_after=delay if type(delay) is int and delay > 0 else 5)
            if response.status_code != 200 or body.get("ok") is not True or "result" not in body:
                raise TelegramError()
            return body["result"]
        except (requests.RequestException, ValueError):
            raise TelegramError() from None

    def poll(self, offset: int) -> list[dict]:
        updates = self.call("getUpdates", {
            "offset": offset, "timeout": 8, "allowed_updates": ["message", "callback_query"],
        })
        if not isinstance(updates, list):
            raise TelegramError()
        return updates

    def send(self, message: str, *, force_reply: bool = False,
             buttons: list[list[tuple[str, str]]] | None = None) -> int:
        payload = {"chat_id": self.owner, "text": message}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": action} for label, action in row] for row in buttons
                ],
            }
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True}
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError()
        return result["message_id"]

    def acknowledge(self, callback_id: str) -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id})
        except TelegramError as error:
            if error.stop:
                raise
