"""Async REST-клиент Bitrix24 поверх httpx."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Bitrix24Error(Exception):
    """Ошибка REST API Bitrix24 (поле error в ответе)."""

    def __init__(self, code: str, description: str = ""):
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")


class Bitrix24Client:
    """Async REST-клиент Bitrix24.

    Один экземпляр на портал (client_endpoint).
    auth_token передаётся в каждый call (управляется TokenManager'ом).
    """

    def __init__(self, client_endpoint: str, timeout: float = 30.0):
        if not client_endpoint.endswith("/"):
            client_endpoint += "/"
        self._endpoint = client_endpoint
        self._timeout = timeout

    async def call(
        self,
        method: str,
        auth_token: str,
        params: dict[str, Any] | None = None,
        method_http: str = "POST",
    ) -> Any:
        url = f"{self._endpoint}{method}.json"
        # Тело — JSON: B24 принимает application/json для всех .json-методов,
        # вложенные структуры (fields и т.п.) уходят нативными объектами.
        # Form-кодирование здесь не подходит: str(dict) даёт python-repr,
        # а JSON-строка в form-поле для crm.item.add отвергается (error 100,
        # найдено спайком на проде, план 003).
        body = {"auth": auth_token, **(params or {})}

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.request(method_http, url, json=body)

        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            raise Bitrix24Error(
                code=data["error"],
                description=data.get("error_description", ""),
            )
        return data.get("result")
