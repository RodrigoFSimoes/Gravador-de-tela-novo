import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

# Coordenadas reais em pixels (evita erro de escala do Windows)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
FPS = 15
AUDIO_KBPS = 64
NO_MIC = "(sem microfone)"


def ffmpeg_exe():
    folders = []
    if getattr(sys, "frozen", False):
        folders.append(os.path.dirname(sys.executable))
    folders.append(os.path.dirname(os.path.abspath(__file__)))
    for f in folders:
        p = os.path.join(f, "ffmpeg.exe")
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg") or "ffmpeg"


def run_ffmpeg_text(args):
    return subprocess.run(
        [ffmpeg_exe(), "-hide_banner"] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )


def list_mics():
    try:
        out = run_ffmpeg_text(["-list_devices", "true", "-f", "dshow", "-i", "dummy"]).stderr
    except Exception:
        return []
    names = re.findall(r'"([^"]+)"\s+\(audio', out)
    if not names:  # formato antigo do ffmpeg
        in_audio = False
        for line in out.splitlines():
            if "DirectShow audio devices" in line:
                in_audio = True
                continue
            if "DirectShow video devices" in line:
                in_audio = False
                continue
            if in_audio and "Alternative name" not in line:
                m = re.search(r'"([^"]+)"', line)
                if m:
                    names.append(m.group(1))
    unique = []
    for n in names:
        if n not in unique:
            unique.append(n)
    return unique


def get_duration(path):
    out = run_ffmpeg_text(["-i", path]).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


