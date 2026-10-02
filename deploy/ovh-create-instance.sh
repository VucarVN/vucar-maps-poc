#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Tạo VM Ubuntu 24.04 trên OVH Public Cloud qua OVH API + cloud-init chạy POC.
#
# Bước 0 — tạo keys trên https://api.ovh.com/createToken (cần GET/POST /cloud/*)
# rồi export:
#   export OVH_APPLICATION_KEY=...
#   export OVH_APPLICATION_SECRET=...
#   export OVH_CONSUMER_KEY=...
#   export OVH_ENDPOINT=ovh-eu        # ovh-eu | ovh-us | ovh-ca
#
# Tuỳ chọn (mặc định hợp lý):
#   OVH_PROJECT_ID=...                # có nhiều project thì chỉ định
#   OVH_REGION=GRA11                  # region VM
#   OVH_FLAVOR=d2-4                   # 4 vCPU / 8GB
#   SSH_PUBKEY=$HOME/.ssh/id_ed25519.pub
#   VIETMAP_API_KEY=...               # để benchmark so sánh ngay
#
# Chạy:  bash deploy/ovh-create-instance.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENDPOINT="${OVH_ENDPOINT:-ovh-eu}"
case "$ENDPOINT" in
  ovh-eu) API="https://api.ovh.com/1.0" ;;
  ovh-us) API="https://api.us.ovhcloud.com/1.0" ;;
  ovh-ca) API="https://api.ca.ovhcloud.com/1.0" ;;
  *) echo "❌ OVH_ENDPOINT phải là ovh-eu/ovh-us/ovh-ca"; exit 1 ;;
esac

: "${OVH_APPLICATION_KEY:?set OVH_APPLICATION_KEY}"
: "${OVH_APPLICATION_SECRET:?set OVH_APPLICATION_SECRET}"
: "${OVH_CONSUMER_KEY:?set OVH_CONSUMER_KEY}"

REGION="${OVH_REGION:-GRA11}"
FLAVOR="${OVH_FLAVOR:-d2-4}"
INSTANCE_NAME="${INSTANCE_NAME:-vucar-maps-poc}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
VIETMAP_API_KEY="${VIETMAP_API_KEY:-}"
POC_REPO="${POC_REPO:-https://github.com/VucarVN/vucar-maps-poc.git}"

# ── OVH auth ────────────────────────────────────────────────────────────────
_nonce() { openssl rand -hex 16; }

ovh() { # method path [body]
  local method="$1" path="$2" body="${3:-}"
  local ts nonce sig full_sig
  ts=$(curl -fsS "$API/auth/time")
  nonce=$(_nonce)
  sig=$(printf '%s' "$OVH_APPLICATION_KEY+$ts+$nonce+$method+$path+$body" \
    | openssl dgst -sha1 -hmac "$OVH_APPLICATION_SECRET" | awk '{print $2}')
  full_sig="\$1\$$ts\$$nonce\$$sig"
  curl -fsS -X "$method" "$API$path" \
    -H "X-Ovh-Application: $OVH_APPLICATION_KEY" \
    -H "X-Ovh-Consumer: $OVH_CONSUMER_KEY" \
    -H "X-Ovh-Timestamp: $ts" \
    -H "X-Ovh-Signature: $full_sig" \
    ${body:+-H "Content-Type: application/json" -d "$body"}
}

jqreq() { :; }  # no-op (giữ chỗ)

echo "==> [1/5] Xác định project Public Cloud"
if [ -z "${OVH_PROJECT_ID:-}" ]; then
  PROJECT_ID=$(ovh GET "/cloud/project" | jq -r '.[0]')
  [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "null" ] || { echo "❌ Không có project — tạo project trên OVH console trước"; exit 1; }
else
  PROJECT_ID="$OVH_PROJECT_ID"
fi
echo "    project: $PROJECT_ID"

echo "==> [2/5] Chọn region + flavor + image"
# Ưu tiên Singapore (latency VN thấp), nếu không có thì GRA, rồi region đầu tiên
if [ -z "${OVH_REGION:-}" ]; then
  REGION=$(ovh GET "/cloud/project/$PROJECT_ID/region" \
    | jq -r '.[].name' | grep -iE "sgb|sgp|sing|gra11" | head -1)
  [ -z "$REGION" ] && REGION=$(ovh GET "/cloud/project/$PROJECT_ID/region" | jq -r '.[].name' | head -1)
  echo "    (tự chọn region: $REGION — set OVH_REGION nếu muốn khác)"
fi
FLAVOR_ID=$(ovh GET "/cloud/project/$PROJECT_ID/flavor?region=$REGION" \
  | jq -r ".[] | select(.name==\"$FLAVOR\") | .id" | head -1)
