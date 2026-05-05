# 📡 Bot Telegram — Veille Claude Code

> Bot autonome qui agrège chaque matin à **8h00 heure de Paris** les actualités de l'écosystème **Claude Code** (Anthropic) depuis 10+ sources, applique un classement IA, et publie un digest éditorialisé sur **Telegram** et **Discord**.
>
> 100 % gratuit — tourne sur GitHub Actions (0 €/mois). Aucun serveur à maintenir.

---

## ✨ Aperçu

Chaque matin, le bot :
1. Récupère ~100–300 items en parallèle depuis 10 sources (officielles, communautaires, agrégateurs)
2. Normalise, déduplique (URL exacte + titre similaire ≥ 85 % + cache inter-runs 7 jours)
3. Filtre les hors-sujet et les contenus SEO recyclés
4. Classe via **Gemini Flash** : pertinence, importance (`critical` / `notable` / `minor`), sentiment, résumé en une phrase
5. Génère une **synthèse éditoriale** de 2-3 phrases sur les tendances du jour
6. Envoie le digest sur **Telegram** + **Discord**, avec alertes **breaking news** séparées pour les items critiques

```
📡 Veille Claude Code — 5 mai 2026

💡 Anthropic publie une mise à jour majeure de Claude Code avec...

🚨 Officiel
• 🔥 Claude Code Release v2.0 : sub-agents et hooks natifs
  → https://github.com/anthropics/claude-code/releases/...

💬 Communauté
• [HN 423pts] Why I switched from Cursor to Claude Code
  → https://news.ycombinator.com/...

📊 10 sources analysées · 187 items collectés · 5 retenus
```

---

## 🏗️ Architecture

### Pipeline

```
┌─────────┐   ┌───────────┐   ┌───────────────────────┐   ┌─────────────┐
│ collect │──▶│ normalize │──▶│ resolve_google_news   │──▶│ deduplicate │
└─────────┘   └───────────┘   └───────────────────────┘   └─────────────┘
                                                                  │
                                                                  ▼
┌──────────────┐   ┌───────────┐   ┌──────────────────┐   ┌──────────────┐
│ send         │◀──│ format    │◀──│ summarize (LLM)  │◀──│ score_filter │
│ Telegram +   │   │ (TG / DC) │   │ + synthèse       │   │              │
│ Discord      │   │           │   │ + breaking news  │   │              │
└──────────────┘   └───────────┘   └──────────────────┘   └──────────────┘
```

| Étape | Rôle |
|---|---|
| `collect()` | 12 fetchs HTTP en parallèle (`ThreadPoolExecutor`, max 12 workers) |
| `normalize()` | Convertit en `NewsItem` (dataclass), parse les dates en ISO 8601 timezone-aware |
| `resolve_google_news_urls()` | Suit les redirections Google News → URLs finales (10 workers parallèles, max 30 items) |
| `deduplicate()` | URL normalisée (UTM strippés) + `SequenceMatcher` ≥ 0.85 sur titres + cache Gist |
| `score_and_filter()` | Filtre relevance + SEO noise + fenêtre 24 h ; score 0-100 (cf. `compute_score`) |
| `summarize()` | Gemini Flash : classification enrichie + synthèse 2-3 phrases en français |
| `format_*()` | Formatage Markdown (Telegram) ou Markdown bold (Discord), 3500/1900 caractères max |
| `send_*()` | Envoi avec fallback plain-text si parse Markdown échoue (Telegram), non-bloquant (Discord) |

### Sources collectées (10 services, 12 fetchers)

