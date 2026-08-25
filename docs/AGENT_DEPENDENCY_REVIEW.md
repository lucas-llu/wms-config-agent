# Agent Runtime Dependency Review

> Reviewed: 2026-08-26
> Scope: dependencies introduced by the V2 Agent runtime foundation
> Lock source: `uv.lock`

## Decision

The selected Agent runtime dependencies are acceptable for the local, read-only V2 MVP. The
direct packages and their immediate Agent-specific dependency tree use permissive MIT, Apache-2.0,
or BSD-2-Clause licenses. Package artifacts and hashes are pinned in `uv.lock`.

The implementation imports only the open-source LangGraph state graph and SQLite checkpointer.
It does not configure LangSmith tracing, LangGraph Agent Server, or any external telemetry service.
Local conversation and checkpoint data therefore remain within the project-defined storage paths.

## Reviewed packages

| Package | Locked version | License evidence from installed metadata |
|---|---:|---|
| `langgraph` | 1.2.11 | MIT |
| `langgraph-checkpoint-sqlite` | 3.1.1 | MIT |
| `langgraph-checkpoint` | 4.2.0 | MIT |
| `langgraph-prebuilt` | 1.1.0 | MIT |
| `langgraph-sdk` | 0.4.3 | MIT |
| `langchain-core` | 1.6.0 | MIT |
| `langchain-protocol` | 0.0.18 | MIT |
| `aiosqlite` | 0.22.1 | MIT classifier |
| `sqlite-vec` | 0.1.9 | MIT / Apache-2.0 |
| `ormsgpack` | 1.12.2 | Apache-2.0 OR MIT |
| `xxhash` | 4.0.1 | BSD-2-Clause |
| `pydantic` | 2.13.4 | MIT |

## Re-review triggers

Repeat this review when any of the following occurs:

- the `langgraph` or `langgraph-checkpoint-sqlite` minor-version range changes;
- `uv.lock` resolves a new Agent-specific direct or transitive dependency;
- external tracing, Agent Server, cloud checkpointing, or remote stores are enabled;
- the runtime begins sending configuration-session content outside the local process.
