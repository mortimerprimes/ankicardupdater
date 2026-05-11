"""
⚡ Anki Card Generator - Liquid Glass Edition
Echtes iOS 26 Liquid Glass Design mit Flet
WEISS & CLEAN - Scrollbar & Responsive
"""

import flet as ft
import requests
import re
import os
import configparser
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from pathlib import Path


APP_NAME = "Anki Card Updater"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
ANKI_TIMEOUT = 60
OPENROUTER_TIMEOUT = 90
MODEL_FETCH_TIMEOUT = 20

logging.basicConfig(filename=APP_SUPPORT_DIR / "anki_updater.log", level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

DETAILED_LOGGING = False


def remove_cloze(text: str) -> str:
    cleaned = re.sub(r'\{\{\s*[cC]\d+\s*::.*?\}\}', '', text, flags=re.DOTALL)
    return re.sub(r'\s+', ' ', cleaned).strip()


class ConfigManager:
    CONFIG_FILE = APP_SUPPORT_DIR / "config.ini"
    LEGACY_CONFIG_FILE = Path(__file__).resolve().parent / "config.ini"
    DEFAULT = {
        'OpenRouter_API_Key': '', 'Default_Prompt': 'Beantworte folgende Frage:\n\n{frage}',
        'Favorite_Model': '', 'Favorite_Deck': '', 'Target_Field': 'Extra',
        'Question_Field': 'Text', 'Fill_Mode': 'Überspringen', 'max_tokens': '150',
        'temperature': '0.7', 'top_p': '1.0', 'Remove_Cloze': 'True', 'Concurrency': '5'
    }

    def __init__(self):
        self.config = configparser.ConfigParser(interpolation=None)
        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE)
        else:
            self.config['DEFAULT'] = self.DEFAULT
            self.merge_legacy_values()
            self.save()

        self.merge_legacy_values()
        self.save()

    def merge_legacy_values(self):
        if self.LEGACY_CONFIG_FILE.exists():
            legacy = configparser.ConfigParser(interpolation=None)
            legacy.read(self.LEGACY_CONFIG_FILE)
            for key in self.DEFAULT:
                value = legacy['DEFAULT'].get(key, '').strip()
                current = self.config['DEFAULT'].get(key, '').strip()
                if value and not current:
                    self.config['DEFAULT'][key] = value

    def save(self):
        with open(self.CONFIG_FILE, 'w') as f:
            self.config.write(f)

    def get(self, key, fallback=""):
        return self.config['DEFAULT'].get(key, fallback)

    def set(self, key, value):
        self.config['DEFAULT'][key] = value

    def save_ui_values(self, api_key, prompt, deck, model, target, question, fill_mode, remove_cloze, concurrency):
        self.set('OpenRouter_API_Key', (api_key or '').strip())
        self.set('Default_Prompt', (prompt or '').strip())
        if deck and deck != "Lade...":
            self.set('Favorite_Deck', deck)
        if model and model != "Lade...":
            self.set('Favorite_Model', model)
        self.set('Target_Field', target or 'Extra')
        self.set('Question_Field', question or 'Text')
        self.set('Fill_Mode', fill_mode or 'Überspringen')
        self.set('Remove_Cloze', str(remove_cloze))
        self.set('Concurrency', str(concurrency))
        self.save()


ANKI_URL = 'http://localhost:8765'

def anki_invoke(action, params=None):
    try:
        response = requests.post(
            ANKI_URL,
            json={'action': action, 'version': 6, 'params': params or {}},
            timeout=ANKI_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def get_deck_names():
    r = anki_invoke("deckNames")
    return r.get("result", []) if not r.get("error") else []

def fetch_models_api(api_key):
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=MODEL_FETCH_TIMEOUT,
        )
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if "id" in m]
    except Exception as e:
        logging.exception("Modelle konnten nicht geladen werden")
        return []

def get_ai_response(api_key, model, frage, prompt, log_cb, max_tokens, temp, top_p, rm_cloze):
    if rm_cloze:
        frage = remove_cloze(frage)
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt.replace("{frage}", frage)}],
                  "max_tokens": max_tokens, "temperature": temp, "top_p": top_p},
            timeout=OPENROUTER_TIMEOUT,
        )
        if r.status_code != 200:
            log_cb(f"OpenRouter Fehler {r.status_code}: {r.text[:160]}")
            return ""
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.exception("OpenRouter-Anfrage fehlgeschlagen")
        log_cb(f"OpenRouter Anfrage fehlgeschlagen: {e}")
        return ""


