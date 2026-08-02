#!/usr/bin/env python3
"""
Tests for structured-document extraction in the read_file tool.

Covers .ipynb / .docx / .xlsx extraction (ported from Kilo-Org/kilocode
#10733, #10737, #10740) and the read_file_tool integration: pagination,
line-numbering, graceful fallback on malformed input, and hidden-sheet
omission.

Run with:  python -m pytest tests/tools/test_read_extract.py -v
"""

import json
import os
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools.read_extract import (
    ExtractionError,
    extract_document_text,
    is_extractable_document,
)
from tools.file_tools import read_file_tool


# ---------------------------------------------------------------------------
# Fixture builders — construct minimal valid OOXML / notebook files.
# ---------------------------------------------------------------------------

def _write_notebook(path, cells, nbformat=4):
    nb = {"cells": cells, "metadata": {}, "nbformat": nbformat, "nbformat_minor": 5}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(nb, fh)


def _write_docx(path, document_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)


def _write_xlsx(path, *, workbook, rels, shared, sheets):
    """sheets: dict of part-name -> xml string."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", shared)
        for part, xml in sheets.items():
            z.writestr(part, xml)


def _write_msg(
    path,
    body_text,
    *,
    storage="fat",
    chain="valid",
):
    """Write a minimal v3 CFBF file with one Unicode MAPI body stream."""
    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD
    sector_size = 512

    if storage not in {"fat", "mini"}:
        raise ValueError(f"unknown storage: {storage}")
    if chain not in {"valid", "cycle", "short"}:
        raise ValueError(f"unknown chain: {chain}")
    if storage == "mini" and chain == "short":
        raise ValueError("short-chain fixture uses regular FAT storage")

    header = bytearray(sector_size)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header, 24, 0x003E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    first_mini_fat = 2 if storage == "mini" else end_of_chain
    mini_fat_count = 1 if storage == "mini" else 0
    struct.pack_into(
        "<IIIIIIIII",
        header,
        40,
        0,
        1,
        1,
        0,
        4096,
        first_mini_fat,
        mini_fat_count,
        end_of_chain,
        0,
    )
    struct.pack_into("<I", header, 76, 0)
    for offset in range(80, sector_size, 4):
        struct.pack_into("<I", header, offset, free_sector)

    fat = bytearray(b"\xff" * sector_size)
    if storage == "mini":
        fat_entries = [fat_sector, end_of_chain, end_of_chain, end_of_chain]
    elif chain == "valid":
        fat_entries = [fat_sector, end_of_chain] + list(range(3, 10)) + [end_of_chain]
    elif chain == "cycle":
        fat_entries = [fat_sector, end_of_chain, 2]
    else:
        fat_entries = [fat_sector, end_of_chain, end_of_chain]
    for index, value in enumerate(fat_entries):
        struct.pack_into("<I", fat, index * 4, value)

    def directory_entry(name, entry_type, child, start_sector, stream_size):
        entry = bytearray(128)
        encoded_name = (name + "\0").encode("utf-16le")
        entry[: len(encoded_name)] = encoded_name
        struct.pack_into("<HBBIII", entry, 64, len(encoded_name), entry_type, 1, free_sector, free_sector, child)
        struct.pack_into("<I", entry, 116, start_sector)
        struct.pack_into("<Q", entry, 120, stream_size)
        return entry

    encoded_body = (body_text + "\r\n\0").encode("utf-16le")
    if storage == "mini":
        if len(encoded_body) <= 64 or len(encoded_body) > 128:
            raise ValueError("mini fixture body must span exactly two mini sectors")
        root_start, root_size = 3, 128
        body_start, body_size = 0, len(encoded_body)
    else:
        root_start, root_size = end_of_chain, 0
        body_start, body_size = 2, 4096

    directory = bytearray(sector_size)
    directory[:128] = directory_entry("Root Entry", 5, 1, root_start, root_size)
    directory[128:256] = directory_entry(
        "__substg1.0_1000001F",
        2,
        free_sector,
        body_start,
        body_size,
    )

    if storage == "mini":
        mini_fat = bytearray(b"\xff" * sector_size)
        struct.pack_into("<I", mini_fat, 0, 0 if chain == "cycle" else 1)
        struct.pack_into("<I", mini_fat, 4, end_of_chain)
        mini_stream = bytearray(sector_size)
        mini_stream[: len(encoded_body)] = encoded_body
        sectors = [fat, directory, mini_fat, mini_stream]
    elif chain == "valid":
        body = (encoded_body * ((4096 // len(encoded_body)) + 1))[:4096]
        sectors = [fat, directory] + [
            body[offset : offset + sector_size]
            for offset in range(0, 4096, sector_size)
        ]
    else:
        sectors = [fat, directory, encoded_body.ljust(sector_size, b"\0")]

    with open(path, "wb") as fh:
        fh.write(header)
        for sector in sectors:
            fh.write(sector)


def _write_msg_with_directory_collision(
    path,
    root_body,
    nested_body,
    *,
    cycle=False,
    root_sid=0,
):
    """Write a mini-stream MSG whose embedded message has the first body entry."""
    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD
    sector_size = 512

    header = bytearray(sector_size)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header, 24, 0x003E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into(
        "<IIIIIIIII",
        header,
        40,
        0,
        1,
        1,
        0,
        4096,
        2,
        1,
        end_of_chain,
        0,
    )
    struct.pack_into("<I", header, 76, 0)
    for offset in range(80, sector_size, 4):
        struct.pack_into("<I", header, offset, free_sector)

    fat = bytearray(b"\xff" * sector_size)
    for index, value in enumerate((fat_sector, end_of_chain, end_of_chain, end_of_chain)):
        struct.pack_into("<I", fat, index * 4, value)

    def directory_entry(
        name,
        entry_type,
        *,
        color=1,
        left=free_sector,
        right=free_sector,
        child=free_sector,
        start_sector=end_of_chain,
        stream_size=0,
    ):
        entry = bytearray(128)
        encoded_name = (name + "\0").encode("utf-16le")
        entry[: len(encoded_name)] = encoded_name
        struct.pack_into(
            "<HBBIII",
            entry,
            64,
            len(encoded_name),
            entry_type,
            color,
            left,
            right,
            child,
        )
        struct.pack_into("<I", entry, 116, start_sector)
        struct.pack_into("<Q", entry, 120, stream_size)
        return entry

    nested_bytes = (nested_body + "\r\n\0").encode("utf-16le")
    root_bytes = (root_body + "\r\n\0").encode("utf-16le")
    if not (64 < len(nested_bytes) <= 128 and 64 < len(root_bytes) <= 128):
        raise ValueError("collision fixture bodies must span exactly two mini sectors")

    if root_sid not in {0, 1}:
        raise ValueError("root_sid must be 0 or 1")

    directory = bytearray(sector_size)
    root_entry = directory_entry(
        "Root Entry",
        5,
        child=1 if root_sid == 0 else 3,
        start_sector=3,
        stream_size=256,
    )
    attachment_entry = directory_entry(
        "__attach_version1.0_#00000000",
        1,
        left=1 if cycle else free_sector,
        right=3,
        child=2,
    )
    if root_sid == 0:
        directory[:128] = root_entry
        directory[128:256] = attachment_entry
    else:
        directory[:128] = directory_entry("Decoy Storage", 1)
        directory[128:256] = root_entry
    directory[256:384] = directory_entry(
        "__substg1.0_1000001F",
        2,
        start_sector=0,
        stream_size=len(nested_bytes),
    )
    directory[384:512] = directory_entry(
        "__substg1.0_1000001F",
        2,
        color=0 if root_sid == 0 else 1,
        start_sector=2,
        stream_size=len(root_bytes),
    )

    mini_fat = bytearray(b"\xff" * sector_size)
    for index, value in enumerate((1, end_of_chain, 3, end_of_chain)):
        struct.pack_into("<I", mini_fat, index * 4, value)
    mini_stream = bytearray(sector_size)
    mini_stream[: len(nested_bytes)] = nested_bytes
    mini_stream[128 : 128 + len(root_bytes)] = root_bytes

    with open(path, "wb") as fh:
        fh.write(header)
        for sector in (fat, directory, mini_fat, mini_stream):
            fh.write(sector)


_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ---------------------------------------------------------------------------
# is_extractable_document
# ---------------------------------------------------------------------------

class TestIsExtractable(unittest.TestCase):
    def test_recognized_extensions(self):
        self.assertTrue(is_extractable_document("a.ipynb"))
        self.assertTrue(is_extractable_document("/x/B.DOCX"))
        self.assertTrue(is_extractable_document("report.xlsx"))
        self.assertTrue(is_extractable_document("a.pdf"))
        self.assertTrue(is_extractable_document("/synthetic/REPORT.PDF"))
        self.assertTrue(is_extractable_document("mail.MSG"))

    def test_unrecognized_extensions(self):
        self.assertFalse(is_extractable_document("a.py"))
        self.assertFalse(is_extractable_document("a.txt"))


# ---------------------------------------------------------------------------
# PDF dispatch / failure contract
# ---------------------------------------------------------------------------

class TestPdfExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_pdf_")
        self.path = os.path.join(self.tmp, "synthetic.PDF")
        with open(self.path, "wb") as fh:
            fh.write(b"%PDF-1.4\nsynthetic test bytes only\n\x00\xff")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pdf_dispatches_to_pdf_extract_with_synthetic_path(self):
        with patch(
            "tools.pdf_extract.extract_pdf_text",
            return_value="Synthetic PDF text\n",
        ) as extract_pdf_text:
            self.assertEqual(
                extract_document_text(self.path),
                "Synthetic PDF text\n",
            )
        extract_pdf_text.assert_called_once_with(self.path)

    def test_pdf_error_propagates_as_honest_unreadable_not_binary_garbage(self):
        """Extraction failure must not fall through to raw-bytes-as-text.

        Before M-U1-D, a failed PDF/MSG extract returned binary garbage as
        ``content`` (and the request memo marked the file read). The model then
        produced a complete-looking audit that never actually read the file.
        """
        with patch(
            "tools.pdf_extract.extract_pdf_text",
            side_effect=ExtractionError("synthetic extractor failure"),
        ):
            with self.assertRaisesRegex(
                ExtractionError,
                "synthetic extractor failure",
            ):
                extract_document_text(self.path)

            result = json.loads(read_file_tool(self.path))

        self.assertNotIn("extracted_document", result)
        self.assertNotIn("content", result)
        self.assertTrue(result.get("extraction_failed"))
        self.assertIs(result.get("readable"), False)
        self.assertEqual(result.get("report_as"), "unreadable")
        self.assertIn("synthetic extractor failure", result["error"])
        self.assertIn("unreadable materials", result["error"])
        self.assertNotIn("%PDF-1.4", result.get("error", ""))


class TestMsgExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_msg_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unicode_mapi_body_is_extracted_from_real_cfbf(self):
        path = os.path.join(self.tmp, "invoice.msg")
        _write_msg(path, "Invoice 2026-071 audit body")

        text = extract_document_text(path)

        self.assertIn("Invoice 2026-071 audit body", text)

    def test_small_unicode_mapi_body_is_extracted_from_mini_stream(self):
        path = os.path.join(self.tmp, "mini-stream.msg")
        body = "Mini Unicode body: café / 東京 / résumé"
        _write_msg(path, body, storage="mini")

        text = extract_document_text(path)

        self.assertIn(body, text)

    def test_root_message_body_wins_over_nested_duplicate_stream(self):
        path = os.path.join(self.tmp, "nested-body.msg")
        root_body = "Root message body is the expected audit text."
        nested_body = "Nested attachment body must never be selected."
        _write_msg_with_directory_collision(path, root_body, nested_body)

        text = extract_document_text(path)

        self.assertIn(root_body, text)
        self.assertNotIn(nested_body, text)

    def test_directory_sibling_cycle_is_rejected(self):
        path = os.path.join(self.tmp, "directory-cycle.msg")
        _write_msg_with_directory_collision(
            path,
            "Root message body is the expected audit text.",
            "Nested attachment body must never be selected.",
            cycle=True,
        )

        with self.assertRaisesRegex(ExtractionError, r"(?i)directory.*cycle|cycle.*directory"):
            extract_document_text(path)

    def test_directory_root_must_be_sid_zero(self):
        path = os.path.join(self.tmp, "root-not-sid-zero.msg")
        _write_msg_with_directory_collision(
            path,
            "Root message body is the expected audit text.",
            "Nested attachment body must never be selected.",
            root_sid=1,
        )

        with self.assertRaisesRegex(ExtractionError, r"(?i)root.*SID.?0|SID.?0.*root"):
            extract_document_text(path)

    def test_directory_root_self_cycle_is_rejected(self):
        from tools.msg_extract import MsgExtractionError, _CompoundFile

        root = {"child": 0}

        with self.assertRaisesRegex(MsgExtractionError, r"(?i)cycle"):
            _CompoundFile._direct_children([root], root)

    def test_fat_cycle_is_rejected(self):
        path = os.path.join(self.tmp, "malformed-fat.msg")
        _write_msg(path, "cycle", chain="cycle")

        with self.assertRaisesRegex(ExtractionError, r"(?i)FAT.*cycle|cycle.*FAT"):
            extract_document_text(path)

    def test_mini_fat_cycle_is_rejected(self):
        path = os.path.join(self.tmp, "malformed-mini.msg")
        _write_msg(
            path,
            "Mini Unicode body: café / 東京 / résumé",
            storage="mini",
            chain="cycle",
        )

        with self.assertRaisesRegex(
            ExtractionError,
            r"(?i)mini.?FAT.*cycle|cycle.*mini.?FAT",
        ):
            extract_document_text(path)

    def test_declared_stream_larger_than_fat_chain_is_rejected(self):
        path = os.path.join(self.tmp, "short-chain.msg")
        _write_msg(path, "short", chain="short")

        with self.assertRaisesRegex(
            ExtractionError,
            r"(?i)(stream|chain).*(short|trunc|declared|size)",
        ):
            extract_document_text(path)

    def test_truncated_msg_raises_extraction_error(self):
        path = os.path.join(self.tmp, "truncated.msg")
        with open(path, "wb") as fh:
            fh.write(bytes.fromhex("d0cf11e0a1b11ae1") + b"truncated")

        with self.assertRaises(ExtractionError) as raised:
            extract_document_text(path)
        self.assertNotIn("Unsupported document type", str(raised.exception))


# ---------------------------------------------------------------------------
# Notebooks (.ipynb) — #10733
# ---------------------------------------------------------------------------

class TestNotebookExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_nb_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_markdown_and_code_in_order(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": ["# Title\n", "para"]},
            {"cell_type": "code", "source": "x = 1\nprint(x)",
             "outputs": [{"output_type": "stream", "text": ["1\n"]}],
             "execution_count": 1},
        ])
        text = extract_document_text(p)
        self.assertIn("# Title", text)
        self.assertIn("print(x)", text)
        # Output payloads must NOT leak into the extracted text.
        self.assertNotIn("output_type", text)
        self.assertNotIn("execution_count", text)
        # Order preserved: markdown before code.
        self.assertLess(text.index("Title"), text.index("print(x)"))

    def test_string_source_form(self):
        p = os.path.join(self.tmp, "nb2.ipynb")
        _write_notebook(p, [{"cell_type": "code", "source": "single string source"}])
        self.assertIn("single string source", extract_document_text(p))

    def test_legacy_worksheets_form(self):
        p = os.path.join(self.tmp, "nb3.ipynb")
        nb = {"worksheets": [{"cells": [
            {"cell_type": "code", "input": "ignored", "source": "legacy cell"}]}],
            "nbformat": 3}
        with open(p, "w") as fh:
            json.dump(nb, fh)
        self.assertIn("legacy cell", extract_document_text(p))

    def test_malformed_notebook_raises(self):
        p = os.path.join(self.tmp, "bad.ipynb")
        with open(p, "w") as fh:
            fh.write("{ not valid json")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_empty_cells_raises(self):
        p = os.path.join(self.tmp, "empty.ipynb")
        _write_notebook(p, [])
        with self.assertRaises(ExtractionError):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# Word documents (.docx) — #10737
# ---------------------------------------------------------------------------

class TestDocxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_docx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, body):
        return (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                f'<w:body>{body}</w:body></w:document>')

    def test_paragraphs_and_runs(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, self._doc(
            '<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>World</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Second</w:t></w:r></w:p>'))
        text = extract_document_text(p)
        self.assertIn("Hello World", text)
        self.assertIn("Second", text)

    def test_tabs_and_breaks(self):
        p = os.path.join(self.tmp, "d2.docx")
        _write_docx(p, self._doc(
            '<w:p><w:r><w:t>A</w:t><w:tab/><w:t>B</w:t><w:br/><w:t>C</w:t></w:r></w:p>'))
        text = extract_document_text(p)
        self.assertIn("A\tB", text)
        self.assertIn("C", text)

    def test_not_a_zip_raises(self):
        p = os.path.join(self.tmp, "bad.docx")
        with open(p, "wb") as fh:
            fh.write(b"plain bytes, not a zip")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_missing_document_xml_raises(self):
        p = os.path.join(self.tmp, "nodoc.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("other.xml", "<x/>")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# Excel workbooks (.xlsx) — #10740
# ---------------------------------------------------------------------------

class TestXlsxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_xlsx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, path, *, include_hidden=True):
        r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        hidden_sheet = (f'<sheet name="Hidden" sheetId="2" state="hidden" '
                        f'xmlns:r="{r}" r:id="rId2"/>') if include_hidden else ""
        workbook = (
            f'<workbook xmlns="{_NS_S}" xmlns:r="{r}"><sheets>'
            f'<sheet name="Data" sheetId="1" r:id="rId1"/>{hidden_sheet}'
            f'</sheets></workbook>')
        rels = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="x"/>'
            '</Relationships>')
        shared = (f'<sst xmlns="{_NS_S}"><si><t>Name</t></si><si><t>Score</t></si>'
                  f'<si><t>Alice</t></si></sst>')
        sheet1 = (
            f'<worksheet xmlns="{_NS_S}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>95</v></c></row>'
            '</sheetData></worksheet>')
        sheet2 = (f'<worksheet xmlns="{_NS_S}"><sheetData>'
                  '<row r="1"><c r="A1" t="str"><v>SECRETDATA</v></c></row>'
                  '</sheetData></worksheet>')
        _write_xlsx(path, workbook=workbook, rels=rels, shared=shared,
                    sheets={"xl/worksheets/sheet1.xml": sheet1,
                            "xl/worksheets/sheet2.xml": sheet2})

    def test_visible_sheet_content(self):
        p = os.path.join(self.tmp, "wb.xlsx")
        self._build(p)
        text = extract_document_text(p)
        self.assertIn("Data", text)        # sheet label
        self.assertIn("Name\tScore", text)  # shared-string header row
        self.assertIn("Alice\t95", text)    # string + numeric cells

    def test_hidden_sheet_omitted(self):
        p = os.path.join(self.tmp, "wb2.xlsx")
        self._build(p)
        text = extract_document_text(p)
        self.assertNotIn("SECRETDATA", text)
        self.assertNotIn("Hidden", text)

    def test_not_a_zip_raises(self):
        p = os.path.join(self.tmp, "bad.xlsx")
        with open(p, "wb") as fh:
            fh.write(b"nope")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# read_file_tool integration
# ---------------------------------------------------------------------------

class TestReadFileToolIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_int_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notebook_read_is_line_numbered(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": "# H"},
            {"cell_type": "code", "source": "print(1)"},
        ])
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("1|", res["content"])  # line-number gutter
        self.assertIn("print(1)", res["content"])

    def test_pagination(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "code", "source": "a\nb\nc\nd\ne\nf"},
        ])
        res = json.loads(read_file_tool(p, offset=1, limit=2))
        self.assertTrue(res.get("truncated"))
        self.assertIn("offset=3", res.get("hint", ""))
        # Only first 2 lines present.
        self.assertIn("1|# ── Code cell 1 ──", res["content"])

    def test_corrupt_docx_reports_honest_unreadable_not_binary_garbage(self):
        """A corrupt DOCX must surface as unreadable, not as raw-bytes content.

        Before M-U1-D this fell through to the binary-extension guard, whose
        message merely said "binary". That still let the request memo mark the
        file read, so the model could write a complete-looking audit over a file
        it never read. The DOCX path now carries the same contract as the PDF
        path in ``test_pdf_error_propagates_as_honest_unreadable_not_binary_garbage``.
        """
        p = os.path.join(self.tmp, "bad.docx")
        with open(p, "wb") as fh:
            fh.write(b"not a zip")
        res = json.loads(read_file_tool(p))
        # Should NOT crash, and must not hand back any readable-looking payload.
        self.assertNotIn("extracted_document", res)
        self.assertNotIn("content", res)
        self.assertTrue(res.get("extraction_failed"))
        self.assertIs(res.get("readable"), False)
        self.assertEqual(res.get("report_as"), "unreadable")
        self.assertIn("Not a valid DOCX", res["error"])
        self.assertIn("unreadable materials", res["error"])

    def test_docx_read_extracts(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                        '<w:body><w:p><w:r><w:t>Report body</w:t></w:r></w:p>'
                        '</w:body></w:document>'))
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("Report body", res["content"])


if __name__ == "__main__":
    unittest.main()
