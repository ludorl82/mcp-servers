"""Tests hors ligne des serveurs : le protocole MCP et la configuration.

Aucun appel réseau : chaque cas s'arrête avant l'API (outil inconnu, équipe
inconnue, lieu absent), ou ne touche que le serveur HTTP local. Pour `web`,
un faux site sur 127.0.0.1 joue les redirections ; seul ce test lève le
refus des adresses locales.

    python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SERVEURS = {"hockey": "match_lnh", "meteo": "meteo", "articles": "chercher_articles", "web": "lire_page"}
VARIABLES = ("HOCKEY_EQUIPE", "METEO_LIEU", "METEO_NOM", "HOST", "PORT")


def charge(nom, **env):
    """Charge <nom>/server.py comme module neuf, avec ces variables d'environnement."""
    for k in VARIABLES:
        os.environ.pop(k, None)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(f"serveur_{nom}", RACINE / nom / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def appel(m, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return m.handle(msg)


class Protocole(unittest.TestCase):
    def test_chaque_serveur(self):
        for nom, outil in SERVEURS.items():
            with self.subTest(serveur=nom):
                m = charge(nom)
                init = appel(m, "initialize", {"protocolVersion": "2025-06-18"})["result"]
                self.assertEqual(init["protocolVersion"], "2025-06-18")
                self.assertEqual(init["serverInfo"]["name"], nom)
                self.assertTrue(init["instructions"])
                # Une version inconnue reçoit la plus récente que le serveur parle.
                autre = appel(m, "initialize", {"protocolVersion": "1999-01-01"})["result"]
                self.assertEqual(autre["protocolVersion"], m.PROTOCOLS[0])
                outils = appel(m, "tools/list")["result"]["tools"]
                self.assertIn(outil, [o["name"] for o in outils])
                for o in outils:
                    self.assertTrue(o["description"])
                    self.assertEqual(o["inputSchema"]["type"], "object")
                    self.assertTrue(o["annotations"]["readOnlyHint"])
                self.assertEqual(appel(m, "ping")["result"], {})
                self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
                self.assertEqual(appel(m, "n/importe")["error"]["code"], -32601)
                self.assertEqual(m.handle({"id": 3, "method": "ping"})["error"]["code"], -32600)
                self.assertEqual(appel(m, "tools/call", {"name": "absent"})["error"]["code"], -32602)
                mauvais = appel(m, "tools/call", {"name": outil, "arguments": {"pas_un_argument": 1}})
                self.assertEqual(mauvais["error"]["code"], -32602)


class Hockey(unittest.TestCase):
    def test_equipe_par_defaut(self):
        self.assertEqual(charge("hockey").DEFAUT, "MTL")
        m = charge("hockey", HOCKEY_EQUIPE="Oilers")
        self.assertEqual(m.DEFAUT, "EDM")
        self.assertIn("les Oilers d'Edmonton", m.EQUIPE["description"])
        self.assertIn("Les Oilers d'Edmonton par défaut", m.INSTRUCTIONS)

    def test_equipe_inconnue_au_demarrage(self):
        with self.assertRaises(SystemExit):
            charge("hockey", HOCKEY_EQUIPE="Bruins")

    def test_equipe_inconnue_a_l_appel(self):
        m = charge("hockey")
        r = appel(m, "tools/call", {"name": "match_lnh", "arguments": {"equipe": "Bruins"}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("n'est pas une équipe canadienne", r["content"][0]["text"])


class Meteo(unittest.TestCase):
    def test_sans_lieu_par_defaut(self):
        m = charge("meteo")
        self.assertIn("Obligatoire", m.LIEU["description"])
        self.assertNotIn("maison", m.INSTRUCTIONS)
        r = appel(m, "tools/call", {"name": "meteo", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("aucun lieu par défaut", r["content"][0]["text"])

    def test_le_lieu_ne_sort_jamais(self):
        m = charge("meteo", METEO_LIEU="Sherbrooke", METEO_NOM="le chalet")
        textes = m.INSTRUCTIONS + json.dumps(appel(m, "tools/list")["result"], ensure_ascii=False)
        self.assertNotIn("Sherbrooke", textes)
        self.assertIn("le chalet", textes)


class FauxSite(BaseHTTPRequestHandler):
    """Deux « sites » sur le même port, distingués par l'en-tête Host."""
    PAGES = {
        "/": (200, "<html lang='fr'><head><title>Accueil</title></head><body>"
                   "<nav>Menu Accueil Contact</nav><main><h1>Bonjour</h1><p>Le <b>vrai</b> texte.</p>"
                   "<svg><title>icône</title></svg></main><footer>Pied</footer></body></html>"),
        "/vieux": (301, "/neuf"),
        "/neuf": (302, "http://ailleurs.test:{port}/profil"),
        "/profil": (200, "<title>Profil</title><p>Ailleurs, maintenant.</p>"),
        "/meta": (200, "<meta http-equiv='refresh' content='0; url=/'><p>On vous redirige…</p>"),
        "/boucle": (302, "/boucle2"),
        "/boucle2": (302, "/boucle"),
        "/fuite": (302, "http://10.0.0.1:{port}/"),
        "/texte": (200, "simple texte"),
        "/image": (200, "PNG"),
    }

    def do_GET(self):
        code, contenu = self.PAGES.get(self.path, (404, "<p>Introuvable</p>"))
        self.send_response(code)
        if 300 <= code < 400:
            self.send_header("Location", contenu.format(port=self.server.server_address[1]))
            contenu = ""
        ctype = {"/texte": "text/plain", "/image": "image/png"}.get(self.path, "text/html; charset=utf-8")
        corps = contenu.encode()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *a):
        pass


class Web(unittest.TestCase):
    def setUp(self):
        self.m = charge("web")
        site = ThreadingHTTPServer(("127.0.0.1", 0), FauxSite)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.shutdown)
        self.port = site.server_address[1]
        # Le faux site est sur 127.0.0.1 : on lève le refus pour ses noms seuls,
        # et on accepte son port (le serveur ne lit que 80 et 443).
        vraie = self.m.verifie
        self.m.verifie = lambda hote, port: "127.0.0.1" if hote.endswith(".test") else vraie(hote, port)
        self.m.PORTS = {"http": self.port, "https": 443}

    def lis(self, chemin, hote="site.test", **args):
        r = appel(self.m, "tools/call", {"name": "lire_page",
                                        "arguments": {"url": f"http://{hote}:{self.port}{chemin}", **args}})
        return r["result"]["content"][0]["text"], r["result"]["isError"]

    def test_texte_sans_les_accessoires(self):
        texte, erreur = self.lis("/")
        self.assertFalse(erreur)
        self.assertIn("aucune redirection", texte)
        self.assertIn("Titre : Accueil", texte)
        self.assertIn("Langue : fr", texte)
        self.assertIn("Bonjour\nLe vrai texte.", texte)
        for absent in ("Menu", "Pied", "icône"):
            self.assertNotIn(absent, texte)

    def test_redirections_vers_un_autre_site(self):
        texte, erreur = self.lis("/vieux")
        self.assertFalse(erreur)
        self.assertTrue(texte.startswith("Attention : site.test redirige vers un AUTRE site, ailleurs.test."), texte)
        self.assertIn("Redirections (2)", texte)
        # Le port du faux site passe pour le port par défaut : les adresses sont dites sans lui.
        self.assertIn("1. 301 (déplacée pour de bon) : http://site.test/vieux → http://site.test/neuf", texte)
        self.assertIn("2. 302 (déplacée pour l'instant) : http://site.test/neuf → http://ailleurs.test/profil", texte)
        self.assertIn("Adresse finale : http://ailleurs.test/profil", texte)
        self.assertIn("Ailleurs, maintenant.", texte)

    def test_meta_refresh(self):
        texte, _ = self.lis("/meta")
        self.assertIn("balise meta refresh", texte)
        self.assertIn("sans changer de site", texte)
        self.assertIn("Le vrai texte.", texte)

    def test_redirection_vers_une_adresse_privee(self):
        texte, erreur = self.lis("/fuite")
        self.assertTrue(erreur)
        self.assertIn("pas sur Internet (10.0.0.1)", texte)
        self.assertIn("redirection depuis http://site.test/fuite", texte)

    def test_boucle(self):
        texte, erreur = self.lis("/boucle")
        self.assertTrue(erreur)
        self.assertIn("tournent en rond", texte)

    def test_erreur_et_types(self):
        texte, erreur = self.lis("/absente")
        self.assertFalse(erreur)
        self.assertIn("404 (introuvable)", texte)
        self.assertIn("page d'erreur du site", texte)
        self.assertIn("simple texte", self.lis("/texte")[0])
        self.assertIn("pas une page de texte (image/png)", self.lis("/image")[0])

    def test_suite(self):
        texte, _ = self.lis("/", caracteres=500, debut=10)
        self.assertIn("à partir du caractère 10", texte)

    def test_refus(self):
        m = charge("web")
        for url, raison in (("http://127.0.0.1/", "pas sur Internet"), ("http://10.1.2.3/", "pas sur Internet"),
                            ("http://169.254.169.254/latest/", "pas sur Internet"), ("http://[::1]/", "pas sur Internet"),
                            ("http://[::ffff:192.168.0.1]/", "pas sur Internet"), ("http://100.64.0.1/", "pas sur Internet"),
                            ("ftp://exemple.com/", "http ou https"), ("exemple.com:22", "port 22"),
                            ("https://moi:secret@exemple.com/", "mot de passe"), ("", "Il faut une adresse")):
            with self.subTest(url=url):
                r = appel(m, "tools/call", {"name": "lire_page", "arguments": {"url": url}})["result"]
                self.assertTrue(r["isError"])
                self.assertIn(raison, r["content"][0]["text"])


class Http(unittest.TestCase):
    def test_transport(self):
        m = charge("hockey", HOST="127.0.0.1")
        serveur = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        self.addCleanup(serveur.shutdown)
        base = f"http://127.0.0.1:{serveur.server_address[1]}"
        with urllib.request.urlopen(base + "/healthz") as r:
            self.assertEqual(r.read(), b"ok\n")
        corps = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"}]).encode()
        req = urllib.request.Request(base + "/mcp", corps, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.load(r), [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        for chemin, code in (("/mcp", 405), ("/ailleurs", 404)):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + chemin)
            self.assertEqual(e.exception.code, code)


PUBLICS = ("hockey", "meteo", "articles")


class OAuthOuvert(unittest.TestCase):
    def setUp(self):
        m = charge("meteo", HOST="127.0.0.1")
        serveur = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        self.addCleanup(serveur.shutdown)
        self.base = f"http://127.0.0.1:{serveur.server_address[1]}"

    def demande(self, chemin, corps=None, ctype="application/json"):
        req = urllib.request.Request(self.base + chemin, corps, {"Content-Type": ctype} if corps else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, json.loads(e.read() or b"null")

    def test_meme_bloc_partout(self):
        def bloc(nom):
            t = (RACINE / nom / "server.py").read_text()
            return t[t.index("# --- OAuth ouvert"):t.index("# --- fin de l'OAuth ouvert ---")]
        for nom in PUBLICS[1:]:
            with self.subTest(serveur=nom):
                self.assertEqual(bloc(nom), bloc(PUBLICS[0]))

    def test_metadonnees(self):
        code, _, ressource = self.demande("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(code, 200)
        self.assertEqual(ressource["resource"], self.base + "/mcp")
        code, _, serveur = self.demande("/.well-known/oauth-authorization-server")
        self.assertEqual(serveur["issuer"], ressource["authorization_servers"][0])
        self.assertEqual(serveur["registration_endpoint"], self.base + "/register")
        self.assertIn("S256", serveur["code_challenge_methods_supported"])

    def test_flux_complet(self):
        retour = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-1-meteo_tptpt_net"
        code, _, client = self.demande("/register", json.dumps(
            {"client_name": "Google", "redirect_uris": [retour], "token_endpoint_auth_method": "client_secret_post"}).encode())
        self.assertEqual(code, 201)
        self.assertTrue(client["client_id"] and client["client_secret"])
        q = urllib.parse.urlencode({"client_id": client["client_id"], "redirect_uri": retour, "response_type": "code",
                                    "state": "abc", "code_challenge": "x", "code_challenge_method": "S256"})
        ouvreur = urllib.request.build_opener(type("SansSuivre", (urllib.request.HTTPRedirectHandler,),
                                                   {"redirect_request": lambda *a: None}))
        with self.assertRaises(urllib.error.HTTPError) as e:
            ouvreur.open(self.base + "/authorize?" + q)
        self.assertEqual(e.exception.code, 302)
        lieu = urllib.parse.urlparse(e.exception.headers["Location"])
        self.assertEqual(lieu._replace(query="").geturl(), retour)
        p = dict(urllib.parse.parse_qsl(lieu.query))
        self.assertEqual(p["state"], "abc")
        code, entetes, jeton = self.demande("/token", urllib.parse.urlencode(
            {"grant_type": "authorization_code", "code": p["code"], "code_verifier": "y"}).encode(),
            "application/x-www-form-urlencoded")
        self.assertEqual((code, jeton["token_type"]), (200, "Bearer"))
        self.assertEqual(entetes["Cache-Control"], "no-store")
        code, _, _ = self.demande("/token", b"grant_type=password", "application/x-www-form-urlencoded")
        self.assertEqual(code, 400)

    def test_pas_de_redirection_ouverte(self):
        for retour in ("https://ailleurs.example/cb", "https://claude.ai/api/mcp/auth_callback/x",
                       "https://oauth-redirect.googleusercontent.com.ailleurs.example/r/x"):
            with self.subTest(retour=retour):
                q = urllib.parse.urlencode({"redirect_uri": retour, "response_type": "code"})
                code, entetes, _ = self.demande("/authorize?" + q)
                self.assertEqual(code, 400)
                self.assertIsNone(entetes["Location"])

    def test_mcp_sans_jeton(self):
        code, _, r = self.demande("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode())
        self.assertEqual((code, r["result"]), (200, {}))
        code, _, _ = self.demande("/register", b"{}")
        self.assertEqual(code, 400)


class QuiParle(unittest.TestCase):
    def test_meme_bloc_partout(self):
        def bloc(nom):
            t = (RACINE / nom / "server.py").read_text()
            return t[t.index("# --- Qui parle"):t.index("# --- fin de qui parle ---")]
        for nom in list(SERVEURS)[1:]:
            with self.subTest(serveur=nom):
                self.assertEqual(bloc(nom), bloc("hockey"))

    def test_la_ligne_dit_qui_sans_secret(self):
        m = charge("meteo", HOST="127.0.0.1")
        serveur = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        self.addCleanup(serveur.shutdown)
        base = f"http://127.0.0.1:{serveur.server_address[1]}"
        retour = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-1-meteo_tptpt_net"
        ouvreur = urllib.request.build_opener(type("SansSuivre", (urllib.request.HTTPRedirectHandler,),
                                                   {"redirect_request": lambda *a: None}))

        def demande(chemin, corps=None, ctype="application/json"):
            req = urllib.request.Request(base + chemin, corps, {"User-Agent": "Google-Gemini/2.0 (x; y)",
                                                                "Content-Type": ctype})
            try:
                with ouvreur.open(req) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                return e

        journal = io.StringIO()
        with contextlib.redirect_stderr(journal):
            client = demande("/register", json.dumps({"client_name": "Gemini Connecteur", "redirect_uris": [retour],
                                                      "token_endpoint_auth_method": "client_secret_post"}).encode())
            e = demande("/authorize?" + urllib.parse.urlencode({"client_id": client["client_id"], "redirect_uri": retour,
                                                                 "response_type": "code"}))
            code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(e.headers["Location"]).query))["code"]
            jeton = demande("/token", urllib.parse.urlencode({"grant_type": "authorization_code", "code": code}).encode(),
                            "application/x-www-form-urlencoded")
            demande("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "inconnu", "arguments": {}}}).encode())
            serveur.shutdown()
        lignes = journal.getvalue().splitlines()
        self.assertIn("meteo: POST /register 201 ua=Google-Gemini/2.0 client=Gemini_Connecteur", lignes)
        self.assertIn("meteo: GET /authorize 302 ua=Google-Gemini/2.0 retour=oauth-redirect.googleusercontent.com", lignes)
        self.assertIn("meteo: POST /token 200 ua=Google-Gemini/2.0 grant=authorization_code", lignes)
        self.assertIn("meteo: POST /mcp 200 ua=Google-Gemini/2.0 rpc=tools/call:inconnu erreur=-32602", lignes)
        for secret in (client["client_id"], client["client_secret"], code, jeton["access_token"], jeton["refresh_token"]):
            self.assertNotIn(secret, journal.getvalue())


if __name__ == "__main__":
    unittest.main()
