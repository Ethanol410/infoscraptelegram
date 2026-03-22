# Prompt — Bot Telegram de veille "Claude Code" (MVP)

## Contexte

Tu es un Développeur Python Senior spécialisé en automatisation et intégration d'API.

Je veux créer un bot Telegram qui m'envoie chaque matin à 8h00 (heure de Paris) un résumé des nouveautés sur "Claude Code" d'Anthropic publiées dans les dernières 24 heures.

Le bot tourne entièrement sur **GitHub Actions** (runner éphémère, aucune persistence entre les runs, pas de base de données). C'est une contrainte centrale de l'architecture.

## Budget et contraintes économiques

- **Budget cible : 0 €/mois** hors GitHub Actions (qui est gratuit pour les repos publics).
- Le LLM utilisé doit avoir un tier gratuit suffisant pour ~1 appel/jour avec un prompt court (< 3000 tokens). Exemples acceptables : Gemini Flash (API gratuite), ou tout modèle avec un free tier adapté.
- Aucune dépendance à un service payant.
- Si le LLM échoue ou n'est pas disponible, le bot doit quand même envoyer un résumé basique (dégradé mais fonctionnel).

## Stack

- Python 3.12+
- GitHub Actions (cron schedule)
- Telegram Bot API (via `requests`, pas de lib tierce Telegram)
- Secrets via GitHub Secrets : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `LLM_API_KEY`

## Architecture cible

Un pipeline linéaire en 6 étapes, chacune = une fonction claire :

```
collect() → normalize() → deduplicate() → score_and_filter() → summarize() → send_telegram()
```

Chaque fonction prend en entrée la sortie de la précédente. Le `main()` orchestre le tout avec logging et gestion d'erreur à chaque étape.

## Sources — MVP uniquement

Ne code QUE ces sources pour le MVP. Elles sont choisies parce qu'elles ont des endpoints stables et parsables :

### Priorité 1 — Officiel
1. **Blog Anthropic** — RSS feed `https://www.anthropic.com/rss.xml` (ou scraping léger de la page blog si pas de RSS)
2. **Changelog / Release notes Anthropic** — page `https://docs.anthropic.com/en/docs/changelog` ou équivalent

### Priorité 2 — Communautaire
3. **Hacker News** — API Algolia : `https://hn.algolia.com/api/v1/search_by_date?query=...`
4. **Reddit** — JSON public : `https://www.reddit.com/search.json?q=...&sort=new&t=day`

### Priorité 3 — Agrégateur
5. **Google News RSS** — `https://news.google.com/rss/search?q=...&hl=fr&gl=FR&ceid=FR:fr`

**Requêtes à utiliser** (pour les sources qui acceptent un paramètre de recherche) :
- `"Claude Code"`
- `"Anthropic Claude Code"`
- `"Claude Code update"`
- `"claude code CLI"`

⚠️ Si une source échoue (timeout, 403, parsing error), le pipeline continue avec les autres. Log un warning, ne crash pas.

