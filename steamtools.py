import os
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser

DND_AVAILABLE = False
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

def find_steam_path():
    try:
        import winreg
    except Exception:
        return None

    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ["SteamPath", "InstallPath"]),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", ["InstallPath", "SteamPath"]),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ["InstallPath", "SteamPath"]),
    ]

    for hive, subkey, value_names in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for name in value_names:
                    try:
                        val, _ = winreg.QueryValueEx(key, name)
                        if isinstance(val, str) and os.path.isdir(val):
                            return os.path.normpath(val)
                    except OSError:
                        continue
        except OSError:
            continue

    default = r"C:\Program Files (x86)\Steam"
    return default if os.path.isdir(default) else None

_VDF_TOKEN = re.compile(r'"([^"]*)"|(\{)|(\})')

def parse_vdf(text: str):
    tokens = []
    for m in _VDF_TOKEN.finditer(text):
        if m.group(1) is not None:
            tokens.append(("STR", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("{", "{"))
        elif m.group(3) is not None:
            tokens.append(("}", "}"))

    i = 0
    def parse_obj():
        nonlocal i
        obj = {}
        while i < len(tokens):
            t, v = tokens[i]
            if t == "}":
                i += 1
                break
            if t != "STR":
                i += 1
                continue
            key = v
            i += 1
            if i >= len(tokens):
                break
            t2, v2 = tokens[i]
            if t2 == "{":
                i += 1
                obj[key] = parse_obj()
            elif t2 == "STR":
                obj[key] = v2
                i += 1
            else:
                i += 1
        return obj

    root = {}
    while i < len(tokens):
        t, v = tokens[i]
        if t == "STR":
            key = v
            i += 1
            if i < len(tokens) and tokens[i][0] == "{":
                i += 1
                root[key] = parse_obj()
            elif i < len(tokens) and tokens[i][0] == "STR":
                root[key] = tokens[i][1]
                i += 1
        else:
            i += 1
    return root


def read_text_file(path: str):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as f:
                return f.read()
        except Exception:
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def get_library_folders(steam_path: str):
    libs = set()
    libs.add(os.path.normpath(steam_path))

    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if not os.path.isfile(vdf_path):
        return sorted(libs)

    try:
        data = parse_vdf(read_text_file(vdf_path))
        lf = data.get("libraryfolders", {}) if isinstance(data, dict) else {}
        for _, v in lf.items():
            if isinstance(v, dict):
                p = v.get("path")
                if p and os.path.isdir(p):
                    libs.add(os.path.normpath(p))
            elif isinstance(v, str):
                if os.path.isdir(v):
                    libs.add(os.path.normpath(v))
    except Exception:
        pass

    return sorted(libs)


def build_installed_games_index(steam_path: str):
    games = []
    libs = get_library_folders(steam_path)

    for lib in libs:
        steamapps = os.path.join(lib, "steamapps")
        if not os.path.isdir(steamapps):
            continue

        for fn in os.listdir(steamapps):
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue

            acf_path = os.path.join(steamapps, fn)
            try:
                text = read_text_file(acf_path)
                data = parse_vdf(text)
                st = data.get("AppState", {})
                if not isinstance(st, dict):
                    continue

                appid = st.get("appid") or st.get("AppID")
                name = st.get("name") or st.get("Name") or ""
                if not appid:
                    continue

                depots = set()

                def add_depots_from_dict(d):
                    if not isinstance(d, dict):
                        return
                    for depot_id, depot_val in d.items():
                        s = str(depot_id)
                        if s.isdigit():
                            depots.add(s)
                        if isinstance(depot_val, dict):
                            for k2 in depot_val.keys():
                                s2 = str(k2)
                                if s2.isdigit():
                                    depots.add(s2)

                add_depots_from_dict(st.get("InstalledDepots"))
                add_depots_from_dict(st.get("MountedDepots"))

                for key, val in st.items():
                    if isinstance(key, str) and "depots" in key.lower():
                        add_depots_from_dict(val)

                games.append({
                    "appid": str(appid),
                    "name": str(name),
                    "depot_ids": depots,
                    "library": lib,
                    "manifest_path": acf_path
                })
            except Exception:
                continue

    games.sort(key=lambda g: (g.get("name") or "").lower())
    return games


def route_and_copy(files, lua_path, manifest_path):
    copied, skipped, errors = 0, 0, []
    for f in files:
        if not os.path.isfile(f):
            skipped += 1
            continue
        ext = os.path.splitext(f)[1].lower()
        try:
            if ext == ".lua":
                shutil.copy2(f, lua_path)
                copied += 1
            elif ext == ".manifest":
                shutil.copy2(f, manifest_path)
                copied += 1
            else:
                skipped += 1
        except Exception as e:
            errors.append(f"{os.path.basename(f)}: {e}")
    return copied, skipped, errors


def restart_steam_silent(steam_path: str):
    steam_exe = os.path.join(steam_path, "steam.exe")
    if not os.path.isfile(steam_exe):
        return
    try:
        subprocess.Popen([steam_exe, "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)
        subprocess.call(
            ["taskkill", "/F", "/IM", "steam.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=True
        )
        time.sleep(1)
        subprocess.Popen([steam_exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def restart_app():
    try:
        os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception:
        try:
            subprocess.Popen([sys.executable, *sys.argv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        raise SystemExit

class SteamtoolsApp:
    def __init__(self):
        self.steam_path = find_steam_path()
        if not self.steam_path:
            messagebox.showerror("Error", "Steam not found.")
            raise SystemExit

        self.lua_path = os.path.join(self.steam_path, "config", "stplug-in")
        self.manifest_path = os.path.join(self.steam_path, "config", "depotcache")

        for p in (self.lua_path, self.manifest_path):
            if not os.path.isdir(p):
                messagebox.showerror("Error", f"Missing folder:\n{p}")
                raise SystemExit

        self.root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
        self.root.title("Steamtools")
        self.root.geometry("540x520")
        self.root.resizable(False, False)

        self.BG = "#2b2d31"
        self.PANEL = "#1f2125"
        self.CARD = "#26282d"
        self.CARD_HOVER = "#2f3238"
        self.BORDER = "#3a3f46"
        self.TEXT = "#e8e9ec"
        self.MUTED = "#a7abb3"
        self.BTN = "#3a3f46"
        self.BTN_HOVER = "#4a515b"
        self.DANGER = "#b24a4a"
        self.INPUT = "#202227"

        self.root.configure(bg=self.BG)

        self.container = tk.Frame(self.root, bg=self.BG)
        self.container.pack(fill="both", expand=True)

        self.games_index = None

        self.pages = {}
        self._build_pages()
        self.show("menu")

    def btn(self, parent, text, command, danger=False):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=(self.DANGER if danger else self.BTN),
            fg=self.TEXT,
            activebackground=(self.DANGER if danger else self.BTN_HOVER),
            activeforeground=self.TEXT,
            relief="flat",
            bd=0,
            padx=14,
            pady=10,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2"
        )

    def title(self, parent, text):
        tk.Label(parent, text=text, bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 18, "bold")).pack(pady=(14, 10))

    def show(self, page_name: str):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[page_name].pack(fill="both", expand=True)

    def _build_pages(self):
        self.pages["menu"] = self._page_menu(self.container)
        self.pages["upload"] = self._page_upload(self.container)
        self.pages["uninstall"] = self._page_uninstall(self.container)

    def _page_menu(self, parent):
        frame = tk.Frame(parent, bg=self.BG)
        self.title(frame, "Steamtools")

        panel = tk.Frame(frame, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        panel.pack(padx=26, pady=18, fill="x")
        panel.configure(height=360)
        panel.pack_propagate(False)

        wrap = tk.Frame(panel, bg=self.PANEL)
        wrap.pack(expand=True)

        # NEW: Generator button on top
        self.btn(wrap, "Generator", lambda: webbrowser.open("https://manifestluagenerator.org/")).pack(pady=10, ipadx=30)

        self.btn(wrap, "Manifest & Lua", lambda: self.show("upload")).pack(pady=10, ipadx=30)
        self.btn(wrap, "Uninstall", lambda: self.show("uninstall")).pack(pady=10, ipadx=30)
        self.btn(wrap, "Restart", restart_app).pack(pady=10, ipadx=30)

        return frame

    def _page_upload(self, parent):
        frame = tk.Frame(parent, bg=self.BG)
        self.title(frame, "Steamtools")

        panel = tk.Frame(frame, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        panel.pack(padx=26, pady=10, fill="x")
        panel.configure(height=240)
        panel.pack_propagate(False)

        card = tk.Frame(panel, bg=self.CARD, width=440, height=130,
                        highlightthickness=1, highlightbackground=self.BORDER)
        card.pack(padx=24, pady=35)
        card.pack_propagate(False)

        t = tk.Label(card, text="Select File", bg=self.CARD, fg=self.TEXT,
                     font=("Segoe UI", 16, "bold"))
        t.place(relx=0.5, rely=0.44, anchor="center")

        s = tk.Label(card, text=".lua & .manifest", bg=self.CARD, fg=self.MUTED,
                     font=("Segoe UI", 10))
        s.place(relx=0.5, rely=0.70, anchor="center")

        status_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=status_var, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(pady=(6, 0))

        def run_copy(files):
            if not files:
                return
            copied, skipped, errors = route_and_copy(files, self.lua_path, self.manifest_path)
            status_var.set(f"Copied: {copied} | Skipped: {skipped}")
            if errors:
                messagebox.showerror("Error", "Some files failed to copy.")

        def browse():
            files = filedialog.askopenfilenames(
                filetypes=[("Lua & Manifest", "*.lua *.manifest"), ("All", "*.*")]
            )
            run_copy(files)

        def on_enter(_):
            card.configure(bg=self.CARD_HOVER)
            t.configure(bg=self.CARD_HOVER)
            s.configure(bg=self.CARD_HOVER)

        def on_leave(_):
            card.configure(bg=self.CARD)
            t.configure(bg=self.CARD)
            s.configure(bg=self.CARD)

        for w in (card, t, s):
            w.bind("<Button-1>", lambda e: browse())
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.configure(cursor="hand2")

        btn_row = tk.Frame(frame, bg=self.BG)
        btn_row.pack(side="bottom", pady=14)

        self.btn(btn_row, "Back", lambda: self.show("menu")).pack(side="left", padx=8)
        self.btn(btn_row, "Browse…", browse).pack(side="left", padx=8)
        self.btn(btn_row, "Restart Steam", lambda: restart_steam_silent(self.steam_path)).pack(side="left", padx=8)

        return frame

    def _page_uninstall(self, parent):
        frame = tk.Frame(parent, bg=self.BG)
        self.title(frame, "Steamtools")

        panel = tk.Frame(frame, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        panel.pack(padx=26, pady=10, fill="both", expand=True)

        current_type = tk.StringVar(value="lua")
        selected_game = {"appid": None, "name": None, "depot_ids": set()}

        # --- Top row: tabs + clear selection ---
        topbar = tk.Frame(panel, bg=self.PANEL)
        topbar.pack(fill="x", padx=16, pady=(16, 10))

        tabs = tk.Frame(topbar, bg=self.PANEL)
        tabs.pack(side="left")

        lua_tab = tk.Label(
            tabs, text="Lua", bg=self.CARD, fg=self.TEXT,
            font=("Segoe UI", 10, "bold"), padx=12, pady=8, cursor="hand2"
        )
        lua_tab.pack(side="left")

        man_tab = tk.Label(
            tabs, text="Manifest", bg=self.PANEL, fg=self.MUTED,
            font=("Segoe UI", 10, "bold"), padx=12, pady=8, cursor="hand2"
        )
        man_tab.pack(side="left", padx=(8, 0))

        def clear_game_selection():
            selected_game["appid"] = None
            selected_game["name"] = None
            selected_game["depot_ids"] = set()
            try:
                game_list.selection_clear(0, "end")
            except Exception:
                pass
            refresh_files_list()

        clear_btn = self.btn(topbar, "Clear", clear_game_selection)
        clear_btn.configure(padx=10, pady=8, font=("Segoe UI", 9, "bold"))
        clear_btn.pack(side="right")

        # --- Game list frame (scrollable) ---
        game_list_frame = tk.Frame(panel, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        game_list_frame.pack(padx=16, pady=(0, 10), fill="x")
        game_list_frame.configure(height=110)
        game_list_frame.pack_propagate(False)

        game_scroll = tk.Scrollbar(game_list_frame)
        game_scroll.pack(side="right", fill="y")

        game_list = tk.Listbox(
            game_list_frame,
            height=5,
            selectmode="browse",
            bg=self.INPUT,
            fg=self.TEXT,
            highlightthickness=0,
            relief="flat",
            activestyle="none",
            selectbackground=self.CARD_HOVER,
            selectforeground=self.TEXT,
            yscrollcommand=game_scroll.set
        )
        game_list.pack(side="left", fill="both", expand=True)
        game_scroll.config(command=game_list.yview)

        lb_frame = tk.Frame(panel, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        lb_frame.pack(padx=16, pady=0, fill="both", expand=True)

        scrollbar = tk.Scrollbar(lb_frame)
        scrollbar.pack(side="right", fill="y")

        files_list = tk.Listbox(
            lb_frame,
            selectmode="browse",
            bg=self.CARD,
            fg=self.TEXT,
            highlightthickness=0,
            relief="flat",
            activestyle="none",
            selectbackground=self.CARD_HOVER,
            selectforeground=self.TEXT,
            yscrollcommand=scrollbar.set
        )
        files_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=files_list.yview)

        status_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=status_var, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(pady=(6, 0))

        # --- Action row inside panel: Uninstall + Back + Restart Steam ---
        action_row = tk.Frame(panel, bg=self.PANEL)
        action_row.pack(fill="x", padx=16, pady=(10, 16))

        def ensure_games_index():
            if self.games_index is None:
                status_var.set("Indexing installed games…")
                frame.update_idletasks()
                self.games_index = build_installed_games_index(self.steam_path)
            return self.games_index

        def list_lua_files_filtered():
            try:
                all_lua = sorted([f for f in os.listdir(self.lua_path) if f.lower().endswith(".lua")])
            except Exception:
                all_lua = []

            if selected_game["appid"]:
                target = f"{selected_game['appid']}.lua"
                if target in all_lua:
                    return [target]
                return all_lua

            return all_lua

        def list_manifest_files_filtered():
          
            try:
                all_m = sorted([f for f in os.listdir(self.manifest_path) if f.lower().endswith(".manifest")])
            except Exception:
                all_m = []

            depots = selected_game["depot_ids"]
            if not depots:
                return all_m

            out = []
            for fn in all_m:
                m = re.match(r"^(\d+)", fn)
                if m and m.group(1) in depots:
                    out.append(fn)

            if not out:
                return all_m
            return out

        def refresh_files_list():
            files_list.delete(0, "end")
            if current_type.get() == "lua":
                items = list_lua_files_filtered()
            else:
                items = list_manifest_files_filtered()

            for x in items:
                files_list.insert("end", x)

            if current_type.get() == "manifest" and selected_game["name"]:
                status_var.set(f"Found: {len(items)} (filtered by: {selected_game['name']})")
            elif current_type.get() == "manifest":
                status_var.set(f"Found: {len(items)}")
            else:
                status_var.set(f"Found: {len(items)}")

        def refresh_game_list():
          
            games = ensure_games_index()

            game_list.delete(0, "end")
            matches = []

            for g in games:
                name = (g.get("name") or "").strip()
                if not name:
                    continue

                appid = str(g.get("appid") or "")
                if not appid:
                    continue

                has_lua = os.path.isfile(os.path.join(self.lua_path, f"{appid}.lua"))
                if not has_lua:
                    continue

                matches.append(g)

            matches = matches[:400]
            game_list._matches = matches
            for g in matches:
                game_list.insert("end", g["name"])

        def select_game_from_list(_evt=None):
            sel = game_list.curselection()
            if not sel:
                return
            idx = sel[0]
            matches = getattr(game_list, "_matches", [])
            if idx >= len(matches):
                return
            g = matches[idx]
            selected_game["appid"] = g["appid"]
            selected_game["name"] = g["name"]
            selected_game["depot_ids"] = set(g.get("depot_ids", set()))
            refresh_files_list()

        def set_tab(which: str):
            current_type.set(which)
            if which == "lua":
                lua_tab.configure(bg=self.CARD, fg=self.TEXT)
                man_tab.configure(bg=self.PANEL, fg=self.MUTED)
            else:
                man_tab.configure(bg=self.CARD, fg=self.TEXT)
                lua_tab.configure(bg=self.PANEL, fg=self.MUTED)
            refresh_files_list()

        def uninstall_selected_one():
            sel = files_list.curselection()
            if not sel:
                status_var.set("Select a file first.")
                return

            name = files_list.get(sel[0])
            folder = self.lua_path if current_type.get() == "lua" else self.manifest_path
            path = os.path.join(folder, name)

            try:
                if not os.path.exists(path):
                    status_var.set("File not found.")
                    return
                os.remove(path)
                status_var.set(f"Removed: {name}")
            except Exception as e:
                status_var.set(f"Failed: {e}")

            refresh_files_list()
            refresh_game_list()

        lua_tab.bind("<Button-1>", lambda e: set_tab("lua"))
        man_tab.bind("<Button-1>", lambda e: set_tab("manifest"))
        game_list.bind("<<ListboxSelect>>", select_game_from_list)

        self.btn(action_row, "Uninstall", uninstall_selected_one, danger=True).pack(side="left")
        self.btn(action_row, "Back", lambda: self.show("menu")).pack(side="left", padx=8)
        self.btn(action_row, "Restart Steam", lambda: restart_steam_silent(self.steam_path)).pack(side="left", padx=8)

        set_tab("lua")
        refresh_game_list()
        refresh_files_list()

        return frame

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    SteamtoolsApp().run()


