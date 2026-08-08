import os
import json
import requests
import feedparser
from bs4 import BeautifulSoup

SYNEKTIK_URL = "https://synektik.com.pl/centrum-inwestora/raporty-biezace/"
BANKIER_RSS = "https://www.bankier.pl/rss/gielda"

DISCORD_WEBHOOK = os.environ["SYNEKTIK_WEBHOOK"]
STATE_FILE = "synektik_state.json"

KEYWORDS = [
    "synektik",
    "synektik s.a.",
    "synektik sa",
    "snt"
]


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "synektik": [],
            "bankier": []
        }

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def is_about_synektik(title, description=""):
    text = f"{title} {description}".lower()

    return any(keyword in text for keyword in KEYWORDS)


def get_synektik_reports():
    response = requests.get(
        SYNEKTIK_URL,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    table = soup.find("table")

    if not table:
        raise RuntimeError("Nie znaleziono tabeli raportów Synektik.")

    reports = []

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")

        if len(cells) < 4:
            continue

        report = cells[0].get_text(" ", strip=True)
        title = cells[1].get_text(" ", strip=True)
        date = cells[2].get_text(" ", strip=True)

        link_tag = cells[3].find("a")

        if not link_tag:
            continue

        link = link_tag.get("href")

        if link and not link.startswith("http"):
            link = "https://synektik.com.pl" + link

        reports.append({
            "id": link,
            "title": title,
            "date": date,
            "source": "Synektik S.A.",
            "link": link
        })

    return reports


def get_bankier_articles():
    feed = feedparser.parse(BANKIER_RSS)

    articles = []

    for entry in feed.entries:

        title = entry.get("title", "")
        description = entry.get("summary", "")
        link = entry.get("link", "")

        if not is_about_synektik(title, description):
            continue

        date = (
            entry.get("published", "")
            or entry.get("updated", "")
            or "Brak daty"
        )

        articles.append({
            "id": link,
            "title": title,
            "date": date,
            "source": "Bankier.pl",
            "description": BeautifulSoup(
                description,
                "html.parser"
            ).get_text(" ", strip=True),
            "link": link
        })

    return articles


def send_to_discord(article):
    description = article.get("description", "")

    if len(description) > 500:
        description = description[:500].rsplit(" ", 1)[0] + "..."

    embed = {
        "title": f"📢 {article['title']}",
        "url": article["link"],
        "description": (
            f"📅 **Data:** {article['date']}\n"
            f"🌐 **Źródło:** {article['source']}\n\n"
            f"📝 {description}"
        ),
        "color": 0x2ECC71
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json={"embeds": [embed]},
        timeout=20
    )

    response.raise_for_status()


def process_source(articles, source_name, state):
    sent = state.setdefault(source_name, [])

    new_articles = 0

    for article in reversed(articles):

        if not article["id"]:
            continue

        if article["id"] in sent:
            continue

        send_to_discord(article)

        sent.append(article["id"])

        # Maksymalnie 100 zapamiętanych artykułów
        if len(sent) > 100:
            sent.pop(0)

        new_articles += 1

    return new_articles


def main():
    state = load_state()

    total_new = 0

    print("Sprawdzam Synektik...")

    try:
        reports = get_synektik_reports()

        print(f"Znaleziono raportów Synektik: {len(reports)}")

        total_new += process_source(
            reports,
            "synektik",
            state
        )

    except Exception as error:
        print(f"Błąd Synektik: {error}")

    print("Sprawdzam Bankier...")

    try:
        articles = get_bankier_articles()

        print(f"Znaleziono artykułów Bankier dotyczących Synektika: {len(articles)}")

        total_new += process_source(
            articles,
            "bankier",
            state
        )

    except Exception as error:
        print(f"Błąd Bankier: {error}")

    save_state(state)

    print(f"Nowych informacji wysłanych: {total_new}")


if __name__ == "__main__":
    main()
