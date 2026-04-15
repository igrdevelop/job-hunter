from hunter.sources.base import BaseSource


class _DummySource(BaseSource):
    name = "dummy"

    def search(self):
        return []


def test_coarse_prefilter_rejects_excluded_patterns() -> None:
    src = _DummySource()
    assert src.matches_coarse_prefilter("Senior Java Developer", "") is False


def test_coarse_prefilter_requires_keyword_in_title_or_context() -> None:
    src = _DummySource()
    assert src.matches_coarse_prefilter("Engineer", "Angular frontend project") is True
    assert src.matches_coarse_prefilter("Engineer", "Data platform role") is False
