# Déploiement atomique du control plane local

Ce paquet relie Atlas, l’orchestrateur multi-modèles API-first, Hermes et le
Zulip local à un seul workflow borné. Il ne déploie rien par sa présence dans
le dépôt. La release `.11` conserve le bootstrap fresh-only pour une machine
neuve et ajoute uniquement la reprise corrective bornée de l’échec `.10`
identifié par ses cinq empreintes exactes. Cette reprise n’est ni une mise à
jour générique ni une seconde tentative libre : elle n’accepte que le WAL `.10`
terminal après rollback, puis consomme un marqueur correctif distinct. Elle
exige toujours Docker, Atlas et les données Zulip absents et ne publie ni
commande d’application ni rollback après commit. Le seul acteur supervisé prévu est
`codex-supervised`. L’unique transaction d’amorçage local est explicitement
autorisée dans `AGENTS.md`. Après sa consommation, toute nouvelle action de
classe C (mise à jour ou rollback produit compris) devra utiliser un runbook v2
séparé, approuvé depuis le Zulip alors disponible.

## Propriétés garanties

- aucune commande ou chemin libre n’est transmis au worker root ;
- le dispatcher public ne connaît que le statut en lecture ; le worker refuse
  `preflight`, `apply` et `rollback` en CLI. Ses primitives mutatrices sont
  appelées uniquement par le bootstrap root scellé après consentement ;
- la release est copiée depuis les sources dans un répertoire root-only, puis
  son empreinte `sha256-tree-v1` est recalculée avant toute exécution ;
- cette empreinte couvre aussi le worker root, son dispatcher, son sudoers, ses
  seul runbook de statut et ses unités systemd. Elle couvre en plus l'intégralité des
  sources, helpers, unités et dépendances du broker, tous les runbooks et
  `policies/actions.yaml` que `install-broker.sh` peut copier. Seul le manifeste
  porteur de l'empreinte est exclu afin d'éviter une auto-référence impossible ;
- la reprise `.10` vers `.11` exige le bloc d’autorité correctif exact dans
  `AGENTS.md`, conserve sans réécriture le marqueur, le WAL et l’audit `.10`
  sous `/var/lib/ops-control-plane-bootstrap-history/`, et publie son propre
  marqueur par création exclusive avant de transférer atomiquement l’état ;
- avant cette publication, seules les unités et le helper correctifs revus
  peuvent être installés. Leur watcher est inerte sans marqueur ; le helper
  vérifie aussi les fragments réellement chargés par systemd et n’accepte que
  le drop-in global Fedora déjà épinglé pour l’unité de service (aucun pour
  l’unité de chemin). Après interruption, le même WAL est rejoué sous le même verrou de
  déploiement, sans autoriser une seconde correction ni une reprise « fresh » ;
- le RPM Atlas et les cinq images Zulip sont épinglés par SHA-256 ;
- Docker rootful doit être entièrement absent, puis est installé depuis les seuls dépôts Fedora
  `fedora`/`updates` avec les NEVRA revus `moby-engine-0:29.7.2-1.fc44.x86_64`,
  `docker-cli-0:29.7.2-1.fc44.x86_64` et
  `docker-compose-0:5.5.0-1.fc44.x86_64`. La résolution sans dépendances
  faibles est elle aussi bornée, notamment à `containerd-0:2.3.4-1.fc44` et
  `runc-2:1.5.1-1.fc44`. Les 31 dépendances revues, y compris celles d’un
  Fedora minimal (`systemd`, SELinux, util-linux et leurs bibliothèques), sont
  toutes inscrites au manifeste. `container-selinux`, `iptables-nft` et
  `nftables` doivent déjà être présents et intègres ; la résolution des huit
  RPM manquants est téléchargée dans le backup
  privé, chaque signature RPM est vérifiée et tout NEVRA hors allowlist est
  refusé avant l’installation locale avec les dépôts désactivés ;
- Hermes est épinglé par version, tag et commit. Son export GitHub au commit
  exact est fourni comme artefact local. Avant consentement, Atlas vérifie le
  fichier régulier, sa taille, son SHA-256 et sa liaison au commit. Après le
  marqueur one-shot, mais avant toute extraction ou mutation de production, le
  worker vérifie en plus l’inventaire borné et le SHA-256 canonique des 10 925
  fichiers, puis répète cette validation intégrale sur la copie de staging ;
