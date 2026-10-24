"""Tests hors ligne des trois serveurs : le protocole MCP et la configuration.

Aucun appel réseau : chaque cas s'arrête avant l'API (outil inconnu, équipe
inconnue, lieu absent), ou ne touche que le serveur HTTP local.

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SERVEURS = {"hockey": "match_lnh", "meteo": "meteo", "articles": "chercher_articles"}
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


if __name__ == "__main__":
    unittest.main()
