# Hướng dẫn chạy TSS Scraper2

Scraper2 là API FastAPI (port **8002**) dùng fork **news-please** vendored trong thư mục `./newspaper`, tương thích endpoint với `tss-scraper` (port 8000): `/health`, `/crawl`, `/source`.

---

## 1. Điều kiện cần có

| Thành phần | Ghi chú |
|-------------|---------|
| **uv** | Cài theo [tài liệu uv](https://docs.astral.sh/uv/). Script `install.sh` gọi `uv`. |
| **Python** | `uv venv` sẽ dùng Python có sẵn trên máy (thường 3.10+). |
| Thư mục **`./newspaper`** | Source fork news-please (editable). Sau khi clone repo, phải có `scraper2/newspaper/setup.py`. |

---

## 2. Chuẩn bị môi trường (bắt buộc sau clone / cập nhật `newspaper/`)

Từ máy chủ, vào thư mục scraper2:

```bash
cd /home/tss/scraper2
./install.sh
```

`install.sh` sẽ:

1. Tạo `.venv` nếu chưa có (`uv venv .venv`).
2. Cài dependency theo `requirements.txt`, trong đó có **`-e ./newspaper`** (editable trỏ vào fork local).
3. Tự kiểm tra `./newspaper/setup.py` và báo lỗi sớm nếu thiếu source fork.
4. Nếu thiếu `uv`, script sẽ tự cài qua installer chính thức (cần `curl` hoặc `wget`).

**Lưu ý:** Nếu bạn chỉ copy mã mà không chạy bước này, editable install có thể vẫn trỏ đường cũ → `import newsplease` lỗi → service không khởi động được.

### Cách thủ công (tương đương `install.sh`)

```bash
cd /home/tss/scraper2
uv venv .venv                    # chỉ lần đầu
uv pip install -r requirements.txt --python .venv/bin/python
```

---

## 3. Chạy thử trực tiếp (không systemd)

```bash
cd /home/tss/scraper2
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

- API docs: `http://<host>:8002/docs`
- Kiểm tra nhanh:

```bash
curl -sS "http://127.0.0.1:8002/health"
# Kỳ vọng: {"status":"ok"}
```

Ví dụ crawl (GET):

```bash
curl -sS --get "http://127.0.0.1:8002/crawl" \
  --data-urlencode "url=https://example.com/"
```

Tham số hữu ích (query / POST body): `language`, `download_media`, v.v. (giống scraper gốc).

---

## 4. Chạy bằng systemd (production)

### 4.1. Cài unit

```bash
sudo cp /home/tss/scraper2/tss-scraper2.service /etc/systemd/system/tss-scraper2.service
sudo systemctl daemon-reload
```

### 4.2. Bật và khởi động

```bash
sudo systemctl enable --now tss-scraper2
sudo systemctl status tss-scraper2
```

Unit mặc định:

- `WorkingDirectory=/home/tss/scraper2`
- `ExecStart=.../scraper2/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8002 --workers 2`

Sau khi đổi code hoặc cập nhật `./newspaper`:

```bash
cd /home/tss/scraper2 && ./install.sh
sudo systemctl restart tss-scraper2
```

---

## 5. Biến môi trường (tùy chọn)

Có thể khai báo trong unit file bằng `Environment=` hoặc file `EnvironmentFile=`.

| Biến | Mặc định | Ý nghĩa |
|------|-----------|---------|
| `SCRAPER_REQUEST_TIMEOUT` | `30` | Timeout giây cho tải HTML / media. |
| `MEDIA_STORAGE_PATH` | `./medias` (relative → resolve theo `WorkingDirectory`) | Thư mục lưu file khi `download_media=true`. |
| `CDN_MEDIA_BASE_URL` | `https://sapbao.local/cdn-medias` | URL prefix trả về sau khi tải media. |
| `MAX_MEDIA_SIZE_BYTES` | `52428800` (50 MB) | Giới hạn kích thước một file media. |
| `MEDIA_WORKERS` | `8` | Số luồng tải media song song (tối đa theo số URL). |

Ví dụ thêm vào `[Service]` của unit (chỉ minh họa):

```ini
Environment=MEDIA_STORAGE_PATH=/var/www/scraper2-medias
Environment=CDN_MEDIA_BASE_URL=https://example.com/cdn-medias
```

Đảm bảo user chạy service có quyền ghi `MEDIA_STORAGE_PATH`.

---

## 6. Liên kết với Laravel (NewsStand)

`TssScraperClient` dùng base URL + `/crawl`, `/source`. Để dùng scraper2, cấu hình base URL trỏ tới host: **8002** (ví dụ `http://172.18.x.x:8002`), không đổi path.

---

## 7. Xử lý sự cố thường gặp

| Hiện tượng | Hướng xử lý |
|-------------|-------------|
| `ModuleNotFoundError: newsplease` | Chạy lại `./install.sh` từ `scraper2`. |
| `connection refused` ngay sau `restart` | Đợi 1–2 giây rồi gọi lại `/health`; xem `journalctl -u tss-scraper2 -n 50`. |
| Import OK nhưng code cũ | Kiểm tra `MAPPING` trong `.venv/.../__editable___news_please_*_finder.py` phải trỏ tới `.../scraper2/newspaper/newsplease`. Chạy `./install.sh` để uv ghi lại editable. |
| Port 8002 đã bị chiếm | Đổi port trong `ExecStart` hoặc dừng process đang bind. |

---

## 8. Cấu trúc thư mục liên quan

```
scraper2/
├── guide.md              ← file này
├── install.sh            ← cài / refresh .venv
├── main.py               ← FastAPI
├── requirements.txt      ← -e ./newspaper + fastapi/uvicorn
├── newspaper/            ← fork news-please (editable)
├── medias/               ← tạo khi download media (có thể thêm vào .gitignore)
├── .venv/                ← không commit; tạo bởi uv
└── tss-scraper2.service  ← mẫu systemd
```

Hoàn tất.
