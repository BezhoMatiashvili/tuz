-- Private learning store; no grants/default privileges from the legacy enrichment schema.
CREATE SCHEMA automotive_knowledge;
REVOKE ALL ON SCHEMA automotive_knowledge FROM PUBLIC;
COMMENT ON SCHEMA automotive_knowledge IS
    'Private machine-validated knowledge, never human VERIFIED evidence or a Data API schema.';

CREATE TABLE automotive_knowledge.source_policies (
    source TEXT PRIMARY KEY,
    reuse_permission TEXT NOT NULL CHECK (reuse_permission IN ('owned','authorized','unknown')),
    dataset TEXT NOT NULL,
    configured_by TEXT NOT NULL,
    configured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE automotive_knowledge.vehicle_profiles (
    profile_key TEXT PRIMARY KEY CHECK (length(profile_key) = 64),
    configuration JSONB NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE automotive_knowledge.candidate_jobs (
    job_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    profile_key TEXT NOT NULL REFERENCES automotive_knowledge.vehicle_profiles,
    category TEXT NOT NULL,
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX knowledge_jobs_claim ON automotive_knowledge.candidate_jobs(status, created_at);
CREATE TABLE automotive_knowledge.validation_audits (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES automotive_knowledge.candidate_jobs,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','completed','technical_failure','expired')),
    context JSONB,
    research JSONB,
    verdicts JSONB,
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE TABLE automotive_knowledge.accepted_facts (
    fact_id TEXT PRIMARY KEY,
    profile_key TEXT NOT NULL REFERENCES automotive_knowledge.vehicle_profiles,
    category TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vehicle_identity','oil_viscosity',
        'manufacturer_approval','capacity','product_recommendation','part_number','fitment')),
    slot TEXT NOT NULL,
    value JSONB NOT NULL,
    provenance JSONB NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES automotive_knowledge.validation_audits,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','suspended','revoked')),
    revocation_reason TEXT,
    revoked_by TEXT,
    revoked_at TIMESTAMPTZ,
    UNIQUE (attempt_id, fact_id)
);
CREATE INDEX knowledge_facts_lookup ON automotive_knowledge.accepted_facts(profile_key, category);
CREATE TABLE automotive_knowledge.daily_quota (
    utc_day DATE PRIMARY KEY,
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 0 AND 25)
);

-- Runtime can only enqueue through this function. Policy is owned by the operator;
-- neither the model, runtime nor validator can grant a provider reuse permission.
CREATE FUNCTION automotive_knowledge.enqueue(p_id TEXT, p_fingerprint TEXT,
    p_profile TEXT, p_context JSONB, p_claims JSONB) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, automotive_knowledge AS $$
DECLARE filtered JSONB; inserted INTEGER;
BEGIN
    SELECT jsonb_agg(c) INTO filtered FROM jsonb_array_elements(p_claims) c
    JOIN automotive_knowledge.source_policies p ON p.source = c->>'source'
    WHERE p.reuse_permission IN ('owned','authorized');
    IF filtered IS NULL OR octet_length(p_context::TEXT) > 32000
        OR octet_length(filtered::TEXT) > 500000 THEN RETURN FALSE; END IF;
    INSERT INTO automotive_knowledge.vehicle_profiles(profile_key, configuration)
        VALUES (p_profile, p_context->'vehicle') ON CONFLICT DO NOTHING;
    IF NOT EXISTS (SELECT 1 FROM automotive_knowledge.vehicle_profiles
        WHERE profile_key=p_profile AND configuration=p_context->'vehicle') THEN
        RAISE EXCEPTION 'Profile collision';
    END IF;
    INSERT INTO automotive_knowledge.candidate_jobs
        (job_id, fingerprint, profile_key, category, payload)
        VALUES (p_id, p_fingerprint, p_profile, p_context->>'category',
                jsonb_build_object('context',p_context,'claims',filtered))
        ON CONFLICT (fingerprint) DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    -- New contrary observations remove old facts from reuse immediately. No runtime
    -- operation can unsuspend, publish, delete or revoke a fact.
    UPDATE automotive_knowledge.accepted_facts f SET state='suspended'
    WHERE f.profile_key=p_profile AND f.state='active' AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(filtered) c WHERE c->>'kind'=f.kind
        AND c->>'category'=f.category AND c->>'slot'=f.slot AND c->'value'<>f.value);
    RETURN inserted > 0;
END $$;

-- Serialize accounting and claims across processes, including crashed workers.
CREATE FUNCTION automotive_knowledge.claim_job(p_token TEXT, p_model TEXT, p_prompt TEXT,
    p_cap INTEGER DEFAULT 25, p_concurrency INTEGER DEFAULT 1)
