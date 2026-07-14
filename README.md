# UAS PJAR — Flask, OTP Gmail, TCP, UDP, dan Live Chat

Aplikasi web Python untuk tugas Pemrograman Jaringan. Sistem menggunakan Flask
sebagai web server, SQLite untuk akun dan chat, SMTP Gmail untuk OTP, socket TCP
untuk transfer file, serta socket UDP untuk video streaming.

## Fitur

1. Registrasi akun dengan OTP email.
2. Login password yang wajib dilanjutkan OTP email.
3. Upload file melalui socket TCP.
4. Lihat, download, dan hapus file hasil transfer TCP.
5. Upload file video sebagai sumber streaming UDP.
6. Pengiriman frame UDP dengan pemecahan paket/chunking.
7. Live feed video UDP pada halaman website.
8. Track audio file video dikirim sebagai MP3 melalui UDP port terpisah.
9. Kontrol suara pada live feed: play, pause, volume, mute, dan muat ulang.
10. Hapus file video UDP dan hentikan stream aktif secara aman.
11. Live chat antar pengguna pada bagian bawah dashboard.
12. `video_sender.py` untuk mengirim webcam/video dari perangkat client lain.

## Struktur penting

```text
app.py              Flask, login/OTP, route TCP, UDP, dan chat
db.py               SQLite users, OTP, dan chat_messages
tcp_upload.py       TCP server dan TCP client pengirim file
udp_stream.py       Protokol chunking frame dan UDP video receiver
udp_audio.py        Sender/receiver audio MP3 melalui UDP
udp_video.py        Streamer video dan audio upload secara background
video_sender.py     UDP client untuk webcam atau video perangkat lain
templates/          Tampilan HTML
uploads/            File yang diterima TCP server
udp_videos/         File sumber video UDP
temp_uploads/       File sementara sebelum dikirim melalui TCP
```

## Instalasi Windows

Disarankan menggunakan Python 3.10 atau 3.11.

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Salin `.env.example` menjadi `.env`, kemudian isi akun Gmail pengirim OTP:

```env
MAIL_MODE=smtp
MAIL_USERNAME=emailanda@gmail.com
MAIL_PASSWORD=app_password_16_digit
MAIL_DEFAULT_SENDER=emailanda@gmail.com
SHOW_OTP_IN_TERMINAL=false
```

`MAIL_PASSWORD` harus berupa Google App Password, bukan password Gmail biasa.

Jalankan server:

```bat
python app.py
```

Buka:

```text
http://127.0.0.1:5000
```

Untuk client lain dalam Wi-Fi yang sama, gunakan alamat yang tampil di terminal,
misalnya:

```text
http://192.168.1.104:5000
```

## Alur upload TCP

```text
Browser --HTTP--> Flask --TCP port 9001--> TCP Upload Server --> uploads/
```

Pada halaman **File TCP**, pengguna dapat:

- upload file;
- melihat file di tab browser;
- download file;
- menghapus file.

File dikirim bertahap per 64 KB agar tidak harus dimuat seluruhnya ke RAM.

## Alur upload dan streaming UDP

```text
Browser --HTTP upload--> Flask --> udp_videos/
                                  |
                                  v
                          JPEG frame chunks
                                  |
                              UDP 9002
                                  |
                          UDP Receiver --> MJPEG --> Browser

Track audio --MP3 chunks--> UDP 9003 --> Audio receiver --> Browser
```

Buka halaman **Video UDP**, pilih video, lalu aktifkan opsi **Langsung mulai
streaming setelah upload**. Video diputar berulang sampai tombol hentikan ditekan
atau video lain dipilih.

UDP tidak menjamin semua paket tiba. Karena itu frame memiliki `frame_id`, nomor
chunk, dan jumlah chunk. Frame yang tidak lengkap akan dibuang setelah timeout.

Audio tidak berada di dalam frame JPEG. Aplikasi menggunakan FFmpeg untuk
mentranskode track audio menjadi MP3 secara real-time, mengirimnya melalui UDP
port 9003, lalu Flask meneruskannya sebagai `audio/mpeg` ke kontrol suara browser.
Browser dapat melakukan play/pause, mengatur volume, mute, dan memuat ulang audio.
Apabila video tidak memiliki track audio, streaming video tetap berjalan dan
halaman akan menampilkan status **audio tidak tersedia**.

## Streaming dari perangkat kedua

Pastikan dua perangkat berada pada Wi-Fi yang sama. Jalankan server pada Laptop 1.
Pada Laptop 2, salin proyek dan instal dependensi, lalu jalankan:

```bat
python video_sender.py 0 --host 192.168.1.104 --port 9002 --audio-port 9003
```

`0` berarti webcam. Untuk file video:

```bat
python video_sender.py "D:\Video\demo.mp4" --host 192.168.1.104 --port 9002 --audio-port 9003
```

Izinkan Python pada Windows Firewall untuk TCP port 5000, UDP port 9002 untuk
video, dan UDP port 9003 untuk audio. Webcam tetap mengirim gambar saja; audio
otomatis dikirim ketika sumber `video_sender.py` berupa file video.

## Live chat

Live chat berada di bawah kartu TCP dan UDP pada dashboard. Browser melakukan:

- `GET /api/chat/messages` untuk menerima pesan baru;
- `POST /api/chat/messages` untuk mengirim pesan.

Pesan disimpan di tabel `chat_messages`, sehingga pengguna dari browser atau
perangkat berbeda dapat saling berkomunikasi. Halaman mengambil pesan baru setiap
1,5 detik melalui HTTP.

## Penghapusan file

- File TCP dihapus melalui tombol **Hapus** pada halaman File TCP.
- File UDP dihapus melalui tombol **Hapus** pada halaman Video UDP.
- Jika file UDP yang dihapus sedang aktif, streamer dihentikan terlebih dahulu.
- Semua route pengelolaan file hanya dapat diakses setelah login dan OTP benar.

## Port

| Layanan | Protokol | Port default |
|---|---|---:|
| Flask website | HTTP/TCP | 5000 |
| Upload server | TCP | 9001 |
| Video receiver | UDP | 9002 |
| Audio receiver | UDP | 9003 |

## Catatan keamanan

- Jangan unggah `.env` ke GitHub.
- Ganti `SECRET_KEY` dengan nilai acak.
- OTP disimpan sebagai hash, berlaku lima menit, dan hanya dapat digunakan sekali.
- Nama file dibersihkan menggunakan `secure_filename` untuk mencegah path traversal.
