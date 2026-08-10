"""CRM-операции Bitrix24: поиск/создание контакта, создание сделки, timeline."""

from typing import Any

from app.b24.client import Bitrix24Client

# entityTypeId для универсального метода crm.item.add
ENTITY_LEAD = 1
ENTITY_DEAL = 2
ENTITY_CONTACT = 3
ENTITY_COMPANY = 4


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


class CrmService:
    """CRM-операции Bitrix24 поверх Bitrix24Client."""

    def __init__(self, client: Bitrix24Client):
        self._client = client

    async def find_contact_by_phone(
        self, auth_token: str, phone: str
    ) -> ContactInfo | None:
        """Поиск контакта по номеру телефона через crm.duplicate.findbyComm.

        Реальный ответ findbyComm — dict вида ``{"CONTACT": [ids], "LEAD": [...], ...}``.
        Для тестов допускается также список (контактов или ID).
        """
        result = await self._client.call(
            "crm.duplicate.findbyComm",
            auth_token=auth_token,
            params={"type": "PHONE", "values[]": [phone]},
        )
        if not result:
            return None

        contact_id = self._extract_contact_id(result)
        if not contact_id:
            return None

        # Достаём имя контакта через crm.contact.get.
        detail = await self._client.call(
            "crm.contact.get", auth_token=auth_token, params={"id": contact_id},
        )
        if detail is None:
            return ContactInfo(id=contact_id)
        name = (detail.get("NAME", "") + " " + detail.get("LAST_NAME", "")).strip() or None
        return ContactInfo(id=contact_id, name=name)

    @staticmethod
    def _extract_contact_id(result: Any) -> int | None:
        """Извлечь ID контакта из ответа findbyComm произвольной формы."""
        # Реальная форма: {"CONTACT": [275, 2297], "LEAD": [...]}
        if isinstance(result, dict):
            ids = result.get("CONTACT") or result.get("contact") or []
            return int(ids[0]) if ids else None
        # Тестовая/legacy форма: список контактов с полями ID/NAME.
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                raw = first.get("ID") or first.get("id") or 0
                return int(raw) if raw else None
            return int(first)  # список ID
        return None

    async def create_contact(
        self, auth_token: str, name: str, phone: str,
        assigned_by_id: int, source: str = "telegram",
    ) -> ContactInfo:
        fields: dict[str, Any] = {
            "NAME": name,
            "ASSIGNED_BY_ID": assigned_by_id,
            "SOURCE_ID": source.upper(),
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}],
        }
        result = await self._client.call(
            "crm.item.add",
            auth_token=auth_token,
            params={"entityTypeId": ENTITY_CONTACT, "fields": fields},
        )
        item = result.get("item", result) if isinstance(result, dict) else {}
        return ContactInfo(id=int(item.get("id", 0)), name=item.get("title"))

    async def create_deal(
        self, auth_token: str, title: str, contact_id: int, assigned_by_id: int,
    ) -> DealInfo:
        fields = {
            "TITLE": title,
            "CONTACT_ID": contact_id,
            "ASSIGNED_BY_ID": assigned_by_id,
            "OPENED": "Y",
        }
        result = await self._client.call(
            "crm.item.add",
            auth_token=auth_token,
            params={"entityTypeId": ENTITY_DEAL, "fields": fields},
        )
        item = result.get("item", result) if isinstance(result, dict) else {}
        return DealInfo(id=int(item.get("id", 0)), title=item.get("title"))

    async def add_timeline_comment(
        self, auth_token: str, entity_type: str, entity_id: int, comment: str,
    ) -> int:
        result = await self._client.call(
            "crm.timeline.comment.add",
            auth_token=auth_token,
            params={
                "fields": {
                    "ENTITY_TYPE": entity_type,
                    "ENTITY_ID": entity_id,
                    "COMMENT": comment,
                }
            },
        )
        return int(result)
