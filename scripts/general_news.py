import base64
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup


DISCORD_WEBHOOK = os.environ["GENERAL_NEWS_WEBHOOK"]

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "xtb_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-xtb-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; XtbMonitor/1.0)"}

XTB_URL = "https://www.xtb.com/pl/analizy-rynkowe/kategorie/aktualnosci-rynkowe"

# Tylko linki do artykułów w obrębie "wiadomosci-rynkowe" (czyli treść, nie
# nawigacja/stopka).
ARTICLE_PATTERN = re.compile(
    r"^https://www\.xtb\.com/pl/analizy-rynkowe/wiadomosci-rynkowe/[a-z0-9\-]+/?$",
    re.IGNORECASE,
)

TIME_PATTERN = re.compile(r"·\s*(\d{2}:\d{2})")

# Nazwy kategorii, jakie XTB doczepia do tytułu na liście — używane tylko
# do odcięcia ich z tytułu, żeby wiadomość na Discordzie była czytelna.
KNOWN_CATEGORIES = [
    "Aktualności Rynkowe",
    "Sygnał Transakcyjny",
    "Analiza Techniczna",
    "Wiadomości Ekonomiczne",
    "Market Alert",
    "Kryptowaluty",
    "Surowce",
    "Indeksy",
    "Forex",
    "Krypto",
    "Akcje",
    "ETF",
]
CATEGORY_SPLIT_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in KNOWN_CATEGORIES) + r")\b"
)

MAX_IDS = 500
MAX_SAVE_RETRIES = 5


def load_remote_state():
    response = requests.get(
        GH_API_URL,
        headers=GH_HEADERS,
        params={"ref": GH_BRANCH},
        timeout=20,
    )

    if response.status_code == 404:
        return {"seen_ids": []}, None

    response.raise_for_status()
    payload = response.json()

    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content), payload["sha"]


def save_remote_state(state, sha):
    body = {
        "message": "Update XTB news state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def persist_seen_ids(new_ids):
    if not new_ids:
        print("Brak nowych ID do zapisania – pomijam zapis stanu.")
        return

    last_error = None

    for attempt in range(1, MAX_SAVE_RETRIES + 1):
        try:
            state, sha = load_remote_state()
            bucket = state.setdefault("seen_ids", [])

            for article_id in new_ids:
                if article_id not in bucket:
                    bucket.append(article_id)

            if len(bucket) > MAX_IDS:
                del bucket[: len(bucket) - MAX_IDS]

            response = save_remote_state(state, sha)

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


def clean_title(raw_text, time_text):
    title = raw_text

    if time_text:
        title = title.split(time_text)[0]

    match = CATEGORY_SPLIT_PATTERN.search(title)

    if match:
        title = title[: match.start()]

    title = title.replace("·", "").strip(" ,-")

    return title or raw_text.strip()


def get_xtb_articles():
    response = requests.get(XTB_URL, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    articles = []
    seen_links = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        if href.startswith("/"):
            href = "https://www.xtb.com" + href

        if not ARTICLE_PATTERN.match(href):
            continue

        if href in seen_links:
            continue

        seen_links.add(href)

        full_text = a_tag.get_text(" ", strip=True)
        time_match = TIME_PATTERN.search(full_text)
        time_text = time_match.group(1) if time_match else ""

        title = clean_title(full_text, time_match.group(0) if time_match else "")

        if not title:
            continue

        articles.append(
            {
                "id": href,
                "title": title,
                "time": time_text,
                "link": href,
            }
        )

    return articles


def send_to_discord(article):
    embed = {
        "title": f"📊 {article['title']}",
        "url": article["link"],
        "description": (
            f"🌐 **Źródło:** XTB – Aktualności Rynkowe\n"
            + (f"🕒 **Godzina:** {article['time']}\n" if article["time"] else "")
            + f"\n🔗 [Otwórz oryginał]({article['link']})"
        ),
        "color": 0xE30613,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()
    already_sent = set(known_state.get("seen_ids", []))

    print("Sprawdzam XTB – Aktualności Rynkowe...")

    try:
        articles = get_xtb_articles()
    except Exception as error:
        print(f"Błąd pobierania strony XTB: {error}")
        return

    print(f"Znaleziono pozycji: {len(articles)}")

    new_ids = []

    for article in reversed(articles):
        if article["id"] in already_sent:
            continue

        send_to_discord(article)
        time.sleep(1)

        new_ids.append(article["id"])

    print("Zapisuję stan...")
    persist_seen_ids(new_ids)

    print(f"Nowych informacji wysłanych: {len(new_ids)}")


if __name__ == "__main__":
    main()