| Source | Type | Méthode | Fenêtre |
|---|---|---|---|
| **Anthropic Blog** | officiel | RSS (3 endpoints) → fallback Scrapling CSS → fallback regex | n/a |
| **Anthropic Changelog** | officiel | Scrapling `h2`/`h3` → fallback regex | n/a |
| **GitHub Releases** (`anthropics/claude-code`) | officiel | API REST publique | 48 h |
| **GitHub Issues** (`anthropics/claude-code`) | communauté | API REST `/issues?since=...` (PR filtrés) | 48 h |
| **GitHub Trending** | communauté | Scrapling `article.Box-row` (filtré sur `claude` / `anthropic`) | jour |
| **Hacker News** | communauté | Algolia `search_by_date` × 4 queries | 24 h |
| **Reddit** | communauté | OAuth `client_credentials` → fallback public JSON | 24 h |
| **Dev.to** | communauté | API publique (tags `claudecode`, `anthropic` + recherche) | 48 h |
| **Stack Overflow** | communauté | Stack Exchange API 2.3 (300 req/jour sans clé) | 48 h |
| **Lobste.rs** | communauté | `search.json` | 48 h |
| **Medium** | communauté | RSS par tag (`claude-code`, `anthropic`, `claude-ai`) | 48 h |
| **Google News** | aggregator | RSS × 2 queries + résolution redirections | n/a |

> **Scrapling** est utilisé pour le scraping résilient (CSS selectors avec auto-retry sur changements DOM). Si le module n'est pas dispo, fallback automatique sur regex.

### Modèle de données

```python
@dataclass
class NewsItem:
    title: str
    url: str
    source_name: str
    source_type: str        # "official" | "community" | "aggregator"
    published_at: Optional[str]
    snippet: str
    query_used: str
    score: int = 0          # 0-100, calculé par compute_score()
    category: str = ""      # rempli par Gemini
    one_line_summary: str = ""
    # Phase 3 — enrichissement IA
    importance: str = ""    # "critical" | "notable" | "minor"
    sentiment: str = ""     # "positive" | "neutral" | "negative" | "mixed"
    why_relevant: str = ""
    raw_score: int = 0      # HN points, Reddit score, Dev.to reactions, etc.
```

### Scoring (0-100)

| Critère | Bonus |
|---|---|
| Source officielle | +40 |
| Source communauté | +20 |
| `"claude code"` dans le titre | +20 |
| Mots-clés (`release`, `update`, `launch`, `changelog`, `new feature`) | +15 |
| Publié dans les 24 h | +5 |
| `raw_score` > 100 (HN/Reddit/Dev.to) | +10 |
| `raw_score` > 20 | +5 |
| **IA — `importance: critical`** | +30 |
| **IA — `importance: notable`** | +10 |
| **IA — `sentiment: negative`** (bugs, controverses) | +5 |

Seuil minimum pour publication : **`MIN_SCORE = 30`**. Les items officiels passent toujours le filtre relevance.

### Enrichissement IA (Gemini Flash)

Le LLM reçoit jusqu'à 15 items et renvoie un JSON structuré :

```json
{
  "items": [{
    "index": 0,
    "dominated": false,
    "category": "official | tutorial | discussion | noise",
    "importance": "critical | notable | minor",
    "one_line_summary": "...",
    "why_relevant": "...",
    "sentiment": "positive | neutral | negative | mixed"
  }]
}
```

- Items classés `noise` ou `dominated` sont retirés.
- Items `critical` déclenchent une **alerte breaking news** envoyée séparément en tête du flux Telegram/Discord.
- Une **synthèse éditoriale** (2-3 phrases en français, ton direct, sans bullshit marketing) est générée pour le top 5.
- **Fallback** : si Gemini est indisponible (timeout, JSON invalide, quota), le pipeline continue avec le scoring Python uniquement. Stratégies de récupération JSON multiples (regex sur items complets) en cas de réponse tronquée.

### Cache inter-runs (GitHub Gist)

- Cache stocké dans un Gist privé (`cache.json`) : `{"seen": {hash_url: iso_timestamp}}`
- Hash : `sha256(normalized_url)[:16]`
- TTL : **7 jours** (purge automatique au chargement)
- Évite les rediffusions d'un jour sur l'autre

### Schedule (GitHub Actions)

