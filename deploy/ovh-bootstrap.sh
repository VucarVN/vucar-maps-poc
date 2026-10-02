#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VUCAR Maps POC — bootstrap cho Ubuntu 24.04 VPS (OVH Cloud)
# Chạy:  sudo bash ovh-bootstrap.sh
# Kết quả: Valhalla :8002 + adapter :8010 + graph VN + benchmark sẵn sàng.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

POC_REPO="${POC_REPO:-https://github.com/VucarVN/vucar-maps-poc.git}"
POC_DIR=/opt/vucar-maps-poc

echo "==> [1/6] Cài hệ điều hành dependencies"
sudo apt-get update -y
sudo apt-get install -y git curl unzip docker.io docker-compose-v2

echo "==> [2/6] Docker service"
sudo systemctl enable --now docker

echo "==> [3/6] Swap 4GB (phòng khi build graph thiếu RAM)"
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 4G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=4096
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> [4/6] Clone POC"
sudo mkdir -p "$POC_DIR"
if [ ! -d "$POC_DIR/.git" ]; then
  sudo git clone --depth 1 "$POC_REPO" "$POC_DIR"
fi
cd "$POC_DIR"

echo "==> [5/6] Tải OpenStreetMap Việt Nam (~1.2GB)"
sudo -E make download-osm

echo "==> [6/6] Start Valhalla + adapter (lần đầu build graph 10-30 phút)"
sudo cp -n .env.example .env || true
sudo docker compose up -d --build

echo
echo "──────────────────────────────────────────────────────────────"
echo " Đang build graph (xem log: sudo docker compose logs -f valhalla)"
echo " Sau khi graph xong, test:"
echo "   curl -s -X POST localhost:8010/matrix -H 'Content-Type: application/json' \\"
echo "     -d '{\"points\":[{\"lat\":10.74452,\"lng\":106.70212},{\"lat\":10.77584,\"lng\":106.70070}],\"sources\":[0],\"destinations\":[1]}'"
echo " Benchmark vs Vietmap (set VIETMAP_API_KEY trong .env rồi):"
echo "  cd $POC_DIR && VIETMAP_API_KEY=xxx python3 benchmark/benchmark_matrix.py"
echo "──────────────────────────────────────────────────────────────"
