"""Short transaction-local user contexts around every checkpoint operation."""

from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

from multiuser.access import AccessDenied, context_values


class UserPostgresSaver(AsyncPostgresSaver):
    def __init__(self, pool, context):
        self.context = context
        super().__init__(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))

    async def setup(self):
        raise PermissionError("Checkpoint migrations must not run as a request identity")

    @asynccontextmanager
    async def _cursor(self, *, pipeline=False):
        # Inherited implementations enter this context for reads and writes.
        # No connection/transaction is held during the model's thinking time.
        async with self.lock, self.conn.connection() as connection, connection.transaction():
            await connection.execute(
                "SET LOCAL search_path TO agent_checkpoints,pg_catalog,"
                "agent_business,identity_business"
            )
            for name, value in context_values(self.context).items():
                await connection.execute("SELECT set_config(%s,%s,true)", (name, value))
            cursor = await connection.execute("SELECT identity_business.actor_active() AS active")
            if not (await cursor.fetchone())["active"]:
                raise AccessDenied("Account or session is unavailable")
            async with connection.cursor(binary=True, row_factory=dict_row) as cursor:
                yield cursor
