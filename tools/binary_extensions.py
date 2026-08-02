"""Binary and image extension policy for file-reading operations.

These files can't be meaningfully compared as text and are often large.
Ported from free-code src/constants/files.ts.
"""

IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
})

BINARY_EXTENSIONS = IMAGE_EXTENSIONS | frozenset({
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    # Executables/binaries
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    # Documents (exclude .pdf — text-based, agents may want to inspect)
    # .doc/.docx/.xls/.xlsx are still extractable via read_extract before the
    # binary guard; listing them here only blocks non-extractable fallthrough.
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Mail stores / shortcuts with no Path-B direct reader (.msg is extractable
    # and deliberately NOT listed; .eml is plain MIME text and stays readable).
    ".pst", ".ost", ".oab",
    ".lnk",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Bytecode / VM artifacts
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    # Database files
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    # Design / 3D
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    # Flash
    ".swf", ".fla",
    # Lock/profiling data
    ".lockb", ".dat", ".data",
})


def has_binary_extension(path: str) -> bool:
    """Check if a file path has a binary extension. Pure string check, no I/O."""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in BINARY_EXTENSIONS


def has_image_extension(path: str) -> bool:
    """Check if a file path is an image supported by the vision route."""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in IMAGE_EXTENSIONS
