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
   (voir la section "HÉBERGEMENT" en bas).

=== EXÉCUTION AUTOMATIQUE 1X/JOUR ===
GitHub Actions (gratuit, pas de serveur à gérer) — voir
.github/workflows/daily_news.yml fourni à côté de ce script.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
from mistralai.client import Mistral

# Flux RSS crypto publics, aucune clé requise pour les lire
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# Certains flux (Cointelegraph, Decrypt) répondent 403 à l'identifiant par défaut de feedparser
# depuis les serveurs GitHub : on se présente comme un navigateur.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Limite de débit Mistral (erreur 429) : pause entre deux articles, puis attentes croissantes avant de réessayer.
PAUSE_ENTRE_ARTICLES = 3  # secondes
ATTENTE_429 = [10, 30, 60]  # secondes
ECHECS_CONSECUTIFS_MAX = 3  # on arrête le run si Mistral échoue sur autant d'articles d'affilée

MAX_ARTICLES_PER_FEED = 5
MAX_TOTAL_ARTICLES = 10

# Cascade de modèles Mistral par ordre de préférence, même logique que sur Intrigues :
# on retombe sur un modèle plus petit si le premier choix est indisponible/quota atteint.
MODELES_PAR_PRIORITE = ["mistral-large-2512", "mistral-medium-2505", "mistral-small-2506"]

LANGUES = ["fr", "en", "es", "de", "pt-BR", "hi"]

SYSTEM_PROMPT = """Tu résumes des articles d'actualité crypto, factuel et neutre, en 1-2 phrases
maximum par langue. Pas d'opinion, pas de conseil financier, pas de sensationnalisme.
Pas de markdown, pas de lien, pas de mention de la source (elle est affichée séparément).
Réponds UNIQUEMENT avec un objet JSON valide ayant exactement ces clés, chacune contenant le même
résumé dans la langue correspondante : "fr" (français), "en" (anglais), "es" (espagnol),
"de" (allemand), "pt-BR" (portugais du Brésil), "hi" (hindi, écriture devanagari).
Si l'article n'est pas vraiment lié à la crypto/Bitcoin, réponds exactement SKIP (sans JSON)."""


def fetch_raw_articles():
    """Récupère les articles bruts depuis les flux RSS, sans les résumer encore."""
    articles = []
    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url, agent=USER_AGENT)
        source_name = parsed.feed.get("title", feed_url)
        print(f"Flux {feed_url} : HTTP {parsed.get('status', '?')}, {len(parsed.entries)} entrées"
              + (f" (erreur : {parsed.bozo_exception})" if parsed.get("bozo") else ""))
        for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
            articles.append({
                "title": entry.get("title", "").strip(),
                "raw_summary": entry.get("summary", "")[:500],
                "url": entry.get("link", ""),
                "source": source_name,
                "published": entry.get("published", ""),
            })
    return articles[:MAX_TOTAL_ARTICLES]


def summarize_article(client: Mistral, article: dict) -> dict | None:
    """Résume un article dans les 6 langues via Mistral (un seul appel), avec cascade de modèles en cas d'échec."""
    prompt = f"Titre : {article['title']}\n\nContenu : {article['raw_summary']}"

    last_error = None
    for model in MODELES_PAR_PRIORITE:
        for attempt in range(len(ATTENTE_429) + 1):
            try:
                response = client.chat.complete(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=900,
                    response_format={"type": "json_object"},
                )
                text = response.choices[0].message.content.strip()
                if text.strip('"') == "SKIP" or not text:
                    return None
                data = json.loads(text)
                if isinstance(data, dict) and str(data.get("fr", "")).strip().upper() == "SKIP":
                    return None
                out = {}
                for lang in LANGUES:
                    v = data.get(lang)
                    if not isinstance(v, str) or not v.strip():
                        raise ValueError(f"langue manquante : {lang}")
                    # Filet de sécurité : lien markdown ou mention "Source :" malgré la consigne.
                    v = re.sub(r'\n*Source\s*:.*$', '', v, flags=re.IGNORECASE | re.DOTALL).strip()
                    out[lang] = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', v)
                return out
            except Exception as e:
                last_error = e
                if "429" in str(e) and attempt < len(ATTENTE_429):
                    # Quota atteint : on patiente puis on réessaie le même modèle.
                    wait = ATTENTE_429[attempt]
                    print(f"  {model} : limite de débit (429), nouvel essai dans {wait} s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                break  # autre erreur, ou essais épuisés : on passe au modèle suivant de la cascade

    print(f"Tous les modèles ont échoué pour '{article['title']}': {last_error}", file=sys.stderr)
    return None


def main():
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        print("ERREUR : variable d'environnement MISTRAL_API_KEY manquante.", file=sys.stderr)
        sys.exit(1)

    client = Mistral(api_key=api_key)
    raw_articles = fetch_raw_articles()
    print(f"{len(raw_articles)} articles récupérés depuis les flux RSS.")
    if not raw_articles:
        # Ne rien publier plutôt qu'un fichier vide : l'ancien news.json reste en ligne et le run apparaît en échec.
        print("ERREUR : aucun article récupéré (flux bloqués ou indisponibles).", file=sys.stderr)
        sys.exit(1)

    items = []
    echecs = 0
    for i, article in enumerate(raw_articles):
        if i:
            time.sleep(PAUSE_ENTRE_ARTICLES)
        summary = summarize_article(client, article)
        if summary is None:
            echecs += 1
            if echecs >= ECHECS_CONSECUTIFS_MAX and not items:
                print("Mistral échoue sur les premiers articles : arrêt du run.", file=sys.stderr)
                break
            continue
        echecs = 0
        items.append({
            "title": article["title"],
            "summary": summary["fr"],     # français (compat. anciennes versions de l'app)
            "summaries": summary,         # fr, en, es, de, pt-BR, hi : l'app choisit selon sa langue
            "source": article["source"],
            "url": article["url"],
            "publishedAt": article["published"],
        })

    print(f"{len(items)} articles résumés sur {len(raw_articles)}.")
    if not items:
        print("ERREUR : aucun résumé produit (clé Mistral invalide ou modèles indisponibles ?).", file=sys.stderr)
        sys.exit(1)

    digest = {
        "generatedAt": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "items": items,
    }

    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    print(f"news.json généré avec {len(items)} articles.")


if __name__ == "__main__":
    main()
