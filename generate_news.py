"""
generate_news.py — Script à exécuter 1x/jour (cron/GitHub Actions), PAS dans l'app.

Récupère des flux RSS crypto publics, résume chaque article via l'API Mistral,
et écrit un fichier news.json prêt à être publié à une URL statique que l'app
Halvo va simplement lire (voir NewsRepository.kt côté app).

Coût maîtrisé : un seul run par jour, donc un nombre fixe d'appels API — peu importe
si l'app a 10 ou 10 000 utilisateurs, ce script ne tourne qu'une fois.

=== CE QU'IL TE FAUT ===
1. Une clé API Mistral (https://console.mistral.ai) -> variable d'environnement
   MISTRAL_API_KEY (ne JAMAIS la mettre en dur dans ce fichier ni la committer).
2. pip install mistralai feedparser requests --break-system-packages
3. Un endroit où publier le news.json généré : GitHub Pages sur ce même repo
   (voir la section "HÉBERGeMENT" en bas).

=== EXÉCUTION AUTOMATIQUE 1X/JOUR ===
GitHub Actions (gratuit, pas de serveur à gérer) — voir
.github/workflows/daily_news.yml fourni à côté de ce script.
"""

import json
import os
import sys
from datetime import datetime, timezone

import feedparser
from mistralai import Mistral

# Flux RSS crypto publics, aucune clé requise pour les lire
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

MAX_ARTICLES_PER_FEED = 5
MAX_TOTAL_ARTICLES = 10

# Cascade de modèles Mistral par ordre de préférence, même logique que sur Intrigues :
# on retombe sur un modèle plus petit si le premier choix est indisponible/quota atteint.
MODELES_PAR_PRIORITE = ["mistral-large-2512", "mistral-medium-2505", "mistral-small-2506"]

SYSTEM_PROMPT = """Tu résumes des articles d'actualité crypto en français, en 1-2 phrases
maximum, factuel et neutre. Pas d'opinion, pas de conseil financier, pas de sensationnalisme.
Si l'article n'est pas vraiment lié à la crypto/Bitcoin, réponds exactement "SKIP"."""


def fetch_raw_articles():
    """Récupère les articles bruts depuis les flux RSS, sans les résumer encore."""
    articles = []
    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_name = parsed.feed.get("title", feed_url)
        for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
            articles.append({
                "title": entry.get("title", "").strip(),
                "raw_summary": entry.get("summary", "")[:500],
                "url": entry.get("link", ""),
                "source": source_name,
                "published": entry.get("published", ""),
            })
    return articles[:MAX_TOTAL_ARTICLES]


def summarize_article(client: Mistral, article: dict) -> str | None:
    """Résume un article via Mistral, avec cascade de modèles en cas d'échec."""
    prompt = f"Titre : {article['title']}\n\nContenu : {article['raw_summary']}"

    last_error = None
    for model in MODELES_PAR_PRIORITE:
        try:
            response = client.chat.complete(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=150,
            )
            text = response.choices[0].message.content.strip()
            if text == "SKIP" or not text:
                return None
            return text
        except Exception as e:
            last_error = e
            continue  # tente le modèle suivant de la cascade

    print(f"Tous les modèles ont échoué pour '{article['title']}': {last_error}", file=sys.stderr)
    return None


def main():
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        print("ERREUR : variable d'environnement MISTRAL_API_KEY manquante.", file=sys.stderr)
        sys.exit(1)

    client = Mistral(api_key=api_key)
    raw_articles = fetch_raw_articles()

    items = []
    for article in raw_articles:
        summary = summarize_article(client, article)
        if summary is None:
            continue
        items.append({
            "title": article["title"],
            "summary": summary,
            "source": article["source"],
            "url": article["url"],
            "publishedAt": article["published"],
        })

    digest = {
        "generatedAt": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "items": items,
    }

    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    print(f"news.json généré avec {len(items)} articles.")


if __name__ == "__main__":
    main()
