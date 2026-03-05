from __future__ import annotations

import os
import re
import math
import uuid
import json
import heapq
from datetime import datetime
from typing import List, Tuple, Dict, Optional, Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_TITLE = "Warehouse Routing + Picking (Phase 2) - Optimized"
ROUTING_XLSX = os.path.join(os.path.dirname(__file__), "MAG_ROUTING_DATA_v2.xlsx")
SESSION_DB = os.path.join(os.path.dirname(__file__), "sessions.json")

app = FastAPI(title=APP_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOC_RE = re.compile(r"^([A-Z])\s*([0-9]+)$")
SKU_RE = re.compile(r"^[0-9]{6}$")

# ----------------------------
# Logging
# ----------------------------
def log_action(session_id: str, action: str, data: dict = None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] SID:{session_id} {action}"
    if data:
        log_entry += f" {json.dumps(data, default=str)}"
    print(log_entry)
    
    log_file = os.path.join(os.path.dirname(__file__), "warehouse.log")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_entry + "\n")


def parse_location(s: str) -> Tuple[str, int]:
    s = s.strip().upper()
    m = LOC_RE.match(s)
    if not m:
        raise ValueError(f"Neispravna lokacija: {s}")
    return m.group(1), int(m.group(2))


def sector_rank(sec: str) -> int:
    return {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}.get(sec, 99)


def sector_group(loc: str) -> str:
    return parse_location(loc)[0]


