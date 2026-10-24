#!/usr/bin/env python3
"""Serveur MCP « articles » : les articles de labodeludo.dev, ceux de Bob et de Ludo.

Chercher dans les articles, en lire un, lister les derniers parus, pour une
question comme « Bob a-tu déjà écrit sur le vMotion ? ». Les deux auteurs du
blogue sont Bob, le robot du labo, et Ludo.

RIEN N'EST INDEXÉ ICI. Le site publie déjà ce qu'il faut, et le serveur le
relit chaque heure :
- /bob-vectors.json : les articles découpés en tronçons d'environ 1200
  caractères, chacun avec son vecteur bge-m3 normalisé puis quantifié en int8
  (scripts/build-bob-vectors.mjs du site). C'est l'index du chat du site ;
- /search-index.json : titre, date, description et étiquettes de chaque
  article. L'étiquette « bob » ou « ludo » donne l'auteur.
Seule la QUESTION est vectorisée en direct, par un Ollama qui sert le même
modèle que celui qui a vectorisé l'index. Si Ollama ne répond pas, la
recherche retombe sur les mots de la question, et la réponse le dit : une
recherche qui perd ses extraits en silence aurait l'air de marcher.

Le classement reprend les réglages mesurés pour le chat du site : une langue
à la fois, plancher absolu 0,45, écart 0,06 au meilleur, trois tronçons au
plus par article, les trous courts entre deux tronçons retenus comblés, le
résumé technique des deux premiers articles ajouté. Chaque extrait porte son
auteur et sa date : le « je » de Ludo n'est pas celui de Bob, et un article
décrit le labo à sa date, pas aujourd'hui.

Configuration, par variables d'environnement :
- ARTICLES_SITE : le site à relire. Défaut : https://labodeludo.dev.
- ARTICLES_EMBED : l'API d'Ollama qui vectorise la question.
  Défaut : http://localhost:11434/api/embed.
- ARTICLES_MODELE : le modèle de vecteurs ; le MÊME que celui de l'index.
  Défaut : bge-m3.
- HOST, PORT : l'adresse d'écoute. Défaut : toutes les interfaces, port 8080.
Un autre site peut s'en servir s'il publie les deux fichiers dans le même
format ; les descriptions des outils, elles, nomment labodeludo.dev.

Transport, comme hockey : MCP « Streamable HTTP » sans état, réponse JSON
simple, bibliothèque standard seulement.
"""
import base64
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = os.environ.get("ARTICLES_SITE", "https://labodeludo.dev").rstrip("/")
EMBED = os.environ.get("ARTICLES_EMBED", "http://localhost:11434/api/embed")
MODELE = os.environ.get("ARTICLES_MODELE", "bge-m3")
RAFRAICHIR_S = 3600
HOST = os.environ.get("HOST", "")
PORT = int(os.environ.get("PORT", "8080"))
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024

# Les réglages du chat du site, mesurés sur ses questions.
PLANCHER = 0.45
ECART = 0.06
PAR_ARTICLE = 3
MAX_COMBLES = 2
MAX_RESUMES = 2
RESUME = re.compile(r"^\s*>?\s*\*\*(?:Résumé technique|Technical summary)\*\*", re.I)
MAX_LECTURE = 20000

# --- Le corpus, relu du site ----------------------------------------------------

_corpus = None
_charge_a = 0.0
_lock = threading.Lock()


