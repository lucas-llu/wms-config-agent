"""Real isolated P3 worker/executor fixture; deliberately no model API key."""

import os
import time
from pathlib import Path

from api.execution import create_execution_app
from core.settings import load_settings
from multiuser.access import AccessStore
from multiuser.agent import UserAgent
from multiuser.control import RunControl
from multiuser.executor import RunExecutor
from multiuser.governor import ModelGovernor, ModelLimits
from multiuser.runs import RunLimits
from multiuser.session_authority import SessionAuthority
from scripts.p2_fixture_app import SyntheticKnowledge, SyntheticModel
from workers.runs import redis_client


def create_fixture_executor():
    if os.getenv("WMS_P3_LIVE") != "1":
        raise RuntimeError("P3 execution fixture requires explicitly isolated test mode")
    store = AccessStore(os.environ["P1_POSTGRES_DSN"])
    control = RunControl(
        os.environ["P3_CONTROL_DSN"],
        limits=RunLimits(
            lease_seconds=int(os.getenv("P3_FIXTURE_LEASE", "45")),
        ),
    )
    if os.getenv("WMS_P4_BROWSER") == "1":
        from multiuser.usage import UsageService

        store.usage = UsageService(
            model="synthetic", provider_key="fixture:synthetic", control=control
        )
    authority = SessionAuthority(
        os.environ["P0_OIDC_ISSUER"],
        "wms-session-reader",
        os.environ["P3_SESSION_CLIENT_SECRET"],
        allow_local_http=True,
    )
    governor = FixtureGovernor(
        control,
        os.getenv("P3_FIXTURE_MODEL_KEY", "fixture:synthetic"),
        limits=ModelLimits(tpm=1000000),
    )
    agent = UserAgent(
        store,
        os.environ["P1_POSTGRES_DSN"],
        FixtureModel(),
        load_settings().agent,
        SyntheticKnowledge(),
    )
    retrieval = ModelGovernor(
        control,
        "fixture:retrieval",
        limits=ModelLimits(
            inflight=4,
            review_inflight=0,
            rpm=100000,
            tpm=1000000,
            retries=0,
        ),
    )
    executor = RunExecutor(store, control, authority, governor, agent, retrieval_governor=retrieval)
    return create_execution_app(executor, redis_client(), os.environ["WMS_EXECUTION_TOKEN"])


def pause_fixture(boundary):
    if os.getenv("P3_FAULT_BOUNDARY") == boundary:
        Path(os.environ["P3_FAULT_FLAG"]).touch()
        time.sleep(30)  # Synthetic fault only: parent test kills this process before it returns.


class FixtureModel(SyntheticModel):
    def chat(self, messages, trace=None):
        pause_fixture("inflight")
        return super().chat(messages, trace)


class FixtureGovernor(ModelGovernor):
    def release(self, permit, *, actual=None):
        super().release(permit, actual=actual)
        pause_fixture("after_return")
