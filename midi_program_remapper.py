#!/usr/bin/env python3
"""
Remap MIDI Program Change events from a text map.

Map file example:

    0-based
    0 -> 1  # Acoustic Grand Piano -> Piano keysplit
    1 -> 0  # Bright Piano -> Acoustic Grand Piano
    # Full-line comments and blank lines are ignored.
    2 -> 23 # Octave Piano -> Strings

The first non-comment line is an integer base declaration. Program numbers in
the map are converted to raw MIDI patch numbers by subtracting that base. For
example, with "1-based", the text value 1 means raw MIDI program 0.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import struct
import sys
from typing import Iterable


BASE_RE = re.compile(r"^\s*([+-]?(?:0[xX][0-9a-fA-F]+|\d+))\s*-?\s*based\s*$", re.IGNORECASE)
MAP_RE = re.compile(r"^\s*(.+?)\s*->\s*(.+?)\s*$")


class RemapError(Exception):
    """Raised for user-facing parsing or MIDI format errors."""


@dataclass(frozen=True)
class Rule:
    src_raw: int
    dst_raw: int
    src_text: int
    dst_text: int
    line_no: int


@dataclass
class MidiInfo:
    midi_format: int | None = None
    declared_tracks: int | None = None
    division: int | None = None
    processed_tracks: int = 0


@dataclass
class RemapStats:
    seen: Counter[int] = field(default_factory=Counter)
    remapped: Counter[tuple[int, int]] = field(default_factory=Counter)
    unmapped: Counter[int] = field(default_factory=Counter)


def strip_inline_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_int(token: str, *, path: Path, line_no: int) -> int:
    text = token.strip()
    try:
        return int(text, 0)
    except ValueError as exc:
        raise RemapError(f"{path}:{line_no}: expected an integer, got {text!r}") from exc


def parse_map(path: Path) -> tuple[int, dict[int, Rule], list[Rule]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RemapError(f"Could not read map file {path}: {exc}") from exc

    base = None
    base_line_no = None
    base_index = None
    for index, raw_line in enumerate(lines):
        base_line = strip_inline_comment(raw_line)
        if not base_line:
            continue

        match = BASE_RE.match(base_line)
        if not match:
            raise RemapError(
                f"{path}:{index + 1}: first non-comment line must look like "
                f"'0-based', '1-based', or '<integer>-based'"
            )
        base_line_no = index + 1
        base_index = index
        base = parse_int(match.group(1), path=path, line_no=base_line_no)
        break

    if base is None or base_index is None:
        raise RemapError(f"{path}: map file has no base declaration")

    rules: dict[int, Rule] = {}
    ordered_rules: list[Rule] = []

    for line_no, raw_line in enumerate(lines[base_index + 1 :], start=base_index + 2):
        line = strip_inline_comment(raw_line)
        if not line:
            continue

        match = MAP_RE.match(line)
        if not match:
            raise RemapError(f"{path}:{line_no}: expected a rule like '0 -> 1'")

        src_text = parse_int(match.group(1), path=path, line_no=line_no)
        dst_text = parse_int(match.group(2), path=path, line_no=line_no)
        src_raw = src_text - base
        dst_raw = dst_text - base

        for label, text_value, raw_value in (
            ("source", src_text, src_raw),
            ("target", dst_text, dst_raw),
        ):
            if not 0 <= raw_value <= 127:
                raise RemapError(
                    f"{path}:{line_no}: {label} program {text_value} becomes raw MIDI "
                    f"program {raw_value}, outside 0..127 for base {base}"
                )

        if src_raw in rules:
            first = rules[src_raw]
            raise RemapError(
                f"{path}:{line_no}: duplicate rule for program {src_text} "
                f"(already defined on line {first.line_no})"
            )

        rule = Rule(
            src_raw=src_raw,
            dst_raw=dst_raw,
            src_text=src_text,
            dst_text=dst_text,
            line_no=line_no,
        )
        rules[src_raw] = rule
        ordered_rules.append(rule)

    return base, rules, ordered_rules


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def read_vlq(data: bytes | bytearray, offset: int, *, track_index: int, field: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise RemapError(f"track {track_index}: unterminated variable-length {field}")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset
    raise RemapError(f"track {track_index}: variable-length {field} is longer than 4 bytes")


def channel_event_data_len(status: int, *, track_index: int, offset: int) -> int:
    event_type = status & 0xF0
    if event_type in (0xC0, 0xD0):
        return 1
    if 0x80 <= event_type <= 0xE0:
        return 2
    raise RemapError(f"track {track_index}: unexpected MIDI status 0x{status:02X} at byte {offset}")


def ensure_available(
    data: bytes | bytearray,
    offset: int,
    length: int,
    *,
    track_index: int,
    event_name: str,
) -> None:
    if offset + length > len(data):
        raise RemapError(
            f"track {track_index}: {event_name} at byte {offset} extends past the end of the track"
        )


def remap_program_byte(
    track: bytearray,
    data_offset: int,
    status: int,
    rules: dict[int, Rule],
    stats: RemapStats,
) -> None:
    if (status & 0xF0) != 0xC0:
        return

    original_program = track[data_offset]
    stats.seen[original_program] += 1

    rule = rules.get(original_program)
    if rule is None:
        stats.unmapped[original_program] += 1
        return

    # The lookup is against original_program, so symmetric swaps are buffered.
    track[data_offset] = rule.dst_raw
    stats.remapped[(rule.src_raw, rule.dst_raw)] += 1


def remap_track(track_data: bytes, track_index: int, rules: dict[int, Rule], stats: RemapStats) -> bytes:
    track = bytearray(track_data)
    offset = 0
    running_status: int | None = None

    while offset < len(track):
        _, offset = read_vlq(track, offset, track_index=track_index, field="delta time")
        if offset >= len(track):
            raise RemapError(f"track {track_index}: delta time at end of track has no event")

        event_offset = offset
        status_or_data = track[offset]

        if status_or_data >= 0x80:
            status = status_or_data
            offset += 1

            if 0x80 <= status <= 0xEF:
                running_status = status
                data_len = channel_event_data_len(status, track_index=track_index, offset=event_offset)
                ensure_available(
                    track,
                    offset,
                    data_len,
                    track_index=track_index,
                    event_name=f"MIDI event 0x{status:02X}",
                )
                remap_program_byte(track, offset, status, rules, stats)
                offset += data_len
                continue

            if status == 0xFF:
                ensure_available(track, offset, 1, track_index=track_index, event_name="meta event")
                offset += 1  # meta type
                length, offset = read_vlq(track, offset, track_index=track_index, field="meta length")
                ensure_available(track, offset, length, track_index=track_index, event_name="meta event")
                offset += length
                continue

            if status in (0xF0, 0xF7):
                length, offset = read_vlq(track, offset, track_index=track_index, field="sysex length")
                ensure_available(track, offset, length, track_index=track_index, event_name="sysex event")
                offset += length
                continue

            data_len = {
                0xF1: 1,
                0xF2: 2,
                0xF3: 1,
                0xF6: 0,
                0xF8: 0,
                0xFA: 0,
                0xFB: 0,
                0xFC: 0,
                0xFE: 0,
            }.get(status)
            if data_len is None:
                raise RemapError(
                    f"track {track_index}: unsupported system status 0x{status:02X} at byte {event_offset}"
                )
            ensure_available(
                track,
                offset,
                data_len,
                track_index=track_index,
                event_name=f"system event 0x{status:02X}",
            )
            offset += data_len
            continue

        if running_status is None:
            raise RemapError(
                f"track {track_index}: running-status data byte 0x{status_or_data:02X} "
                f"at byte {offset} has no previous channel status"
            )

        status = running_status
        data_len = channel_event_data_len(status, track_index=track_index, offset=event_offset)
        ensure_available(
            track,
            offset,
            data_len,
            track_index=track_index,
            event_name=f"running MIDI event 0x{status:02X}",
        )
        remap_program_byte(track, offset, status, rules, stats)
        offset += data_len

    return bytes(track)


def parse_header_info(data: bytes) -> MidiInfo:
    if len(data) < 14 or data[:4] != b"MThd":
        raise RemapError("Input is not a Standard MIDI File: missing MThd header")

    header_len = read_u32(data, 4)
    if header_len < 6:
        raise RemapError(f"MThd chunk is too short: {header_len} bytes")
    if 8 + header_len > len(data):
        raise RemapError("MThd chunk extends past the end of the file")

    midi_format, declared_tracks, division = struct.unpack_from(">HHH", data, 8)
    return MidiInfo(
        midi_format=midi_format,
        declared_tracks=declared_tracks,
        division=division,
    )


def remap_midi(data: bytes, rules: dict[int, Rule], stats: RemapStats) -> tuple[bytes, MidiInfo]:
    info = parse_header_info(data)
    output = bytearray()
    offset = 0

    while offset < len(data):
        if offset + 8 > len(data):
            raise RemapError(f"Chunk header at byte {offset} extends past the end of the file")

        chunk_type = data[offset : offset + 4]
        chunk_len = read_u32(data, offset + 4)
        body_start = offset + 8
        body_end = body_start + chunk_len

        if body_end > len(data):
            name = chunk_type.decode("ascii", errors="replace")
            raise RemapError(f"Chunk {name!r} at byte {offset} extends past the end of the file")

        body = data[body_start:body_end]
        if chunk_type == b"MTrk":
            info.processed_tracks += 1
            body = remap_track(body, info.processed_tracks, rules, stats)

        output += chunk_type
        output += struct.pack(">I", len(body))
        output += body
        offset = body_end

    return bytes(output), info


def default_output_for(midi_path: Path) -> Path:
    if midi_path.suffix:
        return midi_path.with_name(f"{midi_path.stem}_remapped{midi_path.suffix}")
    return midi_path.with_name(f"{midi_path.name}_remapped.mid")


def prompt_path(label: str) -> Path:
    while True:
        value = input(f"{label}: ").strip().strip('"')
        if value:
            return Path(value).expanduser()


def display_program(raw_program: int, base: int) -> str:
    shown = raw_program + base
    if base == 0:
        return str(shown)
    return f"{shown} (raw {raw_program})"


def plural(count: int, singular: str, plural_text: str | None = None) -> str:
    if count == 1:
        return singular
    return plural_text if plural_text is not None else f"{singular}s"


def format_rule(rule: Rule, base: int) -> str:
    src = display_program(rule.src_raw, base)
    dst = display_program(rule.dst_raw, base)
    return f"{src} -> {dst}"


def print_report(
    *,
    input_path: Path,
    output_path: Path,
    map_path: Path,
    base: int,
    ordered_rules: Iterable[Rule],
    stats: RemapStats,
    info: MidiInfo,
) -> None:
    print(f"Input MIDI: {input_path}")
    print(f"Map file:   {map_path}")
    print(f"Output:     {output_path}")
    print(
        "MIDI:       "
        f"format {info.midi_format}, {info.processed_tracks} "
        f"{plural(info.processed_tracks, 'track')} processed"
        f", division {info.division}"
    )
    if info.declared_tracks is not None and info.declared_tracks != info.processed_tracks:
        print(
            f"Warning: header declares {info.declared_tracks} "
            f"{plural(info.declared_tracks, 'track')}, but {info.processed_tracks} MTrk chunks were found."
        )

    total_program_events = sum(stats.seen.values())
    print(
        f"Program Change events: {total_program_events} "
        f"{plural(total_program_events, 'event')} across {len(stats.seen)} "
        f"distinct {plural(len(stats.seen), 'program')}."
    )

    ordered_rules = list(ordered_rules)
    applied_rules = [
        (rule, stats.remapped[(rule.src_raw, rule.dst_raw)])
        for rule in ordered_rules
        if stats.remapped[(rule.src_raw, rule.dst_raw)]
    ]
    unused_rules = [
        rule
        for rule in ordered_rules
        if not stats.remapped[(rule.src_raw, rule.dst_raw)]
    ]

    if applied_rules:
        print("Applied remaps:")
        for rule, count in applied_rules:
            print(f"  {format_rule(rule, base)}: {count} {plural(count, 'event')}")
    else:
        print("Applied remaps: none")

    if stats.unmapped:
        print("Programs present without a map rule:")
        for raw_program, count in sorted(stats.unmapped.items()):
            print(f"  {display_program(raw_program, base)}: {count} {plural(count, 'event')}")
    else:
        print("Programs present without a map rule: none")

    if unused_rules:
        print("Map rules not used by this MIDI:")
        for rule in unused_rules:
            print(f"  {format_rule(rule, base)}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remap Program Change events in a Standard MIDI File using a text map."
    )
    parser.add_argument("midi_file", nargs="?", help="input .mid/.midi file")
    parser.add_argument("map_file", nargs="?", help="text map file")
    parser.add_argument(
        "-o",
        "--output",
        help="output MIDI file; defaults to '<input>_remapped.mid'",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing an existing output file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.midi_file).expanduser() if args.midi_file else prompt_path("MIDI file")
    map_path = Path(args.map_file).expanduser() if args.map_file else prompt_path("Map text file")
    output_path = Path(args.output).expanduser() if args.output else default_output_for(input_path)

    if output_path == input_path:
        parser.error("output path must be different from the input MIDI path")
    if output_path.exists() and not args.overwrite:
        parser.error(f"output file already exists: {output_path} (use --overwrite to replace it)")

    try:
        base, rules, ordered_rules = parse_map(map_path)
        if not rules:
            raise RemapError(f"{map_path}: map has no remap rules")

        try:
            input_data = input_path.read_bytes()
        except OSError as exc:
            raise RemapError(f"Could not read MIDI file {input_path}: {exc}") from exc

        stats = RemapStats()
        output_data, info = remap_midi(input_data, rules, stats)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output_path.write_bytes(output_data)
        except OSError as exc:
            raise RemapError(f"Could not write output MIDI file {output_path}: {exc}") from exc

        print_report(
            input_path=input_path,
            output_path=output_path,
            map_path=map_path,
            base=base,
            ordered_rules=ordered_rules,
            stats=stats,
            info=info,
        )
        return 0
    except RemapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
