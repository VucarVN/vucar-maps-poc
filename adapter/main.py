"""Adapter: Vietmap-compatible Matrix API backed by self-hosted Valhalla.

Drop-in cho `calculate_matrix_travel_times` của Inspection-Scheduling-System:
cùng request/response shape (`code`, `durations[][sec]`, `distances[][m]`).

Backend chỉ cần đổi base URL, cache (PR #407) giữ nguyên.

Run: uvicorn main:app --host 0.0.0.0 --port 8010
"""

import logging
import os
import math

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VUCAR Maps Adapter", version="0.2.0")

VALHALLA_URL = os.getenv("VALHALLA_URL", "http://valhalla:8002")
COSTING = os.getenv("COSTING", "motor_scooter")  # motor_scooter = KĐV đi xe máy
VALHALLA_TIMEOUT = float(os.getenv("VALHALLA_TIMEOUT", "30"))
# Valhalla từ chối (HTTP 400, error 154) khi path vượt `max_distance` (mặc định
# 500km). Lọc trước các cặp chắc chắn vượt để 1 cặp Bắc–Nam không làm hỏng cả
# matrix. Đặt dưới 500 một chút vì Valhalla đo theo đường đi, không phải đường chim bay.
MAX_PAIR_KM = float(os.getenv("MAX_PAIR_KM", "480"))
# Khi Valhalla 400 (điểm không nối được vào graph — error 170), dò từng điểm
# bằng cặp (p → p) để biết điểm nào hỏng, rồi chỉ trả null cho các cặp dính nó.
PROBE_UNROUTABLE_POINTS = os.getenv("PROBE_UNROUTABLE_POINTS", "true").lower() not in (
    "false",
    "0",
    "off",
)


class MatrixRequest(BaseModel):
    points: list[dict]  # [{lat, lng}] — index = point index
    sources: list[int]  # source indices (row)
    destinations: list[int]  # destination indices (col)
    costing: str | None = None  # override; default motor_scooter


@app.get("/health")
def health():
    return {
        "status": "ok",
        "valhalla": VALHALLA_URL,
        "max_pair_km": MAX_PAIR_KM,
        "probe_unroutable_points": PROBE_UNROUTABLE_POINTS,
    }


def _haversine_km(a: dict, b: dict) -> float:
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = math.radians(b["lat"] - a["lat"])
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


async def _post_valhalla(sources: list[dict], targets: list[dict], costing: str):
    payload = {"sources": sources, "targets": targets, "costing": costing, "units": "km"}
    async with httpx.AsyncClient(timeout=VALHALLA_TIMEOUT) as client:
        return await client.post(f"{VALHALLA_URL}/sources_to_targets", json=payload)


def _parse_rows(data: dict, n_src: int, n_dst: int):
    """Valhalla verbose rows → (durations[m][n], distances[m][n]); None = no route."""
    durations = [[None] * n_dst for _ in range(n_src)]
    distances = [[None] * n_dst for _ in range(n_src)]
    rows = data.get("sources_to_targets")
    if rows is None:
        return None
    if rows and isinstance(rows[0], dict):
        rows = [rows]  # phòng trường hợp đã flatten
    for row in rows:
        for cell in row:
            i, j = cell["from_index"], cell["to_index"]
            if cell.get("time") is not None:
                durations[i][j] = int(cell["time"])
            if cell.get("distance") is not None:
                distances[i][j] = round(cell["distance"] * 1000.0)
    return durations, distances


async def _probe_unroutable_points(points: list[dict], costing: str) -> set[int]:
    """Điểm nào không nối được vào graph? Dò bằng cặp (p → p).

    Lưu ý: `points` là payload thô của backend (`lat`/`lng`), còn Valhalla cần
    `lat`/`lon` — phải đổi key trước khi gửi, nếu không MỌI probe đều 400 và ta
    sẽ đánh dấu nhầm cả danh sách là hỏng. Nếu probe fail toàn bộ (khác thường)
    coi như không kết luận được (trả rỗng) thay vì null hết matrix.
    """
    bad: set[int] = set()
    for idx, pt in enumerate(points):
        probe = {"lat": pt["lat"], "lon": pt.get("lon", pt.get("lng"))}
        try:
            resp = await _post_valhalla([probe], [probe], costing)
        except Exception:  # noqa: BLE001 - network: coi như không kết luận được
            return set()
        if resp.status_code >= 400:
            bad.add(idx)
    if len(bad) == len(points) and points:
        logger.warning("valhalla probe failed for ALL %d points — inconclusive", len(points))
        return set()
    return bad


