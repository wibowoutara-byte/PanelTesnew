"""
Bot Telegram AnzNokosFree — Fitur Lengkap
Semua user bisa pakai gratis. Admin punya panel khusus.
"""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone


# Support .env file untuk dijalankan di laptop/terminal lokal
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ZURA_EMAIL       = os.environ.get("ZURA_EMAIL", "")
ZURA_PASSWORD    = os.environ.get("ZURA_PASSWORD", "")
ZURA_COOKIE      = os.environ.get("ZURA_COOKIE", "")   # e.g. "PHPSESSID=abc123" atau "laravel_session=xyz"
BASE_URL         = os.environ.get("BASE_URL", "https://x.zurastore.my.id")
ADMIN_CHAT_ID    = os.environ.get("ADMIN_CHAT_ID", "")
PRICE_PER_NUMBER = float(os.environ.get("PRICE_PER_NUMBER", "0"))
CURRENCY         = os.environ.get("CURRENCY", "Rp")

LOGIN_URL  = f"{BASE_URL}/"
GETNUM_URL = f"{BASE_URL}/dashboardz/getnum/"
DASH_URL   = f"{BASE_URL}/dashboardz/"
PAY_URL    = f"{BASE_URL}/dashboardz/payment/"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

USERS_FILE = "users.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("anznokosfree")

# ConversationHandler states
WAITING_RANGE      = 1   # input range manual
WAITING_SMS_WATCH  = 2   # input nomor untuk dipantau SMS-nya

# SMS notif toggle (in-memory)
SMS_NOTIF_ENABLED = False
SMS_NOTIF_TASK    = None      # asyncio.Task
_LAST_SMS_IDS: set[str] = set()

AJAX_HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": GETNUM_URL,
}


