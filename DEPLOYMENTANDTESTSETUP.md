# Runbook — Deploy & Test

Companion to `README.md` (architecture/overview). This file is just the
step-by-step commands, split cleanly into **Deployment** (get it running)
and **Testing** (prove it works). Run Deployment first, top to bottom.

---

## PART 1 — DEPLOYMENT

### 1.1 One-time setup

```bash
cd infra-pulumi
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

pulumi login --local
pulumi stack init dev
```

You'll be asked to set a **passphrase** to encrypt the stack's secrets.
Write it down somewhere safe — there is no recovery if you lose it; losing
it means deleting the stack and starting over.

### 1.2 Configure the stack

```bash
pulumi config set aws:region ap-southeast-2
pulumi config set my_ip "$(curl -s ifconfig.me)/32"
pulumi config set --secret db_master_password
pulumi config set --secret app_db_password
```

The two `--secret` commands will prompt you to type a password (input is
hidden). Pick anything; they only need to match what you use in Part 1.3.

### 1.3 Deploy the infrastructure

```bash
export PULUMI_CONFIG_PASSPHRASE='the passphrase from 1.1'
pulumi up
```

Review the plan, confirm `yes`. This creates ~75 AWS resources: S3, SQS,
RDS, 4 ECR repos + built Docker images, the EKS cluster, and the full
Kubernetes deployment (6 pods).

**Expect 15–20 minutes.** Most of it is EKS cluster/node-group provisioning
and the 4 Docker image builds — it is not stuck, just slow. If you're on a
memory-constrained machine (under ~6GB RAM) and see plugin crash errors
(`exited prematurely`) partway through, re-run `pulumi up` — it resumes from
where it left off rather than starting over.

### 1.4 Load the database schema

RDS starts empty every time — this step is required after **every** fresh
`pulumi up`, since `pulumi destroy` deletes the database along with
everything else.

```bash
cd ../infra
RDS_ENDPOINT=$(cd ../infra-pulumi && pulumi stack output rds_endpoint)

psql -h $RDS_ENDPOINT -U poc_admin -d enterprise_db \
  -c "CREATE ROLE app_user WITH LOGIN PASSWORD 'match_your_app_db_password_from_1.2';"

psql -h $RDS_ENDPOINT -U poc_admin -d enterprise_db -f schema.sql

psql -h $RDS_ENDPOINT -U poc_admin -d enterprise_db \
  -c "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_accounts, account_limits, document_chunks TO app_user;"

psql -h $RDS_ENDPOINT -U poc_admin -d enterprise_db \
  -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;"
```

Each `psql` command will prompt for `poc_admin`'s password — that's the
`db_master_password` you set in 1.2.

This seeds two tenants and two test accounts:

| Tenant | Account | Risk | Balance | Daily wire limit |
|---|---|---|---|---|
| `risk_dept_01` | ACC-9021 | LOW_RISK | $48,250 | $50,000 |
| `finance_dept_01` | ACC-9022 | MEDIUM_RISK | $125,000 | $150,000 |

### 1.5 Connect to the cluster

```bash
cd ../infra-pulumi
pulumi stack output kubeconfig --show-secrets > kubeconfig.yaml
export KUBECONFIG=$(pwd)/kubeconfig.yaml
kubectl get pods -n ai-platform
```

All 6 pods (`gateway`, `mcp-server`, `worker`, `ui`, `redis`,
`presidio-analyzer`) should show `1/1 Running`.

### 1.6 (Optional) Ingest policy documents

Needed only if you want document-grounded (RAG) answers instead of
account-only answers. Upload to `documents/<tenant_id>/<filename>` — the
tenant is parsed directly from the S3 key path:

```bash
BUCKET=$(pulumi stack output s3_bucket)
aws s3 cp ../policy_documents/risk_dept_01/wire_transfer_policy.txt \
  s3://$BUCKET/documents/risk_dept_01/wire_transfer_policy.txt
aws s3 cp ../policy_documents/finance_dept_01/wire_transfer_policy.txt \
  s3://$BUCKET/documents/finance_dept_01/wire_transfer_policy.txt
```

Confirm the worker processed them:
```bash
kubectl logs -n ai-platform -l app=worker --tail=20
```
Look for `Indexed N chunks ... for tenant <tenant_id>`.

### 1.7 Open ports

```bash
kubectl port-forward -n ai-platform svc/gateway 8000:8000 &
kubectl port-forward -n ai-platform svc/ui 8501:8501 &
```

- API: `http://localhost:8000/api/v1/query`
- Chat UI: `http://localhost:8501`

**Deployment is done.** Move to Part 2 to verify it actually works, or go
straight to `http://localhost:8501` and start chatting.

### 1.8 Tear down when finished (stops billing)

```bash
BUCKET=$(pulumi stack output s3_bucket)
aws s3 rm s3://$BUCKET --recursive   # bucket must be empty before Pulumi can delete it
pulumi destroy
```

Verify nothing's left running:
```bash
aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"
aws eks list-clusters
aws ec2 describe-instances --filters "Name=instance-state-name,Values=running" --query "Reservations[].Instances[].InstanceId"
aws s3 ls | grep ai-idp-poc
aws sqs list-queues --queue-name-prefix ai-idp-poc
```
All five should return empty.

---

## PART 2 — TESTING

Run these against a fully deployed stack (Part 1 complete, port-forwards
open). Each test explains what a pass looks like.

### 2.1 Basic query