- la configuration de l’orchestrateur porte explicitement le mode
  `api-first` ; l’état d’`ollama.service` est capturé, puis l’unité est arrêtée
  et désactivée avant activation. Un rollback restaure exactement ses états
  actif/activé antérieurs sans supprimer modèle, journal ni autre preuve ;
- le runbook normal ne reçoit les AppRole que par leurs blobs
  `systemd-creds`. L’amorçage one-shot peut créer les 14 références locales,
  mais les secrets passent uniquement par des pipes anonymes et des escrows
  `systemd-creds` root-only ; ils ne sont ni affichés ni journalisés ;
- le daemon de soldes tourne sous l’identité dédiée verrouillée `opsfinance`,
  sans StateDirectory. Son socket `0660 opsfinance:opsfinance` n’accepte que le
  client `opsorchestrator`; le healthcheck adopte précisément cette identité au
  lieu de contourner le contrôle SO_PEERCRED avec root. La présence antérieure
  de cette identité est enregistrée et le compte/groupe ne sont supprimés au
  rollback que s’ils ont été créés par cette transaction et restent conformes
  au profil dédié ;
- le manifeste fixe les scopes : l’orchestrateur reçoit Qwen et DeepSeek ainsi
  que dix credentials catalogue optionnels (Z-AI, Mistral AI, MiniMax, Google,
  Cohere, Moonshot, Tencent, xAI, OpenAI et Anthropic) ; le daemon finance ne
  reçoit que DeepSeek+Moonshot+StepFun, son timer aucune clé, et la façade
  Hermes seulement Qwen+DeepSeek+son token local ;
- l’état précédent est capturé avant installation. Tout échec d’installation
  ou de santé déclenche le rollback automatique préautorisé par l’approbation
  du déploiement ;
- le proxy Unix Alertmanager est activé à la demande et s’arrête après 30
  secondes sans client. Son bit `active` n’est donc jamais utilisé comme
  preuve durable : le bootstrap refuse un préétat déjà actif qu’il ne pourrait
  pas reproduire, le rollback restaure seulement son enablement, et la santé
  exige le socket `0660 root:zulipbridge` activé ainsi qu’un vrai GET local
  réussi vers `/api/v2/alerts` ;
- les réseaux Docker utilisent les noms et bridges fixes `zulip-local_backend`
  / `zulip-backend` et `zulip-local_frontend` / `zulip-frontend`, sans
  masquerade ni DNS externe. Des règles INPUT/FORWARD persistantes isolent les
  conteneurs, puis la recette redémarre Docker, recharge firewalld, attend son
  callback Moby et revalide inventaire, DNS interne/externe et HTTPS local. Le
  health périodique réévalue aussi les routes et DNS effectifs apparus après le
  déploiement et arrête la pile en cas de collision ;
- jusqu’au commit global durable, les cinq conteneurs portent une politique de
  redémarrage `no`. Le finaliseur post-commit, rejouable après mise à jour
  partielle, arme seulement alors `unless-stopped` ;
- le journal de transaction est fsync avant la première quiescence ; après une
  interruption ou un redémarrage, la phase enregistrée permet de restaurer les
  services sans jamais extraire une archive encore partielle. Cette reprise
  automatique précède les préconditions et le secret one-shot d'un nouveau
  déploiement, afin qu'une entrée bootstrap absente ne puisse pas bloquer le
  rollback automatique déjà autorisé. Après le point de commit global, la
  reprise ne fait que terminer le nettoyage et ne rétrograde jamais en rollback.

Le manifeste autoritatif est
[`release-manifest.v1.json`](release-manifest.v1.json), validé à la fois par le
worker strict et par
[`release-manifest.v1.schema.json`](release-manifest.v1.schema.json). Une modification d’un
fichier inclus, du RPM, d’une version ou d’un digest exige un nouvel identifiant
de release et une nouvelle approbation. Le worker refuse un contenu différent,
un symlink, un fichier spécial, une architecture autre que x86_64, un Docker
rootless ou un RPM Atlas d’une autre identité.

