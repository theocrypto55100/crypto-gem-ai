import requests

print("🔎 Récupération des nouvelles paires de tokens...")

url = "https://api.dexscreener.com/latest/dex/search"
response = requests.get(url, params={"q": "USDT"})
data = response.json()

pairs = data.get("pairs", [])

print(f"Nombre de paires trouvées : {len(pairs)}")
for p in pairs[:10]:
    base = p.get("baseToken", {}).get("symbol", "?")
    quote = p.get("quoteToken", {}).get("symbol", "?")
    liq = p.get("liquidity", {}).get("usd", 0)
    print(f"{base}/{quote} — Liquidité : ${liq:,}")
# --- Ajout d’un score automatique simple ---
print("\n🏆 Classement des projets par score de potentiel :")

def calculer_score(p):
    liq = p.get("liquidity", {}).get("usd", 0) or 0
    vol = p.get("volume", {}).get("h24", 0) or 0
    txns = (p.get("txns", {}).get("m5", {}).get("buys", 0) or 0) + (p.get("txns", {}).get("m5", {}).get("sells", 0) or 0)

    score = 0
    if liq > 50000: score += 30
    if liq > 200000: score += 20
    if vol > 100000: score += 30
    if vol > 1000000: score += 20
    if txns > 50: score += 10
    if txns > 200: score += 10
    return min(score, 100)

# Calcul et tri des scores
scored = []
for p in pairs:
    s = calculer_score(p)
    base = p.get("baseToken", {}).get("symbol", "?")
    quote = p.get("quoteToken", {}).get("symbol", "?")
    scored.append((s, f"{base}/{quote}", p.get("liquidity", {}).get("usd", 0)))

# Tri décroissant
scored.sort(reverse=True)

# Affiche les 10 meilleurs
for s, name, liq in scored[:10]:
    print(f"{name:15} | Score : {s:3d}/100 | Liquidité : ${liq:,.0f}")
import pandas as pd

# --- Filtres anti-scam ---
filtered = []
for p in pairs:
    liq = p.get("liquidity", {}).get("usd", 0) or 0
    vol = p.get("volume", {}).get("h24", 0) or 0
    if liq < 50000 or vol < 25000:
        continue  # on ignore les projets trop petits
    score = calculer_score(p)
    base = p.get("baseToken", {}).get("symbol", "?")
    quote = p.get("quoteToken", {}).get("symbol", "?")
    filtered.append({
        "Pair": f"{base}/{quote}",
        "Liquidité (USD)": round(liq, 2),
        "Volume 24h (USD)": round(vol, 2),
        "Score": score
    })

# --- Sauvegarde dans un CSV ---
if filtered:
    df = pd.DataFrame(filtered)
    df = df.sort_values(by="Score", ascending=False)
    df.to_csv("top_projets.csv", index=False)
    print(f"\n💾 {len(filtered)} projets sauvegardés dans 'top_projets.csv'")
else:
    print("\n⚠️ Aucun projet ne passe les filtres pour cette exécution.")
import os, time, math, requests, pandas as pd, numpy as np
from datetime import datetime
from notify import send

DEX_URL = "https://api.dexscreener.com/latest/dex/search"

# --- utilitaires robustes ---
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

def get_pairs(query="USDT"):
    data = http_get(DEX_URL, params={"q": query}, retries=3, timeout=20)
    return data.get("pairs", []) or []

