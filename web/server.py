#!/usr/bin/env python3
"""Serveur MCP « web » : lire une page web, et dire où elle mène vraiment.

Un outil, `lire_page` : il va chercher une adresse, suit les redirections et
rend le texte lisible de la page d'arrivée. Les redirections passent EN
PREMIER dans la réponse, avec chaque saut et l'adresse finale, et une phrase
le dit quand on change de site. C'est la raison d'être du serveur : un index
de recherche peut montrer une vieille copie d'un site qui, en vrai, renvoie
ailleurs depuis des années ; lue en direct, la redirection saute aux yeux.

Sont suivies, et nommées comme telles :
- les redirections HTTP (301, 302, 303, 307, 308) ;
- la balise <meta http-equiv="refresh">, si elle part en moins de 30 s.
Une redirection en JavaScript (window.location…) n'est PAS exécutée : la
réponse signale qu'il y en a une, et vers où si l'adresse est écrite en clair.

UN SERVEUR QUI VA CHERCHER N'IMPORTE QUELLE ADRESSE EST UN PROXY. Il ne doit
jamais avoir d'adresse publique sans authentification devant. Il refuse
lui-même tout ce qui n'est pas Internet : adresses privées, locales, de
lien local (169.254.169.254, les métadonnées d'un nuage), CGNAT, multicast,
réservées. Chaque saut est vérifié, et la connexion se fait sur l'adresse IP
vérifiée, pas sur un nom résolu une deuxième fois (un DNS qui changerait de
réponse entre les deux ne passe pas). Ports 80 et 443 seulement. Dans
Kubernetes, une NetworkPolicy en sortie fait la même chose une deuxième fois.

Bornes : 10 sauts, 15 s par requête et 40 s en tout, 3 Mo lus au plus ; 8000 caractères de
texte rendus par défaut, et `debut` pour lire la suite.

Configuration, par variables d'environnement :
- HOST, PORT : l'adresse d'écoute. Défaut : toutes les interfaces, port 8080.

Transport, comme hockey : MCP « Streamable HTTP » sans état, réponse JSON
simple, bibliothèque standard seulement.
"""
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import zlib
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HOST", "")
PORT = int(os.environ.get("PORT", "8080"))
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
USER_AGENT = "mcp-servers-web/1.0 (+https://github.com/ludorl82/mcp-servers)"

MAX_SAUTS = 10
DELAI_S = 15
DELAI_TOTAL_S = 40
MAX_LU = 3 * 1024 * 1024
PORTS = {"http": 80, "https": 443}
REFRESH_MAX_S = 30
CARACTERES = 8000
CARACTERES_MAX = 30000

STATUTS = {
    200: "OK", 203: "OK (copie)", 204: "page vide",
    301: "déplacée pour de bon", 302: "déplacée pour l'instant", 303: "voir ailleurs",
    307: "déplacée pour l'instant", 308: "déplacée pour de bon",
    400: "requête refusée", 401: "connexion exigée", 403: "accès refusé", 404: "introuvable",
    410: "retirée pour de bon", 429: "trop de requêtes", 451: "retirée pour raison légale",
    500: "erreur du serveur", 502: "passerelle en erreur", 503: "service indisponible",
    504: "passerelle sans réponse", 999: "refus anti-robot (LinkedIn)",
}


class Refus(Exception):
    """L'adresse ne sera pas lue ; le message dit pourquoi, prêt à être répété."""


# --- L'adresse : la normaliser, la vérifier -------------------------------------


