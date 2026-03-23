# Bot Telegram — Veille Claude Code

Bot qui envoie chaque matin à **8h00 heure de Paris** un résumé des nouveautés sur **Claude Code** d'Anthropic, directement dans Telegram. Fonctionne entièrement sur GitHub Actions (0 €/mois).

## Arborescence

```
.
├── main.py                          # Script principal (pipeline complet)
├── requirements.txt                 # Dépendances Python (requests uniquement)
├── SETUP_V2.md                      # Guide de configuration des fonctionnalités V2
├── .github/
│   └── workflows/
│       └── daily_veille.yml         # Workflow GitHub Actions (cron 8h Paris, adaptatif DST)
└── README.md
```

## Pipeline

```
collect() → normalize() → resolve_google_news_urls() → deduplicate() → score_and_filter() → summarize() → send_telegram()
```

| Étape | Rôle |
|---|---|
| `collect` | Récupère les items bruts depuis 5 sources |
| `normalize` | Convertit tout en `NewsItem` avec dates ISO 8601 |
| `resolve_google_news_urls` | Résout les redirections Google News vers les URLs finales |
| `deduplicate` | Supprime les doublons (URL exacte + titre similaire ≥ 85% + cache inter-runs) |
| `score_and_filter` | Filtre les hors-sujet, attribue un score 0–100 |
| `summarize` | Appel Gemini Flash optionnel pour éliminer le bruit |
| `send_telegram` | Envoie le message formaté |

**Sources collectées :**
- Anthropic Blog (RSS avec fallback scraping)
- Anthropic Changelog (scraping léger)
- **GitHub Releases** `anthropics/claude-code` (nouveau — API publique)
- Hacker News (API Algolia)
- Reddit (JSON public — OAuth si configuré, voir `SETUP_V2.md`)
- Google News (RSS + résolution des URLs)

**LLM utilisé :** Gemini Flash — free tier (jusqu'à ~1 500 req/jour), suffisant pour 1 appel/jour. Si l'API est indisponible, le bot envoie quand même un résumé basé sur le scoring Python.

---

## Setup pas à pas

### 1. Créer le bot Telegram (BotFather)

1. Ouvrir Telegram et chercher `@BotFather`
2. Envoyer `/newbot`
3. Choisir un nom et un username (ex : `claude_veille_bot`)
4. Copier le **token** fourni (format : `123456789:ABCdef...`)

### 2. Obtenir votre Chat ID

**Option A — bot GetIDs :**
1. Chercher `@userinfobot` dans Telegram
2. Envoyer `/start` → il vous affiche votre Chat ID

**Option B — via l'API :**
1. Démarrer une conversation avec votre bot (cliquer `/start`)
2. Ouvrir dans un navigateur :
   ```
   https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates
   ```
3. Le champ `"id"` dans `"chat"` est votre Chat ID

> Pour recevoir les messages dans un **groupe** : ajoutez le bot au groupe, envoyez un message, puis récupérez le Chat ID du groupe (il commence par `-`).

### 3. Obtenir une clé API Gemini (optionnel mais recommandé)

1. Aller sur [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Créer une clé API gratuite
3. Le free tier est largement suffisant (1 appel/jour)

> Sans cette clé, le bot fonctionne en mode dégradé (scoring Python uniquement, pas de résumé LLM).

### 4. Configurer les GitHub Secrets

Dans votre repo GitHub : **Settings → Secrets and variables → Actions → New repository secret**

**Secrets requis :**

| Secret | Valeur |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token obtenu via BotFather |
| `TELEGRAM_CHAT_ID` | Votre Chat ID Telegram |
| `LLM_API_KEY` | Clé API Gemini (optionnel) |

**Secrets optionnels V2 :**

| Secret | Valeur | Fonctionnalité |
|---|---|---|
| `CACHE_GIST_ID` | ID de votre Gist privé | Cache inter-runs (évite les rediffusions) |
| `CACHE_GITHUB_TOKEN` | PAT GitHub (scope `gist`) | Cache inter-runs |
| `REDDIT_CLIENT_ID` | Client ID app Reddit | Reddit OAuth (évite les blocages 403) |
| `REDDIT_CLIENT_SECRET` | Client Secret app Reddit | Reddit OAuth |

> Voir `SETUP_V2.md` pour le guide de configuration des secrets optionnels.

### 5. Activer GitHub Actions

Pousser le code sur votre repo. Le workflow se déclenchera automatiquement à **8h00 heure de Paris**, été comme hiver (double cron adaptatif).

---

## Test local avec `--dry-run`

Le flag `--dry-run` exécute tout le pipeline sans envoyer le message Telegram.

```bash
# Installer les dépendances
pip install -r requirements.txt

# Test sans aucune variable d'environnement
python main.py --dry-run

# Test avec LLM (remplacer par votre vraie clé)
LLM_API_KEY=votre_clé python main.py --dry-run
```

Le message formaté s'affiche dans les logs à la fin.

---

## Lancement manuel du workflow

Dans GitHub : onglet **Actions → Veille Claude Code → Run workflow**

Les déclenchements manuels contournent le check d'heure Paris (toujours exécutés).

---

## Limites connues

| Limite | Impact | Statut |
|---|---|---|
| **Scraping Changelog fragile** | Si Anthropic change son HTML, les entrées disparaissent | En cours — surveiller les logs |
| **Blog Anthropic (SPA)** | Le site charge les articles en JS, le scraping retourne 0 items | Limitation structurelle ; GitHub Releases compense |
| **Reddit bloqué depuis GitHub Actions** | IPs datacenter bloquées sans OAuth | Configurable via `SETUP_V2.md` (Reddit OAuth) |
| **Gemini Flash** | Free tier peut être restreint selon la région | Fallback automatique sur scoring Python |
| **Cache sans Gist configuré** | Articles peuvent être re-signalés le lendemain | Configurable via `SETUP_V2.md` (Cache Gist) |

---

## Fonctionnalités V2 implémentées

| # | Fonctionnalité | Statut | Configuration |
|---|---|---|---|
| 1 | **Cache inter-runs** (Gist privé, TTL 7 jours) | Prêt | `SETUP_V2.md` → étape 6 |
| 2 | **Source GitHub Releases** (`anthropics/claude-code`) | Actif | Automatique |
| 3 | **Résolution URLs Google News** (parallèle, 10 workers) | Actif | Automatique |
| 4 | **Cron adaptatif heure d'été/hiver** (double cron + check Paris) | Actif | Automatique |
| 5 | **Reddit OAuth** (client_credentials, sans compte utilisateur) | Prêt | `SETUP_V2.md` → étape 5 |
| 6 | **Résumé LLM enrichi** (synthèse narrative) | À venir | — |
| 7 | **Multi-canal** (Discord, Slack, email) | À venir | — |
