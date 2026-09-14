# Mémoire Ops V1

`ops-memory` implémente la mémoire externe des exigences 34–44 et 60–63. Le
service est local-first, persistant, cloisonné et utilisable sans API externe.
Il ne remplace ni OpenBao, ni Git, ni l’inventaire, ni le monitoring, ni la base
d’état des workflows.

## Architecture

Le pipeline normal est :

```text
requête authentifiée par UID Unix
  -> filtre projet/environnement/classification/rôles
  -> embedding CPU local
  -> Qdrant (10 à 30 candidats)
  -> nouveau filtre côté catalogue SQLite
  -> reranker CPU local
  -> 1 à 5 résultats au contexte actif
```

La mémoire chaude est uniquement le petit résultat renvoyé au modèle. La
mémoire de travail utilise le niveau `working` avec une durée de validité. La
mémoire froide utilise le niveau `cold`, le catalogue SQLite sur SSD et
Qdrant. Le texte d’origine n’est jamais détruit par une synthèse.

Qdrant est l’index vectoriel primaire. Son API loopback exige une clé dédiée.
Sa seule copie persistante se trouve dans l’objet KV v2 exact
`kv-infra-shared/data/memory/qdrant` d’OpenBao. Seuls le RoleID et le SecretID
de l’AppRole `ops-memory-runtime` persistent localement, chiffrés séparément par
`systemd-creds` avec la combinaison clé hôte + TPM2. Au démarrage,
`ops-memory-secrets.service` obtient un token de 60 secondes limité à deux
usages, lit cet objet exact, révoque le token, puis et seulement puis publie la
clé dans `/run/ops-memory-secrets/qdrant-api-key`, root-only et en tmpfs.
Chaque consommateur utilise `LoadCredential=` pour recevoir sa propre copie
systemd. Pour Qdrant, un renderer crée son `production.yaml` privé dans
`/run/ops-memory-qdrant`, également en tmpfs ; seule cette configuration est
montée dans le conteneur. La valeur n’apparaît ni dans argv ni dans la
configuration persistante du conteneur. Il n’existe ni clé claire sous `/etc`,
ni blob de clé Qdrant dans le credstore, ni secret persistant dans le magasin
Podman. SQLite est le catalogue cohérent des
enregistrements, sources, validités, supersessions, liens de synthèse et audits.
Si Qdrant est indisponible, une recherche lexicale locale, filtrée et bornée à
`fallback_scan_limit`, permet un fonctionnement dégradé explicite. Les réponses
portent alors `backend=sqlite-lexical-degraded` et `degraded=true`.

L’embedding `ops-hash-embedding-v1` est un modèle CPU déterministe à hachage de
caractéristiques (tokens et bigrammes), sans allocation VRAM ni dépendance
Python lourde. Il donne une base reproductible sur le Dell. L’interface permet
l’escalade vers une API compatible OpenAI pour une recherche importante, après
activation, avec un appel maximum par opération et un budget journalier. Le
reranker local combine similarité vectorielle, recouvrement lexical, autorité,
importance et récence. Un reranker HTTP de secours est également prévu.

Les deux API sont désactivées par défaut. La configuration ne contient que le
nom d’un credential systemd, jamais sa valeur. Les déclarations optionnelles
`LoadCredentialEncrypted=` recherchent les blobs dans les magasins de
credentials systemd ; leur absence n’empêche pas le démarrage. Un provisionneur
OpenBao peut fournir ou renouveler ces valeurs hors de la configuration. Sans
credential, le résultat local est conservé automatiquement. Le cœur accepte
également une référence de variable d’environnement pour les tests supervisés,
mais la configuration de production n’utilise pas ce mécanisme.

## Sécurité et cloisonnement

Le service écoute uniquement sur le socket Unix
`/run/ops-memory/ops-memory.sock`, mode `0660`, groupe `ops-memory-api`. Le
serveur lit `SO_PEERCRED` pour obtenir l’UID réel. Une requête ne peut pas
choisir librement ses rôles : l’acteur déclaré doit être lié à cet UID dans
`memory.toml`, puis les rôles, projets, environnements, classification maximale
et capacités viennent exclusivement de cette politique. La politique borne
aussi les types de sources qu’un acteur peut déclarer, les sources qu’il peut
considérer comme autoritatives, le droit de marquer une information revue et le
droit de consommer le budget d’une API externe.

