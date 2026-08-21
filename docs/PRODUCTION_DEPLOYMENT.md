# Production Deployment

The ECS template is a reference stack, not a complete landing zone. Production
deployments should use an existing customer VPC, private ECS/RDS subnets, and
the customer's DNS, certificate, alerting, logging, and security services.
Production deployments must keep `EnableTls=true`; the raw HTTP walkthrough
mode is not a production transport.

## Topology

```text
Corporate network / approved internet sources
                    |
              Route 53 + ACM
                    |
              ALB + optional WAF       public subnets
                    |
              ECS Fargate service      private app subnets
                    |
              RDS PostgreSQL           private database subnets
```

Private ECS subnets need NAT egress or VPC endpoints for the AWS services used
by the task plus approved HTTPS egress to the configured JWKS endpoint. Pass
the ALB, task, and database subnet IDs separately; all must belong to the VPC
exported by `NetworkingStackName`.

When the customer already has a landing-zone VPC, deploy
`deployment/infrastructure/networking.yaml` with `ExistingVpcId`,
`ExistingPublicSubnet1`, and `ExistingPublicSubnet2`. This creates only the
CloudFormation exports consumed by the gateway stack; it does not modify the
customer VPC.

## Build Immutable Images

Choose a reviewed LiteLLM base-image digest. Do not build from a moving tag.

```bash
export LITELLM_BASE_IMAGE=ghcr.io/berriai/litellm@sha256:<reviewed-digest>
export RELEASE_TAG=2026-07-30.1
export ECR_IMAGE="$ECR_REGISTRY/$LITELLM_REPO:$RELEASE_TAG"

docker buildx build \
  --platform linux/amd64 \
  --build-arg LITELLM_BASE_IMAGE="$LITELLM_BASE_IMAGE" \
  --tag "$ECR_IMAGE" \
  --file deployment/litellm/Dockerfile \
  --push \
  deployment/litellm

export IMAGE_DIGEST=$(aws ecr describe-images \
  --repository-name "$LITELLM_REPO" \
  --image-ids imageTag="$RELEASE_TAG" \
  --query 'imageDetails[0].imageDigest' \
  --output text)
export LITELLM_IMAGE="$ECR_REGISTRY/$LITELLM_REPO@$IMAGE_DIGEST"
```

Scan the digest, record the result, and promote that same digest. Rebuilding
the same source for production creates a different artifact and is not
promotion.

## Deploy Staging

Use AWS CLI v2. Run local tests and `cfn-lint` before creating a CloudFormation
change set.

```bash
python3 deployment/scripts/preflight-litellm.py \
  --stage deploy \
  --check-ecr-image
```

```bash
aws cloudformation deploy \
  --stack-name codex-gateway-staging \
  --template-file deployment/litellm/ecs/litellm-ecs.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
      NetworkingStackName="$NETWORKING_STACK" \
      EnableTls=true \
      DBMultiAz=true \
      AwsRegion="$BEDROCK_REGION" \
      LiteLLMImage="$LITELLM_IMAGE" \
      AlbCertificateArn="$ALB_CERTIFICATE_ARN" \
      AlbDomainName="$GATEWAY_DOMAIN_NAME" \
      AllowedCidr="$ALLOWED_CIDR" \
      AlbSubnetIds="$PUBLIC_SUBNET_IDS" \
      TaskSubnetIds="$PRIVATE_APP_SUBNET_IDS" \
      DatabaseSubnetIds="$PRIVATE_DB_SUBNET_IDS" \
      AssignPublicIp=DISABLED \
      DesiredCount=2 \
      MinTaskCount=2 \
      MaxTaskCount=10 \
      EnableWaf=true \
      AlarmTopicArn="$ALARM_TOPIC_ARN"
```

RDS generates and stores its own password. The `DatabaseSecretArn` output is
for operations and rotation; do not retrieve the password during normal
deployment.

## Validate Before Promotion

1. Confirm the CloudFormation change set contains only intended replacements.
2. Wait for ALB targets and ECS deployment rollback state to become healthy.
3. Run `deployment/scripts/validate-responses-contract.py`.
4. Test streaming, tool calls, request cancellation, and a five-minute request.
5. Load-test above expected peak and confirm ECS scales out and back in.
6. Force an unhealthy image in staging and confirm the circuit breaker rolls
   back.
7. Restore the latest RDS snapshot into an isolated stack and record recovery
   time and recovery point.
8. Verify WAF blocking, alarm delivery, log access, and key revocation.
9. Confirm no database password appears in the task definition, change set,
   build logs, or CI artifacts.

## Production Overrides

Use a separate stack and credentials. At minimum, change:

```text
DBDeletionProtection=true
EnableAlbDeletionProtection=true
AssignPublicIp=DISABLED
EnableWaf=true
DBBackupRetentionDays=35
DBInstanceClass=<load-tested-class>
DBAllocatedStorage=<measured-baseline>
DBMaxAllocatedStorage=<approved-limit>
DesiredCount=<multi-AZ-baseline>
MinTaskCount=<multi-AZ-baseline>
MaxTaskCount=<tested-limit>
AlarmTopicArn=<production-topic>
```

The template retains ALB logs, the CloudWatch log group, DynamoDB user-key
mappings, RDS snapshots, Secrets Manager data, and the KMS keys needed to
decrypt them during deletion or replacement. Document how the operations team
inventories and removes retained resources after approved decommissioning.

## Rollback

Application rollback is digest-based: redeploy the last approved digest. Do
not mutate or retag an image in place. Database schema changes must be backward
compatible with both the new and previous gateway release. If they are not,
the release requires a separate migration and restore plan.

CloudFormation change sets are the production approval artifact. Creating a
change set does not deploy it; execution requires a separate approval. Never
test rollback by deleting the production stack.
