SET search_path=telco,public;
-- Server-verified principal subject; deliberately contains no phone number/email.
CREATE TABLE principal_grant(principal_id text NOT NULL, scope_type text NOT NULL CHECK(scope_type IN ('outlet','district')),
 entity_id integer NOT NULL, granted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(principal_id,scope_type,entity_id));
CREATE FUNCTION guard_principal_grant() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
BEGIN
 IF NEW.scope_type='outlet' AND NOT EXISTS(SELECT 1 FROM outlet WHERE id=NEW.entity_id) THEN RAISE EXCEPTION 'unknown outlet grant'; END IF;
 IF NEW.scope_type='district' AND NOT EXISTS(SELECT 1 FROM district WHERE id=NEW.entity_id) THEN RAISE EXCEPTION 'unknown district grant'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER principal_grant_ref BEFORE INSERT OR UPDATE ON principal_grant FOR EACH ROW EXECUTE FUNCTION guard_principal_grant();
CREATE FUNCTION can_access(p_principal text,p_scope text,p_id integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path=telco,pg_temp AS $$
 SELECT EXISTS(SELECT 1 FROM principal_grant g WHERE g.principal_id=p_principal AND (
 (g.scope_type=p_scope AND g.entity_id=p_id) OR
 (p_scope='outlet' AND g.scope_type='district' AND EXISTS(SELECT 1 FROM outlet o WHERE o.id=p_id AND o.district_id=g.entity_id))));
$$;
