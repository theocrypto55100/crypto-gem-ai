import requests

TOKEN = "8131296930:AAFouT2VXQ1d9zy9n10bYRJimrXAIkVUf5Y"
CHAT_ID = "7164386695"

msg = "🚀 Test réussi ! Ton bot CryptoGem envoie bien des alertes."
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg})
r.raise_for_status()
print("Message envoyé ✅")
