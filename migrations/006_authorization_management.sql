SET LOCAL ROLE p1_migrator;
ALTER TABLE identity_business.workspaces ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE identity_business.memberships ADD COLUMN revision integer NOT NULL DEFAULT 1;
CREATE FUNCTION identity_business.authorization_snapshot() RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity_business
AS $$ BEGIN
 IF NOT identity_business.platform_admin() THEN RAISE EXCEPTION 'management denied' USING ERRCODE='42501'; END IF;
 RETURN jsonb_build_object('workspaces',coalesce((SELECT jsonb_agg(row_to_json(w)) FROM
 (SELECT workspace_id,policy_json::jsonb AS policy,revision FROM identity_business.workspaces
 WHERE workspace_id<>'workspace:legacy' ORDER BY workspace_id LIMIT 100) w),'[]'::jsonb),
 'memberships',coalesce((SELECT jsonb_agg(row_to_json(m)) FROM
 (SELECT user_id,workspace_id,role,active,revision FROM identity_business.memberships
 ORDER BY workspace_id,user_id LIMIT 1000) m),'[]'::jsonb)); END $$;
CREATE FUNCTION identity_business.manage_scope(workspace text,payload jsonb,expected integer,note text)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,identity_business,agent_business
AS $$ DECLARE current_revision integer; dimension text; BEGIN
 IF NOT identity_business.platform_admin() OR workspace='workspace:legacy'
 OR workspace !~ '^workspace:[A-Za-z0-9_-]{1,64}$' OR payload->>'workspace_id'<>workspace
 OR coalesce(length(trim(payload->>'name')),0)=0 THEN RAISE EXCEPTION 'invalid scope' USING ERRCODE='42501'; END IF;
 FOREACH dimension IN ARRAY ARRAY['collections','modules','sites','environments'] LOOP
 IF jsonb_typeof(payload->dimension)<>'array' OR jsonb_array_length(payload->dimension) NOT BETWEEN 1 AND 100
 OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(payload->dimension) v WHERE v='*' OR length(trim(v))=0)
 THEN RAISE EXCEPTION 'explicit scope required' USING ERRCODE='22023'; END IF;
 END LOOP;
 PERFORM pg_advisory_xact_lock(hashtextextended(workspace,9134205));
 SELECT revision INTO current_revision FROM identity_business.workspaces WHERE workspace_id=workspace FOR UPDATE;
 IF coalesce(current_revision,0)<>expected THEN RAISE EXCEPTION 'scope changed' USING ERRCODE='40001'; END IF;
 INSERT INTO identity_business.workspaces(workspace_id,policy_json,revision) VALUES(workspace,payload::text,1)
 ON CONFLICT(workspace_id) DO UPDATE SET policy_json=excluded.policy_json,revision=workspaces.revision+1
 RETURNING revision INTO current_revision;
 INSERT INTO agent_business.management_audit VALUES(gen_random_uuid()::text,identity_business.actor_id(),
 identity_business.actor_id(),'workspace_scope',workspace,note,clock_timestamp());
 RETURN current_revision; END $$;
CREATE FUNCTION identity_business.manage_membership_v2(target uuid,workspace text,membership_role text,
 enabled boolean,expected integer,note text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,identity_business,agent_business
AS $$ DECLARE current_revision integer; BEGIN
 IF NOT identity_business.platform_admin() OR membership_role NOT IN ('member','reviewer','workspace_admin')
 OR workspace='workspace:legacy' THEN RAISE EXCEPTION 'management denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(target::text||workspace,9134206));
 SELECT revision INTO current_revision FROM identity_business.memberships WHERE user_id=target AND workspace_id=workspace FOR UPDATE;
 IF coalesce(current_revision,0)<>expected THEN RAISE EXCEPTION 'membership changed' USING ERRCODE='40001'; END IF;
 INSERT INTO identity_business.memberships(user_id,workspace_id,role,active,revision) VALUES(target,workspace,membership_role,enabled,1)
 ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=excluded.role,active=excluded.active,revision=memberships.revision+1
 RETURNING revision INTO current_revision;
 INSERT INTO agent_business.management_audit VALUES(gen_random_uuid()::text,identity_business.actor_id(),target,
 'membership',workspace,note,clock_timestamp()); RETURN current_revision; END $$;
REVOKE ALL ON FUNCTION identity_business.manage_membership(uuid,text,text,boolean,text) FROM p1_runtime;
REVOKE ALL ON FUNCTION identity_business.authorization_snapshot(),identity_business.manage_scope(text,jsonb,integer,text),
 identity_business.manage_membership_v2(uuid,text,text,boolean,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity_business.authorization_snapshot(),identity_business.manage_scope(text,jsonb,integer,text),
 identity_business.manage_membership_v2(uuid,text,text,boolean,integer,text) TO p1_runtime;
