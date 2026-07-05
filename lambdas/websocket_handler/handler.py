"""
handler.py — websocket_handler Lambda

Manages API Gateway WebSocket connections for real-time streaming delivery of
answer tokens to connected browser clients.

Responsibilities:
  $connect    — validate token (optional), store connection ID in DynamoDB
  $disconnect — remove connection ID from DynamoDB
  sendmessage — accept a streaming token batch from Answer Generator,
                push each token frame to the connected client via
                API Gateway Management API, close connection on complete.

DynamoDB table schema (WS_CONNECTIONS_TABLE):
  PK: connection_id (string)
  SK: —
  Attributes:
    request_id   : str     — echoed query request ID
    connected_at : int     — epoch seconds
    ttl          : int     — epoch + 3600 (auto-expire stale connections)

Streamed message frame format (JSON, sent per token batch):
  { "type": "token",    "data": "...partial text..." }
  { "type": "done",     "data": "",  "request_id": "...", "action": "PASS" }
  { "type": "error",    "data": "Error description" }

Timeout:  29 s  (API Gateway WebSocket max integration timeout = 29 s)
Memory:   256 MB
VPC:      no
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL))

# ---------------------------------------------------------------------------
# Environment variables (injected by CDK ApiStack)
# ---------------------------------------------------------------------------
WS_CONNECTIONS_TABLE = os.environ.get("WS_CONNECTIONS_TABLE", "ResearchWSConnections")
CW_NAMESPACE         = os.environ.get("CW_NAMESPACE", "ResearchRAG")
CONNECTION_TTL_SECS  = int(os.environ.get("CONNECTION_TTL_SECS", "3600"))

# ---------------------------------------------------------------------------
# AWS clients (module-level for Lambda container reuse)
# ---------------------------------------------------------------------------
dynamodb    = boto3.resource("dynamodb")
cloudwatch  = boto3.client("cloudwatch")

_ws_table: Any = None   # lazily initialised below


def _get_table():
    global _ws_table
    if _ws_table is None:
        _ws_table = dynamodb.Table(WS_CONNECTIONS_TABLE)
    return _ws_table


# ===========================================================================
# Route handlers
# ===========================================================================

def _handle_connect(connection_id: str, event: dict) -> dict:
    """Store the new WebSocket connection ID in DynamoDB."""
    now = int(time.time())

    # Optional: extract request_id from query string parameters
    qs = event.get("queryStringParameters") or {}
    request_id = qs.get("request_id", str(uuid.uuid4()))

    try:
        _get_table().put_item(
            Item={
                "connection_id": connection_id,
                "request_id":    request_id,
                "connected_at":  now,
                "ttl":           now + CONNECTION_TTL_SECS,
            }
        )
        logger.info("WebSocket CONNECT | connection_id=%s request_id=%s", connection_id, request_id)
        _emit_metric("WebSocketConnect", 1)
    except ClientError as exc:
        logger.error("DynamoDB put_item failed for CONNECT: %s", exc)
        return {"statusCode": 500, "body": "Failed to register connection"}

    return {"statusCode": 200, "body": "Connected"}


def _handle_disconnect(connection_id: str) -> dict:
    """Remove the WebSocket connection from DynamoDB."""
    try:
        _get_table().delete_item(Key={"connection_id": connection_id})
        logger.info("WebSocket DISCONNECT | connection_id=%s", connection_id)
        _emit_metric("WebSocketDisconnect", 1)
    except ClientError as exc:
        logger.warning("DynamoDB delete_item failed for DISCONNECT: %s", exc)
        # Non-fatal — connection will auto-expire via TTL

    return {"statusCode": 200, "body": "Disconnected"}


def _handle_send_message(connection_id: str, event: dict, apigw_endpoint: str) -> dict:
    """Push a message frame to a connected WebSocket client.

    Accepted body formats:
      (a) Token frame:   { "action": "sendmessage", "type": "token",  "data": "..." }
      (b) Done frame:    { "action": "sendmessage", "type": "done",   "data": "", "request_id": "..." }
      (c) Error frame:   { "action": "sendmessage", "type": "error",  "data": "Description" }
      (d) Full answer:   { "action": "sendmessage", "type": "answer", "payload": { ... full response ... } }
    """
    body_raw = event.get("body", "{}")
    try:
        body: dict = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
    except json.JSONDecodeError:
        logger.error("Invalid JSON in WebSocket message body")
        return {"statusCode": 400, "body": "Invalid JSON"}

    msg_type = body.get("type", "token")

    # Build the frame to send to the client
    if msg_type == "answer":
        # Full answer payload — send as a single structured message
        frame = {
            "type":    "answer",
            "payload": body.get("payload", {}),
        }
    elif msg_type == "done":
        frame = {
            "type":       "done",
            "data":       body.get("data", ""),
            "request_id": body.get("request_id", ""),
            "action":     body.get("action", ""),
        }
    elif msg_type == "error":
        frame = {"type": "error", "data": body.get("data", "An error occurred")}
    else:
        # Default: token frame
        frame = {"type": "token", "data": body.get("data", "")}

    return _post_to_connection(
        connection_id=connection_id,
        apigw_endpoint=apigw_endpoint,
        message=frame,
    )


def _post_to_connection(connection_id: str, apigw_endpoint: str, message: dict) -> dict:
    """Post a JSON message frame to a specific WebSocket connection."""
    apigw_mgmt = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=apigw_endpoint,
    )
    try:
        apigw_mgmt.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(message).encode("utf-8"),
        )
        logger.debug(
            "Posted frame | connection_id=%s type=%s",
            connection_id, message.get("type"),
        )
        _emit_metric("WebSocketMessageSent", 1)
        return {"statusCode": 200, "body": "Message sent"}

    except apigw_mgmt.exceptions.GoneException:
        # Connection no longer active — clean up DynamoDB entry
        logger.info("Stale connection %s — removing from table", connection_id)
        try:
            _get_table().delete_item(Key={"connection_id": connection_id})
        except ClientError:
            pass
        return {"statusCode": 410, "body": "Connection gone"}

    except ClientError as exc:
        logger.error("post_to_connection failed | %s | %s", connection_id, exc)
        _emit_metric("WebSocketPostError", 1)
        return {"statusCode": 500, "body": "Failed to send message"}


# ===========================================================================
# Batch push helper  (called directly by Answer Generator / Query Handler)
# ===========================================================================

def push_streaming_tokens(
    connection_id: str,
    tokens: list[str],
    apigw_endpoint: str,
    request_id: str = "",
    final_action: str = "PASS",
) -> None:
    """Push a list of token strings to a WebSocket connection, then send done frame.

    This helper is designed to be called from the Answer Generator Lambda when
    streaming answer tokens need to be delivered in real-time.
    """
    for token in tokens:
        result = _post_to_connection(
            connection_id=connection_id,
            apigw_endpoint=apigw_endpoint,
            message={"type": "token", "data": token},
        )
        if result["statusCode"] in (410, 500):
            logger.warning(
                "Aborting token stream for %s (status=%d)",
                connection_id, result["statusCode"],
            )
            return

    # Send the terminal "done" frame
    _post_to_connection(
        connection_id=connection_id,
        apigw_endpoint=apigw_endpoint,
        message={"type": "done", "data": "", "request_id": request_id, "action": final_action},
    )


# ===========================================================================
# CloudWatch metrics
# ===========================================================================

def _emit_metric(metric_name: str, value: float) -> None:
    """Emit a single WebSocket metric — best-effort, non-blocking."""
    try:
        cloudwatch.put_metric_data(
            Namespace=CW_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": "Service", "Value": "websocket-handler"}],
                    "Value": value,
                    "Unit":  "Count",
                }
            ],
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("CloudWatch metric emission failed (non-fatal): %s", exc)


# ===========================================================================
# Lambda handler
# ===========================================================================

def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point for the WebSocket Handler.

    Routes on `requestContext.routeKey`:
      $connect     → _handle_connect
      $disconnect  → _handle_disconnect
      sendmessage  → _handle_send_message

    Returns API Gateway-compatible response: { statusCode, body }
    """
    request_context = event.get("requestContext", {})
    route_key       = request_context.get("routeKey", "$default")
    connection_id   = request_context.get("connectionId", "")
    domain_name     = request_context.get("domainName", "")
    stage           = request_context.get("stage", "prod")

    # API Gateway Management API endpoint for pushing messages back
    apigw_endpoint = f"https://{domain_name}/{stage}"

    logger.info(
        "WebSocket event | route=%s connection_id=%s",
        route_key, connection_id,
    )

    if route_key == "$connect":
        return _handle_connect(connection_id=connection_id, event=event)

    if route_key == "$disconnect":
        return _handle_disconnect(connection_id=connection_id)

    if route_key == "sendmessage":
        return _handle_send_message(
            connection_id=connection_id,
            event=event,
            apigw_endpoint=apigw_endpoint,
        )

    # Unrecognised route — return 400
    logger.warning("Unrecognised WebSocket route: %s", route_key)
    return {"statusCode": 400, "body": f"Unknown route: {route_key}"}
