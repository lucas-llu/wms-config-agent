-- Offline operator boundary. Never grant p1_migrator to a public/runtime login.
SET LOCAL ROLE p1_migrator;
ALTER TABLE agent_business.sessions ADD COLUMN legacy_readonly boolean NOT NULL DEFAULT false;
CREATE TABLE agent_business.release_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 phase text NOT NULL CHECK(phase IN ('active','draining','frozen','rollback_readonly')),
 revision integer NOT NULL DEFAULT 1, reason text NOT NULL, changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO agent_business.release_state VALUES(true,'frozen',1,'Initial offline migration',clock_timestamp());
GRANT SELECT ON agent_business.release_state TO p1_runtime,p3_control;
CREATE TABLE agent_business.migration_receipts (
 bundle_hash text PRIMARY KEY CHECK(length(bundle_hash)=64), plan_hash text NOT NULL,
 operator_id uuid NOT NULL REFERENCES identity_business.users, imported_count integer NOT NULL,
 projection_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE agent_business.migration_sessions (
 session_id text PRIMARY KEY REFERENCES agent_business.sessions ON DELETE CASCADE,
 bundle_hash text NOT NULL REFERENCES agent_business.migration_receipts,
 source_hash text NOT NULL, projection_hash text NOT NULL
);
REVOKE ALL ON agent_business.release_state,agent_business.migration_receipts,agent_business.migration_sessions FROM PUBLIC;
REVOKE INSERT,UPDATE,DELETE ON agent_business.release_state,agent_business.migration_receipts,agent_business.migration_sessions FROM p1_runtime,p3_control;
ALTER POLICY reviewer_insert ON agent_business.approvals TO p1_runtime;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['sessions','revisions','turns','deleted_sessions','conversation_titles',
 'decisions','approvals','exports','feedback_signals','checkpoint_threads','resources'] LOOP
 EXECUTE format('CREATE POLICY offline_import ON agent_business.%I TO p1_migrator USING(true) WITH CHECK(true)',t);
 END LOOP; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['runs','run_model_calls','run_outbox','run_events','usage_runs',
 'usage_attempts','quota_reservations','quota_limits','quota_accounts','model_permits','model_governors',
 'dispatch_owners'] LOOP
 EXECUTE format('CREATE POLICY offline_preserve ON agent_business.%I TO p1_migrator USING(true) WITH CHECK(true)',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['checkpoints','checkpoint_blobs','checkpoint_writes'] LOOP
 EXECUTE format('CREATE POLICY offline_preserve ON agent_checkpoints.%I TO p1_migrator USING(true)',t);
 END LOOP; END $$;
CREATE POLICY offline_preserve ON identity_business.revoked_sessions TO p1_migrator USING(true);
CREATE FUNCTION agent_business.release_phase() RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,agent_business AS $$
 SELECT phase FROM agent_business.release_state WHERE singleton FOR SHARE $$;
REVOKE ALL ON FUNCTION agent_business.release_phase() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_business.release_phase() TO p1_runtime,p3_control;
CREATE FUNCTION agent_business.release_write_guard() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog,agent_business AS $$
DECLARE phase_now text; parent text; readonly_parent boolean; BEGIN
 IF current_user='p1_migrator' THEN RETURN coalesce(NEW,OLD); END IF;
 phase_now:=agent_business.release_phase();
 IF phase_now IN ('frozen','rollback_readonly') OR
 (phase_now='draining' AND TG_TABLE_NAME IN ('sessions','runs') AND TG_OP='INSERT') THEN
 RAISE EXCEPTION 'release maintenance' USING ERRCODE='55000'; END IF;
 IF TG_TABLE_NAME='runs' THEN parent:=NEW.conversation_id;
 ELSE parent:=coalesce(NEW.session_id,OLD.session_id); END IF;
 SELECT legacy_readonly INTO readonly_parent FROM agent_business.sessions WHERE session_id=parent;
 IF coalesce(readonly_parent,false) THEN
 RAISE EXCEPTION 'legacy history is read only' USING ERRCODE='42501'; END IF;
 RETURN coalesce(NEW,OLD); END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['sessions','revisions','turns','deleted_sessions','conversation_titles',
 'decisions','approvals','exports','feedback_signals','checkpoint_threads','resources'] LOOP
 EXECUTE format('CREATE TRIGGER release_write_guard BEFORE INSERT OR UPDATE OR DELETE ON agent_business.%I FOR EACH ROW EXECUTE FUNCTION agent_business.release_write_guard()',t);
 END LOOP; END $$;
CREATE TRIGGER release_admission BEFORE INSERT ON agent_business.runs
 FOR EACH ROW EXECUTE FUNCTION agent_business.release_write_guard();
