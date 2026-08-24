"""
Full production-pipeline-style Pulumi program: provisions AWS infra AND
deploys the application to it, in one `pulumi up`.

Provisions:
  - S3 bucket + SQS queue (document ingestion bus)
  - RDS PostgreSQL (structured data + pgvector RAG store)
  - ECR repositories, and BUILDS + PUSHES all 4 app images to them
  - EKS cluster: intentionally 1 node (t3.medium), no HPA, no ALB -- scoped
    down for cost, not for architectural correctness. See README-pulumi.md
    for the cost breakdown and why this is meant to be run for a session
    and torn down, not left running.
  - IRSA (IAM Roles for Service Accounts): pods authenticate to
    Bedrock/S3/SQS via a federated OIDC role, not static access keys --
    this is what makes it "production-pipeline-style" rather than just a
    bigger Compose file.
  - Full Kubernetes deployment: redis, presidio-analyzer, mcp-server,
    gateway, worker, ui -- Deployments + Services + a NetworkPolicy
    restricting Presidio to gateway-only traffic (mirrors the original
    SAD's zero-trust requirement).

Usage:
    pulumi config set aws:region ap-southeast-2
    pulumi config set my_ip <your-public-ip>/32
    pulumi config set --secret db_master_password <password>
    pulumi config set --secret app_db_password <password>   # for app_user
    pulumi up
    # afterwards:
    pulumi stack output kubeconfig --show-secrets > kubeconfig.yaml
    export KUBECONFIG=./kubeconfig.yaml
    kubectl get pods -n ai-platform
"""

import json
import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx
import pulumi_eks as eks
import pulumi_kubernetes as k8s

config = pulumi.Config()
my_ip = config.require("my_ip")
db_master_password = config.require_secret("db_master_password")
app_db_password = config.require_secret("app_db_password")
model_id = config.get("model_id") or "au.anthropic.claude-haiku-4-5-20251001-v1:0"
embed_model_id = config.get("embed_model_id") or "amazon.titan-embed-text-v2:0"

project = "ai-idp-poc"
account_id = aws.get_caller_identity().account_id
namespace_name = "ai-platform"

# =============================================================================
# S3 + SQS -- document ingestion bus (unchanged from the Compose-based build)
# =============================================================================
bucket = aws.s3.BucketV2("docs-bucket", bucket=f"{project}-docs-{account_id}")

aws.s3.BucketServerSideEncryptionConfigurationV2(
    "docs-bucket-encryption",
    bucket=bucket.id,
    rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}],
)

queue = aws.sqs.Queue(
    "ingestion-queue",
    name=f"{project}-ingestion-queue",
    visibility_timeout_seconds=300,
    message_retention_seconds=86400,
    sqs_managed_sse_enabled=True,
)

queue_policy = aws.sqs.QueuePolicy(
    "ingestion-queue-policy",
    queue_url=queue.id,
    policy=pulumi.Output.all(queue.arn, bucket.arn).apply(
        lambda args: json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "SQS:SendMessage",
                "Resource": args[0],
                "Condition": {"ArnLike": {"aws:SourceArn": args[1]}},
            }],
        })
    ),
)

aws.s3.BucketNotification(
    "docs-bucket-notification",
    bucket=bucket.id,
    queues=[{"queue_arn": queue.arn, "events": ["s3:ObjectCreated:*"], "filter_prefix": "documents/"}],
    opts=pulumi.ResourceOptions(depends_on=[queue_policy]),
)

# =============================================================================
# RDS PostgreSQL -- single instance, db.t4g.micro (unchanged sizing)
# Security group allows: your IP (for psql/schema loading) AND the EKS node
# security group (added after the cluster is created, below).
# =============================================================================
db_security_group = aws.ec2.SecurityGroup(
    "db-sg",
    description="Postgres access: operator IP + EKS nodes",
    ingress=[{"protocol": "tcp", "from_port": 5432, "to_port": 5432, "cidr_blocks": [my_ip]}],
    egress=[{"protocol": "-1", "from_port": 0, "to_port": 0, "cidr_blocks": ["0.0.0.0/0"]}],
)

db_instance = aws.rds.Instance(
    "poc-db",
    identifier=f"{project}-db",
    instance_class="db.t4g.micro",
    engine="postgres",
    engine_version="16.9",
    username="poc_admin",
    password=db_master_password,
    allocated_storage=20,
    db_name="enterprise_db",
    multi_az=False,
    publicly_accessible=True,
    vpc_security_group_ids=[db_security_group.id],
    backup_retention_period=1,
    skip_final_snapshot=True,
)