# ---------------------------------------------------------------------------
# User tracking
# ---------------------------------------------------------------------------
def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_users(data: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def track_user(user) -> dict:
    data = load_users()
    uid  = str(user.id)
    now  = datetime.now(timezone.utc).timestamp()
    if uid not in data:
        data[uid] = {
            "name": user.full_name,
            "username": user.username or "",
            "first_seen": now,
            "last_active": now,
            "numbers_claimed": 0,
            "numbers_success": 0,
        }
    else:
        data[uid]["last_active"] = now
        data[uid]["name"] = user.full_name
    save_users(data)
    return data[uid]


def record_number_claimed(user_id: str, success: bool):
    data = load_users()
    if user_id in data:
        data[user_id]["numbers_claimed"] += 1
        if success:
            data[user_id]["numbers_success"] += 1
        save_users(data)


# ---------------------------------------------------------------------------
# ZuraClient
# ---------------------------------------------------------------------------
class ZuraClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        self.logged_in       = False
        self.last_login_html = ""   # simpan HTML respons login terakhir
        self.cookie_mode     = False

        # Kalau ZURA_COOKIE di-set, langsung inject ke session — skip login otomatis
        if ZURA_COOKIE:
            self._load_cookies_from_env()

    def _load_cookies_from_env(self):
        """Parse ZURA_COOKIE (format: "nama=nilai; nama2=nilai2") dan inject ke session."""
        from urllib.parse import urlparse
        domain = urlparse(BASE_URL).hostname or "x.zurastore.my.id"
        count  = 0
        for part in ZURA_COOKIE.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                self.session.cookies.set(name.strip(), value.strip(), domain=domain)
                count += 1
        if count:
            self.logged_in   = True
            self.cookie_mode = True
            log.info(f"Cookie mode aktif — {count} cookie dimuat dari ZURA_COOKIE")

    # ── Login ──────────────────────────────────────────────────────────────
    def login(self) -> tuple[bool, str]:
        """Login ke website. Return (ok, pesan_debug)."""
        if not ZURA_EMAIL or not ZURA_PASSWORD:
            return False, "ZURA_EMAIL / ZURA_PASSWORD kosong di .env"

        # Coba beberapa kemungkinan URL login
        login_candidates = [
            BASE_URL + "/",
        ]

        csrf       = None
        login_url  = login_candidates[0]
        field_email    = "email"
        field_password = "password"

        # Step 1: GET halaman login, deteksi form
        for candidate in login_candidates:
            try:
                r0        = self.session.get(candidate, timeout=15, allow_redirects=True)
                final_url = r0.url          # URL setelah redirect (penting!)
                soup      = BeautifulSoup(r0.text, "html.parser")

                # ── Cari CSRF di form (hidden input) ──────────────────────
                for name in ("csrfmiddlewaretoken", "_token", "csrf_token", "_csrf", "csrf"):
                    tag = soup.find("input", {"name": name})
                    if tag and tag.get("value"):
                        csrf = tag["value"]
                        break

                # ── Cari CSRF di cookies (Double Submit Cookie pattern) ───
                if not csrf:
                    for cname in ("XSRF-TOKEN", "csrf_token", "csrftoken", "_csrf", "csrf"):
                        cv = self.session.cookies.get(cname)
                        if cv:
                            csrf = cv
                            log.info(f"CSRF dari cookie '{cname}': {csrf[:12]}...")
                            break

                # ── Cari field email/username ──────────────────────────────
                for fname in ("email", "username", "user", "login"):
                    if soup.find("input", {"name": fname}):
                        field_email = fname
                        break

                # ── Cari field password ────────────────────────────────────
                for fname in ("password", "pass", "passwd", "pwd"):
                    if soup.find("input", {"name": fname}):
                        field_password = fname
                        break

                # ── Tentukan URL POST dari form action ─────────────────────
                form = soup.find("form")
                if form:
                    action = form.get("action", "").strip()
                    if action:
                        if action.startswith("http"):
                            login_url = action
                        elif action.startswith("/"):
                            # path absolut dari domain
                            from urllib.parse import urlparse
                            parsed = urlparse(final_url)
                            login_url = f"{parsed.scheme}://{parsed.netloc}{action}"
                        else:
                            login_url = final_url.rstrip("/") + "/" + action
                    else:
                        # action kosong → POST ke URL akhir setelah redirect
                        login_url = final_url

                    log.info(f"Form login: GET={candidate} → final={final_url}, POST={login_url}")
                    log.info(f"Field: {field_email}/{field_password}, csrf={bool(csrf)}")
                    break
            except requests.RequestException as e:
                log.warning(f"GET {candidate} gagal: {e}")
                continue

        # Step 2: POST login
        payload = {field_email: ZURA_EMAIL, field_password: ZURA_PASSWORD}
        if csrf:
            payload["_token"]              = csrf
            payload["csrfmiddlewaretoken"] = csrf
            payload["csrf_token"]          = csrf

        # Header tambahan — meniru browser sungguhan (Referer & Origin wajib di banyak backend)
        post_headers = {
            "Origin":           BASE_URL,
            "Referer":          login_url,
            "Content-Type":     "application/x-www-form-urlencoded",
            "Accept":           "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language":  "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control":    "no-cache",
            "Pragma":           "no-cache",
        }

        try:
            r = self.session.post(login_url, data=payload,
                                  headers=post_headers,
                                  allow_redirects=True, timeout=20)
        except requests.RequestException as e:
            msg = f"Koneksi error saat login: {e}"
            log.error(msg)
            self.last_login_html = ""
            return False, msg

        # Simpan HTML respons untuk keperluan debug
        self.last_login_html = r.text

        # Step 3: Cek apakah login berhasil
        ok = (
            "dashboardz" in r.url
            or "logout" in r.text.lower()
            or "dashboard" in r.url.lower()
        )
        self.logged_in = ok

        if ok:
            log.info("Login berhasil ✅")
            return True, f"Login berhasil (url={r.url})"
        else:
            # Coba cek pesan error dari halaman (cek berbagai selector dan teks)
            soup2 = BeautifulSoup(r.text, "html.parser")
            err_msg = ""
            # Cari di berbagai elemen HTML yang biasa dipakai untuk error
            for sel in [
                ".alert", ".alert-danger", ".alert-error",
                ".error", ".errors", ".invalid-feedback",
                ".text-danger", ".text-red", "#error",
                "p.error", "div.error", "span.error",
                ".msg-error", ".message.error",
            ]:
                tag = soup2.select_one(sel)
                if tag:
                    err_msg = tag.get_text(" ", strip=True)[:200]
                    break
            # Juga coba cari teks "invalid", "wrong", "gagal", "salah", "incorrect"
            if not err_msg:
                body_text = soup2.get_text(" ", strip=True)
                for keyword in ["invalid", "wrong", "gagal", "salah", "incorrect", "failed", "error"]:
                    idx = body_text.lower().find(keyword)
                    if idx != -1:
                        err_msg = body_text[max(0, idx-30):idx+80].strip()
                        break
            detail = f"url={r.url}, http={r.status_code}"
            if err_msg:
                detail += f", pesan='{err_msg}'"
            log.warning(f"Login gagal: {detail}")
            return False, f"Login gagal — {detail}"

    def ensure_login(self) -> bool:
        if self.logged_in:
            return True
        ok, _ = self.login()
        return ok

    def _relogin_if_needed(self, resp: requests.Response) -> bool:
        if "login" in resp.url.lower() or resp.status_code in (401, 403):
            self.logged_in = False
            if self.cookie_mode:
                # Cookie expired — tidak bisa auto-login, user harus update cookie
                log.warning("⚠️ Cookie session expired! Update ZURA_COOKIE di .env dengan cookie baru dari browser.")
                return False
            ok, _ = self.login()
            return ok
        return False

    # ── Ambil nomor manual ─────────────────────────────────────────────────
    def get_number(self, prefix: str) -> tuple[str | None, str | None]:
        if not self.ensure_login():
            return None, "Gagal login ke website."
        clean     = prefix.rstrip("Xx")
        range_str = clean + "XXX"
        url = GETNUM_URL + "?ajax=get_number"
        try:
            r = self.session.post(url, json={"range": range_str}, headers=AJAX_HEADERS, timeout=20)
        except requests.RequestException as e:
            return None, f"Koneksi error: {e}"
        if self._relogin_if_needed(r):
            try:
                r = self.session.post(url, json={"range": range_str}, headers=AJAX_HEADERS, timeout=20)
            except requests.RequestException as e:
                return None, f"Error setelah login ulang: {e}"
        try:
            d = r.json()
        except Exception:
            return None, f"Response bukan JSON (HTTP {r.status_code})."
        if d.get("status") == "success":
            num = d.get("data", {}).get("full_number") or d.get("data", {}).get("number")
            return (str(num), None) if num else (None, "Nomor tidak ada di response.")
        return None, d.get("message") or d.get("error") or "Gagal mendapat nomor."

    # ── Sync data ──────────────────────────────────────────────────────────
    def sync_data(self) -> dict | None:
        if not self.ensure_login():
            return None
        url     = GETNUM_URL + "?ajax=sync_data"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": GETNUM_URL}
        try:
            r = self.session.get(url, headers=headers, timeout=20)
        except requests.RequestException:
            return None
        if self._relogin_if_needed(r):
            try:
                r = self.session.get(url, headers=headers, timeout=20)
            except requests.RequestException:
                return None
        try:
            return r.json()
        except Exception:
            return None

    # ── Hapus nomor ────────────────────────────────────────────────────────
    def delete_number(self, number: str) -> tuple[bool, str]:
        """Coba hapus nomor. Return (ok, pesan)."""
        if not self.ensure_login():
            return False, "Gagal login."
        # Coba beberapa endpoint umum
        endpoints = [
            (GETNUM_URL + "?ajax=delete_number",  {"number": number}),
            (GETNUM_URL + "?ajax=remove_number",  {"number": number}),
            (GETNUM_URL + "?ajax=delete",         {"number": number}),
            (DASH_URL   + "?ajax=delete_number",  {"number": number}),
        ]
        for url, payload in endpoints:
            try:
                r = self.session.post(url, json=payload, headers=AJAX_HEADERS, timeout=15)
                if r.status_code == 404:
                    continue
                try:
                    d = r.json()
                    if d.get("status") == "success":
                        return True, d.get("message", "Berhasil dihapus.")
                    # jika ada pesan bermakna, kembalikan
                    msg = d.get("message") or d.get("error") or ""
                    if msg:
                        return False, msg
                except Exception:
                    pass
            except requests.RequestException:
                continue
        return False, "Endpoint hapus tidak ditemukan / tidak didukung."

    # ── Saldo (scrape) ─────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        if not self.ensure_login():
            return {}
        try:
            r    = self.session.get(PAY_URL, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            texts = [t.strip() for t in soup.find_all(string=True) if t.strip()]
            ready, locked = "", ""
            for i, t in enumerate(texts):
                if "Saldo Ready" in t:
                    for j in range(i+1, min(i+5, len(texts))):
                        if re.match(r"^\$[\d.,]+$", texts[j]):
                            ready = texts[j]; break
                if "Saldo Locked" in t:
                    for j in range(i+1, min(i+5, len(texts))):
                        if re.match(r"^\$[\d.,]+$", texts[j]):
                            locked = texts[j]; break
            # fallback: cari angka dolar di seluruh halaman
            if not ready:
                m = re.search(r'ready["\s:>]*(\$[\d.,]+)', r.text, re.I)
                if m: ready = m.group(1)
            if not locked:
                m = re.search(r'lock(?:ed)?["\s:>]*(\$[\d.,]+)', r.text, re.I)
                if m: locked = m.group(1)
            return {"ready": ready or "N/A", "locked": locked or "N/A"}
        except Exception:
            return {"ready": "N/A", "locked": "N/A"}

    # ── Dashboard stats (scrape) ───────────────────────────────────────────
    def get_dashboard_stats(self) -> dict:
        """Scrape halaman dashboard utama untuk statistik."""
        if not self.ensure_login():
            return {}
        try:
            r    = self.session.get(DASH_URL, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            texts = [t.strip() for t in soup.find_all(string=True) if t.strip()]
            stats: dict[str, str] = {}
            keywords = ["Total Nomor", "Nomor Aktif", "Nomor Sukses",
                        "Total SMS", "Saldo", "Deposit"]
            for i, t in enumerate(texts):
                for kw in keywords:
                    if kw.lower() in t.lower() and kw not in stats:
                        for j in range(i+1, min(i+4, len(texts))):
                            if re.match(r"^[\d$.,]+$", texts[j]):
                                stats[kw] = texts[j]; break
            return stats
        except Exception:
            return {}

    # ── Riwayat hari ini ───────────────────────────────────────────────────
    def get_today_history(self) -> list[dict]:
        data = self.sync_data()
        if not data:
            return []
        numbers    = data.get("numbers") or []
        sms_list   = data.get("sms") or []
        now_ts     = datetime.now(timezone.utc).timestamp()
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()

        def time_ago(ts):
            if not ts: return "?"
            d = int(now_ts - float(ts))
            if d < 60:    return "Baru saja"
            if d < 3600:  return f"{d//60} mnt lalu"
            if d < 86400: return f"{d//3600} jam lalu"
            return f"{d//86400} hari lalu"

        result, used_sms = [], set()
        for num in numbers:
            ts = float(num.get("last_update") or num.get("time") or 0)
            if ts < today_start:
                continue
            full   = str(num.get("full_number") or num.get("number") or "")
            status = num.get("status", "pending")
            matched = []
            for sms in sms_list:
                sn  = str(sms.get("number", ""))
                key = f"{sn}_{sms.get('time')}_{sms.get('otp')}"
                if (sn == full or sn in full or full in sn) and key not in used_sms:
                    used_sms.add(key)
                    matched.append(sms)
            if matched:
                for sms in matched:
                    result.append({
                        "full_number": full,
                        "status": "success",
                        "country": num.get("country", ""),
                        "operator": num.get("operator", ""),
                        "time_ago": time_ago(sms.get("time") or ts),
                        "otp": sms.get("otp", ""),
                        "is_wa": sms.get("is_wa", False),
                    })
            else:
                disp = "failed" if status == "success" else status
                result.append({
                    "full_number": full, "status": disp,
                    "country": num.get("country", ""),
                    "operator": num.get("operator", ""),
                    "time_ago": time_ago(ts), "otp": "", "is_wa": False,
                })
        result.sort(key=lambda x: x["status"] == "success", reverse=True)
        return result

    # ── Semua nomor aktif ──────────────────────────────────────────────────
    def get_all_numbers(self) -> list[dict]:
        """Semua nomor dari sync_data, diurutkan terbaru dulu."""
        data = self.sync_data()
        if not data:
            return []
        nums = data.get("numbers") or []
        return sorted(nums, key=lambda x: float(x.get("last_update") or x.get("time") or 0), reverse=True)

    # ── Cek SMS untuk nomor tertentu ───────────────────────────────────────
    def get_sms_for_number(self, number: str) -> list[dict]:
        data = self.sync_data()
        if not data:
            return []
        sms_list = data.get("sms") or []
        num_clean = number.strip().lstrip("+")
        result = []
        for s in sms_list:
            sn = str(s.get("number", "")).lstrip("+")
            if sn == num_clean or sn in num_clean or num_clean in sn:
                result.append(s)
        result.sort(key=lambda x: float(x.get("time") or 0), reverse=True)
        return result

    # ── SMS terbaru ────────────────────────────────────────────────────────
    def get_latest_sms(self, limit=10) -> list[dict]:
        data = self.sync_data()
        if not data:
            return []
        sms = data.get("sms") or []
        sms.sort(key=lambda x: float(x.get("time") or 0), reverse=True)
        return sms[:limit]

    # ── Range terbaru ──────────────────────────────────────────────────────
    def get_recent_ranges(self, n=8) -> list[str]:
        data = self.sync_data()
        if not data:
            return []
        nums = data.get("numbers") or []
        seen, out = set(), []
        for num in sorted(nums, key=lambda x: float(x.get("last_update") or x.get("time") or 0), reverse=True):
            r = str(num.get("range") or "")
            if r and r not in seen:
                seen.add(r)
                out.append(r)
            if len(out) >= n:
                break
        return out

    # ── Scrape range dari halaman ──────────────────────────────────────────
    def _scrape_ranges_from_page(self) -> list[str]:
        if not self.ensure_login():
            return []
        try:
            resp = self.session.get(GETNUM_URL, timeout=15)
        except requests.RequestException:
            return []
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        results: list[str] = []

        def add(r: str):
            r = r.strip()
            clean      = r.rstrip("Xx")
            normalized = clean + "XXX"
            if normalized not in seen and re.match(r"^\d{5,11}X{3}$", normalized):
                seen.add(normalized)
                results.append(normalized)

        for m in re.findall(r"\b(\d{5,11}[Xx]{3,})\b", html):
            add(m)
        for el in soup.find_all(True):
            for attr in ("data-range", "data-value", "value"):
                val = el.get(attr, "")
                if val and re.match(r"^\d{5,11}[Xx]{3,}$", val.strip()):
                    add(val)
        for sc in soup.find_all("script"):
            txt = sc.string or ""
            for m in re.findall(r"['\"](\d{5,11}[Xx]{3,})['\"]", txt):
                add(m)
            for m in re.findall(r"(\d{5,11}[Xx]{3,})", txt):
                add(m)

        log.info(f"[scrape] {len(results)} range dari halaman getnum")
        return results

    # ── Build kandidat auto search ─────────────────────────────────────────
    def build_auto_candidates(self) -> list[str]:
        seen: set[str] = set()
        candidates: list[str] = []

        def push(r: str):
            if r and r not in seen:
                seen.add(r)
                candidates.append(r)

        page_ranges = self._scrape_ranges_from_page()
        for r in page_ranges:
            push(r)

        if not candidates:
            data = self.sync_data() or {}
            nums = data.get("numbers") or []
            success_nums = [n for n in nums if n.get("status") == "success"]
            other_nums   = [n for n in nums if n.get("status") != "success"]
            ordered = (
                sorted(success_nums, key=lambda x: float(x.get("last_update") or x.get("time") or 0), reverse=True)
                + sorted(other_nums,  key=lambda x: float(x.get("last_update") or x.get("time") or 0), reverse=True)
            )
            for num in ordered:
                raw = str(num.get("range") or "")
                push(raw)
                clean = raw.rstrip("Xx")
                if len(clean) > 6:
                    push(clean[:-1] + "XXX")

        log.info(f"[auto] {len(candidates)} kandidat (halaman={len(page_ranges)})")
        return candidates

    # ── Test endpoint AJAX ─────────────────────────────────────────────────
    def try_ajax(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        """Coba endpoint AJAX, return {status_code, json_or_text, error}."""
        if not self.ensure_login():
            return {"error": "Login gagal"}
        url = BASE_URL + path if path.startswith("/") else path
        try:
            if method.upper() == "POST":
                r = self.session.post(url, json=payload or {}, headers=AJAX_HEADERS, timeout=15)
            else:
                r = self.session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
            try:
                return {"status_code": r.status_code, "json": r.json()}
            except Exception:
                return {"status_code": r.status_code, "text": r.text[:500]}
        except Exception as e:
            return {"error": str(e)}


zura = ZuraClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_admin(uid: int | str) -> bool:
    if not ADMIN_CHAT_ID:
        return False
    return str(uid) == str(ADMIN_CHAT_ID)


def fmt_time_ago(ts) -> str:
    if not ts: return "?"
    d = int(datetime.now(timezone.utc).timestamp() - float(ts))
    if d < 60:    return "Baru saja"
    if d < 3600:  return f"{d//60} mnt lalu"
    if d < 86400: return f"{d//3600} jam lalu"
    return f"{d//86400} hari lalu"


def fmt_ts(ts) -> str:
    if not ts: return "?"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%d/%m %H:%M")


STATUS_EMOJI = {"success": "✅", "failed": "❌", "pending": "⏳", "active": "🟢"}


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------
def main_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🎯 Auto Cari",    callback_data="menu_auto"),
            InlineKeyboardButton("📱 Get Number",   callback_data="menu_getnumber"),
        ],
        [
            InlineKeyboardButton("📊 Nomor Aktif",  callback_data="menu_active"),
            InlineKeyboardButton("⏳ Tunggu SMS",   callback_data="menu_waitsms"),
        ],
        [
            InlineKeyboardButton("📋 Riwayat",      callback_data="menu_history"),
            InlineKeyboardButton("💬 SMS / OTP",    callback_data="menu_sms"),
        ],
        [
            InlineKeyboardButton("👤 Profil Saya",  callback_data="menu_profile"),
        ],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🔧 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")]])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Saldo & Revenue",     callback_data="admin_balance"),
            InlineKeyboardButton("📊 Statistik Hari Ini",  callback_data="admin_today"),
        ],
        [
            InlineKeyboardButton("📈 Dashboard Website",   callback_data="admin_dashboard"),
            InlineKeyboardButton("👥 Daftar User",         callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast",           callback_data="admin_broadcast"),
            InlineKeyboardButton("📥 Export Data",         callback_data="admin_export"),
        ],
        [
            InlineKeyboardButton("🔍 Debug AJAX",          callback_data="admin_ajax"),
            InlineKeyboardButton("📄 Debug HTML Page",     callback_data="admin_debugpage"),
        ],
        [
            InlineKeyboardButton("🔔 Notif SMS: OFF",      callback_data="admin_notif"),
            InlineKeyboardButton("🗑️ Hapus Semua Nomor",   callback_data="admin_deleteall"),
        ],
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="menu_main")]])


