import os
import time
import json
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
DEX_URL = "https://api.dexscreener.com/latest/dex/search"

# on veut choper des petits projets → on abaisse les seuils
MIN_LIQ = 8_000          # liquidité mini
MIN_VOL24 = 5_000        # volume mini

# on ne traite pas les “projets” qui sont juste des stables
BAN_BASE = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE"}

# fichier d'historique des alertes envoyées
HISTORY_FILE = Path("history_alerts.json")

# notif Telegram (TOKEN + CHAT_ID dans les secrets GitHub)
from notify import send


# ------------------------------------------------------------
# OUTILS
# ------------------------------------------------------------
def http_get(url, params=None, retries=3, timeout=20):
    """GET robuste vers Dexscreener."""
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
    """Accès sécurisé dans les dicts profonds."""
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
# SCORING (0 → 100)
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

    # Activité court terme (plus il y a de tx en 5 min, plus c'est chaud)
    if tx5 >= 10: s += 5
    if tx5 >= 25: s += 5
    if tx5 >= 75: s += 5

    # Petites pénalités si ça dump déjà
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
# FILTRE SUR LES NOMS NULS
# ------------------------------------------------------------
def is_suspicious_name(name: str) -> bool:
    if not name:
        return True
    name = name.lower()
    bad_words = [
        "test", "scam", "rug", "honeypot",
        "airdrop", "free", "reward",
        "pump", "elon", "pepepepe",
        "shit", "fake"
    ]
    return any(w in name for w in bad_words)


# ------------------------------------------------------------
# SAUVEGARDE D’UNE ALERTE
# ------------------------------------------------------------
def save_alert_row(alert: dict):
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
# SCAN MULTI-SOURCES
# ------------------------------------------------------------
def run_once():
    print("🔎 Scan multi-sources Dexscreener…")

    queries = ["USDT", "USDC", "SOL", "BNB"]
    all_pairs = []

    for q in queries:
        raw = http_get(DEX_URL, params={"q": q}, retries=3, timeout=20)
        pairs = raw.get("pairs", []) or []
        print(f"📦 {q} → {len(pairs)} paires reçues")
        all_pairs.extend(pairs)

    print(f"📦 Total brut : {len(all_pairs)} paires (avant filtres)")

    kept = []
    seen = set()

    for p in all_pairs:
        base_sym = (safe(p, ["baseToken", "symbol"], "") or "").upper()
        quote_sym = (safe(p, ["quoteToken", "symbol"], "") or "").upper()
        key = f"{base_sym}/{quote_sym}"

        # éviter les doublons exacts
        if key in seen:
            continue
        seen.add(key)

        # éviter les stables en base
        if base_sym in BAN_BASE:
            continue

        s, liq, vol24, tx5 = score_pair(p)

        # filtres qualité de base
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

    ts = datetime.utcnow().strftime("%Y-%m-%d_%Hh%MmUTC")
    os.makedirs("history", exist_ok=True)
    df.to_csv("top_projets.csv", index=False)
    df.to_csv(f"history/top_projets_{ts}.csv", index=False)

    print(f"💾 {len(df)} projets sauvegardés (snapshot {ts})")
    print("\n🏆 Top projets :")
    print(df.head(10).to_string(index=False))

    return len(df), df


# ------------------------------------------------------------
# ALERTE TELEGRAM AVEC ANTI-DOUBLON
# ------------------------------------------------------------
def alert_if_needed(df, threshold=55.0, min_liq=6_000):
    # normalisation
    if isinstance(df, (list, tuple)):
        df = pd.DataFrame(df)

    if df is None or (hasattr(df, "empty") and df.empty) or len(df) == 0:
        print("⚠️ Aucun candidat après filtres — sortie normale.")
        return

    # filtre final
    df = df[df["Liquidité_USD"] >= min_liq]
    df = df[df["Score"] >= threshold]

    if df.empty:
        print("ℹ️ Aucune alerte envoyée (seuil non atteint).")
        return

    df = df.sort_values(["Score", "Liquidité_USD", "Volume24h_USD"], ascending=False)
    top = df.head(1).iloc[0]

    # anti-nom louche
    if is_suspicious_name(top.get("Pair", "")):
        print("❗ Projet ignoré : nom suspect")
        return

    # anti-doublon : pas 2x la même paire dans la journée
    already = []
    if HISTORY_FILE.exists():
        try:
            already = json.loads(HISTORY_FILE.read_text())
        except Exception:
            already = []

    pair_name = top["Pair"]
    today = datetime.utcnow().date().isoformat()
    for a in already:
        if a.get("pair") == pair_name and a.get("detected_at", "").startswith(today):
            print(f"ℹ️ Alerte déjà envoyée aujourd'hui pour {pair_name}, pas de doublon.")
            return

    # message Telegram
    msg = (
        "🔥 Nouveau petit projet détecté\n"
        f"{top['Pair']} – {top['Chain']}\n"
        f"Liq: ${int(top['Liquidité_USD'])} | V24h: ${int(top['Volume24h_USD'])}\n"
        f"Score: {top['Score']}/100\n"
        f"{top['URL']}"
    ).replace(",", " ")

    send(msg)

    alert = {
        "pair": top["Pair"],
        "chain": top["Chain"],
        "score": float(top["Score"]),
        "liq_usd": float(top["Liquidité_USD"]),
        "vol24h": float(top["Volume24h_USD"]),
        "url": top["URL"],
        "detected_at": datetime.utcnow().isoformat(),
        "status": "pending"
    }
    HISTORY_FILE.write_text(json.dumps(already + [alert], indent=2))

    print("✅ Alerte envoyée & loggée.")


# ------------------------------------------------------------
# POINT D’ENTRÉE
# ------------------------------------------------------------
if __name__ == "__main__":
    n, df = run_once()
    alert_if_needed(df, threshold=55.0, min_liq=6_000)
