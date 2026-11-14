from pathlib import Path

import httpx
import pytest

from job_apply_agent.collectors.base import CollectorError
from job_apply_agent.collectors.job_link import (
    MAX_HTML_BYTES,
    fetch_job_url,
    parse_job_page,
    validate_public_url,
)
from job_apply_agent.domain.enums import ATSKind

FIXTURES = Path(__file__).parent / "fixtures"


HTML = """
<html><head><script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "identifier": {"value": "job-123"},
  "title": "Machine Learning Engineer",
  "description": "Build and evaluate LLM systems with Python.",
  "hiringOrganization": {"name": "Example Labs"},
  "employmentType": "FULL_TIME",
  "jobLocation": {"address": {
    "addressLocality": "Toronto",
    "addressRegion": "Ontario",
    "addressCountry": "Canada"
  }}
}
</script></head></html>
"""


def test_parse_schema_org_job_posting():
    job = parse_job_page("https://example.com/jobs/123", HTML)

    assert job.source is ATSKind.UNKNOWN
    assert job.source_id == "job-123"
    assert job.company == "Example Labs"
    assert job.title == "Machine Learning Engineer"
    assert job.location == "Toronto, Ontario, Canada"
    assert job.raw_snapshot == {}


async def test_fetch_generic_job_page_without_redirects():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=HTML))
    async with httpx.AsyncClient(transport=transport) as client:
        job = await fetch_job_url(
            "https://example.com/jobs/123",
            client=client,
            host_validator=lambda url: _noop(),
        )
    assert job.source_id == "job-123"


async def test_private_url_is_rejected():
    with pytest.raises(CollectorError, match="私有网络"):
        await validate_public_url("http://127.0.0.1/internal")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_html_fallback_extracts_title_company_description():
    job = parse_job_page("https://example.com/jobs/9", _fixture("job_page_basic.html"))

    assert job.source is ATSKind.UNKNOWN
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Robotics"
    assert "real-time telemetry" in job.description
    assert job.employment_type == "full-time"
    assert job.metadata == {"schema_org": False}
    assert job.raw_snapshot == {}


def test_parse_html_fallback_prefers_meta_tags():
    job = parse_job_page("https://example.com/jobs/7", _fixture("job_page_meta_only.html"))

    assert job.title == "Staff Data Engineer"
    assert job.company == "Northwind Analytics"
    assert job.employment_type == "permanent"


def test_parse_html_fallback_company_falls_back_to_hostname():
    job = parse_job_page("https://jobs.acme.ca/jobs/5", _fixture("job_page_hostname_company.html"))

    assert job.title == "Platform Engineer"
    assert job.company == "acme"


def test_parse_html_fallback_reads_at_company_title():
    html = """
    <html><head>
    <title>Job Application for Associate, Corporate Development at Tenstorrent</title>
    </head><body>
    <main>
      <h1>Associate, Corporate Development</h1>
      <p>Tenstorrent is looking for an Associate on the Corporate Development team to support
      M&amp;A analysis, market research, and board materials. The role is based in Toronto and
      reports to the Director of Corporate Development. Candidates should have two years of
      investment banking or consulting experience and strong financial modelling skills.</p>
      <p>We offer comprehensive benefits, stock options, and a collaborative hybrid schedule
      with three days per week in our downtown Toronto office.</p>
    </main>
    </body></html>
    """
    job = parse_job_page("https://job-boards.example.io/acme/jobs/11", html)

    assert job.title == "Associate, Corporate Development"
    assert job.company == "Tenstorrent"


def test_parse_html_fallback_rejects_insufficient_page():
    with pytest.raises(CollectorError, match="正文太短"):
        parse_job_page("https://example.com/jobs/1", _fixture("job_page_insufficient.html"))


def test_jsonld_pages_still_prefer_jsonld():
    html = """
    <html><head><title>Wrong H1 Title | Decoy Corp</title>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Real Title", "description": "Real description.",
     "hiringOrganization": {"name": "Real Corp"}}
    </script></head>
    <body><h1>Wrong H1 Title</h1><p>Decoy body text that should be ignored entirely.</p></body>
    </html>
    """
    job = parse_job_page("https://example.com/jobs/3", html)

    assert job.title == "Real Title"
    assert job.company == "Real Corp"
    assert job.metadata["schema_org"] is True


async def test_fetch_generic_html_page_without_jsonld():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_fixture("job_page_basic.html"))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        job = await fetch_job_url(
            "https://example.com/jobs/9",
            client=client,
            host_validator=lambda url: _noop(),
        )
    assert job.company == "Acme Robotics"


async def test_fetch_rejects_redirects():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.example/j/1"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CollectorError, match="不允许自动跟随跳转"):
            await fetch_job_url(
                "https://example.com/jobs/1",
                client=client,
                host_validator=lambda url: _noop(),
            )


async def test_fetch_rejects_oversized_pages():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="x" * (MAX_HTML_BYTES + 1))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CollectorError, match="2 MB"):
            await fetch_job_url(
                "https://example.com/jobs/1",
                client=client,
                host_validator=lambda url: _noop(),
            )


async def _noop() -> None:
    return None


async def test_oversized_stream_is_closed_before_reading_the_whole_body():
    class LargeStream(httpx.AsyncByteStream):
        chunks_read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.chunks_read += 1
                yield b"x" * 65536

        async def aclose(self):
            self.closed = True

    stream = LargeStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CollectorError, match="2 MB"):
            await fetch_job_url(
                "https://example.com/jobs/1", client=client, host_validator=lambda url: _noop()
            )
    assert stream.chunks_read <= MAX_HTML_BYTES // 65536 + 2
    assert stream.closed


async def test_redirect_policy_overrides_client_defaults():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/internal"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(CollectorError, match="不允许自动跟随跳转"):
            await fetch_job_url(
                "https://example.com/jobs/1", client=client, host_validator=lambda url: _noop()
            )
    assert requests == ["https://example.com/jobs/1"]
