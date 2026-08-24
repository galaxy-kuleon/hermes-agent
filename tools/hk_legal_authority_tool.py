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


def _fetch_bytes(opener, url: str, *, byte_range: tuple[int, int] | None = None) -> bytes:
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


def _retain_versioned_xml(version: AuthorityVersion, content: bytes) -> tuple[Path, str]:
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
            title = _normalised_itertext(title_node) if title_node is not None else f"Cap. {chapter}"
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


def hk_legal_authority(chapter: str, provisions: list[str], *, opener=None) -> str:
    wanted_chapter = str(chapter).strip().upper()
    if not isinstance(provisions, list) or not provisions or len(provisions) > MAX_PROVISIONS:
        return json.dumps(
            {
                "success": False,
                "cannot_confirm": True,
                "error": f"provisions must contain 1-{MAX_PROVISIONS} section/rule numbers",
            }
        )
    try:
        for provision in provisions:
            _normalise_provision_request(provision)
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
    return json.dumps(
        {
            "success": not missing,
            "cannot_confirm": bool(missing),
            "freshness": freshness,
            "source": "Hong Kong e-Legislation open data, Department of Justice",
            "dataset_url": DATASET_URL,
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
                "If cannot_confirm is true, do not state a confident statutory conclusion."
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
        "normalized to the whole provision and preserved in the result trace."
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
        },
        "required": ["chapter", "provisions"],
        "additionalProperties": False,
    },
}


def _handle_hk_legal_authority(args, **_kwargs):
    return hk_legal_authority(args.get("chapter", ""), args.get("provisions"))


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
    "extract_provisions",
    "hk_legal_authority",
    "parse_catalog",
    "read_zip_member_by_range",
]
