import os
import time
import json
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

# ------------------------------------------------------------
# Config globale
# ------------------------------------------------------------
DEX_URL = "https://api.dexscreener.com/latest/dex/search"
MIN_LIQ = 50_000        # liquidité mini pour considérer
MIN_VOL24 = 25_000      # volume 24h mini
BAN_BASE = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE"}  # éviter stables comme "projet"
HISTORY_FILE = Path("history_alerts.json")

# import de l'env GitHub (TOKEN & CHAT_ID) dans notify.py
from notify import send


# ------------------------------------------------------------
# Utils HTTP
# ------------------------------------------------------------
def http_get(url, params=None, retries=3, timeout=20):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                print(f"❌ GET failed after {retries} tries: {e}")
                return {}
            time.sleep(1.5 * (i + 1))


def safe(d, path, default=None):
    """Accès dict sécurisé: safe(d, ["a","b"], 0)."""
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


# ------------------------------------------------------------
# Pondération par chain (à affiner plus tard)
# ------------------------------------------------------------
CHAIN_WEIGHT = {
    "ethereum": 1.00,
    "solana": 1.00,
    "bsc": 0.95,
    "arbitrum": 0.95,
    "base": 0.95,
    "polygon": 0.90,
}


def chain_weight(chain_id: str) -> float:
    return CHAIN_WEIGHT.get(chain_id or "", 0.90)


# ------------------------------------------------------------
# Scoring principal
# ------------------------------------------------------------
def score_pair(p):
    liq = float(safe(p, ["liquidity", "usd"], 0) or 0)
    vol24 = float(safe(p, ["volume", "h24"], 0) or 0)
    tx_b = int(safe(p, ["txns", "m5", "buys"], 0) or 0)
    tx_s = int(safe(p, ["txns", "m5", "sells"], 0) or 0)
    tx5 = tx_b + tx_s

    s = 0.0
    # Liquidité
    if liq >= 50_000: s += 10
    if liq >= 100_000: s += 10
    if liq >= 250_000: s += 10
    if liq >= 500_000: s += 5

    # Volume 24h
    if vol24 >= 100_000: s += 10
    if vol24 >= 500_000: s += 10
    if vol24 >= 1_000_000: s += 10
    if vol24 >= 5_000_000: s += 5

    # Activité m5
    if tx5 >= 25: s += 5
    if tx5 >= 75: s += 7
    if tx5 >= 150: s += 8

    # Pénalités sur chutes violentes
    ch1 = float(safe(p, ["priceChange", "h1"], 0) or 0)
    ch6 = float(safe(p, ["priceChange", "h6"], 0) or 0)
    if ch1 <= -20: s -= 5
    if ch6 <= -35: s -= 5

    # Bonus par chain
    s *= chain_weight(p.get("chainId", ""))

    # Bornes
    s = max(0.0, min(100.0, s))
    return s, liq, vol24, tx5


def build_row(p, s, liq, vol24, tx5):
    base = safe(p, ["baseToken", "symbol"], "?")
    quote = safe(p, ["quoteToken", "symbol"], "?")
    chain = p.get("chainId", "?")
    url = p.get("url", "")
    return {
        "Pair": f"{base}/{quote}",
        "Chain": chain,
        "Score": round(s, 1),
        "Liquidité_USD": round(liq, 2),
        "Volume24h_USD": round(vol24, 2),
        "Tx_5min": tx5,
        "URL": url,
    }


# ------------------------------------------------------------
# Anti-noms chelous (peut être durci ensuite)
# ------------------------------------------------------------
def is_suspicious_name(name: str) -> bool:
    if not name:
        return True
    name = name.lower()
    bad_words = [
        "test", "scam", "rug", "honeypot", "airdrop", "free",
        "pump", "elon", "pepepepe", "shit", "fake"
    ]
    return any(w in name for w in bad_words)