# =============================================================================
# ECR repositories + build & push all 4 app images
# This is the "production pipeline" part: pulumi up builds your local Docker
# context and pushes straight to ECR, same shape as a CI/CD job would.
# =============================================================================
def make_repo_and_image(name: str, context: str, dockerfile: str):
    repo = awsx.ecr.Repository(f"{name}-repo", force_delete=True)
    image = awsx.ecr.Image(
        f"{name}-image",
        repository_url=repo.url,
        context=context,
        dockerfile=dockerfile,
        image_tag=f"{name}-latest",
    )
    return repo, image

gateway_repo, gateway_image = make_repo_and_image("gateway", "../app", "../app/Dockerfile.gateway")
mcp_repo, mcp_image = make_repo_and_image("mcp-server", "../app", "../app/Dockerfile.mcp")
worker_repo, worker_image = make_repo_and_image("worker", "../app", "../app/Dockerfile.worker")
ui_repo, ui_image = make_repo_and_image("ui", "../ui", "../ui/Dockerfile.ui")

# =============================================================================
# EKS cluster -- intentionally 1 node. See module docstring.
# NOTE: create_oidc_provider was removed. This account is a member account
# inside an AWS Organization with an SCP that explicitly denies
# iam:CreateOpenIDConnectProvider, so IRSA is not possible here. Falling back
# to granting permissions on the node instance role instead (see below) --
# a documented, deliberate trade-off: less granular than per-pod IRSA, but
# still no static AWS access keys anywhere in the cluster.
# =============================================================================
eks_cluster = eks.Cluster(
    f"{project}-cluster",
    instance_type="t3.medium",
    desired_capacity=1,
    min_size=1,
    max_size=1,
    node_root_volume_size=20,
)

# Let EKS nodes reach RDS (their security group is only known after cluster creation)
aws.ec2.SecurityGroupRule(
    "db-sg-allow-eks-nodes",
    type="ingress",
    from_port=5432,
    to_port=5432,
    protocol="tcp",
    security_group_id=db_security_group.id,
    source_security_group_id=eks_cluster.node_security_group_id,
)

# =============================================================================
# App permissions -- FALLBACK from IRSA (blocked by org SCP, see note above).
# Attached directly to the EKS node instance role instead of a per-pod IRSA
# role. Scope is still limited to exactly Bedrock invoke + this bucket +
# this queue -- just applies to the whole node rather than a single pod.
# No static AWS access keys anywhere in the cluster either way.
# =============================================================================
app_policy_document = pulumi.Output.all(bucket.arn, queue.arn).apply(
    lambda args: json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
             "Resource": [args[0], f"{args[0]}/*"]},
            {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
             "Resource": args[1]},
        ],
    })
)

app_policy = aws.iam.Policy("app-irsa-policy", policy=app_policy_document)

# eks_cluster.instance_roles is a list output (one role per node group -- one here)
node_instance_role_name = eks_cluster.instance_roles.apply(lambda roles: roles[0].name)
aws.iam.RolePolicyAttachment(
    "app-irsa-attachment",
    role=node_instance_role_name,
    policy_arn=app_policy.arn,
)

# =============================================================================
# Kubernetes deployment -- everything below runs against the new cluster
# =============================================================================
k8s_provider = k8s.Provider("k8s-provider", kubeconfig=eks_cluster.kubeconfig_json)

namespace = k8s.core.v1.Namespace(
    "ai-platform-ns",
    metadata={"name": namespace_name},
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# No eks.amazonaws.com/role-arn annotation -- permissions come from the node
# instance role now (IRSA unavailable in this account, see note above).
app_sa = k8s.core.v1.ServiceAccount(
    "app-sa",
    metadata={
        "name": "app-sa",
        "namespace": namespace_name,
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace]),
)

db_secret = k8s.core.v1.Secret(
    "db-secret",
    metadata={"name": "db-secret", "namespace": namespace_name},
    string_data={"DB_PASSWORD": app_db_password},
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace]),
)

app_config = k8s.core.v1.ConfigMap(
    "app-config",
    metadata={"name": "app-config", "namespace": namespace_name},
    data=pulumi.Output.all(bucket.id, queue.id, db_instance.address).apply(
        lambda args: {
            "AWS_REGION": aws.get_region().name,
            "S3_BUCKET": args[0],
            "SQS_QUEUE_URL": f"https://sqs.{aws.get_region().name}.amazonaws.com/{account_id}/{project}-ingestion-queue",
            "RDS_ENDPOINT": args[2],
            "RDS_PORT": "5432",
            "RDS_DB_NAME": "enterprise_db",
            "DB_USER": "app_user",
            "MODEL_ID": model_id,
            "EMBED_MODEL_ID": embed_model_id,
            "EMBED_DIMENSIONS": "1024",
            "REDIS_HOST": "redis",
            "REDIS_PORT": "6379",
            "PRESIDIO_ANALYZER_URL": "http://presidio-analyzer:3000/analyze",
            "MCP_SERVER_URL": "http://mcp-server:8001/sse",
        }
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace]),
)

