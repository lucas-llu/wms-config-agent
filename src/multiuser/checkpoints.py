"""PostgreSQL checkpointer compatibility adapter, isolated from business tables."""

import hashlib
import re
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row


def checkpoint_thread(identity_key: str, conversation_id: str) -> str:
    if not identity_key or not conversation_id:
        raise ValueError("Trusted identity and server-owned conversation ID are required")
    return "mu:" + hashlib.sha256(f"{identity_key}\0{conversation_id}".encode()).hexdigest()


@asynccontextmanager
async def open_postgres_checkpointer(dsn: str, *, setup=False, schema="p0_checkpoints"):
    if not re.fullmatch(r"p0_checkpoints(?:_[a-z0-9]{1,16})?", schema):
        raise ValueError("P0 checkpoint schema must be explicitly isolated")
    async with await AsyncConnection.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
    ) as connection:
        if setup:
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        saver = AsyncPostgresSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        if setup:
            await saver.setup()
        yield saver
