#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

command -v kubectl >/dev/null 2>&1 || die 'kubectl is required by the Portkey Helm post-renderer'

service_name="${PORTKEY_POST_RENDER_SERVICE_NAME:-}"
[[ -n "$service_name" ]] || \
  die 'PORTKEY_POST_RENDER_SERVICE_NAME must name the gateway Service'
[[ ${#service_name} -le 63 && "$service_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || \
  die 'PORTKEY_POST_RENDER_SERVICE_NAME must be a valid Kubernetes Service name'

work_dir="$(mktemp -d)" || die 'could not create a temporary post-render workspace'
chmod 700 "$work_dir"
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

umask 077
input_manifest="$work_dir/rendered.yaml"
validation_manifest="$work_dir/validated.yaml"
final_manifest="$work_dir/final.yaml"
kustomize_error="$work_dir/kustomize-error.log"
cat >"$input_manifest"
chmod 600 "$input_manifest"
[[ -s "$input_manifest" ]] || die 'Helm supplied an empty rendered manifest'

marker="portkey-post-render-target-$$"
cat >"$work_dir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - rendered.yaml
patches:
  - target:
      group: ""
      version: v1
      kind: Service
      name: "^${service_name}$"
    patch: |-
      - op: test
        path: /spec/type
        value: LoadBalancer
      - op: add
        path: /metadata/labels/guidance-codex.openai.com~1post-render-target
        value: ${marker}
EOF
chmod 600 "$work_dir/kustomization.yaml"

if ! kubectl kustomize "$work_dir" >"$validation_manifest" 2>"$kustomize_error"; then
  die "rendered manifest must be valid and contain exactly one LoadBalancer Service named $service_name"
fi
chmod 600 "$validation_manifest" "$kustomize_error"
match_count="$(grep -F -c "guidance-codex.openai.com/post-render-target: $marker" \
  "$validation_manifest" || true)"
[[ "$match_count" == 1 ]] || \
  die "rendered manifest must be valid and contain exactly one LoadBalancer Service named $service_name"

cat >"$work_dir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - rendered.yaml
patches:
  - target:
      group: ""
      version: v1
      kind: Service
      name: "^${service_name}$"
    patch: |-
      - op: test
        path: /spec/type
        value: LoadBalancer
      - op: add
        path: /spec/allocateLoadBalancerNodePorts
        value: false
EOF
chmod 600 "$work_dir/kustomization.yaml"

if ! kubectl kustomize "$work_dir" >"$final_manifest" 2>"$kustomize_error"; then
  die 'could not disable load-balancer NodePort allocation in the rendered gateway Service'
fi
chmod 600 "$final_manifest" "$kustomize_error"
cat "$final_manifest"
