# Deploy (Render + Netlify)

## 0) Sta je vec spremno
- Backend ulaz: `wave_app.py`
- Render config: `render.yaml`
- Frontend fajl: `wave_index okok.html` (izgled nije menjan)
- Netlify config: `netlify.toml`

## 1) Deploy backend na Render
1. Pushuj projekat na GitHub.
2. U Render-u klikni `New +` -> `Blueprint`.
3. Povezi GitHub repo i potvrdi deploy.
4. Render ce procitati `render.yaml` i podici servis.
5. Posle deploy-a dobices URL tipa:
   `https://ime-servisa.onrender.com`
6. Test:
   - `https://ime-servisa.onrender.com/wave/__debug`
   - `https://ime-servisa.onrender.com/docs`

## 2) Povezi frontend sa backendom
U fajlu `wave_index okok.html` API je vec podesiv:
- Lokalno: koristi `http://127.0.0.1:8000`
- Online: koristi vrednost iz `localStorage.API_BASE_URL`, a ako ne postoji koristi placeholder.

Postavi pravi API URL u browser konzoli (jednom):
```js
localStorage.setItem("API_BASE_URL", "https://ime-servisa.onrender.com");
location.reload();
```

## 3) Deploy frontend na Netlify
1. U Netlify: `Add new site` -> `Import from Git`.
2. Izaberi isti repo.
3. Build command ostavi prazno.
4. Publish directory: `.`
5. Deploy.
6. Otvori sajt; zbog `netlify.toml` root `/` ide na `wave_index okok.html`.

## 4) Brza provera rada
- Ucitaj Excel
- Pokreni `KRECEMO`
- U Network tabu proveri da request ide na Render URL (`/wave/start`, `/wave/{id}`)

## Napomena
Ako Render uspava servis na free planu, prvi request moze biti sporiji (30-60s).
