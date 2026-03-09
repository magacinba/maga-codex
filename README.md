# MAGACIN ROUTING - Local Start

## 1) Backend (FastAPI)

Open terminal in this folder:
`C:\Users\ThinkCentre Win10\Desktop\CODEX\MAGACIN RUTING\magacin-routing`

Create virtual environment:
```powershell
python -m venv .venv
```
If `python` is not available, use:
```powershell
py -3 -m venv .venv
```

Activate environment:
```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:
```powershell
pip install -r requirements.txt
```

Run backend:
```powershell
uvicorn wave_app:app --host 127.0.0.1 --port 8000 --reload
```

API test in browser:
- [http://127.0.0.1:8000/wave/__debug](http://127.0.0.1:8000/wave/__debug)
- [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 2) Frontend (HTML)

Recommended (served over HTTP):
```powershell
python -m http.server 5500
```
If `python` is not available:
```powershell
py -3 -m http.server 5500
```

Then open:
- [http://127.0.0.1:5500/wave_index.html](http://127.0.0.1:5500/wave_index.html)

Alternative: open `wave_index.html` directly (double-click). In that case frontend automatically uses `http://127.0.0.1:8000`.

## 3) Production entrypoint

Procfile is already set to:
```txt
web: uvicorn wave_app:app --host 0.0.0.0 --port $PORT
```
