import io
import json
import random
import urllib.error
import zipfile
from pathlib import Path

import pytest

import tools.hk_legal_authority_tool as authority_tool
from tools.hk_legal_authority_tool import (
    CATALOG_URL,
    extract_provisions,
    hk_legal_authority,
    parse_catalog,
    read_zip_member_by_range,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


CATALOG = b"""<?xml version="1.0"?>
<Listing UpdatedDateTime="2026-08-17T21:08:09">
  <DataSet>
    <DataResource url="https://resource.data.one.gov.hk/doj/data/hkel_c_leg_cap_301_cap_600_en.zip"
      sha256="ABCDEF">Legislation</DataResource>
  </DataSet>
  <Chapter>
    <CapNo>559</CapNo>
    <ChapterTitleEnglish>Trade Marks Ordinance</ChapterTitleEnglish>
    <Version>
      <VersionDate isCurrentVersion="true" statusCategory="InEffect">2025-02-14T00:00:00</VersionDate>
      <DataResourceUrl>https://resource.data.one.gov.hk/doj/data/hkel_c_leg_cap_301_cap_600_en.zip</DataResourceUrl>
      <FileLocation>cap_559_en_c</FileLocation>
      <FileName>cap_559_20250214000000_en_c.xml</FileName>
      <Web>https://www.elegislation.gov.hk/hk/cap559!en</Web>
    </Version>
  </Chapter>
</Listing>
"""


AUTHORITY_XML = b"""<?xml version="1.0"?>
<ordinance xmlns="http://www.xml.gov.hk/schemas/hklm/1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <meta><docName>Cap. 559</docName><docStatus>In effect</docStatus>
 <dc:date>2025-02-14</dc:date></meta>
 <main><docTitle>Trade Marks Ordinance</docTitle>
  <section name="s52"><num>52.</num><heading>Revocation</heading>
   <subsection><num>(1)</num><content>The registration of a trade mark may be revoked.</content></subsection>
  </section>
  <section name="s53"><num>53.</num><heading>Invalidity</heading>
   <subsection><num>(1)</num><content>The registration of a trade mark may be declared invalid.</content></subsection>
  </section>
  <schedule name="sch9"><section name="s52" temporalId="sch9_s52">
   <num>52.</num><heading>Unrelated schedule provision</heading>
  </section></schedule>
 </main>
</ordinance>
"""


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def close(self):
        super().close()


class FakeOpener:
    def __init__(self, archive):
        self.archive = archive
        self.ranges = []

    def open(self, request, timeout):
        assert timeout == 600
        url = request.full_url
        if url == CATALOG_URL:
            return FakeResponse(CATALOG)
        if request.get_method() == "HEAD":
            return FakeResponse(b"", headers={"Content-Length": str(len(self.archive))})
        value = request.headers.get("Range")
        if not value:
            raise AssertionError("archive fetch must always be bounded")
        start, end = map(int, value.removeprefix("bytes=").split("-"))
        self.ranges.append((start, end))
        return FakeResponse(self.archive[start : end + 1], status=206)


def make_archive(member_name, content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
        archive.writestr("unrelated/large.xml", random.Random(559).randbytes(200_000))
    return output.getvalue()


def test_parse_catalog_resolves_current_official_version():
    result = parse_catalog(CATALOG, "559")
    assert result.chapter == "559"
    assert result.title == "Trade Marks Ordinance"
    assert result.version_date == "2025-02-14T00:00:00"
    assert result.archive_sha256 == "abcdef"
    assert result.member_name == "cap_559_en_c/cap_559_20250214000000_en_c.xml"


def test_container_boot_seeds_and_repairs_authority_cache_ownership():
    stage2 = (REPO_ROOT / "docker/stage2-hook.sh").read_text(encoding="utf-8")
    assert '"$HERMES_HOME/legal-authority-cache"' in stage2
    repair = (
        'if [ -d "$HERMES_HOME/legal-authority-cache" ] '
        '&& tree_has_non_hermes_owner "$HERMES_HOME/legal-authority-cache"; then'
    )
    assert repair in stage2
    assert 'chown_hermes_tree "$HERMES_HOME/legal-authority-cache"' in stage2


@pytest.mark.parametrize("chapter", ["", "../../etc", "559<script>", "A559"])
def test_parse_catalog_rejects_non_chapter_values(chapter):
    with pytest.raises(ValueError):
        parse_catalog(CATALOG, chapter)


def test_range_reader_extracts_only_requested_backslash_member():
    member = r"cap_559_en_c\cap_559_20250214000000_en_c.xml"
    archive = make_archive(member, AUTHORITY_XML)
    opener = FakeOpener(archive)

    result = read_zip_member_by_range(
        "https://resource.data.one.gov.hk/doj/data/hkel_c_leg_cap_301_cap_600_en.zip",
        "cap_559_en_c/cap_559_20250214000000_en_c.xml",
        opener=opener,
    )

    assert result == AUTHORITY_XML
    assert opener.ranges
    assert all(end - start + 1 < len(archive) for start, end in opener.ranges)


def test_extract_provisions_does_not_confuse_revocation_and_invalidity():
    rows = extract_provisions(AUTHORITY_XML, ["52", "s53", "999"])
    assert rows[0]["found"] is True
    assert "Revocation" in rows[0]["text"]
    assert "Invalidity" not in rows[0]["text"]
    assert "Unrelated schedule" not in rows[0]["text"]
    assert rows[1]["found"] is True
    assert "Invalidity" in rows[1]["text"]
    assert rows[2] == {"provision": "999", "found": False}


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("section 53(5)(b)", "53"),
        ("rule 52", "52"),
        ("Sch. 1 rule 53", "53"),
        ("schedule 1, r. 52", "52"),
    ],
)
def test_extract_provisions_normalizes_common_legal_labels(requested, expected):
    row = extract_provisions(AUTHORITY_XML, [requested])[0]

    assert row["provision"] == expected
    assert row["found"] is True
    assert row["normalized_from"] == requested


