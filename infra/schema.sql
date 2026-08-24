-- Run this once against your RDS PostgreSQL instance after it's available:
--   psql "host=<RDS_ENDPOINT> port=5432 dbname=enterprise_db user=<DB_USER>" -f infra/schema.sql
--
-- Requires PostgreSQL 15.2+ / 14.11+ / 16.1+ (pgvector is available on
-- plain Amazon RDS for PostgreSQL as of these versions -- Aurora is NOT
-- required, which is what keeps this cheap for a POC).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Structured tables used by mcp_server.py, isolated per tenant via RLS
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer_accounts (
    account_id   VARCHAR(20) PRIMARY KEY,
    tenant_id    VARCHAR(50) NOT NULL,
    risk_status  VARCHAR(20) NOT NULL,
    balance_usd  NUMERIC(14,2) NOT NULL
);

ALTER TABLE customer_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_accounts FORCE ROW LEVEL SECURITY; -- applies RLS even to the table owner
DROP POLICY IF EXISTS tenant_isolation_accounts ON customer_accounts;
CREATE POLICY tenant_isolation_accounts ON customer_accounts
    USING (tenant_id = current_setting('app.current_tenant', true));

CREATE TABLE IF NOT EXISTS account_limits (
    account_id                VARCHAR(20) PRIMARY KEY REFERENCES customer_accounts(account_id),
    tenant_id                 VARCHAR(50) NOT NULL,
    max_daily_wire_limit      NUMERIC(14,2) NOT NULL,
    daily_transferred_today   NUMERIC(14,2) NOT NULL DEFAULT 0,
    last_reset_date           DATE NOT NULL DEFAULT CURRENT_DATE
);

ALTER TABLE account_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_limits FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_limits ON account_limits;
CREATE POLICY tenant_isolation_limits ON account_limits
    USING (tenant_id = current_setting('app.current_tenant', true));

-- ---------------------------------------------------------------------------
-- Vector store for RAG -- replaces Bedrock Knowledge Bases + OpenSearch
-- Serverless. embedding dimension matches Titan Embed Text v2 (1024).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    id          SERIAL PRIMARY KEY,
    tenant_id   VARCHAR(50) NOT NULL,
    source_key  TEXT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   vector(1024)
);

ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_chunks ON document_chunks;
CREATE POLICY tenant_isolation_chunks ON document_chunks
    USING (tenant_id = current_setting('app.current_tenant', true));

-- ivfflat needs at least a handful of rows to build a useful index; fine to
-- create it now, it'll just behave like a sequential scan until you've
-- ingested enough chunks. Rebuild with `REINDEX` after a large ingest.
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- Seed data -- two tenants, so you can demonstrate isolation in the demo
-- ---------------------------------------------------------------------------
INSERT INTO customer_accounts (account_id, tenant_id, risk_status, balance_usd) VALUES
    ('ACC-9021', 'risk_dept_01',    'LOW_RISK',    48250.00),
    ('ACC-9022', 'finance_dept_01', 'MEDIUM_RISK', 125000.00)
ON CONFLICT (account_id) DO NOTHING;

INSERT INTO account_limits (account_id, tenant_id, max_daily_wire_limit, daily_transferred_today) VALUES
    ('ACC-9021', 'risk_dept_01',    50000.00,  0),
    ('ACC-9022', 'finance_dept_01', 200000.00, 0)
ON CONFLICT (account_id) DO NOTHING;
