from __future__ import annotations

import asyncio
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob

from .ashby import AshbySource
from .base import CollectorError
from .greenhouse import GreenhouseSource
from .lever import LeverSource


MAX_HTML_BYTES = 2_000_000
MIN_TITLE_CHARS = 3
MIN_DESCRIPTION_CHARS = 200
HostValidator = Callable[[str], Awaitable[None]]

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset(
    {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article", "main",
     "ul", "ol", "table", "tr", "blockquote", "pre", "header", "footer", "dd", "dt", "figcaption"}
)
_META_KEYS = frozenset({"description", "og:title", "og:site_name"})
_GENERIC_HOST_LABELS = frozenset(
    {"www", "jobs", "job", "careers", "career", "work", "boards", "job-boards", "apply",
     "hiring", "openings", "positions", "join", "company"}
)
_GENERIC_SITE_WORDS = frozenset(
    {"careers", "jobs", "job", "hiring", "apply", "work", "company", "remote", "home",
     "team", "opportunities", "position", "openings"}
)
_TITLE_AT_TAIL_RE = re.compile(r"\s+at\s+([A-Za-z0-9][^,|·]*?)\s*$", re.IGNORECASE)
_EMPLOYMENT_RE = re.compile(
    r"\b(full[\s-]?time|part[\s-]?time|contract|temporary|casual|permanent|internship)\b",
    re.IGNORECASE,
)


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._chunks: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        self._capturing = (
            tag.casefold() == "script"
            and (attributes.get("type") or "").casefold() == "application/ld+json"
        )
        if self._capturing:
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._capturing:
            return
        self._capturing = False
        try:
            self.documents.append(json.loads("".join(self._chunks)))
        except json.JSONDecodeError:
            pass


def _opens_main_container(tag: str, attributes: dict[str, str]) -> bool:
    if tag in {"main", "article"}:
        return True
    if tag != "div":
        return False
    if attributes.get("role", "").casefold() == "main":
        return True
    return "job" in attributes.get("id", "").casefold() or "job" in attributes.get(
        "class", ""
    ).casefold()


