# Healthcare AI Governance Framework on AWS

> **Clinical AI deployed without guardrails is a regulatory and liability gap.**  
> This framework closes it.

A production-grade reference architecture for governed, auditable clinical AI on AWS — using **Bedrock Guardrails**, **Step Functions**, and **CloudWatch** to enforce PHI safety, human oversight, and model drift detection.

---

## The Problem

Healthcare organizations deploying LLM-based clinical decision support face three compounding risks:

| Risk | Consequence |
|------|-------------|
| No PHI filtering on prompts/responses | HIPAA breach exposure, OCR penalties |
| No human review for low-confidence outputs | Adverse patient events, liability |
| No mechanism to detect model output drift | Regulatory non-compliance, silent degradation |

i want to  configure Bedrock and  architect the *governance layer* around it.

---

## Architecture

```
Clinical App / EHR Integration
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│                    API Gateway (REST)                        │
│              WAF + Cognito Authorizer                        │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              Lambda: Guardrail Proxy                         │
│   • Bedrock Guardrails — PHI pattern blocking               │
│   • Sensitive entity redaction (PII/PHI)                    │
│   • Prompt injection detection                              │
│   • Response content filtering                              │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              Lambda: Confidence Evaluator                    │
│   • Parses Bedrock response + confidence indicators         │
│   • Routes: HIGH → return directly                          │
│             LOW  → trigger Step Functions review workflow   │
└──────────┬──────────────────────────┬───────────────────────┘
           │ HIGH confidence          │ LOW confidence
           ▼                          ▼
    Return to caller        ┌─────────────────────────┐
    (with audit log)        │  Step Functions ASL      │
                            │  Human-in-the-Loop       │
                            │  Review Workflow         │
                            │                         │
                            │  1. Notify reviewer      │
                            │     (SNS → SES/Slack)    │
                            │  2. Wait for callback    │
                            │     (taskToken pattern)  │
                            │  3. Approve / Override / │
                            │     Reject               │
                            │  4. Audit → DynamoDB     │
                            └─────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              Lambda: Drift Detector (scheduled)             │
│   • Samples recent inference outputs                        │
│   • Computes semantic similarity vs. baseline embeddings    │
│   • Emits custom CloudWatch metric: ModelDriftScore         │
└─────────────────────┬───────────────────────────────────────┘
                      │ metric > threshold
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              CloudWatch Alarm → SNS                         │
│   • PagerDuty / OpsGenie integration                        │
│   • Auto-rollback SSM Parameter Store model version flag    │
│   • EventBridge rule triggers rollback Lambda               │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### Bedrock Guardrails — Not Optional Middleware
Guardrails are applied at the **API layer**, not inside application code. This ensures PHI filtering survives application changes and is enforced uniformly. The guardrail ID is stored in Parameter Store and injected at runtime — enabling guardrail version swaps without code deployments.

### Step Functions taskToken Pattern for Human Review
Rather than polling, the workflow uses the **waitForTaskToken** integration. The reviewer receives a callback URL containing an encrypted token. Approval/rejection sends the token back to Step Functions, resuming the execution. This is fully asynchronous with configurable heartbeat timeouts.

### Drift Detection via Custom CloudWatch Metrics
A scheduled Lambda samples the last N inference outputs, embeds them using Bedrock's Titan Embeddings, and computes cosine similarity against a stored baseline distribution. The `ModelDriftScore` metric feeds a CloudWatch Alarm. When the alarm fires, an EventBridge rule triggers a rollback Lambda that flips the active model version in Parameter Store — no manual intervention required.

### Audit Trail Architecture
Every inference — approved, rejected, or auto-returned — writes a structured record to DynamoDB with:
- Request hash (no raw PHI stored)
- Guardrail action taken
- Confidence score
- Human reviewer ID (if applicable)
- Final disposition
- Timestamp + TTL (90-day HIPAA retention window)

---

## Repository Structure

```
healthcare-ai-governance/
├── cloudformation/
│   ├── root-stack.yaml          # Nested stack orchestrator
│   ├── guardrails-stack.yaml    # Bedrock Guardrail + IAM
│   ├── stepfunctions-stack.yaml # State machine + IAM
│   ├── cloudwatch-stack.yaml    # Alarms, dashboards, log groups
│   └── storage-stack.yaml       # DynamoDB, S3, Parameter Store
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── modules/
│   │   ├── bedrock_guardrails/
│   │   ├── step_functions/
│   │   ├── cloudwatch/
│   │   └── storage/
│   └── environments/
│       ├── dev.tfvars
│       └── prod.tfvars
├── lambda/
│   ├── guardrail_proxy/         # PHI filtering + Bedrock invocation
│   │   ├── handler.py
│   │   └── requirements.txt
│   ├── confidence_evaluator/    # Route by confidence score
│   │   ├── handler.py
│   │   └── requirements.txt
│   ├── drift_detector/          # Scheduled drift metric emission
│   │   ├── handler.py
│   │   └── requirements.txt
│   └── human_review_callback/   # Process reviewer approve/reject
│       ├── handler.py
│       └── requirements.txt
├── step-functions/
│   └── clinical-review-workflow.asl.json
├── cloudwatch/
│   └── dashboard.json
├── docs/
│   ├── architecture.md
│   ├── guardrails-config.md
│   ├── runbook-drift-response.md
│   └── hipaa-controls-mapping.md
├── scripts/
│   ├── deploy.sh
│   ├── baseline-embeddings.py   # Generate drift detection baseline
│   └── test-guardrails.sh
└── tests/
    ├── test_guardrail_proxy.py
    ├── test_confidence_evaluator.py
    └── test_drift_detector.py