def safe(d, path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

# --- paramètres anti-scams / qualité ---
MIN_LIQ = 50_000          # liquidité mini pour considérer
MIN_VOL24 = 25_000        # volume 24h mini
BAN_BASE = {"USDT","USDC","DAI","TUSD","FDUSD","USDE"}  # éviter les stablecoins comme "base" projet
MIN_LP_LOCK_HINT = 0      # (placeholder pour plus tard)

# pondération par chaîne (ajuste si tu veux favoriser ETH/SOL)
CHAIN_WEIGHT = {
    "ethereum": 1.00,
    "solana":   1.00,
    "bsc":      0.95,
    "arbitrum": 0.95,
    "base":     0.95,
    "polygon":  0.90,
}

def chain_weight(chain_id:str):
    return CHAIN_WEIGHT.get(chain_id or "", 0.90)

# --- scoring avancé (0-100) ---
def score_pair(p):
    liq = float(safe(p, ["liquidity","usd"], 0) or 0)
    vol24 = float(safe(p, ["volume","h24"], 0) or 0)
    tx_b = int(safe(p, ["txns","m5","buys"], 0) or 0)
    tx_s = int(safe(p, ["txns","m5","sells"], 0) or 0)
    tx5 = tx_b + tx_s

    s = 0.0
    # Liquidité (stabilité d'exécution)
    s += 10 if liq >= 50_000 else 0
    s += 10 if liq >= 100_000 else 0
    s += 10 if liq >= 250_000 else 0
    s += 5  if liq >= 500_000 else 0

    # Volume 24h (traction)
    s += 10 if vol24 >= 100_000 else 0
    s += 10 if vol24 >= 500_000 else 0
    s += 10 if vol24 >= 1_000_000 else 0
    s += 5  if vol24 >= 5_000_000 else 0

    # Activité 5 min (momentum court terme)
    s += 5  if tx5 >= 25 else 0
    s += 7  if tx5 >= 75 else 0
    s += 8  if tx5 >= 150 else 0

    # Pénalités soft : dispersion extrême à la hausse/baisse récente (si dispo)
    # Ici on utilise priceChange.h1 et h6 si présents
    ch1 = float(safe(p, ["priceChange","h1"], 0) or 0)
    ch6 = float(safe(p, ["priceChange","h6"], 0) or 0)
    if ch1 <= -20: s -= 5
    if ch6 <= -35: s -= 5

    # Bonus par chaîne (préférence de qualité/profondeur)
    s *= chain_weight(p.get("chainId",""))

    # bornes
    s = max(0.0, min(100.0, s))
    return s, liq, vol24, tx5

def build_row(p, s, liq, vol24, tx5):
    base = safe(p, ["baseToken","symbol"], "?")
    quote= safe(p, ["quoteToken","symbol"], "?")
    chain= p.get("chainId", "?")
    url  = p.get("url","")
    return {
        "Pair": f"{base}/{quote}",
        "Chain": chain,
        "Score": round(s,1),
        "Liquidité_USD": round(liq,2),
        "Volume24h_USD": round(vol24,2),
        "Tx_5min": tx5,
        "URL": url
    }

def run_once():
    pairs = get_pairs("USDT")
    if not pairs:
        print("⚠️ Aucune donnée reçue de Dexscreener.")
        return 0, []

    kept = []
    for p in pairs:
        # filtre “projet” : on évite les stables en base
        base_sym = (safe(p, ["baseToken","symbol"], "") or "").upper()
        if base_sym in BAN_BASE:
            continue

        s, liq, vol24, tx5 = score_pair(p)
        # filtres qualité
        if liq < MIN_LIQ or vol24 < MIN_VOL24:
            continue

        kept.append(build_row(p, s, liq, vol24, tx5))

    if not kept:
        print("⚠️ Aucun candidat après filtres.")
        return 0, []

    df = pd.DataFrame(kept).sort_values(["Score","Liquidité_USD","Volume24h_USD"], ascending=False)
    ts = datetime.utcnow().strftime("%Y-%m-%d_%Hh%MmUTC")
    os.makedirs("history", exist_ok=True)
    df.to_csv("top_projets.csv", index=False)
    df.to_csv(f"history/top_projets_{ts}.csv", index=False)
    print(f"💾 {len(df)} projets sauvegardés (snapshot {ts})")
    return len(df), df

def alert_if_needed(df, threshold=80.0, min_liq=100_000):
    import pandas as pd

    # Normalise df au bon format
    if isinstance(df, (list, tuple)):
        df = pd.DataFrame(df)

    # Si df est vide ou None → sortie sans erreur
    if df is None or (hasattr(df, "empty") and df.empty) or len(df) == 0:
        print("⚠️ Aucun candidat après filtres — sortie normale.")
        return

    top = df[(df["Score"] >= threshold) & (df["Liquidité_USD"] >= min_liq)].copy()
    if top.empty:
        print("Aucune alerte (seuil non atteint).")
        return

    top = top.sort_values(["Score", "Liquidité_USD", "Volume24h_USD"], ascending=False).head(1).iloc[0]
    msg = (
        f"🚀 *Candidat détecté*\n"
        f"*{top['Pair']}* – *{top['Chain']}*\n"
        f"*Score:* {top['Score']} /100\n"
        f"*Liq:* ${int(top['Liquidité_USD'])} | V24h: ${int(top['Volume24h_USD'])} | TX5: {int(top['TX_5min'])}\n"
        f"{top['URL']}"
    )
    msg = msg.replace(",", " ")
    send(msg)
