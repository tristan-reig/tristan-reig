from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

# region Config
W, H = 68, 51                 # taille du gif
MAX_BRANCHES = 12             # 6 hauteurs des 2 côtés
ADULT_CONTRIB = 150
BARK = "#7A5440"
POT, POT_RIM, POT_TOP = "#9A5434", "#B9673F", "#5B3A28"
STAND, STAND_DARK = "#8B6A4E", "#6B4F3B"
UNKNOWN_LANG = "#8B949E"
MIN_LUMINANCE = 0.12
MIN_LEAF_RADIUS = 2.2

# Couleurs des langages pour les repos privés trouvés via l'API REST
# (l'API REST ne renvoie pas les couleurs, contrairement à GraphQL).
LANG_COLORS = {
    "Java": "#B07219", "Python": "#3572A5", "JavaScript": "#F1E05A", "TypeScript": "#3178C6",
    "C": "#555555", "C++": "#F34B7D", "C#": "#178600", "Lua": "#000080", "OCaml": "#EF7A08",
    "Dart": "#00B4AB", "HTML": "#E34C26", "CSS": "#563D7C", "SCSS": "#C6538C",
    "Shell": "#89E051", "Makefile": "#427819", "Dockerfile": "#384D54", "Rust": "#DEA584",
    "Go": "#00ADD8", "Kotlin": "#A97BFF", "Swift": "#F05138", "PHP": "#4F5D95",
    "Ruby": "#701516", "Vue": "#41B883", "Jupyter Notebook": "#DA5B0B", "TeX": "#3D6117",
}

FRAME_MS = 80                 # durée d'une image de l'animation
BIRD_PASSES = 3               # passages d'oiseau par boucle
BIRD_BEAK = "#F0B040"
BIRD_PLUMAGES = [
    ("#8A97A6", "#6F7D8C", "#C9D3DC"),
    ("#D9603F", "#A8452C", "#F2A07E"),
    ("#3E8FD6", "#2A6DA8", "#9FCBF2"),
    ("#E0B532", "#B08A1E", "#F5DE8A"),
    ("#5FAE5A", "#3F8A3C", "#A9DCA4"),
    ("#B07ACF", "#8A57A8", "#DCC1EC"),
]
BIRD_W = 7
BIRD_BASE = [(0, 1, "b"), (1, 1, "b"), (2, 1, "b"), (3, 1, "b"), (4, 1, "b"),
             (2, 2, "b"), (3, 2, "b"), (5, 0, "h"), (5, 1, "h"), (6, 1, "k")]
# Battement d'ailes
BIRD_WINGS = [[(3, 0, "w"), (2, -1, "w"), (1, -2, "w")],
              [(1, 0, "w"), (2, 0, "w"), (3, 0, "w")],
              [(3, 2, "w"), (2, 3, "w"), (1, 3, "w")],
              [(1, 0, "w"), (2, 0, "w"), (3, 0, "w")]]