Les blocs lisibles `components.model_catalogue` et
`components.provider_integrations` figent aussi le schéma, la révision,
l'empreinte et les comptes des deux documents métier. Le premier publie la
recette runtime vérifiée : 58 cartes et 21 comptes, dont 5 cartes statiques
reliées à 7 déploiements, 9 cartes dynamiques et donc 14 cartes invocables. Les
44 autres restent explicitement bloquées : 28 faute de tarifs officiels, 11 en
quarantaine, 4 faute d'identifiant modèle vérifié et 1 faute de contrat Chat
MiniMax M3 vérifié. Le worker ne fait pas confiance à ces compteurs seuls : il
les recalcule dans un sous-processus isolé avec la logique canonique
`expand_catalogue_runtime` + `ModelCatalogue.load`, sans clé ni appel réseau,
et refuse tout écart avant installation.

Le paquet Atlas local n’est pas signé par une clé RPM de publication. Dans ce
profil mono-machine il est donc accepté uniquement après copie protégée,
vérification de son SHA-256 exact et contrôle de son identité RPM ; tout autre
octet est refusé avant `dnf`.

Lorsque le paquet n'est pas encore installé, le seul premier amorçage graphique
admis est `scripts/enroll-atlas-trust-anchor`. Il capture dans des memfd scellés
les octets confirmés, publie atomiquement une ancre root-owned versionnée via la
boîte Polkit, puis exécute son `launch-atlas-reviewed-rpm`. Les lancements
suivants utilisent directement cette ancre sans relire ni réexécuter le
checkout. Le lanceur lie le RPM et son exécutable aux
deux empreintes du manifeste, matérialise le payload uniquement dans un memfd
`atlas-reviewed-rpm` en mode `0500` avec les quatre scellés, puis exécute le
binaire Rust/Tauri exact. Le helper privilégié revalide indépendamment cette
ascendance et le même payload avant d'annoncer le canal de secrets prêt.

## Limites de reproductibilité et dépendances réseau

Le manifeste distingue explicitement version verrouillée et artefact
reproductible octet pour octet. Les closures Python de l’orchestrateur (29
distributions) et du broker (32) ont une liste et une empreinte exactes ; les
installateurs imposent `--no-deps`, `--only-binary=:all:`, revérifient chaque
version avec `importlib.metadata`, puis exécutent `pip check`. Cependant ces
listes ne contiennent pas les hashes des wheels : elles sont téléchargées du
réseau et ne constituent ni un bundle offline ni une garantie byte-for-byte.

Hermes reste épinglé au commit déclaré, exige le `uv.lock` régulier de ce commit,
utilise `uv 0.12.0` et termine toujours par `uv sync --extra all --locked`. Sa
source ne dépend plus de Smart Git pendant le préflight ou l’installation :
l’export local est vérifié par SHA-256, inventaire et arbre canonique, puis
extrait sans lien ni chemin spécial. Le lock porte les transitives et leurs
hashes, mais les artefacts uv et le script bootstrap uv nécessitent encore le
réseau ; ce dernier n’est pas lui-même un bundle local. Une indisponibilité ou
une divergence fait donc échouer l’installation et déclenche le rollback, sans
transformer ce workflow en build hermétique. Docker repose de même sur des RPM
Fedora signés et des NEVRA exacts, résolus au moment de l’action.

L'audit Rust final d'Atlas se termine avec le code zéro et ne relève aucune
vulnérabilité connue bloquante, mais il ne signifie pas « zéro advisory » : au
8 septembre 2026, il signale 7 avertissements transitifs, dont 6 crates non
maintenues de la pile GTK3/Tauri et `RUSTSEC-2024-0429` sur `glib 0.18.5`,
classé `unsound`. Aucune
correction directe n'existe dans le code de cette application ; la suppression
de ces avertissements dépend d'une migration de la pile amont et reste une
limite connue de cette release.

## Ordre d’installation

Après le preflight pur, l’affichage des quatre empreintes et le consentement
physique, le bootstrap scellé appelle les primitives du worker depuis la release
root-only :

1. copie et gèle la release vérifiée sous
   `/var/lib/ops-control-plane-deployment/releases/` ;