def test_invalid_provision_is_a_controlled_tool_result_before_network():
    result = json.loads(
        hk_legal_authority("559A", ["schedule unknown rule maybe"], opener=None)
    )

    assert result["success"] is False
    assert result["cannot_confirm"] is True
    assert "invalid provision" in result["error"]
    assert result["instruction"].startswith("Correct the provision label")


def test_rule_13_manual_returns_only_matching_official_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HK_LEGAL_CACHE", str(tmp_path))
    monkeypatch.setattr(
        authority_tool,
        "_pdf_text",
        lambda _content: (
            "cover page\f"
            "Rule 13(3). The prescribed period for taking the above action expires "
            "6 months thereafter (prescribed 6-month period). Upon receipt of a "
            "request on the specified form with the prescribed fee filed within the "
            "prescribed 6-month period, the Registrar will grant an extension of time "
            "of 3 months. It should however be noted that it cannot be extended.\f"
            "unrelated page\f"
        ),
    )

    class ManualOpener:
        def open(self, request, timeout):
            assert request.full_url == authority_tool.IPD_TIME_LIMITS_MANUAL_URL
            assert timeout == 600
            return FakeResponse(
                b"%PDF-1.7 fake",
                headers={"Last-Modified": "Mon, 24 Aug 2026 00:00:00 GMT"},
            )

    result = authority_tool._rule_13_practice_guidance(ManualOpener())

    assert result["success"] is True
    assert result["cannot_confirm"] is False
    assert result["official_url"] == authority_tool.IPD_TIME_LIMITS_MANUAL_URL
    assert result["server_version_hint"] == "Mon, 24 Aug 2026 00:00:00 GMT"
    assert [page["page"] for page in result["matched_pages"]] == [2]
    assert "Rule 13(3)" in result["matched_pages"][0]["text"]
    assert len(result["verified_extracts"]) == 2
    assert "expires 6 months" in result["verified_extracts"][0]["text"]
    assert "specified form" in result["verified_extracts"][1]["text"]
    assert "path" not in json.dumps(result).lower()


