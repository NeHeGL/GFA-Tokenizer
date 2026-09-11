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

# "KEY" (GFAPFT 170) has no legitimate standalone use as a generic
# expression atom -- its only real GFA-BASIC appearance is inside the
# "ON MENU KEY GOSUB" event-trap statement (see ON_STATEMENT_LCP's own
# "ON MENU KEY GOSUB" entry), which is matched and encoded entirely by
# its own dedicated regex before ever reaching tokenize_expr. Leaving
# it in this generic table meant tokenize_expr's var-ref-vs-keyword
# tie-break (which favors the keyword, correct for real collisions like
# CHR$( shadowing a same-named array -- see that tie-break's own
# comment) wrongly turned a plain bare variable named "key" into this
# keyword's token everywhere it appeared inside an expression, even
# though the real compiler happily lets "key" be an ordinary variable
# there. Confirmed real: BALL.LST's own "key=ASC(INKEY$)" / "EXIT IF
# key==27" / etc. -- reloading our tokenized output in the real
# GFA-BASIC editor and resaving it turned every expression use of "key"
# into "KEY", verified against the same editor's own resave of the
# real, untouched BALL.GFA (which keeps "key" as a plain variable).
del PFT_TEXT_TO_CODE["KEY"]

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

# GFAPFT builtins whose argument is coerced to REAL (pft 223, same as a
# literal right after a binary/comparison operator) regardless of the
# literal's own source-text shape -- confirmed 2026-09-10 via
# COVFULL.LST vs a real editor's own COVFULL9.GFA (the companion GFA
# Decompiler project's coverage-test corpus). Deliberately NOT a
# blanket "every function argument" rule: FACT(/STR$(/CHR$(/SPACE$(/
# HEX$(/OCT$(/BIN$(/DIR$(/INPUT$(/ERR$(/CVI(/CVL( are all confirmed
# NOT wanting this (plain-integer argument instead) from the same
# comparison -- a first, broader attempt at this fix regressed all of
# those. MAX(/MIN( confirmed only for their FIRST argument so far (the
# comma-repeat case for a second/later argument isn't handled by this
# per-keyword flag alone, same limitation as array_open's own single-
# shot nature).
PFT_REAL_ARG_FUNCTIONS = {
    "SQR(", "SIN(", "COS(", "TAN(", "ATN(", "EXP(", "LOG(", "LOG10(",
    "ACOS(", "ASIN(", "COSQ(", "SINQ(", "DEG(", "RAD(",
    "INT(", "ROUND(", "FRAC(", "TRUNC(", "RND(", "RANDOM(",
    "MAX(", "MIN(",
}

# GEM AES/VDI-family GFASFT builtins whose FIRST argument uses the odd-
# filler integer form (array_open=True, zero_filler=False -- same
# mechanism as SUCC(/PRED(''s GFASFT argument) instead of the plain
# form -- confirmed 2026-09-10 via COVFULL.LST vs a real editor's own
# COVFULL9.GFA. A clear family pattern (every one of these takes a
# GEM handle/index as its first argument), but each one still
# individually confirmed present in the diff, not assumed from the
# other members alone.
PFT_ODD_FILLER_FIRST_ARG_FUNCTIONS = {
    "APPL_READ(", "APPL_WRITE(", "RSRC_GADDR(", "RSRC_SADDR(",
    "SHEL_GET(", "OBJC_EDIT(", "FORM_BUTTON(",
}

