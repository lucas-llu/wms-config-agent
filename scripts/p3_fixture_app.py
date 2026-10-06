"""Real isolated P3 worker/executor fixture; deliberately no model API key."""

import os

from api.execution import create_execution_app
from core.settings import load_settings
from multiuser.access import AccessStore
from multiuser.agent import UserAgent
from multiuser.control import RunControl
from multiuser.executor import RunExecutor
from multiuser.governor import ModelGovernor, ModelLimits
from multiuser.session_authority import SessionAuthority
from scripts.p2_fixture_app import SyntheticKnowledge, SyntheticModel
from workers.runs import redis_client


def create_fixture_executor():
    if os.getenv("WMS_P3_LIVE") != "1":
        raise RuntimeError("P3 execution fixture requires explicitly isolated test mode")
    store = AccessStore(os.environ["P1_POSTGRES_DSN"])
    control = RunControl(os.environ["P3_CONTROL_DSN"])
    authority = SessionAuthority(
        os.environ["P0_OIDC_ISSUER"],
        "wms-session-reader",
        os.environ["P3_SESSION_CLIENT_SECRET"],
        allow_local_http=True,
    )
    governor = ModelGovernor(control, "fixture:synthetic", limits=ModelLimits(tpm=1000000))
    agent = UserAgent(
        store,
        os.environ["P1_POSTGRES_DSN"],
        SyntheticModel(),
        load_settings().agent,
        SyntheticKnowledge(),
    )
    executor = RunExecutor(store, control, authority, governor, agent)
    return create_execution_app(executor, redis_client(), os.environ["WMS_EXECUTION_TOKEN"])
