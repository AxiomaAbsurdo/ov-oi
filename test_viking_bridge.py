"""Tests for viking_bridge.py — all HTTP interactions are mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from viking_bridge import Tools, VikingSearch, VikingWrite, _format_find_results, get_metrics


@pytest.fixture
def tools():
    """Return a fresh Tools instance with test valves."""
    t = Tools()
    t.valves.openviking_base_url = "http://localhost:1933"
    t.valves.openviking_api_key = "test-key-000"
    t.valves.openviking_account = "default"
    t.valves.openviking_user = "testuser"
    t._reset_client()
    return t


@pytest.fixture
def mock_user():
    return {"id": "user123", "chat_id": "chat456"}


def _ok_response(result, status_code=200):
    """Build a mock httpx.Response with a JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {"status": "ok", "result": result, "error": None}
    resp.text = ""
    return resp


def _error_response(status_code, detail="error"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {"detail": detail}
    resp.text = detail
    return resp


# ------------------------------------------------------------------
# Pydantic model validation
# ------------------------------------------------------------------


class TestPydanticModels:
    def test_viking_search_defaults(self):
        s = VikingSearch(prompt="test")
        assert s.top_k == 5
        assert s.score_threshold == 0.0

    def test_viking_search_bounds(self):
        s = VikingSearch(prompt="test", top_k=20, score_threshold=0.9)
        assert s.top_k == 20
        assert s.score_threshold == 0.9

    def test_viking_search_rejects_invalid_top_k(self):
        with pytest.raises(Exception):
            VikingSearch(prompt="test", top_k=0)

    def test_viking_write_required_fields(self):
        w = VikingWrite(uri="viking://test", content="hello")
        assert w.session_id is None

    def test_viking_write_with_session(self):
        w = VikingWrite(uri="viking://test", content="hello", session_id="s1")
        assert w.session_id == "s1"


# ------------------------------------------------------------------
# Configuration validation
# ------------------------------------------------------------------


class TestConfigValidation:
    @pytest.mark.asyncio
    async def test_missing_api_key_returns_error(self):
        t = Tools()
        t.valves.openviking_api_key = ""
        result = await t.query_openviking("viking://resources/test/")
        assert "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_api_key_on_send(self):
        t = Tools()
        t.valves.openviking_api_key = ""
        result = await t.send_to_viking("test query")
        assert "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_api_key_on_write(self):
        t = Tools()
        t.valves.openviking_api_key = ""
        result = await t.write_to_viking("viking://test", "content")
        assert "not configured" in result.lower()


# ------------------------------------------------------------------
# query_openviking
# ------------------------------------------------------------------


class TestQueryOpenViking:
    @pytest.mark.asyncio
    async def test_success_with_l2_content(self, tools, mock_user):
        resp = _ok_response("This is the full L2 content.")
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.query_openviking(
                "viking://resources/test/.abstract.md", __user__=mock_user
            )
        assert "L2 content" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_overview(self, tools, mock_user):
        call_count = 0

        async def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _ok_response("")
            return _ok_response("Overview text.")

        with patch.object(tools._get_client(), "request", side_effect=fake_request):
            result = await tools.query_openviking("viking://resources/test/", __user__=mock_user)
        assert "Overview text" in result
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_abstract(self, tools, mock_user):
        call_count = 0

        async def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count in (1, 2):
                return _ok_response("")
            return _ok_response("Abstract text.")

        with patch.object(tools._get_client(), "request", side_effect=fake_request):
            result = await tools.query_openviking("viking://resources/test/", __user__=mock_user)
        assert "Abstract text" in result
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_empty_context_id(self, tools):
        result = await tools.query_openviking("")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_content_found(self, tools, mock_user):
        resp = _ok_response("")
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.query_openviking("viking://resources/empty/", __user__=mock_user)
        assert "No content found" in result


# ------------------------------------------------------------------
# send_to_viking (with tunables)
# ------------------------------------------------------------------


class TestSendToViking:
    @pytest.mark.asyncio
    async def test_success(self, tools, mock_user):
        resp = _ok_response({
            "memories": [],
            "resources": [
                {
                    "context_type": "resource",
                    "uri": "viking://resources/example_app/AUTH",
                    "level": 0,
                    "score": 0.47,
                    "abstract": "Auth implementation summary.",
                }
            ],
            "skills": [],
            "total": 1,
        })
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("authentication flow", __user__=mock_user)
        assert "0.47" in result
        assert "AUTH" in result

    @pytest.mark.asyncio
    async def test_with_tunables(self, tools, mock_user):
        resp = _ok_response({
            "memories": [],
            "resources": [
                {"uri": "viking://test/A", "score": 0.9, "abstract": "High."},
                {"uri": "viking://test/B", "score": 0.3, "abstract": "Low."},
            ],
            "skills": [],
            "total": 2,
        })
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking(
                "test", top_k=10, score_threshold=0.5, __user__=mock_user
            )
        assert "viking://test/A" in result
        assert "viking://test/B" not in result  # filtered by threshold

    @pytest.mark.asyncio
    async def test_empty_prompt(self, tools):
        result = await tools.send_to_viking("")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_results(self, tools, mock_user):
        resp = _ok_response({"memories": [], "resources": [], "skills": [], "total": 0})
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("nonexistent topic", __user__=mock_user)
        assert "No results" in result


# ------------------------------------------------------------------
# write_to_viking
# ------------------------------------------------------------------


class TestWriteToViking:
    @pytest.mark.asyncio
    async def test_success(self, tools, mock_user):
        resp = _ok_response({"written": True})
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.write_to_viking(
                "viking://resources/test/NOTE",
                "Summary of findings.",
                __user__=mock_user,
            )
        assert "Written to" in result
        assert "webui_user123_chat456" in result

    @pytest.mark.asyncio
    async def test_with_explicit_session(self, tools, mock_user):
        resp = _ok_response({"written": True})
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.write_to_viking(
                "viking://resources/test/NOTE",
                "Content.",
                session_id="my-session-1",
                __user__=mock_user,
            )
        assert "my-session-1" in result

    @pytest.mark.asyncio
    async def test_empty_uri(self, tools):
        result = await tools.write_to_viking("", "content")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_empty_content(self, tools):
        result = await tools.write_to_viking("viking://test", "")
        assert "Error" in result


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout(self, tools):
        with patch.object(
            tools._get_client(), "request", side_effect=httpx.TimeoutException("timed out")
        ):
            result = await tools.query_openviking("viking://resources/test/")
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_connection_error(self, tools):
        with patch.object(
            tools._get_client(), "request", side_effect=httpx.ConnectError("refused")
        ):
            result = await tools.query_openviking("viking://resources/test/")
        assert "unreachable" in result.lower()

    @pytest.mark.asyncio
    async def test_auth_failure_401(self, tools):
        resp = _error_response(401, "Unauthorized")
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.query_openviking("viking://resources/test/")
        assert "authentication" in result.lower()

    @pytest.mark.asyncio
    async def test_auth_failure_403(self, tools):
        resp = _error_response(403, "Forbidden")
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("test query")
        assert "authentication" in result.lower()

    @pytest.mark.asyncio
    async def test_server_error_500(self, tools):
        resp = _error_response(500, "Internal Server Error")
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("test")
        assert "500" in result

    @pytest.mark.asyncio
    async def test_malformed_json(self, tools):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.text = "not json at all"
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.query_openviking("viking://resources/test/")
        assert "json" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_status_field(self, tools):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"result": "something"}
        resp.text = ""
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.query_openviking("viking://resources/test/")
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_error_status(self, tools):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"status": "error", "error": "something broke"}
        resp.text = ""
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("test")
        assert "something broke" in result

    @pytest.mark.asyncio
    async def test_unexpected_result_type(self, tools):
        resp = _ok_response({"unexpected": True})
        with patch.object(tools._get_client(), "request", new_callable=AsyncMock, return_value=resp):
            result = await tools.send_to_viking("test")
        assert "No results" in result or "Unexpected" in result


