"""
Solana Wallet Intelligence
===========================
Five-tab Streamlit app — deploy free on Streamlit Community Cloud.

Tab 1 — Cohort Analyzer:     classify holders by total wallet net worth
Tab 2 — Whale Overlap:       find what tokens the big wallets currently share
Tab 3 — Recent Acquisitions: what have whales/sharks actually bought in last N days
Tab 4 — Watchlist:           scan your personal preset list of wallets for recent buys
Tab 5 — Common Holders:      find wallets that appear on both of two holder CSVs
Tab 6 — Whale Pressure:      scan top holders of a coin and score net buy/sell conviction

To add paid access gating later:
  1. In Streamlit Cloud dashboard → Secrets, add:
        ACCESS_CODES = ["code1", "code2", "code3"]
  2. Uncomment the GATING BLOCK below.
"""

import io
import time
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Solana Wallet Intel",
    page_icon="🔬",
    layout="centered",
)

st.markdown("""
<style>
    .stProgress > div > div { background-color: #89b4fa; }
    code { font-size: 0.78rem; }
</style>
""", unsafe_allow_html=True)

# ── constants ─────────────────────────────────────────────────────────────────
COHORT_BRACKETS = [
    {"name": "Whale 🐋",   "min_usd": 100_000, "max_usd": float("inf")},
    {"name": "Shark 🦈",   "min_usd": 25_000,  "max_usd": 100_000},
    {"name": "Dolphin 🐬", "min_usd": 5_000,   "max_usd": 25_000},
    {"name": "Fish 🐟",    "min_usd": 500,     "max_usd": 5_000},
    {"name": "Minnow 🦐",  "min_usd": 0,       "max_usd": 500},
]

SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
}

MAX_WALLETS = 150

# Candidate column names commonly used in Solscan / Birdeye / Dexscreener holder exports
ADDRESS_COL_CANDIDATES = [
    "Account", "Wallet Address", "Wallet", "Address", "Owner", "owner", "address", "wallet",
]

# ─────────────────────────────────────────────────────────────────────────────
# Known exchange / CEX hot wallets on Solana — excluded from Whale Pressure
# Add more as you encounter them
# ─────────────────────────────────────────────────────────────────────────────
EXCHANGE_WALLETS = {
    # Binance
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "5tzFkiKscXHK5ZXCGbGuEgkrUjDA9b6AXetFnq5SxFBP",
    # Coinbase
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
    # OKX
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5",
    # Kraken
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2",
    # Bybit
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm",
    # KuCoin
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6",
    # Gate.io
    "8i5HqznCcCPaFLXyUNtPNM1sPQSCyR7D7BQYUURNE2iV",
    # Bitfinex
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    # MEXC
    "Fc8SF1XqMqmxFrszJNAEKMbW8V6MNrDsmW5sFt2E9wfB",
    # Raydium AMM vaults (programmatic — often noise)
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    # Jupiter aggregator
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}


