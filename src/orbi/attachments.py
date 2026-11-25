#!/usr/bin/env python3
"""The files a person attaches to a draft (Issue #2364).

One list per draft: the round document and the creation document carry the
same 'attachments: [{name, src}]', where 'src' is a local path or an
http(s) address and the type follows from the extension. This module owns
everything that list means:

- the bounds a person can act on: 10 files, 10 MB each, and all HTML and text
  of one draft together at most 50,000 characters, so they still fit in the
  new Issue's body (65,536) beside the request itself;
- staging: every file is copied or downloaded into the session directory as
  '.pi-session/attachments/<n>.<ext>', never under its own name, so a name
  carrying '../' cannot leave the directory;
- what the session gets: the ONE context line naming the files and the
  '@<path>' arguments — an image only when the run's model declares image
  input (Pi sends no image otherwise, orbi.model_catalog), the HTML and text
  only for the tool-less fidelity pass, because the drafting session reads
  them with 'read';
- what a NEW Issue gets: the Attachments section after the draft marker, a
  local image uploaded through 'gh issue create --attach' and an http(s) one
  written as its own address and never requested, so a creation does not
  depend on that address being reachable (Issue #2374).

The extension is the only type signal. Every other type is refused with its
reason, and so is a file over a bound: the person reads why, in one sentence.
"""
from __future__ import annotations