[ -n "$FLAVOR_ID" ] || { echo "❌ Không tìm thấy flavor $FLAVOR tại $REGION (thử: d2-2, d2-4, b2-7)"; exit 1; }
echo "    flavor: $FLAVOR ($FLAVOR_ID)"

IMAGE_ID=$(ovh GET "/cloud/project/$PROJECT_ID/image?region=$REGION" \
  | jq -r '.[] | select(.name | test("Ubuntu 24.04";"i")) | .id' | head -1)
[ -n "$IMAGE_ID" ] || { echo "❌ Không tìm thấy Ubuntu 24.04 tại $REGION"; exit 1; }
echo "    image: Ubuntu 24.04 ($IMAGE_ID)"

echo "==> [3/5] Upload SSH key (nếu có $SSH_PUBKEY)"
SSH_KEY_ID=""
if [ -f "$SSH_PUBKEY" ]; then
  PUB=$(tr -d '\n' < "$SSH_PUBKEY")
  SSH_KEY_ID=$(ovh GET "/cloud/project/$PROJECT_ID/ssh/key" \
    | jq -r --arg k "$PUB" '.[] | select(.publicKey==$k) | .id' | head -1)
  if [ -z "$SSH_KEY_ID" ]; then
    SSH_KEY_ID=$(ovh POST "/cloud/project/$PROJECT_ID/ssh/key" \
      "{\"publicKey\":\"$PUB\",\"name\":\"vucar-maps-poc-key\"}" | jq -r '.id')
  fi
  echo "    ssh key: $SSH_KEY_ID"
else
  echo "    (không có SSH_PUBKEY — bỏ qua)"
fi

echo "==> [4/5] Cloud-init: cài Docker + clone POC + build graph"
VIETMAP_ENV_LINE=""
[ -n "$VIETMAP_API_KEY" ] && VIETMAP_ENV_LINE="  - printf 'VIETMAP_API_KEY=%s\\n' '$VIETMAP_API_KEY' > /opt/vucar-maps-poc/.env"
USERDATA="#cloud-config
runcmd:
  - apt-get update -y && apt-get install -y git curl docker.io docker-compose-v2
  - systemctl enable --now docker
  - git clone --depth 1 '$POC_REPO' /opt/vucar-maps-poc || true
  - cd /opt/vucar-maps-poc
  - fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile || true
  - make download-osm
  - cp -n .env.example .env || true
  $VIETMAP_ENV_LINE
  - docker compose up -d --build
  - touch /var/log/vucar-poc-done
"

echo "==> [5/5] Tạo instance $INSTANCE_NAME ($FLAVOR, $REGION)"
BODY=$(jq -nc \
  --arg name "$INSTANCE_NAME" \
  --arg flavor "$FLAVOR_ID" \
  --arg image "$IMAGE_ID" \
  --arg region "$REGION" \
  --arg key "$SSH_KEY_ID" \
  --arg data "$USERDATA" \
  '{instanceName:$name, flavorId:$flavor, imageId:$image, region:$region, sshKeyId:$key, userData:$data, monthlyBilling:false}')

RESP=$(ovh POST "/cloud/project/$PROJECT_ID/instance" "$BODY")
INST_ID=$(printf '%s' "$RESP" | jq -r '.id')
echo "✅ Instance đã tạo: $INST_ID (status: $(printf '%s' "$RESP" | jq -r '.status'))"

# ── Chờ active + in IP (tối đa 10 phút) ──
echo "==> Chờ instance ACTIVE + lấy IP…"
for i in $(seq 1 60); do
  ST=$(ovh GET "/cloud/project/$PROJECT_ID/instance/$INST_ID" | jq -r '.status')
  if [ "$ST" = "ACTIVE" ]; then
    IP=$(ovh GET "/cloud/project/$PROJECT_ID/instance/$INST_ID" \
      | jq -r '.ipAddresses[] | select(.type=="public") | .ip' | head -1)
    echo "✅ ACTIVE — SSH: root@$IP"
    echo
    echo "POC tự chạy qua cloud-init (clone + download OSM + build graph + start)."
    echo "Sau ~30-45 phút (build graph), SSH vào rồi:"
    echo "  ssh root@$IP"
    echo "  cd /opt/vucar-maps-poc && python3 benchmark/benchmark_matrix.py"
    exit 0
  fi
  [ $((i % 6)) -eq 0 ] && echo "    ($i*10s) trạng thái: $ST"
  sleep 10
done
echo
echo "Instance chưa ACTIVE sau 10 phút. Tra thủ công:"
echo "  ovh GET /cloud/project/$PROJECT_ID/instance/$INST_ID"
echo "  (đã có id: $INST_ID)"
