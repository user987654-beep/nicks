"""Shared, conservative classification and lossless representation of source files."""
from __future__ import annotations

import base64
import codecs
import hashlib
import re
from pathlib import PurePosixPath
from pathlib import Path


_SOURCE_DIRS = {"src", "source", "sources", "lib", "include", "test", "tests", "config", "configs"}
_SOURCE_NAMES = {"makefile", "dockerfile", "justfile", "pyproject.toml", "setup.cfg", "tox.ini",
                 "requirements.txt", "cargo.toml", "go.mod", "package.json", "tsconfig.json"}
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".pyx", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash", ".zsh", ".rb",
    ".php", ".swift", ".kt", ".kts", ".scala", ".sql", ".html", ".css", ".scss",
    ".xml", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".md", ".rst", ".txt", ".csv", ".proto", ".graphql", ".cmake", ".gradle",
}
_ARCHIVES = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".ear"}


def _source_like(name: str) -> bool:
    path = PurePosixPath(str(name).replace("\\", "/"))
    parts = [part.lower() for part in path.parts]
    if any(part in _SOURCE_DIRS for part in parts[:-1]):
        return True
    base = path.name.lower()
    if base in _SOURCE_NAMES:
        return True
    # Inspect every suffix: e.g. module.py.zip remains source-like.
    return any(suffix.lower() in _SOURCE_SUFFIXES for suffix in path.suffixes)


def _archive_magic(raw: bytes) -> str | None:
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ".zip"
    if raw.startswith(b"\x1f\x8b"):
        return ".gz"
    if raw.startswith(b"BZh"):
        return ".bz2"
    if raw.startswith(b"\xfd7zXZ\x00"):
        return ".xz"
    if raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if raw.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if len(raw) >= 262 and raw[257:262] == b"ustar":
        return ".tar"
    return None


def _decode_unambiguous(name: str, raw: bytes):
    """Decode only explicit Unicode encodings or UTF-8; never guess a legacy codec."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", "strict"), "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        return raw.decode("utf-32", "strict"), "utf-32"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", "strict"), "utf-16"
    if PurePosixPath(str(name)).suffix.lower() in {".py", ".pyi", ".pyx"}:
        first = raw.splitlines()[:2]
        for line in first:
            match = re.match(br"\s*#.*?coding[:=]\s*([-\w.]+)", line)
            if match:
                encoding = match.group(1).decode("ascii")
                return raw.decode(encoding, "strict"), encoding
    text = raw.decode("utf-8", "strict")
    if "\x00" in text:
        raise UnicodeDecodeError("utf-8", raw, raw.index(b"\x00"), raw.index(b"\x00") + 1,
                                 "NUL byte requires an explicit Unicode encoding")
    return text, "utf-8"


def classify_source(name: str, raw: bytes) -> str:
    """Return ``text``, ``archive`` or ``binary`` using conservative evidence."""
    if not isinstance(raw, bytes):
        raise TypeError("raw source must be bytes")
    source_like = _source_like(name)
    suffix = PurePosixPath(str(name).replace("\\", "/")).suffix.lower()
    # Source relevance wins over archive naming and magic, including double suffixes.
    if source_like:
        try:
            _decode_unambiguous(name, raw)
            return "text"
        except (UnicodeDecodeError, LookupError):
            return "binary"
    archive_type = _archive_magic(raw)
    if suffix in _ARCHIVES and suffix == archive_type:
        return "archive"
    if archive_type:
        return "binary"
    try:
        _decode_unambiguous(name, raw)
        return "text"
    except (UnicodeDecodeError, LookupError):
        return "binary"


def source_entries(name: str, raw: bytes) -> list[dict]:
    """Return exact indexed originals and required views for unambiguous text."""
    kind = classify_source(name, raw)
    label = "Reviewed source: " + str(name)
    entries = [{"label": label, "data": raw, "required": True}]
    if kind == "text":
        try:
            view, encoding = _decode_unambiguous(name, raw)
        except (UnicodeDecodeError, LookupError):
            return entries
        utf8_original = encoding in {"utf-8", "utf-8-sig"} and not _source_like(name) or (
            encoding in {"utf-8", "utf-8-sig"} and _source_like(name) and not _archive_magic(raw))
        if utf8_original:
            entries[0]["required"] = False
            framed = f"--- {name} (exact UTF-8 original; raw bytes indexed separately) ---\n".encode("utf-8") + raw
            if not raw.endswith((b"\n", b"\r")):
                framed += b"\n"
            framed += b"\n"
            entries.append({"label": "UTF-8 source view: " + str(name) + " (raw bytes preserved)",
                            "data": framed, "required": True})
        else:
            framed = (f"--- {name} (exact original; sha256 {hashlib.sha256(raw).hexdigest()}; "
                      f"encoding base64; {len(raw)} bytes) ---\n" +
                      base64.b64encode(raw).decode('ascii') + '\n\n' +
                      f"--- {name} (decoded view; {encoding}; exact original retained) ---\n{view}\n\n").encode("utf-8")
            entries.append({"label": "Decoded source view: " + str(name) + " (" + encoding + ")",
                            "data": framed, "required": True})
    elif kind == "archive" and not _source_like(name):
        entries[0]["required"] = False
    return entries


def git_path_value(value):
    """Decode Git's C-quoted UTF-8 paths (octal byte escapes are not JSON)."""
    if value and value.startswith('"') and value.endswith('"'):
        raw = value[1:-1].encode('utf-8')
        escapes = {b'n': b'\n', b't': b'\t', b'r': b'\r', b'b': b'\b',
                   b'f': b'\f', b'v': b'\v', b'\\': b'\\', b'"': b'"'}
        def unquote(match):
            code = match.group(1)
            return bytes([int(code, 8)]) if len(code) == 3 else escapes[code]
        raw = re.sub(br'\\([0-7]{3}|[ntrbfv\\"])', unquote, raw)
        return raw.decode('utf-8', 'strict')
    return value


