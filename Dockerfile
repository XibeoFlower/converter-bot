FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    QT_QPA_PLATFORM=offscreen \
    MSCORE_BIN=/opt/musescore/bin/mscore4portable

# --- System deps ---------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        wget \
        ca-certificates \
        libopengl0 \
        libnss3 \
        libxkbcommon0 \
        libgl1 \
        fontconfig \
        fonts-freefont-ttf \
        ffmpeg \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# --- MuseScore 4 (AppImage, extracted so it runs without FUSE) ----------
RUN wget -q -O /tmp/musescore.appimage \
        https://cdn.jsdelivr.net/musescore/v4.4.1/MuseScore-Studio-4.4.1.242490810-x86_64.AppImage \
    && chmod +x /tmp/musescore.appimage \
    && mkdir -p /opt/musescore \
    && cd /opt/musescore \
    && /tmp/musescore.appimage --appimage-extract >/dev/null \
    && mv squashfs-root/* . \
    && rmdir squashfs-root \
    && rm /tmp/musescore.appimage \
    && ln -s /opt/musescore/bin/mscore4portable /usr/local/bin/mscore

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
