"""CI-only real identity/PG service with synthetic, no-cost model and knowledge."""

import json
import os
from dataclasses import replace
from pathlib import Path

from agents.contracts import Evidence
from agents.tools import KnowledgeSearchResult
from api.users import create_app
from core.settings import load_settings
from libs.llm import ChatResponse
from multiuser.access import AccessStore
from multiuser.account import AccountClient
from multiuser.agent import UserAgent
from multiuser.application import OwnedApplication
from multiuser.identity import OIDCVerifier
from multiuser.session_auth import TokenIntrospector


class SyntheticModel:
    def chat(self, messages, trace=None):
        text = messages[-1]["content"]
        if '"question"' in text and '"evidence"' in text:
            chinese = "Chinese" in text or "中文" in text
            payload = {
                "status": "answered",
                "gap": "",
                "claims": [
                    {
                        "text": "SYN_MODE 为可选项。" if chinese else "SYN_MODE is optional.",
                        "source_id": "1",
                        "quote": "SYN_MODE is optional.",
                    }
                ],
            }
        elif '"claims"' in text:
            payload = {"invalid": "synthetic review unavailable"}
        else:
            payload = {"intent": "atomic_query", "confidence": 1, "reason": "synthetic"}
        return ChatResponse(
            json.dumps(payload), model="synthetic", metadata={"usage": {"total_tokens": 12}}
        )


class SyntheticKnowledge:
    def search(self, query, *, filters, top_k=5, trace=None):
        source = Evidence(
            "e:synthetic",
            "c:synthetic",
            "synthetic.pdf",
            "SYN_MODE is optional.",
            1,
            collection=filters["collection"],
            module=filters["module"],
            site=filters["site"],
            environment=filters["environment"],
        )
        return KnowledgeSearchResult(query, filters, (source,), True, ())


def create_fixture_app():
    if os.getenv("WMS_P2_LIVE") != "1":
        raise RuntimeError("Synthetic backend requires the explicit disposable test job")
    issuer = os.environ["P0_OIDC_ISSUER"]
    store = AccessStore(os.environ["P1_POSTGRES_DSN"])
    if os.getenv("WMS_P4_BROWSER") == "1":
        from multiuser.usage import UsageService

        store.usage = UsageService(model="synthetic", provider_key="fixture:synthetic")
    settings = replace(load_settings().agent, export_root=Path("data/p2-exports"))
    application = OwnedApplication(
        store,
        export_root=settings.export_root,
        agent=UserAgent(
            store, os.environ["P1_POSTGRES_DSN"], SyntheticModel(), settings, SyntheticKnowledge()
        ),
    )
    app = create_app(
        OIDCVerifier(issuer, "wms-api", allow_local_http=True),
        TokenIntrospector(issuer, "wms-api", os.environ["P1_API_CLIENT_SECRET"]),
        application,
        account=AccountClient(issuer),
        allowed_origins=("http://127.0.0.1:5173",),
        web_config={"issuer": issuer, "client_id": "wms-workbench"},
        run_limits=fixture_run_limits(),
        durable_execution=os.getenv("WMS_P3_LIVE") == "1",
    )
    app.state.user_store = store
    return app


def fixture_run_limits():
    if os.getenv("WMS_P3_LIVE") == "1":
        from multiuser.runs import RunLimits

        return RunLimits()
    return None