def ajax_test_keyboard() -> InlineKeyboardMarkup:
    endpoints = [
        ("sync_data",      "admin_ajax_sync_data"),
        ("get_ranges",     "admin_ajax_get_ranges"),
        ("list_numbers",   "admin_ajax_list_numbers"),
        ("get_operators",  "admin_ajax_get_operators"),
        ("get_countries",  "admin_ajax_get_countries"),
        ("dashboard_data", "admin_ajax_dashboard_data"),
    ]
    rows = [[InlineKeyboardButton(f"🔌 {name}", callback_data=cb)] for name, cb in endpoints]
    rows.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user(user)
    admin_badge = " 👑" if is_admin(user.id) else ""
    txt = (
        f"Halo, *{user.first_name}*{admin_badge}! 👋\n\n"
        "Selamat datang di *AnzNokosFree* 🔥\n"
        "Semua fitur *gratis* untuk semua orang!\n\n"
        "Pilih menu di bawah:"
    )
    kb = main_menu_keyboard(user.id)
    if update.message:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    await q.answer()

    # ════════════════════════════════════════
    # ── Menu Utama
    # ════════════════════════════════════════
    if data == "menu_main":
        await start(update, context)

    # ════════════════════════════════════════
    # ── Auto Cari Range
    # ════════════════════════════════════════
    elif data == "menu_auto":
        user = q.from_user
        track_user(user)
        await q.edit_message_text(
            "🎯 *Auto Cari Nomor*\n\n🔄 Mengambil kandidat range dari website...",
            parse_mode="Markdown",
        )

        loop = asyncio.get_running_loop()
        candidates = await loop.run_in_executor(None, zura.build_auto_candidates)

        if not candidates:
            await q.edit_message_text(
                "❌ *Tidak ada kandidat range.*\n\n"
                "Website tidak menampilkan range aktif. Coba *Get Number* manual dulu.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Get Number Manual", callback_data="menu_getnumber")],
                    [InlineKeyboardButton("🔙 Menu Utama",        callback_data="menu_main")],
                ])
            )
            return

        await q.edit_message_text(
            f"🎯 *Auto Cari Nomor*\n\n"
            f"📡 *{len(candidates)}* kandidat range ditemukan.\n"
            f"Mencoba satu per satu...",
            parse_mode="Markdown",
        )

        url = GETNUM_URL + "?ajax=get_number"

        async def try_range(range_str: str):
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: zura.session.post(url, json={"range": range_str}, headers=AJAX_HEADERS, timeout=20)
                )
                d = resp.json()
                if d.get("status") == "success":
                    num = d.get("data", {}).get("full_number") or d.get("data", {}).get("number")
                    return str(num) if num else None, d.get("message", "ok")
                return None, d.get("message") or "tidak tersedia"
            except Exception as e:
                return None, str(e)[:40]

        found_num, found_range, errors = None, None, []

        for i, rng in enumerate(candidates):
            if i % 2 == 0:
                try:
                    await q.edit_message_text(
                        f"🎯 *Auto Cari* — {i+1}/{len(candidates)}\n\n"
                        f"🔍 Range: `{rng}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            num, msg = await try_range(rng)
            if num:
                found_num, found_range = num, rng
                break
            errors.append(f"❌ `{rng}`: {msg}")

        if found_num:
            record_number_claimed(str(user.id), True)
            log.info(f"Auto cari: {user.id} dapat {found_num} via {found_range}")
            await q.edit_message_text(
                f"✅ *Nomor ditemukan!*\n\n"
                f"📱 `{found_num}`\n"
                f"📡 Range: `{found_range}`\n"
                f"🔍 Dicoba: {len(errors)+1}/{len(candidates)} range\n\n"
                f"Gunakan secepatnya sebelum expired!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏳ Tunggu SMS Nomor Ini", callback_data=f"watch_{found_num}")],
                    [InlineKeyboardButton("🎯 Auto Cari Lagi",       callback_data="menu_auto")],
                    [InlineKeyboardButton("💬 Cek SMS/OTP",          callback_data="menu_sms")],
                    [InlineKeyboardButton("🔙 Menu Utama",           callback_data="menu_main")],
                ])
            )
        else:
            record_number_claimed(str(user.id), False)
            err_preview = "\n".join(errors[:6])
            if len(errors) > 6:
                err_preview += f"\n_...dan {len(errors)-6} lainnya_"
            await q.edit_message_text(
                f"❌ *Auto Cari Selesai — Tidak Ada Nomor*\n\n"
                f"Semua *{len(candidates)}* range sudah dicoba.\n\n"
                f"{err_preview}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Input Manual", callback_data="menu_getnumber")],
                    [InlineKeyboardButton("🔄 Coba Lagi",   callback_data="menu_auto")],
                    [InlineKeyboardButton("🔙 Menu Utama",  callback_data="menu_main")],
                ])
            )

    # ════════════════════════════════════════
    # ── Get Number manual → conversation
    # ════════════════════════════════════════
    elif data == "menu_getnumber":
        ranges = zura.get_recent_ranges(8)
        if ranges:
            hint = "\n".join(f"  `{r}`" for r in ranges)
            txt  = (
                "📡 *Range yang baru dipakai:*\n\n"
                f"{hint}\n\n"
                "Ketik range nomor yang ingin kamu pakai:\n"
                "_(Contoh: `22465375XXX`)_"
            )
        else:
            txt = (
                "📱 *Get Number*\n\n"
                "Ketik range nomor yang ingin kamu pakai:\n"
                "_(Contoh: `22465375XXX`)_"
            )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=cancel_keyboard())
        return WAITING_RANGE

    # ════════════════════════════════════════
    # ── Nomor Aktif
    # ════════════════════════════════════════
    elif data == "menu_active":
        await q.edit_message_text("🔄 Mengambil semua nomor...")
        nums = zura.get_all_numbers()
        if not nums:
            await q.edit_message_text(
                "📭 Belum ada nomor di akun.",
                reply_markup=back_keyboard()
            )
            return

        lines = [f"📊 *Semua Nomor* — {len(nums)} total\n"]
        rows  = []
        for n in nums[:15]:
            status   = n.get("status", "pending")
            em       = STATUS_EMOJI.get(status, "❓")
            full     = str(n.get("full_number") or n.get("number") or "?")
            country  = n.get("country") or ""
            operator = n.get("operator") or ""
            ts       = n.get("last_update") or n.get("time") or 0
            detail   = " | ".join(filter(None, [country, operator]))
            lines.append(f"{em} `{full}` — {fmt_time_ago(ts)}")
            if detail:
                lines.append(f"   _{detail}_")
            rows.append([
                InlineKeyboardButton(f"⏳ Tunggu SMS — {full[-6:]}", callback_data=f"watch_{full}"),
                InlineKeyboardButton("🗑️", callback_data=f"del_num:{full}"),
            ])

        if len(nums) > 15:
            lines.append(f"\n_...dan {len(nums)-15} lainnya_")

        rows.append([InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")])
        await q.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ── Hapus nomor (dari tombol di Nomor Aktif)
    elif data.startswith("del_num:"):
        number = data.split(":", 1)[1]
        await q.answer(f"🗑️ Menghapus {number}...", show_alert=False)
        loop = asyncio.get_running_loop()
        ok, msg = await loop.run_in_executor(None, lambda: zura.delete_number(number))
        if ok:
            await q.answer(f"✅ {number} berhasil dihapus.", show_alert=True)
        else:
            await q.answer(f"⚠️ {msg}", show_alert=True)
        # Refresh daftar nomor
        context.user_data["_refresh_active"] = True
        await button_handler_active_refresh(q, context)

    # ── Shortcut: tunggu SMS langsung dari tombol nomor
    elif data.startswith("watch_"):
        number = data.split("_", 1)[1]
        context.user_data["watch_number"] = number
        await start_sms_watch(q, context, number)

    # ════════════════════════════════════════
    # ── Tunggu SMS → conversation
    # ════════════════════════════════════════
    elif data == "menu_waitsms":
        await q.edit_message_text(
            "⏳ *Tunggu SMS / OTP*\n\n"
            "Ketik nomor telepon yang ingin dipantau.\n"
            "Bot akan polling tiap 10 detik selama 3 menit.\n\n"
            "_(Contoh: `628123456789` atau `+628123456789`)_",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )
        return WAITING_SMS_WATCH

    # ════════════════════════════════════════
    # ── Riwayat
    # ════════════════════════════════════════
    elif data == "menu_history":
        await q.edit_message_text("🔄 Mengambil riwayat hari ini...")
        records = zura.get_today_history()
        if not records:
            await q.edit_message_text("📭 Belum ada aktivitas hari ini.", reply_markup=back_keyboard())
            return

        s_r = [r for r in records if r["status"] == "success"]
        f_r = [r for r in records if r["status"] == "failed"]
        p_r = [r for r in records if r["status"] == "pending"]

        lines = [
            f"📋 *Riwayat Hari Ini* — {len(records)} nomor\n",
            f"✅ {len(s_r)} Sukses  ❌ {len(f_r)} Gagal  ⏳ {len(p_r)} Pending\n",
        ]
        for r in records[:12]:
            em   = STATUS_EMOJI.get(r["status"], "❓")
            line = f"{em} `{r['full_number']}`"
            if r["country"]: line += f" — {r['country']}"
            if r["otp"]:
                line += f"\n   💬 _{r['otp'].replace('`', chr(39))[:80]}_"
            lines.append(line)
        if len(records) > 12:
            lines.append(f"\n_...dan {len(records)-12} lainnya_")

        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_keyboard())

    # ════════════════════════════════════════
    # ── SMS / OTP
    # ════════════════════════════════════════
    elif data == "menu_sms":
        await q.edit_message_text("🔄 Mengambil SMS/OTP terbaru...")
        sms_list = zura.get_latest_sms(15)
        if not sms_list:
            await q.edit_message_text("📭 Belum ada SMS/OTP.", reply_markup=back_keyboard())
            return
        lines = ["💬 *SMS / OTP Terbaru:*\n"]
        for s in sms_list:
            num = s.get("number", "?")
            otp = (s.get("otp") or "").strip().replace("`", "'")
            wa  = " ⚠️_WA_" if s.get("is_wa") else ""
            lines.append(f"📱 `{num}`{wa} — _{fmt_time_ago(s.get('time'))}_")
            if otp:
                lines.append(f"   `{otp[:120]}`")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("🔄 Refresh", callback_data="menu_sms")],
                                      [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")],
                                  ]))

    # ════════════════════════════════════════
    # ── Profil
    # ════════════════════════════════════════
    elif data == "menu_profile":
        track_user(q.from_user)
        users = load_users()
        u         = users.get(str(uid), {})
        n_claimed = u.get("numbers_claimed", 0)
        n_success = u.get("numbers_success", 0)
        n_failed  = n_claimed - n_success
        rate      = f"{(n_success/n_claimed*100):.1f}%" if n_claimed else "N/A"
        first     = (datetime.fromtimestamp(u.get("first_seen", 0), tz=timezone.utc)
                     .strftime("%d %b %Y") if u.get("first_seen") else "?")
        uname     = f"@{q.from_user.username}" if q.from_user.username else "–"
        txt = (
            f"👤 *Profil Kamu*\n\n"
            f"Nama    : {q.from_user.full_name}\n"
            f"Username: {uname}\n"
            f"ID      : `{uid}`\n"
            f"Bergabung: {first}\n\n"
            f"📱 Diklaim   : *{n_claimed}*\n"
            f"✅ Berhasil  : *{n_success}*\n"
            f"❌ Gagal     : *{n_failed}*\n"
            f"📈 Success   : *{rate}*"
        )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=back_keyboard())

    # ════════════════════════════════════════
    # ── ADMIN PANEL
    # ════════════════════════════════════════
    elif data == "menu_admin":
        if not is_admin(uid):
            await q.answer("⛔ Akses ditolak.", show_alert=True)
            return
        await q.edit_message_text("🔧 *Admin Panel*\nPilih menu:", parse_mode="Markdown",
                                  reply_markup=admin_keyboard())

    # ── Admin: Saldo & Revenue
    elif data == "admin_balance":
        if not is_admin(uid): return
        await q.edit_message_text("🔄 Mengambil data saldo...")
        loop = asyncio.get_running_loop()
        bal   = await loop.run_in_executor(None, zura.get_balance)
        users = load_users()

        total_claimed = sum(u.get("numbers_claimed", 0) for u in users.values())
        total_success = sum(u.get("numbers_success", 0) for u in users.values())
        revenue       = PRICE_PER_NUMBER * total_success

        lines = [
            "💰 *Saldo & Revenue*\n",
            "🏦 *Saldo Akun:*",
            f"  Ready : `{bal.get('ready', 'N/A')}`",
            f"  Locked: `{bal.get('locked', 'N/A')}`\n",
            "📊 *Statistik Bot:*",
            f"  Total klaim  : *{total_claimed}*",
            f"  Berhasil     : *{total_success}*",
            f"  Gagal        : *{total_claimed - total_success}*",
            f"  Total user   : *{len(users)}*",
        ]
        if PRICE_PER_NUMBER > 0:
            lines += [
                "\n💵 *Revenue Bot:*",
                f"  Harga/nomor  : *{CURRENCY} {PRICE_PER_NUMBER:,.0f}*",
                f"  Total revenue: *{CURRENCY} {revenue:,.0f}*",
            ]
        else:
            lines.append("\n_(Set `PRICE_PER_NUMBER` di Secrets untuk track revenue)_")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())

    # ── Admin: Statistik hari ini
    elif data == "admin_today":
        if not is_admin(uid): return
        await q.edit_message_text("🔄 Mengambil statistik hari ini...")
        records = zura.get_today_history()
        data2   = zura.sync_data() or {}
        sms_all = data2.get("sms") or []
        nums_all = data2.get("numbers") or []

        s_r = [r for r in records if r["status"] == "success"]
        f_r = [r for r in records if r["status"] == "failed"]
        p_r = [r for r in records if r["status"] == "pending"]
        rate = f"{len(s_r)/len(records)*100:.1f}%" if records else "N/A"

        lines = [
            "📊 *Statistik Hari Ini*\n",
            f"📱 Total nomor  : *{len(records)}*",
            f"✅ Berhasil     : *{len(s_r)}*",
            f"❌ Gagal        : *{len(f_r)}*",
            f"⏳ Pending      : *{len(p_r)}*",
            f"📈 Success rate : *{rate}*",
            f"💬 SMS masuk    : *{len(sms_all)}*",
            f"📦 Total nomor  : *{len(nums_all)}* (semua waktu)",
        ]
        if s_r:
            lines.append("\n✅ *Sukses Hari Ini:*")
            for r in s_r[:5]:
                otp = r.get("otp", "")[:60].replace("`", "'")
                lines.append(f"  `{r['full_number']}` — {r.get('country', '')}")
                if otp: lines.append(f"    💬 _{otp}_")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())

    # ── Admin: Dashboard website
    elif data == "admin_dashboard":
        if not is_admin(uid): return
        await q.edit_message_text("🔄 Scraping dashboard website...")
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(None, zura.get_dashboard_stats)
        data2 = zura.sync_data() or {}
        nums  = data2.get("numbers") or []
        sms   = data2.get("sms") or []

        lines = ["📈 *Dashboard Website*\n"]
        if stats:
            for k, v in stats.items():
                lines.append(f"  {k}: *{v}*")
        else:
            lines.append("_(Tidak bisa scrape stats dari halaman dashboard)_")

        lines += [
            f"\n📦 Nomor di sync_data : *{len(nums)}*",
            f"💬 SMS di sync_data   : *{len(sms)}*",
        ]

        # Status breakdown
        by_status: dict[str, int] = {}
        for n in nums:
            s = n.get("status", "pending")
            by_status[s] = by_status.get(s, 0) + 1
        if by_status:
            lines.append("\n*Status nomor:*")
            for s, c in by_status.items():
                em = STATUS_EMOJI.get(s, "❓")
                lines.append(f"  {em} {s}: *{c}*")

        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())

    # ── Admin: Daftar User
    elif data == "admin_users":
        if not is_admin(uid): return
        users = load_users()
        if not users:
            await q.edit_message_text("Belum ada user.", reply_markup=admin_keyboard())
            return
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("last_active", 0), reverse=True)
        lines = [f"👥 *Daftar User* ({len(users)} orang)\n"]
        for i, (user_id, u) in enumerate(sorted_users[:20], 1):
            name    = u.get("name", "?")
            claimed = u.get("numbers_claimed", 0)
            success = u.get("numbers_success", 0)
            last    = fmt_time_ago(u.get("last_active"))
            lines.append(f"{i}. *{name}* (`{user_id}`)\n"
                         f"   📱 {claimed} klaim | ✅ {success} sukses | 🕐 {last}")
        if len(users) > 20:
            lines.append(f"\n_...dan {len(users)-20} user lainnya_")
        if PRICE_PER_NUMBER > 0:
            total_rev = PRICE_PER_NUMBER * sum(u.get("numbers_success", 0) for u in users.values())
            lines.append(f"\n💰 Total revenue: *{CURRENCY} {total_rev:,.0f}*")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())

    # ── Admin: Broadcast
    elif data == "admin_broadcast":
        if not is_admin(uid): return
        users = load_users()
        await q.edit_message_text(
            f"📢 *Broadcast*\n\n"
            f"Ketik pesan untuk dikirim ke *{len(users)} user*.\n"
            f"_Ketik /batal untuk membatalkan._",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )
        context.user_data["broadcast_mode"] = True

    # ── Admin: Export Data
    elif data == "admin_export":
        if not is_admin(uid): return
        await q.answer("📥 Menyiapkan file export...")
        users     = load_users()
        export    = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_users": len(users),
            "users": users,
        }
        buf = io.BytesIO(json.dumps(export, indent=2, ensure_ascii=False).encode())
        buf.name = "users_export.json"
        await context.bot.send_document(
            chat_id=uid,
            document=buf,
            filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            caption=f"📥 Export data — {len(users)} user\n{datetime.now().strftime('%d/%m/%Y %H:%M')}",
        )

    # ── Admin: Debug AJAX endpoints
    elif data == "admin_ajax":
        if not is_admin(uid): return
        await q.edit_message_text(
            "🔍 *Debug AJAX Endpoints*\n\n"
            "Pilih endpoint yang ingin dicoba:",
            parse_mode="Markdown",
            reply_markup=ajax_test_keyboard()
        )

    elif data.startswith("admin_ajax_"):
        if not is_admin(uid): return
        endpoint_name = data.replace("admin_ajax_", "")
        endpoint_map  = {
            "sync_data":      ("/dashboardz/getnum/?ajax=sync_data",      "GET",  None),
            "get_ranges":     ("/dashboardz/getnum/?ajax=get_ranges",      "GET",  None),
            "list_numbers":   ("/dashboardz/getnum/?ajax=list_numbers",    "GET",  None),
            "get_operators":  ("/dashboardz/getnum/?ajax=get_operators",   "GET",  None),
            "get_countries":  ("/dashboardz/getnum/?ajax=get_countries",   "GET",  None),
            "dashboard_data": ("/dashboardz/?ajax=dashboard_data",         "GET",  None),
        }
        if endpoint_name not in endpoint_map:
            return
        path, method, payload = endpoint_map[endpoint_name]
        await q.edit_message_text(f"🔄 Mencoba `{path}`...", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: zura.try_ajax(path, method, payload))

        status = result.get("status_code", "?")
        err    = result.get("error")
        if err:
            txt = f"🔍 *{endpoint_name}*\n\n❌ Error: `{err}`"
        elif "json" in result:
            j    = result["json"]
            dump = json.dumps(j, indent=2, ensure_ascii=False)[:1200]
            txt  = f"🔍 *{endpoint_name}* — HTTP {status}\n\n```\n{dump}\n```"
        else:
            txt = f"🔍 *{endpoint_name}* — HTTP {status}\n\n```\n{result.get('text','(kosong)')}\n```"

        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("🔙 Debug AJAX", callback_data="admin_ajax")]
                                  ]))

    # ── Admin: Debug HTML Page (langsung via tombol)
    elif data == "admin_debugpage":
        if not is_admin(uid): return
        await q.edit_message_text("🔄 Fetching halaman getnum...")
        loop = asyncio.get_running_loop()

        def fetch():
            if not zura.ensure_login(): return None, "Login gagal."
            try:
                r = zura.session.get(GETNUM_URL, timeout=15)
                return r.text, None
            except Exception as e:
                return None, str(e)

        html, err = await loop.run_in_executor(None, fetch)
        if err or not html:
            await q.edit_message_text(f"❌ {err or 'HTML kosong'}", reply_markup=admin_keyboard())
            return

        page_ranges = await loop.run_in_executor(None, zura._scrape_ranges_from_page)

        if page_ranges:
            rlist   = "\n".join(f"  `{r}`" for r in page_ranges[:25])
            suffix  = f"\n_...+{len(page_ranges)-25}_" if len(page_ranges) > 25 else ""
            summary = f"🔍 *Debug Page*\n\n📡 *{len(page_ranges)}* range ditemukan:\n{rlist}{suffix}"
        else:
            summary = (
                "🔍 *Debug Page*\n\n"
                "⚠️ Tidak ada range di HTML.\n"
                "_(Range mungkin di-render oleh JS)_\n\n"
                "📄 HTML mentah dikirim sebagai file."
            )
        await q.edit_message_text(summary, parse_mode="Markdown", reply_markup=admin_keyboard())

        buf = io.BytesIO(html.encode("utf-8", errors="replace")[:80_000])
        buf.name = "getnum_page.html"
        await context.bot.send_document(
            chat_id=uid, document=buf, filename="getnum_page.html",
            caption=f"📄 HTML getnum ({len(html):,} karakter)"
        )

    # ── Admin: Toggle Notif SMS
    elif data == "admin_notif":
        if not is_admin(uid): return
        global SMS_NOTIF_ENABLED, SMS_NOTIF_TASK
        SMS_NOTIF_ENABLED = not SMS_NOTIF_ENABLED
        if SMS_NOTIF_ENABLED:
            if SMS_NOTIF_TASK is None or SMS_NOTIF_TASK.done():
                SMS_NOTIF_TASK = asyncio.create_task(
                    sms_notif_loop(context.bot, int(ADMIN_CHAT_ID))
                )
            label = "🔔 Notif SMS: ON"
            msg   = "✅ Notifikasi SMS aktif. Admin akan dapat pesan tiap ada SMS baru."
        else:
            if SMS_NOTIF_TASK and not SMS_NOTIF_TASK.done():
                SMS_NOTIF_TASK.cancel()
                SMS_NOTIF_TASK = None
            label = "🔔 Notif SMS: OFF"
            msg   = "🔕 Notifikasi SMS dimatikan."

        # Update tombol notif di admin keyboard
        new_rows = []
        for row in admin_keyboard().inline_keyboard:
            new_row = []
            for btn in row:
                if "Notif SMS" in btn.text:
                    new_row.append(InlineKeyboardButton(label, callback_data="admin_notif"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        await q.edit_message_text(
            f"🔧 *Admin Panel*\n\n{msg}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(new_rows)
        )

    # ── Admin: Hapus semua nomor
    elif data == "admin_deleteall":
        if not is_admin(uid): return
        await q.edit_message_text(
            "⚠️ *Konfirmasi Hapus Semua Nomor*\n\n"
            "Ini akan mencoba menghapus semua nomor dari akun website.\n"
            "Yakin?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ya, Hapus Semua", callback_data="admin_deleteall_confirm")],
                [InlineKeyboardButton("❌ Batal",           callback_data="menu_admin")],
            ])
        )

    elif data == "admin_deleteall_confirm":
        if not is_admin(uid): return
        await q.edit_message_text("🗑️ Menghapus semua nomor...")
        loop = asyncio.get_running_loop()
        nums = await loop.run_in_executor(None, zura.get_all_numbers)
        ok_count, fail_count = 0, 0
        for n in nums:
            num_str = str(n.get("full_number") or n.get("number") or "")
            if num_str:
                ok, _ = await loop.run_in_executor(None, lambda: zura.delete_number(num_str))
                if ok: ok_count += 1
                else:  fail_count += 1
        await q.edit_message_text(
            f"🗑️ *Hapus Selesai*\n\n"
            f"✅ Berhasil: *{ok_count}*\n"
            f"❌ Gagal   : *{fail_count}*",
            parse_mode="Markdown",
            reply_markup=admin_keyboard()
        )


