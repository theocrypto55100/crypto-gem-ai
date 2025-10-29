import os, requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg: str):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Pas de TELEGRAM_TOKEN/CHAT_ID (pas d'alerte).")
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg})
    try:
        r.raise_for_status()
        print("✅ Alerte envoyée.")
    except Exception as e:
        print("❌ Erreur envoi Telegram:", e)