# ══════════════════════════════════════════════════════════════════════════════
# ▼▼▼  PASTE YOUR WALLET ADDRESSES HERE  ▼▼▼
# ══════════════════════════════════════════════════════════════════════════════
PRESET_WALLETS = [
    "5N69dUvxdiQGFaRob32oPSwLuUYTqNgHz6GoEtnrRd8S",  # suppoman
    "AQ8t7FmGaDQ4AqmNtaX2d9NqfACCHb16yKo8BavExWkV",  # suppo1
    "2UGhBWG6K9UJq5iM96t1PebJfCBuNxYgYjwNsvE9nwBw",  # Suppoman alt5
    "8qRh4cJDH9bBAgUHHuoudBMoDsNHsrjDVHsAd3PXxZ5A",  # Suppo alt4
    "8H86inoTa6PfeoCgRiuup2ZFkeR6WMQYTJWNtHcdSpQW",  # suppo?
    "Bfujsb5We6iW4JVTocFnHcCuH2NZawAaawawdsSy2G2N",  # crypto gains 3
    "ADiP2QxRegS56oP9bEbnVm3Kv9Si8d931W75DWRQiU1Z",  # crypto gains 2
    "FKjyLNBEaio8TMRT4R5KvAm6viYHYLXPogZVsV23kmzc",  # crypto zach public
    "6vPb1fgadFCnmjHHHb8wVVYXXRyv66ZVHBwS8KduVtDD",  # crypto zach confirmed side
    "CWscK3ppFqXR9auTLxkwwLf31DP6KuZ1BqKE6UpvWbTv",  # Crypto Zach tucker/cbm
    "AgbYtSqB2LEaP6BzBwsQ8eRp7bMRwqJz54DEzYdkmk5v",  # Suppo Sol 2
    "3pSbsfviHu1ERTKMpfzPNbHA75ffvx37h7uyvBr2tamd",  # Suppo Sol 3
    "F6zTiTZo9Gx3gSXzWgRot7kqWxMdsAmhYCfk5qKuXVZL",  # Kyle Doops
    "BkCQ9T6HeGZmuHRpF55dJ47TU1bdSUcf5dUVRP5jdqrN",  # Suppo New 1
    "ErSwAVyTNxbLxwERXCeHJECeehhEdZtVSSMJxsKoigon",  # suppo/cg?
    "HFozUwnbKHE4quZZcQiYShkmQ2kZvJayNvxBNQoAo7hY",  # cg?
    "Gjae1mkaRxbAgsEwSYfwvJneH6sfz1mCoAznmXV4iRCd",  # cg robots
    "8udujy8heeEYnoeN4H3pY2EAED2ZsfAn8uWVtqgAJQBR",  # antisniper
    "H9C5wWzkm72atwk7Gom3tns1hnMYt28iKmUUVMrgZgp7",  # sindoor
    "HwzHpQCGS1fJbF3YFyWVcAjR5kuZW79bDusoj4SH7C3k",  # new 2 suppo
    "DdgY42vCyJkuhFVwH4SAZBmoUKxPY7q7qyUubvdkd7RQ",  # new 3 suppo
    "J3ekaUh3CM7SNVAoyGncKg9s86JffFenLJqWNEn1cjx9",  # Follower
    "2XkjbK82TEf9azFaSiVkRKRn5x3XbMQaydUF4fahPvuG",  # suppo bully1
    "66N8cLukCYD8XTb6HxZpYKKEv7psa6rjHt3fRtj2dvxh",  # suppo bully2
    "5NDGWY5ahREkWsiUZhod5XQHZUkqFAcnebTkkDXxy1Ng",  # Rubicon
    "FSF8wftBC6yX6dRhruqaeQBMLhKqKF2rUbNq3dyBqcVP",  # CG new 1
    "72EQ7KLyLGxubJCcBrqjjRzqVuv69WgT7Riy9E9cvfhX",  # cg/suppo new 2
    "HMWoXFdicDDLpj8oWG4WGx51gaA4rses6DXWwwRa68dB",  # Suppo Bully 4
    "Y9GWjAt5XCwA4RM5usG5Ymyvziy8gRFci9aX6V252M1",   # Suppoman tracke
    "BKKJtyzpE5He7uDtqcFgaXh5UZ6tZ14ZjRVaaJQrs7ho",  # CRYPTO GAINS
    "aQdDcJ3DnZe9DNT5rk7gJqiz4NF3LrPtLMYqQgJAPaj",   # cg squirrel 2
    "WJpRJzVvMfCSv4bzZLZCBd719ufvgLCR76Wkxjr33e9",   # cg squirrel 3
    "8oETpd62mEr9bYJ8dGDRDtv4wc3DeSwybd2E23is4jJy",  # cg squirrel 4
    "DTpekwcCuPDTWm5es2W2mZax33rYnzwHWCAq6zV8KDgh",  # cg squirrel 5
    "GBZmnPzxcMnZkdHzHYnAc6b8ocT9K4ZHecJi8UZ1J1Wj",  # cg vbucks1
    "E8JkqZ4BBEGWc6SmHQX2j8kn5GYu3WjxSPRqpmBkB4uG",  # suppo ninja1
    "CsAdVm9AxsvuzMyXCEfX2VKNiFcHacg7Fxie2WCRqUhr",  # suppo ninja 2
    "7nXLN8rh2UMtzMHPqRBun6ncfK3GjvZwSkm3zLPPEXNV",  # suppo ninja 3
    "F4FMGhL6SJaZZP9fmCxiUqUz2CSABEHdj9ABrXQbTgeb",  # suppo ninja 4
    "Fj33PrX4LCwqTRpLHGccqHid1qvdeUxbwkoRTFSfUyjU",  # suppo ninja 5
    "4sYgCsFmxnZhNrfJ4xihMU9Q52Fpj3wzWPknUXkSKtHr",  # suppo mortgage1
    "DGJvKGNmGszfnYzv3WdgB6chT8NdiUZQxARsMUtQAAND",  # suppo connect
    "jTreizFAcJTzAQGjLibVqa7BJYyyXRqtBhXJGywEVEP",   # suppo mortgage 3
    "CEvuhW2QnMbdweLPkooiGZDYmBz2Akxx7o4uLZ3PEQhk",  # suppo mortgage 4
    "DYrMUyszqtKBszKdhrVR35X5b2tFKKX7y8TrGy5ZaDxc",  # suppo PORT
    "5inVmnvCxeBtBPACEkLpS2hgx6Hi9JL8G22Sfz2iZHYP",  # suppo MEGA
    "7D8jMyRHeqULXWnmNXisfqaPJLYHaYin5Y2LiZn4A9nr",  # Suppo MAJOR
    "14FfkJpAjraKnPZdZCczgSMbw1mUbr5XrJMNEu6aKzvG",  # cg? suppo?
    "46fHjStWyKonc7QrtD1kQLYavGk3iLu9k4VAF67cwwcG",  # nin1
    "2yFn8jn9PcBPqrpqKEMby9QkGvy6hpBoQ3Dw4qitJHfN",  # Suppo HFN
    "EZ1iuVDEJbNhKvp4PxnTmtrrfNFF8ELW53eSVNweFJ31",  # nin3
    "A9KWS4Cbn7xeDBdnmcVgz3u63vtcYLXYNr8DB4oXtXkw",  # nin4
    "FMhQ7KCuKoDNiKHBi3fSKLYcGTdf9iFsADoAjhHhQHCn",  # SupHCN cnct-and
    "GhDvDWzw1rZuXLqrmKvavC2XyFS76YjEsj1hyjHszvdp",  # b2
    "41bhtRrQ6QvSespWWEaTCeahguYaq4YSUofaH26Qo3KT",  # b3
    "86jTb3yxGXEYYEjboYWcTY8xwqfTfz84Sbvt8ZP4scTq",  # b5
    "AVEdK2oG88tabPNGYSbJW41zMSsjadp6ZkqnBYxXp7Rm",  # pk
    "G64VpM645W8iUMQuzq8J5rTV4McePSpVtQj7Gzw8t5Hq",  # Suppo Port CONNECTED
    "EnLr63K6KvAp34kQP42PfRU6PcLyPKYoXPFzmWzsWiEY",  # Suppo connected
    "FLRJFaxkKBtsuN5nQTKqQ7TxSByV1brzabt3Fmbvrz5w",  # spom ninja
    "mfguNrfhLiEkMUMSWmb54DT64PmT3rMvyEVFi9S7gyH",   # spom ninja 2
    "DnB1DLTeS5rqAkmFmH5rpS8sVVFSu5ircjKdc7aeeNCM",  # Spom Ninja 3
    "C6Gj8u3pXAXte3LyvAgAE7s4KKdHQXXRC4ZFMZJCtm6M",  # Suppo Giga?
    "HkS3GXY1AU97xuLBie6VWgTefw1i7nmLSr2CcmWSZsGV",  # suppo - cnct to port
    "4orogPNfVbPgyN8oKoHSiGKfnuworVFavoQGdp2YpVoP",  # CG Trash Memes
    "4L8d5uFZrkFdNd6q1Si8joXmx1U6aUkMnVUeSpbivTRC",  # CG c:memes 1
    "FDiSLdiKrgZPBiZN4QaG883UFzvQQnYUSZmsYJh2bThT",  # CG c:memes 2
    "AQmaqp5RpLXDJSRAxJKL13WiWkAgXMLwh4s6EovS7aLJ",  # CG trash - memes
    "CmJ6gGVP4q8FeFtqs5D15W4AyxUiveTqVdMavUNEgw1J",  # CG!!!!trashmeme
    "4VzMg7B2bCNqaRvrqC5aCPRYPXbu7zVxyAuyj8Kp3bJS",  # HMO Spom
    "8KF7RtTYugwLrEWtZwC6HZXyZrmAhoLzx7jkdtWuerBg",  # Suppo HMO 2
    "FCMh9rhCzF8wkMc2fe9DScyFBD39PTMyRE3Pa1zkzJHy",  # CG Lol/Seekai
    "GAMx7B9TY5jKKseZDE1hUmPSoo9d4dKWJ9g6LDoVFz2J",  # Suppo Negan 1
    "8HBhhEQH2iDW3roy5koBH2sjwAgRxKSaZwvNhvFAJadM",  # suppo giga?
    "A95p4sED3VBXJepXS7VjQsAKeHfRWecWuZePMKmT8mta",  # Maybe Suppo?
    "5rzrYVvREQjfJZ4NNb3cbsBTmFiiE2GeGs8VcW8paa6y",  # Telegram AndyB
    "6FD5FijB93BxarebSjDcNeRCZK8vf1rMtEKY9qeW1DEr",  # Telegram Group2
    "G6C1DxMSJ57CEcYtQiwTJRv4eFYbW47b615waZLB4fUZ",  # giga dev1
    "GxYTZvdrTeHBYvjE6dLQke5RtsBLX4knyvuXwbTC71bM",  # negan2
    "DJJmT3FBqjivxZd2kasgYDHxNVmsheqWhtj2d6JpVcnf",  # negan3
    "B2f9ubz5ztzeNtzqwrg9YZ68A2n6Vvpf32gzUXPF71Kq",  # negan4
    "BgzpfCwKKsDFdENcgbse8CTEHgU5eBqK7Zvb9RsYrcPL",  # telegram group5
    "HPt5nDNpf99Hi4XARXYNGKMyT9oBymgJ8f8Fqyi4hcmN",  # CG Retail Guy
    "A77ZErL8ebYLGiTrY1XyHoaHhjkxTfnSfm6ENnCbnAJf",  # CG Retail3
    "DGuZnBKdpJNVhMAaKpEv2wB7deqjtbVN8FtzdL3yHAc4",  # Telegram Droz
    "CJRLAWHoG6p7gcoY4m15G3ZFiJje7btwXkcbf7nDdE6s",  # Telegram 10xHntr
    "8urHCg3RBqWF7LQSk2rbTe4EWh3dhYPLGZ8WnEbuEviL",  # Telegram group3
    "8oJJuG5MLXBLSfwCMELzkUNrAiTPaTFqgsFDmqck3Fmf",  # Telegram Group4
    "DQ9xct3btLPaCrnSgQW7o9iS7SHzGLxTJgsZ6dqS4Bmr",  # Suppo f925 buy
    "5SHc1ymh5fEYzmHydxQCHiUxYPHSZRk2KfqNkfuokPhr",  # Suppo follower
    "9vbt8SNdWwB8cEWkyZDrpVXAQMwkuFLCTNB3R8nrXNLC",  # Win All Day?
    "FXzLRnzn9knVK8zTNGxenDHstTkVGzCR1kKDfBDaJVKV",  # CULT trash
    "4bRiZ2p4eRWpVWV8yGU588K3JSTdfzGjKTzJfSQdd2RL",  # CULT trash2
    "GKHVpw8umdML7d4NqQetcPPbZqaZ7sbzSVsw7yD2q3Y4",  # CULT trash3
    "C9Q75kpAP9NnEwsjmYhRX72Z2FLiSr3kZPxkr1E8k19m",  # CULT trash4
    "83inDF9iH167knFh54S1ZBse2EcyMo5rcEZUxCUSSVvU",  # CULT trash5
    "FMwFDEJ7wGTzGBWvscjyxJgMUpFpEEoLWiMwtHcfUo8A",  # Suppo Tits
    "9asvyWEWKcyTiG9tYAneiSbi3S2BUfJCmK9gWRaJGYwk",  # Suppo Feet1
    "HHBHjy3S1fp3f3vccB4iYUBF1Lwp7vdaPfLfY3ni8ykH",  # Suppo Feet2
    "7qKxCiZQ7ZG1Ct8yk7nDVRLghNz3jNRpw1TjRJhK7Bcn",  # CG Bread Memes
    "HBZtiaiFfQHMJGn5bWfAS3Qn3ufkBSjm2esrkY2LT8Sg",  # CG bread memes
    "6R9noJaCdnJN54HyMm7yLJyiung7xRJ1PQFsw7JrrF7i",  # TG Group - WIM
    "5C6LHDSfwM8FjVuyDhQTtzhAeWGRoxssjdRuZeAeLQsp",  # Suppo Marie?
    "A4grzCdRYWbuzLeUqTp5Hhn1GZHaGoVXqNV7ZDqQpA7W",  # telegram group5
    "89AUSZhkMaXB38P2Zobi4ozT8yBUVChyvRLUv5TdDUH8",  # CG - coinbit
    "7ZnbGnu5mt1Br41Fzu8ikwFVXuSozBv3YC1AJN3GSf5n",  # suppo connected to hfn
    "NnQzYEt2p7otFGcoKDehAbycrMdJnMXoP5x2Rp5gTNp",   # grandowge
    "FLtQBx63VVK4p4mXePKeWBN2HVRiHBtaW2KgT8ekFd4c",  # connct -AAND
    "GEphCQRSdVBq9MvLssZso198d2sTgSXxZsiiHmjPVdx4",  # guy who follows
    "5Koxy8TDLjMGzuP9qFpo7BfpuKe5WqfHDfNzHRE11NRt",  # connct to -hfn
    "6icj6RCZmL3BbSDE56bhqNcgmpyNfw2TEJJLSktvEBEh",  # sup conct to f925 buy
    "6JHaZaHHL49SDfks87xbeosMrcrBEfEockyJHZjzqK9k",  # test
    "DqAXLmRTR5RCU5UTTLXxvfiAh9KjK4DtjRcpsJDeiJYt",  # ustream gd
    "9NRrppLN1XMSqmLgN61fZtuEaagLSGSM91gcxVdjNVHM",  # ustream gd2
    "12VGCoTPz6oecXD9y2zMN3BZyqdg2d7kTAJRsxAm7UBH",  # Pump Fun
    "AmHX3tvgjsosdUXdH9J3q2R4vncMFbHgPS2a3rkZ67eS",  # Land Giga 1
    "FxkYAJLtBoCSYeRRc6hCKrGhgu2cd7MqL3aTdWCUnYbN",  # Land Giga 2
    "AR85bDQGKkKxVhdNWfLWm2HrPs5ZYbYzWmod6MwN1ECj",  # Telegram Group4
    "7NysB6n8qsUx2PAUSjLoWQFjRgCV1rpog5xPhYpo1zHc",  # Marie Creator
    "8s7aNM9nD9GXYEqeZZykK5a2S2Q7AeJ4Wxg6svxPhpGi",  # cnct to Marie
    "AaZkwhkiDStDcgrU37XAj9fpNLrD8Erz5PNkdm4k5hjy",  # test
    "6HMoJqFfifATfSqD7YY3YXA3CZxwjfCwpExGEvQ5bekY",  # alphastrike.sol
    "2bu3tNi1NfDvyG6RMGPUWYiUvgsFKdKvAiN1tXtyCUxX",  # fried
    "EqYGemqo1DeFkKoAvps8baQNqaLEHaTg1EBkXTxa431",   # Altcoin Gordon2
    "A8L7hRc3qUbA9JXb4D4NcYtECx9qzpY7KCoz6kAwqqx5",  # Altcoin Gordon1
    "AxebAp8y2WeBePpwXHbgDo7RqNFbHjtEABqemKbBZ8tc",  # cnct to negan2
    "5SvEcbKh32Yk3TpuWXRUoxKuujX8zfKVUarPXRESqtvd",  # cnct to -wiey
    "5vV7ckkxofwmz1xSRn3F18bcY62HoZzFhcgMFpD71aLu",  # bitty buyer
    "teStzXQ6CwnVkhzzSxskjFJUUzsGuzokuYCf9w5Sjxt",   # Bitty Buyer Connect
    "HtMgPgsjokdspHk6guFXHaayvrqXn288PUsK9CnfjPXM",  # TG Group 6
    "DfLKQ6j1ZniwibSysRs7yaZ5fi98MRN1nMyzNcxzo9uU",  # CG gigadad
    "EkDzy9WXV3pKYwTVKCW3zTq5hhwvyAnQMjikSXE5RFzo",  # Guy in Suppo gr
    "2j5icyy6o9NcNjxYGxUPveAX64ygyXxYN7bJmP7JwT52",  # Suppo follower
    "EA42u6qrpkWgBDACRLtQy2JAax5ZfPoYxpTNYg3vbFaS",  # CG E-T-H 2
    "GDTxj3ZirX1ejrt8zqgioh6CaKEn8siA8ZpVSH4NpeYW",  # CG - MMIP1
    "7MwSR5Y3V9tCxTAgAWfL3kwu2LziKbhkE6z4WewyXvig",  # Fresh Giga1
    "sm2TmB17kAvHb8R2qaWYNSfQYRifGKaDQ4Yn5UyXsEj",   # pk2
    "CnpPPRy87DthsPDDSAeGGuQhPEJx6j2HCRPR2fYZLuG2",  # Fresh Giga2
    "6uBKte2HfwCddSAZmS86ePzCg1zNEohYx6U95rgMS5FD",  # cnct to SuppoTG
    "9hLiEFhLFSotsbZnYt6GHttHUiX2VRM1UXH2FfuDBps2",  # CG Bitty
    "2K5iaXtq6XsUspRtYkfkVAuyVM21STdTUbZBmBJH1DKs",  # CG MMIP
    "5CvCsDJUeFoLGhhjhHZDAxnkcrMuUKAbZFQUkQ6aj24E",  # CG MMIP2
    "FQWYMzji9WLaPEiuSoEd9AFS7CStXZew3ha6NcSbRBbu",  # CG MMIP3
    "5N2DB6b7zZEqUMNhyxcys9afARn7L3tH2QUP12i1fPwa",  # CG Giga
    "B3Qz21iaybax11XgQrDtNmuG8U6QpPXB57sgtxE4Wpbh",  # Suppo honey
    "J9oCqq3nbGoNPAX4TX7US54vhPkz7ggyaktaDi85nbLs",  # CG DOX1
    "VEUPkbAd2oBcyBU5ucRda9u9sEJrRxZzkNi9ksHGJTG",   # CG Dox2
    "EjpE7E5586RcbiaPoJeXG7cLPWYFhJkyCY9SL3m49cgf",  # CG Dox3
    "EpuMRmBj5jmqnr2Zx1TDUuozMDuMyAQiD4VP6TM7miLv",  # CG Dox4
    "CKUhxNE9eWDsm5w9otjXZFVLkUcwjtGnjmyXjSTdQZR1",  # CG Dox5
    "AHUvNNWjzyvGdT4pc3dheAGK42ifUWZcKscR6YqAd9i6",  # CG Dox6
    "14Na35u5xdAywXGrLrmqAJ412V46588gLKc4rtBGNozJ",  # CG Dox7
    "CrHb8bz2x4f24FBAqr9mKM3J6iqCwqxcaLTRStefsQmV",  # CG VNTR
    "v5r7d85xnQ9nk7aMm75cD6NM29tm1jWsp2r5sznQosS",   # CG Dox8
    "915qNoGNtZGMmTBPteXinmDk2hmot921H5tQK2RWwC4x",  # CG Mudeng
    "4Z1NjgKje6Pwb5QdmdnHJENiYujmWjawzKfm872YX6PF",  # CG Dox9
    "CfkU8TohFjF7zM5QPffFGphxEfJyj2mn1KL3CvKNu7cq",  # Suppotrsh giga
    "CLdA3RBUcAAzEXXZcMYpqHFkxrZ25cyMStXJ5G3FUWW4",  # Telegram 8
    "7Aru8gbneZZDhEhnSxmexePyTUDSTvLFYpUxh3Rn2xa1",  # cg RETAIL
    "7stVAuYoj69rU4GERTpt1t5MLzAJ6bmbrihngrKXkw3t",  # Suppo Boat
    "BjTpQ7ktUFtsf7nhxjZ5ncfNJM1WTkapw7Ycupu2URrd",  # CG tremp?
    "Eb4HpAMtqVprLMtchgyWLZ93e3FE35gNXJbC7FNCWcbv",  # CG DOX cnct1
    "65QnhwsD7myZBkCgdCCPKQGE5auNYAMB3pJt7KagiNbW",  # Suppo honey3
    "HWHFvqfBGubwZttu5uKSSDEoh25nvRcVEysCZh6vLUdx",  # cgdx 1
    "CiRRp8ykLmuYjy4XijfV6YwYdSMKNs26C6Huz5UedRqw",  # cgdx 2
    "HazKSo6DK95mvdRwLmU6GiQ8N1TZwixfguyMLgYDDJGL",  # cgdx3
    "9Fa5VKhvhYC7smExnPR2i1gtQnhk2xqA8Jvw15TwFknA",  # cgdx 4
    "6MxfbUeLEZPVxwMXztrzZKdyKkvNjp1eg46gVF3TB5jq",  # cgdx 5
    "QpXtSwiVWtwL4VF69doEZqNNn4Gt1QsovLrVgBQsKZw",   # cgdx 7
    "3rbdpAbmFWVwZaWnCNGBwdUB8WppoQDTsvkUTw3NTpnh",  # cgdx 8
    "6qWUokEUNc9Tcpn43viUyt41BarU47BtRqDSHmw8Znzu",  # Suppo Potcoin
    "863eTpU4DTTqnrXQTsy1mUGzXopbH1SHLpEr45aL1Jnm",  # Suppo Kitteh
    "EnR9oKxVgNtL2DR9S9U6LtcJyBGUd2TVtYsfia3dX3S3",  # TG group Andy?
    "Bejd2UmNi29c4amjJJaKTCDoFU8Uoqgp5CyB1YYCafRD",  # Chrome Axiom
    "3QWnrGBQHpEjzYrKR2T2GEFWGAsp4N2JgEw8MfNwBi6e",  # TG group
    "8Pe2vwFAZM8HRej5C5sXDmZon5FEeQgsJGsESAojkrna",  # trust
    "2n5jXt9YwL4VDaarUVpeFzrAxhQKm8PrfJywGxaFciha",  # giga awr
    "HdW58hqd8UmYZeuwEwHXmP9ibAuazfbYbtsP5XWfa6pc",  # giga sol
    "7EBEdGXKEg4za5ZcvaNM4obYJAqiBktBoTUCWTqDYHpB",  # ustreamer
    "5TxBdkHpWERMx25w9gk7nALxsj462Ei56bUCuKNwkdQo",  # CG Stimmy
    "5U11yxEWRszr5BYCNciSvoxQ46GKB4edsTs3tNHarKSv",  # funded by suppo port1
    "3jJrMJFQ9m2b2dtKfemcgwTuAeWDjSh3UbgvGveZUvnK",  # CG money giga
    "HRmn3VsdS5kwJJF1uHJoHRiHQEXkT1jgzHKmTP7oiPFf",  # pk limit order
    "8L3giAGPuhtuZJrP5SXPc1SG1n22f1vqLNrMwt5RPwj3",  # fresh giga
    "aPvLrvGVWEU6gxPrcVHGsuoiwbY1orC7RBuGWm2HwNo",   # fresh giga2
    "9HmsRfR1WYdCbEffabkn2CfbMhnnQxTf8Uqd74vSJxUw",  # cg popdog1
    "BGR2CZTMuVzCN8nMeEekmGRGCr1WaH8rwUfgSAoXwKPj",  # suppo popdog
    "3fnFrzSRFfUtDotcYQgrZY9k6HKZXafDvW14struBi22",  # pk new phantom
    "7KRiBwhjUeLD4uG61UGYqh5CwkbH68M5TkbkgEAtkzq8",  # suppo oigon cnct
    "9v5ceZc1C3UzzWuy11wEsXuEncwZiVRPCrKjxv5vNT8o",  # suppo magic
    "EjF8xMovv4QBUKMpagTniy4G6RxaHHwzCrL5AKAKM2bK",  # suppo bridge from eth
    "7XTB95B9qXZPcCkaqs8TKaV1SkPNNPXGQEz1wAcnQjdu",  # suppo bridge
    "HpX2xQxo864Pn3N8a53yxWjCXQKV3s3QWv1w2a6meTw5",  # suppo bridge 2
    "Dm4gLzxotRj4csCXtFmRBbudAAnJPvbt3FLNJ7N1CSZh",  # Suppo Magic 3
    "DsTdm1j7vyLnzftpmyv5iAZfYcBpwagUKHUYwc2R65mB",  # Suppo Contra
    "AKxUREkAX7SMx3KfcN4DoTB8VWBaGT5SpRWo1QvaN4Aq",  # CG RI
    "ADEJ4dzXnD34J1SkwirvksZMYMTiabzNhBcJqn4td6nX",  # CG RI2
    "65Qfj1bzwZPJjGtLwcS72nXRRZcmFRcN7xstj4jFmnVg",  # RI CG?
    "9vpDUd66XScDynMys3eQmsgcXnCMZSjJrkuedXWLrAzo",  # RI CG2
    "28tMjRp8AFtQnfyTxMb8XUCXmMruz76kTC2QKmrrkYXL",  # ri cg3
    "B5oUKxsw84TdxsRczscwMrRnfv9ZMQkTzq3obsTs47Co",  # Suppo Harry 2
    "8bR9j2FikiFkThfHDYrNTG5JV165Mm71Fgkv7NRzLgNC",  # fried 2
    "wD4veK3LUfQeTa1TWR8oVU2tjmg6oLe9pwSEGtHsgmj",   # CG Rally
    "JBZcbw2YaJd7LLxjBVRxT3n1X6kjMPz1yvRLoVUCiFrx",  # PK - FRX
    "6gmW5CsGJuRYaRkLVoh8kTQCE7v4DJ9tpoeEaHZsVvYR",  # gadget goober fetchr1
    "Gk8pnC2xrKvnRQxHxv4VZ6Z4AiXPfpAhfKM88gSiRRb5",  # follower of sup
    "GdSFRvK8AKrPHkisWbTCjzjqFK2gGF919HF9qghP3kuu",  # cepryl
    "3S22MmmQPuJ6nUsKWRKyS82dGwxjf4H1cwqfMBHfsn6V",  # smartie
    "BM9CcyErJcu2mjrFvUsRRrD3snGeHDDVirJLvL6EjvMN",  # giant wallet
    "DV3NGC4mcptJVUBsQ5Am626YYtJPfZBF69XsjGenYiKv",  # CG Viral
    "41YadHSTLs3dXS6bC5kLEsX1MQjetyJqVRVLpW63upLt",  # suppo spom
    "BovY4yhUh8s6u9oiFnLQezwRTS2foevqH6KmbDiviYC9",  # cg payai
    "CYwxLpwHiJ81YGrnBbuhmLnqejUskn1XanwR6tZeEEaX",  # CG fu
    "42GnRwZyr1RsqofeEdayrCmAFbvpgyuwuKEoKEsjQbW6",  # test
    "4BhPzNRja1r7XJVD1kLFHx3z9zFiXhUhAsUCqAsN1acL",  # Suppo Print1
    "8CrJ5wyhsGbs1UZ74yBFzqtqBVjSUCi2mr8MXUmzsq91",  # Suppo Print2
    "3NV4JLiA3meQeSDQqpU6DxcsBVgPyC5rNKUA8dLefvqr",  # Suppo Print3
    "BBKGavUJoBUrarwtSPgCKwKFwuUzA9vEZCGPcuUDC7w3",  # Suppo Print4
    "AtzeX2TF1Epkg8DitmjW6Tg7A4QYmbQ2QyPY1igpayMJ",  # MAD CG
    "7Z15U9xpHeVUAe5b7dXrx3Z8Rq8qfLfN3ERNaE2zKpWh",  # CG Bored 3
    "F8GpobQGivx5P68dgXxAiPrQLb7Z9MEdP3p12p4QWHRt",  # CG Bart2
    "4FgcnDhKn3qEycuJfxvvQ5eKg6cAnHu2DfYeU6kQmgt5",  # CG bored
    "3haRTNqYsBT1U9o26XDvkRV6jxwiqbSPcqRkf4REp3Hn",  # CG bored2
    "6rcfuiUX445seYXr8yeZuMfwqbn8wTivPmoBJW1eU3C6",  # CG Pup Polywog
    "Fi2Ta5jWq5ttNRp1D3wXkeotJEA3J4nPTAVewbGubHUr",  # CG LMAO
    "3VnAKxxjn1wWv3S8wrN5enM3xksvCp2QqvofhHsMzD5f",  # CG polywog2
    "FBchxQhd3UesmJtdLqsBHK5wVTzwwCnZqffkP9HZQJwQ",  # Polywog Altszn
    "35VkqS77CDLEjgoegzbMf1MuzTtmhXSxQ4oLwePkeU6v",  # Print Altszn
    "5JgADWaAVpDib4cCM868Q2MdHfAAoUjZrzghB2YSg4C1",  # Cg Altszn
    "5oRNbpo9jSAhJsqiidUjnrF7DrnjgSKMZ9trXKHbmzpw",  # suppo for sure
]
# ══════════════════════════════════════════════════════════════════════════════
# ▲▲▲  END OF WALLET LIST  ▲▲▲
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# GATING BLOCK — uncomment when you want to sell access
# ══════════════════════════════════════════════════════════════════════════════
# def check_access():
#     valid_codes = st.secrets.get("ACCESS_CODES", [])
#     code = st.text_input("Enter access code", type="password", key="access_code")
#     if not code:
#         st.info("Enter your access code to continue. Purchase at [your-site.com](https://your-site.com).")
#         st.stop()
#     if code not in valid_codes:
#         st.error("Invalid access code.")
#         st.stop()
# check_access()
# ══════════════════════════════════════════════════════════════════════════════


