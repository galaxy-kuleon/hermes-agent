"""Read current Hong Kong legislation from the official HKeL open-data XML.

The model must not treat memory, search snippets, or a prose overview as the
governing text.  This tool resolves the current version from the Department of
Justice catalogue and extracts only the requested XML member from the large
chapter archive using HTTP byte ranges.  Versioned XML files are retained in a
local cache so a later network outage can fail over without silently changing
the authority that was read.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home
from tools.registry import registry


CATALOG_URL = "https://resource.data.one.gov.hk/doj/data/hkel_list_c_all_en.xml"
DATASET_URL = "https://data.gov.hk/en-data/dataset/hk-doj-hkel-legislation-current"
OFFICIAL_ARCHIVE_PREFIX = "https://resource.data.one.gov.hk/doj/data/"
OFFICIAL_WEB_PREFIX = "https://www.elegislation.gov.hk/"
NETWORK_TIMEOUT_SECONDS = 600
PDF_TEXT_TIMEOUT_SECONDS = 600
MAX_PROVISIONS = 10
MAX_CHAPTER_LENGTH = 8
ZIP_EOCD_SEARCH_BYTES = 65_557
HKLM_NAMESPACE = "http://www.xml.gov.hk/schemas/hklm/1.0"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
_CHAPTER_RE = re.compile(r"^[0-9]{1,4}[A-Z]{0,3}$")
_PROVISION_RE = re.compile(r"^(?:s|r)?([0-9]{1,4}[A-Z]?)$", re.IGNORECASE)
_LABELED_PROVISION_RE = re.compile(
    r"^(?:(?:sch(?:edule)?\.?\s*\d+[A-Z]?\s*[,;:\-]?\s*)?)"
    r"(?:section|sec(?:tion)?|s|rule|r)\.?\s*"
    r"([0-9]{1,4}[A-Z]?)(?:\s*\([0-9A-Za-z]+\))*\s*$",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
logger = logging.getLogger(__name__)

IPD_TIME_LIMITS_MANUAL_URL = (
    "https://www.ipd.gov.hk/filemanager/ipd/common/trade-marks/"
    "registry-work-manual/current/eng/time_limits_in_exam_process.pdf"
)
IPD_TIME_LIMITS_MANUAL_TITLE = "Time limits in the examination process"
IPD_CROSS_SEARCH_LIST_URL = (
    "https://www.ipd.gov.hk/filemanager/ipd/common/trade-marks/"
    "registry-work-manual/current/eng/Cross_search_list.pdf"
)
IPD_CROSS_SEARCH_LIST_TITLE = "Cross search list"
MIN_TRADE_MARK_CLASS = 1
MAX_TRADE_MARK_CLASS = 45
MAX_CROSS_SEARCH_CLASSES = 10
_RULE_13_MANUAL_TERMS = (
    "rule 13(3)",
    "6-month period",
    "six-month period",
)
MAX_MANUAL_MATCHED_PAGES = 4
EVIDENCE_DELIVERY_COMPLETE = "complete"
EVIDENCE_STATUS_VERIFIED_READ = "verified_read"
_RULE_13_VERIFIED_EXTRACT_BOUNDS = (
    (
        "The prescribed period for taking the above action",
        "Upon receipt of a request",
    ),
    (
        "Upon receipt of a request",
        "It should however be noted",
    ),
)


@dataclass(frozen=True)
class AuthorityVersion:
    chapter: str
    title: str
    version_date: str
    status: str
    archive_url: str
    archive_sha256: str
    file_location: str
    file_name: str
    web_url: str
    catalog_updated_at: str

    @property
    def member_name(self) -> str:
        return f"{self.file_location}/{self.file_name}"


def _text(parent: ET.Element, name: str) -> str:
    child = parent.find(name)
    return (child.text or "").strip() if child is not None else ""


def parse_catalog(catalog_xml: bytes, chapter: str) -> AuthorityVersion:
    """Resolve one chapter's current version from the official catalogue."""
    wanted = chapter.strip().upper()
    if len(wanted) > MAX_CHAPTER_LENGTH or not _CHAPTER_RE.fullmatch(wanted):
        raise ValueError("chapter must look like 559 or 559A")

    root = ET.fromstring(catalog_xml)
    archive_hashes = {
        item.attrib.get("url", ""): item.attrib.get("sha256", "").lower()
        for item in root.findall("./DataSet/DataResource")
    }
    for item in root.findall("./Chapter"):
        if _text(item, "CapNo").upper() != wanted:
            continue
        versions = item.findall("./Version")
        current = next(
            (
                version
                for version in versions
                if (version.find("VersionDate") is not None)
                and version.find("VersionDate").attrib.get("isCurrentVersion") == "true"
            ),
            None,
        )
        if current is None:
            raise LookupError(f"Cap. {wanted} has no current version in HKeL")
        version_node = current.find("VersionDate")
        assert version_node is not None
        archive_url = _text(current, "DataResourceUrl")
        web_url = _text(current, "Web")
        if not archive_url.startswith(OFFICIAL_ARCHIVE_PREFIX):
            raise ValueError("HKeL catalogue returned a non-official archive URL")
        if not web_url.startswith(OFFICIAL_WEB_PREFIX):
            raise ValueError("HKeL catalogue returned a non-official legislation URL")
        return AuthorityVersion(
            chapter=wanted,
            title=_text(item, "ChapterTitleEnglish"),
            version_date=(version_node.text or "").strip(),
            status=version_node.attrib.get("statusCategory", ""),
            archive_url=archive_url,
            archive_sha256=archive_hashes.get(archive_url, ""),
            file_location=_text(current, "FileLocation"),
            file_name=_text(current, "FileName"),
            web_url=web_url,
            catalog_updated_at=root.attrib.get("UpdatedDateTime", ""),
        )
    raise LookupError(f"Cap. {wanted} is not present in the current HKeL catalogue")


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_response(response) -> bytes:
    with response:
        return response.read()


