"""Apply P1 schema with a migration identity and create a non-owner runtime role."""

import asyncio
import os
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row


async def prepare():
    dsn = os.environ["P0_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        for role, login in (("p1_migrator", False), ("p1_runtime", True)):
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
                    ).format(sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN"))
                )
        connection.execute(
            sql.SQL("ALTER ROLE p1_runtime PASSWORD {}").format(
                sql.Literal(os.environ["P1_DB_PASSWORD"])
            )
        )
        with connection.transaction():
            connection.execute(Path("migrations/001_user_isolation.sql").read_text(), prepare=False)
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, row_factory=dict_row
    ) as connection:
        await connection.execute("SET ROLE p1_migrator")
        await connection.execute("SET search_path TO agent_checkpoints")
        saver = AsyncPostgresSaver(connection)
        await saver.setup()
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            await connection.execute(
                sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table))
            )
            await connection.execute(
                sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table))
            )
            await connection.execute(
                sql.SQL(
                    "CREATE POLICY private_checkpoints ON {table} USING(EXISTS("
                    "SELECT 1 FROM agent_business.checkpoint_threads t "
                    "WHERE t.thread_id={table}.thread_id AND ("
                    "current_setting('app.purge',true)='1' OR NOT EXISTS("
                    "SELECT 1 FROM agent_business.deleted_sessions d "
                    "WHERE d.session_id=t.session_id)))) "
                    "WITH CHECK(EXISTS(SELECT 1 FROM agent_business.checkpoint_threads t "
                    "WHERE t.thread_id={table}.thread_id AND NOT EXISTS("
                    "SELECT 1 FROM agent_business.deleted_sessions d "
                    "WHERE d.session_id=t.session_id)))"
                ).format(table=sql.Identifier(table))
            )
        await connection.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES "
            "IN SCHEMA agent_checkpoints TO p1_runtime"
        )
    print("P1 schema and non-owner runtime role ready")


if __name__ == "__main__":
    asyncio.run(prepare())
