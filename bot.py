import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from converter import SUPPORTED_FORMATS, convert_many, ConversionError

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: set GUILD_ID to sync commands instantly to one server while testing.
GUILD_ID = os.environ.get("GUILD_ID")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("msc-converter-bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

FORMAT_LABELS = {
    "pdf": "PDF (sheet music)",
    "png": "PNG (sheet music image)",
    "musicxml": "MusicXML",
    "mscz": "MSCZ (MuseScore file)",
    "midi": "MIDI (original)",
    "roblox": "QWERTY Sheet (Roblox Piano)",
    "guitar": "Guitar Tab (TAB)",
    "guitarmp3": "Guitar Audio (MP3)",
    "guitarwav": "Guitar Audio (WAV)",
    "guitarogg": "Guitar Audio (OGG)",
    "violinmp3": "Violin Audio (MP3)",
    "violinwav": "Violin Audio (WAV)",
    "violinogg": "Violin Audio (OGG)",
    "pianomp3": "Piano Audio (MP3)",
    "pianowav": "Piano Audio (WAV)",
    "pianoogg": "Piano Audio (OGG)",
}

# Discord's *default* per-file upload cap; boosted servers get more.
# We fall back to this when we can't read the guild's real limit (e.g. in DMs).
DEFAULT_FILESIZE_LIMIT = 25 * 1024 * 1024


class FormatSelect(discord.ui.Select):
    def __init__(self, midi_bytes: bytes, original_name: str):
        options = [
            discord.SelectOption(label=FORMAT_LABELS[f], value=f)
            for f in sorted(SUPPORTED_FORMATS)
        ]
        super().__init__(
            placeholder="Chọn định dạng muốn xuất (có thể chọn nhiều)",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.midi_bytes = midi_bytes
        self.original_name = original_name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        formats = self.values
        filesize_limit = getattr(interaction.guild, "filesize_limit", DEFAULT_FILESIZE_LIMIT)

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            midi_path = work_dir / "input.mid"
            midi_path.write_bytes(self.midi_bytes)

            try:
                results = await convert_many(midi_path, formats, work_dir)
            except Exception as e:
                log.exception("Conversion crashed")
                await interaction.followup.send(f"{interaction.user.mention} ❌ Lỗi khi chuyển đổi: {e}")
                return

            errors = results.pop("__errors__", None)
            base_name = Path(self.original_name).stem

            files_to_send = []
            oversized = []
            for fmt, path in results.items():
                size = path.stat().st_size
                target_name = f"{base_name}.{path.suffix.lstrip('.')}"
                if size > filesize_limit:
                    oversized.append(target_name)
                    continue
                files_to_send.append((target_name, path.read_bytes()))

            attachments = []
            for name, data in files_to_send:
                attachments.append(discord.File(io.BytesIO(data), filename=name))

            mention = interaction.user.mention

            msg_parts = []
            if attachments:
                msg_parts.append(f"{mention} ✅ Đã chuyển đổi **{len(attachments)}** file.")
            else:
                msg_parts.append(f"{mention} ❌ Không có file nào được tạo.")
            if errors:
                failed = ", ".join(f"{f} ({err.splitlines()[0][:100]})" for f, err in errors.items())
                msg_parts.append(f"⚠️ Không chuyển được: {failed}")
            if oversized:
                msg_parts.append(
                    f"⚠️ File quá lớn để gửi (>{filesize_limit // (1024*1024)}MB), đã bỏ qua: "
                    + ", ".join(oversized)
                )

            if not attachments:
                await interaction.followup.send("\n".join(msg_parts))
                return

            # Discord caps at 10 attachments per message.
            for i in range(0, len(attachments), 10):
                chunk = attachments[i:i + 10]
                content = "\n".join(msg_parts) if i == 0 else None
                await interaction.followup.send(content=content, files=chunk)


class FormatView(discord.ui.View):
    def __init__(self, midi_bytes: bytes, original_name: str):
        super().__init__(timeout=300)
        self.add_item(FormatSelect(midi_bytes, original_name))


@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    log.info("Logged in as %s", bot.user)


@bot.tree.command(name="converter", description="Chuyển đổi file MIDI sang PDF/PNG/MusicXML/MSCZ/Guitar Tab/Guitar-Violin-Piano Audio")
@app_commands.describe(file="File MIDI cần chuyển đổi (.mid / .midi)")
async def converter(interaction: discord.Interaction, file: discord.Attachment):
    if not file.filename.lower().endswith((".mid", ".midi")):
        await interaction.response.send_message("❌ Vui lòng tải lên file .mid hoặc .midi", ephemeral=True)
        return

    if file.size > 25 * 1024 * 1024:
        await interaction.response.send_message("❌ File MIDI quá lớn.", ephemeral=True)
        return

    midi_bytes = await file.read()
    view = FormatView(midi_bytes, file.filename)
    await interaction.response.send_message(
        "Chọn (các) định dạng muốn xuất từ file MIDI này:",
        view=view,
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(TOKEN)
