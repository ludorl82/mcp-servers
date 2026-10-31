#!/usr/bin/env python3
"""Serveur MCP « meteo » : la météo d'un lieu par défaut, ou d'un lieu nommé.

L'outil principal s'appelle simplement `meteo` : un prompt d'assistant peut
dire « Météo, température, pluie → appelle meteo » sans plus de détour.

Sources, toutes deux redistribuables avec mention de la source (c'est
pourquoi le serveur peut être public) :
- Open-Meteo (api.open-meteo.com), sans clé, données sous CC BY 4.0 : la
  météo et le géocodage. L'heure locale vient de la réponse
  (timezone=auto), donc pas de tzdata.
- Environnement et Changement climatique Canada (api.weather.gc.ca), Licence
  du gouvernement ouvert – Canada : la Cote air santé, à la station la plus
  proche. Il n'y a PAS de pollen : aucune source ouverte ne le couvre au
  Canada (Open-Meteo ne l'a qu'en Europe).
Les alertes et la Cote air santé ne couvrent que le Canada ; la météo,
partout.

Configuration, par variables d'environnement :
- METEO_LIEU : le lieu par défaut (« Sherbrooke », « London, ON »), géocodé
  par Open-Meteo. Le serveur ne le dit JAMAIS dans ses réponses, il dit
  METEO_NOM à la place : le lieu peut rester privé (un Secret, pas git).
  Sans lui, le serveur tourne, et chaque appel doit nommer un lieu.
- METEO_NOM : comment dire le lieu par défaut. Défaut : « la maison ».
- HOST, PORT : l'adresse d'écoute. Défaut : toutes les interfaces, port 8080.

Transport, comme hockey : MCP « Streamable HTTP » sans état, réponse JSON
simple, bibliothèque standard seulement.

Les réponses sont des DONNÉES déjà en mots (« maximum 14 degrés »), l'ancrage
temporel en DERNIER, comme l'ancien script : le modèle choisit ce qu'il dit.
"""
import inspect
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
ECCC = "https://api.weather.gc.ca/collections"
AQHI_RAYON_KM = 60
SOURCES = ("Sources : Open-Meteo.com (CC BY 4.0) pour la météo ; Environnement et Changement climatique "
           "Canada pour les alertes et la Cote air santé — contient des informations visées par la Licence du "
           "gouvernement ouvert – Canada.")
HOST = os.environ.get("HOST", "")
PORT = int(os.environ.get("PORT", "8080"))
HOME = os.environ.get("METEO_LIEU", "").strip()
NOM_HOME = os.environ.get("METEO_NOM", "").strip() or "la maison"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")
# Codes météo de l'OMM, tels qu'Open-Meteo les rend.
CIEL = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine faible", 53: "bruine", 55: "bruine forte",
    56: "bruine verglaçante", 57: "bruine verglaçante forte",
    61: "pluie faible", 63: "pluie", 65: "pluie forte",
    66: "pluie verglaçante", 67: "pluie verglaçante forte",
    71: "neige faible", 73: "neige", 75: "neige forte", 77: "grésil",
    80: "averses faibles", 81: "averses", 82: "averses violentes",
    85: "averses de neige", 86: "averses de neige fortes",
    95: "orage", 96: "orage avec grêle", 99: "orage avec forte grêle",
}

# --- Open-Meteo, avec un petit cache ------------------------------------------

_cache = {}
_lock = threading.Lock()


