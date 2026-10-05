"""Verify that the project package boundaries are importable."""


def test_top_level_packages_import() -> None:
    import agents
    import core
    import ingestion
    import libs
    import mcp_server
    import observability

    assert all((agents, core, ingestion, libs, mcp_server, observability))