# ------------------------------------------------------------
# Sauvegarde d'une alerte pour la suivre plus tard
# ------------------------------------------------------------
def save_alert_row(row: dict):
    """Enregistre une alerte envoyée pour pouvoir la suivre plus tard."""
    alert = {
        "pair": row.get("Pair"),
        "chain": row.get("Chain"),
        "score": float(row.get("Score", 0)),
        "liq_usd": float(row.get("Liquidité_USD", 0)),
        "vol24h": float(row.get("Volume24h_USD", 0)),
        "url": row.get("URL"),
        "detected_at": datetime.utcnow().isoformat(),
        "status": "pending"
    }

    data = []
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text())
        except Exception:
            data = []

    data.append(alert)
    HISTORY_FILE.write_text(json.dumps(data, indent=2))
    print("📝 Alerte enregistrée dans history_alerts.json")


# ------------------------------------------------------------
# Run principal : récupère Dexscreener, filtre, score, CSV
# ------------------------------------------------------------
def run_once():
    print("🔎 Récupération des nouvelles paires de tokens…")
    raw = http_get(DEX_URL, params={"q": "USDT"}, retries=3, timeout=20)
    pairs = raw.get("pairs", []) or []

    print(f"📦 Paires reçues : {len(pairs)}")

    kept = []
    for p in pairs:
        base_sym = (safe(p, ["baseToken", "symbol"], "") or "").upper()
        # on dégage les “USDT/USDC…” en base
        if base_sym in BAN_BASE:
            continue

        s, liq, vol24, tx5 = score_pair(p)

        if liq < MIN_LIQ or vol24 < MIN_VOL24:
            continue

        kept.append(build_row(p, s, liq, vol24, tx5))

    if not kept:
        print("⚠️ Aucun candidat après filtres.")
        return 0, []

    # DataFrame principal
    df = pd.DataFrame(kept).sort_values(
        ["Score", "Liquidité_USD", "Volume24h_USD"],
        ascending=False
    )

    # sauvegardes
    ts = datetime.utcnow().strftime("%Y-%m-%d_%Hh%MmUTC")
    os.makedirs("history", exist_ok=True)
    df.to_csv("top_projets.csv", index=False)
    df.to_csv(f"history/top_projets_{ts}.csv", index=False)
    print(f"💾 {len(df)} projets sauvegardés (snapshot {ts})")

    # affichage console (utile dans GitHub Actions)
    print("\n🏆 Top projets :")
    print(df.head(10).to_string(index=False))

    return len(df), df


# ------------------------------------------------------------
# Partie “alerte”
# ------------------------------------------------------------
def alert_if_needed(df, threshold=80.0, min_liq=15_000):
    # si on reçoit une liste → on la transforme
    if isinstance(df, (list, tuple)):
        df = pd.DataFrame(df)

    # si rien → on sort propre
    if df is None or (hasattr(df, "empty") and df.empty) or len(df) == 0:
        print("⚠️ Aucun candidat après filtres — sortie normale.")
        return

    # filtres finaux
    df = df[(df["Liquidité_USD"] >= min_liq)]
    df = df.sort_values(["Score", "Liquidité_USD", "Volume24h_USD"], ascending=False)
    df = df[df["Score"] >= threshold]

    if df.empty:
        print("ℹ️ Aucune alerte envoyée (seuil non atteint).")
        return

    top = df.head(1).iloc[0]

    # anti-nom chelou
    if is_suspicious_name(top.get("Pair", "")):
        print("❗ Projet ignoré : nom suspect")
        return

    # message Telegram
    msg = (
        "🔥 Nouveau projet détecté\n"
        f"Pair : {top['Pair']} – {top['Chain']}\n"
        f"Liq: ${int(top['Liquidité_USD'])} | V24h: ${int(top['Volume24h_USD'])}\n"
        f"Score: {top['Score']}/100\n"
        f"{top['URL']}"
    ).replace(",", " ")

    send(msg)
    save_alert_row(top)
    print("✅ Alerte envoyée & loggée.")


# ------------------------------------------------------------
# Entrée du script (pour GitHub Actions)
# ------------------------------------------------------------
if __name__ == "__main__":
    # 1. on fait tourner le scan
    n, df = run_once()
    # 2. on envoie une alerte seulement si fort candidat
    alert_if_needed(df, threshold=80.0, min_liq=15_000)

    # 3. (optionnel) petit test de liaison Telegram
    # send("🚀 Test réussi : le bot CryptoGem est bien connecté à Telegram.")
