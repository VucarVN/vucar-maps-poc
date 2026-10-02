# VUCAR Maps POC — Valhalla self-hosted matrix (thay Vietmap Matrix)

POC để đánh giá thay thế **Vietmap Matrix** (90% hóa đơn Maps API) bằng
**Valhalla** self-hosted trên OpenStreetMap. Chi tiết chiến lược: [`PROPOSAL.md`](PROPOSAL.md).

## Kiến trúc

```
backend (Inspection-Scheduling) ── calculate_matrix_travel_times() ──┐
                                                                     ▼
                               Adapter :8010  POST /matrix (Vietmap-compatible)
                                                                     ▼
                               Valhalla :8002 /sources_to_targets (costing=motor_scooter)
                               data: vietnam-latest.osm.pbf
```

Adapter giữ **đúng contract** của `calculate_matrix_travel_times`
(`code`/`durations`/`distances`) → backend chỉ đổi base URL, **cache PR #407 giữ nguyên**.

## Quickstart (máy có Docker, ≥6GB RAM)

```bash
cp .env.example .env              # set VIETMAP_API_KEY nếu muốn benchmark so sánh
make download-osm                 # ~1.2GB (Geofabrik vietnam-latest.osm.pbf)
make up                           # lần đầu build Valhalla graph 10-30 phút
make consumer                     # test 1 cặp qua adapter
make benchmark                    # so sánh Valhalla vs Vietmap trên 10 tuyến HCMC
```

Xem log build graph: `make logs`.

## Deploy lên OVH Cloud (VPS Ubuntu 24.04)

**Cấu hình khuyến nghị:** Public Cloud `d2-4` (4 vCPU / 8GB) hoặc VPS `vps-2024-8`, disk 40GB+, Ubuntu 24.04.

1. Tạo VM trên OVH console (bật SSH key).
2. SSH vào rồi chạy 1 lệnh:

```bash
curl -fsSL https://raw.githubusercontent.com/VucarVN/vucar-maps-poc/main/deploy/ovh-bootstrap.sh | sudo bash
```

Script tự: cài Docker + swap 4GB → clone POC → tải OSM VN → build graph → start.
Sau đó benchmark:
```bash
cd /opt/vucar-maps-poc
sudo nano .env        # đặt VIETMAP_API_KEY
python3 benchmark/benchmark_matrix.py
```

## Test nhanh

```bash
curl -X POST http://localhost:8010/matrix -H 'Content-Type: application/json' \
  -d '{"points":[{"lat":10.74452,"lng":106.70212},{"lat":10.77584,"lng":106.70070}],"sources":[0],"destinations":[1]}'
```

→ `{"code":"OK","durations":[[…sec]],"distances":[[…m]]}` — giống Vietmap.

## Benchmark

`make benchmark` chạy `benchmark/benchmark_matrix.py`:
- 10 tuyến mẫu HCMC (KĐV home → địa điểm khách)
- Hoặc: `--pairs pairs.json` với file `[[name, lat1, lng1, lat2, lng2], …]`
- Output: duration từng tuyến (Vietmap vs Valhalla), |mean Δ|, % trong ±2/±5 phút, bias

**Tiêu chí chấp nhận đề xuất:** % trong ±2 phút ≥ 70% và không có bias hệ thống > 3 phút.

## Data & license

- OSM: Geofabrik Vietnam extract — © OpenStreetMap contributors (ODbL)
- Valhalla: BSD-3 (Mapbox)
- Adapter code trong thư mục này: MIT (mặc định của VUCAR)

## Lưu ý production (ngoài POC)

- HA: 2 replicas + proxy; hoặc 1 box tốt + cập nhật OSM định kỳ (1-3 tháng)
- Nếu cần geocode/search (thay 10% còn lại): test **Photon** trên data VN trước
- `motor_scooter` time có thể khác thực tế giờ cao điểm → benchmark thật + tune
