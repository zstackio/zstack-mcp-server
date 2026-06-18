import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from zstack_mcp.zstack_client import ZStackApiError, ZStackClient


class _DummyResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingHttpClient:
    def __init__(self):
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.post_payload: dict[str, Any] = {
            "org.zstack.header.zone.APIQueryZoneReply": {
                "success": True,
                "inventories": [],
            }
        }
        self.get_payload: dict[str, Any] = {
            "org.zstack.header.zone.APIQueryZoneReply": {
                "success": True,
                "inventories": [],
            }
        }

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _DummyResponse(self.post_payload)

    async def get(self, url: str, headers: dict[str, str]):
        self.gets.append({"url": url, "headers": headers})
        return _DummyResponse(self.get_payload)

    async def aclose(self):
        return None


def test_access_key_signature_matches_go_sdk_shape() -> None:
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )
    date = "Mon, 02 Jan 2006 15:04:05 UTC"
    headers = client._access_key_auth_headers("POST", client.api_endpoint, date=date)

    string_to_sign = f"POST\n{date}\n/api/"
    expected_signature = base64.b64encode(
        hmac.new(b"sk", string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    assert headers["Authorization"] == f"ZStack ak:{expected_signature}"
    assert headers["Date"] == date
    assert ZStackClient._canonical_access_key_uri("http://example.com:8080/zstack/api/?x=1") == "/api/"


def test_explicit_access_key_is_not_overridden_by_env_session(monkeypatch) -> None:
    monkeypatch.setenv("ZSTACK_SESSION_ID", "env-session")

    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )

    assert client.auth_mode == "access_key"
    assert client.session is None


@pytest.mark.anyio
async def test_execute_with_access_key_uses_rest_get(monkeypatch) -> None:
    recorder = _RecordingHttpClient()
    recorder.get_payload = {"inventories": []}
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )

    async def get_http_client():
        return recorder

    monkeypatch.setattr(client, "_get_http_client", get_http_client)

    result = await client.execute(
        "QueryZone",
        "org.zstack.header.zone.APIQueryZoneMsg",
        {
            "conditions": [{"name": "name", "op": "=", "value": "zone-a"}],
            "limit": 10,
            "replyWithCount": True,
        },
    )

    assert result == {"inventories": []}
    assert recorder.posts == []
    assert len(recorder.gets) == 1
    assert recorder.gets[0]["url"].startswith("http://example.com:8080/zstack/v1/zones?")
    assert "q=name%3Dzone-a" in recorder.gets[0]["url"]
    assert "limit=10" in recorder.gets[0]["url"]
    assert "replyWithCount=true" in recorder.gets[0]["url"]
    assert recorder.gets[0]["headers"]["Authorization"].startswith("ZStack ak:")
    assert "Date" in recorder.gets[0]["headers"]


@pytest.mark.anyio
async def test_execute_with_access_key_blocks_unmapped_message_api(monkeypatch) -> None:
    recorder = _RecordingHttpClient()
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )

    async def get_http_client():
        return recorder

    monkeypatch.setattr(client, "_get_http_client", get_http_client)

    with pytest.raises(ZStackApiError) as exc:
        await client.execute(
            "QueryNotMappedResource",
            "org.zstack.header.unknown.APIQueryNotMappedResourceMsg",
            {"conditions": []},
        )

    assert exc.value.code == "REST_MAPPING_NOT_FOUND"
    assert recorder.posts == []
    assert recorder.gets == []


@pytest.mark.anyio
async def test_query_metric_data_with_access_key_uses_rest_get(monkeypatch) -> None:
    recorder = _RecordingHttpClient()
    recorder.get_payload = {
        "data": [
            {
                "labels": {"VMUuid": "vm-1", "CPUNum": "0"},
                "time": 1700000000,
                "value": 42.5,
            }
        ]
    }
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )

    async def get_http_client():
        return recorder

    monkeypatch.setattr(client, "_get_http_client", get_http_client)

    result = await client.query_metric_data(
        namespace="ZStack/VM",
        metric_name="CPUUsedUtilization",
        start_time=1700000000000,
        end_time=1700000060,
        period="60",
        labels={"VMUuid": "vm-1", "CPUNum": "0"},
    )

    assert result == recorder.get_payload
    assert recorder.posts == []
    assert len(recorder.gets) == 1
    parsed = urlparse(recorder.gets[0]["url"])
    query = parse_qs(parsed.query)
    assert parsed.path == "/zstack/v1/zwatch/metrics"
    assert query["namespace"] == ["ZStack/VM"]
    assert query["metricName"] == ["CPUUsedUtilization"]
    assert query["startTime"] == ["1700000000"]
    assert query["endTime"] == ["1700000060"]
    assert query["period"] == ["60"]
    assert query["labels"] == ["VMUuid=vm-1", "CPUNum=0"]
    assert recorder.gets[0]["headers"]["Authorization"].startswith("ZStack ak:")
    assert "Date" in recorder.gets[0]["headers"]


@pytest.mark.anyio
async def test_execute_get_metric_data_with_access_key_uses_metric_rest_params(monkeypatch) -> None:
    recorder = _RecordingHttpClient()
    recorder.get_payload = [
        {
            "labels": {"VMUuid": "vm-1"},
            "time": 1700000000,
            "value": 7,
        }
    ]
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )

    async def get_http_client():
        return recorder

    monkeypatch.setattr(client, "_get_http_client", get_http_client)

    result = await client.execute(
        "GetMetricData",
        "org.zstack.zwatch.api.APIGetMetricDataMsg",
        {
            "namespace": "ZStack/VM",
            "metricName": "CPUUsedUtilization",
            "valueConditions": ["value>1"],
            "functions": ["max"],
        },
    )

    assert result == {"data": recorder.get_payload}
    assert recorder.posts == []
    assert len(recorder.gets) == 1
    parsed = urlparse(recorder.gets[0]["url"])
    query = parse_qs(parsed.query)
    assert parsed.path == "/zstack/v1/zwatch/metrics"
    assert query["namespace"] == ["ZStack/VM"]
    assert query["metricName"] == ["CPUUsedUtilization"]
    assert query["valueConditions"] == ["value>1"]
    assert query["functions"] == ["max"]


@pytest.mark.anyio
async def test_poll_job_with_access_key_signs_get(monkeypatch) -> None:
    recorder = _RecordingHttpClient()
    client = ZStackClient(
        api_url="http://example.com:8080",
        access_key_id="ak",
        access_key_secret="sk",
    )
    client.JOB_POLL_INTERVAL = 0

    async def get_http_client():
        return recorder

    monkeypatch.setattr(client, "_get_http_client", get_http_client)

    result = await client._poll_job("http://example.com:8080/zstack/api/result/job-1")

    assert result["success"] is True
    assert len(recorder.gets) == 1
    assert recorder.gets[0]["headers"]["Authorization"].startswith("ZStack ak:")
    assert "Date" in recorder.gets[0]["headers"]