def deleted_paths(diff: bytes) -> set[str]:
    """Paths whose own complete unified diff block proves deletion."""
    try:
        text = diff.decode('utf-8', 'strict')
    except UnicodeDecodeError as exc:
        raise ValueError('reviewed diff is not parseable for source deletion evidence') from exc
    blocks = []
    current = None
    for line in text.splitlines():
        if line.startswith('diff --git '):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    deleted = set()
    for block in blocks:
        old_line = git_path_value(next((x[4:] for x in block if x.startswith('--- ')), None))
        new_line = git_path_value(next((x[4:] for x in block if x.startswith('+++ ')), None))
        old = old_line[2:] if old_line and old_line.startswith('a/') else old_line
        new = new_line[2:] if new_line and new_line.startswith('b/') else new_line
        deleted_mode = any(x.startswith('deleted file mode ') for x in block)
        if old is None or new is None:
            if deleted_mode:
                raise ValueError('ambiguous reviewed diff deletion evidence')
            continue
        if deleted_mode and new == '/dev/null':
            deleted.add(old)
        elif new == '/dev/null' or (deleted_mode and new != '/dev/null'):
            raise ValueError('ambiguous reviewed diff deletion evidence')
    return deleted


def changed_sources(snapshot):
    """Return present changed originals, proving missing paths from their own diff blocks."""
    root = Path(snapshot['snapshot_dir'])
    deleted = set()
    found = []
    for rel in snapshot.get('changed_files', []):
        path = root / rel
        if path.is_symlink():
            raise ValueError('changed source is a symlink: ' + rel)
        if not path.exists():
            if not deleted:
                diff_path = snapshot.get('diff_path')
                if not diff_path:
                    raise ValueError('changed source is missing without reviewed deletion evidence: ' + rel)
                deleted = deleted_paths(Path(diff_path).read_bytes())
            if rel in deleted:
                continue
            raise ValueError('changed source is missing without explicit deletion: ' + rel)
        if not path.is_file():
            raise ValueError('changed source is not a regular file: ' + rel)
        found.append((rel, path.read_bytes()))
    return found