2. capture les unités, configurations et états locaux sans inclure
   `/etc/credstore.encrypted`, les données OpenBao ni une clé fournisseur ;
3. confirme le baseline fresh-only (Docker, Atlas et les trois racines Zulip
   absents) ; en mode correctif, il vérifie en plus l’identité, le rollback et
   l’audit `.10`, archive ces preuves sans les altérer, puis arrête les
   consommateurs, timers et résolveurs de secrets ;
4. arrête et désactive `ollama.service` si elle existe, tout en conservant son
   état antérieur dans la transaction pour un rollback réversible ;
5. résout Docker rootful, vérifie que le bundle signé est
   exactement l’allowlist revue, l’installe dépôts désactivés, puis enregistre
   l’ID exact de la transaction DNF et démarre le daemon ;
6. installe l’orchestrateur API-first, le RPM Atlas et Hermes en mode dormant ;
7. installe et initialise Zulip 12.2 à partir des images par digest, réconcilie
   realm, propriétaire, bot et canaux, puis active les timers locaux ;
8. active le daemon de soldes isolé, l’orchestrateur et ses métriques. En mode
   d’amorçage sans clé, le timer finance, la façade et Hermes restent désactivés
   jusqu’à la publication ultérieure d’une clé compatible par Atlas ;
9. installe et active le bridge d’approbation Zulip ;
10. vérifie Docker/Compose, l’absence d’inférence locale active/activée, les
   unités, l’inventaire exact des 19 déploiements distants, les API Unix de
   l’orchestrateur, de la finance et du broker, le proxy Unix Alertmanager par
   une requête réelle, l’HTTPS Zulip et l’intégrité du RPM Atlas. Les 21
   namespaces fournisseur doivent être absents dans OpenBao et aucun canari ni
   appel fournisseur n’est effectué.

Le mot de passe propriétaire Zulip est saisi localement par le bootstrap puis
transmis au provisionneur par un pipe anonyme borné ; il n’est écrit ni dans
argv, ni dans l’environnement, ni sur un stockage persistant.

## Installation du runbook dans le broker

`scripts/install-broker.sh` installe atomiquement :

- le dispatcher root allowlisté et son sudoers exact ;
- le worker et le manifeste root-owned ;
- aucune unité d’application ou de rollback public. Les trois unités temporaires
  de reprise du bootstrap sont installées et armées seulement pendant cette
  transaction. Les deux gardes et
  toutes les barrières sont supprimées à la finalisation ; l’activateur/janitor
  et le helper root-only restent inertes, hashés et désactivés après le WAL
  terminal afin que leur propre nettoyage reste rejouable ;
- le seul runbook public `local.control-plane-deployment-status.v1` ;
- les répertoires privés déclarés par tmpfiles.

Le remplacement des politiques déjà installées reste une opération de
production séparée. Il doit donc être effectué uniquement par le workflow de
classe C déjà autorisé pour mettre à jour le broker, jamais par une commande
root improvisée. La présence des fichiers dans ce dépôt ne constitue ni cette
autorisation ni une preuve de déploiement.

## Interface après bootstrap

Cette release n’expose que `local.control-plane-deployment-status.v1`. Sa sortie
JSON bornée contient le statut, la release, la transaction et l’état d’inférence
locale. Les opérations post-commit sont volontairement absentes : un futur
runbook v2 devra définir son propre backup OpenBao, sa preuve et son approbation
Zulip avant toute mutation.

## Préconditions non secrètes

- Fedora 44 x86_64, OpenBao local non scellé, broker Unix privé actif ;
- Docker, son groupe, ses unités, ses racines état/configuration, ses réseaux et
  les zones/politiques firewalld Moby absents ; Atlas et ses trois payloads RPM
  absents ; les trois racines Zulip absentes. Seules les trois unités legacy
  bridge exactes, dormantes et hashées sont tolérées puis remplacées dans la
  transaction ;
- dépôts Fedora signés `fedora` et `updates` capables de fournir les NEVRA
  Docker épinglés ; toute installation partielle, étrangère ou d’une autre
  version est refusée ;
