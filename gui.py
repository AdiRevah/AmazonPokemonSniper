from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter.messagebox
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "executable")).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - depends on local runtime
    vendored = BASE_DIR / "_internal"
    if vendored.exists():
        sys.path.insert(0, str(vendored))
        import customtkinter as ctk
    else:
        raise

from bot import SniperBot
from config import APP_NAME, SITES, VERSION

LOG_FILE = BASE_DIR / "bot.log"
SETTINGS_FILE = BASE_DIR / "settings.json"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

AMAZON_ORANGE = "#FF9900"
POKEMON_BLUE = "#2A75BB"
ACCENT_YELLOW = "#FFCB05"
ACCENT_GREEN = "#00E676"
BG_DARK = "#0B0E14"
BG_SIDEBAR = "#0F1219"
BG_CARD = "#151921"
TEXT_PRI = "#FFFFFF"
TEXT_SEC = "#8E97A4"
FONT_TITLE = ("Segoe UI", 26, "bold")
FONT_BODY = ("Segoe UI", 13)
FONT_MONO = ("Consolas", 11)


class TaskRow(ctk.CTkFrame):
    def __init__(
        self,
        master,
        task_key: str,
        type_label: str,
        query: str,
        status: str,
        index: int,
        on_remove,
        quantity: int = 1,
        max_price: float = 0.0,
        free_shipping: bool = False,
        stop_after_purchase: bool = False,
        **kw,
    ) -> None:
        bg = "#1A1F29" if index % 2 == 0 else "#151921"
        super().__init__(master, fg_color=bg, corner_radius=10, **kw)
        self.task_key = task_key
        self.columnconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text=type_label,
            font=("Segoe UI", 9, "bold"),
            text_color="white",
            fg_color=POKEMON_BLUE if type_label == "KEYWORDS" else "#455A64",
            corner_radius=6,
            width=70,
            height=24,
        ).grid(row=0, column=0, padx=12, pady=10)

        meta_text = f"QTY:{quantity} | ${max_price}"
        if free_shipping:
            meta_text += " | Free Ship"
        if stop_after_purchase:
            meta_text += " | Stop on Buy"

        ctk.CTkLabel(
            self,
            text=meta_text,
            font=("Segoe UI", 9, "bold"),
            text_color=ACCENT_YELLOW,
            fg_color="#1F2430",
            corner_radius=6,
            width=120,
            height=24,
        ).grid(row=0, column=1, padx=(0, 8), pady=10)

        short_query = query if len(query) <= 45 else f"{query[:42]}..."
        ctk.CTkLabel(
            self,
            text=short_query,
            font=FONT_BODY,
            text_color=TEXT_PRI,
            anchor="w",
        ).grid(row=0, column=2, padx=5, pady=10, sticky="ew")

        self.status_var = ctk.StringVar(value=status)
        self._status_lbl = ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            font=("Segoe UI", 12, "bold"),
            anchor="e",
            width=140,
        )
        self._status_lbl.grid(row=0, column=3, padx=8, pady=10, sticky="e")
        self.update_color(status)

        ctk.CTkButton(
            self,
            text="x",
            width=28,
            height=28,
            fg_color="#2A1F1F",
            hover_color="#5D1B1B",
            text_color="#FF6B6B",
            font=("Segoe UI", 11, "bold"),
            corner_radius=6,
            command=lambda: on_remove(task_key),
        ).grid(row=0, column=4, padx=12, pady=10)

    def update_status(self, status: str) -> None:
        self.status_var.set(status)
        self.update_color(status)

    def update_color(self, status: str) -> None:
        lowered = status.lower()
        if "success" in lowered or "active" in lowered or "running" in lowered:
            color = ACCENT_GREEN
        elif "queue" in lowered or "search" in lowered or "check" in lowered:
            color = ACCENT_YELLOW
        elif "captcha" in lowered or "warning" in lowered:
            color = AMAZON_ORANGE
        else:
            color = TEXT_SEC
        self._status_lbl.configure(text_color=color)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} - ULTIMATE EDITION")
        self.geometry("1200x880")
        self.configure(fg_color=BG_DARK)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._rows: dict[str, TaskRow] = {}
        self._bot_running = False
        self._active_tab = "dashboard"
        self._log_rid = None
        self._site_key = "amazon_us"

        self.bot = SniperBot(self._on_log, self._on_status)
        self._load_settings()
        self._build_sidebar()
        self._build_content_area()
        self.select_tab("dashboard")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_settings(self) -> None:
        self._refresh_interval_var = ctk.StringVar(value="2")
        self._confirm_checkout_var = ctk.BooleanVar(value=True)
        self._email_vars = {
            "smtp_server": ctk.StringVar(),
            "smtp_port": ctk.StringVar(value="587"),
            "email": ctk.StringVar(),
            "password": ctk.StringVar(),
            "target_email": ctk.StringVar(),
        }

        if not SETTINGS_FILE.exists():
            return

        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return

        for key, variable in self._email_vars.items():
            if key in data:
                variable.set(data[key])

        self._refresh_interval_var.set(data.get("refresh_interval", "2"))
        self.bot.refresh_interval = float(self._refresh_interval_var.get())
        self._confirm_checkout_var.set(data.get("confirm_checkout", True))

    def _save_settings(self) -> None:
        data = {key: variable.get().strip() for key, variable in self._email_vars.items()}
        data["refresh_interval"] = self._refresh_interval_var.get().strip()
        data["confirm_checkout"] = self._confirm_checkout_var.get()

        try:
            SETTINGS_FILE.write_text(json.dumps(data))
            self.bot.refresh_interval = float(data["refresh_interval"])
            tkinter.messagebox.showinfo("Settings", "Settings saved successfully!")
        except Exception as exc:
            tkinter.messagebox.showerror("Error", f"Failed to save: {exc}")

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, fg_color=BG_SIDEBAR, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(10, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            sidebar,
            text="SNIPER",
            font=FONT_TITLE,
            text_color=AMAZON_ORANGE,
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(30, 0))
        ctk.CTkLabel(
            sidebar,
            text=" PRO",
            font=("Segoe UI", 20, "bold"),
            text_color=POKEMON_BLUE,
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(0, 20))

        nav_items = [
            ("dashboard", "Dashboard"),
            ("accounts", "Accounts"),
            ("settings", "Settings"),
            ("logs", "Logs"),
        ]
        self.nav_btns = {}
        for tid, label in nav_items:
            button = ctk.CTkButton(
                sidebar,
                text=label,
                fg_color="transparent",
                hover_color="#1F2430",
                text_color=TEXT_SEC,
                anchor="w",
                command=lambda value=tid: self.select_tab(value),
            )
            button.pack(fill="x", padx=16, pady=6)
            self.nav_btns[tid] = button

        self.sidebar_status_lbl = ctk.CTkLabel(
            sidebar,
            text="● Bot Stopped",
            text_color="#FF5252",
            anchor="w",
        )
        self.sidebar_status_lbl.pack(side="bottom", fill="x", padx=24, pady=24)

    def _open_chrome(self) -> None:
        from open_profile import open_chrome_with_profile

        url = SITES[self._site_key]["base_url"]
        threading.Thread(
            target=open_chrome_with_profile,
            args=(url,),
            daemon=True,
        ).start()

    def _build_content_area(self) -> None:
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=0, column=1, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        self.tabs = {
            "dashboard": self._build_dashboard_tab(container),
            "accounts": self._build_accounts_tab(container),
            "settings": self._build_settings_tab(container),
            "logs": self._build_logs_tab(container),
        }

    def _build_dashboard_tab(self, container):
        frame = ctk.CTkFrame(container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(frame, text="Dashboard", font=FONT_TITLE, text_color=TEXT_PRI, anchor="w").grid(
            row=0, column=0, padx=24, pady=(24, 12), sticky="w"
        )

        top = ctk.CTkFrame(frame, fg_color="#232936", corner_radius=14)
        top.grid(row=1, column=0, padx=24, pady=(0, 14), sticky="ew")
        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top,
            text="Add Sniper Task",
            font=("Segoe UI", 18, "bold"),
            text_color=TEXT_PRI,
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")

        self._task_type_var = ctk.StringVar(value="URL")
        ctk.CTkSegmentedButton(
            top,
            values=["URL", "KEYWORDS"],
            variable=self._task_type_var,
            selected_color=POKEMON_BLUE,
        ).grid(row=1, column=0, padx=18, pady=(0, 12), sticky="w")

        self._query_var = ctk.StringVar()
        query_entry = ctk.CTkEntry(
            top,
            textvariable=self._query_var,
            placeholder_text="Enter Amazon URL or Search Keywords...",
        )
        query_entry.grid(row=2, column=0, padx=18, pady=(0, 12), sticky="ew")
        query_entry.bind("<Return>", lambda _event: self._add_task())

        controls = ctk.CTkFrame(top, fg_color="#0B0E14", corner_radius=12)
        controls.grid(row=3, column=0, padx=18, pady=(0, 16), sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(3, weight=1)
        controls.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(controls, text="Neg. Keywords:", text_color=TEXT_SEC, anchor="w").grid(
            row=0, column=0, padx=14, pady=14, sticky="w"
        )
        self._neg_query_var = ctk.StringVar()
        ctk.CTkEntry(controls, textvariable=self._neg_query_var, placeholder_text="exclude, term").grid(
            row=0, column=1, padx=(0, 12), pady=14, sticky="ew"
        )

        ctk.CTkLabel(controls, text="Max Price:", text_color=TEXT_SEC, anchor="w").grid(
            row=0, column=2, padx=(0, 8), pady=14, sticky="e"
        )
        self._max_price_var = ctk.StringVar(value="0")
        ctk.CTkEntry(controls, textvariable=self._max_price_var, justify="center", width=90).grid(
            row=0, column=3, padx=(0, 12), pady=14, sticky="ew"
        )

        ctk.CTkLabel(controls, text="Qty:", text_color=TEXT_SEC, anchor="w").grid(
            row=0, column=4, padx=(0, 8), pady=14, sticky="e"
        )
        self._qty_var = ctk.StringVar(value="1")
        ctk.CTkEntry(controls, textvariable=self._qty_var, justify="center", width=70).grid(
            row=0, column=5, padx=(0, 12), pady=14, sticky="ew"
        )

        ctk.CTkLabel(controls, text="Refresh:", text_color=TEXT_SEC, anchor="w").grid(
            row=1, column=0, padx=14, pady=(0, 14), sticky="w"
        )
        ctk.CTkEntry(controls, textvariable=self._refresh_interval_var, justify="center", width=70).grid(
            row=1, column=1, padx=(0, 12), pady=(0, 14), sticky="w"
        )

        ctk.CTkCheckBox(
            controls,
            text="Confirm Buy",
            variable=self._confirm_checkout_var,
            text_color=TEXT_PRI,
        ).grid(row=1, column=2, padx=(0, 12), pady=(0, 14), sticky="w")

        self._free_shipping_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            controls,
            text="Free Ship Only",
            variable=self._free_shipping_var,
            text_color=TEXT_PRI,
        ).grid(row=1, column=3, padx=(0, 12), pady=(0, 14), sticky="w")

        self._stop_after_purchase_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            controls,
            text="Stop After Buy",
            variable=self._stop_after_purchase_var,
            text_color=TEXT_PRI,
        ).grid(row=1, column=4, padx=(0, 12), pady=(0, 14), sticky="w")

        ctk.CTkButton(
            controls,
            text="+ Add Task",
            fg_color="#C0581A",
            hover_color=AMAZON_ORANGE,
            command=self._add_task,
        ).grid(row=1, column=5, padx=14, pady=(0, 14), sticky="e")

        self._toggle_btn = ctk.CTkButton(
            top,
            text="START SNIPER",
            fg_color="#1B6E36",
            hover_color="#14542A",
            command=self._toggle_bot,
        )
        self._toggle_btn.grid(row=4, column=0, padx=18, pady=(0, 18), sticky="ew")

        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.grid(row=3, column=0, padx=24, pady=(0, 24), sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self._scroll = ctk.CTkScrollableFrame(body, fg_color="#232936", corner_radius=14)
        self._scroll.grid(row=0, column=0, padx=(0, 12), sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)

        log_card = ctk.CTkFrame(body, fg_color="#232936", corner_radius=14)
        log_card.grid(row=0, column=1, sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_card,
            text="Live Sniper Activity",
            font=("Segoe UI", 18, "bold"),
            text_color=TEXT_PRI,
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="ew")

        self._log_box = ctk.CTkTextbox(log_card, fg_color="#05070A", font=FONT_MONO)
        self._log_box.grid(row=1, column=0, padx=18, pady=(0, 18), sticky="nsew")
        self._log_box.configure(state="disabled")
        return frame

    def _build_settings_tab(self, container):
        frame = ctk.CTkFrame(container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="Email Settings", font=FONT_TITLE, text_color=TEXT_PRI, anchor="w").grid(
            row=0, column=0, padx=24, pady=(24, 12), sticky="w"
        )

        card = ctk.CTkFrame(frame, fg_color="#232936", corner_radius=14)
        card.grid(row=1, column=0, padx=24, pady=(0, 24), sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            card,
            text="Email Alerts Configuration",
            text_color=TEXT_PRI,
            font=("Segoe UI", 18, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, padx=18, pady=(16, 12), sticky="ew")

        fields = [
            ("smtp_server", "SMTP Server"),
            ("smtp_port", "SMTP Port"),
            ("email", "Email"),
            ("password", "Password"),
            ("target_email", "Target Email"),
        ]
        for row, (field, label) in enumerate(fields, start=1):
            ctk.CTkLabel(card, text=label, text_color=TEXT_SEC, anchor="w").grid(
                row=row, column=0, padx=18, pady=8, sticky="w"
            )
            entry_kwargs = {"textvariable": self._email_vars[field]}
            if field == "password":
                entry_kwargs["show"] = "*"
            entry = ctk.CTkEntry(card, **entry_kwargs)
            entry.grid(row=row, column=1, padx=(0, 18), pady=8, sticky="ew")

        ctk.CTkButton(
            card,
            text="Save Settings",
            fg_color="#1B6E36",
            hover_color="#14542A",
            command=self._save_settings,
        ).grid(row=len(fields) + 1, column=0, columnspan=2, padx=18, pady=(12, 18), sticky="ew")
        return frame

    def _build_accounts_tab(self, container):
        frame = ctk.CTkFrame(container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="Amazon Account", font=FONT_TITLE, text_color=TEXT_PRI, anchor="w").grid(
            row=0, column=0, padx=24, pady=(24, 12), sticky="w"
        )

        card = ctk.CTkFrame(frame, fg_color="#232936", corner_radius=14)
        card.grid(row=1, column=0, padx=24, pady=(0, 24), sticky="ew")

        ctk.CTkLabel(
            card,
            text="Initial Version: Uses active Chrome Profile sessions.",
            text_color=TEXT_SEC,
            anchor="w",
        ).pack(fill="x", padx=18, pady=(18, 12))
        ctk.CTkButton(
            card,
            text="Open Amazon to Login",
            fg_color="#C0581A",
            hover_color=AMAZON_ORANGE,
            command=self._open_chrome,
        ).pack(anchor="w", padx=18, pady=(0, 18))
        return frame

    def _build_logs_tab(self, container):
        frame = ctk.CTkFrame(container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="ew")

        ctk.CTkLabel(tools, text="Activity Logs", font=FONT_TITLE, text_color=TEXT_PRI).pack(side="left")
        ctk.CTkButton(tools, text="Refresh", fg_color="#1A2030", command=self._refresh_logs).pack(
            side="right", padx=(8, 0)
        )
        ctk.CTkButton(
            tools,
            text="Clear",
            fg_color="#2A1F1F",
            hover_color="#FF6B6B",
            command=self._clear_logs,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            tools,
            text="Open",
            fg_color="#1A2744",
            hover_color="#7EB3FF",
            command=self._open_log,
        ).pack(side="right")

        self._full_log = ctk.CTkTextbox(frame, fg_color="#05070A", font=FONT_MONO)
        self._full_log.grid(row=1, column=0, padx=24, pady=(0, 24), sticky="nsew")
        self._full_log.configure(state="disabled")
        return frame

    def select_tab(self, tid: str) -> None:
        self._active_tab = tid
        for tab_id, frame in self.tabs.items():
            if tab_id == tid:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()

        for tab_id, button in self.nav_btns.items():
            button.configure(
                fg_color="#1F2430" if tab_id == tid else "transparent",
                text_color=TEXT_PRI if tab_id == tid else TEXT_SEC,
            )

        if tid == "logs":
            self._refresh_logs()
            self._start_log_refresh()
        else:
            self._stop_log_refresh()

    def _refresh_logs(self) -> None:
        try:
            content = LOG_FILE.read_text("utf-8") if LOG_FILE.exists() else "No log yet."
            self._full_log.configure(state="normal")
            self._full_log.delete("1.0", "end")
            self._full_log.insert("end", content)
            self._full_log.see("end")
            self._full_log.configure(state="disabled")
        except Exception:
            return

    def _clear_logs(self) -> None:
        if not tkinter.messagebox.askyesno("Clear Logs", "Clear the log file?"):
            return

        try:
            LOG_FILE.write_text("", "utf-8")
            self._refresh_logs()
            self._log_box.configure(state="normal")
            self._log_box.delete("1.0", "end")
            self._log_box.configure(state="disabled")
        except Exception:
            return

    def _open_log(self) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_FILE))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(LOG_FILE)])
            else:
                subprocess.Popen(["xdg-open", str(LOG_FILE)])
        except Exception:
            return

    def _start_log_refresh(self) -> None:
        self._stop_log_refresh()

        def loop() -> None:
            self._refresh_logs()
            self._log_rid = self.after(1500, loop)

        loop()

    def _stop_log_refresh(self) -> None:
        if self._log_rid:
            self.after_cancel(self._log_rid)
            self._log_rid = None

    def _add_task(self) -> None:
        query = self._query_var.get().strip()
        if not query:
            return

        task_type = self._task_type_var.get()
        if task_type == "URL" and not query.startswith(("http://", "https://")):
            tkinter.messagebox.showerror(
                "Invalid URL",
                "For URL tasks, the input must start with 'http://' or 'https://'.\n\n"
                "If you want to search, switch the task type to 'KEYWORDS'.",
            )
            return

        try:
            qty = int(self._qty_var.get())
        except Exception:
            qty = 1

        try:
            max_price = float(self._max_price_var.get())
        except Exception:
            max_price = 0.0

        task_id = f"task_{len(self._rows)}"
        task = {
            "key": task_id,
            "type": task_type,
            "query": query,
            "negative": self._neg_query_var.get().strip(),
            "quantity": qty,
            "max_price": max_price,
            "free_shipping": self._free_shipping_var.get(),
            "stop_after_purchase": self._stop_after_purchase_var.get(),
        }
        self.bot.tasks.append(task)

        row = TaskRow(
            self._scroll,
            task_id,
            task_type,
            query,
            "Queued",
            len(self._rows),
            self._remove_task,
            quantity=qty,
            max_price=max_price,
            free_shipping=self._free_shipping_var.get(),
            stop_after_purchase=self._stop_after_purchase_var.get(),
        )
        row.grid(row=len(self._rows), column=0, sticky="ew", padx=10, pady=5)
        self._rows[task_id] = row

        self._query_var.set("")
        self._neg_query_var.set("")
        self._max_price_var.set("0")
        self._qty_var.set("1")
        self._free_shipping_var.set(False)
        self._stop_after_purchase_var.set(True)

    def _remove_task(self, key: str) -> None:
        if key in self._rows:
            self._rows[key].destroy()
            del self._rows[key]

        self.bot.tasks = [task for task in self.bot.tasks if task["key"] != key]

    def _toggle_bot(self) -> None:
        if not self._bot_running:
            if not self.bot.tasks:
                tkinter.messagebox.showwarning("Warning", "Add at least one task first!")
                return

            self.bot.refresh_interval = float(self._refresh_interval_var.get())
            email_cfg = {key: value.get().strip() for key, value in self._email_vars.items()}
            email_cfg["smtp_port"] = int(email_cfg["smtp_port"] or 587)
            self.bot.set_email_config(email_cfg)

            if self._confirm_checkout_var.get():
                self.bot.confirm_callback = self._confirm_purchase
            else:
                self.bot.confirm_callback = None

            self.bot.start(self.bot.tasks, self._site_key)
            self._bot_running = True
            self._toggle_btn.configure(
                text="STOP SNIPER",
                fg_color="#5D1B1B",
                hover_color="#3D1212",
            )
            self.sidebar_status_lbl.configure(text="● SNIPER ACTIVE", text_color=ACCENT_GREEN)
            return

        self.bot.stop()
        self._bot_running = False
        self._toggle_btn.configure(
            text="START SNIPER",
            fg_color="#1B6E36",
            hover_color="#14542A",
        )
        self.sidebar_status_lbl.configure(text="● Bot Stopped", text_color="#FF5252")

    def _on_log(self, line: str) -> None:
        self.after(0, lambda text=line: self._log_append(text))

    def _log_append(self, line: str) -> None:
        if hasattr(self, "_log_box") and self._log_box.winfo_exists():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", line + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")

        if self._active_tab == "logs":
            self._refresh_logs()

    def _on_status(self, key: str, status: str) -> None:
        self.after(0, lambda: self._rows[key].update_status(status) if key in self._rows else None)

    def _confirm_purchase(self, msg: str) -> bool:
        return tkinter.messagebox.askyesno("Confirm Order", msg)

    def _on_close(self) -> None:
        self._stop_log_refresh()
        if self._bot_running:
            self.bot.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
