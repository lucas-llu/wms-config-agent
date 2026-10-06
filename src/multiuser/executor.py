"""Shared bounded execution service: durable guards, graph takeover and atomic result."""

import asyncio
import copy
import threading
from contextlib import contextmanager, suppress

from agents.repositories import SessionNotFoundError
from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from multiuser.access import AccessDenied
from multiuser.agent import GuardedKnowledge
from multiuser.governor import GovernedLLM
from multiuser.identity import IdentityUnavailable
from multiuser.run_repository import RunRepository
from multiuser.runs import TERMINAL, LostLease
from multiuser.staged_session import StagedSessionRepository


async def copy_checkpoint(saver, source, target):
    if source == target:
        raise ValueError("Distinct generation checkpoint threads required")
    item = await saver.aget_tuple({"configurable": {"thread_id": source, "checkpoint_ns": ""}})
    if item is None:
        return False
    checkpoint = copy.deepcopy(item.checkpoint)
    config = {"configurable": {"thread_id": target, "checkpoint_ns": ""}}
    config = await saver.aput(config, checkpoint, item.metadata, checkpoint["channel_versions"])
    pending = {}
    for task, channel, value in item.pending_writes or []:
        pending.setdefault(task, []).append((channel, value))
    for task, writes in pending.items():
        await saver.aput_writes(config, writes, task)
    return True


