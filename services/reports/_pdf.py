"""A deterministic, portable, Unicode-capable PDF writer for report exports.

Built on `fpdf2 <https://pypi.org/project/fpdf2/>`_ with an **embedded open-license Unicode
font** (GNU Unifont, bundled under ``assets/``). A brief whose text is Cyrillic, CJK or Arabic
now renders real glyphs and its text extracts back to the exact original code points -- not the
``?`` the earlier WinAnsi/base-14 writer produced for every script outside Western European.

Two properties are load-bearing and are preserved from the previous hand-rolled writer:

* **Deterministic bytes.** The same document renders byte-identically every time. A *fixed*
  ``/CreationDate`` is set, so no wall-clock time is written; the trailer ``/ID`` is fpdf2's
  content hash over that fixed-metadata buffer (never randomness); the ``/Producer`` string is
  pinned; fpdf2 loads the font with ``recalcTimestamp=False`` and subsets it from the used-glyph
  set alone, so the embedded font program is a pure function of the text; and page content
  streams are left uncompressed, so the bytes never depend on a zlib level and stay greppable in
  tests. (The embedded font subset is Flate-compressed by fpdf2; that is deterministic for a
  given runtime.)
* **Safe text.** With an embedded font, drawn text is emitted as hex glyph ids inside ``TJ``
  arrays -- never as a literal PDF ``( ... )`` string -- so no field value can terminate a
  string and forge PDF structure. Control/format characters are neutralised to spaces before
  layout, and only ``http(s)`` URLs ever become clickable link annotations.

Font asset: :data:`_FONT_PATH` -- GNU Unifont, dual-licensed under the SIL Open Font License
v1.1 and the GNU GPL v2+ with the Font Embedding Exception (see ``assets/LICENSE-unifont.txt``).
The pinned upstream version and SHA-256 are recorded in :data:`_FONT_UPSTREAM` /
:data:`_FONT_SHA256`; the file is read from disk only -- there is no network access at any time.
Arabic is rendered in logical (visual-order) form without contextual shaping or RTL reordering,
because no text-shaping engine is taken on: the code points are retained exactly, but joined
forms and right-to-left ordering are a documented visual-layout limitation.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

#: The bundled Unicode font, read from disk only (no download, no system-font dependency).
_FONT_PATH = Path(__file__).parent / "assets" / "unifont-14.0.04.ttf"
_FONT_FAMILY = "unifont"
#: Pinned upstream provenance of the bundled font asset -- verified before bundling.
_FONT_UPSTREAM = (
    "GNU Unifont 14.0.04 (https://ftp.gnu.org/gnu/unifont/unifont-14.0.04/unifont-14.0.04.ttf)"
)
#: SHA-256 of the bundled ``assets/unifont-14.0.04.ttf`` (== the upstream file's digest).
_FONT_SHA256 = "211d1ddddc566511db254c746d83df77b4a1b9b2549cdffe0a3e901e46eefea8"

# US Letter, 72pt = 1 inch. A one-inch margin on every side.
_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_MARGIN = 72
_USABLE_WIDTH = _PAGE_WIDTH - 2 * _MARGIN

_BODY_SIZE = 10.0
_HEADING_SIZE = 13.0
_TITLE_SIZE = 18.0
_LINE_SPACING = 1.35
#: Hanging/whole-block indents (points) for bullets and their source-URL lines.
_BULLET_INDENT = 12.0
_LINK_INDENT = 24.0

#: A fixed, timezone-aware creation date. It makes ``/CreationDate`` constant and feeds fpdf2's
#: content-hash ``/ID``, so identical input yields byte-identical output. Never wall-clock.
_FIXED_CREATION_DATE = datetime(2001, 1, 1, tzinfo=UTC)
_PRODUCER = "news-intelligence-platform daily-brief export"

#: Only these URI schemes become clickable link annotations; anything else is drawn as inert
#: text, so a ``javascript:``/``data:`` URL can never become an actionable PDF link.
_SAFE_URL_PREFIXES = ("http://", "https://")


def is_safe_url(url: str) -> bool:
    """True only for an ``http(s)`` URL that may become a clickable link annotation."""
    return url.startswith(_SAFE_URL_PREFIXES)


def _neutralise(text: str) -> str:
    """Replace every control/format character (and any exotic whitespace) with a space.

    A literal ``\\n`` survives, so a section body's paragraph breaks become hard line breaks;
    every other C0/C1 or Unicode ``C*`` character -- which could otherwise reach a metadata
    string -- collapses to a space. Printable glyphs of every script pass through untouched.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\n")
        elif ch.isspace() or unicodedata.category(ch).startswith("C"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class _Block:
    """One content block: a kind (``title``/``heading``/``paragraph``/``bullet``/``link``/
    ``spacer``) and its already-neutralised text."""

    kind: str
    text: str


class PdfDocument:
    """Accumulate headings/paragraphs/bullets/links, then render deterministic PDF bytes.

    The document is built by calling the content methods in order; :meth:`render` lays the blocks
    out top-to-bottom in a single column, wrapping on real glyph metrics and starting a new page
    whenever a line would cross the bottom margin.
    """

    def __init__(self, *, title: str) -> None:
        self._title = _neutralise(title)
        self._blocks: list[_Block] = []

    # -- content ------------------------------------------------------------------------

    def title(self, text: str) -> None:
        self._blocks.append(_Block("title", _neutralise(text)))
        self.spacer()

    def heading(self, text: str) -> None:
        self.spacer()
        self._blocks.append(_Block("heading", _neutralise(text)))

    def paragraph(self, text: str) -> None:
        self._blocks.append(_Block("paragraph", _neutralise(text)))
        self.spacer()

    def bullet(self, text: str) -> None:
        """A ``- `` prefixed line; wrapped continuation lines hang under the text, not the dash."""
        self._blocks.append(_Block("bullet", _neutralise(text)))

    def link(self, url: str) -> None:
        """An indented line for a source URL, clickable only if it is an ``http(s)`` URL."""
        self._blocks.append(_Block("link", _neutralise(url)))

    def spacer(self) -> None:
        self._blocks.append(_Block("spacer", ""))

    # -- serialisation ------------------------------------------------------------------

    def render(self) -> bytes:
        pdf = FPDF(orientation="portrait", unit="pt", format=(_PAGE_WIDTH, _PAGE_HEIGHT))
        pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
        pdf.set_auto_page_break(auto=True, margin=_MARGIN)
        # Uncompressed content streams -> no zlib-level dependence, greppable in tests.
        pdf.set_compression(False)
        # Fixed metadata -> stable /CreationDate and the content-hash /ID both stay deterministic.
        pdf.set_creation_date(_FIXED_CREATION_DATE)
        pdf.set_producer(_PRODUCER)
        pdf.set_title(self._title)
        pdf.add_font(_FONT_FAMILY, "", str(_FONT_PATH))
        pdf.add_page()

        for block in self._blocks:
            if block.kind == "title":
                self._write_line(pdf, block.text, _TITLE_SIZE)
            elif block.kind == "heading":
                self._write_line(pdf, block.text, _HEADING_SIZE)
            elif block.kind == "paragraph":
                self._write_line(pdf, block.text, _BODY_SIZE)
            elif block.kind == "bullet":
                self._write_line(pdf, "- " + block.text, _BODY_SIZE, indent=_BULLET_INDENT)
            elif block.kind == "link":
                safe = block.text if is_safe_url(block.text) else ""
                self._write_line(pdf, block.text, _BODY_SIZE, indent=_LINK_INDENT, link=safe)
            else:  # spacer
                pdf.ln(_BODY_SIZE * _LINE_SPACING)

        # Emit at least PDF 1.4 (as the previous writer did), keeping any higher version a
        # feature such as a link annotation may already have required.
        pdf.pdf_version = max(pdf.pdf_version, "1.4")
        return bytes(pdf.output())

    def _write_line(
        self, pdf: FPDF, text: str, size: float, *, indent: float = 0.0, link: str = ""
    ) -> None:
        """Lay out one wrapped block at ``size``, optionally hung under a left ``indent``.

        An empty block still advances the cursor by one line, so a blank paragraph keeps its gap.
        """
        pdf.set_font(_FONT_FAMILY, "", size)
        height = size * _LINE_SPACING
        if not text:
            pdf.ln(height)
            return
        left = _MARGIN + indent
        pdf.set_left_margin(left)
        pdf.set_x(left)
        pdf.multi_cell(
            w=_USABLE_WIDTH - indent,
            h=height,
            text=text,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            link=link,
        )
        pdf.set_left_margin(_MARGIN)
