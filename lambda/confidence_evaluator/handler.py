"""
confidence_evaluator/handler.py

Lambda function: Confidence Evaluator
- Receives Bedrock output from Guardrail Proxy
- Evaluates confidence score
- HIGH confidence + low-risk use case → auto-approve, return directly
- LOW/MEDIUM/UNKNOWN or clinical use case → start Step Functions human review

This is the governance routing layer — the decision point between
autonomous AI response and mandatory human oversight.
"""

import json
import os
import logging
import boto3
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STATE_MACHINE_ARN = os.environ["STEP_FUNCTIONS_STATE_MACHINE_ARN"]
AUDIT_TABLE = os.environ["AUDIT_DYNAMODB_TABLE"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

sfn = boto3.client("stepfunctions", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
audit_table = dynamodb.Table(AUDIT_TABLE)


def lambda_handler(event, context):
    """
    Expects:
    {
        "session_id": str,
        "request_id": str,
        "requester_id": str,
        "use_case": str,
        "output_text": str,
        "confidence_score": "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN",
        "guardrail_action": str
    }
    """
    session_id = event["session_id"]
    request_id = event["request_id"]
    requester_id = event.get("requester_id", "unknown")
    use_case = event.get("use_case", "unspecified")
    output_text = event.get("output_text", "")
    confidence_score = event.get("confidence_score", "UNKNOWN")
    guardrail_action = event.get("guardrail_action", "NONE")

    logger.info(json.dumps({
        "event": "confidence_evaluator_invoked",
        "session_id": session_id,
        "confidence_score": confidence_score,
        "use_case": use_case,
    }))

    # ── Routing decision ─────────────────────────────────────────────────────
    # Clinical decision support ALWAYS requires human review regardless
    # of confidence — this is the conservative clinical governance policy.
    # A model can be highly confident and still be wrong in a clinical context.
    hipaa_sensitive = use_case in {
        "clinical_decision_support",
        "treatment_recommendation"
    }

    requires_review = (
        confidence_score in ("LOW", "UNKNOWN", "MEDIUM")
        or (hipaa_sensitive and confidence_score != "HIGH")
    )

    if requires_review:
        return _route_to_human_review(
            session_id=session_id,
            request_id=request_id,
            requester_id=requester_id,
            use_case=use_case,
            output_text=output_text,
            confidence_score=confidence_score,
            guardrail_action=guardrail_action,
        )
    else:
        return _auto_approve(
            session_id=session_id,
            request_id=request_id,
            output_text=output_text,
            confidence_score=confidence_score,
            guardrail_action=guardrail_action,
        )


def _route_to_human_review(
    session_id, request_id, requester_id, use_case,
    output_text, confidence_score, guardrail_action
):
    """
    Start Step Functions execution for human-in-the-loop review.
    Async — returns 202 immediately, reviewer notified separately.
    """
    execution_name = (
        f"review-{session_id[:8]}-"
        f"{int(datetime.now(timezone.utc).timestamp())}"
    )

    sfn_input = {
        "session_id": session_id,
        "request_id": request_id,
        "requester_id": requester_id,
        "use_case": use_case,
        "output_text": output_text,
        "confidence_score": confidence_score,
        "guardrail_action": guardrail_action,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        response = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps(sfn_input),
        )
        execution_arn = response["executionArn"]

        logger.info(json.dumps({
            "event": "human_review_initiated",
            "session_id": session_id,
            "execution_arn": execution_arn,
            "reason": f"confidence_score={confidence_score}",
        }))

        _update_audit_disposition(
            session_id, request_id,
            "pending_human_review", execution_arn
        )

        return {
            "statusCode": 202,
            "body": json.dumps({
                "session_id": session_id,
                "status": "PENDING_REVIEW",
                "message": (
                    "This response requires clinical review before delivery. "
                    "You will be notified when review is complete."
                ),
                "execution_arn": execution_arn,
                "audit_reference": request_id,
                "estimated_review_time_minutes": 30,
            }),
        }

    except ClientError as e:
        logger.error(f"Failed to start Step Functions execution: {e}")
        # Fail safe — do not return unreviewed response on routing failure
        return {
            "statusCode": 503,
            "body": json.dumps({
                "error": "Human review system temporarily unavailable",
                "session_id": session_id,
                "audit_reference": request_id,
            }),
        }


def _auto_approve(
    session_id, request_id,
    output_text, confidence_score, guardrail_action
):
    """
    High confidence, low-risk use case — return directly to caller.
    Still writes AUTO_APPROVED audit record — every inference is logged.
    """
    logger.info(json.dumps({
        "event": "auto_approved",
        "session_id": session_id,
        "confidence_score": confidence_score,
    }))

    _update_audit_disposition(session_id, request_id, "AUTO_APPROVED")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "session_id": session_id,
            "response": output_text,
            "confidence_score": confidence_score,
            "guardrail_action": guardrail_action,
            "disposition": "AUTO_APPROVED",
            "audit_reference": request_id,
        }),
    }


def _update_audit_disposition(
    session_id, request_id,
    disposition, execution_arn=None
):
    """Update existing audit record with final disposition."""
    update_expression = "SET disposition = :d, disposition_timestamp = :t"
    expression_values = {
        ":d": disposition,
        ":t": datetime.now(timezone.utc).isoformat(),
    }

    if execution_arn:
        update_expression += ", step_functions_execution_arn = :e"
        expression_values[":e"] = execution_arn

    try:
        audit_table.update_item(
            Key={"session_id": session_id, "request_id": request_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update audit disposition: {e}")
