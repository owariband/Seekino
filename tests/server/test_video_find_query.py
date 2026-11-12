import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from server.model.dto.mediaDTO import VideoFindTerms
from server.model.video import Series, SeriesVisibility
from server.service import video_find_query
from tests.server.test_cloud_access import _guest, cloud_api
from tests.server.test_public_course_readiness import public_course


@pytest.mark.parametrize(("input_text", "model_input", "output", "expected"), [
    (" 红温 ", "红温", {"slang": "红温", "query": "生气", "synonyms": ["恼火", "红温", "生气", "恼火"]},
     {"query": "生气", "synonyms": ["恼火"]}),
    ("紅 溫", "紅溫", {"slang": "紅溫", "query": "紅溫", "synonyms": ["生氣", "不滿"]},
     {"query": "生氣", "synonyms": ["不滿"]}),
    ("red guitar", "red guitar", {"slang": None, "query": "red", "synonyms": []},
     {"query": "red guitar", "synonyms": []}),
    ("红温", "红温", {"slang": "其他词", "query": "脸红", "synonyms": []}, {"query": "红温", "synonyms": []}),
])
def test_interpretation_uses_the_default_chat_binding_once_without_tools_and_deduplicates(
    monkeypatch, input_text, model_input, output, expected,
):
    settings, catalog, model = Mock(), Mock(), Mock()
    settings.catalog.return_value = catalog
    catalog.new_chat_id.return_value = "my-chat"
    binding = catalog.find.return_value.binding.model_copy.return_value
    binding.request_timeout_seconds = 12
    gateway = Mock()
    factory = Mock(return_value=gateway)
    monkeypatch.setattr(video_find_query, "GatewayClient", factory)
    gateway.create_generative_model.return_value = model
    model.generate.return_value = video_find_query.InterpretedFindQuery(**output)
    assert video_find_query.interpret_video_find_query(input_text, settings).model_dump() == expected
    catalog.find.assert_called_once_with("my-chat")
    gateway.create_generative_model.assert_called_once_with(binding, mcp=False)
    assert model.generate.call_count == 1
    assert model.generate.call_args.args[0][0].text.endswith(json.dumps(model_input, ensure_ascii=False))
    options = model.generate.call_args.kwargs
    assert (options["max_model_calls"], options["request_timeout_seconds"]) == (1, 12)
    gateway.close.assert_called_once()


def test_interpret_api_protects_the_episode_and_rechecks_withdrawn_access(public_course, monkeypatch):
    course = public_course
    services = course.api.services
    services.model_settings = Mock()
    terms = VideoFindTerms(query="生气", synonyms=["恼火", "不耐烦"])
    interpret = Mock(return_value=terms)
    monkeypatch.setattr("server.api.media.interpret_video_find_query", interpret)
    url = "/api/videos/find-query"
    body = {"tenantId": course.tenant, "videoId": course.episodes[0].video_id, "query": "红温"}
    with TestClient(course.api.app) as client:
        client.headers["X-CSRF-Token"] = _guest(client)["csrfToken"]
        assert client.post(url, json={**body, "tenantId": "unrelated"}).status_code == 404
        assert client.post(url, json={**body, "query": "  "}).status_code == 422
        assert client.post(url, json={**body, "query": "a" * 501}).status_code == 422
        interpret.assert_not_called()
        response = client.post(url, json=body)
        assert response.status_code == 200 and response.json() == terms.model_dump()
        assert response.headers["cache-control"] == "private, no-store"
        interpret.assert_called_once_with("红温", services.model_settings)
        interpret.side_effect = video_find_query.FindQueryUnavailable()
        unavailable = client.post(url, json=body)
        assert unavailable.status_code == 503 and unavailable.json()["code"] == "VIDEO_FIND_QUERY_UNAVAILABLE"

        def withdraw(*_):
            with course.api.sessions.begin() as session:
                session.get(Series, course.series_id).visibility = SeriesVisibility.PRIVATE
            return terms

        interpret.side_effect = withdraw
        response = client.post(url, json=body)
        assert response.status_code == 404 and "恼火" not in response.text
        assert client.post(url, json=body).status_code == 404
        assert interpret.call_count == 3
