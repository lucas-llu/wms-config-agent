-- Preserve checksum of already-applied 007; fail closed after metadata loss.
SET LOCAL ROLE p1_migrator;
CREATE OR REPLACE FUNCTION agent_business.release_phase() RETURNS text LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,agent_business AS $$ DECLARE value text; BEGIN
 SELECT phase INTO value FROM agent_business.release_state WHERE singleton FOR SHARE;
 IF value IS NULL THEN RAISE EXCEPTION 'release state unavailable' USING ERRCODE='55000'; END IF;
 RETURN value; END $$;
