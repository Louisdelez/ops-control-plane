# Orchestrateur IA V1

Ce composant couvre le routage et l'observabilité des modèles sans exécuter
d'action d'exploitation. L'autorisation et l'exécution restent exclusivement
du ressort du broker et de ses runbooks déterministes.

## Rôles et routage

Les agents utilisent sept rôles stables, indépendants des fournisseurs :

- `ROLE_TINY` : classification et décisions simples via Qwen3.7 Flash ;
- `ROLE_LOCAL_OPS` : palier opérations standard, via DeepSeek V4 Flash hébergé
  par Alibaba, puis Qwen3.7 Plus, puis DeepSeek V4 Flash via l'API directe ; son
  identifiant historique ne désigne plus une exécution locale ;
- `ROLE_REASONING` : raisonnement expert, via DeepSeek V4 Pro ;
- `ROLE_PREMIUM` : palier exceptionnel, via Qwen3.8 Max uniquement lorsque les
  paliers capables moins chers échouent, sauf enjeu critique qui le justifie
  directement ;
- `ROLE_CODER` : proposition de code uniquement ;
- `ROLE_EMBEDDING` : embedding par API facultative ;
- `ROLE_RERANKER` : reranker par API facultative.

La configuration runtime par défaut ne contient aucun fournisseur Ollama. Le
fournisseur de chaque rôle est une liste dont l'ordre d'essai est recalculé au
coût réservé estimé, à capacité égale. Il peut donc être changé sans modifier
les agents. La sélection la moins chère n'abaisse jamais le niveau de capacité
requis : le routage examine d'abord le type de tâche, la complexité, le risque,
la confiance, l'urgence, le volume du contexte et le plafond de coût de la
requête, puis trie les fournisseurs du rôle retenu. Son ordre général est :
outil déterministe, utility, opérations standard, raisonnement, premium, puis
humain. Le premium n'est donc pas essayé si un résultat suffisamment confiant
est obtenu auparavant. Une tâche critique peut aller directement au premium,
mais reste soumise à approbation humaine et aux mêmes seuils de capacités ; si
le profil ne les atteint pas, aucun appel n'est émis. Une tâche déterministe ne
déclenche aucun appel LLM, même si elle est longue.

La matrice générative par défaut est :

| Rôle | Fournisseurs API, du coût estimé le plus bas au plus haut |
| --- | --- |
| `ROLE_TINY` | Qwen3.7 Flash |
| `ROLE_LOCAL_OPS` | DeepSeek V4 Flash (Alibaba Global), Qwen3.7 Plus, DeepSeek V4 Flash (API directe) |
| `ROLE_REASONING` | DeepSeek V4 Pro |
| `ROLE_PREMIUM` | Qwen3.8 Max |
| `ROLE_CODER` | Qwen3 Coder Flash, DeepSeek V4 Pro |

L'ordre indiqué correspond aux tarifs conservateurs configurés au 5 septembre
2026 ; l'orchestrateur recalcule le coût réservé pour chaque requête à partir
de ces prix. Les remises temporaires, cache ou heures creuses ne sont pas
présumées. Le point d'accès Alibaba Global est statiquement épinglé ; les chemins
de fichiers de clés passent par l'environnement, tandis que les modèles et prix sont épinglés
dans la configuration revue. Une URL Qwen Global ne garantit pas une
résidence des données dans l'Union européenne : la région et le workspace
doivent être choisis et validés explicitement avant activation.
Le provider DeepSeek hébergé par Alibaba est réservé à `ROLE_LOCAL_OPS` et
emploie le dialecte Alibaba avec `enable_thinking=false`. Il partage le drapeau
d'activation, l'endpoint Global Singapore et le fichier de clé Qwen ; aucun troisième
secret fournisseur n'est requis. Le provider DeepSeek direct reste un fallback
distinct, et `ROLE_CODER` demeure Qwen3 Coder Flash puis DeepSeek V4 Pro.
Les calculs et sources sont archivés dans
`docs/model-api-pricing-2026-09-05.md`.

