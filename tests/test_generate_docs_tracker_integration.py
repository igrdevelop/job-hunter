import generate_docs


def test_update_tracker_delegates_to_tracker_service(monkeypatch) -> None:
    calls: list[tuple[dict, bool]] = []

    def _fake_record_successful_apply(content: dict, force: bool = False) -> bool:
        calls.append((content, force))
        return True

    monkeypatch.setattr(generate_docs, "record_successful_apply", _fake_record_successful_apply)

    payload = {
        "company_name": "Acme",
        "job_title": "Senior Frontend Developer",
        "output_folder": "D:/tmp/Applications/2026-04-16/Acme",
        "apply_url": "https://example.com/jobs/1",
    }
    generate_docs.update_tracker(payload, force_mode=True)

    assert calls == [(payload, True)]
