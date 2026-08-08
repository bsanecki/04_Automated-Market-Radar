import base64
import json
import os
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup


DISCORD_WEBHOOK = os.environ["GOLD_SILVER_WEBHOOK"]

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "gold_silver_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-gold-silver-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GoldSilverMonitor/1.0)"}

BANKIER_GOLD_URL = "https://www.bankier.pl/tag/zloto"
BANKIER_SILVER_URL = "https://www.bankier.pl/tag/srebro"
BANKIER_BASE = "https://www.bankier.pl"

MAX_IDS_PER_SOURCE = 300
MAX_SAVE_RETRIES = 5

# Pokazujemy tylko newsy od 1 czerwca 2026 włącznie (bez cichego seeda
# całej historii tagu).
CUTOFF_DATE = date(2026, 6, 1)

DATE_PATTERN = re.compile(r"\d{2}\.\d{2}\.\d{4},?\s*\d{2}:\d{2}")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}")
BANKIER_ARTICLE_PATTERN = re.compile(r"/wiadomosc/.+-\d+\.html", re.IGNORECASE)


def parse_date_value(date_str):
    date_str = (date_str or "").strip()

    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y, %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    return None


def is_recent_enough(article):
    parsed = article.get("parsed_date")
    return parsed is None or parsed >= CUTOFF_DATE


def load_remote_state():
    response = requests.get(
        GH_API_URL,
        headers=GH_HEADERS,
        params={"ref": GH_BRANCH},
        timeout=20,
    )

    if response.status_code == 404:
        return {}, None

    response.raise_for_status()
    payload = response.json()

    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content), payload["sha"]


def save_remote_state(state, sha):
    body = {
        "message": "Update Gold/Silver news state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def merge_sent_ids(state, sent_ids_by_source):
    for source_name, ids in sent_ids_by_source.items():
        bucket = state.setdefault(source_name, [])

        for article_id in ids:
            if article_id not in bucket:
                bucket.append(article_id)

        if len(bucket) > MAX_IDS_PER_SOURCE:
            del bucket[: len(bucket) - MAX_IDS_PER_SOURCE]

    return state


def persist_sent_ids(sent_ids_by_source):
    if not any(sent_ids_by_source.values()):
        print("Brak nowych ID do zapisania – pomijam zapis stanu.")
        return

    last_error = None

    for attempt in range(1, MAX_SAVE_RETRIES + 1):
        try:
            state, sha = load_remote_state()
            merged = merge_sent_ids(state, sent_ids_by_source)
            response = save_remote_state(merged, sha)

            if response.status_code in (200, 201):
                print(f"Stan zapisany (próba {attempt}).")
                return

            if response.status_code in (409, 422):
                print(
                    f"Konflikt zapisu stanu (próba {attempt}/{MAX_SAVE_RETRIES}), "
                    f"pobieram świeży stan i ponawiam..."
                )
                last_error = response.text
                time.sleep(1.5 * attempt)
                continue

            response.raise_for_status()

        except requests.RequestException as error:
            last_error = str(error)
            print(f"Błąd sieci przy zapisie stanu (próba {attempt}): {error}")
            time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"Nie udało się zapisać stanu po {MAX_SAVE_RETRIES} próbach. "
        f"Ostatni błąd: {last_error}"
    )


def get_bankier_tag_articles(url, source_name, category_label):
    """
    Uniwersalny parser dla stron tagowych Bankier.pl (ten sam format co
    tag/syn2bio i tag/bitcoin). Wymagamy obecności daty przy linku, żeby
    odciąć boczny panel 'Najpopularniejsze' (niepowiązane newsy).
    """
    response = requests.get(url, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    articles = []
    seen_links = set()

    for link_tag in soup.find_all("a", href=True):
        href = link_tag["href"]

        if not BANKIER_ARTICLE_PATTERN.search(href):
            continue

        link = href if href.startswith("http") else BANKIER_BASE + href

        if link in seen_links:
            continue

        seen_links.add(link)

        full_text = link_tag.get_text(" ", strip=True)
        date_match = ISO_DATE_PATTERN.search(full_text) or DATE_PATTERN.search(
            full_text
        )

        if date_match:
            date_text = date_match.group(0)
            title = full_text[len(date_text):].strip(" :-") or full_text
        else:
            container = link_tag.find_parent(["li", "div"]) or link_tag.parent
            container_text = container.get_text(" ", strip=True) if container else ""
            date_match = ISO_DATE_PATTERN.search(
                container_text
            ) or DATE_PATTERN.search(container_text)

            if not date_match:
                continue

            date_text = date_match.group(0)
            title = full_text

        if not title:
            continue

        if len(title) > 180:
            title = title[:180].rsplit(" ", 1)[0] + "..."

        articles.append(
            {
                "id": link,
                "title": title,
                "date": date_text,
                "parsed_date": parse_date_value(date_text),
                "source": f"Bankier.pl – {source_name}",
                "category": category_label,
                "description": f"Wiadomość dotycząca kategorii {category_label} (Bankier.pl).",
                "link": link,
            }
        )

    return articles


def get_gold_articles():
    return get_bankier_tag_articles(BANKIER_GOLD_URL, "Złoto", "Gold")


def get_silver_articles():
    return get_bankier_tag_articles(BANKIER_SILVER_URL, "Srebro", "Silver")


def send_to_discord(article):
    category = article["category"]
    color = 0xF1C40F if category == "Gold" else 0xBDC3C7
    category_emoji = "🥇" if category == "Gold" else "🥈"

    embed = {
        "title": f"📰 {article['title']}",
        "url": article["link"],
        "description": (
            f"{category_emoji} **Kategoria:** {category}\n"
            f"📅 **Data:** {article['date']}\n"
            f"🌐 **Źródło:** {article['source']}\n\n"
            f"📝 {article['description']}\n\n"
            f"🔗 [Otwórz oryginał]({article['link']})"
        ),
        "color": color,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def process_source(articles, source_name, known_state, sent_ids_by_source):
    already_sent = set(known_state.get(source_name, []))
    new_ids = []

    for article in reversed(articles):
        article_id = article["id"]

        if not article_id or article_id in already_sent:
            continue

        if not is_recent_enough(article):
            continue

        send_to_discord(article)
        time.sleep(1)

        new_ids.append(article_id)

    sent_ids_by_source[source_name] = new_ids
    return len(new_ids)


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()

    sent_ids_by_source = {}
    total_new = 0

    sources = [
        ("bankier_gold", "Sprawdzam Bankier.pl (tag: zloto)...", get_gold_articles),
        ("bankier_silver", "Sprawdzam Bankier.pl (tag: srebro)...", get_silver_articles),
    ]

    for source_name, log_message, fetch_fn in sources:
        print(log_message)

        try:
            articles = fetch_fn()
            print(f"  Znaleziono pozycji: {len(articles)}")
            total_new += process_source(
                articles, source_name, known_state, sent_ids_by_source
            )
        except Exception as error:
            print(f"  Błąd źródła '{source_name}': {error}")

    print("Zapisuję stan...")
    persist_sent_ids(sent_ids_by_source)

    print(f"Nowych informacji wysłanych: {total_new}")


if __name__ == "__main__":
    main()