def normalise(url):
    """Une adresse tapée ou dictée → (schéma, hôte, port, chemin+requête, adresse complète)."""
    url = (url or "").strip()
    if not url:
        raise Refus("Il faut une adresse, comme « exemple.com » ou « https://exemple.com/page ».")
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    p = urllib.parse.urlsplit(url)
    schema = p.scheme.lower()
    if schema not in PORTS:
        raise Refus(f"Je ne lis que des adresses web (http ou https), pas « {schema}: ».")
    if p.username or p.password:
        raise Refus("L'adresse contient un identifiant ou un mot de passe ; je ne l'envoie pas.")
    hote = (p.hostname or "").rstrip(".")
    if not hote:
        raise Refus(f"Je ne trouve pas de nom de site dans « {url} ».")
    try:
        port = p.port or PORTS[schema]
    except ValueError:
        raise Refus(f"Le port de « {url} » n'est pas un nombre.")
    if port not in PORTS.values():
        raise Refus(f"Je ne lis que les ports web habituels (80 et 443), pas le port {port}.")
    try:
        ipaddress.ip_address(hote)
    except ValueError:
        try:
            hote = hote.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise Refus(f"Le nom de site « {hote} » n'est pas valide.")
    chemin = urllib.parse.quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
    requete = urllib.parse.quote(p.query, safe="=&%:@!$'()*+,;/?-._~") if p.query else ""
    cible = chemin + ("?" + requete if requete else "")
    nom = f"[{hote}]" if ":" in hote else hote
    complet = f"{schema}://{nom}{'' if port == PORTS[schema] else ':' + str(port)}{cible}"
    return schema, hote, port, cible, complet