@app.post("/matrix")
async def matrix(req: MatrixRequest):
    """Vietmap-compatible /matrix/v4 → Valhalla sources_to_targets.

    Response: {"code": "OK", "durations": [[sec]], "distances": [[m]]}
    durations[i][j] = sources[i] -> destinations[j]. Cell null = không route được
    (backend fallback Vietmap cho RIÊNG cặp đó).

    Valhalla trả HTTP 400 khi có điểm không nối được vào graph (error 170) hoặc
    cặp vượt `max_distance` (error 154). Trước đây mọi lỗi bị map thành 502 →
    backend hiểu là Valhalla chết → cooldown + fallback cả chunk. Giờ lọc đúng
    point/pair hỏng rồi trả null cho phần đó.
    """
    if not req.points or not req.sources or not req.destinations:
        raise HTTPException(status_code=400, detail="points/sources/destinations required")

    try:
        sources = [{"lat": req.points[i]["lat"], "lon": req.points[i]["lng"]} for i in req.sources]
        targets = [{"lat": req.points[i]["lat"], "lon": req.points[i]["lng"]} for i in req.destinations]
    except (IndexError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"bad point index: {e}")

    costing = req.costing or COSTING
    n_src, n_dst = len(sources), len(targets)

    try:
        resp = await _post_valhalla(sources, targets, costing)
        resp.raise_for_status()
        parsed = _parse_rows(resp.json(), n_src, n_dst)
        if parsed is not None:
            durations, distances = parsed
            return {"code": "OK", "durations": durations, "distances": distances}
        logger.error("valhalla returned no matrix (sources=%d targets=%d)", n_src, n_dst)
    except httpx.HTTPStatusError as e:
        # 4xx = vấn đề DỮ LIỆU (điểm/cặp), KHÔNG phải Valhalla chết. 5xx = lỗi server.
        status = e.response.status_code
        body = (e.response.text or "")[:300]
        if status >= 500:
            logger.error("valhalla %s: %s", status, body)
            raise HTTPException(status_code=502, detail=f"valhalla server error: {body}")
        logger.warning(
            "valhalla %s (data): %s | sources=%d targets=%d max_pair_km=%s",
            status, body, n_src, n_dst, MAX_PAIR_KM,
        )
    except Exception as e:  # noqa: BLE001 - connect/timeout: Valhalla thực sự không tới được
        logger.error("valhalla error: %s", e)
        raise HTTPException(status_code=502, detail=f"valhalla unavailable: {e}")

    # ── Tolerant retry: lọc point/pair không route được, trả null cho phần đó ──
    bad_points: set[int] = set()
    if PROBE_UNROUTABLE_POINTS:
        # điểm hỏng có thể nằm ở bất kỳ index nào backend gửi lên
        bad_points = await _probe_unroutable_points(req.points, costing)
        if bad_points:
            logger.warning(
                "valhalla unroutable points: %s (%d/%d)",
                sorted(bad_points), len(bad_points), len(req.points),
            )

    durations = [[None] * n_dst for _ in range(n_src)]
    distances = [[None] * n_dst for _ in range(n_src)]
    skipped_far = 0

    for i, src_idx in enumerate(req.sources):
        if src_idx in bad_points:
            continue
        keep = [
            (j, dst_idx)
            for j, dst_idx in enumerate(req.destinations)
            if dst_idx not in bad_points
            and _haversine_km(sources[i], targets[j]) <= MAX_PAIR_KM
        ]
        skipped_far += len(req.destinations) - len(keep)
        if not keep:
            continue
        sub_targets = [targets[j] for j, _ in keep]
        try:
            sub = await _post_valhalla([sources[i]], sub_targets, costing)
            sub.raise_for_status()
            parsed = _parse_rows(sub.json(), 1, len(sub_targets))
        except Exception as e:  # noqa: BLE001 - per-source fail → null phần này
            logger.warning("valhalla per-source retry failed (src idx %s): %s", src_idx, e)
            continue
        if parsed is None:
            continue
        sub_dur, sub_dist = parsed
        for col, (j, _) in enumerate(keep):
            durations[i][j] = sub_dur[0][col]
            distances[i][j] = sub_dist[0][col]

    logger.info(
        "tolerant matrix: sources=%d targets=%d bad_points=%d skipped_far_pairs=%d",
        n_src, n_dst, len(bad_points), skipped_far,
    )
    return {"code": "OK", "durations": durations, "distances": distances}


# ─── Google/Goong-compatible (cho việc repoint app sau này) ─────────────
class DistanceMatrixRequest(BaseModel):
    origins: list[str]  # "lat,lng"
    destinations: list[str]
    mode: str = "motor_scooter"


@app.post("/distancematrix/json")
async def distancematrix(req: DistanceMatrixRequest):
    points = []
    for s in req.origins + req.destinations:
        lat, lng = s.split(",")
        points.append({"lat": float(lat), "lng": float(lng)})
    n_orig = len(req.origins)
    result = await matrix(
        MatrixRequest(
            points=points,
            sources=list(range(n_orig)),
            destinations=list(range(n_orig, n_orig + len(req.destinations))),
            costing=req.mode,
        )
    )
    return {
        "destination_addresses": req.destinations,
        "origin_addresses": req.origins,
        "rows": [
            {
                "elements": [
                    {
                        "status": "OK",
                        "duration": {"value": result["durations"][i][j] or -1, "text": ""},
                        "distance": {"value": result["distances"][i][j] or -1, "text": ""},
                    }
                    for j in range(len(req.destinations))
                ]
            }
            for i in range(len(req.origins))
        ],
    }
