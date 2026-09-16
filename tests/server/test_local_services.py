from scripts.local_services import config_digest


def test_config_digest_tracks_model_api_key_changes(monkeypatch) -> None:
    monkeypatch.setenv("ARK_API_KEY", "ark-first")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-first")
    initial = config_digest()

    monkeypatch.setenv("ARK_API_KEY", "ark-second")
    ark_changed = config_digest()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-second")
    both_changed = config_digest()

    assert initial != ark_changed != both_changed
    assert len(initial) == len(ark_changed) == len(both_changed) == 64
