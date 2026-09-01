# GFA Tokenizer for PC

![GFA Tokenizer](app_image.jpg)

**Direction: `.lst` -> `.gfa`** (readable source in, tokenized binary out). For the reverse direction (`.gfa` -> `.lst`), see the companion [GFA Detokenizer](https://github.com/) project.

Converts readable GFA-BASIC `.lst` source listings into the tokenized `.gfa` binary format the Atari ST GFA-BASIC editor loads and saves. Produced files can be loaded into the real GFA-BASIC editor and compiled with the real compiler.

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

1. Writes the fixed 164-byte header: a 38-entry pointer table (`sep[]`) giving the byte bounds of the identifier pool and the program listing.
2. Scans each source line, building the 16-group identifier pool (one group per variable/array/label/procedure sigil) as new names are discovered, in first-use order.
3. Encodes each line into `[lcp keyword code][statement-specific header bytes][token-stream bytes]`, using the same keyword/operator tables the companion Detokenizer uses (`gfa_token_tables.json`, from [gfalist](https://github.com/mmuman/gfalist) by Peter Backes, GPL-2.0).
4. Indentation in the source `.lst` is informational only and is not written anywhere — the tokenized format never stores it; the real editor re-derives display indentation purely from each line's `lcp` code when it loads the file.

## Verification

Every construct this tool supports has been confirmed against the real GFA-BASIC editor and compiler running under emulation (Hatari) — not just round-tripped through this project's own tokenizer/detokenizer pair, which is more lenient than the real editor about some byte-level details. All **423 real GFA-BASIC 3.x statement keywords** have a confirmed working encoding. The full verification corpus (an independent compiler's own bundled test archive plus hand-curated edge cases, ~5,900 lines) round-trips byte-for-byte clean.

## Supported Constructs

- Scalar assignment (`x%=5`, `y$="text"`, all six sigils: `#` `$` `%` `!` `&` `|`, with or without an explicit `LET`) and **array-element assignment** (`a%(i)=5`, index expression can be arbitrary), with a full expression on the right-hand side (arithmetic, comparisons, function/built-in calls, string literals, numbers in decimal/`&H`/`&O`/`&X`). Array-element *reads* work anywhere an expression is expected (e.g. `y%=a%(0)+3`).
- Every built-in function/operator in the keyword tables, including the ones whose names collide with the array-reference sigils (`STR$(`, `CHR$(`, `OCT$(`, `MID$(`, `SHL&(`, and the ~30 others like them) — these resolve as the built-in, not a same-named user array.
- `MID$(str$,pos,len)=value$`, the in-place substring-assignment statement form (distinct from `MID$(` used as a read-only function).
- `DIM`, `ARRAYFILL`.
- `IF`/`ENDIF`/`ELSE`/`ELSE IF`, `DO`/`LOOP`, `DO WHILE`/`LOOP UNTIL` and the other `DO`/`LOOP` compound forms, `WHILE`/`WEND`, `REPEAT`/`UNTIL`, `SELECT`/`CASE`/`DEFAULT`/`ENDSELECT`, `FOR`/`NEXT` (numeric/integer loop variable types, with or without an explicit `STEP`).
- `INC`/`DEC` and the `ADD`/`SUB`/`MUL`/`DIV` compound-assignment statements (`INC i%`, `ADD i%,5`, `INC a%(i)`) for both scalar and array-element targets.
- `LOCAL`, comments (`'` and `REM`, both standalone and trailing `!`-comments — correctly distinguished from a `!` single-precision sigil like `a!=0`), blank lines, labels (`name:`), `$directive`/`.directive` metacommand lines (raw passthrough, e.g. `$m 1000000`, `.ifndef X`).
- Bare and `>`-prefixed `PROCEDURE name(args)` / `FUNCTION name(args)` declarations, `DEFFN name(args)=expr`, bare `RETURN` / `RETURN value`, and procedure calls via `@name(args)` or bare `name(args)`.
- `GOTO`, `GOSUB` (including `AFTER`/`EVERY ... GOSUB` and every `ON ERROR`/`ON BREAK`/`ON MENU ... GOSUB` event-trap form), `ON`, `RESTORE`, `READ`, `DATA`, `END`, `STOP`, `CONT`, `SWAP`, `ERASE`, `CLR`, `INPUT`, `LINE INPUT`, `POKE`/`DPOKE`/`LPOKE`/`SPOKE`, `BYTE{`/`WORD{`/`CARD{`/`LONG{`/`INT{`/`CHAR{`/`FLOAT{`/`DOUBLE{`/`SINGLE{`/`{` memory-write statements, `OPEN`/`CLOSE`, `OUT`/`OUT&`/`OUT%`, `SEEK`/`RELSEEK`, `BSAVE`/`BLOAD`, `BPUT`/`BGET`, `BMOVE`, `RESERVE`, `INLINE addr%,length`, `DELETE`, `CLS`, `PRINT`/`LPRINT`, `~`-prefixed direct calls (`~EVNT_TIMER(1)`, `~GEMDOS(...)`), the full graphics/window/object-tree/mouse-keyboard/file command set (`BOX`, `CIRCLE`, `OPENW`, `OB_STATE`, `KEYGET`, `LOF(#`, and the rest), and GEMDOS/XBIOS/BIOS `L:`/`W:` size-cast call arguments.
- Hex/octal/binary numeric literals (`&H1F`, `&O17`, `&X101`) preserve their original notation on round-trip, including the real compiler's own lossy behavior for values needing the top bit of a 32-bit word (e.g. `&HFFFFFFFF` becomes `&H-1`, exactly as the real compiler's own output does).

## Known Limitations

- Multi-statement lines (`:`-separated) are not supported.
- A handful of statement-keyword variants have no confirmed real-source trigger yet (bare `OPENW`/`CLOSEW`/`CLEARW` without a `#` channel, `V~H=`/`_DATA=`'s rarer lcp variants, `RESUME`'s alternate forms) — the common form of each is fully supported.
- The AES/VDI/GEM system-call families (`WIND_*`, `OBJC_*`, `GRAF_*`, `MENU_*`, `RSRC_*`, `EVNT_*`, `FORM_*`, `SHEL_*`, `SCRP_*`, raw `GEMDOS`/`BIOS`/`XBIOS`) round-trip as generic function calls but haven't each been checked against a real compile for their specific argument shapes.
- Two editor-navigation bytes this format reserves per `IF`/`FOR`/`NEXT`/etc. header are zero-filled rather than computed — doesn't affect loading or compiling, but the real editor's brace-jump/fold navigation for these lines may not work as expected.
- A few identifier-pool reference sites (`PROCEDURE`/`FUNCTION` names, `@name(...)` calls, `INPUT`/`READ` variable lists) always emit the word-sized index form rather than the real editor's byte-sized form when the index is small enough — functionally correct either way, just not always byte-identical to real editor output.
- The GFAPFT operator/keyword table lists a few display texts more than once (a third `=`, `AT(`, `ROUND(`, `MIN(`, `MAX(`, and others) where this tool's choice of which underlying opcode to emit isn't independently ground-truth-confirmed yet.