- archive source Hermes exacte au chemin déclaré ; accès réseau aux indexes
  Python, aux artefacts uv et aux registries des images. Le preflight vérifie
  son statut de fichier régulier, sa taille, son SHA-256 et sa liaison au commit,
  ainsi que les locks et politiques locales. Après le marqueur one-shot, mais
  avant extraction ou mutation de production, le worker vérifie l’inventaire et
  l’arbre canonique complets. Aucun de ces contrôles ne garantit la disponibilité
  future d’un artefact distant de dépendance ;
- au moins 5 Gio libres dans les racines état et backup ;
- le RPM Atlas exact au chemin déclaré ; le bootstrap initial crée les 14
  références AppRole locales et n’exige aucune clé fournisseur ;
- les sous-réseaux fixes `172.30.10.0/24` et `172.30.11.0/24` sans collision
  avec route, adresse ou serveur DNS effectif de l’hôte.

La présence d’Ollama n’est pas une erreur de preflight : elle est signalée puis
neutralisée par le cutover approuvé. Seuls ses états systemd sont modifiés ; ses
modèles locaux, caches et historiques ne font pas partie des chemins de
suppression ou de sauvegarde de ce paquet.

Tout Atlas ou Docker préexistant fait échouer le preflight avant consommation.
Le rollback automatique précommit arrête la pile, désactive Docker, répare
offline toute transaction RPM interrompue puis retire exactement les paquets
attribuables. Il déplace auparavant l’intégralité de
`/var/lib/docker/volumes` vers le répertoire privé
`/var/backups/zulip-local/recovered-<transaction_id>/docker-volumes` ; ces
données et tout snapshot chiffré achevé restent des preuves récupérables et ne
sont jamais supprimés automatiquement.

## Accès local après recette

L’interface Zulip est disponible uniquement sur l’hôte à
<https://zulip.ops.local:8443>. Elle n’écoute pas le LAN. Atlas apparaît comme
l’application desktop « Modèles IA ». L’orchestrateur reste sur son socket Unix
privé et la façade Hermes sur `127.0.0.1:8643` ; aucune API de gestion ou clé
fournisseur n’est exposée sur le réseau.

## Vérification hors production

Les vérifications de source ne lancent aucune unité :

```bash
broker/.venv/bin/python -m pytest -q tests/test_control_plane_bootstrap.py
broker/.venv/bin/python -m pytest -q tests/test_control_plane_deployment.py
broker/.venv/bin/python -m pytest -q broker/tests/test_runbook_registry.py \
  broker/tests/test_deploy_assets.py
deploy/control-plane/bin/control-plane-deployment-worker source-digest
```

La dernière commande doit produire exactement `source.tree_sha256`. Après toute
modification incluse, reconstruire Atlas, incrémenter `release_id`, mettre à
jour les deux SHA-256 avec une modification revue, puis relancer tous les tests.

## Bootstrap initial et correction bornée

L’exception d’amorçage déclarée par le propriétaire dans `AGENTS.md` résout le
cas où Zulip, son compte propriétaire et le runbook n’existent pas encore. Elle
ne s’applique qu’à cette release, uniquement sur la session physique locale de
`ops-user`, avec authentification Polkit/root native et confirmation explicite.
Le chemin normal est l’assistant graphique Atlas. Pour une machine neuve il
appelle les deux opérations initiales. Pour tout état de reprise, l'interface
appelle uniquement les opérations neutres; le helper root détermine ensuite si
l'état concret est `fresh` ou `corrective` :

```text
control-plane-bootstrap gui-preflight
control-plane-bootstrap gui-apply
control-plane-bootstrap gui-corrective-preflight
control-plane-bootstrap gui-corrective-apply
control-plane-bootstrap gui-recovery-preflight
control-plane-bootstrap gui-recovery-apply
```

Le lanceur refuse un appel GUI root ou sans ascendance Atlas exacte. Il lie le
PID, le starttime, l’inode et le SHA-256 du binaire Atlas aux deux pipes anonymes
privés conservés à travers `pkexec` et le memfd scellé. La sortie est un JSONL
borné sans secret. Les entrées `gui-apply`, `gui-corrective-apply` et
`gui-recovery-apply` utilisent
une trame binaire `ATLASBOOT1`
contenant d’abord la release et le suffixe de confirmation non secrets, puis
le mode, l’identité `.10` en mode correctif et les deux mots de passe bornés.
Aucun mot de passe ne traverse argv,
l’environnement, un fichier ou un journal. `ATLASCTL1/CANCEL` reste accepté
jusqu’à l’événement JSONL `mutation-started`; cet événement est écrit et vidé
immédiatement avant le WAL one-shot. La fermeture prématurée du pipe annule en
échec sûr.