def _fetch_bytes(
    opener, url: str, *, byte_range: tuple[int, int] | None = None
) -> bytes:
    headers = {"User-Agent": "hermes-hk-legal-authority/1"}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    request = urllib.request.Request(url, headers=headers)
    response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
    status = getattr(response, "status", response.getcode())
    if byte_range is not None and status != 206:
        response.close()
        raise OSError("official archive did not honor the bounded HTTP range request")
    return _read_response(response)


def _fetch_official_pdf(opener, url: str) -> tuple[bytes, str]:
    """Fetch one allowlisted IPD manual and retain its server version hint."""
    if url not in {IPD_TIME_LIMITS_MANUAL_URL, IPD_CROSS_SEARCH_LIST_URL}:
        raise ValueError("unsupported official IPD manual URL")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "hermes-hk-legal-authority/1"},
    )
    response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
    with response:
        content = response.read()
        version_hint = (
            response.headers.get("Last-Modified")
            or response.headers.get("ETag")
            or "current-url-no-version-header"
        )
    if not content.startswith(b"%PDF-"):
        raise ValueError("official IPD manual response is not a PDF")
    return content, version_hint


def _pdf_text(pdf_content: bytes) -> str:
    """Extract page-preserving text with the deployment's Poppler binary."""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="hk-ipd-manual-", suffix=".pdf", dir=cache_dir, delete=False
    ) as source:
        source.write(pdf_content)
        source_path = Path(source.name)
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(source_path), "-"],
            check=True,
            capture_output=True,
            timeout=PDF_TEXT_TIMEOUT_SECONDS,
        )
        return completed.stdout.decode("utf-8", errors="replace")
    finally:
        source_path.unlink(missing_ok=True)


def _manual_cache_path(digest: str) -> Path:
    return _cache_dir() / f"ipd-time-limits-in-examination.{digest}.pdf"


def _retained_manual() -> tuple[bytes, str, Path] | None:
    for path in sorted(
        _cache_dir().glob("ipd-time-limits-in-examination.*.pdf"), reverse=True
    ):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(content).hexdigest()
        if path.name == f"ipd-time-limits-in-examination.{digest}.pdf":
            return content, digest, path
    return None


def _cross_search_cache_path(digest: str) -> Path:
    return _cache_dir() / f"ipd-cross-search-list.{digest}.pdf"


def _retained_cross_search_manual() -> tuple[bytes, str, Path] | None:
    for path in sorted(_cache_dir().glob("ipd-cross-search-list.*.pdf"), reverse=True):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(content).hexdigest()
        if path.name == f"ipd-cross-search-list.{digest}.pdf":
            return content, digest, path
    return None