### Sources explicitement exclues du MVP (V2)
- GitHub (trop de bruit, nécessite un filtrage complexe)
- DuckDuckGo (pas d'API stable)
- Twitter/X (API payante)
- Scraping de pages dynamiques (fragile)

## Structure de données normalisée

Chaque item collecté doit être converti en ce format :

```python
@dataclass
class NewsItem:
    title: str
    url: str
    source_name: str          # ex: "Hacker News", "Anthropic Blog"
    source_type: str           # "official" | "community" | "aggregator"
    published_at: str | None   # ISO 8601 ou None si indisponible
    snippet: str               # max 300 caractères
    query_used: str            # la requête qui a remonté cet item
```

**Quand `published_at` est absent** : conserver l'item mais le flaguer. Ne pas l'exclure silencieusement. Mentionner "date inconnue" dans le score si nécessaire.

## Déduplication

Puisqu'il n'y a **aucune persistence entre les runs** (GitHub Actions éphémère), la déduplication se fait uniquement au sein d'un même run :

1. **URL exacte** — supprimer les doublons stricts
2. **Titre similaire** — utiliser `difflib.SequenceMatcher` avec un seuil ≥ 0.85. C'est suffisant pour le MVP.

Pas de mémoire inter-runs. C'est une limite acceptée du MVP.

## Scoring et filtrage

### Filtrage par règles Python (avant le LLM) :
- Exclure si le titre ne contient aucun des termes : `claude code`, `claude-code`, `anthropic` + `code`, `code.claude`
- Exclure si le snippet contient des marqueurs SEO évidents : `"best AI tools"`, `"top 10"`, `"vs ChatGPT"` (liste configurable)
- Exclure si `published_at` existe et est > 24h

### Scoring simple (Python) :
Attribuer un score entier 0-100 basé sur :
- Source officielle : +40
- Source communautaire reconnue (HN, Reddit) : +20
- Titre contient "Claude Code" exactement : +20
- Snippet mentionne release/update/launch/changelog : +15
- Date confirmée dans les 24h : +5

Garder uniquement les items avec score ≥ 30.

### Filtrage LLM (optionnel, si l'API est disponible) :
Envoyer les items restants (max 15) au LLM avec ce prompt interne :

```
Voici une liste d'items de veille sur "Claude Code" d'Anthropic.
Pour chaque item, réponds UNIQUEMENT en JSON :
{"items": [{"index": 0, "dominated": true/false, "category": "official|tutorial|discussion|noise", "one_line_summary": "..."}]}

Règles :
- "noise" = pas spécifiquement lié à Claude Code d'Anthropic, ou contenu recyclé/vague
- Ne jamais inventer d'information absente des métadonnées fournies
- Si tu n'es pas sûr, mets "noise"
```

Si le LLM échoue : utiliser uniquement le scoring Python. Le résumé sera moins élégant mais fonctionnel.

## Format du message Telegram

### Exemple concret — cas avec contenu :

```
📡 Veille Claude Code — 22 mars 2025

🚨 Officiel
• Claude Code supporte maintenant les MCP servers en local
  → https://docs.anthropic.com/changelog#...

💬 Communauté
• Discussion HN : retour d'expérience sur Claude Code en monorepo (142 pts)
  → https://news.ycombinator.com/item?id=...
• Thread Reddit : comparaison Claude Code vs Cursor sur un projet React
  → https://reddit.com/r/...

📊 3 sources analysées · 12 items collectés · 3 retenus
```

### Exemple concret — cas sans contenu :

```
☕ Rien de neuf sur Claude Code aujourd'hui. Bonne journée !
```

### Règles du message :
- **5 items maximum** dans le message final. Si plus de 5 items passent le filtre, garder les 5 avec le meilleur score.
- Si un item est officiel, il a toujours priorité sur un item communautaire.
- Utiliser le format Markdown Telegram (`*gras*`, `[lien](url)`).
- Inclure une ligne de stats en bas (sources analysées, items collectés, retenus).
- Max ~1500 caractères pour le message total.

## Robustesse

- Chaque appel réseau : `timeout=15` secondes, dans un `try/except`
- Si une source échoue : log warning + continuer
- Si le LLM échoue : fallback sur le scoring Python seul
- Si Telegram échoue : log error + exit code 1 (pour que GitHub Actions marque le run comme failed)
- Valider les variables d'environnement au démarrage, exit immédiat si manquantes
- Logging avec le module `logging` de Python, niveau INFO par défaut

## Mode test / dry-run

Le script doit accepter un flag `--dry-run` qui :
- Exécute tout le pipeline normalement
- Affiche le message Telegram dans les logs au lieu de l'envoyer
- Permet de tester localement sans token Telegram

## Livrables demandés

Fournis dans cet ordre :

### Phase 1 — Architecture (court)
- Schéma du pipeline en texte
- Justification du choix de LLM
- Limites connues du MVP (sois honnête)

### Phase 2 — Code
1. `main.py` — script complet, exécutable, commenté
2. `requirements.txt` — dépendances minimales
3. `.github/workflows/daily_veille.yml` — workflow GitHub Actions

### Phase 3 — Documentation
4. `README.md` contenant :
   - Setup pas à pas (BotFather, Chat ID, GitHub Secrets)
   - Test local avec `--dry-run`
   - Lancement manuel du workflow
   - Arborescence du projet

### Phase 4 — Évolutions
5. Liste de 5-7 améliorations concrètes pour la V2, classées par impact/effort

## Consignes de réponse

- Code réel et exécutable, pas de pseudo-code
- Pas de dépendances inutiles (pas de `python-telegram-bot`, pas de frameworks lourds)
- Commente le code uniquement quand c'est utile (pas de commentaires évidents)
- Si un arbitrage est nécessaire, choisis toujours la solution la plus simple et la plus fiable
- Sois honnête sur les limites plutôt que de promettre des fonctionnalités fragiles