Repo = tuple[str, str, int]   # (identifiant, couleur du langage, commits)

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      restrictedContributionsCount
      contributionCalendar { totalContributions }
      commitContributionsByRepository(maxRepositories: 100) {
        contributions { totalCount }
        repository {
          nameWithOwner
          isPrivate
          primaryLanguage { name color }
        }
      }
    }
  }
}
"""
# endregion


# region Données GitHub
def token_kind(token: str) -> str:
    """Type de token, sans jamais afficher sa valeur."""
    if token.startswith("ghs_"):
        return "GITHUB_TOKEN automatique (activité publique seulement)"
    if token.startswith("github_pat_"):
        return "PAT fine-grained"
    if token.startswith("ghp_"):
        return "PAT classic"
    return "type inconnu"


def rest(path: str, token: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "github-bonsai"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def count_commits(full_name: str, login: str, since: str, token: str) -> int:
    """Commits de `login` sur la branche par défaut depuis `since` (comme le compteur de GitHub)."""
    n = 0
    for page in range(1, 6):
        try:
            batch = rest(f"/repos/{full_name}/commits?author={login}&since={since}"
                         f"&per_page=100&page={page}", token)
        except urllib.error.HTTPError:
            return n   # ex. 409 : repo vide
        n += len(batch)
        if len(batch) < 100:
            break
    return n


def private_activity(login: str, token: str, frm: datetime,
                     known: set[str]) -> list[tuple[str, bool, str, int]]:
    """Repli REST : liste les repos privés visibles par le token et compte les commits récents.

    GraphQL masque parfois ces repos (« contributions privées non rattachées »),
    alors que l'API REST y a accès avec le même token.
    """
    since = frm.strftime("%Y-%m-%dT%H:%M:%SZ")
    found: list[tuple[str, bool, str, int]] = []
    try:
        repos: list[dict] = []
        for page in range(1, 6):
            batch = rest("/user/repos?visibility=private&sort=pushed&per_page=100&page=" + str(page)
                         + "&affiliation=owner,collaborator,organization_member", token)
            repos += batch
            if len(batch) < 100:
                break
        for r in repos:
            name = r["full_name"]
            if r.get("fork") or name in known:
                continue
            pushed = r.get("pushed_at")
            if pushed and datetime.fromisoformat(pushed.replace("Z", "+00:00")) < frm:
                continue
            n = count_commits(name, login, since, token)
            if n > 0:
                found.append((name, True, r.get("language") or "?", n))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"AVERTISSEMENT : repli REST impossible ({e}). "
              "Le token a-t-il le scope 'repo' (classic) ?")
    return found


def fetch(login: str, token: str, days: int) -> tuple[list[Repo], int, dict]:
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days)
    body = json.dumps({"query": QUERY, "variables": {
        "login": login, "from": frm.isoformat(), "to": to.isoformat()}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "github-bonsai"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        sys.exit(f"Erreur GraphQL : {data['errors']}")
    if not data["data"]["user"]:
        sys.exit(f"Utilisateur introuvable : {login}")
    cc = data["data"]["user"]["contributionsCollection"]
    repos: list[Repo] = []
    langs: dict[str, int] = {}
    detail: list[tuple[str, bool, str, int]] = []
    private = 0
    for item in cc["commitContributionsByRepository"]:
        repo, n = item["repository"], item["contributions"]["totalCount"]
        lang = repo["primaryLanguage"] or {}
        lang_name = lang.get("name") or "?"
        repos.append((repo["nameWithOwner"], lang.get("color") or UNKNOWN_LANG, n))
        langs[lang_name] = langs.get(lang_name, 0) + n
        detail.append((repo["nameWithOwner"], bool(repo["isPrivate"]), lang_name, n))
        private += repo["isPrivate"]

    # Repli REST pour les repos privés que GraphQL ne détaille pas
    extra = private_activity(login, token, frm, {d[0] for d in detail})
    for name, is_private, lang_name, n in extra:
        repos.append((name, LANG_COLORS.get(lang_name, UNKNOWN_LANG), n))
        langs[lang_name] = langs.get(lang_name, 0) + n
        detail.append((name, is_private, lang_name, n))
        private += 1

    stats = {"repos": len(repos), "private_repos": private, "languages": langs,
             "restricted": cc.get("restrictedContributionsCount", 0), "detail": detail,
             "rest_private": len(extra)}
    return repos, cc["contributionCalendar"]["totalContributions"], stats


def demo() -> tuple[list[Repo], int, dict]:
    colors = {"C": "#555555", "Python": "#3572A5", "JavaScript": "#F1E05A",
              "OCaml": "#EF7A08", "Dart": "#00B4AB", "Java": "#B07219"}
    raw = [("demo/projet-c", "C", 22), ("demo/ml-tp", "Python", 30), ("demo/site", "JavaScript", 12),
           ("demo/interpreteur", "OCaml", 16), ("demo/app", "Dart", 9), ("demo/poo", "Java", 7)]
    return ([(n, colors[l], c) for n, l, c in raw], 96,
            {"repos": len(raw), "private_repos": 0, "restricted": 0, "detail": []})
# endregion


# region Couleurs et hash
def stable_hash(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)


def luminance(hex_color: str) -> float:
    def lin(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hex_color[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable(hex_color: str, min_lum: float = MIN_LUMINANCE) -> str:
    """Éclaircit les couleurs trop sombres pour le thème sombre de GitHub."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    k = 0.0
    while k < 1 and luminance("#{:02X}{:02X}{:02X}".format(
            *(int(c + (255 - c) * k) for c in (r, g, b)))) < min_lum:
        k += 0.05
    return "#{:02X}{:02X}{:02X}".format(*(int(c + (255 - c) * k) for c in (r, g, b)))


