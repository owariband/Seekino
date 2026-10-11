import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from gateway.rerank import RerankScore, StructuredRerankModel


def model_with_scores(scores):
    delegate = Mock(provider_name="configured-provider", model_name="configured-model")
    delegate.generate.side_effect = lambda inputs, **kwargs: kwargs["result_type"].model_validate({"scores": scores})
    return StructuredRerankModel(delegate), delegate


def test_scores_preserve_document_indexes_and_unicode():
    model, delegate = model_with_scores([{"index": 1, "score": 0.2}, {"index": 0, "score": 0.9}])
    assert model.rerank("查找片段", ["甲", "乙"]) == (RerankScore(0, 0.9), RerankScore(1, 0.2))
    prompt = delegate.generate.call_args.args[0][0].text
    assert json.loads(prompt.split("\n", 1)[1]) == {
        "query": "查找片段", "candidates": [{"index": 0, "text": "甲"}, {"index": 1, "text": "乙"}],
    }
    assert model.provider_name == "configured-provider"
    assert model.model_name == "configured-model"
    model.close()
    delegate.close.assert_called_once()


@pytest.mark.parametrize("scores", [
    [{"index": 0, "score": 0.1}],
    [{"index": 0, "score": 0.1}, {"index": 0, "score": 0.2}],
    [{"index": 0, "score": 0.1}, {"index": 2, "score": 0.2}],
])
def test_rejects_missing_duplicate_and_invented_indexes(scores):
    model, _ = model_with_scores(scores)
    with pytest.raises(ValueError, match="exactly once"):
        model.rerank("query", ["first", "second"])


@pytest.mark.parametrize("score", [float("inf"), float("nan"), "0.5"])
def test_rejects_invalid_provider_scores(score):
    model, _ = model_with_scores([{"index": 0, "score": score}])
    with pytest.raises(ValidationError):
        model.rerank("query", ["document"])


@pytest.mark.parametrize("query,documents", [(" ", ["doc"]), ("q", []), ("q", [" "]), ("q", "doc")])
def test_invalid_requests_do_not_invoke_provider(query, documents):
    model, delegate = model_with_scores([])
    with pytest.raises(ValueError):
        model.rerank(query, documents)
    delegate.generate.assert_not_called()