# ── shared API helpers ────────────────────────────────────────────────────────
def get_assets(wallet: str, helius_url: str) -> list:
    """Fetch all fungible assets for a wallet via Helius DAS."""
    payload = {
        "jsonrpc": "2.0", "id": "wai",
        "method": "getAssetsByOwner",
        "params": {
            "ownerAddress": wallet,
            "page": 1, "limit": 1000,
            "displayOptions": {"showFungible": True},
        },
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("result", {}).get("items", [])
    except Exception:
        return []


def wallet_usd_value(assets: list) -> float:
    total = 0.0
    for item in assets:
        ti = item.get("token_info", {})
        pi = ti.get("price_info", {})
        if pi:
            price  = float(pi.get("price_per_token", 0))
            bal    = float(ti.get("balance", 0))
            dec    = int(ti.get("decimals", 0))
            actual = bal / (10 ** dec) if dec > 0 else bal
            total += actual * price
    return total


def assign_cohort(usd: float) -> str:
    for b in COHORT_BRACKETS:
        if b["min_usd"] <= usd < b["max_usd"]:
            return b["name"]
    return "Minnow 🦐"


def detect_address_col(df: pd.DataFrame):
    for col in df.columns:
        if df[col].astype(str).str.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$").any():
            return col
    return None


def parse_wallets_from_csv(uploaded) -> list:
    df = pd.read_csv(io.BytesIO(uploaded.read()))
    col = detect_address_col(df)
    if not col:
        return []
    return df[col].dropna().astype(str).str.strip().unique().tolist()


def detect_holder_address_col(df: pd.DataFrame):
    """Detect the wallet-address column in a holder export."""
    for cand in ADDRESS_COL_CANDIDATES:
        if cand in df.columns:
            return cand
    return detect_address_col(df)


def fetch_signatures(wallet: str, helius_url: str, limit: int = 100) -> list:
    payload = {
        "jsonrpc": "2.0", "id": "sigs",
        "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": limit}],
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def fetch_transaction(sig: str, helius_url: str):
    payload = {
        "jsonrpc": "2.0", "id": "tx",
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("result")
    except Exception:
        return None


def parse_token_inflows(tx, wallet: str, sig: str) -> list:
    """Return list of token inflows for `wallet` in `tx`."""
    inflows = []
    if not tx:
        return inflows

    meta       = tx.get("meta", {})
    block_time = tx.get("blockTime", 0)

    pre  = {e["accountIndex"]: e for e in meta.get("preTokenBalances", [])}
    post = {e["accountIndex"]: e for e in meta.get("postTokenBalances", [])}

    wallet_indices = set()
    for i, key_info in enumerate(tx.get("transaction", {}).get("message", {}).get("accountKeys", [])):
        pubkey = key_info if isinstance(key_info, str) else key_info.get("pubkey", "")
        if pubkey == wallet:
            wallet_indices.add(i)
    for idx in set(pre) | set(post):
        entry = post.get(idx) or pre.get(idx, {})
        if entry.get("owner") == wallet:
            wallet_indices.add(idx)

    for idx in wallet_indices:
        pre_entry  = pre.get(idx, {})
        post_entry = post.get(idx, {})
        pre_amt    = float((pre_entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_amt   = float((post_entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        if post_amt > pre_amt:
            mint = post_entry.get("mint") or pre_entry.get("mint", "unknown")
            if mint in SKIP_TOKENS:
                continue
            inflows.append({
                "mint":            mint,
                "amount_received": round(post_amt - pre_amt, 6),
                "timestamp":       block_time,
                "date":            datetime.fromtimestamp(block_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "tx_sig":          sig,
            })
    return inflows


def scan_wallet_acquisitions(wallet: str, helius_url: str, cutoff_ts: int) -> list:
    """Fetch and parse all token inflows for a wallet since cutoff_ts."""
    acquisitions = []
    sigs = fetch_signatures(wallet, helius_url, limit=100)
    for sig_info in sigs:
        if sig_info.get("blockTime", 0) < cutoff_ts:
            break
        tx     = fetch_transaction(sig_info["signature"], helius_url)
        found  = parse_token_inflows(tx, wallet, sig_info["signature"])
        acquisitions.extend(found)
        time.sleep(0.1)
    return acquisitions


def enrich_token_metadata(mints: list, helius_url: str) -> dict:
    """Batch-fetch symbol/name for a list of mint addresses."""
    meta = {}
    for i in range(0, len(mints), 100):
        batch = mints[i:i+100]
        try:
            r = requests.post(helius_url, json={
                "jsonrpc": "2.0", "id": "batch-meta",
                "method": "getAssetBatch",
                "params": {"ids": batch},
            }, timeout=30)
            for asset in r.json().get("result", []):
                mint = asset.get("id", "")
                if mint:
                    m = asset.get("content", {}).get("metadata", {})
                    meta[mint] = {
                        "symbol": m.get("symbol", mint[:8]),
                        "name":   m.get("name", "Unknown"),
                    }
        except Exception:
            pass
    return meta


def render_acquisition_results(
    all_acq: list,
    token_wallets: dict,
    token_meta: dict,
    total_wallets: int,
    min_shared: int,
    days: int,
    download_filename: str,
):
    """Shared display logic for Tab 3 and Tab 4."""
    summary = []
    for mint, buying_wallets in token_wallets.items():
        meta   = token_meta.get(mint, {"symbol": mint[:8], "name": ""})
        events = [a for a in all_acq if a["mint"] == mint]
        summary.append({
            "mint":           mint,
            "symbol":         meta["symbol"],
            "name":           meta["name"],
            "wallets_bought": len(buying_wallets),
            "total_received": round(sum(e["amount_received"] for e in events), 4),
            "last_seen":      max(e["date"] for e in events),
            "coordinated":    len(buying_wallets) >= min_shared,
        })
    summary.sort(key=lambda x: (-x["wallets_bought"], x["last_seen"]))

    coordinated = [s for s in summary if s["coordinated"]]
    if coordinated:
        st.markdown("---")
        st.subheader(f"🚨 Coordination Signals — bought by {min_shared}+ wallets")
        st.caption("These tokens were independently acquired by multiple wallets in your window.")
        st.dataframe(pd.DataFrame([{
            "Symbol":         s["symbol"],
            "Name":           s["name"],
            "Wallets Bought": s["wallets_bought"],
            "Total Received": s["total_received"],
            "Last Buy":       s["last_seen"],
            "Mint":           s["mint"],
        } for s in coordinated]), use_container_width=True, hide_index=True)

        for s in coordinated:
            with st.expander(f"**{s['symbol']}** — {s['wallets_bought']} wallets · {s['name']}"):
                st.caption(f"Mint: `{s['mint']}`")
                events = sorted(
                    [a for a in all_acq if a["mint"] == s["mint"]],
                    key=lambda x: x["timestamp"], reverse=True,
                )
                for ev in events:
                    st.markdown(
                        f"- `{ev['wallet'][:12]}...`  +{ev['amount_received']:,.2f} tokens  ·  {ev['date']}"
                    )
    else:
        st.info(f"No tokens were bought by {min_shared}+ wallets in this window. Try lowering the threshold or extending the lookback.")

    st.markdown("---")
    st.subheader(f"🛒 All Buys ({len(all_acq)} acquisitions across {len(summary)} unique tokens)")
    st.caption("Every individual buy by every scanned wallet — not just shared/coordinated ones.")

    buys_rows = []
    for acq in sorted(all_acq, key=lambda x: x["timestamp"], reverse=True):
        meta = token_meta.get(acq["mint"], {"symbol": acq["mint"][:8], "name": ""})
        buys_rows.append({
            "Date":           acq["date"],
            "Wallet":         acq["wallet"],
            "Symbol":         meta["symbol"],
            "Name":           meta["name"],
            "Amount":         acq["amount_received"],
            "Wallets (total)": len(token_wallets[acq["mint"]]),
            "🚨 Coordinated": "✅" if len(token_wallets[acq["mint"]]) >= min_shared else "",
            "Mint":           acq["mint"],
            "Tx":             acq["tx_sig"],
        })
    st.dataframe(pd.DataFrame(buys_rows), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader(f"📋 Token Summary ({len(summary)} unique tokens)")
    st.dataframe(pd.DataFrame([{
        "Symbol":         s["symbol"],
        "Name":           s["name"],
        "Wallets":        s["wallets_bought"],
        "Total Received": s["total_received"],
        "Last Buy":       s["last_seen"],
        "🚨 Signal":      "✅" if s["coordinated"] else "",
        "Mint":           s["mint"],
    } for s in summary]), use_container_width=True, hide_index=True)

    st.markdown("---")
    dl_rows = []
    for acq in all_acq:
        meta = token_meta.get(acq["mint"], {"symbol": "", "name": ""})
        dl_rows.append({
            "wallet":          acq["wallet"],
            "mint":            acq["mint"],
            "symbol":          meta["symbol"],
            "name":            meta["name"],
            "amount_received": acq["amount_received"],
            "date":            acq["date"],
            "tx_sig":          acq["tx_sig"],
            "wallets_bought":  len(token_wallets[acq["mint"]]),
            "coordinated":     len(token_wallets[acq["mint"]]) >= min_shared,
        })
    csv_out = pd.DataFrame(dl_rows).sort_values(
        ["coordinated", "wallets_bought"], ascending=[False, False]
    ).to_csv(index=False).encode()
    st.download_button("⬇️ Download CSV", csv_out, download_filename, "text/csv")


# ── Whale Pressure helpers ────────────────────────────────────────────────────
def get_token_largest_accounts(mint: str, helius_url: str) -> list:
    """Fetch top holders of a token via getTokenLargestAccounts."""
    payload = {
        "jsonrpc": "2.0", "id": "tla",
        "method": "getTokenLargestAccounts",
        "params": [mint, {"commitment": "finalized"}],
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("result", {}).get("value", [])
    except Exception:
        return []


def resolve_token_account_owner(token_account: str, helius_url: str) -> str:
    """Resolve a token account address to its owner wallet."""
    payload = {
        "jsonrpc": "2.0", "id": "gai",
        "method": "getAccountInfo",
        "params": [token_account, {"encoding": "jsonParsed"}],
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=20)
        r.raise_for_status()
        result = r.json().get("result", {})
        parsed = result.get("value", {}).get("data", {}).get("parsed", {})
        return parsed.get("info", {}).get("owner", "")
    except Exception:
        return ""


def get_token_supply(mint: str, helius_url: str) -> float:
    """Fetch total supply for % ownership calculation."""
    payload = {
        "jsonrpc": "2.0", "id": "ts",
        "method": "getTokenSupply",
        "params": [mint],
    }
    try:
        r = requests.post(helius_url, json=payload, timeout=20)
        r.raise_for_status()
        val = r.json().get("result", {}).get("value", {})
        return float(val.get("uiAmount") or 0)
    except Exception:
        return 0.0


def conviction_score(bought: float, sold: float, buy_txs: int, sell_txs: int) -> float:
    """Returns a score from -100 (max selling) to +100 (max buying)."""
    total_vol = bought + sold
    total_txs = buy_txs + sell_txs
    vol_score = ((bought - sold) / total_vol * 100) if total_vol > 0 else 0
    tx_score  = ((buy_txs - sell_txs) / total_txs * 100) if total_txs > 0 else 0
    return round(0.7 * vol_score + 0.3 * tx_score, 1)


def score_label(score: float) -> str:
    if score >= 70:  return "🟢 Strong Accumulation"
    if score >= 35:  return "🟩 Accumulating"
    if score >= 10:  return "🔵 Slight Buying"
    if score >= -10: return "⚪ Neutral / Mixed"
    if score >= -35: return "🟡 Slight Selling"
    if score >= -70: return "🟠 Distributing"
    return "🔴 Heavy Distribution"


# ── global sidebar: API key + remember me ────────────────────────────────────
with st.sidebar:
    st.title("🔬 Solana Wallet Intel")
    st.markdown("---")

    components.html("""
<script>
(function() {
    const saved = localStorage.getItem('helius_api_key');
    if (saved) {
        window.parent.postMessage({type: 'helius_key', key: saved}, '*');
    }
})();
window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'save_helius_key')
        localStorage.setItem('helius_api_key', e.data.key);
    if (e.data && e.data.type === 'clear_helius_key')
        localStorage.removeItem('helius_api_key');
});
</script>
""", height=0)

    if "helius_key_value" not in st.session_state:
        st.session_state["helius_key_value"] = ""

    helius_key = st.text_input(
        "Helius API Key",
        type="password",
        placeholder="Paste key — stays in your browser",
        value=st.session_state["helius_key_value"],
        key="helius_key_input",
    )
    remember = st.checkbox("Remember in this browser", value=True)

    if helius_key:
        st.session_state["helius_key_value"] = helius_key
        if remember:
            components.html(f"""
<script>
window.parent.postMessage({{type: 'save_helius_key', key: '{helius_key}'}}, '*');
</script>
""", height=0)
        else:
            components.html("""
<script>
window.parent.postMessage({type: 'clear_helius_key'}, '*');
</script>
""", height=0)

    if not helius_key:
        st.info("💡 Saved key auto-fills after page load.")

    st.markdown("---")
    st.caption("Get a free key at [helius.dev](https://helius.dev)")


HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={helius_key.strip()}" if helius_key else ""


# ── tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "🐋 Cohort Analyzer",
    "🔍 Whale Overlap",
    "📅 Recent Buys",
    "📌 Watchlist",
    "🤝 Common Holders",
    "📊 Whale Pressure",
])


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — COHORT ANALYZER
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.header("Cohort Analyzer")
    st.caption("Classify token holders by total wallet net worth.")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
1. **Prepare a CSV of Solana wallet addresses.** Any CSV with a column of wallet addresses works — the app will detect the address column automatically. Some ways to get one:
   - **Solscan:** go to a token page → *Holders* tab → *Download CSV*
   - **Birdeye / Dexscreener:** holder exports from the token analytics pages
   - **Your own list:** paste addresses into a spreadsheet, save as CSV — one address per row is fine
2. Upload the CSV below and hit **Run Cohort Analysis**
3. Results bucket each wallet into Whale / Shark / Dolphin / Fish / Minnow tiers by total portfolio value
4. Whales, Sharks & Dolphins are automatically passed to the **Whale Overlap** and **Recent Buys** tabs for deeper analysis
""")

    c1_file = st.file_uploader("Upload holder CSV", type=["csv"], key="c1_file")
    c1_max  = st.slider("Max wallets", 10, MAX_WALLETS, 50, 10, key="c1_max")
    c1_btn  = st.button("🚀 Run Cohort Analysis", type="primary",
                        disabled=not (helius_key and c1_file), key="c1_btn")

    if c1_btn:
        wallets = parse_wallets_from_csv(c1_file)
        if not wallets:
            st.error("No valid Solana addresses found in CSV.")
            st.stop()
        if len(wallets) > c1_max:
            st.info(f"CSV has {len(wallets)} addresses — analyzing top {c1_max}.")
            wallets = wallets[:c1_max]

        st.markdown("---")
        prog = st.progress(0)
        status = st.empty()

        cohort_buckets = defaultdict(list)
        rows = []

        for i, wallet in enumerate(wallets):
            status.text(f"[{i+1}/{len(wallets)}] {wallet[:12]}...")
            assets    = get_assets(wallet, HELIUS_URL)
            net_worth = wallet_usd_value(assets)
            label     = assign_cohort(net_worth)
            cohort_buckets[label].append({"wallet": wallet, "net_worth": net_worth})
            rows.append({"wallet": wallet, "net_worth_usd": round(net_worth, 2), "cohort": label})
            prog.progress((i + 1) / len(wallets))
            time.sleep(0.2)

        status.empty()
        prog.empty()

        big_wallets = [
            r["wallet"] for r in rows
            if r["cohort"] in ("Whale 🐋", "Shark 🦈", "Dolphin 🐬")
        ]
        st.session_state["whale_wallets"] = big_wallets
        if big_wallets:
            st.success(f"✅ {len(big_wallets)} Whale/Shark/Dolphin wallets saved — available in Whale Overlap and Recent Buys tabs.")

        st.markdown("---")
        st.subheader("📊 Distribution")
        total = len(wallets)
        cols  = st.columns(len(COHORT_BRACKETS))
        for col, bracket in zip(cols, COHORT_BRACKETS):
            count = len(cohort_buckets[bracket["name"]])
            col.metric(bracket["name"], count, f"{count/total*100:.1f}%")

        st.markdown("---")
        st.subheader("🏷️ Holders by Cohort")
        for bracket in COHORT_BRACKETS:
            members = cohort_buckets[bracket["name"]]
            if not members:
                continue
            with st.expander(f"{bracket['name']}  ·  {len(members)} holders"):
                df_out = pd.DataFrame([
                    {"Wallet": m["wallet"], "Net Worth (USD)": f"${m['net_worth']:,.2f}"}
                    for m in sorted(members, key=lambda x: -x["net_worth"])
                ])
                st.dataframe(df_out, use_container_width=True, hide_index=True)

        st.markdown("---")
        csv_bytes = pd.DataFrame(rows).sort_values("net_worth_usd", ascending=False).to_csv(index=False).encode()
        st.download_button("⬇️ Download results CSV", csv_bytes, "holder_cohorts.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — WHALE OVERLAP
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.header("Whale Overlap")
    st.caption("See what tokens a group of wallets share — find what the big players are all holding.")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
**Two ways to load wallets:**
- Run the Cohort Analyzer first → Whales, Sharks & Dolphins auto-populate here
- Or paste wallet addresses directly (one per line)

Results show every token held by 2+ of the wallets, ranked by how many wallets share it.
Stablecoins and wSOL are filtered out automatically.
""")

    source = st.radio(
        "Wallet source",
        ["Use Whales/Sharks/Dolphins from Cohort tab", "Paste wallets manually", "Upload new CSV"],
        key="t2_source",
        horizontal=True,
    )

    t2_wallets = []

    if source == "Use Whales/Sharks/Dolphins from Cohort tab":
        saved = st.session_state.get("whale_wallets", [])
        if saved:
            st.success(f"{len(saved)} wallets loaded from Cohort Analysis (Whales, Sharks & Dolphins).")
            t2_wallets = saved
            with st.expander("View wallets"):
                for w in saved:
                    st.code(w)
        else:
            st.info("Run the Cohort Analyzer first to populate this automatically.")

    elif source == "Paste wallets manually":
        raw = st.text_area(
            "Paste wallet addresses (one per line)",
            height=150,
            placeholder="7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU\n...",
            key="t2_paste",
        )
        if raw.strip():
            t2_wallets = [w.strip() for w in raw.strip().splitlines() if len(w.strip()) >= 32]
            st.caption(f"{len(t2_wallets)} addresses detected.")

    else:
        t2_file = st.file_uploader("Upload wallet CSV", type=["csv"], key="t2_file")
        if t2_file:
            t2_wallets = parse_wallets_from_csv(t2_file)
            if t2_wallets:
                st.caption(f"{len(t2_wallets)} addresses found.")
            else:
                st.error("No valid Solana addresses detected in CSV.")

    t2_max = st.slider("Max wallets to scan", 5, MAX_WALLETS, 30, 5, key="t2_max")
    min_shared = st.slider("Min wallets sharing a token (filter noise)", 2, 10, 2, 1, key="t2_min")

    t2_btn = st.button(
        "🔍 Run Overlap Analysis",
        type="primary",
        disabled=not (helius_key and t2_wallets),
        key="t2_btn",
    )

    if t2_btn:
        wallets = t2_wallets[:t2_max]
        if len(t2_wallets) > t2_max:
            st.info(f"Capped to {t2_max} wallets.")

        st.markdown("---")
        prog2   = st.progress(0)
        status2 = st.empty()

        token_counts   = defaultdict(int)
        token_metadata = {}
        token_holders  = defaultdict(list)

        for i, wallet in enumerate(wallets):
            status2.text(f"[{i+1}/{len(wallets)}] {wallet[:12]}...")
            assets = get_assets(wallet, HELIUS_URL)
            seen_this_wallet = set()

            for asset in assets:
                mint      = asset.get("id", "")
                interface = asset.get("interface", "")
                if interface != "FungibleToken" or mint in SKIP_TOKENS:
                    continue
                ti  = asset.get("token_info", {})
                bal = float(ti.get("balance", 0))
                if bal <= 0 or mint in seen_this_wallet:
                    continue

                seen_this_wallet.add(mint)
                token_counts[mint] += 1
                token_holders[mint].append(wallet)

                if mint not in token_metadata:
                    meta = asset.get("content", {}).get("metadata", {})
                    pi   = ti.get("price_info", {})
                    token_metadata[mint] = {
                        "symbol":    meta.get("symbol", "???"),
                        "name":      meta.get("name", "Unknown"),
                        "price_usd": float(pi.get("price_per_token", 0)),
                    }

            prog2.progress((i + 1) / len(wallets))
            time.sleep(0.2)

        status2.empty()
        prog2.empty()

        shared = {m: c for m, c in token_counts.items() if c >= min_shared}
        sorted_tokens = sorted(shared.items(), key=lambda x: -x[1])

        if not sorted_tokens:
            st.warning(f"No tokens found shared by {min_shared}+ wallets.")
        else:
            st.subheader(f"🏆 {len(sorted_tokens)} shared tokens found")

            summary_rows = []
            for mint, count in sorted_tokens[:50]:
                meta = token_metadata[mint]
                summary_rows.append({
                    "Symbol":        meta["symbol"],
                    "Name":          meta["name"],
                    "Wallets Holding": count,
                    "% of Group":    f"{count/len(wallets)*100:.1f}%",
                    "Price (USD)":   f"${meta['price_usd']:,.6f}" if meta["price_usd"] > 0 else "—",
                    "Mint":          mint,
                })
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("🔎 Token Detail")
            for mint, count in sorted_tokens[:30]:
                meta    = token_metadata[mint]
                holders = token_holders[mint]
                pct     = count / len(wallets) * 100
                with st.expander(f"**{meta['symbol']}** — {count} wallets ({pct:.1f}%)  ·  {meta['name']}"):
                    st.caption(f"Mint: `{mint}`")
                    if meta["price_usd"] > 0:
                        st.caption(f"Price: ${meta['price_usd']:,.6f}")
                    for h in holders:
                        st.code(h)

            st.markdown("---")
            dl_rows = []
            for mint, count in sorted_tokens:
                meta = token_metadata[mint]
                for h in token_holders[mint]:
                    dl_rows.append({
                        "mint": mint,
                        "symbol": meta["symbol"],
                        "name": meta["name"],
                        "wallets_holding": count,
                        "wallet": h,
                    })
            csv2 = pd.DataFrame(dl_rows).to_csv(index=False).encode()
            st.download_button("⬇️ Download overlap CSV", csv2, "whale_overlap.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — RECENT ACQUISITIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.header("Recent Buys")
    st.caption("What tokens have whales/sharks/dolphins actually purchased in the last N days?")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
- Run **Cohort Analyzer** first to auto-populate wallets, or paste/upload your own list
- Set your lookback window (1–30 days)
- Results show **every individual buy** by every scanned wallet, plus a separate section
  flagging tokens bought by 2+ wallets — that's your coordination signal
- Stablecoins and wSOL are filtered automatically
""")

    t3_source = st.radio(
        "Wallet source",
        ["Use Whales/Sharks/Dolphins from Cohort tab", "Paste wallets manually", "Upload new CSV"],
        key="t3_source",
        horizontal=True,
    )

    t3_wallets = []

    if t3_source == "Use Whales/Sharks/Dolphins from Cohort tab":
        saved3 = st.session_state.get("whale_wallets", [])
        if saved3:
            st.success(f"{len(saved3)} wallets loaded from Cohort Analysis (Whales, Sharks & Dolphins).")
            t3_wallets = saved3
            with st.expander("View wallets"):
                for w in saved3:
                    st.code(w)
        else:
            st.info("Run the Cohort Analyzer first to populate this automatically.")

    elif t3_source == "Paste wallets manually":
        raw3 = st.text_area(
            "Paste wallet addresses (one per line)",
            height=150,
            placeholder="7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU\n...",
            key="t3_paste",
        )
        if raw3.strip():
            t3_wallets = [w.strip() for w in raw3.strip().splitlines() if len(w.strip()) >= 32]
            st.caption(f"{len(t3_wallets)} addresses detected.")

    else:
        t3_file = st.file_uploader("Upload wallet CSV", type=["csv"], key="t3_file")
        if t3_file:
            t3_wallets = parse_wallets_from_csv(t3_file)
            if t3_wallets:
                st.caption(f"{len(t3_wallets)} addresses found.")
            else:
                st.error("No valid Solana addresses detected in CSV.")

    col_a, col_b = st.columns(2)
    with col_a:
        t3_days = st.slider("Lookback (days)", 1, 30, 7, 1, key="t3_days")
    with col_b:
        t3_max  = st.slider("Max wallets to scan", 5, 50, 20, 5, key="t3_max",
                             help="Each wallet scans up to 100 recent txs — keep low for speed")

    t3_min_shared = st.slider(
        "Highlight when bought by N+ wallets",
        2, 10, 2, 1, key="t3_min_shared",
        help="Tokens bought by this many wallets are flagged as coordination signals",
    )

    t3_btn = st.button(
        "📅 Run Acquisition Scan",
        type="primary",
        disabled=not (helius_key and t3_wallets),
        key="t3_btn",
    )

    if t3_btn:
        wallets3   = t3_wallets[:t3_max]
        cutoff_ts  = int((datetime.now(timezone.utc) - timedelta(days=t3_days)).timestamp())
        cutoff_str = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        if len(t3_wallets) > t3_max:
            st.info(f"Capped to {t3_max} wallets.")

        st.markdown(f"**Scanning {len(wallets3)} wallets for buys since {cutoff_str}...**")
        st.caption("This tab reads raw transactions — it's slower than the others. ~2–5s per wallet.")

        prog3   = st.progress(0)
        status3 = st.empty()

        all_acq3       = []
        token_wallets3 = defaultdict(set)
        token_meta3    = {}

        for i, wallet in enumerate(wallets3):
            status3.text(f"[{i+1}/{len(wallets3)}] {wallet[:12]}... scanning transactions")
            acqs = scan_wallet_acquisitions(wallet, HELIUS_URL, cutoff_ts)
            for acq in acqs:
                mint = acq["mint"]
                token_wallets3[mint].add(wallet)
                acq["wallet"] = wallet
                all_acq3.append(acq)
                if mint not in token_meta3:
                    token_meta3[mint] = {"symbol": mint[:8], "name": ""}
            prog3.progress((i + 1) / len(wallets3))

        status3.empty()
        prog3.empty()

        if not all_acq3:
            st.warning(f"No token inflows found in the last {t3_days} days for these wallets.")
        else:
            unknown3 = [m for m in token_meta3 if token_meta3[m]["name"] == ""]
            token_meta3.update(enrich_token_metadata(unknown3, HELIUS_URL))
            render_acquisition_results(
                all_acq3, token_wallets3, token_meta3,
                len(wallets3), t3_min_shared, t3_days,
                f"whale_acquisitions_{t3_days}d.csv",
            )


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 4 — WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.header("Watchlist")
    st.caption("Scan your personal preset list of wallets for recent token acquisitions.")

    if PRESET_WALLETS:
        st.info(
            f"**{len(PRESET_WALLETS)} wallets** in your watchlist. "
            "To add or remove wallets, edit `PRESET_WALLETS` near the top of `app.py` and redeploy."
        )
        with st.expander("View preset wallets"):
            for w in PRESET_WALLETS:
                st.code(w)
    else:
        st.warning(
            "Your watchlist is empty. Open `app.py`, find `PRESET_WALLETS`, "
            "and add your wallet addresses there."
        )
        st.stop()

    col_a4, col_b4 = st.columns(2)
    with col_a4:
        t4_days = st.slider("Lookback (days)", 1, 30, 7, 1, key="t4_days")
    with col_b4:
        t4_min_shared = st.slider(
            "Highlight when bought by N+ wallets", 2, 10, 2, 1, key="t4_min_shared"
        )

    t4_btn = st.button(
        "📌 Scan Watchlist",
        type="primary",
        disabled=not helius_key,
        key="t4_btn",
    )

    if t4_btn:
        cutoff_ts  = int((datetime.now(timezone.utc) - timedelta(days=t4_days)).timestamp())
        cutoff_str = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        st.markdown(f"**Scanning {len(PRESET_WALLETS)} wallets for buys since {cutoff_str}...**")
        st.caption("Reads raw transactions — ~2–5s per wallet.")

        prog4   = st.progress(0)
        status4 = st.empty()

        all_acq4       = []
        token_wallets4 = defaultdict(set)
        token_meta4    = {}

        for i, wallet in enumerate(PRESET_WALLETS):
            status4.text(f"[{i+1}/{len(PRESET_WALLETS)}] {wallet[:12]}... scanning transactions")
            acqs = scan_wallet_acquisitions(wallet, HELIUS_URL, cutoff_ts)
            for acq in acqs:
                mint = acq["mint"]
                token_wallets4[mint].add(wallet)
                acq["wallet"] = wallet
                all_acq4.append(acq)
                if mint not in token_meta4:
                    token_meta4[mint] = {"symbol": mint[:8], "name": ""}
            prog4.progress((i + 1) / len(PRESET_WALLETS))

        status4.empty()
        prog4.empty()

        if not all_acq4:
            st.warning(f"No token inflows found in the last {t4_days} days for your watchlist wallets.")
        else:
            unknown4 = [m for m in token_meta4 if token_meta4[m]["name"] == ""]
            token_meta4.update(enrich_token_metadata(unknown4, HELIUS_URL))
            render_acquisition_results(
                all_acq4, token_wallets4, token_meta4,
                len(PRESET_WALLETS), t4_min_shared, t4_days,
                f"watchlist_acquisitions_{t4_days}d.csv",
            )


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 5 — COMMON HOLDERS
# ══════════════════════════════════════════════════════════════════════════════
with tab5:
    st.header("Common Holders Finder")
    st.caption("Find wallets that hold both Token A and Token B from two holder export CSVs.")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
1. Export holder lists for two tokens (e.g. from Solscan's *Holders* tab → *Download CSV*)
2. Upload each CSV below
3. The address column is detected automatically (works with Solscan's `Account`,
   Birdeye/Dexscreener exports, or any CSV containing a column of Solana addresses)
4. Common holders — wallets present in both files — are listed and downloadable
""")

    col1, col2 = st.columns(2)
    with col1:
        file1 = st.file_uploader("Token A holder CSV", type="csv", key="ch_file1")
    with col2:
        file2 = st.file_uploader("Token B holder CSV", type="csv", key="ch_file2")

    if file1 and file2:
        df1 = pd.read_csv(file1)
        df2 = pd.read_csv(file2)

        col1_name = detect_holder_address_col(df1)
        col2_name = detect_holder_address_col(df2)

        if not col1_name or not col2_name:
            st.error(
                "Couldn't detect a wallet address column in one or both files. "
                "Expected a column named one of: " + ", ".join(ADDRESS_COL_CANDIDATES) +
                ", or a column containing valid Solana addresses."
            )
        else:
            st.caption(f"Token A address column: `{col1_name}`  ·  Token B address column: `{col2_name}`")

            addrs1 = set(df1[col1_name].dropna().astype(str).str.strip())
            addrs2 = set(df2[col2_name].dropna().astype(str).str.strip())
            common = addrs1 & addrs2

            m1, m2, m3 = st.columns(3)
            m1.metric("Token A holders", len(addrs1))
            m2.metric("Token B holders", len(addrs2))
            m3.metric("Common holders", len(common))

            common_df = pd.DataFrame(sorted(common), columns=["Wallet Address"])
            st.dataframe(common_df, use_container_width=True, hide_index=True)

            st.download_button(
                "⬇️ Download common holders CSV",
                common_df.to_csv(index=False).encode(),
                "common_holders.csv",
                "text/csv",
            )


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 6 — WHALE PRESSURE
# ══════════════════════════════════════════════════════════════════════════════
with tab6:
    st.header("Whale Pressure")
    st.caption("Scan top holders of any token and score net buy/sell conviction over 1d, 2d, and 7d.")

    with st.expander("ℹ️ How it works", expanded=False):
        st.markdown("""
**Steps:**
1. Enter a token mint address
2. The app fetches the top holders by on-chain token balance (ownership %, not USD value)
3. Known exchange wallets are automatically excluded
4. Each remaining wallet's recent transactions are scanned to calculate net token flow
5. A **Conviction Score** (−100 to +100) is computed per time window:
   - `+100` = everyone buying, nobody selling
   - `−100` = everyone selling, nobody buying
   - Blends volume flow (70%) and transaction count ratio (30%)

**Score key:**
| Score | Signal |
|-------|--------|
| ≥ 70 | 🟢 Strong Accumulation |
| 35–69 | 🟩 Accumulating |
| 10–34 | 🔵 Slight Buying |
| −9 to 9 | ⚪ Neutral / Mixed |
| −10 to −34 | 🟡 Slight Selling |
| −35 to −69 | 🟠 Distributing |
| ≤ −70 | 🔴 Heavy Distribution |

**Notes:**
- Top holders are resolved from token accounts → owner wallets
- Exchange wallets (Binance, Coinbase, OKX, etc.) are skipped automatically
- Each wallet scans up to 150 recent transactions — this tab is slower than others
- Results are most meaningful for tokens with identifiable individual whale holders
""")

    t6_mint = st.text_input(
        "Token Mint Address",
        placeholder="e.g. DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        key="t6_mint",
    )

    col6a, col6b = st.columns(2)
    with col6a:
        t6_top_n = st.slider(
            "Top N holders to scan", 5, 40, 20, 5,
            key="t6_top_n",
            help="Fetches top holders on-chain, then resolves owners. Higher = slower.",
        )
    with col6b:
        t6_min_pct = st.number_input(
            "Min ownership % to include",
            min_value=0.0, max_value=10.0, value=0.1, step=0.05,
            key="t6_min_pct",
            help="Skip dust wallets holding < this % of supply",
        )

    t6_btn = st.button(
        "📊 Run Whale Pressure Scan",
        type="primary",
        disabled=not (helius_key and t6_mint.strip()),
        key="t6_btn",
    )

    if t6_btn:
        mint_addr = t6_mint.strip()
        st.markdown("---")

        # Step 1: token supply
        with st.spinner("Fetching token supply..."):
            total_supply = get_token_supply(mint_addr, HELIUS_URL)

        if total_supply <= 0:
            st.error("Couldn't fetch token supply. Check the mint address.")
            st.stop()

        # Step 2: top holders
        with st.spinner("Fetching top token accounts..."):
            largest = get_token_largest_accounts(mint_addr, HELIUS_URL)

        if not largest:
            st.error("No holder data returned. Check mint address or Helius key.")
            st.stop()

        # Step 3: resolve token accounts → owner wallets
        st.markdown(f"**Resolving {min(len(largest), t6_top_n)} token accounts → owner wallets...**")
        resolve_prog = st.progress(0)
        resolved_holders = []
        skipped_exchange = 0

        for i, entry in enumerate(largest[:t6_top_n]):
            token_acct = entry.get("address", "")
            ui_amount  = float(entry.get("uiAmount") or 0)
            pct        = (ui_amount / total_supply * 100) if total_supply > 0 else 0

            if pct < t6_min_pct:
                resolve_prog.progress((i + 1) / min(len(largest), t6_top_n))
                continue

            owner = resolve_token_account_owner(token_acct, HELIUS_URL)
            if not owner:
                resolve_prog.progress((i + 1) / min(len(largest), t6_top_n))
                continue

            if owner in EXCHANGE_WALLETS:
                skipped_exchange += 1
                resolve_prog.progress((i + 1) / min(len(largest), t6_top_n))
                continue

            resolved_holders.append({
                "token_account": token_acct,
                "owner":         owner,
                "balance":       ui_amount,
                "pct_supply":    round(pct, 4),
            })
            resolve_prog.progress((i + 1) / min(len(largest), t6_top_n))
            time.sleep(0.1)

        resolve_prog.empty()

        if not resolved_holders:
            st.warning("No qualifying holder wallets found after filtering exchanges.")
            st.stop()

        if skipped_exchange:
            st.info(f"ℹ️ Skipped {skipped_exchange} exchange wallet(s).")

        st.success(f"✅ {len(resolved_holders)} whale wallets identified. Scanning transactions across 1d / 2d / 7d windows...")

        # Show holder table
        st.subheader("🐳 Qualified Holders")
        holder_df = pd.DataFrame([{
            "Rank":        i + 1,
            "Wallet":      h["owner"],
            "Balance":     f"{h['balance']:,.0f}",
            "% of Supply": f"{h['pct_supply']:.4f}%",
        } for i, h in enumerate(resolved_holders)])
        st.dataframe(holder_df, use_container_width=True, hide_index=True)

        # Step 4: scan flows
        now_ts  = int(datetime.now(timezone.utc).timestamp())
        windows = {"1d": 1, "2d": 2, "7d": 7}
        cutoffs = {k: now_ts - v * 86400 for k, v in windows.items()}

        scan_prog   = st.progress(0)
        scan_status = st.empty()
        wallet_flows = {}

        for i, holder in enumerate(resolved_holders):
            wallet = holder["owner"]
            scan_status.text(f"[{i+1}/{len(resolved_holders)}] Scanning {wallet[:12]}...")

            sigs = fetch_signatures(wallet, HELIUS_URL, limit=150)
            txs_parsed = []

            for sig_info in sigs:
                bt = sig_info.get("blockTime", 0)
                if bt < cutoffs["7d"]:
                    break
                tx = fetch_transaction(sig_info["signature"], HELIUS_URL)
                if not tx:
                    continue

                meta = tx.get("meta", {})
                pre  = {e["accountIndex"]: e for e in meta.get("preTokenBalances", [])}
                post = {e["accountIndex"]: e for e in meta.get("postTokenBalances", [])}

                for idx in set(list(pre.keys()) + list(post.keys())):
                    pre_e     = pre.get(idx, {})
                    post_e    = post.get(idx, {})
                    this_mint = post_e.get("mint") or pre_e.get("mint", "")
                    owner     = post_e.get("owner") or pre_e.get("owner", "")
                    if this_mint != mint_addr or owner != wallet:
                        continue
                    pre_amt  = float((pre_e.get("uiTokenAmount")  or {}).get("uiAmount") or 0)
                    post_amt = float((post_e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                    delta    = post_amt - pre_amt
                    if delta != 0:
                        txs_parsed.append({"delta": delta, "ts": bt})

                time.sleep(0.07)

            wallet_flows[wallet] = {}
            for wname, days in windows.items():
                cutoff   = cutoffs[wname]
                relevant = [t for t in txs_parsed if t["ts"] >= cutoff]
                bought   = sum(t["delta"] for t in relevant if t["delta"] > 0)
                sold     = sum(abs(t["delta"]) for t in relevant if t["delta"] < 0)
                buy_txs  = sum(1 for t in relevant if t["delta"] > 0)
                sell_txs = sum(1 for t in relevant if t["delta"] < 0)
                wallet_flows[wallet][wname] = {
                    "bought":   round(bought, 2),
                    "sold":     round(sold, 2),
                    "buy_txs":  buy_txs,
                    "sell_txs": sell_txs,
                    "net":      round(bought - sold, 2),
                    "score":    conviction_score(bought, sold, buy_txs, sell_txs),
                }

            scan_prog.progress((i + 1) / len(resolved_holders))

        scan_status.empty()
        scan_prog.empty()

        # Step 5: display results
        st.markdown("---")
        st.subheader("📊 Conviction Scores")

        for wname in windows:
            scores   = [wallet_flows[h["owner"]][wname]["score"] for h in resolved_holders]
            agg      = round(sum(scores) / len(scores), 1) if scores else 0
            net_buys = sum(wallet_flows[h["owner"]][wname]["net"] > 0 for h in resolved_holders)
            net_sell = sum(wallet_flows[h["owner"]][wname]["net"] < 0 for h in resolved_holders)
            neutral  = len(resolved_holders) - net_buys - net_sell
            label    = score_label(agg)
            bar_color = "#22c55e" if agg >= 10 else "#ef4444" if agg <= -10 else "#6b7280"
            bar_pct   = int((agg + 100) / 2)

            st.markdown(f"### {wname} Window")
            st.markdown(f"""
<div style="background:#1e293b;border-radius:10px;padding:16px 20px;margin-bottom:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="font-size:1.6rem;font-weight:700;color:{'#22c55e' if agg>=10 else '#ef4444' if agg<=-10 else '#94a3b8'};">{agg:+.1f}</span>
    <span style="font-size:1rem;color:#e2e8f0;">{label}</span>
  </div>
  <div style="background:#334155;border-radius:6px;height:10px;overflow:hidden;">
    <div style="width:{bar_pct}%;height:100%;background:{bar_color};border-radius:6px;"></div>
  </div>
  <div style="display:flex;gap:24px;margin-top:10px;font-size:0.85rem;color:#94a3b8;">
    <span>🟢 Buying: <b style="color:#e2e8f0;">{net_buys}</b></span>
    <span>🔴 Selling: <b style="color:#e2e8f0;">{net_sell}</b></span>
    <span>⚪ Neutral: <b style="color:#e2e8f0;">{neutral}</b></span>
  </div>
</div>
""", unsafe_allow_html=True)

        # Per-wallet breakdown
        st.markdown("---")
        st.subheader("🔍 Per-Wallet Breakdown")

        for wname in windows:
            with st.expander(f"{wname} — individual wallet flows"):
                rows6 = []
                for h in resolved_holders:
                    w  = h["owner"]
                    wf = wallet_flows[w][wname]
                    rows6.append({
                        "Wallet":    w,
                        "% Supply":  f"{h['pct_supply']:.4f}%",
                        "Bought":    f"{wf['bought']:,.0f}",
                        "Sold":      f"{wf['sold']:,.0f}",
                        "Net":       f"{'+' if wf['net']>=0 else ''}{wf['net']:,.0f}",
                        "Buy Txs":   wf["buy_txs"],
                        "Sell Txs":  wf["sell_txs"],
                        "Score":     f"{wf['score']:+.1f}",
                        "Signal":    score_label(wf["score"]),
                    })
                rows6.sort(key=lambda x: float(x["Score"]), reverse=True)
                st.dataframe(pd.DataFrame(rows6), use_container_width=True, hide_index=True)

        # Download
        st.markdown("---")
        dl6_rows = []
        for h in resolved_holders:
            w = h["owner"]
            for wname in windows:
                wf = wallet_flows[w][wname]
                dl6_rows.append({
                    "wallet":      w,
                    "pct_supply":  h["pct_supply"],
                    "window":      wname,
                    "bought":      wf["bought"],
                    "sold":        wf["sold"],
                    "net":         wf["net"],
                    "buy_txs":     wf["buy_txs"],
                    "sell_txs":    wf["sell_txs"],
                    "score":       wf["score"],
                    "signal":      score_label(wf["score"]),
                })
        csv6 = pd.DataFrame(dl6_rows).to_csv(index=False).encode()
        st.download_button(
            "⬇️ Download Whale Pressure CSV",
            csv6,
            f"whale_pressure_{mint_addr[:8]}.csv",
            "text/csv",
        )
