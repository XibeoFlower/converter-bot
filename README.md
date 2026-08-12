# MIDI Converter Bot

Bot Discord với lệnh `/converter` — người dùng tải lên một file **MIDI của họ**
và chọn các định dạng muốn xuất: PDF, PNG, MP3, WAV, FLAC, OGG, MIDI,
MusicXML, MSCZ.

> Bot này chỉ xử lý file MIDI mà người dùng tự upload — nó **không** tải hay
> bẻ khóa nội dung có bản quyền từ MuseScore.com hay bất kỳ nguồn nào khác.

## Cách hoạt động

1. `mscore` (MuseScore 4, chạy headless qua `xvfb-run` trong Docker) render
   MIDI ra PDF / PNG / MusicXML / MSCZ / WAV.
2. `ffmpeg` mã hoá lại file WAV đó sang MP3 / OGG / FLAC (tránh vướng vấn đề
   bản quyền của bộ mã hoá MP3 tích hợp trong MuseScore 4).
3. MIDI output = file gốc, copy nguyên vẹn.

## Chạy thử local

```bash
cp .env.example .env
# điền DISCORD_TOKEN vào .env

docker build -t midi-converter-bot .
docker run --env-file .env midi-converter-bot
```

## Deploy lên Railway (qua GitHub repo)

1. Push toàn bộ thư mục này lên một repo GitHub.
2. Trên [railway.app](https://railway.app) → **New Project** →
   **Deploy from GitHub repo** → chọn repo vừa push.
3. Railway sẽ tự nhận `Dockerfile` (nhờ có `railway.json`) và build image —
   không cần cấu hình buildpack thủ công.
4. Vào tab **Variables** của service, thêm:
   - `DISCORD_TOKEN` — token bot lấy từ [Discord Developer Portal](https://discord.com/developers/applications)
   - (tuỳ chọn) `GUILD_ID` — ID server Discord bạn muốn sync lệnh ngay lập tức
     khi test (không set thì lệnh sync global, mất tới ~1h để lên hết).
5. Deploy. Xem log để chắc bot login thành công (`Logged in as ...`).

### Lưu ý resource trên Railway

MuseScore + Xvfb khá nặng RAM (khuyến nghị ≥ 1GB). Nếu dùng gói miễn phí/hobby
và gặp lỗi out-of-memory hoặc container bị kill khi convert, cần nâng plan
hoặc giới hạn số format xử lý cùng lúc.

## Cách dùng trong Discord

```
/converter file:<đính kèm file .mid/.midi>
```

Sau khi chạy lệnh, bot hiện một menu chọn định dạng (chọn được nhiều), rồi
trả về các file đã convert. Nếu file kết quả vượt giới hạn upload của
Discord (mặc định 25MB, server boost thì cao hơn), bot sẽ báo và bỏ qua file
đó thay vì gửi lỗi.

## Giới hạn hiện tại / có thể cải thiện thêm

- Input hiện chỉ nhận `.mid` / `.midi`. Muốn nhận cả `.mscz`/`.musicxml` làm
  input thì chỉnh `converter.py` cho tổng quát hơn (không khó, cùng cơ chế).
- Xử lý tuần tự từng format — nếu cần nhanh hơn có thể chạy song song bằng
  `asyncio.gather`, đánh đổi lấy nhiều RAM/CPU hơn cùng lúc.
- Chưa có hàng đợi (queue) khi nhiều người dùng convert cùng lúc — với traffic
  cao nên thêm worker queue (ví dụ Redis + RQ) để tránh nghẽn.