class AnkiUpdater:
    def __init__(self, log_cb):
        self.log = log_cb

    def process(self, deck, api_key, model, prompt, target, question, fill_mode, conc, progress_cb, cancel, max_t, temp, top_p, rm_cloze):
        self.log("🔍 Suche Notizen...")
        find = anki_invoke("findNotes", {"query": f'deck:"{deck}"'})
        if find.get("error"):
            self.log(f"AnkiConnect Fehler bei findNotes: {find['error']}")
            return {"updated": 0, "skipped": 0, "errors": 1}
        if not find.get("result"):
            self.log("Keine Notizen gefunden")
            return {"updated": 0, "skipped": 0, "errors": 0}

        self.log("📥 Lade Notizdetails...")
        info = anki_invoke("notesInfo", {"notes": find["result"]})
        if info.get("error"):
            self.log(f"AnkiConnect Fehler bei notesInfo: {info['error']}")
            return {"updated": 0, "skipped": 0, "errors": 1}

        notes = info.get("result", [])
        total, done = len(notes), 0
        self.log(f"📚 {total} Notizen")
        stats = {"updated": 0, "skipped": 0, "errors": 0}

        with ThreadPoolExecutor(max_workers=max(1, conc)) as pool:
            futures = [pool.submit(self._proc, n, api_key, model, prompt, target, question, fill_mode, max_t, temp, top_p, rm_cloze) for n in notes]
            for f in as_completed(futures):
                if cancel():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    result = f.result()
                    if result in stats:
                        stats[result] += 1
                except Exception as e:
                    stats["errors"] += 1
                    logging.exception("Notizverarbeitung fehlgeschlagen")
                    self.log(f"Fehler bei einer Notiz: {e}")
                done += 1
                progress_cb(done, total)
        self.log(f"✅ Fertig: {stats['updated']} aktualisiert, {stats['skipped']} übersprungen, {stats['errors']} Fehler")
        return stats

    def _proc(self, note, api_key, model, prompt, target, question, fill_mode, max_t, temp, top_p, rm_cloze):
        frage = note["fields"].get(question, {}).get("value", "")
        current = note["fields"].get(target, {}).get("value", "")
        if not frage or (current.strip() and fill_mode == "Überspringen"):
            return "skipped"
        answer = get_ai_response(api_key, model, frage, prompt, self.log, max_t, temp, top_p, rm_cloze)
        if answer:
            new_val = answer if fill_mode != "Anhängen" or not current.strip() else f"{current.strip()}\n{answer}"
            updated = anki_invoke("updateNoteFields", {"note": {"id": note["noteId"], "fields": {target: new_val}}})
            if updated.get("error"):
                self.log(f"Update-Fehler bei {note['noteId']}: {updated['error']}")
                return "errors"
            self.log(f"✓ {note['noteId']}")
            return "updated"
        return "errors"