Le mode terminal historique reste disponible uniquement pour le bootstrap
initial. La correction est volontairement limitée au parcours graphique qui
affiche et lie les deux identités de release :

```bash
deploy/control-plane/bin/control-plane-bootstrap preflight
deploy/control-plane/bin/control-plane-bootstrap apply
```

Le premier enrôlement lie les cinq snapshots scellés au digest affiché, puis le
processus root publie l'ancre versionnée sans rouvrir le checkout. À chaque
opération, Atlas copie le helper root-owned dans un memfd scellé. Avant
consentement, il traite le manifeste et le worker
comme des données, recalcule le SHA du manifeste, de l’arbre et du RPM, les
présente intégralement dans Atlas et vérifie localement le fichier régulier de
l’archive Hermes liée au commit, sa taille et son SHA-256. L’inventaire et
l’arbre canonique sont vérifiés par le worker après le marqueur one-shot, mais
avant extraction ou mutation de production, puis à nouveau sur la copie de
staging. Atlas exige une confirmation liée à `release_id` et au suffixe des
quatre empreintes helper/manifeste/source/archive. Le mode terminal exige la phrase courte
`DEPLOY <release_id> <suffixe-12-hex>`. Ces empreintes garantissent la cohérence
du lot accepté selon une frontière TOFU locale ; elles ne constituent pas une
signature de provenance. Aucun DNF, installation ou écriture durable hors
`/run` n’est lancé avant cette confirmation (les traces normales de Polkit et
du journal système restent celles du système). Le lanceur maintient en plus un
inhibiteur local `idle:sleep:shutdown` pendant toute l’opération longue.

Le marqueur one-shot est le WAL initial complet, publié par inode temporaire
fsync puis `linkat` sans remplacement. La même identité de release peut reprendre
un enrôlement interrompu tant qu’aucune mutation de production n’a commencé ;
elle ne redonne jamais une nouvelle autorité et toute autre identité échoue.
Le helper installe ensuite depuis le snapshot root-only vérifié trois unités de
reprise : garde OpenBao, rollback fichiers/OpenBao, puis réactivation différée
des seuls services qui étaient actifs. La première étape enfile sans attendre
les états start/stop exacts ; la troisième unité est ordonnée après toutes ces
unités et vérifie leur matrice actif/activé avant de rendre le rollback
terminal. Leurs fichiers, barrières systemd et
liens d’activation sont vérifiés et fsync avant que la transaction soit déclarée
armée. Tous les descendants mutateurs héritent du même verrou OFD ; une
interruption les termine ou empêche la reprise de courir en parallèle.
L’ordre d’enrôlement est lui aussi crash-sûr : helper et trois unités sont
d’abord synchronisés, puis le déclencheur de rollback et son janitor sont
activés et synchronisés ; les barrières des services et la garde OpenBao ne
sont publiées qu’après l’existence de cette ancre de démarrage durable.

Pour récupérer l’accès humain OpenBao sans réinitialiser, rekeyer ou effacer le
stockage, le helper ouvre temporairement un listener Unix `0600` privé dans
`/var/lib/openbao`, tout en laissant l’ancien endpoint TCP non authentifié
désactivé. Il utilise exactement les deux shares `systemd-creds` déjà présentes
(seuil 2), conserve OTP/token uniquement en mémoire ou escrow chiffré, remplace
le mot de passe userpass `ops-user`, puis révoque les tokens root générés. Le
nouveau mot de passe est lui-même conservé temporairement dans un escrow
`systemd-creds` lié à l’hôte et au TPM avant le premier PUT : après une coupure,
la reprise peut rejouer reset et login avec un token humain limité à deux
minutes, fixer ensuite les TTL finaux, puis supprimer durablement cet escrow. Ce
nouveau mot de passe humain est une récupération de compte volontairement
persistante même si le reste est rollbacké ; les données OpenBao ne sont jamais
réinitialisées. L’état géré OpenBao est capturé avant/après et le rollback ne le
modifie que si le post-état correspond encore exactement, sinon il échoue fermé
sans écraser une modification concurrente.

