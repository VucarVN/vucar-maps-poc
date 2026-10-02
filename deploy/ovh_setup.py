#!/usr/bin/env python3
"""Tạo VM Ubuntu 24.04 trên OVH Public Cloud + cloud-init chạy POC Valhalla.

Signature chuẩn python-ovh: sha1(AS + "+" + CK + "+" + METHOD + "+" + FULL_URL + "+" + BODY + "+" + TS)
Header: X-Ovh-Signature: $1$<hex> | X-Ovh-Timestamp: <ts>

Env: OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET, OVH_CONSUMER_KEY (từ ~/.ovh.env)
     OVH_PROJECT_ID (mặc định: project đầu), OVH_REGION (mặc định SGP1→GRA→first),
     OVH_FLAVOR (d2-4), SSH_PUBKEY (~/.ssh/id_ed25519.pub), VIETMAP_API_KEY
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

AS = os.environ["OVH_APPLICATION_SECRET"]
AK = os.environ["OVH_APPLICATION_KEY"]
CK = os.environ["OVH_CONSUMER_KEY"]
EP = "https://ca.api.ovh.com/1.0"  # ovh-ca — xác định qua test
POC_REPO = os.getenv("POC_REPO", "https://github.com/VucarVN/vucar-maps-poc.git")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "vucar-maps-poc")


def _time():
    with urllib.request.urlopen(EP + "/auth/time", timeout=10) as r:
        return int(r.read())


ST = _time()


def call(method, path, data=None):
    body = "" if data is None else json.dumps(data, separators=(",", ":"))
    target = EP + path
    now = str(ST)
    sig = hashlib.sha1("+".join([AS, CK, method.upper(), target, body, now]).encode()).hexdigest()
    req = urllib.request.Request(
        target,
        data=body.encode() if body else None,
        method=method.upper(),
        headers={
            "X-Ovh-Application": AK,
            "X-Ovh-Consumer": CK,
            "X-Ovh-Timestamp": now,
            "X-Ovh-Signature": "$1$" + sig,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path):
    st, body = call("GET", path)
    if st != 200:
        raise SystemExit(f"GET {path} -> {st}: {body.get('message', body)}")
    return body


def post(path, data):
    st, body = call("POST", path, data)
    if st not in (200, 201, 202):
        raise SystemExit(f"POST {path} -> {st}: {body.get('message', body)}")
    return body


def main():
    # 1. project
    pid = os.getenv("OVH_PROJECT_ID") or (get("/cloud/project") or [None])[0]
    if not pid:
        raise SystemExit("Không có project Public Cloud — tạo trên OVH console trước.")
    info = get(f"/cloud/project/{pid}")
    print(f"[1/5] project: {info.get('projectName', pid)} ({pid[:8]}…)")

    # 2. region — ưu tiên Singapore
    region = os.getenv("OVH_REGION")
    if not region:
        regions = get(f"/cloud/project/{pid}/region")
        if regions and isinstance(regions[0], dict):
            regions = [r["name"] for r in regions]
        region = next((r for r in regions if r.upper().startswith("SGP")), None) or \
                 next((r for r in regions if r.startswith("GRA")), None) or regions[0]
    print(f"[2/5] region: {region}")

    # 3. flavor + image
    flavor = os.getenv("OVH_FLAVOR", "d2-4")
    flavors = get(f"/cloud/project/{pid}/flavor?region={region}")
    fid = next((f["id"] for f in flavors if f["name"] == flavor), None) or \
          next((f["id"] for f in flavors if f.get("osType") == "linux"), None)
    if not fid:
        raise SystemExit(f"Không tìm thấy flavor {flavor} tại {region}. Có: {[f['name'] for f in flavors][:8]}")
    print(f"[3/5] flavor: {flavor}")

    images = get(f"/cloud/project/{pid}/image?region={region}")
    iid = next((i["id"] for i in images if "Ubuntu 24.04" in i.get("name", "")), None)
    if not iid:
        raise SystemExit("Không tìm thấy image Ubuntu 24.04 — cần update script.")
    print(f"       image: Ubuntu 24.04")

    # 4. SSH key — đưa thẳng vào cloud-init (ssh_authorized_keys), không phụ thuộc
    #    OVH ssh/key API (endpoint này 404 trên project này).
    keyid = ""
    pubk = ""
    pub = os.getenv("SSH_PUBKEY", os.path.expanduser("~/.ssh/id_ed25519.pub"))
    if os.path.exists(pub):
        with open(pub) as f:
            pubk = f.read().strip()
        print(f"[4/5] ssh key: {os.path.basename(pub)}")
    else:
        print("[4/5] (không có SSH key local — VM sẽ không SSH được!)")

    # 5. cloud-init + create
    vmapikey = os.getenv("VIETMAP_API_KEY", "")
    env_line = f"  - printf 'VIETMAP_API_KEY=%s\\n' '{vmapikey}' > /opt/vucar-maps-poc/.env" if vmapikey else ""
    userdata = (
        "#cloud-config\n"
        + (f"ssh_authorized_keys:\n  - {pubk}\n" if pubk else "")
        + "runcmd:\n"
        "  - apt-get update -y && apt-get install -y git curl docker.io docker-compose-v2\n"
        "  - systemctl enable --now docker\n"
        f"  - git clone --depth 1 {POC_REPO} /opt/vucar-maps-poc || true\n"
        "  - cd /opt/vucar-maps-poc\n"
        "  - fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile || true\n"
        "  - make download-osm\n"
        "  - cp -n .env.example .env || true\n"
        + (env_line + "\n" if env_line else "")
        + "  - docker compose up -d --build\n"
        "  - touch /var/log/vucar-poc-done\n"
    )
    payload = {
        "name": INSTANCE_NAME,
        "flavorId": fid,
        "imageId": iid,
        "region": region,
        "userData": userdata,
        "monthlyBilling": False,
    }

    print("[5/5] Tạo instance…")
    inst = post(f"/cloud/project/{pid}/instance", payload)
    iid2 = inst["id"]
    print(f"       instance id: {iid2}")

    # 6. poll ACTIVE + IP
    print("       chờ ACTIVE + lấy IP (tối đa 10 phút)…")
    for i in range(60):
        d = get(f"/cloud/project/{pid}/instance/{iid2}")
        if d.get("status") == "ACTIVE":
            ip = next((a["ip"] for a in d.get("ipAddresses", []) if a.get("type") == "public"), "?")
            print(f"✅ ACTIVE — SSH: root@{ip}")
            print(f"   POC đang chạy qua cloud-init (~30-45 phút build graph).")
            print(f"   ssh root@{ip}  →  cd /opt/vucar-maps-poc && docker compose logs -f valhalla")
            return
        time.sleep(10)
    print("⚠️ Chưa ACTIVE sau 10 phút — tra thủ công id:", iid2)


if __name__ == "__main__":
    main()
