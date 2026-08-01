SHELL := /bin/bash
LITELLM_SCRIPT := deployment/scripts/litellm-stack.sh
PORTKEY_SCRIPT := deployment/scripts/portkey-stack.sh
IDC_SCRIPT := deployment/scripts/idc-stack.sh

.PHONY: help litellm-check litellm-build litellm-plan litellm-deploy \
	litellm-status litellm-provision-key litellm-codex-config litellm-validate \
	litellm-cleanup-plan litellm-cleanup \
	portkey-aws-check portkey-aws-plan portkey-aws-deploy portkey-aws-status \
	portkey-aws-cleanup-plan portkey-aws-cleanup portkey-check \
	portkey-codex-config portkey-validate portkey-codex-validate \
	portkey-auth-negative \
	idc-check idc-plan idc-deploy idc-provision idc-status idc-client-config \
	idc-validate

help:
	@printf '%s\n' \
		'Enterprise Codex deployment helpers' \
		'' \
		'LiteLLM on ECS:' \
		'  make litellm-check litellm-build litellm-plan litellm-deploy' \
		'  make litellm-status litellm-provision-key litellm-codex-config litellm-validate' \
		'  make litellm-cleanup-plan litellm-cleanup' \
		'' \
		'Portkey hosted Bedrock Mantle evaluation:' \
		'  make portkey-aws-check portkey-aws-plan portkey-aws-deploy portkey-aws-status' \
		'  make portkey-check portkey-codex-config portkey-validate portkey-codex-validate' \
		'  make portkey-auth-negative portkey-aws-cleanup-plan portkey-aws-cleanup' \
		'' \
		'IAM Identity Center:' \
		'  make idc-check idc-plan idc-deploy idc-provision idc-status' \
		'  make idc-client-config idc-validate'

litellm-check:
	@$(LITELLM_SCRIPT) check

litellm-build:
	@$(LITELLM_SCRIPT) build

litellm-plan:
	@$(LITELLM_SCRIPT) plan

litellm-deploy:
	@$(LITELLM_SCRIPT) deploy

litellm-status:
	@$(LITELLM_SCRIPT) status

litellm-provision-key:
	@$(LITELLM_SCRIPT) provision-key

litellm-codex-config:
	@$(LITELLM_SCRIPT) codex-config

litellm-validate:
	@$(LITELLM_SCRIPT) validate

litellm-cleanup-plan:
	@$(LITELLM_SCRIPT) cleanup-plan

litellm-cleanup:
	@$(LITELLM_SCRIPT) cleanup

portkey-aws-check:
	@$(PORTKEY_SCRIPT) aws-check

portkey-aws-plan:
	@$(PORTKEY_SCRIPT) plan

portkey-aws-deploy:
	@$(PORTKEY_SCRIPT) deploy

portkey-aws-status:
	@$(PORTKEY_SCRIPT) status

portkey-aws-cleanup-plan:
	@$(PORTKEY_SCRIPT) cleanup-plan

portkey-aws-cleanup:
	@$(PORTKEY_SCRIPT) cleanup

portkey-check:
	@$(PORTKEY_SCRIPT) check

portkey-codex-config:
	@$(PORTKEY_SCRIPT) codex-config

portkey-validate:
	@$(PORTKEY_SCRIPT) validate

portkey-codex-validate:
	@$(PORTKEY_SCRIPT) codex-validate

portkey-auth-negative:
	@$(PORTKEY_SCRIPT) auth-negative

idc-check:
	@$(IDC_SCRIPT) check

idc-plan:
	@$(IDC_SCRIPT) plan

idc-deploy:
	@$(IDC_SCRIPT) deploy

idc-provision:
	@$(IDC_SCRIPT) provision

idc-status:
	@$(IDC_SCRIPT) status

idc-client-config:
	@$(IDC_SCRIPT) client-config

idc-validate:
	@$(IDC_SCRIPT) validate
