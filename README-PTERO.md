# AnzNokosFree Bot — Panduan Deploy ke Pterodactyl

## Isi Paket Ini

```
bot.py                  → Kode utama bot
requirements.txt        → Library Python yang dibutuhkan
install.sh              → Script instalasi (opsional, manual)
egg-anznokosfree.json   → Egg Pterodactyl (import ke panel)
```

---

## Cara Deploy ke Pterodactyl Panel

### Langkah 1: Import Egg

1. Login ke Pterodactyl admin area
2. Pergi ke **Admin → Nests** (atau **Service Management**)
3. Buat Nest baru: klik **"Create New"**, beri nama misalnya `Bots`
4. Klik **"Import Egg"**
5. Upload file `egg-anznokosfree.json`

### Langkah 2: Buat Server Baru

1. Pergi ke **Servers → Create New Server**
2. Pilih egg **AnzNokosFree Telegram Bot**
3. Isi form:
   - **Name**: AnzNokosFree (atau nama lain)
   - **Node**: pilih node yang tersedia
   - **Docker Image**: `Python 3.11` (default)
   - **Memory**: minimal **256 MB**
   - **Disk**: minimal **512 MB**
   - **CPU**: minimal **25%**

4. Isi variabel di bagian **Startup**:

| Variabel | Isi |
|----------|-----|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `ZURA_EMAIL` | Email akun zurastore |
| `ZURA_PASSWORD` | Password akun zurastore |
| `ADMIN_CHAT_ID` | Telegram ID kamu (cek lewat @userinfobot) |
| `BASE_URL` | `https://x.zurastore.my.id` (biarkan default) |

5. Klik **"Create Server"** dan tunggu instalasi selesai

### Langkah 3: Upload File Bot

1. Buka server yang baru dibuat
2. Pergi ke tab **Files**
3. Upload file-file berikut:
   - `bot.py`
   - `requirements.txt`
4. Klik **"Start"** untuk menjalankan bot

---

## Startup Command (sudah otomatis dari egg)

```bash
pip install -r /home/container/requirements.txt --quiet && python /home/container/bot.py
```

Tidak perlu diubah — sudah dikonfigurasi di egg.

---

## Troubleshooting

| Problem | Solusi |
|---------|--------|
| Bot tidak start, error token | Cek `TELEGRAM_BOT_TOKEN` di tab Startup |
| Login zura gagal | Cek `ZURA_EMAIL` dan `ZURA_PASSWORD` |
| ModuleNotFoundError | Pastikan `requirements.txt` sudah diupload |
| Bot crash langsung | Cek log di tab Console, cari baris error |
| Memory limit | Naikkan memory ke minimal 256 MB |

---

## Perintah Bot di Telegram

| Perintah | Fungsi |
|----------|--------|
| `/start` | Menu utama |
| `/debug_page` | Debug halaman getnum (admin) |
| `/batal` | Batalkan input aktif |