# ---------------------------------------------------------------------------
# Helper: refresh daftar nomor aktif (dipanggil setelah hapus)
# ---------------------------------------------------------------------------
async def button_handler_active_refresh(q, context):
    nums = zura.get_all_numbers()
    if not nums:
        await q.edit_message_text("📭 Tidak ada nomor.", reply_markup=back_keyboard())
        return
    lines = [f"📊 *Semua Nomor* — {len(nums)} total\n"]
    rows  = []
    for n in nums[:15]:
        status = n.get("status", "pending")
        em     = STATUS_EMOJI.get(status, "❓")
        full   = str(n.get("full_number") or n.get("number") or "?")
        ts     = n.get("last_update") or n.get("time") or 0
        lines.append(f"{em} `{full}` — {fmt_time_ago(ts)}")
        rows.append([
            InlineKeyboardButton(f"⏳ {full[-6:]}", callback_data=f"watch_{full}"),
            InlineKeyboardButton("🗑️", callback_data=f"del_num:{full}"),
        ])
    if len(nums) > 15:
        lines.append(f"\n_...dan {len(nums)-15} lainnya_")
    rows.append([InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")])
    await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------------------
# Tunggu SMS: start langsung dari tombol shortcut (watch_XXXXX)
# ---------------------------------------------------------------------------
async def start_sms_watch(q, context, number: str):
    msg = await q.edit_message_text(
        f"⏳ *Menunggu SMS untuk*\n`{number}`\n\n"
        f"Polling tiap 10 detik — maks 3 menit...",
        parse_mode="Markdown",
    )
    asyncio.create_task(
        poll_sms_for_number(context.bot, q.from_user.id, number, msg.chat.id, msg.message_id)
    )


# ---------------------------------------------------------------------------
# Background: polling SMS untuk nomor tertentu
# ---------------------------------------------------------------------------
async def poll_sms_for_number(bot, user_id: int, number: str,
                               chat_id: int, message_id: int,
                               max_seconds: int = 180):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    attempt  = 0

    while loop.time() < deadline:
        attempt += 1
        remaining = int(deadline - loop.time())
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=(
                    f"⏳ *Menunggu SMS* — `{number}`\n\n"
                    f"Percobaan ke-{attempt} | Sisa ~{remaining}s\n"
                    f"_{fmt_time_ago(None)}_"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

        sms_list = await loop.run_in_executor(None, lambda: zura.get_sms_for_number(number))

        if sms_list:
            latest = sms_list[0]
            otp    = (latest.get("otp") or "").strip()
            wa     = " ⚠️ WA" if latest.get("is_wa") else ""
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=(
                        f"✅ *SMS Masuk!*\n\n"
                        f"📱 Nomor : `{number}`{wa}\n"
                        f"🔑 OTP   : `{otp or '(kosong)'}` \n"
                        f"🕐 Waktu : {fmt_time_ago(latest.get('time'))}\n\n"
                        f"_{latest.get('otp', '')[:200]}_"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Lihat Semua SMS", callback_data="menu_sms")],
                        [InlineKeyboardButton("🔙 Menu Utama",      callback_data="menu_main")],
                    ])
                )
            except Exception:
                pass
            return

        await asyncio.sleep(10)

    # Timeout
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=(
                f"⌛ *Timeout* — Tidak ada SMS masuk untuk\n`{number}`\n\n"
                f"dalam {max_seconds//60} menit.\n"
                f"Coba tunggu lagi atau cek manual."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏳ Tunggu Lagi", callback_data=f"watch_{number}")],
                [InlineKeyboardButton("💬 Cek SMS",    callback_data="menu_sms")],
                [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")],
            ])
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background: notif SMS otomatis ke admin
# ---------------------------------------------------------------------------
async def sms_notif_loop(bot, admin_id: int):
    global _LAST_SMS_IDS
    loop = asyncio.get_running_loop()
    log.info("[notif] SMS notif loop started")

    # Seed awal agar tidak spam notif lama
    data = await loop.run_in_executor(None, zura.sync_data)
    if data:
        for s in (data.get("sms") or []):
            _LAST_SMS_IDS.add(f"{s.get('number')}_{s.get('time')}_{s.get('otp')}")

    while SMS_NOTIF_ENABLED:
        await asyncio.sleep(30)
        try:
            data = await loop.run_in_executor(None, zura.sync_data)
            if not data:
                continue
            new_sms = []
            for s in (data.get("sms") or []):
                key = f"{s.get('number')}_{s.get('time')}_{s.get('otp')}"
                if key not in _LAST_SMS_IDS:
                    _LAST_SMS_IDS.add(key)
                    new_sms.append(s)
            for s in new_sms:
                num = s.get("number", "?")
                otp = (s.get("otp") or "").strip()
                wa  = " ⚠️ WA" if s.get("is_wa") else ""
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🔔 *SMS Baru Masuk*{wa}\n\n"
                        f"📱 `{num}`\n"
                        f"🔑 `{otp or '(kosong)'}`\n"
                        f"🕐 {fmt_time_ago(s.get('time'))}"
                    ),
                    parse_mode="Markdown",
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[notif] error: {e}")

    log.info("[notif] SMS notif loop stopped")


