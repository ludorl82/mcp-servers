#!/usr/bin/env python3
"""Serveur MCP « hockey » : les sept équipes canadiennes de la LNH.

Horaire, pointage en direct, résultats avec les marqueurs, classement et
statistiques des joueurs, pour une équipe par défaut (le Canadien, ou
HOCKEY_EQUIPE) et pour les six autres sur demande. Les données viennent de
l'API publique de la LNH (api-web.nhle.com), sans clé : le serveur ne garde
aucun secret et ne fait que lire.

Configuration, par variables d'environnement :
- HOCKEY_EQUIPE : l'équipe par défaut, son nom, sa ville ou son abréviation
  (« Oilers », « Edmonton », « EDM »). Défaut : le Canadien.
- HOST, PORT : l'adresse d'écoute. Défaut : toutes les interfaces, port 8080.

Transport : MCP « Streamable HTTP » réduit au strict nécessaire. Un POST sur
/mcp porte un message JSON-RPC (ou un lot) ; la réponse est du JSON simple,
jamais un flux SSE, et le serveur ne garde aucune session : chaque requête se
suffit. C'est permis par la spécification et ça évite toute dépendance — rien
que la bibliothèque standard, donc une image python stock et pas de pip au
démarrage.

Les réponses sont écrites pour être LUES À VOIX HAUTE : des phrases courtes en
français, pas de tableaux, l'heure de l'Est.
"""
import json
import os
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api-web.nhle.com/v1"
HOST = os.environ.get("HOST", "")
PORT = int(os.environ.get("PORT", "8080"))
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")
TYPES = {1: "match préparatoire", 3: "match des séries"}
DIVISIONS = {"Atlantic": "Atlantique", "Metropolitan": "Métropolitaine", "Central": "Centrale", "Pacific": "Pacifique"}
CONFERENCES = {"Eastern": "l'Est", "Western": "l'Ouest"}
POSITIONS = {"C": "centre", "L": "ailier gauche", "R": "ailier droit", "D": "défenseur", "G": "gardien"}

# Les sept équipes canadiennes : comment on les nomme, et si le sujet est
# pluriel (« les Oilers reçoivent ») ou singulier (« le Canadien reçoit »).
EQUIPES = {
    "MTL": ("le Canadien", False, ("canadien", "canadiens", "habs", "montreal", "tricolore", "ch")),
    "TOR": ("les Maple Leafs", True, ("maple leafs", "leafs", "toronto")),
    "OTT": ("les Sénateurs", True, ("senateurs", "senators", "sens", "ottawa")),
    "WPG": ("les Jets", True, ("jets", "winnipeg")),
    "CGY": ("les Flames", True, ("flames", "calgary")),
    "EDM": ("les Oilers", True, ("oilers", "edmonton")),
    "VAN": ("les Canucks", True, ("canucks", "vancouver")),
}
# Le nom complet, pour dire l'équipe par défaut dans les descriptions.
NOMS_COMPLETS = {"MTL": "le Canadien de Montréal", "TOR": "les Maple Leafs de Toronto",
                 "OTT": "les Sénateurs d'Ottawa", "WPG": "les Jets de Winnipeg",
                 "CGY": "les Flames de Calgary", "EDM": "les Oilers d'Edmonton",
                 "VAN": "les Canucks de Vancouver"}
DEFAUT = "MTL"
TOUTES = ("toutes", "tous", "canadiennes", "equipes canadiennes", "toutes les equipes")


class EquipeInconnue(Exception):
    pass


def defaut(texte):
    """HOCKEY_EQUIPE → l'abréviation ; une valeur inconnue arrête le serveur au démarrage."""
    if not texte.strip():
        return "MTL"
    try:
        return equipe(texte)
    except EquipeInconnue:
        sys.exit(f"hockey: HOCKEY_EQUIPE={texte!r} n'est pas une équipe canadienne de la LNH")


