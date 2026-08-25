# GFA Tokenizer for PC

![GFA Tokenizer](app_image.jpg)

**Direction: `.lst` -> `.gfa`** (readable source in, tokenized binary out). For the reverse direction (`.gfa` -> `.lst`), see the companion [GFA Detokenizer](https://github.com/) project.

Converts readable GFA-BASIC `.lst` source listings into the tokenized `.gfa` binary format the Atari ST GFA-BASIC editor loads and saves — the reverse of the companion GFA Detokenizer project. Produced files can be loaded into the real GFA-BASIC editor and compiled with the real compiler.

## Requirements

- Python 3.10+
- Optional: PySimpleGUI (`pip install PySimpleGUI`) — enables GUI mode

## Usage

**Windows:** Double-click `start_app.bat` — it creates a local `.venv` and installs dependencies from `requirements.txt`, then launches the app. Command-line arguments are passed through, e.g. `start_app.bat source.lst`.

**GUI:** run with no arguments. Source is Browse-only (pick a `.lst` file). Output defaults to the same name with a `.gfa` extension in the same folder as the source, and is freely editable before you click Convert.

**Manual (any platform):**

```
python gfa_tokenizer.py source.lst
python gfa_tokenizer.py source.lst -o output.gfa
```

## Building a standalone .exe

`build.bat` builds `dist\GFA Tokenizer.exe` via PyInstaller (no console window, custom icon).

## How It Works

1. Writes the fixed 164-byte header: a 38-entry pointer table (`sep[]`) giving the byte bounds of the identifier pool and the program listing, matching the layout the companion Detokenizer project reverse-engineered.
2. Scans each source line, building the 16-group identifier pool (one group per variable/array/label/procedure sigil) as new names are discovered, in first-use order.
3. Encodes each line into `[lcp keyword code][statement-specific header bytes][token-stream bytes]`, using the same keyword/operator tables the Detokenizer uses (`gfa_token_tables.json`, from [gfalist](https://github.com/mmuman/gfalist) by Peter Backes, GPL-2.0).
4. Indentation in the source `.lst` is informational only and is not written anywhere — the tokenized format never stores it; the real editor re-derives display indentation purely from each line's `lcp` code when it loads the file.

Verified by round-tripping generated `.gfa` files back through the companion Detokenizer and checking the output matches the original source, including a program exercising a string variable, string literal, `IF`/`ENDIF`, and a `FOR`/`NEXT` loop. Also verified by actually loading and compiling a generated `.gfa` in the real GFA-BASIC editor and compiler under emulation (Hatari) — the stronger check that caught six real encoding bugs no amount of round-tripping through this project's own tokenizer/detokenizer pair alone ever could, since that pair is lenient about things the real editor rejects outright (see Known Limitations' changelog note below).

## Supported Constructs (current coverage)

- Scalar assignment (`x%=5`, `y$="text"`, all six sigils: `#` `$` `%` `!` `&` `|`, with or without an explicit `LET`) and **array-element assignment** (`a%(i)=5`, index expression can be arbitrary), with a full expression on the right-hand side (arithmetic, comparisons, function/built-in calls, string literals, numbers in decimal/`&H`/`&O`/`&X`). Array-element *reads* work anywhere an expression is expected (e.g. `y%=a%(0)+3`).
- Every built-in function/operator in the keyword tables, including the ones whose names collide with the array-reference sigils (`STR$(`, `CHR$(`, `OCT$(`, `MID$(`, `SHL&(`, and the ~30 others like them) — these resolve as the built-in, not a same-named user array.
- `MID$(str$,pos,len)=value$`, the in-place substring-assignment statement form (distinct from `MID$(` used as a read-only function).
- `DIM`, `ARRAYFILL`.
- `IF`/`ENDIF`/`ELSE`/`ELSE IF`, `DO`/`LOOP`, `DO WHILE`/`LOOP UNTIL` and the other `DO`/`LOOP` compound forms, `WHILE`/`WEND`, `REPEAT`/`UNTIL`, `SELECT`/`CASE`/`DEFAULT`/`ENDSELECT`, `FOR`/`NEXT` (numeric/integer loop variable types, with or without an explicit `STEP`) — all verified round-trip.
- `INC`/`DEC` and the `ADD`/`SUB`/`MUL`/`DIV` compound-assignment statements (`INC i%`, `ADD i%,5`, `INC a%(i)`) for both scalar and array-element targets.
- `LOCAL`, comments (`'` and `REM`, both standalone and trailing `!`-comments — correctly distinguished from a `!` single-precision sigil like `a!=0`), blank lines, labels (`name:`), `$directive`/`.directive` metacommand lines (raw passthrough, e.g. `$m 1000000`, `.ifndef X`).
- `> PROCEDURE name(args)` / `> FUNCTION name(args)` declarations, `DEFFN name(args)=expr`, bare `RETURN` / `RETURN value`, and procedure calls via `@name(args)` or bare `name(args)` (GFA-BASIC allows both).
- `GOTO`, `GOSUB` (including `AFTER`/`EVERY ... GOSUB` and all six `ON MENU ... GOSUB` event-trap forms), `ON`, `RESTORE`, `READ`, `DATA` (opaque payload, never tokenized as an expression — matching how the real compiler treats it), `END`, `STOP`, `CONT`, `SWAP`, `ERASE`, `CLR`, `INPUT`, `LINE INPUT`, `POKE`/`DPOKE`/`LPOKE`/`SPOKE`, `BYTE{`/`WORD{`/`CARD{`/`LONG{`/`{` memory-write statements, `OPEN`, `CLOSE`, `OUT`/`OUT&`/`OUT%`, `SEEK`/`RELSEEK`, `BSAVE`/`BLOAD`, `BPUT`/`BGET`, `BMOVE` (including the `V:` address-of operator), `RESERVE`, `DELETE`, `CLS`, `PRINT`/`LPRINT`, and `~`-prefixed direct calls (`~EVNT_TIMER(1)`, `~GEMDOS(...)`) as statement keywords.
- Hex/octal/binary numeric literals (`&H1F`, `&O17`, `&X101`) preserve their original notation on round-trip, including the real compiler's own lossy behavior for values needing the top bit of a 32-bit word (e.g. `&HFFFFFFFF` becomes `&H-1`, exactly as the real compiler's own output does).

## Known Limitations

- `INLINE` (raw machine code) statements and multi-statement lines (`:`-separated) are not supported at all.
- Two editor-navigation bytes this format reserves per `IF`/`FOR`/`NEXT`/etc. header (almost certainly a back-reference the editor uses for brace-jump/fold navigation) are zero-filled rather than computed — this doesn't affect loading or compiling the file, but the real editor's navigation features for these lines may not work as expected.
- Identifier-pool indices use the real editor's own byte-sized form (`pft` 224-239) for array/variable references inside expressions when the index fits, matching real output exactly for that case — but a handful of other reference sites (`PROCEDURE`/`FUNCTION` names, `@name(...)` calls, `INPUT`/`READ` variable lists) still always emit the word-sized form. Functionally correct either way, just not always byte-identical to what the real editor would produce in those specific spots.

**Fixed 2026-08-24, all confirmed against a real GFA-BASIC editor and compiler under emulation (Hatari), not just this project's own round-trip:** the real editor rejected every file this tool had ever produced outright ("bombs" on load/compile), despite clean round-trips through this project's own Tokenizer/Detokenizer pair — six real encoding bugs invisible to that pair specifically because it's lenient about things the real editor is strict about: a missing per-statement end-of-line sentinel byte, wrong numeric-literal opcode forms, wrong array-index-literal encoding for the second-and-later value in a dimension/argument list, uppercase-vs-lowercase identifier storage, and two wrong header fields (`sep[18]`, and `sep[36]`/`[37]`'s runtime variable-storage size hint, previously left at zero). See the Tokenizer's own `tokenize_expr`/`tokenize_source` docstrings for the full detail on each.

Verified with a byte-for-byte-clean round trip (tokenize, then decode with the companion Detokenizer, then diff against the original source) across a wide range of hand-written test programs, plus the *entirety* of an independent, real GFA-BASIC 3.x compiler's own bundled test archive: a 276-line exhaustive language-feature test file, two real-world programs of 3,626 and 1,738 lines, and several smaller edge-case files — over 5,900 lines with zero diffs.