class AreaSelector:
    """Tela escurecida onde você arrasta o retângulo a gravar."""

    def __init__(self, root, callback):
        u = ctypes.windll.user32
        x, y, w, h = u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        self.callback = callback
        self.start = None
        self.rect = None
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.3)
        self.top.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self.top.configure(bg="black")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.focus_force()

    def on_press(self, e):
        self.start = (e.x_root, e.y_root, e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3, fill="white")

    def on_move(self, e):
        if self.start:
            self.canvas.coords(self.rect, self.start[2], self.start[3], e.x, e.y)

    def on_release(self, e):
        if not self.start:
            return
        x1, y1 = self.start[0], self.start[1]
        x2, y2 = e.x_root, e.y_root
        self.top.destroy()
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        w -= w % 2
        h -= h % 2
        if w >= 64 and h >= 64:
            self.callback(x, y, w, h)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gravador Compacto")
        self.root.geometry("420x330")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        u = ctypes.windll.user32
        self.region = (0, 0, u.GetSystemMetrics(0) & ~1, u.GetSystemMetrics(1) & ~1)
        self.tmp = None
        self.segments = []
        self.proc = None
        self.log = None
        self.state = "idle"  # idle | recording | paused | working
        self.acc = 0.0
        self.seg_start = 0.0
        self.out_dir = os.path.join(os.path.expanduser("~"), "Videos")
        os.makedirs(self.out_dir, exist_ok=True)

        pad = {"padx": 12, "pady": 4}
        ttk.Label(self.root, text="Microfone").pack(anchor="w", **pad)
        self.mic = ttk.Combobox(self.root, state="readonly", width=52)
        self.mic.pack(**pad)
        self.refresh_mics()

        self.area_lbl = ttk.Label(self.root, text="")
        self.area_lbl.pack(anchor="w", **pad)
        row = ttk.Frame(self.root)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="Selecionar área", command=self.pick_area).pack(side="left")
        ttk.Button(row, text="Tela cheia", command=self.full_screen).pack(side="left", padx=6)
        ttk.Button(row, text="Atualizar mics", command=self.refresh_mics).pack(side="left")

        row2 = ttk.Frame(self.root)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Limite (MB):").pack(side="left")
        self.limit = tk.IntVar(value=5)
        ttk.Spinbox(row2, from_=1, to=500, width=5, textvariable=self.limit, command=self.update_hint).pack(side="left", padx=6)
        ttk.Button(row2, text="Pasta de saída", command=self.pick_dir).pack(side="left", padx=6)

        self.hint = ttk.Label(self.root, text="", foreground="gray")
        self.hint.pack(anchor="w", **pad)

        self.timer = ttk.Label(self.root, text="00:00", font=("Segoe UI", 22, "bold"))
        self.timer.pack(**pad)

        row3 = ttk.Frame(self.root)
        row3.pack(**pad)
        self.btn_rec = ttk.Button(row3, text="● Gravar", command=self.start)
        self.btn_rec.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(row3, text="❚❚ Pausar", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(row3, text="■ Parar e salvar", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        self.status = ttk.Label(self.root, text="Pronto.")
        self.status.pack(anchor="w", **pad)

        self.update_area_label()
        self.update_hint()
        self.root.update()
        self.hide_from_capture()
        self.tick()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- interface ----------
    def hide_from_capture(self):
        # Tenta esconder esta janela da gravação (Windows 10 2004+)
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
        except Exception:
            pass

    def refresh_mics(self):
        mics = list_mics()
        self.mic["values"] = mics + [NO_MIC]
        self.mic.current(0)

    def update_area_label(self):
        x, y, w, h = self.region
        self.area_lbl.config(text=f"Área: {w}x{h} (posição {x},{y})")

    def update_hint(self):
        try:
            limit_bits = self.limit.get() * 1024 * 1024 * 8 * 0.92
        except tk.TclError:
            return
        secs = limit_bits / ((300 + AUDIO_KBPS) * 1000)
        self.hint.config(text=f"Boa qualidade com até ~{secs/60:.1f} min. Acima disso, o app reduz a qualidade para caber.")

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def pick_area(self):
        self.root.withdraw()
        self.root.after(200, lambda: AreaSelector(self.root, self.area_chosen))
        self.root.after(250, self.watch_selector)

    def watch_selector(self):
        # reaparece quando o seletor fecha
        tops = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        if tops:
            self.root.after(150, self.watch_selector)
        else:
            self.root.deiconify()

    def area_chosen(self, x, y, w, h):
        self.region = (x, y, w, h)
        self.update_area_label()

    def full_screen(self):
        u = ctypes.windll.user32
        self.region = (0, 0, u.GetSystemMetrics(0) & ~1, u.GetSystemMetrics(1) & ~1)
        self.update_area_label()

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir)
        if d:
            self.out_dir = d

    def tick(self):
        elapsed = self.acc
        if self.state == "recording":
            elapsed += time.time() - self.seg_start
        m, s = divmod(int(elapsed), 60)
        self.timer.config(text=f"{m:02d}:{s:02d}")
        self.root.after(300, self.tick)

    # ---------- gravação ----------
    def start(self):
        self.tmp = tempfile.mkdtemp(prefix="gravador_")
        self.segments = []
        self.acc = 0.0
        self.start_segment()
        self.btn_rec.config(state="disabled")
        self.btn_pause.config(state="normal", text="❚❚ Pausar")
        self.btn_stop.config(state="normal")

    def start_segment(self):
        x, y, w, h = self.region
        seg = os.path.join(self.tmp, f"seg{len(self.segments):03d}.mkv")
        cmd = [ffmpeg_exe(), "-hide_banner", "-y",
               "-f", "gdigrab", "-framerate", str(FPS), "-draw_mouse", "1",
               "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop"]
        mic = self.mic.get()
        has_audio = mic and mic != NO_MIC
        if has_audio:
            cmd += ["-f", "dshow", "-thread_queue_size", "1024", "-i", f"audio={mic}"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "1", "-af", "aresample=async=1:first_pts=0"]
        cmd.append(seg)
        self.log = open(os.path.join(self.tmp, f"log{len(self.segments)}.txt"), "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log, creationflags=NO_WINDOW)
        self.segments.append(seg)
        self.seg_start = time.time()
        self.state = "recording"
        self.set_status("Gravando...")
        self.root.after(1500, self.check_alive)

    def check_alive(self):
        if self.state == "recording" and self.proc and self.proc.poll() is not None:
            self.state = "idle"
            self.log.close()
            try:
                with open(self.log.name, encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-8:])
            except Exception:
                tail = ""
            messagebox.showerror("Erro ao gravar", "O FFmpeg parou. Verifique o microfone e a área.\n\n" + tail)
            self.reset_buttons()

    def stop_segment(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.acc += time.time() - self.seg_start
        self.proc = None
        try:
            self.log.close()
        except Exception:
            pass

    def toggle_pause(self):
        if self.state == "recording":
            self.stop_segment()
            self.state = "paused"
            self.btn_pause.config(text="▶ Continuar")
            self.set_status("Pausado.")
        elif self.state == "paused":
            self.start_segment()
            self.btn_pause.config(text="❚❚ Pausar")

    def stop(self):
        if self.state == "recording":
            self.stop_segment()
        self.state = "working"
        self.btn_pause.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.set_status("Comprimindo... aguarde.")
        threading.Thread(target=self.finalize, daemon=True).start()

    def reset_buttons(self):
        self.btn_rec.config(state="normal")
        self.btn_pause.config(state="disabled", text="❚❚ Pausar")
        self.btn_stop.config(state="disabled")

    # ---------- compressão ----------
    def finalize(self):
        try:
            segs = [s for s in self.segments if os.path.exists(s) and os.path.getsize(s) > 0]
            if not segs:
                raise RuntimeError("Nada foi gravado.")
            duration = sum(get_duration(s) for s in segs) or max(self.acc, 1)
            listfile = os.path.join(self.tmp, "list.txt")
            with open(listfile, "w", encoding="utf-8") as f:
                for s in segs:
                    f.write("file '" + s.replace("\\", "/") + "'\n")

            mic = self.mic.get()
            has_audio = bool(mic) and mic != NO_MIC
            limit_bytes = self.limit.get() * 1024 * 1024
            target_bytes = limit_bytes * 0.92
            total_kbps = target_bytes * 8 / duration / 1000
            vk = max(total_kbps - (AUDIO_KBPS if has_audio else 0), 30)

            out = os.path.join(self.out_dir, f"gravacao_{datetime.now():%Y%m%d_%H%M%S}.mp4")
            for attempt in range(4):
                width = 1280 if vk >= 500 else 960 if vk >= 250 else 720 if vk >= 120 else 480
                self.set_status(f"Comprimindo (tentativa {attempt + 1})... {vk:.0f} kbps")
                cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                       "-vf", f"scale='min({width},iw)':-2,fps={FPS}",
                       "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
                       "-b:v", f"{int(vk)}k", "-maxrate", f"{int(vk * 1.15)}k", "-bufsize", f"{int(vk * 2)}k"]
                if has_audio:
                    cmd += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "1", "-ar", "44100"]
                else:
                    cmd += ["-an"]
                cmd += ["-movflags", "+faststart", out]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                if r.returncode != 0 or not os.path.exists(out):
                    raise RuntimeError("Falha ao comprimir:\n" + r.stderr[-600:])
                size = os.path.getsize(out)
                if size <= limit_bytes:
                    break
                vk *= (limit_bytes / size) * 0.93

            size = os.path.getsize(out)
            mb = size / 1024 / 1024
            ok = size <= limit_bytes
            self.root.after(0, lambda: self.done(out, mb, ok))
        except Exception as e:
            self.root.after(0, lambda: self.fail(str(e)))

    def done(self, out, mb, ok):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.state = "idle"
        self.acc = 0.0
        self.reset_buttons()
        self.set_status(f"Salvo: {mb:.2f} MB")
        msg = f"Vídeo salvo ({mb:.2f} MB):\n{out}"
        if not ok:
            msg += "\n\nAinda ficou acima do limite. Grave um trecho mais curto."
        messagebox.showinfo("Pronto", msg)
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(out)])
        except Exception:
            pass

    def fail(self, msg):
        self.state = "idle"
        self.reset_buttons()
        self.set_status("Erro.")
        messagebox.showerror("Erro", msg)

    def on_close(self):
        if self.state in ("recording", "paused"):
            if not messagebox.askyesno("Sair", "Há uma gravação em andamento. Descartar e sair?"):
                return
            self.stop_segment()
        self.root.destroy()


if __name__ == "__main__":
    App().root.mainloop()
