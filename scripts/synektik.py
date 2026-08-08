import os
import json
import requests
from bs4 import BeautifulSoup

URL = "https://synektik.com.pl/centrum-inwestora/raporty-biezace/"
DISCORD_WEBHOOK = os.environ["SYNEKTIK_WEBHOOK"]
STATE_FILE = "synektik_state.json"


def get_latest_report():
    response = requests.get(
        URL,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    table = soup.find("table")
    if not table:
        raise RuntimeError("Nie znaleziono tabeli raportów.")

    row = table.find("tr")
    rows = table.find_all("tr")

    # Pomijamy nagłówek tabeli
    for row in rows[1:]:
        cells = row.find_all("td")

        if len(cells) < 4:
            continue

        report = cells[0].get_text(" ", strip=True)
        description = cells[1].get_text(" ", strip=True)
        date = cells[2].get_text(" ", strip=True)

        link_tag = cells[3].find("a")
        if not link_tag:
            continue

        link = link_tag.get("href")

        if link and not link.startswith("http"):
            link = "https://synektik.com.pl" + link

        return {
            "id": link,
            "report": report,
            "title": description,
            "date": date,
            "link": link
        }

    raise RuntimeError("Nie znaleziono żadnego raportu.")


def load_last_id():
    if not os.path.exists(STATE_FILE):
        return None

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    return data.get("last_id")


def save_last_id(report_id):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump({"last_id": report_id}, file)


def send_to_discord(report):
    embed = {
        "title": f"📢 {report['report']}",
        "description": (
            f"**{report['title']}**\n\n"
            f"📅 Data: {report['date']}\n"
            f"🌐 Źródło: Synektik S.A.\n\n"
            f"[🔗 Otwórz raport]({report['link']})"
        ),
        "color": 0x2ECC71
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json={"embeds": [embed]},
        timeout=20
    )

    response.raise_for_status()


def main():
    report = get_latest_report()
    last_id = load_last_id()

    print(f"Najnowszy raport: {report['report']}")
    print(f"Tytuł: {report['title']}")
    print(f"Data: {report['date']}")

    if report["id"] == last_id:
        print("Brak nowego raportu.")
        return

    send_to_discord(report)
    save_last_id(report["id"])

    print("Nowy raport wysłany na Discord.")


if __name__ == "__main__":
    main()
