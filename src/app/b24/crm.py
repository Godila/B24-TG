"""CRM-операции Bitrix24: контакты, сделки, лиды, timeline."""

from typing import Any

from app.b24.client import Bitrix24Client, Bitrix24Error

# entityTypeId для универсального метода crm.item.add. Лид (1) сюда НЕ
# входит: item.add молча теряет PHONE/IM — лиды ходят классическим
# crm.lead.* (см. create_lead).
ENTITY_DEAL = 2
ENTITY_CONTACT = 3


class ContactInfo:
    """Результат поиска/создания контакта."""

    __slots__ = ("id", "name")

    def __init__(self, id: int, name: str | None = None):
        self.id = id
        self.name = name


class DealInfo:
    """Результат создания сделки."""

    __slots__ = ("id", "title")

    def __init__(self, id: int, title: str | None = None):
        self.id = id
        self.title = title


class LeadInfo:
    """Результат поиска/чтения лида (режим crm_mode=lead).

    status_id/contact_id заполняются только из crm.lead.get — по ним
    оркестрация понимает, что лид сконвертирован и где его контакт.
    """

    __slots__ = ("contact_id", "id", "status_id")

    def __init__(
        self,
        id: int,
        status_id: str | None = None,
        contact_id: int | None = None,
    ):
        self.id = id
        self.status_id = status_id
        self.contact_id = contact_id


def _contact_display_name(detail: dict) -> str | None:
    """Имя контакта из ответа crm.contact.get: NAME + LAST_NAME.

    ГРАБЛЯ (поймана в проде 2026-08-17): B24 для незаполненного поля
    возвращает явный JSON-null («LAST_NAME»: null) — ``dict.get(key, "")``
    даёт пустую строку только при ОТСУТСТВИИ ключа, а null проходит
    как None и ронял конкатенацию «str + None». Пропускаем пустые части.
    """
    return " ".join(p for p in (detail.get("NAME"), detail.get("LAST_NAME")) if p).strip() or None


