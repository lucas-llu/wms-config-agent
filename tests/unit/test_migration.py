import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from agents.repositories import SessionRepository
from agents.repositories.feedback_repository import FeedbackRepository
from agents.workspace import Workspace
from libs.llm import ChatResponse
from multiuser.migration import file_digest, load_snapshot, project, read_database, snapshot
from multiuser.pressure import ProbeBudget, percentile, probe_supplier
from multiuser.release import preserve_post_cutover


def legacy(tmp_path, *, scoped=True):
    repository = SessionRepository(tmp_path / "source.sqlite")
    sid = "session:" + uuid.uuid4().hex
    repository.create_session(session_id=sid, goal="Private synthetic migration")
    citation = {"source": "synthetic.pdf", "excerpt": "PRIVATE synthetic citation"}
    if scoped:
        citation.update(collection="fixture", module="inbound", site="DC01", environment="test")
    repository.append_turn(
        session_id=sid,
        expected_revision=1,
        role="assistant",
        message="PRIVATE original answer",
        metadata={"citations": [citation]},
    )
    FeedbackRepository(repository).record(sid, 1, "thumbs_up")
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "INSERT INTO approvals VALUES(?,?,1,'approved','host_process',"
            "'Original approval','2026-10-07T00:00:00Z')",
            (uuid.uuid4().hex, sid),
        )
        connection.execute(
            "INSERT INTO exports VALUES(?,?,1,?,'2026-10-07T00:00:00Z')",
            (
                uuid.uuid4().hex,
                sid,
                json.dumps(
                    {"format": "markdown", "path": "/old/private.md", "fingerprint": "0" * 64}
                ),
            ),
        )
    return repository, sid


def plan(sid):
    owner = str(uuid.uuid4())
    workspace = Workspace(
        "workspace:fixture", "Fixture", ("fixture",), ("inbound",), ("DC01",), ("test",)
    )
    assignments = {
        sid: {
            "user_id": owner,
            "workspace_id": workspace.workspace_id,
            "reason": "Verified fixture owner",
        }
    }
    return (
        assignments,
        {workspace.workspace_id: workspace},
        {(owner, workspace.workspace_id): "fixture_identity"},
    )


def test_snapshot_preserves_source_and_requires_explicit_stop_and_new_destination(tmp_path):
    repository, sid = legacy(tmp_path)
    original = file_digest(repository.database_path)
    with pytest.raises(ValueError, match="writers"):
        snapshot(repository.database_path, tmp_path / "bundle")
    manifest = snapshot(
        repository.database_path,
        tmp_path / "bundle",
        writers_stopped=True,
        checkpoints=repository.database_path,
    )
    assert manifest["counts"]["approvals"] == 1 and manifest["checkpoint_mode"] == "archive_only"
    assert file_digest(repository.database_path) == original
    loaded, records = load_snapshot(tmp_path / "bundle")
    assert loaded == manifest and records["sessions"][0]["session_id"] == sid
    with pytest.raises(ValueError, match="overwrite"):
        snapshot(repository.database_path, tmp_path / "bundle", writers_stopped=True)
    with sqlite3.connect(tmp_path / "bundle" / "business.sqlite") as connection:
        connection.execute("UPDATE sessions SET goal='tampered'")
    with pytest.raises(ValueError, match="checksum"):
        load_snapshot(tmp_path / "bundle")


def test_projection_requires_individual_authorization_and_keeps_history_approval(tmp_path):
    repository, sid = legacy(tmp_path)
    records = read_database(repository.database_path)
    assignments, policies, identities = plan(sid)
    projected = project(records, assignments, policies, identities)
    assert projected["quarantine"] == []
    assert projected["records"]["sessions"][0]["legacy_readonly"]
    assert projected["records"]["approvals"] == records["approvals"]
    assert projected["records"]["turns"] == records["turns"]
    assert projected["records"]["sessions"][0]["checkpoint_thread_id"].startswith("mu:")
    assert (
        projected["records"]["exports"][0]["artifact_json"]
        != records["exports"][0]["artifact_json"]
    )
    assert (
        project(records, {}, policies, identities)["quarantine"][0]["reason"] == "owner_unassigned"
    )
    assert project(records, assignments, policies, {})["receipts"] == []
    assert project(records, assignments, {}, identities)["receipts"] == []
    with pytest.raises(ValueError):
        project(records, {**assignments, "unknown": assignments[sid]}, policies, identities)
    with pytest.raises(ValueError):
        project(records, {sid: {**assignments[sid], "reason": ""}}, policies, identities)


