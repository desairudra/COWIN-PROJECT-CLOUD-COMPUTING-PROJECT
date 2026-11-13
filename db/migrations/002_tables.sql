-- =============================================================================
-- 002_tables.sql
-- Registrations, beneficiaries and the global photo ID registry.
--
-- Partitioning (Sec 9.4, "PostgreSQL declarative partitioning"):
--   registrations  -> HASH(mobile_number)
--   beneficiaries  -> HASH(mobile_number)   same modulus, so rows for one mobile
--                                           sit in partitions with the same index
--   photo_id_registry -> HASH(photo_id_number)
--
-- Why a separate photo_id_registry table?
--   PostgreSQL only enforces a UNIQUE constraint on a partitioned table if the
--   constraint includes the partition key. beneficiaries is partitioned by
--   mobile_number, so UNIQUE(photo_id_type, photo_id_number) cannot be declared
--   on it. The same photo ID could sit under two mobiles in two different
--   partitions. The registry is partitioned by the photo ID itself, so its
--   primary key IS a global uniqueness guarantee. A trigger (003) writes it in
--   the same transaction as the beneficiary row.
--   The same idea carries over to sharding in commit 4: the registry is keyed by
--   photo ID, not mobile, so it needs its own routing rule.
-- =============================================================================

-- One row per verified mobile number (the "account" that owns beneficiaries).
-- Created or refreshed after a successful OTP verification. The OTP itself
-- never touches PostgreSQL; it lives in Redis with a TTL (commit 3).
CREATE TABLE registrations (
    mobile_number      text        NOT NULL,
    beneficiary_count  smallint    NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_verified_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT registrations_pk PRIMARY KEY (mobile_number),
    -- Indian mobile numbers: 10 digits, starting 6-9.
    CONSTRAINT registrations_mobile_format CHECK (mobile_number ~ '^[6-9][0-9]{9}$'),
    -- Rule 1 (Sec 9.2): at most 4 beneficiaries per mobile number.
    -- Last line of defence: even a buggy trigger cannot push this past 4.
    CONSTRAINT registrations_max_4_beneficiaries CHECK (beneficiary_count BETWEEN 0 AND 4)
) PARTITION BY HASH (mobile_number);

SELECT create_hash_partitions('registrations', configured_partition_count());


-- One row per person registered for vaccination.
CREATE TABLE beneficiaries (
    beneficiary_id     uuid            NOT NULL DEFAULT gen_random_uuid(),
    mobile_number      text            NOT NULL,
    full_name          text            NOT NULL,
    -- Report Sec 9.2 says "age". We store year of birth, as CoWIN did,
    -- because a stored age goes stale. Age = current year - birth_year.
    birth_year         smallint        NOT NULL,
    gender             gender_t        NOT NULL,
    photo_id_type      photo_id_type_t NOT NULL,
    photo_id_number    text            NOT NULL,   -- stored normalized (trigger in 003)
    -- Rule 3 (Sec 9.2): a retried request never creates a duplicate record.
    -- The client sends the same key on every retry of the same request.
    idempotency_key    text            NOT NULL,
    -- SHA-256 of the normalized request body, so the same key cannot be
    -- silently reused for a different person.
    request_hash       bytea           NOT NULL,
    created_at         timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT beneficiaries_pk PRIMARY KEY (mobile_number, beneficiary_id),
    CONSTRAINT beneficiaries_idempotency_uq UNIQUE (mobile_number, idempotency_key),
    CONSTRAINT beneficiaries_registration_fk FOREIGN KEY (mobile_number)
        REFERENCES registrations (mobile_number) ON DELETE CASCADE,

    CONSTRAINT beneficiaries_name_len     CHECK (char_length(full_name) BETWEEN 1 AND 100),
    CONSTRAINT beneficiaries_birth_year   CHECK (birth_year BETWEEN 1900 AND 2100),
    CONSTRAINT beneficiaries_photo_id_fmt CHECK (photo_id_number ~ '^[A-Z0-9]{4,20}$'),
    CONSTRAINT beneficiaries_idem_key_fmt CHECK (idempotency_key ~ '^[A-Za-z0-9_\-]{8,64}$'),
    CONSTRAINT beneficiaries_hash_len     CHECK (octet_length(request_hash) = 32)
) PARTITION BY HASH (mobile_number);

SELECT create_hash_partitions('beneficiaries', configured_partition_count());


-- Rule 2 (Sec 9.2): the same photo ID is never registered twice, across ALL
-- mobiles and ALL partitions. Uniqueness is by (type, number): the same digits on
-- a PAN and on a passport are different documents.
CREATE TABLE photo_id_registry (
    photo_id_type      photo_id_type_t NOT NULL,
    photo_id_number    text            NOT NULL,
    mobile_number      text            NOT NULL,
    beneficiary_id     uuid            NOT NULL,
    created_at         timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT photo_id_registry_pk PRIMARY KEY (photo_id_type, photo_id_number),
    -- Deleting a beneficiary frees its photo ID.
    CONSTRAINT photo_id_registry_beneficiary_fk FOREIGN KEY (mobile_number, beneficiary_id)
        REFERENCES beneficiaries (mobile_number, beneficiary_id) ON DELETE CASCADE
) PARTITION BY HASH (photo_id_number);

SELECT create_hash_partitions('photo_id_registry', configured_partition_count());

-- Supports the FK's ON DELETE CASCADE lookup (otherwise a scan of every partition).
CREATE INDEX photo_id_registry_beneficiary_idx
    ON photo_id_registry (mobile_number, beneficiary_id);


COMMENT ON TABLE registrations IS
    'One row per OTP-verified mobile number. HASH partitioned on mobile_number.';
COMMENT ON TABLE beneficiaries IS
    'People registered under a mobile (max 4). HASH partitioned on mobile_number.';
COMMENT ON TABLE photo_id_registry IS
    'Global uniqueness index for photo IDs. HASH partitioned on photo_id_number.';
COMMENT ON COLUMN beneficiaries.idempotency_key IS
    'Client-supplied key (e.g. Idempotency-Key header). Unique per mobile; a retry returns the original row.';
