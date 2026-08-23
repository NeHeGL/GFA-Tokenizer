# GFA Tokenizer for PC

Converts readable GFA-BASIC `.lst` source listings into the tokenized `.gfa` binary format the Atari ST GFA-BASIC editor loads and saves — the reverse of the companion [GFA Detokenizer](https://github.com/) project. Produced files can be loaded into the real GFA-BASIC editor and compiled with the real compiler.

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

Verified by round-tripping generated `.gfa` files back through the companion Detokenizer and checking the output matches the original source, including a program exercising a string variable, string literal, `IF`/`ENDIF`, and a `FOR`/`NEXT` loop.

## Supported Constructs (current coverage)

- Scalar assignment (`x%=5`, `y$="text"`, all six sigils: `#` `$` `%` `!` `&` `|`), with a full expression on the right-hand side (arithmetic, comparisons, function calls, string literals, numbers in decimal/`&H`/`&O`/`&X`).
- `IF`/`ENDIF`, `FOR`/`NEXT` (numeric/integer loop variable types), comments (`'` and `REM`, both standalone and trailing `!`-comments), blank lines, labels (`name:`).
- `> PROCEDURE name(args)` / `> FUNCTION name(args)` declarations and bare `RETURN` / `RETURN value`.
- Bare statement/procedure-call lines (`@procname(args)`, `PRINT expr`) via a generic expression fallback.

## Known Limitations

- **Array element assignment** (`a%(i)=5`) is not yet encoded — the header this needs wasn't confirmed before this first release shipped. Attempting it currently either mis-encodes or falls through to the generic expression path, which does **not** produce a correct array-target statement.
- **`FOR` loops always emit an explicit `STEP`** (defaulting to `STEP 1` when the source omits one) — functionally identical GFA-BASIC, since the compiler treats an implicit and explicit `STEP 1` the same way, but not a byte-for-byte reproduction of how the real editor encodes the terser no-step form.
- `DO`/`LOOP`/`WHILE`/`WEND`/`SELECT`/`CASE`/`ENDSELECT`/`ELSE`/`LOCAL` keywords are wired up but have not been round-trip verified as thoroughly as the constructs listed above.
- `DATA` statements, `INLINE` (raw machine code) statements, and multi-statement lines (`:`-separated) are not supported.
- Two editor-navigation bytes this format reserves per `IF`/`FOR`/`NEXT`/etc. header (almost certainly a back-reference the editor uses for brace-jump/fold navigation) are zero-filled rather than computed — this doesn't affect loading or compiling the file, but the real editor's navigation features for these lines may not work as expected.

This is a first release covering the common cases; array assignment and the less-verified control-flow keywords are the natural next additions.
