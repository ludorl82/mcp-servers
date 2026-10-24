# mcp-servers

Trois serveurs [MCP](https://modelcontextprotocol.io) écrits pour le labo de
[labodeludo.dev](https://labodeludo.dev), où ils servent l'appli Claude (en
mode vocal aussi) et un assistant Home Assistant. Un seul fichier Python
chacun, **la bibliothèque standard seulement** : rien à installer, rien à
construire.

| Serveur | Ce qu'il donne | Données |
|---|---|---|
| [`hockey/`](hockey/server.py) | Les sept équipes canadiennes de la LNH : horaire, pointage en direct avec les marqueurs, résultats, classement, statistiques des joueurs | API publique de la LNH, sans clé |
| [`meteo/`](meteo/server.py) | Météo actuelle, prévisions, heure par heure ; alertes et Cote air santé au Canada | [Open-Meteo](https://open-meteo.com) (CC BY 4.0), Environnement et Changement climatique Canada (Licence du gouvernement ouvert – Canada) |
| [`articles/`](articles/server.py) | Chercher par le sens dans les articles de labodeludo.dev, en lire un, lister les derniers | Le site lui-même, et un [Ollama](https://ollama.com) pour vectoriser la question |

Les réponses sont en français, en **phrases courtes prêtes à lire à voix
haute** : pas de tableau, pas de JSON, l'heure dite comme on la dit. Le
modèle qui appelle l'outil n'a que ce texte pour décider, et la personne qui
écoute n'a pas d'écran. C'est aussi vrai des messages d'erreur : chacun dit
ce qui ne va pas et quoi faire ensuite.

## Le transport

MCP « Streamable HTTP » réduit au strict nécessaire, ce que la spécification
permet : un `POST /mcp` porte un message JSON-RPC (ou un lot), la réponse est
du JSON simple, jamais un flux SSE, et le serveur ne garde aucune session.
`GET /mcp` répond 405, `GET /healthz` répond `ok`. Les versions du protocole
acceptées vont de `2024-11-05` à `2025-11-25`.

La couche JSON-RPC est recopiée dans chaque fichier, exprès : un serveur se
copie et se lance seul, sans module à côté.

## Les lancer

N'importe quel Python 3.12 ou plus récent :

```sh
python3 hockey/server.py                       # écoute sur :8080
PORT=8082 METEO_LIEU="Sherbrooke" python3 meteo/server.py
```

Les trois ensemble, avec Docker : `docker compose up` (ports 8081 à 8083).

Pour essayer à la main :

```sh
curl -s localhost:8080/mcp -H 'Content-Type: application/json' -d \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"match_lnh","arguments":{}}}'
```

Dans Kubernetes, chaque dossier est une base Kustomize (un Deployment et un
Service, sans namespace ; `server.py` monté depuis un ConfigMap sur l'image
`python` officielle) :

```yaml
resources:
  - https://github.com/ludorl82/mcp-servers//hockey?ref=<commit>
```

Pour l'appli Claude, il faut une adresse HTTPS publique devant `/mcp` : le
connecteur est appelé depuis le nuage d'Anthropic, pas depuis votre réseau.

## La configuration

Tout passe par des variables d'environnement ; aucune n'est obligatoire.

| Variable | Serveur | Rôle | Défaut |
|---|---|---|---|
| `HOST`, `PORT` | tous | Adresse d'écoute | toutes les interfaces, `8080` |
| `HOCKEY_EQUIPE` | hockey | Équipe par défaut : nom, ville ou abréviation (`Oilers`, `EDM`) | le Canadien |
| `METEO_LIEU` | meteo | Lieu par défaut, géocodé par Open-Meteo. **Jamais répété dans les réponses** : il peut rester privé | aucun : chaque appel nomme un lieu |
| `METEO_NOM` | meteo | Comment dire le lieu par défaut | « la maison » |
| `ARTICLES_SITE` | articles | Le site à relire | `https://labodeludo.dev` |
| `ARTICLES_EMBED` | articles | L'API `/api/embed` d'Ollama | `http://localhost:11434/api/embed` |
| `ARTICLES_MODELE` | articles | Le modèle de vecteurs, le même que celui de l'index | `bge-m3` |

Sans Ollama, `articles` cherche par les mots et le dit dans sa réponse.
Il lit deux fichiers que le site publie, `/bob-vectors.json` et
`/search-index.json` ; un autre site peut s'en servir s'il publie le même
format (voir l'en-tête de [`articles/server.py`](articles/server.py)).

## Les tests

```sh
python3 -m unittest discover -s tests -v
```

Hors ligne : le protocole, la configuration, le transport HTTP. Aucun appel
aux API.

## English

Three stdlib-only Python MCP servers (Streamable HTTP, stateless, plain JSON
responses): Canadian NHL teams, weather with Canadian alerts and air quality,
and semantic search over the labodeludo.dev blog. Answers are in French and
written to be read aloud. Run with `python3 <server>/server.py`; configure
through the environment variables above. MIT licence.
