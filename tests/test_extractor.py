import json
from unittest import mock

import pytest
import requests

import extractor
from extractor import (
    ModelNotFound, ModelOutputError, OllamaTimeout, OllamaUnavailable,
    normalize_items, parse_model_json, validate_result,
)


def fake_response(body, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


def ollama_says(text):
    return fake_response({"response": text})


GOOD = {"items": [
    {"type": "DEADLINE", "category": "assignment", "action": "Submit DBMS assignment",
     "details": None, "date_text": "Friday", "time_text": "11:59 PM",
     "source_text": "Submit the DBMS assignment by Friday at 11:59 PM.", "applicability": "explicit"},
    {"type": "EVENT", "category": "meeting", "action": "Presentation",
     "details": None, "date_text": "next Monday", "time_text": "10 AM",
     "source_text": "The presentation is next Monday at 10 AM.", "applicability": "explicit"},
]}


# ------------------------------------------------------------ parsing
def test_parse_plain_json():
    assert parse_model_json(json.dumps(GOOD)) == GOOD


def test_parse_with_think_and_fence():
    raw = "<think>hmm</think>\nHere you go:\n```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_model_json(raw) == GOOD


def test_parse_with_surrounding_chatter():
    raw = "Sure! " + json.dumps(GOOD) + " Hope that helps {not json"
    assert parse_model_json(raw) == GOOD


@pytest.mark.parametrize("raw", ["", "not json at all", '{"items": [', None])
def test_parse_invalid(raw):
    with pytest.raises(ModelOutputError):
        parse_model_json(raw)


# ------------------------------------------------------------ normalisation
def test_normalize_repairs_and_drops():
    items, warnings = normalize_items({"tasks": [
        {"title": "Lab report", "type": "deadline", "date": "tomorrow", "time": None},
        {"type": "EVENT"},                      # no title -> dropped
        "garbage",                              # not an object -> dropped
        {"action": "Quiz", "type": "EXAM", "applicability": "maybe", "date_text": 5},
    ]})
    assert [i["action"] for i in items] == ["Lab report", "Quiz"]
    assert items[0]["type"] == "DEADLINE" and items[0]["date_text"] == "tomorrow"
    assert items[1]["type"] == "EVENT" and items[1]["applicability"] == "unclear"
    assert items[1]["date_text"] == "5"
    assert len(warnings) == 2


def test_normalize_top_level_list_and_single_object():
    items, _ = normalize_items([{"action": "A", "type": "TASK"}])
    assert items[0]["action"] == "A"
    items, _ = normalize_items({"action": "B", "type": "TASK"})
    assert items[0]["action"] == "B"


def test_normalize_rejects_wrong_root():
    with pytest.raises(ModelOutputError):
        normalize_items({"foo": 1})
    with pytest.raises(ModelOutputError):
        normalize_items("string")


def test_normalize_caps_lengths():
    items, _ = normalize_items({"items": [{"action": "x" * 500, "type": "TASK", "details": "y" * 5000}]})
    assert len(items[0]["action"]) == 120 and len(items[0]["details"]) == 1000


def test_strict_validate_result_kept():
    assert validate_result(GOOD) is None
    assert validate_result({"items": [{"type": "X"}]})


# ------------------------------------------------------------ transport
def test_extract_items_multiple_deadlines():
    with mock.patch("extractor.requests.post", return_value=ollama_says(json.dumps(GOOD))) as post:
        out = extractor.extract_items("Submit the DBMS assignment by Friday at 11:59 PM. The presentation is next Monday at 10 AM.")
    assert len(out["items"]) == 2
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == extractor.MODEL and payload["think"] is False
    assert payload["options"]["temperature"] == 0
    assert "<<<" in payload["prompt"]


def test_retry_once_on_malformed_output():
    responses = [ollama_says("{broken"), ollama_says(json.dumps(GOOD))]
    with mock.patch("extractor.requests.post", side_effect=responses):
        out = extractor.extract_items("text")
    assert len(out["items"]) == 2


def test_malformed_twice_raises():
    with mock.patch("extractor.requests.post", return_value=ollama_says("nope")):
        with pytest.raises(ModelOutputError):
            extractor.extract_items("text")


def test_connection_error():
    with mock.patch("extractor.requests.post", side_effect=requests.exceptions.ConnectionError()):
        with pytest.raises(OllamaUnavailable):
            extractor.extract_items("text")


def test_timeout():
    with mock.patch("extractor.requests.post", side_effect=requests.exceptions.Timeout()):
        with pytest.raises(OllamaTimeout):
            extractor.extract_items("text")


def test_model_missing():
    with mock.patch("extractor.requests.post", return_value=fake_response({"error": "model not found"}, 404)):
        with pytest.raises(ModelNotFound) as exc:
            extractor.extract_items("text")
    assert "ollama pull" in str(exc.value)


def test_schema_format_fallback_on_old_ollama():
    responses = [fake_response({"error": "invalid format"}, 400), ollama_says(json.dumps(GOOD))]
    with mock.patch("extractor.requests.post", side_effect=responses) as post:
        extractor.extract_items("text")
    assert isinstance(post.call_args_list[0].kwargs["json"]["format"], dict)
    assert post.call_args_list[1].kwargs["json"]["format"] == "json"


def test_ollama_error_field():
    with mock.patch("extractor.requests.post", return_value=fake_response({"error": "out of memory"})):
        with pytest.raises(OllamaUnavailable):
            extractor.extract_items("text")


def test_long_text_is_chunked_and_deduplicated():
    text = ("Submit the DBMS assignment by Friday at 11:59 PM. " * 150).strip()
    with mock.patch("extractor.requests.post", return_value=ollama_says(json.dumps(GOOD))) as post:
        out = extractor.extract_items(text)
    assert post.call_count > 1
    assert len(out["items"]) == 2  # duplicates across chunks removed


def test_very_long_text_truncated_with_warning():
    with mock.patch("extractor.requests.post", return_value=ollama_says('{"items": []}')):
        out = extractor.extract_items("word " * 5000)
    assert any("first" in w for w in out["warnings"])


def test_legacy_extract_obligations_reports_errors():
    with mock.patch("extractor.requests.post", side_effect=requests.exceptions.ConnectionError()):
        res = extractor.extract_obligations("hi")
    assert res["success"] is False and res["error_code"] == "ollama_unavailable"
