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

# GFA-BASIC uses a DIFFERENT opcode for the numeric and string forms of
# '+' (concatenation) and every comparison operator, even though both
# read identically in source -- GFAPFT lists each of these texts twice
# (e.g. '+' at both 6 and 28), and PFT_TEXT_TO_CODE's own "first
# occurrence wins" dedup above always keeps the lower (numeric) index.
# That silently made every STRING '+'/comparison wrong until now.
#
# Confirmed via a real GFA-BASIC -s debug compile: 'c$=a$+b$+g$'
# tokenized with the numeric '+' (code 6, PFT_TEXT_TO_CODE's default)
# failed to load in the real editor (error 65535), while an otherwise
# byte-identical hand-typed program (using the real editor's own
# tokenizer) used code 28 instead. The eight comparison operators show
# the exact same "listed twice, 8 slots apart" shape in GFAPFT (12-19
# numeric, 20-27 string) -- included here by structural symmetry with
# the confirmed '+' pair, not yet independently Level-4 confirmed the
# way '+' itself now is (see the companion GFA Decompiler project's
# test.md for the verification-level convention). If a future compile
# ever shows a string comparison behaving differently, re-check this
# table first.
AMBIGUOUS_OP_CODES: dict[str, tuple[int, int]] = {
    "+": (6, 28),
    "<>": (12, 20),
    "<=": (13, 21),
    "=<": (14, 22),
    ">=": (15, 23),
    "=>": (16, 24),
    "<": (17, 25),
    ">": (18, 26),
    "=": (19, 27),
}

# GFAVST type indices whose values are strings (scalar '$' and string
# array '$(') -- used to track whether the operand immediately before
# an ambiguous operator was string-typed. Index 15 (GFAVST's other '$'
# entry) is deliberately excluded: SUFFIX_TO_TYPE's own first-occurrence
# dedup means a plain '$' suffix always resolves to type 1, so 15 is
# never actually returned by parse_var_ref.
STRING_VST_TYPES = {1, 5}

# Word-shaped operators that DON'T produce a value themselves -- used so
# a '-' right after one of these (e.g. 'a AND -b') is still recognized
# as unary, not binary. Every other alphabetic keyword match (a builtin
# function call, a bare constant like 'PI', etc.) is assumed to produce
# a value.
WORD_OPERATORS = {"AND", "OR", "XOR", "IMP", "EQV", "MOD", "DIV", "NOT"}

# LEFT$(/RIGHT$( also each list two PFT codes for the identical display
# text (58/59, 60/61) -- unlike '+'/the comparisons above, this isn't a
# numeric-vs-string distinction (both codes are for the same read-only
# string-returning function). Confirmed via a real GFA-BASIC -s debug
# compile (RTLIBTS2): 'd$=LEFT$(c$,3)' compiles to code 59 (0x3b), never
# PFT_TEXT_TO_CODE's default (58, the first/lower occurrence found by
# its dedup) -- and 'h$=RIGHT$(c$,3)' likewise uses 61, not 60. What
# triggers the OTHER member of each pair isn't known yet (not exercised
# by any test program so far) -- MID$( has the identical duplicate
# shape (62/63) but is deliberately left alone here since there's no
# ground truth yet for which of its two codes a plain read use needs.
PFT_CODE_OVERRIDE: dict[str, int] = {
    "LEFT$(": 59,
    "RIGHT$(": 61,
}

# Full inventory of every OTHER GFAPFT display-text collision, found by
# scanning the whole table after the '+'/comparison/LEFT$/RIGHT$ bugs
# above turned out to all share the same root cause (PFT_TEXT_TO_CODE's
# first-occurrence dedup silently picking one of several real, distinct
# opcodes that happen to render the same text). None of these are fixed
# yet -- there's no ground truth for what triggers the second (or
# third) member of each pair/triple, the way a real -s debug compile
# gave us for the ones above -- but they're the same shape of risk and
# should be the first place to look if a generated .gfa using any of
# these ever fails to load again:
#   '-' at 5 (binary) vs 30 (unary) -- CONFIRMED via real ground truth
#     already in this repo (gb36test_archive/hell.gfa's own compiled
#     bytes for 'UNTIL token&=-1'/'UNTIL z&=-1', not a guess): unary
#     minus isn't just a different opcode, it's a DIFFERENT ENCODING
#     for its operand too. 'token&=-1' compiles to [...=19][30=0x1e]
#     [0xdd][0x80][8-byte packed float encoding +1.0, decodable with
#     this file's own gfa_float_to_double] -- i.e. the operand is
#     stored as its ABSOLUTE VALUE in a packed-float form (pft 221, not
#     219's plain packed-float or the 200-207 integer forms), and the
#     0x1e opcode supplies the negation. double_to_gfa_float(1.0)'s own
#     8-byte output ('00 00 00 00 00 00 03 ff') matches bytes 2-9 of
#     that blob exactly except a 0x80 vs 0x00 first byte -- likely a
#     sign/exponent flag this project's own encoder never sets, not yet
#     understood. NOT fixed: this is a bigger, separate feature (a
#     unary-minus code path with its own literal-encoding rules) than a
#     simple opcode swap, and only 2 examples (both integer -1) are
#     confirmed -- not enough to know if non-literal operands or
#     non-int values behave the same way. Flagging clearly rather than
#     guessing further; see the companion GFA Decompiler project's
#     memory (project_gfa_pft_duplicate_codes) for the investigation.
#   ')' at 32, 51 -- '(' at 35, 157 -- ',' at 33, 156 (plain punctuation!)
#   '=' at 19, 27, 69 (a third '=' beyond the numeric/string comparison pair)
#   'AT(' at 89, 122; 'INPUT$(' at 94, 95; 'ROUND(' at 112, 113
#   'BIN$(' at 115, 116; 'MIN(' at 117, 118; 'MAX(' at 119, 120
#   'STRING$(' at 129, 130; 'STR$(' at 190, 191, 192 (three-way!)
#   'HEX$(' at 193, 194; 'OCT$(' at 195, 196
# All of these currently fall through to PFT_TEXT_TO_CODE's plain
# first-occurrence default. Both of this project's own real -s test
# programs (RTLIBTST, RTLIBTS2) exercise plain '(' /')'/',' constantly
# and matched a real hand-typed compile byte-for-byte using that
# default, so the lower code is at least correct for ordinary
# function-call/grouping use -- whatever triggers the alternate code
# for these remains unknown.

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
    # Masked to the raw 32-bit pattern rather than packed signed: GFA's
    # own compiler has the same behavior for hex/octal/binary literals
    # whose value needs the top bit (e.g. &HFFFFFFFF) -- it stores the
    # bit pattern, and its OWN detokenizer reads it back as a signed int,
    # rendering "&H-1" instead of "&HFFFFFFFF" (confirmed directly
    # against ground truth: default5.gfa's own bytes for exactly this
    # case). Masking here just avoids a struct.error for the same values
    # the real compiler already round-trips this same lossy way.
    buf += struct.pack(">I", val & 0xFFFFFFFF)


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
                # Real GFA-BASIC always stores pool identifiers upper-
                # case, regardless of how the user typed them -- this
                # project's own Detokenizer already lowercases on read
                # to compensate (_to_lower_ascii), which is why this
                # asymmetry went unnoticed until compared directly
                # against a real editor-saved file byte-for-byte.
                raw = name.upper().encode("latin1", errors="replace")
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


def parse_number(text: str, pos: int) -> tuple[float | int, bool, int, int] | None:
    """Returns (value, is_float, new_pos, base) for a numeric literal at
    pos, or None. is_float is True for anything with a decimal point/
    exponent (always base 10). base is 10/16/8/2 for a plain/&H/&O/&X
    integer literal -- callers use it to pick the matching pft code
    (200=decimal, 202=hex, 204=octal, 206=binary) so e.g. '&H1F2F3F4F'
    round-trips back to hex, not a decoded decimal value.
    """
    m = _NUM_RE.match(text, pos)
    if not m:
        return None
    tok = m.group(0)
    if tok[:2] in ("&H", "&h"):
        return int(tok[2:], 16), False, m.end(), 16
    if tok[:2] in ("&O", "&o"):
        return int(tok[2:], 8), False, m.end(), 8
    if tok[:2] in ("&X", "&x"):
        return int(tok[2:], 2), False, m.end(), 2
    if "." in tok or "e" in tok.lower():
        return float(tok), True, m.end(), 10
    return int(tok), False, m.end(), 10


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
        # Word-boundary check only makes sense when the candidate's own
        # last character is itself alphanumeric (a "word" ending, like
        # "OR") -- many SFT/function-name entries end in a literal '('
        # (e.g. "ADD(", baked into the token text since the function
        # always needs one), and checking the boundary there would
        # wrongly reject "ADD(1,1)" just because '1' is alnum.
        if cand[-1:].isalnum():
            end = pos + length
            if end < len(text) and (text[end].isalnum() or text[end] == "_"):
                continue
        best = (table[key], pos + length)
        break
    return best