## Catalogue public v2 et cartes de modèles

Le registre `catalog/model-catalog.v2.json` contient les 59 entrées du fichier
fourni sous 58 cartes normalisées et 21 comptes fournisseur. Parmi elles,
48 cartes disposent d'au moins une grille tarifaire et 11 cartes restent
visibles en `quarantined`, sans déploiement ni tarif inventé. Les tarifs
creux/pleins de chaque DeepSeek V4 sont des schedules
d'une même carte, tandis qu'Aya Expanse 8B et 32B sont deux cartes.

La répartition d'état est de 5 cartes `configured`, 42 `catalogued` et 11
`quarantined` dans le fichier source. Au chargement, un générateur fail-closed
ajoute actuellement 9 liaisons API revues : 14 cartes deviennent invocables et
16 déploiements sont rattachés au catalogue. Il exige un ID exact avec source
officielle allowlistée, un prix entrée/sortie officiellement vérifié, et un
contrat endpoint/auth/protocole supporté; chaque refus expose son motif.
`qwen-coder-api` reste une voie runtime
auxiliaire explicitement déclarée hors catalogue : son identifiant
`qwen3-coder-flash-2025-07-28` n'est celui d'aucune carte du seed, et il n'est
donc rattaché artificiellement ni à Qwen3-Coder-Next, ni aux cartes 30B/480B.
Les backups génériques embedding/reranker sont déclarés auxiliaires pour la même
raison d'absence d'identifiant de carte fixe.

Une carte, un compte fournisseur, un déploiement et un tarif sont des objets
distincts. Ainsi, plusieurs cartes Alibaba partagent une seule référence
OpenBao ; aucune clé n'est dupliquée par modèle. Les quatre états à ne jamais
confondre sont : carte cataloguée, déploiement déclaré, clé résolue localement
par le runtime, puis canari réseau fournisseur réussi. Le statut `configured`
affirme seulement qu'un identifiant de déploiement correspondant existe dans
les sources runtime. Il ne prouve aucune des trois étapes suivantes.

Le prix affiché par une carte est la grille source attachée au compte de cette
carte. Il peut différer du coût du même modèle servi par un autre hébergeur :
DeepSeek V4 Flash, par exemple, possède un déploiement direct et un déploiement
Alibaba. Pour une prévisualisation limitée aux déploiements runtime, chaque
déploiement disponible fournit son propre coût du scénario commun, calculé avec
le tarif conservateur de son hébergeur ; le routeur sélectionne alors le moins
cher et expose cette base comme `runtime_deployment`. Hors runtime, la base
reste `catalogue_card`. Une estimation de carte ne doit donc jamais être
présentée comme la facture garantie d'un hébergeur alternatif.

Les scores 0–10 initiaux sont explicitement des priors éditoriaux à faible
confiance, issus des descriptions du seed. Ils ne sont ni des benchmarks ni une
preuve de qualité. Le scénario commun fixe 1 000 appels de 10 000 tokens
d'entrée et 2 000 de sortie, soit les mêmes 10 M/2 M mensuels, pour rendre les
paliers par requête reproductibles. Les calculs sont effectués en microdollars
avec arrondi prudent, jamais en flottants binaires.

Les cartes exposent séparément fenêtre de contexte, langues, limitations,
latences p50/p95 et provenance technique. Les valeurs absentes du seed restent
nulles ou vides sauf enrichissement officiel séparément sourcé. Les grilles
tarifaires comportent également un prix d'entrée cache nullable, leur confiance
et leur source : le TXT n'en donne aucun, tandis que les valeurs enrichies ne
sont admises qu'avec une source officielle explicite.