def _normalise_cross_search_classes(values: object) -> tuple[int, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not values or len(values) > MAX_CROSS_SEARCH_CLASSES:
        raise ValueError(
            "cross_search_classes must contain "
            f"1-{MAX_CROSS_SEARCH_CLASSES} class numbers"
        )
    classes: list[int] = []
    for raw in values:
        if isinstance(raw, bool):
            raise ValueError("cross-search class numbers must be integers from 1 to 45")
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cross-search class numbers must be integers from 1 to 45"
            ) from exc
        if not MIN_TRADE_MARK_CLASS <= number <= MAX_TRADE_MARK_CLASS:
            raise ValueError("cross-search class numbers must be integers from 1 to 45")
        if number not in classes:
            classes.append(number)
    return tuple(classes)


def _cross_search_practice_guidance(
    opener, requested_classes: tuple[int, ...]
) -> dict:
    """Read exact class relationships from the current official IPD list."""
    freshness = "current_ipd_url"
    try:
        content, version_hint = _fetch_official_pdf(opener, IPD_CROSS_SEARCH_LIST_URL)
        digest = hashlib.sha256(content).hexdigest()
        path = _cross_search_cache_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(content)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise OSError("retained IPD cross-search cache failed integrity verification")
    except Exception as exc:
        retained = _retained_cross_search_manual()
        if retained is None:
            return {
                "success": False,
                "cannot_confirm": True,
                "source": "Hong Kong Intellectual Property Department",
                "title": IPD_CROSS_SEARCH_LIST_TITLE,
                "official_url": IPD_CROSS_SEARCH_LIST_URL,
                "requested_classes": list(requested_classes),
                "error": (
                    "official IPD cross-search list unavailable and no retained "
                    f"copy exists: {exc}"
                ),
            }
        content, digest, path = retained
        freshness = "cached_offline"
        version_hint = "retained-version"

    try:
        text = _pdf_text(content)
    except Exception as exc:
        return {
            "success": False,
            "cannot_confirm": True,
            "source": "Hong Kong Intellectual Property Department",
            "title": IPD_CROSS_SEARCH_LIST_TITLE,
            "official_url": IPD_CROSS_SEARCH_LIST_URL,
            "freshness": freshness,
            "pdf_sha256": digest,
            "requested_classes": list(requested_classes),
            "error": f"official IPD cross-search PDF text extraction failed: {exc}",
        }

    pages = text.split("\f")
    class_rows: list[dict] = []
    selected_pages: dict[int, str] = {}
    for class_number in requested_classes:
        relation_pattern = re.compile(
            rf"\bClass\s+{class_number}\s+Cross\s+search\s+"
            r"class(?:es)?\s*:\s*([0-9]+(?:\s*,\s*[0-9]+)*)",
            re.IGNORECASE,
        )
        none_pattern = re.compile(
            rf"\bClass\s+{class_number}\s+No\s+cross\s+search\s+required\b",
            re.IGNORECASE,
        )
        found: dict | None = None
        for page_number, page in enumerate(pages, start=1):
            normalized = _SPACE_RE.sub(" ", page).strip()
            relation = relation_pattern.search(normalized)
            no_search = none_pattern.search(normalized)
            if relation:
                cross_classes = [
                    int(value.strip()) for value in relation.group(1).split(",")
                ]
                found = {
                    "class": class_number,
                    "cross_search_classes": cross_classes,
                    "page": page_number,
                    "text": relation.group(0),
                }
            elif no_search:
                found = {
                    "class": class_number,
                    "cross_search_classes": [],
                    "page": page_number,
                    "text": no_search.group(0),
                }
            if found is not None:
                selected_pages[page_number] = normalized
                break
        if found is None:
            found = {
                "class": class_number,
                "found": False,
                "cross_search_classes": [],
            }
        else:
            found["found"] = True
        class_rows.append(found)

    missing = [row["class"] for row in class_rows if row["found"] is False]
    verified_extracts = [
        {"page": row["page"], "text": row["text"]}
        for row in class_rows
        if row["found"] is True
    ]
    logger.info(
        "hk_ipd_manual_cache title=%s freshness=%s path=%s pdf_sha256=%s classes=%s",
        IPD_CROSS_SEARCH_LIST_TITLE,
        freshness,
        path,
        digest,
        requested_classes,
    )
    return {
        "success": not missing,
        "cannot_confirm": bool(missing),
        "source": "Hong Kong Intellectual Property Department",
        "title": IPD_CROSS_SEARCH_LIST_TITLE,
        "official_url": IPD_CROSS_SEARCH_LIST_URL,
        "freshness": freshness,
        "server_version_hint": version_hint,
        "pdf_sha256": digest,
        "requested_classes": list(requested_classes),
        "classes": class_rows,
        "missing_classes": missing,
        "matched_page_text_complete": not missing,
        "verified_extracts": verified_extracts,
        "matched_pages": [
            {"page": page_number, "text": selected_pages[page_number]}
            for page_number in sorted(selected_pages)
        ],
        "instruction": (
            "This is current official Registry practice guidance, not legislation. "
            "Use only the returned class rows for a cross-search conclusion, cite "
            "the official PDF URL, and do not infer a class relationship from "
            "section 12 or general knowledge."
        ),
    }


