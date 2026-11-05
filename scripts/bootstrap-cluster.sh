#!/usr/bin/env bash
# One-time setup of the home k3s cluster: k3s, namespaces, Argo CD, and the two Applications.
# Run on the Ubuntu ARM64 VM from a checkout of this repo. Safe to re-run.
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.37.1+k3s1}"
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.7}"
cd "$(dirname "$0")/.."

# 1. k3s (bundles kubectl, containerd, and a NetworkPolicy controller)
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - --write-kubeconfig-mode 600
fi
until sudo test -f /etc/rancher/k3s/k3s.yaml; do sleep 2; done
mkdir -p ~/.kube
sudo install -m 600 -o "$USER" /etc/rancher/k3s/k3s.yaml ~/.kube/config
# k3s's kubectl reads the root-only /etc/rancher/k3s/k3s.yaml unless KUBECONFIG is set.
export KUBECONFIG="$HOME/.kube/config"
kubectl wait --for=condition=Ready node --all --timeout=120s
kubectl get nodes -o wide

# 2. App namespaces, with the "restricted" Pod Security Standard enforced
for ns in showcase-staging showcase-prod; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace "$ns" --overwrite \
    pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/warn=restricted
done

# 3. Argo CD, pinned to a release (server-side apply: the CRDs are too big for client-side)
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=300s

# 4. The two Applications (staging tracks `staging`, prod tracks `main`)
kubectl apply -f argocd/

cat <<'MSG'

Cluster ready. Next:
  - Make kubectl use your kubeconfig in new shells:
      echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc
  - Heartbeat secrets: copy them from SSM (the source of truth). From a machine with AWS access:
      for env in staging prod; do
        aws ssm get-parameter --region us-west-2 --name /showcase/$env/hmac --with-decryption \
          --query Parameter.Value --output text | tr -d '\n' |
        ssh <user>@<vm> "kubectl --kubeconfig ~/.kube/config -n showcase-$env \
          create secret generic heartbeat --from-file=HMAC_KEY=/dev/stdin"
      done
  - Argo CD admin password:
      kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
  - UI (over your SSH tunnel): kubectl -n argocd port-forward svc/argocd-server 8443:443
MSG