class RunExecutor:
    def __init__(
        self, store, control, authority, governor, agent, *, capacity=4, retrieval_governor=None
    ):
        self.store, self.control, self.authority, self.governor, self.agent = (
            store,
            control,
            authority,
            governor,
            agent,
        )
        if type(capacity) is not int or not 1 <= capacity <= 20:
            raise ValueError("Bounded execution capacity required")
        self.capacity = threading.BoundedSemaphore(capacity)
        self.retrieval = threading.BoundedSemaphore(capacity)
        self.retrieval_governor = retrieval_governor
        self.review_capacity = threading.BoundedSemaphore(1)

    def repository(self, row):
        context = self.control.context(row)
        self.authority.check(context)
        workspace = self.store.workspace_for(context, row["conversation_id"])
        repository = RunRepository(self.store, context, workspace, limits=self.control.limits)
        # workspace_for also serves recycle-bin operations; execution must require a LIVE parent.
        repository.sessions.get_session(row["conversation_id"])
        return repository

    @contextmanager
    def heartbeat(self, repository, lease):
        ended, errors = threading.Event(), []

        def beat():
            while not ended.wait(max(1, repository.limits.lease_seconds / 3)):
                try:
                    self.authority.check(repository.context)
                    repository.heartbeat(lease)
                except Exception as exc:
                    errors.append(exc)
                    return

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:

            def guard():
                if errors:
                    raise errors[0]
                self.authority.check(repository.context)
                repository.execution(lease)

            yield guard
        finally:
            ended.set()
            thread.join(timeout=6)

    def execute(self, run_id):
        if not self.capacity.acquire(blocking=False):
            return {"accepted": False, "reason": "execution_busy"}
        row, repository, lease = None, None, None
        reviewing = False
        try:
            row = self.control.load(run_id)
            if row is None or row["status"] in TERMINAL:
                return {"accepted": False}
            if row["answer_strategy"] == "review":
                if not self.review_capacity.acquire(blocking=False):
                    return {"accepted": False, "reason": "review_execution_busy"}
                reviewing = True
            repository = self.repository(row)
            lease = repository.claim(run_id)
            if lease is None:
                return {"accepted": False}
            with self.heartbeat(repository, lease) as guard:
                staged = StagedSessionRepository(repository.sessions, lease.conversation_id, run_id)
                llm = GovernedLLM(
                    self.agent.llm,
                    self.governor,
                    repository,
                    lease,
                    guard,
                    review=repository.execution(lease)["answer_strategy"] == "review",
                )
                knowledge = SharedKnowledge(
                    self.agent.knowledge,
                    guard,
                    staged.workspace,
                    self.retrieval,
                    repository,
                    lease,
                    self.retrieval_governor,
                )
                runner = RequirementSessionRunner(
                    supervisor=Supervisor(
                        llm=llm,
                        settings=self.agent.settings,
                        knowledge_adapter=knowledge,
                        workspace=staged.workspace,
                    ),
                    sessions=SessionService(staged),
                )

                async def run():
                    # Pool is loop-scoped and bounded; model/index objects remain shared.
                    async with self.agent.saver(repository.context, lease=lease) as saver:
                        current = repository.execution(lease)
                        with repository.store.transaction(repository.context) as connection:
                            if not connection.execute(
                                "SELECT 1 FROM agent_business.checkpoint_threads "
                                "WHERE thread_id=%s AND session_id=%s",
                                (current["source_thread"], lease.conversation_id),
                            ).fetchone():
                                raise RuntimeError("Checkpoint source conversation mismatch")
                        copied = await copy_checkpoint(
                            saver, current["source_thread"], lease.checkpoint_thread
                        )
                        guard()
                        config = {"configurable": {"thread_id": lease.checkpoint_thread}}
                        if current["resume_ready"]:
                            if not copied:
                                raise RuntimeError("Trusted recovery checkpoint is missing")
                            staged.user_turn(current["message"])
                            graph = runner.supervisor.compile(saver)
                            state = await graph.aget_state(config)
                            if state.values.get("latest_turn_id") != staged.pending[0].turn_id:
                                return await runner.continue_session(
                                    lease.conversation_id,
                                    current["message"],
                                    checkpointer=saver,
                                    answer_strategy=current["answer_strategy"],
                                    checkpoint_thread_id=lease.checkpoint_thread,
                                )
                            if state.next and not state.interrupts:
                                await runner._run_graph(
                                    graph, None, config, lease.conversation_id, staged.base_revision
                                )
                            return await runner._persist_result(graph, config, staged.base_revision)
                        # Flag is set only after a checkpoint exists, by the saver below.
                        if copied:
                            result = await runner.continue_session(
                                lease.conversation_id,
                                current["message"],
                                checkpointer=saver,
                                answer_strategy=current["answer_strategy"],
                                checkpoint_thread_id=lease.checkpoint_thread,
                            )
                        else:
                            result = await runner.start(
                                current["message"],
                                session_id=lease.conversation_id,
                                checkpointer=saver,
                                answer_strategy=current["answer_strategy"],
                                checkpoint_thread_id=lease.checkpoint_thread,
                            )
                        return result

                asyncio.run(run())
                guard()
                repository.progress(lease, "persisting")
                repository.complete(lease, staged.persist)
                return {"accepted": True, "committed": True}
        except LostLease:
            if repository and lease:
                with suppress(AccessDenied, SessionNotFoundError):
                    repository.acknowledge_cancel(lease)
            return {"accepted": False, "reason": "lost_lease"}
        except IdentityUnavailable:
            # Do not discard a valid checkpoint during an IdP outage. Lease expiry drives retry.
            return {"accepted": False, "reason": "identity_unavailable"}
        except (AccessDenied, SessionNotFoundError):
            if row:
                self.control.stop(
                    run_id, lease.epoch if lease else row["epoch"], "authorization_required"
                )
            return {"accepted": False, "reason": "authorization_required"}
        except Exception as exc:
            if lease:
                try:
                    current = repository.execution(lease)
                    self.control.stop(
                        run_id,
                        lease.epoch,
                        "uncertain" if current["open_model_calls"] else "failed",
                    )
                except (LostLease, AccessDenied, SessionNotFoundError):
                    pass
            return {
                "accepted": False,
                "reason": "execution_failed",
                "error_type": type(exc).__name__,
            }
        finally:
            if reviewing:
                self.review_capacity.release()
            self.capacity.release()

    def reconcile(self):
        for row in self.control.expired():
            try:
                repository = self.repository(row)
                repository.recover()
                current = self.control.load(row["run_id"])
                if current["status"] == "recovery_required" and not repository.take_over(
                    row["run_id"]
                ):
                    self.control.stop(row["run_id"], current["epoch"], "failed")
            except (AccessDenied, SessionNotFoundError):
                self.control.stop(row["run_id"], row["epoch"], "authorization_required")
            except IdentityUnavailable:
                continue


class SharedKnowledge(GuardedKnowledge):
    def __init__(self, delegate, guard, workspace, semaphore, repository, lease, governor=None):
        super().__init__(delegate, guard, workspace)
        self.semaphore, self.repository, self.lease = semaphore, repository, lease
        self.governor = governor

    def search(self, *args, **kwargs):
        permit = self.governor.acquire(1, self.guard) if self.governor else None
        acquired = False
        try:
            while not self.semaphore.acquire(timeout=0.2):
                self.guard()
            acquired = True
            self.repository.progress(self.lease, "retrieving")
            return super().search(*args, **kwargs)
        finally:
            if acquired:
                self.semaphore.release()
            if permit:
                self.governor.release(permit)
