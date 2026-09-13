"""Translate wallpaper labels and descriptions, locally.

A lot of Workshop wallpapers are authored in Chinese, Japanese or Korean, so
their property labels and descriptions are unreadable for an English speaker
even though the controls themselves are fine. This translates them locally --
no API key, nothing leaves the machine, off by default.

Three providers, auto-detected in this order (WPE_TRANSLATE_PROVIDER overrides):

  LibreTranslate  a real translation service over HTTP if you run one
                  (WPE_LIBRETRANSLATE_URL, default localhost:5010). See
                  docker/compose.yml.
  argos           offline neural translation, ~0.07s for a panel of labels.
  ollama          whatever model is already on the box. Slower (~4s for a new
                  batch, then cached) but noticeably better on the short
                  technical labels these panels are full of.

**On the size of "just use Argos".** `pip install argostranslate` lands at
**5.9 GB**: ctranslate2's wheel hard-depends on the CUDA runtime (3.2 GB of
`nvidia` packages that CPU inference never loads), and argostranslate needs
stanza for sentence splitting, which pulls torch (1.2 GB) and triton (897 MB).

None of that is required to run the models. An `.argosmodel` is a zip holding a
CTranslate2 model and a sentencepiece vocabulary; loading those two directly
skips argostranslate and stanza entirely. UI labels are single phrases, so
there is nothing for a sentence splitter to do anyway.

    ctranslate2 (--no-deps) + sentencepiece + numpy      ~206 MB
    zh + ja + ko language packs                          ~309 MB
    ------------------------------------------------------------
    offline CJK translation, total                       ~515 MB

versus 5.9 GB before a single model is downloaded. Individual packs:
zh 74 MB, ja 117 MB, ko 118 MB, fr 66 MB, pt 69 MB, de 150 MB, ru 156 MB,
es 285 MB.

`translate-shell` (277 KB, in apt) is genuinely tiny but sends the text to
Google or Bing, so it is not offered here.

Three things keep it from being annoying:

  * results are cached on disk forever, keyed by the source text, so a
    wallpaper is only ever translated once;
  * text that is already Latin is skipped without asking the model anything,
    which is most of the library;
  * a batch is one request with the strings numbered, rather than one request
    per label. A properties panel is 25 labels; 25 round trips to a local 8B is
    fifteen seconds of nothing happening.

If Ollama is not running, every call is a no-op that returns the original text.
The UI shows the setting as unavailable rather than silently doing nothing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile

from . import paths

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
# Port 5010, not LibreTranslate's default 5000, which postiz already uses here.
LIBRE = os.environ.get("WPE_LIBRETRANSLATE_URL", "http://127.0.0.1:5010").rstrip("/")
# qwen is the strongest CJK->English of what is on this box, and 8b answers a
# 25-label batch in a couple of seconds.
MODEL = os.environ.get("WPE_TRANSLATE_MODEL", "qwen3:8b")
CACHE_FILE = os.path.join(paths.CONFIG_DIR, "translations.json")

# --- argos: the models, without the framework --------------------------------
ARGOS_DIR = os.path.expanduser("~/.local/share/wpe-studio/translate")
ARGOS_VENV = os.path.join(ARGOS_DIR, "venv")
ARGOS_INDEX = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
PREFERRED = os.environ.get("WPE_TRANSLATE_PROVIDER", "")

# Which pack to use for a piece of text, decided by script rather than by a
# language-detection dependency. Ranges are ordered most-specific first:
# Hangul and kana are unambiguous, Han characters are only Chinese once the
# Japanese-specific scripts have been ruled out.
SCRIPTS = (
    ("ko", re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")),
    ("ja", re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")),
    ("zh", re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
)


def detect_language(text):
    for code, rx in SCRIPTS:
        if rx.search(text):
            return code
    return None


def argos_python():
    """The interpreter that has ctranslate2, or None."""
    cand = os.path.join(ARGOS_VENV, "bin", "python")
    return cand if os.path.exists(cand) else None


def argos_installed():
    """{lang code: pack directory} for every pack on disk."""
    out = {}
    try:
        for name in os.listdir(ARGOS_DIR):
            d = os.path.join(ARGOS_DIR, name)
            if not os.path.isdir(d) or not name.startswith("translate-"):
                continue
            code = name.split("-", 1)[1].split("_", 1)[0]
            if os.path.isdir(os.path.join(d, "model")):
                out[code] = d
    except OSError:
        pass
    return out


def argos_ready():
    return bool(argos_python()) and bool(argos_installed())


# The worker runs inside the venv, so it is a script rather than an import.
_WORKER = r"""
import json, sys
import ctranslate2, sentencepiece
packdir, = sys.argv[1:2]
lines = json.load(sys.stdin)
sp = sentencepiece.SentencePieceProcessor(packdir + "/sentencepiece.model")
tr = ctranslate2.Translator(packdir + "/model", device="cpu", inter_threads=2)
res = tr.translate_batch([sp.encode(x, out_type=str) for x in lines],
                         beam_size=4, max_batch_size=32)