def deployment(name, image, port, replicas=1, use_sa=True, extra_env=None, command=None):
    env_from = [{"config_map_ref": {"name": "app-config"}}]
    env = [{"name": "DB_PASSWORD", "value_from": {"secret_key_ref": {"name": "db-secret", "key": "DB_PASSWORD"}}}]
    if extra_env:
        env += extra_env
    container = {
        "name": name,
        "image": image,
        "env_from": env_from,
        "env": env,
    }
    if port:
        container["ports"] = [{"container_port": port}]
    if command:
        container["command"] = command
    return k8s.apps.v1.Deployment(
        f"{name}-deployment",
        metadata={"name": name, "namespace": namespace_name},
        spec={
            "replicas": replicas,
            "selector": {"match_labels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "service_account_name": "app-sa" if use_sa else "default",
                    # Disable Kubernetes' automatic {SERVICE_NAME}_PORT / _HOST
                    # env var injection. Our Redis Service is named "redis",
                    # which caused Kubernetes to auto-inject REDIS_PORT as
                    # "tcp://<ip>:6379" -- silently overriding/pre-empting our
                    # app's own numeric REDIS_PORT expectation and crashing
                    # int() parsing in mcp_server.py. We use DNS + ConfigMap
                    # for service discovery, so this legacy injection is not
                    # needed and is actively harmful here.
                    "enable_service_links": False,
                    "containers": [container],
                },
            },
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace, app_sa, db_secret, app_config]),
    )

def service(name, port, target_port, svc_type="ClusterIP"):
    return k8s.core.v1.Service(
        f"{name}-service",
        metadata={"name": name, "namespace": namespace_name},
        spec={
            "selector": {"app": name},
            "ports": [{"port": port, "target_port": target_port}],
            "type": svc_type,
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace]),
    )

# --- redis (self-hosted, no ElastiCache -- same cost call as the Compose build)
deployment("redis", "redis:7-alpine", 6379, use_sa=False)
service("redis", 6379, 6379)

# --- presidio-analyzer (presidio-anonymizer intentionally omitted -- the
# app does its own reversible masking in-process, see main.py::mask_pii)
deployment("presidio-analyzer", "mcr.microsoft.com/presidio-analyzer:latest", 3000, use_sa=False)
service("presidio-analyzer", 3000, 3000)

# --- mcp-server
deployment("mcp-server", mcp_image.image_uri, 8001)
service("mcp-server", 8001, 8001)

# --- gateway
deployment("gateway", gateway_image.image_uri, 8000)
gateway_svc = service("gateway", 8000, 8000)

# --- worker (no port)
deployment("worker", worker_image.image_uri, None)

# --- ui
deployment("ui", ui_image.image_uri, 8501)
ui_svc = service("ui", 8501, 8501)

# --- NetworkPolicy: only the gateway can reach Presidio (zero-trust boundary
# from the original SAD, enforced at the k8s layer)
k8s.networking.v1.NetworkPolicy(
    "presidio-network-policy",
    metadata={"name": "presidio-access-control", "namespace": namespace_name},
    spec={
        "pod_selector": {"match_labels": {"app": "presidio-analyzer"}},
        "policy_types": ["Ingress"],
        "ingress": [{"from": [{"pod_selector": {"match_labels": {"app": "gateway"}}}]}],
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace]),
)

# =============================================================================
# Outputs
# =============================================================================
pulumi.export("cluster_name", eks_cluster.eks_cluster.name)
pulumi.export("kubeconfig", pulumi.Output.secret(eks_cluster.kubeconfig_json))
pulumi.export("rds_endpoint", db_instance.address)
pulumi.export("s3_bucket", bucket.id)
pulumi.export("sqs_queue_url", queue.id)
pulumi.export("gateway_service", pulumi.Output.concat("kubectl port-forward -n ", namespace_name, " svc/gateway 8000:8000"))
pulumi.export("ui_service", pulumi.Output.concat("kubectl port-forward -n ", namespace_name, " svc/ui 8501:8501"))