import contextlib
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
TEXT_SUFFIXES = frozenset(
    {".htm", ".html", ".txt", ".md", ".csv", ".tsv", ".json", ".log"}
)
MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARACTERS = 50_000
DOWNLOAD_TIMEOUT_SECONDS = 60
# The staged files live in the session directory's own subdirectory, so a
# caller accounting the session (docs/public-contracts.mdx) finds them there.
DIRECTORY = "attachments"
SECTION_HEADING = "## Attachments"
# The ONE downloader every attachment source uses (Issues #2377, #2390):
# orbi.build and beta.orbi.build answer urllib's default
# `Python-urllib/3.x` User-Agent with HTTP 403 and Orbi's own value with
# 200 (measured 2026-11-23), so `orbi draft` and a delivery share it.
USER_AGENT = "orbi"
DOWNLOAD_TIMEOUT_SECONDS = 60
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_MAGIC = (
    (b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"),
    (b"GIF8", ".gif"), (b"RIFF", ".webp"),
)
_TICK_RUN_RE = re.compile("[\u0060]+")
_ATTACHMENTS_HEADING_RE = re.compile(
    r"^##[ \t]+Attachments[ \t]*$", re.MULTILINE,
)
# One block of a written section: an image line, or a folded text file.
_BLOCK_RE = re.compile(
    r"^!\[[^\]]*\]\([^)]*\)[ \t]*$|<details>.*?</details>",
    re.MULTILINE | re.DOTALL,
)


class AttachmentError(RuntimeError):
    """One file, or the list itself, the engine refuses; str() is the reason."""


@dataclass(frozen=True)
class Attachment:
    """One entry of the draft's attachments list."""

    name: str
    src: str


@dataclass(frozen=True)
class StagedFile:
    """One attachment as it sits in the session or the filing directory."""

    name: str          # the generated name, '<n>.<ext>'
    source: str        # the person's own name
    image: bool
    path: Path
    address: str = ""  # the http(s) src of a downloaded file


@dataclass(frozen=True)
class Attached:
    """The files one draft carries, and whether its model can view images."""

    files: tuple[StagedFile, ...] = ()
    viewable: bool = False

    def naming(self) -> str:
        """The ONE context line naming every file, or "" with none.

        '<n>.<ext> = <the person's name>' pairs, then where the files are and
        what the session must do with them. An image of a session whose model
        declares no image input is named as not viewable: it is not sent.
        """
        if not self.files:
            return ""
        pairs = ", ".join(
            f"{file.name} = {file.source}" for file in self.files
        )
        notes = [f"they are in {self.files[0].path.parent}"]
        if any(not file.image for file in self.files):
            notes.append("read the HTML and text files with read")
        line = f"The person attached: {pairs} ({'; '.join(notes)})"
        unreadable = [
            file.name for file in self.files
            if file.image and not self.viewable
        ]
        if unreadable:
            line += (
                "\nThis session's model declares no image input, so "
                f"{', '.join(unreadable)} cannot be viewed: ask the person "
                "to describe it when it matters."
            )
        return line

    def arguments(self, *, text: bool = False) -> list[str]:
        """The Pi '@<path>' arguments this session receives.

        A viewable image and, with 'text', every HTML and text file travel as
        file arguments: Pi expands a text file into the first prompt, which is
        the only way the tool-less fidelity pass reads one.
        """
        return [
            f"@{file.path}" for file in self.files
            if (file.image and self.viewable) or (text and not file.image)
        ]


@dataclass(frozen=True)
class Filing:
    """The attachments one creation writes into every NEW Issue."""

    directory: Path
    files: tuple[StagedFile, ...] = ()

    def select(self, numbers: "Sequence[int] | None") -> "Filing":
        """This filing restricted to the numbers one Issue uses.

        Each drafted Issue carries 'attachments: [n]' (absent = all of
        them), so a split writes only what each Issue uses; a number the
        draft does not have is refused with the reason.
        """
        return Filing(
            directory=self.directory,
            files=_chosen(self.files, numbers, "draft"),
        )

    def arguments(self) -> list[str]:
        """'gh issue create --attach' for every image with a local src."""
        return [
            item for file in self.files
            if file.image and not file.address
            for item in ("--attach", f"./{file.name}")
        ]

    def section(self) -> str:
        """The Attachments section: the images, then each text file folded."""
        return _render(tuple(
            _image_block(file) if file.image else _text_block(file)
            for file in self.files
        ))

    def unuploaded(self, body: object) -> tuple[str, ...]:
        """The local images gh did not upload, by the person's own name.

        'gh issue create --attach' rewrites '![name](./<n>.<ext>)' to the
        uploaded asset; a reference still written as './<n>.<ext>' is the
        image gh refused — it creates the Issue with the rest and exits
        non-zero.
        """
        text = body if isinstance(body, str) else ""
        return tuple(
            file.source for file in self.files
            if file.image and not file.address
            and f"](./{file.name})" in text
        )


@dataclass(frozen=True)
class Copied:
    """The Attachments of an existing Issue, copied into a child.

    The '/orbi split' outcome does not upload the draft's files: a child
    carries the links the original Issue already has, restricted to the
    numbers that child uses (Issue #2364).
    """

    blocks: tuple[str, ...] = ()

    def select(self, numbers: "Sequence[int] | None") -> "Copied":
        """These blocks restricted to the numbers one child uses."""
        return Copied(_chosen(self.blocks, numbers, "original Issue"))

    def section(self) -> str:
        """The child's own Attachments section, or "" with none."""
        return _render(self.blocks)


def copied(body: object) -> Copied:
    """The attachment blocks of one existing Issue body, in order."""
    text = body if isinstance(body, str) else ""
    heading = _ATTACHMENTS_HEADING_RE.search(text)
    if heading is None:
        return Copied()
    return Copied(tuple(
        match.group(0).strip()
        for match in _BLOCK_RE.finditer(text[heading.end():])
    ))


def section(source: "Filing | Copied | None") -> str:
    """The Attachments section one new Issue carries, or "" with no file."""
    return "" if source is None else source.section()


def source(
    numbers: "Sequence[int] | None", position: int, *,
    filed: "Filing | None", copied: "Copied | None",
) -> "Filing | Copied | None":
    """The attachments ONE drafted Issue uses, or None.

    A new-Issue creation selects from the draft's staged files; a split
    selects the links of the original Issue's body; no source and no
    selection keeps the exact pre-#2364 create.
    """
    if filed is not None:
        return filed.select(numbers)
    if copied is not None:
        return copied.select(numbers)
    if numbers is not None:
        raise AttachmentError(
            f"Issue {position}: attachments names files this creation does "
            "not have"
        )
    return None


def parse(value: object) -> tuple[Attachment, ...]:
    """The document's attachments list; an unusable entry is refused."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AttachmentError("attachments must be a list of {name, src}")
    entries: list[Attachment] = []
    for position, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise AttachmentError(f"attachment {position} is not an object")
        name = entry.get("name")
        src = entry.get("src")
        if not isinstance(name, str) or not name.strip():
            raise AttachmentError(f"attachment {position} has no name")
        if not isinstance(src, str) or not src.strip():
            raise AttachmentError(f"attachment {position} has no src")
        entries.append(Attachment(name=name.strip(), src=src.strip()))
    return tuple(entries)


def from_paths(paths: Sequence[str]) -> list[dict]:
    """The '--attach <path>' arguments as document entries.

    The person's own file name is the display name and the src is the path
    as the person gave it ('~' expanded), so the local draft shows the file
    where they see it; the same process reads it moments later.
    """
    return [
        {"name": Path(path).name, "src": str(Path(path).expanduser())}
        for path in paths
    ]


def with_paths(payload: dict, paths: Sequence[str]) -> dict:
    """One document plus the '--attach <path>' files, after its own list.

    The document (a caller such as Orbi Cloud fills it) and the flag are the
    same list (Issue #2364): both become {name, src} entries the round
    stages, and a refusal names the file either way.
    """
    if not paths:
        return payload
    entries = payload.get("attachments")
    if entries is None:
        entries = []
    elif not isinstance(entries, list):
        entries = [entries]
    return {**payload, "attachments": [*entries, *from_paths(paths)]}


def stage(value: object, destination: Path, *, viewable: bool) -> Attached:
    """Stage one draft's attachments; a refusal is a person-readable reason."""
    return Attached(
        files=_staged(parse(value), Path(destination)), viewable=viewable,
    )


@contextlib.contextmanager
def filing(value: object) -> Iterator[Filing | None]:
    """Stage one creation's attachments; nothing is left behind.

    None when the draft carries no file, so a draft without attachments keeps
    the exact pre-#2364 creation: no section, no '--attach', no download.
    """
    entries = parse(value)
    if not entries:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="orbi-filing-") as directory:
        yield Filing(
            directory=Path(directory),
            files=_staged(entries, Path(directory), download=False),
        )


def with_arguments(command: list[str], arguments: Sequence[str]) -> list[str]:
    """One argv with 'arguments' in front of its LAST element.

    The context file is every role's last argument and stays last
    (tests/test_pi_command.py pins it); Pi's '@<path>' file form is how an
    image or a text file enters the first prompt.
    """
    if not arguments:
        return command
    return [*command[:-1], *arguments, command[-1]]


def upload_notice(names: Sequence[str]) -> str:
    """The one line naming the images gh did not upload, and the repair."""
    listed = ", ".join(names)
    them = "it" if len(names) == 1 else "them"
    return (
        f"The Issue was created without {listed}: gh did not upload {them}. "
        f"Paste {them} into the Issue, or run 'gh auth login' as yourself and "
        "draft again."
    )


def _positions(
    count: int, numbers: "Sequence[int] | None", source: str,
) -> list[int]:
    """The 1-based positions one number list selects, in its own order."""
    if numbers is None:
        return list(range(1, count + 1))
    chosen = []
    for number in numbers:
        if not 1 <= number <= count:
            raise AttachmentError(
                f"attachment {number} does not exist: the {source} has "
                f"{count}"
            )
        chosen.append(number)
    return chosen


def _chosen(
    files: tuple, numbers: "Sequence[int] | None", source: str,
) -> tuple:
    """The files one number list selects, in the list's own order."""
    return tuple(files[number - 1] for number in _positions(
        len(files), numbers, source,
    ))


def _render(blocks: tuple[str, ...]) -> str:
    """One Attachments section from its blocks, or "" with none."""
    if not blocks:
        return ""
    return "\n\n".join([SECTION_HEADING, "", "\n\n".join(blocks)])


def _staged(
    entries: tuple[Attachment, ...], destination: Path, *, download: bool = True,
) -> tuple[StagedFile, ...]:
    """Validate every entry and write it under its generated name.

    With 'download' false an http(s) image is only recorded with its own
    address: a new Issue writes it as a link, so reading it would make the
    creation depend on that address being reachable (Issue #2374). Drafting
    rounds keep downloading it, because the session receives the file.
    """
    if len(entries) > MAX_FILES:
        raise AttachmentError(
            f"at most {MAX_FILES} files per draft, got {len(entries)}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    staged: list[StagedFile] = []
    characters = 0
    for position, entry in enumerate(entries, start=1):
        suffix, image = _kind(entry.name)
        if image and not download and _URL_RE.match(entry.src):
            name = f"{position}{suffix}"
            staged.append(StagedFile(
                name=name, source=entry.name, image=True,
                path=destination / name, address=entry.src,
            ))
            continue
        data, address = _read(entry)
        megabytes = MAX_FILE_BYTES // (1024 * 1024)
        if len(data) > MAX_FILE_BYTES:
            raise AttachmentError(f"{entry.name} is larger than {megabytes} MB")
        if image:
            try:
                suffix = image_suffix(data)
            except ValueError:
                raise AttachmentError(
                    f"{entry.name}: the file's bytes are not an image"
                ) from None
        else:
            characters += len(data.decode("utf-8", errors="replace"))
            if characters > MAX_TEXT_CHARACTERS:
                raise AttachmentError(
                    "the HTML and text files carry more than "
                    f"{MAX_TEXT_CHARACTERS} characters together"
                )
        path = destination / f"{position}{suffix}"
        path.write_bytes(data)
        staged.append(StagedFile(
            name=path.name, source=entry.name, image=image, path=path,
            address=address,
        ))
    return tuple(staged)


def staged_name(position: int, name: str) -> str:
    """The generated name of one entry: its number plus its own suffix.

    The ONE naming rule: staging writes '<n>.<ext>' and the local draft shows
    the same name, so the number a person reads is the number the created
    Issue references (Issue #2364).
    """
    return f"{position}{_kind(name)[0]}"


def display(
    entries: "Sequence[Attachment]", numbers: "Sequence[int] | None",
) -> list[str]:
    """The files one drafted Issue uses, as the local draft lists them.

    '<n>.<ext>  <the person's name>  <src>' per file (Issue #2364): the name
    the created Issue references, the name the person gave it and where it
    sits.
    """
    return [
        f"{staged_name(position, entries[position - 1].name)}  "
        f"{entries[position - 1].name}  {entries[position - 1].src}"
        for position in _positions(len(entries), numbers, "draft")
    ]


def _kind(name: str) -> tuple[str, bool]:
    """One entry's generated suffix and whether it is an image."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return suffix, True
    if suffix in TEXT_SUFFIXES:
        return suffix, False
    raise AttachmentError(
        f"{name}: unsupported file type {suffix or '(none)'} — attach an "
        f"image ({', '.join(sorted(IMAGE_SUFFIXES))}), HTML or text "
        f"({', '.join(sorted(TEXT_SUFFIXES))})"
    )


def download(address: str) -> bytes:
    """One http(s) attachment's bytes, through the ONE downloader.

    The request names `orbi` instead of urllib, because an orbi.build
    address answers the default User-Agent with HTTP 403 (Issue #2377).
    """
    request = urllib.request.Request(
        address, headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(
        request, timeout=DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        return response.read()


def image_suffix(data: bytes) -> str:
    """One downloaded file's image suffix, from its own magic bytes.

    Anything else is not an image: `orbi draft` refuses it, and a delivery
    counts it as unreadable rather than guessing a type from the name
    (Issues #2364, #2390).
    """
    for magic, suffix in _MAGIC:
        if data.startswith(magic):
            return suffix
    raise ValueError("not an image")


def _read(entry: Attachment) -> tuple[bytes, str]:
    """One entry's bytes and, for a downloaded file, its address."""
    if _URL_RE.match(entry.src):
        try:
            return download(entry.src), entry.src
        except Exception as exc:  # noqa: BLE001 - reported with its reason
            raise AttachmentError(
                f"{entry.name}: could not be downloaded from {entry.src}: "
                f"{type(exc).__name__}"
            ) from None
    try:
        return Path(entry.src).expanduser().read_bytes(), ""
    except OSError as exc:
        raise AttachmentError(
            f"{entry.name}: cannot be read from {entry.src}: "
            f"{exc.strerror or type(exc).__name__}"
        ) from None


def _image_block(file: StagedFile) -> str:
    """One image: its uploaded reference, or its own address."""
    return f"![{file.source}]({file.address or f'./{file.name}'})"


def _text_block(file: StagedFile) -> str:
    """One HTML or text file as a collapsed block a reader opens."""
    text = file.path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    fence = "\u0060" * max(3, _longest_run(text) + 1)
    language = Path(file.name).suffix.lstrip(".")
    return "\n".join((
        f"<details><summary>{file.source}</summary>", "",
        f"{fence}{language}", text, fence, "", "</details>",
    ))


def _longest_run(text: str) -> int:
    """The length of the longest run of fence characters in one text."""
    return max((len(run) for run in _TICK_RUN_RE.findall(text)), default=0)