RETURNS SETOF automotive_knowledge.candidate_jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, automotive_knowledge AS $$
DECLARE j automotive_knowledge.candidate_jobs; used INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(1728036742);
    UPDATE automotive_knowledge.validation_audits a SET status='expired', completed_at=now()
        FROM automotive_knowledge.candidate_jobs c WHERE a.attempt_id=c.lease_token
        AND a.status='running' AND c.lease_until<=now();
    UPDATE automotive_knowledge.candidate_jobs SET status='failed',updated_at=now()
        WHERE status='running' AND lease_until<=now() AND attempts>=2;
    IF (SELECT count(*) FROM automotive_knowledge.candidate_jobs
        WHERE status='running' AND lease_until>now()) >= least(greatest(p_concurrency,1),4)
        THEN RETURN; END IF;
    INSERT INTO automotive_knowledge.daily_quota VALUES ((now() AT TIME ZONE 'UTC')::DATE,0)
        ON CONFLICT DO NOTHING;
    SELECT attempts INTO used FROM automotive_knowledge.daily_quota
        WHERE utc_day=(now() AT TIME ZONE 'UTC')::DATE FOR UPDATE;
    IF used >= least(greatest(p_cap,0),25) THEN RETURN; END IF;
    SELECT * INTO j FROM automotive_knowledge.candidate_jobs
        WHERE (status='pending' OR (status='running' AND lease_until<=now())) AND attempts<2
        AND payload IS NOT NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN; END IF;
    UPDATE automotive_knowledge.daily_quota SET attempts=attempts+1
        WHERE utc_day=(now() AT TIME ZONE 'UTC')::DATE;
    UPDATE automotive_knowledge.candidate_jobs SET status='running',attempts=attempts+1,
        lease_token=p_token,lease_until=now()+INTERVAL '130 seconds',updated_at=now()
        WHERE job_id=j.job_id RETURNING * INTO j;
    INSERT INTO automotive_knowledge.validation_audits
        (attempt_id,job_id,model,prompt_version,status,context)
        VALUES (p_token,j.job_id,p_model,p_prompt,'running',j.payload->'context');
    RETURN NEXT j;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['source_policies','vehicle_profiles','candidate_jobs',
        'validation_audits','accepted_facts','daily_quota'] LOOP
        EXECUTE format('ALTER TABLE automotive_knowledge.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY validator_access ON automotive_knowledge.%I FOR ALL '
            || 'USING (EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''automotive_validator'' '
            || 'AND pg_has_role(current_user,oid,''member''))) WITH CHECK '
            || '(EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''automotive_validator'' '
            || 'AND pg_has_role(current_user,oid,''member'')))',t);
    END LOOP;
END $$;
CREATE POLICY runtime_facts ON automotive_knowledge.accepted_facts FOR SELECT USING (
    state='active' AND valid_until>now() AND EXISTS (SELECT 1 FROM pg_roles
    WHERE rolname='automotive_runtime' AND pg_has_role(current_user,oid,'member')));
CREATE POLICY runtime_policies ON automotive_knowledge.source_policies FOR SELECT USING (
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname='automotive_runtime'
    AND pg_has_role(current_user,oid,'member')));
REVOKE ALL ON ALL TABLES IN SCHEMA automotive_knowledge FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA automotive_knowledge FROM PUBLIC;

-- This helper is safe to rerun during provisioning, including installations where
-- roles did not exist when the migration ran. It never changes role membership.
CREATE FUNCTION automotive_knowledge.provision() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='automotive_runtime') THEN
        GRANT USAGE ON SCHEMA automotive_knowledge TO automotive_runtime;
        GRANT SELECT ON automotive_knowledge.accepted_facts,
            automotive_knowledge.source_policies TO automotive_runtime;
        GRANT EXECUTE ON FUNCTION automotive_knowledge.enqueue(TEXT,TEXT,TEXT,JSONB,JSONB)
            TO automotive_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='automotive_validator') THEN
        GRANT USAGE ON SCHEMA automotive_knowledge TO automotive_validator;
        GRANT SELECT ON ALL TABLES IN SCHEMA automotive_knowledge TO automotive_validator;
        GRANT INSERT, UPDATE ON automotive_knowledge.accepted_facts,
            automotive_knowledge.validation_audits TO automotive_validator;
        GRANT UPDATE ON automotive_knowledge.candidate_jobs TO automotive_validator;
        GRANT EXECUTE ON FUNCTION automotive_knowledge.claim_job(TEXT,TEXT,TEXT,INTEGER,INTEGER)
            TO automotive_validator;
    END IF;
END $$;
REVOKE ALL ON FUNCTION automotive_knowledge.provision() FROM PUBLIC;
SELECT automotive_knowledge.provision();
