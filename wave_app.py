from __future__ import annotations

import os
import re
import math
import uuid
import json
import heapq
import threading
from datetime import datetime
from typing import List, Tuple, Dict, Optional, Any

import pandas as pd
# OR-Tools import sa jasnim fallback-om
ORTOOLS_AVAILABLE = False
try:
    from ortools.constraint_solver import routing_enums_pb2, pywrapcp
    ORTOOLS_AVAILABLE = True
    print("OR-Tools uspesno uvezen")
except ImportError as e:
    print(f"OR-Tools nije instaliran: {e}")
    print("Instaliraj sa: pip install ortools")
except Exception as e:
    print(f"Neocekivana greska pri uvozu OR-Tools: {e}")
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_TITLE = "Warehouse Wave Picking (6 kutija)"
ROUTING_XLSX = os.path.join(os.path.dirname(__file__), "MAG_ROUTING_DATA_v2.xlsx")
SESSION_DB = os.path.join(os.path.dirname(__file__), "wave_sessions.json")

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
# Persistent sessions
# ----------------------------
def load_sessions() -> Dict[str, dict]:
    """Uitava sesije iz JSON fajla ako postoji."""
    if os.path.exists(SESSION_DB):
        try:
            with open(SESSION_DB, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f" Greka pri uitavanju sesija: {e}")
    return {}

def save_sessions() -> None:
    """uva sve sesije u JSON fajl."""
    try:
        with open(SESSION_DB, "w", encoding="utf-8") as f:
            json.dump(WAVE_SESSIONS, f, indent=2, default=str)
    except Exception as e:
        print(f" Greka pri uvanju sesija: {e}")
# Debounced save to reduce per-click latency
_save_lock = threading.Lock()
_save_timer: Optional[threading.Timer] = None

def save_sessions_debounced(delay_s: float = 1.0) -> None:
    global _save_timer
    with _save_lock:
        if _save_timer and _save_timer.is_alive():
            return

        def _flush():
            global _save_timer
            try:
                save_sessions()
            finally:
                with _save_lock:
                    _save_timer = None

        _save_timer = threading.Timer(delay_s, _flush)
        _save_timer.daemon = True
        _save_timer.start()

# ----------------------------
# Logging
# ----------------------------
def log_action(session_id: str, action: str, data: dict = None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] SID:{session_id} {action}"
    if data:
        log_entry += f" {json.dumps(data, default=str)}"
    print(log_entry)
    
    log_file = os.path.join(os.path.dirname(__file__), "wave.log")
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
            print(f" GREKA pri uitavanju Excel fajla: {e}")
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
        print(f" Uitan magacin: {len(self.aisle_nodes)} prolaza, {len(self.loc_to_block_rc)} lokacija")

    def _create_fallback_map(self):
        print(" Kreiram fallback mapu za testiranje...")
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
            raise ValueError("Nema aisle elija u mapi.")
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
            raise ValueError(f"Nema puta izmeu {loc_a} i {loc_b} kroz prolaze.")
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


# Global warehouse instance
try:
    WAREHOUSE = WarehouseMap(ROUTING_XLSX)
except Exception as e:
    print(f" Ne mogu da uitan magacin: {e}")
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

    try:
        entrances = list(WAREHOUSE.entrances.keys())

        def entrance_distance_to_loc(loc: str) -> float:
            try:
                return min(WAREHOUSE.distance_entrance_to_location(en, loc) for en in entrances)
            except Exception:
                return 100.0

        ranked = sorted(locs, key=entrance_distance_to_loc)
        K = min(max_starts, len(ranked))

        def nn_path(start_loc: str) -> List[str]:
            remaining = locs[:]
            remaining.remove(start_loc)
            path = [start_loc]
            while remaining:
                last = path[-1]
                try:
                    j = min(range(len(remaining)), key=lambda k: WAREHOUSE.distance_between_locations(last, remaining[k]))
                except Exception:
                    j = 0
                path.append(remaining.pop(j))
            return path

        def two_opt(path: List[str]) -> List[str]:
            # Za veoma velike rute preskacemo 2-opt da ne blokira start.
            if len(path) > 120:
                return path
            def inner_len(p: List[str]) -> float:
                s = 0.0
                for i in range(len(p) - 1):
                    try:
                        s += WAREHOUSE.distance_between_locations(p[i], p[i + 1])
                    except Exception:
                        s += 10.0
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
            try:
                p = nn_path(start_loc)
                p = two_opt(p)
                _, _, total = best_entrance_combo_for_path(p)
                if best_cost is None or total < best_cost:
                    best_cost = total
                    best_path = p
            except Exception as e:
                print(f"Greska za start {start_loc}: {e}")
                continue

        return best_path or locs
    except Exception as e:
        print(f"Greska u optimal_multistart: {e}")
        return locs


