# Enterprise Agentic RAG & IDP Platform — Runnable POC

Cost-optimized, single-host version of the original EKS design (see the
architecture doc for the full HLD/LLD and the cost-optimization writeup this
repo implements). Estimated cost: **~$50–100/month** while running, close to
**$0** if you tear it down between sessions (Section 7).

What changed vs. the original design, and why, is called out inline in the
code and below — worth knowing cold if this comes up in an interview.

| Original | This POC | Why |
|---|---|---|
| EKS cluster (3–10 nodes) | Single host via Docker Compose | No multi-user load to justify orchestration cost yet |
| Bedrock Knowledge Bases + OpenSearch Serverless | pgvector on the same RDS instance | OpenSearch Serverless has a ~$175/mo floor even at zero traffic |
| ElastiCache Redis | Redis container on the same host | No need for a managed cache for a single-user POC |
| ALB + ACM cert | Direct port / SSH tunnel | No external users yet |
| Claude 3.5 Sonnet | Claude 3.5 Haiku (default, swappable) | Cheaper for iteration; swap back for real demos |

Two bugs from the original code are also fixed here:
- **PII re-hydration** now uses a real per-request token map (`main.py::mask_pii`/`rehydrate`) instead of hardcoding `.replace("<PERSON>", "John Doe")`.
- **Wire transfer limit** is now checked *and recorded* atomically in one transaction (`mcp_server.py::check_and_record_wire_transfer`) instead of only ever reading the running total.

---

## Prerequisites

- An AWS account with billing enabled
- AWS CLI v2, configured with a profile that can create IAM/RDS/S3/SQS resources
- Docker + Docker Compose
- Python 3.11+ (only needed locally to run the test suite)

---

## Step 1 — Buy / provision the AWS services

### 1a. Enable Bedrock model access (console only)

This step can't be reliably scripted across accounts/regions, so do it manually, once:

1. AWS Console → **Amazon Bedrock** → **Model access** (left sidebar)
2. Click **Manage model access** / **Enable specific models**
3. Enable:
   - **Anthropic → Claude 3.5 Haiku** (and Claude 3.5 Sonnet if you want it available for real demos)
   - **Amazon → Titan Embed Text v2**
4. Submit — for these models, access is usually granted within a few minutes

### 1b. Provision S3, SQS, RDS, and an IAM user

```bash
export AWS_REGION=us-east-1
export DB_MASTER_PASSWORD="pick-a-strong-password-here"
chmod +x infra/provision.sh
./infra/provision.sh
```

This creates:
- An S3 bucket (SSE-encrypted) for document uploads
- An SQS queue wired to S3 `documents/` upload events
- A single **db.t4g.micro** RDS PostgreSQL instance (~10 min to provision)
- An IAM user scoped to exactly: Bedrock invoke, S3 read/write on your bucket, SQS consume on your queue

At the end it prints values to paste into `.env`. Then create the access key it can't create for you (shown only once):

```bash
aws iam create-access-key --user-name ai-idp-poc-app-user
```

### 1c. Security notes for the POC

- `provision.sh` creates RDS as `--publicly-accessible` for convenience — **restrict the RDS security group to your own IP** right after creation (`aws ec2 authorize-security-group-ingress ...` or via console), and remove public accessibility entirely once you're past the POC stage.
- The IAM user's access keys go in `.env` — never commit that file. Rotate or delete the user via `infra/teardown.sh` once you're done for the day.

---

## Step 2 — Set up the database

Create a **dedicated, non-superuser** app role — connecting as the RDS master user would bypass Row-Level Security entirely, silently defeating the tenant-isolation control:

```bash
psql "host=<RDS_ENDPOINT> port=5432 dbname=enterprise_db user=poc_admin" -c "
  CREATE ROLE app_user LOGIN PASSWORD 'change_me';
  GRANT ALL ON SCHEMA public TO app_user;
"
```

Then load the schema (tables, RLS policies, pgvector extension, seed data):

```bash
psql "host=<RDS_ENDPOINT> port=5432 dbname=enterprise_db user=poc_admin" -f infra/schema.sql
```

---

## Step 3 — Configure environment

```bash
cp .env.example .env
```