Les filtres sont appliqués deux fois : dans la requête Qdrant, puis sur les
enregistrements du catalogue. Cette seconde vérification empêche une erreur de
filtre, un index périmé ou une réponse Qdrant incorrecte de franchir une
frontière de projet. Une ressource hors périmètre est renvoyée comme inexistante.

Les écritures sont elles aussi contrôlées. `allowed_roles` doit être un
sous-ensemble des rôles que l’acteur est autorisé à approvisionner. Un auteur
qui écrit pour un rôle qu’il ne possède pas reçoit seulement l’identifiant et
le statut technique, pas les métadonnées d’un éventuel doublon existant.

Le service refuse notamment :

- les clés privées, tokens Bearer, clés API et affectations de mots de passe
  détectables ;
- les URI de source relatives, contenant des credentials ou une query string ;
- la catégorie `secrets` et le type `openbao-secret` ;
- les règles, procédures et décisions non revues ;
- la promotion durable d’un message Zulip sans `promote=true` ;
- les changements de portée lors d’une supersession.

Les secrets restent dans OpenBao. Ils ne doivent être placés ni dans `content`,
ni dans `metadata`, ni dans une URI ou référence de source.

## Admission et cycle de vie

Chaque mémoire contient au minimum : contenu, projet, environnement,
classification, catégorie, type de mémoire, niveau, audience, source, statut
d’autorité, importance et période de validité. Les métadonnées opérationnelles
autorisées comprennent notamment serveur, service, version, sévérité, agent,
statut, incident et mission.

Les types sont : `noise`, `conversation`, `observation`, `information`,
`decision`, `rule`, `procedure`, `incident` et `summary`.

- Le bruit et les conversations non promues ne sont pas enregistrés.
- Une observation sans expiration reçoit automatiquement le TTL configuré.
- Une empreinte normalisée empêche les doublons dans une même portée et pour
  une même audience ; les nouvelles références sont ajoutées à
  `memory_sources`.
- `supersedes_id` marque atomiquement l’ancienne version et programme sa
  suppression de Qdrant. La supersession exige l’accès à l’ancienne mémoire et
  ne peut élargir son audience, abaisser sa classification ou changer sa
  catégorie sans une capacité distincte de déclassification/reclassification.
- Une invalidation conserve l’historique et sa raison mais retire la mémoire
  des recherches courantes.
- Une synthèse structurée conserve objectif, décisions, changements et éléments
  ouverts, puis référence tous les originaux dans `summary_members`.

La maintenance quotidienne :

1. invalide les entrées dont la période de validité est terminée ;
2. marque comme périmés les anciens contextes non normatifs ;
3. retire de Qdrant les mémoires invalidées ou remplacées ;
4. réindexe les écritures restées en attente pendant une panne ;
5. enregistre un bilan dans `maintenance_runs`.

Elle ne réécrit jamais automatiquement une règle ou une procédure. La
consolidation sémantique passe par l’opération explicite `summarize`, afin qu’un
modèle faible ne puisse pas transformer silencieusement une règle de production.

## Sources de vérité

Une réponse mémoire porte toujours `advisory_only=true` et `verify_against`.
Le fichier de configuration donne les sources d’autorité par catégorie :

| Donnée | Source d’autorité |
|---|---|
| Secrets | OpenBao, jamais `ops-memory` |
| Code et configuration | Git |
| Version de production | Déploiement et inventaire |
| État actuel | Monitoring |
| Communication | Zulip |
| Mémoire sémantique | Qdrant |
| Workflow | Base structurée Hermes/broker |
| Documentation | Dépôt documentaire Git |

Une source configurée n’est marquée `authoritative` que si l’entrée est revue.
Un contenu produit par `llm`, `model` ou `generated` reste `derived`. Le LLM
n’est jamais une source de vérité.

## Protocole local

Le socket et le mode stdio utilisent une ligne JSON par requête et réponse :

```json
{
  "version": 1,
  "request_id": "a5cfd4b9-00a4-447a-a551-31425335ae6f",
  "operation": "search",
  "actor": "hermes-coordinator",
  "payload": {
    "query": "dernière décision concernant le proxy Minecraft",
    "project": "minecraft",
    "environment": "production",
    "max_classification": "restricted",
    "categories": ["decision"],
    "top_k": 3,
    "candidate_limit": 20,
    "important": false,
    "allow_api": false
  }
}
```

Opérations disponibles :

- `health` et `stats` ;
- `ingest`, `search` et `get` ;
- `invalidate` ;
- `summarize` ;
- `maintain` pour l’identité technique dédiée.