Les trois escrows utilisent des emplacements de staging fixes et un inventaire
fermé. La reprise promeut uniquement une progression root monotone portant le
même nonce et la même époque ; un staging userpass antérieur au premier PUT est
validé puis supprimé, tandis qu’un staging undo/userpass déjà lié au WAL est
promu et rejoué. Tout ancien nom aléatoire ou fichier inattendu fait échouer la
reprise sans effacer la preuve chiffrée.

OpenBao 2.6 peut créer un root token avant que la dernière réponse HTTP soit
reçue. Sous la frontière de maintenance locale exclusive du socket Unix, le
WAL borne chaque fenêtre possible à 10 secondes, autorise au plus un remint et
réconcilie seulement les accessors root au profil exact créés dans ces fenêtres.
Une seconde réponse finale ambiguë arrête la reprise en échec fermé en conservant
escrow, garde et preuves pour récupération manuelle ; elle ne boucle jamais et
ne révoque aucun token ne correspondant pas au profil et aux fenêtres.

Les timers de vérification et de sauvegarde du broker sont inclus dans le même
snapshot actif/activé, arrêtés avant toute archive et restaurés depuis l’état
extérieur à la sauvegarde. Leurs oneshots et services de métriques doivent être
inactifs à la dernière frontière précédant la consommation. Le fichier de
métrique d’une sauvegarde broker réussie et les sauvegardes valides restent des
preuves append-only hors rollback : ils ne sont ni remplacés par une ancienne
preuve ni supprimés automatiquement.

Le bootstrap crée le propriétaire/bot/canaux Zulip, persiste l’identifiant du
propriétaire root-only, réécrit puis vérifie l’unique acteur broker
`zulip:<id>`, et démarre le control plane en état `healthy-degraded` sans clé
fournisseur. Atlas ajoutera ultérieurement les clés ; le helper de recharge
active alors uniquement les consommateurs devenus utilisables. Une fois le
commit et sa preuve durable enregistrés, le bootstrap révoque son autorité,
supprime ses barrières et se désactive. Le helper root-only et l’unité janitor
restent comme fichiers inertes et désactivés après le WAL terminal : conserver
ce dernier ancrage rend le nettoyage rejouable jusqu’à sa propre désactivation,
sans réintroduire une capacité one-shot. Toute action de classe C suivante doit
obligatoirement reprendre le chemin normal d’approbation Zulip ; le bootstrap
consommé ne peut pas être rejoué.

### Réparation locale .14 — migration des anciens rôles OpenBao

Les rôles `deepseek-client` et `hermes-coordinator` encore présents sont inclus
dans le snapshot chiffré du bootstrap. Après ce snapshot, leurs policies sont
placées en quarantaine et les rôles existants sont limités à leur seule policy
de quarantaine, sans policy par défaut. Le provisionneur Hermes conserve ces
rôles en mode transactionnel pour préserver leurs SecretIDs lors d'un rollback.
Le provisionnement manuel conserve son comportement de suppression précédent.
L'historique .13 est archivé avant publication de .14.

La révision .15 corrige la lecture des documents de sauvegarde chiffrés : les
espaces des policies sont conservés, tandis que les parts Shamir gardent leur
validation sans espaces. Le snapshot inutilisé de .14 est archivé avant de
laisser la récupération standard terminer ; le WAL historique reste intact.

La révision .16 distingue les champs de création et de mise à jour des AppRoles
(`local_secret_ids` est vérifié mais n'est jamais réécrit sur un rôle existant).
Les listes vides `null`/`[]` sont normalisées lors du snapshot et de sa
vérification. Des tests isolés avec le binaire OpenBao réel vérifient migration,
interruption et rollback avec préservation des RoleIDs et SecretIDs. L'audit
d'erreur conserve le chemin API géré ainsi que la cause du rollback, sans corps
de réponse. Atlas reçoit la phase d'échec initiale même si le rollback échoue.