`POST /v1/catalogue/route-preview` applique d'abord les seuils de capacités et
spécialités comme contraintes dures, puis trie les survivants au coût mensuel
prudent. Par défaut il exclut toute carte sans déploiement revu et disponible.
Cette route est consultative : elle ne fait aucun appel fournisseur, ne change
aucune activation et ne remplace pas les règles de risque ou l'autorisation du
broker.

Les 58 cartes sont consultables et comparables. Le runtime source relie 5 cartes
statiques et génère 9 liaisons revues via Z.ai, OpenAI Responses et Moonshot :
14 cartes ont ainsi un contrat d'invocation et 44 sont bloquées avec un motif
explicite. Une liaison générée ou une clé stockée ne prouve ni activation, ni
disponibilité fournisseur, ni succès d'un canari réseau.

Un résultat de modèle n'est accepté que sous l'enveloppe JSON stricte définie
dans `providers.py`, avec une confiance suffisante. Prose libre, champ inattendu,
dépassement de taille, timeout ou usage de tokens supérieur à la réservation
provoquent un arrêt sûr ou une escalade. Les sorties sont consultatives. Le code
est marqué `code_proposal_review_tests_git_required` et ne peut jamais modifier
la production depuis la réponse du modèle.

Une tâche est **importante** si son risque vaut `HIGH` ou `CRITICAL`, ou si son
impact est supérieur ou égal à 0,75. Son résultat exécutant n'est jamais accepté
seul : un second appel doit le vérifier. Le candidat vérificateur doit être un
provider `openai_chat` distant, satisfaire les mêmes minima de capacités et
avoir à la fois un `provider_account_id` et une `chat_family` différents de
l'exécutant. Une identité inconnue ne prouve pas l'indépendance et provoque donc
un retour humain.

Le prompt de vérification conserve le contexte borné de la tâche et ajoute le
résultat exécutant sous le tag `UNTRUSTED_EXECUTOR_RESULT`; ce bloc est traité
comme donnée non fiable, jamais comme instruction. Le vérificateur doit rendre
explicitement `verification=agree|disagree|uncertain`. Seuls `status=ok`,
`verification=agree` et une confiance au-dessus du seuil permettent de conserver
le résultat exécutant. Un désaccord ou une incertitude est terminal afin de ne
pas sélectionner opportunément un avis favorable. Une panne purement technique
peut essayer le candidat indépendant suivant, mais chaque essai réserve et
comptabilise son coût et consomme les limites globales d'appels et d'itérations.
Sans accord disponible dans ces bornes, la route finit en
`human_escalation_required`.

La réponse distingue `executor_result` de `verification`; même lors d'une
escalade humaine, le premier reste explicitement marqué comme non validé. Les
tâches de code importantes peuvent suivre ce contrôle indépendant, mais restent
des propositions exigeant tests, revue humaine et Git. Aucune vérification de
modèle n'est enregistrée comme validation externe d'apprentissage : seules les
preuves ajoutées via l'API dédiée alimentent `model_result_validations`.

Après `ROLE_REASONING` ou `ROLE_PREMIUM`, la trace rend le contrôle au plan
déterministe et à l'humain. Hermes doit alors transformer le plan en demandes
de runbooks autorisées ; le fournisseur API ne conserve pas le contrôle de la
mission.

Lors d'une montée Utility → Ops standard → Reasoning → Premium, seul le résumé
borné du modèle précédent peut être transmis. Il est marqué
`UNTRUSTED_PRIOR_MODEL_HANDOFFS`, n'est jamais traité comme une preuve et n'est
pas persisté. Les résultats d'outils restent les seules observations factuelles.

## Contexte minimal

`POST /v1/route` accepte uniquement cinq catégories : instructions de la
mission, mission, état courant, souvenirs pertinents et résultats d'outils. Les
valeurs par défaut limitent le contexte total à 12 000 caractères et 4 096
tokens estimés, avec au maximum huit souvenirs et huit résultats d'outils. Le
contenu n'est jamais stocké dans SQLite ni dans la trace. Seuls les métadonnées,
les décisions, les modèles, les tokens et le coût sont conservés.

