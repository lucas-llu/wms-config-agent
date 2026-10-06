-- Metadata-only extension; preserve the checksum of the already-versioned 003.
SET LOCAL ROLE p1_migrator;
GRANT SELECT(answer_strategy) ON agent_business.runs TO p3_control;