Fill in the values from Step 1's `provision.sh` output, the access key from `aws iam create-access-key`, and the `DB_USER`/`DB_PASSWORD` you just created in Step 2.

---

## Step 4 — Build and run

```bash
docker-compose up -d --build
docker-compose ps          # confirm all 6 services are up
docker-compose logs -f gateway   # watch startup logs
```

Check the health endpoint:

```bash
curl http://localhost:8000/health
# {"status":"HEALTHY"}
```

---

## Step 5 — Ingest a sample document (feeds the RAG side)

```bash
echo "Company wire transfer policy: transfers above the daily departmental \
limit require Risk Committee sign-off. Risk department accounts are capped \
at \$50,000/day." > policy.txt

aws s3 cp policy.txt "s3://<S3_BUCKET>/documents/risk_dept_01/policy.txt"
```

Watch the worker pick it up:

```bash
docker-compose logs -f worker
# Should log: "Indexed N chunks from documents/risk_dept_01/policy.txt for tenant risk_dept_01"
```

---

## Step 6 — Use it

**Via the UI:** open `http://localhost:8501`, pick a tenant in the sidebar, ask a question.

**Via curl:**
```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"user_id":"emp_1","tenant_department":"risk_dept_01","prompt":"What is the wire transfer policy and my account risk status?"}'
```

---

## Step 7 — Testing

### Unit tests (no AWS, no running containers needed)
```bash
pip install pytest fastapi pydantic requests redis boto3 pgvector psycopg2-binary langgraph
pytest tests/test_unit.py -v
```
Covers: PII masking/rehydration round-trip, multi-entity masking (regression guard for the original bug), fail-open behavior when Presidio is down, chunking, and tenant-scoped cache keys.

### Smoke tests (after `docker-compose up` + Steps 5–6)
```bash
pip install pytest requests
pytest tests/test_smoke.py -v
```
Covers: health check, a real end-to-end query, cache-hit on repeat query, and a basic two-tenant isolation signal.

### Manual test: PII masking actually works
```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","tenant_department":"risk_dept_01","prompt":"My name is Alexandra Petrov, what is my balance?"}'
docker-compose logs gateway | grep masked_prompt
```
The logged `masked_prompt` should show `<PERSON_1>` in place of the name; the `response` field in the same log line should show the real name restored — that's the re-hydration fix working end-to-end.

### Manual test: tenant isolation
Ask the same question as both `risk_dept_01` and `finance_dept_01` (via the UI's tenant selector, or curl with different `tenant_department` values) and confirm the account details returned differ (ACC-9021/$48,250 vs. ACC-9022/$125,000) — each tenant should never see the other's account.

### Manual test: wire transfer limit fix
Call the MCP tool directly to confirm the running total now actually updates (the original bug):
```bash
# First call: should APPROVE and record $10,000 against the $50,000 daily cap
# Second identical call: remaining limit should now reflect the first transfer
```
(Exercise this via the gateway's MCP integration, or write a small script that opens an MCP SSE session to `localhost:8001` and calls `check_and_record_wire_transfer` twice in a row.)

---

## Step 8 — Nightly quality check (optional)

```bash
docker logs gateway > gateway_logs.jsonl
pip install ragas datasets pandas
python app/evaluation_cron.py
```

---

## Teardown (stop paying for it)

```bash
docker-compose down
./infra/teardown.sh
```

RDS is the one component that bills continuously while it exists — if you want to pause without fully tearing down, `aws rds stop-db-instance --db-instance-identifier ai-idp-poc-db` works too, but note AWS auto-restarts a stopped RDS instance after 7 days.

---

## What's still a POC shortcut, on purpose

- `check_erp_server_status` is a mocked stub (matches one hardcoded IP) — not a real integration.
- Fixed-size text chunking (`retrieval.chunk_text`) is naive; fine for a demo document, not for real PDFs/tables.
- Presidio fails open (forwards raw text on error) — a deliberate POC trade-off; flip to fail-closed before this touches real data.
- No authentication — `tenant_department` is a client-supplied field, not derived from a verified session.

These are worth calling out explicitly rather than hiding, both to your collaborator and if this project comes up as an interview talking point — naming what you'd fix before production reads as judgment, not a gap.
