"""
Conversion helpers: MIDI -> PDF / PNG / MusicXML / MSCZ / WAV / MP3 / OGG / FLAC / MIDI

Strategy:
  - MuseScore (headless, via xvfb-run) handles anything that needs music
    notation rendering: PDF, PNG, MusicXML, MSCZ, and WAV (raw audio render).
  - ffmpeg converts the WAV render into MP3 / OGG / FLAC, which sidesteps
    MuseScore 4's separate (license-gated) MP3 encoder download.
  - MIDI output is just the original file, copied through unchanged.
"""

import asyncio
import shutil
from pathlib import Path

MSCORE_BIN = "mscore"  # symlinked in the Docker image

# Formats MuseScore itself can export to directly from a single command.
_MSCORE_NATIVE = {"pdf", "png", "musicxml", "mscz", "wav"}
# Formats produced by re-encoding the WAV render with ffmpeg.
_FFMPEG_DERIVED = {"mp3", "ogg", "flac"}

SUPPORTED_FORMATS = _MSCORE_NATIVE | _FFMPEG_DERIVED | {"midi"}


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

    if proc.returncode != 0:
        raise ConversionError(
            f"Command failed ({' '.join(cmd)}):\n{stderr.decode(errors='ignore')[-1500:]}"
        )


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

    if fmt in _MSCORE_NATIVE:
        out_path = work_dir / f"{stem}.{fmt if fmt != 'musicxml' else 'musicxml'}"
        await _mscore_export(input_midi, out_path, work_dir)
        if not out_path.exists():
            raise ConversionError(f"MuseScore did not produce {out_path.name}")
        return out_path

    if fmt in _FFMPEG_DERIVED:
        # Render WAV first (cached per work_dir), then re-encode.
        wav_path = work_dir / f"{stem}.wav"
        if not wav_path.exists():
            await _mscore_export(input_midi, wav_path, work_dir)
            if not wav_path.exists():
                raise ConversionError("MuseScore failed to render audio (WAV) for encoding")

        out_path = work_dir / f"{stem}.{fmt}"
        codec = {"mp3": ["-codec:a", "libmp3lame", "-qscale:a", "2"],
                 "ogg": ["-codec:a", "libvorbis", "-qscale:a", "5"],
                 "flac": ["-codec:a", "flac"]}[fmt]
        cmd = ["ffmpeg", "-y", "-i", str(wav_path), *codec, str(out_path)]
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