Un adaptateur séparé utilise le SDK MCP officiel épinglé (`mcp==2.1.1`) et
publie sept tools : `memory_search`, `memory_get`, `memory_ingest`,
`memory_summarize`, `memory_invalidate`, `memory_stats` et `memory_health`.
L’acteur n’apparaît dans aucun schéma de tool : il est fixé par
`OPS_MEMORY_MCP_ACTOR` au lancement du serveur, puis vérifié par l’UID du
processus lorsque l’adaptateur appelle le socket. Le chemin du socket est lui
aussi fixé à `/run/ops-memory/ops-memory.sock`.

Lancement MCP supervisé pour Codex :

```bash
OPS_MEMORY_MCP_ACTOR=codex-supervised \
  /opt/ops-memory/bin/ops-memory-mcp
```

Hermes lance `/usr/local/libexec/ops-memory-mcp-profile` avec un nom de profil
root-owned. Pour les profils secondaires, une règle sudo exacte adopte le compte
`minecraft-ops`, `infra-network`, `ops-monitor`, `backup-agent`, `deploy-agent`
ou `security-audit` avant de fixer l’acteur. Un changement de variable ne permet
pas une usurpation : la politique du socket exige simultanément l’UID Unix
configuré. `infra-shared` et `network-shared` partagent volontairement le compte
technique réseau, mais conservent deux acteurs et des listes de rôles distinctes.

Les enveloppes refusent les champs inconnus. Les erreurs ont un code stable et
ne renvoient jamais de traceback. Les requêtes sont limitées en taille et le
socket applique un timeout.

Pour un client déjà autorisé par son compte Unix :

```bash
/opt/ops-memory/bin/ops-memory request \
  --socket /run/ops-memory/ops-memory.sock \
  --input /chemin/requete.json
```

Pour un connecteur stdio supervisé :

```bash
/opt/ops-memory/bin/ops-memory stdio --config /etc/ops-memory/memory.toml
```

Le mode stdio conserve exactement la même liaison acteur/UID et les mêmes
politiques.

## Installation Fedora

Dépendances système exactes : Python 3.11 ou ultérieur, systemd/systemd-creds,
un TPM2, Podman et le SDK MCP Python épinglé installé dans un environnement
séparé. Le cœur du runtime Python utilise uniquement la bibliothèque standard.
Sur Fedora :

```bash
sudo dnf install -y python3 podman
sudo systemctl stop ops-memory.service ops-memory-qdrant.service \
  ops-memory-secrets.service ops-memory-maintenance.service \
  ops-memory-maintenance.timer ops-memory-health.service ops-memory-health.timer
sudo /usr/local/sbin/provision-memory-openbao
sudo /home/ops-user/ops-control-plane/scripts/install-memory-stack.sh --enable
```

Le provisionneur est installé par `configure-openbao.sh` et par une installation
dormante de la mémoire. Il demande une confirmation littérale et le mot de passe
OpenBao de `ops-user`, sans jamais afficher de valeur. Il réconcilie la policy et
l’AppRole bornés, centralise la clé existante, teste login/lecture/révocation,
chiffre uniquement RoleID et SecretID avec `host+tpm2`, puis efface l’ancien
`/etc/credstore.encrypted/ops-memory-qdrant-api-key`. En cas d’échec, le jeu de
credentials local et le blob historique sont remis dans leur état antérieur.
Les services restent arrêtés. Si une clé existe déjà dans OpenBao et dans le
blob historique, les valeurs doivent être identiques ; sinon l’outil refuse
toute modification.

L’installateur :

- crée `opsmemory` et le groupe de socket `ops-memory-api` ;
- installe le code root-owned sous `/opt/ops-memory` ;
- installe la politique sous `/etc/ops-memory` ;
- tire `docker.io/qdrant/qdrant:v1.19.0`, puis écrit son ID SHA-256 local
  immuable dans `qdrant.env` ;
- refuse tout ancien blob Qdrant, secret Podman ou clé claire tant que la
  migration explicite n’a pas réussi ;
- installe le résolveur OpenBao, son unité oneshot et les deux références
  AppRole `host+tpm2`, sans créer de clé ni de solution de repli ;
- installe les unités durcies, le watchdog systemd et un probe externe chaque
  minute ;
- ordonne Qdrant, la mémoire et la maintenance avec `Requires=` et `After=` sur
  `ops-memory-secrets.service`, elle-même après `openbao-unseal.service` ;
- valide le certificat CA public d’OpenBao puis en installe une copie dédiée
  root-owned sous `/etc/ops-memory/openbao-ca.crt`, sans ouvrir le répertoire
  TLS aux comptes techniques ;