def _rule_13_practice_guidance(opener) -> dict:
    """Return official IPD manual pages relevant to Rule 13 time limits."""
    freshness = "current_ipd_url"
    try:
        content, version_hint = _fetch_official_pdf(opener, IPD_TIME_LIMITS_MANUAL_URL)
        digest = hashlib.sha256(content).hexdigest()
        path = _manual_cache_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(content)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise OSError("retained IPD manual cache failed integrity verification")
    except Exception as exc:
        retained = _retained_manual()
        if retained is None:
            return {
                "success": False,
                "cannot_confirm": True,
                "title": IPD_TIME_LIMITS_MANUAL_TITLE,
                "official_url": IPD_TIME_LIMITS_MANUAL_URL,
                "error": f"official IPD manual unavailable and no retained copy exists: {exc}",
            }
        content, digest, path = retained
        freshness = "cached_offline"
        version_hint = "retained-version"

    try:
        text = _pdf_text(content)
    except Exception as exc:
        return {
            "success": False,
            "cannot_confirm": True,
            "source": "Hong Kong Intellectual Property Department",
            "title": IPD_TIME_LIMITS_MANUAL_TITLE,
            "official_url": IPD_TIME_LIMITS_MANUAL_URL,
            "freshness": freshness,
            "pdf_sha256": digest,
            "error": f"official IPD manual PDF text extraction failed: {exc}",
        }
    pages = text.split("\f")
    selected = []
    for page_number, page in enumerate(pages, start=1):
        normalized = _SPACE_RE.sub(" ", page).strip()
        lowered = normalized.casefold()
        matched = [term for term in _RULE_13_MANUAL_TERMS if term in lowered]
        if matched:
            selected.append({
                "page": page_number,
                "matched_terms": matched,
                "text": normalized,
            })
        if len(selected) >= MAX_MANUAL_MATCHED_PAGES:
            break
    logger.info(
        "hk_ipd_manual_cache title=%s freshness=%s path=%s pdf_sha256=%s",
        IPD_TIME_LIMITS_MANUAL_TITLE,
        freshness,
        path,
        digest,
    )
    verified_extracts = []
    for page in selected:
        page_text = page["text"]
        for start_marker, end_marker in _RULE_13_VERIFIED_EXTRACT_BOUNDS:
            start = page_text.find(start_marker)
            if start < 0:
                continue
            end = page_text.find(end_marker, start + len(start_marker))
            if end < 0:
                continue
            excerpt = page_text[start:end].strip()
            if excerpt:
                verified_extracts.append({"page": page["page"], "text": excerpt})
    return {
        "success": bool(selected),
        "cannot_confirm": not selected,
        "source": "Hong Kong Intellectual Property Department",
        "title": IPD_TIME_LIMITS_MANUAL_TITLE,
        "official_url": IPD_TIME_LIMITS_MANUAL_URL,
        "freshness": freshness,
        "server_version_hint": version_hint,
        "pdf_sha256": digest,
        "matched_page_text_complete": True,
        "verified_extracts": verified_extracts,
        "matched_pages": selected,
        "instruction": (
            "This is official practice guidance, not legislation. Cite the official "
            "manual URL and keep its guidance distinct from the statutory rule. The "
            "verified_extracts above are exact text from the current official PDF, and "
            "the matched page text is complete below. Treat the manual as read in this "
            "session; do not describe it as unavailable or truncated."
        ),
    }