def tokenize_expr(text: str, pos: int, end: int, pool: IdentPool, array_open: bool = False) -> bytes:
    out = bytearray()
    # Tracks whether the most recently emitted atom (literal, var-ref, or
    # builtin-function call) was string-typed -- used to pick the right
    # code for an ambiguous operator ('+' or a comparison, see
    # AMBIGUOUS_OP_CODES) immediately after it. Not reset by punctuation
    # (',', '(', ')') so a parenthesized/argument-list boundary doesn't
    # lose track of the enclosing expression's own type.
    last_was_string = False
    # Which filler-byte VALUE the next odd+filler numeric literal (see
    # array_open below) should use -- True for a plain 0x00, False for
    # the pair's own even code instead. Defaults True to match every
    # existing array_open=True caller (ARRAY_ASSIGN_LCP's index,
    # FOR's start value), both genuinely array/dimension-index-shaped
    # literals like DIM's own confirmed 0x00 case. Only the new
    # comma-after-a-string-argument trigger below (LEFT$(c$,3)'s "3")
    # sets this False, confirmed via RTLIBTS2's own 'c9 c8' bytes ('LET
    # i%=1' and hex/octal/binary literals reuse the even code too, per
    # the odd/filler branch's own docstring, but aren't re-armed through
    # this array_open mechanism at all, so they don't need tracking here).
    zero_filler = True
    # True right after any value-producing atom (string/numeric literal,
    # var-ref, function call, or a closing ')' or '$'-suffixed builtin) --
    # False right after an operator, '(', ',', or at the very start.
    # Used only to tell a unary '-' (e.g. 'z%=-1', '-x%') apart from a
    # binary one (e.g. 'x%-1') -- GFA-BASIC uses a different opcode (30,
    # not 5) and a different operand encoding for the unary form, see
    # pending_unary_minus below.
    just_saw_value = False
    # Set for exactly one iteration right after emitting a unary minus
    # (opcode 30, see the '-' handling below): the very next numeric
    # literal -- of ANY type, integer or float -- gets encoded as a
    # packed float (pft 221) holding its ABSOLUTE value, immediately
    # preceded by one extra byte (0x80, meaning not yet understood) --
    # completely different from this literal's normal encoding (the
    # 200-207 integer forms, or 219's plain packed float). Confirmed via
    # gb36test_archive/hell.gfa's own 'token&=-1'/'z&=-1': both compile
    # to '[opcode 30][0xdd][0x80][8-byte packed float of +1.0]', where
    # the trailing 8 bytes match this file's own double_to_gfa_float(1.0)
    # exactly. Only integer '-1' is confirmed this way -- applying the
    # same shape to a unary-minus float literal too is this project's
    # best guess pending a real compile to confirm it, not itself
    # independently ground-truth-checked yet.
    pending_unary_minus = False
    # Set for exactly one iteration right after emitting a BINARY
    # arithmetic operator ('+', '-', '*', '/' with a real value before
    # it -- string '+' concatenation excluded, see its own check below):
    # the very next plain base-10 literal (integer OR float) is encoded
    # as pft 223 -- 8 bytes (double_to_gfa_float of the value, first byte
    # OR'd with 0x80), with NO extra marker byte -- instead of its normal
    # form. Confirmed 2026-08-25 via a real hand-typed-and-compiled
    # 'y%=x%-1': its '1' operand is 'df 80 00 00 00 00 00 03 ff', which is
    # pft 223 followed by double_to_gfa_float(1.0) ('00 00 00 00 00 00 03
    # ff') with its own first byte OR'd -- NOT the plain integer form
    # (200/201) this project had always used for every bare literal
    # until now. Only confirmed for '-' so far; '+'/'*'/'/' are a
    # reasoned generalization (same runtime arithmetic stack, no reason
    # GFA would special-case the operator symbol here) pending their own
    # direct confirmation.
    just_saw_binary_arith_op = False
    # True once any real (non-whitespace) token has been consumed --
    # used only to tell whether a literal is the very FIRST token of this
    # tokenize_expr call (see the odd+filler-vs-plain literal choice
    # below). Confirmed 2026-08-25 via 'z%=0-x%' (the safe rewrite for
    # 'z%=-x%'): the leading '0' -- the first token of the RHS expression,
    # but not the entire RHS by itself (so the bare-assignment shortcut
    # above doesn't intercept it) -- needs the same odd+filler form as a
    # bare 'x%=5' does, not the plain form this project previously
    # defaulted every non-array-context literal to.
    seen_any_token = False
    # Set right after emitting an array-reference token ('name(' --
    # already includes the '(' as part of its sigil, see resolve_var);
    # consumed (and cleared) by the very next numeric literal, which
    # then gets the special odd/filler form below instead of the plain
    # one every literal after it in the same dimension/argument list
    # uses. Confirmed against the real compiler's own bundled test
    # archive (gb36test_archive/hell.gfa): 'DIM var$(16,1024),p%(38)'
    # -- 16 (right after 'var$(') gets the odd form, 1024 (after a bare
    # ',', no new array-ref) gets the plain one, then 38 (right after
    # the new 'p%(') gets the odd form again. Cleared on any other
    # token too (not just a literal), so an index expression that isn't
    # a bare literal doesn't leave this sitting stale for something
    # unrelated later in the same call.
    #
    # The `array_open` parameter lets a caller seed this as already-True
    # for the first token: ARRAY_ASSIGN_LCP/LET_ARRAY_ASSIGN_LCP's index
    # expression (e.g. 'a$(2)=...') never sees a var-ref token of its
    # own here -- the array's name+type is already encoded directly in
    # the statement header -- so without this, its first index literal
    # would wrongly get the plain form. Confirmed against
    # gb36test_archive/default.gfa's own 'LET i$(1)="AA"', which matches
    # this project's tokenizer output byte-for-byte once seeded this way.
    while pos < end:
        c = text[pos]
        # Consumed by this iteration's branches below (the numeric-
        # literal one specifically); cleared for every OTHER kind of
        # token automatically, and re-armed only by the var-ref branch
        # further down when it just emitted a fresh array-reference.
        was_array_open, array_open = array_open, False
        was_zero_filler, zero_filler = zero_filler, True
        was_pending_unary_minus, pending_unary_minus = pending_unary_minus, False
        was_binary_arith_op, just_saw_binary_arith_op = just_saw_binary_arith_op, False
        if c == " ":
            pos += 1
            continue
        was_first_token, seen_any_token = not seen_any_token, True
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
            last_was_string = True
            just_saw_value = True
            continue
        num = parse_number(text, pos)
        if num is not None:
            value, is_float, newpos, base = num
            if was_binary_arith_op and base == 10:
                # See just_saw_binary_arith_op's own docstring above:
                # a plain literal (integer or float) right after a
                # BINARY arithmetic operator uses pft 223 -- the packed
                # float of the value with its own first byte OR'd with
                # 0x80, and no separate marker byte at all (unlike pft
                # 221 below, which always has one). Only base-10 is
                # confirmed; &H/&O/&X literals in this position fall
                # through to the older, separately-unconfirmed forms
                # below rather than guessing at this shape for them too.
                float_bytes = bytearray(double_to_gfa_float(value))
                float_bytes[0] |= 0x80
                out.append(223)
                out += float_bytes
            elif was_pending_unary_minus or is_float:
                # pft 219 (a plain packed float with no sign byte) is
                # NEVER actually used by the real compiler for a bare
                # literal -- confirmed 2026-08-25 via TESTVEX.GFA, a real
                # hand-typed-and-compiled 'v!=3.5' (no unary minus at
                # all), whose bytes are '[opcode 221][marker 0x00][8-byte
                # packed float, first byte OR'd with 0x80]', not
                # '[opcode 219][plain packed float]' the way this file
                # used to encode it. So EVERY float literal -- negated or
                # not -- uses pft 221: the payload is always the packed
                # float of the ABSOLUTE value with its own first byte's
                # top bit forced set, preceded by one marker byte that's
                # 0x80 when a unary minus produced this literal (the
                # value's actual sign) and 0x00 otherwise. A plain
                # (non-float) integer right after a unary minus
                # (confirmed only via gb36test_archive/hell.gfa's own
                # 'token&=-1'/'z&=-1') reuses this exact same shape too
                # (marker 0x80, packed float of the integer's absolute
                # value) -- unary-minus + a non-float, non-base-10-
                # integer literal (hex/octal/binary) has no ground truth
                # either way yet, so that case still falls through to the
                # separate '0 - operand' rewrite below instead of
                # guessing at this shape for it too.
                float_bytes = bytearray(double_to_gfa_float(abs(value)))
                float_bytes[0] |= 0x80
                out.append(221)
                out.append(0x80 if was_pending_unary_minus else 0x00)
                out += float_bytes
            elif was_first_token and not was_array_open:
                # The very first token of this tokenize_expr call, but
                # NOT the entire expression by itself (a bare 'x%=5'
                # takes the separate _try_bare_int_literal_rhs shortcut
                # above tokenize_expr entirely and never reaches here) --
                # e.g. the leading '0' in the '0 - operand' rewrite's
                # '0-x%'. Confirmed 2026-08-25 via 'z%=0-x%': needs the
                # same odd+filler form (own even code as filler) as a
                # bare whole-RHS literal does, not the plain form used
                # for a later literal in the same list/call (see
                # was_array_open's own comment below).
                even = {10: 200, 16: 202, 8: 204, 2: 206}[base]
                out.append(even + 1)
                out.append(even)
                push32(out, int(value))
            elif not was_array_open:
                # Plain form: no filler byte. Used for every numeric
                # literal except the first one right after an array
                # reference opens (see was_array_open's own comment
                # above and the odd/filler branch just below), and
                # except the very first token of the whole expression
                # (see was_first_token's own branch just above).
                out.append({10: 200, 16: 202, 8: 204, 2: 206}[base])
                push32(out, int(value))
            else:
                # Base determines the *pair* of pft codes (200/201=
                # decimal, 202/203=hex, 204/205=octal, 206/207=binary --
                # preserves the literal's original notation on round-trip,
                # e.g. &H1F2F3F4F stays hex instead of decoding to a
                # decimal value). Real GFA-BASIC always emits the ODD
                # code of the pair, immediately followed by one filler
                # byte (value irrelevant -- the decoder's own 'if pft in
                # (201,203,205,207): pos += 1' unconditionally skips it
                # without ever reading it back), then the 4-byte number.
                # Confirmed against the real compiler's own bundled test
                # archive (gb36test_archive/default.gfa): 'DIM a$(5)'
                # uses a zero filler byte ('c9 00 00000005'), while 'LET
                # i%=1' and the &H/&O/&X literal forms use each pair's
                # own even code as filler instead ('c9 c8 00000001', 'cb
                # ca ...', etc.). The filler's value truly doesn't matter
                # to THIS tool's own decoder (the real one's own
                # unconditional 'pos += 1' skip never reads it back
                # either) -- but the real editor's LOAD validation is
                # strict about matching its own tokenizer byte-for-byte,
                # so getting the VALUE right matters for that, even
                # though it's functionally inert. zero_filler (see its
                # own docstring above) tracks which of the two a given
                # odd+filler trigger needs -- confirmed a second time via
                # RTLIBTS2's own 'LEFT$(c$,3)'/'RIGHT$(c$,3)' count
                # argument, which needs the even-code form, not 0x00.
                even = {10: 200, 16: 202, 8: 204, 2: 206}[base]
                out.append(even + 1)
                out.append(0 if was_zero_filler else even)
                push32(out, int(value))
            pos = newpos
            last_was_string = False
            just_saw_value = True
            continue
        # Try a variable/array reference AND both keyword tables, then take
        # whichever match consumes the MOST text, with keywords winning a
        # tie. A reserved word always wins a tie because the real compiler
        # doesn't allow a user identifier to shadow one -- and several
        # builtins collide exactly with the array-reference sigils here:
        # every "NAME$(" string function (STR$(, CHR$(, OCT$(, MID$(, ...)
        # parses identically to a fresh string-array reference "NAME" +
        # "$(" sigil, and SHL&(/SHR&(/etc. collide the same way with the
        # "&(" integer-array sigil. Checking var-ref first and only
        # falling back to keywords for a bare, sigil-less word (as this
        # code used to) silently turned every one of those builtins into
        # a bogus same-named user array on first use.
        varref = parse_var_ref(text, pos)
        kw = _try_match_keyword(text, pos, PFT_TEXT_TO_CODE, _MAX_PFT_WORD_LEN)
        sft = _try_match_keyword(text, pos, SFT_TEXT_TO_CODE, _MAX_SFT_WORD_LEN)
        var_len = (varref[3] - pos) if varref is not None else -1
        kw_len = (kw[1] - pos) if kw is not None else -1
        sft_len = (sft[1] - pos) if sft is not None else -1
        best = max(var_len, kw_len, sft_len)
        if best == -1:
            pass
        elif kw_len == best:
            code, newpos = kw
            matched = text[pos:newpos]
            op_key = matched.upper() if matched[:1].isalpha() else matched
            if op_key == "-" and not just_saw_value:
                # Unary minus (nothing value-shaped precedes it: start of
                # the expression, or right after another operator/'('/
                # ','). Opcode 30's packed-float operand encoding (see
                # pending_unary_minus' own docstring) is confirmed ONLY
                # for an immediately-following plain INTEGER literal
                # ('z%=-1') -- a first version of this fix applied it to
                # ANY unary '-' (variable operands like '-x%', float
                # literals like '-3.5' too) and the real editor rejected
                # both of those ("3 bombs" on load/compile, confirmed by
                # the user against UNARYTST.GFA). For anything else,
                # rewrite as '0 - operand' instead: a plain 0 literal
                # (already-confirmed encoding) plus the already-confirmed
                # BINARY minus, letting the operand tokenize completely
                # normally right after -- semantically identical, and
                # doesn't require guessing at another unconfirmed byte
                # shape the way extending the packed-float trick would.
                peek = parse_number(text, newpos)
                if peek is not None and not peek[1] and peek[3] == 10:
                    # Confirmed only for a plain base-10 integer literal
                    # ('z%=-1') -- &H/&O/&X literals fall through to the
                    # '0 - operand' rewrite below, same as float/variable/
                    # function-call operands, since there's no evidence
                    # either way for those bases specifically.
                    out.append(30)
                    pending_unary_minus = True
                    pos = newpos
                else:
                    out.append(200)  # plain decimal 0
                    push32(out, 0)
                    out.append(5)  # binary minus
                    just_saw_value = True
                    # This rewrite ends in a real binary minus (opcode
                    # 5), same as any other -- so a literal right after
                    # it (e.g. the '3.5' in 'v!=-3.5' -> '0-3.5') needs
                    # the same pft-223 treatment as 'y%=x%-1''s '1' does.
                    just_saw_binary_arith_op = True
                    pos = newpos
                continue
            if op_key in AMBIGUOUS_OP_CODES:
                # See AMBIGUOUS_OP_CODES' own comment: pick the numeric
                # or string-typed opcode based on what the operand right
                # before this operator was, instead of PFT_TEXT_TO_CODE's
                # fixed (always-numeric) choice.
                num_code, str_code = AMBIGUOUS_OP_CODES[op_key]
                out.append(str_code if last_was_string else num_code)
                just_saw_value = False
                if op_key == "+" and not last_was_string:
                    just_saw_binary_arith_op = True
            else:
                out.append(PFT_CODE_OVERRIDE.get(matched.upper(), code))
                if matched[:1].isalpha():
                    # Any other keyword match (a builtin function call,
                    # AND/OR/MOD/DIV/NOT, etc.) -- string-typed only if
                    # its own text ends in '$' (LEFT$(/DATE$/TIME$/...),
                    # matching the real editor's own suffix convention
                    # for string-returning builtins; everything else
                    # (INSTR(/LEN(/ASC(/AND/...) returns numeric. Only a
                    # real value-producing match (not a word operator
                    # like AND/OR/MOD/DIV/NOT) counts as "just saw a
                    # value" for the next '-'/unary-minus check.
                    last_was_string = matched.rstrip("(").endswith("$")
                    just_saw_value = op_key not in WORD_OPERATORS
                elif matched == "," and last_was_string:
                    # Re-arm the odd+filler literal form (see array_open's
                    # own docstring above) for the numeric argument right
                    # after this comma -- confirmed via RTLIBTS2's own
                    # 'LEFT$(c$,3)'/'RIGHT$(c$,3)': the count argument
                    # needs the odd+filler form same as an array index
                    # does, but only because the PRECEDING argument was
                    # string-typed. Doesn't fire for DIM's own multi-
                    # dimension commas ('DIM var$(16,1024)') since those
                    # sit between two NUMERIC dimension sizes -- already
                    # confirmed ground truth that only the first one gets
                    # the odd form there (see tokenize_expr's own opening
                    # docstring) -- so gating on last_was_string here
                    # keeps that case untouched.
                    array_open = True
                    zero_filler = False
                    just_saw_value = False
                elif matched == ")":
                    # A closing paren completes a value (grouped
                    # expression or function-call result) -- a '-' right
                    # after it is binary, not unary (e.g. 'FRE(0)-1').
                    just_saw_value = True
                elif matched in ("-", "*", "/"):
                    # Binary arithmetic (this '-' already fell through
                    # the unary-minus branch above, so just_saw_value was
                    # True right before it -- genuinely binary). See
                    # just_saw_binary_arith_op's own docstring above.
                    just_saw_value = False
                    just_saw_binary_arith_op = True
                else:
                    # Any other punctuation ('(', ';', etc.) or operator
                    # symbol -- not a value; last_was_string untouched (an
                    # argument/paren boundary shouldn't lose track of the
                    # enclosing expression's own type).
                    just_saw_value = False
            pos = newpos
            continue
        elif sft_len == best:
            code, newpos = sft
            out.append(208)
            out.append(code)
            matched = text[pos:newpos]
            last_was_string = matched.rstrip("(").upper().endswith("$")
            just_saw_value = True
            pos = newpos
            continue
        else:
            type_, name, is_array, newpos = varref
            idx = pool.get_or_add(type_, name)
            last_was_string = type_ in STRING_VST_TYPES
            just_saw_value = True
            # Real GFA-BASIC uses the byte-sized var-index form
            # (pft 224-239) whenever the pool index fits in a byte,
            # falling back to the word-sized form (240-255, this
            # function's previous unconditional choice) only once it
            # doesn't -- confirmed against a Hatari-compiled test
            # program in the companion GFA Decompiler project, whose
            # 'a$(2)'/'b$(1,1)' references both used the byte form.
            # Always emitting the word form still decodes fine through
            # this project's own detokenizer, but isn't what the real
            # editor itself ever produces.
            if idx < 256:
                out.append(224 + type_)
                out.append(idx)
            else:
                out.append(240 + type_)
                push16(out, idx)
            pos = newpos
            if is_array:
                array_open = True
                zero_filler = True
            continue
        # Last resort: a bare identifier with no sigil and no keyword
        # match is a LABEL reference (GOTO/GOSUB/ON...GOTO targets --
        # labels have no sigil of their own, so parse_var_ref never
        # matches them).
        wm = _NAME_RE.match(text, pos)
        if wm:
            label_name = wm.group(0)
            idx = pool.get_or_add(10, label_name)
            # Same byte-form-when-it-fits choice as the array/variable
            # var-ref case above -- confirmed needed here too against a
            # real GFA-BASIC editor's own tokenized 'RESTORE lbl'.
            if idx < 256:
                out.append(224 + 10)
                out.append(idx)
            else:
                out.append(240 + 10)
                push16(out, idx)
            pos = wm.end()
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
# Explicit "LET var=expr" form -- same operand shape as bare assignment,
# just a different lcp per type (GFALCT's own "LET " keyword text).
LET_ASSIGN_LCP = {0: 256, 1: 260, 2: 264, 3: 268, 8: 272, 9: 276}

# Bare (no LET) array-element assignment: the header just resolves the
# array's own name (GFAVST's array suffixes, e.g. "%(", already include
# the opening paren) -- the index expression, closing paren, and "="
# are then just ordinary tokens in the generic stream that follows,
# using GFAPFT's dedicated combined ")=" token. Confirmed directly
# against a real compiled program's own bytes (a ground-truth .gfa from
# the companion GFA Decompiler project's test archive): 'p%(0)=0'
# encodes as lcp=336 / pop16(name) / <index expr> / pft ")=" (57) /
# <rhs expr>, with no extra header bytes beyond the name index.
ARRAY_ASSIGN_LCP = {4: 328, 5: 332, 6: 336, 7: 340, 12: 344, 13: 348}
LET_ARRAY_ASSIGN_LCP = {4: 280, 5: 284, 6: 288, 7: 292, 12: 296, 13: 300}

# INC/DEC/ADD/SUB/MUL/DIV as STATEMENTS (e.g. "INC i%", "ADD i%,5") --
# confirmed directly from a comment in a ground-truth real program's own
# source ("hell.lst", from the companion GFA Decompiler project's test
# archive): "124(NEXT),76(FOR),256(LET),640(INC),672(DEC),704(ADD),
# 736(SUB),768(MUL),800(DIV)". INC/DEC take just the variable (no
# value); ADD/SUB/MUL/DIV take the variable, a literal ',', then a
# value expression in the generic stream.
INC_LCP = {0: 640, 2: 644, 8: 648, 9: 652}
DEC_LCP = {0: 672, 2: 676, 8: 680, 9: 684}
ARITH_STMT_LCP = {
    "ADD": {0: 704, 2: 708, 8: 712, 9: 716},
    "SUB": {0: 736, 2: 740, 8: 744, 9: 748},
    "MUL": {0: 768, 2: 772, 8: 776, 9: 780},
    "DIV": {0: 800, 2: 804, 8: 808, 9: 812},
}

# Array-element counterparts (e.g. "INC a%(i)", "ADD a%(i),5") -- no type5
# ($(), string array) or type7 (!(), single-precision array) entries exist
# for any of these six operations, confirmed directly from the same
# decoder source used for ARRAY_ASSIGN_LCP above (those two types' lcp
# tuples list only the two assignment forms, nothing else).
ARRAY_INC_LCP = {4: 656, 6: 660, 12: 664, 13: 668}
ARRAY_DEC_LCP = {4: 688, 6: 692, 12: 696, 13: 700}
ARRAY_ARITH_LCP = {
    "ADD": {4: 720, 6: 724, 12: 728, 13: 732},
    "SUB": {4: 752, 6: 756, 12: 760, 13: 764},
    "MUL": {4: 784, 6: 788, 12: 792, 13: 796},
    "DIV": {4: 816, 6: 820, 12: 824, 13: 828},
}

# FOR-loop header. Each type has 3 sub-variant lcps spaced 4 apart --
# no-step (base), step-literal (base+4), step-expr (base+8, the form
# used below whenever the source actually writes a STEP) -- confirmed
# directly against ground truth: "FOR i#=1 TO 1" (default.gfa's own
# bytes, no STEP written) uses lcp=76 (== FOR_STEP_EXPR_LCP[0] - 8) and
# decodes back to "FOR i#=1 TO 1" with no STEP token at all in the
# generic stream that follows, whereas the general step-expr form always
# emits an explicit STEP token + value.
FOR_STEP_EXPR_LCP = {0: 84, 2: 96, 8: 108, 9: 120}
FOR_NO_STEP_LCP = {t: lcp - 8 for t, lcp in FOR_STEP_EXPR_LCP.items()}
# NEXT var, one representative lcp per type (of that type's own 3 sub-
# variants, whose exact distinguishing condition isn't confirmed).
NEXT_LCP = {0: 124, 2: 136, 8: 148, 9: 160}

# Simple no-argument / fixed-text statements: matched directly against
# GFALCT text, no special header beyond the keyword itself.
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])=(?!=)")

_INT_RHS_RE = re.compile(r"^(-?\d+)\s*$")


def _try_bare_int_literal_rhs(rhs: str) -> bytes | None:
    """A bare scalar assignment whose ENTIRE right-hand side is a plain
    base-10 integer literal ('v%=-1' OR 'x%=5', nothing else on the
    right) encodes it directly as an integer literal -- pft 201 (the ODD
    code of the 200/201 decimal pair) with the pair's own EVEN code (200)
    as filler, then the value's 32-bit two's-complement bytes -- NOT via
    the opcode-30 unary-minus + packed-float mechanism confirmed
    elsewhere (e.g. inside a comparison like 'UNTIL z&=-1' in
    gb36test_archive/hell.gfa), and NOT via plain pft 200 (no filler)
    either, despite that being what every other bare-literal context
    uses. Confirmed 2026-08-25 via two real hand-typed-and-compiled
    lines in the same file (COMBOJST.GFA): 'w%=-1' -> '[201][200][FF FF
    FF FF]' (as found in an earlier, narrower version of this function)
    AND 'x%=5' -> '[201][200][00 00 00 05]' -- so the odd+filler form
    turns out to apply to ANY bare integer assignment RHS, not just a
    negated one. Scoped narrowly to this exact case -- a bare integer
    literal anywhere else (inside a larger expression, a comparison, a
    function argument) still uses whichever other, separately-confirmed
    encoding applies there instead.
    """
    m = _INT_RHS_RE.match(rhs)
    if not m:
        return None
    out = bytearray()
    out.append(201)
    out.append(200)
    push32(out, int(m.group(1)))
    return bytes(out)
_LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s*$")
_COMMENT_RE = re.compile(r"^\s*(!|REM\b)\s?(.*)$", re.IGNORECASE)
_TRAILING_COMMENT_RE = re.compile(r"(?<!&)!(?!=)(.*)$")


def _split_trailing_comment(text: str) -> tuple[str, str | None]:
    """Splits 'STATEMENT ! comment' into (statement, comment_text) --
    comment_text is None if there's no trailing '!' comment. Doesn't
    split on '!' inside a string literal, or on a '!' that's actually
    the single-precision REAL sigil (e.g. 'a!=0 !COMMENT' has a sigil
    '!' right after 'a' and a real comment '!' later -- distinguished
    by an identifier character immediately before AND '=' or '(' or a
    following identifier character immediately after, the shapes a
    sigil actually appears in; a real comment '!' has neither).
    """
    in_str = False
    for i, c in enumerate(text):
        if c == '"':
            in_str = not in_str
            continue
        if c != "!" or in_str:
            continue
        # A sigil attaches to a real IDENTIFIER (starts with a letter),
        # never to a bare numeric literal ('0!' is not a sigil use even
        # though '0' is alnum) -- so require an actual identifier ending
        # right at this position, not just any alnum character.
        looks_like_sigil = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", text[:i]) is not None
        if looks_like_sigil:
            continue
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
    if not (
        body.startswith("'")
        or body[:3].upper() == "REM"
        or body.startswith("$")
        or body.startswith(".")
        or (body[:4].upper() == "DATA" and (len(body) == 4 or body[4] in " \t"))
    ):
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

    # "$directive" -- a metacommand/compiler-directive line (e.g. "$m
    # 1000000" reserving workspace memory). GFALCT's own "$" text (lcp
    # 1644) has no trailing space and the decoder appends the raw
    # remainder verbatim with no auto-inserted space (unlike REM/'/DATA,
    # which do get one) -- confirmed against sky.lst's ground-truth first
    # line, "$m1000000".
    if body.startswith("$"):
        push16(out, 1644)
        out += body[1:].encode("latin1", errors="replace")
        out.append(0x0D)
        if len(out) & 1:
            out.append(0)
        return bytes(out)

    # ".directive" -- same raw-passthrough shape as "$", one lower-level
    # GFALCT entry over (lcp 1016), used for assembler-style conditional
    # blocks embedded directly in a listing (e.g. ".ifndef X" / ".endif").
    if body.startswith("."):
        push16(out, 1016)
        out += body[1:].encode("latin1", errors="replace")
        out.append(0x0D)
        if len(out) & 1:
            out.append(0)
        return bytes(out)

    # DATA payload is opaque text, never tokenized -- confirmed directly
    # from the decoder's own dispatch, which groups lcp=468 (DATA) with
    # REM/'/==>/$/. as a raw-passthrough-to-CR line type, not a real
    # expression list. Ground truth includes DATA lines whose payload
    # contains characters ('[', ']', unmatched '$', '<', '>') that would
    # never parse as a GFA expression, so this can't go through
    # tokenize_expr at all -- it must be copied byte-for-byte like REM,
    # with the same single-space-after-keyword stripping.
    if body[:4].upper() == "DATA" and (len(body) == 4 or body[4] in " \t"):
        data_text = body[4:]
        if data_text.startswith(" "):
            data_text = data_text[1:]
        push16(out, 468)
        out += data_text.encode("latin1", errors="replace")
        out.append(0x0D)
        if len(out) & 1:
            out.append(0)
        return bytes(out)

    # "DEFFN name(params)=expr" -- single-line function definition. The
    # name resolves through the same function-name group (type 14, or 15
    # if it ends in '$') that "> FUNCTION" declarations use -- confirmed
    # by the shared type semantics, not a separate ground-truth sample --
    # followed by the params as generic tokens, the combined ")=" token
    # (pft 57, same one array-element assignment uses), then the body
    # expression.
    m = re.match(r"^DEFFN\s+([A-Za-z_][A-Za-z0-9_.]*\$?)\((.*)\)=(.*)$", body, re.IGNORECASE)
    if m:
        fname, params, value_expr = m.groups()
        ftype = 15 if fname.endswith("$") else 14
        # resolve_var appends GFAVST[type_] ("$" for type 15) after the
        # pool name automatically -- storing it WITH the sigil already
        # attached doubles it up on decode ("name$" -> "name$$").
        idx = pool.get_or_add(ftype, fname[:-1] if ftype == 15 else fname)
        push16(out, 228)
        out.append(240 + ftype)
        push16(out, idx)
        # lcp=228 isn't special-cased in the decoder at all (falls
        # straight into the generic stream after "DEFFN "), so unlike
        # "> PROCEDURE" (lcp 216/24, which auto-synthesizes "(" on
        # decode), the "(" has to be an explicit token here -- same
        # fix as "> FUNCTION" (lcp 1796) above.
        out.append(PFT_TEXT_TO_CODE["("])
        if params.strip():
            out += tokenize_expr(params, 0, len(params), pool)
        out.append(PFT_TEXT_TO_CODE[")="])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "SEEK #expr" / "RELSEEK #expr" -- GFALCT bakes the "#" directly into
    # the keyword text ("SEEK #", "RELSEEK #"), with no space before the
    # following expression (confirmed: sky.lst's ground-truth "SEEK
    # #1,ADD(p%(17),164)" has no space after '#'), unlike bare "#channel"
    # arguments elsewhere where "#" is its own separate PFT token.
    m = re.match(r"^(SEEK|RELSEEK)\s*#(.*)$", body, re.IGNORECASE)
    if m:
        kw, rest = m.group(1).upper(), m.group(2)
        push16(out, 832 if kw == "SEEK" else 836)
        out += tokenize_expr(rest, 0, len(rest), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "MID$(str$,pos,len)=value$" -- the special substring-assignment
    # statement form (writes into the middle of an existing string in
    # place, distinct from MID$( used as a read-only function in an
    # expression). Its own dedicated GFALCT text "MID$(" (lcp=1220)
    # already bakes in the opening paren, followed by the generic args,
    # the combined ")=" token, then the value expression -- same shape
    # as BYTE{/WORD{/CARD{/LONG{ below.
    m = re.match(r"^MID\$\((.+)\)=(.+)$", body, re.IGNORECASE)
    if m:
        args_expr, value_expr = m.groups()
        push16(out, 1220)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE[")="])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "{addr}=value" -- untyped (word-size) generic memory-write, the
    # bare-brace counterpart of BYTE{/WORD{/CARD{/LONG{ below (lcp=920,
    # own GFALCT text is just "{"). addr can itself contain a nested
    # "{...}" memory READ (SFT 112, mid-expression) computing the actual
    # target address from a pointer stored elsewhere -- ground truth:
    # sky.lst's "{{*a|()}}=SUCC(j%)" writes through a pointer read out of
    # array a|()'s own base address (an empty-index array reference,
    # already handled generically since parse_var_ref only consumes the
    # sigil+"(", leaving the immediately-following ")" as an ordinary
    # empty-index token with nothing to fill it).
    m = re.match(r"^\{(.+)\}=(.+)$", body)
    if m:
        addr_expr, value_expr = m.groups()
        push16(out, 920)
        out += tokenize_expr(addr_expr, 0, len(addr_expr), pool)
        out.append(PFT_TEXT_TO_CODE["}="])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "BYTE{addr}=value" / "WORD{...}" / "CARD{...}" / "LONG{...}" --
    # direct memory-write statements, each its own dedicated lcp whose
    # GFALCT text already includes the opening brace, followed by the
    # address expression, the combined "}=" token (pft 67 -- same shape
    # as array-element assignment's ")=" token), then the value expression.
    m = re.match(r"^(BYTE|WORD|CARD|LONG)\{(.+)\}=(.+)$", body, re.IGNORECASE)
    if m:
        kw, addr_expr, value_expr = m.group(1).upper(), m.group(2), m.group(3)
        lcp = {"CARD": 932, "BYTE": 936, "LONG": 924, "WORD": 1672}[kw]
        push16(out, lcp)
        out += tokenize_expr(addr_expr, 0, len(addr_expr), pool)
        out.append(PFT_TEXT_TO_CODE["}="])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "RETURN" (bare, lcp=28, no trailing space in its own GFALCT text)
    # vs. "RETURN value" (used inside FUNCTIONs, lcp=68, whose GFALCT
    # text already has the trailing space baked in) -- two genuinely
    # different tokens sharing the same displayed keyword, confirmed
    # from ground truth and already documented in TRIM_DOLLAR_CALLS'
    # sibling project; conflating them (e.g. always using 28) produces
    # 'RETURNvalue&' with the space silently swallowed.
    m = re.match(r"^RETURN\s*$", body, re.IGNORECASE)
    if m:
        push16(out, 28)
        _append_comment(out, comment)
        return bytes(out)
    m = re.match(r"^RETURN\s+(.*)$", body, re.IGNORECASE)
    if m:
        value_expr = m.group(1)
        push16(out, 68)
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "AFTER delay GOSUB name" / "EVERY delay GOSUB name" -- confirmed
    # from ground truth: the keyword's own lcp is followed directly by
    # the delay expression, then the SAME mid-expression "GOSUB" pft
    # token (76) used inside "ON expr GOSUB target" forms, then the
    # target resolved through the procedure name group (type 11), same
    # as standalone GOSUB above. AFTHOLD/AFTCONT/EVEHOLD/EVECONT (the
    # other three lcp each of these keywords also has) aren't handled.
    m = re.match(r"^(AFTER|EVERY)\s+(.*?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        kw, delay_expr, target = m.group(1).upper(), m.group(2), m.group(3)
        lcp = 1460 if kw == "AFTER" else 1448
        idx = pool.get_or_add(11, target)
        push16(out, lcp)
        out += tokenize_expr(delay_expr, 0, len(delay_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    # "ON MENU ... GOSUB target" event-trap forms -- each has its OWN
    # dedicated GFALCT lcp with the keyword text baked in (some, like
    # "ON MENU KEY GOSUB ", bake in the trailing "GOSUB " too; others,
    # like "ON MENU BUTTON ", stop short of it because BUTTON/IBOX/OBOX
    # take numeric args first). Falling through to the generic "ON"=504
    # keyword + expression-stream tokenizer breaks these because GFAPFT's
    # own "MENU"/"BUTTON"/"KEY"/"MESSAGE"/"IBOX"/"OBOX" entries have NO
    # baked-in spacing (unlike operators such as " AND "), so consecutive
    # bare keyword tokens would render glued together with no separator.
    # Even though these three bake "...GOSUB " fully into their own lcp
    # text, the decoder doesn't special-case lcp 532/536/540 the way it
    # special-cases lcp=244 plain "GOSUB " -- it falls through to the
    # generic post-header token stream regardless, so the target must be
    # encoded as an ordinary marker+index mid-expression reference (240+11
    # + 16-bit pool index), same as the BUTTON/IBOX/OBOX forms below.
    # Confirmed by round-trip: a bare index here decoded as garbage PFT
    # bytes ("AND"/"OR"/...) instead of the target name.
    m = re.match(r"^ON\s+MENU\s+MESSAGE\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 536)
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+KEY\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 540)
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+BUTTON\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 544)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+IBOX\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 952)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+OBOX\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 956)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    # Plain "ON MENU GOSUB target" -- lcp=532 bakes in the trailing
    # "GOSUB " already, so the body is just the target.
    m = re.match(r"^ON\s+MENU\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 532)
        out.append(240 + 11)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    # "GOSUB name" -- confirmed from ground truth that GOSUB targets
    # resolve through the PROCEDURE name group (type 11), the same group
    # "> PROCEDURE"/"@name" use -- NOT the label group (type 10) that
    # GOTO targets and "name:" declarations use. Handled as its own
    # dedicated case (rather than falling through _SIMPLE_KEYWORDS into
    # the generic expression tokenizer's bare-identifier-is-a-label
    # fallback) specifically so the target lands in the right group.
    m = re.match(r"^GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 244)
        push16(out, idx)
        _append_comment(out, comment)
        return bytes(out)

    m = _LABEL_RE.match(body)
    if m:
        # lcp=252 confirmed directly against a ground-truth compiled
        # label ("var_length:" in the companion GFA Decompiler project's
        # test archive) -- NOT 1668 (that's the INLINE/raw-machine-code
        # marker; using it here would make the decoder treat everything
        # after this line as opaque binary, not further statements).
        idx = pool.get_or_add(10, m.group(1))
        push16(out, 252)
        # Same byte-form-when-it-fits choice as the label-reference case
        # in tokenize_expr -- confirmed needed here too against a real
        # GFA-BASIC editor's own tokenized 'lbl:' declaration.
        if idx < 256:
            out.append(224 + 10)
            out.append(idx)
        else:
            out.append(240 + 10)
            push16(out, idx)
        out.append(PFT_TEXT_TO_CODE[":"])
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
        # Unlike "> PROCEDURE " (lcp 216/24), which auto-synthesizes "("
        # on decode by peeking at the next byte, "> FUNCTION " (lcp 1796)
        # has NO such logic at all in the decoder (it just sets
        # handled_prefix and falls straight into the generic stream) --
        # so the "(" has to be an explicit token here. Confirmed directly
        # against ground truth (hell.gfa's own bytes for a FUNCTION with
        # args): name-ref byte, then pft=35 "(" literally in the stream,
        # THEN the args. Omitting it is how '@myproc(1,2)' lost its "("
        # the first time this exact mistake was made for lcp=248 -- same
        # root cause, different lcp.
        fname = m.group(1)
        ftype = 15 if fname.endswith("$") else 14
        push16(out, 1796)
        # resolve_var appends GFAVST[15] ("$") after the pool name
        # automatically -- storing it with the sigil already attached
        # doubles it up on decode ("name$" -> "name$$").
        idx = pool.get_or_add(ftype, fname[:-1] if ftype == 15 else fname)
        out.append(240 + ftype)
        push16(out, idx)
        args = m.group(3)
        if args is not None:
            out.append(PFT_TEXT_TO_CODE["("])
            if args.strip():
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
        if step_expr is None:
            lcp = FOR_NO_STEP_LCP.get(type_) if type_ is not None else None
        else:
            lcp = FOR_STEP_EXPR_LCP.get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            # array_open=True: the start value is the first literal
            # right after a header that (like ARRAY_ASSIGN_LCP) encodes
            # its own variable directly with no var-ref token of its
            # own -- needs the odd/filler literal form for the same
            # reason an array's first index does. Confirmed against a
            # real GFA-BASIC editor's own tokenized 'FOR i%=1 TO 3': the
            # start value (1) uses the odd form, the TO value (3) doesn't
            # -- so only start_expr is seeded, not to_expr/step_expr.
            out += tokenize_expr(start_expr, 0, len(start_expr), pool, array_open=True)
            out.append(PFT_TEXT_TO_CODE["TO"])
            out += tokenize_expr(to_expr, 0, len(to_expr), pool)
            if step_expr is not None:
                out.append(PFT_TEXT_TO_CODE["STEP"])
                out += tokenize_expr(step_expr, 0, len(step_expr), pool)
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

    m = re.match(
        r"^(INC|DEC)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)\s*$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil, index_expr = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        type_ = SUFFIX_TO_TYPE.get(sigil + "(")
        lcp = (ARRAY_INC_LCP if kw == "INC" else ARRAY_DEC_LCP).get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
            out.append(PFT_TEXT_TO_CODE[")"])
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(
        r"^(INC|DEC)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\s*$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil = m.group(1).upper(), m.group(2), m.group(3)
        type_ = SUFFIX_TO_TYPE.get(sigil)
        lcp = (INC_LCP if kw == "INC" else DEC_LCP).get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(
        r"^(ADD|SUB|MUL|DIV)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)\s*,\s*(.*)$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil, index_expr, value_expr = m.groups()
        kw = kw.upper()
        type_ = SUFFIX_TO_TYPE.get(sigil + "(")
        lcp = ARRAY_ARITH_LCP.get(kw, {}).get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
            # Array ADD/SUB/MUL/DIV use two SEPARATE tokens here (plain
            # ")" then plain ","), unlike the scalar form (whose header
            # already implies the comma) and unlike array assignment
            # (whose combined ")=" token covers both at once) -- confirmed
            # directly against a ground-truth compiled 'ADD i#(1),1'.
            out.append(PFT_TEXT_TO_CODE[")"])
            out.append(PFT_TEXT_TO_CODE[","])
            out += tokenize_expr(value_expr, 0, len(value_expr), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(
        r"^(ADD|SUB|MUL|DIV)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\s*,\s*(.*)$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil, value_expr = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        type_ = SUFFIX_TO_TYPE.get(sigil)
        lcp = ARITH_STMT_LCP.get(kw, {}).get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            # The header's own decode already appends "," after the
            # variable (see ARITH_STMT_LCP's docstring) -- no separate
            # comma token needed here.
            out += tokenize_expr(value_expr, 0, len(value_expr), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(
        r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)=(.*)$",
        body,
    )
    if m and (m.group(2) + "(") in SUFFIX_TO_TYPE:
        name, sigil, index_expr, rhs = m.groups()
        type_ = SUFFIX_TO_TYPE[sigil + "("]
        lcp = ARRAY_ASSIGN_LCP.get(type_)
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
            out.append(PFT_TEXT_TO_CODE[")="])
            out += tokenize_expr(rhs, 0, len(rhs), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(r"^LET\s+", body, re.IGNORECASE)
    if m:
        let_rest = body[m.end() :]
        arr_m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)=(.*)$", let_rest)
        if arr_m and (arr_m.group(2) + "(") in SUFFIX_TO_TYPE:
            name, sigil, index_expr, rhs = arr_m.groups()
            type_ = SUFFIX_TO_TYPE[sigil + "("]
            lcp = LET_ARRAY_ASSIGN_LCP.get(type_)
            if lcp is not None:
                idx = pool.get_or_add(type_, name)
                push16(out, lcp)
                push16(out, idx)
                out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
                out.append(PFT_TEXT_TO_CODE[")="])
                out += tokenize_expr(rhs, 0, len(rhs), pool)
                _append_comment(out, comment)
                return bytes(out)
        am = _ASSIGN_RE.match(let_rest)
        if am and am.group(2) in SUFFIX_TO_TYPE:
            name, sigil = am.group(1), am.group(2)
            type_ = SUFFIX_TO_TYPE[sigil]
            lcp = LET_ASSIGN_LCP.get(type_)
            if lcp is not None:
                idx = pool.get_or_add(type_, name)
                push16(out, lcp)
                push16(out, idx)
                rhs = let_rest[am.end() :]
                bare_int_lit = _try_bare_int_literal_rhs(rhs)
                out += bare_int_lit if bare_int_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
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
            bare_int_lit = _try_bare_int_literal_rhs(rhs)
            out += bare_int_lit if bare_int_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
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
            if lcp in PRINT_LCPS:
                # See PRINT_LCPS' own comment: each non-string-typed
                # print item needs an extra invisible marker byte (pft
                # 55) right before it.
                for chunk in _split_print_items(rest):
                    if chunk in (",", ";"):
                        out += tokenize_expr(chunk, 0, len(chunk), pool)
                    elif chunk.strip():
                        if not _expr_starts_string(chunk):
                            out.append(55)
                        out += tokenize_expr(chunk, 0, len(chunk), pool)
            else:
                out += tokenize_expr(rest, 0, len(rest), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "@name" / "@name(args)" -- direct PROCEDURE/FUNCTION call syntax.
    # lcp=248 confirmed against ground truth ("@procedure" in the
    # companion GFA Decompiler project's test archive): resolves the
    # callee's name directly in the header (type 11, same group as ">
    # PROCEDURE" declarations). Unlike "> PROCEDURE"/"> FUNCTION" (whose
    # decoder peeks ahead and synthesizes "(" without consuming a token),
    # lcp 248's own decode branch (240,244,248) does NOT auto-add "(" --
    # confirmed the hard way (round-tripped '@myproc(1,2)' came back
    # missing its open paren until this was added explicitly).
    m = re.match(r"^@([A-Za-z_][A-Za-z0-9_.$]*)\s*(\((.*)\))?\s*$", body)
    if m:
        name = m.group(1)
        ptype = 15 if name.endswith("$") else 11
        # resolve_var appends GFAVST[15] ("$") after the pool name
        # automatically -- storing it with the sigil already attached
        # doubles it up on decode ("name$" -> "name$$").
        idx = pool.get_or_add(ptype, name[:-1] if ptype == 15 else name)
        push16(out, 248)
        push16(out, idx)
        args = m.group(3)
        if args is not None and args.strip():
            out.append(PFT_TEXT_TO_CODE["("])
            out += tokenize_expr(args, 0, len(args), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
        _append_comment(out, comment)
        return bytes(out)

    # Bare "name" / "name(args)" (no leading "@") -- a PROCEDURE call
    # using GFA-BASIC's other, "@"-less call syntax. lcp=240 confirmed
    # against ground truth both for a bare name alone ("procedure", same
    # test archive) AND for one with a full argument list (sky.lst's
    # "gf4tp_debug(...)", ground-truth lcp=240) -- the args, when
    # present, are just an ordinary "(" + generic tokens + ")" in the
    # stream that follows, identical in shape to "@name(args)" (lcp=248)
    # just without the leading "@" marker.
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_.$]*)\s*(\((.*)\))?\s*$", body)
    if m:
        name = m.group(1)
        ptype = 15 if name.endswith("$") else 11
        # resolve_var appends GFAVST[15] ("$") after the pool name
        # automatically -- storing it with the sigil already attached
        # doubles it up on decode ("name$" -> "name$$").
        idx = pool.get_or_add(ptype, name[:-1] if ptype == 15 else name)
        push16(out, 240)
        push16(out, idx)
        args = m.group(3)
        if args is not None and args.strip():
            out.append(PFT_TEXT_TO_CODE["("])
            out += tokenize_expr(args, 0, len(args), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
        _append_comment(out, comment)
        return bytes(out)

    raise GfaTokenizeError(f"unrecognized statement: {body!r}")


def _append_comment(out: bytearray, comment: tuple[int, str] | None) -> None:
    if comment is None:
        # Every real statement line ends with pft=70 (the same
        # "comment marker / end-of-line sentinel" the decoder already
        # treats as a harmless no-comment terminator when nothing
        # follows it), then an even-byte pad if needed -- confirmed
        # against a real GFA-BASIC editor's own tokenized output
        # (the companion GFA Decompiler project's Hatari-based
        # verification): every one of a small test program's 8 lines
        # matched byte-for-byte once this sentinel+pad was accounted
        # for, with no other difference. The previous version of this
        # function omitted it entirely, reasoning (wrongly, it turns
        # out) that our own detokenizer's lenient `while pos < len(raw)`
        # loop tolerates its absence -- true, but the real editor
        # doesn't accept files missing it: this was a real, confirmed
        # bug, not a style choice (round-tripping through this project's
        # own tokenizer/detokenizer pair never caught it, since the
        # detokenizer never required the byte it was missing).
        #
        # The pad byte's own VALUE is 70 again (repeating the sentinel),
        # not a zero byte -- that first 8-line test program never
        # happened to exercise a line needing this pad at all, so the
        # zero-byte guess went unverified until a real GFA-BASIC -s
        # debug compile of 'c$=a$+b$+g$'/'i$=TRIM$(...)'/three PRINT
        # statements (RTLIBTS2) showed the real editor's own tokenizer
        # emitting a second 70 byte in every line needing a pad, never
        # a zero. Fixed: pad with another sentinel byte, not zero.
        out.append(70)
        if len(out) & 1:
            out.append(70)
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
    "IF": 32, "ENDIF": 36, "ENDFUNC": 44,
    "SELECT": 48, "ENDSELECT": 52, "ELSE": 56, "CASE": 224,
    "EXIT IF": 172, "LOCAL": 212, "PRINT": 588, "DIM": 840, "DEFAULT": 60,
    "~": 964,
    "END": 496, "STOP": 1360, "CONT": 1268, "GOTO": 232,
    "ON": 504, "RESTORE": 236, "READ": 1488, "POKE": 388,
    "CLR": 1256, "ERASE": 1288, "SWAP": 472, "INPUT": 1472,
    "SPOKE": 400, "DPOKE": 392, "LPOKE": 396, "OPEN": 1060, "CLOSE": 1072,
    "OUT": 1228, "BSAVE": 1616, "BLOAD": 1620, "LPRINT": 1212,
    "OUT&": 1680, "OUT%": 1684, "RESERVE": 416, "BPUT": 448, "BGET": 444,
    "ARRAYFILL": 1588, "LINE INPUT": 616, "BMOVE": 852, "DELETE": 1404,
    "CLS": 1260,
    "DO WHILE": 196, "DO UNTIL": 200, "LOOP WHILE": 204, "LOOP UNTIL": 208,
    "ELSE IF": 64,
}


# lcp values for PRINT/LPRINT -- the only statements confirmed so far to
# need an extra invisible marker byte (GFAPFT opcode 55, whose own display
# text is '' -- never rendered as source, just a structural tag) right
# before any print-item whose expression is NOT string-typed. Confirmed
# 2026-08-25 via TESTVEX.GFA, a real hand-typed-and-compiled 'PRINT v!'
# (a bare single-precision variable): its bytes are '[lcp][opcode 55]
# [var-ref]', not '[lcp][var-ref]' the way this project previously
# encoded every PRINT item uniformly (confirmed correct only for STRING
# arguments so far, e.g. RTLIBTJ2's own 'PRINT h$'/'PRINT e$'/etc.).
# Applied to every non-string top-level item, not just SINGLE
# specifically -- there's no reason GFA-BASIC's own PRINT routine would
# special-case one non-string type over another here, but this
# generalization (INTEGER, REAL, LONG, etc. also needing it) is still
# pending its own direct confirmation.
PRINT_LCPS = {588, 1212}


def _split_print_items(text: str) -> list[str]:
    """Splits a PRINT/LPRINT argument list at top-level ','/';' separators
    (outside quotes and parens), returning items and separators
    interleaved (separators as their own single-character elements).
    """
    items: list[str] = []
    depth = 0
    in_quote = False
    start = 0
    for i, c in enumerate(text):
        if in_quote:
            if c == '"':
                in_quote = False
        elif c == '"':
            in_quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and c in ",;":
            items.append(text[start:i])
            items.append(c)
            start = i + 1
    items.append(text[start:])
    return items


def _expr_starts_string(text: str) -> bool:
    """Best-effort check of whether an expression's leading atom is
    string-typed -- used only to decide PRINT's own marker byte (see
    PRINT_LCPS' own comment above), not general type inference.
    """
    s = text.lstrip()
    if not s:
        return False
    if s[0] == '"':
        return True
    if parse_number(s, 0) is not None:
        return False
    varref = parse_var_ref(s, 0)
    kw = _try_match_keyword(s, 0, PFT_TEXT_TO_CODE, _MAX_PFT_WORD_LEN)
    sft = _try_match_keyword(s, 0, SFT_TEXT_TO_CODE, _MAX_SFT_WORD_LEN)
    var_len = varref[3] if varref is not None else -1
    kw_len = kw[1] if kw is not None else -1
    sft_len = sft[1] if sft is not None else -1
    best = max(var_len, kw_len, sft_len)
    if best == -1:
        return False
    if kw_len == best:
        matched = s[:kw_len]
        return matched[:1].isalpha() and matched.rstrip("(").upper().endswith("$")
    if sft_len == best:
        matched = s[:sft_len]
        return matched.rstrip("(").upper().endswith("$")
    return varref[0] in STRING_VST_TYPES


def _match_leading_keyword(body: str) -> tuple[int, int] | None:
    upper = body.upper()
    for kw in sorted(_SIMPLE_KEYWORDS, key=len, reverse=True):
        if not kw[:1].isalpha():
            # Punctuation-led keywords (e.g. "~EVNT_TIMER(1)", GFA's
            # direct XBIOS/GEMDOS/AES call syntax) attach directly to
            # whatever follows -- no space/paren separator to require.
            if upper.startswith(kw):
                return _SIMPLE_KEYWORDS[kw], len(kw)
            continue
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
    #   sep[35..37] = trailing variable-value storage area. NOT a byte
    #                 offset into this file's own content -- confirmed by
    #                 comparing two real GFA-BASIC-editor-saved files with
    #                 identical variable sets and identical total file
    #                 length but different sep[36]/[37] values: this is a
    #                 runtime storage-size hint for the loader/interpreter
    #                 to pre-allocate space for the program's variables
    #                 when it starts running, not something physically
    #                 present in the .gfa file. Previously left at the
    #                 same value as sep[35] (i.e. "zero extra") -- always
    #                 wrong, and load-bombing the real editor on any
    #                 program actually using its declared variables
    #                 (confirmed against Hatari-compiled test programs in
    #                 the companion GFA Decompiler project). Sized here
    #                 per GFAVST type index using each GFA-BASIC scalar
    #                 type's own real runtime size (REAL=8, STRING
    #                 descriptor=6, INTEGER=4 -- not the 2-byte value
    #                 width, apparently padded to a 4-byte cell at rest;
    #                 confirmed by a real 'FOR i%=1 TO 3' test whose
    #                 sep[36] was exactly 2 higher than this table's first,
    #                 2-byte-assuming version predicted -- LONG=4, BYTE=1)
    #                 and the same 6-byte descriptor size for every array
    #                 type still (confirmed for STRING scalars/arrays
    #                 specifically; the sizes for other scalar/array types
    #                 are documentation-derived, not yet independently
    #                 ground-truth-confirmed the way STRING's and
    #                 INTEGER's are). PROCEDURE/FUNCTION/label names
    #                 (indices 10/11/14) aren't variables and contribute
    #                 nothing.
    TRAILING_STORAGE_SIZE = {
        0: 8, 1: 6, 2: 4, 3: 4, 4: 6, 5: 6, 6: 6, 7: 6,
        8: 4, 9: 1, 12: 6, 13: 6, 15: 6,
    }
    trailing_extra = sum(
        TRAILING_STORAGE_SIZE.get(i, 0) * group_entry_counts[i] for i in range(16)
    )
    sep = [0] * 38
    running = 0
    for i in range(16):
        sep[i] = running
        running += group_byte_counts[i]
    sep[16] = running
    sep[17] = sep[16]
    listing_end = sep[16] + len(listing)
    sep[19] = listing_end
    # sep[18] = start of the END_OF_PROGRAM sentinel's own encoded line
    # (2-byte size prefix + 2-byte lcp=180 content -- always exactly 4
    # bytes, the last entry in `encoded`/`listing`) -- confirmed against
    # a real GFA-BASIC editor's own saved file, whose sep[18] was
    # consistently sep[19]-4 rather than sep[16] (this function's
    # previous placeholder assumption).
    sep[18] = listing_end - 4
    running_count = listing_end
    for i in range(16):
        running_count += 4 * group_entry_counts[i]
        sep[20 + i] = running_count
    sep[36] = running_count + trailing_extra
    sep[37] = sep[36]

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
