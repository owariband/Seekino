import copy
import json

from evals.parse_half_episode_benchmark import history_context


def test_history_uses_only_completed_outputs_and_preserves_provenance():
    outputs = [
        {'timeline': [{'type': 'dialogue', 'speaker': '甲', 'text': '前一句', 'start': 0, 'end': 1000}]},
        {'timeline': [{'type': 'dialogue', 'speaker': '乙', 'text': '后一句', 'start': 60000, 'end': 61000}]},
    ]
    original = copy.deepcopy(outputs)
    history = history_context(outputs)
    assert [(item['source_chunk'], item['event']['text']) for item in history['events']] == [(0, '前一句'), (1, '后一句')]
    assert history['names'] == ['乙', '甲']
    assert not history['older_events_omitted']
    assert outputs == original


def test_history_truncates_whole_old_events_without_inventing_a_summary():
    outputs = [
        {'timeline': [{'type': 'shot', 'text': '早先事实' * 100, 'start': 0, 'end': 1000}]},
        {'timeline': [{'type': 'shot', 'text': '最新事实', 'start': 60000, 'end': 61000}]},
    ]
    history = history_context(outputs, limit=200)
    assert history['older_events_omitted']
    assert len(history['events']) == 1
    assert history['events'][0]['source_chunk'] == 1
    assert history['events'][0]['event'] == outputs[1]['timeline'][0]
    assert len(json.dumps(history, ensure_ascii=False)) <= 250


def test_first_chunk_has_no_previous_output_or_identity_answers():
    assert history_context([]) == {'names': [], 'events': [], 'older_events_omitted': False}