# LEFT$(/RIGHT$( also each list two PFT codes for the identical display
# text (58/59, 60/61) -- unlike '+'/the comparisons above, this isn't a
# numeric-vs-string distinction (both codes are for the same read-only
# string-returning function). Confirmed via a real GFA-BASIC -s debug
# compile (RTLIBTS2): 'd$=LEFT$(c$,3)' compiles to code 59 (0x3b), never
# PFT_TEXT_TO_CODE's default (58, the first/lower occurrence found by
# its dedup) -- and 'h$=RIGHT$(c$,3)' likewise uses 61, not 60.
#
# MID$( has the identical duplicate shape (62/63) -- previously left
# alone here since there was no ground truth yet for which of its two
# codes a plain read use needs, but by direct analogy with LEFT$/
# RIGHT$ (both needing the SECOND/higher code, never the first) 63 is
# now used here too. Confirmed real and a genuine crash fix: BALL.LST's
# own 'RANDOMIZE VAL(MID$(TIME$,7,8))' -- using the default (62)
# tokenized to clean-looking text and passed the real editor's own load/
# save/Test checks, but crashed hard (a real 68000 exception) on both
# Run and Compile. Isolated to this exact line via bisection (every
# other statement type between the array declarations and here was
# independently confirmed clean first).
PFT_CODE_OVERRIDE: dict[str, int] = {
    "LEFT$(": 59,
    "RIGHT$(": 61,
    "MID$(": 63,
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
#   '=' at 19, 27, 69 (a third '=' beyond the numeric/string comparison
#     pair) -- 69 CONFIRMED as the plain assignment '=' (DEFFN's own
#     matcher hardcodes it directly rather than going through
#     PFT_TEXT_TO_CODE's ambiguous default; see that matcher's own
#     comment). 19 and 27 remain unconfirmed comparison-operator forms.
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
    # Masked to the raw 32-bit bit pattern rather than packed signed: a
    # hex/octal/binary literal needing the top bit (e.g. &HFFFFFFFF) is
    # stored as that literal 32-bit pattern, same as the real compiler.
    # (Earlier revision of this comment claimed the real detokenizer
    # reads this back as a signed int, rendering "&H-1" -- that was a
    # misreading of a bug in the companion Detokenizer project's OWN
    # decode path, since fixed there; the real GFA-BASIC editor, gfalist
    # (an independent, unrelated implementation), and this project's own
    # Detokenizer post-fix all decode default5.gfa's &HFFFFFFFF correctly.
    # The masking itself was never wrong -- it's still exactly what the
    # real compiler's own bytes for this construct look like -- only the
    # justification citing "&H-1" as correct was.)
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
    # No sigil at all: Float ('#') is GFA-Basic's documented default
    # variable type ("As this is the default type, no postfix is
    # necessary"), confirmed against real-world archived source
    # (TRUCOLST's bare 'rez', among others) -- an ordinary bare scalar,
    # type 0.
    #
    # Deliberately NOT extended to "name immediately followed by '('
    # means a bare float array": that would make this function match
    # THROUGH the '(', competing on length against this same function's
    # caller (see tokenize_expr's own longest-match-wins comment) against
    # every builtin function call that isn't sigil-shadowed -- 'SIN(x)'
    # would out-length keyword-table's 3-char 'SIN' match with this
    # branch's 4-char 'SIN(' and get silently mistokenized as a bare
    # array reference. A bare array DECLARATION (MOLMASSE's own
    # 'DIM atomgewicht(69)') needs its own fix elsewhere, gated to
    # contexts that are unambiguously a declaration, not simply reusing
    # this shared expression-level identifier parser.
    return 0, name, False, p



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


# Bare (unsuffixed) array names DIM'd earlier in the file currently
# being encoded -- read by tokenize_expr (see its own use, right at the
# top of its main dispatch loop) to resolve a bare array READ the same
# way encode_line's own dedicated matchers already resolve a bare array
# WRITE. Set once per encode_line() call rather than threaded as a
# parameter through tokenize_expr's ~55 call sites -- this file only
# ever encodes one line at a time, single-threaded, so a module-level
# variable scoped to "the line currently being encoded" is safe and
# far less invasive than a signature change everywhere.
_CURRENT_DECLARED_ARRAYS: frozenset[str] = frozenset()


def tokenize_expr(text: str, pos: int, end: int, pool: IdentPool, array_open: bool = False, bare_word_is_label: bool = False, seed_binary_arith_op: bool = False, seed_force_float_literal: bool = False) -> bytes:
    out = bytearray()
    # Tracks whether the most recently emitted atom (literal, var-ref, or
    # builtin-function call) was string-typed -- used to pick the right
    # code for an ambiguous operator ('+' or a comparison, see
    # AMBIGUOUS_OP_CODES) immediately after it. Not reset by punctuation
    # (',', '(', ')') so a parenthesized/argument-list boundary doesn't
    # lose track of the enclosing expression's own type.
    last_was_string = False
    # True only right after a string LITERAL specifically (not a string
    # VARIABLE) -- confirmed 2026-09-10 via COVFULL.LST vs a real
    # editor's own COVFULL9.GFA: 'LEFT$("hello",3)''s count argument
    # (3) wants the PLAIN form, contradicting the earlier RTLIBTS2-
    # confirmed 'LEFT$(c$,3)' (a string VARIABLE first argument, not a
    # literal), which needs the odd-filler form the comma-rearm below
    # already produces. So the comma-rearm has to gate out the literal
    # case specifically, not just check last_was_string alone.
    last_was_string_literal = False
    # Depth counter for "currently inside an open PFT_REAL_ARG_FUNCTIONS
    # call's own argument list" -- confirmed 2026-09-10 via COVFULL.LST
    # vs a real editor's own COVFULL9.GFA: MAX(/MIN( are the only two
    # confirmed multi-argument members of that set, and their SECOND
    # argument ('a%=MAX(3,7)''s 7) also needs pft 223, not just the
    # first (which the plain per-match flag already covers) -- so the
    # comma handler below needs to know it's still "inside" the call
    # across the comma, not just for one token.
    real_arg_paren_depth = 0
    # General "inside any function-call/grouping parens" depth counter
    # (see its own use-site docstring below).
    paren_depth = 0
    # See PFT_ODD_FILLER_FIRST_ARG_FUNCTIONS' own match-site docstring.
    odd_filler_pending = False
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
    # Set for exactly one iteration right after matching a GFASFT
    # function whose argument is coerced to REAL regardless of the
    # literal's own source-text shape (confirmed 2026-09-10 via
    # COVFULL.LST vs a real editor's own COVFULL9.GFA: 'a%=EVEN(4)''s
    # bare-integer '4' argument is 'dd 00' + double_to_gfa_float(4.0) --
    # pft 221, the SAME default packed-float form a genuine float literal
    # like 'c#=3.14' gets, not the plain-integer form its own text would
    # normally produce). Only EVEN(/ODD( confirmed so far; SUCC(/PRED( --
    # also GFASFT -- do NOT get this (confirmed wanting the plain
    # odd+filler integer form instead, see force_odd_filler below), so
    # this can't be a blanket "any GFASFT argument" rule -- each GFASFT
    # entry needs its own confirmation before being added here.
    force_float_literal = seed_force_float_literal
    # Set for exactly one iteration right after emitting a BINARY
    # arithmetic OR comparison operator ('+', '-', '*', '/', '=', '<>',
    # '<', '>', '<=', '>=' with a real value before it -- string '+'
    # concatenation excluded, see its own check below): the very next
    # plain base-10 literal (integer OR float) is encoded as pft 223 --
    # 8 bytes (double_to_gfa_float of the value, first byte OR'd with
    # 0x80), with NO extra marker byte -- instead of its normal form.
    # Confirmed 2026-08-25 via a real hand-typed-and-compiled 'y%=x%-1':
    # its '1' operand is 'df 80 00 00 00 00 00 03 ff', which is pft 223
    # followed by double_to_gfa_float(1.0) ('00 00 00 00 00 00 03 ff')
    # with its own first byte OR'd -- NOT the plain integer form
    # (200/201) this project had always used for every bare literal
    # until now.
    #
    # Comparison operators confirmed 2026-09-10 via the companion GFA
    # Decompiler project's COVFULL.LST coverage test: 'IF a%=1's '1' is
    # 'df 80 00 00 00 00 00 03 ff' (identical shape) in a real editor's
    # own Merge-then-Save of the file, but this tokenizer produced
    # 'c8 00 00 00 01' (plain 32-bit integer, pft 200) instead -- a real
    # 4-byte-per-occurrence length mismatch that threw off every
    # subsequent line's own byte accounting, corrupting identifier-pool
    # resolution deep into the file and load-bombing the real editor.
    # The bug: this flag was only ever set for '+' (see the '+'-specific
    # check below), never for the other seven AMBIGUOUS_OP_CODES entries
    # (comparisons share that same table). Fixed to cover all of them --
    # '*'/'/' were already a reasoned-but-unconfirmed generalization from
    # '-'; comparisons are now independently ground-truth-confirmed too.
    just_saw_binary_arith_op = seed_binary_arith_op
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
        if c == " ":
            # Skipped BEFORE the one-shot flag rotation below (not
            # after) -- confirmed 2026-09-10 via COVFULL.LST vs a real
            # editor's own COVFULL9.GFA: 'a%=a% MOD 3' needs its '3' to
            # see just_saw_binary_arith_op from the 'MOD' token two
            # characters back, but rotating these flags on the
            # intervening space's own iteration (the previous version
            # of this loop did the rotation unconditionally, THEN
            # checked for space) silently lost every one of them across
            # any whitespace -- confirmed broken for every WORD_OPERATOR
            # ('a% MOD 3'/'a% DIV 3'/etc. always have a space before
            # their operand in valid syntax, so this bug fired on all
            # of them) and would equally have broken a spaced comparison
            # ('IF a% = 1') or PFT_REAL_ARG_FUNCTIONS call had either
            # appeared with a space in the corpus tested so far.
            pos += 1
            continue
        # Consumed by this iteration's branches below (the numeric-
        # literal one specifically); cleared for every OTHER kind of
        # token automatically, and re-armed only by the var-ref branch
        # further down when it just emitted a fresh array-reference.
        was_array_open, array_open = array_open, False
        was_zero_filler, zero_filler = zero_filler, True
        was_pending_unary_minus, pending_unary_minus = pending_unary_minus, False
        was_force_float_literal, force_float_literal = force_float_literal, False
        was_binary_arith_op, just_saw_binary_arith_op = just_saw_binary_arith_op, False
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
            last_was_string_literal = True
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
            elif was_pending_unary_minus or is_float or was_force_float_literal:
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
                # above tokenize_expr entirely and never reaches here).
                # NOTE: the '0 - operand' unary-minus rewrite's own
                # injected leading '0' (see pending_unary_minus's own
                # docstring above) does NOT reach this branch -- it's
                # emitted by its own separate, self-contained code
                # right where it's injected (using the pair's own even
                # code as filler, confirmed via 'z%=0-x%'), specifically
                # BECAUSE this shared branch needs a different filler
                # for every other caller that lands here. This branch is
                # for a genuine first-argument literal of a bare
                # statement's own argument list (e.g. 'ARECT 10,...',
                # 'ALINE 10,...', 'CURVE 10,...', 'POLYLINE 2,x(),y()')
                # -- CONFIRMED via a real Hatari compile (the companion
                # GFA Decompiler project's Hard_Drive/TESTING/
                # DIMPROBF.GFA) across seven independent occurrences
                # (ARECT/ALINE/ACHAR/CURVE/POLYLINE/POLYFILL/POLYMARK,
                # all with the SAME 10 or 2 leading literal) that this
                # context wants a plain ZERO filler byte, not the pair's
                # own even code the way this project used to emit
                # unconditionally here.
                even = {10: 200, 16: 202, 8: 204, 2: 206}[base]
                out.append(even + 1)
                out.append(0)
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
                odd_filler_pending = False
            pos = newpos
            last_was_string = False
            last_was_string_literal = False
            just_saw_value = True
            continue
        # A word operator (MOD/DIV/AND/...) glued directly onto a
        # following digit with no space (e.g. '(x+2)MOD3') still has to
        # be recognized as the operator, not as one long bare identifier
        # "mod3" -- _try_match_keyword's own word-boundary check (see its
        # docstring) treats a following digit as continuing the same
        # word, which is right for a genuine identifier prefix collision
        # (a variable named "mod2" must not be split into "MOD"+"2") but
        # wrong here: right after a value, the grammar requires an
        # OPERATOR next, so a value-shaped word can only start once the
        # operator's own text has been consumed. Gated on just_saw_value
        # so it only fires where an operator is actually expected, never
        # at the start of a new operand (where "mod3" as a genuine
        # variable name must still parse as one identifier). Confirmed
        # real: BALL.LST's own '(b_line(0)+2)MOD3' -- reloading our
        # tokenized output in the real GFA-BASIC editor and resaving it
        # showed 'mod3' passed through as literal text instead of the
        # MOD operator, verified against the same editor's own resave of
        # the real, untouched BALL.GFA (which shows 'MOD 3').
        if just_saw_value:
            wm = re.match(r"[A-Za-z]+", text[pos:end])
            if wm and wm.group(0).upper() in WORD_OPERATORS and wm.end() < end - pos and text[pos + wm.end()].isdigit():
                word = wm.group(0).upper()
                code = PFT_TEXT_TO_CODE[word]
                out.append(code)
                pos += wm.end()
                just_saw_value = False
                last_was_string = False
                last_was_string_literal = False
                continue
        # A bare (unsuffixed) name immediately followed by '(' that's a
        # KNOWN declared array (DIM'd bare earlier in the file) is read
        # here as a genuine array reference, not the plain scalar
        # parse_var_ref alone would produce. parse_var_ref deliberately
        # never resolves a bare 'name(' as an array on its own (see its
        # own docstring: doing so would make 'SIN(x)' lose to a same-
        # shaped bare-array match) -- but once declared_arrays confirms
        # this SPECIFIC name is the user's own array, there's no such
        # ambiguity to worry about. Without this, every READ of a bare
        # array (anywhere in an expression -- not just its own
        # assignment, which encode_line's dedicated matcher already
        # handles separately) silently fell back to a bare SCALAR var-
        # ref for the name, immediately followed by an ordinary '(',
        # literal, ')' as unrelated tokens -- which still decodes to the
        # same-looking text on a round-trip (concatenation hides the
        # structural difference) but isn't a real array reference at
        # all. Confirmed real, and a genuine bug: BALL.LST's own bare
        # arrays (x/y/x1/x2/... etc, see the DIM fix above) are read
        # throughout the file (e.g. inside @put_sprite(...) calls);
        # reloading our tokenized output in the real GFA-BASIC editor
        # and running it crashed hard (a real 68000 exception, not a
        # graceful BASIC error) -- isolated with a minimal 'DIM
        # arr(5)' + 'PRINT arr(0)' test file that crashed the same way,
        # while a 'DIM arr(5)' + 'arr(0)=1' write-only test (going
        # through the already-correct dedicated assignment matcher, not
        # this generic expression path) ran fine.
        nm = _NAME_RE.match(text, pos)
        if nm and text[nm.end() : nm.end() + 1] == "(" and nm.group(0).lower() in _CURRENT_DECLARED_ARRAYS:
            idx = pool.get_or_add(4, nm.group(0))
            if idx < 256:
                out.append(224 + 4)
                out.append(idx)
            else:
                out.append(240 + 4)
                push16(out, idx)
            pos = nm.end() + 1
            array_open = True
            zero_filler = True
            last_was_string = False
            last_was_string_literal = False
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
        # 'SUCC('/'PRED(' are the only two keywords (besides '*', already
        # unambiguous) present in BOTH GFAPFT and GFASFT -- confirmed
        # 2026-09-10 via COVFULL.LST vs a real editor's own COVFULL9.GFA:
        # the real compiler always emits the GFASFT form ('d0'+sft-index,
        # e.g. 'a%=SUCC(5)' -> 'd0 60 c9 c8 00 00 00 05'), but this
        # tokenizer's tie-break (kw_len checked before sft_len below)
        # always picked the GFAPFT single-byte form instead, producing an
        # entirely different, wrong-length opcode.
        prefer_sft = kw_len == best and sft_len == best and text[pos:pos + kw_len].upper() in ("SUCC(", "PRED(")
        if best == -1:
            pass
        elif kw_len == best and not prefer_sft:
            code, newpos = kw
            matched = text[pos:newpos]
            op_key = matched.upper() if matched[:1].isalpha() else matched
            # General "inside function-call/grouping parens" depth
            # counter -- see last_was_string_literal's own docstring:
            # the comma-rearm's string-literal exclusion only applies
            # AT paren depth > 0 (a function call's own argument list,
            # e.g. LEFT$("hello",3)); a bare statement's own top-level
            # comma (paren_depth == 0, e.g. OPEN "R",#3,"random.dat",32)
            # rearms regardless of literal-vs-variable -- confirmed
            # 2026-09-10 via COVFULL.LST vs a real editor's own
            # COVFULL9.GFA: excluding literals unconditionally (this
            # project's first attempt) fixed LEFT$ but broke OPEN's own
            # last argument, which needs the same rearm even though
            # "random.dat" right before its comma is also a literal.
            if matched.endswith("("):
                paren_depth += 1
            elif matched == ")" and paren_depth > 0:
                paren_depth -= 1
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
                    # This injected '0' is a real literal token in its
                    # own right, subject to the same odd+filler-vs-plain
                    # choice as any other (see was_first_token's own
                    # docstring) -- it was hardcoded to the plain form
                    # here, which is wrong whenever this '0' is the
                    # expression's first token (e.g. 'z%=-x%' -> '0-x%'
                    # at the very start of the RHS -- confirmed needing
                    # odd+filler, same as 'z%=0-x%' typed directly).
                    if was_first_token:
                        out.append(201)
                        out.append(200)
                    else:
                        out.append(200)
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
                if not last_was_string:
                    # Every AMBIGUOUS_OP_CODES entry ('+' and all eight
                    # comparisons) is a binary operator when numeric --
                    # see just_saw_binary_arith_op's own docstring above
                    # for the '+' and 'IF a%=1' confirmations.
                    just_saw_binary_arith_op = True
            else:
                out.append(PFT_CODE_OVERRIDE.get(matched.upper(), code))
                if matched.upper() in ("L:", "W:"):
                    # Size-cast markers for GEMDOS/XBIOS/BIOS call args
                    # (confirmed real syntax, GFA_BASIC_Version_3_
                    # Interpreter_User_Manual_OCR.pdf p.447: '~GEMDOS(9,
                    # L:adr%)'). The literal that immediately follows
                    # needs the same odd+filler re-arm as the numeric
                    # argument right after a string-typed ','
                    # (last_was_string branch below) or a fresh array
                    # reference -- confirmed via a real MULTI_V1.PRG
                    # round-trip: 'GEMDOS(32,L:0)' compiled clean with
                    # the plain literal form for every OTHER argument in
                    # the file, but the real 3.60TT compiler rejected
                    # this exact line ('SAVE - not compiled', its own
                    # generic dead-line error) until the literal right
                    # after 'L:' got the odd+filler form instead -- the
                    # OTHER confirmed instances of this shape in the same
                    # file ('GEMDOS(72,L:-1)') happened to already dodge
                    # the bug because '-1' routes through the separate
                    # unary-minus rewrite, which independently already
                    # produces odd+filler-equivalent bytes.
                    array_open = True
                    zero_filler = False
                    just_saw_value = False
                elif matched[:1].isalpha():
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
                    last_was_string_literal = False
                    just_saw_value = op_key not in WORD_OPERATORS
                    # Same pft-223 rule as AMBIGUOUS_OP_CODES' operators
                    # (see just_saw_binary_arith_op's own docstring) --
                    # confirmed 2026-09-10 via COVFULL.LST vs a real
                    # editor's own COVFULL9.GFA for two separate groups:
                    # WORD_OPERATORS' RHS operand ('a%=a% MOD 3''s 3 is
                    # 'df c0 00 00 00 00 00 04 00', not the plain '3'
                    # this tokenizer produced), and a specific, confirmed
                    # set of GFAPFT builtins whose argument is coerced to
                    # REAL regardless of the literal's own shape (same
                    # idea as force_float_literal for GFASFT, just via
                    # the pft-223 no-marker form instead of pft 221) --
                    # NOT every function (FACT(/STR$(/SUCC(/etc. confirmed
                    # NOT wanting this -- see PFT_REAL_ARG_FUNCTIONS'
                    # own comment), so only this specific confirmed set.
                    if op_key in WORD_OPERATORS or matched.upper() in PFT_REAL_ARG_FUNCTIONS:
                        just_saw_binary_arith_op = True
                    if matched.upper() in PFT_REAL_ARG_FUNCTIONS and matched.endswith("("):
                        real_arg_paren_depth += 1
                elif matched == "," and real_arg_paren_depth > 0:
                    just_saw_binary_arith_op = True
                elif matched == "," and odd_filler_pending:
                    array_open = True
                    zero_filler = False
                elif matched == "," and last_was_string and not (last_was_string_literal and paren_depth > 0):
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
                    if real_arg_paren_depth > 0:
                        real_arg_paren_depth -= 1
                    odd_filler_pending = False
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
            matched_upper = matched.upper()
            last_was_string = matched_upper.rstrip("(").endswith("$")
            last_was_string_literal = False
            paren_depth += 1
            just_saw_value = True
            # Per-function argument-encoding overrides, confirmed
            # 2026-09-10 via COVFULL.LST vs a real editor's own
            # COVFULL9.GFA -- NOT a blanket rule for every GFASFT entry
            # (see force_float_literal's own docstring above), only
            # these specifically confirmed ones.
            if matched_upper in ("EVEN(", "ODD("):
                force_float_literal = True
            elif matched_upper in ("SUCC(", "PRED("):
                array_open = True
                zero_filler = False
            elif matched_upper in PFT_ODD_FILLER_FIRST_ARG_FUNCTIONS:
                # Unlike SUCC(/PRED( (whose odd-filler argument is the
                # very next token), these take one or more leading
                # variable-reference arguments before their first real
                # literal (e.g. 'OBJC_EDIT(tree%,obj&,65,...)' -- 65 is
                # the third token). array_open/zero_filler are one-shot
                # (reset every loop iteration), so a plain one-time set
                # here would be consumed by 'tree%' instead. odd_filler_
                # pending persists across the intervening var-refs/
                # commas (re-armed by each, see their own sites below)
                # until an actual literal consumes it. Also set
                # array_open/zero_filler directly here too (not just via
                # odd_filler_pending) for the immediate case (e.g.
                # 'SHEL_GET(500,...)', no leading var-ref at all).
                odd_filler_pending = True
                array_open = True
                zero_filler = False
            pos = newpos
            continue
        else:
            type_, name, is_array, newpos = varref
            if bare_word_is_label and not is_array and newpos == pos + len(name):
                # parse_var_ref's "no sigil at all" branch defaults a
                # bare word to a type-0 Float variable -- correct for a
                # real expression, but GOTO/RESTORE/RESUME's target is
                # never a variable, it's a label (a separate namespace
                # with no sigil of its own). Confirmed against a real
                # GFA-BASIC editor round-trip (FOO_TEST.LST/.GFA in the
                # companion GFA Decompiler project's Hard_Drive/TESTING
                # folder): a plain 'GOTO foo' must stay 'foo', not
                # silently become 'foo#' the way the default-Float path
                # renders it. Only fires when the caller has told us
                # (bare_word_is_label) we're in one of those statements'
                # own target position AND no explicit sigil was actually
                # written (newpos==pos+len(name) -- an explicit 'foo#'
                # still means what it says).
                idx = pool.get_or_add(10, name)
                if idx < 256:
                    out.append(224 + 10)
                    out.append(idx)
                else:
                    out.append(240 + 10)
                    push16(out, idx)
                pos = newpos
                just_saw_value = True
                continue
            idx = pool.get_or_add(type_, name)
            last_was_string = type_ in STRING_VST_TYPES
            last_was_string_literal = False
            just_saw_value = True
            if odd_filler_pending:
                array_open = True
                zero_filler = False
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
        # This used to fall through to a "last resort: bare identifier is
        # a LABEL reference" branch here, on the theory that parse_var_ref
        # returns None for a sigil-less word and leaves it for this code
        # to claim. That premise was wrong -- parse_var_ref's own "no
        # sigil at all" default-Float rule (see its docstring) means
        # varref is NEVER None for a word matching _NAME_RE, so this
        # branch was actually dead code; the real fix is the
        # bare_word_is_label check above, in the branch that actually
        # runs. Nothing reaches this point except genuinely unmatchable
        # text.
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
# "FOR var=start DOWNTO end" -- each type's 3rd sub-variant, sitting
# exactly between its no-step and step-expr siblings (base, base+4,
# base+8). Confirmed directly: msx_emul.GFA's own
# 'FOR page&=3 DOWNTO 0' uses lcp 104 == FOR_NO_STEP_LCP[8] + 4. DOWNTO
# itself is a plain GFAPFT token (73, ' DOWNTO ') emitted in the same
# stream position "TO"/"STEP" use -- not special-cased in the decoder,
# same generic keyword-text mechanism as every other operator.
FOR_DOWNTO_LCP = {t: lcp + 4 for t, lcp in FOR_NO_STEP_LCP.items()}
# NEXT var, one representative lcp per type (of that type's own 3 sub-
# variants, whose exact distinguishing condition isn't confirmed).
NEXT_LCP = {0: 124, 2: 136, 8: 148, 9: 160}

# Simple no-argument / fixed-text statements: matched directly against
# GFALCT text, no special header beyond the keyword itself.
#
# The suffix is optional: Float ('#') is GFA-Basic's documented default
# variable type ("As this is the default type, no postfix is necessary"),
# so a bare name here ('rez=XBIOS(4)', confirmed against real-world
# archived source -- TRUCOLST.LST and others) is an ordinary type-0
# float assignment, same as if '#' had been written explicitly. Callers
# must treat a missing group(2) as type 0, not skip the statement --
# see the two call sites below.
#
# Whitespace before '=' is also tolerated (BALL.LST's own
# 'nb_balls        = playmode+1', column-aligned source formatting) --
# confirmed inconsequential to the real token bytes: BALL.GFA's own
# compiled form for this exact line has no space at all
# ('nb_balls#=playmode#+1'), so the source whitespace is purely
# cosmetic and safely discarded, not something the real tokenizer ever
# preserved in the first place.
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?\s*=(?!=)")

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


def _try_bare_int_literal_array_value(rhs: str) -> bytes | None:
    """ARRAY_ASSIGN_LCP/LET_ARRAY_ASSIGN_LCP's own value expression ('the
    5' in 'arr%(0)=5') -- a THIRD, previously undocumented bare-integer
    shape, distinct from both _try_bare_int_literal_rhs's plain-scalar
    form (pft 201 + even-code 200 filler + 4 bytes, used for 'x%=5') and
    tokenize_expr's own 'was_first_token' fallback (also 201+200 filler,
    meant for a literal injected mid-expression, e.g. the '0' in a
    '0-x%' rewrite) -- this one drops the odd-code prefix AND the filler
    byte entirely: just the plain EVEN code (200) followed directly by
    the 4-byte two's-complement value, one byte shorter than either of
    those two.

    Before this existed, 'arr(i)=<bare int literal>' fell through to a
    plain `tokenize_expr(rhs, ...)` call with no array_open flag, which
    (since the value is the sole/first token of that call) always took
    tokenize_expr's own 'was_first_token' branch and emitted the WRONG,
    one-byte-longer 201+200-filler shape instead.

    CONFIRMED via a real Hatari compile (the companion GFA Decompiler
    project's Hard_Drive/TESTING/DIMPROBF.GFA, an editor-resave of a
    file this project's own tokenizer had produced): 'arr%(0)=5',
    'arr2%(1,1)=9', 'px%(0)=50', 'py%(0)=50', 'px%(1)=150', and
    'py%(1)=150' all independently confirm the same shorter [200][4-byte
    value] shape with no exceptions -- discovered because the extra byte
    this project used to emit shifted every subsequent listing offset,
    which is suspected (not yet independently proven) to be the actual
    cause of a real "3 bombs" GFA-BASIC editor crash on load reported
    against a file containing several of these assignments together
    (see DEVLOG.md's DIMPROBE investigation for the full writeup).
    Base-10 only, same scope restriction as _try_bare_int_literal_rhs --
    &H/&O/&X literals and non-literal expressions still fall through to
    the caller's own generic tokenize_expr call unchanged.
    """
    m = _INT_RHS_RE.match(rhs)
    if not m:
        return None
    out = bytearray()
    out.append(200)
    push32(out, int(m.group(1)))
    return bytes(out)


def _append_v_h_data_end(out: bytearray, comment: tuple[int, str] | None) -> None:
    """V~H=/_DATA='s own end-of-line sentinel+pad. CONFIRMED via real
    ground truth (Hard_Drive/TESTING/VTILDE.PRG's 'V~H=-1' and
    VTILDE2.PRG's 'V~H=100'/'V~H=0', all three independently) to use a
    plain ZERO pad byte after the sentinel, not the repeated-sentinel pad
    _append_comment's own no-comment branch uses for ordinary statements
    (confirmed separately via RTLIBTS2 -- see that function's own
    docstring). Comment handling isn't independently confirmed for this
    statement type, so that case still defers to the shared, already-
    confirmed _append_comment rather than guessing.
    """
    if comment is not None:
        _append_comment(out, comment)
        return
    out.append(70)
    if len(out) & 1:
        out.append(0)


def _v_h_data_rhs(rhs: str, pool: IdentPool) -> bytes:
    """V~H=/_DATA='s own RHS encoding -- a THIRD, previously undocumented
    odd+filler convention, distinct from both _try_bare_int_literal_rhs's
    normal-assignment form (also pft 201, but with the pair's own even
    code 200 as filler) and tokenize_expr's own 'was_first_token' branch
    (also 200, meant for a narrower case -- an injected literal inside a
    larger rewritten expression, not a genuine whole-RHS bare literal).
    V~H=/_DATA= skip both of those paths entirely (never routing through
    _try_bare_int_literal_rhs the way a normal 'var=literal' assignment
    does), so a bare integer RHS here used to fall into tokenize_expr's
    'was_first_token' branch and get the WRONG (200) filler -- and a
    negative one fell into the entirely unrelated unary-minus/packed-
    float path instead, having never been recognized as one combined
    literal token at all.

    CONFIRMED via a real Hatari compile (the companion GFA Decompiler
    project's Hard_Drive/TESTING/VTILDE2.PRG, 'V~H=100'/'V~H=0') plus the
    original VTILDE.PRG's own 'V~H=-1': all three encode as plain
    [pft 201][0x00 filler][4-byte two's-complement value] -- zero filler,
    not 200, and handling the sign directly in the 32-bit value rather
    than via a separate unary-minus opcode.
    """
    m = _INT_RHS_RE.match(rhs)
    if m:
        out = bytearray()
        out.append(201)
        out.append(0)
        push32(out, int(m.group(1)))
        return bytes(out)
    return tokenize_expr(rhs, 0, len(rhs), pool)


# Name may start with a digit (BEAN_ADV.LST's own numeric-named
# 'PROCEDURE'/'@name'/'GOSUB' targets have a label-declaration sibling
# too: '1730:'), matching this project's other "old line-numbered
# BASIC" fixes. Declaration position is unambiguous (line starts with
# an identifier-shaped token immediately followed by ':' and nothing
# else), unlike a bare number appearing as a GOTO/GOSUB *target*
# reference inside the shared generic expression tokenizer, which is
# genuinely ambiguous against a plain numeric literal there and is
# deliberately NOT widened here.
_LABEL_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.]*):\s*$")
_COMMENT_RE = re.compile(r"^\s*(!|REM\b)\s?(.*)$", re.IGNORECASE)
_TRAILING_COMMENT_RE = re.compile(r"(?<!&)!(?!=)(.*)$")


