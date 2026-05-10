import os
import json
import logging
import hashlib
import uuid
from datetime import datetime
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STEP_FUNCTIONS = boto3.client("stepfunctions")
DYNAMODB = boto3.resource("dynamodb")

AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE_NAME", "")
REVIEWER_ACCESS_TOKENS = {
    token.strip()
    for token in os.environ.get("REVIEWER_ACCESS_TOKENS", "").split(",")
    if token.strip()
}


def lambda_handler(event, context):
    reviewer_id = validate_reviewer(event)
    if reviewer_id is None:
        return response(401, {"message": "Unauthorized reviewer"})

    try:
        payload = parse_event_body(event)
    except ValueError as exc:
        logger.error("Invalid request body: %s", exc)
        return response(400, {"message": "Invalid JSON payload"})

    task_token = payload.get("taskToken")
    decision = payload.get("decision")
    comments = payload.get("comments", "")

    if not task_token or not isinstance(task_token, str):
        return response(400, {"message": "Missing taskToken"})
    if not decision or not isinstance(decision, str):
        return response(400, {"message": "Missing decision"})

    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        return response(400, {"message": "Decision must be approve or reject"})

    output = {
        "reviewDecision": normalized_decision.upper(),
        "reviewerId": reviewer_id,
        "comments": comments,
    }

    try:
        STEP_FUNCTIONS.send_task_success(
            taskToken=task_token, output=json.dumps(output)
        )
    except Exception as exc:
        logger.exception("Failed to resume Step Functions execution")
        return response(502, {"message": "Failed to resume workflow", "error": str(exc)})

    try:
        write_audit_record(
            task_token=task_token,
            reviewer_id=reviewer_id,
            decision=normalized_decision,
            comments=comments,
            payload=payload,
        )
    except Exception as exc:
        logger.exception("Failed to write audit record")
        return response(500, {"message": "Failed to write audit record", "error": str(exc)})

    return response(200, {"status": "success", "decision": normalized_decision})


def validate_reviewer(event):
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization")
    if not auth_header:
        return None

    token = auth_header.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if REVIEWER_ACCESS_TOKENS and token in REVIEWER_ACCESS_TOKENS:
        return token

    return None


def parse_event_body(event):
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def write_audit_record(task_token, reviewer_id, decision, comments, payload):
    if not AUDIT_TABLE_NAME:
        raise RuntimeError("AUDIT_TABLE_NAME is not configured")

    table = DYNAMODB.Table(AUDIT_TABLE_NAME)
    record = {
        "reviewId": str(uuid.uuid4()),
        "taskTokenHash": hashlib.sha256(task_token.encode("utf-8")).hexdigest(),
        "reviewerId": reviewer_id,
        "decision": decision,
        "comments": comments,
        "createdAt": datetime.utcnow().isoformat() + "Z",
        "requestPayload": payload,
    }
    table.put_item(Item=record)
    logger.info("Audit record saved: %s", record["reviewId"])


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }