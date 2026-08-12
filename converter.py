"""
Conversion helpers: MIDI -> PDF / PNG / MusicXML / MSCZ / WAV / MP3 / OGG / FLAC / MIDI / Roblox QWERTY sheet

Strategy:
  - MuseScore (headless, via xvfb-run) handles anything that needs music
    notation rendering: PDF, PNG, MusicXML, MSCZ, and WAV (raw audio render).
  - Before notation formats (PDF/PNG/MusicXML/MSCZ) and the Roblox/QWERTY
    text sheet, we quantize a *copy* of the input MIDI to a 16th-note grid.
    Raw/human-performed MIDI has timing that doesn't line up to any clean
    rhythmic grid, so reading it at tick-level precision produces a mess of
    tiny, fragmented values. Quantizing first gives clean rhythms to work
    with. Audio and MIDI-passthrough outputs use the *original*, unquantized
    file so the actual performance timing/feel is preserved.
  - ffmpeg converts the WAV render into MP3 / OGG / FLAC. We apply loudness
    normalization + a peak limiter here, because MuseScore's internal mixer
    can sum overlapping notes above 0 dBFS and hard-clip (audible
    distortion/crackle) on dense scores.
  - MIDI output is just the original file, copied through unchanged.
  - The Roblox/QWERTY sheet maps each note to a letter on the standard
    virtual-piano keyboard layout (1234567890 / qwertyuiop / asdfghjkl /
    zxcvbnm, covering C2–C7). Sharps/flats are rounded to the nearest
    natural key since that layout only exposes white keys directly.
    Simultaneous notes are grouped as a bracketed chord, e.g. [sdf].
"""

import asyncio
import logging
import shutil
from pathlib import Path

import mido

MSCORE_BIN = "mscore"  # symlinked in the Docker image

# Formats MuseScore itself can export to directly from a single command.
_MSCORE_NATIVE = {"pdf", "png", "musicxml", "mscz", "wav"}
# Notation formats that benefit from quantizing the MIDI first.
_NOTATION_FORMATS = {"pdf", "png", "musicxml", "mscz"}
# Formats produced by re-encoding the WAV render with ffmpeg.
_FFMPEG_DERIVED = {"mp3", "ogg", "flac"}
# Plain-text virtual-piano / Roblox piano key sheet.
_TEXT_FORMATS = {"roblox"}

SUPPORTED_FORMATS = _MSCORE_NATIVE | _FFMPEG_DERIVED | _TEXT_FORMATS | {"midi"}

# Audio filter: loudnorm brings overall level to a safe target *before* any
# clipping happens, alimiter catches remaining peaks. This fixes crackle/
# distortion that comes from MuseScore's internal mix exceeding 0 dBFS.
_ANTI_CLIP_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95"

# Standard virtual-piano / Roblox-piano keyboard layout: 36 white keys
# (natural notes only) spanning C2 (MIDI 36) to C7 (MIDI 96).
_ROBLOX_KEYS = "1234567890qwertyuiopasdfghjklzxcvbnm"


def _build_roblox_key_table() -> list[tuple[str, int]]:
    # Semitone steps between consecutive natural notes starting from C:
    # C->D->E->F->G->A->B->C ...
    steps = [2, 2, 1, 2, 2, 2, 1]
    notes = [36]  # C2
    i = 0
    while len(notes) < len(_ROBLOX_KEYS):
        notes.append(notes[-1] + steps[i % 7])
        i += 1
    return list(zip(_ROBLOX_KEYS, notes))


_ROBLOX_KEY_TABLE = _build_roblox_key_table()
_ROBLOX_MIN_NOTE = _ROBLOX_KEY_TABLE[0][1]
_ROBLOX_MAX_NOTE = _ROBLOX_KEY_TABLE[-1][1]

log = logging.getLogger("converter")


class ConversionError(RuntimeError):
    pass