Exemple sans appel de modèle :

```json
{
  "mission_id": "incident-483",
  "project_id": "minecraft",
  "task_type": "ANALYZE",
  "risk": "MEDIUM",
  "complexity": 0.2,
  "impact": 0.3,
  "confidence": 0.9,
  "urgency": "URGENT",
  "deterministic_available": true,
  "requested_max_cost_usd": "0.050000",
  "context": {
    "instructions": "Utiliser uniquement le runbook autorisé.",
    "mission": "Qualifier l'alerte lobby-02.",
    "current_state": "Le check déterministe est disponible.",
    "relevant_memories": [],
    "tool_results": []
  }
}
```

## API locale et CLI

Le daemon écoute uniquement
`/run/ops-orchestrator/api.sock`, mode `0660`, groupe
`opsorchestrator-api`. Il n'ouvre aucun port TCP entrant.

```bash
ops-orchestrator health
ops-orchestrator route --file request.json
ops-orchestrator budgets
ops-orchestrator performance
ops-orchestrator catalogue --offset 0 --limit 100
ops-orchestrator preview-candidates --file requirements.json
ops-orchestrator trace incident-483
ops-orchestrator signal mission_succeeded --project-id minecraft
ops-orchestrator record-outcome --file validated-outcome.json
```

Les routes HTTP équivalentes sont :

- `GET /v1/health` ;
- `GET /v1/budgets` ;
- `GET /v1/model-performance` ;
- `GET /v1/metrics` (snapshot Prometheus borné, utilisé par le publisher) ;
- `GET /v1/catalogue?offset=0&limit=100` (maximum 200 cartes par page) ;
- `GET /v1/traces/<mission_id>` ;
- `POST /v1/route` ;
- `POST /v1/catalogue/route-preview` ;
- `POST /v1/signals` ;
- `POST /v1/model-outcomes`.

L'API Unix et la façade HTTP loopback appliquent la même admission bornée :
au plus 16 threads de requête et un timeout de 5 secondes sur chaque opération
de lecture ou d'écriture du socket client. Une connexion excédentaire est
fermée sans parser ses octets. Une requête partielle ou un client qui cesse de
lire ne peut donc pas conserver indéfiniment un thread sous `TasksMax=64`.
Pour SSE, ce timeout borne seulement une opération socket bloquée ; il ne fixe
pas la durée totale d'un flux dont le client continue à consommer les chunks.

`POST /v1/signals` accepte uniquement l'objet exact
`{"signal":"...","project_id":"..."}`. Comme pour le routage, le daemon
obtient l'identité réelle du client par `SO_PEERCRED` et refuse le projet s'il
n'appartient pas au grant root-owned correspondant. Le projet est un identifiant
borné à 128 caractères sûrs et devient le label Prometheus `project`; cette
route ne contient aucune capacité d'approbation humaine.

`POST /v1/model-outcomes` exige exactement `route_id`, `mission_id`,
`project_id`, `outcome`, `quality`, `corrections_required` et `evidence_kind`.
Le daemon récupère lui-même catégorie, rôle, fournisseur, compte et modèle dans
ses écritures de route ; le client ne peut donc pas attribuer une note à un
autre modèle. Une route déterministe, escaladée ou inconnue est refusée, ainsi
qu'une seconde validation de la même route. Les preuves admises sont
`deterministic_test`, `human_review` et `production_observation`.

Par exemple :

```bash
curl --fail --unix-socket /run/ops-orchestrator/api.sock \
  -H 'Content-Type: application/json' \
  --data-binary @request.json http://localhost/v1/route
```

## Adaptateur MCP stdio pour Codex et Hermes

