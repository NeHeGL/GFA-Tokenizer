#!/usr/bin/env python3
"""Tokenize readable GFA-BASIC3 .LST listings into the .GFA binary format
the Atari ST GFA-BASIC editor saves programs in -- the reverse of the
companion GFA Detokenizer project.

Driven by the same keyword/operator tables (gfa_token_tables.json,
extracted from gfalist's tables.c) and the same file-format understanding
documented in gfa_detokenizer.py:

  offset 0-1:    general info: [type byte (0x00=SAVE)][version]
  offset 2-11:   10-byte magic "GFA-BASIC3"
  offset 12+:    38 x 4-byte big-endian "sep" pointers (sep[0] is always 0)
  pool_base = 12 + 38*4 = 164

Identifier pool: 16 groups (by sigil), each a sequence of Pascal strings.
Program listing: sequence of [2-byte size][token bytes] lines.

Unlike the detokenizer, this tool does NOT need to reconstruct or consume
indentation -- the tokenized format never stores it (the editor derives
display indentation purely from each line's lcp code on load), so leading
whitespace in the input .lst is simply stripped per line.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

try:
    import PySimpleGUI as sg
    HAS_GUI = True
except ImportError:
    HAS_GUI = False


def _resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


_tables = json.loads(_resource_path("gfa_token_tables.json").read_text())
GFALCT: list[str] = _tables["gfalct"]
GFAPFT: list[str] = _tables["gfapft"]
GFASFT: list[str] = _tables["gfasft"]

GFAVST = ["#", "$", "%", "!", "#(", "$(", "%(", "!(", "&", "|", "", "", "&(", "|(", "", "$"]
GFANCT = "0123456789ABCDEF"
GFARECL = {3: (10, 38), 4: (10, 38)}


class GfaTokenizeError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reverse lookup tables, built once from the same tables the detokenizer uses
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return s.strip()


# lct index -> lcp is `lct * 4`; build text -> lcp, preferring the FIRST
# (lowest-lcp) entry for any given normalized keyword text, since several
# lcp values intentionally share display text (e.g. two RETURN variants) --
# the lowest one is the "plain"/most general form, and the encoder picks a
# more specific one explicitly where it matters (see RETURN/FUNCTION below).
LCT_TEXT_TO_LCP: dict[str, int] = {}
for _i, _name in enumerate(GFALCT):
    key = _norm(_name)
    if not key:
        continue
    if key not in LCT_TEXT_TO_LCP:
        LCT_TEXT_TO_LCP[key] = _i * 4

# pft index -> text, for operators/keywords appearing mid-expression.
# Skip indices with special/variable-length meaning (handled explicitly).
PFT_SPECIAL = {70, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207, 208,
               215, 216, 217, 218, 219, 220, 221, 222, 223}
PFT_TEXT_TO_CODE: dict[str, int] = {}
for _i, _name in enumerate(GFAPFT):
    if _i in PFT_SPECIAL or _i >= 224:
        continue
    key = _norm(_name)
    if not key:
        continue
    if key not in PFT_TEXT_TO_CODE:
        PFT_TEXT_TO_CODE[key] = _i

SFT_TEXT_TO_CODE: dict[str, int] = {}
for _i, _name in enumerate(GFASFT):
    key = _norm(_name)
    if not key:
        continue
    if key not in SFT_TEXT_TO_CODE:
        SFT_TEXT_TO_CODE[key] = _i

# Variable-suffix text -> type index (reverse of GFAVST; several types share
# no suffix text of their own -- e.g. label/PROCEDURE/FUNCTION names are
# resolved by identifier-pool group context, not by a sigil in GFAVST).
SUFFIX_TO_TYPE: dict[str, int] = {}
for _i, _suf in enumerate(GFAVST):
    if _suf and _suf not in SUFFIX_TO_TYPE:
        SUFFIX_TO_TYPE[_suf] = _i


def push16(buf: bytearray, val: int) -> None:
    buf += struct.pack(">H", val & 0xFFFF)


def push32(buf: bytearray, val: int) -> None:
    buf += struct.pack(">i", val)


# ---------------------------------------------------------------------------
# Identifier pool
# ---------------------------------------------------------------------------

class IdentPool:
    """Tracks the 16 sigil groups' name lists as they're discovered while
    scanning source lines, assigning each a stable index within its group
    (first-seen order) -- matching gf4tp_getvar's own linear allocation.
    """

    def __init__(self) -> None:
        self.groups: list[list[str]] = [[] for _ in range(16)]
        self._index: list[dict[str, int]] = [{} for _ in range(16)]

    def get_or_add(self, type_: int, name: str) -> int:
        key = name.lower()
        idx_map = self._index[type_]
        if key in idx_map:
            return idx_map[key]
        idx = len(self.groups[type_])
        self.groups[type_].append(name)
        idx_map[key] = idx
        return idx

    def to_bytes(self) -> tuple[bytes, list[int]]:
        """Returns (pool_bytes, per_group_byte_counts)."""
        out = bytearray()
        counts = []
        for names in self.groups:
            start = len(out)
            for name in names:
                raw = name.encode("latin1", errors="replace")
                if len(raw) > 255:
                    raise GfaTokenizeError(f"identifier too long: {name!r}")
                out.append(len(raw))
                out += raw
            consumed = len(out) - start
            if consumed & 1:
                out.append(0)
            counts.append(len(out) - start)
        return bytes(out), counts


# ---------------------------------------------------------------------------
# Variable-reference parsing (name + sigil + optional array parens)
# ---------------------------------------------------------------------------

# Longest-suffix-first so "!(" is tried before "!", etc.
_SIGILS = sorted(SUFFIX_TO_TYPE.keys(), key=len, reverse=True)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def parse_var_ref(text: str, pos: int) -> tuple[int, str, bool, int] | None:
    """If a variable reference starts at pos, returns (type, name, is_array,
    new_pos) with new_pos just past the sigil (and the opening '(' if the
    sigil itself doesn't include it, e.g. plain '&' / '|' arrays). Returns
    None if no identifier starts here.
    """
    m = _NAME_RE.match(text, pos)
    if not m:
        return None
    name = m.group(0)
    p = m.end()
    # GFAVST's array-type suffixes ("#(", "&(", ...) already include the
    # opening paren, so trying longest-suffix-first here naturally prefers
    # the array form over the bare scalar form when both match.
    for suf in _SIGILS:
        if text[p : p + len(suf)] == suf:
            type_ = SUFFIX_TO_TYPE[suf]
            is_array = suf.endswith("(")
            return type_, name, is_array, p + len(suf)
    return None


# ---------------------------------------------------------------------------
# Packed-float encoding (reverse of gfa_float_to_double): the real 64-bit
# IEEE double, sign bit dropped (GFA's packed format only represents
# non-negative magnitudes -- negation is a separate unary-minus token in
# the expression stream, same asymmetry the detokenizer's own decoder
# documents), is a left-rotate-by-11 of the remaining 63 bits. Confirmed
# by bit-probing the decoder itself (set one source bit at a time, observe
# which destination bit lights up) rather than guessed from the shift
# constants alone.
# ---------------------------------------------------------------------------

_FIELD63 = (1 << 63) - 1


def double_to_gfa_float(value: float) -> bytes:
    (bits,) = struct.unpack(">Q", struct.pack(">d", abs(value)))
    field = bits & _FIELD63
    rotated = ((field << 11) | (field >> (63 - 11))) & _FIELD63
    return rotated.to_bytes(8, "big")


# ---------------------------------------------------------------------------
# Number literal parsing
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(
    r"&H[0-9A-Fa-f]+|&O[0-7]+|&X[01]+|\d+\.\d+([Ee][+-]?\d+)?|\d+[Ee][+-]?\d+|\d+"
)


def parse_number(text: str, pos: int) -> tuple[float | int, bool, int] | None:
    """Returns (value, is_float, new_pos) for a numeric literal at pos, or
    None. is_float is True for anything with a decimal point/exponent --
    everything else (including &H/&O/&X forms) is encoded as a plain
    32-bit integer.
    """
    m = _NUM_RE.match(text, pos)
    if not m:
        return None
    tok = m.group(0)
    if tok[:2] in ("&H", "&h"):
        return int(tok[2:], 16), False, m.end()
    if tok[:2] in ("&O", "&o"):
        return int(tok[2:], 8), False, m.end()
    if tok[:2] in ("&X", "&x"):
        return int(tok[2:], 2), False, m.end()
    if "." in tok or "e" in tok.lower():
        return float(tok), True, m.end()
    return int(tok), False, m.end()


# ---------------------------------------------------------------------------
# Expression / generic token-stream encoding
# ---------------------------------------------------------------------------

# Multi-character operator/keyword text, longest first, matched against
# PFT_TEXT_TO_CODE / SFT_TEXT_TO_CODE by trying progressively shorter
# candidate substrings starting at the scan position.
_MAX_PFT_WORD_LEN = max((len(k) for k in PFT_TEXT_TO_CODE), default=1)
_MAX_SFT_WORD_LEN = max((len(k) for k in SFT_TEXT_TO_CODE), default=1)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$]*\$?")


def _try_match_keyword(text: str, pos: int, table: dict[str, int], max_len: int) -> tuple[int, int] | None:
    """Longest-match a keyword/operator/symbol starting at pos against
    table. Word-shaped candidates only match on a word boundary (so 'OR'
    doesn't fire inside 'FOR'); punctuation candidates match anywhere.
    """
    best = None
    limit = min(max_len, len(text) - pos)
    for length in range(limit, 0, -1):
        cand = text[pos : pos + length]
        key = cand.upper() if cand[:1].isalpha() else cand
        if key not in table:
            continue
        if cand[:1].isalpha():
            end = pos + length
            if end < len(text) and (text[end].isalnum() or text[end] == "_"):
                continue
        best = (table[key], pos + length)
        break
    return best


def tokenize_expr(text: str, pos: int, end: int, pool: IdentPool) -> bytes:
    out = bytearray()
    while pos < end:
        c = text[pos]
        if c == " ":
            pos += 1
            continue
        if c == '"':
            close = text.find('"', pos + 1)
            if close == -1:
                close = end
            s = text[pos + 1 : close]
            raw = s.encode("latin1", errors="replace")
            out.append(222)
            out.append(len(raw))
            out += raw
            pos = close + 1
            continue
        num = parse_number(text, pos)
        if num is not None:
            value, is_float, newpos = num
            if is_float:
                out.append(219)  # packed float, decimal display
                out += double_to_gfa_float(value)
            else:
                out.append(200)
                push32(out, int(value))
            pos = newpos
            continue
        varref = parse_var_ref(text, pos)
        if varref is not None:
            type_, name, is_array, newpos = varref
            # A bare word matching a known FUNCTION/SFT name (e.g. SQR, LEN)
            # takes priority over treating it as a fresh identifier -- try
            # the keyword tables first when the "sigil" consumed was empty
            # (is_array False and newpos == end-of-word, no real sigil char).
            word_end = newpos if newpos > pos + len(name) else pos + len(name)
            bare = text[pos:word_end]
            sft_hit = SFT_TEXT_TO_CODE.get(bare.upper())
            pft_hit = PFT_TEXT_TO_CODE.get(bare.upper())
            if newpos == pos + len(name) and (sft_hit is not None or pft_hit is not None):
                if pft_hit is not None:
                    out.append(pft_hit)
                else:
                    out.append(208)
                    out.append(sft_hit)
                pos = word_end
                continue
            idx = pool.get_or_add(type_, name)
            out.append(240 + type_)
            push16(out, idx)
            pos = newpos
            continue
        kw = _try_match_keyword(text, pos, PFT_TEXT_TO_CODE, _MAX_PFT_WORD_LEN)
        if kw is not None:
            code, newpos = kw
            out.append(code)
            pos = newpos
            continue
        sft = _try_match_keyword(text, pos, SFT_TEXT_TO_CODE, _MAX_SFT_WORD_LEN)
        if sft is not None:
            code, newpos = sft
            out.append(208)
            out.append(code)
            pos = newpos
            continue
        raise GfaTokenizeError(f"can't tokenize {text[pos:pos+20]!r} at column {pos}")
    return bytes(out)


# ---------------------------------------------------------------------------
# Statement-line encoding
# ---------------------------------------------------------------------------

# Bare (no LET) scalar-assignment lcp, keyed by GFAVST type index. Confirmed
# from the detokenizer's own header decode table (lcp in (304,...): type0
# "var#=", etc.) -- these are the plain "var=expr" forms without an
# explicit LET keyword, the overwhelmingly common case in real source.
ASSIGN_LCP = {0: 304, 1: 308, 2: 312, 3: 316, 8: 320, 9: 324}

# FOR-loop header, always using the "STEP expr" variant (the most general
# of each type's 3 sub-variants -- no-step / step-literal / step-expr) --
# a plain "FOR i%=1 TO 10" with no user-written STEP is simply encoded as
# if "STEP 1" were present, which is functionally identical GFA-BASIC
# (the compiler treats them the same way) even though it isn't a byte-
# for-byte reproduction of how the real editor would encode the terser
# no-step form.
FOR_STEP_EXPR_LCP = {0: 84, 2: 96, 8: 108, 9: 120}
# NEXT var, one representative lcp per type (of that type's own 3 sub-
# variants, whose exact distinguishing condition isn't confirmed).
NEXT_LCP = {0: 124, 2: 136, 8: 148, 9: 160}

# Simple no-argument / fixed-text statements: matched directly against
# GFALCT text, no special header beyond the keyword itself.
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])=(?!=)")
_LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s*$")
_COMMENT_RE = re.compile(r"^\s*(!|REM\b)\s?(.*)$", re.IGNORECASE)
_TRAILING_COMMENT_RE = re.compile(r"(?<!&)!(?!=)(.*)$")


def _split_trailing_comment(text: str) -> tuple[str, str | None]:
    """Splits 'STATEMENT ! comment' into (statement, comment_text) --
    comment_text is None if there's no trailing '!' comment. Doesn't
    split on '!' inside a string literal.
    """
    in_str = False
    for i, c in enumerate(text):
        if c == '"':
            in_str = not in_str
        elif c == "!" and not in_str:
            lead = len(text[:i]) - len(text[:i].rstrip(" "))
            return text[:i].rstrip(" "), (len(text[:i]) - len(text[:i].rstrip(" ")), text[i + 1 :])
    return text, None


def encode_line(text: str, pool: IdentPool) -> bytes:
    """Encodes one source line's CONTENT bytes (no outer 2-byte size
    prefix -- the caller adds that). Returns b'' for a blank line (encoded
    by the caller as a bare REM, matching what real files contain for
    blank editor lines).
    """
    stripped = text.strip()
    body = stripped
    comment: tuple[int, str] | None = None
    if not (body.startswith("'") or body[:3].upper() == "REM"):
        body, comment = _split_trailing_comment(body)
        body = body.rstrip()

    out = bytearray()

    if body == "":
        if comment is not None:
            n, ctext = comment
            push16(out, 460)
            out.append(70)
            out.append(0)
            out.append(n)
            out += ctext.encode("latin1", errors="replace")
            out.append(0x0D)
            if len(out) & 1:
                out.append(0)
            return bytes(out)
        push16(out, 460)
        return bytes(out)

    if body.startswith("'") or body[:3].upper() == "REM":
        is_rem = body[:3].upper() == "REM"
        lcp = 456 if is_rem else 460
        rem_text = body[3:] if is_rem else body[1:]
        if rem_text.startswith(" "):
            rem_text = rem_text[1:]
        push16(out, lcp)
        out += rem_text.encode("latin1", errors="replace")
        out.append(0x0D)
        if len(out) & 1:
            out.append(0)
        return bytes(out)

    m = _LABEL_RE.match(body)
    if m:
        idx = pool.get_or_add(10, m.group(1))
        push16(out, 1668)  # generic bare-identifier/label statement marker
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^>\s*PROCEDURE\s+([A-Za-z_][A-Za-z0-9_.]*)\s*(\((.*)\))?\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 216)
        push16(out, idx)
        args = m.group(3)
        if args is not None and args.strip():
            out += tokenize_expr(args, 0, len(args), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^>\s*FUNCTION\s+([A-Za-z_][A-Za-z0-9_.$]*)\s*(\((.*)\))?\s*$", body, re.IGNORECASE)
    if m:
        # Unlike "> PROCEDURE " (lcp 216), which reads its name index
        # directly in the header, "> FUNCTION " (lcp 1796) consumes no
        # header bytes at all -- the function's own name is just the
        # first ordinary variable-reference token in the stream that
        # follows, same as any other name appearing in an expression.
        fname = m.group(1)
        ftype = 15 if fname.endswith("$") else 14
        push16(out, 1796)
        idx = pool.get_or_add(ftype, fname)
        out.append(240 + ftype)
        push16(out, idx)
        args = m.group(3)
        if args is not None and args.strip():
            out += tokenize_expr(args, 0, len(args), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(
        r"^FOR\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])=(.*?)\s+TO\s+(.*?)(?:\s+STEP\s+(.*))?$",
        body, re.IGNORECASE,
    )
    if m:
        name, sigil, start_expr, to_expr, step_expr = m.groups()
        type_ = SUFFIX_TO_TYPE.get(sigil)
        lcp = FOR_STEP_EXPR_LCP.get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(start_expr, 0, len(start_expr), pool)
            out.append(PFT_TEXT_TO_CODE["TO"])
            out += tokenize_expr(to_expr, 0, len(to_expr), pool)
            out.append(PFT_TEXT_TO_CODE["STEP"])
            step_text = step_expr if step_expr is not None else "1"
            out += tokenize_expr(step_text, 0, len(step_text), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(r"^NEXT(\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|]))?\s*$", body, re.IGNORECASE)
    if m and m.group(2):
        name, sigil = m.group(2), m.group(3)
        type_ = SUFFIX_TO_TYPE.get(sigil)
        lcp = NEXT_LCP.get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            out += b"\x00\x00\x00\x00"  # same navigation back-reference NEXT's own header reserves
            push16(out, idx)
            _append_comment(out, comment)
            return bytes(out)

    m = _ASSIGN_RE.match(body)
    if m and m.group(2) in SUFFIX_TO_TYPE:
        name, sigil = m.group(1), m.group(2)
        type_ = SUFFIX_TO_TYPE[sigil]
        lcp = ASSIGN_LCP.get(type_)
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            rhs = body[m.end() :]
            out += tokenize_expr(rhs, 0, len(rhs), pool)
            _append_comment(out, comment)
            return bytes(out)

    kw_lcp = _match_leading_keyword(body)
    if kw_lcp is not None:
        lcp, rest_start = kw_lcp
        push16(out, lcp)
        if lcp in HEADER_SKIP4_LCP:
            out += b"\x00\x00\x00\x00"
        rest = body[rest_start:]
        if rest.strip():
            out += tokenize_expr(rest, 0, len(rest), pool)
        _append_comment(out, comment)
        return bytes(out)

    # Fallback: treat the whole line as a generic expression/call statement
    # (covers bare '@proc(...)' calls once '@' is consumed as a plain PFT
    # token, and other constructs not given a dedicated header above).
    push16(out, 1668)
    out += tokenize_expr(body, 0, len(body), pool)
    _append_comment(out, comment)
    return bytes(out)


def _append_comment(out: bytearray, comment: tuple[int, str] | None) -> None:
    if comment is None:
        # No trailing padding here: a plain statement line has no
        # terminator of its own, so render_line's generic token loop
        # (`while pos < len(raw)`) would misread any padding byte we
        # added as a real token (pft=0 decodes as a bare 'AND', not a
        # no-op) -- unlike the comment path below, whose own explicit
        # 0x0D terminator lets the decoder skip a parity byte by
        # advancing `pos` past it without ever treating it as a token.
        return
    n, ctext = comment
    out.append(70)
    if len(out) & 1:
        out.append(0)
    out.append(min(n, 255))
    out += ctext.encode("latin1", errors="replace")
    out.append(0x0D)
    if len(out) & 1:
        out.append(0)


# lcp values whose header reserves 4 extra bytes right after the lcp
# field itself, confirmed directly from the detokenizer's own decode
# (`elif lcp in (4, 12, ..., 224): pos += 4`) -- almost certainly an
# editor-navigation back-reference (e.g. IF's own offset to its matching
# ENDIF, for the editor's brace-jump/fold features) that the compiler
# itself doesn't need, so zero-filling it is safe for a file that only
# needs to load and compile correctly.
HEADER_SKIP4_LCP = {4, 12, 16, 20, 32, 48, 56, 60, 64, 172, 176, 196, 200, 204, 208, 220, 224}

# Simple keyword -> lcp for statement types whose header is just the
# keyword itself (no operand encoding beyond the generic expression that
# may follow) -- built from GFALCT text where the lowest lcp sharing that
# text is the plain/general-purpose form.
_SIMPLE_KEYWORDS = {
    "DO": 0, "LOOP": 4, "REPEAT": 8, "UNTIL": 12, "WHILE": 16, "WEND": 20,
    "RETURN": 28, "IF": 32, "ENDIF": 36, "ENDFUNC": 44,
    "SELECT": 48, "ENDSELECT": 52, "ELSE": 56, "CASE": 224,
    "EXIT IF": 172, "LOCAL": 212, "PRINT": 588,
}


def _match_leading_keyword(body: str) -> tuple[int, int] | None:
    upper = body.upper()
    for kw in sorted(_SIMPLE_KEYWORDS, key=len, reverse=True):
        if upper == kw or upper.startswith(kw + " ") or upper.startswith(kw + "("):
            return _SIMPLE_KEYWORDS[kw], len(kw)
    return None


# ---------------------------------------------------------------------------
# Top-level file assembly
# ---------------------------------------------------------------------------

END_OF_PROGRAM_LCP = 180
MAGIC = b"GFA-BASIC3"


def tokenize_source(text: str) -> bytes:
    pool = IdentPool()
    lines = text.splitlines()
    encoded: list[bytes] = []
    for line in lines:
        try:
            content = encode_line(line, pool)
        except GfaTokenizeError as exc:
            raise GfaTokenizeError(f"line {len(encoded) + 1}: {exc}") from exc
        encoded.append(content)
    sentinel = struct.pack(">H", END_OF_PROGRAM_LCP)
    encoded.append(sentinel)

    listing = bytearray()
    for content in encoded:
        push16(listing, len(content) + 2)
        listing += content

    pool_bytes, group_byte_counts = pool.to_bytes()
    group_entry_counts = [len(names) for names in pool.groups]

    # The full sep[] array is ONE monotonically-increasing sequence of
    # cumulative "boundary" markers -- not several independent sub-tables
    # each starting back at zero -- so they must be computed in strict
    # left-to-right order:
    #   sep[0..16]  = running BYTE offset through each pool group
    #   sep[17..18] = editor bookkeeping (unused here, held at sep[16])
    #   sep[19]     = listing END offset (sep[16] + len(listing) bytes) --
    #                 doubles as split_listing_lines' `sep[19]-sep[16]`
    #                 listing-length term AND as the zero-point the
    #                 group-count table below continues counting from
    #   sep[20..35] = sep[19] plus a running total of 4*ENTRY_COUNT[i] --
    #                 an entry tally scaled by 4, NOT a byte length (that's
    #                 what sep[0..16] is for) -- matching parse_identifier_
    #                 pool's own `(sep[20+i] - sep[19+i]) // 4` extraction,
    #                 where the divide-by-4 recovers a plain entry count
    #   sep[35..37] = trailing variable-value storage area (left empty,
    #                 held at the same final offset)
    sep = [0] * 38
    running = 0
    for i in range(16):
        sep[i] = running
        running += group_byte_counts[i]
    sep[16] = running
    sep[17] = sep[16]
    sep[18] = sep[16]
    listing_end = sep[16] + len(listing)
    sep[19] = listing_end
    running_count = listing_end
    for i in range(16):
        running_count += 4 * group_entry_counts[i]
        sep[20 + i] = running_count
    sep[36] = running_count
    sep[37] = running_count

    out = bytearray()
    out.append(0x00)  # SAVE (not PSAVE)
    out.append(4)  # format version (3.5+ era layout)
    out += MAGIC
    for v in sep:
        push32(out, v)
    out += pool_bytes
    out += listing
    return bytes(out)


def tokenize_file(src: Path, dest: Path) -> int:
    text = src.read_text(encoding="latin1")
    data = tokenize_source(text)
    dest.write_bytes(data)
    return len(text.splitlines())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert readable GFA-BASIC .lst source into tokenized .gfa"
    )
    parser.add_argument("input", type=Path, nargs="?", help="Plain-text .lst source")
    parser.add_argument("-o", "--output", type=Path, help="Output file (default: <name>.gfa)")
    args = parser.parse_args(argv)

    if not args.input:
        parser.print_help()
        return 1

    output = args.output or args.input.with_suffix(".gfa")
    try:
        line_count = tokenize_file(args.input, output)
    except GfaTokenizeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Source  : {args.input} ({line_count} lines)")
    print(f"Output  : {output} ({output.stat().st_size:,} bytes)")
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

GFA_THEME = {
    "BACKGROUND": "#20262E",
    "TEXT": "#E8EAED",
    "INPUT": "#FFFFFF",
    "TEXT_INPUT": "#1C2430",
    "SCROLL": "#20262E",
    "BUTTON": ("#FFFFFF", "#3B5170"),
    "PROGRESS": ("#000000", "#000000"),
    "BORDER": 1,
    "SLIDER_DEPTH": 0,
    "PROGRESS_DEPTH": 0,
}
ACCENT_COLOR = "#6FB1E8"
MUTED_COLOR = "#8A93A0"
INFO_TEXT = "#3D9E99"


def run_gui() -> None:
    if not HAS_GUI:
        raise SystemExit("PySimpleGUI not installed; use CLI mode.")

    sg.theme_add_new("GFADark", GFA_THEME)
    sg.theme("GFADark")

    layout = [
        [sg.Text("GFA Tokenizer", font=("Helvetica", 16, "bold"))],
        [sg.Text("Convert readable GFA-BASIC .lst source into tokenized .gfa",
                 font=("Helvetica", 10), text_color=ACCENT_COLOR)],
        [sg.Text("")],
        [
            sg.Text("Source:", size=(8, 1)),
            sg.Input(key="-SRC-", enable_events=True, size=(45, 1),
                     disabled=True, use_readonly_for_disable=False),
            sg.FileBrowse(file_types=(("GFA-BASIC listing", "*.lst;*.LST"),)),
        ],
        [sg.Text("", size=(8, 1)), sg.Text("-", key="-INFO-", size=(58, 1),
                                            font=("Helvetica", 9), text_color=INFO_TEXT)],
        [
            sg.Text("Output:", size=(8, 1)),
            sg.Input(key="-DEST-", size=(45, 1)),
            sg.Button("Convert", key="-CONVERT-", disabled=True),
        ],
        [sg.Text("", size=(8, 1)), sg.Text("-", key="-STATUS-", size=(58, 1),
                                            font=("Helvetica", 9), text_color=INFO_TEXT)],
        [sg.Text("")],
        [sg.Button("Exit"), sg.Push(), sg.Text("by Jeff Molofee (NeHe)", font=("Helvetica", 8), text_color=MUTED_COLOR)],
    ]
    icon_path = _resource_path("icon.ico")
    window = sg.Window(
        "GFA Tokenizer", layout, finalize=True,
        icon=str(icon_path) if icon_path.exists() else None,
    )
    src_full_path = None

    while True:
        event, values = window.read()
        if event in (sg.WIN_CLOSED, "Exit"):
            break

        if event == "-SRC-" and values["-SRC-"]:
            src_full_path = Path(values["-SRC-"])
            try:
                text = src_full_path.read_text(encoding="latin1")
                dest_full_path = src_full_path.with_suffix(".gfa")
                window["-SRC-"].update(src_full_path.name)
                window["-INFO-"].update(f"{len(text.splitlines())} lines  |  {src_full_path.stat().st_size:,} bytes")
                window["-DEST-"].update(dest_full_path.name)
                window["-CONVERT-"].update(disabled=False)
                window["-STATUS-"].update("")
            except Exception as exc:
                window["-INFO-"].update(f"Can't read file: {exc}")
                window["-DEST-"].update("")
                window["-CONVERT-"].update(disabled=True)

        if event == "-CONVERT-" and src_full_path:
            dest_name = values["-DEST-"].strip()
            if not dest_name:
                sg.popup_error("Output filename can't be empty.")
                continue
            dest_full_path = src_full_path.with_name(dest_name)
            try:
                line_count = tokenize_file(src_full_path, dest_full_path)
                window["-DEST-"].update(dest_full_path.name)
                window["-STATUS-"].update(f"DONE - {line_count} lines written to {dest_full_path.name}")
            except Exception as exc:
                sg.popup_error(str(exc))

    window.close()


def _attach_console_if_cli() -> None:
    if sys.platform != "win32" or len(sys.argv) <= 1:
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")
    except Exception:
        pass


def main() -> int:
    _attach_console_if_cli()
    if len(sys.argv) > 1:
        return run_cli()
    if HAS_GUI:
        run_gui()
        return 0
    print("Usage: gfa_tokenizer.py source.lst [-o out.gfa]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
