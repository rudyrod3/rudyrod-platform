# CI sets KUSTOMIZE="kubectl kustomize", which GitHub runners ship with.
KUSTOMIZE ?= kustomize
# Schemas for CRDs (Argo CD Application etc.) that kubeconform doesn't ship with.
CRD_SCHEMAS := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

.PHONY: validate
validate: ## Render the k8s overlay and validate it plus the Argo CD Applications
	$(KUSTOMIZE) build k8s/overlays/live | kubeconform -strict -summary -
	kubeconform -strict -summary -schema-location default -schema-location '$(CRD_SCHEMAS)' argocd/