def route_tsp_ortools(locations: List[str], time_limit_s: int = 120) -> List[str]:
    locs = [l.strip().upper() for l in locations if l.strip()]
    if len(locs) <= 2:
        return locs

    n = len(locs)
    dummy_idx = n

    def dist(i: int, j: int) -> int:
        if i == dummy_idx or j == dummy_idx:
            return 0
        return int(round(WAREHOUSE.distance_between_locations(locs[i], locs[j]) * 100))

    manager = pywrapcp.RoutingIndexManager(n + 1, 1, dummy_idx)
    routing = pywrapcp.RoutingModel(manager)

    def cb(from_index: int, to_index: int) -> int:
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        return dist(i, j)

    transit_cb = routing.RegisterTransitCallback(cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.seconds = int(time_limit_s)
    search_params.log_search = False

    solution = routing.SolveWithParameters(search_params)
    if not solution:
        log_action("ROUTE", "OR-Tools nema resenje, fallback", {"count": len(locs)})
        return locs

    order = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != dummy_idx:
            order.append(locs[node])
        index = solution.Value(routing.NextVar(index))

    return order


def route_with_ortools(locations: List[str], time_limit_seconds: int = 10) -> List[str]:
    """
    Koristi Google OR-Tools za pronalazenje najkrace putanje.
    """
    print("OR-Tools funkcija je pozvana")
    print(f"ORTOOLS_AVAILABLE = {ORTOOLS_AVAILABLE}")
    print(f"Broj lokacija: {len(locations)}")

    locs = [l.strip().upper() for l in locations if l.strip()]

    if len(locs) <= 3:
        print("Premalo lokacija za OR-Tools")
        return locs

    if not ORTOOLS_AVAILABLE:
        print("OR-Tools nije dostupan, koristim fallback")
        return route_optimal_multistart(locs)

    try:
        print(f"OR-Tools je dostupan, pokusavam TSP za {len(locs)} lokacija...")

        # Mapiranje lokacija -> indeksi
        loc_to_index = {loc: i for i, loc in enumerate(locs)}
        index_to_loc = {i: loc for i, loc in enumerate(locs)}

        # Matrica udaljenosti
        n = len(locs)
        distance_matrix = [[0] * n for _ in range(n)]

        print("Racunam matricu udaljenosti...")
        for i, loc_i in enumerate(locs):
            for j, loc_j in enumerate(locs):
                if i != j:
                    try:
                        dist = WAREHOUSE.distance_between_locations(loc_i, loc_j)
                        distance_matrix[i][j] = max(1, int(dist * 1000))
                    except Exception as e:
                        print(f"Greska pri racunanju distance {loc_i}->{loc_j}: {e}")
                        distance_matrix[i][j] = 10000
                else:
                    distance_matrix[i][j] = 0

        first_solution_strategies = [
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC,
            routing_enums_pb2.FirstSolutionStrategy.SAVINGS,
            routing_enums_pb2.FirstSolutionStrategy.SWEEP,
            routing_enums_pb2.FirstSolutionStrategy.CHRISTOFIDES,
        ]

        strategy_names = {
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC: "PATH_CHEAPEST_ARC",
            routing_enums_pb2.FirstSolutionStrategy.SAVINGS: "SAVINGS",
            routing_enums_pb2.FirstSolutionStrategy.SWEEP: "SWEEP",
            routing_enums_pb2.FirstSolutionStrategy.CHRISTOFIDES: "CHRISTOFIDES",
        }

        best_route = None
        best_distance = float("inf")

        for strategy_idx, strategy in enumerate(first_solution_strategies):
            strategy_name = strategy_names.get(strategy, "NEPOZNATA")
            print(f"Pokrecem strategiju {strategy_idx + 1}: {strategy_name}")

            try:
                manager = pywrapcp.RoutingIndexManager(n, 1, 0)
                routing = pywrapcp.RoutingModel(manager)

                def distance_callback(from_index, to_index):
                    from_node = manager.IndexToNode(from_index)
                    to_node = manager.IndexToNode(to_index)
                    return distance_matrix[from_node][to_node]

                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                search_parameters.first_solution_strategy = strategy
                search_parameters.local_search_metaheuristic = (
                    routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
                )
                search_parameters.time_limit.seconds = max(2, time_limit_seconds // len(first_solution_strategies))
                search_parameters.log_search = False

                solution = routing.SolveWithParameters(search_parameters)

                if solution:
                    index = routing.Start(0)
                    route = []
                    while not routing.IsEnd(index):
                        node = manager.IndexToNode(index)
                        route.append(index_to_loc[node])
                        index = solution.Value(routing.NextVar(index))

                    total_dist = 0.0
                    for i in range(len(route) - 1):
                        try:
                            total_dist += WAREHOUSE.distance_between_locations(route[i], route[i + 1])
                        except Exception:
                            total_dist += 10.0

                    print(f"  Strategija {strategy_name} dala rutu od {total_dist:.1f}m")

                    if total_dist < best_distance:
                        best_distance = total_dist
                        best_route = route
                else:
                    print(f"  Strategija {strategy_name} nije uspela")
            except Exception as e:
                print(f"  Greska u strategiji {strategy_name}: {e}")

        if best_route:
            print(f"OR-Tools pronasao rutu od {best_distance:.1f}m")
            return best_route

    except Exception as e:
        print(f"OR-Tools neocekivana greska: {e}")

    print("OR-Tools ne radi, koristim fallback algoritam")
    return route_optimal_multistart(locs)


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
                log_action("ROUTE", f"Greka u distanci: {err}", {"start": s, "end": e})
                continue
                
    if best is None:
        raise ValueError("Ne mogu da izraunam rutu - proveri ulaze i lokacije")
    return best


def compute_route(locations: List[str], mode: str) -> List[str]:
    mode = (mode or "").strip().lower()
    locs = [l.strip().upper() for l in locations if l.strip()]
    print(f"\ncompute_route: mode='{mode}', lokacija={len(locs)}")
    print(f"ORTOOLS_AVAILABLE = {ORTOOLS_AVAILABLE}")

    if mode == "sector":
        print("Koristim sector algoritam")
        return route_sector_then_within(locs)
    elif mode == "hybrid":
        print("Koristim hybrid algoritam")
        return route_hybrid(locs)
    elif mode == "ortools":
        print("Koristim OR-Tools (eksplicitno)")
        return route_with_ortools(locs)
    else:  # "optimal"
        if ORTOOLS_AVAILABLE and len(locs) > 5:
            print("OR-Tools dostupan, koristim ga za optimal")
            return route_with_ortools(locs)
        else:
            print("OR-Tools nije dostupan, koristim standardni")
            return route_optimal_multistart(locs)


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


# Reusable response builder to avoid extra /wave/{id} round-trips
def _build_wave_response(sess: dict, include_route_legs: bool = True) -> Dict[str, Any]:
    _advance_if_done(sess)
    cur = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None
    progress = _session_progress(sess)
    return {
        "session_id": sess["id"],
        "mode": sess["mode"],
        "start": sess["start"],
        "end": sess["end"],
        "distance_m": round(float(sess["distance_m"]), 2),
        "ordered_locations": sess["ordered_locations"],
        "current_location": cur,
        "items_by_loc": sess["items_by_loc"],
        "box_assignment": sess["box_assignment"],
        "progress": progress,
        
    }
# ----------------------------
# Wave Picking Modeli
# ----------------------------
class WaveItem(BaseModel):
    sku: str
    qty: int
    location: str
    invoice: str


class StartWaveRequest(BaseModel):
    items: List[WaveItem]
    mode: str = "optimal"


class WaveUpdateRequest(BaseModel):
    sku: str
    invoice: str
    action: str
    qty_picked: Optional[int] = None
    note: Optional[str] = None
    location: Optional[str] = None  # opciona lokacija za runi unos


# Uitavanje sesija iz fajla pri startu
WAVE_SESSIONS: Dict[str, dict] = load_sessions()


def _validate_item(it: dict) -> None:
    if not SKU_RE.match(it["sku"].strip()):
        raise ValueError(f"SKU mora imati 6 cifara: {it['sku']}")
    try:
        parse_location(it["location"])
    except ValueError as e:
        raise ValueError(f"Neispravna lokacija: {it['location']}")


def _group_items_by_location(items: List[dict]) -> Dict[str, List[dict]]:
    grouped = {}
    for it in items:
        loc = it["location"].strip().upper()
        grouped.setdefault(loc, []).append({
            "sku": it["sku"],
            "qty_required": it["qty"],
            "qty_picked": 0,
            "qty_missing": it["qty"],
            "status": "pending",
            "invoice": it["invoice"],
            "box": None,
            "note": None,
            "updated_at": None,
        })
    return grouped


def _item_done(it: dict) -> bool:
    # SAMO "taken" se smatra gotovim za potrebe napretka
    return it["status"] in {"taken", "oos", "problem"}


def _location_done(arr: List[dict]) -> bool:
    return bool(arr) and all(_item_done(x) for x in arr)


def _session_progress(sess: dict) -> Dict[str, Any]:
    total_items = 0
    done_items = 0
    total_qty = 0
    picked_qty = 0
    
    boxes_progress = {}
    
    for _, arr in sess["items_by_loc"].items():
        for it in arr:
            total_items += 1
            total_qty += it["qty_required"]
            picked_qty += it["qty_picked"]
            if _item_done(it):
                done_items += 1
            
            inv = it["invoice"]
            if inv not in boxes_progress:
                boxes_progress[inv] = {"total": 0, "done": 0}
            # progress po kolicini (ne po statusu)
            boxes_progress[inv]["total"] += it["qty_required"]
            boxes_progress[inv]["done"] += it["qty_picked"]

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
        "progress_percent": round((picked_qty / total_qty * 100) if total_qty > 0 else 0, 1),
        "boxes": boxes_progress
    }


def _advance_if_done(sess: dict) -> None:
    while sess["current_index"] < len(sess["ordered_locations"]):
        loc = sess["ordered_locations"][sess["current_index"]]
        arr = sess["items_by_loc"].get(loc, [])
        if _location_done(arr):
            sess["current_index"] += 1
            continue
        break


@app.post("/wave/start")
def start_wave(req: StartWaveRequest):
    print(f"\n=== WAVE START ===")
    print(f"Primljeno {len(req.items)} stavki")
    
    log_action("WAVE_START", f"Pokretanje wave pickinga", {"items": len(req.items), "mode": req.mode})
    
    if not req.items:
        raise HTTPException(status_code=400, detail="Nema stavki.")
    
    # Grupii po faktutama
    invoices = list(dict.fromkeys(it.invoice for it in req.items))
    print(f"Fakture ({len(invoices)}): {invoices}")
    
    if len(invoices) > 6:
        raise HTTPException(status_code=400, detail=f"Previe faktura! Maksimalno 6, ima ih {len(invoices)}")
    
    # Pripremi sve stavke
    all_items = []
    for it in req.items:
        try:
            _validate_item({"sku": it.sku, "location": it.location})
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        
        all_items.append({
            "sku": it.sku,
            "qty": it.qty,
            "location": it.location,
            "invoice": it.invoice
        })
    
    # Grupii po lokacijama za rutu
    items_by_loc = _group_items_by_location(all_items)
    unique_locs = list(items_by_loc.keys())
    print(f"Lokacije za rutu ({len(unique_locs)}): {unique_locs}")
    
    # Dodeli kutije (1-6) po redu faktura
    box_assignment = {}
    for i, inv in enumerate(invoices):
        box_assignment[inv] = i + 1
    print(f"Dodela kutija: {box_assignment}")
    
    # Dodaj box broj u svaku stavku
    for loc, items in items_by_loc.items():
        for it in items:
            it["box"] = box_assignment[it["invoice"]]
    
    # Optimizuj rutu za SVE lokacije
    ordered = compute_route(unique_locs, req.mode)
    print(f"Optimizovana ruta: {ordered}")
    
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
        "box_assignment": box_assignment,
        "current_index": 0,
    }
    
    WAVE_SESSIONS[session_id] = sess
    save_sessions()  #  uvamo odmah
    _advance_if_done(sess)
    
    cur = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None
    progress = _session_progress(sess)
    
    print(f"Session kreiran: {session_id}")
    print(f"Trenutna lokacija: {cur}")
    print(f"=== KRAJ WAVE START ===\n")
    
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
        "box_assignment": box_assignment,
        
    }