async def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ConversionError(f"Timed out running: {' '.join(cmd)}")

    # Always log the full output server-side — the message we raise for Discord
    # is truncated, but Railway logs should have everything for debugging.
    log.info(
        "cmd=%s returncode=%s\n--- stdout ---\n%s\n--- stderr ---\n%s",
        " ".join(cmd), proc.returncode,
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore"),
    )

    if proc.returncode != 0:
        combined = (stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")).strip()
        raise ConversionError(
            f"Command failed ({' '.join(cmd)}), exit {proc.returncode}:\n{combined[-1500:] or '(no output)'}"
        )


def _quantize_midi(src: Path, dst: Path, subdivision: int = 16) -> None:
    """Snap all event times onto a `subdivision`-note grid (default: 16th notes).

    This does not change note pitches, velocities, or instruments — only
    when each event lands in time — so the audible performance character is
    unaffected when this file is only used for notation rendering.
    """
    mid = mido.MidiFile(str(src))
    ticks_per_beat = mid.ticks_per_beat
    # ticks per grid unit: a quarter note = ticks_per_beat, so a 1/16 note
    # is ticks_per_beat / 4, generalized as ticks_per_beat * 4 / subdivision.
    grid = max(1, round(ticks_per_beat * 4 / subdivision))

    for track in mid.tracks:
        abs_time = 0
        timed_events = []
        for msg in track:
            abs_time += msg.time
            timed_events.append([abs_time, msg])

        for pair in timed_events:
            pair[0] = round(pair[0] / grid) * grid

        # Stable sort: preserves original relative order for same-tick events
        # (e.g. note_off before note_on of a different note at the same tick).
        timed_events.sort(key=lambda pair: pair[0])

        new_track = mido.MidiTrack()
        prev_time = 0
        for q_time, msg in timed_events:
            delta = max(0, q_time - prev_time)
            new_track.append(msg.copy(time=delta))
            prev_time = q_time
        track.clear()
        track.extend(new_track)

    mid.save(str(dst))


async def _get_quantized_midi(input_midi: Path, work_dir: Path) -> Path:
    quantized_path = work_dir / f"{input_midi.stem}.quantized.mid"
    if not quantized_path.exists():
        try:
            await asyncio.to_thread(_quantize_midi, input_midi, quantized_path)
        except Exception as e:
            log.warning("Quantization failed (%s), falling back to original MIDI", e)
            shutil.copy(input_midi, quantized_path)
    return quantized_path


def _nearest_roblox_key(note: int) -> str:
    # Transpose into the supported C2–C7 range, then snap to the nearest
    # natural (white) key — this is how sharps/flats get handled since the
    # base layout only exposes white keys directly.
    while note < _ROBLOX_MIN_NOTE:
        note += 12
    while note > _ROBLOX_MAX_NOTE:
        note -= 12
    return min(_ROBLOX_KEY_TABLE, key=lambda pair: abs(pair[1] - note))[0]


def _midi_to_roblox_sheet(src: Path, dst: Path, subdivision: int = 16) -> None:
    mid = mido.MidiFile(str(src))
    ticks_per_beat = mid.ticks_per_beat
    grid = max(1, round(ticks_per_beat * 4 / subdivision))
    quarter = ticks_per_beat

    merged = mido.merge_tracks(mid.tracks)
    abs_time = 0
    notes_at_tick: dict[int, list[int]] = {}
    for msg in merged:
        abs_time += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            if getattr(msg, "channel", 0) == 9:
                continue  # skip GM drum channel — not meaningful on a piano sheet
            q_tick = round(abs_time / grid) * grid
            notes_at_tick.setdefault(q_tick, []).append(msg.note)

    if not notes_at_tick:
        dst.write_text("# Không tìm thấy nốt nhạc nào trong file MIDI này.\n")
        return

    tokens = []
    prev_tick = None
    for tick in sorted(notes_at_tick):
        if prev_tick is not None:
            gap = tick - prev_tick
            if gap > quarter * 1.5:
                tokens.append("--")
            elif gap > quarter * 0.75 and gap > grid * 3:
                tokens.append("-")
        pitches = sorted(set(notes_at_tick[tick]))
        letters = [_nearest_roblox_key(p) for p in pitches]
        tokens.append(f"[{''.join(letters)}]" if len(letters) > 1 else letters[0])
        prev_tick = tick

    lines = ["# Roblox / Virtual Piano sheet — tự động tạo từ MIDI.",
             "# Nốt thăng/giáng được làm tròn về phím tự nhiên gần nhất.",
             "# [abc] = bấm cùng lúc (hợp âm). '-' / '--' = nghỉ.",
             ""]
    for i in range(0, len(tokens), 20):
        lines.append(" ".join(tokens[i:i + 20]))

    dst.write_text("\n".join(lines) + "\n")


async def _get_roblox_sheet(input_midi: Path, work_dir: Path) -> Path:
    source = await _get_quantized_midi(input_midi, work_dir)
    out_path = work_dir / f"{input_midi.stem}.roblox.txt"
    await asyncio.to_thread(_midi_to_roblox_sheet, source, out_path)
    if not out_path.exists():
        raise ConversionError("Không tạo được Roblox QWERTY sheet")
    return out_path


async def _mscore_export(input_midi: Path, out_path: Path, work_dir: Path) -> None:
    cmd = [
        "xvfb-run", "-a",
        MSCORE_BIN,
        str(input_midi),
        "-o", str(out_path),
    ]
    await _run(cmd, cwd=work_dir)


async def convert_one(input_midi: Path, fmt: str, work_dir: Path) -> Path:
    """Convert input_midi to `fmt`, returning the path to the produced file."""
    fmt = fmt.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ConversionError(f"Unsupported format: {fmt}")

    stem = input_midi.stem

    if fmt == "midi":
        out_path = work_dir / f"{stem}.mid"
        shutil.copy(input_midi, out_path)
        return out_path

    if fmt in _TEXT_FORMATS:
        return await _get_roblox_sheet(input_midi, work_dir)

    if fmt in _NOTATION_FORMATS:
        source = await _get_quantized_midi(input_midi, work_dir)
        out_path = work_dir / f"{stem}.{fmt}"
        await _mscore_export(source, out_path, work_dir)
        if not out_path.exists():
            raise ConversionError(f"MuseScore did not produce {out_path.name}")
        return out_path

    if fmt == "wav":
        out_path = work_dir / f"{stem}.wav"
        raw_wav = work_dir / f"{stem}.raw.wav"
        if not raw_wav.exists():
            await _mscore_export(input_midi, raw_wav, work_dir)
            if not raw_wav.exists():
                raise ConversionError("MuseScore failed to render audio (WAV)")
        cmd = ["ffmpeg", "-y", "-i", str(raw_wav), "-af", _ANTI_CLIP_FILTER, str(out_path)]
        await _run(cmd, cwd=work_dir)
        if not out_path.exists():
            raise ConversionError("ffmpeg did not produce anti-clip WAV")
        return out_path

    if fmt in _FFMPEG_DERIVED:
        # Render raw WAV first (cached per work_dir), then re-encode with the
        # anti-clip filter applied.
        raw_wav = work_dir / f"{stem}.raw.wav"
        if not raw_wav.exists():
            await _mscore_export(input_midi, raw_wav, work_dir)
            if not raw_wav.exists():
                raise ConversionError("MuseScore failed to render audio (WAV) for encoding")

        out_path = work_dir / f"{stem}.{fmt}"
        codec = {"mp3": ["-codec:a", "libmp3lame", "-qscale:a", "2"],
                 "ogg": ["-codec:a", "libvorbis", "-qscale:a", "5"],
                 "flac": ["-codec:a", "flac"]}[fmt]
        cmd = ["ffmpeg", "-y", "-i", str(raw_wav), "-af", _ANTI_CLIP_FILTER, *codec, str(out_path)]
        await _run(cmd, cwd=work_dir)
        if not out_path.exists():
            raise ConversionError(f"ffmpeg did not produce {out_path.name}")
        return out_path

    raise ConversionError(f"Unhandled format: {fmt}")


async def convert_many(input_midi: Path, formats: list[str], work_dir: Path) -> dict[str, Path]:
    """Convert to several formats, returning {format: output_path}. Stops-on-error is per format."""
    results: dict[str, Path] = {}
    errors: dict[str, str] = {}
    for fmt in formats:
        try:
            results[fmt] = await convert_one(input_midi, fmt, work_dir)
        except ConversionError as e:
            errors[fmt] = str(e)
    if errors:
        # Attach errors so the caller can report partial failure without losing successes.
        results["__errors__"] = errors  # type: ignore[assignment]
    return results