class _HeuristicJobParser(HTMLParser):
    """Capture title, meta, h1, and block text without any external dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_text = ""
        self.meta: dict[str, str] = {}
        self.headings: list[str] = []
        self._body_chunks: list[str] = []
        self._main_chunks: list[str] = []
        self._h1_chunks: list[str] = []
        self._main_stack: list[str] = []
        self._skip_depth = 0
        self._head_depth = 0
        self._h1_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "head":
            self._head_depth += 1
            return
        if tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").casefold()
            content = attributes.get("content", "").strip()
            if key in _META_KEYS and content:
                self.meta.setdefault(key, content)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "h1":
            self._h1_depth += 1
        if tag in _BLOCK_TAGS:
            self._body_chunks.append("\n")
            self._main_chunks.append("\n")
        if _opens_main_container(tag, attributes):
            self._main_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "head":
            self._head_depth = max(0, self._head_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "h1":
            self._h1_depth = max(0, self._h1_depth - 1)
            if self._h1_depth == 0 and self._h1_chunks:
                self.headings.append(" ".join(self._h1_chunks))
                self._h1_chunks = []
        # A matching container may be nested under unclosed inner tags; pop up
        # to and including it so sloppy HTML cannot leak main content.
        if tag in self._main_stack:
            while self._main_stack:
                if self._main_stack.pop() == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data.strip():
            return
        text = " ".join(data.split())
        if self._in_title:
            self.title_text += text
            return
        if self._head_depth > 0:
            return
        self._body_chunks.append(text)
        if self._main_stack:
            self._main_chunks.append(text)
        if self._h1_depth > 0:
            self._h1_chunks.append(text)


async def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise CollectorError("链接必须是公开的 HTTP(S) 岗位页面")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise CollectorError("无法解析岗位链接的域名") from exc
    if not addresses:
        raise CollectorError("岗位链接没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise CollectorError("岗位链接不能指向本机或私有网络")


def _known_ats(url: str) -> tuple[ATSKind, str, str] | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        if len(parts) >= 3 and parts[-2] == "jobs":
            return ATSKind.GREENHOUSE, parts[0], parts[-1]
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        return ATSKind.LEVER, parts[0], parts[1]
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return ATSKind.ASHBY, parts[0], parts[1]
    return None


async def _fetch_known(ats: ATSKind, board: str, source_id: str) -> RawJob:
    source = {
        ATSKind.GREENHOUSE: GreenhouseSource,
        ATSKind.LEVER: LeverSource,
        ATSKind.ASHBY: AshbySource,
    }[ats](board)
    try:
        jobs = await source.fetch()
    finally:
        await source.close()
    for job in jobs:
        if job.source_id == source_id or source_id in str(job.url):
            return job
    raise CollectorError("公开 ATS 接口中找不到这个岗位，可能已经关闭")


def _iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_json_objects(graph)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("value"))
    if isinstance(value, list):
        return " · ".join(filter(None, (_text(item) for item in value)))
    return ""


def _location(value: Any) -> str:
    locations = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in locations:
        if not isinstance(item, dict):
            continue
        address = item.get("address") or {}
        if isinstance(address, dict):
            name = ", ".join(
                filter(
                    None,
                    (
                        _text(address.get("addressLocality")),
                        _text(address.get("addressRegion")),
                        _text(address.get("addressCountry")),
                    ),
                )
            )
        else:
            name = _text(address)
        if name:
            names.append(name)
    return " · ".join(dict.fromkeys(names))


def _split_title_segments(text: str) -> list[str]:
    return [
        segment
        for segment in re.split(r"\s+[|·–—-]\s+", text.strip())
        if segment
    ]


def _visible_text(chunks: list[str]) -> str:
    return "\n".join(
        line.strip() for line in "".join(chunks).split("\n") if line.strip()
    )


def _hostname_stem(url: str) -> str:
    labels = (urlsplit(url).hostname or "").casefold().split(".")
    while len(labels) > 1 and labels[0] in _GENERIC_HOST_LABELS:
        labels.pop(0)
    return labels[0]


def _plausible_company(value: str) -> bool:
    token = value.strip().casefold()
    if len(token) < 2 or not any(char.isalnum() for char in token):
        return False
    return token not in _GENERIC_SITE_WORDS


def _fallback_title(parser: _HeuristicJobParser) -> str:
    candidates = (
        parser.meta.get("og:title", ""),
        parser.headings[0] if parser.headings else "",
        parser.title_text,
    )
    for source in candidates:
        text = _TITLE_AT_TAIL_RE.sub("", source).strip()
        segments = _split_title_segments(text)
        if segments and len(segments[0].strip()) >= MIN_TITLE_CHARS:
            return segments[0].strip()
    return ""


def _fallback_company(parser: _HeuristicJobParser, url: str) -> str:
    candidates = [parser.meta.get("og:site_name", "")]
    at_tail = _TITLE_AT_TAIL_RE.search(parser.title_text)
    if at_tail:
        candidates.append(at_tail.group(1).strip().rstrip("."))
    title_segments = _split_title_segments(parser.title_text)
    if len(title_segments) >= 2:
        candidates.append(title_segments[-1])
    candidates.append(_hostname_stem(url))
    for candidate in candidates:
        if _plausible_company(candidate):
            return candidate.strip()
    return ""


def _fallback_employment_type(text: str) -> str | None:
    match = _EMPLOYMENT_RE.search(text)
    if not match:
        return None
    return re.sub(r"[\s-]+", "-", match.group(1).casefold())


def _parse_html_fallback(url: str, html: str) -> RawJob:
    """Heuristic parse for public pages without schema.org JobPosting JSON-LD."""
    parser = _HeuristicJobParser()
    parser.feed(html)
    title = _fallback_title(parser)
    if len(title) < MIN_TITLE_CHARS:
        raise CollectorError("页面缺少可解析的岗位标题")
    description = _visible_text(parser._main_chunks)
    if len(description) < MIN_DESCRIPTION_CHARS:
        description = _visible_text(parser._body_chunks)
    if len(description) < MIN_DESCRIPTION_CHARS:
        raise CollectorError("页面可见正文太短，无法识别岗位描述")
    company = _fallback_company(parser, url)
    if not _plausible_company(company):
        raise CollectorError("无法从页面识别公司名称")
    return RawJob(
        source=ATSKind.UNKNOWN,
        source_id=hashlib.sha256(url.encode()).hexdigest(),
        company=company,
        title=title,
        url=url,
        description=description,
        employment_type=_fallback_employment_type(
            _visible_text(parser._body_chunks)
        ),
        metadata={"schema_org": False},
        raw_snapshot={},
    )


def parse_job_page(url: str, html: str) -> RawJob:
    parser = _JsonLdParser()
    parser.feed(html)
    posting = next(
        (
            item
            for document in parser.documents
            for item in _iter_json_objects(document)
            if _text(item.get("@type")).casefold() == "jobposting"
        ),
        None,
    )
    if posting is None:
        return _parse_html_fallback(url, html)
    title = _text(posting.get("title"))
    description = _text(posting.get("description"))
    company = _text(posting.get("hiringOrganization"))
    if not title or not description or not company:
        raise CollectorError("岗位页面缺少 title、company 或 description")
    identifier = _text(posting.get("identifier"))
    source_id = identifier or hashlib.sha256(url.encode()).hexdigest()
    return RawJob(
        source=ATSKind.UNKNOWN,
        source_id=source_id[:255],
        company=company,
        title=title,
        url=url,
        location=_location(posting.get("jobLocation")),
        description=description,
        employment_type=_text(posting.get("employmentType")) or None,
        metadata={"schema_org": True},
        raw_snapshot={},
    )


async def fetch_job_url(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    host_validator: HostValidator = validate_public_url,
) -> RawJob:
    await host_validator(url)
    known = _known_ats(url)
    if known:
        return await _fetch_known(*known)
    owns_client = client is None
    client = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(30),
        headers={"User-Agent": "job-apply-agent/0.1 (+personal job ingestion)"},
    )
    try:
        response = await client.get(url)
        if response.is_redirect:
            raise CollectorError(
                "通用岗位链接不允许自动跟随跳转，请发送最终 URL"
            )
        response.raise_for_status()
        if len(response.content) > MAX_HTML_BYTES:
            raise CollectorError("岗位页面超过 2 MB，拒绝解析")
        return parse_job_page(url, response.text)
    except httpx.HTTPError as exc:
        raise CollectorError("无法读取岗位页面") from exc
    finally:
        if owns_client:
            await client.aclose()
