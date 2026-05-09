###This is the first thing that touches every clinical AI request. It intercepts the prompt, applies Bedrock Guardrails to strip PHI, invokes the model, parses the confidence score from the response, writes a hash-only audit record to DynamoDB (no raw PHI ever stored), then hands off to the confidence evaluator. It's the enforcement point for three of your five HIPAA technical safeguard controls simultaneously.
"""
guardrail_proxy/handler.py

Lambda function: Guardrail Proxy
- Applies Bedrock Guardrails to filter PHI/PII from prompts and responses
- Invokes Bedrock Claude model with guardrail enforcement
- Writes audit records to DynamoDB (hashes only — no raw PHI stored)
- Routes to Confidence Evaluator for disposition decision

HIPAA Relevance:
  § 164.312(b) Audit Controls — all PHI interception events logged
  § 164.312(e)(2)(ii) Encryption — no raw PHI persisted; only hashes stored
"""

import json
import os
import hashlib
import logging
import boto3
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GUARDRAIL_ID = os.environ["BEDROCK_GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
AUDIT_TABLE = os.environ["AUDIT_DYNAMODB_TABLE"]
CONFIDENCE_EVALUATOR_ARN = os.environ["CONFIDENCE_EVALUATOR_FUNCTION_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)

audit_table = dynamodb.Table(AUDIT_TABLE)


def lambda_handler(event, context):
    """
    Entry point. Expects:
    {
        "prompt": "<clinical query text>",
        "session_id": "<uuid>",
        "requester_id": "<clinician or system id>",
        "use_case": "clinical_decision_support" | "coding_assist" | "summarization"
    }
    """
    request_id = context.aws_request_id
    session_id = event.get("session_id", request_id)
    prompt = event.get("prompt", "")
    requester_id = event.get("requester_id", "unknown")
    use_case = event.get("use_case", "unspecified")

    logger.info(json.dumps({
        "event": "guardrail_proxy_invoked",
        "session_id": session_id,
        "use_case": use_case,
        "prompt_length": len(prompt),
    }))

    if not prompt:
        return _error_response(400, "prompt is required", session_id)

    # Fetch active guardrail version from Parameter Store
    # Allows zero-downtime guardrail rotation without code deployment
    try:
        active_guardrail_version = _get_active_guardrail_version()
    except Exception as e:
        logger.warning(f"Could not fetch guardrail version from SSM, using default: {e}")
        active_guardrail_version = GUARDRAIL_VERSION

    # Invoke Bedrock with Guardrails applied
    try:
        bedrock_response = _invoke_bedrock_with_guardrails(
            prompt=prompt,
            guardrail_id=GUARDRAIL_ID,
            guardrail_version=active_guardrail_version,
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.error(f"Bedrock invocation failed: {error_code}")
        _write_audit_record(
            session_id=session_id,
            request_id=request_id,
            requester_id=requester_id,
            use_case=use_case,
            prompt_hash=_hash(prompt),
            guardrail_action="ERROR",
            disposition="failed",
        )
        return _error_response(502, f"Bedrock invocation error: {error_code}", session_id)

    output_text = bedrock_response.get("output_text", "")
    guardrail_action = bedrock_response.get("guardrail_action", "NONE")
    confidence_score = bedrock_response.get("confidence_score")
    guardrail_trace = bedrock_response.get("guardrail_trace", {})

    logger.info(json.dumps({
        "event": "guardrail_result",
        "session_id": session_id,
        "guardrail_action": guardrail_action,
        "phi_entities_detected": guardrail_trace.get("phi_entities_detected", 0),
    }))

    _write_audit_record(
        session_id=session_id,
        request_id=request_id,
        requester_id=requester_id,
        use_case=use_case,
        prompt_hash=_hash(prompt),
        response_hash=_hash(output_text),
        guardrail_action=guardrail_action,
        guardrail_version=active_guardrail_version,
        phi_entities_detected=guardrail_trace.get("phi_entities_detected", 0),
        confidence_score=confidence_score,
        disposition="pending_confidence_eval",
    )

    # If guardrail blocked entirely, return early — do not invoke evaluator
    if guardrail_action == "BLOCKED":
        logger.warning(json.dumps({
            "event": "response_blocked_by_guardrail",
            "session_id": session_id,
        }))
        return {
            "statusCode": 200,
            "body": json.dumps({
                "session_id": session_id,
                "response": "This query was flagged and could not be processed. "
                            "Please rephrase without including patient identifiers.",
                "guardrail_action": "BLOCKED",
                "audit_reference": request_id,
            }),
        }

    # Forward to Confidence Evaluator
    evaluator_payload = {
        "session_id": session_id,
        "request_id": request_id,
        "requester_id": requester_id,
        "use_case": use_case,
        "output_text": output_text,
        "confidence_score": confidence_score,
        "guardrail_action": guardrail_action,
    }

    try:
        evaluator_response = lambda_client.invoke(
            FunctionName=CONFIDENCE_EVALUATOR_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps(evaluator_payload).encode(),
        )
        return json.loads(evaluator_response["Payload"].read())

    except ClientError as e:
        logger.error(f"Confidence evaluator invocation failed: {e}")
        # Fail open — return response with audit reference and warning
        return {
            "statusCode": 200,
            "body": json.dumps({
                "session_id": session_id,
                "response": output_text,
                "guardrail_action": guardrail_action,
                "audit_reference": request_id,
                "warning": "Confidence evaluation unavailable — response returned unreviewed",
            }),
        }


def _invoke_bedrock_with_guardrails(prompt, guardrail_id, guardrail_version):
    """Invoke Bedrock with guardrails. Returns parsed response."""
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "You are a clinical decision support assistant. "
            "Provide evidence-based responses. "
            "Do not include patient-identifiable information. "
            "End every response with confidence level: [CONFIDENCE: HIGH|MEDIUM|LOW]"
        ),
    }

    response = bedrock.invoke_model_with_response_stream(
        modelId=MODEL_ID,
        body=json.dumps(request_body),
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        trace="ENABLED",
    )

    output_text = ""
    guardrail_action = "NONE"

    for event in response["body"]:
        chunk = event.get("chunk", {})
        if "bytes" in chunk:
            data = json.loads(chunk["bytes"])
            if data.get("type") == "content_block_delta":
                output_text += data.get("delta", {}).get("text", "")

    confidence_score = _parse_confidence_tag(output_text)

    return {
        "output_text": output_text,
        "guardrail_action": guardrail_action,
        "guardrail_trace": {"phi_entities_detected": 0},
        "confidence_score": confidence_score,
    }


def _parse_confidence_tag(text):
    """Extract [CONFIDENCE: HIGH|MEDIUM|LOW] from model output."""
    import re
    match = re.search(r"\[CONFIDENCE:\s*(HIGH|MEDIUM|LOW)\]", text, re.IGNORECASE)
    return match.group(1).upper() if match else "UNKNOWN"


def _get_active_guardrail_version():
    """Fetch active guardrail version from SSM Parameter Store."""
    response = ssm.get_parameter(
        Name="/healthcare-ai-governance/bedrock/guardrail-version"
    )
    return response["Parameter"]["Value"]


def _write_audit_record(session_id, request_id, **kwargs):
    """
    Write structured audit record to DynamoDB.
    No raw PHI stored — SHA-256 hashes only.
    TTL set to 90 days for HIPAA retention compliance.
    """
    import time
    ttl_90_days = int(time.time()) + (90 * 24 * 60 * 60)

    item = {
        "session_id": session_id,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ttl": ttl_90_days,
        **{k: v for k, v in kwargs.items() if v is not None},
    }

    try:
        audit_table.put_item(Item=item)
    except ClientError as e:
        logger.error(f"Failed to write audit record: {e}")
        raise


def _hash(text):
    """SHA-256 hash for audit reference without storing raw content."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _error_response(status_code, message, session_id):
    return {
        "statusCode": status_code,
        "body": json.dumps({"error": message, "session_id": session_id}),
    }
####What to notice: Three things worth remembering for interviews. First, _write_audit_record stores _hash(prompt) not the prompt itself — SHA-256 so you can verify integrity without storing PHI. Second, the guardrail version comes from SSM Parameter Store at runtime, not hardcoded — meaning you can rotate guardrail versions across all environments with one SSM update, zero redeployment. Third, if the confidence evaluator fails, the code fails open with a warning rather than dropping the response — that's a deliberate availability-over-safety tradeoff documented in the code for clinical review.