# ---------------------------------------------------------------------------
# ConversationHandler: input range manual
# ---------------------------------------------------------------------------
async def receive_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("broadcast_mode"):
        return await handle_broadcast_message(update, context)

    user   = update.effective_user
    prefix = update.message.text.strip()

    if prefix.startswith("/"):
        await update.message.reply_text(
            "Dibatalkan. Ketik /start untuk kembali ke menu.",
            reply_markup=main_menu_keyboard(user.id)
        )
        context.user_data.clear()
        return ConversationHandler.END

    track_user(user)
    msg = await update.message.reply_text(
        f"🔄 Mencari nomor dengan range `{prefix.rstrip('Xx')}XXX`...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()
    number, error    = await loop.run_in_executor(None, lambda: zura.get_number(prefix))
    success          = number is not None
    record_number_claimed(str(user.id), success)

    if error:
        await msg.edit_text(f"❌ *Gagal:*\n{error}", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 Coba Range Lain", callback_data="menu_getnumber")],
                                [InlineKeyboardButton("🔙 Menu Utama",      callback_data="menu_main")],
                            ]))
    else:
        log.info(f"User {user.id} ({user.full_name}) dapat {number}")
        await msg.edit_text(
            f"✅ *Nomor berhasil!*\n\n📱 `{number}`\n\nGunakan secepatnya!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏳ Tunggu SMS Nomor Ini", callback_data=f"watch_{number}")],
                [InlineKeyboardButton("📱 Ambil Lagi",           callback_data="menu_getnumber")],
                [InlineKeyboardButton("💬 Cek SMS/OTP",          callback_data="menu_sms")],
                [InlineKeyboardButton("🔙 Menu Utama",           callback_data="menu_main")],
            ])
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler: input nomor untuk dipantau SMS-nya
# ---------------------------------------------------------------------------
async def receive_sms_watch_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    number = update.message.text.strip()

    if number.startswith("/"):
        await update.message.reply_text("Dibatalkan.", reply_markup=main_menu_keyboard(user.id))
        context.user_data.clear()
        return ConversationHandler.END

    track_user(user)
    msg = await update.message.reply_text(
        f"⏳ *Memantau SMS untuk*\n`{number}`\n\n"
        f"Polling tiap 10 detik — maks 3 menit...",
        parse_mode="Markdown"
    )
    asyncio.create_task(
        poll_sms_for_number(context.bot, user.id, number, msg.chat.id, msg.message_id)
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Broadcast handler
# ---------------------------------------------------------------------------
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return ConversationHandler.END

    text  = update.message.text
    users = load_users()
    context.user_data.pop("broadcast_mode", None)

    sent, failed = 0, 0
    status_msg = await update.message.reply_text(f"📢 Mengirim ke {len(users)} user...")

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=f"📢 *Pesan dari Admin:*\n\n{text}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📢 *Broadcast selesai!*\n\n✅ Terkirim: *{sent}*\n❌ Gagal: *{failed}*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------
async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /debug_page  (admin command)
# ---------------------------------------------------------------------------
async def cmd_debug_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Hanya admin.")
        return

    msg  = await update.message.reply_text("🔄 Fetching halaman getnum...")
    loop = asyncio.get_running_loop()

    def fetch():
        if not zura.ensure_login(): return None, "Gagal login."
        try:
            r = zura.session.get(GETNUM_URL, timeout=15)
            return r.text, None
        except Exception as e:
            return None, str(e)

    html, err = await loop.run_in_executor(None, fetch)
    if err or not html:
        await msg.edit_text(f"❌ {err or 'HTML kosong'}")
        return

    page_ranges = await loop.run_in_executor(None, zura._scrape_ranges_from_page)
    if page_ranges:
        rlist   = "\n".join(f"  `{r}`" for r in page_ranges[:30])
        suffix  = f"\n_...+{len(page_ranges)-30}_" if len(page_ranges) > 30 else ""
        summary = f"🔍 *Debug Page*\n\n📡 *{len(page_ranges)}* range:\n{rlist}{suffix}"
    else:
        summary = "🔍 *Debug Page*\n\n⚠️ Tidak ada range di HTML.\n📄 File HTML dikirim di bawah."

    await msg.edit_text(summary, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")]
                        ]))

    buf = io.BytesIO(html.encode("utf-8", errors="replace")[:80_000])
    await update.message.reply_document(
        document=buf, filename="getnum_page.html",
        caption=f"📄 HTML getnum ({len(html):,} karakter)"
    )