- active Qdrant, le service mémoire et les timers uniquement avec `--enable`.

L’option `--skip-image-pull` interdit le téléchargement Qdrant et exige que
l’image versionnée soit déjà présente. L’installation du SDK MCP requiert
néanmoins l’accès à l’index Python configuré si son wheel n’est pas en cache.
Aucun token API applicatif n’est nécessaire à l’installation. Le provisionnement
échoue explicitement si `host+tpm2` n’est pas disponible ; aucune solution de
repli en clair, par variable d’environnement ou liée uniquement au logiciel
n’est utilisée.

Contrôles après installation :

```bash
systemctl --no-pager --full status ops-memory-secrets.service \
  ops-memory-qdrant.service ops-memory.service
sudo stat -c '%U:%G %a %n' /run/ops-memory-secrets/qdrant-api-key
sudo test ! -e /etc/credstore.encrypted/ops-memory-qdrant-api-key
systemctl list-timers --all ops-memory-maintenance.timer ops-memory-health.timer
ss -lntx | grep -E '6333|ops-memory.sock'
sudo -u opsmemory /opt/ops-memory/bin/ops-memory maintain \
  --config /etc/ops-memory/memory.toml --actor memory-maintenance
```

Qdrant écoute avec authentification sur `127.0.0.1:6333`. Aucun port mémoire
n’est exposé au réseau. La clé Qdrant n’est persistée que par OpenBao ; le TPM
protège uniquement les deux identifiants AppRole locaux. Le service refuse de
démarrer si OpenBao, l’unseal, la lecture exacte ou la révocation du token
échoue. Arrêter `ops-memory-secrets.service` retire la copie tmpfs et propage
l’arrêt aux consommateurs qui la requièrent.
Le watchdog de 30 secondes dépend du heartbeat réel de la boucle serveur. Le
timer `ops-memory-health` vérifie indépendamment chaque minute le socket, SQLite
et Qdrant ; un état Qdrant dégradé fait échouer le probe.
Le conteneur a une limite de 768 Mio et 2 CPU ; le service Python a une limite
de 384 Mio. Le catalogue est dans `/var/lib/ops-memory` et les vecteurs Qdrant
dans le répertoire séparé, détenu par root, `/var/lib/ops-memory-qdrant`.

Le plan déterministe dédié est installé par
`scripts/install-memory-backup.sh` : sauvegarde en ligne SQLite, snapshot de
collection Qdrant, manifeste SHA-256, rétention de 14 jeux et test hebdomadaire
de restauration dans un conteneur éphémère. Aucune copie à chaud des fichiers
Qdrant n'est effectuée. La procédure complète et les limites de cohérence
inter-stockages sont documentées dans
`../runbooks/memory/memory-backup-restore.md`.

Les unités `OnSuccess`/`OnFailure` publient séparément le résultat de la dernière
tentative et l'horodatage du dernier succès pour le backup et le test de
restauration : `ops_memory_backup_last_*` et
`ops_memory_restore_test_last_*`. Un échec ne remplace jamais l'horodatage du
dernier succès et les alertes distinguent absence, échec et vieillissement.

## Tests

Depuis le dépôt, avec l’environnement de développement existant :

```bash
/home/ops-user/ops-control-plane/broker/.venv/bin/python -m pytest \
  /home/ops-user/ops-control-plane/memory/tests -q
shellcheck /home/ops-user/ops-control-plane/scripts/install-memory-stack.sh \
  /home/ops-user/ops-control-plane/scripts/install-memory-backup.sh \
  /home/ops-user/ops-control-plane/memory/deploy/ops-memory \
  /home/ops-user/ops-control-plane/memory/deploy/ops-memory-mcp \
  /home/ops-user/ops-control-plane/memory/deploy/ops-memory-qdrant-launcher
python3 -m py_compile \
  /home/ops-user/ops-control-plane/scripts/provision-memory-openbao \
  /home/ops-user/ops-control-plane/memory/deploy/ops-memory-openbao-resolve
```

Les tests couvrent embeddings déterministes, reranking, filtres doublés,
liaison UID, fuite inter-rôle, secrets, admission sélective, TTL, déduplication,
supersession, synthèse, invalidation, redémarrage, panne Qdrant, réindexation,
obsolescence, protocole, configuration et assets de déploiement.
Ils vérifient aussi les schémas et l’identité fixe de l’adaptateur MCP officiel.
