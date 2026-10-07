SET LOCAL ROLE p1_migrator;
CREATE FUNCTION identity_business.platform_admin() RETURNS boolean LANGUAGE sql STABLE
AS $$ SELECT identity_business.actor_active() AND EXISTS(SELECT 1 FROM identity_business.users
 WHERE user_id=identity_business.actor_id() AND is_platform_admin) $$;
CREATE TABLE agent_business.quota_limits (
 user_id uuid PRIMARY KEY REFERENCES identity_business.users,
 monthly_tokens bigint NOT NULL CHECK(monthly_tokens>=0), revision integer NOT NULL DEFAULT 1
);
CREATE TABLE agent_business.quota_accounts (
 user_id uuid REFERENCES identity_business.users, period date NOT NULL,
 used_tokens bigint NOT NULL DEFAULT 0 CHECK(used_tokens>=0),
 held_tokens bigint NOT NULL DEFAULT 0 CHECK(held_tokens>=0), PRIMARY KEY(user_id,period)
);
-- Accounting survives trash purge. IDs are immutable opaque references, not FK cascades to chat.
CREATE TABLE agent_business.usage_runs (
 run_id text PRIMARY KEY, owner_user_id uuid NOT NULL REFERENCES identity_business.users,
 conversation_id text NOT NULL, workspace_id text NOT NULL, strategy text NOT NULL,
 budget bigint NOT NULL CHECK(budget>0), model text NOT NULL, provider_key text NOT NULL,
 price_json text, closed boolean NOT NULL DEFAULT false,
 accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE agent_business.quota_reservations (
 run_id text REFERENCES agent_business.usage_runs, period date NOT NULL,
 owner_user_id uuid NOT NULL REFERENCES identity_business.users,
 held_tokens bigint NOT NULL CHECK(held_tokens>=0), closed boolean NOT NULL DEFAULT false,
 PRIMARY KEY(run_id,period)
);
CREATE TABLE agent_business.usage_attempts (
 attempt_id text PRIMARY KEY, run_id text NOT NULL REFERENCES agent_business.usage_runs,
 owner_user_id uuid NOT NULL REFERENCES identity_business.users,
 call_key text NOT NULL, attempt integer NOT NULL, period date NOT NULL,
 allocated bigint NOT NULL CHECK(allocated>0), input_estimate bigint NOT NULL CHECK(input_estimate>=0),
 status text NOT NULL CHECK(status IN ('inflight','returned','rejected','unknown')),
 source text NOT NULL CHECK(source IN ('provider','estimated','unknown','rejected')),
 input_tokens bigint, output_tokens bigint, total_tokens bigint, cached_tokens bigint, reasoning_tokens bigint,
 estimated_cost numeric(30,12), provider_request_id text, revision integer NOT NULL DEFAULT 1,
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
 UNIQUE(run_id,call_key,attempt), CHECK(total_tokens IS NULL OR total_tokens>=0)
);
CREATE INDEX usage_owner_period ON agent_business.usage_attempts(owner_user_id,period,started_at);
CREATE TABLE agent_business.management_audit (
 audit_id text PRIMARY KEY, actor_user_id uuid NOT NULL, target_user_id uuid NOT NULL,
 action text NOT NULL, resource_id text NOT NULL, reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['quota_limits','quota_accounts'] LOOP
 EXECUTE format('ALTER TABLE agent_business.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE agent_business.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY private_quota ON agent_business.%I TO p1_runtime '
 'USING(identity_business.actor_active() AND (user_id=identity_business.actor_id() OR identity_business.platform_admin())) '
 'WITH CHECK(identity_business.actor_active() AND (user_id=identity_business.actor_id() OR identity_business.platform_admin()))',t);
 EXECUTE format('CREATE POLICY accounting_control ON agent_business.%I TO p3_control USING(true) WITH CHECK(true)',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['usage_runs','quota_reservations','usage_attempts'] LOOP
 EXECUTE format('ALTER TABLE agent_business.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE agent_business.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY own_accounting ON agent_business.%I TO p1_runtime '
 'USING(identity_business.actor_active() AND (owner_user_id=identity_business.actor_id() OR identity_business.platform_admin())) '
 'WITH CHECK(identity_business.actor_active() AND (owner_user_id=identity_business.actor_id() OR identity_business.platform_admin()))',t);
 EXECUTE format('CREATE POLICY accounting_control ON agent_business.%I TO p3_control USING(true) WITH CHECK(true)',t);
 END LOOP; END $$;
ALTER TABLE agent_business.management_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_business.management_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY management_only ON agent_business.management_audit TO p1_runtime
 USING(identity_business.platform_admin()) WITH CHECK(identity_business.platform_admin() AND actor_user_id=identity_business.actor_id());
CREATE POLICY management_user_updates ON identity_business.users AS RESTRICTIVE FOR UPDATE
 USING(user_id=identity_business.actor_id() OR identity_business.platform_admin());
-- Existing own_identity remains restrictive about other users. Admin writes use a fixed audited function.
GRANT SELECT,INSERT,UPDATE ON agent_business.quota_limits,agent_business.quota_accounts,
 agent_business.usage_runs,agent_business.quota_reservations,agent_business.usage_attempts TO p1_runtime,p3_control;
GRANT SELECT,INSERT ON agent_business.management_audit TO p1_runtime;
CREATE POLICY operator_user_control ON identity_business.users TO p1_migrator USING(true) WITH CHECK(true);
CREATE POLICY operator_membership_control ON identity_business.memberships TO p1_migrator USING(true) WITH CHECK(true);
CREATE POLICY operator_workspace_control ON identity_business.workspaces TO p1_migrator USING(true) WITH CHECK(true);
CREATE POLICY operator_audit ON agent_business.management_audit TO p1_migrator USING(true) WITH CHECK(true);
CREATE FUNCTION identity_business.manage_user(target uuid, enabled boolean, note text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity_business,agent_business
AS $$ BEGIN
 IF NOT identity_business.platform_admin() OR target=identity_business.actor_id() THEN
 RAISE EXCEPTION 'management denied' USING ERRCODE='42501'; END IF;
 UPDATE identity_business.users SET status=CASE WHEN enabled THEN 'active' ELSE 'disabled' END,
 revoked_before=greatest(revoked_before,floor(extract(epoch FROM clock_timestamp()))::bigint)
 WHERE user_id=target;
 IF NOT FOUND THEN RAISE EXCEPTION 'account not found' USING ERRCODE='P0002'; END IF;
 INSERT INTO agent_business.management_audit VALUES(gen_random_uuid()::text,identity_business.actor_id(),
 target,'account_status',target::text,note,clock_timestamp()); END $$;
CREATE FUNCTION identity_business.manage_membership(target uuid, workspace text, membership_role text,
 enabled boolean, note text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,identity_business,agent_business
AS $$ BEGIN
 IF NOT identity_business.platform_admin() OR membership_role NOT IN ('member','reviewer','workspace_admin')
 OR workspace='workspace:legacy' THEN RAISE EXCEPTION 'management denied' USING ERRCODE='42501'; END IF;
 INSERT INTO identity_business.memberships VALUES(target,workspace,membership_role,enabled)
 ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=excluded.role,active=excluded.active;
 INSERT INTO agent_business.management_audit VALUES(gen_random_uuid()::text,identity_business.actor_id(),
 target,'membership',workspace,note,clock_timestamp()); END $$;
CREATE FUNCTION identity_business.management_users() RETURNS TABLE(user_id uuid,nickname text,status text)
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity_business
AS $$ BEGIN IF NOT identity_business.platform_admin() THEN
 RAISE EXCEPTION 'management denied' USING ERRCODE='42501'; END IF;
 RETURN QUERY SELECT u.user_id,u.nickname,u.status FROM identity_business.users u ORDER BY u.user_id LIMIT 100;
 END $$;
REVOKE ALL ON FUNCTION identity_business.manage_user(uuid,boolean,text),
 identity_business.manage_membership(uuid,text,text,boolean,text),identity_business.management_users() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity_business.manage_user(uuid,boolean,text),
 identity_business.manage_membership(uuid,text,text,boolean,text),identity_business.management_users() TO p1_runtime;
CREATE FUNCTION agent_business.immutable_accounting() RETURNS trigger LANGUAGE plpgsql
AS $$ BEGIN
 IF to_jsonb(NEW)->'owner_user_id' IS DISTINCT FROM to_jsonb(OLD)->'owner_user_id'
 OR to_jsonb(NEW)->'run_id' IS DISTINCT FROM to_jsonb(OLD)->'run_id' THEN
 RAISE EXCEPTION 'immutable accounting binding' USING ERRCODE='42501'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER immutable_usage_run BEFORE UPDATE ON agent_business.usage_runs
 FOR EACH ROW EXECUTE FUNCTION agent_business.immutable_accounting();
CREATE TRIGGER immutable_usage_attempt BEFORE UPDATE ON agent_business.usage_attempts
 FOR EACH ROW EXECUTE FUNCTION agent_business.immutable_accounting();
