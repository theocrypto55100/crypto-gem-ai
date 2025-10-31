# follow_up.py
#
# But : relire les alertes envoyées (history_alerts.json),
# re-télécharger les données Dexscreener pour ces paires,
# comparer l'état actuel avec l'état au moment de l'alerte
# et marquer chaque alerte comme "gain" / "loss" / "dead".
#
# ⚠️ Ce fichier NE remplace PAS main.py, il le complète.

import json
from pathlib import Path
from datetime import datetime, timezone
import requests

HISTORY_FILE = Path("history_alerts.json")
DEX_URL = "https://api.dexscreener.com/latest/dex/search"
# seuils de perf (à ajuster plus tard)
GAIN_PCT = 0.30   # +30% de liquidité → gain
LOSS_PCT = -0.30  # -30% de liquidité → perte

def load_history():
    """Charge le fichier des alertes, sinon renvoie une liste vide."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []

def save_history(alerts):
    """Réécrit le fichier d'historique."""
    HISTORY_FILE.write_text(json.dumps(alerts, indent=2, ensure_ascii=False))

def fetch_pair_from_dex(url: str):
    """
    Essaie de récupérer les infos actuelles de la paire.
    On passe par l'URL que Dexscreener nous donne souvent dans main.py.
    Si on n'arrive pas à la requêter, on tente un fallback par symbole.
    """
    if not url:
        return None

    # 1) tentative directe
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            # certains endpoints retournent directement les paires
            if isinstance(data, dict) and "pairs" in data:
                # on prend la première
                if data["pairs"]:
                    return data["pairs"][0]
            return data
    except Exception:
        pass

    # 2) fallback : on tente un search générique (pas toujours dispo)
    try:
        r = requests.get(DEX_URL, params={"q": "USDT"}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data
    except Exception:
        pass

    return None

def compare_liquidity(old_liq: float, new_liq: float):
    """Compare deux niveaux de liquidité et renvoie un statut + %."""
    if old_liq is None or old_liq == 0:
        return "unknown", 0.0
    diff = (new_liq - old_liq) / old_liq
    if diff >= GAIN_PCT:
        return "gain", diff
    if diff <= LOSS_PCT:
        return "loss", diff
    return "flat", diff

def follow_up():
    alerts = load_history()
    if not alerts:
        print("📭 Aucune alerte à suivre.")
        return

    print(f"📊 Suivi de {len(alerts)} alertes déjà envoyées...")

    updated = []
    for alert in alerts:
        url = alert.get("url", "")
        pair_name = alert.get("pair", "?")
        old_liq = float(alert.get("liq_usd", 0) or 0)

        current_data = fetch_pair_from_dex(url)
        if not current_data:
            # on marque comme "dead" ou "not_found"
            alert["last_check_at"] = datetime.now(timezone.utc).isoformat()
            alert["status"] = "dead"
            alert["comment"] = "Pair introuvable sur Dexscreener."
            updated.append(alert)
            print(f"🟥 {pair_name} → introuvable (dead)")
            continue

        # on essaie de lire la liquidité actuelle
        # selon la forme de la réponse (selon endpoint)
        if isinstance(current_data, dict) and "liquidity" in current_data:
            new_liq = float(current_data.get("liquidity", {}).get("usd", 0) or 0)
        elif isinstance(current_data, dict) and "pairs" in current_data:
            # on prend la 1ère
            first = current_data["pairs"][0]
            new_liq = float(first.get("liquidity", {}).get("usd", 0) or 0)
        else:
            new_liq = 0.0

        status, pct = compare_liquidity(old_liq, new_liq)
        alert["last_check_at"] = datetime.now(timezone.utc).isoformat()
        alert["last_liq_usd"] = new_liq
        alert["perf_vs_detect"] = round(pct * 100, 2)
        alert["status"] = status

        if status == "gain":
            print(f"🟩 {pair_name} → +{round(pct*100,2)}% de liq.")
        elif status == "loss":
            print(f"🟥 {pair_name} → {round(pct*100,2)}% de liq.")
        else:
            print(f"⬜ {pair_name} → stable ({round(pct*100,2)}%)")

        updated.append(alert)

    # on réécrit le fichier
    save_history(updated)
    print("💾 Historique mis à jour.")

if __name__ == "__main__":
    follow_up()
