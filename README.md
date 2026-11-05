# rudyrod-platform

The backend for the **Live Infrastructure** panel on [rudyrod.me](https://rudyrod.me):
a Docker image running on k3s at home, deployed by Argo CD, reporting its status to an
AWS Lambda + DynamoDB backend that is built with Terraform.

## The showcase service (`app/`)

One image, two modes:

| Mode | Command | Does |
| --- | --- | --- |
| API (default) | `uvicorn app.main:app` | `GET /healthz` for probes, `GET /info` for version, commit, build time, arch, pod, namespace |
| Heartbeat | `python -m app.report` | Lists pods in its own namespace and POSTs a signed status to `$STATUS_API_URL/heartbeat` |

The heartbeat signing contract (HMAC-SHA256 over `"<timestamp>." + body`) is documented at
the top of [app/report.py](app/report.py); the Lambda verifies it the same way.

### Develop

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/pytest -q
.venv/bin/python -m uvicorn app.main:app --port 8080     # curl localhost:8080/info
POD_NAMESPACE=showcase-staging .venv/bin/python -m app.report --dry-run
```

### Build the image (on the ARM64 VM)

```bash
docker buildx create --use --name multi
docker buildx build --platform linux/arm64 \
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  -t ghcr.io/rudyrod3/rudyrod-platform:dev --load .

# run it the way Kubernetes will: non-root, read-only root fs, no capabilities
docker run --rm -p 8080:8080 --read-only --cap-drop ALL \
  ghcr.io/rudyrod3/rudyrod-platform:dev
curl localhost:8080/info                                 # expect "arch": "aarch64"
docker run --rm --read-only -e POD_NAMESPACE=showcase-staging \
  ghcr.io/rudyrod3/rudyrod-platform:dev python -m app.report --dry-run

trivy image --severity CRITICAL,HIGH ghcr.io/rudyrod3/rudyrod-platform:dev
docker image ls ghcr.io/rudyrod3/rudyrod-platform:dev   # target: under ~150 MB
```

## The cluster (`k8s/`, `argocd/`)

k3s on the home VM, deployed by Argo CD **pulling** from this repo. No inbound ports.

| Application | Tracks | Path | Namespace |
| --- | --- | --- | --- |
| `showcase-staging` | `staging` | `k8s/overlays/live` | `showcase-staging` |
| `showcase-prod` | `main` | `k8s/overlays/live` | `showcase-prod` |

`k8s/base` holds the API Deployment + Service, the `heartbeat` CronJob (every 5 min),
read-only RBAC for the heartbeat, and a default-deny NetworkPolicy that opens only port 8080
on the API. Both namespaces enforce the `restricted` Pod Security Standard.

### First-time setup (on the VM)

**1. Push a first image** so the overlay has a real digest (CI does this from Phase 4 on):

```bash
gh auth refresh -s write:packages
gh auth token | docker login ghcr.io -u rudyrod3 --password-stdin
docker buildx build --platform linux/arm64 \
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  -t ghcr.io/rudyrod3/rudyrod-platform:bootstrap --push .
docker buildx imagetools inspect ghcr.io/rudyrod3/rudyrod-platform:bootstrap | awk '/^Digest:/{print $2}'
```

Put that digest in [k8s/overlays/live/kustomization.yaml](k8s/overlays/live/kustomization.yaml),
then on GitHub open **Packages → rudyrod-platform → Package settings** and set visibility to
**Public** so k3s can pull without credentials. Merge the change into `staging`, then
`staging` into `main`. Argo CD deploys from those branches, not from feature branches.

**2. Install k3s, Argo CD, and the Applications:**

```bash
./scripts/bootstrap-cluster.sh        # pins k3s v1.37.1+k3s1 and Argo CD v3.5.7
```

It finishes by printing the commands for the per-namespace `heartbeat` Secrets and the
Argo CD admin password.

**3. Check:**

```bash
kubectl -n argocd get applications                 # both Synced / Healthy
kubectl -n showcase-staging get pods,cronjob
kubectl -n showcase-staging port-forward svc/showcase-api 8080:8080 &
curl localhost:8080/info                           # namespace: showcase-staging, arch: aarch64
```

Until Phase 3 sets `STATUS_API_URL`, heartbeat runs fail with `STATUS_API_URL must be set`.
To silence them meanwhile:

```bash
for ns in showcase-staging showcase-prod; do
  kubectl -n $ns patch cronjob heartbeat -p '{"spec":{"suspend":true}}'
done
```

Argo CD ignores `spec.suspend`, so the patch sticks. Resume with `"suspend":false`.

### Validate manifests

```bash
brew install kustomize kubeconform   # once
make validate
```

This renders `k8s/overlays/live` with kustomize and checks the output, then checks
`argocd/` against the Argo CD schemas from the community CRD catalog. Don't point
kubeconform at the repo root: it will try to parse `.venv/` files and raw
`kustomization.yaml` files, which aren't Kubernetes resources.

## The AWS backend (`status_api/`, `infra/terraform/`)

One Lambda (Python 3.13, arm64, behind a Function URL) and one DynamoDB table. The website
reads only from here, so the site keeps working when the home lab is off.

| Route | Caller | Does |
| --- | --- | --- |
| `GET /status?env=prod` | Site panel | `STATUS` + `BUILD` + visit count, `stale: true` after 15 min without a heartbeat |
| `POST /heartbeat` | k3s CronJob | Verifies the HMAC signature (per-environment key from SSM) and a ±5 min timestamp, rejects replays, writes `STATUS` |
| `POST /visit` | Site panel | Atomically increments the counter |

| pk | sk | Written by |
| --- | --- | --- |
| `ENV#staging` / `ENV#prod` | `STATUS` | The Lambda, from verified heartbeats |
| `BUILD#staging` / `BUILD#prod` | `BUILD` | GitHub Actions, through OIDC |
| `SITE` | `VISITS` | The Lambda, from `POST /visit` |

`BUILD` records get their own partition because IAM can restrict which partition key a
role writes (`dynamodb:LeadingKeys`), but not which sort key. Each branch's CI role can
write only its own `BUILD#<env>` item.

Terraform also creates the GitHub OIDC provider and four roles: a build writer per branch,
a read-only plan role for pull requests (denied SSM/KMS reads), and the apply role, which
only the `aws` GitHub environment can assume. Plus a $1/month budget with email alerts.

### First apply (from the VM, with your Identity Center credentials)

```bash
aws sso login                          # or: aws configure sso, then export AWS_PROFILE=...
export AWS_REGION=us-west-2            # overrides the profile's default region for every command below
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export TF_STATE_BUCKET=rudyrod-platform-tfstate-$ACCOUNT

# 1. State bucket (once): versioned, no public access
aws s3api create-bucket --bucket $TF_STATE_BUCKET --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2
aws s3api put-bucket-versioning --bucket $TF_STATE_BUCKET \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket $TF_STATE_BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 2. HMAC keys: the same values as the k8s `heartbeat` Secrets (Phase 2). Never in Terraform.
aws ssm put-parameter --name /showcase/staging/hmac --type SecureString --value "$STAGING_SECRET"
aws ssm put-parameter --name /showcase/prod/hmac    --type SecureString --value "$PROD_SECRET"

# 3. Anything that already exists? (table, budget, GitHub OIDC provider)
aws dynamodb list-tables
aws budgets describe-budgets --account-id $ACCOUNT --query 'Budgets[].BudgetName'
aws iam list-open-id-connect-providers
#    If so, uncomment the matching blocks in infra/terraform/imports.tf.

# 4. Apply
cd infra/terraform
export TF_VAR_state_bucket=$TF_STATE_BUCKET TF_VAR_budget_email=you@example.com
terraform init -backend-config="bucket=$TF_STATE_BUCKET"
terraform plan -out tfplan
terraform apply tfplan
terraform output -raw function_url
```

If the apply fails with `UnreservedConcurrentExecution below its minimum value`, your
account's concurrency limit is still 10. Re-run with `TF_VAR_lambda_reserved_concurrency=-1`
and request a quota increase.

### Connect the cluster

Put the Function URL in `STATUS_API_URL` in
[k8s/overlays/live/kustomization.yaml](k8s/overlays/live/kustomization.yaml), merge through
`staging` into `main`, and resume the CronJobs if you suspended them. Then:

```bash
URL=$(terraform -chdir=infra/terraform output -raw function_url)
kubectl -n showcase-staging create job --from=cronjob/heartbeat heartbeat-manual
kubectl -n showcase-staging logs job/heartbeat-manual    # "heartbeat sent", status 200
curl -s "${URL}status?env=staging"                         # "stale": false
curl -s -X POST "${URL}visit"                              # {"visits": 1}
```

Errors from the heartbeat: **401** means the Secret and SSM keys differ, or the VM clock is
off by more than 5 minutes; **409** means a replayed or out-of-order heartbeat.