def test_cap_559a_rule_13_automatically_includes_ipd_manual(monkeypatch, tmp_path):
    catalog = (
        CATALOG
        .replace(b"<CapNo>559</CapNo>", b"<CapNo>559A</CapNo>")
        .replace(b"cap_559_en_c", b"cap_559a_en_c")
        .replace(b"cap_559_", b"cap_559a_")
    )
    xml = AUTHORITY_XML.replace(b'name="s52"', b'name="s13"', 1)
    member = r"cap_559a_en_c\cap_559a_20250214000000_en_c.xml"

    class Rule13Opener(FakeOpener):
        def open(self, request, timeout):
            if request.full_url == CATALOG_URL:
                return FakeResponse(catalog)
            return super().open(request, timeout)

    expected_manual = {
        "success": True,
        "cannot_confirm": False,
        "source": "Hong Kong Intellectual Property Department",
        "title": "Time limits in the examination process",
        "official_url": authority_tool.IPD_TIME_LIMITS_MANUAL_URL,
        "pdf_sha256": "a" * 64,
        "matched_page_text_complete": True,
        "verified_extracts": [
            {"page": 2, "text": "The prescribed period expires 6 months later."},
            {"page": 2, "text": "A timely request grants 3 further months."},
        ],
    }
    monkeypatch.setenv("HERMES_HK_LEGAL_CACHE", str(tmp_path))
    monkeypatch.setattr(
        authority_tool,
        "_rule_13_practice_guidance",
        lambda _opener: expected_manual,
    )

    raw_result = hk_legal_authority(
        "559A",
        ["rule 13(1)"],
        opener=Rule13Opener(make_archive(member, xml)),
    )
    result = json.loads(raw_result)

    assert result["success"] is True
    assert result["official_practice_guidance"] == [expected_manual]
    assert result["answer_evidence"]["delivery_status"] == "complete"
    assert result["answer_evidence"]["verified_practice_guidance"] == [
        {
            "status": "verified_read",
            "source": "Hong Kong Intellectual Property Department",
            "title": "Time limits in the examination process",
            "official_url": authority_tool.IPD_TIME_LIMITS_MANUAL_URL,
            "pdf_sha256": "a" * 64,
            "verified_extracts": expected_manual["verified_extracts"],
        }
    ]
    assert raw_result.index('"answer_evidence"') < raw_result.index(
        '"official_practice_guidance"'
    )
    assert raw_result.index('"official_practice_guidance"') < raw_result.index(
        '"requested_provisions"'
    )


def test_full_tool_retains_versioned_xml_and_reports_authority(
    monkeypatch, tmp_path, caplog
):
    member = r"cap_559_en_c\cap_559_20250214000000_en_c.xml"
    opener = FakeOpener(make_archive(member, AUTHORITY_XML))
    monkeypatch.setenv("HERMES_HK_LEGAL_CACHE", str(tmp_path))

    with caplog.at_level("INFO", logger="tools.hk_legal_authority_tool"):
        result = json.loads(hk_legal_authority("559", ["52", "53"], opener=opener))

    assert result["success"] is True
    assert result["cannot_confirm"] is False
    assert result["freshness"] == "current_catalog"
    assert (
        result["source"] == "Hong Kong e-Legislation open data, Department of Justice"
    )
    assert result["official_web_url"] == "https://www.elegislation.gov.hk/hk/cap559!en"
    assert result["required_answer_citation"] == (
        "Hong Kong e-Legislation, Cap. 559, current version 2025-02-14: "
        "https://www.elegislation.gov.hk/hk/cap559!en"
    )
    assert result["requested_provisions"][0]["provision"] == "52"
    assert "retained_cache_path" not in result
    retained = list(tmp_path.glob("cap_559_20250214000000_en_c.xml.*.xml"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == AUTHORITY_XML
    assert str(retained[0]) in caplog.text
    assert result["xml_sha256"] in caplog.text


def test_network_failure_uses_retained_version_and_labels_it_offline(
    monkeypatch, tmp_path
):
    member = r"cap_559_en_c\cap_559_20250214000000_en_c.xml"
    monkeypatch.setenv("HERMES_HK_LEGAL_CACHE", str(tmp_path))
    first = FakeOpener(make_archive(member, AUTHORITY_XML))
    assert (
        json.loads(hk_legal_authority("559", ["53"], opener=first))["success"] is True
    )

    class Offline:
        def open(self, request, timeout):
            raise urllib.error.URLError("offline")

    result = json.loads(hk_legal_authority("559", ["53"], opener=Offline()))
    assert result["success"] is True
    assert result["freshness"] == "cached_offline"
    assert result["version_date"] == "2025-02-14"


def test_missing_provision_forces_cannot_confirm(monkeypatch, tmp_path):
    member = r"cap_559_en_c\cap_559_20250214000000_en_c.xml"
    monkeypatch.setenv("HERMES_HK_LEGAL_CACHE", str(tmp_path))
    result = json.loads(
        hk_legal_authority(
            "559", ["777"], opener=FakeOpener(make_archive(member, AUTHORITY_XML))
        )
    )
    assert result["success"] is False
    assert result["cannot_confirm"] is True
    assert result["missing_provisions"] == ["777"]
