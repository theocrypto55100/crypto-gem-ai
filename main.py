import os
import time
import json
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

# ------------------------------------------------------------
# CONFIG GLOBALE
# ------------------------------------------------------------
DEX_URL = "https://api.dexscreener.com/latest/dex/search"

# on élargit pour choper des petits projets
MIN_LIQ = 8_000          # avant 50 000
MIN_VOL24 = 5_000        # avant 25 000

# on évite les "projets" qui sont juste des stables
BAN_BASE = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE"}

# fichier où on garde l’historique des alertes envoyées
HISTORY_FILE = Path("history_alerts.json")

# notif Telegram (prend le TOKEN et le CHAT_ID dans les secrets GitHub)
from notify import send


# ------------------------------------------------------------
# OUTILS
# ------------------------------------------------------------
def http_get(url, params=None, retries=3, timeout=20):
    """GET robuste pour Dexscreener"""
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
    """Accès sécurisé dans un gros dict JSON."""
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


# ------------------------------------------------------------
# PONDÉRATION PAR CHAÎNE
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
# SCORING PRINCIPAL (0 → 100)
# ------------------------------------------------------------
def score_pair(p):
    liq = float(safe(p, ["liquidity", "usd"], 0) or 0)
    vol24 = float(safe(p, ["volume", "h24"], 0) or 0)
    tx_b = int(safe(p, ["txns", "m5", "buys"], 0) or 0)
    tx_s = int(safe(p, ["txns", "m5", "sells"], 0) or 0)
    tx5 = tx_b + tx_s

    s = 0.0

    # Liquidité
    if liq >= 8_000: s += 10
    if liq >= 15_000: s += 10
    if liq >= 50_000: s += 10
    if liq >= 100_000: s += 5

    # Volume 24h
    if vol24 >= 5_000: s += 10
    if vol24 >= 25_000: s += 10
    if vol24 >= 100_000: s += 10
    if vol24 >= 500_000: s += 5

    # Activité court terme
    if tx5 >= 10: s += 5
    if tx5 >= 25: s += 5
    if tx5 >= 75: s += 5

    # Pénalités si ça dump fort
    ch1 = float(safe(p, ["priceChange", "h1"], 0) or 0)
    ch6 = float(safe(p, ["priceChange", "h6"], 0) or 0)
    if ch1 <= -20: s -= 5
    if ch6 <= -35: s -= 5

    # Bonus selon la chaîne
    s *= chain_weight(p.get("chainId", ""))

    # bornes
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
# FILTRE SUR LES NOMS NULS / SUSPECTS
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
# SAUVEGARDE D’UNE ALERTE POUR LE SUIVI
# ------------------------------------------------------------
def save_alert_row(row: dict):
    """On garde toutes les alertes envoyées pour les suivre plus tard."""
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
# SCAN PRINCIPAL
# ------------------------------------------------------------
def run_once():
    print("🔎 Récupération des nouvelles paires de tokens…")

    # on commence par USDT
    raw = http_get(DEX_URL, params={"q": "USDT"}, retries=3, timeout=20)
    pairs = raw.get("pairs", []) or []

    print(f"📦 Paires reçues : {len(pairs)}")

    kept = []
    for p in pairs:
        # on évite les stables en base
        base_sym = (safe(p, ["baseToken", "symbol"], "") or "").upper()
        if base_sym in BAN_BASE:
            continue

        s, liq, vol24, tx5 = score_pair(p)

        # gros filtres de base
        if liq < MIN_LIQ or vol24 < MIN_VOL24:
            continue

        kept.append(build_row(p, s, liq, vol24, tx5))

    if not kept:
        print("⚠️ Aucun candidat après filtres.")
        return 0, []

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

    print("\n🏆 Top projets :")
    print(df.head(10).to_string(index=False))

    return len(df), df


# ------------------------------------------------------------
# ENVOI D’ALERTE SI VRAI CANDIDAT
# ------------------------------------------------------------
def alert_if_needed(df, threshold=55.0, min_liq=6_000):
    # GitHub Actions nous envoie parfois une liste → on la transforme
    if isinstance(df, (list, tuple)):
        df = pd.DataFrame(df)

    if df is None or (hasattr(df, "empty") and df.empty) or len(df) == 0:
        print("⚠️ Aucun candidat après filtres — sortie normale.")
        return

    # garde les projets pas trop minus
    df = df[df["Liquidité_USD"] >= min_liq]

    # garde ceux qui ont un score correct
    df = df[df["Score"] >= threshold]

    # trie
    df = df.sort_values(["Score", "Liquidité_USD", "Volume24h_USD"], ascending=False)

    if df.empty:
        print("ℹ️ Aucune alerte envoyée (seuil non atteint).")
        return

    # on prend le meilleur
    top = df.head(1).iloc[0]

    # filtre nom chelou
    if is_suspicious_name(top.get("Pair", "")):
        print("❗ Projet ignoré : nom suspect")
        return

    msg = (
        "🔥 Nouveau petit projet détecté\n"
        f"Pair : {top['Pair']} – {top['Chain']}\n"
        f"Liq: ${int(top['Liquidité_USD'])} | V24h: ${int(top['Volume24h_USD'])}\n"
        f"Score: {top['Score']}/100\n"
        f"{top['URL']}"
    ).replace(",", " ")

    send(msg)
    save_alert_row(top)
    print("✅ Alerte envoyée & loggée.")


# ------------------------------------------------------------
# POINT D’ENTRÉE
# ------------------------------------------------------------
if __name__ == "__main__":
    n, df = run_once()
    alert_if_needed(df, threshold=55.0, min_liq=6_000)
