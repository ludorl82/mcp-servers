# mcp-servers

Quatre serveurs [MCP](https://modelcontextprotocol.io) écrits pour le labo de
[labodeludo.dev](https://labodeludo.dev), où ils servent l'appli Claude (en
mode vocal aussi) et un assistant Home Assistant. Un seul fichier Python
chacun, **la bibliothèque standard seulement** : rien à installer, rien à
construire.

| Serveur | Ce qu'il donne | Données |
|---|---|---|
| [`hockey/`](hockey/server.py) | Les sept équipes canadiennes de la LNH : horaire, pointage en direct avec les marqueurs, résultats, classement, statistiques des joueurs | API publique de la LNH, sans clé |
| [`meteo/`](meteo/server.py) | Météo actuelle, prévisions, heure par heure ; alertes et Cote air santé au Canada | [Open-Meteo](https://open-meteo.com) (CC BY 4.0), Environnement et Changement climatique Canada (Licence du gouvernement ouvert – Canada) |
| [`articles/`](articles/server.py) | Chercher par le sens dans les articles de labodeludo.dev, en lire un, lister les derniers | Le site lui-même, et un [Ollama](https://ollama.com) pour vectoriser la question |
| [`web/`](web/server.py) | Lire une page web en direct : son texte, et surtout où elle mène (chaque redirection, l'adresse finale, l'avertissement quand on change de site) | La page demandée. **Un proxy : à garder privé**, voir plus bas |

Les réponses sont en français, en **phrases courtes prêtes à lire à voix
haute** : pas de tableau, pas de JSON, l'heure dite comme on la dit. Le
modèle qui appelle l'outil n'a que ce texte pour décider, et la personne qui
écoute n'a pas d'écran. C'est aussi vrai des messages d'erreur : chacun dit
ce qui ne va pas et quoi faire ensuite.

## Essayer sans rien installer

Le serveur `articles` est ouvert à tous :

```
https://articles.labodeludo.dev/mcp
```

Dans l'appli Claude : *Paramètres → Connecteurs → Ajouter un connecteur
personnalisé*, avec cette adresse. Pas de compte, pas de clé ; il ne fait que
lire le blogue. Le débit est limité, et chaque recherche passe par une carte
graphique du labo : pour un usage soutenu, lancez votre copie.

`hockey`, `meteo` et `web` n'ont pas d'adresse publique : ils se lancent en une
commande (plus bas), et leurs API sources ont leurs propres limites.

## `web`, un serveur à garder privé

Un index de recherche peut montrer une vieille copie d'un site qui, en vrai,
redirige ailleurs depuis des années. `web` lit la page en direct et met les
redirections en tête de sa réponse :

```
Attention : lbdl.dev redirige vers un AUTRE site, labodeludo.dev. La page lue est celle de labodeludo.dev.
Adresse demandée : http://lbdl.dev/
Redirections (1) :
  1. 301 (déplacée pour de bon) : http://lbdl.dev/ → https://labodeludo.dev/
Adresse finale : https://labodeludo.dev/
```

Il suit les redirections HTTP et la balise `<meta http-equiv="refresh">` ; il
n'exécute pas le JavaScript, mais signale une page presque vide qui redirige
par `window.location`. Le texte rendu laisse de côté menus, en-têtes et pieds
de page.

Un serveur qui va chercher l'adresse qu'on lui donne est un **proxy** : ne
le mettez jamais derrière une adresse publique sans authentification. Il se
défend quand même. Il refuse toute adresse qui n'est pas sur Internet
(privée, locale, CGNAT, lien local comme `169.254.169.254`), à chaque saut
de redirection, et se connecte à l'adresse IP qu'il vient de vérifier plutôt
que de résoudre le nom une deuxième fois. Ports 80 et 443 seulement, pas
d'identifiant dans l'adresse. Dans Kubernetes, sa base ajoute une
NetworkPolicy de sortie qui refait la même chose au niveau du réseau ;
l'entrée, elle, est à fermer selon votre déploiement.

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

Tous ensemble, avec Docker : `docker compose up` (ports 8081 à 8084 ;
`web` n'écoute que sur 127.0.0.1).

Pour essayer à la main :

```sh
curl -s localhost:8080/mcp -H 'Content-Type: application/json' -d \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"match_lnh","arguments":{}}}'
```

Dans Kubernetes, chaque dossier est une base Kustomize (un Deployment et un
Service, plus la NetworkPolicy de sortie de `web`, sans namespace ; `server.py` monté depuis un ConfigMap sur l'image
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

Hors ligne : le protocole, la configuration, le transport HTTP, et pour
`web` les redirections et l'extraction contre un faux site local. Aucun
appel aux API.

## English

Four stdlib-only Python MCP servers (Streamable HTTP, stateless, plain JSON
responses): Canadian NHL teams, weather with Canadian alerts and air quality,
semantic search over the labodeludo.dev blog, and a web page reader that
reports every redirect and the final URL. The web reader is a proxy: keep it
private; it refuses non-public addresses on every hop and pins the checked IP. Answers are in French and
written to be read aloud. Run with `python3 <server>/server.py`; configure
through the environment variables above. The `articles` server is open at
`https://articles.labodeludo.dev/mcp` (rate-limited). MIT licence.
