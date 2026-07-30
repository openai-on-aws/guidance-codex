SHELL := /bin/bash
LITELLM_SCRIPT := deployment/scripts/litellm-stack.sh

.PHONY: help litellm-check litellm-build litellm-plan litellm-deploy \
	litellm-status litellm-provision-key litellm-codex-config litellm-validate

help:
	@$(LITELLM_SCRIPT) help

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
