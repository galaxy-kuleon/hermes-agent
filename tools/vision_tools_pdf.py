import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.read_extract import PDF_PAGE_SCAN_TIMEOUT

PDF_RENDER_DPI = 150


def save_pdf_trace(directory: Path, name: str, value) -> None:
    path = directory / (uuid.uuid4().hex + "-" + name + ".json")
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def render_pdf_page(source: str, page_number) -> dict:
    from hermes_cli.config import load_config_readonly

    path = Path(source).resolve(strict=True)
    directory = get_hermes_home() / "vision" / "pdf-pages" / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    dpi = load_config_readonly().get("vision", {}).get("pdf_render_dpi", PDF_RENDER_DPI)
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("vision.pdf_render_dpi must be a positive integer")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = {"render_id": directory.name, "source_path": str(path),
                "source_sha256": digest, "page_number": page_number, "dpi": dpi}
    save_pdf_trace(directory, "source", metadata)

    def execute(argv):
        try:
            result = subprocess.run(argv, capture_output=True, timeout=PDF_PAGE_SCAN_TIMEOUT)
        except Exception as error:
            save_pdf_trace(directory, "command-error", {"argv": argv, "error": str(error)})
            raise
        save_pdf_trace(directory, "command", {
            "argv": argv, "returncode": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace")})
        if result.returncode:
            raise ValueError("PDF page operation failed: " + result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8", errors="replace")

    info = execute(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match:
        raise ValueError("PDF page count unavailable")
    page_count = int(match.group(1))
    metadata["page_count"] = page_count
    if isinstance(page_number, bool) or not isinstance(page_number, int) or not 1 <= page_number <= page_count:
        save_pdf_trace(directory, "invalid-page", metadata)
        raise ValueError(f"PDF has {page_count} pages; supply page_number as an integer from 1 to {page_count}")
    output = directory / "page"
    execute(["pdftoppm", "-f", str(page_number), "-l", str(page_number),
             "-r", str(dpi), "-singlefile", "-png", str(path), str(output)])
    image = output.with_suffix(".png")
    image.chmod(0o600)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Original PDF changed during rendering")
    metadata["image_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    save_pdf_trace(directory, "rendered", metadata)
    return {"directory": directory, "image": image,
            "source_page": {key: metadata[key] for key in (
                "render_id", "source_sha256", "page_number", "page_count", "dpi", "image_sha256")}}
