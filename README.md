# MIDI Program Remapper

A small command-line utility for remapping **MIDI Program Change** events in Standard MIDI Files using a simple text map.

It is intended for cases where a MIDI file uses one set of instrument program numbers and you need to translate them to another set, for example for a different synth, soundfont, hardware module, or custom playback setup.

## What it does

`midi-program-remapper` reads a `.mid` or `.midi` file, finds MIDI Program Change events, and rewrites matching program numbers according to a text map.

It preserves the rest of the MIDI file structure as much as possible, including non-Program-Change events and non-track chunks.

## What it does not do

This tool does **not** currently:

- rename instruments;
- edit track names;
- edit Bank Select controller events;
- interpret General MIDI instrument names automatically;
- convert between MIDI formats;
- modify notes, tempo, controller automation, pitch bend, lyrics, or meta text.

If your MIDI file selects instruments using both Bank Select and Program Change, this tool only edits the Program Change byte.

## Map file format

The first non-comment line declares the numbering base.

Use `0-based` if the numbers in your map are raw MIDI program numbers from `0` to `127`.
Use `1-based` if the numbers in your map are the more human-facing program numbers from `1` to `128`.
Feel free to also use any other base.

Inline comments are allowed. They will be ignored.
Blank lines are also ignored.

Example:

```python
1-based

1 -> 2   # Acoustic Grand Piano -> Bright Acoustic Piano
2 -> 1   # Bright Acoustic Piano -> Acoustic Grand Piano
49 -> 50 # Strings example
```

## Usage

Basic usage:

```bash
python midi_program_remapper.py input.mid program-map.txt
```

This creates an output file next to the input file using the default name:

```text
input_remapped.mid
```

Specify an output path:

```bash
python midi_program_remapper.py input.mid program-map.txt -o output.mid
```

Overwrite an existing output file:

```bash
python midi_program_remapper.py input.mid program-map.txt -o output.mid --overwrite
```

## Safety behavior

The tool refuses to overwrite an existing output file unless `--overwrite` is provided.

It also refuses to use the same path for input and output.

## Error reporting

The tool reports user-facing errors for common problems such as:

- unreadable input or map files;
- invalid map syntax;
- duplicate source rules;
- program numbers outside the valid MIDI range;
- malformed MIDI chunks or events;

## Example report

After processing, the tool prints a report showing:

- input MIDI path;
- map file path;
- output path;
- MIDI format and track count;
- total Program Change events found;
- applied remaps;
- programs present in the MIDI without a matching map rule;
- map rules that were not used.

## License

This project is **source-available** under the **PolyForm Noncommercial License 1.0.0**.

You may use, copy, modify, and redistribute this software for noncommercial purposes.

Commercial use is not permitted without separate written permission from the author.

Required Notice: Copyright © 2026 AdAstraGL.

## Attribution

If you redistribute this software or a modified version of it, keep the license terms and the required notice with your copy.

Please also clearly indicate when your version contains modifications.