def test_unknown_provenance_and_corrupted_state_are_not_inferred(tmp_path):
    repository, sid = legacy(tmp_path, scoped=False)
    records = read_database(repository.database_path)
    assert project(records, *plan(sid))["receipts"] == []
    records["turns"][0]["metadata_json"] = "{}"
    assert project(records, *plan(sid))["receipts"] == []
    records["revisions"][0]["state_fingerprint"] = "bad"
    assert project(records, *plan(sid))["receipts"] == []


def budget(**changes):
    return ProbeBudget(
        **{
            "approved": True,
            "max_calls": 4,
            "concurrency": 2,
            "rpm": 100,
            "tpm": 100000,
            "max_output_tokens": 100,
            "total_token_budget": 3000,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"approved": False},
        {"max_calls": 0},
        {"max_calls": True},
        {"total_token_budget": 1},
        {"concurrency": 21},
    ],
)
def test_supplier_budget_refuses_unapproved_or_unbounded_configuration(changes):
    with pytest.raises((ValueError, PermissionError)):
        budget(**changes)


def test_supplier_probe_counts_every_attempt_without_private_text_or_retries():
    class Model:
        max_retries = 0
        max_tokens = 100

        def chat(self, messages):
            assert "PRIVATE" not in str(messages)
            return ChatResponse("do not store this", metadata={"usage": {"total_tokens": 60}})

    report = probe_supplier(Model(), budget())
    assert report["complete"] and report["attempted"] == 4 and report["unknown"] == 0
    assert "do not store this" not in json.dumps(report)
    assert percentile([1, 2, 3, 4]) == 4 and percentile([]) is None
    with pytest.raises(TypeError):
        probe_supplier(Model(), {})
    Model.max_retries = 1
    with pytest.raises(ValueError):
        probe_supplier(Model(), budget())


def test_missing_usage_timeout_and_budget_breach_stop_new_supplier_calls():
    class Model:
        max_retries = 0
        max_tokens = 100

        def chat(self, messages):
            return ChatResponse("synthetic", metadata={})

    report = probe_supplier(Model(), budget(concurrency=1))
    assert report["attempted"] == 1 and report["unknown"] == 1 and not report["complete"]
    Model.chat = lambda self, messages: ChatResponse(
        "synthetic", metadata={"usage": {"total_tokens": 3000}}
    )
    report = probe_supplier(Model(), budget(concurrency=1))
    assert report["attempted"] == 1 and report["rows"][0]["budget_breach"]

    class Failed(Model):
        def chat(self, messages):
            raise RuntimeError("SECRET provider response")

    assert "SECRET" not in json.dumps(probe_supplier(Failed(), budget(concurrency=1)))


def test_post_cutover_preservation_does_not_restore_or_expose_credentials(tmp_path):
    def runner(argv, *, env, stdout, stderr, check):
        assert "sensitive" not in str(argv) and env["PGPASSWORD"] == "sensitive"
        stdout.write(b"synthetic pg dump")
        return SimpleNamespace(returncode=0)

    dsn = "host=localhost dbname=fixture user=operator password=sensitive"
    with pytest.raises(ValueError):
        preserve_post_cutover(dsn, tmp_path / "rollback.dump", runner=runner)
    result = preserve_post_cutover(
        dsn, tmp_path / "rollback.dump", workers_stopped=True, runner=runner
    )
    assert (
        result["legacy_writes_allowed"] is False
        and result["restore_mode"] == "separate_database_only"
    )
    with pytest.raises(ValueError):
        preserve_post_cutover(dsn, tmp_path / "rollback.dump", workers_stopped=True, runner=runner)
    with pytest.raises(RuntimeError):
        preserve_post_cutover(
            dsn,
            tmp_path / "failed.dump",
            workers_stopped=True,
            runner=lambda *a, **kw: SimpleNamespace(returncode=1),
        )


def test_backup_rejects_implicit_routing_and_preserves_tls(monkeypatch, tmp_path):
    monkeypatch.setenv("PGHOSTADDR", "wrong-server")
    monkeypatch.setenv("PGSERVICE", "wrong-service")

    def runner(argv, *, env, stdout, **kwargs):
        assert "PGHOSTADDR" not in env and "PGSERVICE" not in env
        assert env["PGSSLROOTCERT"] == "/private/root.pem" and env["PGSSLMODE"] == "verify-full"
        assert "--schema=agent_checkpoints" in argv
        stdout.write(b"fixture dump")
        return SimpleNamespace(returncode=0)

    preserve_post_cutover(
        "host=localhost dbname=fixture user=operator "
        "sslmode=verify-full sslrootcert=/private/root.pem",
        tmp_path / "tls.dump",
        workers_stopped=True,
        runner=runner,
    )
    with pytest.raises(ValueError):
        preserve_post_cutover(
            "dbname=fixture", tmp_path / "bad.dump", workers_stopped=True, runner=runner
        )