# ------------------------------------------------------------------
# User mapping / ACL
# ------------------------------------------------------------------


class TestUserMapping:
    def test_resolve_with_valid_mapping(self, tools):
        tools.valves.user_mapping = '{"alice@co.com": "viking_alice"}'
        assert tools._resolve_viking_user("alice@co.com") == "viking_alice"

    def test_resolve_unknown_user(self, tools):
        tools.valves.user_mapping = '{"alice@co.com": "viking_alice"}'
        assert tools._resolve_viking_user("unknown@co.com") is None

    def test_resolve_empty_mapping(self, tools):
        tools.valves.user_mapping = ""
        assert tools._resolve_viking_user("alice@co.com") is None

    def test_resolve_invalid_json(self, tools):
        tools.valves.user_mapping = "not-json"
        assert tools._resolve_viking_user("alice@co.com") is None


# ------------------------------------------------------------------
# Format helper with score_threshold
# ------------------------------------------------------------------


class TestFormatFindResults:
    def test_basic_format(self):
        result = {
            "total": 1,
            "resources": [{"uri": "viking://test", "score": 0.8, "abstract": "Hello."}],
            "memories": [],
            "skills": [],
        }
        out = _format_find_results(result)
        assert "0.80" in out
        assert "viking://test" in out

    def test_score_threshold_filters(self):
        result = {
            "total": 2,
            "resources": [
                {"uri": "viking://high", "score": 0.9, "abstract": "A."},
                {"uri": "viking://low", "score": 0.2, "abstract": "B."},
            ],
            "memories": [],
            "skills": [],
        }
        out = _format_find_results(result, score_threshold=0.5)
        assert "viking://high" in out
        assert "viking://low" not in out


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------


class TestMetrics:
    def test_metrics_snapshot(self):
        m = get_metrics()
        assert "tool_calls_total" in m
        assert "tool_latency_seconds_avg" in m
