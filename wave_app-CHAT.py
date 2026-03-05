import os
import uuid
import pandas as pd
from typing import Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from ortools.constraint_solver import pywrapcp
from ortools.constraint_solver import routing_enums_pb2

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # za razvoj
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(__file__)
EXCEL_PATH = os.path.join(BASE_DIR, "MAG_ROUTING_DATA_v2.xlsx")

# ----------------------------
# LOAD DATA
# ----------------------------

locations_df = pd.read_excel(EXCEL_PATH, sheet_name="LOCATIONS")
entrances_df = pd.read_excel(EXCEL_PATH, sheet_name="ENTRANCES")

LOCATION_COORDS = {
    row["location"]: (row["x_m"], row["y_m"])
    for _, row in locations_df.iterrows()
}

ENTRANCES = [
    (row["name"], row["x_m"], row["y_m"])
    for _, row in entrances_df.iterrows()
]

# ----------------------------
# DISTANCE FUNCTION (Manhattan)
# ----------------------------

def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])

# ----------------------------
# OR-TOOLS SOLVER
# ----------------------------

def dynamic_time_limit(n):
    if n < 30:
        return 2
    elif n < 60:
        return 5
    elif n < 100:
        return 8
    else:
        return 12

def solve_tsp(distance_matrix):
    size = len(distance_matrix)
    manager = pywrapcp.RoutingIndexManager(size, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(distance_matrix[from_node][to_node] * 100)

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.seconds = dynamic_time_limit(size)

    solution = routing.SolveWithParameters(search_parameters)

    if not solution:
        return None

    index = routing.Start(0)
    route = []

    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        route.append(node)
        index = solution.Value(routing.NextVar(index))

    return route

# ----------------------------
# MODELS
# ----------------------------

class Item(BaseModel):
    sku: str
    location: str
    invoice: str

class WaveRequest(BaseModel):
    items: List[Item]

WAVE_SESSIONS: Dict[str, dict] = {}

# ----------------------------
# START WAVE
# ----------------------------

@app.post("/wave/start")
def start_wave(req: WaveRequest):

    if not req.items:
        raise HTTPException(status_code=400, detail="No items")

    pick_locations = list({item.location for item in req.items})

    best_route = None
    best_distance = float("inf")

    for entrance_name, ex, ey in ENTRANCES:

        nodes = [(entrance_name, (ex, ey))]

        for loc in pick_locations:
            if loc not in LOCATION_COORDS:
                continue
            nodes.append((loc, LOCATION_COORDS[loc]))

        coords = [coord for _, coord in nodes]

        size = len(coords)
        matrix = [
            [manhattan(coords[i], coords[j]) for j in range(size)]
            for i in range(size)
        ]

        route_idx = solve_tsp(matrix)
        if not route_idx:
            continue

        total_dist = sum(
            matrix[route_idx[i]][route_idx[i+1]]
            for i in range(len(route_idx)-1)
        )

        if total_dist < best_distance:
            best_distance = total_dist
            best_route = [nodes[i][0] for i in route_idx]

    if not best_route:
        raise HTTPException(status_code=500, detail="Solver failed")

    session_id = str(uuid.uuid4())

    WAVE_SESSIONS[session_id] = {
        "route": best_route,
        "distance_m": round(best_distance, 2),
        "items": [item.dict() for item in req.items],
    }
    print("==== DEBUG ROUTE ====")
    print("Locations:", pick_locations)
    print("Best route:", best_route)
    print("Distance:", best_distance)
    print("=====================")

    return {
        "session_id": session_id,
        "route": best_route,
        "total_distance_meters": round(best_distance, 2)
    }