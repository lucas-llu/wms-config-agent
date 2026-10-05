"""Existing Agent graph on owner-scoped PG business and checkpoint stores."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from threading import Lock

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from multiuser.scoped_checkpoints import UserPostgresSaver


class GuardedLLM:
    def __init__(self, delegate, guard):
        self.delegate, self.guard = delegate, guard

    def chat(self, messages, trace=None):
        self.guard()
        result = self.delegate.chat(messages, trace=trace)
        self.guard()
        return result


class GuardedKnowledge:
    def __init__(self, delegate, guard, workspace):
        self.delegate, self.guard, self.workspace = delegate, guard, workspace

    def search(self, *args, **kwargs):
        self.guard()
        kwargs["filters"] = self.workspace.filters(kwargs.get("filters", {}))
        result = self.delegate.search(*args, **kwargs)
        self.guard()
        evidence = tuple(e for e in result.evidence if self.workspace.permits_metadata(e.to_dict()))
        return replace(
            result,
            evidence=evidence,
            evidence_sufficient=result.evidence_sufficient and bool(evidence),
        )


class UserAgent:
    def __init__(self, store, dsn, llm, settings, knowledge=None):
        self.store, self.dsn, self.llm, self.settings, self.knowledge = (
            store,
            dsn,
            llm,
            settings,
            knowledge,
        )
        self.locks, self.lock = {}, Lock()

    @asynccontextmanager
    async def saver(self, context):
        async with AsyncConnectionPool(
            self.dsn,
            min_size=0,
            max_size=4,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        ) as pool:
            yield UserPostgresSaver(pool, context)

    def runner(self, context, repository):
        def guard():
            with self.store.transaction(context) as connection:
                repository._check_policy(connection)

        return RequirementSessionRunner(
            supervisor=Supervisor(
                llm=GuardedLLM(self.llm, guard),
                settings=self.settings,
                knowledge_adapter=GuardedKnowledge(self.knowledge, guard, repository.workspace)
                if self.knowledge
                else None,
                workspace=repository.workspace,
            ),
            sessions=SessionService(repository),
        )

    @staticmethod
    def result(result):
        return {
            "session": asdict(result.session),
            "revision": result.revision.revision,
            "status": result.state["status"],
            "message": result.state.get("assistant_reply", ""),
        }

    def start(self, context, repository, goal):
        async def run():
            async with self.saver(context) as saver:
                return await self.runner(context, repository).start(goal, checkpointer=saver)

        return self.result(asyncio.run(run()))

    def continue_session(self, context, repository, session_id, message):
        # P3 will replace this single-process safety guard with durable run leases.
        # Conversation revisions also use database optimistic protection.
        with self.lock:
            lock = self.locks.setdefault((context.user_id, session_id), Lock())
        if not lock.acquire(blocking=False):
            raise RuntimeError("Conversation is busy")
        try:

            async def run():
                async with self.saver(context) as saver:
                    return await self.runner(context, repository).continue_session(
                        session_id, message, checkpointer=saver
                    )

            return self.result(asyncio.run(run()))
        finally:
            lock.release()
