-- =============================================================================
-- 003_triggers.sql
-- Database-enforced invariants. These hold no matter which service, script or
-- psql session writes the data. The API (commit 2) cannot bypass them.
--
-- Error codes raised here (catalogue in db/README.md, used by commit 2 to map
-- to HTTP responses):
--   CW001  beneficiary limit (4) reached for this mobile
--   CW003  photo ID already registered
--   CW005  attempt to change an immutable column
-- =============================================================================

-- ---------------------------------------------------------------------------
-- BEFORE INSERT/UPDATE: normalize input, protect immutable columns.
-- Runs before CHECK constraints, so the format CHECK sees normalized values.
-- ---------------------------------------------------------------------------
CREATE FUNCTION trg_beneficiaries_before_write() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.full_name       := normalize_full_name(NEW.full_name);
    NEW.photo_id_number := normalize_photo_id(NEW.photo_id_number);

    IF TG_OP = 'UPDATE' THEN
        IF NEW.mobile_number   IS DISTINCT FROM OLD.mobile_number
        OR NEW.beneficiary_id  IS DISTINCT FROM OLD.beneficiary_id
        OR NEW.photo_id_type   IS DISTINCT FROM OLD.photo_id_type
        OR NEW.photo_id_number IS DISTINCT FROM OLD.photo_id_number
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
            RAISE EXCEPTION 'mobile_number, beneficiary_id, photo ID and idempotency_key cannot be changed; delete and re-add instead'
                USING ERRCODE = 'CW005';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER beneficiaries_before_write
    BEFORE INSERT OR UPDATE ON beneficiaries
    FOR EACH ROW EXECUTE FUNCTION trg_beneficiaries_before_write();


-- ---------------------------------------------------------------------------
-- AFTER INSERT: enforce the 4-per-mobile limit and claim the photo ID.
--
-- This is deliberately an AFTER trigger. With
--   INSERT ... ON CONFLICT (mobile_number, idempotency_key) DO NOTHING
-- BEFORE triggers still fire for a row that then turns out to be a duplicate.
-- A BEFORE trigger would bump the counter for a retry that inserts nothing.
-- AFTER triggers fire only for rows that were actually inserted.
--
-- Concurrency: the UPDATE takes a row lock on the registrations row, so
-- concurrent inserts for the same mobile queue up one at a time. The guard
-- "beneficiary_count < 4" is re-checked after the lock is acquired (READ
-- COMMITTED re-evaluates the WHERE on the latest row version). So exactly 4
-- succeed even under a burst of parallel requests.
--
-- Lock order is always: registrations row -> photo_id_registry key. No two
-- transactions take them in opposite orders, so this cannot deadlock.
-- ---------------------------------------------------------------------------
CREATE FUNCTION trg_beneficiaries_after_insert() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE registrations
       SET beneficiary_count = beneficiary_count + 1
     WHERE mobile_number = NEW.mobile_number
       AND beneficiary_count < max_beneficiaries_per_mobile();

    IF NOT FOUND THEN
        RAISE EXCEPTION 'mobile % already has the maximum of % beneficiaries',
                        NEW.mobile_number, max_beneficiaries_per_mobile()
            USING ERRCODE = 'CW001';
    END IF;

    INSERT INTO photo_id_registry (photo_id_type, photo_id_number, mobile_number, beneficiary_id)
    VALUES (NEW.photo_id_type, NEW.photo_id_number, NEW.mobile_number, NEW.beneficiary_id)
    ON CONFLICT (photo_id_type, photo_id_number) DO NOTHING;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'photo ID % is already registered', NEW.photo_id_type
            USING ERRCODE = 'CW003';   -- the number is deliberately not echoed (privacy, Sec 7.1)
    END IF;

    RETURN NULL;
END;
$$;

CREATE TRIGGER beneficiaries_after_insert
    AFTER INSERT ON beneficiaries
    FOR EACH ROW EXECUTE FUNCTION trg_beneficiaries_after_insert();


-- ---------------------------------------------------------------------------
-- AFTER DELETE: give the slot back. The photo ID is freed by the FK cascade.
-- When the whole registration is deleted, the cascade removes the parent row
-- first, so this UPDATE matches nothing. That is harmless.
-- ---------------------------------------------------------------------------
CREATE FUNCTION trg_beneficiaries_after_delete() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE registrations
       SET beneficiary_count = beneficiary_count - 1
     WHERE mobile_number = OLD.mobile_number;
    RETURN NULL;
END;
$$;

CREATE TRIGGER beneficiaries_after_delete
    AFTER DELETE ON beneficiaries
    FOR EACH ROW EXECUTE FUNCTION trg_beneficiaries_after_delete();


-- ---------------------------------------------------------------------------
-- The counter is owned by the triggers. Block direct edits so it can never
-- drift from the real number of rows.
-- ---------------------------------------------------------------------------
CREATE FUNCTION trg_registrations_protect_count() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.beneficiary_count := 0;          -- a new mobile always starts empty
    ELSIF pg_trigger_depth() = 1             -- depth 1 = a direct UPDATE, not our triggers
      AND NEW.beneficiary_count IS DISTINCT FROM OLD.beneficiary_count THEN
        RAISE EXCEPTION 'beneficiary_count is maintained by triggers and cannot be set directly'
            USING ERRCODE = 'CW005';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER registrations_protect_count
    BEFORE INSERT OR UPDATE OF beneficiary_count ON registrations
    FOR EACH ROW EXECUTE FUNCTION trg_registrations_protect_count();
