-- Run only with the migration role, never at request time.
CREATE SCHEMA IF NOT EXISTS identity_business AUTHORIZATION p1_migrator;
CREATE SCHEMA IF NOT EXISTS agent_business AUTHORIZATION p1_migrator;
CREATE SCHEMA IF NOT EXISTS agent_checkpoints AUTHORIZATION p1_migrator;
SET LOCAL ROLE p1_migrator;
CREATE TABLE IF NOT EXISTS identity_business.users (
 user_id uuid PRIMARY KEY, identity_issuer text NOT NULL, identity_subject text NOT NULL,
 nickname text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'active'
 CHECK(status IN ('active','disabled')), is_platform_admin boolean NOT NULL DEFAULT false,
 revoked_before bigint NOT NULL DEFAULT 0, UNIQUE(identity_issuer,identity_subject)
);
CREATE TABLE IF NOT EXISTS identity_business.workspaces (
 workspace_id text PRIMARY KEY, policy_json text NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_business.memberships (
 user_id uuid REFERENCES identity_business.users(user_id),
 workspace_id text REFERENCES identity_business.workspaces(workspace_id),
 role text NOT NULL CHECK(role IN ('member','reviewer','workspace_admin')),
 active boolean NOT NULL DEFAULT true, PRIMARY KEY(user_id,workspace_id)
);
CREATE TABLE IF NOT EXISTS identity_business.revoked_sessions (
 user_id uuid REFERENCES identity_business.users(user_id), sid text NOT NULL,
 PRIMARY KEY(user_id,sid)
);
CREATE FUNCTION identity_business.actor_id() RETURNS uuid LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('app.user_id',true),'')::uuid $$;
CREATE FUNCTION identity_business.actor_active() RETURNS boolean LANGUAGE sql STABLE
AS $$ SELECT EXISTS(SELECT 1 FROM identity_business.users u
 WHERE u.user_id=identity_business.actor_id() AND u.status='active'
 AND coalesce(nullif(current_setting('app.token_exp',true),''),'0')::bigint > extract(epoch FROM statement_timestamp())
 AND u.revoked_before<coalesce(nullif(current_setting('app.token_iat',true),''),'0')::bigint
 AND NOT EXISTS(SELECT 1 FROM identity_business.revoked_sessions r
 WHERE r.user_id=u.user_id AND r.sid=current_setting('app.sid',true))) $$;
CREATE FUNCTION identity_business.workspace_allowed(w text) RETURNS boolean LANGUAGE sql STABLE
AS $$ SELECT identity_business.actor_active() AND EXISTS(
 SELECT 1 FROM identity_business.memberships m WHERE m.user_id=identity_business.actor_id()
 AND m.workspace_id=w AND m.active) $$;
ALTER TABLE identity_business.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_business.users FORCE ROW LEVEL SECURITY;
CREATE POLICY own_identity ON identity_business.users
 USING(identity_issuer=current_setting('app.issuer',true) AND identity_subject=current_setting('app.subject',true))
 WITH CHECK(identity_issuer=current_setting('app.issuer',true) AND identity_subject=current_setting('app.subject',true));
ALTER TABLE identity_business.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_business.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY own_membership ON identity_business.memberships USING(user_id=identity_business.actor_id());
ALTER TABLE identity_business.revoked_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_business.revoked_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY own_revocation ON identity_business.revoked_sessions
 USING(user_id=identity_business.actor_id()) WITH CHECK(user_id=identity_business.actor_id());
ALTER TABLE identity_business.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_business.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY member_workspace ON identity_business.workspaces USING(identity_business.workspace_allowed(workspace_id));

