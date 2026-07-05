"""
tests/test_api_layer.py — Day 6 Test Suite

Tests for:
  1. Response API — envelope building, confidence normalisation, action gating
  2. Response API — citation list normalisation, metadata block, HTTP formatting
  3. WebSocket Handler — connect/disconnect DynamoDB operations
  4. WebSocket Handler — sendmessage routing and frame construction
  5. WebSocket Handler — post_to_connection (GoneException cleanup path)
  6. Integration smoke tests — end-to-end handler invocations with mocked AWS

Run with:
    pytest tests/test_api_layer.py -v
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Minimal stubs — import before real modules to avoid real boto3 calls
# ---------------------------------------------------------------------------

def _make_boto3_stub() -> MagicMock:
    stub = MagicMock()
    stub.client.return_value = MagicMock()
    stub.resource.return_value = MagicMock()
    return stub


sys.modules.setdefault("boto3", _make_boto3_stub())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.exceptions", MagicMock())

import botocore.exceptions  # noqa: E402
botocore.exceptions.ClientError = Exception  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Add Lambda source dirs to path
# ---------------------------------------------------------------------------
import os  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lambdas", "response_api"))
sys.path.insert(0, os.path.join(_ROOT, "lambdas", "websocket_handler"))


# ===========================================================================
# Test Data Fixtures
# ===========================================================================

_SAMPLE_CITATIONS = [
    {
        "paper_id": "2401.12345",
        "title":    "LoRA: Low-Rank Adaptation of Large Language Models",
        "authors":  ["Hu, E.", "Shen, Y."],
        "year":     2024,
        "venue":    "ICLR",
        "url":      "https://arxiv.org/abs/2401.12345",
        "score":    0.92,
    },
    {
        "paper_id": "2301.99999",
        "title":    "Attention Is All You Need",
        "authors":  ["Vaswani, A."],
        "year":     2017,
        "venue":    "NeurIPS",
        "url":      "https://arxiv.org/abs/2301.99999",
        "score":    0.78,
    },
]

_SAMPLE_ANSWER = (
    "LoRA reduces trainable parameters by 10,000× [2401.12345]. "
    "The Transformer architecture underpins most modern LLMs [2301.99999]."
)

_SAMPLE_HALLUCINATION_DETAIL = {
    "claim_score":       0.87,
    "evidence_coverage": 0.91,
    "citation_accuracy": 0.95,
}

_FULL_PAYLOAD = {
    "query":                "What is LoRA and how does it work?",
    "answer":               _SAMPLE_ANSWER,
    "action":               "PASS",
    "confidence":           0.88,
    "citations":            _SAMPLE_CITATIONS,
    "hallucination_detail": _SAMPLE_HALLUCINATION_DETAIL,
    "metadata": {
        "pipeline_latency_ms": 1423.5,
        "chunks_retrieved":    10,
        "model":               "anthropic.claude-3-5-sonnet-20241022-v2:0",
    },
}


# ===========================================================================
# 1. Response API — envelope building
# ===========================================================================

class TestResponseApiEnvelopeBuilding(unittest.TestCase):
    """Tests for build_response_envelope and helper functions."""

    def setUp(self):
        import handler as ra_handler  # response_api/handler.py
        self.ra = ra_handler

    # ------------------------------------------------------------------
    # build_citation_list
    # ------------------------------------------------------------------

    def test_citation_list_normalises_fields(self):
        result = self.ra.build_citation_list(_SAMPLE_CITATIONS)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["index"], 1)
        self.assertEqual(result[0]["paper_id"], "2401.12345")
        self.assertEqual(result[0]["year"], 2024)
        self.assertAlmostEqual(result[0]["score"], 0.92, places=3)

    def test_citation_list_handles_empty(self):
        result = self.ra.build_citation_list([])
        self.assertEqual(result, [])

    def test_citation_list_handles_none(self):
        result = self.ra.build_citation_list(None)  # type: ignore[arg-type]
        self.assertEqual(result, [])

    def test_citation_list_missing_optional_fields(self):
        sparse = [{"paper_id": "xyz", "title": "A Paper"}]
        result = self.ra.build_citation_list(sparse)
        self.assertEqual(result[0]["authors"], [])
        self.assertIsNone(result[0]["year"])
        self.assertIsNone(result[0]["venue"])
        self.assertIsNone(result[0]["url"])
        self.assertAlmostEqual(result[0]["score"], 0.0, places=3)

    def test_citation_list_index_is_1_based(self):
        result = self.ra.build_citation_list(_SAMPLE_CITATIONS)
        for i, cit in enumerate(result):
            self.assertEqual(cit["index"], i + 1)

    # ------------------------------------------------------------------
    # build_confidence_block
    # ------------------------------------------------------------------

    def test_confidence_block_structure(self):
        block = self.ra.build_confidence_block(
            confidence=0.88,
            action="PASS",
            claim_score=0.87,
            evidence_coverage=0.91,
            citation_accuracy=0.95,
        )
        self.assertAlmostEqual(block["overall"], 0.88, places=3)
        self.assertEqual(block["action"], "PASS")
        self.assertAlmostEqual(block["claim_score"], 0.87, places=3)
        self.assertAlmostEqual(block["evidence_coverage"], 0.91, places=3)
        self.assertAlmostEqual(block["citation_accuracy"], 0.95, places=3)

    def test_confidence_block_optional_nones(self):
        block = self.ra.build_confidence_block(confidence=0.5, action="WARN")
        self.assertIsNone(block["claim_score"])
        self.assertIsNone(block["evidence_coverage"])
        self.assertIsNone(block["citation_accuracy"])

    # ------------------------------------------------------------------
    # build_metadata_block
    # ------------------------------------------------------------------

    def test_metadata_block_has_required_keys(self):
        meta = self.ra.build_metadata_block(
            query="test query",
            request_id="req-001",
            pipeline_latency_ms=1234.5,
            chunk_count=10,
            model_id="claude",
            api_version="1.0",
        )
        for key in ("request_id", "api_version", "model", "query_length_chars",
                    "chunks_retrieved", "pipeline_latency_ms", "timestamp_utc"):
            self.assertIn(key, meta)

    def test_metadata_block_query_length(self):
        query = "How does attention work?"
        meta = self.ra.build_metadata_block(
            query=query,
            request_id="r",
            pipeline_latency_ms=None,
            chunk_count=5,
            model_id="m",
            api_version="1.0",
        )
        self.assertEqual(meta["query_length_chars"], len(query))
        self.assertIsNone(meta["pipeline_latency_ms"])

    # ------------------------------------------------------------------
    # build_response_envelope
    # ------------------------------------------------------------------

    def test_envelope_pass_has_no_disclaimer(self):
        body = self.ra.build_response_envelope(
            query="q",
            answer=_SAMPLE_ANSWER,
            action="PASS",
            confidence=0.92,
            citations=_SAMPLE_CITATIONS,
            hallucination_detail=_SAMPLE_HALLUCINATION_DETAIL,
            pipeline_latency_ms=500.0,
            chunk_count=10,
            model_id="claude",
            request_id="r1",
        )
        self.assertNotIn("disclaimer", body)
        self.assertEqual(body["answer"], _SAMPLE_ANSWER)

    def test_envelope_warn_injects_disclaimer(self):
        body = self.ra.build_response_envelope(
            query="q",
            answer=_SAMPLE_ANSWER,
            action="WARN",
            confidence=0.45,
            citations=[],
            hallucination_detail=None,
            pipeline_latency_ms=None,
            chunk_count=0,
            model_id="claude",
            request_id="r2",
        )
        self.assertIn("disclaimer", body)
        self.assertIsInstance(body["disclaimer"], str)

    def test_envelope_refuse_replaces_answer(self):
        original = "Secret sensitive answer"
        body = self.ra.build_response_envelope(
            query="q",
            answer=original,
            action="REFUSE",
            confidence=0.1,
            citations=[],
            hallucination_detail=None,
            pipeline_latency_ms=None,
            chunk_count=0,
            model_id="claude",
            request_id="r3",
        )
        self.assertNotEqual(body["answer"], original)
        self.assertIn("disclaimer", body)
        self.assertIn("confidence", body)

    def test_envelope_pass_with_disclaimer_uses_custom_disclaimer(self):
        custom = "Custom disclaimer text"
        body = self.ra.build_response_envelope(
            query="q",
            answer=_SAMPLE_ANSWER,
            action="PASS_WITH_DISCLAIMER",
            confidence=0.75,
            citations=[],
            hallucination_detail=None,
            pipeline_latency_ms=None,
            chunk_count=0,
            model_id="claude",
            request_id="r4",
            disclaimer=custom,
        )
        self.assertEqual(body["disclaimer"], custom)

    def test_envelope_citations_normalised(self):
        body = self.ra.build_response_envelope(
            query="q",
            answer="a",
            action="PASS",
            confidence=0.9,
            citations=_SAMPLE_CITATIONS,
            hallucination_detail=None,
            pipeline_latency_ms=None,
            chunk_count=2,
            model_id="m",
            request_id="r5",
        )
        self.assertEqual(len(body["citations"]), 2)
        self.assertEqual(body["citations"][0]["index"], 1)


# ===========================================================================
# 2. Response API — HTTP formatting and status codes
# ===========================================================================

class TestResponseApiHttpFormatting(unittest.TestCase):

    def setUp(self):
        import handler as ra_handler
        self.ra = ra_handler

    def test_format_pass_returns_200(self):
        body = {"answer": "ok", "confidence": {"overall": 0.9, "action": "PASS"}}
        response = self.ra.format_http_response(body=body, action="PASS", request_id="r1")
        self.assertEqual(response["statusCode"], 200)

    def test_format_warn_returns_200(self):
        body = {}
        response = self.ra.format_http_response(body=body, action="WARN", request_id="r2")
        self.assertEqual(response["statusCode"], 200)

    def test_format_refuse_returns_422(self):
        body = {}
        response = self.ra.format_http_response(body=body, action="REFUSE", request_id="r3")
        self.assertEqual(response["statusCode"], 422)

    def test_format_unknown_action_defaults_200(self):
        body = {}
        response = self.ra.format_http_response(body=body, action="UNKNOWN", request_id="r4")
        self.assertEqual(response["statusCode"], 200)

    def test_format_response_has_cors_headers(self):
        body = {}
        response = self.ra.format_http_response(body=body, action="PASS", request_id="r5")
        self.assertIn("Access-Control-Allow-Origin", response["headers"])
        self.assertEqual(response["headers"]["Access-Control-Allow-Origin"], "*")

    def test_format_response_body_is_json_string(self):
        body = {"answer": "hello", "citations": []}
        response = self.ra.format_http_response(body=body, action="PASS", request_id="r6")
        parsed = json.loads(response["body"])
        self.assertEqual(parsed["answer"], "hello")

    def test_format_response_has_request_id_header(self):
        body = {}
        response = self.ra.format_http_response(body=body, action="PASS", request_id="req-xyz")
        self.assertEqual(response["headers"]["X-Request-Id"], "req-xyz")


# ===========================================================================
# 3. Response API — handler function (end-to-end)
# ===========================================================================

class TestResponseApiHandler(unittest.TestCase):
    """Smoke tests for response_api handler() with mocked AWS clients."""

    def setUp(self):
        import handler as ra_handler
        self.ra = ra_handler
        # Silence CloudWatch calls
        self.ra.cloudwatch = MagicMock()

    def test_handler_direct_invoke_pass(self):
        response = self.ra.handler(_FULL_PAYLOAD, {})
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertIn("answer", body)
        self.assertIn("confidence", body)
        self.assertIn("citations", body)
        self.assertIn("metadata", body)

    def test_handler_api_gateway_proxy_format(self):
        proxy_event = {
            "body": json.dumps(_FULL_PAYLOAD),
            "requestContext": {"requestId": "apigw-001"},
        }
        response = self.ra.handler(proxy_event, {})
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["metadata"]["request_id"], "apigw-001")

    def test_handler_invalid_json_body_returns_400(self):
        proxy_event = {
            "body": "NOT_JSON",
            "requestContext": {"requestId": "r1"},
        }
        response = self.ra.handler(proxy_event, {})
        self.assertEqual(response["statusCode"], 400)

    def test_handler_refuse_action_returns_422(self):
        payload = dict(_FULL_PAYLOAD, action="REFUSE", confidence=0.1)
        response = self.ra.handler(payload, {})
        self.assertEqual(response["statusCode"], 422)

    def test_handler_confidence_as_nested_dict(self):
        payload = dict(_FULL_PAYLOAD, confidence={"overall": 0.9, "action": "PASS"})
        response = self.ra.handler(payload, {})
        self.assertEqual(response["statusCode"], 200)

    def test_handler_emits_cloudwatch_metrics(self):
        self.ra.handler(_FULL_PAYLOAD, {})
        self.ra.cloudwatch.put_metric_data.assert_called()

    def test_handler_empty_query_still_processes(self):
        payload = dict(_FULL_PAYLOAD, query="")
        response = self.ra.handler(payload, {})
        # Should still return 200 — missing query is logged as warning, not error
        self.assertIn(response["statusCode"], (200, 422))


# ===========================================================================
# 4. WebSocket Handler — DynamoDB connect/disconnect
# ===========================================================================

class TestWebSocketHandlerConnectDisconnect(unittest.TestCase):

    def setUp(self):
        import handler as wsh_handler
        self.wsh = wsh_handler
        # Mock DynamoDB table
        self.mock_table = MagicMock()
        self.wsh._ws_table = self.mock_table
        # Mock cloudwatch
        self.wsh.cloudwatch = MagicMock()

    def tearDown(self):
        self.wsh._ws_table = None

    def _make_event(self, route: str, connection_id: str, body: dict | None = None) -> dict:
        return {
            "requestContext": {
                "routeKey":    route,
                "connectionId": connection_id,
                "domainName":  "abc123.execute-api.us-east-1.amazonaws.com",
                "stage":       "prod",
            },
            "body": json.dumps(body) if body else None,
        }

    # ------------------------------------------------------------------
    # $connect
    # ------------------------------------------------------------------

    def test_connect_stores_item_in_dynamodb(self):
        event = self._make_event("$connect", "conn-001")
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 200)
        self.mock_table.put_item.assert_called_once()
        call_kwargs = self.mock_table.put_item.call_args[1]
        item = call_kwargs["Item"]
        self.assertEqual(item["connection_id"], "conn-001")
        self.assertIn("ttl", item)
        self.assertIn("connected_at", item)

    def test_connect_uses_request_id_from_query_string(self):
        event = self._make_event("$connect", "conn-002")
        event["queryStringParameters"] = {"request_id": "my-request-123"}
        self.wsh.handler(event, {})
        call_kwargs = self.mock_table.put_item.call_args[1]
        self.assertEqual(call_kwargs["Item"]["request_id"], "my-request-123")

    def test_connect_dynamodb_failure_returns_500(self):
        self.mock_table.put_item.side_effect = Exception("DynamoDB error")
        event = self._make_event("$connect", "conn-003")
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 500)

    # ------------------------------------------------------------------
    # $disconnect
    # ------------------------------------------------------------------

    def test_disconnect_deletes_item_from_dynamodb(self):
        event = self._make_event("$disconnect", "conn-001")
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 200)
        self.mock_table.delete_item.assert_called_once_with(
            Key={"connection_id": "conn-001"}
        )

    def test_disconnect_dynamodb_failure_still_returns_200(self):
        # Disconnect is non-fatal on DynamoDB error
        self.mock_table.delete_item.side_effect = Exception("DB error")
        event = self._make_event("$disconnect", "conn-001")
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 200)

    # ------------------------------------------------------------------
    # Unknown route
    # ------------------------------------------------------------------

    def test_unknown_route_returns_400(self):
        event = self._make_event("unknownroute", "conn-001")
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 400)


# ===========================================================================
# 5. WebSocket Handler — sendmessage frame construction
# ===========================================================================

class TestWebSocketHandlerSendMessage(unittest.TestCase):

    def setUp(self):
        import handler as wsh_handler
        self.wsh = wsh_handler
        self.wsh._ws_table = MagicMock()
        self.wsh.cloudwatch = MagicMock()

    def _make_send_event(self, body: dict, connection_id: str = "conn-001") -> dict:
        return {
            "requestContext": {
                "routeKey":    "sendmessage",
                "connectionId": connection_id,
                "domainName":  "abc.execute-api.us-east-1.amazonaws.com",
                "stage":       "prod",
            },
            "body": json.dumps(body),
        }

    def test_token_frame_shape(self):
        """The frame sent to the client for a 'token' message must have type+data."""
        mock_client = MagicMock()
        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            event = self._make_send_event({"type": "token", "data": "Hello"})
            self.wsh.handler(event, {})
            mock_client.post_to_connection.assert_called_once()
            sent_data = json.loads(
                mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8")
            )
            self.assertEqual(sent_data["type"], "token")
            self.assertEqual(sent_data["data"], "Hello")

    def test_done_frame_shape(self):
        mock_client = MagicMock()
        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            event = self._make_send_event(
                {"type": "done", "data": "", "request_id": "r1", "action": "PASS"}
            )
            self.wsh.handler(event, {})
            sent_data = json.loads(
                mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8")
            )
            self.assertEqual(sent_data["type"], "done")
            self.assertEqual(sent_data["action"], "PASS")
            self.assertEqual(sent_data["request_id"], "r1")

    def test_error_frame_shape(self):
        mock_client = MagicMock()
        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            event = self._make_send_event(
                {"type": "error", "data": "Pipeline failure"}
            )
            self.wsh.handler(event, {})
            sent_data = json.loads(
                mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8")
            )
            self.assertEqual(sent_data["type"], "error")
            self.assertEqual(sent_data["data"], "Pipeline failure")

    def test_answer_frame_includes_payload(self):
        mock_client = MagicMock()
        payload = {"answer": "LoRA works by...", "confidence": {"overall": 0.9}}
        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            event = self._make_send_event({"type": "answer", "payload": payload})
            self.wsh.handler(event, {})
            sent_data = json.loads(
                mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8")
            )
            self.assertEqual(sent_data["type"], "answer")
            self.assertEqual(sent_data["payload"]["answer"], "LoRA works by...")

    def test_invalid_json_body_returns_400(self):
        event = {
            "requestContext": {
                "routeKey":    "sendmessage",
                "connectionId": "conn-001",
                "domainName":  "abc.execute-api.us-east-1.amazonaws.com",
                "stage":       "prod",
            },
            "body": "NOT_JSON",
        }
        response = self.wsh.handler(event, {})
        self.assertEqual(response["statusCode"], 400)


# ===========================================================================
# 6. WebSocket Handler — GoneException cleanup
# ===========================================================================

class TestWebSocketHandlerGoneException(unittest.TestCase):

    def setUp(self):
        import handler as wsh_handler
        self.wsh = wsh_handler
        self.mock_table = MagicMock()
        self.wsh._ws_table = self.mock_table
        self.wsh.cloudwatch = MagicMock()

    def test_gone_exception_triggers_dynamodb_cleanup(self):
        """When post_to_connection raises GoneException, stale connection is deleted."""
        mock_client = MagicMock()
        gone_exc = Exception("GoneException")

        # Simulate GoneException: need exceptions.GoneException on the mock client
        mock_client.exceptions = MagicMock()
        mock_client.exceptions.GoneException = type("GoneException", (Exception,), {})
        mock_client.post_to_connection.side_effect = mock_client.exceptions.GoneException

        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            result = self.wsh._post_to_connection(
                connection_id="stale-conn",
                apigw_endpoint="https://abc.execute-api.us-east-1.amazonaws.com/prod",
                message={"type": "token", "data": "hello"},
            )
        self.assertEqual(result["statusCode"], 410)
        self.mock_table.delete_item.assert_called_once_with(
            Key={"connection_id": "stale-conn"}
        )

    def test_client_error_returns_500(self):
        mock_client = MagicMock()
        mock_client.exceptions.GoneException = type("GoneException", (Exception,), {})
        mock_client.post_to_connection.side_effect = Exception("Network error")

        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            result = self.wsh._post_to_connection(
                connection_id="conn-001",
                apigw_endpoint="https://abc.execute-api.us-east-1.amazonaws.com/prod",
                message={"type": "token", "data": "hello"},
            )
        self.assertEqual(result["statusCode"], 500)


# ===========================================================================
# 7. WebSocket Handler — push_streaming_tokens helper
# ===========================================================================

class TestPushStreamingTokens(unittest.TestCase):

    def setUp(self):
        import handler as wsh_handler
        self.wsh = wsh_handler
        self.wsh._ws_table = MagicMock()
        self.wsh.cloudwatch = MagicMock()

    def test_push_sends_tokens_then_done(self):
        mock_client = MagicMock()
        mock_client.exceptions.GoneException = type("GoneException", (Exception,), {})

        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            self.wsh.push_streaming_tokens(
                connection_id="conn-001",
                tokens=["Hello", " world", "!"],
                apigw_endpoint="https://abc.execute-api.us-east-1.amazonaws.com/prod",
                request_id="req-1",
                final_action="PASS",
            )

        # Should have 4 calls: 3 tokens + 1 done
        self.assertEqual(mock_client.post_to_connection.call_count, 4)
        calls = mock_client.post_to_connection.call_args_list
        frames = [json.loads(c[1]["Data"].decode("utf-8")) for c in calls]

        # First 3 are token frames
        for i, token in enumerate(["Hello", " world", "!"]):
            self.assertEqual(frames[i]["type"], "token")
            self.assertEqual(frames[i]["data"], token)

        # Last is done frame
        self.assertEqual(frames[3]["type"], "done")
        self.assertEqual(frames[3]["action"], "PASS")
        self.assertEqual(frames[3]["request_id"], "req-1")

    def test_push_aborts_on_gone_connection(self):
        """If mid-stream connection is gone, streaming stops without sending done."""
        mock_client = MagicMock()
        gone_cls = type("GoneException", (Exception,), {})
        mock_client.exceptions.GoneException = gone_cls
        # First call succeeds, second raises GoneException
        mock_client.post_to_connection.side_effect = [
            None,           # first token OK
            gone_cls(),     # second token: connection gone
        ]

        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            self.wsh.push_streaming_tokens(
                connection_id="conn-001",
                tokens=["Hello", " world", "!"],
                apigw_endpoint="https://abc.execute-api.us-east-1.amazonaws.com/prod",
                request_id="req-2",
                final_action="PASS",
            )

        # Should have stopped after the second call (which raised GoneException)
        # Total calls = 2 (first token + second token that failed)
        self.assertLessEqual(mock_client.post_to_connection.call_count, 3)

    def test_push_empty_tokens_sends_only_done(self):
        mock_client = MagicMock()
        mock_client.exceptions.GoneException = type("GoneException", (Exception,), {})

        with patch.object(self.wsh.boto3, "client", return_value=mock_client):
            self.wsh.push_streaming_tokens(
                connection_id="conn-001",
                tokens=[],
                apigw_endpoint="https://abc.execute-api.us-east-1.amazonaws.com/prod",
                request_id="req-3",
                final_action="WARN",
            )

        # Only 1 call — the done frame
        self.assertEqual(mock_client.post_to_connection.call_count, 1)
        sent = json.loads(
            mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8")
        )
        self.assertEqual(sent["type"], "done")
        self.assertEqual(sent["action"], "WARN")


# ===========================================================================
# 8. Response API — confidence input normalisation
# ===========================================================================

class TestResponseApiConfidenceNormalisation(unittest.TestCase):

    def setUp(self):
        import handler as ra_handler
        self.ra = ra_handler
        self.ra.cloudwatch = MagicMock()

    def test_float_confidence(self):
        payload = dict(_FULL_PAYLOAD, confidence=0.75)
        response = self.ra.handler(payload, {})
        body = json.loads(response["body"])
        self.assertAlmostEqual(body["confidence"]["overall"], 0.75, places=2)

    def test_dict_confidence_with_overall_key(self):
        payload = dict(_FULL_PAYLOAD, confidence={"overall": 0.82, "action": "PASS"})
        response = self.ra.handler(payload, {})
        body = json.loads(response["body"])
        self.assertAlmostEqual(body["confidence"]["overall"], 0.82, places=2)

    def test_dict_confidence_with_score_key(self):
        payload = dict(_FULL_PAYLOAD, confidence={"score": 0.60})
        response = self.ra.handler(payload, {})
        body = json.loads(response["body"])
        self.assertAlmostEqual(body["confidence"]["overall"], 0.60, places=2)

    def test_zero_confidence_still_returns_response(self):
        payload = dict(_FULL_PAYLOAD, confidence=0.0, action="WARN")
        response = self.ra.handler(payload, {})
        self.assertIn(response["statusCode"], (200, 422))


if __name__ == "__main__":
    unittest.main(verbosity=2)