L'installation fournit le serveur MCP SDK officiel
`ops-orchestrator-mcp` et le wrapper fermé
`/usr/local/libexec/ops-orchestrator/ops-orchestrator-mcp-codex`. Le wrapper
n'accepte aucun argument, vide l'environnement, ne transmet aucun secret, fixe
l'identité à `codex-supervised` et borne les cinq scopes projets supervises.
L'adaptateur refuse un routage ou un signal hors scope avant d'ouvrir le socket
et verifie le projet renvoye avant de livrer une trace. Il relaie uniquement
neuf outils bornés : santé, routage consultatif, budgets, performances, trace,
signal d'observabilité et enregistrement d'une preuve externe. Il n'expose ni
exécution, ni secret, ni mécanisme d'approbation humaine.

Configuration Codex proposée :

```toml
[mcp_servers.ops_orchestrator]
command = "/usr/local/libexec/ops-orchestrator/ops-orchestrator-mcp-codex"
args = []
startup_timeout_sec = 10
tool_timeout_sec = 240
enabled = true
required = false
enabled_tools = [
  "get_orchestrator_health",
  "get_model_budgets",
  "get_model_performance",
  "get_model_catalogue",
  "get_model_trace",
  "preview_model_candidates",
  "route_model_task",
  "record_orchestrator_signal",
  "record_model_outcome",
]
default_tools_approval_mode = "prompt"
```

