# Setup V2 — Actions manuelles requises

Tout ce que tu dois faire pour activer les nouvelles fonctionnalités.
Les étapes marquées ✅ sont automatiques (rien à faire).

---

## 1. Pusher le code

```bash
git add main.py .github/workflows/daily_veille.yml
git commit -m "feat: V2 — cache, GitHub releases, Reddit OAuth, URL resolution, DST cron"
git push
```

---

## 2. Cron DST ✅ (automatique)

Rien à faire. Le workflow se déclenche maintenant à 6h ET 7h UTC.
Le script détecte l'heure de Paris et skip le mauvais fire automatiquement.

---

## 3. Source GitHub Releases ✅ (automatique)

Rien à faire. L'API publique GitHub ne nécessite pas de token.

---

## 4. Résolution URLs Google News ✅ (automatique)

Rien à faire. Actif dès le prochain run.

---

## 5. Reddit OAuth — 2 étapes

### Étape A — Créer l'application Reddit

1. Aller sur **reddit.com/prefs/apps** (connecté à ton compte Reddit)
2. Scroller en bas → cliquer **"create another app"**
3. Remplir le formulaire :
   - **Name** : `claude-code-veille`
   - **Type** : choisir **"script"**
   - **Description** : (laisser vide)
   - **Redirect URI** : `http://localhost:8080`
4. Cliquer **"create app"**
5. Copier :
   - **Client ID** : la chaîne sous le nom de l'app (ex: `aBcDeFgHiJkL`)
   - **Client Secret** : le champ "secret" (ex: `XyZ123...`)

### Étape B — Ajouter les secrets GitHub

Dans ton repo GitHub : **Settings → Secrets and variables → Actions → New repository secret**

| Nom du secret | Valeur |
|---|---|
| `REDDIT_CLIENT_ID` | Client ID copié ci-dessus |
| `REDDIT_CLIENT_SECRET` | Client Secret copié ci-dessus |

---

## 6. Cache inter-runs — 3 étapes

### Étape A — Créer un Gist privé

1. Aller sur **gist.github.com**
2. Créer un nouveau Gist :
   - **Filename** : `cache.json`
   - **Content** : `{"seen": {}}`
   - Cliquer **"Create secret gist"** (pas public !)
3. Copier l'**ID du Gist** depuis l'URL :
   - URL exemple : `https://gist.github.com/Ethanol410/abc123def456`
   - ID = `abc123def456`

### Étape B — Créer un Personal Access Token (PAT)

1. Aller sur **github.com/settings/tokens**
2. Cliquer **"Generate new token" → "Generate new token (classic)"**
3. Remplir :
   - **Note** : `claude-code-veille-cache`
   - **Expiration** : `No expiration` (ou 1 an avec rappel calendrier)
   - **Scopes** : cocher uniquement **`gist`**
4. Cliquer **"Generate token"**
5. Copier le token immédiatement (il ne sera plus affiché)

### Étape C — Ajouter les secrets GitHub

| Nom du secret | Valeur |
|---|---|
| `CACHE_GIST_ID` | ID du Gist (ex: `abc123def456`) |
| `CACHE_GITHUB_TOKEN` | Token PAT copié ci-dessus |

---

## Vérification finale

### Secrets GitHub attendus (au total)

| Secret | Requis | Évolution |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ déjà configuré | MVP |
| `TELEGRAM_CHAT_ID` | ✅ déjà configuré | MVP |
| `LLM_API_KEY` | ✅ déjà configuré | MVP |
| `REDDIT_CLIENT_ID` | 🔧 à ajouter | Reddit OAuth |
| `REDDIT_CLIENT_SECRET` | 🔧 à ajouter | Reddit OAuth |
| `CACHE_GIST_ID` | 🔧 à ajouter | Cache |
| `CACHE_GITHUB_TOKEN` | 🔧 à ajouter | Cache |

### Test après setup

1. **Actions → Veille Claude Code → Run workflow** (déclencement manuel)
2. Dans les logs, vérifier :
   - `Reddit OAuth token obtained successfully` → Reddit actif
   - `Cache loaded: X URLs already seen` → Cache actif
   - `GitHub Releases: X items` → Source GitHub active
   - `Resolving X Google News URLs...` → Résolution active
3. Le lendemain, relancer et vérifier `Cache: skipped X already-seen items`