CREATE TABLE agent_business.sessions (
 session_id text PRIMARY KEY, owner_user_id uuid NOT NULL DEFAULT identity_business.actor_id()
 REFERENCES identity_business.users(user_id), workspace_id text NOT NULL REFERENCES identity_business.workspaces(workspace_id),
 goal text NOT NULL, display_title text, status text NOT NULL, current_revision integer NOT NULL CHECK(current_revision>=1),
 checkpoint_thread_id text NOT NULL UNIQUE, created_at text NOT NULL, updated_at text NOT NULL,
 cancelled_at text, archived boolean NOT NULL DEFAULT false
);
CREATE FUNCTION agent_business.owns_session(s text) RETURNS boolean LANGUAGE sql STABLE
AS $$ SELECT EXISTS(SELECT 1 FROM agent_business.sessions WHERE session_id=s) $$;
CREATE TABLE agent_business.revisions (
 session_id text REFERENCES agent_business.sessions(session_id) ON DELETE CASCADE, revision integer NOT NULL,
 status text NOT NULL,state_json text NOT NULL,state_fingerprint text NOT NULL,actor text NOT NULL,
 reason text NOT NULL,created_at text NOT NULL,PRIMARY KEY(session_id,revision)
);
CREATE TABLE agent_business.turns (
 turn_id text PRIMARY KEY,session_id text NOT NULL,revision integer NOT NULL,sequence integer NOT NULL,
 role text NOT NULL,message text NOT NULL,metadata_json text NOT NULL,created_at text NOT NULL,
 UNIQUE(session_id,sequence),FOREIGN KEY(session_id,revision) REFERENCES agent_business.revisions ON DELETE CASCADE
);
CREATE TABLE agent_business.deleted_sessions (
 session_id text PRIMARY KEY REFERENCES agent_business.sessions ON DELETE CASCADE,deleted_at text NOT NULL
);
CREATE TABLE agent_business.conversation_titles (
 session_id text PRIMARY KEY REFERENCES agent_business.sessions ON DELETE CASCADE,
 display_title text NOT NULL CHECK(length(display_title) BETWEEN 1 AND 120)
);
CREATE TABLE agent_business.decisions (
 decision_id text PRIMARY KEY,session_id text NOT NULL,revision integer NOT NULL,
 decision_json text NOT NULL,created_at text NOT NULL,
 FOREIGN KEY(session_id,revision) REFERENCES agent_business.revisions ON DELETE CASCADE
);
CREATE TABLE agent_business.approvals (
 approval_id text PRIMARY KEY,session_id text NOT NULL,revision integer NOT NULL,
 decision text NOT NULL,actor text NOT NULL,comment text NOT NULL,created_at text NOT NULL,
 FOREIGN KEY(session_id,revision) REFERENCES agent_business.revisions ON DELETE CASCADE
);
CREATE TABLE agent_business.exports (
 export_id text PRIMARY KEY,session_id text NOT NULL,revision integer NOT NULL,
 artifact_json text NOT NULL,created_at text NOT NULL,
 FOREIGN KEY(session_id,revision) REFERENCES agent_business.revisions ON DELETE CASCADE
);
CREATE TABLE agent_business.feedback_signals (
 feedback_id text PRIMARY KEY,workspace_id text NOT NULL,session_id text NOT NULL,revision integer NOT NULL,
 trace_id text,kind text NOT NULL,reason text NOT NULL,created_at text NOT NULL,
 UNIQUE(workspace_id,session_id,revision,kind,reason),
 FOREIGN KEY(session_id,revision) REFERENCES agent_business.revisions ON DELETE CASCADE
);
CREATE TABLE agent_business.checkpoint_threads (
 thread_id text PRIMARY KEY,session_id text NOT NULL REFERENCES agent_business.sessions ON DELETE CASCADE
);
CREATE TABLE agent_business.resources (
 resource_id text PRIMARY KEY,session_id text NOT NULL REFERENCES agent_business.sessions ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('run','event','attachment','usage','image','evidence')),
 payload_json text NOT NULL,storage_key text
);
ALTER TABLE agent_business.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_business.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY private_sessions ON agent_business.sessions
 USING(owner_user_id=identity_business.actor_id() AND identity_business.workspace_allowed(workspace_id))
 WITH CHECK(owner_user_id=identity_business.actor_id() AND identity_business.workspace_allowed(workspace_id));
CREATE FUNCTION agent_business.immutable_owner() RETURNS trigger LANGUAGE plpgsql
AS $$ BEGIN
 IF (NEW.owner_user_id,NEW.workspace_id,NEW.session_id,NEW.checkpoint_thread_id)
 IS DISTINCT FROM (OLD.owner_user_id,OLD.workspace_id,OLD.session_id,OLD.checkpoint_thread_id) THEN
 RAISE EXCEPTION 'immutable conversation binding' USING ERRCODE='42501'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER immutable_binding BEFORE UPDATE ON agent_business.sessions
 FOR EACH ROW EXECUTE FUNCTION agent_business.immutable_owner();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['revisions','turns','deleted_sessions','conversation_titles','decisions',
 'approvals','exports','feedback_signals','checkpoint_threads','resources'] LOOP
 EXECUTE format('ALTER TABLE agent_business.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE agent_business.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY own_parent ON agent_business.%I USING(agent_business.owns_session(session_id)) WITH CHECK(agent_business.owns_session(session_id))',t);
 END LOOP; END $$;
-- Approvals require a reviewer role even when the requester owns the conversation.
CREATE POLICY reviewer_insert ON agent_business.approvals AS RESTRICTIVE FOR INSERT
 WITH CHECK(EXISTS(SELECT 1 FROM agent_business.sessions s JOIN identity_business.memberships m
 ON s.workspace_id=m.workspace_id WHERE s.session_id=approvals.session_id
 AND m.user_id=identity_business.actor_id() AND m.active AND m.role IN ('reviewer','workspace_admin')));
REVOKE ALL ON SCHEMA identity_business,agent_business,agent_checkpoints FROM PUBLIC;
GRANT USAGE ON SCHEMA identity_business,agent_business,agent_checkpoints TO p1_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA identity_business TO p1_runtime;
GRANT INSERT(user_id,identity_issuer,identity_subject,nickname) ON identity_business.users TO p1_runtime;
GRANT UPDATE(nickname) ON identity_business.users TO p1_runtime;
GRANT INSERT ON identity_business.revoked_sessions TO p1_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agent_business TO p1_runtime;
