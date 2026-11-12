-- =============================================================================
-- 004_functions.sql
-- The data-access contract for the registration service (commit 2).
-- Each call is one round trip, one transaction, and safe to retry.
--
--   upsert_registration(mobile)              -> after OTP verify succeeds
--   add_beneficiary(mobile, key, ...)        -> "add beneficiary" endpoint
--   list_beneficiaries(mobile)               -> "list beneficiaries" endpoint
--
-- Error codes (full table in db/README.md):
--   CW001 limit of 4 reached       CW002 mobile not registered/verified
--   CW003 photo ID already used    CW004 idempotency key reused, different body
--   CW005 immutable column         23514 invalid input (check_violation)
-- =============================================================================

-- Called after the OTP is verified (the OTP itself lives in Redis, commit 3).
-- Retry-safe: calling it twice leaves one row and only refreshes the timestamp.
-- created = true only the first time this mobile is seen.
CREATE FUNCTION upsert_registration(p_mobile text)
    RETURNS TABLE (mobile_number text, created boolean, beneficiary_count smallint)
    LANGUAGE plpgsql
AS $$
#variable_conflict use_column
BEGIN
    -- The usual "RETURNING (xmax = 0)" trick to tell insert from update does
    -- not work on partitioned tables (system columns are unavailable), so try
    -- the insert first and fall back to an update.
    RETURN QUERY
    INSERT INTO registrations AS r (mobile_number)
    VALUES (p_mobile)
    ON CONFLICT (mobile_number) DO NOTHING
    RETURNING r.mobile_number, true, r.beneficiary_count;
    IF FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
    UPDATE registrations AS r
       SET last_verified_at = now()
     WHERE r.mobile_number = p_mobile
    RETURNING r.mobile_number, false, r.beneficiary_count;
END;
$$;


-- Adds one beneficiary under a verified mobile number.
--
-- Idempotency (Sec 9.2 rule 3): the caller passes a key that stays the same
-- across retries of the same request (e.g. a UUID the client generates once).
--   * first call          -> row inserted, created = true
--   * retry, same body    -> original row returned, created = false, nothing new written
--   * same key, new body  -> CW004 (the key was reused by mistake)
-- Two concurrent calls with the same key: the second waits on the unique index,
-- then takes the "retry" path. Still exactly one row.
CREATE FUNCTION add_beneficiary(
        p_mobile           text,
        p_idempotency_key  text,
        p_full_name        text,
        p_birth_year       int,
        p_gender           gender_t,
        p_photo_id_type    photo_id_type_t,
        p_photo_id_number  text)
    RETURNS TABLE (beneficiary_id uuid, created boolean, created_at timestamptz)
    LANGUAGE plpgsql
AS $$
#variable_conflict use_column
DECLARE
    v_hash      bytea;
    v_id        uuid;
    v_at        timestamptz;
    v_old_hash  bytea;
BEGIN
    IF p_mobile IS NULL OR p_mobile !~ '^[6-9][0-9]{9}$' THEN
        RAISE EXCEPTION 'mobile number must be 10 digits starting with 6-9'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_birth_year IS NULL OR p_birth_year < 1900
       OR p_birth_year > extract(year FROM current_date)::int THEN
        RAISE EXCEPTION 'birth year % is not valid', p_birth_year
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM registrations r WHERE r.mobile_number = p_mobile) THEN
        RAISE EXCEPTION 'mobile % is not registered; verify OTP first', p_mobile
            USING ERRCODE = 'CW002';
    END IF;

    -- Fingerprint of the normalized request, to catch key reuse.
    v_hash := sha256(convert_to(concat_ws('|',
                  normalize_full_name(p_full_name),
                  p_birth_year::text,
                  p_gender::text,
                  p_photo_id_type::text,
                  normalize_photo_id(p_photo_id_number)), 'UTF8'));

    INSERT INTO beneficiaries AS b
        (mobile_number, full_name, birth_year, gender,
         photo_id_type, photo_id_number, idempotency_key, request_hash)
    VALUES
        (p_mobile, p_full_name, p_birth_year, p_gender,
         p_photo_id_type, p_photo_id_number, p_idempotency_key, v_hash)
    ON CONFLICT (mobile_number, idempotency_key) DO NOTHING
    RETURNING b.beneficiary_id, b.created_at INTO v_id, v_at;

    IF v_id IS NOT NULL THEN
        RETURN QUERY SELECT v_id, true, v_at;
        RETURN;
    END IF;

    -- Conflict: this key was already used for this mobile. Replay or reject.
    SELECT b.beneficiary_id, b.created_at, b.request_hash
      INTO v_id, v_at, v_old_hash
      FROM beneficiaries b
     WHERE b.mobile_number = p_mobile
       AND b.idempotency_key = p_idempotency_key;

    IF v_old_hash IS DISTINCT FROM v_hash THEN
        RAISE EXCEPTION 'idempotency key was already used for a different beneficiary'
            USING ERRCODE = 'CW004';
    END IF;

    RETURN QUERY SELECT v_id, false, v_at;
END;
$$;


-- Lists beneficiaries under a mobile. Touches one partition only (pruning on
-- mobile_number). The photo ID is masked except the last 4 characters, as
-- CoWIN showed it (privacy by design, Sec 7.1).
CREATE FUNCTION list_beneficiaries(p_mobile text)
    RETURNS TABLE (beneficiary_id  uuid,
                   full_name       text,
                   birth_year      smallint,
                   gender          gender_t,
                   photo_id_type   photo_id_type_t,
                   photo_id_masked text,
                   created_at      timestamptz)
    LANGUAGE sql STABLE
AS $$
    SELECT b.beneficiary_id,
           b.full_name,
           b.birth_year,
           b.gender,
           b.photo_id_type,
           repeat('X', greatest(char_length(b.photo_id_number) - 4, 0))
               || right(b.photo_id_number, 4),
           b.created_at
      FROM beneficiaries b
     WHERE b.mobile_number = p_mobile
     ORDER BY b.created_at, b.beneficiary_id;
$$;


-- Rows per partition for all three tables. Used to check that hash
-- partitioning spreads data evenly (and later as a baseline for E6).
CREATE VIEW partition_balance AS
SELECT 'registrations'::text AS table_name, tableoid::regclass::text AS partition, count(*) AS row_count
  FROM registrations GROUP BY tableoid
UNION ALL
SELECT 'beneficiaries', tableoid::regclass::text, count(*)
  FROM beneficiaries GROUP BY tableoid
UNION ALL
SELECT 'photo_id_registry', tableoid::regclass::text, count(*)
  FROM photo_id_registry GROUP BY tableoid;
