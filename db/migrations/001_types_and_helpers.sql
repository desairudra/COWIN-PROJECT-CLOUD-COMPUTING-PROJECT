-- =============================================================================
-- 001_types_and_helpers.sql
-- CoWIN registration module: shared types and helper functions.
-- Report refs: Sec 9.2 (component + rules), Sec 9.4 (PostgreSQL, partitioning)
-- Requires PostgreSQL 13+ (gen_random_uuid() built in, BEFORE row triggers on
-- partitioned tables, foreign keys that reference partitioned tables).
-- =============================================================================

-- Gender values as collected at CoWIN registration.
CREATE TYPE gender_t AS ENUM ('MALE', 'FEMALE', 'OTHER');

-- The nine photo ID types CoWIN accepted for self-registration
-- (the original seven, plus UDID card and ration card with photo, which
-- were added later). Week 2 report Sec 3.3: "nine ID types accepted [2]".
CREATE TYPE photo_id_type_t AS ENUM (
    'AADHAAR',
    'DRIVING_LICENSE',
    'PAN',
    'PASSPORT',
    'PENSION_PASSBOOK',
    'NPR_SMART_CARD',
    'VOTER_ID',
    'UDID',
    'RATION_CARD'
);

-- Business constant: max beneficiaries under one mobile number.
-- 4 is the figure in the NHA deck and the MoHFW citizen FAQ (Sec 9.2).
-- The CHECK constraint on registrations.beneficiary_count uses the literal 4;
-- change both together if the rule ever changes.
CREATE FUNCTION max_beneficiaries_per_mobile() RETURNS smallint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT 4::smallint $$;

-- Canonical form of a photo ID number, so "1234 5678 9012", "1234-5678-9012"
-- and "123456789012" are treated as the same document.
CREATE FUNCTION normalize_photo_id(raw text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT upper(regexp_replace(raw, '[\s\-/.]', '', 'g')) $$;

-- Canonical form of a name: trimmed, inner whitespace collapsed to one space.
CREATE FUNCTION normalize_full_name(raw text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT regexp_replace(btrim(raw), '\s+', ' ', 'g') $$;

-- Number of hash partitions per table. Read from the custom setting
-- cowin.partitions so experiments (E6) can change it without editing SQL:
--   PGOPTIONS='-c cowin.partitions=16' psql -f ...
-- Defaults to 8.
CREATE FUNCTION configured_partition_count() RETURNS int
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(nullif(current_setting('cowin.partitions', true), ''), '8')::int $$;

-- Creates <parent>_p0 .. <parent>_p(n-1) as hash partitions of <parent>.
CREATE FUNCTION create_hash_partitions(parent regclass, modulus int)
    RETURNS void
    LANGUAGE plpgsql
AS $$
DECLARE
    i int;
    parent_name text := (SELECT relname FROM pg_class WHERE oid = parent);
BEGIN
    IF modulus < 1 OR modulus > 256 THEN
        RAISE EXCEPTION 'partition count must be between 1 and 256, got %', modulus;
    END IF;
    FOR i IN 0 .. modulus - 1 LOOP
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %s FOR VALUES WITH (MODULUS %s, REMAINDER %s)',
            parent_name || '_p' || i, parent, modulus, i);
    END LOOP;
END;
$$;
