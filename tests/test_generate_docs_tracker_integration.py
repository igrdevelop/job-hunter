import generate_docs


def test_update_tracker_delegates_to_tracker_module(monkeypatch) -> None:
    calls: list[tuple[dict, bool]] = []

    def _fake_add_applied(content: dict, force: bool = False) -> bool:
        calls.append((content, force))
        return True

    monkeypatch.setattr(generate_docs, "add_applied", _fake_add_applied)

    payload = {
        "company_name": "Acme",
        "job_title": "Senior Frontend Developer",
        "output_folder": "D:/tmp/Applications/2026-04-16/Acme",
        "apply_url": "https://example.com/jobs/1",
    }
    generate_docs.update_tracker(payload, force_mode=True)

    assert calls == [(payload, True)]
