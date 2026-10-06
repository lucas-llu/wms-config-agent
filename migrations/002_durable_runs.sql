-- Apply once after P1 checkpoint setup, as the migration identity. No startup DDL.
SET LOCAL ROLE p1_migrator;
CREATE TABLE agent_business.runs (
 run_id text PRIMARY KEY,
 owner_user_id uuid NOT NULL DEFAULT identity_business.actor_id()
 REFERENCES identity_business.users(user_id),
 conversation_id text NOT NULL REFERENCES agent_business.sessions ON DELETE CASCADE,
 idempotency_key text NOT NULL, content_hash text NOT NULL,
 message text NOT NULL CHECK(length(message) BETWEEN 1 AND 16000),
 answer_strategy text NOT NULL CHECK(answer_strategy IN ('standard','review')),
 expected_revision integer NOT NULL CHECK(expected_revision>=1),
 status text NOT NULL CHECK(status IN ('queued','running','cancelling','succeeded',
 'cancelled','failed','uncertain','timed_out','authorization_required')),
 stage text NOT NULL, epoch integer NOT NULL DEFAULT 0 CHECK(epoch>=0),
 lease_until timestamptz, execution_deadline timestamptz,
 queue_deadline timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_sequence bigint NOT NULL DEFAULT 0 CHECK(event_sequence>=0),
 open_model_calls integer NOT NULL DEFAULT 0 CHECK(open_model_calls>=0),
 result_revision integer,
 UNIQUE(owner_user_id,conversation_id,idempotency_key),
 FOREIGN KEY(conversation_id,result_revision) REFERENCES agent_business.revisions,
 CHECK((status='succeeded')=(result_revision IS NOT NULL))
);
CREATE UNIQUE INDEX one_active_run_per_conversation ON agent_business.runs(conversation_id)
 WHERE status IN ('queued','running','cancelling');
CREATE INDEX runs_owner_schedule ON agent_business.runs(owner_user_id,status,created_at,run_id);
CREATE TABLE agent_business.run_events (
 run_id text REFERENCES agent_business.runs ON DELETE CASCADE,
 sequence bigint NOT NULL CHECK(sequence>0), status text NOT NULL, stage text NOT NULL,
 result_revision integer, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,sequence)
);
CREATE TABLE agent_business.run_outbox (
 run_id text PRIMARY KEY REFERENCES agent_business.runs ON DELETE CASCADE,
 delivery_id text NOT NULL UNIQUE, dispatched boolean NOT NULL DEFAULT false,
 deliveries integer NOT NULL DEFAULT 0 CHECK(deliveries>=0),
 last_dispatched_at timestamptz
);
ALTER TABLE agent_business.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_business.runs FORCE ROW LEVEL SECURITY;
CREATE POLICY own_run ON agent_business.runs
 USING(owner_user_id=identity_business.actor_id() AND
 agent_business.owns_session(conversation_id) AND NOT EXISTS(
 SELECT 1 FROM agent_business.deleted_sessions d WHERE d.session_id=conversation_id))
 WITH CHECK(owner_user_id=identity_business.actor_id() AND
 agent_business.owns_session(conversation_id) AND NOT EXISTS(
 SELECT 1 FROM agent_business.deleted_sessions d WHERE d.session_id=conversation_id));
CREATE FUNCTION agent_business.immutable_run() RETURNS trigger LANGUAGE plpgsql
AS $$ BEGIN
 IF (NEW.run_id,NEW.owner_user_id,NEW.conversation_id,NEW.idempotency_key,NEW.content_hash,
 NEW.message,NEW.answer_strategy,NEW.expected_revision,NEW.created_at,NEW.queue_deadline)
 IS DISTINCT FROM
 (OLD.run_id,OLD.owner_user_id,OLD.conversation_id,OLD.idempotency_key,OLD.content_hash,
 OLD.message,OLD.answer_strategy,OLD.expected_revision,OLD.created_at,OLD.queue_deadline) THEN
 RAISE EXCEPTION 'immutable run binding' USING ERRCODE='42501'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER immutable_run_binding BEFORE UPDATE ON agent_business.runs
 FOR EACH ROW EXECUTE FUNCTION agent_business.immutable_run();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['run_events','run_outbox'] LOOP
 EXECUTE format('ALTER TABLE agent_business.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE agent_business.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY own_run_parent ON agent_business.%I USING(EXISTS('
 'SELECT 1 FROM agent_business.runs r WHERE r.run_id=%I.run_id)) WITH CHECK(EXISTS('
 'SELECT 1 FROM agent_business.runs r WHERE r.run_id=%I.run_id))',t,t,t);
 END LOOP; END $$;
GRANT SELECT,INSERT,UPDATE ON agent_business.runs,agent_business.run_outbox TO p1_runtime;
GRANT SELECT,INSERT ON agent_business.run_events TO p1_runtime;
