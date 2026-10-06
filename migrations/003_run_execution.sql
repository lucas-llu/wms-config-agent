-- Requires 002. p3_control is a separate non-owner NO BYPASSRLS control-plane role.
SET LOCAL ROLE p1_migrator;
ALTER TABLE agent_business.runs ADD COLUMN identity_issuer text NOT NULL DEFAULT '';
ALTER TABLE agent_business.runs ADD COLUMN identity_subject text NOT NULL DEFAULT '';
ALTER TABLE agent_business.runs ADD COLUMN identity_sid text NOT NULL DEFAULT '';
ALTER TABLE agent_business.runs ADD COLUMN identity_iat bigint NOT NULL DEFAULT 0;
ALTER TABLE agent_business.runs ADD COLUMN source_thread text;
ALTER TABLE agent_business.runs ADD COLUMN resume_ready boolean NOT NULL DEFAULT false;
ALTER TABLE agent_business.runs ADD COLUMN dispatch_after timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE agent_business.sessions ADD COLUMN last_checkpoint_thread text;
ALTER TABLE agent_business.checkpoint_threads ADD COLUMN run_id text
 REFERENCES agent_business.runs ON DELETE CASCADE;
ALTER TABLE agent_business.checkpoint_threads ADD COLUMN epoch integer;
CREATE FUNCTION agent_business.generation_writable(thread text) RETURNS boolean LANGUAGE sql STABLE
AS $$ SELECT EXISTS(SELECT 1 FROM agent_business.checkpoint_threads t
 WHERE t.thread_id=thread AND (t.run_id IS NULL OR EXISTS(
 SELECT 1 FROM agent_business.runs r WHERE r.run_id=t.run_id AND r.epoch=t.epoch
 AND r.status='running' AND r.lease_until>clock_timestamp()
 AND r.execution_deadline>clock_timestamp()))) $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['checkpoints','checkpoint_blobs','checkpoint_writes'] LOOP
 EXECUTE format('CREATE POLICY live_generation_insert ON agent_checkpoints.%I AS RESTRICTIVE '
 'FOR INSERT WITH CHECK(agent_business.generation_writable(thread_id))',t);
 EXECUTE format('CREATE POLICY live_generation_update ON agent_checkpoints.%I AS RESTRICTIVE '
 'FOR UPDATE USING(agent_business.generation_writable(thread_id)) '
 'WITH CHECK(agent_business.generation_writable(thread_id))',t);
 END LOOP; END $$;

CREATE TABLE agent_business.run_model_calls (
 run_id text REFERENCES agent_business.runs ON DELETE CASCADE,
 call_key text NOT NULL, attempt integer NOT NULL CHECK(attempt>0),
 status text NOT NULL CHECK(status IN ('inflight','returned','rejected','unknown')),
 response_json text, PRIMARY KEY(run_id,call_key,attempt),
 CHECK((status='returned')=(response_json IS NOT NULL))
);
ALTER TABLE agent_business.run_model_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_business.run_model_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY own_call_parent ON agent_business.run_model_calls
 USING(EXISTS(SELECT 1 FROM agent_business.runs r WHERE r.run_id=run_model_calls.run_id))
 WITH CHECK(EXISTS(SELECT 1 FROM agent_business.runs r WHERE r.run_id=run_model_calls.run_id));
GRANT SELECT,INSERT,UPDATE ON agent_business.run_model_calls TO p1_runtime;

CREATE TABLE agent_business.dispatch_owners (
 owner_user_id uuid PRIMARY KEY, last_dispatched timestamptz NOT NULL
);
CREATE TABLE agent_business.model_permits (
 permit_id text PRIMARY KEY, provider_key text NOT NULL, review boolean NOT NULL,
 token_reservation bigint NOT NULL CHECK(token_reservation>=0),
 issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL, released boolean NOT NULL DEFAULT false
);
CREATE INDEX model_window ON agent_business.model_permits(provider_key,issued_at);
CREATE TABLE agent_business.model_governors (
 provider_key text PRIMARY KEY, config_json text NOT NULL
);
GRANT USAGE ON SCHEMA agent_business TO p3_control;
GRANT SELECT(run_id,owner_user_id,conversation_id,status,stage,epoch,lease_until,
 execution_deadline,queue_deadline,created_at,event_sequence,open_model_calls,model_attempts,
 result_revision,identity_issuer,identity_subject,identity_sid,identity_iat,source_thread,
 resume_ready,dispatch_after) ON agent_business.runs TO p3_control;
GRANT UPDATE(status,stage,epoch,lease_until,execution_deadline,event_sequence,
 source_thread,resume_ready,dispatch_after) ON agent_business.runs TO p3_control;
GRANT SELECT,INSERT,UPDATE ON agent_business.run_outbox,agent_business.dispatch_owners,
 agent_business.model_permits,agent_business.model_governors TO p3_control;
GRANT INSERT ON agent_business.run_events TO p3_control;
GRANT SELECT ON agent_business.run_events TO p3_control;
CREATE POLICY scheduler_runs ON agent_business.runs TO p3_control USING(true) WITH CHECK(true);
ALTER POLICY own_run ON agent_business.runs TO p1_runtime;
ALTER POLICY own_run_parent ON agent_business.run_outbox TO p1_runtime;
ALTER POLICY own_run_parent ON agent_business.run_events TO p1_runtime;
CREATE POLICY scheduler_outbox ON agent_business.run_outbox TO p3_control USING(true) WITH CHECK(true);
CREATE POLICY scheduler_events ON agent_business.run_events TO p3_control USING(true) WITH CHECK(true);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['dispatch_owners','model_permits','model_governors'] LOOP
 EXECUTE format('ALTER TABLE agent_business.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE agent_business.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY control_only ON agent_business.%I TO p3_control '
 'USING(true) WITH CHECK(true)',t);
 END LOOP; END $$;
GRANT DELETE ON agent_business.model_permits TO p3_control;
CREATE FUNCTION agent_business.immutable_run_identity() RETURNS trigger LANGUAGE plpgsql
AS $$ BEGIN
 IF (NEW.identity_issuer,NEW.identity_subject,NEW.identity_sid,NEW.identity_iat)
 IS DISTINCT FROM (OLD.identity_issuer,OLD.identity_subject,OLD.identity_sid,OLD.identity_iat) THEN
 RAISE EXCEPTION 'immutable run identity' USING ERRCODE='42501'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER immutable_run_identity_binding BEFORE UPDATE ON agent_business.runs
 FOR EACH ROW EXECUTE FUNCTION agent_business.immutable_run_identity();
-- Column grants deliberately prohibit messages, model responses and conversation bodies.
-- No application-facing route uses this role. Private business writes still use p1_runtime + RLS.