def publique(ip):
    """Une adresse joignable sur Internet ? (pas privée, locale, CGNAT, réservée…)"""
    ip = ipaddress.ip_address(ip)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resous(hote, port):
    """Le nom → une adresse IP publique à laquelle se connecter. Refus si une seule ne l'est pas."""
    try:
        infos = socket.getaddrinfo(hote, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refus(f"Le nom « {hote} » n'existe pas (le DNS ne le connaît pas).")
    ips = list(dict.fromkeys(i[4][0].split("%")[0] for i in infos))
    privees = [ip for ip in ips if not publique(ip)]
    if privees:
        # Toutes les adresses doivent être publiques : un nom qui en mêle une
        # privée est soit une erreur de configuration, soit une tentative.
        raise Refus(f"« {hote} » mène à une adresse qui n'est pas sur Internet ({privees[0]}) ; "
                    "je ne lis que des pages publiques.")
    # IPv4 d'abord : un conteneur a rarement une route IPv6.
    return sorted(ips, key=lambda ip: ":" in ip)[0]


def verifie(hote, port):
    """Point d'entrée de la vérification ; les tests le remplacent pour viser 127.0.0.1."""
    return resous(hote, port)


# --- La requête : un saut à la fois ----------------------------------------------


def requete(schema, hote, port, cible):
    """Un GET sans suivre de redirection → (statut, en-têtes, corps, tronqué)."""
    ip = verifie(hote, port)
    classe = HTTPSConnection if schema == "https" else HTTPConnection
    conn = classe(hote, port, timeout=DELAI_S)
    # Se connecter à l'IP vérifiée, pas au nom : sinon le nom serait résolu une
    # deuxième fois, et pourrait alors donner autre chose. Le nom reste celui
    # de l'en-tête Host et du certificat (SNI), vérifié comme d'habitude.
    conn._create_connection = lambda _adresse, *a, **k: socket.create_connection((ip, port), *a, **k)
    try:
        conn.request("GET", cible, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        rep = conn.getresponse()
        corps = rep.read(MAX_LU + 1)
        return rep.status, rep.headers, corps[:MAX_LU], len(corps) > MAX_LU
    finally:
        conn.close()


def decompresse(corps, encodage):
    encodage = (encodage or "").lower().strip()
    if encodage not in ("gzip", "x-gzip", "deflate"):
        return corps
    d = zlib.decompressobj(zlib.MAX_WBITS | 32 if "gzip" in encodage else zlib.MAX_WBITS)
    try:
        return d.decompress(corps, MAX_LU)
    except zlib.error:
        if encodage == "deflate":   # du deflate « brut », sans en-tête zlib
            return zlib.decompressobj(-zlib.MAX_WBITS).decompress(corps, MAX_LU)
        raise


def jeu_de_caracteres(entetes, corps):
    m = re.search(r"charset=[\"']?([\w.:-]+)", entetes.get("Content-Type") or "", re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", corps[:4096], re.I)
    nom = m.group(1) if m else "utf-8"
    nom = nom.decode("ascii", "replace") if isinstance(nom, bytes) else nom
    try:
        "".encode(nom)
        return nom
    except LookupError:
        return "utf-8"


# --- Le texte de la page -----------------------------------------------------------

IGNORES = {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas", "select", "button"}
ACCESSOIRES = {"nav", "header", "footer", "aside", "form"}
ROLES_ACCESSOIRES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "dialog"}
# Les menus d'une page moderne sont souvent des <div> : leur classe ou leur id les trahit.
CLASSES_ACCESSOIRES = re.compile(r"(?:^|[\s_-])(?:menu|nav|navbar|navigation|sidebar|dropdown|toc|breadcrumbs?|"
                                 r"cookies?|consent|banner|footer|header|share|social|skip)(?:$|[\s_-])", re.I)
BLOCS = {"p", "div", "section", "article", "main", "br", "li", "ul", "ol", "tr", "table", "blockquote",
         "pre", "dd", "dt", "dl", "figure", "figcaption", "address", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
         "nav", "header", "footer", "aside", "form", "details", "summary"}
# Entre deux de ces balises, le HTML compacte n'a souvent aucune espace (« Labo2026-10-27ludo »).
ESPACES = {"a", "span", "time", "small", "label", "td", "th", "img", "abbr", "cite", "code"}
VIDES = {"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "track"}


class Lecteur(HTMLParser):
    """Le texte d'une page, en trois couches : tout, sans les accessoires
    (menus, pied de page…), et ce qui est dans <main> ou <article>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titre, self.dans_titre, self.titre_vu = "", False, False
        self.pile = []
        self.morceaux = []   # (texte, accessoire?, principal?)
        self.refresh = self.canonique = self.langue = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.langue = a.get("lang", "")
        elif tag == "title" and not self.titre_vu and not self._dans("svg"):
            self.dans_titre = True
        elif tag == "meta" and a.get("http-equiv", "").lower() == "refresh" and not self.refresh:
            self.refresh = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split() and not self.canonique:
            self.canonique = a.get("href", "")
        if tag in BLOCS:
            self.morceaux.append(("\n", False, False))
        if tag in ("h1", "h2", "h3"):
            self.morceaux.append(("\n", False, False))
        if tag in ESPACES:
            self.morceaux.append((" ", *self._etat()))
        if tag == "li":
            self.morceaux.append(("• ", *self._etat()))
        if tag not in VIDES:
            accessoire = (tag in ACCESSOIRES or a.get("role", "").lower() in ROLES_ACCESSOIRES
                          or a.get("aria-hidden") == "true" or "hidden" in a
                          or tag not in ("html", "body", "main", "article")
                          and bool(CLASSES_ACCESSOIRES.search(a.get("class", "") + " " + a.get("id", ""))))
            self.pile.append((tag, accessoire))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VIDES:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title" and self.dans_titre:
            self.dans_titre, self.titre_vu = False, True
        if tag in ESPACES:
            self.morceaux.append((" ", *self._etat()))
        if self._dans(tag):
            # Fermer jusqu'à la balise ouverte correspondante : le HTML réel en oublie.
            while self.pile and self.pile.pop()[0] != tag:
                pass
        if tag in BLOCS:
            self.morceaux.append(("\n", False, False))

    def _dans(self, *tags):
        return any(t in tags for t, _ in self.pile)

    def _etat(self):
        return any(acc for _, acc in self.pile), self._dans("main", "article")

    def handle_data(self, data):
        if self.dans_titre:
            self.titre += data
            return
        if self._dans("head", *IGNORES):
            return
        self.morceaux.append((data, *self._etat()))

    def texte(self, garde):
        brut = "".join(t for t, accessoire, principal in self.morceaux if t == "\n" or garde(accessoire, principal))
        # Une ligne par bloc, sans lignes vides : la limite de caractères va au texte.
        lignes = (re.sub(r"\s+", " ", l).strip() for l in brut.split("\n"))
        return "\n".join(l for l in lignes if l and l != "•")

    def lisible(self):
        """Le cœur de la page (<main>, <article>) s'il en porte au moins le quart,
        sinon tout sauf les accessoires, sinon tout (une page faite que de menus)."""
        tout = self.texte(lambda acc, princ: True)
        principal = self.texte(lambda acc, princ: princ and not acc)
        if principal and len(principal) * 4 >= len(tout):
            return principal
        return self.texte(lambda acc, princ: not acc) or tout


JS_REDIRECTION = re.compile(
    r"""(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["']([^"']+)["']""")


def refresh_vers(contenu):
    """« 0; url=https://… » → (délai, adresse) ; (None, "") si ce n'en est pas un."""
    m = re.match(r"\s*(\d+)(?:\.\d*)?\s*(?:[;,]\s*(?:url\s*=\s*)?[\"']?([^\"'\s]+)[\"']?)?", contenu or "", re.I)
    if not m or not m.group(2):
        return None, ""
    return int(m.group(1)), m.group(2)


# --- L'outil --------------------------------------------------------------------------


def domaine(hote):
    return hote[4:] if hote.startswith("www.") else hote


def statut_dit(code):
    return f"{code} ({STATUTS[code]})" if code in STATUTS else str(code)


def taille_dite(n):
    return f"{n} octets" if n < 1024 else f"{n // 1024} Ko" if n < 1024 * 1024 else f"{n / 1048576:.1f} Mo"


def lire_page(url="", caracteres=CARACTERES, debut=0):
    caracteres = max(500, min(int(caracteres or CARACTERES), CARACTERES_MAX))
    debut = max(0, int(debut or 0))
    schema, hote, port, cible, adresse = normalise(url)
    depart = hote
    sauts = []          # (statut ou « meta refresh », de, vers)
    vues = {adresse}
    fin = time.monotonic() + DELAI_TOTAL_S
    while True:
        if time.monotonic() > fin:
            raise Refus(f"Les redirections prennent plus de {DELAI_TOTAL_S} secondes ; j'arrête à {adresse}.")
        try:
            statut, entetes, corps, tronque = requete(schema, hote, port, cible)
        except Refus as e:
            if not sauts:
                raise
            raise Refus(f"{e} (C'est là que mène la redirection depuis {sauts[0][1]}.)")
        suivante, comment = None, None
        if statut in (301, 302, 303, 307, 308) and entetes.get("Location"):
            suivante, comment = urllib.parse.urljoin(adresse, entetes["Location"].strip()), statut
        page = None
        type_contenu = (entetes.get("Content-Type") or "").split(";")[0].strip().lower()
        if suivante is None and type_contenu in ("text/html", "application/xhtml+xml") or (
                suivante is None and not type_contenu and corps.lstrip()[:1] == b"<"):
            corps = decompresse(corps, entetes.get("Content-Encoding"))
            page = Lecteur()
            page.feed(corps.decode(jeu_de_caracteres(entetes, corps), "replace"))
            page.close()
            delai, vers = refresh_vers(page.refresh)
            if vers and delai <= REFRESH_MAX_S:
                suivante, comment = urllib.parse.urljoin(adresse, vers), "meta refresh"
        if suivante is None:
            break
        if len(sauts) >= MAX_SAUTS:
            raise Refus(f"Plus de {MAX_SAUTS} redirections à partir de {sauts[0][1]} ; j'arrête là. "
                        f"La dernière menait à {suivante}.")
        try:
            prochain = normalise(suivante)
        except Refus as e:
            raise Refus(f"{adresse} redirige vers {suivante}, que je ne lis pas : {e}")
        sauts.append((comment, adresse, prochain[4]))
        if prochain[4] in vues:
            raise Refus("Les redirections tournent en rond : " + " → ".join([sauts[0][1]] + [s[2] for s in sauts]) + ".")
        vues.add(prochain[4])
        schema, hote, port, cible, adresse = prochain

    lignes = []
    if sauts:
        if domaine(hote) != domaine(depart):
            lignes.append(f"Attention : {depart} redirige vers un AUTRE site, {hote}. "
                          f"La page lue est celle de {hote}.")
        else:
            lignes.append(f"L'adresse redirige, sans changer de site ({hote}).")
        lignes.append(f"Adresse demandée : {sauts[0][1]}")
        lignes.append(f"Redirections ({len(sauts)}) :")
        for i, (comment, de, vers) in enumerate(sauts, 1):
            quoi = statut_dit(comment) if isinstance(comment, int) else "balise meta refresh dans la page"
            lignes.append(f"  {i}. {quoi} : {de} → {vers}")
        lignes.append(f"Adresse finale : {adresse}")
    else:
        lignes.append(f"Adresse : {adresse} (aucune redirection).")
    ctype = entetes.get("Content-Type") or "type inconnu"
    lignes.append(f"Réponse : {statut_dit(statut)}, {ctype.split(';')[0]}, {taille_dite(len(corps))}"
                  + (" (coupé : page trop lourde)" if tronque else "") + ".")
    if statut >= 400:
        lignes.append("La page répond par une erreur : ce qui suit est la page d'erreur du site, pas le contenu voulu.")

    if page is None:
        type_contenu = ctype.split(";")[0].strip().lower()
        if type_contenu.startswith("text/") or type_contenu.endswith(("json", "xml")):
            corps = decompresse(corps, entetes.get("Content-Encoding"))
            texte = corps.decode(jeu_de_caracteres(entetes, corps), "replace").strip()
        else:
            lignes.append(f"Ce n'est pas une page de texte ({type_contenu or 'type inconnu'}) : je n'en extrais rien.")
            return "\n".join(lignes)
    else:
        titre = re.sub(r"\s+", " ", page.titre).strip()
        if titre:
            lignes.append(f"Titre : {titre}")
        if page.langue:
            lignes.append(f"Langue : {page.langue}")
        if page.canonique:
            canon = urllib.parse.urljoin(adresse, page.canonique)
            hote_canon = urllib.parse.urlsplit(canon).hostname or ""
            if domaine(hote_canon) != domaine(hote):
                lignes.append(f"Adresse canonique sur un autre site : {canon}")
        delai, vers = refresh_vers(page.refresh)
        if vers and delai > REFRESH_MAX_S:
            lignes.append(f"La page renvoie vers {urllib.parse.urljoin(adresse, vers)} après {delai} s (non suivi).")
        texte = page.lisible()
        # Une page presque vide qui assigne window.location est une redirection ;
        # dans une vraie page, la même ligne est un bouton ou un menu.
        js = len(texte) < 300 and JS_REDIRECTION.search(corps.decode("utf-8", "replace"))
        if js and urllib.parse.urljoin(adresse, js.group(1)) != adresse:
            lignes.append(f"La page semble rediriger en JavaScript vers {urllib.parse.urljoin(adresse, js.group(1))} "
                          "(non suivi : je n'exécute pas le JavaScript).")

    if not corps.strip():
        lignes.append("La page est vide.")
        return "\n".join(lignes)
    if not texte:
        lignes.append("La page ne contient aucun texte lisible (elle s'affiche peut-être entièrement en JavaScript).")
        return "\n".join(lignes)
    morceau = texte[debut:debut + caracteres]
    if debut >= len(texte):
        lignes.append(f"Le texte fait {len(texte)} caractères ; rien après le caractère {debut}.")
        return "\n".join(lignes)
    lignes.append(f"Texte ({len(texte)} caractères"
                  + (f", à partir du caractère {debut}" if debut else "") + ") :")
    lignes.append("")
    lignes.append(morceau)
    if debut + caracteres < len(texte):
        lignes.append("")
        lignes.append(f"[…] Suite coupée : rappeler avec debut={debut + caracteres} pour lire la suite.")
    return "\n".join(lignes)


TOOLS = {
    "lire_page": (lire_page, {
        "description": "Va chercher une page web EN DIRECT et rend son texte lisible, sans menus ni scripts. "
                       "Suit les redirections et les dit EN PREMIER : chaque saut, l'adresse finale, et un "
                       "avertissement quand on aboutit sur un autre site. À utiliser pour vérifier ce qu'une "
                       "page dit vraiment aujourd'hui, ou où mène une adresse, plutôt que de se fier à un résultat "
                       "de recherche (un index peut garder une vieille copie). Pages publiques seulement.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string",
                    "description": "L'adresse à lire, avec ou sans https:// (« exemple.com », "
                                   "« https://exemple.com/page »)."},
            "caracteres": {"type": "integer", "minimum": 500, "maximum": CARACTERES_MAX, "default": CARACTERES,
                           "description": "Combien de caractères de texte rendre au plus."},
            "debut": {"type": "integer", "minimum": 0, "default": 0,
                      "description": "Où commencer dans le texte, pour lire la suite d'une longue page."},
        }, "required": ["url"]},
    }),
}
for _name, (_fn, _spec) in TOOLS.items():
    _spec["name"] = _name
    _spec["annotations"] = {"readOnlyHint": True, "openWorldHint": True}

INSTRUCTIONS = ("Lit une page web en direct : son texte, et surtout où elle mène (redirections, adresse finale, "
                "changement de site). Pour vérifier une page plutôt que de se fier à un index de recherche.")


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
            "serverInfo": {"name": "web", "title": "Lecture de pages web", "version": "1.0.0"},
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
        except Refus as e:
            text, is_error = str(e), True
        except ssl.SSLCertVerificationError as e:
            text, is_error = (f"Le certificat HTTPS du site n'est pas valide ({e.verify_message}) : "
                              "je ne lis pas une page dont je ne peux pas vérifier l'origine."), True
        except ConnectionRefusedError:
            text, is_error = "Le site refuse la connexion.", True
        except (TimeoutError, socket.timeout):
            text, is_error = f"Le site n'a pas répondu en {DELAI_S} secondes.", True
        except (OSError, HTTPException, ValueError, zlib.error, urllib.error.URLError) as e:
            print(f"web: {params.get('name')} failed: {e!r}", file=sys.stderr, flush=True)
            raison = getattr(e, "reason", None) or getattr(e, "strerror", None) or type(e).__name__
            text, is_error = f"Je n'arrive pas à lire cette page : {raison}.", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Méthode inconnue : {method}")


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
    server_version = "web-mcp/1.0"

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

    def parse_request(self):
        # Chaque requête repart à zéro : une connexion gardée ouverte en sert plusieurs.
        ok = super().parse_request()
        chemin, _, requete = self.path.partition("?")
        self.notes = qui_parle(chemin, requete, self.headers) if ok else {}
        return ok

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
        corps = self.rfile.read(length)
        self.notes = qui_parle("/mcp", "", self.headers, corps)
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
        # Une ligne par requête, sans l'adresse du client ni la page demandée.
        notes = " ".join(f"{k}={v}" for k, v in getattr(self, "notes", {}).items())
        print(f"web: {self.command} {self.path.split("?")[0]} {args[1] if len(args) > 1 else ''} {notes}".rstrip(),
              file=sys.stderr, flush=True)


if __name__ == "__main__":
    print(f"web: listening on {HOST or '*'}:{PORT}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
