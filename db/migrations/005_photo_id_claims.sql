-- Commit 16: photo ID claims, for running on several databases (shards).
--
-- With one database, photo_id_registry (002) keeps every photo ID unique. When
-- registrations are split over several databases by mobile number, each
-- database only sees its own share, so two shards could each accept the same
-- PAN. The service (service/app/db.py, ShardedDatabase) therefore first claims
-- the photo ID here, on the shard chosen by the photo ID number, and only then
-- adds the beneficiary on the shard chosen by the mobile number. If adding
-- fails, it releases the claim.
--
-- Unused (and empty) when the service runs on one database.

CREATE TABLE photo_id_claims (
    photo_id_type    photo_id_type_t NOT NULL,
    photo_id_number  text            NOT NULL,
    mobile_number    text            NOT NULL,
    idempotency_key  text            NOT NULL,
    claimed_at       timestamptz     NOT NULL DEFAULT now(),
    CONSTRAINT photo_id_claims_pk PRIMARY KEY (photo_id_type, photo_id_number)
);

-- true: the ID is now held by this (mobile, key), either just now or by an
-- earlier try of the same request. false: someone else holds it.
CREATE FUNCTION claim_photo_id(p_type photo_id_type_t, p_number text, p_mobile text, p_key text)
    RETURNS boolean
    LANGUAGE plpgsql
AS $$
DECLARE
    v_number text := normalize_photo_id(p_number);
BEGIN
    INSERT INTO photo_id_claims (photo_id_type, photo_id_number, mobile_number, idempotency_key)
    VALUES (p_type, v_number, p_mobile, p_key)
    ON CONFLICT DO NOTHING;
    IF FOUND THEN
        RETURN true;
    END IF;
    RETURN EXISTS (SELECT 1 FROM photo_id_claims c
                    WHERE c.photo_id_type = p_type AND c.photo_id_number = v_number
                      AND c.mobile_number = p_mobile AND c.idempotency_key = p_key);
END;
$$;

-- Gives a claim back. Only the holder can release it.
CREATE FUNCTION release_photo_id(p_type photo_id_type_t, p_number text, p_mobile text, p_key text)
    RETURNS boolean
    LANGUAGE sql
AS $$
    WITH gone AS (
        DELETE FROM photo_id_claims c
         WHERE c.photo_id_type = p_type AND c.photo_id_number = normalize_photo_id(p_number)
           AND c.mobile_number = p_mobile AND c.idempotency_key = p_key
        RETURNING 1)
    SELECT EXISTS (SELECT 1 FROM gone);
$$;
