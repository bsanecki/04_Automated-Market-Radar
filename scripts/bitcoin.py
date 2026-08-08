import base64
import json
import os
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup


DISCORD_WEBHOOK = os.environ["BITCOIN_WEBHOOK"]

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "bitcoin_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-bitcoin-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BitcoinMonitor/1.0)"}

BANKIER_BITCOIN_URL = "https://www.bankier.pl/tag/bitcoin"
BANKIER_BASE = "https://www.bankier.pl"

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_PARAMS = {
    "ids": "bitcoin",
    "vs_currencies": "usd",
    "include_24hr_change": "true",
}

# Próg ruchu ceny, powyżej którego wysyłamy alert (w %).
PRICE_MOVE_THRESHOLD = 5.0

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
        "message": "Update Bitcoin monitor state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def merge_state(state, sent_ids_by_source, price_alert_update):
    for source_name, ids in sent_ids_by_source.items():
        bucket = state.setdefault(source_name, [])

        for article_id in ids:
            if article_id not in bucket:
                bucket.append(article_id)

        if len(bucket) > MAX_IDS_PER_SOURCE:
            del bucket[: len(bucket) - MAX_IDS_PER_SOURCE]

    if price_alert_update is not None:
        state["price_alert"] = price_alert_update

    return state


def persist_state(sent_ids_by_source, price_alert_update):
    has_news = any(sent_ids_by_source.values())

    if not has_news and price_alert_update is None:
        print("Brak nowych newsów ani zmiany alertu cenowego – pomijam zapis stanu.")
        return

    last_error = None

    for attempt in range(1, MAX_SAVE_RETRIES + 1):
        try:
            state, sha = load_remote_state()
            merged = merge_state(state, sent_ids_by_source, price_alert_update)
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


def get_bankier_bitcoin_articles():
    """
    Tag 'bitcoin' na Bankier.pl. Wymagamy obecności daty przy linku,
    żeby odciąć boczny panel 'Najpopularniejsze' (niepowiązane newsy
    z całego portalu).
    """
    response = requests.get(BANKIER_BITCOIN_URL, timeout=20, headers=HTTP_HEADERS)
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
                "source": "Bankier.pl – Wiadomości (Bitcoin)",
                "description": "Wiadomość dotycząca Bitcoina (Bankier.pl).",
                "link": link,
            }
        )

    return articles


def send_news_to_discord(article):
    description = article.get("description", "")

    embed = {
        "title": f"📰 {article['title']}",
        "url": article["link"],
        "description": (
            f"📅 **Data:** {article['date']}\n"
            f"🌐 **Źródło:** {article['source']}\n\n"
            f"📝 {description}\n\n"
            f"🔗 [Otwórz oryginał]({article['link']})"
        ),
        "color": 0x3498DB,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def process_news_source(articles, source_name, known_state, sent_ids_by_source):
    already_sent = set(known_state.get(source_name, []))
    new_ids = []

    for article in reversed(articles):
        article_id = article["id"]

        if not article_id or article_id in already_sent:
            continue

        if not is_recent_enough(article):
            continue

        send_news_to_discord(article)
        time.sleep(1)

        new_ids.append(article_id)

    sent_ids_by_source[source_name] = new_ids
    return len(new_ids)


def get_bitcoin_price_data():
    """Cena BTC/USD i zmiana 24h z CoinGecko (darmowe API, bez klucza)."""
    response = requests.get(
        COINGECKO_URL, params=COINGECKO_PARAMS, timeout=20, headers=HTTP_HEADERS
    )
    response.raise_for_status()
    payload = response.json()["bitcoin"]

    return {
        "price": payload["usd"],
        "change_24h": payload["usd_24h_change"],
    }


def format_price(price):
    return f"{price:,.0f}".replace(",", " ")


def format_change(change):
    text = f"{change:+.1f}".replace(".", ",")
    return f"{text}%"


def send_price_alert(price_data, direction):
    is_up = direction == "up"
    emoji = "🚀" if is_up else "🔻"
    color = 0x2ECC71 if is_up else 0xE74C3C

    embed = {
        "title": f"{emoji} BITCOIN — duży ruch",
        "description": (
            f"💰 **BTC:** ${format_price(price_data['price'])}\n"
            f"📊 **Zmiana 24h:** {format_change(price_data['change_24h'])}\n\n"
            f"🌐 Źródło danych: CoinGecko"
        ),
        "color": color,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def check_price_alert(known_state):
    """
    Wysyła alert gdy zmiana 24h przekroczy próg w górę lub w dół.
    Nie powtarza tego samego alertu co godzinę, dopóki BTC nie wróci
    poniżej progu (wtedy kierunek resetuje się do None).
    """
    try:
        price_data = get_bitcoin_price_data()
    except Exception as error:
        print(f"  Błąd pobierania ceny BTC: {error}")
        return None

    change = price_data["change_24h"]
    print(f"  BTC: ${format_price(price_data['price'])}, zmiana 24h: {change:.2f}%")

    if change >= PRICE_MOVE_THRESHOLD:
        new_direction = "up"
    elif change <= -PRICE_MOVE_THRESHOLD:
        new_direction = "down"
    else:
        new_direction = None

    previous_direction = (known_state.get("price_alert") or {}).get("direction")

    if new_direction is not None and new_direction != previous_direction:
        send_price_alert(price_data, new_direction)
        print(f"  Wysłano alert cenowy: {new_direction}")
    elif new_direction is not None:
        print("  Alert cenowy już wysłany wcześniej dla tego kierunku – pomijam.")
    else:
        print("  Zmiana poniżej progu – brak alertu.")

    return {
        "direction": new_direction,
        "last_change_pct": change,
        "last_price": price_data["price"],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()

    sent_ids_by_source = {}
    total_new = 0

    print("Sprawdzam Bankier.pl (tag: bitcoin)...")
    try:
        articles = get_bankier_bitcoin_articles()
        print(f"  Znaleziono pozycji: {len(articles)}")
        total_new += process_news_source(
            articles, "bankier_bitcoin", known_state, sent_ids_by_source
        )
    except Exception as error:
        print(f"  Błąd źródła 'bankier_bitcoin': {error}")

    print("Sprawdzam cenę BTC (CoinGecko)...")
    price_alert_update = check_price_alert(known_state)

    print("Zapisuję stan...")
    persist_state(sent_ids_by_source, price_alert_update)

    print(f"Nowych newsów wysłanych: {total_new}")


if __name__ == "__main__":
    main()