Cette forme suit le transport STDIO et les champs `command`, `args`, délais et
allowlist décrits dans la
[documentation MCP officielle de Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Hermes utilise `/usr/local/libexec/ops-orchestrator-mcp-profile` avec un nom de
profil fixe. Le wrapper adopte le compte technique correspondant, injecte une
identite distincte et une allowlist projet minimale ; il ne reutilise jamais
l'identite Codex. Les huit mappings sont documentes dans
`config/hermes/README.md`.

Quatre outils complètent les cinq outils historiques :
`get_model_catalogue`, `preview_model_candidates`, `get_model_performance` et
`record_model_outcome`. Les trois premiers donnent les cartes publiques, une
présélection économique et les résultats agrégés. Le dernier n'accepte qu'une
validation externe rattachée à l'identité exacte d'une route déjà terminée ; il
ne donne au modèle aucun pouvoir d'approbation ou d'exécution.

## Budgets et audit

Chaque appel est réservé dans une transaction SQLite `BEGIN IMMEDIATE` avant le
réseau. Les limites portent sur appels, tokens et coût quotidien, mensuel et par
mission pour le déploiement. Une seconde série de plafonds durs partage appels,
tokens et coût par compte fournisseur, sur la journée, le mois et la tâche
(`route_id`). Les deux séries sont vérifiées et la réservation est insérée dans
la même transaction exclusive : deux déploiements concurrents ne peuvent donc
pas franchir ensemble un plafond de compte. Un timeout distant reste
`uncertain` et conserve sa réservation : il a
pu être facturé. Les prix Qwen/DeepSeek génératifs sont les tarifs catalogue
conservateurs datés dans `orchestrator.json`; les adaptateurs génériques
embedding/rerank exigent encore leurs variables de prix, faute de quoi ils sont
indisponibles.

Au premier démarrage de cette version, le schéma SQLite V1 est migré
transactionnellement en V2. Les réservations historiques sont rattachées au
compte déclaré ; une suppression ou une modification ultérieure de cette
liaison échoue fermée afin d'empêcher la remise à zéro implicite d'un budget.

L'API budget fournit l'usage et les limites par déploiement et par
`provider_account_id`; ce dernier est marqué `hard_shared_cap=true` et
`enforcement_scope=provider_account_atomic`. Le budget
interne restant est un garde-fou du ledger local, jamais le solde monétaire
réel du compte chez le fournisseur. Ce dernier reste inconnu tant qu'une API de
facturation officielle et authentifiée ne l'a pas confirmé. Le
provider `alibaba-deepseek-ops-api` dispose de 1 USD par jour, 15 USD par mois
et 0,25 USD par mission. Le compte partagé Alibaba est en plus borné à 4 USD
par jour, 50 USD par mois et 1 USD par tâche. Le compte DeepSeek direct est
borné séparément à 3 USD par jour, 40 USD par mois et 1,50 USD par tâche. Des
plafonds de facturation définis chez le fournisseur restent recommandés.

Les soldes fournisseur sont une couche distincte du ledger. Les trois
connecteurs directs revus (DeepSeek, Moonshot/Kimi et StepFun) s'exécutent dans
un worker stateless sous l'UID/GID dédié `opsfinance`, derrière
`/run/ops-orchestrator-finance/api.sock`. Le daemon de
routage charge uniquement les clés Alibaba/Qwen et DeepSeek ; le daemon finance
charge uniquement DeepSeek, Moonshot et StepFun ; la façade Hermes charge les
deux clés des comptes réellement utilisés et son token local ; le client du
timer ne charge aucun credential. Le proxy Unix est `0660` dans un répertoire
`0750` appartenant à `opsfinance`, vérifie `SO_PEERCRED` contre l'UID
`opsorchestrator`, borne les corps et revalide intégralement les réponses
normalisées. Le worker n'accède pas à SQLite : le daemon principal persiste
seulement les champs normalisés revalidés, avec son propre UUID et horodatage.
La façade partage volontairement avec le daemon principal le domaine de
confiance Qwen/DeepSeek et le ledger ; son unité lui rend toutefois le chemin du
socket finance inaccessible. Cette séparation n'est donc pas présentée comme
une frontière forte entre façade et daemon principal.

La table `model_trace` reconstitue la chaîne Utility → Ops standard → Reasoning
→ Premium → contrôle déterministe/humain. Chaque événement contient rôle, fournisseur, modèle, raison,
confiance, tokens et coût. Les événements sont liés par SHA-256 par mission ; la
commande `trace` vérifie la chaîne avant de la rendre. `model_result_validations`
conserve une validation externe unique et append-only par route, avec catégorie,
succès, qualité, corrections et type de preuve. `adaptive_score_audit` conserve
pour chaque candidat le coût brut, l'historique utilisé, la qualité postérieure,
la médiane de latence, les deux ajustements et le rang. Les appels du second
modèle sont persistés sous le rôle comptable `ROLE_VERIFIER`, afin que leur
latence et leurs tentatives ne polluent pas l'historique d'exécution normal.
Les événements `VERIFICATION_START`, `VERIFICATION_RESULT` et
`VERIFICATION_FAILURE` rendent chaque contrôle et fallback technique auditable.
Ces résultats ne sont pas des approbations : les approbations durables restent
exclusivement dans `ops-broker` et Zulip.

L'algorithme `adaptive-quality-latency-v2` part d'un prior de qualité 750/1000,
attend au moins cinq validations ou huit tentatives pour la qualité, et au
moins cinq appels terminés pour la latence. Il ne lit que les 500 observations
les plus récentes de la même catégorie/modèle. La latence est la médiane du
temps entre réservation et achèvement du seul appel fournisseur, bornée à
300 secondes ; les cibles revues sont 12 s (`ROUTINE`), 8 s (`NORMAL`), 4 s
(`URGENT`) et 2 s (`EMERGENCY`). Son poids maximal augmente avec l'urgence,
mais l'ajustement qualité + latence final reste borné à ±15 %. Il réordonne
uniquement les fournisseurs du palier de capacité déjà choisi et ne modifie
jamais `model-catalog.v2.json`. Sans cinq mesures, la latence est visible mais
ne change pas le score. `GET /v1/model-performance` publie les constantes, les
caps de compte et les agrégats observés ; la commande `trace` publie le calcul
exact appliqué à une route.

Le timer de métriques publie notamment : missions reçues et résultats, appels
par rôle/modèle/fournisseur, escalades, latence, tokens, coûts, blocages de
budget de déploiement et de compte, validations par catégorie, erreurs d'outils
et recherches mémoire dans
`/var/lib/node-exporter/textfile/ops-orchestrator.prom`.

## Activation sans secret en clair

L'installation préserve les fichiers de configuration existants par défaut :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh
```

Cette préservation signifie qu'une installation déjà configurée local-first ne
bascule pas implicitement. Après validation du changement de classe C, la
migration explicite installe les defaults API-first et crée une sauvegarde
privée de l'ancienne configuration :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh \
  --replace-api-config
```

Le chemin de sauvegarde est affiché par l'installateur et sert au rollback.
Réinstaller le binaire sans cette option ne modifie pas `/etc`.

`/etc/ops-orchestrator/orchestrator.env` ne doit contenir que les drapeaux,
endpoints et paramètres optionnels validés. Ne jamais y placer de clé API ni de
chemin contournant les credentials systemd. Le résolveur OpenBao utilise un
AppRole propre limité à 23 usages : 22 lectures exactes (21 emplacements de clés
fournisseur et le token de façade Hermes), puis `revoke-self`. Les objets
fournisseur absents sont optionnels. Il publie le bundle uniquement après
révocation, dans des fichiers root-only sous
`/run/ops-orchestrator-secrets`.

Le service orchestrateur reçoit douze emplacements fournisseur optionnels
strictement nommés : Qwen/Alibaba, DeepSeek direct, Z.ai, Mistral, MiniMax,
Google, Cohere, Moonshot, Tencent, xAI, OpenAI et Anthropic. Ils passent par
`LoadCredential=` et les chemins `*_API_KEY_FILE` sous `%d`; le service ne peut
pas parcourir directement le répertoire root-only. Chaque emplacement possède
un fallback vide `SetCredential=` : une clé absente ne bloque pas le démarrage,
et le lecteur borné la refuse ensuite comme provider indisponible. DeepSeek
hébergé par Alibaba réutilise le fichier Qwen : aucun troisième credential n'est
résolu ni chargé. Le token de façade reste réservé à l'unité correspondante et
n'est pas chargé dans le processus orchestrateur.

L'installation reste dormante et ne crée aucun secret. La cérémonie, soumise à
un changement de classe C, s'effectue interactivement après l'installation des
actifs :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh
sudo /usr/local/sbin/provision-orchestrator-openbao
sudo /home/ops-user/ops-control-plane/scripts/install-orchestrator.sh \
  --replace-api-config --enable
```

Les dépendances Python de l'orchestrateur sont une fermeture de versions
exactes : l'installateur refuse les sdists (`--only-binary`), désactive la
résolution transitive (`--no-deps`), vérifie chaque version installée et exécute
`pip check`. Les wheels restent récupérées sur le réseau et ne sont pas encore
liées par hash au manifeste de release : cette garantie ne doit donc pas être
décrite comme une installation hors ligne ou byte-for-byte reproductible.
Hermes est plus strict côté dépendances : son commit exact contient `uv.lock`,
le wrapper exige `uv 0.12.0` puis force un `uv sync --extra all --locked` avec
les hashes du lock. Son clone et les artefacts verrouillés nécessitent encore
un accès réseau ; le bootstrap de l'outil `uv` est versionné mais son script de
téléchargement TLS n'est pas un artefact local manifesté.

Le provisionneur n'accepte aucune clé en argument, ne l'affiche pas et ne
persiste localement que le RoleID et le SecretID chiffrés `host+tpm2`. Toute
lecture, révocation ou publication incomplète maintient l'orchestrateur arrêté.

Après une rotation effectuée depuis Atlas, le helper desktop attend la
révocation confirmée du token humain puis appelle par une règle sudoers exacte
le programme root sans argument `ops-model-credentials-reload`. Ce programme ne
lit aucune valeur de clé et ne contacte aucun fournisseur : il prend les
verrous du déploiement et de l'installation, relance le résolveur OpenBao, puis
contrôle uniquement les métadonnées des fichiers publiés. Une clé Qwen ou
DeepSeek active automatiquement la façade et Hermes ; une clé financière revue
active le timer de solde. En leur absence, ces unités restent désactivées. Le
programme vérifie ensuite les endpoints locaux et n'émet aucun canari
fournisseur. Atlas ne reçoit le statut `rotated` qu'une fois ce rechargement
réussi.

Chaque fournisseur API exige son drapeau d'activation, un endpoint résolu et
un fichier de clé privé avant le premier appel. Toutes les clés fournisseur,
y compris Qwen et DeepSeek dans les unités principale, financière et façade,
sont optionnelles au démarrage : leur fallback vide maintient le control plane
local sain, mais le lecteur de secrets le classe `credential_unavailable` et
n'émet aucun accès fournisseur. Moonshot et StepFun restent en plus confinés au
seul worker financier. Après démarrage, un endpoint, un drapeau ou une clé
absente rend uniquement le provider concerné indisponible et le health reste
`degraded`. L'activation
Alibaba/Qwen commande ensemble Qwen et DeepSeek hébergé sur ce workspace ;
l'activation de l'API DeepSeek directe reste indépendante.

Le champ `available` du health vérifie localement la configuration et le
fichier secret uniquement : `provider_availability_scope` vaut
`configuration-and-secret-only` et `provider_network_probe` vaut `false`. Il
ne prouve ni le DNS, ni TLS, ni l'authentification, ni l'acceptation du modèle
par le fournisseur. Seul un appel canari distant, explicitement autorisé et
borné en coût, valide ce chemin ; un échec se replie sans exposer le corps de
réponse.

Chaque fournisseur possède des plafonds d'appels, de tokens et de coût ; le
plafond demandé par la requête s'ajoute à ces budgets durs. Un appel distant
n'est autorisé que si la requête porte explicitement `remote_allowed=true`.
Les embeddings et le reranking restent des API facultatives. La mémoire locale
par hash et recherche lexicale ne réalise aucune inférence de modèle, ne coûte
aucun token API et reste inchangée.

La gateway Hermes appelle uniquement l'alias `qwen-coordinator` sur la façade
loopback. Cette façade réserve et réconcilie chaque appel Qwen Flash dans le
même ledger ; les besoins non triviaux sont remis à `route_model_task` pour
l'escalade Qwen/DeepSeek. Aucune clé fournisseur n'entre dans le processus
Hermes, dont l'egress systemd est limité à loopback. Le client ne choisit pas
l'identité budgétaire : tous les appels Hermes partagent une fenêtre de mission
persistante de 0,05 USD par journée UTC, en plus des plafonds jour/mois du
provider. Changer le champ OpenAI `user` ou redémarrer la façade ne remet donc
pas ce compteur à zéro. L'identifiant interne
`hermes/coordinator/utc-day/<date>` contient `/`, interdit par la grammaire des
`mission_id` de l'API/MCP : une route ordinaire ne peut pas préconsommer le
bucket de mission réservé à Hermes.

Après validation des modèles, tarifs, budgets et credentials :

```bash
sudo systemctl enable --now ops-orchestrator.service
sudo systemctl enable --now ops-orchestrator-metrics.timer
ops-orchestrator health
```

Le daemon rend le snapshot Prometheus depuis sa base SQLite privée, puis
`ops-orchestrator-metrics.service` le récupère uniquement via le socket Unix et
l'écrit atomiquement dans le textfile collector. Le publisher n'a aucun accès
au répertoire d'état, aucune capability et ne contacte aucun fournisseur IA.

L'activation d'une API n'engendre aucun appel par elle-même. Toute API absente,
mal tarifée, sans clé privée, hors budget ou en timeout est ignorée au profit
des règles déterministes ou d'une escalade humaine.
