"""
drift_detector/handler.py

Lambda function: Drift Detector (runs every 6 hours via EventBridge)
- Samples recent inference records from DynamoDB audit log
- Generates embeddings via Bedrock Titan Embeddings
- Computes cosine distance vs stored baseline
- Emits custom CloudWatch metric: ModelDriftScore

Why this matters clinically:
  A clinical AI model can change its response patterns without throwing
  a single error. Traditional monitoring misses this entirely.
  Cosine distance on embeddings catches semantic drift that error
  rates and latency metrics never would.
"""

import json
import os
import math
import logging
import boto3
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIT_TABLE = os.environ["AUDIT_DYNAMODB_TABLE"]
BASELINE_BUCKET = os.environ["DRIFT_BASELINE_BUCKET"]
BASELINE_KEY = os.environ.get("DRIFT_BASELINE_S3_KEY", "baselines/embedding-baseline.json")
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "HealthcareAIGovernance")
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v1"
)
DRIFT_SAMPLE_COUNT = int(os.environ.get("DRIFT_SAMPLE_COUNT", "20"))
REGION = os.environ.get("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
cloudwatch = boto3.client("cloudwatch", region_name=REGION)
audit_table = dynamodb.Table(AUDIT_TABLE)


def lambda_handler(event, context):
    run_id = context.aws_request_id
    run_timestamp = datetime.now(timezone.utc)

    logger.info(json.dumps({
        "event": "drift_detection_started",
        "run_id": run_id
    }))

    try:
        baseline = _load_baseline()
    except Exception as e:
        logger.error(f"Failed to load baseline: {e}")
        _emit_cloudwatch_metric("BaselineLoadError", 1, "Count")
        return {"status": "error", "reason": "baseline_unavailable"}

    baseline_centroid = baseline["centroid"]

    logger.info(json.dumps({
        "event": "baseline_loaded",
        "model_version": baseline.get("model_version", "unknown"),
        "dimensions": len(baseline_centroid),
    }))

    try:
        recent_outputs = _sample_recent_outputs(hours_back=6, limit=DRIFT_SAMPLE_COUNT)
    except Exception as e:
        logger.error(f"Failed to sample recent outputs: {e}")
        _emit_cloudwatch_metric("SamplingError", 1, "Count")
        return {"status": "error", "reason": "sampling_failed"}

    if not recent_outputs:
        logger.warning("No recent outputs — skipping drift computation")
        _emit_cloudwatch_metric("SampleCount", 0, "Count")
        return {"status": "skipped", "reason": "no_recent_outputs"}

    embeddings = []
    for output in recent_outputs:
        # Embed proxy text not raw clinical content — HIPAA compliance
        # No PHI enters the embedding model
        proxy_text = (
            f"use_case:{output.get('use_case', 'unknown')} "
            f"confidence:{output.get('confidence_score', 'unknown')} "
            f"disposition:{output.get('disposition', 'unknown')}"
        )
        try:
            embedding = _get_embedding(proxy_text)
            embeddings.append(embedding)
        except Exception as e:
            logger.warning(f"Embedding failed for sample: {e}")
            continue

    if not embeddings:
        logger.error("All embedding requests failed")
        _emit_cloudwatch_metric("EmbeddingError", 1, "Count")
        return {"status": "error", "reason": "embedding_failed"}

    current_centroid = _compute_centroid(embeddings)
    drift_score = _cosine_distance(baseline_centroid, current_centroid)
    classification = _classify_drift(drift_score)

    logger.info(json.dumps({
        "event": "drift_score_computed",
        "drift_score": drift_score,
        "classification": classification,
        "sample_count": len(embeddings),
    }))

    _emit_cloudwatch_metric("ModelDriftScore", drift_score, "None")
    _emit_cloudwatch_metric("DriftSampleCount", len(embeddings), "Count")

    return {
        "status": "success",
        "run_id": run_id,
        "drift_score": drift_score,
        "drift_classification": classification,
        "sample_count": len(embeddings),
        "timestamp": run_timestamp.isoformat(),
    }


def _load_baseline():
    """Load baseline embedding centroid from S3."""
    response = s3.get_object(Bucket=BASELINE_BUCKET, Key=BASELINE_KEY)
    return json.loads(response["Body"].read())


def _sample_recent_outputs(hours_back, limit):
    """Query DynamoDB audit log for recent inference records. No PHI — hashes only."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    cutoff_iso = cutoff.isoformat()

    response = audit_table.scan(
        FilterExpression="#ts >= :cutoff AND attribute_exists(disposition)",
        ExpressionAttributeNames={"#ts": "timestamp"},
        ExpressionAttributeValues={":cutoff": cutoff_iso},
        ProjectionExpression="session_id, use_case, confidence_score, disposition",
        Limit=limit * 3,
    )
    return response.get("Items", [])[:limit]


def _get_embedding(text):
    """Generate embedding vector via Bedrock Titan Embeddings."""
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=json.dumps({"inputText": text}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def _compute_centroid(embeddings):
    """Compute element-wise mean — single point representing all recent outputs."""
    dim = len(embeddings[0])
    centroid = [0.0] * dim
    for emb in embeddings:
        for i in range(dim):
            centroid[i] += emb[i]
    return [v / len(embeddings) for v in centroid]


def _cosine_distance(vec_a, vec_b):
    """
    Cosine distance = 1 - cosine similarity.
    0.0 = identical to baseline, 1.0 = maximum drift.
    Uses angle between vectors — not magnitude — for semantic comparison.
    """
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a ** 2 for a in vec_a))
    mag_b = math.sqrt(sum(b ** 2 for b in vec_b))

    if mag_a == 0 or mag_b == 0:
        return 1.0

    cosine_similarity = dot / (mag_a * mag_b)
    return round(max(0.0, min(1.0, 1.0 - cosine_similarity)), 4)


def _classify_drift(score):
    """Translate drift score into operational buckets matching the runbook."""
    if score < 0.05:
        return "NOMINAL"
    elif score < 0.15:
        return "ELEVATED"
    elif score < 0.30:
        return "HIGH"
    else:
        return "CRITICAL"


def _emit_cloudwatch_metric(metric_name, value, unit):
    """Emit custom metric to CloudWatch — this is what the drift alarm watches."""
    try:
        cloudwatch.put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=[{
                "MetricName": metric_name,
                "Value": value,
                "Unit": unit,
                "Timestamp": datetime.now(timezone.utc),
                "Dimensions": [{
                    "Name": "Environment",
                    "Value": os.environ.get("ENVIRONMENT", "dev"),
                }],
            }],
        )
    except ClientError as e:
        logger.error(f"Failed to emit CloudWatch metric {metric_name}: {e}")