```yaml
on:
  schedule:
    - cron: "0 6 * * *"   # 06h UTC = 8h Paris (CEST) ou 7h Paris (CET)
  workflow_dispatch:        # déclenchement manuel
```

**Time guard double protection** :
1. Workflow YAML : skip si l'heure Paris > 11h (protection contre les retards GitHub Actions)
2. Script Python : skip si heure Paris hors fenêtre 7-10h (sauf `workflow_dispatch` manuel ou `--dry-run`)

### Robustesse

- **Toutes les sources sont indépendantes** : si une source plante, les autres continuent (`try/except` par tâche).
- **Fallbacks en cascade** : RSS → Scrapling → regex (Anthropic Blog/Changelog).
- **Telegram** : retry en plain text si le parse Markdown échoue (ex: `_` dans une URL).
- **Discord** : non-bloquant, log d'erreur si webhook KO.
- **Notification d'échec** : un step `Notify on failure` envoie un message Telegram/Discord si le job échoue.

---

## 📦 Arborescence

```
.
├── main.py                           # Pipeline complet (1 fichier, ~1500 lignes)
├── requirements.txt                  # requests, scrapling
├── README.md
├── SETUP_V2.md                       # Guide setup secrets optionnels (cache, Reddit OAuth)
└── .github/
    └── workflows/
        └── daily_veille.yml          # Cron 6h UTC + time guard + notify on failure
```

---

## 🚀 Setup pas à pas

### 1. Fork ou clone le repo

```bash
git clone https://github.com/<your-user>/<this-repo>.git
cd <this-repo>
```

### 2. Créer le bot Telegram (BotFather)

1. Ouvrir Telegram → chercher `@BotFather`
2. Envoyer `/newbot`, choisir un nom et un username (ex : `claude_veille_bot`)
3. Copier le **token** fourni (format `123456789:ABCdef...`)

### 3. Récupérer ton Chat ID

**Option A — `@userinfobot`** : envoyer `/start`, il affiche ton Chat ID.

**Option B — API** : démarrer une conversation avec ton bot, puis ouvrir
`https://api.telegram.org/bot<TOKEN>/getUpdates` — le champ `chat.id` est ton ID.

> Pour un **groupe** : ajoute le bot, envoie un message, puis récupère le Chat ID (commence par `-`).

### 4. Clé API Gemini (recommandé)

