-- Sales organisation hierarchy (area roles). Fictional persons, real administrative areas.
--   head_of_region (province) -> head_of_sales (EAST/WEST, 19 regencies each) -> head_of_branch (1 regency)
--   -> territory_manager (<=3 districts of one regency) -> sales (1 district)
-- Authorization stays default-deny through principal_grant (district grants expanded from coverage); this table
-- describes WHO is responsible for WHICH area and the reporting line used for approvals.
SET search_path=telco,public;

CREATE TABLE org_position(id integer PRIMARY KEY, code text UNIQUE NOT NULL,
 level text NOT NULL CHECK(level IN ('head_of_region','head_of_sales','head_of_branch','territory_manager','sales')),
 title text NOT NULL, parent_id integer REFERENCES org_position, sales_area text CHECK(sales_area IN ('EAST','WEST')),
 sales_id integer UNIQUE REFERENCES sales, is_synthetic_person boolean NOT NULL DEFAULT true,
 CHECK((level='sales')=(sales_id IS NOT NULL)),
 CHECK((level='head_of_region')=(parent_id IS NULL)));

CREATE TABLE org_territory(position_id integer NOT NULL REFERENCES org_position,
 scope_type text NOT NULL CHECK(scope_type IN ('province','regency','district')), entity_id integer NOT NULL,
 PRIMARY KEY(position_id,scope_type,entity_id));

-- Parent level must be exactly one step up; territory references must exist and match the level.
CREATE FUNCTION guard_org_position() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
DECLARE parent_level text; expected text;
BEGIN
 expected := CASE NEW.level WHEN 'head_of_sales' THEN 'head_of_region' WHEN 'head_of_branch' THEN 'head_of_sales'
   WHEN 'territory_manager' THEN 'head_of_branch' WHEN 'sales' THEN 'territory_manager' END;
 IF NEW.parent_id IS NOT NULL THEN
  SELECT level INTO parent_level FROM org_position WHERE id=NEW.parent_id;
  IF parent_level IS DISTINCT FROM expected THEN RAISE EXCEPTION 'org parent level mismatch for %', NEW.code; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER org_position_guard BEFORE INSERT OR UPDATE ON org_position FOR EACH ROW EXECUTE FUNCTION guard_org_position();

CREATE FUNCTION guard_org_territory() RETURNS trigger LANGUAGE plpgsql SET search_path=telco,pg_temp AS $$
DECLARE lvl text;
BEGIN
 SELECT level INTO lvl FROM org_position WHERE id=NEW.position_id;
 IF (NEW.scope_type='province' AND (lvl<>'head_of_region' OR NOT EXISTS(SELECT 1 FROM province WHERE id=NEW.entity_id)))
 OR (NEW.scope_type='regency' AND (lvl NOT IN ('head_of_sales','head_of_branch') OR NOT EXISTS(SELECT 1 FROM regency WHERE id=NEW.entity_id)))
 OR (NEW.scope_type='district' AND (lvl NOT IN ('territory_manager','sales') OR NOT EXISTS(SELECT 1 FROM district WHERE id=NEW.entity_id)))
 THEN RAISE EXCEPTION 'invalid org territory % % for level %', NEW.scope_type, NEW.entity_id, lvl; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER org_territory_guard BEFORE INSERT OR UPDATE ON org_territory FOR EACH ROW EXECUTE FUNCTION guard_org_territory();

-- Every position's coverage expanded to districts (used for grant provisioning and audits).
CREATE VIEW v_org_coverage AS
 SELECT t.position_id, d.id AS district_id FROM org_territory t JOIN district d ON
   (t.scope_type='district' AND d.id=t.entity_id) OR
   (t.scope_type='regency' AND d.regency_id=t.entity_id) OR
   (t.scope_type='province' AND d.regency_id IN (SELECT id FROM regency WHERE province_id=t.entity_id));

-- Reporting chain (self first, then managers up to head of region).
CREATE VIEW v_org_chain AS
 WITH RECURSIVE c AS (SELECT id AS position_id, id AS ancestor_id, 0 AS depth FROM org_position
   UNION ALL SELECT c.position_id, p.parent_id, c.depth+1 FROM c JOIN org_position p ON p.id=c.ancestor_id WHERE p.parent_id IS NOT NULL)
 SELECT * FROM c;

ALTER TABLE app_principal ADD COLUMN org_position_id integer UNIQUE REFERENCES org_position;
