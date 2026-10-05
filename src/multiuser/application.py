"""One authorized business surface shared by HTTP and authenticated tool adapters."""

from dataclasses import asdict
from pathlib import Path

from agents.contracts import ReviewDecision, stable_contract_id
from agents.services import SolutionService, ValidationService
from multiuser.access import AccessDenied
from multiuser.feedback import PostgresFeedbackRepository
from multiuser.session_repository import PostgresSessionRepository


class OwnedApplication:
    def __init__(self, store, *, export_root, agent=None, images=None):
        self.store, self.export_root, self.agent = store, Path(export_root).resolve(), agent
        self.images = images

    def repository(self, context, session_id):
        workspace = self.store.workspace_for(context, session_id)
        return PostgresSessionRepository(self.store, context, workspace)

    def start(self, context, goal, workspace_id, answer_strategy="standard"):
        repository = PostgresSessionRepository(self.store, context, workspace_id)
        if self.agent:
            return self.agent.start(context, repository, goal, answer_strategy=answer_strategy)
        from agents.services import SessionService

        session = SessionService(repository).create_session(goal)
        return asdict(session)

    def get(self, context, session_id):
        repository = self.repository(context, session_id)
        return {
            "session": asdict(repository.get_session(session_id)),
            "turns": [asdict(t) for t in repository.list_turns(session_id)],
        }

    def continue_session(
        self, context, session_id, message, expected_revision, answer_strategy="standard"
    ):
        repository = self.repository(context, session_id)
        if not self.agent:
            raise RuntimeError("Agent execution is not configured")
        if repository.get_session(session_id).current_revision != expected_revision:
            from agents.repositories import SessionRevisionConflict

            raise SessionRevisionConflict(
                session_id, expected_revision, repository.get_session(session_id).current_revision
            )
        return self.agent.continue_session(
            context,
            repository,
            session_id,
            message,
            expected_revision=expected_revision,
            answer_strategy=answer_strategy,
        )

    def workbench(self, context, session_id, revision=None):
        from core.evidence_text import clean_evidence_text
        from observability.dashboard.services.answer_evidence import split_answer_evidence

        repository = self.repository(context, session_id)
        current = repository.get_session(session_id)
        snapshot = repository.get_revision(session_id, revision)
        turns = []
        for turn in repository.list_turns(session_id):
            if turn.revision > snapshot.revision:
                continue
            view = asdict(turn)
            if turn.role == "assistant":
                view["message"], view["legacy_evidence"] = split_answer_evidence(turn.message)
                view["citations"] = [
                    {
                        **c,
                        "excerpt": clean_evidence_text(
                            c.get("full_excerpt") or c.get("excerpt", "")
                        ),
                    }
                    for c in turn.metadata.get("citations", [])
                ]
                for index, c in enumerate(view["citations"]):
                    from urllib.parse import quote

                    assets, _ = (
                        self.images.resolve(c, repository.workspace) if self.images else ([], False)
                    )
                    prefix = (
                        "/v1/conversations/"
                        + quote(session_id, safe="")
                        + "/turns/"
                        + quote(turn.turn_id, safe="")
                    )
                    c["images"] = [
                        prefix + f"/evidence/{index}/images/{ordinal}"
                        for ordinal in range(len(assets))
                    ]
                    c.pop("full_excerpt", None)
            turns.append(view)
        return {
            "session": asdict(current),
            "revision": snapshot.revision,
            "state": snapshot.state,
            "turns": turns,
            "approvals": [
                asdict(a)
                for a in repository.list_approvals(session_id)
                if a.revision <= snapshot.revision
            ],
        }

    def validate(self, context, session_id, expected_revision):
        repository = self.repository(context, session_id)
        state = repository.get_revision(session_id, expected_revision).state
        result = ValidationService().validate(
            tasks=list(state.get("configuration_tasks", [])),
            dependency_edges=list(state.get("dependency_edges", [])),
            evidence_registry=list(state.get("evidence_registry", [])),
            bindings=list(state.get("task_evidence_bindings", [])),
            confirmed_context=state.get("confirmed_context", {}),
            invalidated_task_ids=state.get("invalidated_task_ids", []),
        )
        return repository.update_revision(
            session_id=session_id,
            expected_revision=expected_revision,
            state_update={
                "status": "paused" if result.blocking else "review_required",
                "validation_findings": [r.to_dict() for r in result.findings],
                "conflicts": [r.to_dict() for r in result.conflicts],
                "validation_fingerprint": result.fingerprint,
            },
            actor="validation",
            reason="explicit_validation",
        )

    def evidence_image(self, context, session_id, turn_id, index, ordinal):
        from PIL import Image, UnidentifiedImageError

        from agents.repositories import SessionNotFoundError

        repository = self.repository(context, session_id)
        turn = next((t for t in repository.list_turns(session_id) if t.turn_id == turn_id), None)
        citations = turn.metadata.get("citations", []) if turn else []
        if self.images is None or not 0 <= index < len(citations):
            raise SessionNotFoundError("Image not found")
        assets, _ = self.images.resolve(citations[index], repository.workspace)
        if not 0 <= ordinal < len(assets):
            raise SessionNotFoundError("Image not found")
        path = assets[ordinal]["path"]
        try:
            with Image.open(path) as image:
                mime = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "WEBP": "image/webp",
                    "GIF": "image/gif",
                }.get(image.format)
            if mime is None:
                raise AccessDenied("Unsupported inline image")
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise AccessDenied("Invalid inline image") from exc
        return path, mime

    def review(self, context, session_id, expected_revision, decision, comment):
        repository = self.repository(context, session_id)
        self.store.require_reviewer(context, repository.workspace_id)
        return SolutionService(repository, self.export_root / context.user_id).review(
            session_id,
            expected_revision=expected_revision,
            decision=ReviewDecision(decision),
            actor=context.user_id,
            comment=comment,
        )

    def export(self, context, session_id, expected_revision, format="markdown"):
        repository = self.repository(context, session_id)
        self.store.require_reviewer(context, repository.workspace_id)
        result = SolutionService(repository, self.export_root / context.user_id).export(
            session_id,
            expected_revision=expected_revision,
            format=format,
        )
        return {
            "export_id": stable_contract_id(
                "export",
                {"session_id": session_id, "revision": expected_revision, "format": format},
            ),
            "revision": expected_revision,
            "format": result.format,
            "fingerprint": result.fingerprint,
        }

    def export_file(self, context, session_id, export_id):
        repository = self.repository(context, session_id)
        records = repository.list_exports(session_id)
        record = next((r for r in records if r.export_id == export_id), None)
        if record is None:
            from agents.repositories import SessionNotFoundError

            raise SessionNotFoundError("Export not found")
        root = (self.export_root / context.user_id).resolve()
        path = Path(record.artifact.path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise AccessDenied("File unavailable")
        return path

    def file(self, context, session_id, resource_id, kind):
        repository = self.repository(context, session_id)
        row = repository.get_resource(session_id, resource_id, kind)
        key = row.get("storage_key")
        if not key or Path(key).is_absolute():
            raise AccessDenied("Invalid file reference")
        root = self.export_root / context.user_id
        path = (root / key).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise AccessDenied("File unavailable")
        return path

    def tool(self, context, name, arguments):
        # No host_process identity and no generic registry fallback.
        from multiuser.tools import parse

        arguments = parse(name, arguments)
        forbidden = {"owner_user_id", "user_id", "roles", "thread_id", "checkpoint_ns", "actor"}
        if forbidden.intersection(arguments):
            raise ValueError("Untrusted identity or checkpoint fields")
        if name == "start_configuration_session":
            if set(arguments) != {"goal", "workspace_id"}:
                raise ValueError("Invalid start fields")
            return self.start(context, arguments["goal"], arguments["workspace_id"])
        session_id = arguments.pop("session_id", None)
        repository = self.repository(context, session_id)
        if name == "get_configuration_session" and not arguments:
            return self.get(context, session_id)
        if name == "continue_configuration_session" and set(arguments) == {
            "message",
            "expected_revision",
        }:
            return self.continue_session(context, session_id, **arguments)
        if name == "validate_configuration_draft" and set(arguments) == {"expected_revision"}:
            return self.validate(context, session_id, **arguments)
        if name == "review_configuration_draft" and set(arguments) == {
            "expected_revision",
            "decision",
            "comment",
        }:
            return self.review(context, session_id, **arguments)
        if name == "export_configuration_solution" and set(arguments) == {
            "expected_revision",
            "format",
        }:
            return self.export(context, session_id, **arguments)
        if name == "record_configuration_feedback" and set(arguments) <= {
            "revision",
            "kind",
            "reason",
        }:
            return PostgresFeedbackRepository(repository).record(session_id, **arguments)
        if name == "get_configuration_feedback_summary" and set(arguments) == {"revision"}:
            return PostgresFeedbackRepository(repository).summary(session_id, **arguments)
        raise ValueError("Unsupported or invalid authenticated tool")