def fetch(url, params, ttl):
    key = url + "?" + urllib.parse.urlencode(params)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(key, headers={"User-Agent": "mcp-servers-meteo (+https://github.com/ludorl82/mcp-servers)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (now, data)
    return data


class LieuInconnu(Exception):
    pass


def simple(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(s.replace("-", " ").replace(".", " ").split())


# Les abréviations des provinces et territoires, vers le nom que rend le
# géocodeur en français (« London, ON » → l'Ontario).
PROVINCES = {
    "qc": "quebec", "on": "ontario", "bc": "colombie britannique", "cb": "colombie britannique",
    "ab": "alberta", "mb": "manitoba", "sk": "saskatchewan", "ns": "nouvelle ecosse",
    "ne": "nouvelle ecosse", "nb": "nouveau brunswick", "nl": "terre neuve", "tnl": "terre neuve",
    "pe": "ile du prince edouard", "ipe": "ile du prince edouard", "yt": "yukon",
    "nt": "territoires du nord ouest", "tno": "territoires du nord ouest", "nu": "nunavut",
}


def geocode(lieu):
    """« Montréal », « London, ON » ou « Paris, France » → (lat, lon, nom à dire).

    Les villes canadiennes passent d'abord : une deuxième recherche limitée au
    Canada trouve « Saint-Jean » ou « Laval » que la recherche mondiale
    classe derrière des homonymes français. Une ville étrangère ne l'emporte
    que si elle est plus de dix fois plus peuplée (Paris, Londres) ; le nom
    rendu dit toujours la province ou la région, pour qu'on puisse préciser.
    """
    nom, _, precision = (s.strip() for s in lieu.partition(","))
    query = {"name": nom, "count": 10, "language": "fr", "format": "json"}
    monde = fetch(GEOCODE, query, 86400).get("results") or []
    canada = fetch(GEOCODE, {**query, "countryCode": "CA"}, 86400).get("results") or []
    if precision:
        p = PROVINCES.get(simple(precision), simple(precision))
        vus, candidats = set(), []
        for r in canada + monde:
            if r.get("id") in vus:
                continue
            vus.add(r.get("id"))
            if p in simple(" ".join(str(r.get(k, "")) for k in ("country", "country_code", "admin1", "admin2"))):
                candidats.append(r)
        if not candidats:
            raise LieuInconnu(lieu)
        r = candidats[0]
    elif canada and (not monde or monde[0].get("country_code") == "CA"
                     or (canada[0].get("population") or 0) * 10 >= (monde[0].get("population") or 0)):
        r = canada[0]
    elif monde:
        r = monde[0]
    else:
        raise LieuInconnu(lieu)
    dit = r["name"] + (f", {r['admin1']}" if r.get("admin1") and r["admin1"] != r["name"] else "")
    return r["latitude"], r["longitude"], dit


def lieu_de(lieu):
    lieu = (lieu or "").strip()
    if lieu:
        return geocode(lieu)
    if not HOME:
        raise LieuInconnu("(aucun lieu par défaut configuré)")
    lat, lon, _ = geocode(HOME)
    return lat, lon, NOM_HOME


def prevision(lat, lon, jours):
    return fetch(FORECAST, {
        "latitude": round(lat, 3), "longitude": round(lon, 3), "timezone": "auto",
        "forecast_days": jours, "wind_speed_unit": "kmh",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,"
                   "wind_gusts_10m,relative_humidity_2m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,wind_speed_10m_max",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code",
    }, 300)


# --- La Cote air santé (Environnement Canada) ----------------------------------


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def station_aqhi(lat, lon):
    """La station de la Cote air santé la plus proche, si elle est à moins de AQHI_RAYON_KM."""
    stations = fetch(f"{ECCC}/aqhi-stations/items", {"f": "json", "limit": 500}, 86400).get("features") or []
    best = min(((distance_km(lat, lon, f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]), f)
                for f in stations), default=None, key=lambda x: x[0])
    if not best or best[0] > AQHI_RAYON_KM:
        return None, None
    return best[1]["properties"], best[0]


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def qualite(lat, lon):
    """{station, km, actuel, periodes} ou None hors du réseau canadien.

    Deux sortes de prévisions sous la même collection : heure par heure
    (forecast_datetime), qui donne la valeur du moment quand la station n'a
    pas d'observation (Montréal n'en a pas : le Québec y publie son propre
    indice), et par période (forecast_period : « Ce soir et cette nuit »,
    « Demain »…), qui se dit tel quel.
    """
    st, km = station_aqhi(lat, lon)
    if not st:
        return None
    lid = st["location_id"]
    obs = fetch(f"{ECCC}/aqhi-observations-realtime/items",
                {"f": "json", "location_id": lid, "sortby": "-observation_datetime", "limit": 1}, 600)
    prev = fetch(f"{ECCC}/aqhi-forecasts-realtime/items",
                 {"f": "json", "location_id": lid, "sortby": "-publication_datetime", "limit": 100}, 600)
    now = datetime.now(timezone.utc)
    tous = [f["properties"] for f in prev.get("features") or []]
    fc = [p for p in tous if "forecast_datetime" in p]
    if fc:
        derniere = max(p["publication_datetime"] for p in fc)
        fc = sorted((p for p in fc if p["publication_datetime"] == derniere), key=lambda p: p["forecast_datetime"])
    par_periode = sorted((p for p in tous if "forecast_period" in p), key=lambda p: p["publication_datetime"])
    periodes = []
    if par_periode:
        bloc = par_periode[-1]["forecast_period"]
        for cle in sorted(bloc, key=lambda k: int(k.rsplit("_", 1)[-1])):
            per = bloc[cle]
            if per.get("aqhi") is not None:
                periodes.append((per.get("forecast_period_fr") or per.get("forecast_period_en", "?"), per["aqhi"]))
    actuel = None
    o = [f["properties"] for f in obs.get("features") or []]
    if o and now - utc(o[0]["observation_datetime"]) < timedelta(hours=3):
        actuel = o[0]["aqhi"]
    else:
        passees = [p for p in fc if utc(p["forecast_datetime"]) <= now]
        if passees:
            actuel = passees[-1]["aqhi"]
    return {"station": st.get("location_name_fr") or st.get("location_name_en"), "km": km, "actuel": actuel,
            "periodes": periodes[:3]}


def cote(x):
    n = max(1, round(x))
    if n > 10:
        return "plus de 10, risque très élevé"
    return f"{n}, risque {'faible' if n <= 3 else 'modéré' if n <= 6 else 'élevé'}"


def conseil(x):
    n = round(x)
    if n >= 7:
        return "Tout le monde devrait réduire les efforts intenses à l'extérieur."
    if n >= 4:
        return "Les personnes sensibles devraient envisager de réduire les efforts intenses à l'extérieur."
    return ""


# --- Les alertes météo (Environnement Canada) -----------------------------------


def dans_polygone(lat, lon, anneau):
    """Lancer de rayon sur un anneau GeoJSON ([lon, lat], ...)."""
    dedans, j = False, len(anneau) - 1
    for i in range(len(anneau)):
        xi, yi = anneau[i][0], anneau[i][1]
        xj, yj = anneau[j][0], anneau[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dedans = not dedans
        j = i
    return dedans


def dans_geometrie(lat, lon, g):
    polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) \
        if g.get("type") == "MultiPolygon" else []
    # Un polygone = un contour puis des trous : dedans le contour et hors de chaque trou.
    return any(dans_polygone(lat, lon, p[0]) and not any(dans_polygone(lat, lon, h) for h in p[1:])
               for p in polys if p)


def alertes(lat, lon):
    """Les alertes en vigueur sur le point, une par sorte (les zones se recouvrent)."""
    data = fetch(f"{ECCC}/weather-alerts/items", {"f": "json", "limit": 1000}, 300)
    vues, res = set(), []
    for f in data.get("features") or []:
        p = f["properties"]
        if p.get("status_en") == "ended" or not dans_geometrie(lat, lon, f.get("geometry") or {}):
            continue
        cle = (p.get("alert_code"), p.get("alert_name_fr"))
        if cle in vues:
            continue
        vues.add(cle)
        res.append(p)
    ordre = {"warning": 0, "watch": 1, "advisory": 2, "statement": 3}
    return sorted(res, key=lambda p: ordre.get(p.get("alert_type"), 9))


def resume(texte, limite=350):
    """Le premier paragraphe d'un texte d'alerte, coupé à une phrase."""
    t = " ".join((texte or "").split("\n\n")[0].split())
    if len(t) <= limite:
        return t
    coupe = t.rfind(". ", 0, limite)
    return t[:coupe + 1] if coupe > 0 else t[:limite].rsplit(" ", 1)[0] + "…"


# --- Mise en mots ---------------------------------------------------------------


def deg(x):
    n = round(x)
    return f"{'moins ' if n < 0 else ''}{abs(n)} degré{'s' if abs(n) > 1 else ''}"


def kmh(x):
    n = round(x)
    return f"{n} kilomètre{'s' if n > 1 else ''} à l'heure"


def ciel(code):
    return CIEL.get(int(code), "conditions inconnues")


def jour_dit(d, i):
    nom = f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"
    return {0: f"aujourd'hui ({nom})", 1: f"demain ({nom})"}.get(i, nom)


def heure_dite(d):
    return f"{d.hour} h" + (f" {d.minute:02d}" if d.minute else "")


def ancrage(data):
    now = datetime.fromisoformat(data["current"]["time"])
    return (f"Nous sommes {JOURS[now.weekday()]} le {now.strftime('%d/%m/%Y')} "
            f"et il est {now.strftime('%H:%M')}, heure locale.")


# --- Les outils ---------------------------------------------------------------


def meteo(lieu="", jours=7):
    jours = max(1, min(int(jours), 14))
    lat, lon, dit = lieu_de(lieu)
    data = prevision(lat, lon, max(jours, 2))
    c = data["current"]
    maintenant = (f"Maintenant : {deg(c['temperature_2m'])}, ressentie {deg(c['apparent_temperature'])}, "
                  f"{ciel(c['weather_code'])}, vent {kmh(c['wind_speed_10m'])} "
                  f"(rafales {round(c['wind_gusts_10m'])}), humidité {round(c['relative_humidity_2m'])} %.")
    d = data["daily"]
    lignes = []
    for i in range(min(jours, len(d["time"]))):
        jour = datetime.fromisoformat(d["time"][i])
        ligne = (f"{jour_dit(jour, i)} : {ciel(d['weather_code'][i])}, maximum {deg(d['temperature_2m_max'][i])}, "
                 f"minimum {deg(d['temperature_2m_min'][i])}")
        pluie, proba = d["precipitation_sum"][i] or 0, d["precipitation_probability_max"][i]
        if pluie >= 0.5:
            ligne += f", précipitations {round(pluie)} millimètre{'s' if round(pluie) > 1 else ''}"
            if proba is not None:
                ligne += f" (probabilité {proba} %)"
        elif proba is not None and proba >= 20:
            ligne += f", risque de précipitations {proba} %"
        lignes.append(ligne + ".")
    air = []
    try:
        q = qualite(lat, lon)
        if q and q["actuel"] is not None:
            air = [f"Qualité de l'air : Cote air santé {cote(q['actuel'])}."]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as e:
        print(f"meteo: Cote air santé indisponible: {e!r}", file=sys.stderr, flush=True)
    alerte = []
    try:
        a = alertes(lat, lon)
        if a:
            alerte = ["Alertes en vigueur : " + ", ".join(p["alert_name_fr"] for p in a) + "."]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as e:
        print(f"meteo: alertes indisponibles: {e!r}", file=sys.stderr, flush=True)
    return "\n".join([f"Lieu : {dit}.", maintenant, *alerte, *air, "Prévisions :", *lignes, ancrage(data)])


def alertes_meteo(lieu=""):
    lat, lon, dit = lieu_de(lieu)
    a = alertes(lat, lon)
    if not a:
        return f"Aucune alerte météo en vigueur pour {dit}." + (
            "" if dit == NOM_HOME else " (Environnement Canada ne publie d'alertes qu'au Canada.)")
    lignes = [f"Lieu : {dit}." if dit == NOM_HOME else f"Alertes pour {dit} :"]
    for p in a:
        couleur = f", niveau {p['risk_colour_fr']}" if p.get("risk_colour_fr") else ""
        statut = " (mise à jour)" if p.get("status_en") == "updated" or p.get("status_fr") == "maintenu" else ""
        lignes.append(f"{p['alert_name_fr'].capitalize()}{couleur}{statut} : {resume(p.get('alert_text_fr'))}")
    return "\n".join(lignes)


def qualite_air(lieu=""):
    lat, lon, dit = lieu_de(lieu)
    data = prevision(lat, lon, 2)
    q = qualite(lat, lon)
    if not q:
        return (f"Pas de Cote air santé pour {dit} : Environnement Canada ne la publie qu'au Canada, "
                f"et aucune de ses stations n'est à moins de {AQHI_RAYON_KM} kilomètres.")
    loin = f" (à {round(q['km'])} kilomètres)" if q["km"] >= 5 else ""
    phrases = [f"Cote air santé, station {q['station']}{loin}"]
    if q["actuel"] is not None:
        phrases[0] += f" : maintenant {cote(q['actuel'])}."
        if conseil(q["actuel"]):
            phrases.append(conseil(q["actuel"]))
    else:
        phrases[0] += " : pas de valeur pour l'heure qui vient de passer."
    for periode, valeur in q["periodes"]:
        phrases.append(f"{periode[0].upper() + periode[1:]} : {cote(valeur)}.")
    if dit == NOM_HOME:
        phrases.insert(0, f"Lieu : {NOM_HOME}.")
    return "\n".join(phrases + [ancrage(data)])


def meteo_heure_par_heure(lieu="", heures=12):
    heures = max(1, min(int(heures), 48))
    lat, lon, dit = lieu_de(lieu)
    data = prevision(lat, lon, 3)
    h = data["hourly"]
    now = datetime.fromisoformat(data["current"]["time"]).replace(minute=0)
    pas = 1 if heures <= 12 else 3
    lignes, vus = [], 0
    for i, t in enumerate(h["time"]):
        moment = datetime.fromisoformat(t)
        if moment < now:
            continue
        if vus % pas == 0:
            ligne = f"{heure_dite(moment)} : {deg(h['temperature_2m'][i])}, {ciel(h['weather_code'][i])}"
            proba = h["precipitation_probability"][i]
            if proba is not None and proba >= 20:
                ligne += f", risque de précipitations {proba} %"
            if moment.date() != now.date():
                ligne = f"{JOURS[moment.weekday()]} {ligne}"
            lignes.append(ligne + ".")
        vus += 1
        if vus >= heures:
            break
    return "\n".join([f"Lieu : {dit}.", *lignes, ancrage(data)])


LIEU = {"type": "string",
        "description": "Ville ou lieu : toute ville canadienne (« Rimouski », « London, ON ») ou étrangère "
                       "(« Paris, France »). Ajouter la province ou le pays après une virgule pour lever "
                       "une ambiguïté. " + (f"Laisser vide pour {NOM_HOME}." if HOME else
                                            "Obligatoire : aucun lieu par défaut n'est configuré.")}
PAR_DEFAUT = f"À {NOM_HOME} par défaut, ou au lieu demandé" if HOME else "Au lieu demandé"
PAR_DEFAUT_MIN = f"à {NOM_HOME} par défaut ou au lieu demandé" if HOME else "au lieu demandé"
TOOLS = {
    "meteo": (meteo, {
        "description": "Donne la température actuelle et ressentie, le ciel, le vent, et les prévisions "
                       f"des prochains jours (maximum, minimum, précipitations). {PAR_DEFAUT}, "
                       "avec la Cote air santé au Canada. À utiliser pour toute "
                       "question sur la météo, la température, la pluie, la neige ou le temps qu'il fera. "
                       "Données Open-Meteo.com (CC BY 4.0) et Environnement Canada.",
        "inputSchema": {"type": "object", "properties": {
            "lieu": LIEU,
            "jours": {"type": "integer", "minimum": 1, "maximum": 14, "default": 7,
                      "description": "Combien de jours de prévisions (1 à 14)."},
        }},
    }),
    "meteo_heure_par_heure": (meteo_heure_par_heure, {
        "description": "La météo heure par heure pour les prochaines heures : température, ciel, risque de "
                       "précipitations. Pour « va-t-il pleuvoir cet après-midi ? » ou « quel temps à 17 h ? ».",
        "inputSchema": {"type": "object", "properties": {
            "lieu": LIEU,
            "heures": {"type": "integer", "minimum": 1, "maximum": 48, "default": 12,
                       "description": "Combien d'heures (1 à 48 ; au-delà de 12, une heure sur trois)."},
        }},
    }),
    "qualite_air": (qualite_air, {
        "description": "La Cote air santé d'Environnement Canada (1 à 10 et plus, risque faible, modéré, "
                       "élevé) : maintenant et les prochaines périodes (ce soir, demain), à la station la plus proche, "
                       "avec le conseil pour les personnes sensibles. Au Canada seulement. Pour « la qualité "
                       "de l'air », « est-ce qu'il y a du smog ? ». Pas de pollen : aucune source ouverte au "
                       "Canada. Contient des informations visées par la Licence du gouvernement ouvert – Canada.",
        "inputSchema": {"type": "object", "properties": {"lieu": LIEU}},
    }),
    "alertes_meteo": (alertes_meteo, {
        "description": "Les alertes météo en vigueur d'Environnement Canada (avertissements, veilles, "
                       "avis, bulletins : verglas, neige, vent, orage, chaleur…) avec leur texte. Au Canada "
                       "seulement. Pour « y a-t-il une alerte ? », « un avertissement de verglas ? ». "
                       "Contient des informations visées par la Licence du gouvernement ouvert – Canada.",
        "inputSchema": {"type": "object", "properties": {"lieu": LIEU}},
    }),
}
for _name, (_fn, _spec) in TOOLS.items():
    _spec["name"] = _name
    _spec["annotations"] = {"readOnlyHint": True, "openWorldHint": True}

INSTRUCTIONS = (f"Météo actuelle, prévisions, heure par heure, alertes et Cote air santé, {PAR_DEFAUT_MIN}. "
                "Les valeurs sont déjà en mots, prêtes à lire à voix haute. " + SOURCES)


# --- Arguments admis : le même bloc dans chaque serveur MCP du labo (un test le vérifie) ---
# Gemini (2026-10-30) appelait un outil qui ne prend aucun paramètre avec des
# arguments quand même : chaque premier appel tombait en -32602.
# Comme le veut JSON Schema sans additionalProperties: false, une clé inconnue
# est ignorée, et une valeur null vaut une clé absente (le défaut de la fonction
# s'applique). Des arguments envoyés en texte JSON sont décodés ; tout autre
# chose qu'un objet rend None, et l'appel -32602. Le journal garde les NOMS
# écartés (ignores=), jamais les valeurs. Recopié tel quel dans les autres
# serveurs MCP du labo : garder toutes les copies identiques.
_appel = threading.local()


def note_appel(cle, valeur):
    """Une note pour la ligne de la requête en cours ; sans ligne ouverte, une ligne à elle."""
    valeur = "".join(c if c.isascii() and (c.isalnum() or c in "_-+.") else "?" for c in str(valeur))[:80]
    notes = getattr(_appel, "notes", None)
    if notes is None:
        print(f"arguments: {cle}={valeur}", file=sys.stderr, flush=True)
    else:
        notes[cle] = valeur


def arguments_admis(fn, args, renomme=None):
    """Les arguments d'un tools/call, ramenés à ce que la fonction de l'outil accepte."""
    if args is None or args == "":
        return {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            note_appel("args", "texte")
            return None
    if not isinstance(args, dict):
        note_appel("args", type(args).__name__)
        return None
    connus = inspect.signature(fn).parameters
    renomme = renomme or {}
    ignores = sorted(k for k, v in args.items() if renomme.get(k, k) not in connus or v is None)
    if ignores:
        note_appel("ignores", "+".join(ignores))
    return {renomme.get(k, k): v for k, v in args.items() if renomme.get(k, k) in connus and v is not None}
# --- fin des arguments admis ---


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
            "serverInfo": {"name": "meteo", "title": "Météo", "version": "1.2.0"},
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
        args = arguments_admis(tool[0], params.get("arguments"))
        if args is None:
            return err(-32602, "Arguments invalides : un objet JSON est attendu.")
        try:
            text, is_error = tool[0](**args), False
        except TypeError as e:
            return err(-32602, f"Arguments invalides : {e}")
        except LieuInconnu as e:
            text, is_error = f"Je ne trouve pas le lieu « {e} ».", True
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, IndexError) as e:
            print(f"meteo: {params.get('name')} failed: {e!r}", file=sys.stderr, flush=True)
            text, is_error = "Le service météo ne répond pas en ce moment ; réessaie dans une minute.", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Méthode inconnue : {method}")


# --- OAuth ouvert : le même bloc dans hockey, meteo et articles (un test le vérifie) ---
# Le Gemini grand public (connecteurs personnalisés) n'ajoute pas un serveur MCP
# sans OAuth : il cherche /.well-known/oauth-protected-resource et abandonne sur
# un 404 (2026-10-28). Les données sont publiques, alors l'autorisation approuve
# toute seule : enregistrement dynamique, /authorize qui renvoie tout de suite
# un code, /token qui rend un jeton. /mcp ne vérifie AUCUN jeton : avec ou sans,
# la réponse est la même. Seule garde : /authorize ne renvoie que vers des
# adresses de retour connues, pour ne pas servir de redirection ouverte.
RETOURS_OUVERTS = re.compile(r"^(https://oauth-redirect(-sandbox|-test)?\.googleusercontent\.com/"
                             r"|https://claude\.(ai|com)/api/mcp/auth_callback$"
                             r"|http://(localhost|127\.0\.0\.1)(:\d+)?/)")
METHODES_CLIENT = ("none", "client_secret_post", "client_secret_basic")


def base_ouverte(entetes):
    # L'émetteur doit être l'adresse que le client a appelée : on la tire de Host.
    hote = entetes.get("Host") or "localhost"
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", hote):
        hote = "localhost"
    return ("http://" if hote.split(":")[0] in ("localhost", "127.0.0.1") else "https://") + hote


def oauth_get(chemin, requete, entetes):
    """GET d'OAuth → (code, corps, entêtes), ou None si le chemin n'en est pas."""
    base = base_ouverte(entetes)
    if chemin in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        return 200, {"resource": base + "/mcp", "authorization_servers": [base],
                     "bearer_methods_supported": ["header"]}, {}
    if chemin in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp"):
        return 200, {"issuer": base, "authorization_endpoint": base + "/authorize",
                     "token_endpoint": base + "/token", "registration_endpoint": base + "/register",
                     "response_types_supported": ["code"],
                     "grant_types_supported": ["authorization_code", "refresh_token"],
                     "code_challenge_methods_supported": ["S256"],
                     "token_endpoint_auth_methods_supported": list(METHODES_CLIENT)}, {}
    if chemin == "/authorize":
        p = dict(urllib.parse.parse_qsl(requete))
        retour = p.get("redirect_uri", "")
        if not RETOURS_OUVERTS.match(retour) or p.get("response_type") != "code":
            return 400, {"error": "invalid_request", "error_description": "Adresse de retour inconnue."}, {}
        q = {"code": secrets.token_urlsafe(24), "iss": base}
        if p.get("state"):
            q["state"] = p["state"]
        return 302, None, {"Location": retour + ("&" if "?" in retour else "?") + urllib.parse.urlencode(q)}
    return None


def oauth_post(chemin, corps):
    """POST à /register ou /token → (code, corps, entêtes)."""
    if chemin == "/register":
        try:
            d = json.loads(corps or b"{}")
        except ValueError:
            d = None
        uris = d.get("redirect_uris") if isinstance(d, dict) else None
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            return 400, {"error": "invalid_redirect_uri", "error_description": "redirect_uris manque."}, {}
        methode = d.get("token_endpoint_auth_method") or "none"
        methode = methode if methode in METHODES_CLIENT else "none"
        r = {"client_id": secrets.token_urlsafe(16), "client_id_issued_at": int(time.time()),
             "client_name": str(d.get("client_name") or "client")[:80], "redirect_uris": uris,
             "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
             "token_endpoint_auth_method": methode}
        if methode != "none":
            r["client_secret"], r["client_secret_expires_at"] = secrets.token_urlsafe(24), 0
        return 201, r, {}
    form = dict(urllib.parse.parse_qsl(corps.decode("utf-8", "replace")))
    if form.get("grant_type") not in ("authorization_code", "refresh_token"):
        return 400, {"error": "unsupported_grant_type"}, {}
    return 200, {"access_token": secrets.token_urlsafe(24), "token_type": "Bearer", "expires_in": 30 * 24 * 3600,
                 "refresh_token": secrets.token_urlsafe(24)}, {"Cache-Control": "no-store"}
# --- fin de l'OAuth ouvert ---


# --- Qui parle : le même bloc dans chaque serveur (un test le vérifie) ---
# La ligne de requête dit QUI appelle (l'appli Claude, Gemini, Home Assistant),
# jamais avec un jeton ni un code : ua= le premier produit du User-Agent,
# client= le nom donné au /register, retour= l'hôte de l'adresse de retour à
# /authorize (claude.ai, googleusercontent.com…), grant= à /token, rpc= la
# méthode JSON-RPC (et l'outil, pour tools/call), erreur= les codes JSON-RPC,
# ou « outil » quand un outil rend isError. Les valeurs n'ont pas d'espace :
# clé=valeur, que `| logfmt` lit dans Loki.
def propre(s, n=40):
    """Une valeur de journal : courte, sans espace ni caractère de contrôle (elle vient du client)."""
    return re.sub(r"[^\w.:/@#+()-]", "", re.sub(r"\s+", "_", str(s).strip()))[:n] or "?"


def rpc(m):
    if not isinstance(m, dict):
        return "invalide"
    methode = str(m.get("method") or "invalide")
    if methode == "tools/call" and isinstance(m.get("params"), dict):
        return f"{methode}:{m['params'].get('name')}"
    return methode


def qui_parle(chemin, requete, entetes, corps=None):
    """Les notes de la ligne de requête, d'après les entêtes et, une fois lu, le corps."""
    notes = {}
    if chemin == "/healthz":
        return notes
    ua = entetes.get("User-Agent")
    notes["ua"] = propre(ua.split(" ")[0], 48) if ua else "aucun"
    if chemin == "/authorize":
        retour = dict(urllib.parse.parse_qsl(requete)).get("redirect_uri", "")
        notes["retour"] = propre(urllib.parse.urlparse(retour).netloc or "aucun")
    if not corps:
        return notes
    if chemin == "/token":
        notes["grant"] = propre(dict(urllib.parse.parse_qsl(corps.decode("utf-8", "replace"))).get("grant_type") or "aucun")
        return notes
    try:
        d = json.loads(corps)
    except ValueError:
        return notes
    if chemin == "/register" and isinstance(d, dict):
        notes["client"] = propre(d.get("client_name") or "client")
    if chemin == "/mcp":
        messages = d if isinstance(d, list) else [d]
        notes["rpc"] = propre("+".join(rpc(m) for m in messages[:5]) + ("+etc" if len(messages) > 5 else ""), 80)
    return notes


def erreurs(reponses):
    codes = []
    for r in reponses:
        if isinstance(r, dict) and "error" in r:
            codes.append(str(r["error"].get("code")))
        elif isinstance(r, dict) and isinstance(r.get("result"), dict) and r["result"].get("isError"):
            codes.append("outil")
    return ",".join(codes)
# --- fin de qui parle ---


class Handler(BaseHTTPRequestHandler):
    server_version = "meteo-mcp/1.0"

    def _send(self, code, body=None, extra=None, ctype="application/json"):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 405:
            self.send_header("Allow", "POST")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def parse_request(self):
        # Chaque requête repart à zéro : une connexion gardée ouverte en sert plusieurs.
        ok = super().parse_request()
        chemin, _, requete = self.path.partition("?")
        self.notes = qui_parle(chemin, requete, self.headers) if ok else {}
        return ok

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n", ctype="text/plain")
        chemin, _, requete = self.path.partition("?")
        r = oauth_get(chemin, requete, self.headers)
        if r:
            return self._send(*r)
        # Pas de flux SSE côté serveur : la spécification demande 405 dans ce cas.
        self._send(405 if self.path.startswith("/mcp") else 404)

    def do_DELETE(self):
        self._send(405 if self.path.startswith("/mcp") else 404)

    def do_POST(self):
        chemin = self.path.split("?")[0]
        if chemin not in ("/mcp", "/register", "/token"):
            return self._send(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413)
        corps = self.rfile.read(length)
        self.notes = qui_parle(chemin, "", self.headers, corps)
        _appel.notes = self.notes
        if chemin != "/mcp":
            return self._send(*oauth_post(chemin, corps))
        try:
            payload = json.loads(corps or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        replies = [r for r in map(handle, payload if isinstance(payload, list) else [payload]) if r is not None]
        if erreurs(replies):
            self.notes["erreur"] = erreurs(replies)
        if isinstance(payload, list):
            return self._send(200, replies) if replies else self._send(202)
        self._send(200, replies[0]) if replies else self._send(202)

    def log_message(self, fmt, *args):
        # Une ligne par requête, sans l'adresse du client ni le lieu demandé.
        notes = " ".join(f"{k}={v}" for k, v in getattr(self, "notes", {}).items())
        print(f"meteo: {self.command} {self.path.split("?")[0]} {args[1] if len(args) > 1 else ''} {notes}".rstrip(),
              file=sys.stderr, flush=True)


if __name__ == "__main__":
    print(f"meteo: listening on {HOST or '*'}:{PORT} (lieu par défaut {'configuré' if HOME else 'ABSENT'})",
          file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