def main(page: ft.Page):
    page.title = APP_NAME
    page.window.width, page.window.height = 1050, 800
    page.window.min_width, page.window.min_height = 500, 400
    page.bgcolor = "#f5f5f7"
    page.padding = 0
    page.scroll = None

    cfg = ConfigManager()
    all_models, all_decks = [], []
    cancelled = False
    max_tokens = int(cfg.get('max_tokens', '150'))
    temperature = float(cfg.get('temperature', '0.7'))
    top_p = float(cfg.get('top_p', '1.0'))
    concurrency = int(cfg.get('Concurrency', '5'))

    # === UI Components ===
    
    status = ft.Text("● Bereit", size=12, color="#34c759", weight=ft.FontWeight.W_500)

    api_input = ft.TextField(
        hint_text="API Key...", password=True, value=cfg.get('OpenRouter_API_Key'),
        border_radius=10, bgcolor="#ffffff", border_color="#d1d1d6", text_size=14,
        focused_border_color="#007aff", expand=True, height=44
    )

    deck_dd = ft.Dropdown(
        options=[ft.dropdown.Option("Lade...")], value="Lade...",
        border_radius=10, bgcolor="#ffffff", border_color="#d1d1d6", text_size=14,
        focused_border_color="#007aff", expand=True
    )
    deck_search = ft.TextField(
        hint_text="Deck suchen...", border_radius=10, bgcolor="#ffffff",
        border_color="#d1d1d6", text_size=13, expand=True, height=40
    )

    model_dd = ft.Dropdown(
        options=[ft.dropdown.Option("Lade...")], value="Lade...",
        border_radius=10, bgcolor="#ffffff", border_color="#d1d1d6", text_size=14,
        focused_border_color="#007aff", expand=True
    )
    model_search = ft.TextField(
        hint_text="Modell suchen...", border_radius=10, bgcolor="#ffffff",
        border_color="#d1d1d6", text_size=13, expand=True, height=40
    )

    prompt_input = ft.TextField(
        hint_text="Prompt... Nutze {frage} als Platzhalter", value=cfg.get('Default_Prompt'),
        multiline=True, min_lines=4, max_lines=6, border_radius=10, bgcolor="#ffffff",
        border_color="#d1d1d6", text_size=14, expand=True
    )

    q_field = ft.TextField(value=cfg.get('Question_Field', 'Text'), border_radius=8,
                           bgcolor="#ffffff", border_color="#d1d1d6", text_size=13, width=150, height=40)
    t_field = ft.TextField(value=cfg.get('Target_Field', 'Extra'), border_radius=8,
                           bgcolor="#ffffff", border_color="#d1d1d6", text_size=13, width=150, height=40)
    fill_dd = ft.Dropdown(
        options=[ft.dropdown.Option(x) for x in ["Überspringen", "Überschreiben", "Anhängen"]],
        value=cfg.get('Fill_Mode', 'Überspringen'), border_radius=8, bgcolor="#ffffff",
        border_color="#d1d1d6", text_size=13, width=150
    )

    cloze_sw = ft.Switch(value=cfg.get('Remove_Cloze') == 'True', active_color="#34c759")
    log_sw = ft.Switch(value=False, active_color="#34c759")

    conc_txt = ft.Text(str(concurrency), size=15, weight=ft.FontWeight.W_600, color="#007aff")
    conc_slider = ft.Slider(value=concurrency, min=1, max=50, divisions=49,
                            active_color="#007aff", inactive_color="#d1d1d6")

    progress = ft.ProgressBar(value=0, bgcolor="#e5e5ea", color="#007aff", bar_height=5)
    progress_txt = ft.Text("0%", size=13, weight=ft.FontWeight.W_600, color="#007aff")

    log_view = ft.ListView(spacing=2, auto_scroll=True, expand=True)

    def log(msg):
        logging.info(msg)
        try:
            log_view.controls.append(ft.Text(f"› {msg}", size=11, color="#8e8e93"))
            if len(log_view.controls) > 100:
                log_view.controls.pop(0)
            page.update()
        except Exception:
            logging.exception("Log-Update in der UI fehlgeschlagen")

    def set_status(txt, col):
        status.value, status.color = f"● {txt}", col
        page.update()

    def persist_settings(e=None):
        cfg.save_ui_values(
            api_input.value,
            prompt_input.value,
            deck_dd.value,
            model_dd.value,
            t_field.value,
            q_field.value,
            fill_dd.value,
            cloze_sw.value,
            concurrency,
        )

    def on_conc(e):
        nonlocal concurrency
        concurrency = int(e.control.value)
        conc_txt.value = str(concurrency)
        persist_settings()
        page.update()
    conc_slider.on_change = on_conc

    def on_log_sw(e):
        global DETAILED_LOGGING
        DETAILED_LOGGING = e.control.value
    log_sw.on_change = on_log_sw

    api_input.on_change = persist_settings
    prompt_input.on_blur = persist_settings
    q_field.on_blur = persist_settings
    t_field.on_blur = persist_settings
    deck_dd.on_change = persist_settings
    model_dd.on_change = persist_settings
    fill_dd.on_change = persist_settings
    cloze_sw.on_change = persist_settings

    def load_models():
        nonlocal all_models
        key = api_input.value.strip() if api_input.value else ""
        if not key:
            log("⚠️ API-Key fehlt")
            return
        persist_settings()
        log("🔄 Modelle...")
        all_models = fetch_models_api(key)
        model_dd.options = [ft.dropdown.Option(m) for m in all_models]
        fav = cfg.get('Favorite_Model')
        model_dd.value = fav if fav in all_models else (all_models[0] if all_models else None)
        persist_settings()
        log(f"✓ {len(all_models)} Modelle")
        page.update()

    def load_decks():
        nonlocal all_decks
        log("🔄 Decks...")
        all_decks = get_deck_names()
        deck_dd.options = [ft.dropdown.Option(d) for d in all_decks]
        fav = cfg.get('Favorite_Deck')
        deck_dd.value = fav if fav in all_decks else (all_decks[0] if all_decks else None)
        persist_settings()
        log(f"✓ {len(all_decks)} Decks")
        page.update()

    def filter_decks(e):
        q = (deck_search.value or "").lower()
        filt = [d for d in all_decks if q in d.lower()] if q else all_decks
        deck_dd.options = [ft.dropdown.Option(d) for d in filt]
        if filt:
            deck_dd.value = filt[0]
            persist_settings()
        page.update()
    deck_search.on_change = filter_decks

    def filter_models(e):
        q = (model_search.value or "").lower()
        filt = [m for m in all_models if q in m.lower()] if q else all_models
        model_dd.options = [ft.dropdown.Option(m) for m in filt]
        if filt:
            model_dd.value = filt[0]
            persist_settings()
        page.update()
    model_search.on_change = filter_models

    def set_fav(e):
        if model_dd.value:
            cfg.set('Favorite_Model', model_dd.value)
            cfg.save()
            log(f"⭐ {model_dd.value[:25]}...")

    def start(e):
        nonlocal cancelled
        d, k, p, m = deck_dd.value, api_input.value, prompt_input.value, model_dd.value
        if not all([d, k, p, m]) or d == "Lade..." or m == "Lade...":
            log("⚠️ Felder ausfüllen!")
            return

        target_field = t_field.value or 'Extra'
        question_field = q_field.value or 'Text'
        fill_mode = fill_dd.value or 'Überspringen'
        if target_field == question_field and fill_mode == "Überspringen":
            set_status("Option prüfen", "#ff9500")
            log("⚠️ Frage-Feld und Ziel-Feld sind gleich. Mit „Überspringen“ wird jede bereits gefüllte Karte übersprungen. Wähle „Überschreiben“ oder ein anderes Ziel-Feld.")
            return

        cancelled = False

        try:
            persist_settings()
        except Exception as ex:
            logging.exception("Einstellungen konnten nicht gespeichert werden")
            set_status("Speichern fehlgeschlagen", "#ff3b30")
            log(f"⚠️ Einstellungen konnten nicht gespeichert werden: {ex}")
            return

        set_status("Läuft...", "#007aff")
        progress.value, progress.color, progress_txt.value = 0, "#007aff", "0%"
        start_btn.disabled, cancel_btn.disabled = True, False
        page.update()

        def run():
            nonlocal cancelled
            def prog(done, total):
                pct = int(done / total * 100)
                progress.value, progress_txt.value = pct / 100, f"{pct}%"
                if done == total:
                    progress.color = "#34c759"
                page.update()

            try:
                stats = AnkiUpdater(log).process(
                    d, k.strip(), m, p.strip(), target_field, question_field, fill_mode, concurrency,
                    prog, lambda: cancelled, max_tokens, temperature, top_p, cloze_sw.value
                )
                if cancelled:
                    set_status("Abgebrochen", "#ff9500")
                elif stats and stats.get("errors"):
                    set_status("Mit Fehlern fertig", "#ff9500")
                else:
                    set_status("Fertig!", "#34c759")
            except Exception as ex:
                logging.error("Startlauf fehlgeschlagen:\n%s", traceback.format_exc())
                log(f"❌ Lauf abgebrochen: {ex}")
                set_status("Fehler", "#ff3b30")
            finally:
                start_btn.disabled, cancel_btn.disabled = False, True
                page.update()

        threading.Thread(target=run, daemon=True).start()

    def cancel(e):
        nonlocal cancelled
        cancelled = True
        log("⏹ Abbruch...")

    start_btn = ft.ElevatedButton("▶  Generieren", bgcolor="#007aff", color="#fff",
                                   style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12),
                                   padding=ft.Padding(28, 14, 28, 14)), on_click=start)
    cancel_btn = ft.OutlinedButton("✕  Abbrechen", disabled=True,
                                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12),
                                    padding=ft.Padding(20, 12, 20, 12), side=ft.BorderSide(1, "#007aff")),
                                    on_click=cancel)

    # === Glass Card Helper ===
    def card(content, title="", icon=""):
        return ft.Container(
            content=ft.Column([
                ft.Row([ft.Text(icon, size=18), ft.Text(title, size=14, weight=ft.FontWeight.W_600, color="#1d1d1f")], spacing=8) if title else ft.Container(),
                ft.Container(height=8) if title else ft.Container(),
                content,
            ], spacing=0),
            bgcolor="#ffffff",
            border_radius=16,
            padding=18,
            shadow=ft.BoxShadow(blur_radius=16, color="#00000010", offset=ft.Offset(0, 4)),
        )

    # === Layout ===
    header = ft.Container(
        content=ft.Row([
            ft.Row([ft.Text("⚡", size=26), ft.Column([
                ft.Text(APP_NAME, size=18, weight=ft.FontWeight.W_700, color="#1d1d1f"),
                ft.Text("macOS Edition", size=10, color="#8e8e93"),
            ], spacing=0)], spacing=10),
            status,
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        padding=ft.Padding(20, 12, 20, 12),
        bgcolor="#ffffff",
        border=ft.Border(bottom=ft.BorderSide(1, "#e5e5ea")),
    )

    scroll_content = ft.Column([
        # Row 1
        ft.ResponsiveRow([
            ft.Container(card(ft.Column([
                ft.Text("API Key", size=11, color="#8e8e93"),
                api_input,
                ft.Container(height=10),
                ft.Text("Deck", size=11, color="#8e8e93"),
                deck_dd,
                deck_search,
            ], spacing=6), "Verbindung", "🔗"), col={"xs": 12, "md": 6}, padding=6),

            ft.Container(card(ft.Column([
                ft.Row([ft.Text("Frage-Feld", size=11, color="#8e8e93", width=70), q_field], spacing=8),
                ft.Row([ft.Text("Ziel-Feld", size=11, color="#8e8e93", width=70), t_field], spacing=8),
                ft.Row([ft.Text("Bei gefüllt", size=11, color="#8e8e93", width=70), fill_dd], spacing=8),
                ft.Divider(height=1, color="#e5e5ea"),
                ft.Row([ft.Text("Cloze entfernen", size=12, color="#1d1d1f", expand=True), cloze_sw]),
                ft.Row([ft.Text("Detail-Log", size=12, color="#1d1d1f", expand=True), log_sw]),
            ], spacing=8), "Felder & Optionen", "⚙️"), col={"xs": 12, "md": 6}, padding=6),
        ]),

        # Row 2
        ft.ResponsiveRow([
            ft.Container(card(ft.Column([
                ft.Text("Modell", size=11, color="#8e8e93"),
                model_dd,
                model_search,
                ft.Container(height=4),
                ft.Row([
                    ft.TextButton("🔄 Laden", on_click=lambda e: threading.Thread(target=load_models, daemon=True).start(),
                                  style=ft.ButtonStyle(padding=ft.Padding(10, 6, 10, 6), bgcolor="#007aff10")),
                    ft.TextButton("⭐ Favorit", on_click=set_fav,
                                  style=ft.ButtonStyle(padding=ft.Padding(10, 6, 10, 6), bgcolor="#007aff10")),
                ], spacing=6),
            ], spacing=6), "KI Modell", "🤖"), col={"xs": 12, "md": 6}, padding=6),

            ft.Container(card(ft.Column([
                ft.Row([ft.Text("Parallele Anfragen", size=12, color="#1d1d1f", expand=True), conc_txt]),
                conc_slider,
            ], spacing=2), "Performance", "⚡"), col={"xs": 12, "md": 6}, padding=6),
        ]),

        # Row 3
        ft.ResponsiveRow([
            ft.Container(card(ft.Column([
                prompt_input,
                ft.Text("{frage} = Platzhalter", size=10, color="#8e8e93"),
            ], spacing=6), "Prompt", "✍️"), col={"xs": 12, "md": 6}, padding=6),

            ft.Container(card(ft.Container(
                content=log_view,
                bgcolor="#f5f5f7",
                border_radius=10,
                padding=10,
                height=120,
            ), "Log", "📋"), col={"xs": 12, "md": 6}, padding=6),
        ]),
    ], spacing=4, scroll=ft.ScrollMode.AUTO)

    footer = ft.Container(
        content=ft.Column([
            ft.Row([ft.Container(content=progress, expand=True), progress_txt], spacing=10),
            ft.Container(height=10),
            ft.Row([ft.Container(expand=True), cancel_btn, ft.Container(width=10), start_btn]),
        ]),
        padding=ft.Padding(20, 14, 20, 16),
        bgcolor="#ffffff",
        border=ft.Border(top=ft.BorderSide(1, "#e5e5ea")),
    )

    page.add(ft.Column([
        header,
        ft.Container(content=scroll_content, expand=True, padding=ft.Padding(10, 6, 10, 6)),
        footer,
    ], spacing=0, expand=True))

    threading.Thread(target=load_decks, daemon=True).start()
    threading.Thread(target=load_models, daemon=True).start()


if __name__ == "__main__":
    ft.app(target=main, name=APP_NAME)
