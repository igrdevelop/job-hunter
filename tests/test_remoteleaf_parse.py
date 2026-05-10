"""RemoteLeaf HTML card parsing (no network)."""

from hunter.sources.remoteleaf import RemoteleafSource, parse_job_cards_from_html

ONE_CARD_HTML = """
<div id="job-results">
  <div class="space-y-4">
    <div class="card bg-base-100 mb-4 shadow-md hover:shadow-xl transition-shadow border border-base-200 group relative">
      <div class="card-body p-6">
        <div class="flex flex-col md:flex-row gap-5">
          <div class="flex-shrink-0">
            <a href="/company/acme/" class="relative z-10 block w-14 h-14"></a>
          </div>
          <div class="w-full flex-1">
            <h3 class="text-xl font-semibold mb-2">
              <a href="/company/acme/senior-angular-dev-remote/" class="hover:text-primary">Senior Angular Developer</a>
            </h3>
            <div class="flex flex-wrap items-center gap-x-3 mb-3">
              <a href="/company/acme/" class="relative z-10 inline-flex items-center gap-1.5 text-sm text-primary">
                <svg class="w-4 h-4"></svg>
                Acme Inc
              </a>
            </div>
            <p class="text-base-content/90 text-sm line-clamp-2 mb-4">Build Angular apps with TypeScript.</p>
            <div class="flex flex-wrap gap-3 mb-4">
              <a href="/jobs/in-poland/" class="group/pill flex items-center gap-1.5">
                <span class="text-sm font-medium group-hover/pill:text-primary">Poland</span>
              </a>
            </div>
            <div class="flex flex-wrap gap-1.5">
              <span class="px-2 py-0.5 text-xs border border-base-300 rounded-md">Angular</span>
              <span class="px-2 py-0.5 text-xs border border-base-300 rounded-md">TypeScript</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
"""


def test_parse_one_card() -> None:
    rows = parse_job_cards_from_html(ONE_CARD_HTML)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Senior Angular Developer"
    assert r["company"] == "Acme Inc"
    assert r["location"] == "Poland"
    assert r["url"] == "https://remoteleaf.com/company/acme/senior-angular-dev-remote/"
    assert "Angular" in r["skills"]


def test_remoteleaf_source_parse() -> None:
    src = RemoteleafSource()
    raw = parse_job_cards_from_html(ONE_CARD_HTML)[0]
    job = src._parse(raw)
    assert job is not None
    assert job.title == "Senior Angular Developer"
    assert job.company == "Acme Inc"
    assert job.source == "remoteleaf"


def test_remoteleaf_parse_incomplete() -> None:
    src = RemoteleafSource()
    assert src._parse({"title": "", "company": "A", "url": "https://x"}) is None
