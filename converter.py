"""
Conversion helpers: MIDI -> PDF / PNG / MusicXML / MSCZ / WAV / MP3 / OGG / FLAC / MIDI / Roblox QWERTY sheet / Guitar Tab / Guitar Audio (MP3) / Violin Audio (MP3) / Piano Audio (MP3)

Strategy:
  - MuseScore (headless, via xvfb-run) handles only notation rendering now:
    PDF, PNG, MusicXML, MSCZ.
  - Audio (WAV / MP3 / OGG / FLAC) is rendered with FluidSynth instead of
    MuseScore's own WAV export. MuseScore's headless audio export (running
    under xvfb, no real soundcard) periodically drops or duplicates audio
    blocks, which is heard as skipping/stuttering ("audio bị nhảy"). FluidSynth
    renders MIDI to WAV in one non-realtime pass (no live audio device, no
    frame drops), which is the same approach most MIDI-to-MP3 web converters
    use, and produces smooth, glitch-free output.
  - Before notation formats (PDF/PNG/MusicXML/MSCZ) and the text sheets
    (Roblox/QWERTY, Guitar Tab), we quantize a *copy* of the input MIDI to a
    16th-note grid. Raw/human-performed MIDI has timing that doesn't line up
    to any clean rhythmic grid, so reading it at tick-level precision
    produces a mess of tiny, fragmented values. Quantizing first gives clean
    rhythms to work with. Audio and MIDI-passthrough outputs use the
    *original*, unquantized file so the actual performance timing/feel is
    preserved.
  - ffmpeg converts the WAV render into MP3 / OGG / FLAC. We apply loudness
    normalization + a peak limiter here, because summing overlapping notes
    can exceed 0 dBFS and hard-clip (audible distortion/crackle) on dense
    scores.
  - MIDI output is just the original file, copied through unchanged.
  - The Roblox/QWERTY sheet maps each note to a letter on the standard
    virtual-piano keyboard layout (1234567890 / qwertyuiop / asdfghjkl /
    zxcvbnm, covering C2–C7). Sharps/flats are rounded to the nearest
    natural key since that layout only exposes white keys directly.
    Simultaneous notes are grouped as a bracketed chord, e.g. [sdf].
  - The Guitar Tab sheet maps notes onto standard-tuned guitar strings
    (EADGBE) as fret numbers, greedily assigning each simultaneous note to
    the lowest free string that can reach it. Notes outside the guitar's
    range are octave-shifted to fit, and chords with more notes than
    playable strings have the excess dropped — this is a best-effort tab,
    not a full fingering/voicing solver.
  - Guitar Audio (MP3), Violin Audio (MP3), and Piano Audio (MP3) render the
    piece with FluidSynth like the other audio formats, but first rewrite a
    *copy* of the MIDI so every note plays on a single instrument instead of
    whatever the original file specifies, and drop the GM drum channel
    (none of these three can play a drum kit). Violin additionally uses
    GM's "String Ensemble 2" (a.k.a. "Slow Strings") patch rather than the
    raw solo "Violin" patch — the plain GM violin sample has a hard, plucky
    attack that reads as harsh; Slow Strings has a much softer, slower
    onset, closer to a gentle bowed sound. It also gets quieter velocities,
    a FluidSynth reverb turned down further (small room, low level, chorus
    off) so it isn't "vang" (echoey), and its own gentle lowpass + slow-
    attack compressor pass in ffmpeg to round off any remaining harshness.
    A dedicated soundfont for violin can be supplied via the
    VIOLIN_SOUNDFONT_PATH env var (e.g. pointing at a real soft-solo-violin
    .sf2) — it falls back to the shared GM soundfont if unset. Piano uses
    the bundled soundfonts/Piano.sf2 (overridable via PIANO_SOUNDFONT_PATH)
    and holds the sustain pedal (CC64) down for the entire piece — a CC64
    "on" is injected at the very start of each track and any sustain
    pedal messages already in the source file are stripped out, so the
    pedal is never released and notes keep ringing/blending continuously
    instead of cutting off. Only MP3 is offered for these three, since
    that's the one people actually want out of them.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

import mido

MSCORE_BIN = "mscore"  # symlinked in the Docker image
FLUIDSYNTH_BIN = "fluidsynth"
_BASE_DIR = Path(__file__).resolve().parent
# General MIDI soundfont installed via the `fluid-soundfont-gm` apt package
# (see Dockerfile). Overridable in case a different soundfont is mounted.
SOUNDFONT_PATH = os.environ.get(
    "SOUNDFONT_PATH", "/usr/share/sounds/sf2/FluidR3_GM.sf2"
)
# Optional dedicated soundfont for the violin render (e.g. a real soft-solo-
# violin .sf2 mounted into the container). Falls back to the shared GM
# soundfont above when unset.
VIOLIN_SOUNDFONT_PATH = os.environ.get("VIOLIN_SOUNDFONT_PATH", SOUNDFONT_PATH)
# Dedicated piano soundfont bundled with the repo (soundfonts/Piano.sf2).
# Overridable if a different piano .sf2 is mounted instead.
PIANO_SOUNDFONT_PATH = os.environ.get(
    "PIANO_SOUNDFONT_PATH", str(_BASE_DIR / "soundfonts" / "Piano.sf2")
)

# General MIDI program numbers (0-indexed).
GUITAR_PROGRAM = 24       # Acoustic Guitar (steel)
VIOLIN_SOFT_PROGRAM = 49  # "String Ensemble 2" / "Slow Strings" — much
                           # softer, slower attack than the raw solo
                           # "Violin" patch (40), which sounds hard/plucky.
PIANO_PROGRAM = 0         # Acoustic Grand Piano — the standard preset
                           # (bank 0, program 0) that single-instrument
                           # piano soundfonts are mapped to.

# Formats MuseScore itself can export to directly from a single command.
_MSCORE_NATIVE = {"pdf", "png", "musicxml", "mscz", "wav"}
# Notation formats that benefit from quantizing the MIDI first.
_NOTATION_FORMATS = {"pdf", "png", "musicxml", "mscz"}
# Formats produced by re-encoding the WAV render with ffmpeg.
_FFMPEG_DERIVED = {"mp3", "ogg", "flac"}
# ffmpeg codec args per audio container — shared between the plain audio
# formats above and the per-instrument renders below, so e.g. "...ogg"
# always means the same libvorbis quality everywhere.
_AUDIO_CODEC_ARGS = {
    "mp3": ["-codec:a", "libmp3lame", "-qscale:a", "2"],
    "ogg": ["-codec:a", "libvorbis", "-qscale:a", "5"],
    "wav": ["-codec:a", "pcm_s16le"],
}
# Single-instrument audio renders, each offered as MP3/WAV/OGG (format keys
# "<tag>mp3" / "<tag>wav" / "<tag>ogg", e.g. "violinwav"). Each base entry
# configures how the source MIDI is rewritten (instrument program, note
# velocity, whether the sustain pedal is forced on), how FluidSynth renders
# it (soundfont, gain, extra synth options for tone/space), and an optional
# extra ffmpeg filter applied before the shared anti-clip chain (e.g. to
# tame a bright/harsh attack).
_INSTRUMENT_BASE_CONFIGS = {
    "guitar": {
        "program": GUITAR_PROGRAM,
        "soundfont": SOUNDFONT_PATH,
        "velocity_scale": 1.0,
        "hold_sustain": False,
        "gain": 1.0,
        "fluidsynth_args": [],
        "extra_filter": None,
    },
    "violin": {
        "program": VIOLIN_SOFT_PROGRAM,
        "soundfont": VIOLIN_SOUNDFONT_PATH,
        # Softer attack/dynamics ("nhẹ nhàng", "nhấn quá mạnh" fix) — scale
        # velocities down well below the original (often piano/percussion-
        # tuned) curve.
        "velocity_scale": 0.65,
        "hold_sustain": False,
        # Moderate overall level ("vừa phải").
        "gain": 0.8,
        # Small room, low reverb level, chorus off: keeps the tone present
        # and close instead of washed out/echoey ("quá vang" fix).
        "fluidsynth_args": [
            "-o", "synth.reverb.active=1",
            "-o", "synth.reverb.room-size=0.15",
            "-o", "synth.reverb.damp=0.5",
            "-o", "synth.reverb.width=0.4",
            "-o", "synth.reverb.level=0.15",
            "-o", "synth.chorus.active=0",
        ],
        # Gentle lowpass to cut the brightest/harshest overtones, then a
        # slow-attack compressor to round off any remaining sharp note
        # onsets without squashing the overall performance.
        "extra_filter": "lowpass=f=7500,acompressor=threshold=-20dB:ratio=3:attack=20:release=150",
    },
    "piano": {
        "program": PIANO_PROGRAM,
        "soundfont": PIANO_SOUNDFONT_PATH,
        "velocity_scale": 1.0,
        # Sustain pedal held down for the whole piece — notes keep ringing
        # and blending into each other instead of cutting off.
        "hold_sustain": True,
        "gain": 1.0,
        "fluidsynth_args": [],
        "extra_filter": None,
    },
}
_INSTRUMENT_AUDIO_CONFIGS = {
    f"{tag}{ext}": {**base, "tag": tag, "ext": ext}
    for tag, base in _INSTRUMENT_BASE_CONFIGS.items()
    for ext in _AUDIO_CODEC_ARGS
}
_INSTRUMENT_AUDIO_FORMATS = set(_INSTRUMENT_AUDIO_CONFIGS)
# Plain-text virtual-piano / Roblox piano key sheet, and guitar tab.
_TEXT_FORMATS = {"roblox", "guitar"}

SUPPORTED_FORMATS = _MSCORE_NATIVE | _FFMPEG_DERIVED | _TEXT_FORMATS | _INSTRUMENT_AUDIO_FORMATS | {"midi"}

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

# Standard guitar tuning, low string to high string: E2 A2 D3 G3 B3 E4.
_GUITAR_STRINGS = [40, 45, 50, 55, 59, 64]  # open-string MIDI note numbers
_GUITAR_STRING_LABELS = ["E", "A", "D", "G", "B", "e"]  # low -> high (tab convention: 'e' = high E)
_GUITAR_MAX_FRET = 19
_GUITAR_MIN_NOTE = _GUITAR_STRINGS[0]
_GUITAR_MAX_NOTE = _GUITAR_STRINGS[-1] + _GUITAR_MAX_FRET

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


def _fit_guitar_range(note: int) -> int:
    # Octave-shift into the guitar's playable range [E2, E4+max_fret], the
    # same trick used for the Roblox layout — keeps the note but moves it
    # into reach instead of dropping it.
    while note < _GUITAR_MIN_NOTE:
        note += 12
    while note > _GUITAR_MAX_NOTE:
        note -= 12
    return note


def _assign_frets(notes: list[int]) -> dict[int, int]:
    """Greedily assign one chord/group of simultaneous notes to strings.

    Returns {string_index: fret}, where string_index 0 = low E, 5 = high e.
    Lowest notes are assigned first; each note goes to the free string that
    reaches it with the lowest fret (falling back to the lower string on
    ties) — this keeps the shape closer to how a guitarist would actually
    finger it, favoring open strings and low positions over high ones. A
    note that can't fit on any free string (e.g. a dense chord with more
    notes than open strings) is dropped; this is a best-effort tab, not a
    full voicing solver.
    """
    assignment: dict[int, int] = {}
    used_strings: set[int] = set()
    for note in sorted(set(_fit_guitar_range(n) for n in notes)):
        candidates = [
            (s_idx, note - open_note)
            for s_idx, open_note in enumerate(_GUITAR_STRINGS)
            if s_idx not in used_strings and 0 <= note - open_note <= _GUITAR_MAX_FRET
        ]
        if not candidates:
            continue  # no free string can reach this note — drop it
        s_idx, fret = min(candidates, key=lambda pair: (pair[1], pair[0]))
        assignment[s_idx] = fret
        used_strings.add(s_idx)
    return assignment


def _midi_to_guitar_tab(src: Path, dst: Path, subdivision: int = 16) -> None:
    mid = mido.MidiFile(str(src))
    ticks_per_beat = mid.ticks_per_beat
    grid = max(1, round(ticks_per_beat * 4 / subdivision))

    merged = mido.merge_tracks(mid.tracks)
    abs_time = 0
    notes_at_tick: dict[int, list[int]] = {}
    for msg in merged:
        abs_time += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            if getattr(msg, "channel", 0) == 9:
                continue  # skip GM drum channel — not meaningful on a guitar tab
            q_tick = round(abs_time / grid) * grid
            notes_at_tick.setdefault(q_tick, []).append(msg.note)

    if not notes_at_tick:
        dst.write_text("# Không tìm thấy nốt nhạc nào trong file MIDI này.\n")
        return

    # One column per grid step that actually has notes (silent grid steps
    # are skipped entirely to keep the tab compact/readable).
    columns = [_assign_frets(notes_at_tick[tick]) for tick in sorted(notes_at_tick)]

    cells_by_string: dict[int, list[str]] = {i: [] for i in range(6)}
    for col in columns:
        for s_idx in range(6):
            fret = col.get(s_idx)
            cell = f"{fret:<2}" if fret is not None else "--"
            cells_by_string[s_idx].append(cell + "-")

    lines = [
        "# Guitar Tab — tự động tạo từ MIDI (tuning chuẩn EADGBE).",
        "# Số = phím (fret) cần bấm, '-' = không bấm dây đó lúc này.",
        "# Bản tab gần đúng: nốt ngoài tầm đàn được chuyển quãng 8 cho vừa,",
        "# hợp âm quá dày (nhiều nốt hơn số dây) có thể bị bỏ bớt nốt.",
        "",
    ]
    chunk = 24  # events per line block, so lines wrap to a readable width
    total = len(columns)
    for start in range(0, total, chunk):
        end = min(start + chunk, total)
        for s_idx in range(5, -1, -1):
            row = "".join(cells_by_string[s_idx][start:end])
            lines.append(f"{_GUITAR_STRING_LABELS[s_idx]}|{row}|")
        lines.append("")

    dst.write_text("\n".join(lines).rstrip() + "\n")


async def _get_guitar_tab(input_midi: Path, work_dir: Path) -> Path:
    source = await _get_quantized_midi(input_midi, work_dir)
    out_path = work_dir / f"{input_midi.stem}.guitar.txt"
    await asyncio.to_thread(_midi_to_guitar_tab, source, out_path)
    if not out_path.exists():
        raise ConversionError("Không tạo được Guitar Tab")
    return out_path


def _force_instrument(
    src: Path,
    dst: Path,
    program: int,
    velocity_scale: float = 1.0,
    hold_sustain: bool = False,
) -> None:
    """Rewrite a MIDI copy so the whole piece plays on one instrument.

    Every note-carrying message is moved onto channel 0 with `program`
    forced; the GM drum channel (9) is dropped entirely since a melodic
    instrument can't play a drum kit. Timing is preserved exactly — when a
    message is dropped, its delta time is carried forward onto the next
    kept message instead of being lost. `velocity_scale` optionally softens
    (or boosts) note-on velocities, e.g. for a gentler bowed-string feel.

    When `hold_sustain` is true, any sustain-pedal (CC64) messages already
    in the source are stripped out, and a CC64 "on" (value 127) is inserted
    at the very start of each track instead. The pedal is then never
    released for the rest of the piece, so notes keep ringing/blending
    together continuously instead of cutting off.

    This is a best-effort instrument swap, not a real arrangement: if two
    original tracks happen to play the same pitch at once, folding them onto
    one channel means one note-off can end both notes early. For most MIDI
    files (single melodic line, or non-overlapping parts) this isn't
    noticeable.
    """
    mid = mido.MidiFile(str(src))
    for track in mid.tracks:
        new_track = mido.MidiTrack()
        carry = 0
        for msg in track:
            is_drum = hasattr(msg, "channel") and msg.channel == 9
            is_program_change = msg.type == "program_change"
            is_sustain_cc = (
                hold_sustain and msg.type == "control_change" and msg.control == 64
            )
            if is_drum or is_program_change or is_sustain_cc:
                carry += msg.time
                continue
            msg = msg.copy(time=msg.time + carry)
            carry = 0
            if hasattr(msg, "channel"):
                msg = msg.copy(channel=0)
            if msg.type == "note_on" and msg.velocity > 0 and velocity_scale != 1.0:
                new_velocity = max(1, min(127, round(msg.velocity * velocity_scale)))
                msg = msg.copy(velocity=new_velocity)
            new_track.append(msg)
        new_track.insert(0, mido.Message("program_change", program=program, channel=0, time=0))
        if hold_sustain:
            new_track.insert(1, mido.Message("control_change", control=64, value=127, channel=0, time=0))
        track.clear()
        track.extend(new_track)
    mid.save(str(dst))


async def _get_instrument_midi(
    input_midi: Path,
    work_dir: Path,
    tag: str,
    program: int,
    velocity_scale: float = 1.0,
    hold_sustain: bool = False,
) -> Path:
    out_path = work_dir / f"{input_midi.stem}.{tag}_instrument.mid"
    if not out_path.exists():
        await asyncio.to_thread(
            _force_instrument, input_midi, out_path, program, velocity_scale, hold_sustain
        )
    return out_path


async def _mscore_export(input_midi: Path, out_path: Path, work_dir: Path) -> None:
    cmd = [
        "xvfb-run", "-a",
        MSCORE_BIN,
        str(input_midi),
        "-o", str(out_path),
    ]
    await _run(cmd, cwd=work_dir)


async def _fluidsynth_render(
    input_midi: Path,
    out_path: Path,
    work_dir: Path,
    soundfont: str = SOUNDFONT_PATH,
    gain: float = 1.0,
    extra_args: list[str] | None = None,
) -> None:
    """Render MIDI to WAV with FluidSynth in one non-realtime pass.

    Unlike MuseScore's headless (xvfb) audio export, this doesn't render
    against a live audio device/clock, so there's nothing for the process to
    fall behind on — no dropped or duplicated audio blocks, i.e. no
    skipping/stuttering ("nhảy") in the resulting audio. This mirrors how
    most MIDI-to-MP3 web converters render audio. `soundfont` and
    `extra_args` let callers use a different .sf2 / tune reverb-chorus per
    instrument.
    """
    cmd = [
        FLUIDSYNTH_BIN,
        "-ni",                  # no interactive shell, no MIDI input device
        "-g", str(gain),        # synth gain
        "-r", "44100",          # sample rate
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd += [
        "-F", str(out_path),    # render straight to a WAV file (non-realtime)
        soundfont,
        str(input_midi),
    ]
    await _run(cmd, cwd=work_dir, timeout=180)


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
        return await _get_guitar_tab(input_midi, work_dir) if fmt == "guitar" \
            else await _get_roblox_sheet(input_midi, work_dir)

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
            await _fluidsynth_render(input_midi, raw_wav, work_dir)
            if not raw_wav.exists():
                raise ConversionError("FluidSynth failed to render audio (WAV)")
        cmd = ["ffmpeg", "-y", "-i", str(raw_wav), "-af", _ANTI_CLIP_FILTER, str(out_path)]
        await _run(cmd, cwd=work_dir)
        if not out_path.exists():
            raise ConversionError("ffmpeg did not produce anti-clip WAV")
        return out_path

    if fmt in _INSTRUMENT_AUDIO_FORMATS:
        cfg = _INSTRUMENT_AUDIO_CONFIGS[fmt]
        instrument_midi = await _get_instrument_midi(
            input_midi, work_dir, cfg["tag"], cfg["program"], cfg["velocity_scale"], cfg["hold_sustain"]
        )
        raw_wav = work_dir / f"{stem}.{cfg['tag']}.raw.wav"
        if not raw_wav.exists():
            await _fluidsynth_render(
                instrument_midi, raw_wav, work_dir,
                soundfont=cfg["soundfont"], gain=cfg["gain"], extra_args=cfg["fluidsynth_args"],
            )
            if not raw_wav.exists():
                raise ConversionError(f"FluidSynth failed to render {cfg['tag']} audio (WAV)")

        out_path = work_dir / f"{stem}.{cfg['tag']}.{cfg['ext']}"
        af = f"{cfg['extra_filter']},{_ANTI_CLIP_FILTER}" if cfg.get("extra_filter") else _ANTI_CLIP_FILTER
        cmd = ["ffmpeg", "-y", "-i", str(raw_wav), "-af", af,
               *_AUDIO_CODEC_ARGS[cfg["ext"]], str(out_path)]
        await _run(cmd, cwd=work_dir)
        if not out_path.exists():
            raise ConversionError(f"ffmpeg did not produce {cfg['tag']} {cfg['ext'].upper()}")
        return out_path

    if fmt in _FFMPEG_DERIVED:
        # Render raw WAV first (cached per work_dir), then re-encode with the
        # anti-clip filter applied.
        raw_wav = work_dir / f"{stem}.raw.wav"
        if not raw_wav.exists():
            await _fluidsynth_render(input_midi, raw_wav, work_dir)
            if not raw_wav.exists():
                raise ConversionError("FluidSynth failed to render audio (WAV) for encoding")

        out_path = work_dir / f"{stem}.{fmt}"
        cmd = ["ffmpeg", "-y", "-i", str(raw_wav), "-af", _ANTI_CLIP_FILTER,
               *_AUDIO_CODEC_ARGS.get(fmt, ["-codec:a", "flac"]), str(out_path)]
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