@app.get("/wave/{session_id}")
def get_wave(session_id: str):
    sess = WAVE_SESSIONS.get(session_id)
    if not sess:
        log_action("WAVE_GET", f"Sesija ne postoji: {session_id}")
        raise HTTPException(status_code=404, detail="Session not found")

    return _build_wave_response(sess)


@app.post("/wave/{session_id}/update")
def update_wave_item(session_id: str, req: WaveUpdateRequest):
    sess = WAVE_SESSIONS.get(session_id)
    if not sess:
        log_action("WAVE_UPDATE", f"Sesija ne postoji: {session_id}")
        raise HTTPException(status_code=404, detail="Session not found")

    _advance_if_done(sess)
    
    # Odredi na kojoj lokaciji radimo
    if req.action == "dopuna":
        # Za dopunu ne proveravamo lokaciju - traiemo po celoj sesiji
        target_loc = None
    elif req.location:
        target_loc = req.location.strip().upper()
        if target_loc not in sess["items_by_loc"]:
            raise HTTPException(status_code=400, detail="Lokacija ne postoji u ovoj sesiji.")
    else:
        if sess["current_index"] >= len(sess["ordered_locations"]):
            return {"ok": True, "done": True, "message": "Sve zavreno."}
        target_loc = sess["ordered_locations"][sess["current_index"]]

    sku = req.sku.strip()

    if not SKU_RE.match(sku):
        raise HTTPException(status_code=400, detail="SKU mora imati 6 cifara.")

    action = (req.action or "").strip().lower()
    if action not in ["take", "oos", "problem", "dopuna"]:
        raise HTTPException(status_code=400, detail="action mora biti: take | oos | problem | dopuna")

    note = req.note.strip() if req.note else None
    now = datetime.utcnow().isoformat()

    # Pronai artikal
    found = False
    target_it = None
    target_loc_found = None
    
    if action == "dopuna":
        # Za dopunu, pretrai sve lokacije
        for loc, items in sess["items_by_loc"].items():
            for it in items:
                if it["sku"] == sku and it["invoice"] == req.invoice:
                    found = True
                    target_it = it
                    target_loc_found = loc
                    break
            if found:
                break
        
        if not found:
            raise HTTPException(status_code=400, detail=f"SKU {sku} za fakturu {req.invoice} ne postoji ni na jednoj lokaciji.")
    else:
        # Za ostale akcije, trai samo na target_loc
        arr = sess["items_by_loc"].get(target_loc, [])
        for it in arr:
            if it["sku"] == sku and it["invoice"] == req.invoice:
                found = True
                target_it = it
                target_loc_found = target_loc
                break
        
        if not found:
            raise HTTPException(status_code=400, detail=f"SKU {sku} za fakturu {req.invoice} ne postoji na lokaciji {target_loc}.")
    
    # Sada radi sa target_it
    it = target_it
    target_loc = target_loc_found
    
    if action == "take":
        if req.qty_picked is None:
            raise HTTPException(status_code=400, detail="qty_picked je obavezan za action=take")
        picked = int(req.qty_picked)
        
        if picked < 0 or picked > int(it["qty_required"]):
            raise HTTPException(status_code=400, detail=f"qty_picked mora biti izmeu 0 i {it['qty_required']}")

        it["qty_picked"] = picked
        it["qty_missing"] = int(it["qty_required"]) - picked
        
        if it["qty_missing"] == 0:
            it["status"] = "taken"
        else:
            it["status"] = "partial"
        
        it["note"] = note
        it["updated_at"] = now
        log_action("WAVE_UPDATE", f"TAKE {sku} {req.invoice}@{target_loc}", {"picked": picked})

    elif action == "oos":
        it["status"] = "oos"
        it["note"] = (it["note"] or "") + " | " + (note or "OOS prijavljen")
        it["updated_at"] = now
        log_action("WAVE_UPDATE", f"OOS {sku} {req.invoice}@{target_loc}", {"note": note})
        
        # Pomeri na sledeu lokaciju (samo ako radimo na trenutnoj)
        if not req.location and sess["current_index"] < len(sess["ordered_locations"]) - 1:
            sess["current_index"] += 1

    elif action == "problem":
        it["status"] = "problem"
        if req.qty_picked is not None:
            it["qty_picked"] = int(req.qty_picked)
            it["qty_missing"] = int(it["qty_required"]) - int(req.qty_picked)
        it["note"] = (it["note"] or "") + " | " + (note or "Problem prijavljen")
        it["updated_at"] = now
        log_action("WAVE_UPDATE", f"PROBLEM {sku} {req.invoice}@{target_loc}", {"picked": req.qty_picked, "note": note})
        
        # Pomeri na sledeu lokaciju (samo ako radimo na trenutnoj)
        if not req.location and sess["current_index"] < len(sess["ordered_locations"]) - 1:
            sess["current_index"] += 1
        
    elif action == "dopuna":
        # DIREKTNO postavi na "taken" i qty_picked na required
        it["status"] = "taken"
        it["qty_picked"] = it["qty_required"]
        it["qty_missing"] = 0
        it["note"] = (it["note"] or "") + " | " + (note or "Dopunjeno do pune koliine")
        it["updated_at"] = now
        log_action("WAVE_UPDATE", f"DOPUNA (kompletno) {sku} {req.invoice}@{target_loc}")

    # Auriramo current_index samo ako radimo na trenutnoj lokaciji
    if not req.location and action != "dopuna":
        _advance_if_done(sess)
    
    save_sessions_debounced()  #  uvamo posle svake izmene
    
    cur2 = sess["ordered_locations"][sess["current_index"]] if sess["current_index"] < len(sess["ordered_locations"]) else None

    return {"ok": True, "done": cur2 is None, "current_location": cur2, "progress": _session_progress(sess), "wave": _build_wave_response(sess, include_route_legs=False)}
# ==================== NOVA RUTA ZA KOORDINATE ====================
@app.get("/warehouse/coordinates")
def get_coordinates():
    """Vraa koordinate svih lokacija u magacinu"""
    coords = []
    for loc, (row, col) in WAREHOUSE.loc_to_block_rc.items():
        if loc in WAREHOUSE.loc_coords:
            x, y = WAREHOUSE.loc_coords[loc]
            coords.append({
                "location": loc,
                "x": float(x),
                "y": float(y),
                "row": int(row),
                "col": int(col)
            })
    return coords


@app.get("/wave/__debug")
def wave_debug():
    return {
        "file": __file__,
        "warehouse_locations": len(WAREHOUSE.loc_to_block_rc),
        "active_sessions": len(WAVE_SESSIONS),
    }



































