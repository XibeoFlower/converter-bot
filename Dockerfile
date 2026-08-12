FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    MSCORE_BIN=/opt/musescore/bin/mscore4portable

# --- System deps -----------------------------------------------------------
# We install the `musescore` apt package purely to pull in its full runtime
# dependency graph (OpenGL, audio, X11, font libs, etc. — MuseScore 4's
# AppImage is picky and under-documents what it actually needs). We then
# remove just the `musescore` binary package afterwards and run the newer
# MuseScore 4 AppImage instead, keeping all the transitively-installed libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        wget \
        ca-certificates \
        musescore \
        libnss3 \
        libxkbcommon0 \
        libegl1 \
        libopengl0 \
        libgl1 \
        fontconfig \
        fonts-freefont-ttf \
        ffmpeg \
        python3 \
        python3-pip \
    && apt-get remove -y musescore \
    && rm -rf /var/lib/apt/lists/*

# --- MuseScore 4 (AppImage, extracted so it runs without FUSE) ----------
RUN wget -q -O /tmp/musescore.appimage \
        https://cdn.jsdelivr.net/musescore/v4.4.1/MuseScore-Studio-4.4.1.242490810-x86_64.AppImage \
    && chmod +x /tmp/musescore.appimage \
    && mkdir -p /opt/musescore \
    && cd /opt/musescore \
    && /tmp/musescore.appimage --appimage-extract >/dev/null \
    && cp -a squashfs-root/. . \
    && rm -rf squashfs-root \
    && rm /tmp/musescore.appimage \
    && ln -s /opt/musescore/bin/mscore4portable /usr/local/bin/mscore

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
