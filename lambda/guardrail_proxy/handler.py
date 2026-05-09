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

    try:
        active_guardrail_version = _get_active_guardrail_version()
    except Exception as e:
        logger.warning(f"Could not fetch guardrail version from SSM: {e}")
        active_guardrail_version = GUARDRAIL_VERSION

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
        s
        