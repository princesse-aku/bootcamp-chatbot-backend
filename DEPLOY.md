# Déploiement Study Buddy

## Backend (Render)

1. Créer un Web Service depuis le dépôt `bootcamp-chatbot-backend`.
2. Runtime : Python 3.
3. Build command :

```bash
pip install uv && uv sync && uv run alembic upgrade head
```

4. Start command :

```bash
uv run uvicorn main:app --host 0.0.0.0 --port $PORT
```

5. Variables d'environnement :
   - `RODIUMAI_API_KEY` (secret)
   - `ALLOWED_MODELS`
   - `DEFAULT_MODEL`
   - `CORS_ORIGINS` = URL du frontend déployé (et éventuellement `http://localhost:5173`)
   - `DATABASE_URL` : pour un vrai déploiement, préférer PostgreSQL Render ; sinon SQLite éphémère sur le disque du service.

## Frontend (Vercel)

1. Importer le dépôt `bootcamp-chatbot-frontend`.
2. Framework : Vite.
3. Variable `VITE_API_URL` = URL publique du backend (sans slash final), ex. `https://study-buddy-api.onrender.com`.
4. Build : `npm run build` ; output : `dist`.

Le client utilise `VITE_API_URL` si défini, sinon le proxy local `/api`.
