# Deployment

CrowdSorcerer deploys as a Docker Compose stack on Coolify (Hetzner ARM VPS).

## Domain

- **Web**: `crowdsourcerer.rebaselabs.online`
- **API**: `crowdsourcerer.rebaselabs.online/api` or `api.crowdsourcerer.rebaselabs.online`

## Services

| Service | Port | Notes |
|---------|------|-------|
| `api`   | 8100 | FastAPI task broker |
| `web`   | 4321 | Astro SSR frontend |
| `db`    | 5432 | PostgreSQL (internal only) |

## Coolify setup

1. Create new project in Coolify
2. Add Docker Compose service, point to this repo
3. Set env vars (see `.env.example`)
4. Add Traefik labels for routing (Coolify handles this automatically)

## Environment variables

Copy `.env.example` → `.env` and fill in:

- `JWT_SECRET` — `openssl rand -hex 32`
- `API_KEY_SALT` — `openssl rand -hex 32`
- `REBASEKIT_API_KEY` — from RebaseKit dashboard
- `STRIPE_SECRET_KEY` — from Stripe dashboard (optional)

## Database migrations

```bash
# On first deploy
docker compose exec api alembic upgrade head

# After schema changes
docker compose exec api alembic revision --autogenerate -m "description"
docker compose exec api alembic upgrade head
```

## Updating

```bash
git push origin main
# Coolify auto-deploys on push to main
```