def lit(chemin):
    req = urllib.request.Request(SITE + chemin, headers={"User-Agent": "mcp-servers-articles (+https://github.com/ludorl82/mcp-servers)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def charge():
    vecteurs, index = lit("/bob-vectors.json"), lit("/search-index.json")
    articles = {}
    for e in index["entries"]:
        if e.get("kind") != "article":
            continue
        tags = [t.lower() for t in e.get("tags", ())]
        e["auteur"] = "Bob" if "bob" in tags else "Ludo" if "ludo" in tags else ""
        articles[(e["lang"], e["slug"])] = e
    troncons = []
    for c in vecteurs["chunks"]:
        c["i"] = int(c["i"])
        c["v"] = [b - 256 if b > 127 else b for b in base64.b64decode(c.pop("v"))]
        troncons.append(c)
    # Un article par langue, ses tronçons dans l'ordre du texte.
    par_article = defaultdict(list)
    for n, c in enumerate(troncons):
        par_article[(c["lang"], c["slug"])].append(n)
    for ns in par_article.values():
        ns.sort(key=lambda n: troncons[n]["i"])
    return {"troncons": troncons, "articles": articles, "par_article": par_article,
            "genere": vecteurs.get("generated", "")}


def corpus():
    global _corpus, _charge_a
    with _lock:
        if _corpus is None or time.monotonic() - _charge_a > RAFRAICHIR_S:
            try:
                _corpus = charge()
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                # Le site ne répond pas : on garde la dernière copie, s'il y en a une.
                print(f"articles: reload failed: {e!r}", file=sys.stderr, flush=True)
                if _corpus is None:
                    raise
            _charge_a = time.monotonic()
        return _corpus


def vectorise(texte):
    body = json.dumps({"model": MODELE, "input": [texte]}).encode()
    req = urllib.request.Request(EMBED, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        v = json.load(r)["embeddings"][0]
    norme = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norme for x in v]


# --- Mise en mots ----------------------------------------------------------------


def simple(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


MOTS_EN = re.compile(r"\b(the|and|you|is|are|what|how|why|did|does|do|have|has|your|about|with|which|when)\b")
MOTS_FR = re.compile(r"\b(le|la|les|un|une|des|est|que|qui|pour|dans|avec|quoi|comment|pourquoi|tu|ton|ta|tes|sur)\b")


def langue_de(texte, langue):
    langue = simple(langue)[:2]
    if langue in ("fr", "en"):
        return langue
    t = str(texte).lower()
    return "en" if len(MOTS_EN.findall(t)) > len(MOTS_FR.findall(t)) else "fr"


def lien(c_ou_e):
    return SITE + (c_ou_e.get("url") or c_ou_e.get("href"))


def signature(data, langue, slug):
    e = data["articles"].get((langue, slug)) or data["articles"].get(("fr", slug), {})
    return e.get("auteur") or "?"


def entete(data, langue, slug, c):
    par = "by" if langue == "en" else "par"
    return f"« {c['title']} », {par} {signature(data, langue, slug)}, {c['date']} — {lien(c)}"


def recolle(data, ns):
    """Les tronçons d'un article, dans l'ordre, en un texte.

    Le découpage du site (chunk() dans build-bob-vectors.mjs) reporte le
    dernier paragraphe d'un tronçon, s'il fait moins de 400 caractères, en
    tête du suivant : une idée coupée à la jonction reste trouvable des deux
    côtés. Recollés tels quels, ces paragraphes sortaient deux fois, souvent un
    titre de section, et Claude voyait des sections en double. On retire le
    report entre deux tronçons qui se suivent ; ailleurs, « […] » marque le saut.
    """
    morceaux, avant = [], None
    for n in ns:
        c = data["troncons"][n]
        texte = c["text"].strip()
        if avant is not None and c["i"] == avant["i"] + 1:
            report = avant["text"].strip().split("\n\n")[-1]
            if len(report) < 400 and texte.startswith(report + "\n\n"):
                texte = texte[len(report) + 2:]
            morceaux.append("\n\n" + texte)
        else:
            morceaux.append(("\n[…]\n" if avant is not None else "") + texte)
        avant = c
    return "".join(morceaux)


# --- La recherche -----------------------------------------------------------------


def classe_vecteurs(data, langue, q):
    scores = []
    for n, c in enumerate(data["troncons"]):
        if c["lang"] == langue:
            scores.append((sum(a * b for a, b in zip(q, c["v"])) / 127, n))
    scores.sort(reverse=True)
    return scores


def classe_mots(data, langue, question):
    mots = [m for m in simple(question).split() if len(m) > 3]
    scores = []
    for n, c in enumerate(data["troncons"]):
        if c["lang"] != langue:
            continue
        texte = simple(c["title"] + " " + c["text"])
        s = sum(texte.count(m) for m in mots)
        if s:
            scores.append((s, n))
    scores.sort(reverse=True)
    return scores


def retiens(data, langue, scores, nombre, vecteurs):
    if not scores or (vecteurs and scores[0][0] < PLANCHER):
        return []
    seuil = scores[0][0] - ECART if vecteurs else 0
    pris, par_slug = [], defaultdict(int)
    for score, n in scores:
        if len(pris) >= nombre or score < seuil:
            break
        slug = data["troncons"][n]["slug"]
        if par_slug[slug] >= PAR_ARTICLE:
            continue
        par_slug[slug] += 1
        pris.append(n)
    ordre = list(dict.fromkeys(data["troncons"][n]["slug"] for n in pris))
    # Les trous courts entre deux tronçons retenus d'un même article : la charnière
    # d'un raisonnement tombe souvent entre les deux.
    combles = 0
    for slug in ordre:
        rangs = sorted(data["troncons"][n]["i"] for n in pris if data["troncons"][n]["slug"] == slug)
        du_texte = {data["troncons"][n]["i"]: n for n in data["par_article"][(langue, slug)]}
        for a, b in zip(rangs, rangs[1:]):
            if b - a <= 3:
                for i in range(a + 1, b):
                    if combles < MAX_COMBLES and i in du_texte and du_texte[i] not in pris:
                        pris.append(du_texte[i])
                        combles += 1
    # Le résumé technique des deux premiers articles : écrit pour être lu hors contexte.
    for slug in ordre[:MAX_RESUMES]:
        premiers = data["par_article"][(langue, slug)]
        if premiers and premiers[0] not in pris and RESUME.match(data["troncons"][premiers[0]]["text"]):
            pris.append(premiers[0])
    return [(slug, sorted((n for n in pris if data["troncons"][n]["slug"] == slug),
                          key=lambda n: data["troncons"][n]["i"])) for slug in ordre]


def chercher_articles(question="", langue="", nombre=5):
    question = str(question or "").strip()
    if not question:
        return "De quoi veux-tu que je cherche dans les articles ?"
    nombre = max(1, min(int(nombre), 8))
    data = corpus()
    langue = langue_de(question, langue)
    note = ""
    try:
        scores, vecteurs = classe_vecteurs(data, langue, vectorise(question)), True
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
        print(f"articles: embed failed: {e!r}", file=sys.stderr, flush=True)
        scores, vecteurs = classe_mots(data, langue, question), False
        note = ("(Recherche par mots seulement : le modèle de recherche sémantique ne répond pas en ce "
                "moment, les résultats peuvent rater un article qui dit la même chose autrement.)\n")
    retenus = retiens(data, langue, scores, nombre, vecteurs)
    if not retenus:
        return note + "Aucun article ne traite vraiment de ça."
    blocs = [note + "Extraits d'articles du blogue, les plus proches de la question en premier. Chacun est "
             "daté : il décrit le labo à la date de l'article, pas forcément aujourd'hui. Le « je » d'un "
             "extrait est celui de son auteur."]
    for slug, ns in retenus:
        c = data["troncons"][ns[0]]
        blocs.append(entete(data, langue, slug, c) + "\n" +
                     recolle(data, ns))
    return "\n\n".join(blocs)


def lire_article(article="", langue="fr"):
    voulu = simple(article)
    if not voulu:
        return "Quel article ? Donne son titre, une partie du titre ou son lien."
    data = corpus()
    langue = "en" if simple(langue)[:2] == "en" else "fr"
    # Un lien ou un slug tel quel.
    dernier = str(article).strip().rstrip("/").split("/")[-1]
    if (langue, dernier) in data["par_article"]:
        voulu = simple(dernier)
    meilleurs, meilleur = [], 0
    mots = [m for m in voulu.split() if len(m) > 2] or voulu.split()
    for (l, slug), ns in data["par_article"].items():
        if l != langue:
            continue
        if slug == dernier:
            meilleurs, meilleur = [slug], len(mots)
            break
        titre = simple(data["troncons"][ns[0]]["title"]) + " " + simple(slug)
        s = sum(m in titre for m in mots)
        if s > meilleur:
            meilleurs, meilleur = [slug], s
        elif s == meilleur and s:
            meilleurs.append(slug)
    if not meilleurs or meilleur < max(1, len(mots) // 2):
        return f"Je ne trouve pas d'article qui ressemble à « {article} ». chercher_articles cherche dans le texte."
    if len(meilleurs) > 1:
        titres = "; ".join(f"« {data['troncons'][data['par_article'][(langue, s)][0]]['title']} »"
                           for s in meilleurs[:5])
        return f"Plusieurs articles correspondent : {titres}. Lequel ?"
    slug = meilleurs[0]
    ns = data["par_article"][(langue, slug)]
    texte = recolle(data, ns)
    suite = ""
    if len(texte) > MAX_LECTURE:
        texte = texte[:MAX_LECTURE].rsplit("\n", 1)[0]
        suite = f"\n\n[…] Article coupé à {MAX_LECTURE} caractères ; la suite est sur le site."
    return entete(data, langue, slug, data["troncons"][ns[0]]) + "\n\n" + texte + suite


def articles_recents(auteur="", nombre=5, langue="fr"):
    nombre = max(1, min(int(nombre), 20))
    data = corpus()
    langue = "en" if simple(langue)[:2] == "en" else "fr"
    qui = simple(auteur)
    qui = "Bob" if "bob" in qui else "Ludo" if "ludo" in qui or "ludovic" in qui else ""
    liste = sorted((e for (l, _), e in data["articles"].items()
                    if l == langue and (not qui or e["auteur"] == qui)),
                   key=lambda e: e["date"], reverse=True)[:nombre]
    if not liste:
        return "Aucun article trouvé."
    par = "by" if langue == "en" else "par"
    return "\n".join(f"{e['date']} — « {e['title']} », {par} {e['auteur']} : {e['desc']} {lien(e)}" for e in liste)


TOOLS = {
    "chercher_articles": (chercher_articles, {
        "description": "Cherche dans les articles du blogue labodeludo.dev (ceux de Bob et ceux de Ludo) les "
                       "passages qui répondent à une question, par le sens et pas seulement par les mots. Pour "
                       "« Bob, as-tu écrit sur… ? », « comment Ludo a-t-il réglé… ? », « qu'est-ce que l'article "
                       "dit de… ? ». Rend des extraits avec le titre, l'auteur, la date et le lien.",
        "inputSchema": {"type": "object", "required": ["question"], "properties": {
            "question": {"type": "string", "description": "La question ou le sujet, en phrase complète."},
            "langue": {"type": "string", "description": "« fr » ou « en » : la langue des articles. Vide = "
                                                        "celle de la question."},
            "nombre": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5,
                       "description": "Combien d'extraits au plus."},
        }},
    }),
    "lire_article": (lire_article, {
        "description": "Le texte d'un article du blogue, par son titre, une partie du titre ou son lien. Pour "
                       "« lis-moi l'article sur… », ou pour aller plus loin après chercher_articles.",
        "inputSchema": {"type": "object", "required": ["article"], "properties": {
            "article": {"type": "string", "description": "Titre, mots du titre, slug ou lien de l'article."},
            "langue": {"type": "string", "description": "« fr » (défaut) ou « en »."},
        }},
    }),
    "articles_recents": (articles_recents, {
        "description": "Les derniers articles parus sur le blogue, avec date, auteur, résumé et lien. Pour « c'est "
                       "quoi ton dernier article, Bob ? », « qu'est-ce que Ludo a publié dernièrement ? ».",
        "inputSchema": {"type": "object", "properties": {
            "auteur": {"type": "string", "description": "« Bob », « Ludo », ou vide pour les deux."},
            "nombre": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            "langue": {"type": "string", "description": "« fr » (défaut) ou « en »."},
        }},
    }),
}
for _name, (_fn, _spec) in TOOLS.items():
    _spec["name"] = _name
    _spec["annotations"] = {"readOnlyHint": True, "openWorldHint": True}

INSTRUCTIONS = ("Les articles du blogue labodeludo.dev, le labo de Ludo : chercher un passage, lire un article, "
                "lister les derniers parus. Deux auteurs : Bob, le robot du labo, et Ludo. Un article décrit le "
                "labo à sa date ; ce n'est pas forcément l'état actuel.")


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
            "serverInfo": {"name": "articles", "title": "Articles du labo", "version": "1.0.0"},
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
        args = params.get("arguments") or {}
        try:
            text, is_error = tool[0](**args), False
        except TypeError as e:
            return err(-32602, f"Arguments invalides : {e}")
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, IndexError) as e:
            print(f"articles: {params.get('name')} failed: {e!r}", file=sys.stderr, flush=True)
            text, is_error = "Le site ne répond pas en ce moment ; réessaie dans une minute.", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Méthode inconnue : {method}")


class Handler(BaseHTTPRequestHandler):
    server_version = "articles-mcp/1.0"

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
        # Une ligne par requête, sans l'adresse du client ni la question.
        print(f"articles: {self.command} {self.path} {args[1] if len(args) > 1 else ''}", file=sys.stderr, flush=True)


def prechauffe():
    # Le corpus se charge au démarrage, pas à la première question posée à voix haute.
    try:
        d = corpus()
        print(f"articles: corpus {len(d['troncons'])} tronçons, {len(d['articles'])} articles "
              f"(index du {d['genere']})", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001 — le prochain appel réessaie
        print(f"articles: preload failed: {e!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print(f"articles: listening on {HOST or '*'}:{PORT}", file=sys.stderr, flush=True)
    threading.Thread(target=prechauffe, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
