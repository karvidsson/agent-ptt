"""Make message text sound right when spoken.

Agents paste file paths, links and IDs into their messages. Read aloud they are
noise ("slash Users slash krille slash dot claude …"), so before synthesis each
one becomes a short spoken stand-in: ``/Users/k/brief.md`` → "a Markdown file",
``https://…`` → "a link", a UUID → "an ID". Only the speech changes: the
transcript, history and every displayed copy keep the original text.

``spoken_text`` is applied in ``tts.synthesize_speech``, the single path every
playback goes through (channel TTS worker, workspace speech, CLI preview).
"""

from __future__ import annotations

import re

# Extension (lower case, no dot) → spoken type, without the article.
FILE_TYPES: dict[str, str] = {}


def _types(name: str, *extensions: str) -> None:
    for extension in extensions:
        FILE_TYPES[extension] = name


_types("Markdown file", "md", "markdown", "mdx")
_types("PDF", "pdf")
_types("Python file", "py", "pyi", "pyc")
_types("notebook", "ipynb")
_types("JavaScript file", "js", "mjs", "cjs", "jsx")
_types("TypeScript file", "ts", "tsx", "mts", "cts")
_types("JSON file", "json", "jsonl", "ndjson")
_types("YAML file", "yaml", "yml")
_types("TOML file", "toml")
_types("config file", "ini", "cfg", "conf", "env", "plist")
_types("XML file", "xml")
_types("HTML file", "html", "htm")
_types("CSS file", "css", "scss", "sass", "less")
_types("image", "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "heic", "tif", "tiff")
_types("audio file", "mp3", "wav", "ogg", "m4a", "flac", "aac", "opus")
_types("video", "mp4", "mov", "webm", "mkv", "avi")
_types("text file", "txt", "rst")
_types("log file", "log")
_types("CSV file", "csv", "tsv")
_types("spreadsheet", "xlsx", "xls", "ods", "numbers")
_types("Word document", "docx", "doc", "odt", "rtf")
_types("presentation", "pptx", "ppt", "key", "odp")
_types("shell script", "sh", "bash", "zsh", "fish", "ps1")
_types("database file", "db", "sqlite", "sqlite3")
_types("SQL file", "sql")
_types("archive", "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar")
_types("lock file", "lock")
_types("diff", "diff", "patch")
_types("Rust file", "rs")
_types("Go file", "go")
_types("Java file", "java")
_types("Kotlin file", "kt", "kts")
_types("Swift file", "swift")
_types("C file", "c", "h")
_types("C++ file", "cpp", "cc", "hpp", "cxx")
_types("C sharp file", "cs")
_types("Ruby file", "rb")
_types("PHP file", "php")
_types("Lua file", "lua")
_types("Dart file", "dart")
_types("Vue file", "vue")
_types("Svelte file", "svelte")
_types("design file", "pen", "fig", "sketch")

# Absolute folders that are clearly on disk even without an extension
# (``/reap`` or ``/projects/x/work-items`` are API routes and stay as they are).
_DISK_ROOTS = (
    "/Users/",
    "/home/",
    "/tmp/",
    "/private/",
    "/var/",
    "/etc/",
    "/opt/",
    "/usr/",
    "/Volumes/",
    "/mnt/",
    "/srv/",
    "/Applications/",
    "/Library/",
)

_SEG = r"[\w.@+-]+"  # one path segment: no spaces, braces or quotes
_EXT = r"\.([A-Za-z0-9]{1,10})"
_LINE = r"(?::\d+(?::\d+)?)?"  # path.py:12 or path.py:12:3
_LEAD = r"(?<![\w/\\.:~@-])"  # a path starts at a word boundary, not mid-token

URL_RE = re.compile(r"\b(?:https?|ftp|wss?)://[^\s<>()\[\]\"'`]+", re.IGNORECASE)
FILE_URL_RE = re.compile(r"\bfile://(/[^\s<>()\[\]\"'`]*)", re.IGNORECASE)
WINDOWS_RE = re.compile(_LEAD + r"[A-Za-z]:\\(?:[^\s\\<>\"'`|?*]+\\?)*")
# Absolute (/…), home (~/…, ~user/…) and dot-relative (./…, ../…) paths.
ROOTED_RE = re.compile(
    _LEAD + r"(?:~[\w.-]*|\.\.?)?/(?:" + _SEG + r"/)*(?:" + _SEG + r")?/?" + _LINE
)
# Relative paths with a slash: only taken when the last segment has a known
# extension or the path ends in a slash, so "and/or", "1/2" and "TCP/IP" stay.
RELATIVE_RE = re.compile(_LEAD + r"(?:" + _SEG + r"/)+" + r"(?:" + _SEG + r")?/?" + _LINE)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Long hex (git SHAs, digests): 16+ characters with at least one digit and one
# letter, so plain numbers and words stay. Short 7-character SHAs are kept.
HASH_RE = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{16,}\b")
_TICKS = re.compile(r"`([^`\n]*)`")


def _article(noun: str) -> str:
    return ("an " if noun[0].lower() in "aeio" else "a ") + noun


def _strip_line(path: str) -> str:
    return re.sub(r":\d+(?::\d+)?$", "", path)


def describe_path(path: str) -> str | None:
    """The spoken stand-in for a file or folder path, or None to leave it.

    ``path`` is a path as written (``/a/b.md``, ``~/x/``, ``C:\\x\\y.pdf``).
    """
    path = _strip_line(path.rstrip(".,;:!?"))
    if path.endswith(("/", "\\")):
        return "a folder"
    name = re.split(r"[/\\]", path)[-1]
    match = re.search(_EXT + "$", name)
    if match:  # ".env" alone counts too: a dotfile is still a file
        return _article(FILE_TYPES.get(match.group(1).lower(), "file"))
    return None


def _cut_short(match: re.Match) -> bool:
    """True when the path runs on into something it can't hold (``/p/{id}``, ``a/**``).

    That is a route template or a glob, not a file: say it as written.
    """
    rest = match.string[match.end() : match.end() + 1]
    return bool(rest) and not rest.isspace() and rest not in ",;:!?)]}'\"`.>"


def _rooted(match: re.Match) -> str:
    raw = match.group(0)
    if _cut_short(match):
        return raw
    trail = ""
    while raw and raw[-1] in ".,;:!?" and not re.search(r":\d+$", raw):
        trail = raw[-1] + trail
        raw = raw[:-1]
    if raw in {"/", "~", "./", "../", "~/"} or raw.count("/") == 0:
        return match.group(0)
    spoken = describe_path(raw)
    if spoken is None:
        home = raw.startswith(("~", "./", "../"))
        if home or raw.startswith(_DISK_ROOTS):
            spoken = "a folder"
        else:
            return match.group(0)  # an API route like /projects/x/work-items
    return spoken + trail


def _relative(match: re.Match) -> str:
    raw = match.group(0)
    if _cut_short(match):
        return raw
    trail = ""
    while raw and raw[-1] in ".,;:!?" and not re.search(r":\d+$", raw):
        trail = raw[-1] + trail
        raw = raw[:-1]
    if raw.endswith("/") and raw.count("/") >= 2:
        return "a folder" + trail
    name = _strip_line(raw).rsplit("/", 1)[-1]
    extension = re.search(_EXT + "$", name)
    if extension and extension.group(1).lower() in FILE_TYPES and name != extension.group(0):
        return _article(FILE_TYPES[extension.group(1).lower()]) + trail
    return match.group(0)


def _windows(match: re.Match) -> str:
    raw = match.group(0)
    trail = ""
    while raw and raw[-1] in ".,;:!?":
        trail = raw[-1] + trail
        raw = raw[:-1]
    return (describe_path(raw) or "a folder") + trail


def _file_url(match: re.Match) -> str:
    raw = match.group(1)
    trail = ""
    while raw and raw[-1] in ".,;:!?":
        trail = raw[-1] + trail
        raw = raw[:-1]
    return (describe_path(raw) or "a folder") + trail


def _url(match: re.Match) -> str:
    raw = match.group(0)
    trail = ""
    while raw and raw[-1] in ".,;:!?":
        trail = raw[-1] + trail
        raw = raw[:-1]
    return "a link" + trail


def _unticked(match: re.Match) -> str:
    inner = match.group(1)
    replaced = _replace(inner)
    # Drop the backticks only when the whole span turned into a stand-in.
    return replaced if replaced != inner and _is_stand_in(replaced) else match.group(0)


def _is_stand_in(text: str) -> bool:
    return bool(re.fullmatch(r"(?:an? [A-Za-z+ ]+|a link|an ID|a hash)[.,;:!?]*", text))


def _replace(text: str) -> str:
    text = FILE_URL_RE.sub(_file_url, text)
    text = URL_RE.sub(_url, text)
    text = WINDOWS_RE.sub(_windows, text)
    text = ROOTED_RE.sub(_rooted, text)
    text = RELATIVE_RE.sub(_relative, text)
    text = UUID_RE.sub("an ID", text)
    return HASH_RE.sub("a hash", text)


def spoken_text(text: str) -> str:
    """``text`` with paths, links and long IDs replaced by short spoken stand-ins."""
    if not text:
        return text
    original = text
    text = _replace(_TICKS.sub(_unticked, text))
    if text == original:
        return text
    # "the ~/x/y.env file" → "the a config file file" → "a config file"
    text = re.sub(r"\b(?:the|this|that|your|my|our) (an? )\b", r"\1", text, flags=re.I)
    return re.sub(r"\b(file|folder|link|image|PDF|document|script) (?:file|folder)\b", r"\1", text)
