import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import B24Token, Base


def test_b24_token_table_columns():
    cols = {c.name for c in B24Token.__table__.columns}
    for required in (
        "id", "member_id", "access_token", "refresh_token",
        "client_endpoint", "expires_at", "user_id", "scope",
    ):
        assert required in cols, f"missing column: {required}"


def test_b24_token_member_id_unique():
    assert B24Token.__table__.c.member_id.unique is True


@pytest.mark.asyncio
async def test_b24_token_creates_in_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "b24_tokens" in tables
    await engine.dispose()