def _split_trailing_comment(text: str) -> tuple[str, str | None]:
    """Splits 'STATEMENT ! comment' into (statement, comment_text) --
    comment_text is None if there's no trailing '!' comment. Doesn't
    split on '!' inside a string literal, or on a '!' that's actually
    the BOOLEAN type sigil (e.g. 'a!=0 !COMMENT' has a sigil
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
        # right at this position, not just any alnum character. A hex/
        # octal/binary literal's digit run (e.g. the "H8B" of "&H8B")
        # also matches that same alnum-run shape, so it has to be
        # excluded explicitly too -- confirmed real: MSX_EMUL.LST's own
        # 'CASE &H98 TO &H9B,&H88 TO &H8B! VDP Ports' has an unspaced
        # trailing comment glued directly onto a hex literal, which was
        # being misdetected as a sigil use on a variable named "h8b".
        looks_like_sigil = (
            re.search(r"[A-Za-z_][A-Za-z0-9_]*$", text[:i]) is not None
            and re.search(r"&[HOX][0-9A-Fa-f]*$", text[:i], re.IGNORECASE) is None
        )
        if looks_like_sigil:
            continue
        return text[:i].rstrip(" "), (len(text[:i]) - len(text[:i].rstrip(" ")), text[i + 1 :])
    return text, None


def encode_line(text: str, pool: IdentPool, declared_arrays: set[str] = frozenset()) -> bytes:
    """Encodes one source line's CONTENT bytes (no outer 2-byte size
    prefix -- the caller adds that). Returns b'' for a blank line (encoded
    by the caller as a bare REM, matching what real files contain for
    blank editor lines).

    declared_arrays: lowercased names DIM'd with no explicit suffix
    anywhere in the file (see tokenize_source's own pre-scan) -- used to
    recognize bare array-element assignment ('arr(i)=5' with no sigil)
    without guessing at every bare 'name(...)=...' being an array. See
    that matcher's own comment for why a blanket rule isn't safe.
    """
    global _CURRENT_DECLARED_ARRAYS
    _CURRENT_DECLARED_ARRAYS = frozenset(declared_arrays)
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
        # A genuinely blank line (no leading "'" at all) is otherwise
        # identical to a bare "'" -- both are an empty-text lcp=460
        # REM-type line -- and MUST carry the same 0x0D raw-passthrough
        # terminator + even-byte pad that every other REM/'/DATA branch
        # emits. Omitting it here was a real, confirmed bug: the real
        # editor's raw-passthrough reader for this line type keeps
        # consuming bytes past the missing terminator, silently
        # swallowing the following line(s)' own length-prefix and
        # content as garbage "comment" text until it happens to hit an
        # unrelated 0x0D elsewhere in the stream. Confirmed real via
        # BALL.LST's own several genuinely-blank (whitespace-only, no
        # "'") lines -- reloading our tokenized output in the real
        # GFA-BASIC editor and resaving showed corrupted binary-garbage
        # comment lines exactly at each one, verified against the same
        # editor's own resave of the real ground-truth BALL.GFA.
        push16(out, 460)
        out.append(0x0D)
        if len(out) & 1:
            out.append(0)
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
    # followed by the params as generic tokens, a plain ")" token, a
    # plain "=" token, then the body expression.
    #
    # Ground truth: DIR_BAUM.GFA's own 'DEFFN fsfirst(adr_dateiname%,
    # attribut%)=GEMDOS(...)' encodes the tail after the params as
    # SEPARATE ")" (pft 32) and "=" (pft 69) tokens -- NOT the combined
    # ")=" token (pft 57) array-element assignment uses, despite the
    # visual resemblance. An earlier revision of this matcher used pft
    # 57 here based on that resemblance alone ("confirmed by the shared
    # type semantics, not a separate ground-truth sample" -- its own
    # words); this was wrong, caught by finally tracing real bytes.
    #
    # The manual documents params as optional ("DEFFN func[(x1,x2,...)]
    # =expression"), and real source confirms a genuinely paren-less form
    # exists (not just empty parens): the same file's 'DEFFN
    # fsnext=GEMDOS(&H4F)' (no '(' at all in source) encodes as
    # [lcp 228][name-ref]["="][value expression] -- no '(' or ')' token
    # at all, just the bare "=". Matched as a separate case rather than
    # making the parens optional in one regex, since the two forms are
    # genuinely different byte shapes, not just an empty-vs-nonempty
    # params list.
    m = re.match(r"^DEFFN\s+([A-Za-z_][A-Za-z0-9_.]*\$?)\((.*)\)=(.*)$", body, re.IGNORECASE)
    m_bare = None if m else re.match(r"^DEFFN\s+([A-Za-z_][A-Za-z0-9_.]*\$?)=(.*)$", body, re.IGNORECASE)
    if m or m_bare:
        if m:
            fname, params, value_expr = m.groups()
        else:
            fname, value_expr = m_bare.groups()
            params = None
        ftype = 15 if fname.endswith("$") else 14
        # resolve_var appends GFAVST[type_] ("$" for type 15) after the
        # pool name automatically -- storing it WITH the sigil already
        # attached doubles it up on decode ("name$" -> "name$$").
        idx = pool.get_or_add(ftype, fname[:-1] if ftype == 15 else fname)
        push16(out, 228)
        out.append(240 + ftype)
        push16(out, idx)
        if params is not None:
            # lcp=228 isn't special-cased in the decoder at all (falls
            # straight into the generic stream after "DEFFN "), so unlike
            # "> PROCEDURE" (lcp 216/24, which auto-synthesizes "(" on
            # decode), the "(" has to be an explicit token here -- same
            # fix as "> FUNCTION" (lcp 1796) above.
            out.append(PFT_TEXT_TO_CODE["("])
            if params.strip():
                out += tokenize_expr(params, 0, len(params), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
        # pft 69, not PFT_TEXT_TO_CODE["="] (which resolves to 19, the
        # first of three real, distinct opcodes that all render as "="
        # -- see PFT_CODE_OVERRIDE's own docstring). Confirmed directly:
        # DIR_BAUM.GFA's real bytes for both DEFFN forms use 69 here.
        #
        # NOT yet byte-identical past this point: the GEMDOS(...) call in
        # the value expression differs from DIR_BAUM.GFA's real bytes by
        # one filler byte inside its own literal-argument encoding (0xCB
        # vs this tool's 0xCA) -- round-trips correctly through this
        # project's own Tokenizer/Detokenizer pair either way, but per
        # this project's own README, "the real editor's LOAD validation
        # is strict about matching its own tokenizer byte-for-byte", so
        # this specific value could still matter there. Appears to be a
        # pre-existing, DEFFN-unrelated quirk in how a literal filler
        # byte gets chosen for a GEMDOS(...) call's own comma-separated
        # arguments generally, not something this fix touches -- flagged
        # rather than chased further here.
        out.append(69)
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    # "*var%=expr" -- pre-3.0-era pointer-dereference write (the manual's
    # ARRPTR()/'*' address-of operator, used here as an lvalue: write
    # THROUGH the value var% holds as a pointer, not to var% itself).
    # GFALCT[122] (lcp 488) is literally the text '*', which the generic
    # header-keyword-text mechanism already emits for free -- the rest is
    # just [var-ref]['='][value expr], the same tail DEFFN above uses.
    #
    # Ground truth: DIR_BAUM.GFA's own '*adr_name%=name$' encodes as
    # [lcp 488]['%'-type var-ref for adr_name%][pft 69 "="][$-type var-ref
    # for name$]. Only the '%' (type 2) form is confirmed -- GFALCT also
    # has a second, unconfirmed '*'-text lcp (484, one type-slot below
    # 488 in what looks like the same per-sigil-type spacing this
    # project's other statement families use) that may cover a different
    # sigil; not guessed at here since there's no ground truth for it.
    m = re.match(r"^\*([A-Za-z_][A-Za-z0-9_.]*)%=(.*)$", body)
    if m:
        name, value_expr = m.groups()
        idx = pool.get_or_add(2, name)
        push16(out, 488)
        # Byte-form when it fits, same as every other var-ref site (see
        # tokenize_expr's own comment on this) -- confirmed here too:
        # DIR_BAUM.GFA's 'adr_name%' (pool index 9) used the byte form.
        if idx < 256:
            out.append(224 + 2)
            out.append(idx)
        else:
            out.append(240 + 2)
            push16(out, idx)
        out.append(69)
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

    # "OB_NEXT(tree,obj)=value" / "OB_HEAD(...)" / "OB_TAIL(...)" /
    # "OB_TYPE(...)" / "OB_FLAGS(...)" / "OB_STATE(...)" / "OB_X(...)" /
    # "OB_Y(...)" / "OB_W(...)" / "OB_H(...)" -- direct object-structure
    # field writes. GFA_BASIC_Version_3_Interpreter_User_Manual_OCR.pdf
    # p.391 lists these together, explicitly addressed "for both reading
    # and writing" with no dereference wrapper -- unlike OB_SPEC (listed
    # right alongside them in the same sentence, but semantically
    # different: it returns a POINTER to further data, read via
    # CHAR{}/LONG{}/etc., never assigned to directly) -- so OB_SPEC is
    # deliberately excluded here.
    #
    # Same shape as MID$( above: dedicated lcp (each GFALCT text already
    # includes its own opening paren), the args, the combined ")=" token,
    # then the value expression -- confirmed byte-for-byte against a real
    # editor-saved .GFA for 'OB_STATE(tree_main%,obj_main_items&)=0'
    # (found via the companion GFA Decompiler project's MULTI_V1 crash
    # investigation: an earlier version of that project's own matcher
    # used a '{OB_STATE(...)}=value' curly-brace wrapper here, reasoning
    # by analogy with OB_SPEC/BYTE{/WORD{ -- since the bare form didn't
    # tokenize at the time -- but that wrapper compiles to a completely
    # different, broken instruction sequence: real GFA-BASIC calls two
    # distinct GFA3BLIB routines, a dedicated OB_STATE getter then a
    # dedicated setter; the curly-brace-compiled form calls the same
    # getter routine twice and does a raw pointer store instead, applying
    # BCLR to an address rather than a value and corrupting the target --
    # confirmed as the actual root cause of a real Bus Error crash).
    # lcp=988 itself (OB_STATE's own) was independently confirmed via
    # that same real .GFA file's own bytes, matching gfalist_reference's
    # documented value exactly.
    #
    # The value expression needs array_open=True -- confirmed against
    # that same real .GFA file: a bare integer RHS here ('=0') encodes as
    # plain pft 201 followed directly by the raw 4-byte value with NO
    # pft-200 filler byte (the "odd+filler" convention
    # _try_bare_int_literal_rhs implements is specific to that other,
    # unrelated context; this one is one byte shorter). array_open=True
    # reproduces that exact shorter encoding for a bare integer literal
    # here; a real expression RHS (e.g. 'BCLR(OB_STATE(tree,obj),0)')
    # goes through tokenize_expr's normal function-call path regardless
    # of this flag, so it isn't affected.
    m = re.match(r"^(OB_NEXT|OB_HEAD|OB_TAIL|OB_TYPE|OB_FLAGS|OB_STATE|OB_X|OB_Y|OB_W|OB_H)\((.+)\)=(.+)$", body, re.IGNORECASE)
    if m:
        kw, args_expr, value_expr = m.group(1).upper(), m.group(2), m.group(3)
        lcp = {"OB_NEXT": 968, "OB_HEAD": 972, "OB_TAIL": 976, "OB_TYPE": 980,
               "OB_FLAGS": 984, "OB_STATE": 988, "OB_X": 996, "OB_Y": 1000,
               "OB_W": 1004, "OB_H": 1008}[kw]
        push16(out, lcp)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE[")="])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool, array_open=True)
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

    # "BYTE{addr}=value" / "WORD{...}" / "CARD{...}" / "LONG{...}" /
    # "INT{...}" / "CHAR{...}" / "FLOAT{...}" / "DOUBLE{...}" /
    # "SINGLE{...}" -- direct memory-write statements, each its own
    # dedicated lcp whose GFALCT text already includes the opening
    # brace, followed by the address expression, the combined "}="
    # token (pft 67 -- same shape as array-element assignment's ")="
    # token), then the value expression. Found via the companion GFA
    # Decompiler project's MULTI_V1 round-trip work hitting
    # 'CHAR{LPEEK(OB_SPEC(...))}=pack_name$' -- only BYTE/WORD/CARD/LONG
    # were confirmed/implemented before; INT/CHAR/FLOAT/DOUBLE/SINGLE
    # share the exact same "}=" shape, same GFALCT-brace convention.
    m = re.match(r"^(BYTE|WORD|CARD|LONG|INT|CHAR|FLOAT|DOUBLE|SINGLE)\{(.+)\}=(.+)$", body, re.IGNORECASE)
    if m:
        kw, addr_expr, value_expr = m.group(1).upper(), m.group(2), m.group(3)
        lcp = {"CARD": 932, "BYTE": 936, "LONG": 924, "WORD": 1672,
               "INT": 928, "CHAR": 940, "FLOAT": 944, "DOUBLE": 948,
               "SINGLE": 492}[kw]
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
        # delay_expr's literal is coerced to REAL (pft 221), same as
        # EVEN(/ODD(''s argument -- confirmed 2026-09-10 via COVFULL.LST
        # vs a real editor's own COVFULL9.GFA ('EVERY 400 GOSUB ...''s
        # 400 is 'dd 00' + double_to_gfa_float(400.0), not the plain
        # integer this tokenizer produced without this seed). AFTER not
        # independently confirmed but assumed the same shape (untested).
        out += tokenize_expr(delay_expr, 0, len(delay_expr), pool, seed_force_float_literal=True)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        # Byte-sized var-index form (224-239) whenever the pool index
        # fits in a byte, word-sized (240-255) only once it doesn't --
        # this call site always used the wide form regardless, confirmed
        # wrong via the same COVFULL9.GFA comparison ('EVERY 400 GOSUB
        # timer_target''s target used the byte form, idx fit in a byte).
        if idx < 256:
            out.append(224 + 11)
            out.append(idx)
        else:
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
    # encoded as an ordinary marker+index mid-expression reference, same
    # as the BUTTON/IBOX/OBOX forms below.
    #
    # Marker+index convention: the companion Detokenizer decodes
    # pft 224-239 as "group (pft-224), 1-BYTE index" and pft 240-255 as
    # "group (pft-240), 2-BYTE index" (gfa_detokenizer.py's own
    # `elif 224 <= pft <= 239` / `elif 240 <= pft <= 255` branches) --
    # two genuinely different reference widths, not interchangeable.
    # This block used to always use the 240+group/2-byte form (same as
    # plain "GOSUB name" elsewhere in this file, which IS confirmed
    # correct for that construct via extensive separate ground truth).
    # That was wrong for the ON-MENU-GOSUB family specifically: a real
    # GFA-BASIC -s debug compile (MENUFNPS.PRG/.GFA, companion GFA
    # Decompiler project, 2026-09-09) byte-diffed "ON MENU GOSUB
    # menu_gosub_target" against this tokenizer's own output and showed
    # the real file using marker 224+11 (0xEB) with a single index byte
    # (0x00), not 240+11 (0xFB) with a two-byte index (0x0000) -- same
    # final index value, different, and previously wrong, encoding
    # width/marker. Confirmed by round-trip: a bare index with NO marker
    # at all here decoded as garbage PFT bytes ("AND"/"OR"/...) instead
    # of the target name -- that earlier finding is still valid, it just
    # didn't distinguish 224 vs 240 since both are valid marker ranges
    # and this construct's index (small, event-trap-table-sized) never
    # happened to exceed 255 in whatever was tested before. Only the
    # plain "ON MENU GOSUB" form has real-file confirmation so far; the
    # MESSAGE/KEY/BUTTON/IBOX/OBOX variants below are changed the same
    # way on the strength of being byte-for-byte structurally identical
    # code (same author, same block, same unverified assumption) to the
    # one now-confirmed case, but are NOT independently ground-truth-
    # confirmed yet -- flag for re-verification if a real compile of any
    # of those five ever becomes available.
    m = re.match(r"^ON\s+MENU\s+MESSAGE\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 536)
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+KEY\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 540)
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+BUTTON\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 544)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+IBOX\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 952)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^ON\s+MENU\s+OBOX\s+(.+?)\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        args_expr, target = m.group(1), m.group(2)
        idx = pool.get_or_add(11, target)
        push16(out, 956)
        out += tokenize_expr(args_expr, 0, len(args_expr), pool)
        out.append(PFT_TEXT_TO_CODE["GOSUB"])
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    # Plain "ON MENU GOSUB target" -- lcp=532 bakes in the trailing
    # "GOSUB " already, so the body is just the target. This is the
    # one variant with direct real-file confirmation -- see the long
    # comment above this block.
    m = re.match(r"^ON\s+MENU\s+GOSUB\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 532)
        out.append(224 + 11)
        out.append(idx & 0xFF)
        _append_comment(out, comment)
        return bytes(out)

    # "GOSUB name" -- confirmed from ground truth that GOSUB targets
    # resolve through the PROCEDURE name group (type 11), the same group
    # "> PROCEDURE"/"@name" use -- NOT the label group (type 10) that
    # GOTO targets and "name:" declarations use. Handled as its own
    # dedicated case (rather than falling through _SIMPLE_KEYWORDS into
    # the generic expression tokenizer's bare-identifier-is-a-label
    # fallback) specifically so the target lands in the right group.
    # Target name may start with a digit ("GOSUB 1370", confirmed real
    # source -- BEAN_ADV.LST, calling a numeric-named PROCEDURE, same
    # "old line-numbered BASIC" naming this project's own '@2030' fix
    # documents). Safe to widen here for the same reason as '@name':
    # "GOSUB " is itself a unique, unambiguous keyword prefix.
    m = re.match(r"^GOSUB\s+([A-Za-z0-9_][A-Za-z0-9_.]*)\s*(?:\((.*)\))?\s*$", body, re.IGNORECASE)
    if m:
        name, args = m.groups()
        idx = pool.get_or_add(11, name)
        push16(out, 244)
        push16(out, idx)
        if args is not None:
            # "GOSUB name(args)" -- a parameterized-call GOSUB form.
            # Ground truth: TRUCOLST.GFA's own many 'GOSUB
            # zest_button(upper_x%,upper_y%,...)' sites all encode as
            # [lcp 244][word idx] then a plain '(' token, the args as
            # ordinary generic tokens, then a plain ')' token -- same
            # tail shape DEFFN's with-params form uses above, just no
            # trailing '=' (this is a statement, not an assignment).
            out.append(PFT_TEXT_TO_CODE["("])
            if args.strip():
                out += tokenize_expr(args, 0, len(args), pool)
            out.append(PFT_TEXT_TO_CODE[")"])
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

    # Name may start with a digit -- same "old line-numbered BASIC"
    # naming as '@2030'/'GOSUB 1370' (BEAN_ADV.LST's own
    # 'PROCEDURE 1370', called both ways). Safe to widen here too:
    # "> PROCEDURE " is a unique, unambiguous keyword prefix.
    m = re.match(r"^>\s*PROCEDURE\s+([A-Za-z0-9_][A-Za-z0-9_.]*)\s*(\((.*)\))?\s*$", body, re.IGNORECASE)
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

    # Bare "PROCEDURE name(args)" -- no leading "> " -- confirmed real
    # usage from GFA_BASIC_3-5_Compiler_User_Manual_OCR.pdf p.35 (a C-
    # function-replacement example listing uses bare "FUNCTION doub(a%)"
    # directly; PROCEDURE is its confirmed structural sibling, same
    # lcp-24/216 duplicate-text pairing this project's own convention
    # elsewhere always treats as interchangeable). Needs the same
    # dedicated name-pool resolution as "> PROCEDURE" above (lcp 24
    # shares 216's exact decode behavior, including the auto-synthesized
    # "(" -- both appear in the same INDENT_AFTER/type-decode groups) --
    # NOT the generic _SIMPLE_KEYWORDS path, which was confirmed to
    # silently resolve the name into the wrong pool group and corrupt it.
    m = re.match(r"^PROCEDURE\s+([A-Za-z0-9_][A-Za-z0-9_.]*)\s*(\((.*)\))?\s*$", body, re.IGNORECASE)
    if m:
        idx = pool.get_or_add(11, m.group(1))
        push16(out, 24)
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
        r"^FOR\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?=(.*?)\s+TO\s+(.*?)(?:\s+STEP\s+(.*))?$",
        body, re.IGNORECASE,
    )
    if m:
        name, sigil, start_expr, to_expr, step_expr = m.groups()
        # No suffix: Float is GFA-Basic's documented default variable
        # type ("As this is the default type, no postfix is necessary"),
        # so a bare loop variable is an ordinary type-0 float.
        type_ = SUFFIX_TO_TYPE.get(sigil) if sigil else 0
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
            # TO's own value uses pft 223 (the same "right after a binary
            # operator" form -- see just_saw_binary_arith_op's own
            # docstring) -- but ONLY when this FOR loop also has an
            # explicit STEP clause (a different LCP entirely,
            # FOR_STEP_EXPR_LCP vs FOR_NO_STEP_LCP -- confirmed via a
            # real editor's own 'FOR a%=10 TO 1 STEP -1': TO's value (1)
            # is 'df 80 00 00 00 00 00 03 ff'). A step-LESS loop's TO
            # value stays plain instead -- confirmed via 'FOR a%=1 TO
            # 10' (no STEP): TO's value (10) is 'c8 00 00 00 0a', NOT
            # pft 223 -- applying this unconditionally regressed that
            # case, so it's gated on step_expr being present.
            out += tokenize_expr(to_expr, 0, len(to_expr), pool, seed_binary_arith_op=step_expr is not None)
            if step_expr is not None:
                out.append(PFT_TEXT_TO_CODE["STEP"])
                # STEP's own value is a plain integer, sign baked directly
                # into a two's-complement pft-200 literal -- confirmed
                # 2026-09-10 via a real editor's own 'FOR a%=10 TO 1 STEP
                # -1'/'FOR a%=1 TO 10 STEP 2': '-1' is 'c8 ff ff ff ff'
                # (no unary-minus opcode 30, no pft-221 packed-float
                # rewrite at all -- the sign is just baked into the plain
                # 32-bit value) and '2' is 'c8 00 00 00 02' (plain, no
                # skip byte -- NOT the odd-filler form a fresh
                # tokenize_expr call's was_first_token would otherwise
                # produce). Only a simple signed-integer STEP value is
                # handled this way; anything else (a variable, an
                # expression) falls through to the general tokenizer,
                # unconfirmed shape, not guessed at.
                step_stripped = step_expr.strip()
                step_num_m = re.match(r"^-?\d+$", step_stripped)
                if step_num_m:
                    out.append(200)
                    push32(out, int(step_stripped) & 0xFFFFFFFF)
                else:
                    out += tokenize_expr(step_expr, 0, len(step_expr), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(
        r"^FOR\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?=(.*?)\s+DOWNTO\s+(.*)$",
        body, re.IGNORECASE,
    )
    if m:
        name, sigil, start_expr, to_expr = m.groups()
        type_ = SUFFIX_TO_TYPE.get(sigil) if sigil else 0
        lcp = FOR_DOWNTO_LCP.get(type_) if type_ is not None else None
        if lcp is not None:
            idx = pool.get_or_add(type_, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(start_expr, 0, len(start_expr), pool, array_open=True)
            out.append(PFT_TEXT_TO_CODE["DOWNTO"])
            out += tokenize_expr(to_expr, 0, len(to_expr), pool)
            _append_comment(out, comment)
            return bytes(out)

    m = re.match(r"^NEXT(\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?)?\s*$", body, re.IGNORECASE)
    if m and m.group(2):
        name, sigil = m.group(2), m.group(3)
        type_ = SUFFIX_TO_TYPE.get(sigil) if sigil else 0
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

    # Bare array-element INC/DEC ('DEC arr(i)', no sigil at all) -- same
    # DIM-pre-scan gating as bare array-element assignment above.
    # Confirmed real: BALL.LST's own 'DEC snd_timer(i)' (snd_timer
    # DIM'd bare).
    m = re.match(
        r"^(INC|DEC)\s+([A-Za-z_][A-Za-z0-9_.]*)\((.*?)\)\s*$", body, re.IGNORECASE,
    )
    if m and m.group(2).lower() in declared_arrays:
        kw, name, index_expr = m.group(1).upper(), m.group(2), m.group(3)
        lcp = (ARRAY_INC_LCP if kw == "INC" else ARRAY_DEC_LCP)[4]
        idx = pool.get_or_add(4, name)
        push16(out, lcp)
        push16(out, idx)
        out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
        out.append(PFT_TEXT_TO_CODE[")"])
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(
        r"^(INC|DEC)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?\s*$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil = m.group(1).upper(), m.group(2), m.group(3)
        type_ = SUFFIX_TO_TYPE.get(sigil) if sigil else 0
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

    # Bare array-element ADD/SUB/MUL/DIV ('ADD arr(i),v', no sigil at all) --
    # same DIM-pre-scan gating as bare array-element INC/DEC above. Confirmed
    # real: BALL.LST's own 'ADD b_state(which), b_dir(which)' (b_state DIM'd
    # bare).
    m = re.match(
        r"^(ADD|SUB|MUL|DIV)\s+([A-Za-z_][A-Za-z0-9_.]*)\((.*?)\)\s*,\s*(.*)$", body, re.IGNORECASE,
    )
    if m and m.group(2).lower() in declared_arrays:
        kw, name, index_expr, value_expr = m.groups()
        kw = kw.upper()
        lcp = ARRAY_ARITH_LCP.get(kw, {})[4]
        idx = pool.get_or_add(4, name)
        push16(out, lcp)
        push16(out, idx)
        out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
        out.append(PFT_TEXT_TO_CODE[")"])
        out.append(PFT_TEXT_TO_CODE[","])
        out += tokenize_expr(value_expr, 0, len(value_expr), pool)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(
        r"^(ADD|SUB|MUL|DIV)\s+([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?\s*,\s*(.*)$", body, re.IGNORECASE,
    )
    if m:
        kw, name, sigil, value_expr = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        type_ = SUFFIX_TO_TYPE.get(sigil) if sigil else 0
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
        r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)\s*=(.*)$",
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
            arr_val_lit = _try_bare_int_literal_array_value(rhs)
            out += arr_val_lit if arr_val_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
            _append_comment(out, comment)
            return bytes(out)

    # Bare array-element assignment ('arr(i)=5', no sigil at all) -- Float
    # is the documented default type, same reasoning as bare scalars
    # above, so this is type 4 ("#(", ARRAY_ASSIGN_LCP's own entry for
    # it), same shape as the sigil-explicit form just above.
    #
    # Deliberately gated on declared_arrays (populated by a whole-file
    # DIM pre-scan in tokenize_source) rather than matching ANY bare
    # 'name(...)=...' -- a blanket rule would make this fire on
    # OB_STATE(tree,obj)=value and friends (real syntax with a genuinely
    # different meaning, already handled by its own dedicated matcher
    # above and confirmed real via the manual) whenever this matcher
    # happened to run first, and there's no way to tell "known array"
    # from "some other bare-name(...)=... construct" by shape alone.
    # Only a name actually DIM'd bare earlier in the file is treated as
    # an array here. Confirmed real: MOLMASSE.LST's own 'DIM
    # atomgewicht(69)' then later 'gewicht(atomanzahl&)=atomgewicht
    # (stelle&)*menge&'.
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\((.*?)\)\s*=(.*)$", body)
    if m and m.group(1).lower() in declared_arrays:
        name, index_expr, rhs = m.groups()
        lcp = ARRAY_ASSIGN_LCP[4]
        idx = pool.get_or_add(4, name)
        push16(out, lcp)
        push16(out, idx)
        out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
        out.append(PFT_TEXT_TO_CODE[")="])
        arr_val_lit = _try_bare_int_literal_array_value(rhs)
        out += arr_val_lit if arr_val_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
        _append_comment(out, comment)
        return bytes(out)

    m = re.match(r"^LET\s+", body, re.IGNORECASE)
    if m:
        let_rest = body[m.end() :]
        arr_m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])\((.*?)\)\s*=(.*)$", let_rest)
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
                arr_val_lit = _try_bare_int_literal_array_value(rhs)
                out += arr_val_lit if arr_val_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
                _append_comment(out, comment)
                return bytes(out)
        # "LET arr(i)=expr", no sigil -- same DIM-pre-scan gating as the
        # non-LET bare array form above. Confirmed real: SPRIT_ED.LST's
        # own 'Let Sprite_foreground(X%,Y%)=1'.
        bare_arr_m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\((.*?)\)\s*=(.*)$", let_rest)
        if bare_arr_m and bare_arr_m.group(1).lower() in declared_arrays:
            name, index_expr, rhs = bare_arr_m.groups()
            lcp = LET_ARRAY_ASSIGN_LCP[4]
            idx = pool.get_or_add(4, name)
            push16(out, lcp)
            push16(out, idx)
            out += tokenize_expr(index_expr, 0, len(index_expr), pool, array_open=True)
            out.append(PFT_TEXT_TO_CODE[")="])
            arr_val_lit = _try_bare_int_literal_array_value(rhs)
            out += arr_val_lit if arr_val_lit is not None else tokenize_expr(rhs, 0, len(rhs), pool)
            _append_comment(out, comment)
            return bytes(out)
        am = _ASSIGN_RE.match(let_rest)
        if am and (not am.group(2) or am.group(2) in SUFFIX_TO_TYPE):
            name, sigil = am.group(1), am.group(2)
            type_ = SUFFIX_TO_TYPE[sigil] if sigil else 0
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

    # DATE$=/TIME$= -- dedicated pseudo-variable assignment lcps (1632,
    # 1628), checked ahead of the generic string-assignment fallback
    # below specifically so they don't get treated as an ordinary user
    # string variable literally named "date"/"time" (which the generic
    # path would silently do, since 'DATE$=...' matches its assignment
    # regex just as well as any real string variable would). Confirmed
    # directly from gfalct: 'TIME$=' and 'DATE$=' are their own distinct
    # entries, textually identical to no other lcp.
    m = re.match(r"^(DATE|TIME)\$\s*=\s*(.*)$", body, re.IGNORECASE)
    if m:
        lcp = 1632 if m.group(1).upper() == "DATE" else 1628
        push16(out, lcp)
        rhs = m.group(2)
        out += tokenize_expr(rhs, 0, len(rhs), pool)
        _append_comment(out, comment)
        return bytes(out)

    # V~H=/_DATA= -- same pseudo-variable-assignment shape as DATE$=/
    # TIME$= above, confirmed from GFA_BASIC_Version_3_Interpreter_User_
    # Manual_OCR.pdf: 'V~H=x' sets the internal VDI handle (p.353,
    # "Sets the internal VDI handle... to the value x", e.g. 'V~H=-1');
    # '_DATA=dp%(j%)' sets the DATA pointer (p.542-543's own worked
    # example). Bare 'V~H'/'_DATA' (no '=', read forms) aren't in this
    # project's missing-keyword list at all -- already resolved
    # generically as ordinary PFT/SFT function tokens.
    m = re.match(r"^V~H\s*=\s*(.*)$", body, re.IGNORECASE)
    if m:
        push16(out, 1624)
        rhs = m.group(1)
        out += _v_h_data_rhs(rhs, pool)
        _append_v_h_data_end(out, comment)
        return bytes(out)
    m = re.match(r"^_DATA\s*=\s*(.*)$", body, re.IGNORECASE)
    if m:
        push16(out, 1692)
        rhs = m.group(1)
        out += _v_h_data_rhs(rhs, pool)
        _append_v_h_data_end(out, comment)
        return bytes(out)

    # INLINE addr%,length -- reserves a `length`-byte area within the
    # program, initially zero-filled (GFA_BASIC_Version_3_Interpreter_
    # User_Manual_OCR.pdf p.83: "addr: 4-byte integer variable... length:
    # Integer constant, less than 32700... The reserved area always
    # begins at an even address and it is initially filled with zeros.
    # When implementing INLINE this address is written to the integer
    # variable addr."). Confirmed byte-for-byte against a real editor-
    # saved MULTI_V1.GFA (companion GFA Decompiler project): lcp=1668,
    # then addr encoded as an ordinary bare variable-reference token
    # (not baked into the header the way most other statements' operands
    # are), then a literal ',' token, then marker byte 70, then -- if
    # the position is now odd -- one zero pad byte for even alignment,
    # then the `length` raw bytes themselves. The displayed `,length` in
    # a decoded listing is derived from the actual embedded byte count,
    # not stored as its own encoded number anywhere -- so there is
    # nothing to encode for it beyond emitting exactly that many bytes.
    # The real editor's own Help-key LOAD/SAVE/DUMP/CLEAR menu is the
    # only way to populate this area with real non-zero content at
    # edit time (out of reach for a text-only .lst source); this
    # project's own convention is to zero-fill and load real content at
    # runtime instead, with a BLOAD right after (see the companion GFA
    # Decompiler project's README for MULTI_V1's own confirmed working
    # example of this pattern). Per the manual, no trailing comment is
    # possible on this statement (the byte that would hold it is
    # reserved for the data area instead) -- any comment is dropped.
    m = re.match(r"^INLINE\s+([A-Za-z_][A-Za-z0-9_.]*%)\s*,\s*(\d+)\s*$", body, re.IGNORECASE)
    if m:
        varexpr, length_str = m.groups()
        length = int(length_str)
        push16(out, 1668)
        out += tokenize_expr(varexpr, 0, len(varexpr), pool)
        out.append(PFT_TEXT_TO_CODE[","])
        out.append(70)
        if len(out) % 2 == 1:
            out.append(0)
        out += bytes(length)
        return bytes(out)

    m = _ASSIGN_RE.match(body)
    if m and (not m.group(2) or m.group(2) in SUFFIX_TO_TYPE):
        name, sigil = m.group(1), m.group(2)
        type_ = SUFFIX_TO_TYPE[sigil] if sigil else 0
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
        rest = body[rest_start:]
        if lcp == 1424 and re.search(r"\bOFFSET\b", rest, re.IGNORECASE):
            # CLIP has a distinct lcp (1432, not 1424) when it carries an
            # optional 'OFFSET dx,dy' clause -- confirmed 2026-09-10 via
            # COVFULL.LST vs a real editor's own COVFULL9.GFA ('CLIP
            # 0,0,100,100 OFFSET 5,5' uses lcp 1432, plain 'CLIP
            # 0,0,100,100' uses 1424). The OFFSET clause's own dx,dy
            # values are plain, no special literal encoding needed --
            # only the header lcp differs, so 'rest' (including the
            # literal 'OFFSET' text) still tokenizes through the
            # ordinary generic path below unchanged.
            lcp = 1432
        if lcp == 1596 and not _split_top_level_commas(rest.strip())[1:]:
            # BITBLT has two distinct compiled forms depending on its
            # own argument count/shape: the documented 3-array form
            # ('BITBLT s_mfdb%(),d_mfdb%(),par%()', lcp=1596, the only
            # one this project had ever encoded) versus a single bare
            # pointer-argument form ('BITBLT addr%', an INLINE-style MFDB
            # struct address) which uses a genuinely different lcp,
            # 1604. CONFIRMED via a real Hatari compile (the companion
            # GFA Decompiler project's Hard_Drive/TESTING/DIMPROBF.GFA,
            # an editor-resave of a file this project's own tokenizer
            # had produced): 'BITBLT addr%' -> '[lcp 1604][addr%][46]',
            # NOT lcp 1596 the way this project always emitted regardless
            # of argument shape. Gated on "no top-level comma" (i.e.
            # exactly one argument) rather than sniffing the argument's
            # own shape, since the 3-array form always has exactly two
            # commas and the scalar form always has none.
            lcp = 1604
        push16(out, lcp)
        if lcp in HEADER_SKIP4_LCP:
            out += b"\x00\x00\x00\x00"
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
            elif lcp == 840:
                # DIM -- see _encode_dim_list's own docstring for why
                # this can't just go through the generic tokenize_expr
                # call every other _SIMPLE_KEYWORDS statement uses.
                out += _encode_dim_list(rest, pool)
            elif lcp in _LABEL_TARGET_LCPS:
                # GOTO/RESTORE/RESUME: their bare-word target is a LABEL
                # (type 10), never a variable -- see tokenize_expr's own
                # bare_word_is_label docstring for why this can't just be
                # the plain fallthrough every other _SIMPLE_KEYWORDS
                # statement uses. Confirmed real bug fixed here, not
                # guessed: a real GFA-BASIC editor round-trip
                # (Hard_Drive/TESTING/FOO_TEST.LST/.GFA in the companion
                # GFA Decompiler project) showed 'GOTO foo' must stay
                # 'foo', while this project's own tokenizer/detokenizer
                # pair had been silently rendering it 'foo#' (the
                # default-Float sigil) -- undetected until now because
                # re-tokenizing 'foo#' produces the identical bytes,
                # making the bug invisible to a same-tool round-trip
                # check (only comparing against the real editor's own
                # output caught it). RESUME's other two lcp variants
                # (424/428) aren't in this set -- their own real-source
                # trigger is still unconfirmed (see _SIMPLE_KEYWORDS'
                # own comment), left untouched rather than guessed.
                out += tokenize_expr(rest, 0, len(rest), pool, bare_word_is_label=True)
            elif lcp in (1616, 444, 448, 852):
                # BSAVE/BGET/BPUT/BMOVE: the LAST top-level argument
                # (their own byte-count/length) uses the odd-filler
                # integer form (array_open=True, zero_filler stays its
                # own default True) -- confirmed 2026-09-10 via
                # COVFULL.LST vs a real editor's own COVFULL9.GFA
                # ('BGET #1,addr%,100''s 100 is 'c9 00 00 00 00 64', not
                # the plain form the generic tokenize_expr fallthrough
                # produces for every other argument). Earlier arguments
                # are untouched -- confirmed the SAME comparison shows
                # BGET's own first argument (#1) stays plain.
                parts = _split_top_level_commas(rest)
                for i, part in enumerate(parts):
                    if i > 0:
                        out.append(PFT_TEXT_TO_CODE[","])
                    out += tokenize_expr(part, 0, len(part), pool, array_open=(i == len(parts) - 1))
            elif lcp == 1588:
                # ARRAYFILL: the fill-value (last argument) is coerced
                # to REAL, same mechanism as EVEN(/ODD( -- confirmed
                # 2026-09-10 via COVFULL.LST vs a real editor's own
                # COVFULL9.GFA ('ARRAYFILL afill%(),7''s 7 is 'dd e0' +
                # double_to_gfa_float(7.0), not the plain integer this
                # tokenizer produced). The array reference itself
                # (first argument) is untouched.
                parts = _split_top_level_commas(rest)
                for i, part in enumerate(parts):
                    if i > 0:
                        out.append(PFT_TEXT_TO_CODE[","])
                    out += tokenize_expr(part, 0, len(part), pool, seed_force_float_literal=(i == len(parts) - 1))
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
    #
    # Name may start with a digit ("@2030", confirmed real source --
    # BEAN_ADV.LST, apparently a numeric procedure "name" carried over
    # from an old line-numbered BASIC program). Safe to widen only here,
    # unlike a bare identifier elsewhere (which could never legitimately
    # start with a digit): '@' is a unique, unambiguous marker that only
    # ever means "direct procedure/function call", so there's no risk of
    # this colliding with a numeric literal or any other construct.
    m = re.match(r"^@([A-Za-z0-9_][A-Za-z0-9_.$]*)\s*(\((.*)\))?\s*$", body)
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
    #
    # The naive version of this regex used a plain greedy '(.*)' for the
    # argument list, which silently mismatched real assignments to a
    # function-call target (e.g. 'OB_STATE(tree%,obj%)=BCLR(...)',
    # confirmed real syntax -- GFA_BASIC_Version_3_Interpreter_User_
    # Manual_OCR.pdf p.392, "addressed... for both reading and
    # writing"): backtracking let '(.*)' swallow everything up to the
    # LAST ')' in the whole line, silently absorbing the trailing
    # '=BCLR(...)' into a garbled "argument list" instead of erroring
    # loudly -- corrupting the statement instead of failing it. Fixed by
    # finding the name's own matching close paren with explicit depth
    # tracking and requiring nothing but whitespace after it, so a
    # statement shaped like this one now correctly falls through to the
    # "unrecognized statement" error below rather than being silently
    # misencoded as a bare procedure call.
    #
    # The depth-tracking loop itself also has to skip over string-literal
    # contents rather than counting every raw '(' / ')' character: a call
    # like 'scrolle("I love cubes :)")' has a ')' inside its own quoted
    # argument, which a naive scan sees as the closing paren -- confirmed
    # real via FRGTNBTS.LST's own line of exactly that shape.
    m = None
    head = re.match(r"^([A-Za-z_][A-Za-z0-9_.$]*)\s*(\()?", body)
    if head and head.group(2) is None and body[head.end(1):].strip() == "":
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.$]*)\s*(\((.*)\))?\s*$", body)
    elif head and head.group(2) is not None:
        depth = 0
        close = None
        in_string = False
        k = head.end(2) - 1
        while k < len(body):
            ch = body[k]
            if in_string:
                if ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close = k
                    break
            k += 1
        if close is not None and body[close + 1 :].strip() == "":
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_.$]*)\s*(\((.*)\))\s*$", body[: close + 1])
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
        # verification). The previous version of this function omitted
        # the sentinel entirely, reasoning (wrongly) that our own
        # detokenizer's lenient `while pos < len(raw)` loop tolerates
        # its absence -- true, but the real editor doesn't accept files
        # missing it.
        #
        # The pad byte's own VALUE is a plain zero, NOT another 70. An
        # earlier version of this comment claimed the opposite (citing
        # an "RTLIBTS2" ground-truth compile whose files no longer
        # exist in the repo to re-check), but that is contradicted by
        # FIVE independent, directly-isolated confirmations in one
        # session (2026-09-09): a Hatari -s debug compile named
        # MENUFNPS.PRG/.GFA (containing RETURN, END, a bare PROCEDURE
        # header, and MENU OFF -- all bare, comment-less, odd-length-
        # so-far lines) byte-diffed exactly against this tokenizer's
        # own output for the same source, with the ONLY remaining
        # difference at this exact pad position, in every one of the
        # five: real GFA-BASIC wrote 0x00, this code wrote 70. Given
        # the RTLIBTS2 claim can no longer be re-verified and this
        # fresh evidence is unambiguous and reproducible, trusting the
        # zero-byte pad. (See DEVLOG.md, "GFA Tokenizer sep[18] bug
        # FOUND+FIXED" entry, if this ever needs re-litigating against
        # new ground truth.)
        out.append(70)
        if len(out) & 1:
            out.append(0)
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
# GOTO=232, RESTORE=236, RESUME=420 (see _SIMPLE_KEYWORDS below) -- the
# three confirmed-real _SIMPLE_KEYWORDS statements whose bare-word
# argument is a LABEL reference, not a variable. See the
# bare_word_is_label call site's own comment for why this needs special
# handling instead of the plain tokenize_expr(rest, ...) fallthrough
# every other entry uses.
_LABEL_TARGET_LCPS = {232, 236, 420}

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
    # Confirmed 2026-09-10 via COVFULL.LST vs a real editor's own
    # COVFULL9.GFA (companion GFA Decompiler project): bare 'MONITOR'
    # (lcp 192, from GFALCT index 48) was simply missing from this
    # dict, so it fell through to being tokenized as an ordinary bare
    # identifier/expression statement instead -- wrong lcp (240) and
    # two spurious extra bytes for a fabricated variable reference.
    "MONITOR": 192,
    "DO WHILE": 196, "DO UNTIL": 200, "LOOP WHILE": 204, "LOOP UNTIL": 208,
    "ELSE IF": 64,
    # lcp=1024 confirmed directly from ground truth: sky.lst's own GFALCT
    # table dump (a companion project's test corpus) contains the literal
    # line 'DATA 1024,"ALERT "', i.e. gfalct index 256 * 4 = 1024. ALERT
    # had never been exercised by any program in that corpus, so nothing
    # caught the gap until the companion GFA Decompiler project tried to
    # round-trip a real ALERT statement.
    "ALERT": 1024,
    # RESUME: gfalct has three entries sharing display text ('RESUME' at
    # 420 with no trailing space, 'RESUME ' at 424 and 428 both with one)
    # -- same "lowest lcp sharing that text is the plain/general-purpose
    # form" convention as every other _SIMPLE_KEYWORDS entry. 420 alone
    # covers every real form: bare 'RESUME', 'RESUME label'/'RESUME 0',
    # AND 'RESUME NEXT' -- confirmed directly (round-tripped clean) that
    # 'NEXT' isn't baked into a dedicated lcp the way 'DO WHILE' is; it
    # rides through as an ordinary GFAPFT keyword token (code 168, "NEXT")
    # inside the generic expression that follows RESUME's header, the
    # same as any other bare keyword-shaped identifier in an expression.
    # lcp 424/428's own distinct real-source triggers are still
    # unconfirmed -- left unhandled rather than guessed.
    "RESUME": 420,
    # Batch found systematically via the companion GFA Decompiler
    # project's command-coverage regression suite (deliberately
    # exercising every gfalct/gfapft/gfasft entry, rather than waiting
    # for real-world code to happen to hit a gap) rather than one at a
    # time from real decompiled code. Each lcp is the single or lowest
    # gfalct index sharing that keyword's display text, same convention
    # as every other entry above; none of these have more than one
    # non-generic real-argument form to disambiguate.
    "SDPOKE": 404, "SLPOKE": 408, "DELAY": 440, "ABSOLUTE": 1012,
    "RANDOMIZE": 1020, "CHDRIVE": 1248, "DIR": 1276, "FILES": 1300,
    "MKDIR": 1324, "KILL": 1332, "RMDIR": 1336, "PAUSE": 1376,
    "QSORT": 1380, "SSORT": 1384, "DEFINT": 1524, "DEFFLT": 1528,
    "DEFBYT": 1532, "DEFWRD": 1536, "DEFBIT": 1540, "DEFSTR": 1544,
    "TRON": 572, "TROFF": 584,
    # CHDIR: never implemented -- explains a pre-existing workaround in
    # the companion GFA Decompiler project (its MULTI_V1 round-trip test
    # substituted a REM comment for the real CHDIR line rather than
    # testing it, from before this statement's absence was diagnosed).
    "CHDIR": 1244,
    # ON ERROR GOSUB / ON BREAK GOSUB / ON BREAK CONT: confirmed via the
    # independent gfalist/sky reference implementation (mmuman/gfalist,
    # vendored in the companion GFA Decompiler project) that these are
    # each their own fixed compound lcp -- NOT special-cased anywhere in
    # gfalist's own decode switch, meaning (unlike bare GOSUB's dedicated
    # PROCEDURE-group target resolution) the handler name after them is
    # just an ordinary trailing generic expression, same as any other
    # _SIMPLE_KEYWORDS entry. This corrects an earlier wrong assumption
    # that they'd need a GOSUB-style dedicated case.
    "ON ERROR GOSUB": 516, "ON ERROR": 512,
    "ON BREAK GOSUB": 528, "ON BREAK CONT": 524, "ON BREAK": 520,
    "ON MENU MESSAGE GOSUB": 536, "ON MENU KEY GOSUB": 540,
    "ON MENU BUTTON": 544, "ON MENU GOSUB": 532, "ON MENU": 548,
    # Bulk batch: every remaining gfalct statement keyword with zero
    # code path anywhere in this file, found by diffing the full gfalct
    # table against every lcp literal referenced in this module and
    # cross-checking each candidate against gfalist_reference/sky.c's
    # own decode switch (mmuman/gfalist, vendored in the companion GFA
    # Decompiler project). None of these appear in that switch at all
    # -- meaning, on the decode side, they need no special operand
    # parsing beyond "print the keyword text, then decode whatever
    # generic expression tokens follow" -- the same mechanism every
    # other _SIMPLE_KEYWORDS entry already uses, just never confirmed
    # per-keyword before. Where a base form and a "#"-channel form both
    # exist for the same keyword (OPENW/OPENW #, etc.), the "#" form's
    # lcp is used, confirmed as the real one via hell.lst's own
    # 'OPENW #1,140,80,340,220,&X1' example. Excluded from this batch:
    # bare PROCEDURE/FUNCTION (only the '> '-prefixed forms are
    # confirmed real usage anywhere in ground truth), and _DATA=/V~H=
    # (assignment-shaped, need a dedicated pre-check like DATE$=/TIME$=
    # rather than a plain keyword entry). Not yet individually verified
    # against a real compile -- see regression_tests/README.md in the
    # companion project for the verification status of this batch.
    "PLOT": 352, "PSET": 356, "ALINE": 360, "HLINE": 364,
    "ARECT": 368, "APOLY": 372, "ACHAR": 376, "ACLIP": 380,
    "COLOR": 384, "RECORD": 436, "ATEXT": 452, "LOCATE": 500,
    "MENU": 556, "MENU OFF": 560, "MENU KILL": 564, "TEXT": 596,
    "RCALL": 604, "CALL": 608, "FORM INPUT": 612, "LINE": 620,
    "SETCOLOR": 844, "VDISYS": 860, "GEMSYS": 876, "VOID": 960,
    "GET": 1028, "PUT": 1040,
    # OPENW/CLOSEW/CLEARW/TITLEW/INFOW: confirmed from
    # GFA_BASIC_Version_3_Interpreter_User_Manual_OCR.pdf (p.313-316)
    # that BOTH the bare and "#"-prefixed forms are real, distinct
    # source syntax -- 'OPENW nr [,x_pos,y_pos]' (simplified quadrant
    # window) vs 'OPENW #n,x,y,w,h,attr' (full AES form), and
    # 'CLEARW/TITLEW/INFOW [#] n' (the manual's own worked example uses
    # bare 'CLEARW 1'/'TITLEW 4,...' directly alongside '#'-prefixed
    # 'TOPW #1'/'CLOSEW #1'). OPENW/CLOSEW/CLEARW have their own
    # separate lcp per form (bare vs '#'); TITLEW/INFOW share a single
    # lcp for both spellings (gfalct has only the '#' text baked in, so
    # bare input still decodes with a synthesized '#' -- same class of
    # canonical-reformatting round-trip as case-folding elsewhere, not
    # a literal-text-preserving one). Longest-key-first matching in
    # _match_leading_keyword means a '#'-typed input tries the more
    # specific entry first, avoiding the double-'#' bug a single
    # '#'-only entry caused when fed bare input.
    "OPENW #": 1068, "OPENW": 1064, "CLOSEW #": 1080, "CLOSEW": 1076,
    "CLEAR": 1084, "CLEARW #": 1092, "CLEARW": 1088, "TOPW #": 1096,
    "TITLEW #": 1100, "TITLEW": 1100, "INFOW #": 1104, "INFOW": 1104,
    "DEFLINE": 1108, "GRAPHMODE": 1112, "DEFMOUSE": 1116,
    "DEFLIST": 1124, "DEFMARK": 1128, "DEFNUM": 1132, "DEFTEXT": 1136,
    "DEFFILL": 1140, "BOX": 1148, "PBOX": 1152, "RBOX": 1156,
    "PRBOX": 1160, "CIRCLE": 1164, "PCIRCLE": 1172, "ELLIPSE": 1180,
    "PELLIPSE": 1188, "ERROR": 1196, "FILL": 1200, "HIDEM": 1208,
    "LSET": 1216, "NEW": 1224, "QUIT": 1236, "HTAB": 1280,
    "VTAB": 1284, "EXEC": 1292, "FIELD": 1296, "TOUCH #": 1304,
    "EDIT": 1312, "FILESELECT": 1316, "NAME": 1320, "MOUSE": 1328,
    "RSET": 1340, "SETTIME": 1344, "SGET": 1348, "SHOWM": 1352,
    "SPUT": 1356, "SYSTEM": 1364, "VSYNC": 1368, "HARDCOPY": 1372,
    "POLYLINE": 1388, "POLYFILL": 1392, "POLYMARK": 1396, "INSERT": 1400,
    "RENAME": 1408, "STICK": 1412, "SOUND": 1416, "WAVE": 1420,
    "CLIP": 1424, "FULLW": 1444, "DRAW": 1480, "SETMOUSE": 1496,
    "KEYPAD": 1500, "KEYTEST": 1504, "KEYGET": 1508, "KEYLOOK": 1512,
    "KEYPRESS": 1516, "KEYDEF": 1520, "BOUNDARY": 1548, "LIST": 1552,
    "LLIST": 1556, "SAVE": 1560, "PSAVE": 1564, "CHAIN": 1568,
    "RUN": 1572, "LOAD": 1580, "SETDRAW": 1584, "DUMP": 1592,
    "BITBLT": 1596, "STORE": 1608, "RECALL": 1612, "SPRITE": 1636,
    "OPTION": 1640, "RC_COPY": 1652, "MODE": 1656, "WRITE": 1664,
    "VSETCOLOR": 1676, "CURVE": 1688, "MAT ADD": 1696, "MAT SUB": 1704,
    "MAT CPY": 1712, "MAT XCPY": 1716, "MAT DET": 1720, "MAT NEG": 1724,
    "MAT ABS": 1728, "MAT NORM": 1732, "MAT READ": 1736, "MAT PRINT": 1740,
    "MAT TRANS": 1744, "MAT CLR": 1748, "MAT SET": 1752, "MAT ONE": 1756,
    "MAT BASE": 1760, "MAT QDET": 1764, "MAT INPUT": 1768, "MAT RANG": 1772,
    "MAT MUL": 1776, "MAT INV": 1792, "DMASOUND": 1800, "DMACONTROL": 1804,
    "MW_OUT": 1808,
    # Bare "FUNCTION name(args)" -- confirmed real usage directly from
    # GFA_BASIC_3-5_Compiler_User_Manual_OCR.pdf p.35 ("FUNCTION
    # doub(a%)" in a C-function-replacement example). Unlike bare
    # PROCEDURE (lcp 24, needs the same dedicated name-pool case as "> "
    # PROCEDURE), lcp 40 round-trips correctly as a plain generic
    # keyword -- confirmed directly, not assumed: the name+args that
    # follow encode fine through the ordinary trailing-expression path,
    # unlike PROCEDURE's name which corrupts if routed that way.
    "FUNCTION": 40,
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


def _split_top_level_commas(text: str) -> list[str]:
    """Splits text at top-level ',' separators (outside quotes and
    parens), e.g. DIM's own 'x(50),y(50)' declaration list.
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
        elif depth == 0 and c == ",":
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


_DIM_ITEM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)([#$%!&|])?\((.*)\)$", re.DOTALL)


def _encode_dim_list(rest: str, pool: IdentPool) -> bytes:
    """Encodes DIM's own comma-separated array-declaration list.

    Can't just hand the whole list to tokenize_expr like every other
    _SIMPLE_KEYWORDS statement's argument list: parse_var_ref
    deliberately never matches a BARE name immediately followed by '('
    as an array (see its own docstring -- doing so would make 'SIN(x)'
    lose to a same-shaped bare-array match in tokenize_expr's generic
    var-ref/keyword race). Inside DIM specifically there's no such
    ambiguity -- every comma-separated item is unambiguously a
    declaration, never a builtin call -- so this walks the list itself
    and emits each item as a genuine array var-ref (the same byte shape
    tokenize_expr's own var-ref branch produces for a SUFFIXED array
    like '&(', just computed directly here for the bare/type-4 case too)
    followed by its dimension-list expression and closing ')'.
    ARRAY_ASSIGN_LCP-style bare-array support elsewhere in this file
    already treats an unsuffixed array as type 4 ('#(', Float being the
    documented default type) -- reused here for consistency.

    Confirmed real, and a genuine bug fix: BALL.LST's own 'DIM
    x(50),y(50),x1(50),x2(50),y1(50),y2(50),sp$(50),spb$(50)' -- before
    this, the six bare entries were silently encoded as a scalar var-ref
    immediately followed by a stray '(', a literal, and ')' as three
    unrelated tokens (text-identical on decode, since concatenating
    "x#" + "(" + "50" + ")" still LOOKS like "x#(50)" on a round-trip
    display -- but structurally wrong). The real GFA-BASIC 3.60TT
    compiler rejected the resulting file with a "Division by zero"
    compile-time error; reloading it in the real editor and resaving it
    as text (which doesn't require a full compile) never caught this.
    """
    out = bytearray()
    for i, item in enumerate(_split_top_level_commas(rest)):
        if i:
            out.append(PFT_TEXT_TO_CODE[","])
        stripped = item.strip()
        m = _DIM_ITEM_RE.match(stripped)
        if not m:
            # Not a recognized array-declaration shape (e.g. a bare
            # scalar DIM, if that's ever real source) -- fall through to
            # the generic expression tokenizer for just this one item,
            # same as before this function existed.
            out += tokenize_expr(stripped, 0, len(stripped), pool)
            continue
        name, sigil, dims = m.groups()
        type_ = SUFFIX_TO_TYPE[sigil + "("] if sigil else 4
        idx = pool.get_or_add(type_, name)
        if idx < 256:
            out.append(224 + type_)
            out.append(idx)
        else:
            out.append(240 + type_)
            push16(out, idx)
        out += tokenize_expr(dims, 0, len(dims), pool, array_open=True)
        out.append(PFT_TEXT_TO_CODE[")"])
    return bytes(out)


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
        if not kw[:1].isalpha() or not kw[-1:].isalnum():
            # Punctuation-led keywords (e.g. "~EVNT_TIMER(1)", GFA's
            # direct XBIOS/GEMDOS/AES call syntax) attach directly to
            # whatever follows -- no space/paren separator to require.
            # Same for punctuation-TRAILED keywords like "OPENW #" --
            # gfalct's own display text already bakes the '#' in, so the
            # channel number that follows in real source ("OPENW #1,...")
            # attaches directly to the keyword text with no extra
            # separator of its own required either.
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


_DIM_LINE_RE = re.compile(r"^\s*DIM\s+(.*)$", re.IGNORECASE)
_DIM_BARE_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\(")

# GFA-BASIC's built-in VDI parameter-block arrays -- always available
# bare, with no DIM required, unlike a user array. Confirmed real:
# EASYMINT.LST's own 'CONTRL(0)=101'/'GCONTRL(0)=48' etc. (a companion
# project's real-world archive) has no DIM anywhere in the file for any
# of these, and the official compiler's own bundled test archive
# (hell.lst) independently lists all eight as recognized built-in names
# with their own numeric codes (DATA 880,"PTSIN(" / 884,"PTSOUT(" /
# 888,"INTIN(" / 892,"INTOUT(" / 904,"GINTIN(" / 908,"GINTOUT(" /
# 916,"GCONTRL(") -- the standard screen-VDI set (CONTRL/INTIN/INTOUT/
# PTSIN/PTSOUT) plus the printer/GDOS variant's reduced 3-array set
# (GCONTRL/GINTIN/GINTOUT, no GPTSIN/GPTSOUT found in the same table),
# not project-specific guesswork.
_BUILTIN_BARE_ARRAYS = {
    "contrl", "intin", "intout", "ptsin", "ptsout",
    "gcontrl", "gintin", "gintout",
}


def _scan_declared_bare_arrays(lines: list[str]) -> set[str]:
    """Whole-file pre-scan for DIM'd array names with no explicit type
    suffix -- used to gate bare array-element assignment recognition (see
    encode_line's own comment on why a blanket 'any bare name(...)=...'
    rule isn't safe). Deliberately whole-file rather than only-names-
    seen-so-far: a PROCEDURE body can reference an array DIM'd later in
    the file (or in an outer scope encountered after it in source order),
    and this pre-scan doesn't need to be precise about declaration
    order -- it only needs to avoid false positives on names that are
    genuinely never DIM'd bare anywhere.

    Deliberately simple substring/regex scanning, not full statement
    parsing: a DIM line's own comma-separated entries are each either
    'name(' (bare -- what this collects) or 'name<sigil>(' (explicit
    suffix, already handled by the sigil-explicit matcher). The name
    pattern's own character class ([A-Za-z0-9_.]) can't include a sigil
    character, so it naturally can't match through to the '(' for a
    suffixed entry ('atomgewicht#(' has no name+'(' substring at all,
    since '#' breaks the match) -- no separate suffix check needed.
    Good enough to find real bare array declarations without needing
    this file's full expression grammar just for a pre-pass.
    """
    declared: set[str] = set(_BUILTIN_BARE_ARRAYS)
    for line in lines:
        m = _DIM_LINE_RE.match(line)
        if not m:
            continue
        for nm in _DIM_BARE_NAME_RE.finditer(m.group(1)):
            declared.add(nm.group(1).lower())
    return declared


def tokenize_source(text: str) -> bytes:
    # A trailing 0x1A (Ctrl-Z) is the classic DOS/CP-M text-file EOF
    # marker, not GFA-BASIC syntax -- confirmed real: FCOMP2.LST and
    # INSTALLR.LST (a companion project's own real-world archive) both
    # end their last real line's CRLF with a lone 0x1A byte, which
    # previously fell through to "unrecognized statement" as if it were
    # source text. DOS text-mode reads stop at the first Ctrl-Z, so
    # truncating there (not just stripping trailing whitespace) matches
    # what the real editor's own file-load would have seen.
    cut = text.find("\x1a")
    if cut != -1:
        text = text[:cut]
    pool = IdentPool()
    # NOT text.splitlines() -- it breaks on every Unicode line-boundary
    # code point (NEL U+0085, LS U+2028, PS U+2029, etc.), and this file
    # is read as latin1, so byte 0x85 (an accented character in the
    # Atari ST charset -- e.g. French "à") decodes straight to U+0085 and
    # gets treated as a mid-comment line break. Confirmed real:
    # FRGTNBTS.LST's own "Jusqu'à 70 étoiles..." comment (the "à" is
    # 0x85) was silently split into two fake lines, corrupting the
    # comment and desyncing every subsequent line number in error
    # messages. Splitting only on the format's actual CRLF/LF convention
    # avoids this.
    norm = text.replace("\r\n", "\n")
    if norm == "":
        lines: list[str] = []
    else:
        lines = norm.split("\n")
        if norm.endswith("\n"):
            # A trailing newline shouldn't produce a phantom extra blank
            # line -- matches str.splitlines()'s own behavior, which
            # text.split("\n") doesn't share.
            lines.pop()
    declared_arrays = _scan_declared_bare_arrays(lines)
    encoded: list[bytes] = []
    for line in lines:
        try:
            content = encode_line(line, pool, declared_arrays)
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
    # sep[18]: real GFA-BASIC-editor-saved files consistently show
    # sep[18] == sep[16] == sep[17] (all three sitting at the START of
    # the listing, not near its end) -- confirmed against SEVEN
    # independent real saved files: the GFA-BASIC 3.60TT compiler's own
    # bundled default.gfa/hell.gfa test files (both containing
    # PROCEDURE definitions, both with the classic identifier pool
    # only -- no extra literal-constant pool), PLUS three Hatari-
    # compiled ground-truth probes built for the companion GFA
    # Decompiler project (AESPROBE/WINPROBE/MENUFNPS -- resaved by the
    # real editor after a naive sep[19]-4 guess here bombed the editor
    # 3 times on load for the PROCEDURE-containing one, MENUFNPROBE).
    # A previous placeholder guessed sep[19]-4 instead -- that number
    # only ever matched by coincidence, on trivial test files (like
    # gb36test_archive's default2/3/4.gfa) whose ENTIRE listing is just
    # the 4-byte END_OF_PROGRAM sentinel with no real content, making
    # "start of listing" (sep[16]) and "listing_end - 4" the same
    # number purely because the listing itself is only 4 bytes long.
    # (Two OTHER real files in that same bundled-test corpus,
    # default5.gfa and sky.gfa, show sep[17]/sep[18] diverging from
    # sep[16] by a large amount -- real GFA-BASIC appears to intern an
    # extra literal-constant pool for programs with unusual numeric
    # literals (default5's is entirely &H/&O/&X radix-prefixed
    # constants) between the identifier pool and the listing proper in
    # some cases. This tokenizer does not build any such extra pool --
    # everything is inlined directly into the token stream, the same
    # way the companion Detokenizer decodes it -- so for every file
    # THIS tokenizer itself produces, sep[16]/[17]/[18] are correctly
    # always identical: there is no other pool to point past.)
    sep[18] = sep[16]
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
    return len(text.replace("\r\n", "\n").split("\n"))


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
                line_count = len(text.replace("\r\n", "\n").split("\n"))
                window["-INFO-"].update(f"{line_count} lines  |  {src_full_path.stat().st_size:,} bytes")
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
