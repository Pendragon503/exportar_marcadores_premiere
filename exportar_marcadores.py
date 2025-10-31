import csv
import os
import shutil
import subprocess
from pathlib import Path
import codecs
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox

# ================= CONFIG POR DEFECTO (se sobreescribe con diálogos) =================
PARALLEL_JOBS = 2           # súbelo a 3–4 cuando tengas NVENC operativo
USE_DOUBLE_SEEK = True      # cortes más precisos
WRITE_LOG = True
LOG_FILE = "export_log.txt"
NAME_PREFIX = "parte_"
EXT = "mp4"

# ================= UTIL =================
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None

def safe_filename(s: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        s = s.replace(ch, "_")
    return s.strip() or "sin_nombre"

def strip_accents_lower(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().strip()

def parse_time_any(s: str, fps: float) -> float:
    s = s.strip().replace(";", ":")
    if not s:
        raise ValueError("Tiempo vacío")
    parts = s.split(":")
    if len(parts) == 4:  # HH:MM:SS:FF
        hh, mm, ss, ff = parts
        return int(hh)*3600 + int(mm)*60 + int(ss) + (int(ff)/float(fps))
    if "." in parts[-1]:  # HH:MM:SS.mmm
        hh, mm, sec_ms = int(parts[0]), int(parts[1]), float(parts[2])
        return hh*3600 + mm*60 + sec_ms
    if len(parts) == 3:   # HH:MM:SS
        hh, mm, ss = [int(x) for x in parts]
        return hh*3600 + mm*60 + ss
    return float(s)

def sniff_encoding(csv_path: str) -> str:
    with open(csv_path, "rb") as f:
        head = f.read(4)
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    return "utf-8-sig"

def sniff_delimiter(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t")
        return dialect.delimiter
    except Exception:
        if "\t" in sample_text: return "\t"
        if ";" in sample_text:  return ";"
        return ","

def find_header(fieldnames, candidates):
    norm_map = {strip_accents_lower(f): f for f in fieldnames if f}
    for cand in candidates:
        key = strip_accents_lower(cand)
        if key in norm_map:
            return norm_map[key]
    return None

# ================= DETECCIÓN FPS =================
def parse_fraction(fr: str) -> float:
    # e.g. "30000/1001" -> 29.97
    if "/" in fr:
        num, den = fr.split("/", 1)
        try:
            return float(num) / float(den)
        except Exception:
            pass
    try:
        return float(fr)
    except Exception:
        return 0.0

def detect_fps(input_video: str) -> float:
    # Preferimos avg_frame_rate; si falla, r_frame_rate.
    if not ffprobe_available():
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate",
            "-of", "default=nw=1:nk=1", input_video
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()
        # out puede traer dos líneas: avg_frame_rate y r_frame_rate
        vals = [parse_fraction(x.strip()) for x in out if x.strip()]
        vals = [v for v in vals if v > 0]
        if not vals:
            return 0.0
        # elegimos la primera válida; si es ~29.97, redondeamos nicamente
        fps = vals[0]
        # Ajustes suaves a tasas típicas
        if abs(fps - 29.97) < 0.01: return 29.97
        if abs(fps - 23.976) < 0.01: return 23.976
        if abs(fps - 59.94) < 0.02:  return 59.94
        return float(f"{fps:.3f}")
    except Exception:
        return 0.0

# ================= CODEC PRESETS =================
def nvenc_video_opts(fps):
    gop = max(2, int(round(fps * 2)))
    return [
        "-c:v", "h264_nvenc",
        "-rc", "vbr_hq",
        "-cq", "19",
        "-b:v", "0", "-maxrate", "0",
        "-preset", "p6",
        "-spatial_aq", "1", "-temporal_aq", "1", "-aq-strength", "8",
        "-bf", "3", "-rc-lookahead", "20",
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

def qsv_video_opts(fps):
    gop = max(2, int(round(fps * 2)))
    return [
        "-c:v", "h264_qsv",
        "-global_quality", "19",
        "-look_ahead", "1",
        "-bf", "3",
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

def x264_video_opts(fps):
    gop = max(2, int(round(fps * 2)))
    return [
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

AUDIO_OPTS = ["-c:a", "aac", "-b:a", "192k"]

def test_backend(backend_name: str, input_video: str, fps: float) -> bool:
    if backend_name == "nvenc":
        vopts = nvenc_video_opts(fps)
    elif backend_name == "qsv":
        vopts = qsv_video_opts(fps)
    elif backend_name == "x264":
        vopts = x264_video_opts(fps)
    else:
        return False

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.mp4"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0", "-i", input_video,
            "-frames:v", "1",
            *vopts, *AUDIO_OPTS,
            str(out)
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return out.exists() and out.stat().st_size > 0
        except subprocess.CalledProcessError:
            return False
        except Exception:
            return False

def choose_backend(input_video: str, fps: float) -> str:
    # Prioridad: NVENC -> QSV -> x264
    if test_backend("nvenc", input_video, fps):
        return "nvenc"
    if test_backend("qsv", input_video, fps):
        return "qsv"
    return "x264"

# ================= LOAD MARKERS =================
def load_markers(csv_path: str, fps: float):
    enc = sniff_encoding(csv_path)
    with open(csv_path, "r", encoding=enc, errors="strict") as f:
        sample = f.read(4096)
    delimiter = sniff_delimiter(sample)

    with open(csv_path, "r", encoding=enc, newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        fieldnames = [fn for fn in (reader.fieldnames or []) if fn]
        if not fieldnames:
            raise ValueError("No se pudieron leer cabeceras del CSV.")

        start_candidates = [
            "Start", "In", "Time", "Inicio", "Entrada", "Start Time", "StartTime",
            "Punto de entrada", "Tiempo de inicio"
        ]
        name_candidates  = [
            "Name", "Marker Name", "Nombre", "Nombre del marcador", "Comment", "Descripción"
        ]

        start_col = find_header(fieldnames, start_candidates)
        name_col  = find_header(fieldnames, name_candidates)
        if not start_col:
            raise ValueError(f"No encuentro columna de tiempo (probé: {start_candidates}). Cabeceras: {fieldnames}")

        markers = []
        for row in reader:
            raw_tc = (row.get(start_col) or "").strip()
            if not raw_tc:
                continue
            try:
                t = parse_time_any(raw_tc, fps)
            except Exception:
                continue
            name = (row.get(name_col) or "").strip() if name_col else ""
            markers.append((t, name))

    markers.sort(key=lambda x: x[0])
    return markers

# ================= EXPORT =================
def build_ffmpeg_cmd(input_video: str, start_s: float, duration: float, out_file: Path, backend: str, fps: float, use_double_seek: bool):
    if backend == "nvenc":
        vopts = nvenc_video_opts(fps)
    elif backend == "qsv":
        vopts = qsv_video_opts(fps)
    else:
        vopts = x264_video_opts(fps)

    if use_double_seek:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.3f}", "-i", input_video,
            "-ss", "0", "-t", f"{duration:.3f}",
            *vopts, *AUDIO_OPTS,
            "-movflags", "+faststart",
            str(out_file)
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.3f}", "-i", input_video,
            "-t", f"{duration:.3f}",
            *vopts, *AUDIO_OPTS,
            "-movflags", "+faststart",
            str(out_file)
        ]
    return cmd

def export_one(i, segment, backend, fps, use_double_seek, log_fp):
    start_s, end_s, name_start, out_file, input_video = segment
    duration = end_s - start_s
    cmd = build_ffmpeg_cmd(input_video, start_s, duration, out_file, backend, fps, use_double_seek)
    print(" ".join(cmd))
    if log_fp:
        log_fp.write(f"[{i+1:03d}] {out_file.name}\n")
        log_fp.write(f"  Backend: {backend}\n")
        log_fp.write(f"  Inicio: {start_s:.3f}s  Duración: {duration:.3f}s\n")
        log_fp.write("  CMD   : " + " ".join(cmd) + "\n")
    subprocess.run(cmd, check=True)
    return out_file

def export_segments_parallel(markers, backend: str, input_video: str, output_dir: str, fps: float):
    if not Path(input_video).exists():
        raise FileNotFoundError(f"No encuentro el video de entrada: {input_video}")
    if len(markers) < 2:
        raise ValueError("Se requieren al menos 2 marcadores (inicio y fin).")
    if not ffmpeg_available():
        raise EnvironmentError("ffmpeg no está en PATH.")

    print("Diagnóstico:")
    print("  CSV   : OK (seleccionado por diálogo)")
    print("  VIDEO :", Path(input_video).resolve(), "->", "OK" if Path(input_video).exists() else "NO")
    try:
        ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        print("  FFmpeg:", ver.stdout.splitlines()[0])
    except Exception:
        print("  FFmpeg: en PATH pero no pude leer versión.")
    print("  Backend elegido:", backend)
    print("  FPS:", fps)

    os.makedirs(output_dir, exist_ok=True)

    segments = []
    for i in range(len(markers) - 1):
        start_s, name_start = markers[i]
        end_s, _ = markers[i + 1]
        if end_s <= start_s:
            continue
        base = f"{NAME_PREFIX}{i+1:03d}"
        if name_start:
            base += f"_{safe_filename(name_start)}"
        out_file = Path(output_dir) / f"{base}.{EXT}"
        segments.append((start_s, end_s, name_start, out_file, input_video))

    log_fp = None
    if WRITE_LOG:
        log_fp = open(Path(output_dir) / LOG_FILE, "a", encoding="utf-8")
        log_fp.write("\n" + "="*80 + "\n")
        log_fp.write(f"Fecha: {datetime.now().isoformat(timespec='seconds')}\n")
        log_fp.write(f"Video: {Path(input_video).resolve()}\n")
        log_fp.write(f"FPS  : {fps} | PARALLEL_JOBS: {PARALLEL_JOBS}\n")
        log_fp.write(f"Backend: {backend}\n")
        log_fp.write("-"*80 + "\n")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, PARALLEL_JOBS)) as ex:
        futs = {ex.submit(export_one, idx, seg, backend, fps, USE_DOUBLE_SEEK, log_fp): idx for idx, seg in enumerate(segments)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                out = fut.result()
                results.append(out)
            except subprocess.CalledProcessError as e:
                msg = f"[{idx+1:03d}] ffmpeg falló con código {e.returncode}"
                print("ERROR:", msg)
                if log_fp:
                    log_fp.write("ERROR: " + msg + "\n")
            except Exception as e:
                msg = f"[{idx+1:03d}] Error inesperado: {e}"
                print("ERROR:", msg)
                if log_fp:
                    log_fp.write("ERROR: " + msg + "\n")

    if log_fp:
        log_fp.write("Estado: OK\n")
        log_fp.close()

    print(f"Listo. Archivos en: {output_dir}")

# ================= UI (DIÁLOGOS) =================
def ask_paths_via_gui():
    root = tk.Tk()
    root.withdraw()
    root.update()

    messagebox.showinfo("Exportar por marcadores", 
        "Selecciona primero el CSV de marcadores exportado desde Premiere.")
    csv_path = filedialog.askopenfilename(
        title="Elige el CSV de marcadores",
        filetypes=[("CSV", "*.csv"), ("Todos", "*.*")]
    )
    if not csv_path:
        raise SystemExit("Cancelado por el usuario (CSV).")

    messagebox.showinfo("Exportar por marcadores", 
        "Ahora selecciona el video maestro (MP4) de la secuencia completa.")
    video_path = filedialog.askopenfilename(
        title="Elige el video maestro (MP4)",
        filetypes=[("MP4", "*.mp4"), ("Todos", "*.*")]
    )
    if not video_path:
        raise SystemExit("Cancelado por el usuario (MP4).")

    messagebox.showinfo("Exportar por marcadores", 
        "Elige la carpeta donde quieres guardar los clips exportados.")
    out_dir = filedialog.askdirectory(
        title="Elige carpeta de salida"
    )
    if not out_dir:
        raise SystemExit("Cancelado por el usuario (carpeta de salida).")

    root.destroy()
    return csv_path, video_path, out_dir

# ================= MAIN =================
if __name__ == "__main__":
    if not ffmpeg_available():
        print("❌ ffmpeg no está en PATH. Instálalo y vuelve a intentar.")
        raise SystemExit(1)

    csv_path, video_path, output_dir = ask_paths_via_gui()

    # Detectar FPS automáticamente; si falla, pedirlo
    fps = detect_fps(video_path)
    if fps <= 0:
        root = tk.Tk(); root.withdraw()
        resp = simpledialog.askstring("FPS", "No pude detectar el FPS. Ingresa el FPS de tu timeline (p. ej., 29.97, 30, 25):")
        root.destroy()
        try:
            fps = float(resp)
        except Exception:
            print("No se pudo obtener un FPS válido. Uso 30.0 por defecto.")
            fps = 30.0

    print("Leyendo marcadores...")
    print("CSV:", Path(csv_path).resolve(), " -> existe:", Path(csv_path).exists())
    print("VIDEO:", Path(video_path).resolve(), " -> existe:", Path(video_path).exists())

    markers = load_markers(csv_path, fps)
    print(f"Marcadores detectados: {len(markers)}")

    backend = choose_backend(video_path, fps)
    export_segments_parallel(markers, backend, video_path, output_dir, fps)
