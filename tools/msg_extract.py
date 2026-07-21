"""Bounded stdlib extraction of the Unicode body from Outlook MSG files."""

from __future__ import annotations

import struct
from pathlib import Path


_CFBF_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
_FREE_SECTOR = 0xFFFFFFFF
_END_OF_CHAIN = 0xFFFFFFFE
_FAT_SECTOR = 0xFFFFFFFD
_DIFAT_SECTOR = 0xFFFFFFFC
_SPECIAL_SECTORS = {
    _FREE_SECTOR,
    _END_OF_CHAIN,
    _FAT_SECTOR,
    _DIFAT_SECTOR,
}
_UNICODE_BODY_STREAM = "__substg1.0_1000001F"
_MAX_MSG_BYTES = 100 * 1024 * 1024
_MAX_BODY_BYTES = 20 * 1024 * 1024


class MsgExtractionError(ValueError):
    """Raised when a MSG compound file is malformed or has no text body."""


class _CompoundFile:
    def __init__(self, data: bytes):
        if len(data) < 512:
            raise MsgExtractionError("MSG compound-file header is truncated")
        if data[:8] != _CFBF_SIGNATURE:
            raise MsgExtractionError("MSG is not a CFBF compound file")
        if struct.unpack_from("<H", data, 28)[0] != 0xFFFE:
            raise MsgExtractionError("MSG compound file has invalid byte order")

        self.data = data
        self.major_version = struct.unpack_from("<H", data, 26)[0]
        sector_shift = struct.unpack_from("<H", data, 30)[0]
        mini_sector_shift = struct.unpack_from("<H", data, 32)[0]
        expected_shift = {3: 9, 4: 12}.get(self.major_version)
        if expected_shift is None or sector_shift != expected_shift:
            raise MsgExtractionError("MSG compound file has unsupported sector geometry")
        if mini_sector_shift != 6:
            raise MsgExtractionError("MSG compound file has unsupported mini-sector geometry")

        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_sector_shift
        if len(data) < self.sector_size or (len(data) - self.sector_size) % self.sector_size:
            raise MsgExtractionError("MSG compound file has a truncated sector")
        self.sector_count = (len(data) - self.sector_size) // self.sector_size
        self.mini_stream_cutoff = struct.unpack_from("<I", data, 56)[0]
        if self.mini_stream_cutoff != 4096:
            raise MsgExtractionError("MSG compound file has invalid mini-stream cutoff")

        self.first_directory_sector = struct.unpack_from("<I", data, 48)[0]
        self.first_mini_fat_sector = struct.unpack_from("<I", data, 60)[0]
        self.mini_fat_sector_count = struct.unpack_from("<I", data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", data, 68)[0]
        self.difat_sector_count = struct.unpack_from("<I", data, 72)[0]
        self.fat = self._read_fat(struct.unpack_from("<I", data, 44)[0])

    def _sector(self, sector_id: int, *, label: str) -> bytes:
        if sector_id in _SPECIAL_SECTORS or sector_id >= self.sector_count:
            raise MsgExtractionError(f"MSG {label} references an invalid sector")
        offset = (sector_id + 1) * self.sector_size
        return self.data[offset : offset + self.sector_size]

    def _read_fat(self, fat_sector_count: int) -> list[int]:
        if fat_sector_count <= 0 or fat_sector_count > self.sector_count:
            raise MsgExtractionError("MSG compound file has invalid FAT size")
        fat_sector_ids = [
            sector_id
            for sector_id in struct.unpack_from("<109I", self.data, 76)
            if sector_id != _FREE_SECTOR
        ]

        difat_id = self.first_difat_sector
        visited: set[int] = set()
        entries_per_difat = self.sector_size // 4 - 1
        for _ in range(self.difat_sector_count):
            if difat_id in visited:
                raise MsgExtractionError("MSG DIFAT cycle detected")
            visited.add(difat_id)
            sector = self._sector(difat_id, label="DIFAT")
            values = struct.unpack(f"<{entries_per_difat + 1}I", sector)
            fat_sector_ids.extend(
                sector_id for sector_id in values[:-1] if sector_id != _FREE_SECTOR
            )
            difat_id = values[-1]
        if self.difat_sector_count and difat_id != _END_OF_CHAIN:
            raise MsgExtractionError("MSG DIFAT chain is longer than declared")
        if len(fat_sector_ids) < fat_sector_count:
            raise MsgExtractionError("MSG FAT sector list is shorter than declared")

        fat: list[int] = []
        entries_per_sector = self.sector_size // 4
        for sector_id in fat_sector_ids[:fat_sector_count]:
            sector = self._sector(sector_id, label="FAT")
            fat.extend(struct.unpack(f"<{entries_per_sector}I", sector))
        return fat

    def _regular_chain(
        self,
        start_sector: int,
        *,
        label: str,
        declared_size: int | None = None,
    ) -> bytes:
        if start_sector == _END_OF_CHAIN:
            if declared_size:
                raise MsgExtractionError(f"MSG {label} chain is shorter than declared size")
            return b""
        chunks: list[bytes] = []
        visited: set[int] = set()
        sector_id = start_sector
        while sector_id != _END_OF_CHAIN:
            if sector_id in visited:
                raise MsgExtractionError(f"MSG {label} FAT cycle detected")
            visited.add(sector_id)
            if sector_id >= len(self.fat):
                raise MsgExtractionError(f"MSG {label} FAT chain references an invalid sector")
            chunks.append(self._sector(sector_id, label=label))
            if len(chunks) > self.sector_count:
                raise MsgExtractionError(f"MSG {label} FAT chain is unbounded")
            sector_id = self.fat[sector_id]

        value = b"".join(chunks)
        if declared_size is not None:
            if len(value) < declared_size:
                raise MsgExtractionError(f"MSG {label} chain is shorter than declared size")
            value = value[:declared_size]
        return value

    def _directory_entries(self) -> list[dict[str, int | str]]:
        directory = self._regular_chain(
            self.first_directory_sector,
            label="directory",
        )
        entries: list[dict[str, int | str]] = []
        for offset in range(0, len(directory), 128):
            raw = directory[offset : offset + 128]
            if len(raw) < 128:
                break
            name_bytes = struct.unpack_from("<H", raw, 64)[0]
            entry_type = raw[66]
            if entry_type == 0:
                continue
            if name_bytes < 2 or name_bytes > 64 or name_bytes % 2:
                raise MsgExtractionError("MSG directory has an invalid entry name")
            try:
                name = raw[: name_bytes - 2].decode("utf-16le")
            except UnicodeDecodeError as exc:
                raise MsgExtractionError("MSG directory entry name is malformed") from exc
            size = struct.unpack_from("<Q", raw, 120)[0]
            if self.major_version == 3:
                size &= 0xFFFFFFFF
            entries.append(
                {
                    "name": name,
                    "type": entry_type,
                    "start": struct.unpack_from("<I", raw, 116)[0],
                    "size": size,
                }
            )
        return entries

    def unicode_body(self) -> bytes:
        entries = self._directory_entries()
        root = next((entry for entry in entries if entry["type"] == 5), None)
        body = next(
            (
                entry
                for entry in entries
                if entry["type"] == 2 and entry["name"] == _UNICODE_BODY_STREAM
            ),
            None,
        )
        if root is None or body is None:
            raise MsgExtractionError("MSG contains no Unicode plain-text body")
        body_size = int(body["size"])
        if body_size <= 0:
            raise MsgExtractionError("MSG Unicode body is empty")
        if body_size > _MAX_BODY_BYTES:
            raise MsgExtractionError("MSG Unicode body exceeds the extraction limit")

        if body_size >= self.mini_stream_cutoff:
            return self._regular_chain(
                int(body["start"]),
                label="stream",
                declared_size=body_size,
            )
        return self._mini_chain(body, root)

    def _mini_chain(
        self,
        body: dict[str, int | str],
        root: dict[str, int | str],
    ) -> bytes:
        if self.mini_fat_sector_count <= 0:
            raise MsgExtractionError("MSG mini FAT is missing")
        mini_fat_bytes = self._regular_chain(
            self.first_mini_fat_sector,
            label="mini FAT",
            declared_size=self.mini_fat_sector_count * self.sector_size,
        )
        mini_fat = list(
            struct.unpack(
                f"<{len(mini_fat_bytes) // 4}I",
                mini_fat_bytes,
            )
        )
        root_stream = self._regular_chain(
            int(root["start"]),
            label="root mini stream",
            declared_size=int(root["size"]),
        )

        body_size = int(body["size"])
        mini_sector_id = int(body["start"])
        chunks: list[bytes] = []
        visited: set[int] = set()
        while mini_sector_id != _END_OF_CHAIN:
            if mini_sector_id in visited:
                raise MsgExtractionError("MSG mini FAT cycle detected")
            visited.add(mini_sector_id)
            if mini_sector_id >= len(mini_fat):
                raise MsgExtractionError("MSG mini FAT references an invalid sector")
            offset = mini_sector_id * self.mini_sector_size
            chunk = root_stream[offset : offset + self.mini_sector_size]
            if len(chunk) != self.mini_sector_size:
                raise MsgExtractionError("MSG mini stream chain is shorter than declared size")
            chunks.append(chunk)
            if len(chunks) > len(mini_fat):
                raise MsgExtractionError("MSG mini FAT chain is unbounded")
            mini_sector_id = mini_fat[mini_sector_id]

        value = b"".join(chunks)
        if len(value) < body_size:
            raise MsgExtractionError("MSG mini stream chain is shorter than declared size")
        return value[:body_size]


def extract_msg_text(path: str) -> str:
    """Return the Unicode plain-text MAPI body from an Outlook MSG file."""
    try:
        file_path = Path(path)
        size = file_path.stat().st_size
        if size > _MAX_MSG_BYTES:
            raise MsgExtractionError("MSG file exceeds the extraction size limit")
        compound = _CompoundFile(file_path.read_bytes())
        body = compound.unicode_body()
        if len(body) % 2:
            raise MsgExtractionError("MSG Unicode body has an odd byte length")
        text = body.decode("utf-16le", errors="strict").rstrip("\x00")
    except OSError as exc:
        raise MsgExtractionError(f"Cannot read MSG file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise MsgExtractionError("MSG Unicode body is malformed") from exc
    if not text.strip():
        raise MsgExtractionError("MSG Unicode body contains no readable text")
    return text.rstrip("\r\n") + "\n"


__all__ = ["MsgExtractionError", "extract_msg_text"]