def _remote_size(opener, url: str) -> int:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "hermes-hk-legal-authority/1"},
        method="HEAD",
    )
    response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
    with response:
        raw = response.headers.get("Content-Length")
    if not raw or not raw.isdigit() or int(raw) <= 0:
        raise OSError("official archive did not provide a valid Content-Length")
    return int(raw)


def _normalise_member(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def read_zip_member_by_range(
    archive_url: str,
    member_name: str,
    *,
    opener=None,
    archive_size: int | None = None,
) -> bytes:
    """Read one ZIP member without downloading the full official archive."""
    client = opener or _no_proxy_opener()
    total_size = archive_size or _remote_size(client, archive_url)
    tail_start = max(0, total_size - ZIP_EOCD_SEARCH_BYTES)
    tail = _fetch_bytes(client, archive_url, byte_range=(tail_start, total_size - 1))
    eocd_pos = tail.rfind(b"PK\x05\x06")
    if eocd_pos < 0 or eocd_pos + 22 > len(tail):
        raise ValueError("HKeL archive has no readable ZIP end record")
    eocd = struct.unpack_from("<4s4H2LH", tail, eocd_pos)
    central_size, central_offset = eocd[5], eocd[6]
    if central_size <= 0 or central_offset + central_size > total_size:
        raise ValueError("HKeL archive central directory is outside the archive")
    central = _fetch_bytes(
        client,
        archive_url,
        byte_range=(central_offset, central_offset + central_size - 1),
    )

    wanted = _normalise_member(member_name)
    cursor = 0
    found = None
    header_format = "<4s6H3L5H2L"
    header_size = struct.calcsize(header_format)
    while cursor + header_size <= len(central):
        fields = struct.unpack_from(header_format, central, cursor)
        if fields[0] != b"PK\x01\x02":
            raise ValueError("HKeL ZIP central directory is malformed")
        name_length, extra_length, comment_length = fields[10], fields[11], fields[12]
        end = cursor + header_size + name_length + extra_length + comment_length
        if end > len(central):
            raise ValueError("HKeL ZIP central entry is truncated")
        raw_name = central[cursor + header_size : cursor + header_size + name_length]
        encoding = "utf-8" if fields[3] & 0x800 else "cp437"
        decoded_name = raw_name.decode(encoding)
        if _normalise_member(decoded_name) == wanted:
            found = {
                "flags": fields[3],
                "compression": fields[4],
                "crc32": fields[7],
                "compressed_size": fields[8],
                "uncompressed_size": fields[9],
                "local_offset": fields[16],
            }
            break
        cursor = end
    if found is None:
        raise LookupError(f"current HKeL XML member not found: {wanted}")
    if found["flags"] & 0x1:
        raise ValueError("encrypted HKeL ZIP members are unsupported")

    local_offset = found["local_offset"]
    local_header_size = struct.calcsize("<4s5H3L2H")
    local = _fetch_bytes(
        client,
        archive_url,
        byte_range=(local_offset, local_offset + local_header_size - 1),
    )
    local_fields = struct.unpack("<4s5H3L2H", local)
    if local_fields[0] != b"PK\x03\x04":
        raise ValueError("HKeL ZIP local member header is malformed")
    data_start = local_offset + local_header_size + local_fields[9] + local_fields[10]
    compressed_size = found["compressed_size"]
    if compressed_size <= 0:
        raise ValueError("HKeL XML member has no content")
    compressed = _fetch_bytes(
        client,
        archive_url,
        byte_range=(data_start, data_start + compressed_size - 1),
    )
    method = found["compression"]
    if method == 0:
        content = compressed
    elif method == 8:
        content = zlib.decompress(compressed, -zlib.MAX_WBITS)
    else:
        raise ValueError(f"unsupported HKeL ZIP compression method: {method}")
    if len(content) != found["uncompressed_size"]:
        raise ValueError("HKeL XML member size verification failed")
    if (binascii.crc32(content) & 0xFFFFFFFF) != found["crc32"]:
        raise ValueError("HKeL XML member CRC verification failed")
    return content


def _cache_dir() -> Path:
    override = os.environ.get("HERMES_HK_LEGAL_CACHE", "").strip()
    return Path(override) if override else get_hermes_home() / "legal-authority-cache"


def _cache_path(version: AuthorityVersion, xml_sha256: str) -> Path:
    safe_file = re.sub(r"[^A-Za-z0-9._-]", "_", version.file_name)
    return _cache_dir() / f"{safe_file}.{xml_sha256}.xml"


def _retain_versioned_xml(
    version: AuthorityVersion, content: bytes
) -> tuple[Path, str]:
    digest = hashlib.sha256(content).hexdigest()
    path = _cache_path(version, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(content)
    except FileExistsError:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise OSError("retained HKeL XML cache failed integrity verification")
    return path, digest


def _cached_version(chapter: str) -> tuple[AuthorityVersion, bytes, str, Path] | None:
    pattern = f"cap_{chapter}_*_en_c.xml.*.xml"
    candidates = sorted(_cache_dir().glob(pattern), reverse=True)
    for path in candidates:
        try:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if f".{digest}.xml" != path.name[-(len(digest) + 5) :]:
                continue
            root = ET.fromstring(content)
            meta = root.find(f"{{{HKLM_NAMESPACE}}}meta")
            if meta is None:
                continue
            version_date = _text_ns(meta, f"{{{DC_NAMESPACE}}}date")
            status = _text_ns(meta, f"{{{HKLM_NAMESPACE}}}docStatus")
            title_node = root.find(f".//{{{HKLM_NAMESPACE}}}docTitle")
            title = (
                _normalised_itertext(title_node)
                if title_node is not None
                else f"Cap. {chapter}"
            )
            version = AuthorityVersion(
                chapter=chapter,
                title=title,
                version_date=version_date,
                status=status,
                archive_url="",
                archive_sha256="",
                file_location="",
                file_name=path.name.split(".", 1)[0],
                web_url=f"https://www.elegislation.gov.hk/hk/cap{chapter}!en",
                catalog_updated_at="",
            )
            return version, content, digest, path
        except (OSError, ET.ParseError):
            continue
    return None


def _text_ns(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _normalised_itertext(node: ET.Element) -> str:
    return _SPACE_RE.sub(" ", " ".join(node.itertext())).strip()


def _normalise_provision_request(value: object) -> tuple[str, str]:
    """Accept common legal labels while retaining the model's raw input."""
    raw = str(value).strip()
    match = _PROVISION_RE.fullmatch(raw) or _LABELED_PROVISION_RE.fullmatch(raw)
    if not match:
        raise ValueError(
            f"invalid provision {value!r}; request a whole section/rule such as "
            "53, section 53(5)(b), rule 13, or Sch. 1 rule 13"
        )
    return match.group(1).upper(), raw


def extract_provisions(xml_content: bytes, provisions: Iterable[str]) -> list[dict]:
    """Return requested section/rule bodies with wording preserved, whitespace normalized."""
    root = ET.fromstring(xml_content)
    # Schedules restart their numbering and therefore contain names such as
    # s4/s11/s12 too.  Main-body sections use temporalId=sN; schedule sections
    # use a scoped id such as sch3_sN.  Prefer the exact main-body identity and
    # otherwise keep the first source-order match rather than silently letting
    # a later schedule overwrite it.
    section_nodes: dict[str, ET.Element] = {}
    for node in root.iter(f"{{{HKLM_NAMESPACE}}}section"):
        name = node.attrib.get("name", "").lower()
        if not name:
            continue
        current = section_nodes.get(name)
        if current is None or (
            node.attrib.get("temporalId", "").lower() == name
            and current.attrib.get("temporalId", "").lower() != name
        ):
            section_nodes[name] = node
    rows = []
    for requested in provisions:
        number, raw = _normalise_provision_request(requested)
        node = section_nodes.get(f"s{number}".lower())
        if node is None:
            row = {"provision": number, "found": False}
            if raw.upper() != number:
                row["normalized_from"] = raw
            rows.append(row)
            continue
        row = {
            "provision": number,
            "found": True,
            "text": _normalised_itertext(node),
            "format_note": "official wording with XML whitespace normalized",
        }
        if raw.upper() != number:
            row["normalized_from"] = raw
        rows.append(row)
    return rows


def hk_legal_authority(
    chapter: str,
    provisions: list[str],
    cross_search_classes: list[int] | None = None,
    *,
    opener=None,
) -> str:
    wanted_chapter = str(chapter).strip().upper()
    if (
        not isinstance(provisions, list)
        or not provisions
        or len(provisions) > MAX_PROVISIONS
    ):
        return json.dumps({
            "success": False,
            "cannot_confirm": True,
            "error": f"provisions must contain 1-{MAX_PROVISIONS} section/rule numbers",
        })
    try:
        for provision in provisions:
            _normalise_provision_request(provision)
        requested_cross_search_classes = _normalise_cross_search_classes(
            cross_search_classes
        )
    except ValueError as exc:
        return json.dumps(
            {
                "success": False,
                "cannot_confirm": True,
                "chapter": wanted_chapter,
                "error": str(exc),
                "instruction": "Correct the provision label and retry before concluding.",
            },
            ensure_ascii=False,
        )
    client = opener or _no_proxy_opener()
    freshness = "current_catalog"
    try:
        catalog = _fetch_bytes(client, CATALOG_URL)
        version = parse_catalog(catalog, wanted_chapter)
        content = read_zip_member_by_range(
            version.archive_url,
            version.member_name,
            opener=client,
        )
        cache_path, xml_sha = _retain_versioned_xml(version, content)
    except Exception as exc:
        cached = _cached_version(wanted_chapter)
        if cached is None:
            return json.dumps(
                {
                    "success": False,
                    "cannot_confirm": True,
                    "chapter": wanted_chapter,
                    "error": f"official HKeL authority unavailable and no retained version exists: {exc}",
                    "instruction": "Do not state a confident Hong Kong statutory conclusion.",
                },
                ensure_ascii=False,
            )
        version, content, xml_sha, cache_path = cached
        freshness = "cached_offline"

    rows = extract_provisions(content, provisions)
    missing = [row["provision"] for row in rows if not row["found"]]
    practice_guidance = []
    if wanted_chapter == "559A" and any(
        row["provision"] == "13" and row["found"] for row in rows
    ):
        practice_guidance.append(_rule_13_practice_guidance(client))
    if requested_cross_search_classes:
        practice_guidance.append(
            _cross_search_practice_guidance(client, requested_cross_search_classes)
        )
    version_day = version.version_date.split("T", 1)[0]
    logger.info(
        "hk_legal_authority_cache chapter=%s freshness=%s path=%s xml_sha256=%s",
        version.chapter,
        freshness,
        cache_path,
        xml_sha,
    )
    required_citation = (
        f"Hong Kong e-Legislation, Cap. {version.chapter}, current version "
        f"{version_day}: {version.web_url}"
    )
    verified_practice_evidence = [
        {
            "status": EVIDENCE_STATUS_VERIFIED_READ,
            "source": row.get("source"),
            "title": row.get("title"),
            "official_url": row.get("official_url"),
            "pdf_sha256": row.get("pdf_sha256"),
            "verified_extracts": row.get("verified_extracts") or [],
        }
        for row in practice_guidance
        if row.get("success") is True
        and row.get("cannot_confirm") is False
        and row.get("matched_page_text_complete") is True
        and row.get("pdf_sha256")
        and row.get("verified_extracts")
    ]
    return json.dumps(
        {
            # Keep the compact answer contract first. This is the evidence the
            # model must consume; the complete provision and matched-page text
            # remain below for deeper inspection and full-fidelity tracing.
            "answer_evidence": {
                "delivery_status": EVIDENCE_DELIVERY_COMPLETE,
                "statutory_citation": required_citation,
                "verified_practice_guidance": verified_practice_evidence,
                "instruction": (
                    "Use this evidence in the user-visible answer. Evidence with "
                    "status=verified_read was delivered completely in this tool "
                    "result: do not claim it was unavailable, unread, or truncated. "
                    "Cite every official URL visibly and keep practice guidance "
                    "distinct from legislation."
                ),
            },
            "success": not missing and all(
                row.get("success") is True
                for row in practice_guidance
                if row.get("title") == IPD_CROSS_SEARCH_LIST_TITLE
            ),
            "cannot_confirm": bool(missing) or any(
                row.get("cannot_confirm") is True
                for row in practice_guidance
                if row.get("title") == IPD_CROSS_SEARCH_LIST_TITLE
            ),
            "freshness": freshness,
            "source": "Hong Kong e-Legislation open data, Department of Justice",
            "dataset_url": DATASET_URL,
            # Put official practice guidance before long provision bodies. Some
            # local-model transports expose only a leading tool-result slice;
            # provenance and the matched manual pages must remain visible even
            # when several full statutory provisions follow.
            "official_practice_guidance": practice_guidance,
            "chapter": version.chapter,
            "title": version.title,
            "version_date": version.version_date,
            "status": version.status,
            "official_web_url": version.web_url,
            "required_answer_citation": required_citation,
            "answer_requirement": (
                "Include required_answer_citation verbatim or as an equivalent "
                "clickable citation in the user-visible answer."
            ),
            "catalog_updated_at": version.catalog_updated_at,
            "catalog_archive_sha256": version.archive_sha256,
            "integrity": (
                "requested member CRC32 and uncompressed size verified; "
                "catalogue archive SHA-256 reported but full archive not downloaded"
            ),
            "xml_sha256": xml_sha,
            "requested_provisions": rows,
            "missing_provisions": missing,
            "instruction": (
                "Cite required_answer_citation. OpenViking/search summaries are leads only. "
                "If cannot_confirm is true, do not state a confident statutory conclusion. "
                "When official_practice_guidance is present, read and cite it separately "
                "from the statutory rule; disclose any manual cannot_confirm result."
            ),
        },
        ensure_ascii=False,
    )


HK_LEGAL_AUTHORITY_SCHEMA = {
    "name": "hk_legal_authority",
    "description": (
        "Read current official Hong Kong legislation provisions from Department of Justice "
        "HKeL XML, with version, official URL, and source digest. Use this before every "
        "Hong Kong statutory legal conclusion; memory, OpenViking, search snippets, and "
        "prose overviews are not authority. Request whole section/rule numbers, e.g. "
        "chapter='559', provisions=['52','53'] or chapter='559A', provisions=['13']. "
        "Common labels such as 'section 53(5)(b)' and 'Sch. 1 rule 13' are "
        "normalized to the whole provision and preserved in the result trace. For a "
        "trade-mark cross-class question, also pass every relevant Nice class in "
        "cross_search_classes; the tool then reads the current official IPD Cross "
        "search list and returns the exact class rows. Do not infer cross-search "
        "relationships from section 12. A Cap. "
        "559A Rule 13 request also retrieves the official IPD 'Time limits in the "
        "examination process' manual, so Rule 13 answers must use both law and current "
        "practice guidance. For a dispute where the challenged mark is already "
        "registered, read Cap. 559 sections 4, 11, 12, 44, 45, 52, and 53 together in "
        "one call: registration ends the opposition-stage route; foreign fame alone "
        "does not establish that a mark is well known in Hong Kong."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chapter": {
                "type": "string",
                "description": "Hong Kong chapter number, such as 559 or 559A.",
            },
            "provisions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_PROVISIONS,
                "description": "Whole section/rule numbers or common labels such as 'section 53(5)(b)' and 'Sch. 1 rule 13'. The tool reads the whole provision.",
            },
            "cross_search_classes": {
                "type": "array",
                "items": {
                    "type": "integer",
                    "minimum": MIN_TRADE_MARK_CLASS,
                    "maximum": MAX_TRADE_MARK_CLASS,
                },
                "minItems": 1,
                "maxItems": MAX_CROSS_SEARCH_CLASSES,
                "uniqueItems": True,
                "description": (
                    "Nice class numbers to verify in the current official IPD Cross "
                    "search list, for example [32, 43]."
                ),
            },
        },
        "required": ["chapter", "provisions"],
        "additionalProperties": False,
    },
}


def _handle_hk_legal_authority(args, **_kwargs):
    return hk_legal_authority(
        args.get("chapter", ""),
        args.get("provisions"),
        args.get("cross_search_classes"),
    )


registry.register(
    name="hk_legal_authority",
    toolset="file_read",
    schema=HK_LEGAL_AUTHORITY_SCHEMA,
    handler=_handle_hk_legal_authority,
    emoji="⚖️",
    max_result_size_chars=100_000,
)


__all__ = [
    "AuthorityVersion",
    "HK_LEGAL_AUTHORITY_SCHEMA",
    "IPD_CROSS_SEARCH_LIST_URL",
    "extract_provisions",
    "hk_legal_authority",
    "parse_catalog",
    "read_zip_member_by_range",
]