1. [Google AI Studio](https://aistudio.google.com/app/apikey) → créer une clé gratuite
2. Free tier (~1500 req/jour) largement suffisant pour 1 appel/jour

> Sans cette clé, le bot fonctionne en mode dégradé (scoring Python only, pas de classification IA, pas de synthèse, pas de breaking news).

### 5. Webhook Discord (optionnel)

Dans ton serveur Discord : **Paramètres du salon → Intégrations → Webhooks → Nouveau webhook** → copier l'URL.

### 6. GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Requis | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Ton ID Telegram |
| `LLM_API_KEY` | recommandé | Clé Gemini Flash |
| `DISCORD_WEBHOOK_URL` | optionnel | URL webhook Discord |
| `REDDIT_CLIENT_ID` | optionnel | Évite les 403 Reddit (cf. `SETUP_V2.md`) |
| `REDDIT_CLIENT_SECRET` | optionnel | idem |
| `CACHE_GIST_ID` | optionnel | Cache inter-runs (cf. `SETUP_V2.md`) |
| `CACHE_GITHUB_TOKEN` | optionnel | PAT scope `gist` |

### 7. Activer GitHub Actions

Push le code → le workflow se déclenche automatiquement à 8h00 heure de Paris (été comme hiver, time guard adapte).

---

## 🧪 Test local (`--dry-run`)

```bash
pip install -r requirements.txt
python main.py --dry-run
```

Le flag `--dry-run` exécute tout le pipeline mais affiche le message au lieu de l'envoyer. Combinable avec les variables d'env :

```bash
LLM_API_KEY=ton_secret python main.py --dry-run
```

---

## 🔬 Lancement manuel

GitHub → onglet **Actions → Veille Claude Code → Run workflow**.
Les déclenchements manuels (`workflow_dispatch`) **contournent le check d'heure Paris** (toujours exécutés).

---

## ⚠️ Limites connues

| Limite | Impact | Statut |
|---|---|---|
| **Anthropic Blog (SPA)** | Le site charge en JS, le scraping renvoie 0 items | Limitation structurelle — GitHub Releases compense |
| **Scraping Changelog** | Si Anthropic change son HTML, fallback regex prend le relais | Surveillé via logs |
| **Reddit IPs datacenter** | GitHub Actions bloqué par défaut | Résolu via Reddit OAuth (cf. `SETUP_V2.md`) |
| **Gemini Flash** | Free tier restreint dans certaines régions | Fallback scoring Python automatique |
| **Cache sans Gist** | Articles peuvent être re-signalés J+1 | Optionnel — cf. `SETUP_V2.md` |
| **Discord 2000 char** | Messages tronqués si très long | Limite stricte à 1900 caractères |

---

## 📈 Roadmap

| # | Fonctionnalité | Statut |
|---|---|---|
| 1 | Cache inter-runs (Gist privé, TTL 7 j) | ✅ |
| 2 | Source GitHub Releases | ✅ |
| 3 | Résolution URLs Google News (parallèle) | ✅ |
| 4 | Time guard adaptatif été/hiver | ✅ |
| 5 | Reddit OAuth (`client_credentials`) | ✅ |
| 6 | 6 sources additionnelles (Dev.to, Stack Overflow, Lobsters, Medium, GH Trending, GH Issues) | ✅ |
| 7 | Scrapling pour scraping résilient | ✅ |
| 8 | Enrichissement IA (importance, sentiment, why_relevant) | ✅ |
| 9 | Synthèse éditoriale Gemini | ✅ |
| 10 | Breaking news alerts | ✅ |
| 11 | Multi-canal Discord | ✅ |
| 12 | Multi-canal Slack / email | 📋 à venir |
| 13 | Mode opt-in par utilisateur (preferences) | 📋 à venir |
| 14 | Dashboard Grafana / metrics export | 💡 idée |

---

## 🛠️ Stack

| Composant | Choix | Raison |
|---|---|---|
| Langage | **Python 3.12** | Standard GitHub Actions, riche écosystème HTTP/scraping |
| HTTP | `requests` | Standard de facto, fiable |
| Scraping résilient | **Scrapling** ≥ 0.3 | Auto-adaptation aux changements DOM, fallback regex |
| Concurrence | `ThreadPoolExecutor` (12 workers) | I/O-bound, pas besoin d'`asyncio` |
| LLM | **Gemini Flash** (`gemini-flash-latest`) | Free tier généreux, JSON mode |
| Hébergement | **GitHub Actions** | 0 €/mois, ~30 s/run, retry intégré |
| Cache | **GitHub Gist** | Storage gratuit, JSON, accès via PAT |
| Logs | `logging` standard, niveau `INFO` | Lisibles dans l'onglet Actions |

---

## 🧱 Pour les contributeurs

### Ajouter une nouvelle source

1. Écrire `fetch_<source>(query: str = "claude code") -> list[dict]` qui renvoie des dicts au format `NewsItem` (cf. existants).
2. L'ajouter dans `tasks` dans `collect()` :
   ```python
   ("ma_source", fetch_ma_source, ["claude code"]),
   ```
3. Mettre à jour le compteur `sources_count` (officiel / communauté / aggregator).
4. Tester en `--dry-run`.

### Modifier le scoring

Tout est dans `compute_score()` (main.py:961). Bonus / pénalités sont des entiers, score capé à 100.

### Étendre le LLM

- **Classification** : modifier le `prompt` dans `call_gemini()` et adapter le mapping dans `summarize()`.
- **Synthèse** : `call_gemini_synthesis()` est le point unique.

---

## 📝 Licence

MIT — utilise, fork, partage. Une mention au repo originel est appréciée.