class CrmService:
    """CRM-операции Bitrix24 поверх Bitrix24Client."""

    def __init__(self, client: Bitrix24Client):
        self._client = client

    async def find_contact_by_phone(self, auth_token: str, phone: str) -> ContactInfo | None:
        """Поиск контакта по номеру телефона через crm.duplicate.findbyComm.

        Реальный ответ findbyComm — dict вида ``{"CONTACT": [ids], "LEAD": [...], ...}``.
        Для тестов допускается также список (контактов или ID).
        """
        result = await self._client.call(
            "crm.duplicate.findbyComm",
            auth_token=auth_token,
            params={"type": "PHONE", "values": [phone]},
        )
        if not result:
            return None

        ids = self._extract_entity_ids(result, "CONTACT")
        if not ids:
            return None
        contact_id = ids[0]

        # Достаём имя контакта через crm.contact.get.
        detail = await self._client.call(
            "crm.contact.get",
            auth_token=auth_token,
            params={"id": contact_id},
        )
        if detail is None:
            return ContactInfo(id=contact_id)
        return ContactInfo(id=contact_id, name=_contact_display_name(detail))

    @staticmethod
    def _extract_entity_ids(result: Any, key: str) -> list[int]:
        """ID-шники сущности из ответа findbyComm (fail-closed: мусор мимо)."""
        # Реальная форма: {"CONTACT": [275, 2297], "LEAD": [...]}
        if isinstance(result, dict):
            raw = result.get(key.upper()) or result.get(key.lower()) or []
            ids: list[int] = []
            for v in raw:
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):
                    continue
            return ids
        # Тестовая/legacy форма: список контактов с полями ID/NAME.
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                raw = first.get("ID") or first.get("id") or 0
                return [int(raw)] if raw else []
            return [int(first)]  # список ID
        return []

    async def get_contact(self, auth_token: str, contact_id: int) -> ContactInfo | None:
        """Контакт по id (для проверки существующей CRM-связки).

        None — контакт удалён или недоступен (вызывающий ищет по телефону).
        """
        try:
            detail = await self._client.call(
                "crm.contact.get",
                auth_token=auth_token,
                params={"id": contact_id},
            )
        except Exception:  # noqa: BLE001 - контакт мог быть удалён в B24
            return None
        if not isinstance(detail, dict):
            return None
        return ContactInfo(id=contact_id, name=_contact_display_name(detail))

    async def _add_with_source_fallback(
        self,
        auth_token: str,
        method: str,
        fields: dict[str, Any],
        source: str | None,
        *,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """crm.*.add с SOURCE_ID и одним тихим ретраем без него.

        Источника может не быть в справочнике портала (например, MAX до
        запуска scripts/add_max_source.py) — косметика не должна ронять
        создание карточки.
        """
        if source:
            fields = {**fields, "SOURCE_ID": source.upper()}
        params: dict[str, Any] = {"fields": fields}
        if extra_params:
            params.update(extra_params)
        try:
            return await self._client.call(method, auth_token=auth_token, params=params)
        except Bitrix24Error as exc:
            if not source or "SOURCE" not in str(exc).upper():
                raise
            params["fields"] = {k: v for k, v in fields.items() if k != "SOURCE_ID"}
            return await self._client.call(method, auth_token=auth_token, params=params)

    async def create_contact(
        self,
        auth_token: str,
        name: str,
        phone: str,
        assigned_by_id: int | None,
        source: str | None = "telegram",
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> ContactInfo:
        """Создать контакт (классический crm.contact.add).

        НЕ crm.item.add: универсальный метод молча выбрасывает мульти-поля
        PHONE/IM (проверено на проде — контакт создавался с HAS_PHONE=N),
        из-за этого же не работал дедуп findbyComm по телефону. ``name`` —
        отображаемое имя целиком; при наличии раздельных first/last пишем их
        в NAME/LAST_NAME (каноника CRM), иначе всё в NAME.
        ``assigned_by_id=None`` (общий номер без ответственного) — ключ не
        передаём, ответственного назначит B24 по правилам портала.
        """
        fields: dict[str, Any] = {"NAME": first_name or name}
        if assigned_by_id is not None:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        if last_name:
            fields["LAST_NAME"] = last_name
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
        if username:
            fields["IM"] = [{"VALUE": username, "VALUE_TYPE": "TELEGRAM"}]
        # SOURCE_ID обязан существовать в справочнике портала; для каналов без
        # своего источника просто не передаём — B24 возьмёт дефолт.
        result = await self._add_with_source_fallback(auth_token, "crm.contact.add", fields, source)
        # crm.contact.add возвращает id напрямую (не {"item": {...}}).
        return ContactInfo(
            id=int(result),
            name=" ".join(p for p in (first_name, last_name) if p) or name,
        )

    async def find_open_deal_for_contact(self, auth_token: str, contact_id: int) -> DealInfo | None:
        """Найти ОТКРЫТУЮ сделку контакта (новейшую по id).

        Идемпотентность process_inbound: существующий клиент, у которого уже
        есть сделка, не должен получать deal_id=None навсегда — комментарий
        должен попадать в его открытую сделку, а не в карточку контакта.
        """
        result = await self._client.call(
            "crm.item.list",
            auth_token=auth_token,
            params={
                "entityTypeId": ENTITY_DEAL,
                "filter": {"CONTACT_ID": contact_id, "CLOSED": "N"},
                "order": {"id": "desc"},
                "select": ["id", "title"],
            },
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            return None
        it = items[0]
        return DealInfo(id=int(it["id"]), title=it.get("title"))

    async def create_deal(
        self,
        auth_token: str,
        title: str,
        contact_id: int,
        assigned_by_id: int | None,
        source: str | None = None,
    ) -> DealInfo:
        fields: dict[str, Any] = {
            "TITLE": title,
            "CONTACT_ID": contact_id,
            "OPENED": "Y",
        }
        if assigned_by_id is not None:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        result = await self._add_with_source_fallback(
            auth_token,
            "crm.item.add",
            fields,
            source,
            extra_params={"entityTypeId": ENTITY_DEAL},
        )
        item = result.get("item", result) if isinstance(result, dict) else {}
        return DealInfo(id=int(item.get("id", 0)), title=item.get("title"))

    async def find_reusable_lead_by_phone(self, auth_token: str, phone: str) -> LeadInfo | None:
        """Пригодный для продолжения лид по телефону (crm_mode=lead).

        findbyComm отдаёт LEAD-иды без статусов — вторым запросом crm.lead.list
        берём новейший не-конвертированный/не-мусорный. Пустой телефон (MAX) —
        None без вызовов: матчинг ненадёжнее дублей.
        """
        if not phone:
            return None
        result = await self._client.call(
            "crm.duplicate.findbyComm",
            auth_token=auth_token,
            params={"type": "PHONE", "values": [phone]},
        )
        ids = self._extract_entity_ids(result, "LEAD")
        if not ids:
            return None
        items = await self._client.call(
            "crm.lead.list",
            auth_token=auth_token,
            params={
                # CONVERTED/JUNK — системные литералы, не зависят от локали портала.
                "filter": {"ID": ids, "!@STATUS_ID": ["CONVERTED", "JUNK"]},
                "order": {"ID": "desc"},
                "select": ["ID"],
            },
        )
        items = items if isinstance(items, list) else []
        if not items:
            return None
        try:
            return LeadInfo(id=int(items[0]["ID"]))
        except (KeyError, TypeError, ValueError):
            return None  # мусорный ответ — считаем «лида нет»

    async def get_lead(self, auth_token: str, lead_id: int) -> LeadInfo | None:
        """Лид по id (проверка живой привязки диалога). None — удалён/недоступен."""
        try:
            detail = await self._client.call(
                "crm.lead.get",
                auth_token=auth_token,
                params={"id": lead_id},
            )
        except Exception:  # noqa: BLE001 - лид мог быть удалён в B24
            return None
        if not isinstance(detail, dict):
            return None
        contact_raw = detail.get("CONTACT_ID")  # бывает "", "42", int или null
        try:
            contact_id = int(contact_raw) if contact_raw not in (None, "") else None
        except (TypeError, ValueError):
            contact_id = None
        status = detail.get("STATUS_ID")
        return LeadInfo(
            id=lead_id,
            status_id=str(status).upper() if status else None,
            contact_id=contact_id,
        )

    async def create_lead(
        self,
        auth_token: str,
        *,
        title: str,
        phone: str,
        assigned_by_id: int | None,
        source: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> LeadInfo:
        """Создать лид (классический crm.lead.add, режим «Лиды»).

        НЕ crm.item.add: универсальный метод молча выбрасывает мульти-поля
        PHONE/IM (та же прод-грабля, что у контактов). ``title`` — имя
        клиента: канал уходит в SOURCE_ID, не в название; при наличии
        раздельных first/last пишем их в NAME/LAST_NAME лида.
        """
        fields: dict[str, Any] = {"TITLE": title, "OPENED": "Y", "NAME": first_name or title}
        if last_name:
            fields["LAST_NAME"] = last_name
        if assigned_by_id is not None:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
        if username:
            fields["IM"] = [{"VALUE": username, "VALUE_TYPE": "TELEGRAM"}]
        result = await self._add_with_source_fallback(auth_token, "crm.lead.add", fields, source)
        # crm.lead.add возвращает id напрямую (не {"item": {...}}).
        return LeadInfo(id=int(result))

    async def add_timeline_comment(
        self,
        auth_token: str,
        entity_type: str,
        entity_id: int,
        comment: str,
        files: list[tuple[str, str]] | None = None,
    ) -> int:
        """Комментарий в таймлайн CRM; ``files`` — [(имя, base64)] вложений.

        FILES у crm.timeline.comment.add принимает пары [имя, контент]:
        файл попадает прямо в карточку (сделки/контакта), а не только
        текст-метка. None/пустой список — прежнее текстовое поведение.
        """
        fields: dict[str, Any] = {
            "ENTITY_TYPE": entity_type,
            "ENTITY_ID": entity_id,
            "COMMENT": comment,
        }
        if files:
            fields["FILES"] = [[name, content] for name, content in files]
        result = await self._client.call(
            "crm.timeline.comment.add",
            auth_token=auth_token,
            params={"fields": fields},
        )
        return int(result)