```

---

## Deployment

### Prerequisites
- AWS CLI configured with appropriate permissions
- Python 3.11+
- Terraform >= 1.7 (optional — CloudFormation is the primary IaC path)
- Bedrock model access enabled: `anthropic.claude-3-haiku-20240307-v1:0`

### CloudFormation Deploy

```bash
# 1. Package nested stacks to S3
./scripts/deploy.sh --env dev --action package

# 2. Deploy root stack
./scripts/deploy.sh --env dev --action deploy

# 3. Generate drift baseline (run once after first deployment)
python3 scripts/baseline-embeddings.py --env dev
```

### Terraform Deploy

```bash
cd terraform/
terraform init
terraform plan -var-file="environments/dev.tfvars"
terraform apply -var-file="environments/dev.tfvars"
```

---

## HIPAA Controls Mapping

| Control | Implementation |
|---------|---------------|
| § 164.312(a)(1) — Access Control | Cognito + IAM least-privilege per Lambda role |
| § 164.312(b) — Audit Controls | DynamoDB audit log, CloudTrail, CloudWatch Logs |
| § 164.312(c)(1) — Integrity | KMS CMK encryption at rest + in transit (TLS 1.2+) |
| § 164.312(e)(2)(ii) — Encryption | All S3 buckets SSE-KMS, DynamoDB encrypted |
| Technical Safeguard — PHI | Bedrock Guardrails PII/PHI entity redaction |

Full controls mapping: [`docs/hipaa-controls-mapping.md`](docs/hipaa-controls-mapping.md)

---

## Cost Profile (Estimated, dev workload)

| Service | Estimated Monthly |
|---------|------------------|
| Bedrock (Claude Haiku, 10K calls) | ~$1.50 |
| Step Functions (standard, 500 executions) | ~$0.13 |
| Lambda (all functions, 10K invocations) | ~$0.02 |
| CloudWatch (metrics + logs) | ~$3.00 |
| DynamoDB (on-demand, audit log) | ~$1.00 |
| **Total** | **~$5.65/month** |

> **Note:** This project deliberately avoids OpenSearch Serverless (~$350/month idle) for embedding storage. Drift detection baselines are stored in S3 and loaded into Lambda memory at runtime — sufficient for reference architecture scale.

---

## Author

**Ken Palmer** | AWS Solutions Architect Associate  
Palmer Consulting LLC | Pittsburgh, PA  
[github.com/kpalmer412](https://github.com/kpalmer412) | linkedin.com/in/ken-palmer-


*15+ years in healthcare/life sciences (Epic Beaker, HL7/FHIR, NGS/genomics) + AWS cloud engineering*
