"""CRM-операции Bitrix24: поиск/создание контакта, создание сделки, timeline."""

from typing import Any

from app.b24.client import Bitrix24Client, Bitrix24Error

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

        contact_id = self._extract_contact_id(result)
        if not contact_id:
            return None

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

    async def create_contact(
        self,
        auth_token: str,
        name: str,
        phone: str,
        assigned_by_id: int,
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
        """
        fields: dict[str, Any] = {
            "NAME": first_name or name,
            "ASSIGNED_BY_ID": assigned_by_id,
        }
        if last_name:
            fields["LAST_NAME"] = last_name
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
        if username:
            fields["IM"] = [{"VALUE": username, "VALUE_TYPE": "TELEGRAM"}]
        # SOURCE_ID обязан существовать в справочнике портала; для каналов без
        # своего источника просто не передаём — B24 возьмёт дефолт.
        if source:
            fields["SOURCE_ID"] = source.upper()
        try:
            result = await self._client.call(
                "crm.contact.add",
                auth_token=auth_token,
                params={"fields": fields},
            )
        except Bitrix24Error as exc:
            # Источника может не быть в справочнике (например, MAX до запуска
            # scripts/add_max_source.py) — косметика не должна ронять создание
            # карточки: ретраим один раз без SOURCE_ID.
            if not source or "SOURCE" not in str(exc).upper():
                raise
            result = await self._client.call(
                "crm.contact.add",
                auth_token=auth_token,
                params={"fields": {k: v for k, v in fields.items() if k != "SOURCE_ID"}},
            )
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
        assigned_by_id: int,
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