```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "risk_dept_01", "prompt": "What is the risk status and balance for account ACC-9021?"}'
```
**Pass:** response states LOW_RISK, $48,250.00.

### 2.2 Tenant isolation

Same account, wrong tenant:
```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "finance_dept_01", "prompt": "What is the risk status and balance for account ACC-9021?"}'
```
**Pass:** request is refused / no data returned (ACC-9021 belongs to
`risk_dept_01`, not `finance_dept_01` — RLS should block cross-tenant reads).

Confirm directly at the database layer too:
```bash
psql -h $RDS_ENDPOINT -U app_user -d enterprise_db \
  -c "SET app.current_tenant = 'risk_dept_01'; SELECT account_id FROM customer_accounts;"
```
**Pass:** exactly 1 row (ACC-9021), not both accounts.

### 2.3 PII masking

```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "risk_dept_01", "prompt": "My name is Jane Doe and my email is jane.doe@example.com. What is the risk status for account ACC-9021?"}'
```
Then check what actually got sent to Bedrock:
```bash
kubectl logs -n ai-platform -l app=gateway --tail=5
```
**Pass:** the `masked_prompt` field in the log shows `<PERSON_1>` and
`<EMAIL_ADDRESS_1>` in place of the real name/email, with no corrupted or
truncated text around the tokens.

### 2.4 RAG retrieval (requires 1.6 — documents ingested)

```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "risk_dept_01", "prompt": "What is the daily wire transfer limit for this department and what happens if a transfer exceeds it?"}'
```
**Pass:** the `retrieved_context` field in the gateway log shows real text
from the ingested policy document (not "No policy context found"), and the
answer correctly states the $50,000 cap and Risk Committee sign-off
requirement.

### 2.5 Wire-transfer approval / rejection

Check the current limit usage first:
```bash
psql -h $RDS_ENDPOINT -U app_user -d enterprise_db \
  -c "SET app.current_tenant = 'risk_dept_01'; SELECT * FROM account_limits WHERE account_id = 'ACC-9021';"
```

Approve a transfer under the remaining limit:
```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "risk_dept_01", "prompt": "Please process a $20000 wire transfer for account ACC-9021 and record it against todays limit."}'
```
**Pass:** `APPROVED`, and `daily_transferred_today` in the DB increases by
$20,000.

Reject a transfer that exceeds the remaining limit:
```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" \
  -d '{"user_id": "test-1", "tenant_department": "risk_dept_01", "prompt": "Please process a $35000 wire transfer for account ACC-9021 and record it against todays limit."}'
```
**Pass:** `REJECTED`, Risk Committee Override mentioned, and
`daily_transferred_today` in the DB does **not** change.

### 2.6 Wire-transfer atomicity under concurrency

Flush the cache first so both requests actually hit the backend:
```bash
kubectl exec -it -n ai-platform deployment/redis -- redis-cli -n 1 FLUSHDB
```

Fire two requests at the exact same moment — **note both `curl`s and both
`&` are on one line**, ending in `wait`, so they launch simultaneously
rather than one after the other:
```bash
curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" -d '{"user_id": "race-a", "tenant_department": "risk_dept_01", "prompt": "Process wire transfer Alpha: amount X for account ACC-9021, record against todays limit."}' & curl -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" -d '{"user_id": "race-b", "tenant_department": "risk_dept_01", "prompt": "Process wire transfer Bravo: amount X for account ACC-9021, record against todays limit."}' & wait
```
Pick an amount `X` where each request individually fits under the current
remaining limit, but both combined would exceed it — check 2.5's DB output
to work out the right number for your current state.

```bash
psql -h $RDS_ENDPOINT -U app_user -d enterprise_db \
  -c "SET app.current_tenant = 'risk_dept_01'; SELECT daily_transferred_today FROM account_limits WHERE account_id = 'ACC-9021';"
```
**Pass:** the total reflects exactly one transfer recorded (one approved,
one rejected) — not both, which would mean the `FOR UPDATE` row lock failed
to prevent a double-spend race.

### 2.7 UI smoke test

Open `http://localhost:8501`. Set **Tenant department** to `risk_dept_01`,
ask about ACC-9021, confirm a correct response renders. Switch to
`finance_dept_01`, ask the same question, confirm it's correctly refused.

### 2.8 Ingestion pipeline (worker) sanity check

```bash
kubectl logs -n ai-platform -l app=worker --tail=20
```
**Pass:** no crash loops, and (if 1.6 was done) `Indexed N chunks` lines
present with no errors.

---

## Quick reference — common fixes during testing

| Symptom | Cause | Fix |
|---|---|---|
| `SUCCESS_CACHED` with a stale/wrong answer | Redis cached an earlier response for the same prompt text | `kubectl exec -it -n ai-platform deployment/redis -- redis-cli -n 1 FLUSHDB`, then retry |
| `kubectl` says `connection refused` to `localhost:8080` | `KUBECONFIG` not exported in this shell | `export KUBECONFIG=<path to kubeconfig.yaml>` |
| `port-forward` fails: "address already in use" | A stale port-forward from an earlier terminal is still running | `ps aux \| grep port-forward`, `kill <PID>`, retry |
| `psql`: password auth failed on first try, works on retry | Transient / typo on first attempt | Just retry |
| Model refuses to call a tool ("I don't have access...") | Prompt phrased as an instruction to physically execute a real-world action | Rephrase as a policy check / evaluation request rather than a command |