def simple(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(s.replace("-", " ").split())


def equipe(texte):
    """« Oilers », « les Leafs », « Montréal », « EDM » → l'abréviation de la LNH."""
    t = simple(texte)
    if not t:
        return DEFAUT
    for abbr, (_, _, alias) in EQUIPES.items():
        if t == abbr.lower() or any(a == t or a in t.split() or (" " in a and a in t) for a in alias):
            return abbr
    raise EquipeInconnue(texte)


def sujet(t, majuscule=False):
    s = EQUIPES[t][0]
    return s[0].upper() + s[1:] if majuscule else s


def du(t):
    """« du Canadien », « des Oilers »."""
    s = EQUIPES[t][0]
    return "du " + s[3:] if s.startswith("le ") else "des " + s[4:]


def pl(n, mot):
    """« 1 victoire », « 2 victoires »."""
    return f"{n} {mot}{'s' if abs(n) > 1 else ''}"


def nombre_fr(x, decimales):
    """« 4,47 » : la virgule, à la française."""
    return f"{x:.{decimales}f}".replace(".", ",")


def v(t, singulier, pluriel):
    return pluriel if EQUIPES[t][1] else singulier


# --- L'API de la LNH, avec un petit cache -----------------------------------

_cache = {}
_lock = threading.Lock()
USER_AGENT = "mcp-servers-hockey (+https://github.com/ludorl82/mcp-servers)"


def nhl(path, ttl):
    """GET sur l'API de la LNH ; ttl en secondes. urllib suit les 307 de /now."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(path)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(API + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    with _lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[path] = (now, data)
    return data


def season(t):
    return nhl(f"/club-schedule-season/{t}/now", 60)["games"]


def landing(game):
    """La fiche d'un match ; un match fini ne change plus, on la garde une heure."""
    fini = game.get("gameState") in ("OFF", "FINAL")
    return nhl(f"/gamecenter/{game['id']}/landing", 3600 if fini else 15)


# --- Mise en mots ------------------------------------------------------------


def aujourdhui_est():
    """La date du jour à l'heure de l'Est (l'image n'a pas de tzdata : l'heure
    avancée se calcule, du 2e dimanche de mars au 1er dimanche de novembre)."""
    utc = datetime.now(timezone.utc)

    def dimanche(mois, rang):
        d = date(utc.year, mois, 1)
        return d + timedelta(days=(6 - d.weekday()) % 7, weeks=rang - 1)

    debut = datetime.combine(dimanche(3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    fin = datetime.combine(dimanche(11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return (utc - timedelta(hours=4 if debut <= utc < fin else 5)).date()


def local_start(game):
    """L'heure de début, à l'heure de l'Est (décalage fourni par l'API)."""
    start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    off = game.get("easternUTCOffset", "-05:00")
    sign = -1 if off.startswith("-") else 1
    h, m = off[1:].split(":")
    return start.astimezone(timezone(sign * timedelta(hours=int(h), minutes=int(m))))


def heure(d):
    return f"{d.hour} h" + (f" {d.minute:02d}" if d.minute else "")


def quand(game):
    d = local_start(game)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} à {heure(d)}"


def jour(game):
    d = local_start(game)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def nom(team):
    """« les Blackhawks de Chicago » à partir d'un bloc équipe de l'API."""
    if team.get("abbrev") in EQUIPES:
        return sujet(team["abbrev"])
    common = team.get("commonName") or team.get("name") or {}
    common = common.get("fr") or common.get("default", team.get("abbrev", "?"))
    place = (team.get("placeNameWithPreposition") or {}).get("fr") or ""
    return f"les {common} {place}".strip()


def court(team):
    """« les Sharks » : le nom sans la ville, pour les phrases déjà longues."""
    if team.get("abbrev") in EQUIPES:
        return sujet(team["abbrev"])
    common = team.get("commonName") or team.get("name") or {}
    return "les " + (common.get("fr") or common.get("default", team.get("abbrev", "?")))


def chez_soi(game, t):
    return game["homeTeam"]["abbrev"] == t


def cotes(game, t):
    """(notre équipe, l'adversaire) pour le point de vue de t."""
    return (game["homeTeam"], game["awayTeam"]) if chez_soi(game, t) else (game["awayTeam"], game["homeTeam"])


def affiche(game, t, majuscule=False):
    verbe = v(t, "reçoit", "reçoivent") if chez_soi(game, t) else v(t, "visite", "visitent")
    s = f"{sujet(t, majuscule)} {verbe} {nom(cotes(game, t)[1])}"
    if game.get("gameType") in TYPES:
        s += f" ({TYPES[game['gameType']]})"
    return s


def tele(game):
    reseaux = [b["network"] for b in game.get("tvBroadcasts", []) if b.get("countryCode") == "CA"]
    return f", à {' et '.join(reseaux)}" if reseaux else ""


def fin(game):
    f = (game.get("gameOutcome") or {}).get("lastPeriodType") \
        or (game.get("periodDescriptor") or {}).get("periodType")
    return {"OT": " en prolongation", "SO": " en tirs de barrage"}.get(f, "")


def pointage(game, t):
    """« Défaite 4 à 3 en prolongation contre les Sharks de San Jose »."""
    nous, eux = cotes(game, t)
    m, a = nous.get("score", 0), eux.get("score", 0)
    issue = "Victoire" if m > a else "Défaite"
    lieu = "contre" if chez_soi(game, t) else "chez"
    return f"{issue} {max(m, a)} à {min(m, a)}{fin(game)} {lieu} {nom(eux)}"


def periode(pd):
    t = pd.get("periodType")
    if t == "OT":
        return "en prolongation"
    if t == "SO":
        return "en tirs de barrage"
    n = pd.get("number", 1)
    return f"en {n}{'re' if n == 1 else 'e'} période"


def minutes(mmss):
    """« 07:12 » → « 7 minutes 12 », qui se dit mieux à voix haute."""
    try:
        m, sec = (int(x) for x in mmss.split(":"))
    except ValueError:
        return "un peu de temps"
    if m == 0:
        return f"{sec} secondes"
    return f"{m} minute{'s' if m > 1 else ''}" + (f" {sec}" if sec else "")


def buts(fiche):
    return [b for p in (fiche.get("summary") or {}).get("scoring", []) for b in p.get("goals", [])]


def marqueurs(fiche, abbr):
    """« Suzuki, 1 but et 1 passe ; Caufield, 1 but » pour une équipe, dans l'ordre des points."""
    pts = {}
    for b in buts(fiche):
        if (b.get("teamAbbrev") or {}).get("default") != abbr:
            continue
        qui = (b.get("lastName") or b.get("name") or {}).get("default", "?")
        pts.setdefault(qui, [0, 0])[0] += 1
        for a in b.get("assists", []):
            nom_a = (a.get("name") or {}).get("default", "?")
            nom_a = nom_a.split(". ", 1)[-1]  # « N. Suzuki » → « Suzuki »
            pts.setdefault(nom_a, [0, 0])[1] += 1
    if not pts:
        return ""
    ordre = sorted(pts.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), -kv[1][0]))

    def fiche_joueur(g, a):
        morceaux = []
        if g:
            morceaux.append(f"{g} but{'s' if g > 1 else ''}")
        if a:
            morceaux.append(f"{a} passe{'s' if a > 1 else ''}")
        return " et ".join(morceaux)

    return " ; ".join(f"{qui}, {fiche_joueur(g, a)}" for qui, (g, a) in ordre)


def points_du_match(fiche, t):
    """Les marqueurs des deux équipes, en une ou deux phrases."""
    nous, eux = cotes(fiche, t)
    phrases = []
    m = marqueurs(fiche, nous["abbrev"])
    if m:
        phrases.append(f"Points {du(t)} : {m}.")
    e = marqueurs(fiche, eux["abbrev"])
    if e:
        phrases.append(f"Pour {court(eux)} : {e}.")
    return " ".join(phrases)


# --- Les outils --------------------------------------------------------------


def horaire_lnh(equipe_="", nombre=5):
    t = equipe(equipe_)
    nombre = max(1, min(int(nombre), 10))
    a_venir = [g for g in season(t) if g["gameState"] in ("FUT", "PRE")][:nombre]
    if not a_venir:
        return f"Plus aucun match {du(t)} à l'horaire cette saison."
    return "\n".join(f"{quand(g).capitalize()} : {affiche(g, t)}{tele(g)}." for g in a_venir)


def resultats_lnh(equipe_="", nombre=5):
    t = equipe(equipe_)
    nombre = max(1, min(int(nombre), 10))
    finis = [g for g in season(t) if g["gameState"] in ("OFF", "FINAL")][-nombre:]
    if not finis:
        return f"{sujet(t, True)} n'{v(t, 'a', 'ont')} encore joué aucun match cette saison."
    lignes = []
    for g in reversed(finis):
        ligne = f"{jour(g).capitalize()} : {pointage(g, t)}."
        try:
            m = marqueurs(landing(g), t)
            if m:
                ligne += f" Points : {m}."
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
            pass  # le résultat reste vrai sans ses marqueurs
        lignes.append(ligne)
    return "\n".join(lignes)


def match_lnh(equipe_=""):
    """Le match en cours s'il y en a un ; sinon celui de ce soir, sinon le dernier et le prochain."""
    t = equipe(equipe_)
    games = season(t)
    now = datetime.now(timezone.utc)
    for g in games:
        start = local_start(g)
        if not (now - timedelta(hours=6) <= start <= now + timedelta(hours=18)):
            continue
        live = landing(g)
        state = live.get("gameState")
        if state in ("LIVE", "CRIT"):
            return en_direct(live, t)
        if state in ("OFF", "FINAL"):
            return f"Le match est terminé. {pointage(live, t)}. {points_du_match(live, t)}".strip()
        if state in ("FUT", "PRE"):
            return f"Pas de match en cours. {quand(g).capitalize()}, {affiche(g, t)}{tele(g)}."
    dernier = [g for g in games if g["gameState"] in ("OFF", "FINAL")]
    prochain = [g for g in games if g["gameState"] in ("FUT", "PRE")]
    morceaux = [f"Pas de match en cours pour {sujet(t)}."]
    if dernier:
        morceaux.append(f"Dernier match, {jour(dernier[-1])} : {pointage(dernier[-1], t)}.")
    if prochain:
        morceaux.append(f"Prochain : {quand(prochain[0])}, {affiche(prochain[0], t)}{tele(prochain[0])}.")
    return " ".join(morceaux)


def en_direct(live, t):
    nous, eux = cotes(live, t)
    m, a = nous.get("score", 0), eux.get("score", 0)
    if m == a:
        etat = f"{sujet(t, True)} et {nom(eux)} sont à égalité {m} à {a}"
    elif m > a:
        etat = f"{sujet(t, True)} {v(t, 'mène', 'mènent')} {m} à {a} contre {nom(eux)}"
    else:
        etat = f"{sujet(t, True)} {v(t, 'tire', 'tirent')} de l'arrière {m} à {a} contre {nom(eux)}"
    pd, clock = live.get("periodDescriptor", {}), live.get("clock", {})
    if clock.get("inIntermission"):
        n = pd.get("number", 1)
        moment = f"pendant l'entracte après la {n}{'re' if n == 1 else 'e'} période"
    elif pd.get("periodType") == "SO":
        moment = "en tirs de barrage"
    else:
        moment = f"{periode(pd)}, il reste {minutes(clock.get('timeRemaining', ''))}"
    phrases = [f"{etat}, {moment}."]
    if "sog" in nous and "sog" in eux:
        phrases.append(f"Tirs au but : {nous['sog']} pour {sujet(t)}, {eux['sog']} pour {court(eux)}.")
    tous = buts(live)
    if tous:
        b = tous[-1]
        qui = (b.get("name") or {}).get("default", "?")
        equipe_b = (b.get("teamAbbrev") or {}).get("default", "")
        phrases.append(f"Dernier but : {qui} ({equipe_b}), à {minutes(b.get('timeInPeriod', ''))} de la période.")
    pts = points_du_match(live, t)
    if pts:
        phrases.append(pts)
    return " ".join(phrases)


def classement_lnh(equipe_=""):
    rows = nhl("/standings/now", 600).get("standings", [])
    par_equipe = {r["teamAbbrev"]["default"]: r for r in rows}
    if simple(equipe_) in TOUTES:
        canadiennes = sorted((par_equipe[t] for t in EQUIPES if t in par_equipe),
                             key=lambda r: (-r["points"], r["gamesPlayed"]))
        lignes = [f"{sujet(r['teamAbbrev']['default'], True)} : {pl(r['points'], 'point')} en {pl(r['gamesPlayed'], 'match')}, "
                  f"{r['divisionSequence']}e de la division {DIVISIONS.get(r['divisionName'], r['divisionName'])}."
                  for r in canadiennes]
        return "Les équipes canadiennes, de la meilleure à la moins bonne :\n" + "\n".join(lignes)
    t = equipe(equipe_)
    me = par_equipe.get(t)
    if not me:
        return "Le classement n'est pas disponible pour l'instant."
    fiche = f"{pl(me['wins'], 'victoire')}, {pl(me['losses'], 'défaite')}"
    if me["otLosses"]:
        fiche += f" et {me['otLosses']} en prolongation"
    phrases = [
        f"{sujet(t, True)} {v(t, 'a', 'ont')} {pl(me['points'], 'point')} en {pl(me['gamesPlayed'], 'match')} ({fiche}).",
        f"{me['divisionSequence']}e de la division {DIVISIONS.get(me['divisionName'], me['divisionName'])}, "
        f"{me['conferenceSequence']}e dans {CONFERENCES.get(me.get('conferenceName'), 'son association')}, "
        f"{me['leagueSequence']}e de la ligue.",
    ]
    if me.get("wildcardSequence"):
        phrases.append(f"{me['wildcardSequence']}e dans la course aux places de quatrième as.")
    return " ".join(phrases)


def _nom_joueur(p):
    return f"{p['firstName']['default']} {p['lastName']['default']}"


def stats_joueurs(equipe_="", joueur="", nombre=5):
    t = equipe(equipe_)
    nombre = max(1, min(int(nombre), 15))
    data = nhl(f"/club-stats/{t}/now", 600)
    patineurs, gardiens = data.get("skaters", []), data.get("goalies", [])
    if joueur.strip():
        cherche = simple(joueur)
        trouves = [p for p in patineurs + gardiens
                   if cherche in simple(_nom_joueur(p)) or simple(p["lastName"]["default"]) in cherche.split()]
        if not trouves:
            return f"Je ne trouve pas « {joueur} » chez {sujet(t)} cette saison."
        return "\n".join(ligne_joueur(p) for p in trouves[:3])
    meilleurs = sorted(patineurs, key=lambda p: (-p["points"], -p["goals"]))[:nombre]
    lignes = [f"Meilleurs pointeurs {du(t)} cette saison :"] + [ligne_joueur(p) for p in meilleurs]
    if gardiens:
        g = max(gardiens, key=lambda p: p.get("gamesStarted", p["gamesPlayed"]))
        lignes.append("Gardien le plus utilisé : " + ligne_joueur(g))
    return "\n".join(lignes)


def ligne_joueur(p):
    nom_ = _nom_joueur(p)
    if "savePercentage" in p:
        fiche = f"{pl(p['wins'], 'victoire')}, {pl(p['losses'], 'défaite')}"
        if p.get("overtimeLosses"):
            fiche += f" et {p['overtimeLosses']} en prolongation"
        return (f"{nom_}, gardien : {pl(p['gamesPlayed'], 'match')}, {fiche}, moyenne de "
                f"{nombre_fr(p['goalsAgainstAverage'], 2)} buts alloués, taux d'arrêts de "
                f"{nombre_fr(p['savePercentage'] * 100, 1)} %"
                + (f", {p['shutouts']} blanchissage{'s' if p['shutouts'] > 1 else ''}" if p.get("shutouts") else "")
                + ".")
    pm = p.get("plusMinus", 0)
    return (f"{nom_}, {POSITIONS.get(p.get('positionCode'), 'joueur')} : {pl(p['goals'], 'but')}, "
            f"{pl(p['assists'], 'passe')}, {pl(p['points'], 'point')} en {pl(p['gamesPlayed'], 'match')}, différentiel "
            f"{'plus ' if pm > 0 else 'moins ' if pm < 0 else ''}{abs(pm)}.")


def equipes_canadiennes(jour_=""):
    """Tous les matchs des équipes canadiennes d'une journée : aujourd'hui, hier, demain ou AAAA-MM-JJ."""
    # Le jour de l'Est, PAS currentDate de /score/now : la « journée de hockey »
    # de la LNH ne bascule qu'en fin de matinée. À 7 h le 22 octobre, /score/now
    # disait encore le 21 : « aujourd'hui » rendait le match d'hier soir, et
    # « hier » celui de l'avant-veille.
    base = aujourdhui_est()
    j = simple(jour_)
    cible = {"": base, "aujourd'hui": base, "aujourdhui": base, "ce soir": base,
             "hier": base - timedelta(days=1), "hier soir": base - timedelta(days=1),
             "demain": base + timedelta(days=1)}.get(j)
    if cible is None:
        try:
            cible = date.fromisoformat(j)
        except ValueError:
            return f"Je ne comprends pas le jour « {jour_} » : dis aujourd'hui, hier, demain ou une date AAAA-MM-JJ."
    data = nhl(f"/score/{cible.isoformat()}", 30 if cible == base else 60 if cible > base else 3600)
    titre = {base: "Aujourd'hui", base - timedelta(days=1): "Hier",
             base + timedelta(days=1): "Demain"}.get(cible, f"Le {cible.day} {MOIS[cible.month - 1]}")
    lignes = []
    for g in data.get("games", []):
        ca = [x for x in (g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]) if x in EQUIPES]
        if not ca:
            continue
        t = ca[0]  # deux équipes canadiennes : le point de vue de celle qui reçoit
        nous, eux = cotes(g, t)
        etat = g.get("gameState")
        if etat in ("FUT", "PRE"):
            lignes.append(f"{heure(local_start(g))} : {affiche(g, t)}{tele(g)}.")
        elif etat in ("LIVE", "CRIT"):
            m, a = nous.get("score", 0), eux.get("score", 0)
            lignes.append(f"En cours : {sujet(t)} {m}, {court(eux)} {a}, {periode(g.get('periodDescriptor', {}))}.")
        else:
            m, a = nous.get("score", 0), eux.get("score", 0)
            verbe = v(t, "l'emporte", "l'emportent") if m > a else v(t, "s'incline", "s'inclinent")
            lignes.append(f"Terminé : {sujet(t)} {verbe} {max(m, a) if m > a else m} à "
                          f"{min(m, a) if m > a else a}{fin(g)} contre {court(eux)}.")
    if not lignes:
        return f"{titre}, aucune équipe canadienne ne joue."
    return f"{titre}, les équipes canadiennes :\n" + "\n".join(lignes)


DEFAUT = defaut(os.environ.get("HOCKEY_EQUIPE", ""))
EQUIPE = {"type": "string",
          "description": "Une équipe canadienne : Canadien, Maple Leafs, Sénateurs, Jets, Flames, Oilers ou "
                         f"Canucks (le nom ou la ville). Vide = {NOMS_COMPLETS[DEFAUT]}."}
NOMBRE = {"type": "integer", "minimum": 1, "maximum": 10, "default": 5,
          "description": "Combien de matchs (1 à 10)."}
TOOLS = {
    "match_lnh": (match_lnh, {
        "description": "Le match d'une équipe canadienne de la LNH en ce moment : pointage en direct, période, "
                       "temps restant, tirs, qui a marqué et qui a obtenu des passes. S'il n'y a pas de match "
                       "en cours, le dernier résultat et le prochain match. Pour « c'est quoi le score ? ».",
        "inputSchema": {"type": "object", "properties": {"equipe": EQUIPE}},
    }),
    "horaire_lnh": (horaire_lnh, {
        "description": "Les prochains matchs d'une équipe canadienne de la LNH : date, heure de l'Est, "
                       "adversaire, domicile ou visite, chaîne de télé canadienne.",
        "inputSchema": {"type": "object", "properties": {"equipe": EQUIPE, "nombre": NOMBRE}},
    }),
    "resultats_lnh": (resultats_lnh, {
        "description": "Les derniers résultats d'une équipe canadienne de la LNH, du plus récent au plus "
                       "ancien, avec les joueurs de l'équipe qui ont obtenu des points dans chaque match.",
        "inputSchema": {"type": "object", "properties": {"equipe": EQUIPE, "nombre": NOMBRE}},
    }),
    "classement_lnh": (classement_lnh, {
        "description": "Le classement d'une équipe canadienne : points, fiche, rangs dans la division, "
                       "l'association et la ligue. Avec equipe = « toutes », les sept équipes canadiennes "
                       "de la meilleure à la moins bonne.",
        "inputSchema": {"type": "object", "properties": {"equipe": EQUIPE}},
    }),
    "stats_joueurs": (stats_joueurs, {
        "description": "Les statistiques de la saison des joueurs d'une équipe canadienne : buts, passes, "
                       "points, différentiel ; pour les gardiens, victoires, moyenne et taux d'arrêts. Sans "
                       "joueur nommé, les meilleurs pointeurs et le gardien numéro un.",
        "inputSchema": {"type": "object", "properties": {
            "equipe": EQUIPE,
            "joueur": {"type": "string", "description": "Nom (ou nom de famille) d'un joueur, facultatif."},
            "nombre": {"type": "integer", "minimum": 1, "maximum": 15, "default": 5,
                       "description": "Combien de meilleurs pointeurs (1 à 15)."},
        }},
    }),
    "equipes_canadiennes": (equipes_canadiennes, {
        "description": "Tous les matchs des équipes canadiennes d'une journée, avec l'heure ou le pointage : "
                       "« Qui joue ce soir ? », « Comment ont fait les équipes canadiennes hier ? ».",
        "inputSchema": {"type": "object", "properties": {
            "jour": {"type": "string", "description": "aujourd'hui (défaut), hier, demain, ou une date AAAA-MM-JJ."},
        }},
    }),
}
for _name, (_fn, _spec) in TOOLS.items():
    _spec["name"] = _name
    _spec["annotations"] = {"readOnlyHint": True, "openWorldHint": True}
# Les noms des arguments dans le schéma (equipe, jour) diffèrent des noms
# Python (equipe_, jour_) pour ne pas masquer les fonctions du même nom.
RENOMME = {"equipe": "equipe_", "jour": "jour_"}

INSTRUCTIONS = ("Les sept équipes canadiennes de la LNH : horaire, pointage en direct avec les marqueurs, "
                f"résultats, classement et statistiques des joueurs. {NOMS_COMPLETS[DEFAUT][0].upper()}"
                f"{NOMS_COMPLETS[DEFAUT][1:]} par défaut. "
                "Les réponses sont déjà en phrases courtes, prêtes à lire à voix haute.")


# --- JSON-RPC ----------------------------------------------------------------


def handle(msg):
    """Un message JSON-RPC → sa réponse, ou None pour une notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hockey", "title": "Hockey canadien", "version": "2.0.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [spec for _, spec in TOOLS.values()]})
    if method == "tools/call":
        tool = TOOLS.get(params.get("name"))
        if not tool:
            return err(-32602, f"Outil inconnu : {params.get('name')}")
        args = {RENOMME.get(k, k): val for k, val in (params.get("arguments") or {}).items()}
        try:
            text, is_error = tool[0](**args), False
        except TypeError as e:
            return err(-32602, f"Arguments invalides : {e}")
        except EquipeInconnue as e:
            text, is_error = (f"« {e} » n'est pas une équipe canadienne de la LNH. Je connais le Canadien, "
                              "les Maple Leafs, les Sénateurs, les Jets, les Flames, les Oilers et les Canucks."), True
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            print(f"hockey: {params.get('name')} failed: {e!r}", file=sys.stderr, flush=True)
            text, is_error = "Le site de la LNH ne répond pas en ce moment ; réessaie dans une minute.", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Méthode inconnue : {method}")


class Handler(BaseHTTPRequestHandler):
    server_version = "hockey-mcp/2.0"

    def _send(self, code, body=None, ctype="application/json"):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        # Pas de flux SSE côté serveur : la spécification demande 405 dans ce cas.
        self._send(405 if self.path.startswith("/mcp") else 404)

    def do_DELETE(self):
        self._send(405 if self.path.startswith("/mcp") else 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            return self._send(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413)
        try:
            payload = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if isinstance(payload, list):
            replies = [r for r in map(handle, payload) if r is not None]
            return self._send(200, replies) if replies else self._send(202)
        reply = handle(payload)
        self._send(200, reply) if reply is not None else self._send(202)

    def log_message(self, fmt, *args):
        # Une ligne par requête, sans l'adresse du client (derrière un proxy, c'est la sienne de toute façon).
        print(f"hockey: {self.command} {self.path} {args[1] if len(args) > 1 else ''}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print(f"hockey: listening on {HOST or '*'}:{PORT} (équipe par défaut {DEFAUT})", file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