def shade(hex_color: str, k: float = 0.75) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02X}{:02X}{:02X}".format(int(r * k), int(g * k), int(b * k))
# endregion


# region Dessin
class Canvas:
    def __init__(self) -> None:
        self.px: list[list[str | None]] = [[None] * W for _ in range(H)]
        self.leaves: set[tuple[int, int]] = set()

    def put(self, x: float, y: float, c: str, leaf: bool = False) -> None:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < W and 0 <= yi < H:
            self.px[yi][xi] = c
            if leaf:
                self.leaves.add((xi, yi))
            else:
                self.leaves.discard((xi, yi))

    def disc(self, cx: float, cy: float, r: float, c: str, leaf: bool = False) -> None:
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + 0.3:
                    self.put(x, y, c, leaf)

    def copy(self) -> Canvas:
        cv = Canvas()
        cv.px = [row[:] for row in self.px]
        cv.leaves = set(self.leaves)
        return cv

    def line(self, x0: float, y0: float, x1: float, y1: float, c: str) -> None:
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(n + 1):
            t = i / n
            self.put(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, c)


def draw(repos: list[Repo], contrib: int) -> Canvas:
    cv = Canvas()
    # présentoir
    for x in range(14, 54):
        cv.put(x, 48, STAND)
        cv.put(x, 49, STAND_DARK)
    for x in (16, 17, 50, 51):
        cv.put(x, 50, STAND_DARK)
    # pot
    for y in range(41, 48):
        half = 11 - (y - 41) // 2
        for x in range(34 - half, 34 + half):
            cv.put(x, y, POT if y > 41 else POT_RIM)
    for x in range(22, 46):
        cv.put(x, 40, POT_TOP)

    repos = sorted((r for r in repos if r[2] > 0), key=lambda r: -r[2])[:MAX_BRANCHES]
    t = min(math.log10(1 + max(contrib, 0)) / math.log10(1 + ADULT_CONTRIB), 1.0)
    trunk_h = 4 + t * 26
    thick = 1 + int(t * 3.2)
    base, sway = 40, 0.5 + t * 3.5

    def tx(y: float) -> float:
        return 34 + math.sin((base - y) * 0.16) * sway

    for i in range(int(trunk_h) + 1):
        y = base - i
        w = max(1, thick - int(i / max(trunk_h, 1) * thick))
        for k in range(w):
            cv.put(tx(y) - w // 2 + k, y, BARK)

    maxc = max([r[2] for r in repos] + [1])
    tips: list[tuple[float, float, float, str, int]] = []
    used: set[int] = set()
    for name, color, n in sorted(repos, key=lambda r: stable_hash(r[0])):
        hv = stable_hash(name)
        slot = hv % MAX_BRANCHES
        while slot in used:
            slot = (slot + 5) % MAX_BRANCHES
        used.add(slot)
        frac = 0.32 + (slot // 2) * 0.11
        side = 1 if slot % 2 else -1
        slope = 0.25 + ((hv >> 12) % 35) / 100
        y0 = base - frac * trunk_h
        x0 = tx(y0)
        rel = n / maxc
        length = (2 + rel * 9) * (0.45 + t * 0.75)
        x1, y1 = x0 + side * length, y0 - length * slope
        cv.line(x0, y0, x1, y1, BARK)
        tips.append((x1, y1, max(MIN_LEAF_RADIUS, (1.2 + rel * 2.8) * (0.6 + t * 0.5)), color, hv))
    if repos:
        top = repos[0]
        tips.append((tx(base - trunk_h), base - trunk_h - 1, 1.5 + t * 4, top[1], stable_hash(top[0])))

    for x, y, r, color, hv in tips:
        rng = random.Random(hv)
        color = readable(color)
        cv.disc(x, y, r, color, leaf=True)
        dark = shade(color)
        for _ in range(int(r * 3)):
            cv.put(x + rng.uniform(-r, r), y + rng.uniform(0, r) * 0.9, dark, leaf=True)
    return cv
# endregion


# region Animation
def lighten(hex_color: str, k: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02X}{:02X}{:02X}".format(*(int(c + (255 - c) * k) for c in (r, g, b)))


def animate(tree: Canvas, seed: str) -> list[tuple[Canvas, int]]:
    """Renvoie la liste (image, durée en ms) d'une boucle : reflet, pause, puis les passages d'oiseaux."""
    frames: list[tuple[Canvas, int]] = []

    # Reflet de l'arbre
    if tree.leaves:
        diag = [x - y for x, y in tree.leaves]
        for band in range(min(diag) - 3, max(diag) + 2, 4):
            cv = tree.copy()
            for x, y in tree.leaves:
                d = (x - y) - band
                if 0 <= d < 3:
                    c = cv.px[y][x]
                    if c:
                        cv.px[y][x] = lighten(c, 0.45 if d == 1 else 0.22)
            frames.append((cv, FRAME_MS))
    frames.append((tree, 1200))

    # Passage des oiseaux
    rng = random.Random(seed)
    heights: list[int] = []
    for _ in range(BIRD_PASSES):
        y = rng.randint(4, 30)
        while any(abs(y - h) < 8 for h in heights):
            y = rng.randint(4, 30)
        heights.append(y)
    for y in heights:
        rightward = rng.random() < 0.5
        body, head, wing = rng.choice(BIRD_PLUMAGES)
        colors = {"b": body, "h": head, "w": wing, "k": BIRD_BEAK}
        xs = list(range(-BIRD_W, W + 1, 3))
        if not rightward:
            xs = [W - BIRD_W - x for x in xs]
        for i, bx in enumerate(xs):
            cv = tree.copy()
            by = y + (1 if i % 4 == 2 else 0)
            for dx, dy, role in BIRD_BASE + BIRD_WINGS[i % 4]:
                px, py = bx + (dx if rightward else BIRD_W - 1 - dx), by + dy
                if 0 <= px < W and 0 <= py < H and tree.px[py][px] is None:
                    cv.put(px, py, colors[role])
            frames.append((cv, FRAME_MS))
        frames.append((tree, rng.randint(1500, 4500)))
    return frames
# endregion


# region Export GIF
def to_images(canvases: list[Canvas], scale: int) -> list[Image.Image]:
    """Toutes les images partagent la même palette ; l'index 0 est transparent."""
    colors = sorted({c for cv in canvases for row in cv.px for c in row if c})
    if len(colors) > 255:
        sys.exit("Trop de couleurs pour un GIF.")
    index = {c: i + 1 for i, c in enumerate(colors)}
    palette = [0, 0, 0]
    for c in colors:
        palette += [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    palette += [0] * (768 - len(palette))
    images = []
    for cv in canvases:
        img = Image.new("P", (W, H), 0)
        img.putpalette(palette)
        img.putdata([index[c] if c else 0 for row in cv.px for c in row])
        images.append(img.resize((W * scale, H * scale), Image.Resampling.NEAREST))
    return images


def save(frames: list[tuple[Canvas, int]], out: Path, scale: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    images = to_images([cv for cv, _ in frames], scale)
    if out.suffix.lower() == ".png" or len(images) == 1:
        images[0].save(out, transparency=0)
        return
    images[0].save(out, save_all=True, append_images=images[1:], transparency=0,
                   disposal=2, loop=0, duration=[d for _, d in frames], optimize=False)
# endregion


# region Diagnostic
def report(token: str | None, days: int, contrib: int, stats: dict) -> None:
    """Affiche ce que GitHub renvoie, pour comprendre pourquoi un repo manque."""
    print(f"{contrib} contributions sur {days} j · {stats['repos']} repos "
          f"(dont {stats['private_repos']} privés)")
    if token:
        print(f"token utilisé : {token_kind(token)}")
    if stats.get("languages"):
        print("langages :", ", ".join(f"{k} {v}" for k, v in sorted(stats["languages"].items(), key=lambda kv: -kv[1])))
    restricted = stats.get("restricted", 0)
    print(f"contributions privées non rattachées à un repo : {restricted}")
    for name, is_private, lang, n in sorted(stats.get("detail", []), key=lambda d: -d[3]):
        print(f"  {'privé ' if is_private else 'public'}  {n:>4} commits  {lang:<12} {name}")

    if token and token.startswith("ghs_"):
        print("ATTENTION : le secret BONSAI_PAT n'est pas lu, le GITHUB_TOKEN automatique est utilisé. "
              "Vérifie le nom et l'emplacement du secret (Repository secrets).")
    elif restricted > 0 and stats["private_repos"] == 0:
        print("ATTENTION : GitHub compte des contributions privées, mais ni GraphQL ni le repli REST "
              "n'en trouvent. Le token n'a probablement pas le scope 'repo' (classic), ou les repos "
              "sont dans une organisation à autoriser via SSO.")
    elif stats.get("rest_private"):
        print(f"INFO : {stats['rest_private']} repo(s) privé(s) trouvé(s) via l'API REST "
              "(GraphQL ne les détaillait pas).")
    elif restricted == 0 and stats["private_repos"] == 0:
        print("INFO : aucune contribution privée vue. Si un repo privé devrait compter, vérifie que "
              "les commits sont sur la branche par défaut, datent de moins de 30 jours, que le repo "
              "n'est pas un fork et que l'e-mail git est vérifié sur GitHub.")
# endregion


# region Ligne de commande
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="login GitHub")
    ap.add_argument("--out", default="assets/bonsai.gif", help="fichier de sortie (.gif ou .png)")
    ap.add_argument("--days", type=int, default=30, help="taille de la fenêtre glissante")
    ap.add_argument("--scale", type=int, default=5, help="agrandissement des pixels")
    ap.add_argument("--demo", action="store_true", help="données fictives, sans token")
    ap.add_argument("--static", action="store_true", help="une seule image, sans animation")
    ap.add_argument("--seed", help="graine du hasard de l'oiseau (par défaut : login + date du jour)")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if args.demo:
        repos, contrib, stats = demo()
    else:
        if not args.user or not token:
            sys.exit("Il faut --user et un token dans GH_TOKEN (ou utiliser --demo).")
        repos, contrib, stats = fetch(args.user, token, args.days)

    report(None if args.demo else token, args.days, contrib, stats)
    tree = draw(repos, contrib)
    seed = args.seed or f"{args.user or 'demo'}-{datetime.now(timezone.utc).date().isoformat()}"
    frames = [(tree, 0)] if args.static else animate(tree, seed)
    save(frames, Path(args.out), args.scale)
    print(f"-> {args.out}")
# endregion


if __name__ == "__main__":
    main()