# Join the pieces rather than sp.decode(): the CT2 shared vocabulary does not
# round-trip through this sentencepiece model, and decode leaves the U+2581
# word-boundary markers in the output.
out = ["".join(r.hypotheses[0]).replace("\u2581", " ").strip() for r in res]
json.dump(out, sys.stdout)
"""


def _argos_translate(lines, timeout=120):
    """{index: english}. Groups by detected language so each pack loads once."""
    py = argos_python()
    packs = argos_installed()
    if not py or not packs:
        return {}

    groups = {}
    for i, text in enumerate(lines):
        code = detect_language(text)
        if code and code in packs:
            groups.setdefault(code, []).append(i)

    got = {}
    for code, idxs in groups.items():
        payload = [lines[i] for i in idxs]
        try:
            r = subprocess.run([py, "-c", _WORKER, packs[code]],
                               input=json.dumps(payload), capture_output=True,
                               text=True, timeout=timeout)
            if r.returncode != 0:
                continue
            for i, english in zip(idxs, json.loads(r.stdout)):
                if english:
                    got[i] = english
        except Exception:
            continue
    return got

_lock = threading.Lock()
_cache = None

# Anything outside Latin-1 plus the usual punctuation is worth translating.
_NON_LATIN = re.compile(r"[^\x00-\x7f -ɏ -⁯]")


def needs_translation(text):
    return bool(text) and bool(_NON_LATIN.search(text))


def _load():
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(CACHE_FILE, encoding="utf-8") as fh:
                    _cache = json.load(fh)
            except Exception:
                _cache = {}
        return _cache


def _save():
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _libre_up(timeout=1.0):
    try:
        with urllib.request.urlopen(LIBRE + "/languages", timeout=timeout) as r:
            return isinstance(json.load(r), list)
    except Exception:
        return False


def _ollama_up(timeout=1.5):
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


def provider():
    if PREFERRED:
        return PREFERRED
    if _libre_up():
        return "libretranslate"
    if argos_ready():
        return "argos"
    if _ollama_up() is not None:
        return "ollama"
    return None


def available(timeout=1.5):
    models = _ollama_up(timeout)
    prov = provider()
    return {
        "ok": prov is not None,
        "provider": prov,
        "model": MODEL if prov == "ollama" else prov,
        "model_present": prov != "ollama" or (models is not None and MODEL in models),
        "argos_runtime": bool(argos_python()),
        "argos_packs": sorted(argos_installed()),
        "argos_dir": ARGOS_DIR,
        "ollama_up": models is not None,
        "error": None if prov else "no translator available",
    }


def _libre_translate(lines, timeout=30):
    """LibreTranslate one request at a time; it accepts an array for q."""
    body = json.dumps({"q": lines, "source": "auto", "target": "en",
                       "format": "text"}).encode()
    req = urllib.request.Request(LIBRE + "/translate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r).get("translatedText")
    if isinstance(out, str):
        out = [out]
    return {i: t for i, t in enumerate(out or []) if t}


PROMPT = """Translate each numbered line into natural English.

These are UI labels and descriptions from a desktop wallpaper's settings panel.
Keep them short, like the labels they are. If a line already contains an English
translation in brackets, return just that English part. If a line is already
English, return it unchanged.

Reply with ONLY the numbered lines, in the same order, nothing else.

%s"""


def _ask(lines, timeout=120):
    body = json.dumps({
        "model": MODEL,
        "prompt": PROMPT % "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(lines)),
        "stream": False,
        # Deterministic, so the same label does not drift between runs.
        "options": {"temperature": 0, "num_predict": 1200},
        "think": False,
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r).get("response", "")
    # Some models still emit a reasoning block; keep only what follows it.
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()

    got = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
        if m:
            got[int(m.group(1)) - 1] = m.group(2).strip()
    return got


def translate_many(texts, timeout=120):
    """{original: english} for everything worth translating. Never raises."""
    out = {}
    todo = []
    cache = _load()
    for t in texts:
        if not needs_translation(t):
            continue
        if t in cache:
            out[t] = cache[t]
        elif t not in todo:
            todo.append(t)
    if not todo:
        return out

    # Keep a batch small enough that one bad answer does not poison a whole
    # properties panel, and short enough to stay well inside num_predict.
    prov = provider()
    for i in range(0, len(todo), 25):
        chunk = todo[i:i + 25]
        try:
            if prov == "libretranslate":
                got = _libre_translate(chunk)
            elif prov == "argos":
                got = _argos_translate(chunk, timeout=timeout)
            else:
                got = _ask(chunk, timeout=timeout)
        except Exception:
            break            # provider down or slow: fall back to the originals
        for idx, src in enumerate(chunk):
            english = got.get(idx)
            if english and english != src:
                out[src] = english
                cache[src] = english
    _save()
    return out


def apply_to_properties(props, description=""):
    """Translate a properties list in place; returns the translated description."""
    texts = [p.get("label", "") for p in props]
    for p in props:
        for opt in p.get("options") or []:
            texts.append(opt.get("label", ""))
    if description:
        texts.extend(description.splitlines())

    table = translate_many(texts)
    if not table:
        return description

    for p in props:
        p["label_original"] = p.get("label", "")
        p["label"] = table.get(p.get("label", ""), p.get("label", ""))
        for opt in p.get("options") or []:
            opt["label"] = table.get(opt.get("label", ""), opt.get("label", ""))
    if description:
        return "\n".join(table.get(l, l) for l in description.splitlines())
    return description
