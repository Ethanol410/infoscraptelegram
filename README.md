# Bot Telegram — Veille Claude Code

Bot qui envoie chaque matin un résumé des nouveautés sur **Claude Code** d'Anthropic, directement dans Telegram. Fonctionne entièrement sur GitHub Actions (0 €/mois).

## Arborescence

```
.
├── main.py                          # Script principal (pipeline complet)
├── requirements.txt                 # Dépendances Python (requests uniquement)
├── .github/
│   └── workflows/
│       └── daily_veille.yml         # Workflow GitHub Actions (cron 8h Paris)
└── README.md
```

## Pipeline

```
collect() → normalize() → deduplicate() → score_and_filter() → summarize() → send_telegram()
```

| Étape | Rôle |
|---|---|
| `collect` | Récupère les items bruts depuis 5 sources |
| `normalize` | Convertit tout en `NewsItem` avec dates ISO 8601 |
| `deduplicate` | Supprime les doublons (URL exacte + titre similaire ≥ 85%) |
| `score_and_filter` | Filtre les hors-sujet, attribue un score 0–100 |
| `summarize` | Appel Gemini Flash optionnel pour éliminer le bruit |
| `send_telegram` | Envoie le message formaté |

**Sources collectées :**
- Blog Anthropic (RSS)
- Anthropic Changelog (scraping léger)
- Hacker News (API Algolia)
- Reddit (JSON public)
- Google News (RSS)

**LLM utilisé :** Gemini 1.5 Flash — free tier (jusqu'à ~1 500 req/jour), suffisant pour 1 appel/jour. Si l'API est indisponible, le bot envoie quand même un résumé basé sur le scoring Python.

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

| Secret | Valeur |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token obtenu via BotFather |
| `TELEGRAM_CHAT_ID` | Votre Chat ID Telegram |
| `LLM_API_KEY` | Clé API Gemini (optionnel) |

### 5. Activer GitHub Actions

Pousser le code sur votre repo. Le workflow se déclenchera automatiquement à **7h00 UTC (8h Paris en été)**.

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

Utile pour tester sans attendre le cron du lendemain.

---

## Limites connues du MVP

| Limite | Impact | Contournement |
|---|---|---|
| **Pas de mémoire inter-runs** | Un article peut être signalé plusieurs jours de suite | Acceptable pour le MVP ; V2 : cache Redis ou fichier de state |
| **Scraping Changelog fragile** | Si Anthropic change son HTML, les entrées ne seront plus extraites | Surveiller le log ; V2 : utiliser le vrai RSS s'il existe |
| **Timezone fixe à l'heure d'été** | En hiver, le cron envoie à 8h UTC = 9h Paris | Ajuster le cron manuellement en octobre/mars |
| **Gemini Flash** | Le free tier peut être restreint selon la région ou la politique Google | Fallback automatique sur scoring Python |
| **Reddit rate-limiting** | Reddit bloque parfois les robots non authentifiés | Le bot continue sans Reddit ; V2 : OAuth app Reddit |
| **Google News URLs** | Les URLs sont des redirections Google, pas les URLs finales | Acceptable pour le MVP |

---

## Évolutions V2 (classées par impact/effort)

| # | Amélioration | Impact | Effort |
|---|---|---|---|
| 1 | **Cache inter-runs** (fichier JSON dans une GitHub Release ou gist) | Élimine les rediffusions | Moyen |
| 2 | **Source GitHub** (releases/tags `anthropics/claude-code`) | Alertes officielles instantanées | Faible |
| 3 | **Résolution des URLs Google News** | Liens directs dans le message | Faible |
| 4 | **Cron adaptatif heure d'été/hiver** | Heure d'envoi précise toute l'année | Faible |
| 5 | **Reddit OAuth** | Évite les blocages, accès aux scores | Moyen |
| 6 | **Résumé LLM enrichi** (synthèse narrative plutôt que liste) | Message plus lisible | Moyen |
| 7 | **Multi-canal** (Discord, Slack, email) | Touche plus de bénéficiaires | Élevé |
