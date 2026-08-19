"""Async REST-клиент Bitrix24 поверх httpx."""

import asyncio
import logging
import time
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

    Один экземпляр на портал (client_endpoint). auth_token передаётся в
    каждый call (управляется TokenManager'ом).

    Общий ``httpx.AsyncClient`` на экземпляр (переиспользуем TLS-сессии
    вместо нового коннекта на каждый вызов) — закрывается через ``aclose()``
    при остановке процесса.

    Глобальный per-process throttle: между вызовами выдерживаем
    ``min_interval`` секунд (free-порталы режут ~2 rps, превышение даёт
    QUERY_LIMIT_EXCEEDED). На QUERY_LIMIT_EXCEEDED — один повторный вызов
    после паузы 1.5с; повторное превышение — обычная ``Bitrix24Error``.
    """

    def __init__(
        self,
        client_endpoint: str,
        timeout: float = 30.0,
        min_interval: float = 0.6,
    ):
        if not client_endpoint.endswith("/"):
            client_endpoint += "/"
        self._endpoint = client_endpoint
        self._timeout = timeout
        self._min_interval = min_interval
        # Общий HTTP-клиент: пул коннектов живёт столько же, сколько клиент.
        self._http = httpx.AsyncClient(timeout=timeout)
        # Глобальный throttle: момент последнего вызова + блокировка, чтобы
        # конкурентные корутины выстроились в очередь минимум-интервалов.
        self._last_call = 0.0
        self._interval_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        """Выдержать минимальный интервал между вызовами (под блокировкой)."""
        async with self._interval_lock:
            wait = self._last_call + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

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

        await self._throttle()
        resp = await self._http.request(method_http, url, json=body)
        data = self._decode(resp, method)

        if isinstance(data, dict) and data.get("error") == "QUERY_LIMIT_EXCEEDED":
            # Free-портал жёстко режет частоту (~2 rps). Одна повторная
            # попытка после паузы: секунда-полторы — и лимит снова открыт.
            logger.warning("QUERY_LIMIT_EXCEEDED on %s — retrying once", method)
            await asyncio.sleep(1.5)
            await self._throttle()
            resp = await self._http.request(method_http, url, json=body)
            data = self._decode(resp, method)

        if isinstance(data, dict) and "error" in data:
            raise Bitrix24Error(
                code=data["error"],
                description=data.get("error_description", ""),
            )
        return data.get("result")

    @staticmethod
    def _decode(resp: httpx.Response, method: str) -> Any:
        """REST-методы отвечают JSON; всё остальное (HTML-заглушка домена,
        страница логина, прокси-ошибка) — превращаем в Bitrix24Error, а не
        в голый JSONDecodeError с 500 без объяснения (живой кейс: перенос
        стенда, токен ещё указывает на мёртвый портал)."""
        try:
            return resp.json()
        except ValueError:
            raise Bitrix24Error(
                code="invalid_response",
                description=(
                    f"{method}: HTTP {resp.status_code}, ответ не JSON "
                    "(портал недоступен или токен от прежнего портала — "
                    "переустановите приложение)"
                ),
            ) from None

    async def aclose(self) -> None:
        """Закрыть общий HTTP-клиент (вызывать при остановке процесса)."""
        await self._http.aclose()
