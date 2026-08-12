#!/usr/bin/env python3
"""Solver chính của đội — construction + local search + LNS (phá rồi sửa).

Chiến lược:
  1. Xây lời giải ban đầu bằng chèn tham lam (xét cửa sổ thời gian, tải trọng).
  2. Lặp phá-rồi-sửa (Large Neighborhood Search): gỡ một nhóm đơn ra, chèn lại
     bằng cách rẻ nhất, chấp nhận theo luyện kim mô phỏng để thoát cực trị.
  3. Tinh chỉnh cục bộ: 2-opt / or-opt trong tuyến, relocate / swap giữa tuyến.
  4. Đơn nào chèn đắt hơn tiền phạt bỏ thì bỏ.

Hàm chi phí là GIẢ THUYẾT (ban tổ chức giấu trọng số thật). Mọi trọng số chỉnh
được qua CLI — dùng bảng xếp hạng public để hiệu chỉnh:

    python solver.py --orders data/sample_orders.csv --out TEN_DOI.json --team TEN_DOI \
        --time 60 --jobs auto \
        --w-dist 1.0 --w-vehicle 40 --w-late 2.0 --w-overtime 3.0 \
        --w-drop-base 120 --w-drop-demand 12

Luôn ghi file sau mỗi instance xong — bị ngắt giữa chừng vẫn còn file hợp lệ.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE / "src"):
    if (_candidate / "swiftroute").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from swiftroute.io_csv import read_instances  # noqa: E402
from swiftroute.io_submission import write_submission  # noqa: E402
from swiftroute.metrics import evaluate  # noqa: E402
from swiftroute.model import Instance  # noqa: E402

EPS = 1e-7


# ---------------------------------------------------------------------------
# Bài toán đã tiền xử lý: ma trận khoảng cách, mảng thuộc tính, hàng xóm gần.
# Chỉ số nội bộ: 0 = kho, 1..n = đơn hàng.
# ---------------------------------------------------------------------------
class Problem:
    def __init__(self, inst: Instance, w: dict[str, float]):
        self.inst = inst
        self.n = inst.n
        self.V = inst.num_vehicles
        self.cap = inst.vehicle_capacity
        self.shift_start = float(inst.shift_start)
        self.shift_end = float(inst.shift_end)
        self.inv_speed = 1.0 / inst.speed

        n = inst.n
        self.ids = [0] * (n + 1)
        xs = [inst.depot_x] + [o.x for o in inst.orders]
        ys = [inst.depot_y] + [o.y for o in inst.orders]
        self.demand = [0] * (n + 1)
        self.ready = [0.0] * (n + 1)
        self.due = [float("inf")] * (n + 1)
        self.service = [0.0] * (n + 1)
        for k, o in enumerate(inst.orders, start=1):
            self.ids[k] = o.id
            self.demand[k] = o.demand
            self.ready[k] = float(o.ready_time)
            self.due[k] = float(o.due_time)
            self.service[k] = float(o.service_time)

        # Ma trận khoảng cách (n+1)². n vài trăm thì vài chục MB đổ lại, ổn.
        hypot = math.hypot
        self.D = [
            [hypot(xs[a] - xs[b], ys[a] - ys[b]) for b in range(n + 1)]
            for a in range(n + 1)
        ]

        self.w_dist = w["w_dist"]
        self.w_vehicle = w["w_vehicle"]
        self.w_late = w["w_late"]
        self.w_ot = w["w_overtime"]
        self.drop_pen = [0.0] * (n + 1)
        for k in range(1, n + 1):
            self.drop_pen[k] = w["w_drop_base"] + w["w_drop_demand"] * self.demand[k]

        # 20 hàng xóm gần nhất của mỗi đơn — giới hạn vùng tìm kiếm cho nhanh.
        self.neighbors: list[list[int]] = [[]] * (n + 1)
        order_idx = list(range(1, n + 1))
        for i in range(1, n + 1):
            Di = self.D[i]
            self.neighbors[i] = sorted(
                (j for j in order_idx if j != i), key=Di.__getitem__
            )[:20]

        # Quy đổi 1 phút chênh ready_time ra "km tương đương" cho phép phá Shaw.
        self.time_scale = inst.speed


def route_eval(P: Problem, seq: list[int]) -> float:
    """Chi phí một tuyến = quãng đường + trễ hẹn + ngoài ca (theo trọng số)."""
    if not seq:
        return 0.0
    D = P.D
    ready = P.ready
    due = P.due
    service = P.service
    inv_speed = P.inv_speed
    t = P.shift_start
    prev = 0
    dist = 0.0
    late = 0.0
    for i in seq:
        d = D[prev][i]
        dist += d
        t += d * inv_speed
        r = ready[i]
        if t < r:
            t = r
        dt = t - due[i]
        if dt > 0.0:
            late += dt
        t += service[i]
        prev = i
    d = D[prev][0]
    dist += d
    t += d * inv_speed
    ot = t - P.shift_end
    if ot < 0.0:
        ot = 0.0
    return P.w_dist * dist + P.w_late * late + P.w_ot * ot


# ---------------------------------------------------------------------------
# Lời giải: số tuyến cố định = num_vehicles (tuyến rỗng được phép, không tính xe).
# ---------------------------------------------------------------------------
class Sol:
    __slots__ = ("routes", "loads", "rcosts", "unserved", "route_of")

    def __init__(self, P: Problem):
        self.routes: list[list[int]] = [[] for _ in range(P.V)]
        self.loads = [0] * P.V
        self.rcosts = [0.0] * P.V
        self.unserved: set[int] = set(range(1, P.n + 1))
        self.route_of = [-1] * (P.n + 1)

    def copy(self) -> "Sol":
        s = object.__new__(Sol)
        s.routes = [list(r) for r in self.routes]
        s.loads = list(self.loads)
        s.rcosts = list(self.rcosts)
        s.unserved = set(self.unserved)
        s.route_of = list(self.route_of)
        return s


def sol_cost(P: Problem, s: Sol) -> float:
    c = sum(s.rcosts) + P.w_vehicle * sum(1 for r in s.routes if r)
    drop = P.drop_pen
    for i in s.unserved:
        c += drop[i]
    return c


def best_insertion(P: Problem, s: Sol, r: int, i: int):
    """Chỗ chèn rẻ nhất của đơn i vào tuyến r.

    Trả về (delta_tổng, vị_trí, rcost_mới) hoặc None nếu quá tải.
    delta_tổng đã gồm tiền thuê thêm xe nếu tuyến đang rỗng.
    """
    if s.loads[r] + P.demand[i] > P.cap:
        return None
    route = s.routes[r]
    if not route:
        c = route_eval(P, [i])
        return (c + P.w_vehicle, 0, c)

    base = s.rcosts[r]
    D = P.D
    Di = D[i]
    L = len(route)

    if L <= 8:
        positions = range(L + 1)
    else:
        # Lọc thô theo chênh quãng đường, chỉ tính chính xác vài vị trí tốt nhất.
        cands = []
        prev = 0
        for pos in range(L + 1):
            nxt = route[pos] if pos < L else 0
            cands.append((Di[prev] + Di[nxt] - D[prev][nxt], pos))
            prev = nxt
        cands.sort()
        positions = [pos for _, pos in cands[:5]]

    best = None
    for pos in positions:
        c = route_eval(P, route[:pos] + [i] + route[pos:])
        d = c - base
        if best is None or d < best[0]:
            best = (d, pos, c)
    return best


def candidate_routes(P: Problem, s: Sol, i: int) -> list[int]:
    """Các tuyến đáng thử chèn đơn i: tuyến chứa hàng xóm gần + một tuyến rỗng."""
    if P.n <= 250:
        rset = {r for r in range(P.V) if s.routes[r]}
    else:
        rset = {s.route_of[j] for j in P.neighbors[i]}
        rset.discard(-1)
    for r in range(P.V):
        if not s.routes[r]:
            rset.add(r)  # tuyến rỗng nào cũng như nhau, một cái là đủ
            break
    return list(rset)


def apply_insert(P: Problem, s: Sol, r: int, i: int, pos: int, new_rcost: float) -> None:
    s.routes[r].insert(pos, i)
    s.loads[r] += P.demand[i]
    s.rcosts[r] = new_rcost
    s.route_of[i] = r
    s.unserved.discard(i)


def remove_orders(P: Problem, s: Sol, rem: list[int]) -> None:
    """Gỡ các đơn ra khỏi tuyến (chưa coi là bỏ — chờ chèn lại)."""
    remset = set(rem)
    affected = {s.route_of[i] for i in rem}
    affected.discard(-1)
    for r in affected:
        s.routes[r] = [x for x in s.routes[r] if x not in remset]
        s.loads[r] = sum(P.demand[x] for x in s.routes[r])
        s.rcosts[r] = route_eval(P, s.routes[r])
    for i in rem:
        s.route_of[i] = -1


def repair(P: Problem, s: Sol, pending: list[int], rng: random.Random) -> None:
    """Chèn lại từng đơn vào chỗ rẻ nhất; đắt hơn tiền phạt bỏ thì bỏ."""
    variant = rng.randrange(4)
    if variant == 0:
        rng.shuffle(pending)
    elif variant == 1:
        pending.sort(key=lambda i: P.due[i])
    elif variant == 2:
        pending.sort(key=lambda i: -P.demand[i])
    else:
        pending.sort(key=lambda i: -P.drop_pen[i])

    for i in pending:
        best = None
        best_r = -1
        for r in candidate_routes(P, s, i):
            bi = best_insertion(P, s, r, i)
            if bi is not None and (best is None or bi[0] < best[0]):
                best = bi
                best_r = r
        if best is None or best[0] >= P.drop_pen[i]:
            s.unserved.add(i)
        else:
            apply_insert(P, s, best_r, i, best[1], best[2])


# ---------------------------------------------------------------------------
# Các phép phá (destroy) của LNS
# ---------------------------------------------------------------------------
def _served_list(s: Sol) -> list[int]:
    return [i for r in s.routes for i in r]


def destroy_random(P: Problem, s: Sol, q: int, rng: random.Random) -> list[int]:
    served = _served_list(s)
    rng.shuffle(served)
    return served[:q]


def destroy_worst(P: Problem, s: Sol, q: int, rng: random.Random) -> list[int]:
    """Gỡ những đơn đang 'đắt' nhất: vòng vèo nhiều km hoặc đang trễ nặng."""
    D = P.D
    scored = []
    for r_idx, route in enumerate(s.routes):
        if not route:
            continue
        # Lượt mô phỏng lấy độ trễ hiện tại của từng đơn.
        t = P.shift_start
        prev = 0
        for pos, i in enumerate(route):
            t += D[prev][i] * P.inv_speed
            if t < P.ready[i]:
                t = P.ready[i]
            late_i = max(0.0, t - P.due[i])
            t += P.service[i]
            nxt = route[pos + 1] if pos + 1 < len(route) else 0
            detour = D[prev][i] + D[i][nxt] - D[prev][nxt]
            scored.append((P.w_dist * detour + P.w_late * late_i, i))
            prev = i
    scored.sort(reverse=True)
    out = []
    while scored and len(out) < q:
        k = int((rng.random() ** 3) * len(scored))  # thiên về đầu bảng, có nhiễu
        out.append(scored.pop(k)[1])
    return out


def destroy_shaw(P: Problem, s: Sol, q: int, rng: random.Random) -> list[int]:
    """Gỡ một cụm đơn 'liên quan nhau': gần nhau về không gian lẫn thời gian."""
    served = _served_list(s)
    if not served:
        return []
    picked = [rng.choice(served)]
    pool = set(served) - set(picked)
    ts = P.time_scale
    while pool and len(picked) < q:
        seed = rng.choice(picked)
        Ds = P.D[seed]
        ranked = sorted(pool, key=lambda j: Ds[j] + ts * abs(P.ready[seed] - P.ready[j]))
        k = int((rng.random() ** 3) * len(ranked))
        chosen = ranked[k]
        picked.append(chosen)
        pool.discard(chosen)
    return picked


def destroy_route(P: Problem, s: Sol, q: int, rng: random.Random) -> list[int]:
    nonempty = [r for r in range(P.V) if s.routes[r]]
    rng.shuffle(nonempty)
    out: list[int] = []
    for r in nonempty:
        out.extend(s.routes[r])
        if len(out) >= q or len(out) >= len(_served_list(s)):
            break
    return out


DESTROYERS = [destroy_random, destroy_worst, destroy_shaw, destroy_route]


# ---------------------------------------------------------------------------
# Tinh chỉnh cục bộ
# ---------------------------------------------------------------------------
def intra_improve(P: Problem, s: Sol, r: int, deadline: float) -> bool:
    """2-opt (đảo đoạn) + or-opt (dời cụm 1-3 đơn) trong một tuyến."""
    route = s.routes[r]
    if len(route) < 2:
        return False
    base = s.rcosts[r]
    improved_any = False
    improved = True
    while improved:
        if time.perf_counter() > deadline:
            break
        improved = False
        L = len(route)
        # 2-opt, giới hạn độ dài đoạn đảo — đoạn dài hiếm khi lợi khi có giờ hẹn.
        for a in range(L - 1):
            hi = min(L, a + 12)
            for b in range(a + 1, hi):
                cand = route[:a] + route[a : b + 1][::-1] + route[b + 1 :]
                c = route_eval(P, cand)
                if c < base - EPS:
                    route = cand
                    base = c
                    improved = improved_any = True
                    break
            if improved:
                break
        if improved:
            continue
        # or-opt: nhấc một cụm 1-3 đơn liên tiếp đặt sang chỗ khác cùng tuyến.
        for sl in (1, 2, 3):
            for a in range(L - sl + 1):
                seg = route[a : a + sl]
                rest = route[:a] + route[a + sl :]
                for t_pos in range(len(rest) + 1):
                    if t_pos == a:
                        continue
                    cand = rest[:t_pos] + seg + rest[t_pos:]
                    c = route_eval(P, cand)
                    if c < base - EPS:
                        route = cand
                        base = c
                        improved = improved_any = True
                        break
                if improved:
                    break
            if improved:
                break
    if improved_any:
        s.routes[r] = route
        s.rcosts[r] = base
        for pos, i in enumerate(route):
            s.route_of[i] = r
    return improved_any


def inter_improve(P: Problem, s: Sol, deadline: float, rng: random.Random) -> bool:
    """Relocate (dời đơn sang tuyến khác) và swap (đổi chỗ hai đơn giữa hai tuyến)."""
    improved_any = False
    order = _served_list(s)
    rng.shuffle(order)
    for i in order:
        if time.perf_counter() > deadline:
            break
        r1 = s.route_of[i]
        if r1 < 0:
            continue
        tried: set[int] = set()
        for j in P.neighbors[i][:12]:
            r2 = s.route_of[j]
            if r2 < 0 or r2 == r1 or r2 in tried:
                continue
            tried.add(r2)
            # --- relocate i -> tuyến r2
            src = [x for x in s.routes[r1] if x != i]
            c_src = route_eval(P, src)
            delta_src = c_src - s.rcosts[r1]
            if not src:
                delta_src -= P.w_vehicle  # tuyến rỗng thì trả xe
            bi = best_insertion(P, s, r2, i)
            if bi is not None and delta_src + bi[0] < -EPS:
                s.routes[r1] = src
                s.loads[r1] -= P.demand[i]
                s.rcosts[r1] = c_src
                apply_insert(P, s, r2, i, bi[1], bi[2])
                improved_any = True
                r1 = r2
                continue
            # --- swap i <-> j
            if (
                s.loads[r1] - P.demand[i] + P.demand[j] <= P.cap
                and s.loads[r2] - P.demand[j] + P.demand[i] <= P.cap
            ):
                n1 = [j if x == i else x for x in s.routes[r1]]
                n2 = [i if x == j else x for x in s.routes[r2]]
                c1 = route_eval(P, n1)
                c2 = route_eval(P, n2)
                if c1 + c2 < s.rcosts[r1] + s.rcosts[r2] - EPS:
                    s.routes[r1] = n1
                    s.routes[r2] = n2
                    s.loads[r1] += P.demand[j] - P.demand[i]
                    s.loads[r2] += P.demand[i] - P.demand[j]
                    s.rcosts[r1] = c1
                    s.rcosts[r2] = c2
                    s.route_of[i] = r2
                    s.route_of[j] = r1
                    improved_any = True
                    r1 = r2
    return improved_any


def try_reinsert_unserved(P: Problem, s: Sol) -> bool:
    """Đơn đang bỏ mà giờ chèn được rẻ hơn tiền phạt thì cứu lại."""
    improved = False
    for i in list(s.unserved):
        best = None
        best_r = -1
        for r in candidate_routes(P, s, i):
            bi = best_insertion(P, s, r, i)
            if bi is not None and (best is None or bi[0] < best[0]):
                best = bi
                best_r = r
        if best is not None and best[0] < P.drop_pen[i] - EPS:
            apply_insert(P, s, best_r, i, best[1], best[2])
            improved = True
    return improved


def polish(P: Problem, s: Sol, deadline: float, rng: random.Random) -> None:
    rounds = 0
    while time.perf_counter() < deadline and rounds < 6:
        rounds += 1
        a = inter_improve(P, s, deadline, rng)
        b = False
        for r in range(P.V):
            if s.routes[r] and intra_improve(P, s, r, deadline):
                b = True
            if time.perf_counter() > deadline:
                break
        c = try_reinsert_unserved(P, s)
        if not (a or b or c):
            break


# ---------------------------------------------------------------------------
# Vòng lặp chính cho một instance
# ---------------------------------------------------------------------------
def solve_one(inst: Instance, cfg: dict) -> tuple[str, list[list[int]], dict]:
    t0 = time.perf_counter()
    budget = cfg["time_budget"]
    deadline = t0 + budget
    rng = random.Random(cfg["seed"])
    P = Problem(inst, cfg["weights"])

    # --- Xây lời giải ban đầu: 3 lần chèn tham lam với thứ tự khác nhau.
    best: Sol | None = None
    best_cost = float("inf")
    for _ in range(3):
        s = Sol(P)
        repair(P, s, list(range(1, P.n + 1)), rng)
        c = sol_cost(P, s)
        if c < best_cost:
            best, best_cost = s, c
    assert best is not None
    polish(P, best, min(deadline, t0 + 0.1 * budget), rng)
    best_cost = sol_cost(P, best)

    cur = best.copy()
    cur_cost = best_cost

    # --- LNS + luyện kim mô phỏng, chọn phép phá thích nghi theo thành tích.
    T0 = max(1.0, 0.04 * best_cost)
    Tend = max(0.01, 0.0002 * best_cost)
    op_w = [1.0] * len(DESTROYERS)
    op_score = [0.0] * len(DESTROYERS)
    op_used = [0] * len(DESTROYERS)
    iters = 0
    last_polish = time.perf_counter()

    n_served_min = 4
    while True:
        now = time.perf_counter()
        if now > deadline - 0.05 * budget:
            break
        iters += 1
        frac = (now - t0) / budget
        T = T0 * (Tend / T0) ** frac

        served_count = P.n - len(cur.unserved)
        if served_count == 0:
            q = 0
        else:
            q_hi = max(n_served_min + 1, min(60, served_count // 3))
            q = rng.randint(min(n_served_min, q_hi - 1), q_hi)

        op = rng.choices(range(len(DESTROYERS)), weights=op_w)[0]
        cand = cur.copy()
        if q > 0:
            removed = DESTROYERS[op](P, cand, q, rng)
            remove_orders(P, cand, removed)
        else:
            removed = []
        pending = removed + list(cand.unserved)
        cand.unserved.clear()
        repair(P, cand, pending, rng)
        c = sol_cost(P, cand)

        op_used[op] += 1
        accepted = False
        if c < cur_cost - EPS:
            accepted = True
            op_score[op] += 2.0
        elif rng.random() < math.exp(-(c - cur_cost) / T):
            accepted = True
            op_score[op] += 0.5
        if accepted:
            cur, cur_cost = cand, c
        if c < best_cost - EPS:
            best, best_cost = cand.copy(), c
            op_score[op] += 5.0
            # Đánh bóng lời giải tốt nhất, nhưng đừng làm quá thường xuyên.
            if now - last_polish > 0.5:
                polish(P, best, min(deadline, now + 0.06 * budget), rng)
                bc = sol_cost(P, best)
                if bc < best_cost:
                    best_cost = bc
                if bc < cur_cost:
                    cur, cur_cost = best.copy(), bc
                last_polish = time.perf_counter()

        if iters % 200 == 0:  # cập nhật trọng số thích nghi của các phép phá
            for k in range(len(DESTROYERS)):
                if op_used[k]:
                    op_w[k] = 0.7 * op_w[k] + 0.3 * (op_score[k] / op_used[k] + 0.1)
                op_score[k] = 0.0
                op_used[k] = 0

    # --- Vét nốt thời gian còn lại để đánh bóng lần cuối.
    polish(P, best, deadline, rng)
    best_cost = sol_cost(P, best)

    routes_ids = [[P.ids[i] for i in r] for r in best.routes if r]

    stats = evaluate(inst, routes_ids)
    if not stats.feasible:
        # Không bao giờ nên xảy ra; nếu xảy ra thì thà nộp lời giải rỗng hợp lệ.
        routes_ids = []
        stats = evaluate(inst, routes_ids)

    summary = stats.public_summary()
    summary["internal_cost"] = round(best_cost, 2)
    summary["lns_iters"] = iters
    summary["seconds"] = round(time.perf_counter() - t0, 1)
    return inst.instance_id, routes_ids, summary


def _worker(payload):
    inst, cfg = payload
    return solve_one(inst, cfg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--orders", required=True, type=Path, help="file CSV đề bài")
    parser.add_argument("--out", required=True, type=Path, help="file JSON nộp bài")
    parser.add_argument("--team", required=True, help="tên đội")
    parser.add_argument("--time", type=float, default=60.0, help="giây cho MỖI instance (mặc định 60)")
    parser.add_argument("--jobs", default="auto", help="số instance chạy song song, 'auto' = số lõi CPU - 1")
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--w-dist", type=float, default=1.0, help="chi phí mỗi km")
    parser.add_argument("--w-vehicle", type=float, default=40.0, help="chi phí huy động một xe")
    parser.add_argument("--w-late", type=float, default=2.0, help="chi phí mỗi phút trễ hẹn")
    parser.add_argument("--w-overtime", type=float, default=3.0, help="chi phí mỗi phút ngoài ca")
    parser.add_argument("--w-drop-base", type=float, default=120.0, help="phạt cố định mỗi đơn bỏ")
    parser.add_argument("--w-drop-demand", type=float, default=12.0, help="phạt thêm mỗi đơn vị khối lượng bị bỏ")
    args = parser.parse_args()

    weights = {
        "w_dist": args.w_dist,
        "w_vehicle": args.w_vehicle,
        "w_late": args.w_late,
        "w_overtime": args.w_overtime,
        "w_drop_base": args.w_drop_base,
        "w_drop_demand": args.w_drop_demand,
    }

    instances = read_instances(args.orders)
    if args.jobs == "auto":
        jobs = max(1, min(len(instances), (os.cpu_count() or 2) - 1))
    else:
        jobs = max(1, int(args.jobs))

    waves = -(-len(instances) // jobs)  # ceil
    print(
        f"{len(instances)} instance, {args.time:.0f}s/instance, {jobs} tiến trình song song"
        f" — ước tính xong sau ~{waves * args.time / 60:.1f} phút"
    )

    def make_cfg(iid: str) -> dict:
        return {
            "weights": weights,
            "time_budget": args.time,
            "seed": args.seed ^ zlib.crc32(iid.encode()),
        }

    solutions: dict[str, list[list[int]]] = {}

    def report(iid: str, summary: dict) -> None:
        print(
            f"{iid}: {summary['total_distance_km']:8.1f} km  "
            f"{summary['vehicles_used']:2d} xe  "
            f"bỏ {summary['orders_unserved']:3d} đơn  "
            f"trễ {summary['total_lateness_min']:8.1f} phút  "
            f"ngoài ca {summary['total_overtime_min']:7.1f} phút  "
            f"[cost {summary['internal_cost']:.0f}, {summary['lns_iters']} vòng LNS, "
            f"{summary['seconds']}s]"
        )

    if jobs == 1:
        for inst in instances:
            iid, routes, summary = solve_one(inst, make_cfg(inst.instance_id))
            solutions[iid] = routes
            report(iid, summary)
            write_submission(args.out, args.team, solutions)  # lưu dần cho an toàn
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_worker, (inst, make_cfg(inst.instance_id))): inst.instance_id
                for inst in instances
            }
            pending = set(futures)
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    iid, routes, summary = fut.result()
                    solutions[iid] = routes
                    report(iid, summary)
                    write_submission(args.out, args.team, solutions)

    # Ghi lần cuối theo đúng thứ tự instance trong đề cho dễ nhìn.
    ordered = {inst.instance_id: solutions[inst.instance_id] for inst in instances}
    write_submission(args.out, args.team, ordered)
    print(f"\nĐã ghi {args.out} — chạy validate.py trước khi nộp:")
    print(f"  python validate.py --orders {args.orders} --submission {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