# ----------------------------
# Warehouse map from Excel
# ----------------------------
class WarehouseMap:
    def __init__(self, xlsx_path: str):
        self.xlsx_path = xlsx_path
        self.log = []
        
        try:
            self.grid = pd.read_excel(xlsx_path, sheet_name="GRID_CELLS")
            self.locs = pd.read_excel(xlsx_path, sheet_name="LOCATIONS")
            self.ent = pd.read_excel(xlsx_path, sheet_name="ENTRANCES")
            self.params = pd.read_excel(xlsx_path, sheet_name="PARAMS")
        except Exception as e:
            print(f"⚠️ GREŠKA pri učitavanju Excel fajla: {e}")
            print("Kreiram fallback mapu...")
            self._create_fallback_map()
            return

        self.drawer_w = float(self.params.loc[0, "drawer_width_m"]) if "drawer_width_m" in self.params.columns else 0.5
        self.aisle_w = float(self.params.loc[0, "aisle_width_m"]) if "aisle_width_m" in self.params.columns else 1.0
        self.block_depth = 0.5

        self.cell_by_rc: Dict[Tuple[int, int], dict] = {}
        for _, r in self.grid.iterrows():
            rc = (int(r["row"]), int(r["col"]))
            self.cell_by_rc[rc] = {
                "row": int(r["row"]),
                "col": int(r["col"]),
                "cell_type": str(r["cell_type"]),
                "x_m": float(r["x_m"]),
                "y_m": float(r["y_m"]),
                "w_m": float(r["w_m"]),
                "h_m": float(r["h_m"]),
                "value": None if pd.isna(r.get("value", None)) else str(r.get("value")),
            }

        self.aisle_nodes: List[Tuple[int, int]] = [rc for rc, info in self.cell_by_rc.items() if info["cell_type"] == "aisle"]
        self.aisle_index: Dict[Tuple[int, int], int] = {rc: i for i, rc in enumerate(self.aisle_nodes)}

        self.adj: List[List[Tuple[int, float]]] = [[] for _ in self.aisle_nodes]
        for rc in self.aisle_nodes:
            i = self.aisle_index[rc]
            r0, c0 = rc
            for nr, nc in [(r0 - 1, c0), (r0 + 1, c0), (r0, c0 - 1), (r0, c0 + 1)]:
                nrc = (nr, nc)
                if nrc in self.aisle_index:
                    j = self.aisle_index[nrc]
                    w = self._dist_cells(rc, nrc)
                    self.adj[i].append((j, w))

        self.loc_to_block_rc: Dict[str, Tuple[int, int]] = {}
        self.loc_coords: Dict[str, Tuple[float, float]] = {}
        
        for _, r in self.locs.iterrows():
            loc = str(r["location"]).strip().upper()
            self.loc_to_block_rc[loc] = (int(r["row"]), int(r["col"]))
            self.loc_coords[loc] = (float(r["x_m"]), float(r["y_m"]))

        self.entrances: Dict[str, Tuple[int, int]] = {}
        for _, r in self.ent.iterrows():
            name = str(r["cell"]).strip().upper()
            mapped = str(r["mapped_cell"]).strip().upper()
            rc = self._cellref_to_rc(mapped)
            if rc not in self.aisle_index:
                rc = self._nearest_aisle_to_cellref(mapped)
            self.entrances[name] = rc

        self.dist_matrix = self._all_pairs_shortest_paths()
        print(f"✅ Učitan magacin: {len(self.aisle_nodes)} prolaza, {len(self.loc_to_block_rc)} lokacija")

    def _create_fallback_map(self):
        print("🏗️ Kreiram fallback mapu za testiranje...")
        self.drawer_w = 0.5
        self.aisle_w = 1.0
        self.block_depth = 0.5
        
        self.cell_by_rc = {}
        self.aisle_nodes = []
        self.aisle_index = {}
        self.adj = []
        self.loc_to_block_rc = {}
        self.loc_coords = {}
        self.entrances = {"ENTRANCE_1": (2, 2), "ENTRANCE_2": (2, 12)}
        
        test_locs = ["A581", "A87", "C1", "D52", "B1987"]
        for i, loc in enumerate(test_locs):
            self.loc_to_block_rc[loc] = (10 + i, 5)
            self.loc_coords[loc] = (1.25 + i*0.5, 10.25 + i*0.5)
        
        self.dist_matrix = [[0]]

    def _cellref_to_rc(self, cellref: str) -> Tuple[int, int]:
        m = re.match(r"^([A-Z]+)([0-9]+)$", cellref.strip().upper())
        if not m:
            raise ValueError(f"Neispravan cell ref: {cellref}")
        col_letters = m.group(1)
        row = int(m.group(2))
        col = 0
        for ch in col_letters:
            col = col * 26 + (ord(ch) - ord("A") + 1)
        return (row, col)

    def _nearest_aisle_to_cellref(self, cellref: str) -> Tuple[int, int]:
        target_rc = self._cellref_to_rc(cellref)
        if target_rc in self.cell_by_rc:
            tx, ty = self.cell_by_rc[target_rc]["x_m"], self.cell_by_rc[target_rc]["y_m"]
        else:
            tx, ty = 0.0, 0.0

        best_rc = None
        best_d = None
        for rc in self.aisle_nodes:
            x, y = self.cell_by_rc[rc]["x_m"], self.cell_by_rc[rc]["y_m"]
            d = abs(x - tx) + abs(y - ty)
            if best_d is None or d < best_d:
                best_d = d
                best_rc = rc
        if best_rc is None:
            raise ValueError("Nema aisle ćelija u mapi.")
        return best_rc

    def _dist_cells(self, rc1: Tuple[int, int], rc2: Tuple[int, int]) -> float:
        a = self.cell_by_rc[rc1]
        b = self.cell_by_rc[rc2]
        return abs(a["x_m"] - b["x_m"]) + abs(a["y_m"] - b["y_m"])

    def _all_pairs_shortest_paths(self) -> List[List[float]]:
        n = len(self.aisle_nodes)
        INF = 10**18
        dist_all = [[INF] * n for _ in range(n)]
        for s in range(n):
            dist = [INF] * n
            dist[s] = 0.0
            pq = [(0.0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d != dist[u]:
                    continue
                for v, w in self.adj[u]:
                    nd = d + w
                    if nd < dist[v]:
                        dist[v] = nd
                        heapq.heappush(pq, (nd, v))
            dist_all[s] = dist
        return dist_all

    def _block_adjacent_aisles(self, block_rc: Tuple[int, int]) -> List[int]:
        r, c = block_rc
        candidates = []
        for nr, nc in [(r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)]:
            nrc = (nr, nc)
            if nrc in self.aisle_index:
                candidates.append(self.aisle_index[nrc])
        if candidates:
            return candidates

        if block_rc in self.cell_by_rc:
            bx, by = self.cell_by_rc[block_rc]["x_m"], self.cell_by_rc[block_rc]["y_m"]
        else:
            bx, by = 0.0, 0.0

        best_i = None
        best_d = None
        for rc in self.aisle_nodes:
            ax, ay = self.cell_by_rc[rc]["x_m"], self.cell_by_rc[rc]["y_m"]
            d = abs(ax - bx) + abs(ay - by)
            if best_d is None or d < best_d:
                best_d = d
                best_i = self.aisle_index[rc]
        return [best_i] if best_i is not None else []

    def distance_between_locations(self, loc_a: str, loc_b: str) -> float:
        loc_a = loc_a.strip().upper()
        loc_b = loc_b.strip().upper()
        if loc_a == loc_b:
            return 0.0

        if loc_a in self.loc_coords and loc_b in self.loc_coords:
            x1, y1 = self.loc_coords[loc_a]
            x2, y2 = self.loc_coords[loc_b]
            return abs(x1 - x2) + abs(y1 - y2) + self.block_depth

        if loc_a not in self.loc_to_block_rc or loc_b not in self.loc_to_block_rc:
            raise ValueError(f"Lokacija nije u mapi: {loc_a} ili {loc_b}")

        a_block = self.loc_to_block_rc[loc_a]
        b_block = self.loc_to_block_rc[loc_b]
        a_adj = self._block_adjacent_aisles(a_block)
        b_adj = self._block_adjacent_aisles(b_block)

        best = math.inf
        for ai in a_adj:
            for bi in b_adj:
                d = self.dist_matrix[ai][bi]
                if d < best:
                    best = d

        if best == math.inf:
            raise ValueError(f"Nema puta između {loc_a} i {loc_b} kroz prolaze.")
        return float(best) + self.block_depth

    def distance_entrance_to_location(self, entrance_cell: str, loc: str) -> float:
        entrance_cell = entrance_cell.strip().upper()
        loc = loc.strip().upper()
        if entrance_cell not in self.entrances:
            raise ValueError(f"Ulaz nije definisan: {entrance_cell}")
        if loc not in self.loc_to_block_rc:
            raise ValueError(f"Lokacija nije u mapi: {loc}")

        e_rc = self.entrances[entrance_cell]
        e_i = self.aisle_index[e_rc]
        block_rc = self.loc_to_block_rc[loc]
        adj = self._block_adjacent_aisles(block_rc)
        
        if loc in self.loc_coords:
            x_loc, y_loc = self.loc_coords[loc]
            aisle_rc = self.aisle_nodes[adj[0]]
            x_aisle, y_aisle = self.cell_by_rc[aisle_rc]["x_m"], self.cell_by_rc[aisle_rc]["y_m"]
            to_loc_dist = abs(x_loc - x_aisle) + abs(y_loc - y_aisle)
        else:
            to_loc_dist = self.block_depth
            
        best = min(self.dist_matrix[e_i][ai] for ai in adj)
        return float(best) + to_loc_dist

    def distance_location_to_entrance(self, loc: str, entrance_cell: str) -> float:
        return self.distance_entrance_to_location(entrance_cell, loc)


try:
    WAREHOUSE = WarehouseMap(ROUTING_XLSX)
except Exception as e:
    print(f"❌ Ne mogu da učitan magacin: {e}")
    print("Koristim fallback instancu...")
    WAREHOUSE = WarehouseMap.__new__(WarehouseMap)
    WAREHOUSE._create_fallback_map()


# ----------------------------
# Routing optimizacija
# ----------------------------
def route_sector_then_within(locations: List[str]) -> List[str]:
    locs = [l.strip().upper() for l in locations if l.strip()]
    locs.sort(key=lambda x: (sector_rank(parse_location(x)[0]), parse_location(x)[1]))
    return locs


def route_hybrid(locations: List[str], max_cluster_size: int = 20) -> List[str]:
    locs = [l.strip().upper() for l in locations if l.strip()]
    if len(locs) <= 5:
        return route_optimal_multistart(locs)
    
    by_sector = {}
    for loc in locs:
        sec = sector_group(loc)
        by_sector.setdefault(sec, []).append(loc)
    
    sector_points = {}
    for sec, sec_locs in by_sector.items():
        if sec_locs:
            sector_points[sec] = sec_locs[0]
    
    sector_order = sorted(by_sector.keys(), key=lambda s: sector_rank(s))
    
    result = []
    for sec in sector_order:
        sec_locs = by_sector[sec]
        if len(sec_locs) <= 1:
            result.extend(sec_locs)
        else:
            opt_sec = route_optimal_multistart(sec_locs, max_starts=3)
            result.extend(opt_sec)
    
    return result


def route_optimal_multistart(locations: List[str], max_starts: int = 8) -> List[str]:
    locs = [l.strip().upper() for l in locations if l.strip()]
    if len(locs) <= 2:
        return locs

    entrances = list(WAREHOUSE.entrances.keys())

    def entrance_distance_to_loc(loc: str) -> float:
        return min(WAREHOUSE.distance_entrance_to_location(en, loc) for en in entrances)

    ranked = sorted(locs, key=entrance_distance_to_loc)
    K = min(max_starts, len(ranked))

    def nn_path(start_loc: str) -> List[str]:
        remaining = locs[:]
        remaining.remove(start_loc)
        path = [start_loc]
        while remaining:
            last = path[-1]
            j = min(range(len(remaining)), key=lambda k: WAREHOUSE.distance_between_locations(last, remaining[k]))
            path.append(remaining.pop(j))
        return path

    def two_opt(path: List[str]) -> List[str]:
        def inner_len(p: List[str]) -> float:
            s = 0.0
            for i in range(len(p) - 1):
                s += WAREHOUSE.distance_between_locations(p[i], p[i + 1])
            return s

        best = path
        best_len = inner_len(best)
        improved = True
        while improved:
            improved = False
            for i in range(1, len(best) - 2):
                for k in range(i + 1, len(best) - 1):
                    cand = best[:i] + list(reversed(best[i:k + 1])) + best[k + 1:]
                    cand_len = inner_len(cand)
                    if cand_len + 1e-9 < best_len:
                        best = cand
                        best_len = cand_len
                        improved = True
                        break
                if improved:
                    break
        return best

    best_path = None
    best_cost = None

    for start_loc in ranked[:K]:
        p = nn_path(start_loc)
        p = two_opt(p)
        _, _, total = best_entrance_combo_for_path(p)
        if best_cost is None or total < best_cost:
            best_cost = total
            best_path = p

    return best_path or locs


def best_entrance_combo_for_path(path: List[str]) -> Tuple[str, str, float]:
    entrances = list(WAREHOUSE.entrances.keys())
    if not entrances:
        raise ValueError("Nema definisanih ulaza (ENTRANCES).")
    if not path:
        return (entrances[0], entrances[0], 0.0)

    best = None
    for s in entrances:
        for e in entrances:
            dist = 0.0
            try:
                dist += WAREHOUSE.distance_entrance_to_location(s, path[0])
                for i in range(len(path) - 1):
                    dist += WAREHOUSE.distance_between_locations(path[i], path[i + 1])
                dist += WAREHOUSE.distance_location_to_entrance(path[-1], e)
                if best is None or dist < best[2]:
                    best = (s, e, dist)
            except Exception as err:
                log_action("ROUTE", f"Greška u distanci: {err}", {"start": s, "end": e})
                continue
                
    if best is None:
        raise ValueError("Ne mogu da izračunam rutu - proveri ulaze i lokacije")
    return best


def compute_route(locations: List[str], mode: str) -> List[str]:
    mode = (mode or "").strip().lower()
    if mode == "sector":
        return route_sector_then_within(locations)
    elif mode == "hybrid":
        return route_hybrid(locations)
    else:
        return route_optimal_multistart(locations)


def build_route_legs(path: List[str], start: str, end: str) -> Dict[str, Any]:
    legs = []
    total = 0.0
    if not path:
        return {"legs": [], "total_m": 0.0}

    d0 = WAREHOUSE.distance_entrance_to_location(start, path[0])
    legs.append({"from": start, "to": path[0], "dist_m": round(d0, 2)})
    total += d0

    for i in range(len(path) - 1):
        d = WAREHOUSE.distance_between_locations(path[i], path[i + 1])
        legs.append({"from": path[i], "to": path[i + 1], "dist_m": round(d, 2)})
        total += d

    dlast = WAREHOUSE.distance_location_to_entrance(path[-1], end)
    legs.append({"from": path[-1], "to": end, "dist_m": round(dlast, 2)})
    total += dlast

    return {"legs": legs, "total_m": round(total, 2)}


# ----------------------------
# Session persistence
# ----------------------------
def save_sessions():
    try:
        serializable = {}
        for sid, sess in SESSIONS.items():
            serializable[sid] = {
                **sess,
                "created_at": str(sess["created_at"]),
                "items_by_loc": sess["items_by_loc"]
            }
        with open(SESSION_DB, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, default=str)
        log_action("SYSTEM", "Sesije sačuvane", {"count": len(SESSIONS)})
    except Exception as e:
        print(f"⚠️ Ne mogu da sačuvam sesije: {e}")


def load_sessions():
    global SESSIONS
    try:
        if os.path.exists(SESSION_DB):
            with open(SESSION_DB, "r", encoding="utf-8") as f:
                data = json.load(f)
            SESSIONS = data
            log_action("SYSTEM", "Sesije učitane", {"count": len(SESSIONS)})
    except Exception as e:
        print(f"⚠️ Ne mogu da učitan sesije: {e}")


# ----------------------------
# Phase 2: Picking sessions
# ----------------------------
class PickItem(BaseModel):
    sku: str = Field(..., description="6 cifara", pattern=r"^\d{6}$")
    qty: int = Field(..., ge=1)
    location: str


class StartTaskRequest(BaseModel):
    items: List[PickItem]
    mode: str = "optimal"


class UpdateItemRequest(BaseModel):
    sku: str
    action: str
    qty_picked: Optional[int] = None
    note: Optional[str] = None


SESSIONS: Dict[str, dict] = {}
load_sessions()


def _validate_item(it: PickItem) -> None:
    if not SKU_RE.match(it.sku.strip()):
        raise ValueError(f"SKU mora imati 6 cifara: {it.sku}")
    try:
        parse_location(it.location)
    except ValueError as e:
        raise ValueError(f"Neispravna lokacija: {it.location}")


def _group_items(items: List[PickItem]) -> Dict[str, List[dict]]:
    grouped = {}
    for it in items:
        loc = it.location.strip().upper()
        sku = it.sku.strip()
        req_qty = int(it.qty)
        grouped.setdefault(loc, []).append(
            {
                "sku": sku,
                "qty_required": req_qty,
                "qty_picked": 0,
                "qty_missing": req_qty,
                "status": "pending",
                "note": None,
                "updated_at": None,
                "original_qty": req_qty,
            }
        )
    return grouped


def _item_done(it: dict) -> bool:
    return it["status"] in ("taken", "oos", "problem", "skipped")


def _location_done(arr: List[dict]) -> bool:
    return bool(arr) and all(_item_done(x) for x in arr)


def _session_progress(sess: dict) -> Dict[str, Any]:
    total_items = 0
    done_items = 0
    total_qty = 0
    picked_qty = 0
    
    for _, arr in sess["items_by_loc"].items():
        for it in arr:
            total_items += 1
            total_qty += it["qty_required"]
            picked_qty += it["qty_picked"]
            if _item_done(it):
                done_items += 1

    total_locs = len(sess["ordered_locations"])
    done_locs = 0
    for loc in sess["ordered_locations"]:
        arr = sess["items_by_loc"].get(loc, [])
        if _location_done(arr):
            done_locs += 1

    return {
        "done_items": done_items,
        "total_items": total_items,
        "done_locations": done_locs,
        "total_locations": total_locs,
        "current_index": sess["current_index"],
        "picked_qty": picked_qty,
        "total_qty": total_qty,
        "progress_percent": round((picked_qty / total_qty * 100) if total_qty > 0 else 0, 1)
    }


def _advance_if_done(sess: dict) -> None:
    while sess["current_index"] < len(sess["ordered_locations"]):
        loc = sess["ordered_locations"][sess["current_index"]]
        arr = sess["items_by_loc"].get(loc, [])
        if _location_done(arr):
            sess["current_index"] += 1
            continue
        break


@app.post("/task/start")
def start_task(req: StartTaskRequest):
    log_action("START", f"Pokretanje taska", {"mode": req.mode, "items": len(req.items)})
    
    if not req.items:
        raise HTTPException(status_code=400, detail="items je prazno.")

    items = []
    for it in req.items:
        try:
            _validate_item(it)
        except Exception as e:
            log_action("START", f"VALIDATION_ERROR: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))

        loc = it.location.strip().upper()
        if loc not in WAREHOUSE.loc_to_block_rc:
            log_action("START", f"LOKACIJA_NEPOSTOJI: {loc}")
            raise HTTPException(status_code=400, detail=f"Lokacija nije u mapi: {loc}")
        items.append(it)

    items_by_loc = _group_items(items)
    unique_locs = list(items_by_loc.keys())
    log_action("START", f"Unikatne lokacije: {len(unique_locs)}")

    ordered = compute_route(unique_locs, req.mode)
    start, end, dist = best_entrance_combo_for_path(ordered)

    session_id = str(uuid.uuid4())
    sess = {
        "id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "mode": req.mode,
        "start": start,
        "end": end,
        "distance_m": float(dist),
        "ordered_locations": ordered,
        "items_by_loc": items_by_loc,
        "current_index": 0,
    }
    _advance_if_done(sess)
    SESSIONS[session_id] = sess
    save_sessions()

    cur = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None
    legs = build_route_legs(ordered, start, end)
    progress = _session_progress(sess)

    log_action("START", f"Session kreiran: {session_id}", {"current": cur, "progress": progress})
    
    return {
        "session_id": session_id,
        "mode": req.mode,
        "start": start,
        "end": end,
        "ordered_locations": ordered,
        "distance_m": round(float(dist), 2),
        "current_location": cur,
        "progress": progress,
        "items_by_loc": sess["items_by_loc"],
        "route_legs": legs,
    }


@app.get("/task/{session_id}")
def get_task(session_id: str):
    sess = SESSIONS.get(session_id)
    if not sess:
        log_action("GET", f"Session ne postoji: {session_id}")
        raise HTTPException(status_code=404, detail="Session not found")
    
    _advance_if_done(sess)
    cur = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None
    legs = build_route_legs(sess["ordered_locations"], sess["start"], sess["end"])
    progress = _session_progress(sess)
    
    return {
        "session_id": session_id,
        "mode": sess["mode"],
        "start": sess["start"],
        "end": sess["end"],
        "distance_m": round(float(sess["distance_m"]), 2),
        "ordered_locations": sess["ordered_locations"],
        "current_location": cur,
        "items_by_loc": sess["items_by_loc"],
        "progress": progress,
        "route_legs": legs,
    }


@app.post("/task/{session_id}/update")
def update_item(session_id: str, req: UpdateItemRequest):
    sess = SESSIONS.get(session_id)
    if not sess:
        log_action("UPDATE", f"Session ne postoji: {session_id}")
        raise HTTPException(status_code=404, detail="Session not found")

    _advance_if_done(sess)
    if sess["current_index"] >= len(sess["ordered_locations"]):
        return {"ok": True, "done": True, "message": "Sve završeno.", "task": get_task(session_id)}

    cur_loc = sess["ordered_locations"][sess["current_index"]]
    sku = req.sku.strip()

    if not SKU_RE.match(sku):
        raise HTTPException(status_code=400, detail="SKU mora imati 6 cifara.")

    action = (req.action or "").strip().lower()
    if action not in ["take", "oos", "problem", "skip"]:
        raise HTTPException(status_code=400, detail="action mora biti: take | oos | problem | skip")

    note = req.note.strip() if req.note else None
    now = datetime.utcnow().isoformat()

    arr = sess["items_by_loc"].get(cur_loc, [])
    found = False
    
    for it in arr:
        if it["sku"] == sku:
            found = True
            
            if action == "take":
                if req.qty_picked is None:
                    raise HTTPException(status_code=400, detail="qty_picked je obavezan za action=take")
                picked = int(req.qty_picked)
                
                if picked < 0 or picked > int(it["qty_required"]):
                    raise HTTPException(status_code=400, detail=f"qty_picked mora biti između 0 i {it['qty_required']}")

                it["qty_picked"] = picked
                it["qty_missing"] = int(it["qty_required"]) - picked
                
                if it["qty_missing"] == 0:
                    it["status"] = "taken"
                else:
                    it["status"] = "partial"
                
                it["note"] = note
                it["updated_at"] = now
                log_action("UPDATE", f"TAKE {sku}@{cur_loc}", {"picked": picked, "missing": it["qty_missing"]})

            elif action == "oos":
                it["status"] = "oos"
                it["note"] = note
                it["updated_at"] = now
                it["qty_missing"] = int(it["qty_required"]) - it["qty_picked"]
                log_action("UPDATE", f"OOS {sku}@{cur_loc}", {"note": note})

            elif action == "problem":
                if req.qty_picked is not None:
                    it["qty_picked"] = int(req.qty_picked)
                    it["qty_missing"] = int(it["qty_required"]) - int(req.qty_picked)
                else:
                    it["qty_picked"] = 0
                    it["qty_missing"] = int(it["qty_required"])
                    
                it["status"] = "problem"
                it["note"] = note
                it["updated_at"] = now
                log_action("UPDATE", f"PROBLEM {sku}@{cur_loc}", {
                    "note": note, 
                    "picked": it["qty_picked"],
                    "missing": it["qty_missing"]
                })
                
            elif action == "skip":
                it["status"] = "oos"
                it["note"] = "Preskočeno (Skip dugme)" + (f" - {note}" if note else "")
                it["updated_at"] = now
                it["qty_missing"] = 0
                log_action("UPDATE", f"SKIP {sku}@{cur_loc}")
                
            break
    
    if not found:
        raise HTTPException(status_code=400, detail=f"SKU {sku} ne postoji na lokaciji {cur_loc}.")

    _advance_if_done(sess)
    save_sessions()
    
    cur2 = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None

    return {
        "ok": True,
        "done": cur2 is None,
        "current_location": cur2,
        "progress": _session_progress(sess),
        "task": get_task(session_id),
    }


@app.get("/sessions")
def list_sessions():
    result = []
    for sid, sess in SESSIONS.items():
        progress = _session_progress(sess)
        result.append({
            "session_id": sid,
            "created_at": sess["created_at"],
            "mode": sess["mode"],
            "progress": f"{progress['done_items']}/{progress['total_items']}",
            "current": sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else "DONE"
        })
    return {"sessions": result}


@app.delete("/task/{session_id}")
def delete_session(session_id: str):
    if session_id in SESSIONS:
        del SESSIONS[session_id]
        save_sessions()
        log_action("DELETE", f"Session obrisan: {session_id}")
        return {"ok": True, "message": "Session deleted"}
    raise HTTPException(status_code=404, detail="Session not found")


@app.get("/map")
def get_map():
    return {
        "aisle_cells": len(WAREHOUSE.aisle_nodes),
        "locations": len(WAREHOUSE.loc_to_block_rc),
        "entrances": list(WAREHOUSE.entrances.keys()),
        "block_depth_m": WAREHOUSE.block_depth,
        "drawer_width_m": WAREHOUSE.drawer_w,
        "aisle_width_m": WAREHOUSE.aisle_w,
    }


@app.get("/export-report/{session_id}")
def export_report(session_id: str):
    sess = SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    
    report = {
        "session_id": session_id,
        "created_at": sess["created_at"],
        "mode": sess["mode"],
        "start_entrance": sess["start"],
        "end_entrance": sess["end"],
        "total_distance_m": sess["distance_m"],
        "locations": [],
        "summary": _session_progress(sess)
    }
    
    for loc in sess["ordered_locations"]:
        items = sess["items_by_loc"].get(loc, [])
        loc_data = {
            "location": loc,
            "items": items,
            "status": "done" if all(_item_done(i) for i in items) else "pending"
        }
        report["locations"].append(loc_data)
    
    return report


@app.get("/__debug")
def debug_info():
    return {
        "file": __file__,
        "warehouse_loaded": hasattr(WAREHOUSE, "loc_to_block_rc"),
        "warehouse_locations": len(WAREHOUSE.loc_to_block_rc),
        "active_sessions": len(SESSIONS),
        "routes": [{"path": getattr(r, "path", str(r)), "name": getattr(r, "name", "N/A")} for r in app.routes]
    }