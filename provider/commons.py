"""Small, conservative Wikimedia Commons music provider (standard library only).

Search metadata is untrusted.  Only explicitly licensed CC0, CC BY and CC BY-SA
audio with an official Commons MP3 derivative is returned.  The original raw
``metadata`` is retained for provenance; it must never be rendered as HTML.

The time budget is a caller-visible deadline, including DNS and connection setup.
At most one daemon worker uses the Commons API at a time; abandoned requests still
have a socket timeout and cannot create an unbounded number of background workers.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import math
import queue
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://commons.wikimedia.org/w/api.php"
DEFAULT_USER_AGENT = "HomeMusic/1.0 (self-hosted personal music library)"
_MAX_RESPONSE_BYTES = 3 * 1024 * 1024
_REQUEST_SLOT = threading.BoundedSemaphore(1)
_VERSIONS = ("1.0", "2.0", "2.5", "3.0", "4.0")
_ALIASES = {
    "致爱丽丝": "Fur Elise",
    "献给爱丽丝": "Fur Elise",
    "小星星": "Twinkle Twinkle Little Star",
}
_AUDIO_EXTENSION = re.compile(r"\.(?:ogg|oga|opus|wav|wave|mp3|flac|m4a|aac|mid|midi|webm)$", re.I)
_FRAGMENT = re.compile(
    r"\b(?:sample|samples|excerpt|excerpts|fragment|fragments|preview|snippet|"
    r"pronunciation|measures?|demo)\b|片段|节选|试听|发音|演示",
    re.I,
)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "iframe", "object", "noscript"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "iframe", "object", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _plain(value: object, limit: int = 2000) -> str:
    """Remove markup, hidden content, control characters and residual angle brackets."""
    if not isinstance(value, str):
        return ""
    parser = _TextOnly()
    parser.feed(html.unescape(value[:200_000]))
    parser.close()
    text = "".join(parser.parts)
    text = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch not in "<>" and (ch.isspace() or not unicodedata.category(ch).startswith("C"))
    )
    return " ".join(text.split())[:limit]


def canonical_query(keyword: str) -> str:
    """Normalize only explicit voice prefixes and a tiny, unambiguous work dictionary.

    This is not a translation model.  For example, 晴天 stays 晴天, and 周杰伦的
    小星星 is not silently replaced by the traditional English nursery rhyme.
    """
    if not isinstance(keyword, str):
        raise ValueError("歌名必须是文字")
    query = unicodedata.normalize("NFKC", _plain(keyword, 240)).strip()
    query = re.sub(r"^小爱同学[，,、:：\s]+", "", query)
    for prefix in ("请帮我播放", "帮我播放", "请播放", "播放"):
        if query.startswith(prefix) and len(query) > len(prefix):
            query = query[len(prefix):].strip()
            break
    if len(query) >= 2 and (query[0], query[-1]) in {("《", "》"), ("「", "」"), ("“", "”")}:
        query = query[1:-1].strip()
    if not query or len(query) > 120:
        raise ValueError("请提供 1 到 120 个字的歌名")
    return _ALIASES.get(query, query)


def _safe_url(value: object, host: str, *, allow_http: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 5000:
        raise ValueError("缺少有效的来源网址")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value) or "\\" in value:
        raise ValueError("来源网址含有非法字符")
    try:
        parsed = urlsplit(value)
        allowed_schemes = {"https", "http"} if allow_http else {"https"}
        if (
            parsed.scheme not in allowed_schemes
            or parsed.hostname != host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError("来源网址不在允许的官方域名内")
    except (TypeError, ValueError) as exc:
        raise ValueError("来源网址不在允许的官方域名内") from exc
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def _value(metadata: dict, key: str) -> str:
    field = metadata.get(key, {})
    return _plain(field.get("value", "")) if isinstance(field, dict) else ""


def _boolish(value: str, field: str) -> bool | None:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise ValueError(f"许可字段 {field} 无法确认")


def _terms_key(value: str) -> str:
    return " ".join(re.sub(r"[-_,–—]", " ", value.casefold()).split())


def _license(metadata: dict) -> tuple[str, str]:
    if _boolish(_value(metadata, "NonFree"), "NonFree"):
        raise ValueError("该录音不属于明确的开放许可作品")
    if _value(metadata, "Restrictions") or _value(metadata, "DeletionReason"):
        raise ValueError("该录音存在额外限制或待删除标记，需要人工核查")

    name = _value(metadata, "LicenseShortName")
    raw_url = _safe_url(_value(metadata, "LicenseUrl"), "creativecommons.org", allow_http=True)
    parsed = urlsplit(raw_url)
    if parsed.query:
        raise ValueError("许可链接不是标准的 Creative Commons 许可地址")
    # Official Commons CC0 metadata commonly uses http and /deed.en.  Normalize
    # those document variants without trusting any other host or arbitrary path.
    path = re.sub(r"/(?:deed(?:\.[A-Za-z-]+)?|legalcode)/?$", "", parsed.path).rstrip("/")
    name_upper = name.upper()
    terms = _value(metadata, "UsageTerms")
    copyrighted = _boolish(_value(metadata, "Copyrighted"), "Copyrighted")
    attribution_required = _boolish(_value(metadata, "AttributionRequired"), "AttributionRequired")

    if name_upper in {"CC0", "CC0 1.0", "CC0 1.0 UNIVERSAL"}:
        if path != "/publicdomain/zero/1.0":
            raise ValueError("CC0 许可名称与链接不一致")
        allowed_terms = {
            "cc0", "cc0 1.0", "cc0 1.0 universal",
            "creative commons zero public domain dedication",
            "cc0 1.0 universal public domain dedication",
        }
        if terms and _terms_key(terms) not in allowed_terms:
            raise ValueError("CC0 的使用条款与许可名称不一致")
        # CC0 metadata can legitimately report Copyrighted=True; CC0 includes a
        # waiver plus a fallback license.  Never infer public-domain status here.
        return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"

    match = re.fullmatch(r"CC (BY(?:-SA)?) (1\.0|2\.0|2\.5|3\.0|4\.0)", name_upper)
    if not match:
        raise ValueError("仅自动收录许可清晰的 CC0、CC BY 和 CC BY-SA 录音")
    family, version = match.groups()
    if version not in _VERSIONS or path != f"/licenses/{family.lower()}/{version}":
        raise ValueError("Creative Commons 许可名称与链接不一致")
    if copyrighted is False or attribution_required is False:
        raise ValueError("署名许可元数据互相矛盾，需要人工核查")
    canonical_terms = (
        f"creative commons attribution share alike {version}"
        if family == "BY-SA" else f"creative commons attribution {version}"
    )
    allowed_terms = {_terms_key(name), canonical_terms, canonical_terms + " international"}
    if terms and _terms_key(terms) not in allowed_terms:
        raise ValueError("使用条款与许可名称不一致，需要人工核查")
    return f"CC {family} {version}", f"https://creativecommons.org/licenses/{family.lower()}/{version}/"


def _track(page: dict) -> dict:
    page_id = page.get("pageid")
    if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id <= 0 or page.get("ns") != 6:
        raise ValueError("Commons 文件标识无效")
    info_list = page.get("videoinfo")
    if not isinstance(info_list, list) or not info_list or not isinstance(info_list[0], dict):
        raise ValueError("该文件没有音频元数据")
    info = info_list[0]
    if info.get("mediatype") != "AUDIO":
        raise ValueError("搜索结果不是音频")
    mime = str(info.get("mime", ""))
    if not (mime.startswith("audio/") or mime == "application/ogg"):
        raise ValueError("音频 MIME 类型无法确认")
    raw_duration = info.get("duration")
    if isinstance(raw_duration, bool):
        raise ValueError("音频时长无效")
    try:
        duration = float(raw_duration)
    except (ValueError, TypeError) as exc:
        raise ValueError("音频时长无效") from exc
    if not math.isfinite(duration) or not 20.0 <= duration <= 1800.0:
        raise ValueError("仅自动收录 20 秒至 30 分钟的录音")
    title = _AUDIO_EXTENSION.sub("", re.sub(r"^File:", "", _plain(page.get("title"), 300), flags=re.I)).strip()
    if not title or _FRAGMENT.search(title):
        raise ValueError("该结果看起来是片段、示例或发音文件")
    metadata = info.get("extmetadata")
    if not isinstance(metadata, dict):
        raise ValueError("缺少可核查的许可元数据")
    license_name, license_url = _license(metadata)

    download_url = ""
    derivatives = info.get("derivatives", [])
    if not isinstance(derivatives, list):
        raise ValueError("官方音频转码列表格式无效")
    for derivative in derivatives:
        if not isinstance(derivative, dict):
            continue
        content_type = derivative.get("type", "")
        if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().lower() != "audio/mpeg":
            continue
        try:
            candidate = _safe_url(derivative.get("src"), "upload.wikimedia.org")
        except ValueError:
            continue
        path = urlsplit(candidate).path
        if path.startswith("/wikipedia/commons/transcoded/") and path.lower().endswith(".mp3"):
            download_url = candidate
            break
    if not download_url:
        raise ValueError("该录音没有可直接播放的官方 MP3 转码文件")

    # A stable page-id URL cannot be manipulated by HTML or title metadata.
    raw_source = info.get("descriptionurl")
    if raw_source:
        source = _safe_url(raw_source, "commons.wikimedia.org")
        if not (urlsplit(source).path.startswith("/wiki/File:") or urlsplit(source).path == "/w/index.php"):
            raise ValueError("录音详情页地址无效")
    source_url = f"https://commons.wikimedia.org/w/index.php?curid={page_id}"
    artist = _value(metadata, "Artist")[:500]
    uploader = _plain(info.get("user"), 300)
    credit = _value(metadata, "Credit")
    custom_attribution = _value(metadata, "Attribution")
    # Artist sometimes identifies only the composer.  Preserve uploader and
    # credit as well; never represent the composer as the recording performer.
    attribution_parts = [custom_attribution or artist]
    if uploader and uploader not in attribution_parts:
        attribution_parts.append(f"上传者：{uploader}")
    if credit:
        attribution_parts.append(f"来源说明：{credit}")
    attribution_parts.extend((license_name, license_url, source_url))
    track = {
        "id": f"commons-{page_id}",
        "page_id": page_id,
        "title": title,
        "artist": artist or uploader or "作者见来源页面",
        "album": "Wikimedia Commons",
        "duration": duration,
        "source_url": source_url,
        "download_url": download_url,
        "license_name": license_name,
        "license_url": license_url,
        "attribution": "；".join(x for x in attribution_parts if x),
        "metadata": metadata,
    }
    if info.get("url"):
        try:
            original = _safe_url(info["url"], "upload.wikimedia.org")
            if urlsplit(original).path.startswith("/wikipedia/commons/"):
                track["url"] = original
        except ValueError:
            pass
    return track


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Commons API 意外重定向，已停止请求", headers, fp)


class CommonsClient:
    def __init__(self, timeout: float = 12.0, user_agent: str = DEFAULT_USER_AGENT):
        try:
            requested_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("网络超时必须是正数") from exc
        if not math.isfinite(requested_timeout) or requested_timeout <= 0:
            raise ValueError("网络超时必须是正数")
        self.timeout = min(requested_timeout, 30.0)
        if not isinstance(user_agent, str) or not user_agent.strip() or any(ch in user_agent for ch in "\r\n"):
            raise ValueError("请设置有效的 User-Agent，建议包含真实的维护者联系方式")
        self.user_agent = user_agent
        self._opener = build_opener(_NoRedirect())

    def _request(self, params: dict) -> dict:
        deadline = time.monotonic() + self.timeout
        if not _REQUEST_SLOT.acquire(timeout=self.timeout):
            raise TimeoutError("Commons 请求繁忙，本次点歌已超时，请稍后重试")
        result: queue.Queue = queue.Queue(maxsize=1)

        def fetch() -> None:
            try:
                base_params = {"action": "query", "format": "json", "formatversion": 2, "maxlag": 5}
                base_params.update(params)
                request = Request(API_URL + "?" + urlencode(base_params), headers={
                    "User-Agent": self.user_agent, "Accept": "application/json",
                })
                with self._opener.open(request, timeout=self.timeout) as response:
                    content = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("Commons 返回的元数据过大，已停止读取")
                payload = json.loads(content.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Commons 返回的 JSON 格式无效")
                if payload.get("error"):
                    error = payload["error"]
                    code = _plain(error.get("code", "unknown"), 80) if isinstance(error, dict) else "unknown"
                    raise RuntimeError(f"Commons 暂时无法提供结果（{code}），请稍后重试")
                result.put((True, payload))
            except (HTTPError, URLError) as exc:
                result.put((False, RuntimeError(f"Commons 网络请求失败：{_plain(str(exc), 200)}")))
            except Exception as exc:
                result.put((False, exc))
            finally:
                _REQUEST_SLOT.release()

        worker = threading.Thread(target=fetch, name="commons-api", daemon=True)
        try:
            worker.start()
        except Exception:
            _REQUEST_SLOT.release()
            raise
        try:
            succeeded, payload = result.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError("Commons 搜索超时，请稍后重试或播放本地曲库") from exc
        if not succeeded:
            raise payload
        return payload

    @staticmethod
    def _metadata_params() -> dict:
        return {
            "prop": "videoinfo",
            "viprop": "url|size|mime|mediatype|extmetadata|derivatives|user",
            "viextmetadatalanguage": "en",
        }

    def search(self, keyword: str, limit: int = 6) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("搜索数量须为 1 至 20")
        query = canonical_query(keyword)
        # Prevent user text from inserting CirrusSearch operators.  Keep words,
        # CJK text and ordinary title punctuation; our media filter stays fixed.
        search_words = " ".join(re.sub(r'[":|<>\\{}\[\]]', " ", query).split())
        params = self._metadata_params()
        params.update({
            "generator": "search", "gsrsearch": f"{search_words} filetype:audio",
            "gsrnamespace": 6, "gsrlimit": min(20, max(8, limit * 2)),
        })
        payload = self._request(params)
        query_result = payload.get("query", {})
        if not isinstance(query_result, dict):
            raise ValueError("Commons 搜索结果格式无效")
        pages = query_result.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("Commons 搜索结果格式无效")
        def rank(page: dict) -> int:
            value = page.get("index", 1_000_000)
            return value if isinstance(value, int) else 1_000_000
        tracks: list[dict] = []
        seen: set[int] = set()
        for page in sorted((p for p in pages if isinstance(p, dict)), key=rank):
            try:
                track = _track(page)
            except (ValueError, TypeError, AttributeError):
                continue
            if track["page_id"] in seen:
                continue
            track["normalized_query"] = query
            tracks.append(track)
            seen.add(track["page_id"])
            if len(tracks) == limit:
                break
        return tracks

    def get(self, page_id: int) -> dict:
        if isinstance(page_id, str) and re.fullmatch(r"[1-9]\d{0,11}", page_id):
            page_id = int(page_id)
        if not isinstance(page_id, int) or isinstance(page_id, bool) or not 0 < page_id < 10**12:
            raise ValueError("Commons page_id 必须是正整数")
        params = self._metadata_params()
        params["pageids"] = page_id
        payload = self._request(params)
        query_result = payload.get("query", {})
        if not isinstance(query_result, dict):
            raise ValueError("Commons 文件结果格式无效")
        pages = query_result.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("Commons 文件结果格式无效")
        for page in pages:
            if isinstance(page, dict) and page.get("pageid") == page_id:
                return _track(page)
        raise ValueError("指定的 Commons 录音不存在或已经移除")
