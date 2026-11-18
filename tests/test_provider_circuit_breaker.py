import app.model_discovery as discovery


def test_provider_circuit_opens_after_two_upstream_failures(monkeypatch):
    monkeypatch.setattr(discovery, "_cache", {
        "maj": 0, "modeles": [], "echecs": {}, "pauses": {}, "pannes_provider": {}
    })
    monkeypatch.setattr(discovery, "_sauver_cache", lambda: None)

    assert discovery.signaler_provider_echec("gemini", "HTTP 503") == 0
    pause = discovery.signaler_provider_echec("gemini", "HTTP 503")

    assert pause == 90
    assert discovery.provider_en_pause("gemini") is True
    assert discovery.provider_en_pause("groq") is False


def test_provider_circuit_backoff_increases(monkeypatch):
    monkeypatch.setattr(discovery, "_cache", {
        "maj": 0, "modeles": [], "echecs": {}, "pauses": {}, "pannes_provider": {}
    })
    monkeypatch.setattr(discovery, "_sauver_cache", lambda: None)

    discovery.signaler_provider_echec("openrouter", "timeout")
    assert discovery.signaler_provider_echec("openrouter", "timeout") == 90
    assert discovery.signaler_provider_echec("openrouter", "timeout") == 180


def test_success_closes_provider_circuit(monkeypatch):
    monkeypatch.setattr(discovery, "_cache", {
        "maj": 0,
        "modeles": [],
        "echecs": {"gemini/gemma-test": {"n": 3, "t": 1}},
        "pauses": {"gemini": 9999999999},
        "pannes_provider": {"gemini": {"n": 2, "t": 1}},
    })
    monkeypatch.setattr(discovery, "_sauver_cache", lambda: None)

    discovery.signaler_succes("gemini", "gemma-test")

    assert discovery.provider_en_pause("gemini") is False
    assert "gemini" not in discovery._charger_cache()["pannes_provider"]
    assert "gemini/gemma-test" not in discovery._charger_cache()["echecs"]