# ---------------------------------------------------------------------------
# /debug_login  (admin command — diagnosis masalah login)
# ---------------------------------------------------------------------------
async def cmd_debug_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Hanya admin.")
        return

    msg  = await update.message.reply_text("🔄 Mencoba login ke website...")
    loop = asyncio.get_running_loop()

    # Reset status login supaya benar-benar coba ulang dari nol
    zura.logged_in = False
    zura.session.cookies.clear()
    zura.last_login_html = ""

    ok, detail = await loop.run_in_executor(None, zura.login)

    if ok:
        result_text = (
            f"✅ *Login Berhasil!*\n\n"
            f"Detail: `{detail}`\n\n"
            f"Email   : `{ZURA_EMAIL}`\n"
            f"Base URL: `{BASE_URL}`"
        )
        await msg.edit_text(result_text, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")],
                            ]))
    else:
        result_text = (
            f"❌ *Login Gagal*\n\n"
            f"`{detail}`\n\n"
            f"*Email dicoba:* `{ZURA_EMAIL}`\n"
            f"*Base URL:* `{BASE_URL}`\n\n"
            f"*Kemungkinan penyebab:*\n"
            f"• Email/password salah di `.env`\n"
            f"• IP VPS diblokir website\n"
            f"• URL login berbeda\n\n"
            f"📎 File HTML respons dikirim di bawah — cari teks error di sana."
        )
        await msg.edit_text(result_text, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 Coba Login Lagi", callback_data="__debug_relogin__")],
                                [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_main")],
                            ]))

        # Kirim HTML respons sebagai file — user bisa cek pesan error asli dari website
        html_bytes = (zura.last_login_html or "(HTML kosong)").encode("utf-8", errors="replace")
        buf = io.BytesIO(html_bytes[:200_000])   # maks 200 KB
        await update.message.reply_document(
            document=buf,
            filename="login_response.html",
            caption=(
                "📄 HTML yang dikembalikan website setelah POST login.\n"
                "Buka file ini di browser/editor dan cari kata seperti:\n"
                "❝ wrong ❞ ❝ invalid ❞ ❝ gagal ❞ ❝ blocked ❞ ❝ error ❞"
            ),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ TELEGRAM_BOT_TOKEN belum diset!\n"
            "   Buat file .env lalu isi: TELEGRAM_BOT_TOKEN=token_kamu\n"
            "   Lihat .env.example untuk panduan."
        )
    if not ZURA_EMAIL or not ZURA_PASSWORD:
        raise SystemExit(
            "❌ ZURA_EMAIL / ZURA_PASSWORD belum diset!\n"
            "   Buat file .env dan isi variabelnya.\n"
            "   Lihat .env.example untuk panduan."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler — range input + SMS watch + broadcast
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_handler, pattern="^menu_getnumber$"),
            CallbackQueryHandler(button_handler, pattern="^menu_waitsms$"),
            CallbackQueryHandler(button_handler, pattern="^admin_broadcast$"),
        ],
        states={
            WAITING_RANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_range),
            ],
            WAITING_SMS_WATCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sms_watch_number),
            ],
        },
        fallbacks=[
            CommandHandler("batal",  cancel_conv),
            CommandHandler("start",  start),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("debug_page",  cmd_debug_page))
    app.add_handler(CommandHandler("debug_login", cmd_debug_login))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("✅ AnzNokosFree berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
